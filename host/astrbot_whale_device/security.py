from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterable

from .models import IncomingMessage, MessageType, Principal


SENSITIVE_PACKAGE_MARKERS = (
    "alipay",
    "wallet",
    "bank",
    "password",
    "authenticator",
    "otp",
    "keepass",
)
SAFE_SYSTEM_PACKAGES = {
    "android",
    "com.android.systemui",
    "com.android.settings",
    "com.google.android.permissioncontroller",
}


class SecurityError(ValueError):
    pass


@dataclass(frozen=True)
class PrincipalGate:
    owner_user_id: str

    def resolve(self, message: IncomingMessage) -> Principal:
        if not message.user_id or message.is_bot:
            return Principal.UNKNOWN
        if message.message_type is MessageType.GROUP:
            return Principal.GUEST
        if hmac.compare_digest(message.user_id, self.owner_user_id):
            return Principal.OWNER
        return Principal.GUEST

    def can_control(self, message: IncomingMessage) -> bool:
        return self.resolve(message) is Principal.OWNER and message.message_type is MessageType.PRIVATE


class PackagePolicy:
    def __init__(self, allowed_packages: set[str] | None = None, blocked_packages: set[str] | None = None) -> None:
        self.allowed_packages = set(allowed_packages or ())
        self.blocked_packages = set(blocked_packages or ())

    @staticmethod
    def is_sensitive(package_name: str) -> bool:
        value = package_name.lower()
        return any(marker in value for marker in SENSITIVE_PACKAGE_MARKERS)

    def allow(self, package_name: str, system_package: bool = False) -> bool:
        if not package_name:
            return False
        if package_name in self.blocked_packages or self.is_sensitive(package_name):
            return False
        return system_package or package_name in SAFE_SYSTEM_PACKAGES or package_name in self.allowed_packages


class ReplayGuard:
    def __init__(self, clock: Callable[[], int] | None = None, max_age_ms: int = 120_000) -> None:
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.max_age_ms = max_age_ms
        self._last_counter: dict[str, int] = {}

    def accept(self, device_id: str, counter: int, issued_at_ms: int) -> bool:
        now = self._clock()
        if issued_at_ms and abs(now - issued_at_ms) > self.max_age_ms:
            return False
        previous = self._last_counter.get(device_id, -1)
        if counter <= previous:
            return False
        self._last_counter[device_id] = counter
        return True


def canonical_payload(data: dict[str, Any]) -> bytes:
    unsigned = {k: v for k, v in data.items() if k != "signature"}
    return json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_hmac(data: dict[str, Any], secret: bytes) -> str:
    return hmac.new(secret, canonical_payload(data), hashlib.sha256).hexdigest()


def verify_hmac(data: dict[str, Any], secret: bytes) -> bool:
    actual = str(data.get("signature", ""))
    expected = sign_hmac(data, secret)
    return bool(actual) and hmac.compare_digest(actual, expected)


# ======================================================================================
# M04 主人、安全与确认：主机侧镜像（纯新增，不改上面任何既有符号的行为）
#
# 规范来源：docs/modules/M04-owner-security-product.md / M04-owner-security-development.md
#
# 本段只做三件事：
# 1. 确认级别枚举 + 「不可降低清单」的主机侧镜像（与设备端 ConfirmLevelPolicy 对齐）；
# 2. 人工接管语义（密码/验证码/支付/安全设置 → 必须暂停并提示「需要你操作」）；
# 3. 更强的身份判定辅助（昵称/群昵称/自称/声纹一律不能授权）。
#
# 明确不做：不改 PrincipalGate.resolve 的固定 4 步顺序、不放宽 PackagePolicy、
# 不引入任何新错误码（错误语义复用设备端已登记的 ACTION_REJECTED / PERMISSION_REQUIRED 等）。
# ======================================================================================


class ConfirmLevel(str, Enum):
    """M04 固定四级确认级别（`docs/modules/M04-owner-security-product.md`「确认级别」）。

    `DENY` 语义是「不执行」，不是「更严格的确认」；因此 [ConfirmLevelPolicy.effective_level]
    对它单独短路，绝不因「取更严」而被放宽。
    """

    DENY = "DENY"
    CONFIRM_EVERY_STEP = "CONFIRM_EVERY_STEP"
    CONFIRM_FIRST_STEP = "CONFIRM_FIRST_STEP"
    ALLOW_NORMAL = "ALLOW_NORMAL"

    @property
    def strictness(self) -> int:
        return _CONFIRM_STRICTNESS[self]

    def at_least(self, other: "ConfirmLevel") -> "ConfirmLevel":
        """取两个级别里更严格的那个。"""
        return self if self.strictness >= other.strictness else other

    @classmethod
    def parse_or_deny(cls, raw: str | None) -> "ConfirmLevel":
        """解析配置里写下的字符串；无法识别的一律 DENY（fail-closed）。"""
        value = (raw or "").strip()
        for member in cls:
            if member.value == value:
                return member
        return cls.DENY


