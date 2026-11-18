"""AstrBot 插件入口的固定测试。

分两层：

1. **纯函数层（无 AstrBot 也能跑）**：`to_incoming_message` 与 `reply_text_for`。
2. **真实绑定层（有 AstrBot 才跑）**：`build_main_class()` 产出的类必须真的
   继承 `astrbot.api.star.Star`，且被 AstrBot 认成插件类。

第二层用 `pytest.mark.skipif` 守卫：在本机 `E:\\astr\\venv`（Python 3.12，
AstrBot 4.29.4）里是**真跑**；在没装 AstrBot 的环境里跳过而不是失败。

为什么这层重要：AstrBot 只认「继承 Star 的类 + module-level filter 装饰器」，
写错继承就完全不加载，而纯函数测试发现不了。
"""

from __future__ import annotations

import pytest

from astrbot_whale_device.astrbot_star import (
    astrbot_available,
    reply_text_for,
    to_incoming_message,
)
from astrbot_whale_device.models import MessageType


# ---------------------------------------------------------------------------
# 第一层：事件转换（无 AstrBot 依赖）
# ---------------------------------------------------------------------------


def test_private_message_is_converted_without_group() -> None:
    message = to_incoming_message(
        sender_id="10001",
        text="open settings",
        message_id="m-1",
        group_id=None,
        self_id="99999",
    )
    assert message.user_id == "10001"
    assert message.message_id == "m-1"
    assert message.text == "open settings"
    assert message.message_type is MessageType.PRIVATE
    assert message.group_id is None
    assert message.channel == "astrbot"


def test_group_message_keeps_group_id() -> None:
    message = to_incoming_message(
        sender_id="10001",
        text="@bot launch app",
        message_id="m-2",
        group_id="90001",
        self_id="99999",
    )
    assert message.message_type is MessageType.GROUP
    assert message.group_id == "90001"


def test_empty_group_id_string_is_treated_as_private() -> None:
    """空串不是群：AstrBot 某些平台给 `""`，不能当群聊处理。"""
    message = to_incoming_message(
        sender_id="10001",
        text="hi",
        message_id="m-3",
        group_id="",
        self_id="99999",
    )
    assert message.message_type is MessageType.PRIVATE
    assert message.group_id is None


def test_self_message_is_marked_as_bot() -> None:
    message = to_incoming_message(
        sender_id="99999",
        text="self message",
        message_id="m-4",
        group_id=None,
        self_id="99999",
    )
    assert message.is_bot is True


def test_sender_name_never_affects_identity() -> None:
    """M04：昵称不参与身份判定，只作为展示字段带入。"""
    message = to_incoming_message(
        sender_id="20002",
        text="i am the owner",
        message_id="m-5",
        group_id=None,
        self_id="99999",
        sender_name="owner",
    )
    assert message.user_id == "20002"
    assert message.sender_name == "owner"


def test_sender_id_is_coerced_to_string() -> None:
    """AstrBot 可能给 int；核心包 `user_id` 必须是字符串（精确比对）。"""
    message = to_incoming_message(
        sender_id=10001,  # type: ignore[arg-type]
        text="hi",
        message_id="m-6",
        group_id=None,
        self_id="99999",
    )
    assert message.user_id == "10001"


# ---------------------------------------------------------------------------
# 第二层：回复映射（无 AstrBot 依赖）
# ---------------------------------------------------------------------------


def test_reply_kind_returns_model_text() -> None:
    assert reply_text_for({"kind": "reply", "text": "ok master"}) == "ok master"


def test_silent_kind_returns_empty_and_sends_nothing() -> None:
    """SILENT 是正常结果：不产生空消息（M03 product doc）。"""
    assert reply_text_for({"kind": "silent"}) == ""


def test_device_task_kind_returns_fixed_summary_without_payload() -> None:
    """设备任务只回固定摘要，不回显 payload 细节。"""
    result = {
        "kind": "device_task",
        "plan": {
            "commands": [
                {"command": "device.app.launch", "payload": {"package_name": "com.secret.app"}},
            ],
        },
    }
    text = reply_text_for(result)
    assert "1" in text
    assert "com.secret.app" not in text
    assert "device.app.launch" not in text


def test_device_task_with_empty_plan_counts_zero() -> None:
    assert "0" in reply_text_for({"kind": "device_task"})


def test_failed_kind_never_leaks_reason() -> None:
    """失败只回固定文案：`reason` 是内部诊断信息，绝不外泄。"""
    text = reply_text_for({"kind": "failed", "reason": "allowlist_outside_denied: com.bank.app"})
    assert "allowlist" not in text
    assert "com.bank.app" not in text
    assert text


def test_unknown_kind_returns_empty() -> None:
    assert reply_text_for({"kind": "unexpected"}) == ""
    assert reply_text_for({}) == ""


# ---------------------------------------------------------------------------
# 第三层：真实 AstrBot 绑定（无 AstrBot 时跳过）
# ---------------------------------------------------------------------------

requires_astrbot = pytest.mark.skipif(
    not astrbot_available(),
    reason="AstrBot not installed; real binding runs in E:\\astr\\venv (Python 3.12)",
)


@requires_astrbot
def test_main_class_really_inherits_astrbot_star() -> None:
    """AstrBot 只认继承 `star.Star` 的类；工厂产出物必须是真子类。"""
    import astrbot.api.star as star

    from astrbot_whale_device.astrbot_star import build_main_class

    main_cls = build_main_class()
    assert main_cls.__name__ == "Main"
    assert issubclass(main_cls, star.Star)


@requires_astrbot
def test_main_class_has_both_private_and_group_entry_points() -> None:
    """私聊与群聊两个入口都必须存在，不能被漏掉。"""
    from astrbot_whale_device.astrbot_star import build_main_class

    main_cls = build_main_class()
    assert hasattr(main_cls, "on_private_message")
    assert hasattr(main_cls, "on_group_message")
    assert hasattr(main_cls, "on_loaded")


@requires_astrbot
def test_main_class_registers_event_handlers_with_astrbot() -> None:
    """装饰器路径写错时，AstrBot handler 注册表里就看不到我们的处理器。"""
    from astrbot_whale_device.astrbot_star import build_main_class

    build_main_class()

    try:
        from astrbot.core.star.register.star_handler import star_handlers_registry
    except Exception:  # pragma: no cover - 版本差异时的保守跳过
        pytest.skip("AstrBot handler registry path differs in this version")

    names = " ".join(str(getattr(h, "handler_full_name", "")) for h in star_handlers_registry)
    assert "on_private_message" in names
    assert "on_group_message" in names