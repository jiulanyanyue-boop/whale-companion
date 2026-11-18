"""M05 端到端 WSS 链路验证：主机服务端 ↔ 模拟设备客户端。

这份测试要回答的问题只有一个：**这条链路真的能跑吗？**

两边的单测各自都绿，但它们从来没有互相连过。同一个协议被实现对了两遍、方向对反
了一处、握手路径写错了，单测都不会响——只有把两端接起来才会。所以本文件里
**没有任何 mock**：起一个真 ``WhaleDeviceServer``、绑真端口、跑真 TLS/WS 握手、
发真 JSON 帧，另一端是 ``e2e_client_sim.DeviceClientSim``（按手机端的位置独立实现的
WSS 客户端）。

六条链路：

1. **握手** —— ``start()`` 绑真端口，客户端真连上（HTTP ``101`` 升级），心跳后连接
   绑上 ``device_id`` 并标 ONLINE。
2. **命令下发 → 响应回传** —— 同步 ``transport.request(envelope)`` 把帧写到 socket，
   客户端回合法响应，调用方拿到的 ``DeviceResponse`` 内容**逐字段正确**。
3. **``deadline_ms`` 超时** —— 客户端**故意不回**，``request()`` 必须在 deadline 之后
   **返回失败**（``EXPIRED``），绝不能永久阻塞。这是最关键的一条：同步↔异步桥接
   写错就会死锁整个主机。
4. **断线清理** —— 客户端**突然 abort**（不是体面的 close），在途 ``request()`` 必须
   **快速**失败（``NOT_WIRED``）、``pending_count`` 归零。
5. **协议拒绝** —— 客户端回带未知字段的响应，服务端必须拒（``frames_rejected`` 计数）
   且**绝不**把它当成响应交给在途请求。
6. **多连接** —— 同时连两个客户端时行为必须**明确**：谁都没绑定时拒绝下发
   （``NOT_WIRED``，一帧都不写），绑定之后只发给绑定的那条。

线程纪律（这是本文件最容易写错的地方）：

- 同步 ``request()`` **一定**在独立线程里跑。它内部会 ``Future.result(timeout)``
  阻塞；如果放在主线程，``server.stop()`` 与断言全都没机会执行，测试直接挂死。
- 每次等待都带**外部超时**（``RequestCall.wait`` / ``wait_for``），所以「桥接坏掉」
  表现为**断言失败**，而不是 CI 卡住。
- 不用固定 ``sleep`` 猜时序：状态变化一律靠 ``Event`` / ``Condition`` / 条件等待。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from contextlib import contextmanager
from typing import Any

import pytest

pytest.importorskip("websockets")

from astrbot_whale_device.device_server import (  # noqa: E402
    DEFAULT_PATH,
    TIMEOUT_ERROR_CODE,
    NO_CONNECTION_ERROR_CODE,
    ENVELOPE_FIELDS,
    RESPONSE_FIELDS,
    WebSocketDeviceTransport,
    WhaleDeviceServer,
)
from astrbot_whale_device.models import DeviceEnvelope, DeviceResponse  # noqa: E402
from astrbot_whale_device.security import sign_hmac  # noqa: E402
import e2e_client_sim as sim  # noqa: E402

SIGNING_SECRET = b"whale-e2e-signing-secret"

#: 超时类测试用的短 deadline（协议下限是 100ms，这里给 500ms 留余量但绝不让 CI 久等）。
SHORT_DEADLINE_MS = 500
#: 断线测试用一个**很长**的 deadline：只有桥接真的靠「断线通知」唤醒，才可能这么快返回。
LONG_DEADLINE_MS = 3_000

DEVICE_ID = "device-e2e"


# ---------------------------------------------------------------------------
# 同步 request() 的独立线程封装
# ---------------------------------------------------------------------------


def make_envelope(
    *,
    request_id: str,
    device_id: str = DEVICE_ID,
    command: str = "device.app.list",
    deadline_ms: int = LONG_DEADLINE_MS,
    idempotency_key: str | None = None,
    payload: dict[str, Any] | None = None,
    counter: int = 1,
) -> DeviceEnvelope:
    """构造一个**真签名**信封（与 ``plugin.py`` 的下发路径同形）。"""
    envelope = DeviceEnvelope(
        protocol_version=1,
        request_id=request_id,
        device_id=device_id,
        command=command,
        deadline_ms=deadline_ms,
        idempotency_key=idempotency_key or f"key-{request_id}",
        payload=dict(payload or {}),
        counter=counter,
        issued_at_ms=int(time.time() * 1000),
    )
    return DeviceEnvelope(
        protocol_version=1,
        request_id=envelope.request_id,
        device_id=envelope.device_id,
        command=envelope.command,
        deadline_ms=envelope.deadline_ms,
        idempotency_key=envelope.idempotency_key,
        payload=envelope.payload,
        counter=envelope.counter,
        issued_at_ms=envelope.issued_at_ms,
        signature=sign_hmac(envelope.as_dict(), SIGNING_SECRET),
    )


def new_request_id(tag: str) -> str:
    """每次调用都唯一，避免幂等缓存把两次请求合成一次。"""
    return f"{tag}-{time.monotonic_ns()}"


class RequestCall:
    """在**独立线程**里跑同步 ``request()``，并带外层超时。

    这是整套测试的地基：``request()`` 是同步阻塞的，跑在主线程会卡死事件循环。
    同时 ``wait()`` 有超时——桥接一旦死锁，这里报断言失败，CI 不会被吊住。
    """

    def __init__(self, transport: WebSocketDeviceTransport, envelope: DeviceEnvelope) -> None:
        self._transport = transport
        self._envelope = envelope
        self._done = threading.Event()
        self._future: Future[DeviceResponse] = Future()
        self.result: DeviceResponse | None = None
        self.error: BaseException | None = None
        self.elapsed: float = -1.0
        self._thread = threading.Thread(
            target=self._run, name=f"sync-request-{envelope.request_id}", daemon=True
        )

    def start(self) -> "RequestCall":
        self._thread.start()
        return self

    def _run(self) -> None:
        started = time.monotonic()
        try:
            self.result = self._transport.request(self._envelope)
        except BaseException as exc:  # noqa: BLE001 - 交给断言层判断
            self.error = exc
        finally:
            self.elapsed = time.monotonic() - started
            self._done.set()

    @property
    def finished(self) -> bool:
        return self._done.is_set()

    def wait(self, timeout: float = 5.0) -> DeviceResponse:
        """等 ``request()`` 返回；超时即是「桥接阻塞」这个 bug，**明确失败**。"""
        if not self._done.wait(timeout):
            raise AssertionError(
                f"sync request() did not return within {timeout}s — "
                f"the sync↔async bridge is blocking (deadline_ms={self._envelope.deadline_ms})"
            )
        if self.error is not None:
            raise AssertionError(f"sync request() raised instead of returning a response: {self.error!r}")
        assert self.result is not None
        return self.result


def run_request(transport: WebSocketDeviceTransport, envelope: DeviceEnvelope, timeout: float = 5.0) -> DeviceResponse:
    """同步调用便捷入口：起线程 → 立刻等结果。"""
    return RequestCall(transport, envelope).start().wait(timeout)


def wait_for(predicate: Any, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """等一个服务端侧状态条件成立。

    只「等条件成立」而不猜时长：条件一满立刻返回，超时才判失败。用于观测服务端
    内部状态（如心跳绑定后的 ONLINE），这些状态变化没有回调可以接。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


