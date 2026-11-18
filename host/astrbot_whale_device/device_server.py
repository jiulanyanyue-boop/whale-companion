"""M05 主机侧 WSS 设备服务端（Python）。

规范来源：``docs/modules/M05-pairing-transport-development.md``、
``contracts/device-envelope.schema.json``、``contracts/device-events.schema.json``。

**方向固定：手机是 WSS 客户端，主机是服务端。** 设备端
``DeviceConnectionService.kt`` 只做一件事——从 ``device_connection`` 里读
``wss_url``，用 OkHttp ``newWebSocket(url)`` 主动连上来；它不开放任何入站端口。
本模块就是那个 URL 背后的服务端。

三件事：

1. :class:`WhaleDeviceServer` —— 在后台线程的 asyncio 事件循环里跑 ``websockets``
   服务端，维护「连接表 + 在途请求表（``concurrent.futures.Future``）+ 幂等缓存」。
2. :class:`WebSocketDeviceTransport` —— :class:`~astrbot_whale_device.transport.DeviceTransport`
   的真实实现：``plugin.py`` 里**同步**的 ``transport.request(envelope)`` 通过
   ``asyncio.run_coroutine_threadsafe`` 把发送动作投进服务端循环，再用
   ``Future.result(timeout=...)`` 阻塞等待设备响应。**没有任何忙等**
   （没有 ``while True: sleep``），唤醒完全靠 ``concurrent.futures.Future`` 与
   ``threading.Event``。
3. 帧纪律 —— 出向严格遵守 ``device-envelope``，入向严格遵守 ``device-events``：
   未知字段、字段缺失、类型不符、枚举越界一律拒绝；``deadline_ms`` 到点未响应
   即返回失败。错误码**全部**取自 ``CommandErrors.ALL``，本模块不新增任何错误码。

错误码取舍（只用已登记的码，逐条给理由）：

- ``EXPIRED``：``deadline_ms`` 到点仍未收到响应。M01 文档里 ``EXPIRED`` 的语义
  就是「请求 deadline 过期」，设备端 ``ModeGate`` 用的也是它。
- ``NOT_WIRED``：没有该 ``device_id`` 的活跃连接（设备没连上来，或已断开）。
- ``INVALID_PAYLOAD``：帧结构非法（未知字段、必填字段缺失、类型不符、枚举越界）。
- ``PROTOCOL_DECODE_FAILED``：帧不是合法 JSON，或不是 JSON 对象。
- ``UNSUPPORTED_PROTOCOL``：信封 ``protocol_version`` 不是 1。

安全约定：

- ``signature`` 原文**永不**进日志；日志 detail 一律先过
  :func:`astrbot_whale_device.audit.sanitize_detail`（白名单脱敏）。
- 设备帧内容（payload / artifacts / evidence / message）**不**写入审计表；
  本模块也不持有 ``TaskStore`` 的写审计通道，只把幂等结果落 ``effects`` 表。

幂等：同一 ``idempotency_key`` 不会二次下发。命中顺序为「进程内完成表 →
可选 ``TaskStore`` 的 ``effects`` 表 → 在途同键请求复用同一个 future」。
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import Future, InvalidStateError, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Any

from .audit import sanitize_detail
from .models import DeviceEnvelope, DeviceResponse
from .pairing import MAX_BACKOFF_SECONDS, backoff_delay_seconds
from .protocol import ProtocolError, decode_envelope, encode_envelope
from .transport import DeviceTransport

# ---------------------------------------------------------------------------
# 帧字段（与 contracts/*.schema.json 逐条对齐；这里只是运行时的第二道闸门）
# ---------------------------------------------------------------------------

#: 主机 → 设备：``device-envelope.schema.json`` 的 required 集合，且
#: ``additionalProperties: false``，所以这就是**允许的全部**字段。
ENVELOPE_FIELDS: frozenset[str] = frozenset({
    "protocol_version",
    "request_id",
    "device_id",
    "command",
    "deadline_ms",
    "idempotency_key",
    "counter",
    "issued_at_ms",
    "payload",
    "signature",
})

#: 设备 → 主机：``device-events.schema.json`` 的 required 集合（同样禁止额外字段）。
RESPONSE_FIELDS: frozenset[str] = frozenset({
    "request_id",
    "device_id",
    "status",
    "error_code",
    "message",
    "artifacts",
    "evidence",
})

#: ``device-events`` 的 ``status`` 枚举。
RESPONSE_STATUSES: frozenset[str] = frozenset({"ok", "denied", "queued", "failed", "needs_unlock"})

# ---------------------------------------------------------------------------
# 心跳（设备 → 主机的应用级心跳帧；严格长成 device-events 的样子，不发明字段）
# ---------------------------------------------------------------------------

#: 心跳帧固定的 ``request_id``（不会与真实请求的 uuid 撞车）。
HEARTBEAT_REQUEST_ID = "heartbeat"
#: 心跳帧固定的 ``message``。
HEARTBEAT_MESSAGE = "heartbeat"

# ---------------------------------------------------------------------------
# 已登记错误码（值取自 device/.../CommandErrors.kt 的 ALL；本模块只引用、不新增）
# ---------------------------------------------------------------------------

TIMEOUT_ERROR_CODE = "EXPIRED"
NO_CONNECTION_ERROR_CODE = "NOT_WIRED"
INVALID_PAYLOAD_ERROR_CODE = "INVALID_PAYLOAD"
DECODE_ERROR_CODE = "PROTOCOL_DECODE_FAILED"
UNSUPPORTED_PROTOCOL_ERROR_CODE = "UNSUPPORTED_PROTOCOL"

#: 本模块会返回的全部错误码。测试会断言它是 ``CommandErrors.ALL`` 的子集。
DECLARED_ERROR_CODES: frozenset[str] = frozenset({
    TIMEOUT_ERROR_CODE,
    NO_CONNECTION_ERROR_CODE,
    INVALID_PAYLOAD_ERROR_CODE,
    DECODE_ERROR_CODE,
    UNSUPPORTED_PROTOCOL_ERROR_CODE,
})

DEFAULT_PATH = "/device"
#: 幂等完成表的容量上限（有界，避免长期运行内存无界增长）。
MAX_CACHED_RESPONSES = 256
#: 迟到的、没有对应在途请求的响应最多留档多少条。
MAX_ORPHAN_RESPONSES = 64
#: 明文 ws://（无 TLS）只允许回环地址；生产必须 wss://。
LOOPBACK_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})

_ws_serve: Any = None
_ws_connection_closed: type[BaseException] | None = None
_WS_REQUIRED_MSG = "websockets runtime is required for the host WSS device server"
try:  # pragma: no cover - 依赖在目标环境里总是装好的，这里只保证失败时报错清晰
    from websockets.asyncio.server import serve as _ws_serve  # type: ignore[no-redef]
    from websockets.exceptions import ConnectionClosed  # type: ignore[no-redef]

    _ws_connection_closed = ConnectionClosed
    _WEBSOCKETS_AVAILABLE = True
    _WEBSOCKETS_IMPORT_ERROR = ""
except ImportError as exc:  # pragma: no cover
    _WEBSOCKETS_AVAILABLE = False
    _WEBSOCKETS_IMPORT_ERROR = str(exc)


def websockets_available() -> bool:
    """``websockets`` 运行时是否可用（不可用时 :meth:`WhaleDeviceServer.start` 会抛明确错误）。"""
    return _WEBSOCKETS_AVAILABLE


def require_websockets() -> Any:
    """返回 ``websockets.asyncio.server.serve``；不可用时抛出**明确**的依赖错误。"""
    if not _WEBSOCKETS_AVAILABLE or _ws_serve is None:
        raise DeviceServerDependencyError(
            f"{_WS_REQUIRED_MSG}: {_WEBSOCKETS_IMPORT_ERROR or 'websockets is not importable'}"
        )
    return _ws_serve


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class DeviceServerError(RuntimeError):
    """服务端生命周期错误（重复启动、启动超时、明文非回环等）。"""


class DeviceServerDependencyError(DeviceServerError):
    """运行时缺 ``websockets``。**明确报错**，绝不静默降级成假发送。"""


class DeviceFrameError(ValueError):
    """帧拒绝。``code`` 只能是 :data:`DECLARED_ERROR_CODES` 里的值。"""

    def __init__(self, code: str, reason: str) -> None:
        if code not in DECLARED_ERROR_CODES:
            raise ValueError(f"frame rejection code is not registered: {code}")
        self.code = str(code)
        self.reason = str(reason)
        super().__init__(f"{self.code}: {self.reason}")


# ---------------------------------------------------------------------------
# 帧校验 / 编解码
# ---------------------------------------------------------------------------


def validate_outgoing_frame(fields: Mapping[str, Any]) -> DeviceEnvelope:
    """校验并解码一个**主机→设备**信封。

    先自己拦未知字段（拿到更精确的原因），再复用
    :func:`astrbot_whale_device.protocol.decode_envelope` —— 保证主机侧只有
    **一份**信封语义，不写第二套解析。
    """
    if not isinstance(fields, Mapping):
        raise DeviceFrameError(INVALID_PAYLOAD_ERROR_CODE, "envelope must be an object")
    data = {str(key): value for key, value in fields.items()}
    unknown = sorted(set(data) - ENVELOPE_FIELDS)
    if unknown:
        raise DeviceFrameError(INVALID_PAYLOAD_ERROR_CODE, f"unknown envelope field: {unknown}")
    missing = sorted(ENVELOPE_FIELDS - set(data))
    if missing:
        raise DeviceFrameError(INVALID_PAYLOAD_ERROR_CODE, f"missing envelope field: {missing}")
    try:
        return decode_envelope(json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    except ProtocolError as exc:
        message = str(exc)
        if "protocol version" in message:
            raise DeviceFrameError(UNSUPPORTED_PROTOCOL_ERROR_CODE, message) from exc
        raise DeviceFrameError(INVALID_PAYLOAD_ERROR_CODE, message) from exc


def decode_device_frame(raw: str | bytes | bytearray) -> DeviceResponse:
    """校验并解码一个**设备→主机**响应帧。

    未知字段、必填字段缺失、类型不符、``status`` 越界一律
    :class:`DeviceFrameError`；JSON 解析失败用 ``PROTOCOL_DECODE_FAILED``。
    """
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = bytes(raw).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DeviceFrameError(DECODE_ERROR_CODE, "frame is not valid UTF-8") from exc
    if not isinstance(raw, str):
        raise DeviceFrameError(INVALID_PAYLOAD_ERROR_CODE, "frame must be text")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DeviceFrameError(DECODE_ERROR_CODE, "frame is not valid JSON") from exc
    if not isinstance(data, dict):
        raise DeviceFrameError(DECODE_ERROR_CODE, "frame must be a JSON object")

    unknown = sorted(set(data) - RESPONSE_FIELDS)
    if unknown:
        raise DeviceFrameError(INVALID_PAYLOAD_ERROR_CODE, f"unknown response field: {unknown}")
    missing = sorted(RESPONSE_FIELDS - set(data))
    if missing:
        raise DeviceFrameError(INVALID_PAYLOAD_ERROR_CODE, f"missing response field: {missing}")

    request_id = data["request_id"]
    device_id = data["device_id"]
    status = data["status"]
    if not isinstance(request_id, str) or not request_id:
        raise DeviceFrameError(INVALID_PAYLOAD_ERROR_CODE, "request_id must be a non-empty string")
    if not isinstance(device_id, str) or len(device_id) < 3:
        raise DeviceFrameError(INVALID_PAYLOAD_ERROR_CODE, "device_id must be at least 3 characters")
    if not isinstance(status, str) or status not in RESPONSE_STATUSES:
        raise DeviceFrameError(INVALID_PAYLOAD_ERROR_CODE, f"unknown response status: {status!r}")
    if not isinstance(data["error_code"], str):
        raise DeviceFrameError(INVALID_PAYLOAD_ERROR_CODE, "error_code must be a string")
    if not isinstance(data["message"], str):
        raise DeviceFrameError(INVALID_PAYLOAD_ERROR_CODE, "message must be a string")
    artifacts = data["artifacts"]
    if not isinstance(artifacts, list) or any(not isinstance(item, dict) for item in artifacts):
        raise DeviceFrameError(INVALID_PAYLOAD_ERROR_CODE, "artifacts must be an array of objects")
    evidence = data["evidence"]
    if not isinstance(evidence, dict):
        raise DeviceFrameError(INVALID_PAYLOAD_ERROR_CODE, "evidence must be an object")

    return DeviceResponse(
        request_id=request_id,
        device_id=device_id,
        status=status,
        error_code=str(data["error_code"]),
        message=str(data["message"]),
        artifacts=[dict(item) for item in artifacts],
        evidence=dict(evidence),
    )


def build_heartbeat_frame(device_id: str) -> dict[str, Any]:
    """构造一个**完全符合** ``device-events.schema.json`` 的心跳帧。

    设备端发心跳只需要把这份 dict 编码成 JSON 发出来：主机收到后把该连接标为
    ONLINE、退避清零。字段一个不多、一个不少。
    """
    return {
        "request_id": HEARTBEAT_REQUEST_ID,
        "device_id": str(device_id),
        "status": "ok",
        "error_code": "",
        "message": HEARTBEAT_MESSAGE,
        "artifacts": [],
        "evidence": {},
    }


def is_heartbeat_frame(response: DeviceResponse) -> bool:
    """是否为心跳帧（``request_id`` 与 ``message`` 同时命中固定值）。"""
    return response.request_id == HEARTBEAT_REQUEST_ID and response.message == HEARTBEAT_MESSAGE


def is_loopback_host(host: str | None) -> bool:
    """是否回环地址（只有回环允许明文 ``ws://``）。"""
    if not host or not isinstance(host, str):
        return False
    value = host.strip().lower()
    if value in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(value.strip("[]")).is_loopback
    except ValueError:
        return False


