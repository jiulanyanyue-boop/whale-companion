"""OpenAI-compatible 模型 HTTP 客户端（标准库 `urllib.request`，零第三方依赖）。

职责边界（对齐 M02 开发文档「超时和重试」）：

- **只做 HTTP**：把 `ModelGateway` 交给 `transport` 的 payload 翻译成一次
  `POST {base_url}/v1/chat/completions`，再把 `choices[0].message.content`
  **原样**返回。不解析决策 JSON、不做正则修复、不猜字段 —— 那是
  `parse_model_decision` 的事。
- **密钥边界**：API Key 只在 `Authorization: Bearer ...` 请求头里出现一次。
  不写日志、不进异常消息、不进 URL、不进 `repr`；异常里要带响应体片段时先脱敏。
- **超时**：复用 `models.MODEL_TIMEOUT_MS`（30 秒），不自己写数字。
- **不自动重试**：不跟随 3xx（跟随会把 POST 请求体再发一次）、不重发请求体；
  一次调用最多发出一次请求。主备切换由上层负责。

装配方式：

```python
transport = make_model_transport(api_key=secret_store_key)
gateway = ModelGateway(config, transport)
```
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from .models import MODEL_TIMEOUT_MS, SECRET_KEY_INFIXES
from .router import ModelTimeoutError

__all__ = [
    "CHAT_COMPLETIONS_PATH",
    "MAX_ERROR_BODY_BYTES",
    "MAX_RESPONSE_BYTES",
    "MIN_LITERAL_SECRET_CHARS",
    "REDACTED",
    "ModelClientError",
    "ModelEndpointError",
    "ModelHTTPStatusError",
    "ModelHttpTransport",
    "ModelRequestError",
    "ModelResponseError",
    "ModelTransportTimeout",
    "build_chat_completions_url",
    "make_model_transport",
    "make_primary_fallback_transports",
    "redact_secret_text",
]

CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
_CHAT_COMPLETIONS_TAIL = "/chat/completions"
USER_AGENT = "astrbot-whale-device-model-client/0.1"

# 正常响应体上限：超过即判失败，不给内存爆掉的机会。
MAX_RESPONSE_BYTES = 1_000_000
# 异常里允许回显的响应体片段上限。
MAX_ERROR_BODY_BYTES = 512
# 只有拿到「足够长、可用于字面替换」的密钥时才在异常里回显响应体；
# 短于这个长度的密钥无法安全地做字面脱敏，此时干脆什么都不回显。
MIN_LITERAL_SECRET_CHARS = 4

REDACTED = "[redacted]"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MULTI_SLASH = re.compile(r"/{2,}")
_BEARER_SHAPE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{4,}")


def _secret_key_name_pattern() -> str:
    """由 `models.SECRET_KEY_INFIXES` 生成敏感键名正则，保持单一来源。"""
    variants = {
        re.escape(marker).replace("_", "[_-]?").replace("\\-", "[_-]?")
        for marker in SECRET_KEY_INFIXES
    }
    return "(?:" + "|".join(sorted(variants)) + ")"


# 敏感键名后面的值（可带 `Bearer `/`Basic ` scheme 前缀）整体抹掉，键名保留。
_REDACT_VALUE_SHAPE = re.compile(
    r"(?i)([\"']?" + _secret_key_name_pattern() + r"[\"']?\s*[:=]\s*)"
    r"([\"']?)((?:bearer|basic|token)\s+)?([^\s,\"'\[\]}<>]+)([\"']?)"
)


def redact_secret_text(text: str, secret: str = "") -> str:
    """把文本里的密钥痕迹换成 `[redacted]`。

    三层：字面替换已知密钥、`Bearer <token>` 形状、`api_key=...`/`authorization: ...`
    这类键值形状（键名保留，值抹掉）。第二、三层不依赖密钥字面量，因此对
    「密钥由 resolver 动态取」的场景同样有效。
    """
    result = text if isinstance(text, str) else str(text)
    if isinstance(secret, str) and len(secret) >= MIN_LITERAL_SECRET_CHARS:
        result = result.replace(secret, REDACTED)
    # 先按键值形状整段抹掉（含 `Bearer ` 这类 scheme 前缀），再用裸 `Bearer <token>`
    # 兜底 —— 后者只在没有键名的句子里发挥作用，不会把前一步的结果再抹一遍。
    result = _REDACT_VALUE_SHAPE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}{match.group(5)}", result
    )
    return _BEARER_SHAPE.sub(f"Bearer {REDACTED}", result)


def _is_loopback(host: str | None) -> bool:
    return bool(host) and host.lower() in _LOOPBACK_HOSTS


def build_chat_completions_url(base_url: str, *, allow_loopback_http: bool = True) -> str:
    """把配置面的 `base_url` 归一成 chat completions 端点。

    处理：结尾斜杠、内部重复斜杠、已带 `/v1`（或重复 `/v1`）、已给完整端点。
    拒绝：非 https（回环 http 例外，仅为本地测试）、内嵌凭据、query、fragment。
    """
    if not isinstance(base_url, str) or not base_url.strip():
        raise ModelEndpointError("model endpoint base_url is empty")
    parts = urlsplit(base_url.strip())
    scheme = parts.scheme.lower()
    if scheme not in {"https", "http"}:
        raise ModelEndpointError("model endpoint scheme must be https")
    if scheme == "http" and not (allow_loopback_http and _is_loopback(parts.hostname)):
        raise ModelEndpointError("model endpoint must use https (loopback http excepted)")
    if not parts.netloc or not parts.hostname:
        raise ModelEndpointError("model endpoint has no host")
    if parts.username or parts.password:
        raise ModelEndpointError("model endpoint must not embed credentials")
    if parts.query or parts.fragment:
        raise ModelEndpointError("model endpoint must not carry query or fragment")

    path = _MULTI_SLASH.sub("/", parts.path.strip()).rstrip("/")
    while path.endswith("/v1"):
        path = path[:-3].rstrip("/")
    if not path.endswith(_CHAT_COMPLETIONS_TAIL):
        path += CHAT_COMPLETIONS_PATH
    return urlunsplit((scheme, parts.netloc, path, "", ""))


class ModelClientError(ModelTimeoutError):
    """模型出站调用的统一失败类型。

    **故意派生自 `router.ModelTimeoutError`**：`MessageRouter.route` 只把这一族异常
    转成 `failed/model_unavailable`，否则失败会直接穿透路由层。异常消息里绝不携带
    API Key、`Authorization` 原文或未脱敏的响应体。
    """


class ModelEndpointError(ModelClientError):
    """端点或请求体不合法：一律发生在发请求之前。"""


class ModelTransportTimeout(ModelClientError):
    """超过固定超时预算。"""


class ModelRequestError(ModelClientError):
    """网络层失败（DNS / 连接被拒 / 连接重置）。"""


class ModelHTTPStatusError(ModelClientError):
    """非 2xx 状态码（含被拒绝跟随的 3xx）。"""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class ModelResponseError(ModelClientError):
    """响应体不是合法 UTF-8 JSON，或缺 OpenAI 固定字段。"""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """拒绝自动跟随 3xx。

    跟随会把请求体再发一次（重复计费、重复决策），因此 3xx 直接当失败处理。
    `redirect_request` 返回 None 会让 OpenerDirector 落到 `http_error_default`，
    进而抛出 HTTPError。
    """

    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:  # noqa: D102 - 覆盖父类钩子
        return None


class ModelHttpTransport:
    """`ModelGateway(transport=...)` 期望的真实实现。

    签名严格对齐 `astrbot_adapter.ModelGateway`：
    `transport(payload: dict[str, Any]) -> str`，payload 为
    `{provider_id, base_url, model, messages, max_context_tokens}`；返回模型原始文本。

    `api_key` 既可以是字符串，也可以是 `resolver(provider_id) -> str | None`
    （主备供应商可用不同密钥）。密钥只进请求头，且不在 `repr` 里出现。
    """

    __slots__ = ("_allow_loopback_http", "_api_key", "_base_url", "_opener", "_timeout_ms")

    def __init__(
        self,
        *,
        api_key: str | Callable[[str], str | None],
        base_url: str | None = None,
        timeout_ms: int = MODEL_TIMEOUT_MS,
        allow_loopback_http: bool = True,
    ) -> None:
        if (
            not isinstance(timeout_ms, int)
            or isinstance(timeout_ms, bool)
            or timeout_ms <= 0
        ):
            raise ModelEndpointError("model timeout must be a positive integer of milliseconds")
        if not isinstance(api_key, str) and not callable(api_key):
            raise ModelEndpointError("model credential must be a key string or a resolver callable")
        if base_url is not None and (not isinstance(base_url, str) or not base_url.strip()):
            raise ModelEndpointError("model endpoint base_url is empty")
        self._api_key = api_key
        self._base_url = base_url
        self._timeout_ms = timeout_ms
        self._allow_loopback_http = bool(allow_loopback_http)
        # 自建 opener：默认 handler 全集里去掉了「自动跟随重定向」，其余保持标准库行为。
        self._opener = urllib.request.build_opener(_NoRedirectHandler)

    @property
    def timeout_ms(self) -> int:
        return self._timeout_ms

    def __repr__(self) -> str:
        # 只报密钥来源形态，绝不报密钥本身。
        source = "resolver" if callable(self._api_key) else "literal"
        return (
            f"ModelHttpTransport(base_url={self._base_url!r}, "
            f"timeout_ms={self._timeout_ms}, credential={source})"
        )

    def __call__(self, payload: dict[str, Any]) -> str:
        request, secret = self._build_request(payload)
        try:
            with self._opener.open(request, timeout=self._timeout_ms / 1000) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc, secret) from None
        except TimeoutError:
            raise ModelTransportTimeout(
                f"model request timed out after {self._timeout_ms} ms"
            ) from None
        except urllib.error.URLError as exc:
            raise self._url_error(exc) from None
        except OSError as exc:
            raise ModelRequestError(f"model request failed: {type(exc).__name__}") from None
        return self._decode_response(raw, secret)

    # -- 内部：请求构造 ------------------------------------------------------
    def _resolve_credential(self, provider_id: str) -> str:
        source = self._api_key
        value = source(provider_id) if callable(source) else source
        if not isinstance(value, str) or not value.strip():
            raise ModelEndpointError("model endpoint has no usable credential")
        return value.strip()

    def _build_request(self, payload: dict[str, Any]) -> tuple[urllib.request.Request, str]:
        if not isinstance(payload, dict):
            raise ModelEndpointError("model request payload must be an object")
        provider_id = payload.get("provider_id")
        secret = self._resolve_credential(provider_id if isinstance(provider_id, str) else "")
        base_url = self._base_url if self._base_url is not None else payload.get("base_url")
        url = build_chat_completions_url(
            base_url, allow_loopback_http=self._allow_loopback_http
        )
        model = payload.get("model")
        messages = payload.get("messages")
        if not isinstance(model, str) or not model:
            raise ModelEndpointError("model request payload has no model id")
        if not isinstance(messages, list) or not messages:
            raise ModelEndpointError("model request payload has no messages")
        try:
            body = json.dumps(
                {"model": model, "messages": list(messages), "stream": False},
                ensure_ascii=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            # 不回显 payload 内容：它可能含有不该出现在异常里的东西。
            raise ModelEndpointError("model request payload is not JSON serializable") from None
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {secret}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        return request, secret

    # -- 内部：失败描述（一律脱敏） -----------------------------------------
    def _safe_body(self, exc: urllib.error.HTTPError, secret: str) -> str:
        if len(secret) < MIN_LITERAL_SECRET_CHARS:
            return ""
        try:
            raw = exc.read(MAX_ERROR_BODY_BYTES)
        except Exception:  # noqa: BLE001 - 读不到就算了，绝不因此改变失败语义
            return ""
        if not raw:
            return ""
        return redact_secret_text(raw.decode("utf-8", errors="replace"), secret).strip()

    def _http_error(self, exc: urllib.error.HTTPError, secret: str) -> ModelHTTPStatusError:
        status = int(getattr(exc, "code", 0) or 0)
        detail = self._safe_body(exc, secret)
        message = f"model endpoint returned HTTP {status}"
        if detail:
            message = f"{message}: {detail}"
        return ModelHTTPStatusError(message, status=status)

    def _url_error(self, exc: urllib.error.URLError) -> ModelClientError:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, TimeoutError):
            return ModelTransportTimeout(
                f"model request timed out after {self._timeout_ms} ms"
            )
        # 只报异常类名：URLError 的字面消息里带着 URL，没必要时回显。
        name = type(reason).__name__ if reason is not None else "URLError"
        return ModelRequestError(f"model request failed: {name}")

    def _describe_bad_body(self, text: str, secret: str) -> str:
        if len(secret) < MIN_LITERAL_SECRET_CHARS:
            return "model response is not JSON"
        snippet = redact_secret_text(text, secret).strip()[:MAX_ERROR_BODY_BYTES]
        if not snippet:
            return "model response is not JSON"
        return f"model response is not JSON: {snippet}"

    # -- 内部：响应解码（只认 OpenAI 固定字段，缺一即失败） ------------------
    def _decode_response(self, raw: bytes, secret: str) -> str:
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ModelResponseError("model response is too large")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ModelResponseError("model response is not valid UTF-8") from None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise ModelResponseError(self._describe_bad_body(text, secret)) from None
        if not isinstance(data, dict):
            raise ModelResponseError("model response must be a JSON object")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ModelResponseError("model response has no choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise ModelResponseError("model response choice must be an object")
        message = first.get("message")
        if not isinstance(message, dict):
            raise ModelResponseError("model response choice has no message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            # 空内容不能当成成功返回，否则上层会拿到一个「看起来成功的空字符串」。
            raise ModelResponseError("model response has no text content")
        # 原样交给 parse_model_decision；只做一层密钥兜底脱敏。
        return redact_secret_text(content, secret)


def make_model_transport(
    *,
    api_key: str | Callable[[str], str | None],
    base_url: str | None = None,
    timeout_ms: int = MODEL_TIMEOUT_MS,
    allow_loopback_http: bool = True,
) -> ModelHttpTransport:
    """宿主装配入口：把它交给 `ModelGateway(config, transport=...)`。"""
    return ModelHttpTransport(
        api_key=api_key,
        base_url=base_url,
        timeout_ms=timeout_ms,
        allow_loopback_http=allow_loopback_http,
    )


def make_primary_fallback_transports(
    *,
    api_key: str | Callable[[str], str | None],
    primary_base_url: str,
    fallback_base_url: str,
    timeout_ms: int = MODEL_TIMEOUT_MS,
    allow_loopback_http: bool = True,
) -> tuple[ModelHttpTransport, ModelHttpTransport]:
    """主备两个 endpoint 的显式入口。

    两个 transport 各自固定一个 `base_url`，**不会**自动互相重试：谁先谁后由上层
    （`ModelGateway.request_with_fallback`，最多一次）决定。二者可共享同一密钥，
    也可用 resolver 按 `provider_id` 分别取钥。
    """
    primary = ModelHttpTransport(
        api_key=api_key,
        base_url=primary_base_url,
        timeout_ms=timeout_ms,
        allow_loopback_http=allow_loopback_http,
    )
    fallback = ModelHttpTransport(
        api_key=api_key,
        base_url=fallback_base_url,
        timeout_ms=timeout_ms,
        allow_loopback_http=allow_loopback_http,
    )
    return primary, fallback
# ---------------------------------------------------------------------------
# 模型列表（GET /v1/models）
#
# 为什么要它：用户配置里「模型 ID」是个自由文本输入框——而模型 ID 必须与
# 服务商实际提供的名字逐字一致（`gpt-4o-mini` 写成 `gpt-4omini` 就 404）。
# 让用户凭记忆手打，等于把「猜对一个字符串」的负担丢给用户。
#
# OpenAI 兼容服务普遍提供 `GET /v1/models`，这里直接拉列表给界面做单选。
# 拉不到（服务商没实现该端点 / 无权限）时**如实降级**：界面回落到手填，
# 不假装「没有模型可用」。
# ---------------------------------------------------------------------------

MODELS_PATH = "/v1/models"
_MODELS_TAIL = "/models"


def build_models_url(base_url: str, *, allow_loopback_http: bool = True) -> str:
    """把配置面的 `base_url` 归一成模型列表端点。

    与 [build_chat_completions_url] 同样的清洗与拒绝规则（结尾斜杠、重复 `/v1`、
    已给完整端点、非 https、内嵌凭据、query/fragment），只是尾部换成 `/models`。
    """
    if not isinstance(base_url, str) or not base_url.strip():
        raise ModelEndpointError("model endpoint base_url is empty")
    parts = urlsplit(base_url.strip())
    scheme = parts.scheme.lower()
    if scheme not in {"https", "http"}:
        raise ModelEndpointError("model endpoint scheme must be https")
    if scheme == "http" and not (allow_loopback_http and _is_loopback(parts.hostname)):
        raise ModelEndpointError("model endpoint must use https (loopback http excepted)")
    if not parts.netloc or not parts.hostname:
        raise ModelEndpointError("model endpoint has no host")
    if parts.username or parts.password:
        raise ModelEndpointError("model endpoint must not embed credentials")
    if parts.query or parts.fragment:
        raise ModelEndpointError("model endpoint must not carry query or fragment")

    path = _MULTI_SLASH.sub("/", parts.path.strip()).rstrip("/")
    # 先把已知的端点尾巴剥掉（**顺序很重要**：先剥长的 /v1/chat/completions，
    # 否则剥完 /v1 会剩下 /v1/v1/models）。
    if path.endswith(_CHAT_COMPLETIONS_TAIL):
        path = path[: -len(_CHAT_COMPLETIONS_TAIL)].rstrip("/")
    if path.endswith(_MODELS_TAIL):
        path = path[: -len(_MODELS_TAIL)].rstrip("/")
    # 再剥末尾的 /v1，最后统一拼 /v1/models。
    while path.endswith("/v1"):
        path = path[:-3].rstrip("/")
    path += MODELS_PATH
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def parse_models_response(raw: str) -> list[str]:
    """从 `/v1/models` 的响应体里取出模型 ID 列表（去重、保序）。

    兼容两种常见形状：

    - OpenAI 官方：`{"object": "list", "data": [{"id": "gpt-4o-mini", ...}, ...]}`
    - 部分服务商：`{"models": ["a", "b"]}` 或直接 `["a", "b"]`

    取不到就返回**空列表**（由调用方如实降级到「手填」），不抛异常、不编造。
    """
    text = (raw or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except Exception:
        return []

    candidates: list[Any] = []
    if isinstance(payload, list):
        candidates = payload
    elif isinstance(payload, dict):
        for key in ("data", "models", "result"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates = value
                break

    ids: list[str] = []
    for item in candidates:
        if isinstance(item, str):
            value = item.strip()
        elif isinstance(item, dict):
            value = str(item.get("id") or item.get("name") or "").strip()
        else:
            value = ""
        if value and value not in ids:
            ids.append(value)
    return ids


def fetch_model_ids(
    *,
    api_key: str | Callable[[str], str | None],
    base_url: str,
    timeout_ms: int = MODEL_TIMEOUT_MS,
    allow_loopback_http: bool = True,
    transport_factory: Callable[..., Any] | None = None,
) -> list[str]:
    """拉取模型列表。

    失败时抛 [ModelClientError] 的具体子类（与 chat 调用一致的错误类型），
    让界面能区分「地址错」「没权限」「服务商不支持该端点」。
    **认证头与 chat 调用共用同一套逻辑**，不在本函数里重复实现密钥处理。
    """
    url = build_models_url(base_url, allow_loopback_http=allow_loopback_http)
    key = api_key("openai") if callable(api_key) else api_key
    secret = (key or "").strip()

    request = urllib.request.Request(url, method="GET")
    request.add_header("Accept", "application/json")
    if secret:
        request.add_header("Authorization", f"Bearer {secret}")

    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(request, timeout=max(1.0, timeout_ms / 1000.0)) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise _http_error_for(exc, secret) from exc
    except urllib.error.URLError as exc:
        raise ModelRequestError(f"model endpoint unreachable: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ModelTransportTimeout("model endpoint timed out while listing models") from exc

    try:
        text = raw.decode("utf-8")
    except Exception as exc:
        raise ModelResponseError("models response is not valid UTF-8") from exc
    return parse_models_response(text)


def _http_error_for(exc: urllib.error.HTTPError, secret: str) -> ModelClientError:
    """把 HTTP 错误翻成明确的异常（**带状态码**，便于界面区分 401 / 404 / 5xx）。"""
    body = _redact_safe_body(exc, secret)
    detail = f"models request failed with HTTP {exc.code}"
    if body:
        detail += f": {body}"
    return ModelHTTPStatusError(detail, status=exc.code)


def _redact_safe_body(exc: urllib.error.HTTPError, secret: str) -> str:
    """读错误响应体并脱敏；读不到就返回空串（绝不因为读 body 失败掩盖原始错误）。"""
    try:
        raw = exc.read()
    except Exception:
        return ""
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return ""
    text = " ".join(text.split())
    if secret:
        text = text.replace(secret, "***")
    return text[:200]