# ---------------------------------------------------------------------------
# fixtures / 上下文管理器
# ---------------------------------------------------------------------------


@contextmanager
def running_server(logs: list[tuple[str, dict[str, Any]]] | None = None, **kwargs: Any):
    """起一个真服务端（真实端口），**无论断言成败都 stop()**，绝不泄漏端口。

    ``logs`` 可以在外面传进来：``stop()`` 自己也会写日志，所以「停服」这类断言必须
    在 ``with`` 块**退出之后**再做（块内断言永远看不到收尾日志）。
    """
    sink: list[tuple[str, dict[str, Any]]] = logs if logs is not None else []
    options: dict[str, Any] = {
        "host": "127.0.0.1",
        "port": 0,
        "log": lambda event, detail: sink.append((event, dict(detail))),
    }
    options.update(kwargs)
    server = WhaleDeviceServer(**options)
    server.start()
    try:
        yield server, sink
    finally:
        try:
            server.stop()
        finally:
            assert not server.is_running, "device server thread leaked after stop()"


@contextmanager
def connected_client(url: str, **kwargs: Any):
    """连一个模拟设备，**无论断言成败都关掉**。"""
    client = sim.DeviceClientSim(url, **kwargs).start()
    try:
        yield client
    finally:
        client.stop()


def device_url(server: WhaleDeviceServer) -> str:
    """用服务端**自己**报的地址去连——路径错了这里就会连不上（这本身是链路的一部分）。"""
    assert server.path == DEFAULT_PATH, f"unexpected handshake path: {server.path!r}"
    return server.url


