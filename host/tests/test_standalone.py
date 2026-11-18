"""独立宿主的固定测试。

这些测试回答的是「**能不能真的跑起来**」，而不只是「函数对不对」：

1. 装配层：`StandaloneHost` 必须真的把设备服务端、模型网关、核心插件装到一起，
   而且设备服务端**真的能监听**（不是 mock 的 is_running）。
2. 事件层：OneBot 原始字典 → `IncomingMessage` 的转换，含各种脏数据。
3. 回复层：结果 → QQ 文本的固定映射，**绝不外泄内部 reason / payload**。

## 为什么这些必须测

本机装不了 AstrBot 的那些环境里，`astrbot_star` 的真实绑定层只能跳过（见
`test_astrbot_star.py`）。如果没有独立宿主这一层，「主线链路能不能跑」就完全没有
可验证的证据。所以这里的装配测试是**唯一的端到端证据**，不能用 mock 糊过去。
"""

from __future__ import annotations

import os

import pytest

from astrbot_whale_device.host_config import ConfigError, HostConfig
from astrbot_whale_device.standalone import (
    StandaloneHost,
    incoming_from_onebot_event,
    poll_once,
)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> HostConfig:
    raw = {
        "owner_user_id": "10001",
        "device_id": "device-b-01",
        "allowed_packages": ["com.android.settings"],
        "blocked_packages": [],
        "signing_secret_env": "WHALE_SIGNING_SECRET",
        "model": {
            "provider_id": "openai",
            "model_id": "gpt-4o-mini",
            "base_url": "https://api.example.com",
        },
        "device_server": {"host": "127.0.0.1", "port": 0},
    }
    raw.update(overrides)
    return HostConfig.from_mapping(raw)


@pytest.fixture()
def signing_secret_env(monkeypatch: pytest.MonkeyPatch) -> str:
    """签名密钥缺失时必须启动失败，所以每个装配测试都先备好它。"""
    name = "WHALE_SIGNING_SECRET"
    monkeypatch.setenv(name, "assembly-test-secret")
    return name


# ---------------------------------------------------------------------------
# 装配：真的启动设备服务端
# ---------------------------------------------------------------------------

def test_host_assembles_real_components(signing_secret_env: str) -> None:
    """四个零件都必须是**真实实现**，不是 None、不是 mock。"""
    from astrbot_whale_device.astrbot_adapter import ModelGateway
    from astrbot_whale_device.device_server import WhaleDeviceServer, WebSocketDeviceTransport
    from astrbot_whale_device.plugin import WhaleDevicePlugin

    host = StandaloneHost(_cfg())
    try:
        assert isinstance(host.device_server, WhaleDeviceServer)
        assert isinstance(host.transport, WebSocketDeviceTransport)
        assert isinstance(host.gateway, ModelGateway)
        assert isinstance(host.plugin, WhaleDevicePlugin)
        # 未配置 onebot 时发送端可以为 None（不强制要求）。
        assert host.onebot_client is None
    finally:
        host.stop()


def test_device_server_really_listens(signing_secret_env: str) -> None:
    """`start()` 必须真的绑定端口；`url` 必须带实际端口号。

    这是整条链路的入口证据：手机要连的就是这个地址。
    """
    host = StandaloneHost(_cfg())
    try:
        port = host.start()
        assert port > 0, "port=0 时系统应分配一个真实端口"
        assert host.device_server.is_running is True
        assert host.device_url.startswith("ws://127.0.0.1:")
        assert str(port) in host.device_url
    finally:
        host.stop()
        assert host.device_server.is_running is False


def test_host_stops_cleanly(signing_secret_env: str) -> None:
    """`stop()` 之后不能再处于 running 状态（否则端口泄漏）。"""
    host = StandaloneHost(_cfg())
    host.start()
    host.stop()
    assert host.device_server.is_running is False


def test_missing_signing_secret_fails_assembly(monkeypatch: pytest.MonkeyPatch) -> None:
    """签名密钥缺失必须**启动失败**，不能静默用空密钥（那会让签名变装饰品）。"""
    monkeypatch.delenv("WHALE_SIGNING_SECRET", raising=False)
    with pytest.raises(ConfigError):
        StandaloneHost(_cfg())


def test_overlapping_packages_rejected_at_config_level() -> None:
    with pytest.raises(ConfigError):
        _cfg(allowed_packages=["a.b.c"], blocked_packages=["a.b.c"])


def test_port_zero_is_accepted_but_out_of_range_rejected() -> None:
    """`port=0` 是合法的（系统分配）；但越界端口必须拒绝。"""
    assert _cfg(device_server={"host": "127.0.0.1", "port": 0}).device_server.port == 0
    with pytest.raises(ConfigError):
        _cfg(device_server={"host": "127.0.0.1", "port": 70000})


# ---------------------------------------------------------------------------
# 事件转换
# ---------------------------------------------------------------------------