_CONFIRM_STRICTNESS: dict[ConfirmLevel, int] = {
    ConfirmLevel.DENY: 3,
    ConfirmLevel.CONFIRM_EVERY_STEP: 2,
    ConfirmLevel.CONFIRM_FIRST_STEP: 1,
    ConfirmLevel.ALLOW_NORMAL: 0,
}


class ManualTakeoverKind(str, Enum):
    """必须暂停并显示「需要你操作」的四类场景（文档「人工接管」）。"""

    PASSWORD = "PASSWORD"
    VERIFICATION_CODE = "VERIFICATION_CODE"
    PAYMENT = "PAYMENT"
    SECURITY_SETTINGS = "SECURITY_SETTINGS"


# 人工接管时展示给用户的固定提示；不含任何输入内容。
MANUAL_TAKEOVER_PROMPT = "需要你操作"


class ConfirmLevelPolicy:
    """M04 确认级别策略的主机侧镜像（与设备端 `ConfirmLevelPolicy` 同构）。

    - [HIGH_RISK_COMMANDS] 是 M04 权威高风险清单，与开发文档逐条一致；
    - [PINNED_EVERY_STEP_COMMANDS] 是「固定至少 CONFIRM_EVERY_STEP」里按命令名锁死的项；
    - payload 级细化只看「目标指纹」类字段，**刻意排除** `text` 等正文类字段
      （短信正文、用户输入不参与判定，也不进日志）。
    """

    # M04 权威高风险命令集合（开发文档「高风险命令」小节逐条一致）。
    HIGH_RISK_COMMANDS: frozenset[str] = frozenset({
        "device.sms.send",
        "device.system.settings",
        "device.media.share",
        "device.ui.input",
        "device.ui.tap",
    })

    # 命令级直接锁死「每步确认」的项（不依赖 payload）。
    PINNED_EVERY_STEP_COMMANDS: frozenset[str] = frozenset({
        "device.sms.send",
        "device.system.settings",
    })

    CREDENTIAL_MARKERS: tuple[str, ...] = (
        "password", "passwd", "pwd", "pin", "otp", "verification", "verify",
        "captcha", "credential", "secret", "token", "cvv", "card_number", "account_number",
    )
    PAYMENT_MARKERS: tuple[str, ...] = (
        "payment", "checkout", "pay", "transfer", "amount", "wallet", "bank", "alipay", "weixin_pay",
    )
    DESTRUCTIVE_MARKERS: tuple[str, ...] = (
        "delete", "remove", "uninstall", "clear_data", "reset", "publish", "post", "submit", "send_now",
    )
    INSTALLER_MARKERS: tuple[str, ...] = (
        "packageinstaller", "installer", "install_package", "install_app",
    )
    SECURITY_MARKERS: tuple[str, ...] = (
        "security", "privacy", "permission", "developer", "unknown_sources",
        "accessibility", "device_admin", "lock_screen", "screen_lock",
    )

    # 参与 payload 级判定的字段白名单（不含 text/textValue 等正文类字段）。
    MATCH_FIELDS: tuple[str, ...] = (
        "target_id", "selector_hash", "expected_package", "package_name",
        "target", "action", "label", "content_description",
    )
    _MAX_MATCH_FIELDS = 12
    _MAX_MATCH_FIELD_LENGTH = 512

    @classmethod
    def _match_text(cls, payload: dict[str, Any] | None) -> str:
        if not payload:
            return ""
        parts: list[str] = []
        for key in cls.MATCH_FIELDS[: cls._MAX_MATCH_FIELDS]:
            value = payload.get(key)
            if isinstance(value, str) and value:
                parts.append(value)
        return (" ".join(parts))[: cls._MAX_MATCH_FIELD_LENGTH].lower()

    @staticmethod
    def _contains_any(text: str, markers: Iterable[str]) -> bool:
        return bool(text) and any(marker in text for marker in markers)

    @classmethod
    def floor_for(cls, command: str, payload: dict[str, Any] | None = None) -> ConfirmLevel:
        """固定下限：该命令允许达到的最宽确认级别。

        `payload is None` 表示调用方拿不到 payload → **fail-closed**，按更严的下限处理。
        """
        text = cls._match_text(payload)
        unknown = payload is None

        if command in cls.PINNED_EVERY_STEP_COMMANDS:
            return ConfirmLevel.CONFIRM_EVERY_STEP

        if command == "device.ui.input":
            if unknown or cls._contains_any(text, cls.CREDENTIAL_MARKERS + cls.PAYMENT_MARKERS):
                return ConfirmLevel.CONFIRM_EVERY_STEP
            return ConfirmLevel.CONFIRM_FIRST_STEP

        if command == "device.ui.tap":
            if unknown or cls._contains_any(
                text, cls.DESTRUCTIVE_MARKERS + cls.PAYMENT_MARKERS + cls.INSTALLER_MARKERS
            ):
                return ConfirmLevel.CONFIRM_EVERY_STEP
            return ConfirmLevel.CONFIRM_FIRST_STEP

        if command == "device.media.share":
            target = payload.get("target") if isinstance(payload, dict) else None
            # 目标是外部社交应用（只要给了 target 就按最严处理），拿不到 payload 同样 fail-closed。
            if unknown or (isinstance(target, str) and target.strip()):
                return ConfirmLevel.CONFIRM_EVERY_STEP
            return ConfirmLevel.CONFIRM_FIRST_STEP

        if command == "device.app.launch":
            if payload is not None and cls._contains_any(text, cls.INSTALLER_MARKERS):
                return ConfirmLevel.CONFIRM_EVERY_STEP
            return ConfirmLevel.ALLOW_NORMAL

        return ConfirmLevel.ALLOW_NORMAL

    @classmethod
    def effective_level(
        cls,
        requested: ConfirmLevel,
        command: str,
        payload: dict[str, Any] | None = None,
    ) -> ConfirmLevel:
        """最终生效级别 = max(请求级别, 固定下限)：**只收紧、不放宽**。

        人设或模型把级别降到 ALLOW_NORMAL，也压不过系统设置、短信发送等固定项。
        """
        if requested is ConfirmLevel.DENY:
            return ConfirmLevel.DENY
        floor = cls.floor_for(command, payload)
        if floor is ConfirmLevel.DENY:
            return ConfirmLevel.DENY
        return requested.at_least(floor)

    @classmethod
    def is_high_risk(cls, command: str) -> bool:
        return command in cls.HIGH_RISK_COMMANDS

    @classmethod
    def requires_manual_takeover(cls, command: str, payload: dict[str, Any] | None = None) -> bool:
        """是否必须走人工接管（密码/验证码/支付/安全设置）。

        语义边界：`payload is None`（拿不到 payload）时，`device.system.settings` 按
        「可能是安全设置」**fail-closed** 判为人工接管；`device.ui.input` / `device.ui.tap`
        则退回「每步确认」而不是无条件接管——因为前者可能是普通文本输入、后者可能是普通按钮，
        一律升级成人工接管会让产品不可用。这条边界在 `test_security.py` 里被固定住。
        """
        return cls.manual_takeover_kind(command, payload) is not None

    @classmethod
    def manual_takeover_kind(
        cls, command: str, payload: dict[str, Any] | None = None
    ) -> ManualTakeoverKind | None:
        text = cls._match_text(payload)

        if command == "device.system.settings":
            # 系统设置里涉及安全项（权限/隐私/开发者/锁屏/无障碍）必须人工接管；
            # 拿不到 payload 时按 fail-closed 处理。
            if payload is None or cls._contains_any(text, cls.SECURITY_MARKERS):
                return ManualTakeoverKind.SECURITY_SETTINGS
            return None

        if command == "device.ui.input":
            if cls._contains_any(text, cls.PAYMENT_MARKERS):
                return ManualTakeoverKind.PAYMENT
            if cls._contains_any(text, cls.CREDENTIAL_MARKERS):
                # 验证码与密码都是凭据输入；按字段名细分提示类型。
                if cls._contains_any(text, ("otp", "verification", "verify", "captcha")):
                    return ManualTakeoverKind.VERIFICATION_CODE
                return ManualTakeoverKind.PASSWORD
            return None

        if command == "device.ui.tap":
            if cls._contains_any(text, cls.PAYMENT_MARKERS):
                return ManualTakeoverKind.PAYMENT
            return None

        if command == "device.sms.send":
            # 短信发送不是「人工接管」，是确认卡（CONFIRM_EVERY_STEP）。
            return None

        return None