# ---------------------------------------------------------------------------
# 链路 1：握手
# ---------------------------------------------------------------------------


def test_chain1_handshake_really_connects():
    """真握手：HTTP 101 升级 → 心跳绑定 ``device_id`` → 连接标 ONLINE。"""
    logs: list[tuple[str, dict[str, Any]]] = []
    with running_server(logs) as (server, sink):
        with connected_client(
            device_url(server), device_id=DEVICE_ID, heartbeat_first=True
        ) as client:
            # 1) 真的升级成 WebSocket 了（不是失败后静默重试）。
            assert client.handshake_status() == 101, "WSS handshake did not upgrade to 101"
            assert client.error is None, f"client errored during handshake: {client.error!r}"

            # 2) 服务端把这条连接登记进连接表。
            #    注意：客户端"握手成功"早于服务端 handler 登记，所以这里要等条件，
            #    不能立刻断言（否则就是拿客户端侧事实当服务端侧事实，会偶发）。
            assert wait_for(lambda: len(server.connections()) == 1, timeout=3.0), (
                "server did not register the connection"
            )
            assert server.stats.connections_opened == 1

            # 3) 心跳帧（严格 device-events 形状）让连接绑定到 device_id 并 ONLINE。
            assert wait_for(lambda: server.connection_state(DEVICE_ID).state == "ONLINE"), (
                f"connection never became ONLINE; state={server.connection_state(DEVICE_ID)!r}"
            )
            assert server.stats.heartbeats == 1, "heartbeat was not accounted for"
            # 心跳日志在块内就能看到（不依赖 stop）。
            assert any(event == "device.connected" for event, _ in logs)
            assert any(event == "device.heartbeat" for event, _ in logs)

        # 4) 客户端关闭后服务端连接表回到空、断开计数 +1。
        assert client.wait_closed(), "client did not observe its own close"
        assert wait_for(lambda: server.connections() == [], timeout=3.0), (
            "server connection table was not cleaned up after disconnect"
        )
        assert server.stats.connections_closed == 1

    # 5) 停服收尾（``stop()`` 在 ``with`` 退出时执行，所以这一条只能在块外断言）。
    assert any(event == "device.server_stopped" for event, _ in logs), (
        f"server did not log its own shutdown: {[event for event, _ in logs]}"
    )


# ---------------------------------------------------------------------------
# 链路 2：命令下发 → 响应回传（内容必须正确）
# ---------------------------------------------------------------------------