def _cache_key(idempotency_key: str) -> str:
    """``effects`` 表里的键。加前缀避免与出站回复的 ``reply:`` 键空间混淆。"""
    return f"device:{idempotency_key}"


# ---------------------------------------------------------------------------
# 状态快照
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceConnectionStatus:
    """一条设备连接的只读快照（供上层展示/审计，不含任何帧内容）。"""

    device_id: str
    state: str
    peer: str = ""
    connected_at_ms: int = 0
    last_heartbeat_ms: int = 0
    backoff_seconds: int = 0
    requests_sent: int = 0
    responses_received: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_id": self.device_id,
            "state": self.state,
            "peer": self.peer,
            "connected_at_ms": self.connected_at_ms,
            "last_heartbeat_ms": self.last_heartbeat_ms,
            "backoff_seconds": self.backoff_seconds,
            "requests_sent": self.requests_sent,
            "responses_received": self.responses_received,
        }


@dataclass
class DeviceServerStats:
    """累计计数（只增不减；全部整数，便于断言）。"""

    requests_sent: int = 0
    responses_received: int = 0
    frames_rejected: int = 0
    timeouts: int = 0
    idempotent_replays: int = 0
    heartbeats: int = 0
    orphan_responses: int = 0
    connections_opened: int = 0
    connections_closed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests_sent": self.requests_sent,
            "responses_received": self.responses_received,
            "frames_rejected": self.frames_rejected,
            "timeouts": self.timeouts,
            "idempotent_replays": self.idempotent_replays,
            "heartbeats": self.heartbeats,
            "orphan_responses": self.orphan_responses,
            "connections_opened": self.connections_opened,
            "connections_closed": self.connections_closed,
        }


