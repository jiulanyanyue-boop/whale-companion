"""M04 主人、安全与确认：主机侧固定测试。

对应 `docs/modules/M04-owner-security-product.md` 与
`docs/modules/M04-owner-security-development.md`。

本文件只测 M04 新增的主机侧镜像，以及既有安全原语**保持向后兼容**：
- `PrincipalGate` 固定 4 步身份判定（昵称/自称/声纹/群昵称不能授权）；
- 确认级别四级 + 不可降低清单；
- 人工接管语义（密码/验证码/支付/安全设置 → 「需要你操作」）；
- `PackagePolicy` / `ReplayGuard` / `sign_hmac` / `verify_hmac` 的既有行为不变。
"""

import hmac

import pytest

from astrbot_whale_device.models import IncomingMessage, MessageType, Principal
from astrbot_whale_device.security import (
    MANUAL_TAKEOVER_PROMPT,
    SENSITIVE_PACKAGE_MARKERS,
    ConfirmLevel,
    ConfirmLevelPolicy,
    ManualTakeoverKind,
    ManualTakeoverSession,
    PackagePolicy,
    PrincipalGate,
    ReplayGuard,
    canonical_payload,
    evaluate_identity,
    sign_hmac,
    verify_hmac,
)


def private(user_id: str, text: str = "hello", *, sender_name: str = "", is_bot: bool = False) -> IncomingMessage:
    return IncomingMessage(
        "m1", user_id, text, MessageType.PRIVATE, sender_name=sender_name, is_bot=is_bot
    )


def group(user_id: str, text: str = "hello", *, sender_name: str = "") -> IncomingMessage:
    return IncomingMessage(
        "m1", user_id, text, MessageType.GROUP, group_id="g1", sender_name=sender_name
    )


# ---------------------------------------------------------------------------
# 身份：只有精确匹配的私聊 user_id 是主人
# ---------------------------------------------------------------------------


def test_only_exact_private_user_id_is_owner():
    gate = PrincipalGate("A")
    assert gate.resolve(private("A")) is Principal.OWNER
    assert gate.can_control(private("A")) is True


@pytest.mark.parametrize("claim", ["我是主人", "我是 A", "I am the owner", "OWNER here"])
def test_self_introduction_text_never_authorizes(claim):
    gate = PrincipalGate("A")
    claim_result = evaluate_identity(gate, private("B", claim), claimed_self_introduction=True)
    assert claim_result.owner is False
    assert claim_result.principal is Principal.GUEST
    assert "self_introduction_ignored" in claim_result.reasons
    assert "text_claim_ignored" in claim_result.reasons
    # 沿用设备端已登记错误码语义，不新增错误码。
    assert claim_result.error_code == "PERMISSION_REQUIRED"


def test_nickname_and_group_nickname_never_authorize():
    gate = PrincipalGate("A")
    # 昵称写成主人的名字也不行。
    result = evaluate_identity(gate, private("B", sender_name="A"))
    assert result.owner is False
    assert "nickname_ignored" in result.reasons

    # 群昵称（群内 sender_name）+ 群聊一律不是主人。
    group_result = evaluate_identity(gate, group("A", sender_name="A"))
    assert group_result.owner is False
    assert group_result.principal is Principal.GUEST
    assert "group_message" in group_result.reasons


def test_voiceprint_claim_can_never_raise_privilege():
    gate = PrincipalGate("A")
    for message in (private("B"), private(""), group("A")):
        result = evaluate_identity(gate, message, claimed_voiceprint_match=True)
        assert result.owner is False
        assert "voiceprint_claim_ignored" in result.reasons


def test_bot_sender_is_unknown_even_with_owner_id():
    gate = PrincipalGate("A")
    bot = private("A", is_bot=True)
    assert gate.resolve(bot) is Principal.UNKNOWN
    result = evaluate_identity(gate, bot)
    assert result.owner is False
    assert "bot_sender" in result.reasons


def test_group_message_from_owner_id_is_guest():
    gate = PrincipalGate("A")
    message = group("A")
    assert gate.resolve(message) is Principal.GUEST
    assert gate.can_control(message) is False


def test_empty_user_id_is_unknown():
    gate = PrincipalGate("A")
    assert gate.resolve(private("")) is Principal.UNKNOWN