def test_chain2_command_round_trip_returns_correct_content():
    """主机写的字节 → 设备收到 → 设备回的字节 → 主机解出**内容正确**的响应。"""
    payload = {"text": "米饭很好吃", "n": 7}  # 含非 ASCII，顺带压一遍 canonical 转义
    with running_server() as (server, _logs):
        transport = WebSocketDeviceTransport(server)
        with connected_client(
            device_url(server),
            device_id=DEVICE_ID,
            reply_message="e2e-ok",
            reply_artifacts=[{"kind": "screenshot", "uri": "file:///tmp/a.png"}],
            reply_evidence={"observed": True, "count": 3},
        ) as client:
            request_id = new_request_id("chain2")
            envelope = make_envelope(
                request_id=request_id, command="device.app.list", payload=payload
            )

            call = RequestCall(transport, envelope).start()
            # 客户端真的收到了这一帧（说明主机确实写到了 socket）。
            received = client.wait_for_received(lambda frame: frame.get("request_id") == request_id, timeout=5.0)
            response = call.wait(timeout=5.0)

            # 设备侧看到的信封：字段集合与契约完全一致，且业务字段值正确。
            assert set(received) == set(ENVELOPE_FIELDS), (
                f"host wrote a frame outside the envelope contract: {sorted(set(received) ^ set(ENVELOPE_FIELDS))}"
            )
            assert received["command"] == "device.app.list"
            assert received["device_id"] == DEVICE_ID
            assert received["protocol_version"] == 1
            assert received["deadline_ms"] == LONG_DEADLINE_MS
            assert received["payload"] == payload

            # 调用方拿到的响应：逐字段正确（不是「有个对象就行」）。
            assert response.request_id == request_id
            assert response.device_id == DEVICE_ID
            assert response.status == "ok"
            assert response.error_code == ""
            assert response.message == "e2e-ok"
            assert response.artifacts == [{"kind": "screenshot", "uri": "file:///tmp/a.png"}]
            assert response.evidence == {"observed": True, "count": 3}

            assert server.stats.requests_sent == 1
            assert server.stats.responses_received == 1
            assert server.pending_count == 0


def test_chain2b_host_envelope_signature_verifies_with_device_canonical_rules():
    """主机发出的信封，用**设备端**（``CanonicalJson.kt``）的规则能不能验过。

    这是两端唯一的字节级契约：主机用 Python ``sort_keys / 无空格 / ensure_ascii=False``
    做 canonical 输入，设备端手写了一份同样的规则。两边错一处，签名就永远验不过，
    而这种错各自的单测都发现不了。
    """
    payload = {"text": "鲸鱼娘吃米饭", "mixed": {"b": 2, "a": ["/", "\u007f", "\u0001"]}}
    with running_server() as (server, _logs):
        transport = WebSocketDeviceTransport(server)
        with connected_client(device_url(server), device_id=DEVICE_ID) as client:
            request_id = new_request_id("chain2b")
            envelope = make_envelope(request_id=request_id, command="device.ui.observe", payload=payload)

            call = RequestCall(transport, envelope).start()
            received = client.wait_for_received(lambda frame: frame.get("request_id") == request_id)
            call.wait(timeout=5.0)

            assert sim.verify_envelope_signature(received, SIGNING_SECRET), (
                "device-side canonical rules could not verify the host signature; "
                f"device canonical text: {sim.canonical_unsigned(received)!r}"
            )
            assert received["payload"] == payload


# ---------------------------------------------------------------------------
# 链路 3：deadline_ms 超时（最关键的一条）
# ---------------------------------------------------------------------------


def test_chain3_deadline_ms_timeout_returns_expired_instead_of_blocking_forever():
    """客户端故意不回 → ``request()`` 必须在 deadline 后**返回失败**，不许永久阻塞。

    如果同步↔异步桥接把「等响应」写成没有 deadline 的 ``future.result()``，
    这条测试会撞上 ``RequestCall.wait`` 的 5 秒上限并**报断言失败**（而不是挂死 CI）。
    """
    with running_server() as (server, logs):
        transport = WebSocketDeviceTransport(server)
        with connected_client(
            device_url(server), device_id=DEVICE_ID, behavior=sim.BEHAVIOR_SILENT
        ) as client:
            request_id = new_request_id("chain3")
            envelope = make_envelope(
                request_id=request_id, deadline_ms=SHORT_DEADLINE_MS
            )
            call = RequestCall(transport, envelope).start()
            # 先确认帧确实发出去了：超时必须是「发出去了但没人回」，不是「没发出去」。
            client.wait_for_received(lambda frame: frame.get("request_id") == request_id)

            response = call.wait(timeout=5.0)

            assert response.status == "failed"
            assert response.error_code == TIMEOUT_ERROR_CODE
            assert response.request_id == request_id
            assert response.device_id == DEVICE_ID
            # 真的等到了 deadline（不是立刻返回的假超时）…
            assert call.elapsed >= (SHORT_DEADLINE_MS / 1000.0) * 0.8, (
                f"request() returned too early to be a real deadline wait: {call.elapsed:.3f}s"
            )
            # …而且没有拖过头（桥接没有多挂一段）。
            assert call.elapsed <= (SHORT_DEADLINE_MS / 1000.0) * 4.0, (
                f"request() overshot the deadline badly: {call.elapsed:.3f}s"
            )
            assert server.stats.timeouts == 1
            assert server.pending_count == 0, "pending request leaked after deadline timeout"
            assert any(event == "device.command_timed_out" for event, _ in logs)


