"""M03 OneBot v11 HTTP 客户端的真回环测试。

本文件所有 HTTP 交互都用标准库 `http.server` 起**真实本地服务**：真发真收、
真读响应体、真触发超时与连接拒绝。HTTP 层没有任何 mock。
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from astrbot_whale_device import onebot_client as onebot_client_module
from astrbot_whale_device.onebot import OneBotMediaError, OneBotSender
from astrbot_whale_device.onebot_client import (
    SEND_GROUP_ENDPOINT,
    SEND_PRIVATE_ENDPOINT,
    TOKEN_MASK,
    OneBotAPIError,
    OneBotClient,
    OneBotClientError,
    OneBotHTTPError,
    OneBotProtocolError,
    OneBotTimeoutError,
    OneBotTransportError,
)
from astrbot_whale_device.onebot_lifecycle import OneBotLifecycle, OutboundAction
from astrbot_whale_device.storage import TaskStore

TOKEN = "whale-access-token-9f3a"
PRIVATE_OK = {"status": "ok", "retcode": 0, "data": {"message_id": 4242}}


def json_body(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


def default_respond(record, count):
    return 200, json_body(PRIVATE_OK)


def ok_respond(message_id: int = 4242):
    def respond(record, count):
        return 200, json_body({"status": "ok", "retcode": 0, "data": {"message_id": message_id}})

    return respond


class DropConnection(Exception):
    """让 stub 服务端在读完请求后直接断开连接（模拟连接被重置）。"""


class StubHandler(BaseHTTPRequestHandler):
    """记录请求并按 server.respond 的脚本应答。"""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        record = {
            "path": self.path,
            "headers": {key.lower(): value for key, value in self.headers.items()},
            "raw": raw.decode("utf-8", "replace"),
        }
        server = self.server
        with server.lock:
            server.requests.append(record)
            count = len(server.requests)
        try:
            status, body = server.respond(record, count)
        except DropConnection:  # 真·连接重置：不写任何响应就断开
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.connection.close()
            except OSError:
                pass
            return
        except Exception as exc:  # responder 自身出错也要给出可读结果
            status, body = 500, json_body({"status": "failed", "retcode": -1, "error": str(exc)})
        data = body.encode("utf-8") if isinstance(body, str) else bytes(body)
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            # 客户端超时后主动断开：写响应失败是预期行为。
            pass

    def log_message(self, *args) -> None:  # 静默，避免污染测试输出
        return


class OneBotStubServer(ThreadingHTTPServer):
    """真实 HTTP 服务：记录每个请求，支持按请求序号脚本化应答。"""

    daemon_threads = True

    def __init__(self, respond=default_respond) -> None:
        super().__init__(("127.0.0.1", 0), StubHandler)
        self.respond = respond
        self.requests: list[dict] = []
        self.lock = threading.Lock()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def single_request(self) -> dict:
        assert len(self.requests) == 1, f"expected exactly one HTTP request, got {len(self.requests)}"
        return self.requests[0]

    def payloads(self) -> list[dict]:
        return [json.loads(item["raw"]) for item in self.requests]

    def handle_error(self, request, client_address) -> None:
        return


@pytest.fixture
def server_factory():
    created: list[OneBotStubServer] = []

    def start(respond=default_respond) -> OneBotStubServer:
        server = OneBotStubServer(respond)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        created.append(server)
        return server

    yield start
    for server in created:
        server.shutdown()
        server.server_close()


def _closed_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def make_client(server: OneBotStubServer, **kwargs) -> OneBotClient:
    return OneBotClient(server.base_url, **kwargs)


# --------------------------------------------------------------------------
# 正常发送（真回环）
# --------------------------------------------------------------------------
def test_private_message_roundtrip(server_factory):
    server = server_factory()
    client = make_client(server, timeout=5.0)
    result = client.send_private_message("10001", [{"type": "text", "data": {"text": "在的呀"}}])

    assert result["status"] == "ok"
    assert result["retcode"] == 0
    assert result["message_id"] == 4242
    request = server.single_request()
    assert request["path"] == SEND_PRIVATE_ENDPOINT
    assert json.loads(request["raw"]) == {
        "user_id": 10001,
        "message": [{"type": "text", "data": {"text": "在的呀"}}],
    }
    assert request["headers"]["content-type"] == "application/json"
    assert "authorization" not in request["headers"]
    assert client.requests_sent == 1


def test_private_message_accepts_plain_string(server_factory):
    server = server_factory()
    client = make_client(server)
    client.send_private_message(10001, "米饭煮好了哦")
    assert json.loads(server.single_request()["raw"]) == {
        "user_id": 10001,
        "message": [{"type": "text", "data": {"text": "米饭煮好了哦"}}],
    }


def test_group_message_roundtrip(server_factory):
    server = server_factory()
    client = make_client(server)
    client.send_group_message("555", [{"type": "text", "data": {"text": "群聊回复"}}])
    request = server.single_request()
    assert request["path"] == SEND_GROUP_ENDPOINT
    assert json.loads(request["raw"]) == {
        "group_id": 555,
        "message": [{"type": "text", "data": {"text": "群聊回复"}}],
    }


def test_five_segment_types_match_onebot_sender_output(tmp_path, server_factory):
    """五种段：client 发出去的字节必须与 OneBotSender 构造的段完全一致。"""
    image = tmp_path / "clip.png"
    image.write_bytes(b"\x89PNG")
    record = tmp_path / "clip.amr"
    record.write_bytes(b"AMR")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"MP4")

    server = server_factory()
    client = make_client(server)
    sender = OneBotSender(client.send_private_message, allowed_media_roots=[tmp_path])
    media = [
        {"type": "face", "id": 14},
        {"type": "image", "file": str(image)},
        {"type": "record", "file": str(record)},
        {"type": "video", "file": str(video)},
    ]
    expected = sender.build_outbound_segments("10001", "五段齐发", media)
    assert [item["type"] for item in expected] == ["text", "face", "image", "record", "video"]

    sender.send_private("10001", "五段齐发", media, idempotency_key="key-5seg")
    body = json.loads(server.single_request()["raw"])
    assert body["user_id"] == 10001
    assert body["message"] == expected
    assert body["idempotency_key"] == "key-5seg"


@pytest.mark.parametrize("kind", ["face", "image", "record", "video"])
def test_each_segment_type_serializes_correctly(kind, tmp_path, server_factory):
    media_file = tmp_path / "asset.bin"
    media_file.write_bytes(b"data")
    segment = (
        {"type": "face", "data": {"id": "7"}}
        if kind == "face"
        else {"type": kind, "data": {"file": str(media_file)}}
    )
    server = server_factory()
    client = make_client(server)
    client.send_private_message("10001", [segment])
    body = json.loads(server.single_request()["raw"])
    expected = {"type": kind, "data": {"id": "7"}} if kind == "face" else {
        "type": kind,
        "data": {"file": str(media_file)},
    }
    assert body["message"] == [expected]


def test_client_instance_is_usable_as_sender_callback(server_factory):
    """实例可直接传给 OneBotSender（__call__），并带上幂等键。"""
    server = server_factory()
    client = make_client(server)
    sender = OneBotSender(client)
    sender.send_private("10001", "在的", idempotency_key="key-direct")
    body = json.loads(server.single_request()["raw"])
    assert body["message"] == [{"type": "text", "data": {"text": "在的"}}]
    assert body["idempotency_key"] == "key-direct"


def test_bind_sender_delegates_segment_building(server_factory):
    server = server_factory()
    client = make_client(server)
    sender = client.bind_sender()
    assert isinstance(sender, OneBotSender)
    sender.send_private("10001", "绑定成功")
    assert json.loads(server.single_request()["raw"])["message"] == [
        {"type": "text", "data": {"text": "绑定成功"}}
    ]


# --------------------------------------------------------------------------
# 未知段：绝不降级为文本 URL（且不发任何请求）
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kind",
    ["at", "reply", "json", "mface", "node", "poke", "image_url", "unknown", "TEXT"],
)
def test_unknown_segments_refused_without_any_http(kind, server_factory):
    server = server_factory()
    client = make_client(server)
    with pytest.raises(OneBotMediaError) as excinfo:
        client.send_private_message(
            "10001",
            [{"type": kind, "data": {"url": "https://cdn.example.com/secret.png", "qq": "10002"}}],
        )
    assert server.requests == []
    assert client.requests_sent == 0
    # 不降级证明：异常里既没有 URL，也没有被当成文本发出去。
    assert "cdn.example.com" not in str(excinfo.value)
    assert "https" not in str(excinfo.value)


@pytest.mark.parametrize(
    "payload",
    [
        [{"type": "image", "data": {"file": "https://cdn.example.com/a.png"}}],
        [{"type": "record", "data": {"file": "http://cdn.example.com/a.amr"}}],
        [{"type": "video", "data": {"file": "base64://AAAA"}}],
        [{"type": "image", "data": {"file": "data:image/png;base64,AAAA"}}],
        [{"type": "image", "data": {"file": "ftp://host/a.png"}}],
        [{"type": "record", "data": {"file": ""}}],
        [{"type": "image", "data": {}}],
    ],
)
def test_url_and_broken_media_segments_refused(payload, server_factory):
    server = server_factory()
    client = make_client(server)
    with pytest.raises(OneBotMediaError):
        client.send_private_message("10001", payload)
    assert server.requests == []


def test_unresolved_asset_reference_refused(server_factory):
    server = server_factory()
    client = make_client(server)
    with pytest.raises(OneBotMediaError, match="unresolved asset reference"):
        client.send_private_message("10001", [{"type": "image", "data": {"file": "asset:abc"}}])
    assert server.requests == []


@pytest.mark.parametrize(
    "message",
    [[], "", None, 42, {"type": "text", "data": {"text": "x"}}, [{"type": "text", "data": {}}]],
)
def test_empty_or_malformed_message_refused(message, server_factory):
    server = server_factory()
    client = make_client(server)
    with pytest.raises(OneBotMediaError):
        client.send_private_message("10001", message)
    assert server.requests == []


@pytest.mark.parametrize("user_id", ["", "nickname", "10001号", True, -1, 3.5, None, "1" * 21])
def test_non_numeric_targets_refused(user_id, server_factory):
    server = server_factory()
    client = make_client(server)
    with pytest.raises(OneBotMediaError):
        client.send_private_message(user_id, "你好")
    assert server.requests == []


# --------------------------------------------------------------------------
# 失败路径：retcode / HTTP / 协议，且一次调用只发一次
# --------------------------------------------------------------------------
def test_nonzero_retcode_is_failure_and_sends_once(server_factory):
    server = server_factory(
        lambda record, count: (200, json_body({"status": "failed", "retcode": 100, "message": "bad"}))
    )
    client = make_client(server)
    with pytest.raises(OneBotAPIError) as excinfo:
        client.send_private_message("10001", "你好")
    assert excinfo.value.retcode == 100
    assert excinfo.value.status == "failed"
    server.single_request()
    assert client.requests_sent == 1


def test_retcode_zero_with_non_ok_status_is_failure(server_factory):
    server = server_factory(lambda record, count: (200, json_body({"status": "async", "retcode": 0})))
    client = make_client(server)
    with pytest.raises(OneBotAPIError):
        client.send_private_message("10001", "你好")
    server.single_request()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500, 502, 503])
def test_http_error_status_is_reported_once_and_not_retryable(status, server_factory):
    server = server_factory(
        lambda record, count: (status, json_body({"status": "failed", "retcode": -1, "message": "nope"}))
    )
    client = make_client(server)
    with pytest.raises(OneBotHTTPError) as excinfo:
        client.send_private_message("10001", "你好")
    assert excinfo.value.status == status
    # 服务端明确应答 → 不是网络层故障，lifecycle 不得重试。
    assert not isinstance(excinfo.value, (ConnectionError, TimeoutError))
    server.single_request()
    assert client.requests_sent == 1


def test_timeout_is_retryable_network_error(server_factory):
    def respond(record, count):
        time.sleep(1.0)
        return 200, json_body(PRIVATE_OK)

    server = server_factory(respond)
    client = make_client(server, timeout=0.25)
    with pytest.raises(OneBotTimeoutError) as excinfo:
        client.send_private_message("10001", "你好")
    assert isinstance(excinfo.value, TimeoutError)
    assert isinstance(excinfo.value, ConnectionError)
    server.single_request()
    assert client.requests_sent == 1


def test_unreachable_endpoint_is_retryable_transport_error():
    """无监听端口：Windows 回环上可能表现为超时或拒绝，两者都必须可重试。"""
    client = OneBotClient(f"http://127.0.0.1:{_closed_port()}", timeout=2.0)
    with pytest.raises(OneBotTransportError) as excinfo:
        client.send_private_message("10001", "你好")
    assert isinstance(excinfo.value, (ConnectionError, TimeoutError))
    assert client.requests_sent == 1


def test_connection_reset_is_retryable_transport_error(server_factory):
    def respond(record, count):
        raise DropConnection()

    server = server_factory(respond)
    client = make_client(server, timeout=2.0)
    with pytest.raises(OneBotTransportError) as excinfo:
        client.send_private_message("10001", "你好")
    assert isinstance(excinfo.value, ConnectionError)
    server.single_request()
    assert client.requests_sent == 1


@pytest.mark.parametrize(
    "body",
    [
        "not json at all",
        "",
        "<html>gateway</html>",
        json_body([1, 2, 3]),
        json_body("ok"),
        json_body({"status": "ok", "data": {}}),
        json_body({"status": 1, "retcode": 0}),
        json_body({"status": "ok", "retcode": "abc"}),
        json_body({"status": "ok", "retcode": 0, "data": []}),
    ],
)
def test_invalid_json_shapes_raise_protocol_error(body, server_factory):
    server = server_factory(lambda record, count: (200, body))
    client = make_client(server)
    with pytest.raises(OneBotProtocolError):
        client.send_private_message("10001", "你好")
    server.single_request()
    assert client.requests_sent == 1


def test_string_retcode_is_accepted(server_factory):
    server = server_factory(
        lambda record, count: (200, json_body({"status": "ok", "retcode": "0", "data": {"message_id": 7}}))
    )
    client = make_client(server)
    assert client.send_private_message("10001", "你好")["message_id"] == 7


# --------------------------------------------------------------------------
# access_token：只走 Authorization，且绝不出现在异常/返回值/repr/日志里
# --------------------------------------------------------------------------
def test_token_is_sent_only_in_authorization_header(server_factory):
    server = server_factory()
    client = make_client(server, access_token=TOKEN)
    client.send_private_message("10001", "你好")
    request = server.single_request()
    assert request["headers"]["authorization"] == f"Bearer {TOKEN}"
    # token 不进 URL、不进请求载荷（设备帧不会带上它）。
    assert TOKEN not in request["path"]
    assert TOKEN not in request["raw"]
    assert TOKEN not in json.dumps(request["headers"], ensure_ascii=False).replace(
        request["headers"]["authorization"], ""
    )


def test_token_never_appears_in_http_error_when_server_echoes_it(server_factory):
    def respond(record, count):
        # 恶意/愚蠢的服务端把 Authorization 原文回显进错误体。
        echoed = record["headers"].get("authorization", "")
        return 500, json_body(
            {"status": "failed", "retcode": -1, "message": f"bad header {echoed}", "data": {"echo": echoed}}
        )

    server = server_factory(respond)
    client = make_client(server, access_token=TOKEN)
    with pytest.raises(OneBotHTTPError) as excinfo:
        client.send_private_message("10001", "你好")
    message = str(excinfo.value)
    assert TOKEN not in message
    assert TOKEN_MASK in message
    assert TOKEN not in repr(excinfo.value)


def test_token_never_appears_in_api_error_when_server_echoes_it(server_factory):
    def respond(record, count):
        echoed = record["headers"].get("authorization", "")
        return 200, json_body({"status": "failed", "retcode": 1, "wording": f"denied for {echoed}"})

    server = server_factory(respond)
    client = make_client(server, access_token=TOKEN)
    with pytest.raises(OneBotAPIError) as excinfo:
        client.send_private_message("10001", "你好")
    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in excinfo.value.detail
    assert TOKEN_MASK in excinfo.value.detail


def test_token_never_appears_in_success_payload(server_factory):
    def respond(record, count):
        echoed = record["headers"].get("authorization", "")
        return 200, json_body(
            {"status": "ok", "retcode": 0, "data": {"message_id": 1, "token_echo": echoed}}
        )

    server = server_factory(respond)
    client = make_client(server, access_token=TOKEN)
    result = client.send_private_message("10001", "你好")
    assert TOKEN not in json.dumps(result, ensure_ascii=False)
    assert result["data"]["token_echo"] == f"Bearer {TOKEN_MASK}"


def test_token_never_appears_in_protocol_error_body(server_factory):
    def respond(record, count):
        echoed = record["headers"].get("authorization", "")
        return 200, f"<html>leaked {echoed}</html>"

    server = server_factory(respond)
    client = make_client(server, access_token=TOKEN)
    with pytest.raises(OneBotProtocolError) as excinfo:
        client.send_private_message("10001", "你好")
    assert TOKEN not in str(excinfo.value)
    assert TOKEN_MASK in str(excinfo.value)


def test_token_never_appears_in_repr_or_logs(server_factory, caplog):
    server = server_factory()
    client = make_client(server, access_token=TOKEN)
    with caplog.at_level(logging.DEBUG):
        client.send_private_message("10001", "你好")
    assert TOKEN not in repr(client)
    assert "configured" in repr(client)
    assert TOKEN not in caplog.text


def test_outbound_content_containing_token_is_refused_without_http(server_factory):
    server = server_factory()
    client = make_client(server, access_token=TOKEN)
    with pytest.raises(OneBotClientError) as excinfo:
        client.send_private_message("10001", f"我的 token 是 {TOKEN}")
    assert TOKEN not in str(excinfo.value)
    assert server.requests == []
    assert client.requests_sent == 0


def test_token_must_not_be_embedded_in_base_url():
    with pytest.raises(ValueError) as excinfo:
        OneBotClient(f"http://127.0.0.1:3000/?access_token={TOKEN}")
    assert TOKEN not in str(excinfo.value)


def test_empty_token_is_rejected():
    with pytest.raises(ValueError):
        OneBotClient("http://127.0.0.1:3000", access_token="   ")


# --------------------------------------------------------------------------
# 其它输入校验
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "base_url",
    ["", "   ", "ftp://127.0.0.1:3000", "127.0.0.1:3000", "http://user:pw@127.0.0.1:3000", "http://x/#frag"],
)
def test_invalid_base_url_rejected(base_url):
    with pytest.raises(ValueError):
        OneBotClient(base_url)


@pytest.mark.parametrize("timeout", [0, -1, "5", None, True])
def test_invalid_timeout_rejected(timeout):
    with pytest.raises(ValueError):
        OneBotClient("http://127.0.0.1:3000", timeout=timeout)


def test_absolute_endpoint_is_rejected(server_factory):
    server = server_factory()
    client = make_client(server)
    with pytest.raises(ValueError):
        client.call_api("http://evil.example/send_private_msg", {"user_id": 1})
    assert server.requests == []


# --------------------------------------------------------------------------
# 与 onebot_lifecycle 的配合：重试归它，且复用同一幂等键
# --------------------------------------------------------------------------
def test_lifecycle_retry_reuses_same_idempotency_key(server_factory):
    seen_keys: list[str | None] = []

    def respond(record, count):
        body = json.loads(record["raw"])
        seen_keys.append(body.get("idempotency_key"))
        if count == 1:
            time.sleep(0.8)  # 第一次让客户端超时（client timeout=0.2）
        return 200, json_body({"status": "ok", "retcode": 0, "data": {"message_id": 9001}})

    server = server_factory(respond)
    client = make_client(server, timeout=0.2, access_token=TOKEN)
    sender = OneBotSender(client.send_private_message)
    lifecycle = OneBotLifecycle(TaskStore(), "device-1", sender)

    result = lifecycle.dispatch(OutboundAction(idempotency_key="msg-1", user_id="10001", text="在的呀"))

    assert result["status"] == "sent"
    assert result["attempts"] == 2
    assert seen_keys == ["msg-1", "msg-1"]  # 两次重试复用同一幂等键
    assert len(server.requests) == 2
    assert client.requests_sent == 2  # 每次调用只发一次，没有额外自发重试
    for request in server.requests:
        assert request["headers"]["authorization"] == f"Bearer {TOKEN}"


def test_lifecycle_does_not_retry_api_errors(server_factory):
    server = server_factory(
        lambda record, count: (200, json_body({"status": "failed", "retcode": 100, "message": "bad"}))
    )
    client = make_client(server)
    lifecycle = OneBotLifecycle(TaskStore(), "device-1", OneBotSender(client.send_private_message))
    result = lifecycle.dispatch(OutboundAction(idempotency_key="msg-2", user_id="10001", text="你好"))
    assert result["status"] == "failed"
    assert result["attempts"] == 1
    server.single_request()


def test_lifecycle_does_not_retry_http_errors(server_factory):
    server = server_factory(lambda record, count: (502, json_body({"status": "failed", "retcode": -1})))
    client = make_client(server)
    lifecycle = OneBotLifecycle(TaskStore(), "device-1", OneBotSender(client.send_private_message))
    result = lifecycle.dispatch(OutboundAction(idempotency_key="msg-3", user_id="10001", text="你好"))
    assert result["status"] == "failed"
    assert result["attempts"] == 1
    server.single_request()


def test_lifecycle_rejects_unknown_segments_without_http(server_factory):
    server = server_factory()
    client = make_client(server)
    lifecycle = OneBotLifecycle(TaskStore(), "device-1", OneBotSender(client.send_private_message))
    result = lifecycle.dispatch(
        OutboundAction(
            idempotency_key="msg-4",
            user_id="10001",
            text="",
            media=[{"type": "json", "data": {"data": '{"app":"com.tencent.xxx"}'}}],
        )
    )
    assert result["status"] == "rejected"
    assert server.requests == []


def test_lifecycle_recovers_after_network_failure(server_factory):
    state = {"offline": True}

    def respond(record, count):
        if state["offline"]:
            time.sleep(0.5)  # 客户端 timeout=0.15 → 网络层失败
        return 200, json_body({"status": "ok", "retcode": 0, "data": {"message_id": 11}})

    server = server_factory(respond)
    client = make_client(server, timeout=0.15)
    lifecycle = OneBotLifecycle(TaskStore(), "device-1", OneBotSender(client.send_private_message))
    action = OutboundAction(idempotency_key="msg-5", user_id="10001", text="重登后补发")
    failed = lifecycle.dispatch(action)
    assert failed["status"] == "failed"
    assert failed["attempts"] == 3  # 网络层失败用满 3 次，且不重新生成内容
    assert lifecycle.pending_keys() == ["msg-5"]
    keys_before = [item.get("idempotency_key") for item in server.payloads()]
    assert keys_before == ["msg-5", "msg-5", "msg-5"]

    state["offline"] = False  # 重登成功
    recovered = lifecycle.recover_after_relogin([action])
    assert recovered["recovered"] == ["msg-5"]
    assert recovered["already_sent"] == []
    assert len(server.requests) == 4  # 3 次网络层失败 + 1 次补发
    assert json.loads(server.requests[-1]["raw"])["idempotency_key"] == "msg-5"


# --------------------------------------------------------------------------
# 依赖与边界
# --------------------------------------------------------------------------
def test_module_uses_only_standard_library():
    source = Path(onebot_client_module.__file__).read_text(encoding="utf-8")
    for banned in ("httpx", "aiocqhttp", "aiohttp", "websockets"):
        assert banned not in source
    assert "import requests" not in source
    assert "requests." not in source
    assert "import urllib.request" in source
    assert "import urllib.error" in source


def test_client_never_touches_qq_protocol_surface(server_factory):
    """只做 HTTP API 调用方：不存在登录/拉协议相关的入口。"""
    names = {name for name in dir(OneBotClient) if not name.startswith("_")}
    for forbidden in ("login", "connect_ws", "reverse_ws", "start", "run"):
        assert forbidden not in names


def test_endpoint_constants_match_onebot_v11():
    assert SEND_PRIVATE_ENDPOINT == "/send_private_msg"
    assert SEND_GROUP_ENDPOINT == "/send_group_msg"