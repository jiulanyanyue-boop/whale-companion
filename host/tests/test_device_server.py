"""M05 主机侧 WSS 设备服务端测试（**真回环**，不是 mock）。

覆盖：
- 正常帧收发（真 websockets 服务端 + 真 websockets 客户端，真签名信封）；
- 未知字段 / 字段缺失 / 非法枚举 / 非 JSON 拒绝；
- ``deadline_ms`` 超时返回 ``EXPIRED``，且 pending 表清零、迟到响应进孤儿档；
- 应用级心跳（ONLINE、退避清零）与心跳超时清理死连接；
- 幂等：同一 ``idempotency_key`` 不二次下发（进程内 + ``TaskStore.effects`` 跨实例）；
- 错误码纪律：只引用 ``CommandErrors.ALL`` 里已登记的码；
- 日志脱敏：``signature`` 与帧正文永不进日志。

方向固定：手机是 WSS 客户端，主机是服务端。测试里的 ``FakeDevice`` 就是那个客户端。
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import inspect
import ipaddress
import json
import re
import ssl
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("websockets")

from websockets.asyncio.client import connect as ws_connect  # noqa: E402

from astrbot_whale_device import device_server as ds  # noqa: E402
from astrbot_whale_device.device_server import (  # noqa: E402
    DECLARED_ERROR_CODES,
    DECODE_ERROR_CODE,
    ENVELOPE_FIELDS,
    HEARTBEAT_MESSAGE,
    HEARTBEAT_REQUEST_ID,
    INVALID_PAYLOAD_ERROR_CODE,
    NO_CONNECTION_ERROR_CODE,
    RESPONSE_FIELDS,
    TIMEOUT_ERROR_CODE,
    UNSUPPORTED_PROTOCOL_ERROR_CODE,
    DeviceFrameError,
    DeviceServerDependencyError,
    DeviceServerError,
    WebSocketDeviceTransport,
    WhaleDeviceServer,
    build_heartbeat_frame,
    decode_device_frame,
    is_heartbeat_frame,
    is_loopback_host,
    validate_outgoing_frame,
)
from astrbot_whale_device.models import DeviceEnvelope, DeviceResponse  # noqa: E402
from astrbot_whale_device.protocol import decode_envelope, encode_response  # noqa: E402
from astrbot_whale_device.security import sign_hmac  # noqa: E402
from astrbot_whale_device.storage import TaskStore  # noqa: E402
from astrbot_whale_device.transport import DeviceTransport  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMAND_ERRORS_KT = (
    REPO_ROOT / "device" / "app" / "src" / "main" / "java" / "cn" / "whale" / "agent" / "device" / "CommandErrors.kt"
)
SIGNING_SECRET = b"whale-test-signing-secret"


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def registered_error_codes() -> set[str]:
    """从 ``CommandErrors.kt`` 的 ``ALL`` 里取出唯一登记处的错误码（只读，不改设备端）。"""
    text = COMMAND_ERRORS_KT.read_text(encoding="utf-8")
    block = re.search(r"val ALL: Set<String> = setOf\((.*?)\n    \)", text, re.S)
    assert block is not None, "CommandErrors.kt 里找不到 ALL 定义"
    return set(re.findall(r"[A-Z][A-Z0-9_]+", block.group(1))) - {"ALL"}


def make_envelope(
    *,
    request_id: str | None = None,
    device_id: str = "device-1",
    command: str = "device.app.list",
    deadline_ms: int = 2_000,
    idempotency_key: str | None = None,
    payload: dict | None = None,
    counter: int = 1,
) -> DeviceEnvelope:
    """构造一个**真签名**的信封（与 plugin.py 的下发路径同形）。"""
    rid = request_id or f"req-{time.monotonic_ns()}"
    envelope = DeviceEnvelope(
        protocol_version=1,
        request_id=rid,
        device_id=device_id,
        command=command,
        deadline_ms=deadline_ms,
        idempotency_key=idempotency_key or f"key-{rid}",
        payload=dict(payload or {}),
        counter=counter,
        issued_at_ms=int(time.time() * 1000),
    )
    return replace(envelope, signature=sign_hmac(envelope.as_dict(), SIGNING_SECRET))


class FakeDevice:
    """真·回环设备客户端：主动连主机，按 ``mode`` 决定怎么应答。

    这不是 mock —— 它是一条真实的 WSS 连接，收到的是主机真正写出的字节。
    """

    def __init__(
        self,
        url: str,
        *,
        device_id: str = "device-1",
        mode: str = "ok",
        delay_ms: int = 0,
        heartbeat_first: bool = False,
        heartbeat_interval_ms: int | None = None,
        ssl_ctx: ssl.SSLContext | None = None,
    ) -> None:
        self.url = url
        self.device_id = device_id
        self.mode = mode
        self.delay_ms = int(delay_ms)
        self.heartbeat_first = heartbeat_first
        self.heartbeat_interval_ms = heartbeat_interval_ms
        self.ssl_ctx = ssl_ctx
        self.frames: list[str] = []
        self.error: BaseException | None = None
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._closed = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._socket = None

    # -- 生命周期 --------------------------------------------------------
    def start(self, timeout: float = 5.0) -> "FakeDevice":
        self._thread = threading.Thread(target=self._run, name=f"fake-{self.device_id}", daemon=True)
        self._thread.start()
        if not self._connected.wait(timeout):
            raise AssertionError(f"fake device could not connect to {self.url}: {self.error!r}")
        return self

    def wait_closed(self, timeout: float = 5.0) -> bool:
        return self._closed.wait(timeout)

    def close_now(self) -> None:
        """从测试线程主动断开（走 ``run_coroutine_threadsafe``，不轮询）。"""
        loop, socket_ = self._loop, self._socket
        if loop is None or socket_ is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(socket_.close(), loop).result(timeout=5.0)
        except Exception:  # 连接可能已经没了
            pass

    def frame_count(self) -> int:
        with self._lock:
            return len(self.frames)

    def decoded_frames(self) -> list[dict]:
        with self._lock:
            raw = list(self.frames)
        out: list[dict] = []
        for item in raw:
            try:
                parsed = json.loads(item)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
        return out

    # -- 内部 ------------------------------------------------------------
    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as exc:  # noqa: BLE001 - 记录下来供断言失败时诊断
            self.error = exc
        finally:
            self._closed.set()

    async def _main(self) -> None:
        kwargs: dict = {"proxy": None}
        if self.ssl_ctx is not None:
            kwargs["ssl"] = self.ssl_ctx
        async with ws_connect(self.url, **kwargs) as socket_:
            self._loop = asyncio.get_running_loop()
            self._socket = socket_
            self._connected.set()
            beat: asyncio.Task[None] | None = None
            if self.heartbeat_first:
                await socket_.send(json.dumps(build_heartbeat_frame(self.device_id)))
            if self.heartbeat_interval_ms:
                beat = asyncio.create_task(self._beat(socket_))
            try:
                async for raw in socket_:
                    with self._lock:
                        self.frames.append(raw)
                    await self._respond(socket_, raw)
            finally:
                if beat is not None:
                    beat.cancel()

    async def _beat(self, socket_) -> None:
        interval = max(0.01, (self.heartbeat_interval_ms or 0) / 1000)
        while True:
            await asyncio.sleep(interval)
            await socket_.send(json.dumps(build_heartbeat_frame(self.device_id)))

    async def _respond(self, socket_, raw: str) -> None:
        if self.mode == "silent":
            return
        if self.mode == "junk":
            await socket_.send("{not json")
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)
        frame = {
            "request_id": data["request_id"],
            "device_id": data["device_id"],
            "status": "ok",
            "error_code": "",
            "message": "done",
            "artifacts": [],
            "evidence": {},
        }
        if self.mode == "unknown_field":
            frame["debug"] = "extra"
        elif self.mode == "missing_field":
            frame.pop("evidence")
        elif self.mode == "bad_status":
            frame["status"] = "whatever"
        elif self.mode == "wrong_device":
            frame["device_id"] = "device-other"
        await socket_.send(json.dumps(frame))
        if self.mode == "double":
            await socket_.send(json.dumps(frame))


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def logs() -> list[tuple[str, dict]]:
    return []


@pytest.fixture()
def server(logs) -> WhaleDeviceServer:
    instance = WhaleDeviceServer(host="127.0.0.1", port=0, log=lambda event, detail: logs.append((event, dict(detail))))
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


@pytest.fixture(scope="session")
def tls_pair(tmp_path_factory):
    """自签证书：服务端上下文 + 信任该证书的客户端上下文（真 TLS 握手）。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    directory = tmp_path_factory.mktemp("tls")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = _dt.datetime.now(_dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(cert_path), str(key_path))
    client_ctx = ssl.create_default_context(cafile=str(cert_path))
    return server_ctx, client_ctx


