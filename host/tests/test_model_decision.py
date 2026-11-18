"""M02 决策解析的拒绝矩阵与请求构造边界。

每条用例都对应 M02 开发文档「决策解析」/「测试」小节的一条硬性要求。
"""

import json

import pytest

from astrbot_whale_device.models import (
    FIXED_CONFIG_KEYS,
    ConfigError,
    DecisionMode,
    IncomingMessage,
    MessageType,
    ModelConfig,
    Principal,
)
from astrbot_whale_device.persona import PersonaError, PersonaProfile
from astrbot_whale_device.router import DecisionError, MessageRouter, parse_model_decision


def private(user_id: str = "A", text: str = "hello") -> IncomingMessage:
    return IncomingMessage("m1", user_id, text, MessageType.PRIVATE)


def reply(mode: str = "REPLY", **extra) -> str:
    return json.dumps({"mode": mode, **extra}, ensure_ascii=False)


def launch_plan(*commands) -> str:
    return json.dumps({"mode": "DEVICE_TASK", "device_plan": {"commands": list(commands)}}, ensure_ascii=False)


GOOD_COMMAND = {"command": "device.device_status", "payload": {}}


# ---------------------------------------------------------------------------
# 接受面：三种 mode 各一条正向路径。
# ---------------------------------------------------------------------------


def test_parse_accepts_three_modes():
    assert parse_model_decision(reply("REPLY", text="你好")).mode is DecisionMode.REPLY
    assert parse_model_decision(reply("SILENT")).mode is DecisionMode.SILENT
    decision = parse_model_decision(launch_plan(GOOD_COMMAND))
    assert decision.mode is DecisionMode.DEVICE_TASK
    assert decision.device_plan.commands == [GOOD_COMMAND]


def test_parse_accepts_declared_media_segments():
    decision = parse_model_decision(
        reply("REPLY", text="看图", media=[{"type": "image", "file": "a.png"}, {"type": "face", "id": "1"}])
    )
    assert decision.media == [{"type": "image", "file": "a.png"}, {"type": "face", "id": "1"}]


# ---------------------------------------------------------------------------
# 拒绝矩阵 1：不是合法 JSON object。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        'do(action="Tap", node=1)',          # 自然语言动作
        "```json\n" + reply("REPLY") + "\n```",  # Markdown 代码块
        reply("REPLY") + " 好的",            # 尾随自然语言
        '{"mode":"REPLY",}',                 # 尾随逗号
        '[{"mode":"REPLY"}]',                # 顶层数组
        '"REPLY"',                           # 顶层字符串
        "",
        "null",
    ],
)
def test_parse_rejects_non_json_object(raw):
    with pytest.raises(DecisionError):
        parse_model_decision(raw)


def test_parse_rejects_non_string_input():
    with pytest.raises(DecisionError):
        parse_model_decision(None)  # type: ignore[arg-type]
    with pytest.raises(DecisionError):
        parse_model_decision(b'{"mode":"REPLY"}')  # type: ignore[arg-type]


def test_parse_rejects_markdown_and_bare_coordinates():
    for raw in ("我把坐标 (120, 540) 点一下", "tap(x=120,y=540)", "<action>tap</action>"):
        with pytest.raises(DecisionError):
            parse_model_decision(raw)


# ---------------------------------------------------------------------------
# 拒绝矩阵 2：mode 不合法 / 顶层字段未知。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["TAP", "reply", "DEVICE", "", "REPLY ", "TOOL_CALL", None, 1, ["REPLY"]])
def test_parse_rejects_invalid_mode(mode):
    with pytest.raises(DecisionError):
        parse_model_decision(json.dumps({"mode": mode}))


def test_parse_rejects_missing_mode():
    with pytest.raises(DecisionError):
        parse_model_decision('{"text":"hi"}')


@pytest.mark.parametrize(
    "extra",
    [
        {"tool": "device.ui.tap"},        # 未声明工具
        {"action": "tap"},                # 自然语言动作字段
        {"coordinates": [120, 540]},      # 裸坐标
        {"package_name": "com.example"},  # 从消息正文猜字段
        {"reasoning": "..."},
    ],
)
def test_parse_rejects_unknown_top_level_field(extra):
    with pytest.raises(DecisionError):
        parse_model_decision(json.dumps({"mode": "REPLY", **extra}))