# ---------------------------------------------------------------------------
# 链路 4：断线清理
# ---------------------------------------------------------------------------


def test_chain4_abrupt_disconnect_fails_inflight_request_fast_and_clears_pending():
    """客户端**突然**断（abort，没有 close 帧）→ 在途 ``request()`` 快速失败、pending 归零。

    deadline 设成 3 秒：只有服务端真的收到「连接没了」并唤醒 future，才可能远早于
    deadline 返回。所以「快速」这条断言证明的是**断线通知链**能跑，而不是靠 deadline。
    """
    with running_server() as (server, logs):
        transport = WebSocketDeviceTransport(server)
        # 客户端**静默**：请求会真的挂在途，这样「断线唤醒」才是唯一能提前结束它的路径。
        with connected_client(
            device_url(server), device_id=DEVICE_ID, behavior=sim.BEHAVIOR_SILENT
        ) as client:
            request_id = new_request_id("chain4")
            envelope = make_envelope(request_id=request_id, deadline_ms=LONG_DEADLINE_MS)
            call = RequestCall(transport, envelope).start()
            client.wait_for_received(lambda frame: frame.get("request_id") == request_id)
            assert server.pending_count == 1, "the in-flight request was not registered as pending"

            # 突然断线：不是 1000 normal closure，服务端只能靠连接丢失感知。
            client.close_abrupt()
            assert client.wait_closed(timeout=5.0), "abrupt abort did not tear down the client"

            response = call.wait(timeout=5.0)
            assert response.status == "failed"
            assert response.error_code == NO_CONNECTION_ERROR_CODE
            assert response.request_id == request_id
            # 远早于 3 秒的 deadline：证明是断线唤醒的，不是等超时。
            assert call.elapsed < LONG_DEADLINE_MS / 1000.0 / 2.0, (
                f"request() was not woken by the disconnect ({call.elapsed:.3f}s)"
            )
            assert server.pending_count == 0, "pending request leaked after abrupt disconnect"
            assert wait_for(lambda: server.connections() == [], timeout=3.0)
            assert server.stats.connections_closed == 1
            assert server.stats.timeouts == 0, (
                "the request was resolved by the deadline instead of by the disconnect"
            )
            assert any(event == "device.disconnected" for event, _ in logs)


# ---------------------------------------------------------------------------
# 链路 5：协议拒绝
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "behavior, expected_rejections",
    [
        (sim.BEHAVIOR_UNKNOWN_FIELD, 1),
        (sim.BEHAVIOR_BAD_STATUS, 1),
        (sim.BEHAVIOR_JUNK, 1),
    ],
)
def test_chain5_protocol_rejects_nonconforming_response(behavior, expected_rejections):
    """设备回了不合契约的帧 → 服务端必须拒，且**不得**把它当成响应。

    拒了以后在途请求拿不到结果，于是只能按 deadline 失败（``EXPIRED``）——这正是
    严格性的证据：宁可超时，也不把脏帧当结果交给上层。
    """
    with running_server() as (server, logs):
        transport = WebSocketDeviceTransport(server)
        with connected_client(
            device_url(server), device_id=DEVICE_ID, behavior=behavior
        ) as client:
            request_id = new_request_id(f"chain5-{behavior}")
            envelope = make_envelope(request_id=request_id, deadline_ms=SHORT_DEADLINE_MS)
            call = RequestCall(transport, envelope).start()
            client.wait_for_received(lambda frame: frame.get("request_id") == request_id)

            response = call.wait(timeout=5.0)

            assert server.stats.frames_rejected >= expected_rejections, (
                f"server accepted a non-conforming frame (behavior={behavior!r}); "
                f"frames_rejected={server.stats.frames_rejected}"
            )
            assert response.status == "failed"
            assert response.error_code == TIMEOUT_ERROR_CODE
            assert response.message, "failure response must carry a human-readable reason"
            # 脏帧没有变成「响应」：响应计数不涨，pending 也没有泄漏。
            assert server.stats.responses_received == 0
            assert server.pending_count == 0
            assert any(event == "device.frame_rejected" for event, _ in logs), (
                f"no frame_rejected log was emitted for behavior={behavior!r}"
            )