# ---------------------------------------------------------------------------
# 正常收发
# ---------------------------------------------------------------------------


def test_round_trip_over_real_loopback(server, logs):
    """真服务端 + 真客户端：信封原样送达，响应原样回来。"""
    envelope = make_envelope(request_id="req-rt-1", idempotency_key="key-rt-1", payload={"package_name": "com.example"})
    device = FakeDevice(server.url, device_id="device-1", heartbeat_first=True).start()
    assert wait_until(lambda: server.connection_state("device-1").state == "ONLINE")

    response = server.request(envelope)

    assert isinstance(response, DeviceResponse)
    assert response.status == "ok"
    assert response.request_id == "req-rt-1"
    assert response.device_id == "device-1"
    frames = device.decoded_frames()
    assert len(frames) == 1
    sent = frames[0]
    assert set(sent) == ENVELOPE_FIELDS
    assert sent["command"] == "device.app.list"
    assert sent["idempotency_key"] == "key-rt-1"
    assert sent["signature"] == envelope.signature
    assert decode_envelope(json.dumps(sent)).request_id == "req-rt-1"
    assert server.stats.requests_sent == 1
    assert any(event == "device.command_sent" for event, _ in logs)
    device.close_now()


def test_transport_adapter_is_a_sync_device_transport(server):
    """``plugin.py`` 依赖的同步签名必须原样成立（不是 coroutine）。"""
    transport = WebSocketDeviceTransport(server)
    assert isinstance(transport, DeviceTransport)
    assert not inspect.iscoroutinefunction(transport.request)
    device = FakeDevice(server.url, device_id="device-1", heartbeat_first=True).start()
    assert wait_until(lambda: server.connection_state("device-1").state == "ONLINE")
    response = transport.request(make_envelope(request_id="req-adapter", idempotency_key="key-adapter"))
    assert response.status == "ok" and response.request_id == "req-adapter"
    device.close_now()


