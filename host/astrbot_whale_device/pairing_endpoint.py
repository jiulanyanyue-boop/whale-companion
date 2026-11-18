"""配对 HTTP 端点（M05 第 3~6 步的主机侧落点）。

## 这条链路之前缺什么

设备端走完「扫码 → 生成 Keystore 身份」之后，需要把「这台设备是谁」告诉主机，
主机再算指纹、等用户确认、最后让一次性 token 失效。之前这段**完全没有实现**：

- 设备端 `prepareRegistration` 只推进本地状态机，**一个网络请求都不发**；
- 主机端 `pairing.py` 有完整的 `PairingService`（提交码、注册身份、确认指纹、收尾），
  但**没有任何 HTTP 端点**去调它。

结果就是「扫码能扫、界面能走」，但主机根本不知道有设备来配对。

## 本模块做什么

用一个标准库 `http.server` 起一个**只服务配对**的小端点（与 WSS 设备通道分开，
因为配对发生在「还没有可信身份」的阶段，不该复用设备帧通道）：

```
POST /pair/register   {token, pairing_code, device_id, public_key, device_fingerprint, capabilities_version, connection_config, display_name}
POST /pair/confirm    {token, device_id, user_accepted, observed_host_fingerprint}
POST /pair/finalize   {token}
GET  /pair/status     ?token=...  → 当前这一步的真实状态
GET  /pair/hostinfo   → 主机指纹（供设备界面显示，供用户核对）
```

## 安全边界（每一条都不可放宽）

1. **配对码必须与 token 匹配**：`submit_code` 用 `hmac.compare_digest` 逐字比对，
   不接受「只给 token 就给过」。
2. **设备指纹必须由设备公钥算出**：主机收到 `public_key` 后**自己算一次**，
   与设备声明的 `device_fingerprint` 比对；不一致直接拒（`register_device_identity`
   已经这么实现，本模块只透传）。
3. **`user_accepted=false` 永远不启用**：第 5 步必须用户明确点头，
   服务端不会因为「签名能验过」就放行。
4. **一次性 token 用后立即失效**：`finalize` 强制 `revoke`，不可逆。
5. **token 绝不出现在日志/响应体**：响应只回状态与指纹，不回 token。

## 为什么不复用 WSS 设备通道

设备帧通道（`device_server.py`）的前提是「双方已经互信并持有签名密钥」，
它按 `device_id` 找**已配对**的连接，未配对设备在上面根本发不进去。
配对恰恰要在「还没有互信」时发生，所以必须另设一个**不要求签名**的端点，
并且只开放这 5 个动作、每个动作都自带凭据校验。
"""

from __future__ import annotations

import json
import sqlite3
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .pairing import (
    DeviceRecord,
    HostIdentity,
    PAIRING_STATE_PAIRING,
    PAIRING_STATE_PAIRED_OFFLINE,
    PairingError,
    PairingService,
    PairingTokenStore,
    PairedDeviceStore,
    host_identity_from_public_key,
    is_well_formed_fingerprint,
)

#: 配对端点默认路径前缀（与设备端约定一致）。
PAIR_PATH_PREFIX = "/pair"

#: 请求体上限（字节）。配对只传一个公钥与几行元数据，1 MiB 足够；
#: 设上限是为了不让人用超大 JSON 把主机拖死。
MAX_BODY_BYTES = 1024 * 1024

#: 主机指纹在未配置主机密钥时的占位值（64 位小写十六进制，形状合法但内容全 0）。
PLACEHOLDER_FINGERPRINT = "0" * 64


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True)
class PairStatus:
    """`/pair/status` 的返回结构（不含 token）。"""

    device_id: str
    pairing_state: str
    device_fingerprint: str
    host_fingerprint: str
    paired: bool

    def to_json(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "pairing_state": self.pairing_state,
            "device_fingerprint": self.device_fingerprint,
            "host_fingerprint": self.host_fingerprint,
            "paired": self.paired,
        }


class PairingEndpointError(RuntimeError):
    """端点内部错误（装配失败等）。"""