# ---------------------------------------------------------------------------
# 拒绝矩阵 3：REPLY/SILENT 不得携带 device_plan。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["REPLY", "SILENT"])
def test_non_task_mode_cannot_carry_device_plan(mode):
    with pytest.raises(DecisionError):
        parse_model_decision(json.dumps({"mode": mode, "device_plan": {"commands": [GOOD_COMMAND]}}))


@pytest.mark.parametrize("mode", ["REPLY", "SILENT"])
def test_non_task_mode_may_carry_explicit_null_plan(mode):
    assert parse_model_decision(json.dumps({"mode": mode, "device_plan": None})).device_plan is None


@pytest.mark.parametrize("mode", ["REPLY", "SILENT"])
@pytest.mark.parametrize("empty", [{"commands": []}, {}, [], "", 0])
def test_non_task_mode_rejects_any_plan_presence(mode, empty):
    with pytest.raises(DecisionError):
        parse_model_decision(json.dumps({"mode": mode, "device_plan": empty}))


# ---------------------------------------------------------------------------
# 拒绝矩阵 4：DEVICE_TASK 的 commands 必须是 1..64 个合法命令。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "plan",
    [
        None,
        "commands",
        {},
        {"commands": []},
        {"commands": {}},
        {"commands": "device.device_status"},
        {"commands": ["device.device_status"]},
        {"commands": [GOOD_COMMAND] * 65},
        {"commands": [GOOD_COMMAND], "extra": 1},
    ],
)
def test_device_task_rejects_bad_plan_shape(plan):
    with pytest.raises(DecisionError):
        parse_model_decision(json.dumps({"mode": "DEVICE_TASK", "device_plan": plan}))


def test_device_task_accepts_exactly_64_commands():
    decision = parse_model_decision(launch_plan(*([GOOD_COMMAND] * 64)))
    assert len(decision.device_plan.commands) == 64


def test_device_task_rejects_65_commands():
    with pytest.raises(DecisionError):
        parse_model_decision(launch_plan(*([GOOD_COMMAND] * 65)))


@pytest.mark.parametrize(
    "command",
    [
        {"payload": {}},
        {"command": "device.device_status"},
        {"command": None, "payload": {}},
        {"command": "device.device_status", "payload": []},
        {"COMMAND": "device.device_status", "payload": {}},
        {"command": "device.device_status", "payload": {}, "note": "ok"},
        "device.device_status",
        ["device.device_status", {}],
    ],
)
def test_device_task_rejects_bad_command_shape(command):
    with pytest.raises(DecisionError):
        parse_model_decision(launch_plan(command))


# ---------------------------------------------------------------------------
# 拒绝矩阵 5：命令必须走 validate_device_plan —— 白名单、形状、范围、枚举。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        {"command": "device.shell", "payload": {"cmd": "id"}},              # 禁止命令
        {"command": "device.adb", "payload": {}},                          # 禁止命令
        {"command": "device.eval", "payload": {"code": "1+1"}},            # 禁止命令
        {"command": "device.app.launch", "payload": {}},                   # 缺必填字段
        {"command": "device.app.launch", "payload": {"package_name": "ab"}},           # 太短
        {"command": "device.app.launch", "payload": {"package_name": "x" * 300}},      # 太长
        {"command": "device.ui.tap", "payload": {"snapshot_id": "s"}},                  # ui_target 缺字段
        {"command": "device.ui.scroll", "payload": {
            "snapshot_id": "s", "target_id": "t", "selector_hash": "h",
            "expected_package": "p", "direction": "diagonal"}},                          # 枚举外
        {"command": "device.ui.scroll", "payload": {
            "snapshot_id": "s", "target_id": "t", "selector_hash": "h",
            "expected_package": "p", "direction": "up", "amount": 0}},                    # 范围外
        {"command": "device.ui.long_press", "payload": {
            "snapshot_id": "s", "target_id": "t", "selector_hash": "h",
            "expected_package": "p", "duration_ms": True}},                                # bool 冒充 int
        {"command": "device.media.pick", "payload": {"media_type": "image", "max_items": 21}},
        {"command": "device.camera.capture", "payload": {"lens": "tele"}},
        {"command": "device.sms.send", "payload": {"number": "1", "text": "hi"}},
        {"command": "device.sms.send", "payload": {"number": "10086", "text": ""}},
        {"command": "device.ui.observe", "payload": {"mode": "raw"}},
        {"command": "device.app.list", "payload": {"extra": 1}},                          # 必须空 payload
    ],
)
def test_device_task_rejects_invalid_command_payload(command):
    with pytest.raises(DecisionError):
        parse_model_decision(launch_plan(command))