def test_unbound_single_connection_is_pinned_then_used(server):
    """设备端不带身份握手，唯一未绑定连接可被兜底绑定（一旦绑定不再改变）。"""
    device = FakeDevice(server.url, device_id="device-1").start()
    assert wait_until(lambda: len(server.connections()) == 1)
    response = server.request(make_envelope(request_id="req-pin", idempotency_key="key-pin"))
    assert response.status == "ok"
    assert server.connection_state("device-1").state == "ONLINE"
    device.close_now()


def test_two_devices_are_routed_by_device_id(server):
    """两台设备各自绑定自己的连接，请求不会串台。"""
    first = FakeDevice(server.url, device_id="device-1", heartbeat_first=True).start()
    second = FakeDevice(server.url, device_id="device-2", heartbeat_first=True).start()
    assert wait_until(lambda: server.connection_state("device-1").state == "ONLINE")
    assert wait_until(lambda: server.connection_state("device-2").state == "ONLINE")

    response_one = server.request(make_envelope(request_id="req-d1", device_id="device-1", idempotency_key="key-d1"))
    response_two = server.request(make_envelope(request_id="req-d2", device_id="device-2", idempotency_key="key-d2"))

    assert response_one.status == "ok" and response_one.device_id == "device-1"
    assert response_two.status == "ok" and response_two.device_id == "device-2"
    assert first.frame_count() == 1 and second.frame_count() == 1
    assert first.decoded_frames()[0]["device_id"] == "device-1"
    assert second.decoded_frames()[0]["device_id"] == "device-2"
    first.close_now()
    second.close_now()