def test_private_text_message_converts() -> None:
    message = incoming_from_onebot_event(
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": 10001,
            "message_id": 1,
            "message": "打开设置",
            "sender": {"nickname": "主人"},
        },
        self_id="99999",
    )
    assert message is not None
    assert message.user_id == "10001"
    assert message.text == "打开设置"
    assert message.message_type.value == "private"
    assert message.group_id is None


def test_group_segment_array_converts() -> None:
    message = incoming_from_onebot_event(
        {
            "post_type": "message",
            "message_type": "group",
            "user_id": 10001,
            "group_id": 90001,
            "message_id": 2,
            "message": [{"type": "text", "data": {"text": "@bot 打开应用"}}],
        },
    )
    assert message is not None
    assert message.message_type.value == "group"
    assert message.group_id == "90001"
    assert message.text == "@bot 打开应用"


def test_media_segment_becomes_placeholder_not_url() -> None:
    """媒体段用占位符，**不把 URL 当正文**（M03 同一原则）。"""
    message = incoming_from_onebot_event(
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": 10001,
            "message_id": 3,
            "message": [{"type": "image", "data": {"file": "https://evil.example/x.png"}}],
        },
    )
    assert message is not None
    assert "https://" not in message.text
    assert "[image]" in message.text


def test_non_message_event_is_skipped() -> None:
    assert incoming_from_onebot_event({"post_type": "notice", "user_id": 10001}) is None


def test_missing_user_id_is_skipped_even_with_nickname() -> None:
    """昵称绝不能顶替 user_id（M04 硬要求）。"""
    event = {
        "post_type": "message",
        "message_type": "private",
        "message_id": 4,
        "message": "hi",
        "sender": {"nickname": "10001"},
    }
    assert incoming_from_onebot_event(event) is None


def test_unknown_segment_type_is_skipped() -> None:
    """未知段不猜，整条消息判定为无法处理。"""
    event = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 10001,
        "message_id": 5,
        "message": [{"type": "at", "data": {"qq": "99999"}}],
    }
    assert incoming_from_onebot_event(event) is None


def test_missing_message_id_falls_back_to_stable_value() -> None:
    """缺 message_id 时用稳定回退值，**绝不返回空串**（空串会让幂等互相覆盖）。"""
    message = incoming_from_onebot_event(
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": 10001,
            "message": "hi",
            "time": 123456,
        },
    )
    assert message is not None
    assert message.message_id
    assert message.message_id != ""


# ---------------------------------------------------------------------------
# 回复映射
# ---------------------------------------------------------------------------

def test_reply_mapping_never_leaks_internal_fields() -> None:
    host_reply = StandaloneHost.reply_text  # 未绑定方法：只验证纯映射
    assert "secret" not in host_reply(None, {"kind": "failed", "reason": "secret-internal"})  # type: ignore[arg-type]
    assert host_reply(None, {"kind": "silent"}) == ""  # type: ignore[arg-type]


def test_device_task_reply_hides_payload() -> None:
    host_reply = StandaloneHost.reply_text
    text = host_reply(
        None,  # type: ignore[arg-type]
        {
            "kind": "device_task",
            "plan": {"commands": [{"command": "device.app.launch", "payload": {"package_name": "com.bank.app"}}]},
        },
    )
    assert "com.bank.app" not in text
    assert "device.app.launch" not in text


# ---------------------------------------------------------------------------
# 轮询：不因脏数据崩掉
# ---------------------------------------------------------------------------

class _FakeClient:
    """最小假的 OneBot 客户端：让 poll_once 能跑，且能注入脏数据。"""

    def __init__(self, events: list[object]) -> None:
        self._events = events
        self.calls: list[str] = []

    def call_api(self, endpoint: str, payload: dict) -> dict:
        # 端点按 `OneBotClient` 的契约必须以 `/` 开头（标准 OneBot API 路径规则）。
        self.calls.append(endpoint)
        assert endpoint == "/get_latest_events"
        return {"status": "ok", "retcode": 0, "data": {"events": self._events}}


def test_poll_skips_dirty_events_without_crashing(signing_secret_env: str) -> None:
    host = StandaloneHost(_cfg())
    try:
        client = _FakeClient(
            [
                {"post_type": "notice"},
                {"post_type": "message", "message_type": "private", "message": "x"},
                "not-a-dict",
                {
                    "post_type": "message",
                    "message_type": "private",
                    "user_id": 20002,
                    "message_id": 10,
                    "message": "hello",
                },
            ],
        )
        cursor, count = poll_once(client=client, host=host, last_seen=None)  # type: ignore[arg-type]
        assert count == 1, "只有一条合法消息应被处理"
        assert cursor == "10"
    finally:
        host.stop()


def test_poll_returns_zero_when_events_missing(signing_secret_env: str) -> None:
    host = StandaloneHost(_cfg())
    try:
        client = _FakeClient([])
        cursor, count = poll_once(client=client, host=host, last_seen="abc")  # type: ignore[arg-type]
        assert count == 0
        assert cursor == "abc", "空轮询不得清空游标"
    finally:
        host.stop()