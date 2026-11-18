"""M12 审计脱敏。

规范来源：``docs/modules/M12-recovery-audit-product.md``「审计内容」与
``M12-recovery-audit-development.md``「审计脱敏」。

固定规则（少一条都不算合规）：

- **只记录**：事件类型、时间、模式、发送者分类、命令名、包名、结果、错误码。
- **禁止写入**：密码、验证码、短信正文、完整联系人号码、屏幕节点文本、原始音频。
- 电话号码**最多保留后四位**。

设计要点：脱敏是**纯函数**（无 I/O、无全局状态、不依赖 ``android.util.Log``），
因此可以被逐条单测，也可以被 Kotlin 侧逐行镜像。仓储层
(``TaskStore.save_audit``) 只负责落库，**不负责判断该不该写**——判断全部在
:func:`sanitize_detail`。

采用「默认拒绝」而不是「黑名单」：只有落进白话白名单的键才可能被写入。
黑名单永远漏，白名单不会。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping


#: 允许进入审计 detail 的键（白名单，默认拒绝）。
ALLOWED_DETAIL_KEYS: frozenset[str] = frozenset({
    # 身份分类（不是原始 QQ 号：原始号只进 actor 列，不进 detail）
    "mode",
    "message_type",
    "sender_class",
    "actor_class",
    # 命令与结果
    "command",
    "package_name",
    "result",
    "status",
    "error_code",
    "outcome",
    "decision",
    # 设备
    "device_id",
    "connection",
    "connection_state",
    "state",
    "reason",
    # Run 结构
    "run_id",
    "run_status",
    "step_index",
    "command_index",
    "total_commands",
    "from",
    "to",
    "phase",
    "layer",
    "count",
    # 时间与幂等
    "updated_at",
    "created_at",
    "sent_at",
    "attempts",
    "retries",
    "idempotency_key",
    # 恢复语义
    "resumed",
    "skipped",
    "executed",
    "requires_reconfirmation",
    "revalidate_required",
    "lease_until",
})

#: 即使误入白名单也必须硬拒的键（纵深防御；白名单与本表互为双保险）。
FORBIDDEN_DETAIL_KEYS: frozenset[str] = frozenset({
    "password",
    "passwd",
    "pwd",
    "otp",
    "code",
    "verification_code",
    "sms_body",
    "sms_text",
    "body",
    "text",
    "node_text",
    "nodes",
    "screen_text",
    "screen_nodes",
    "screenshot",
    "raw_audio",
    "audio",
    "media",
    "media_bytes",
    "media_content",
    "content",
    "payload",
    "path",
    "uri",
    "file",
    "clipboard",
    "token",
    "secret",
    "api_key",
    "credential",
    "credentials",
})

#: 值里一旦出现这些字面量，整段值丢弃（宁可少记，不可泄漏）。
SENSITIVE_VALUE_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "验证码",
    "密码",
    "短信正文",
    "otp ",
    "verification code",
)

MAX_DETAIL_KEYS = 24
MAX_LIST_ITEMS = 20
MAX_TEXT_CHARS = 512
MAX_DEPTH = 3
#: 电话号码「最多保留后四位」——少于 7 位的数字串不算电话号码，原样保留。
PHONE_MIN_DIGITS = 7

_PHONE_RUN = re.compile(r"(?<!\d)(\d{7,20})(?!\d)")


class AuditError(ValueError):
    pass


# ---------------------------------------------------------------------------
# 纯函数：脱敏
# ---------------------------------------------------------------------------


def redact_phone(value: str) -> str:
    """把长度 >= 7 的数字串只留后四位，前面全部打码。"""
    if not isinstance(value, str) or not value:
        return value

    def _mask(match: re.Match[str]) -> str:
        digits = match.group(1)
        return "*" * (len(digits) - 4) + digits[-4:]

    return _PHONE_RUN.sub(_mask, value)


def redact_text(value: Any) -> str | None:
    """自由文本的最小安全化。

    返回 ``None`` 表示**该值必须整体丢弃**（命中了敏感字面量）；
    返回空字符串表示原本就是空值（属合法状态，如「无错误码」）。
    """
    if not isinstance(value, str):
        return None
    lowered = value.lower()
    if any(marker in lowered for marker in SENSITIVE_VALUE_MARKERS):
        return None
    masked = redact_phone(value)
    if len(masked) > MAX_TEXT_CHARS:
        return masked[:MAX_TEXT_CHARS] + "…"
    return masked


def _is_forbidden_key(key: str) -> bool:
    name = key.strip().lower()
    if not name:
        return True
    if name in FORBIDDEN_DETAIL_KEYS:
        return True
    # 驼峰/点号变体：screenText / screen.text / rawAudio
    flat = name.replace("_", "").replace("-", "").replace(".", "")
    return flat in {item.replace("_", "") for item in FORBIDDEN_DETAIL_KEYS}


_DROP = object()  # 哨兵：表示「该值必须整体丢弃」，与合法的空值区分开


def _scrub(value: Any, depth: int) -> Any:
    """把任意值收敛成可安全落库的 JSON 值；无法收敛的一律丢弃。"""
    if depth > MAX_DEPTH:
        return _DROP
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        # 只保留 2^53 以内的整数，避免超大整数被当成标识/内容。
        return value if -9_007_199_254_740_992 <= value <= 9_007_199_254_740_992 else _DROP
    if isinstance(value, float):
        return value if value == value and abs(value) <= 1e308 else _DROP  # 排除 NaN/Inf
    if value is None:
        return None
    if isinstance(value, str):
        scrubbed = redact_text(value)
        return _DROP if scrubbed is None else scrubbed
    if isinstance(value, Mapping):
        nested = sanitize_detail(value, depth=depth + 1)
        return nested if nested else _DROP
    if isinstance(value, (list, tuple)):
        items: list[Any] = []
        for entry in list(value)[:MAX_LIST_ITEMS]:
            scrubbed = _scrub(entry, depth + 1)
            if scrubbed is _DROP:
                continue
            items.append(scrubbed)
        return items if items else _DROP
    # 其它类型（bytes、Path、对象）一律不写。
    return _DROP


def sanitize_detail(detail: Any, depth: int = 0) -> dict[str, Any]:
    """纯函数：把任意 detail 收敛成白名单内的安全 dict。

    - 非 Mapping → 空 dict
    - 键不在白名单 → 丢弃（默认拒绝）
    - 命中 FORBIDDEN_DETAIL_KEYS → 丢弃（纵深防御）
    - 值命中敏感字面量 → 丢弃该键
    - 字符串截断 + 号码打码；嵌套最多 3 层、每层最多 24 键 / 20 项
    """
    if not isinstance(detail, Mapping):
        return {}
    result: dict[str, Any] = {}
    for index, (raw_key, raw_value) in enumerate(detail.items()):
        if index >= MAX_DETAIL_KEYS:
            break
        key = str(raw_key)
        if " " in key or not key.strip():
            continue
        if _is_forbidden_key(key):
            continue
        if key not in ALLOWED_DETAIL_KEYS:
            continue
        scrubbed = _scrub(raw_value, depth)
        if scrubbed is _DROP:
            continue
        result[key] = scrubbed
    return result


def audit_record(
    event_type: str,
    *,
    message_id: str = "",
    actor: str = "",
    detail: Any = None,
    created_at: int = 0,
) -> dict[str, Any]:
    """纯函数：构造一条可直接落库、也可直接进日志的审计记录。"""
    if not isinstance(event_type, str) or not event_type.strip():
        raise AuditError("audit event_type is required")
    return {
        "event_type": event_type,
        "message_id": str(message_id or ""),
        "actor_user_id": str(actor or ""),
        "detail": sanitize_detail(detail),
        "created_at": int(created_at),
    }


#: M12 固定审计事件名。新增名必须先写进文档，不许在代码里随手造。
AUDIT_EVENTS: frozenset[str] = frozenset({
    "run.transition",
    "run_resumed",
    "run_recovery_planned",
    "run_recovery_reconfirm",
    "step_already_done",
    "device_recovered",
    "device_counter_advanced",
    "audit.retention_swept",
})


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class AuditLogger:
    """审计写入边界：脱敏 → 落 ``audit_events``。

    所有 detail 都过 :func:`sanitize_detail`，**调用方即使传了敏感字段也进不去**。
    """

    store: Any
    clock: Callable[[], int] = field(default=_now_ms)

    # -- 写 --------------------------------------------------------------
    def record(
        self,
        event_type: str,
        *,
        message_id: str = "",
        actor: str = "",
        detail: Any = None,
        created_at: int | None = None,
    ) -> str:
        stamp = self.clock() if created_at is None else int(created_at)
        record = audit_record(event_type, message_id=message_id, actor=actor, detail=detail, created_at=stamp)
        return self.store.save_audit(
            record["event_type"],
            record["actor_user_id"],
            record["message_id"],
            record["detail"],
            record["created_at"],
        )

    # -- 读 --------------------------------------------------------------
    def events(self, event_type: str | None = None, *, limit: int = 200) -> list[dict[str, Any]]:
        return self.store.audit_events(event_type, limit=limit)

    def count(self, event_type: str | None = None) -> int:
        return len(self.events(event_type, limit=10_000))

    # -- 保留期 ----------------------------------------------------------
    def sweep(self, now_ms: int | None = None) -> dict[str, int]:
        stamp = self.clock() if now_ms is None else int(now_ms)
        purged = self.store.purge_expired(stamp)
        self.record(
            "audit.retention_swept",
            detail={
                "count": int(purged.get("audit_events", 0)),
                "updated_at": stamp,
                "result": f"effects={purged.get('effects', 0)};assets={purged.get('assets', 0)}",
            },
            created_at=stamp,
        )
        return purged


def dumps(record: Mapping[str, Any]) -> str:
    """审计记录的稳定序列化（便于比对与测试）。"""
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