def test_concurrent_requests_from_multiple_threads_all_resolve(server):
    """同步桥必须线程安全：四个线程并发下单，各拿各的响应。"""
    device = FakeDevice(server.url, device_id="device-1", heartbeat_first=True).start()
    assert wait_until(lambda: server.connection_state("device-1").state == "ONLINE")
    results: dict[str, DeviceResponse] = {}
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        envelope = make_envelope(request_id=f"req-c-{index}", idempotency_key=f"key-c-{index}")
        try:
            results[f"req-c-{index}"] = server.request(envelope)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors
    assert len(results) == 4
    for request_id, response in results.items():
        assert response.status == "ok"
        assert response.request_id == request_id
    assert server.stats.requests_sent == 4
    assert server.pending_count == 0
    device.close_now()


# ---------------------------------------------------------------------------
# 帧拒绝
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected_rejections"),
    [("unknown_field", 1), ("missing_field", 1), ("bad_status", 1), ("junk", 1)],
)
def test_bad_device_frames_are_rejected_then_request_times_out(server, mode, expected_rejections):
    """未知字段/缺字段/非法枚举/非 JSON 一律拒绝，请求按 deadline 超时。"""
    device = FakeDevice(server.url, device_id="device-1", mode=mode, heartbeat_first=True).start()
    assert wait_until(lambda: server.connection_state("device-1").state == "ONLINE")

    started = time.monotonic()
    response = server.request(
        make_envelope(request_id=f"req-{mode}", idempotency_key=f"key-{mode}", deadline_ms=200)
    )
    elapsed = time.monotonic() - started

    assert response.status == "failed"
    assert response.error_code == TIMEOUT_ERROR_CODE
    assert server.stats.frames_rejected >= expected_rejections
    assert server.pending_count == 0
    assert elapsed < 3.0
    device.close_now()


def test_counterparty_device_id_mismatch_is_rejected(server):
    """响应里的 device_id 与在途请求不符 → 拒绝，绝不串台。"""
    device = FakeDevice(server.url, device_id="device-1", mode="wrong_device").start()
    assert wait_until(lambda: len(server.connections()) == 1)
    response = server.request(make_envelope(request_id="req-cross", idempotency_key="key-cross", deadline_ms=200))
    assert response.status == "failed"
    assert response.error_code == TIMEOUT_ERROR_CODE
    assert server.stats.frames_rejected == 1
    device.close_now()


def test_outgoing_frame_validation_rejects_unknown_and_missing_fields():
    """主机→设备方向：未知字段、缺字段、协议版本、非法命令都要拦。"""
    good = make_envelope(request_id="req-valid", idempotency_key="key-valid").as_dict()
    assert validate_outgoing_frame(good).request_id == "req-valid"

    with pytest.raises(DeviceFrameError) as unknown:
        validate_outgoing_frame({**good, "debug": "extra"})
    assert unknown.value.code == INVALID_PAYLOAD_ERROR_CODE

    with pytest.raises(DeviceFrameError) as missing:
        validate_outgoing_frame({key: value for key, value in good.items() if key != "signature"})
    assert missing.value.code == INVALID_PAYLOAD_ERROR_CODE

    with pytest.raises(DeviceFrameError) as version:
        validate_outgoing_frame({**good, "protocol_version": 2})
    assert version.value.code == UNSUPPORTED_PROTOCOL_ERROR_CODE

    with pytest.raises(DeviceFrameError) as command:
        validate_outgoing_frame({**good, "command": "device.not.a.command"})
    assert command.value.code == INVALID_PAYLOAD_ERROR_CODE

    with pytest.raises(DeviceFrameError) as unsigned:
        validate_outgoing_frame({**good, "signature": ""})
    assert unsigned.value.code == INVALID_PAYLOAD_ERROR_CODE