def test_parse_rejects_launch_outside_allowlist_only_via_router():
    """解析层只看结构；包名白名单属于 PackagePolicy，必须在路由层拦。"""
    raw = launch_plan({"command": "device.app.launch", "payload": {"package_name": "com.example.wallet"}})
    decision = parse_model_decision(raw)
    assert decision.mode is DecisionMode.DEVICE_TASK
    routed = MessageRouter("A", lambda *_: raw, package_policy=None).route(private("A"))
    assert routed["kind"] == "failed"


# ---------------------------------------------------------------------------
# 拒绝矩阵 6：text / media 越界。
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [123, ["hi"], {"a": 1}, None])
def test_parse_rejects_non_string_text(text):
    with pytest.raises(DecisionError):
        parse_model_decision(json.dumps({"mode": "REPLY", "text": text}))


def test_parse_rejects_overlong_text():
    with pytest.raises(DecisionError):
        parse_model_decision(reply("REPLY", text="x" * 20_001))


@pytest.mark.parametrize(
    "media",
    [
        "a.png",
        {"type": "image"},
        [{"type": "unknown", "file": "a"}],
        [{"type": "image"}],
        [{"type": "image", "file": "a.png", "extra": 1}],
        [{"type": "image", "file": ""}],
        [{"file": "a.png"}],
        ["a.png"],
        [{"type": "image", "file": "a.png"}] * 9,
    ],
)
def test_parse_rejects_invalid_media(media):
    with pytest.raises(DecisionError):
        parse_model_decision(json.dumps({"mode": "REPLY", "media": media}))


# ---------------------------------------------------------------------------
# 拒绝矩阵 7：不做正则修复、不猜字段、不二次 repair。
# ---------------------------------------------------------------------------


def test_parse_never_repairs_truncated_json():
    raw = reply("REPLY", text="你好")
    with pytest.raises(DecisionError):
        parse_model_decision(raw[:-1])


def test_parse_never_accepts_wrapped_payload():
    for raw in (
        json.dumps({"decision": json.loads(reply("REPLY"))}),
        json.dumps({"result": {"mode": "REPLY"}}),
        json.dumps({"data": json.loads(reply("REPLY"))}),
    ):
        with pytest.raises(DecisionError):
            parse_model_decision(raw)


def test_parse_rejects_duplicate_mode_keys():
    with pytest.raises(DecisionError):
        parse_model_decision('{"mode":"REPLY","mode":"DEVICE_TASK"}')


# ---------------------------------------------------------------------------
# 配置面：九个固定键、禁止密钥、URL 只来自配置。
# ---------------------------------------------------------------------------


def test_fixed_config_keys_are_exactly_nine():
    assert FIXED_CONFIG_KEYS == (
        "provider_id",
        "base_url",
        "model_id",
        "fallback_model_id",
        "persona_id",
        "language",
        "max_context_tokens",
        "tts_provider_id",
        "allow_proactive_greeting",
    )


def test_model_config_binds_all_fixed_keys():
    config = ModelConfig.from_mapping({
        "provider_id": "openai-compat",
        "base_url": "https://gateway.example.com/v1",
        "model_id": "gpt-4o-mini",
        "fallback_model_id": "gpt-4o",
        "persona_id": "whale",
        "language": "zh-CN",
        "max_context_tokens": 16_384,
        "tts_provider_id": "tts-a",
        "allow_proactive_greeting": True,
    })
    assert set(config.as_dict()) == set(FIXED_CONFIG_KEYS)
    assert config.base_url == "https://gateway.example.com/v1"
    assert config.allow_proactive_greeting is True


@pytest.mark.parametrize(
    "key",
    ["api_key", "apiKey", "secret", "signing_secret", "token", "access_token", "password", "authorization"],
)
def test_model_config_rejects_secret_keys(key):
    with pytest.raises(ConfigError):
        ModelConfig.from_mapping({"provider_id": "p", "model_id": "m", key: "sk-xxx"})


def test_model_config_rejects_unknown_key():
    with pytest.raises(ConfigError):
        ModelConfig.from_mapping({"provider_id": "p", "model_id": "m", "proxy": "http://evil"})