# 昵称/群昵称/自称这类「看起来像主人」的字段：任何情况下都不能授权。
_NON_AUTHORIZING_TEXT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"我是主人"),
    re.compile(r"我是\s*A\b"),
    re.compile(r"\bi\s*am\s*(the\s*)?owner\b", re.IGNORECASE),
    re.compile(r"\bowner\s*here\b", re.IGNORECASE),
    re.compile(r"忽略(之前|上面)?的?(所有)?(规则|指令|限制)"),
    re.compile(r"\bignore\s+(all\s+)?(previous|prior|above)\s+instructions?\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class IdentityClaim:
    """一次身份判定的结果快照。

    - [owner] 只可能来自 `PrincipalGate` 的精确 `user_id` + 私聊匹配；
    - [reasons] 记录「被忽略的非授权因素」，供审计，**不含**消息正文。
    """

    owner: bool
    principal: Principal
    reasons: tuple[str, ...] = ()

    @property
    def error_code(self) -> str:
        """失败时沿用设备端已登记的错误码，不新增错误码。"""
        return "" if self.owner else "PERMISSION_REQUIRED"


def evaluate_identity(
    gate: PrincipalGate,
    message: IncomingMessage,
    *,
    claimed_voiceprint_match: bool = False,
    claimed_self_introduction: bool = False,
) -> IdentityClaim:
    """更强的身份判定辅助（**只做加法**，不改 [PrincipalGate] 的固定 4 步）。

    文档禁止项在这里落地：
    - 昵称/头像/群昵称/消息内容/声纹/自称「我是主人」都不能授权；
    - 语音识别结果不能当身份凭据（`claimed_voiceprint_match` 只被记录为「已忽略」）；
    - 群聊永远不是主人（`PrincipalGate.resolve` 里 GROUP → GUEST）。

    `claimed_voiceprint_match` / `claimed_self_introduction` 只是**审计输入**，
    对返回值没有任何影响——这正是「不能提升权限」的实现形式。
    """
    principal = gate.resolve(message)
    is_owner = principal is Principal.OWNER and message.message_type is MessageType.PRIVATE

    reasons: list[str] = []
    if claimed_voiceprint_match:
        reasons.append("voiceprint_claim_ignored")
    if claimed_self_introduction:
        reasons.append("self_introduction_ignored")
    if message.sender_name:
        reasons.append("nickname_ignored")
    if message.text and any(p.search(message.text) for p in _NON_AUTHORIZING_TEXT_PATTERNS):
        reasons.append("text_claim_ignored")
    if message.is_bot:
        reasons.append("bot_sender")
    if message.message_type is MessageType.GROUP:
        reasons.append("group_message")

    return IdentityClaim(owner=is_owner, principal=principal, reasons=tuple(reasons))


@dataclass(frozen=True)
class ManualTakeoverSession:
    """人工接管请求的主机侧描述：只承载「请用户自己操作」，**不承载任何输入内容**。

    设备端 `UserGate` 负责真正的等待与「继续」；主机侧只做三件事：
    生成提示、拒绝在此期间继续下发动作、以及提醒调用方不要回读屏幕内容。
    """

    request_id: str
    command: str
    kind: ManualTakeoverKind
    prompt: str = MANUAL_TAKEOVER_PROMPT
    # 禁止在接管期间回读的字段（锁屏密码、验证码、支付密码等）。
    forbidden_reads: tuple[str, ...] = ("input_text", "clipboard", "screen_text", "sms_body")

    @staticmethod
    def for_command(
        request_id: str,
        command: str,
        payload: dict[str, Any] | None = None,
    ) -> "ManualTakeoverSession | None":
        """命令需要人工接管时返回会话描述，否则 `None`。"""
        kind = ConfirmLevelPolicy.manual_takeover_kind(command, payload)
        if kind is None:
            return None
        return ManualTakeoverSession(request_id=request_id, command=command, kind=kind)

    def allows_continuation(self, *, user_clicked_continue: bool, timed_out: bool) -> bool:
        """只有用户真实点了「继续」且未超时才允许继续下一步。"""
        return bool(user_clicked_continue) and not timed_out

    def summary(self) -> dict[str, Any]:
        """可安全写进日志/回包的摘要（无正文、无凭据）。"""
        return {
            "request_id": self.request_id,
            "command": self.command,
            "kind": self.kind.value,
            "prompt": self.prompt,
            "forbidden_reads": list(self.forbidden_reads),
        }