def test_decode_device_frame_round_trips_with_protocol_encoder():
    """设备→主机方向：与 protocol.encode_response 生成的帧逐字段一致。"""
    raw = encode_response(
        DeviceResponse(
            request_id="req-enc",
            device_id="device-1",
            status="ok",
            error_code="",
            message="done",
            artifacts=[{"asset_id": "a1"}],
            evidence={"duration_ms": 12},
        )
    )
    parsed = decode_device_frame(raw)
    assert parsed.request_id == "req-enc"
    assert parsed.status == "ok"
    assert parsed.artifacts == [{"asset_id": "a1"}]

    with pytest.raises(DeviceFrameError) as bad_json:
        decode_device_frame("{not json")
    assert bad_json.value.code == DECODE_ERROR_CODE

    with pytest.raises(DeviceFrameError) as unknown:
        decode_device_frame(json.dumps({**json.loads(raw), "debug": "x"}))
    assert unknown.value.code == INVALID_PAYLOAD_ERROR_CODE

    with pytest.raises(DeviceFrameError) as bad_status:
        decode_device_frame(json.dumps({**json.loads(raw), "status": "nope"}))
    assert bad_status.value.code == INVALID_PAYLOAD_ERROR_CODE


def test_request_without_envelope_is_a_failed_response(server):
    response = server.request("not-an-envelope")  # type: ignore[arg-type]
    assert response.status == "failed"
    assert response.error_code == INVALID_PAYLOAD_ERROR_CODE


# ---------------------------------------------------------------------------
# 超时 / pending 泄漏
# ---------------------------------------------------------------------------


def test_deadline_timeout_clears_pending_and_late_reply_is_orphaned(server):
    """超时后 pending 表必须清空；迟到响应进孤儿档，不泄漏 future。"""
    device = FakeDevice(server.url, device_id="device-1", mode="ok", delay_ms=500, heartbeat_first=True).start()
    assert wait_until(lambda: server.connection_state("device-1").state == "ONLINE")

    started = time.monotonic()
    response = server.request(make_envelope(request_id="req-late", idempotency_key="key-late", deadline_ms=150))
    elapsed = time.monotonic() - started

    assert response.status == "failed"
    assert response.error_code == TIMEOUT_ERROR_CODE
    assert elapsed >= 0.1
    assert elapsed < 2.0
    assert server.pending_count == 0
    assert server.stats.timeouts == 1
    assert device.frame_count() == 1

    assert wait_until(lambda: server.stats.orphan_responses >= 1, timeout=5.0)
    assert server.pending_count == 0
    device.close_now()


def test_request_without_any_connection_returns_not_wired(server):
    """没有设备连接时绝不假装成功。"""
    response = server.request(make_envelope(request_id="req-offline", idempotency_key="key-offline"))
    assert response.status == "failed"
    assert response.error_code == NO_CONNECTION_ERROR_CODE
    assert server.stats.requests_sent == 0


def test_stopping_server_fails_inflight_requests(server):
    """停服时在途请求必须被唤醒（同一个 future 不悬挂）。"""
    device = FakeDevice(server.url, device_id="device-1", mode="silent", heartbeat_first=True).start()
    assert wait_until(lambda: server.connection_state("device-1").state == "ONLINE")
    outcome: dict[str, DeviceResponse] = {}

    def worker() -> None:
        outcome["response"] = server.request(
            make_envelope(request_id="req-stop", idempotency_key="key-stop", deadline_ms=30_000)
        )

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert wait_until(lambda: server.pending_count == 1, timeout=5.0)
    server.stop()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert outcome["response"].status == "failed"
    assert outcome["response"].error_code == NO_CONNECTION_ERROR_CODE
    assert server.pending_count == 0
    device.wait_closed(timeout=5.0)