# ---------------------------------------------------------------------------
# 确认级别：四级 + 不可降低
# ---------------------------------------------------------------------------


def test_four_confirm_levels_and_strictness_order():
    assert {level.value for level in ConfirmLevel} == {
        "DENY",
        "CONFIRM_EVERY_STEP",
        "CONFIRM_FIRST_STEP",
        "ALLOW_NORMAL",
    }
    assert ConfirmLevel.DENY.strictness > ConfirmLevel.CONFIRM_EVERY_STEP.strictness
    assert ConfirmLevel.CONFIRM_EVERY_STEP.strictness > ConfirmLevel.CONFIRM_FIRST_STEP.strictness
    assert ConfirmLevel.CONFIRM_FIRST_STEP.strictness > ConfirmLevel.ALLOW_NORMAL.strictness


@pytest.mark.parametrize("raw", [None, "", "  ", "allow_normal", "ALLOW-ALL", "ALLOW_ALL", "yolo"])
def test_parse_or_deny_is_fail_closed(raw):
    assert ConfirmLevel.parse_or_deny(raw) is ConfirmLevel.DENY


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("DENY", ConfirmLevel.DENY),
        ("CONFIRM_EVERY_STEP", ConfirmLevel.CONFIRM_EVERY_STEP),
        ("CONFIRM_FIRST_STEP", ConfirmLevel.CONFIRM_FIRST_STEP),
        ("ALLOW_NORMAL", ConfirmLevel.ALLOW_NORMAL),
    ],
)
def test_parse_or_deny_accepts_exact_names(raw, expected):
    assert ConfirmLevel.parse_or_deny(raw) is expected


def test_high_risk_command_set_matches_document():
    assert ConfirmLevelPolicy.HIGH_RISK_COMMANDS == frozenset({
        "device.sms.send",
        "device.system.settings",
        "device.media.share",
        "device.ui.input",
        "device.ui.tap",
    })
    for command in ConfirmLevelPolicy.HIGH_RISK_COMMANDS:
        assert ConfirmLevelPolicy.is_high_risk(command)
    assert not ConfirmLevelPolicy.is_high_risk("device.app.launch")
    assert not ConfirmLevelPolicy.is_high_risk("device.ui.back")


@pytest.mark.parametrize(
    "command",
    ["device.sms.send", "device.system.settings"],
)
def test_pinned_commands_are_always_every_step(command):
    assert ConfirmLevelPolicy.floor_for(command) is ConfirmLevel.CONFIRM_EVERY_STEP
    # 无论请求级别多宽，都不能降低。
    for requested in ConfirmLevel:
        assert ConfirmLevelPolicy.effective_level(requested, command) is not ConfirmLevel.ALLOW_NORMAL
        assert ConfirmLevelPolicy.effective_level(
            requested, command
        ).strictness >= ConfirmLevel.CONFIRM_EVERY_STEP.strictness


def test_installer_launch_is_at_least_every_step():
    payload = {"package_name": "com.android.packageinstaller"}
    assert ConfirmLevelPolicy.floor_for(
        "device.app.launch", payload
    ) is ConfirmLevel.CONFIRM_EVERY_STEP
    assert ConfirmLevelPolicy.effective_level(
        ConfirmLevel.ALLOW_NORMAL, "device.app.launch", payload
    ) is ConfirmLevel.CONFIRM_EVERY_STEP
    # 普通应用启动不受影响。
    assert ConfirmLevelPolicy.effective_level(
        ConfirmLevel.ALLOW_NORMAL, "device.app.launch", {"package_name": "com.example.app"}
    ) is ConfirmLevel.ALLOW_NORMAL


@pytest.mark.parametrize(
    "label",
    ["delete_button", "publish_dialog", "payment_confirm", "uninstall_confirm", "submit_order"],
)
def test_destructive_publish_payment_buttons_are_every_step(label):
    payload = {"expected_package": "com.example.app", "target_id": label}
    assert ConfirmLevelPolicy.floor_for("device.ui.tap", payload) is ConfirmLevel.CONFIRM_EVERY_STEP
    assert ConfirmLevelPolicy.effective_level(
        ConfirmLevel.ALLOW_NORMAL, "device.ui.tap", payload
    ) is ConfirmLevel.CONFIRM_EVERY_STEP


