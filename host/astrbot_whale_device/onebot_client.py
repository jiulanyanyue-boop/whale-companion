"""OneBot v11 HTTP API 客户端：主机侧「真实发消息」的唯一出口。

边界（依据 `docs/modules/M03-onebot-qq-development.md`）：

* 本模块**只是 HTTP API 调用方**：`POST /send_private_msg`、`POST /send_group_msg`，
  解析 `{"status": "ok", "retcode": 0, "data": {...}}`。连接参数由 AstrBot 侧提供；
  这里**不实现 QQ 登录、不拉 QQ 协议、不建第二套 QQ 客户端**。
* 消息段一律复用 `onebot.OneBotSegment` / `onebot.OneBotSender` 的既有形状
  （`{"type": ..., "data": {...}}`）。本模块只做「合法性校验 + 序列化」，
  未知段（at / reply / json / mface / node / …）直接抛 `OneBotMediaError`，
  **绝不降级为文本 URL**，并且不会向 OneBot 发出任何请求。
* **一次调用只发一次 HTTP 请求**，失败不自动重发。超时/连接失败抛网络类异常
  （`OneBotTimeoutError` / `OneBotTransportError`，同时是 `TimeoutError` /
  `ConnectionError` 子类），交给 `onebot_lifecycle.OneBotLifecycle` 用**同一个幂等键**
  重试；retcode 非 0、HTTP 4xx/5xx、协议错误都不是网络层故障，不参与重试。
* `access_token` 与密钥同级别：只出现在 `Authorization: Bearer` 头。URL、请求载荷、
  返回值、异常消息、`repr` 与日志里都不会出现明文 token（服务端回显场景按 `***` 脱敏；
  出站内容若意外含 token 直接拒绝发送）。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from .onebot import (
    OUTBOUND_SEGMENT_TYPES,
    URL_PREFIXES,
    OneBotMediaError,
    OneBotSegment,
    OneBotSender,
)

#: OneBot HTTP API 服务地址（AstrBot / NapCat / Lagrange 侧监听）。
DEFAULT_BASE_URL = "http://127.0.0.1:3000"
SEND_PRIVATE_ENDPOINT = "/send_private_msg"
SEND_GROUP_ENDPOINT = "/send_group_msg"
DEFAULT_TIMEOUT_SECONDS = 10.0
#: 脱敏后的替身；token 明文永远不出现在任何输出里。
TOKEN_MASK = "***"
CLIENT_USER_AGENT = "whale-device-onebot-client/1.0"
#: 与 `onebot._ID_SHAPE` 对齐：QQ user_id / group_id 是十进制数字。
_ID_SHAPE = re.compile(r"[0-9]{1,20}")
_ALLOWED_URL_SCHEMES = ("http", "https")
_MAX_DETAIL_CHARS = 512

__all__ = [
    "CLIENT_USER_AGENT",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "OneBotAPIError",
    "OneBotClient",
    "OneBotClientError",
    "OneBotHTTPError",
    "OneBotProtocolError",
    "OneBotTimeoutError",
    "OneBotTransportError",
    "SEND_GROUP_ENDPOINT",
    "SEND_PRIVATE_ENDPOINT",
    "TOKEN_MASK",
]


class OneBotClientError(RuntimeError):
    """OneBot 客户端错误基类（非网络层）。"""


class OneBotTransportError(OneBotClientError, ConnectionError):
    """连接被拒 / 重置 / 不可达等网络层故障；`OneBotLifecycle` 会重试。"""


class OneBotTimeoutError(OneBotTransportError, TimeoutError):
    """请求超时；网络层故障，`OneBotLifecycle` 会重试。"""


class OneBotHTTPError(OneBotClientError):
    """服务端返回了非 2xx HTTP 状态。

    这是服务端**明确应答**（不是网络层瞬时故障），因此刻意不继承
    `OSError`/`ConnectionError`，避免被重试逻辑误判。
    """

    def __init__(self, status: int, detail: str = "") -> None:
        self.status = status
        self.detail = detail
        text = f"onebot http status={status}"
        super().__init__(f"{text}: {detail}" if detail else text)


class OneBotProtocolError(OneBotClientError):
    """响应不是合法 JSON / 形状不符合 OneBot v11 响应契约。"""


class OneBotAPIError(OneBotClientError):
    """应答合法但 `retcode != 0`（或 `status != "ok"`）：**不算成功**，不参与重试。"""

    def __init__(self, retcode: int, status: str = "", detail: str = "") -> None:
        self.retcode = retcode
        self.status = status
        self.detail = detail
        text = f"onebot api retcode={retcode} status={status!r}"
        super().__init__(f"{text}: {detail}" if detail else text)


class OneBotClient:
    """OneBot v11 HTTP API 调用方。

    可直接当作 `OneBotSender` 的平台回调使用（`send_private_message` 的签名是
    `(user_id, message, *, idempotency_key=None)`，与 `OneBotSender._call_platform`
    的关键字调用严格一致），也可以直接传实例本身（实现了 `__call__`）。
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        access_token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Any = None,
    ) -> None:
        token = access_token
        if token is not None:
            if not isinstance(token, str) or not token.strip():
                raise ValueError("access_token must be a non-empty string when configured")
            token = token.strip()
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError("timeout must be a positive number of seconds")
        self._token = token
        self._base_url = self._normalize_base_url(base_url)
        self._timeout = float(timeout)
        # 显式禁用环境代理：OneBot 服务通常在回环地址上，代理会破坏连接且引入不确定性。
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._requests_sent = 0

    # ---- 只读属性 -------------------------------------------------------
    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def timeout(self) -> float:
        return self._timeout

    @property
    def token_configured(self) -> bool:
        return self._token is not None

    @property
    def requests_sent(self) -> int:
        """本实例已发出的 HTTP 请求次数（用于证明「一次调用只发一次」）。"""
        return self._requests_sent

    def __repr__(self) -> str:
        # 绝不回显 token 明文，连长度都不透露。
        token_state = "configured" if self._token is not None else "none"
        return (
            f"OneBotClient(base_url={self._base_url!r}, timeout={self._timeout!r}, "
            f"access_token={token_state})"
        )

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty http(s) URL")
        parts = urllib.parse.urlsplit(base_url.strip())
        if parts.scheme.lower() not in _ALLOWED_URL_SCHEMES:
            raise ValueError("base_url must be an http(s) URL")
        if not parts.netloc:
            raise ValueError("base_url must include a host")
        if parts.username or parts.password:
            raise ValueError("base_url must not embed credentials")
        if parts.fragment:
            raise ValueError("base_url must not contain a fragment")
        for key in urllib.parse.parse_qs(parts.query, keep_blank_values=True):
            if "token" in key.lower():
                # token 只允许经 access_token 参数传入，绝不进 URL / 设备帧。
                raise ValueError(
                    "base_url must not embed a token; pass access_token=... instead"
                )
        path = parts.path.rstrip("/")
        return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc, path, "", ""))

    # ---- 脱敏 -----------------------------------------------------------
    def _redact(self, text: str) -> str:
        """把 token 明文（含 URL 编码形态）替换成 `***`。"""
        token = self._token
        if not text or token is None:
            return text
        redacted = text.replace(token, TOKEN_MASK)
        quoted = urllib.parse.quote(token, safe="")
        if quoted and quoted != token:
            redacted = redacted.replace(quoted, TOKEN_MASK)
        return redacted

    def _sanitize(self, value: Any) -> Any:
        """递归脱敏并收敛成 JSON 可序列化结构；用于一切回传给上层的响应数据。"""
        if isinstance(value, Mapping):
            return {str(key): self._sanitize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._sanitize(item) for item in value]
        if isinstance(value, str):
            return self._redact(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return self._redact(str(value))

    def _assert_payload_is_token_free(self, value: Any, path: str = "$") -> None:
        """token 与密钥同级别：绝不允许出现在出站载荷里（含设备帧/落库记录）。"""
        token = self._token
        if token is None:
            return
        if isinstance(value, str):
            if token in value:
                # 只报位置，不回显 token 本身。
                raise OneBotClientError(
                    f"access_token must not appear in the request payload ({path})"
                )
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                self._assert_payload_is_token_free(item, f"{path}.{key}")
            return
        if isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                self._assert_payload_is_token_free(item, f"{path}[{index}]")

    # ---- HTTP 传输 ------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": CLIENT_USER_AGENT,
        }
        if self._token is not None:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _url(self, endpoint: str) -> str:
        if not isinstance(endpoint, str) or not endpoint.startswith("/") or "://" in endpoint:
            raise ValueError(f"invalid OneBot endpoint: {endpoint!r}")
        return f"{self._base_url.rstrip('/')}{endpoint}"

    @staticmethod
    def _read_error_body(exc: urllib.error.HTTPError) -> str:
        try:
            data = exc.read()
        except Exception:
            return ""
        if isinstance(data, bytes):
            return data.decode("utf-8", "replace")[:_MAX_DETAIL_CHARS]
        return str(data)[:_MAX_DETAIL_CHARS]

    def _post(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        """发一次 POST；**不重试**，网络层异常原样上抛给 lifecycle。"""
        url = self._url(endpoint)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, method="POST", headers=self._headers())
        self._requests_sent += 1
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read()
                status = int(getattr(response, "status", None) or getattr(response, "code", 200) or 200)
        except urllib.error.HTTPError as exc:  # 先于 URLError 捕获（HTTPError 是其子类）
            raise OneBotHTTPError(int(exc.code), self._redact(self._read_error_body(exc))) from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, TimeoutError):
                raise OneBotTimeoutError(
                    f"onebot request to {endpoint} timed out after {self._timeout:g}s"
                ) from None
            raise OneBotTransportError(
                f"onebot transport failure at {endpoint}: {self._redact(str(reason))[:200]}"
            ) from None
        except TimeoutError:
            raise OneBotTimeoutError(
                f"onebot request to {endpoint} timed out after {self._timeout:g}s"
            ) from None
        except OSError as exc:
            raise OneBotTransportError(
                f"onebot transport failure at {endpoint}: {exc.__class__.__name__}"
            ) from None
        return self._parse_response(status, raw)

    @staticmethod
    def _coerce_retcode(value: Any) -> int:
        if isinstance(value, bool):
            raise OneBotProtocolError("OneBot response retcode must be an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
            return int(value.strip())
        raise OneBotProtocolError("OneBot response retcode must be an integer")

    def _response_detail(self, parsed: Mapping[str, Any]) -> str:
        parts: list[str] = []
        for key in ("message", "msg", "wording", "echo"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                parts.append(f"{key}={self._redact(value)[:200]}")
        return "; ".join(parts)

    def _parse_response(self, http_status: int, raw: bytes) -> dict[str, Any]:
        text = raw.decode("utf-8", "replace")
        try:
            parsed = json.loads(text)
        except ValueError:
            raise OneBotProtocolError(
                f"OneBot response is not valid JSON (http {http_status}): {self._redact(text[:120])}"
            ) from None
        if not isinstance(parsed, dict):
            raise OneBotProtocolError("OneBot response must be a JSON object")
        if "status" not in parsed:
            raise OneBotProtocolError("OneBot response is missing status")
        status = parsed.get("status")
        if not isinstance(status, str):
            raise OneBotProtocolError("OneBot response status must be a string")
        retcode = self._coerce_retcode(parsed.get("retcode"))
        data = parsed.get("data", {})
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise OneBotProtocolError("OneBot response data must be an object")
        safe_data = self._sanitize(data)
        if retcode != 0 or status != "ok":
            # 非 retcode:0 一律不算成功。
            raise OneBotAPIError(retcode, status, self._response_detail(parsed))
        return {
            "status": status,
            "retcode": retcode,
            # 便捷字段：调用方（lifecycle 落库）可直接拿到平台消息 id。
            "message_id": safe_data.get("message_id"),
            "data": safe_data,
        }

    def call_api(self, endpoint: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """通用 API 入口：只调用 OneBot HTTP API，不实现任何 QQ 协议行为。"""
        if not isinstance(payload, Mapping):
            raise ValueError("payload must be a mapping")
        body = {str(key): value for key, value in payload.items()}
        self._assert_payload_is_token_free(body)
        return self._post(endpoint, body)

    # ---- 消息段校验（复用 onebot.py 的构造器，不另起一套）------------------
    @staticmethod
    def _numeric_id(value: Any, field: str) -> str:
        if isinstance(value, bool):
            raise OneBotMediaError(f"{field} must be a numeric QQ id; nicknames are not accepted")
        if isinstance(value, int):
            if value < 0:
                raise OneBotMediaError(f"{field} must be a numeric QQ id; nicknames are not accepted")
            return str(value)
        if isinstance(value, str):
            text = value.strip()
            if text and _ID_SHAPE.fullmatch(text) is not None:
                return text
        raise OneBotMediaError(f"{field} must be a numeric QQ id; nicknames are not accepted")

    @staticmethod
    def _face_id(value: Any) -> str:
        if isinstance(value, bool):
            raise OneBotMediaError("face segment requires a numeric id")
        if isinstance(value, int) and value >= 0:
            return str(value)
        if isinstance(value, str) and value.strip().isdigit():
            return value.strip()
        raise OneBotMediaError("face segment requires a numeric id")

    @staticmethod
    def _media_file(kind: str, data: Mapping[str, Any]) -> str:
        raw = data.get("file")
        if not isinstance(raw, str) or not raw.strip():
            raise OneBotMediaError(f"{kind} segment requires file")
        value = raw.strip()
        if value.lower().startswith(URL_PREFIXES):
            # 绝不把媒体降级成文本 URL，也不把外部 URL 交给 OneBot。
            raise OneBotMediaError(
                f"{kind} segment refuses URL media; use an AssetStore asset or an allowed local file"
            )
        if value.startswith("asset:"):
            raise OneBotMediaError(
                f"{kind} segment still holds an unresolved asset reference; "
                "OneBotSender must resolve it before sending"
            )
        return value

    def _normalize_segment(self, segment: Any) -> dict[str, Any]:
        """把一段消息收敛成 `{"type": ..., "data": {...}}`，未知段直接抛错。"""
        if isinstance(segment, OneBotSegment):
            kind: Any = segment.type
            data: Any = segment.data
        elif isinstance(segment, Mapping):
            kind = segment.get("type")
            data = segment.get("data", {})
        else:
            raise OneBotMediaError("message segment must be an object")
        if not isinstance(kind, str) or kind not in OUTBOUND_SEGMENT_TYPES:
            raise OneBotMediaError(
                f"unsupported outbound segment: {kind!r}; refusing to downgrade it to text"
            )
        if not isinstance(data, Mapping):
            raise OneBotMediaError(f"{kind} segment data must be an object")
        if kind == "text":
            value = data.get("text", "")
            if not isinstance(value, str):
                raise OneBotMediaError("text segment requires a string text")
            if not value:
                raise OneBotMediaError("text segment requires non-empty text")
            return OneBotSegment("text", {"text": value}).as_dict()
        if kind == "face":
            return OneBotSegment("face", {"id": self._face_id(data.get("id"))}).as_dict()
        return OneBotSegment(kind, {"file": self._media_file(kind, data)}).as_dict()

    def _normalize_message(self, message: Any, field: str = "message") -> list[dict[str, Any]]:
        if isinstance(message, str):
            segments = [self._normalize_segment({"type": "text", "data": {"text": message}})]
        elif isinstance(message, (list, tuple)):
            segments = [self._normalize_segment(item) for item in message]
        else:
            raise OneBotMediaError(f"{field} must be a string or a list of OneBot segments")
        if not segments:
            raise OneBotMediaError("message would contain no segment")
        return segments

    # ---- 出站 API -------------------------------------------------------
    def send_private_message(
        self,
        user_id: Any,
        message: Any,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """`send_private_msg`：一次调用只发一次；幂等键随请求体带给平台侧去重。"""
        target = self._numeric_id(user_id, "user_id")
        payload: dict[str, Any] = {
            "user_id": int(target),
            "message": self._normalize_message(message),
        }
        if idempotency_key is not None:
            payload["idempotency_key"] = str(idempotency_key)
        return self.call_api(SEND_PRIVATE_ENDPOINT, payload)

    def send_group_message(
        self,
        group_id: Any,
        message: Any,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """`send_group_msg`：群聊仅用于对话回复，绝不下发设备命令。"""
        target = self._numeric_id(group_id, "group_id")
        payload: dict[str, Any] = {
            "group_id": int(target),
            "message": self._normalize_message(message),
        }
        if idempotency_key is not None:
            payload["idempotency_key"] = str(idempotency_key)
        return self.call_api(SEND_GROUP_ENDPOINT, payload)

    def __call__(
        self,
        user_id: Any,
        message: Any,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """让实例本身可以直接作为 `OneBotSender` 的平台回调。"""
        return self.send_private_message(user_id, message, idempotency_key=idempotency_key)

    def bind_sender(
        self,
        *,
        asset_store: Any = None,
        allowed_media_roots: Any = (),
    ) -> OneBotSender:
        """组合（而非重写）已有的 `OneBotSender`：段构造与素材校验仍归它管。"""
        return OneBotSender(
            self.send_private_message,
            asset_store=asset_store,
            allowed_media_roots=allowed_media_roots,
        )