# ---------------------------------------------------------------------------
# 心跳与死连接
# ---------------------------------------------------------------------------


def test_heartbeat_marks_online_and_resets_backoff(server):
    device = FakeDevice(server.url, device_id="device-1", heartbeat_first=True).start()
    assert wait_until(lambda: server.connection_state("device-1").state == "ONLINE")
    status = server.connection_state("device-1")
    assert status.last_heartbeat_ms > 0
    assert status.backoff_seconds == 0
    assert server.stats.heartbeats >= 1

    device.close_now()
    assert server.wait_until_disconnected("device-1", timeout=5.0)
    assert server.connection_state("device-1").state == "BACKOFF"
    assert server.recommended_backoff_seconds("device-1") == 1

    second = FakeDevice(server.url, device_id="device-1", heartbeat_first=True).start()
    assert wait_until(lambda: server.connection_state("device-1").state == "ONLINE")
    # 收到 heartbeat → 退避**清零**（文档固定要求）。
    assert server.recommended_backoff_seconds("device-1") == 0
    assert server.connection_state("device-1").backoff_seconds == 0
    second.close_now()


def test_heartbeat_watchdog_closes_dead_connection_and_clears_pending():
    """应用级心跳看门狗：超时未心跳 → 关连接、清 pending、请求立即失败。"""
    instance = WhaleDeviceServer(
        host="127.0.0.1",
        port=0,
        heartbeat_timeout_ms=250,
        heartbeat_check_interval_ms=50,
    )
    instance.start()
    try:
        device = FakeDevice(instance.url, device_id="device-9", mode="silent", heartbeat_first=True).start()
        assert wait_until(lambda: instance.connection_state("device-9").state == "ONLINE")
        response = instance.request(
            make_envelope(request_id="req-dead", device_id="device-9", idempotency_key="key-dead", deadline_ms=30_000)
        )
        assert response.status == "failed"
        assert response.error_code == NO_CONNECTION_ERROR_CODE
        assert instance.pending_count == 0
        assert instance.wait_until_disconnected("device-9", timeout=5.0)
        assert instance.stats.connections_closed >= 1
        device.wait_closed(timeout=5.0)
    finally:
        instance.stop()


def test_protocol_level_ping_is_configured_on_the_server(server):
    """协议级死连接检测（ping/pong）默认开启，不需要应用层心跳也能发现死连接。"""
    device = FakeDevice(server.url, device_id="device-1", heartbeat_first=True).start()
    assert wait_until(lambda: server.connection_state("device-1").state == "ONLINE")
    assert server.ping_interval == 20.0
    assert server.ping_timeout == 20.0
    device.close_now()
    assert server.wait_until_disconnected("device-1", timeout=5.0)


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


def test_duplicate_idempotency_key_is_not_sent_twice(server):
    """同一 ``idempotency_key`` 第二次直接重放缓存，绝不二次下发。"""
    device = FakeDevice(server.url, device_id="device-1", heartbeat_first=True).start()
    assert wait_until(lambda: server.connection_state("device-1").state == "ONLINE")
    first = server.request(make_envelope(request_id="req-idem-1", idempotency_key="key-idem"))
    second = server.request(make_envelope(request_id="req-idem-2", idempotency_key="key-idem"))

    assert first.status == "ok"
    assert second == first
    assert device.frame_count() == 1
    assert server.stats.requests_sent == 1
    assert server.stats.idempotent_replays == 1
    device.close_now()