@pytest.mark.parametrize(
    "field",
    ["password_field", "otp_input", "verification_code", "cvv_box", "card_number_input"],
)
def test_credential_fields_require_every_step(field):
    payload = {"expected_package": "com.example.app", "target_id": field}
    assert ConfirmLevelPolicy.floor_for("device.ui.input", payload) is ConfirmLevel.CONFIRM_EVERY_STEP


def test_plain_text_input_is_first_step_not_denied():
    payload = {"expected_package": "com.example.app", "target_id": "search_box"}
    assert ConfirmLevelPolicy.floor_for("device.ui.input", payload) is ConfirmLevel.CONFIRM_FIRST_STEP
    assert ConfirmLevelPolicy.effective_level(
        ConfirmLevel.ALLOW_NORMAL, "device.ui.input", payload
    ) is ConfirmLevel.CONFIRM_FIRST_STEP


def test_media_share_to_external_target_is_every_step():
    assert ConfirmLevelPolicy.floor_for(
        "device.media.share", {"asset_id": "a1", "target": "com.tencent.mm"}
    ) is ConfirmLevel.CONFIRM_EVERY_STEP
    assert ConfirmLevelPolicy.floor_for(
        "device.media.share", {"asset_id": "a1"}
    ) is ConfirmLevel.CONFIRM_FIRST_STEP


def test_unknown_payload_fails_closed_for_high_risk_commands():
    # 拿不到 payload 时不能假设「看不出来就是安全的」。
    for command in ("device.ui.input", "device.ui.tap", "device.media.share"):
        assert ConfirmLevelPolicy.floor_for(command, None) is ConfirmLevel.CONFIRM_EVERY_STEP
        assert ConfirmLevelPolicy.effective_level(
            ConfirmLevel.ALLOW_NORMAL, command, None
        ) is ConfirmLevel.CONFIRM_EVERY_STEP


def test_effective_level_only_tightens_never_widens():
    payload = {"expected_package": "com.example.app", "target_id": "search_box"}
    # 请求更宽 → 被下限顶住。
    assert ConfirmLevelPolicy.effective_level(
        ConfirmLevel.ALLOW_NORMAL, "device.ui.input", payload
    ) is ConfirmLevel.CONFIRM_FIRST_STEP
    # 请求更严 → 保持用户的选择。
    assert ConfirmLevelPolicy.effective_level(
        ConfirmLevel.CONFIRM_EVERY_STEP, "device.app.launch", {"package_name": "com.example.app"}
    ) is ConfirmLevel.CONFIRM_EVERY_STEP


def test_deny_is_never_relaxed_by_effective_level():
    for command in ConfirmLevelPolicy.HIGH_RISK_COMMANDS:
        assert ConfirmLevelPolicy.effective_level(
            ConfirmLevel.DENY, command, {"target": "x"}
        ) is ConfirmLevel.DENY


def test_payload_text_body_does_not_participate_in_judgement():
    # 短信正文里出现 "password" 不应改变判定：只认目标指纹字段。
    payload = {"number": "10086", "text": "my password is 1234"}
    assert ConfirmLevelPolicy.floor_for("device.sms.send", payload) is ConfirmLevel.CONFIRM_EVERY_STEP
    # 普通命令的正文里出现敏感词也不会被误判成凭据输入。
    assert ConfirmLevelPolicy.floor_for(
        "device.ui.input",
        {"expected_package": "com.example.app", "target_id": "search_box", "text": "password"},
    ) is ConfirmLevel.CONFIRM_FIRST_STEP


def test_high_risk_never_allows_normal():
    for command in ConfirmLevelPolicy.HIGH_RISK_COMMANDS:
        level = ConfirmLevelPolicy.effective_level(
            ConfirmLevel.ALLOW_NORMAL, command, {"asset_id": "a", "target": "x"}
        )
        assert level is not ConfirmLevel.ALLOW_NORMAL