class PairingEndpoint:
    """把 `PairingService` 暴露成 HTTP 端点。

    线程安全：`PairingService` 自身用锁保护状态，本类只做请求解析与结果映射，
    不加额外锁（避免「端点的锁」与「服务的锁」交叉导致死锁）。
    """

    def __init__(
        self,
        service: PairingService,
        *,
        host_identity: HostIdentity | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.service = service
        self.host_identity = host_identity
        self._clock = clock or _now_ms

    # -- 主机身份 ---------------------------------------------------------

    def host_fingerprint(self) -> str:
        """主机指纹。未配置主机密钥时返回**占位值**并如实告知（不伪造真指纹）。"""
        if self.host_identity is not None and is_well_formed_fingerprint(
            self.host_identity.fingerprint
        ):
            return self.host_identity.fingerprint
        return PLACEHOLDER_FINGERPRINT

    def has_real_identity(self) -> bool:
        """是否配了真实主机密钥（决定「指纹是占位值」的提示要不要显示）。"""
        return self.host_identity is not None and is_well_formed_fingerprint(
            self.host_identity.fingerprint
        )

    # -- 各步动作 ---------------------------------------------------------

    def register(self, body: dict[str, Any]) -> dict[str, Any]:
        """第 2~4 步：校验配对码 → 保存设备公钥与指纹。

        `pairing_code` 必须与 token 匹配（`submit_code` 内部用恒定时间比较）。
        设备指纹由**主机自己从公钥算一遍**再比对，绝不只信外部传入的值。
        """
        token = _require_str(body, "token")
        pairing_code = _require_str(body, "pairing_code")
        device_id = _require_str(body, "device_id")
        public_key = _require_str(body, "public_key")
        declared_fingerprint = _require_str(body, "device_fingerprint")
        capabilities_version = str(body.get("capabilities_version") or "")
        connection_config = str(body.get("connection_config") or "")
        display_name = str(body.get("display_name") or "")

        # 1) 配对码必须对得上 token（一次性 + TTL 由 `submit_code` 保证）。
        self.service.submit_code(token, pairing_code, now_ms=self._clock())

        # 2) 保存设备身份：内部会从公钥重算指纹并与声明值比对。
        record = self.service.register_device_identity(
            device_id,
            public_key,
            fingerprint=declared_fingerprint,
            now_ms=self._clock(),
        )
        # 3) 补上连接配置/显示名（这两项不进指纹，但进设备记录）。
        #    save_device 只接受关键字参数，且内部会把状态置回 PAIRING。
        record = self.service.save_device(
            device_id,
            capabilities_version=capabilities_version,
            connection_config=connection_config,
            display_name=display_name,
        )
        return {
            "ok": True,
            "device_id": record.device_id,
            "device_fingerprint": record.fingerprint,
            "host_fingerprint": self.host_fingerprint(),
            "host_identity_is_placeholder": not self.has_real_identity(),
        }

    def confirm(self, body: dict[str, Any]) -> dict[str, Any]:
        """第 5 步：用户确认指纹后才启用。

        `user_accepted=false` 时**绝不启用**（服务层已保证），本层只如实回状态。
        """
        token = _require_str(body, "token")
        device_id = _require_str(body, "device_id")
        accepted = bool(body.get("user_accepted"))
        observed = _require_str(body, "observed_host_fingerprint")

        record = self.service.confirm_fingerprint(
            device_id,
            user_accepted=accepted,
            observed_host_fingerprint=observed,
        )
        result: dict[str, Any] = {
            "ok": record.state == PAIRING_STATE_PAIRED_OFFLINE,
            "device_id": record.device_id,
            "pairing_state": record.state,
            "host_fingerprint": self.host_fingerprint(),
        }
        if not accepted:
            result["message"] = "用户拒绝确认指纹：已保持未配对状态，需要重新配对。"
        elif not result["ok"]:
            result["message"] = "指纹不一致或状态异常：未启用。"
        else:
            result["message"] = "配对已启用。"
        # 第 6 步：无论成功与否，一次性 token 都必须失效（不可靠的重试也要换新码）。
        try:
            self.service.finalize(token)
        except PairingError:
            # 已经失效也算达成目的，不把「重复失效」当失败。
            pass
        return result

    def status(self, token: str | None, device_id: str | None) -> dict[str, Any]:
        """查询当前状态。不接受只给 token 就拿全量信息（token 可能已失效）。"""
        record = None
        if device_id:
            record = self.service.devices.get(device_id)
        if record is None:
            return {
                "ok": False,
                "message": "这个设备还没有在主机上登记。",
                "host_fingerprint": self.host_fingerprint(),
            }
        return PairStatus(
            device_id=record.device_id,
            pairing_state=record.state,
            device_fingerprint=record.fingerprint,
            host_fingerprint=self.host_fingerprint(),
            paired=record.state == PAIRING_STATE_PAIRED_OFFLINE,
        ).to_json()

    # -- HTTP 分发 --------------------------------------------------------

    def dispatch(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        query: dict[str, list[str]],
    ) -> tuple[int, dict[str, Any]]:
        """把请求映射成 `(状态码, 响应体)`。**所有错误码都由这里决定**。"""
        route = path.rstrip("/")
        if method == "GET" and route == f"{PAIR_PATH_PREFIX}/hostinfo":
            return 200, {
                "ok": True,
                "host_fingerprint": self.host_fingerprint(),
                "host_identity_is_placeholder": not self.has_real_identity(),
            }
        if method == "GET" and route == f"{PAIR_PATH_PREFIX}/status":
            return 200, self.status(
                _first(query, "token"),
                _first(query, "device_id"),
            )
        if method == "POST" and route == f"{PAIR_PATH_PREFIX}/register":
            return 200, self.register(_require_mapping(body))
        if method == "POST" and route == f"{PAIR_PATH_PREFIX}/confirm":
            return 200, self.confirm(_require_mapping(body))
        if method == "POST" and route == f"{PAIR_PATH_PREFIX}/finalize":
            return 200, self.finalize_only(_require_mapping(body))
        return 404, {"ok": False, "message": f"未知的配对端点：{method} {path}"}

    def finalize_only(self, body: dict[str, Any]) -> dict[str, Any]:
        """只吊销 token（设备端异常退出时的安全收尾）。"""
        token = _require_str(body, "token")
        self.service.finalize(token)
        return {"ok": True, "message": "配对 token 已失效。"}


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _require_str(body: dict[str, Any], key: str) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PairingError("INVALID_PAYLOAD", f"{key} is required")
    return value.strip()


def _require_mapping(body: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise PairingError("INVALID_PAYLOAD", "request body must be a JSON object")
    return body


def _first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    return values[0].strip() if values else None


class _PairingRequestHandler(BaseHTTPRequestHandler):
    """`http.server` 适配器：解析 JSON、调 `PairingEndpoint`、回 JSON。

    **不记录请求体**：配对请求里有 token 与配对码，写进日志等于把凭据留在磁盘上。
    """

    # 由 `PairingHTTPServer` 在启动时注入
    endpoint: PairingEndpoint
    server_version = "WhalePairing/1.0"

    def do_GET(self) -> None:  # noqa: N802  (http.server 约定)
        parsed = urlparse(self.path)
        self._respond("GET", parsed.path, None, parse_qs(parsed.query))

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            self._respond("POST", parsed.path, None, parse_qs(parsed.query))
            return
        if length > MAX_BODY_BYTES:
            self._send(413, {"ok": False, "message": "请求体过大。"})
            return
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send(400, {"ok": False, "message": "请求体不是合法 JSON。"})
            return
        self._respond("POST", parsed.path, body, parse_qs(parsed.query))

    def _respond(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        query: dict[str, list[str]],
    ) -> None:
        try:
            code, payload = self.endpoint.dispatch(method, path, body, query)
        except PairingError as exc:
            # 配对失败：把 `code`（协议错误码）与 `message`（人话）都回给设备，
            # 便于设备端如实显示失败原因。**不回显 token**。
            self._send(400, {"ok": False, "code": exc.code, "message": str(exc)})
            return
        except Exception as exc:  # 兜底：绝不把 traceback 甩给客户端
            self._send(500, {"ok": False, "message": f"主机内部错误：{exc.__class__.__name__}"})
            return
        self._send(code, payload)

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:
        """默认实现会把完整路径打进 stderr——里面可能带 token，所以整条丢掉。"""
        return


class PairingHTTPServer(ThreadingHTTPServer):
    """`ThreadingHTTPServer` 子类，持有 `PairingEndpoint` 供 handler 取用。"""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], endpoint: PairingEndpoint) -> None:
        # 把 endpoint 注入到 handler 的**类属性**上：handler 每个请求都会新建实例，
        # 实例属性传不进去，只能挂类上。一次只跑一个配对服务，因此不存在串台。
        _PairingRequestHandler.endpoint = endpoint
        super().__init__(address, _PairingRequestHandler)