def test_idempotency_survives_server_restart_through_task_store():
    """``TaskStore.effects`` 里的幂等结果跨进程/跨实例仍然生效（只读只调，不改存储层）。"""
    store = TaskStore(":memory:")
    first = WhaleDeviceServer(host="127.0.0.1", port=0, store=store)
    first.start()
    device = FakeDevice(first.url, device_id="device-1", heartbeat_first=True).start()
    try:
        assert wait_until(lambda: first.connection_state("device-1").state == "ONLINE")
        envelope = make_envelope(request_id="req-store", idempotency_key="key-store")
        response = first.request(envelope)
        assert response.status == "ok"
        assert store.effect("device:key-store")
        assert device.frame_count() == 1
    finally:
        first.stop()
        device.wait_closed(timeout=5.0)

    second = WhaleDeviceServer(host="127.0.0.1", port=0, store=store)
    second.start()
    try:
        # 完全没有设备连接，但仍然命中持久幂等 → 不判 NOT_WIRED，也不下发。
        replay = second.request(make_envelope(request_id="req-store-2", idempotency_key="key-store"))
        assert replay.status == "ok"
        assert replay.request_id == "req-store"
        assert second.stats.requests_sent == 0
        assert second.stats.idempotent_replays == 1
    finally:
        second.stop()
        store.close()


def test_failed_response_does_not_become_a_persistent_effect(server):
    """失败（超时）不写持久幂等表，允许同键真正重试。"""
    store = TaskStore(":memory:")
    instance = WhaleDeviceServer(host="127.0.0.1", port=0, store=store)
    instance.start()
    try:
        device = FakeDevice(instance.url, device_id="device-1", mode="silent", heartbeat_first=True).start()
        assert wait_until(lambda: instance.connection_state("device-1").state == "ONLINE")
        response = instance.request(make_envelope(request_id="req-f", idempotency_key="key-f", deadline_ms=150))
        assert response.error_code == TIMEOUT_ERROR_CODE
        assert store.effect("device:key-f") is None
        device.close_now()
    finally:
        instance.stop()
        store.close()


def test_repeated_device_frames_do_not_break_the_bridge(server):
    """设备重复回同一 request_id 也不会抛 ``InvalidStateError``。"""
    device = FakeDevice(server.url, device_id="device-1", mode="double", heartbeat_first=True).start()
    assert wait_until(lambda: server.connection_state("device-1").state == "ONLINE")
    response = server.request(make_envelope(request_id="req-double", idempotency_key="key-double"))
    assert response.status == "ok"
    assert response.request_id == "req-double"
    assert server.pending_count == 0
    device.close_now()


# ---------------------------------------------------------------------------
# 安全 / 契约 / 依赖
# ---------------------------------------------------------------------------


def test_declared_error_codes_are_all_registered_in_command_errors():
    """**不新增错误码**：本模块用到的码必须是 ``CommandErrors.ALL`` 的子集。"""
    registered = registered_error_codes()
    assert DECLARED_ERROR_CODES <= registered, sorted(DECLARED_ERROR_CODES - registered)
    assert TIMEOUT_ERROR_CODE == "EXPIRED"
    assert NO_CONNECTION_ERROR_CODE == "NOT_WIRED"
    assert len(DECLARED_ERROR_CODES) == 5


def test_every_response_error_code_seen_in_tests_is_registered(server):
    registered = registered_error_codes()
    offline = server.request(make_envelope(request_id="req-codes", idempotency_key="key-codes"))
    silent_device = FakeDevice(server.url, device_id="device-1", mode="silent", heartbeat_first=True).start()
    assert wait_until(lambda: server.connection_state("device-1").state == "ONLINE")
    timed_out = server.request(make_envelope(request_id="req-codes-2", idempotency_key="key-codes-2", deadline_ms=150))
    for response in (offline, timed_out):
        assert response.status == "failed"
        assert response.error_code in registered
    silent_device.close_now()


