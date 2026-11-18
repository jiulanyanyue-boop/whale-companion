"""M05 端到端链路用的**设备端模拟器**：真 WSS 客户端，绝不 mock。

定位：这份代码站在**手机端**的位置上（对应 ``device/app/src/main/java/cn/whale/agent/
device/DeviceConnectionService.kt``），只做一件事——从 ``wss_url`` 主动连上主机，然后
按需要「正常回帧 / 干脆不回 / 回错帧」。方向固定：手机是客户端，主机是服务端。

**刻意不导入 ``astrbot_whale_device``**：模拟器必须代表另一端。如果它复用被测方的
编解码器，那么两端同时写错也会「自洽通过」，端到端验证就失去意义。所以心跳帧、
响应帧、canonical JSON 签名校验全部按 ``contracts/device-events.schema.json`` 与
``CanonicalJson.kt`` **独立实现**一遍。

线程模型：asyncio 事件循环跑在一条后台线程里，测试线程只通过
``threading.Condition`` / ``threading.Event`` 与它同步——**一次 sleep 都没有**，
既不猜时序，也不会因为主线程阻塞而卡死事件循环。
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Mapping
from typing import Any

import websockets  # noqa: F401  （导入即确认运行时在位；不可用时调用方 importorskip）
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed

#: 主机握手的固定路径（与 ``device_server.DEFAULT_PATH`` 同值，这里独立写死）。
DEVICE_PATH = "/device"

# --- 客户端行为 ------------------------------------------------------------

#: 收到主机信封就回一条**合法**响应。
BEHAVIOR_REPLY_OK = "reply_ok"
#: 收到信封**故意不回**（用来逼出服务端的 ``deadline_ms`` 超时）。
BEHAVIOR_SILENT = "silent"
#: 回一条多带一个未知字段的响应（``additionalProperties: false`` 必须拒）。
BEHAVIOR_UNKNOWN_FIELD = "unknown_field"
#: 回一条 ``status`` 越界的响应。
BEHAVIOR_BAD_STATUS = "bad_status"
#: 回一段根本不是 JSON 的垃圾。
BEHAVIOR_JUNK = "junk"
#: 同一个响应连发两次（第二帧必然是孤儿）。
BEHAVIOR_DOUBLE = "double"

BEHAVIORS = frozenset({
    BEHAVIOR_REPLY_OK,
    BEHAVIOR_SILENT,
    BEHAVIOR_UNKNOWN_FIELD,
    BEHAVIOR_BAD_STATUS,
    BEHAVIOR_JUNK,
    BEHAVIOR_DOUBLE,
})

#: ``device-events`` 的七个必填字段（少一个、多一个都要被主机拒）。
RESPONSE_FIELDS: tuple[str, ...] = (
    "request_id",
    "device_id",
    "status",
    "error_code",
    "message",
    "artifacts",
    "evidence",
)

#: 应用级心跳帧：字段必须严格长成 ``device-events`` 的样子，一个不多一个不少。
HEARTBEAT_REQUEST_ID = "heartbeat"
HEARTBEAT_MESSAGE = "heartbeat"


# ---------------------------------------------------------------------------
# canonical JSON（与 ``CanonicalJson.kt`` 逐条对齐的**独立**实现）
# ---------------------------------------------------------------------------


def write_canonical_string(text: str) -> str:
    """按 Python ``json.dumps(..., ensure_ascii=False)`` 的转义规则写出一个字符串。

    与 ``CanonicalJson.writeString`` 同一套规则，其中两处最容易写错：