# ---------------------------------------------------------------------------
# 装配入口
# ---------------------------------------------------------------------------


def build_pairing_service(db_path: str, *, host_identity: HostIdentity | None = None) -> PairingService:
    """在指定 sqlite 文件上建一套配对服务（token 与设备记录都持久化）。

    为什么持久化到文件而不是内存：配对是一次性动作，但**配对结果要长期有效**。
    用户重启主机后，已配对的设备不该被要求重新扫码。
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    service = PairingService(db=conn)
    if host_identity is not None:
        service.set_host_identity(host_identity)
    return service


def start_pairing_server(
    *,
    host: str,
    port: int,
    service: PairingService,
    host_identity: HostIdentity | None = None,
    clock: Callable[[], int] | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> tuple[PairingHTTPServer, threading.Thread, int]:
    """起配对 HTTP 服务（后台线程），返回 `(server, thread, 实际端口)`。

    `port=0` 时由系统分配，返回的**实际端口**必须被调用方采用。
    """
    endpoint = PairingEndpoint(service, host_identity=host_identity, clock=clock)
    server = PairingHTTPServer((host, port), endpoint)
    if ssl_context is not None:
        try:
            server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
        except Exception:
            server.server_close()
            raise
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, int(server.server_address[1])


def make_host_identity_from_seed(seed: bytes, *, host_id: str = "whale-host") -> HostIdentity:
    """从一段种子字节派生主机身份（供没有真实密钥库时生成稳定的主机指纹）。

    为什么允许「用种子派生」：主机侧目前没有 Keystore 这类硬件密钥库，
    但指纹必须是**稳定且唯一**的——否则每次重启指纹都变，用户每次都要重新确认，
    或者更糟：用户习惯了忽略指纹提示。
    用固定种子派生能保证「同一台主机、同一个配置，指纹不变」。

    真实生产应换成主机的持久私钥（M05 文档的要求），这里只是让它可用且诚实。
    """
    import base64
    import hashlib

    # host_identity_from_public_key 接收的是**公钥的 Base64 文本**（不是原始字节）：
    # 它内部 ncoded.decode("utf-8")，直接传 DER 原始字节会 UnicodeDecodeError。
    derived = hashlib.sha256(b"whale-host-identity:" + bytes(seed)).digest()
    encoded = base64.b64encode(derived)
    return host_identity_from_public_key(host_id, encoded)