def test_logs_never_contain_signature_or_frame_body(server, logs):
    """日志只记事件名与计数；``signature`` 与 payload 正文永远进不去。"""
    envelope = make_envelope(
        request_id="req-log",
        idempotency_key="key-log",
        payload={"package_name": "com.example", "text": "secret-payload-value"},
    )
    device = FakeDevice(server.url, device_id="device-1", heartbeat_first=True).start()
    assert wait_until(lambda: server.connection_state("device-1").state == "ONLINE")
    assert server.request(envelope).status == "ok"
    # 再制造一条拒绝路径（没有该设备的连接），确认拒绝日志同样脱敏。
    ghost = server.request(
        make_envelope(request_id="req-log-2", device_id="device-ghost", idempotency_key="key-log-2")
    )
    assert ghost.status == "failed"
    assert ghost.error_code == NO_CONNECTION_ERROR_CODE

    assert logs, "日志钩子必须被调用过"
    blob = json.dumps(logs, ensure_ascii=False, default=str)
    assert envelope.signature not in blob
    assert "secret-payload-value" not in blob
    assert "com.example" not in blob
    for _event, detail in logs:
        assert "signature" not in detail
        assert "payload" not in detail
    device.close_now()


def test_heartbeat_frame_and_host_helpers_match_the_schemas():
    frame = build_heartbeat_frame("device-1")
    assert set(frame) == RESPONSE_FIELDS
    parsed = decode_device_frame(json.dumps(frame))
    assert is_heartbeat_frame(parsed)
    assert parsed.request_id == HEARTBEAT_REQUEST_ID
    assert parsed.message == HEARTBEAT_MESSAGE
    assert not is_heartbeat_frame(DeviceResponse(request_id="req-1", device_id="device-1", status="ok"))

    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("::1")
    assert is_loopback_host("localhost")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("10.1.2.3")
    assert not is_loopback_host("")


def test_plaintext_ws_is_refused_outside_loopback():
    """生产必须 wss：非回环地址没有 TLS 上下文时直接拒绝启动。"""
    instance = WhaleDeviceServer(host="0.0.0.0", port=0)
    with pytest.raises(DeviceServerError) as error:
        instance.start()
    assert "wss://" in str(error.value)
    assert not instance.is_running


def test_wss_round_trip_with_real_tls_handshake(tls_pair):
    """``wss://`` 路径真跑一遍：自签证书 + 真 TLS 握手 + 真帧收发。"""
    server_ctx, client_ctx = tls_pair
    instance = WhaleDeviceServer(host="127.0.0.1", port=0, ssl_context=server_ctx)
    instance.start()
    try:
        assert instance.url.startswith("wss://")
        device = FakeDevice(instance.url, device_id="device-1", heartbeat_first=True, ssl_ctx=client_ctx).start()
        assert wait_until(lambda: instance.connection_state("device-1").state == "ONLINE")
        response = instance.request(make_envelope(request_id="req-tls", idempotency_key="key-tls"))
        assert response.status == "ok"
        assert device.frame_count() == 1
        device.close_now()
    finally:
        instance.stop()


def test_missing_websockets_runtime_reports_a_clear_error(monkeypatch):
    """``websockets`` 缺失时必须抛明确错误，而不是崩或静默降级。"""
    monkeypatch.setattr(ds, "_WEBSOCKETS_AVAILABLE", False)
    monkeypatch.setattr(ds, "_ws_serve", None)
    monkeypatch.setattr(ds, "_WEBSOCKETS_IMPORT_ERROR", "simulated import failure")
    assert ds.websockets_available() is False
    instance = WhaleDeviceServer(host="127.0.0.1", port=0)
    with pytest.raises(DeviceServerDependencyError) as error:
        instance.start()
    assert "websockets" in str(error.value)
    assert not instance.is_running


def test_duplicate_start_and_idempotent_stop(server):
    with pytest.raises(DeviceServerError):
        server.start()
    server.stop()  # 二次 stop 是安全的空操作
    assert not server.is_running
    assert server.connection_state("never-connected").state == "OFFLINE"