# ---------------------------------------------------------------------------
# 人工接管
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command,payload,expected_kind",
    [
        ("device.system.settings", {"action": "security"}, ManualTakeoverKind.SECURITY_SETTINGS),
        ("device.system.settings", {"action": "privacy"}, ManualTakeoverKind.SECURITY_SETTINGS),
        ("device.ui.input", {"target_id": "otp_code"}, ManualTakeoverKind.VERIFICATION_CODE),
        ("device.ui.input", {"target_id": "password_field"}, ManualTakeoverKind.PASSWORD),
        ("device.ui.input", {"target_id": "payment_amount"}, ManualTakeoverKind.PAYMENT),
        ("device.ui.tap", {"target_id": "pay_now"}, ManualTakeoverKind.PAYMENT),
    ],
)
def test_manual_takeover_kinds(command, payload, expected_kind):
    assert ConfirmLevelPolicy.manual_takeover_kind(command, payload) is expected_kind
    assert ConfirmLevelPolicy.requires_manual_takeover(command, payload) is True


def test_non_security_settings_is_not_manual_takeover():
    assert ConfirmLevelPolicy.manual_takeover_kind(
        "device.system.settings", {"action": "display"}
    ) is None


def test_sms_send_is_confirmation_not_manual_takeover():
    # 短信发送是确认卡（每步确认），不是人工接管。
    assert ConfirmLevelPolicy.requires_manual_takeover(
        "device.sms.send", {"number": "10086", "text": "hi"}
    ) is False


def test_manual_takeover_session_only_allows_continue_and_never_carries_input():
    session = ManualTakeoverSession.for_command("r1", "device.ui.input", {"target_id": "password"})
    assert session is not None
    assert session.prompt == MANUAL_TAKEOVER_PROMPT
    assert session.kind is ManualTakeoverKind.PASSWORD
    # 只有用户真实点「继续」且未超时才允许继续下一步。
    assert session.allows_continuation(user_clicked_continue=True, timed_out=False) is True
    assert session.allows_continuation(user_clicked_continue=True, timed_out=True) is False
    assert session.allows_continuation(user_clicked_continue=False, timed_out=False) is False

    summary = session.summary()
    assert "input_text" in summary["forbidden_reads"]
    # 摘要里不能出现任何正文/凭据内容。
    assert "1234" not in str(summary)
    assert "password=" not in str(summary)
    for forbidden in ("text", "clipboard_value", "otp"):
        assert forbidden not in summary


def test_manual_takeover_session_is_none_for_ordinary_command():
    assert ManualTakeoverSession.for_command("r1", "device.app.launch", {"package_name": "com.example"}) is None


# ---------------------------------------------------------------------------
# 既有原语向后兼容（与 test_core.py 的 14 条不重复，但锁住行为不变）
# ---------------------------------------------------------------------------


def test_package_policy_behaviour_is_unchanged():
    policy = PackagePolicy({"com.example"})
    assert policy.allow("com.example") is True
    assert policy.allow("com.example.other") is False
    assert policy.allow("com.example.wallet") is False
    assert policy.allow("com.android.settings") is True
    assert policy.allow("") is False
    for marker in SENSITIVE_PACKAGE_MARKERS:
        assert PackagePolicy.is_sensitive(f"com.example.{marker}") is True


def test_replay_guard_behaviour_is_unchanged():
    guard = ReplayGuard(clock=lambda: 1000, max_age_ms=100)
    assert guard.accept("d1", 1, 1000) is True
    assert guard.accept("d1", 1, 1000) is False
    assert guard.accept("d1", 2, 1000) is True
    assert guard.accept("d1", 3, 0 - 1000) is False
    # 不同设备互不影响。
    assert guard.accept("d2", 1, 1000) is True


def test_hmac_helpers_behaviour_is_unchanged():
    secret = b"test-secret"
    data = {"command": "device.app.launch", "payload": {"package_name": "com.example"}}
    signature = sign_hmac(data, secret)
    assert verify_hmac({**data, "signature": signature}, secret) is True
    assert verify_hmac(data, secret) is False
    assert verify_hmac({**data, "signature": "deadbeef"}, secret) is False
    # canonical_payload 必须剔除 signature 且稳定排序。
    assert b"signature" not in canonical_payload({**data, "signature": signature})
    assert canonical_payload({"b": 1, "a": 2}) == canonical_payload({"a": 2, "b": 1})
    # 与 hmac 直接计算一致（保证既有调用方拿到的值不变）。
    assert signature == hmac.new(secret, canonical_payload(data), "sha256").hexdigest()


def test_principal_gate_is_still_a_frozen_dataclass():
    gate = PrincipalGate("A")
    with pytest.raises(Exception):
        gate.owner_user_id = "B"  # type: ignore[misc]