@pytest.mark.parametrize(
    "url",
    [
        "http://gateway.example.com/v1",                 # 非 https
        "https://user:pass@gateway.example.com/v1",      # 内嵌凭据
        "https://gateway.example.com/v1?api_key=abc",    # 密钥进 query
        "https:///v1",                                    # 无 host
        "https://gateway.example.com/v1#frag",
        "ftp://gateway.example.com/v1",
    ],
)
def test_model_config_rejects_unsafe_base_url(url):
    with pytest.raises(ConfigError):
        ModelConfig(provider_id="p", model_id="m", base_url=url)


def test_model_config_loggable_omits_base_url():
    config = ModelConfig(provider_id="p", model_id="m", base_url="https://gateway.example.com/v1")
    assert "base_url" not in config.loggable()
    assert "gateway.example.com" not in json.dumps(config.loggable())


def test_model_config_deadline_is_host_clamped():
    config = ModelConfig(provider_id="p", model_id="m")
    assert config.device_deadline_ms() == 30_000
    assert config.device_deadline_ms(999_999) == 30_000
    assert config.device_deadline_ms(1) == 100
    assert config.device_deadline_ms(True) == 30_000
    assert config.device_deadline_ms(-5) == 100


@pytest.mark.parametrize(
    "override",
    [
        {"provider_id": "bad id"},
        {"model_id": ""},
        {"max_context_tokens": "big"},
        {"max_context_tokens": 1},
        {"allow_proactive_greeting": "yes"},
        {"language": "chinese"},
    ],
)
def test_model_config_validates_field_types(override):
    base = {"provider_id": "p", "model_id": "m"}
    with pytest.raises(ConfigError):
        ModelConfig.from_mapping({**base, **override})


# ---------------------------------------------------------------------------
# 人设边界：只改语言/语气/长度，不能改身份、白名单、安全与工具目录。
# ---------------------------------------------------------------------------


def test_persona_allows_only_style_keys():
    persona = PersonaProfile.from_mapping({"persona_id": "whale", "tone": "playful", "reply_length": "short"})
    assert persona.tone == "playful"
    block = "\n".join(persona.style_block())
    assert "权限约束" in block


@pytest.mark.parametrize(
    "forbidden",
    [
        "owner_user_id",
        "allowed_packages",
        "blocked_packages",
        "tools",
        "tool_catalog",
        "security_rules",
        "confirmation_rules",
        "system_prompt",
        "principal_gate",
    ],
)
def test_persona_rejects_authority_keys(forbidden):
    with pytest.raises(PersonaError):
        PersonaProfile.from_mapping({"persona_id": "whale", forbidden: ["com.example"]})


@pytest.mark.parametrize("bad", [{"tone": "sarcastic"}, {"reply_length": "epic"}, {"language": "xx-YY"}])
def test_persona_rejects_undeclared_values(bad):
    with pytest.raises(PersonaError):
        PersonaProfile.from_mapping({"persona_id": "whale", **bad})


def test_persona_cannot_change_principal_gate():
    """同样一条主人消息、同样一条客人消息：人设切换前后路由结果不变。"""
    raw = reply("REPLY", text="hi")
    owner_result = MessageRouter("A", lambda *_: raw).route(private("A"))
    guest_result = MessageRouter("A", lambda *_: raw).route(private("B"))
    for tone in ("soft", "concise", "playful", "formal"):
        PersonaProfile.from_mapping({"persona_id": "whale", "tone": tone})
        assert MessageRouter("A", lambda *_: raw).route(private("A")) == owner_result
        assert MessageRouter("A", lambda *_: raw).route(private("B")) == guest_result
    assert owner_result["principal"] == Principal.OWNER.value
    assert guest_result["principal"] == Principal.GUEST.value


def test_persona_cannot_unlock_package_policy():
    from astrbot_whale_device.security import PackagePolicy

    raw = launch_plan({"command": "device.app.launch", "payload": {"package_name": "com.example.wallet"}})
    PersonaProfile.from_mapping({"persona_id": "whale", "tone": "playful"})
    policy = PackagePolicy({"com.example.wallet"})
    routed = MessageRouter("A", lambda *_: raw, package_policy=policy).route(private("A"))
    # 敏感包即使出现在白名单里也仍然被拦，人设没有参与这条判断。
    assert routed["kind"] == "failed"