@dataclass
class _PendingRequest:
    """一个在途请求。``future`` 由调用线程等待，由服务端循环线程写入。"""

    request_id: str
    idempotency_key: str
    device_id: str
    command: str
    connection_id: str
    future: Future = field(default_factory=Future)


@dataclass
class _ConnectionState:
    """一条活跃 WSS 连接的服务端侧状态。"""

    connection: Any
    connection_id: str
    peer: str
    connected_at_ms: int
    connected_at_monotonic_ms: float
    device_id: str = ""
    bound: bool = False
    last_heartbeat_ms: int = 0
    last_heartbeat_monotonic_ms: float = 0.0
    backoff_seconds: int = 0
    heartbeat_misses: int = 0
    requests_sent: int = 0
    responses_received: int = 0

    @property
    def is_open(self) -> bool:
        """连接是否还开着（``close_code`` 为 ``None`` 即未关闭）。"""
        try:
            return self.connection.close_code is None
        except AttributeError:  # pragma: no cover - 兜底，不让状态判断炸掉请求
            return False

    def snapshot(self, state: str) -> DeviceConnectionStatus:
        return DeviceConnectionStatus(
            device_id=self.device_id,
            state=state,
            peer=self.peer,
            connected_at_ms=self.connected_at_ms,
            last_heartbeat_ms=self.last_heartbeat_ms,
            backoff_seconds=self.backoff_seconds,
            requests_sent=self.requests_sent,
            responses_received=self.responses_received,
        )