``0x7f``（DEL）与 ``/`` **不**转义；其它 ``< 0x20`` 的控制字符转 ``\\uXXXX``（小写）。
    """
    out: list[str] = ['"']
    for ch in text:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\b":
            out.append("\\b")
        elif ch == "\f":
            out.append("\\f")
        elif ch < " ":
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def write_canonical_value(value: Any) -> str:
    """递归写出 canonical JSON 值（无空白、对象键排序）。"""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return write_canonical_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Mapping):
        keys = sorted(str(key) for key in value.keys())
        body = ",".join(
            f"{write_canonical_string(key)}:{write_canonical_value(value[key])}" for key in keys
        )
        return "{" + body + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(write_canonical_value(item) for item in value) + "]"
    raise TypeError(f"canonical json does not support this value type: {type(value).__name__}")


def canonical_unsigned(fields: Mapping[str, Any]) -> str:
    """签名输入：去掉顶层 ``signature`` 之后的 canonical 文本。"""
    unsigned = {str(key): value for key, value in fields.items() if str(key) != "signature"}
    return write_canonical_value(unsigned)


def sign_envelope_like_device(fields: Mapping[str, Any], secret: bytes) -> str:
    """用**设备端**的 canonical 规则算 HMAC-SHA256（十六进制小写）。"""
    import hashlib
    import hmac

    payload = canonical_unsigned(fields).encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def verify_envelope_signature(fields: Mapping[str, Any], secret: bytes) -> bool:
    """设备侧视角验签：主机发来的信封能否被手机端验过。"""
    import hmac

    actual = str(fields.get("signature", "") or "")
    expected = sign_envelope_like_device(fields, secret)
    return bool(actual) and hmac.compare_digest(actual, expected)


# ---------------------------------------------------------------------------
# 模拟客户端
# ---------------------------------------------------------------------------


class DeviceClientSim:
    """一条真实的 WSS 客户端连接，带可配置的应答行为。

    生命周期：``start()`` → （测试查询/等待）→ ``stop()`` / ``close_abrupt()``。
    连接、收到帧、断开这些**状态变化**都通过 ``Event`` / ``Condition`` 通知测试线程，
    测试从不靠固定 ``sleep`` 猜时序。
    """

    def __init__(
        self,
        url: str,
        *,
        device_id: str = "device-e2e",
        behavior: str = BEHAVIOR_REPLY_OK,
        reply_message: str | None = None,
        reply_artifacts: list[dict[str, Any]] | None = None,
        reply_evidence: dict[str, Any] | None = None,
        heartbeat_first: bool = False,
        heartbeat_interval_ms: int | None = None,
        open_timeout: float = 5.0,
        ssl_ctx: Any = None,
    ) -> None:
        if behavior not in BEHAVIORS:
            raise ValueError(f"unknown behavior: {behavior!r}")
        self.url = url
        self.device_id = str(device_id)
        self.behavior = behavior
        self.reply_message = reply_message
        self.reply_artifacts = list(reply_artifacts or [])
        self.reply_evidence = dict(reply_evidence or {})
        self.heartbeat_first = bool(heartbeat_first)
        self.heartbeat_interval_ms = heartbeat_interval_ms
        self.open_timeout = float(open_timeout)
        self.ssl_ctx = ssl_ctx

        #: 测试线程另存的错误（连不上、应答异常等）。``None`` 表示没有错误。
        self.error: BaseException | None = None
        #: 被主动 ``abort`` 或主机断开时的连接关闭异常（不是错误，是事实）。
        self.closed_exc: BaseException | None = None

        self._received: list[dict[str, Any]] = []
        self._received_raw: list[str] = []
        self._sent: list[str] = []
        self._cond = threading.Condition()
        self._connected = threading.Event()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._connection: Any = None
        self._expect_abrupt = False

    # -- 生命周期 --------------------------------------------------------
    def start(self, timeout: float = 5.0) -> "DeviceClientSim":
        """启动后台线程里的真 WSS 连接，等它**真的连上**才返回。"""
        self._thread = threading.Thread(
            target=self._run, name=f"sim-{self.device_id}", daemon=True
        )
        self._thread.start()
        if not self._connected.wait(timeout):
            raise AssertionError(
                f"sim client {self.device_id!r} could not connect to {self.url}: {self.error!r}"
            )
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """优雅关闭（``code=1000``）。已经断开时静默返回，方便在 ``finally`` 里无条件调用。"""
        loop, connection = self._loop, self._connection
        if loop is not None and connection is not None and not loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._close_gracefully(connection), loop
                ).result(timeout)
            except Exception:  # 连接早已没了：不影响清理
                pass
        self._closed.wait(timeout)

    def close_abrupt(self, timeout: float = 5.0) -> None:
        """**突然**断开：直接 ``abort()`` 底层 socket，不发 close 帧。

        这就是「手机掉线/被系统杀掉/网络断掉」的样子——不是体面的
        ``1000 normal closure``，主机只能靠连接丢失感知。
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            raise AssertionError(f"sim client {self.device_id!r} has no running loop to abort")
        self._expect_abrupt = True
        future = asyncio.run_coroutine_threadsafe(self._abort(), loop)
        future.result(timeout)

    # -- 只读观测 --------------------------------------------------------
    def received_raw(self) -> list[str]:
        with self._cond:
            return list(self._received_raw)

    def received_frames(self) -> list[dict[str, Any]]:
        with self._cond:
            return [dict(item) for item in self._received]

    def sent_frames(self) -> list[str]:
        with self._cond:
            return list(self._sent)

    def envelopes(self) -> list[dict[str, Any]]:
        """主机下发的信封（带 ``command`` 字段的那些），不含心跳。"""
        return [item for item in self.received_frames() if "command" in item]

    def handshake_status(self) -> int | None:
        """握手的 HTTP 状态码：``101`` 才是真的升级成 WebSocket。"""
        connection = self._connection
        response = getattr(connection, "response", None)
        return getattr(response, "status_code", None)

    # -- 条件等待（无轮询、无固定 sleep）---------------------------------
    def wait_for_received(
        self,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        *,
        count: int = 1,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """等第 ``count`` 个满足 ``predicate`` 的入站帧；超时**立刻失败**并附上已收帧。"""
        def matches() -> list[dict[str, Any]]:
            return [
                item for item in self._received if predicate is None or predicate(item)
            ]

        with self._cond:
            ok = self._cond.wait_for(lambda: len(matches()) >= count, timeout)
            seen = matches()
        if not ok:
            raise AssertionError(
                f"sim client {self.device_id!r} did not receive the expected frame "
                f"within {timeout}s; got: {self.received_frames()!r}"
            )
        return dict(seen[count - 1])

    def wait_closed(self, timeout: float = 5.0) -> bool:
        return self._closed.wait(timeout)

    # -- 测试线程主动发帧 ------------------------------------------------
    def send_frame(self, frame: Mapping[str, Any], timeout: float = 5.0) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            raise AssertionError(f"sim client {self.device_id!r} has no running loop")
        raw = json.dumps(dict(frame), ensure_ascii=False, separators=(",", ":"))
        asyncio.run_coroutine_threadsafe(self._send_text(self._connection, raw), loop).result(
            timeout
        )

    def send_heartbeat(self, timeout: float = 5.0) -> None:
        """发一帧合法心跳：主机收到后会把这条连接绑到 ``device_id`` 并标 ONLINE。"""
        self.send_frame(self.build_heartbeat_frame(self.device_id), timeout)

    @staticmethod
    def build_heartbeat_frame(device_id: str) -> dict[str, Any]:
        return {
            "request_id": HEARTBEAT_REQUEST_ID,
            "device_id": str(device_id),
            "status": "ok",
            "error_code": "",
            "message": HEARTBEAT_MESSAGE,
            "artifacts": [],
            "evidence": {},
        }

    # -- 内部 ------------------------------------------------------------
    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except ConnectionClosed as exc:  # 正常/突然断开都走这里
            self.closed_exc = exc
        except BaseException as exc:  # noqa: BLE001 - 记下来供断言失败时诊断
            if not self._expect_abrupt:
                self.error = exc
        finally:
            with self._cond:
                self._cond.notify_all()
            self._closed.set()

    async def _main(self) -> None:
        kwargs: dict[str, Any] = {"proxy": None, "open_timeout": self.open_timeout}
        if self.ssl_ctx is not None:
            kwargs["ssl"] = self.ssl_ctx
        async with ws_connect(self.url, **kwargs) as connection:
            self._loop = asyncio.get_running_loop()
            self._connection = connection
            self._connected.set()
            beat: asyncio.Task[None] | None = None
            if self.heartbeat_first:
                await self._send_text(
                    connection, json.dumps(self.build_heartbeat_frame(self.device_id))
                )
            if self.heartbeat_interval_ms:
                beat = asyncio.create_task(self._beat(connection))
            try:
                async for raw in connection:
                    self._record(raw)
                    await self._respond(connection, raw)
            finally:
                if beat is not None:
                    beat.cancel()

    async def _beat(self, connection: Any) -> None:
        interval = max(0.01, (self.heartbeat_interval_ms or 0) / 1000.0)
        while True:
            await asyncio.sleep(interval)
            await self._send_text(
                connection, json.dumps(self.build_heartbeat_frame(self.device_id))
            )

    def _record(self, raw: str) -> None:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
        with self._cond:
            self._received_raw.append(raw if isinstance(raw, str) else str(raw))
            if isinstance(parsed, dict):
                self._received.append(parsed)
            self._cond.notify_all()

    async def _respond(self, connection: Any, raw: str) -> None:
        if self.behavior == BEHAVIOR_SILENT:
            return
        if self.behavior == BEHAVIOR_JUNK:
            await self._send_text(connection, "{not json")
            return
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(envelope, dict):
            return
        frame = {
            "request_id": envelope.get("request_id", ""),
            "device_id": envelope.get("device_id", self.device_id),
            "status": "ok",
            "error_code": "",
            "message": self.reply_message or f"pong:{self.device_id}",
            "artifacts": [dict(item) for item in self.reply_artifacts],
            "evidence": dict(self.reply_evidence),
        }
        if self.behavior == BEHAVIOR_UNKNOWN_FIELD:
            frame["debug"] = "extra"
        elif self.behavior == BEHAVIOR_BAD_STATUS:
            frame["status"] = "whatever"
        encoded = json.dumps(frame, ensure_ascii=False, separators=(",", ":"))
        await self._send_text(connection, encoded)
        if self.behavior == BEHAVIOR_DOUBLE:
            await self._send_text(connection, encoded)

    async def _send_text(self, connection: Any, raw: str) -> None:
        await connection.send(raw)
        with self._cond:
            self._sent.append(raw)

    async def _close_gracefully(self, connection: Any) -> None:
        await connection.close(1000, "sim closing")

    async def _abort(self) -> None:
        transport = getattr(self._connection, "transport", None)
        abort = getattr(transport, "abort", None)
        if not callable(abort):
            raise AssertionError(
                "websockets client transport does not expose abort(); cannot simulate a hard drop"
            )
        abort()


__all__ = [
    "BEHAVIORS",
    "BEHAVIOR_BAD_STATUS",
    "BEHAVIOR_DOUBLE",
    "BEHAVIOR_JUNK",
    "BEHAVIOR_REPLY_OK",
    "BEHAVIOR_SILENT",
    "BEHAVIOR_UNKNOWN_FIELD",
    "DEVICE_PATH",
    "DeviceClientSim",
    "HEARTBEAT_MESSAGE",
    "HEARTBEAT_REQUEST_ID",
    "RESPONSE_FIELDS",
    "canonical_unsigned",
    "sign_envelope_like_device",
    "verify_envelope_signature",
    "write_canonical_string",
    "write_canonical_value",
]