def test_chain5b_overlapping_frame_fields_exactly_match_the_event_contract():
    """两侧字段表就是契约本身：一侧多一个字段，端到端就会被拒。"""
    with running_server() as (server, _logs):
        # 服务端拦未知字段时用的白名单必须与 device-events 契约的 7 个字段一致。
        assert set(RESPONSE_FIELDS) == set(sim.RESPONSE_FIELDS), (
            "client simulator and server disagree on the response contract fields: "
            f"{sorted(set(RESPONSE_FIELDS) ^ set(sim.RESPONSE_FIELDS))}"
        )
        with connected_client(device_url(server), device_id=DEVICE_ID) as client:
            assert client.handshake_status() == 101


# ---------------------------------------------------------------------------
# 链路 6：多连接
# ---------------------------------------------------------------------------


def test_chain6_two_connections_are_unambiguous():
    """两个客户端同时连上时，服务端行为必须明确：先拒绝下发，绑定后只发给绑定那条。"""
    with running_server() as (server, logs):
        transport = WebSocketDeviceTransport(server)
        with connected_client(device_url(server), device_id="device-e2e-a") as client_a:
            with connected_client(device_url(server), device_id="device-e2e-b") as client_b:
                # 服务端的登记发生在它自己的 handler 里，晚于客户端握手成功——所以等条件，
                # 不拿客户端侧的事件当服务端状态用（那正是偶发失败的来源）。
                assert wait_for(lambda: len(server.connections()) == 2, timeout=5.0), (
                    f"server did not register both connections: {server.connections()!r}"
                )
                assert server.stats.connections_opened == 2

                # 阶段 1：两条候选、谁都没绑定 → 服务端**明确拒绝**，一帧都不写。
                request_id = new_request_id("chain6-ambiguous")
                response = run_request(
                    transport, make_envelope(request_id=request_id, deadline_ms=SHORT_DEADLINE_MS)
                )
                assert response.status == "failed"
                assert response.error_code == NO_CONNECTION_ERROR_CODE, (
                    f"ambiguous connection must fail clearly, got {response.error_code!r}"
                )
                assert "no device connection" in response.message, (
                    f"failure message is not explicit about the missing connection: {response.message!r}"
                )
                assert client_a.envelopes() == [] and client_b.envelopes() == [], (
                    "server wrote a frame to an unbound connection while two were open"
                )
                assert any(event == "device.command_not_sent" for event, _ in logs)

                # 阶段 2：A 发心跳把自己绑到 device-e2e-a → 请求只发给 A，B 一个字节都收不到。
                client_a.send_heartbeat()
                assert wait_for(
                    lambda: server.connection_state("device-e2e-a").state == "ONLINE"
                ), "client A never became the bound connection"

                routed_id = new_request_id("chain6-routed")
                envelope = make_envelope(
                    request_id=routed_id, device_id="device-e2e-a", deadline_ms=SHORT_DEADLINE_MS
                )
                call = RequestCall(transport, envelope).start()
                client_a.wait_for_received(lambda frame: frame.get("request_id") == routed_id)
                response_b = call.wait(timeout=5.0)

                assert response_b.status == "ok", (
                    f"bound connection did not answer: {response_b.error_code!r} {response_b.message!r}"
                )
                assert response_b.device_id == "device-e2e-a"
                assert client_b.envelopes() == [], (
                    "server leaked a frame to the other device's connection"
                )
                assert server.stats.responses_received == 1
                assert server.pending_count == 0