# ---------------------------------------------------------------------------
# 服务端
# ---------------------------------------------------------------------------


class WhaleDeviceServer:
    """主机侧 WSS 设备服务端。

    生命周期：:meth:`start`（后台线程 + 独立事件循环）→ 设备主动连上来 →
    :meth:`request`（同步）→ :meth:`stop`。

    线程模型：

    - **服务端循环线程**：``asyncio.new_event_loop()`` + ``run_until_complete``，
      跑 ``websockets`` 服务端、连接处理器与心跳看门狗；
    - **调用线程**：``plugin.py`` 的同步执行路径；:meth:`request` 用
      ``asyncio.run_coroutine_threadsafe`` 投递发送协程，再
      ``Future.result(timeout=deadline)`` 阻塞等待 —— 不轮询、不 sleep。
    - 两个世界之间只有两种同步原语：``Future``（请求/响应）与
      ``threading.Event``（断线通知），其余共享状态都在
      ``threading.RLock`` 后面。
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
        path: str = DEFAULT_PATH,
        ssl_context: Any = None,
        store: Any = None,
        heartbeat_timeout_ms: int | None = None,
        heartbeat_check_interval_ms: int = 500,
        ping_interval: float | None = 20.0,
        ping_timeout: float | None = 20.0,
        clock: Callable[[], int] | None = None,
        log: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.host = str(host)
        self.port = int(port)
        self.path = path if path.startswith("/") else f"/{path}"
        self.ssl_context = ssl_context
        #: 可选的 ``TaskStore``：只读 ``effect`` / 只调 ``add_effect``，**不改**它。
        self.store = store
        #: ``None`` = 关闭应用级心跳看门狗（此时死连接依赖 ``websockets`` 的
        #: 协议级 ping/pong 检测）。设成毫秒数后启用：超过该时长没收到心跳帧的连接
        #: 由看门狗关闭并清理。
        self.heartbeat_timeout_ms = None if heartbeat_timeout_ms is None else max(1, int(heartbeat_timeout_ms))
        self.heartbeat_check_interval_ms = max(10, int(heartbeat_check_interval_ms))
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.stats = DeviceServerStats()

        self._clock: Callable[[], int] = clock or (lambda: int(time.time() * 1000))
        self._log_sink = log
        self._lock = threading.RLock()
        self._connections: list[_ConnectionState] = []
        self._pending: dict[str, _PendingRequest] = {}
        self._inflight_by_key: dict[str, _PendingRequest] = {}
        self._completed: OrderedDict[str, DeviceResponse] = OrderedDict()
        self._orphans: list[dict[str, Any]] = []
        self._offline: dict[str, DeviceConnectionStatus] = {}
        self._disconnect_attempts: dict[str, int] = {}
        self._disconnect_events: dict[str, threading.Event] = {}
        self._connection_seq = 0

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: Any = None
        self._started_event = threading.Event()
        self._startup_error: BaseException | None = None
        self._bound_port: int = 0
        self._stopping = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def base_port(self) -> int:
        return self._bound_port or self.port

    @property
    def url(self) -> str:
        scheme = "wss" if self.ssl_context is not None else "ws"
        host = self.host or "127.0.0.1"
        return f"{scheme}://{host}:{self.base_port}{self.path}"

    def start(self, timeout: float = 10.0) -> int:
        """启动服务端，返回实际绑定端口。失败时抛异常而不是留一个半死状态。"""
        with self._lock:
            if self._thread is not None:
                raise DeviceServerError("device server is already started")
            if self.ssl_context is None and not is_loopback_host(self.host):
                raise DeviceServerError(
                    "plaintext ws:// is forbidden outside loopback; production must use wss:// "
                    f"(host={self.host!r})"
                )
            require_websockets()
            self._started_event.clear()
            self._startup_error = None
            self._stopping = False
            self._thread = threading.Thread(target=self._thread_main, name="whale-device-wss", daemon=True)

        self._thread.start()
        if not self._started_event.wait(timeout):
            raise DeviceServerError("device server did not finish starting in time")
        if self._startup_error is not None:
            error = self._startup_error
            with self._lock:
                self._thread = None
            raise DeviceServerError(f"device server failed to start: {error}") from error
        self._log("device.server_started", {"connection_state": self.url, "count": self._bound_port})
        return self._bound_port

    def stop(self, timeout: float = 10.0) -> None:
        """停止服务端：关连接、关服务、结束循环线程，并**兜底失败所有在途请求**。"""
        thread = self._thread
        if thread is None:
            return
        loop = self._loop
        with self._lock:
            self._stopping = True
        if loop is not None and not loop.is_closed():
            try:
                future = asyncio.run_coroutine_threadsafe(self._shutdown(), loop)
                future.result(timeout)
            except Exception:  # 循环已停/已关：下面的 join 与兜底清理仍要做
                pass
        thread.join(timeout)
        with self._lock:
            self._thread = None
            self._loop = None
            self._server = None
        self._fail_pending_for_disconnect()
        self._log("device.server_stopped", {"state": "OFFLINE", "count": self.stats.connections_closed})

    async def _shutdown(self) -> None:
        for state in list(self._connections):
            try:
                await state.connection.close(1001, "server stopping")
            except Exception:  # 连接已经没了：照常继续清理
                pass
        server = self._server
        if server is not None:
            server.close()

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._serve())
        except BaseException as exc:  # noqa: BLE001 - 线程里必须先记录下来再通知 start()
            self._startup_error = exc
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()
            self._started_event.set()

    async def _serve(self) -> None:
        serve = require_websockets()

        async def handler(connection: Any) -> None:
            await self._handler(connection)

        kwargs: dict[str, Any] = {
            "ping_interval": self.ping_interval,
            "ping_timeout": self.ping_timeout,
        }
        if self.ssl_context is not None:
            kwargs["ssl"] = self.ssl_context
        # ``serve(...)`` 是 websockets 15 的异步上下文管理器：``__aenter__`` 才真正
        # 绑定端口；绑定失败（端口占用、证书问题）会在这里抛出并传回 ``start()``。
        async with serve(handler, self.host, self.port, **kwargs) as server:
            self._server = server
            sockets = list(server.sockets or ())
            self._bound_port = int(sockets[0].getsockname()[1]) if sockets else self.port
            watchdog: asyncio.Task[None] | None = None
            if self.heartbeat_timeout_ms is not None:
                watchdog = asyncio.create_task(self._heartbeat_watchdog())
            self._started_event.set()
            try:
                await server.serve_forever()
            except asyncio.CancelledError:  # pragma: no cover - 正常停服不走这条
                pass
            finally:
                if watchdog is not None:
                    watchdog.cancel()

    # ------------------------------------------------------------------
    # 连接处理（服务端循环线程）
    # ------------------------------------------------------------------
    async def _handler(self, connection: Any) -> None:
        state = self._register_connection(connection)
        try:
            async for raw in connection:
                self._handle_raw(state, raw)
        except asyncio.CancelledError:  # pragma: no cover - 停服时取消
            raise
        except Exception as exc:  # 连接异常：只记事件名与设备号，不记任何帧内容
            if not (_ws_connection_closed is not None and isinstance(exc, _ws_connection_closed)):
                self._log("device.connection_error", {"device_id": state.device_id, "reason": exc.__class__.__name__})
        finally:
            self._detach(state)

    def _register_connection(self, connection: Any) -> _ConnectionState:
        remote = getattr(connection, "remote_address", None)
        peer = ""
        if isinstance(remote, tuple) and remote:
            peer = f"{remote[0]}:{remote[1]}" if len(remote) > 1 else str(remote[0])
        with self._lock:
            self._connection_seq += 1
            state = _ConnectionState(
                connection=connection,
                connection_id=f"conn-{self._connection_seq}",
                peer=peer,
                connected_at_ms=int(self._clock()),
                connected_at_monotonic_ms=time.monotonic(),
            )
            self._connections.append(state)
            self.stats.connections_opened += 1
        self._log("device.connected", {"connection_state": "CONNECTING", "count": len(self._connections)})
        return state

    def _detach(self, state: _ConnectionState) -> None:
        """连接断开：移出连接表、记录退避、**清理该连接的全部在途请求**。"""
        doomed: list[_PendingRequest] = []
        event: threading.Event | None = None
        device_id = ""
        with self._lock:
            if state in self._connections:
                self._connections.remove(state)
            self.stats.connections_closed += 1
            device_id = state.device_id if state.bound else ""
            if device_id:
                attempts = int(self._disconnect_attempts.get(device_id, 0)) + 1
                self._disconnect_attempts[device_id] = attempts
                state.backoff_seconds = backoff_delay_seconds(attempts)
                self._offline[device_id] = state.snapshot("BACKOFF")
                event = self._disconnect_events.get(device_id)
            for request_id, pending in list(self._pending.items()):
                if pending.connection_id == state.connection_id:
                    doomed.append(pending)
                    self._pending.pop(request_id, None)
            for pending in doomed:
                if self._inflight_by_key.get(pending.idempotency_key) is pending:
                    self._inflight_by_key.pop(pending.idempotency_key, None)
        for pending in doomed:
            self._resolve_future(pending.future, self._failure_for(pending, NO_CONNECTION_ERROR_CODE, "device connection closed"))
        if event is not None:
            event.set()
        self._log("device.disconnected", {
            "device_id": device_id,
            "state": "BACKOFF",
            "reason": f"backoff={state.backoff_seconds}",
            "count": len(doomed),
        })

    def _handle_raw(self, state: _ConnectionState, raw: Any) -> None:
        """处理一帧入站文本（纯同步，运行在服务端循环线程）。"""
        try:
            response = decode_device_frame(raw)
        except DeviceFrameError as exc:
            with self._lock:
                self.stats.frames_rejected += 1
            self._log("device.frame_rejected", {
                "device_id": state.device_id,
                "error_code": exc.code,
                "reason": exc.reason,
            })
            return
        if not self._bind_from_frame(state, response.device_id):
            with self._lock:
                self.stats.frames_rejected += 1
            self._log("device.frame_rejected", {
                "device_id": state.device_id,
                "error_code": INVALID_PAYLOAD_ERROR_CODE,
                "reason": "connection is already bound to another device_id",
            })
            return
        if is_heartbeat_frame(response):
            self._on_heartbeat(state)
            return
        with self._lock:
            pending = self._pending.get(response.request_id)
            if pending is not None and pending.device_id != response.device_id:
                # 串台：绝不把别的设备的响应交给这个请求。
                pending = None
                self.stats.frames_rejected += 1
        if pending is None:
            with self._lock:
                self.stats.orphan_responses += 1
                self._orphans.append({
                    "device_id": response.device_id,
                    "status": response.status,
                    "error_code": response.error_code,
                    "observed_at": int(self._clock()),
                })
                del self._orphans[:-MAX_ORPHAN_RESPONSES]
            self._log("device.response_orphaned", {
                "device_id": response.device_id,
                "status": response.status,
                "error_code": response.error_code,
            })
            return
        with self._lock:
            self.stats.responses_received += 1
            state.responses_received += 1
        self._resolve_future(pending.future, response)

    def _bind_from_frame(self, state: _ConnectionState, device_id: str) -> bool:
        """把帧里的 ``device_id`` 固化到连接上；已绑定时不接受**另一个** device_id。"""
        with self._lock:
            if not state.bound:
                state.device_id = device_id
                state.bound = True
                self._disconnect_events.setdefault(device_id, threading.Event())
                self._offline.pop(device_id, None)
                return True
            return state.device_id == device_id

    def _on_heartbeat(self, state: _ConnectionState) -> None:
        now_ms = int(self._clock())
        now_mono = time.monotonic()
        with self._lock:
            state.last_heartbeat_ms = now_ms
            state.last_heartbeat_monotonic_ms = now_mono
            state.backoff_seconds = 0
            state.heartbeat_misses = 0
            if state.bound:
                # 文档：成功收到 heartbeat → 退避**清零**。
                self._disconnect_attempts[state.device_id] = 0
                self._offline.pop(state.device_id, None)
            self.stats.heartbeats += 1
        self._log("device.heartbeat", {"device_id": state.device_id, "connection_state": "ONLINE"})

    @staticmethod
    def _resolve_future(future: Future, response: DeviceResponse) -> None:
        try:
            future.set_result(response)
        except InvalidStateError:  # 已经有结果（例如断线清理先到）：不覆盖
            pass

    async def _heartbeat_watchdog(self) -> None:
        """应用级死连接检测：超时未心跳 → 关连接（``_detach`` 会清理在途请求）。"""
        timeout_ms = self.heartbeat_timeout_ms or 0
        interval = max(0.01, self.heartbeat_check_interval_ms / 1000.0)
        while True:
            await asyncio.sleep(interval)
            now = time.monotonic()
            stale: list[_ConnectionState] = []
            with self._lock:
                for state in self._connections:
                    reference = state.last_heartbeat_monotonic_ms or state.connected_at_monotonic_ms
                    if (now - reference) * 1000.0 <= timeout_ms:
                        continue
                    state.heartbeat_misses += 1
                    stale.append(state)
            for state in stale:
                self._log("device.heartbeat_timeout", {
                    "device_id": state.device_id,
                    "reason": "no heartbeat before heartbeat_timeout_ms",
                    "count": state.heartbeat_misses,
                })
                try:
                    await state.connection.close(1001, "heartbeat timeout")
                except Exception:  # pragma: no cover - 连接可能已经关了
                    pass

    # ------------------------------------------------------------------
    # 同步请求路径（调用线程）
    # ------------------------------------------------------------------
    @property
    def pending_count(self) -> int:
        """在途请求数。超时/断线后必须回到 0（future 不泄漏）。"""
        with self._lock:
            return len(self._pending)

    @property
    def orphan_responses(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._orphans]

    def request(self, envelope: DeviceEnvelope) -> DeviceResponse:
        """同步下发一个信封并等待设备响应。

        返回 ``DeviceResponse``；失败不抛异常（调用方 ``plugin.py`` 只看
        ``status`` / ``error_code``）。超时时长为信封自己的 ``deadline_ms``。
        """
        if not isinstance(envelope, DeviceEnvelope):
            return self._failure_fields({}, INVALID_PAYLOAD_ERROR_CODE, "transport requires a DeviceEnvelope")
        try:
            validated = validate_outgoing_frame(envelope.as_dict())
        except DeviceFrameError as exc:
            self._log("device.outgoing_frame_rejected", {
                "device_id": getattr(envelope, "device_id", ""),
                "command": getattr(envelope, "command", ""),
                "error_code": exc.code,
                "reason": exc.reason,
            })
            return self._failure(envelope, exc.code, "outgoing frame rejected")
        return self._dispatch(validated)

    def _dispatch(self, envelope: DeviceEnvelope) -> DeviceResponse:
        key = envelope.idempotency_key
        # 1) 幂等：进程内完成表 / 可选 TaskStore.effects，命中则原样重放，**不二次下发**。
        cached = self._cached_response(key)
        if cached is not None:
            with self._lock:
                self.stats.idempotent_replays += 1
            self._log("device.command_replayed", {
                "device_id": envelope.device_id,
                "command": envelope.command,
                "idempotency_key": key,
                "status": cached.status,
            })
            return cached

        # 2) 同键在途：复用同一个 future，同一幂等键全局只发一帧。
        with self._lock:
            inflight = self._inflight_by_key.get(key)
        if inflight is not None:
            remaining = envelope.deadline_ms / 1000.0
            try:
                response = inflight.future.result(timeout=max(0.0, remaining))
            except FutureTimeoutError:
                return self._timeout_failure(envelope, "idempotent request is still in flight")
            return response

        # 3) 找到该设备的连接；没有就失败（NOT_WIRED），绝不假装成功。
        state = self._acquire_connection(envelope.device_id)
        if state is None:
            self._log("device.command_not_sent", {
                "device_id": envelope.device_id,
                "command": envelope.command,
                "state": "OFFLINE",
                "error_code": NO_CONNECTION_ERROR_CODE,
            })
            return self._failure(envelope, NO_CONNECTION_ERROR_CODE, "no device connection is available")

        deadline_monotonic = time.monotonic() + envelope.deadline_ms / 1000.0
        pending = _PendingRequest(
            request_id=envelope.request_id,
            idempotency_key=key,
            device_id=envelope.device_id,
            command=envelope.command,
            connection_id=state.connection_id,
        )
        loop = self._loop
        with self._lock:
            if self._stopping or loop is None or loop.is_closed():
                return self._failure(envelope, NO_CONNECTION_ERROR_CODE, "device server is stopping")
            self._pending[pending.request_id] = pending
            self._inflight_by_key[key] = pending

        response: DeviceResponse
        try:
            response = self._send_and_wait(state, envelope, pending, deadline_monotonic)
        finally:
            with self._lock:
                if self._pending.get(pending.request_id) is pending:
                    self._pending.pop(pending.request_id, None)
                if self._inflight_by_key.get(key) is pending:
                    self._inflight_by_key.pop(key, None)

        if response.status == "ok":
            self._remember(key, response)
            self._persist_effect(key, response)
        else:
            # 失败响应不进持久幂等表（下一次同键可以真的重试），但进程内短期记住，
            # 避免同一进程内用同一幂等键反复打设备。
            self._remember(key, response)
        return response

    def _send_and_wait(
        self,
        state: _ConnectionState,
        envelope: DeviceEnvelope,
        pending: _PendingRequest,
        deadline_monotonic: float,
    ) -> DeviceResponse:
        loop = self._loop
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            return self._timeout_failure(envelope, "deadline_ms elapsed before the frame was sent")
        try:
            raw = encode_envelope(envelope)
        except ProtocolError as exc:
            return self._failure(envelope, INVALID_PAYLOAD_ERROR_CODE, f"envelope could not be encoded: {exc}")
        try:
            send_future = asyncio.run_coroutine_threadsafe(self._send_text(state, raw), loop)  # type: ignore[arg-type]
        except RuntimeError as exc:
            return self._failure(envelope, NO_CONNECTION_ERROR_CODE, f"device server loop is unavailable: {exc}")
        with self._lock:
            self.stats.requests_sent += 1
            state.requests_sent += 1
        self._log("device.command_sent", {
            "device_id": envelope.device_id,
            "command": envelope.command,
            "idempotency_key": envelope.idempotency_key,
            "connection_state": "ONLINE",
            "sent_at": int(self._clock()),
        })
        try:
            send_future.result(timeout=max(0.0, deadline_monotonic - time.monotonic()))
        except FutureTimeoutError:
            send_future.cancel()
            return self._timeout_failure(envelope, "frame could not be written before deadline_ms")
        except Exception as exc:
            return self._failure(envelope, NO_CONNECTION_ERROR_CODE, f"frame write failed: {exc.__class__.__name__}")
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            return self._timeout_failure(envelope, "deadline_ms elapsed right after the frame was sent")
        try:
            return pending.future.result(timeout=remaining)
        except FutureTimeoutError:
            return self._timeout_failure(envelope, "device did not answer before deadline_ms")

    async def _send_text(self, state: _ConnectionState, raw: str) -> None:
        await state.connection.send(raw)

    def _timeout_failure(self, envelope: DeviceEnvelope, reason: str) -> DeviceResponse:
        """``deadline_ms`` 到点：返回 ``EXPIRED``。**pending 表由调用方 finally 清理**。"""
        with self._lock:
            self.stats.timeouts += 1
        self._log("device.command_timed_out", {
            "device_id": envelope.device_id,
            "command": envelope.command,
            "idempotency_key": envelope.idempotency_key,
            "error_code": TIMEOUT_ERROR_CODE,
            "reason": reason,
            "count": envelope.deadline_ms,
        })
        return self._failure(envelope, TIMEOUT_ERROR_CODE, "device did not answer before deadline_ms")

    def _acquire_connection(self, device_id: str) -> _ConnectionState | None:
        """按 ``device_id`` 找连接。

        设备端（``DeviceConnectionService.kt``）连接时**不带任何身份信息**——它只是
        ``newWebSocket(wss_url)``。因此绑定分两步：先看有没有已经绑定了该
        ``device_id`` 的连接（来自心跳帧或历史响应帧）；若没有，且当前**恰好只有一个**
        尚未绑定的连接，就把这台设备绑上去（单设备回环兜底），一旦绑定不再改变。
        """
        pinned = False
        with self._lock:
            for state in self._connections:
                if state.bound and state.device_id == device_id and state.is_open:
                    return state
            unbound = [state for state in self._connections if not state.bound and state.is_open]
            if len(unbound) != 1:
                return None
            state = unbound[0]
            state.device_id = device_id
            state.bound = True
            self._disconnect_events.setdefault(device_id, threading.Event())
            self._offline.pop(device_id, None)
            pinned = True
        if pinned:
            self._log("device.connection_pinned", {"device_id": device_id, "connection_state": "ONLINE"})
        return state

    # ------------------------------------------------------------------
    # 幂等缓存与持久 effect
    # ------------------------------------------------------------------
    def _cached_response(self, idempotency_key: str) -> DeviceResponse | None:
        with self._lock:
            cached = self._completed.get(idempotency_key)
            if cached is not None:
                self._completed.move_to_end(idempotency_key)
                return cached
        store = self.store
        if store is None:
            return None
        try:
            raw = store.effect(_cache_key(idempotency_key))
        except Exception as exc:  # 存储层异常不该让传输层崩
            self._log("device.idempotency_store_error", {"reason": exc.__class__.__name__})
            return None
        if not raw:
            return None
        try:
            response = decode_device_frame(raw)
        except DeviceFrameError:
            return None
        self._remember(idempotency_key, response)
        return response

    def _remember(self, idempotency_key: str, response: DeviceResponse) -> None:
        with self._lock:
            self._completed[idempotency_key] = response
            self._completed.move_to_end(idempotency_key)
            while len(self._completed) > MAX_CACHED_RESPONSES:
                self._completed.popitem(last=False)

    def _persist_effect(self, idempotency_key: str, response: DeviceResponse) -> None:
        store = self.store
        if store is None:
            return
        try:
            store.add_effect(
                _cache_key(idempotency_key),
                json.dumps(response.as_dict(), ensure_ascii=False, separators=(",", ":")),
                int(self._clock()),
            )
        except Exception as exc:
            self._log("device.idempotency_store_error", {"reason": exc.__class__.__name__})

    # ------------------------------------------------------------------
    # 只读状态
    # ------------------------------------------------------------------
    def connection_state(self, device_id: str) -> DeviceConnectionStatus:
        with self._lock:
            for state in self._connections:
                if state.bound and state.device_id == device_id and state.is_open:
                    return state.snapshot("ONLINE")
            offline = self._offline.get(device_id)
        return offline or DeviceConnectionStatus(device_id=device_id, state="OFFLINE")

    def connections(self) -> list[DeviceConnectionStatus]:
        with self._lock:
            return [state.snapshot("ONLINE" if state.bound else "CONNECTING") for state in self._connections]

    def recommended_backoff_seconds(self, device_id: str) -> int:
        """建议的设备重连退避（固定序列 1、2、4、8、16、30，上限 30；心跳后清零）。"""
        with self._lock:
            attempts = int(self._disconnect_attempts.get(device_id, 0))
        if attempts <= 0:
            return 0
        return min(backoff_delay_seconds(attempts), MAX_BACKOFF_SECONDS)

    def wait_until_disconnected(self, device_id: str, timeout: float = 5.0) -> bool:
        """阻塞等待该设备断开（``threading.Event``，不轮询）。已断开则立即返回 ``True``。"""
        with self._lock:
            if not any(state.bound and state.device_id == device_id and state.is_open for state in self._connections):
                offline = self._offline.get(device_id)
                if offline is not None:
                    return True
            event = self._disconnect_events.setdefault(device_id, threading.Event())
        return event.wait(timeout)

    def _fail_pending_for_disconnect(self) -> None:
        """兜底：无条件把还在等结果的请求全部唤醒，绝不留悬挂的 future。"""
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
            self._inflight_by_key.clear()
        for item in pending:
            self._resolve_future(
                item.future,
                self._failure_for(item, NO_CONNECTION_ERROR_CODE, "device server stopped"),
            )

    # ------------------------------------------------------------------
    # 失败响应与日志
    # ------------------------------------------------------------------
    @staticmethod
    def _failure(envelope: DeviceEnvelope, code: str, message: str) -> DeviceResponse:
        return DeviceResponse(
            request_id=envelope.request_id,
            device_id=envelope.device_id,
            status="failed",
            error_code=code,
            message=message,
            artifacts=[],
            evidence={},
        )

    @staticmethod
    def _failure_fields(fields: Mapping[str, Any], code: str, message: str) -> DeviceResponse:
        return DeviceResponse(
            request_id=str(fields.get("request_id", "") or ""),
            device_id=str(fields.get("device_id", "") or ""),
            status="failed",
            error_code=code,
            message=message,
            artifacts=[],
            evidence={},
        )

    @staticmethod
    def _failure_for(pending: _PendingRequest, code: str, message: str) -> DeviceResponse:
        return DeviceResponse(
            request_id=pending.request_id,
            device_id=pending.device_id,
            status="failed",
            error_code=code,
            message=message,
            artifacts=[],
            evidence={},
        )

    def _log(self, event: str, detail: Mapping[str, Any]) -> None:
        """日志出口：detail 先过白名单脱敏（``audit.sanitize_detail``）。

        ``signature`` 与帧正文既不在 detail 里，也不会被白名单放行——想泄漏也进不来。
        """
        sink = self._log_sink
        if sink is None:
            return
        try:
            sink(str(event), sanitize_detail(dict(detail)))
        except Exception:  # 日志失败不影响传输
            pass


class WebSocketDeviceTransport(DeviceTransport):
    """``DeviceTransport`` 的真实实现：把同步 ``request`` 桥到 WSS 服务端。

    ``plugin.py`` 与 ``OneBotEventBridge`` 只依赖
    ``transport.request(envelope) -> DeviceResponse`` 这个同步签名；本类保持它不变，
    内部交给 :class:`WhaleDeviceServer` 完成「投递 → 等待 → 超时/断线清理」。
    """

    def __init__(self, server: WhaleDeviceServer) -> None:
        if not isinstance(server, WhaleDeviceServer):
            raise TypeError("WebSocketDeviceTransport requires a WhaleDeviceServer")
        self.server = server

    def request(self, envelope: DeviceEnvelope) -> DeviceResponse:
        return self.server.request(envelope)

    # 便捷转发：装配处可以不直接持有 server。
    def start(self, timeout: float = 10.0) -> int:
        return self.server.start(timeout)

    def stop(self, timeout: float = 10.0) -> None:
        self.server.stop(timeout)

    @property
    def is_running(self) -> bool:
        return self.server.is_running

    @property
    def url(self) -> str:
        return self.server.url


__all__ = [
    "DECLARED_ERROR_CODES",
    "DECODE_ERROR_CODE",
    "DEFAULT_PATH",
    "DeviceConnectionStatus",
    "DeviceFrameError",
    "DeviceServerDependencyError",
    "DeviceServerError",
    "DeviceServerStats",
    "ENVELOPE_FIELDS",
    "HEARTBEAT_MESSAGE",
    "HEARTBEAT_REQUEST_ID",
    "INVALID_PAYLOAD_ERROR_CODE",
    "MAX_CACHED_RESPONSES",
    "NO_CONNECTION_ERROR_CODE",
    "RESPONSE_FIELDS",
    "RESPONSE_STATUSES",
    "TIMEOUT_ERROR_CODE",
    "UNSUPPORTED_PROTOCOL_ERROR_CODE",
    "WebSocketDeviceTransport",
    "WhaleDeviceServer",
    "build_heartbeat_frame",
    "decode_device_frame",
    "is_heartbeat_frame",
    "is_loopback_host",
    "require_websockets",
    "validate_outgoing_frame",
    "websockets_available",
]