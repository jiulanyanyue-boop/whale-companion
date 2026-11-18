"""M10 定时任务与主动问候：Schema、时区、时间窗口与状态机。

规范来源：``docs/modules/M10-scheduler-product.md`` 与
``M10-scheduler-development.md``。

本模块承载 M10 的**纯逻辑层**：固定 schema 校验、时区解析、下一次运行时刻、
安静时段判定、状态机与租约。真正的「十步保护顺序」编排在
:mod:`astrbot_whale_device.schedule_worker`。

向后兼容：``Schedule`` / ``ScheduleStore.save`` / ``ScheduleStore.due`` 的
签名与行为**一字未改**（``host/tests/test_core.py`` 依赖它们）。新增能力全部是追加，
且**不修改任何既有表结构**（只在 ``schedules`` 上做向后兼容的加列）。

时区的重要实况（本机实测）：Python 3.15 有 ``zoneinfo`` 模块，但**没有 tzdata**，
``ZoneInfo("Asia/Shanghai")`` 直接抛 ``ZoneInfoNotFoundError``。因此本模块：

1. 先试 ``zoneinfo``（装了 tzdata 就自动用真数据）；
2. 退回内置的**固定偏移**时区表（只收录无夏令时的时区，偏移恒定不会算错）；
3. 两者都不行 → **显式拒绝**。绝不拿一个猜出来的偏移去算触发时刻——
   主动问候错一小时比不问候糟糕得多。

DST 时区（如 ``America/New_York``）在无 tzdata 时会被拒绝；想支持就装 ``tzdata``
或注入自定义 ``tz_resolver``（见 :func:`resolve_timezone` 的 ``resolver`` 参数）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Callable, Mapping


# ---------------------------------------------------------------------------
# 固定 schema（M10 开发文档「Schedule schema」）
# ---------------------------------------------------------------------------

#: 固定字段，顺序与文档一致。多一个少一个都算不合规。
FIXED_FIELDS: tuple[str, ...] = (
    "schedule_id",
    "enabled",
    "kind",
    "timezone",
    "at",
    "weekdays",
    "quiet_start",
    "quiet_end",
    "daily_limit",
    "cooldown_seconds",
    "needs_live_device",
    "next_run_ms",
)

#: 三种调度类型，不多不少。
SCHEDULE_KINDS: frozenset[str] = frozenset({"daily", "weekly", "window"})

#: 时间窗口容差：DUE 项迟到超过这个长度就不再补发，只重排下一次。
#: （防止主机停机数小时后重启，把攒了一夜的问候一口气全轰出去）
WINDOW_TOLERANCE_MS = 30 * 60 * 1000

#: 租约固定 60 秒（M10 开发文档「Worker」）。
SCHEDULE_LEASE_MS = 60_000

_DATA_DEFAULT_FIELDS: dict[str, Any] = {
    "enabled": False,
    "kind": "daily",
    "timezone": "Asia/Shanghai",
    "at": "08:30",
    "weekdays": [],
    "quiet_start": "",
    "quiet_end": "",
    "daily_limit": 3,
    "cooldown_seconds": 3600,
    "needs_live_device": False,
    "next_run_ms": 0,
}

_TIME_RE = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


class ScheduleConfigError(ValueError):
    """Schedule schema 或时区不合规。消息里只描述规则，不回显用户数据。"""


# ---------------------------------------------------------------------------
# 时区：zoneinfo → 固定偏移 → 显式拒绝
# ---------------------------------------------------------------------------

#: 只收录**无夏令时**的时区：偏移恒定，算出来的触发时刻不会因为 DST 偏一小时。
_FIXED_OFFSET_HOURS: dict[str, float] = {
    "UTC": 0.0,
    "Etc/UTC": 0.0,
    "Etc/GMT": 0.0,
    "Asia/Shanghai": 8.0,
    "Asia/Taipei": 8.0,
    "Asia/Hong_Kong": 8.0,
    "Asia/Macau": 8.0,
    "Asia/Singapore": 8.0,
    "Asia/Chongqing": 8.0,
    "Asia/Harbin": 8.0,
    "Asia/Tokyo": 9.0,
    "Asia/Seoul": 9.0,
    "Asia/Bangkok": 7.0,
    "Asia/Jakarta": 7.0,
    "Asia/Ho_Chi_Minh": 7.0,
    "Asia/Kolkata": 5.5,
    "Asia/Dubai": 4.0,
    "Asia/Riyadh": 3.0,
    "Europe/Moscow": 3.0,
    "Africa/Johannesburg": 2.0,
}

_TIMEZONE_NAME_RE = re.compile(r"[A-Za-z0-9_+/-]{1,64}")


def _fixed_tz(hours: float) -> tzinfo:
    return timezone(timedelta(hours=hours))


def resolve_timezone(
    name: str,
    resolver: Callable[[str], tzinfo] | None = None,
) -> tzinfo:
    """把配置里的时区名解析成 ``tzinfo``。

    顺序固定：注入的 ``resolver`` → ``zoneinfo`` → 内置固定偏移表 → 拒绝。
    拒绝是**显式**的：宁可报错，也不拿猜出来的偏移算主动问候时间。
    """
    if not isinstance(name, str) or _TIMEZONE_NAME_RE.fullmatch(name) is None:
        raise ScheduleConfigError("timezone must be an IANA-like name")
    if resolver is not None:
        try:
            return resolver(name)
        except Exception as exc:  # noqa: BLE001 - 注入方失败即显式拒绝
            raise ScheduleConfigError("timezone resolver rejected the requested zone") from exc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - 缺 tzdata / 未知时区都走兜底
        pass
    if name in _FIXED_OFFSET_HOURS:
        return _fixed_tz(_FIXED_OFFSET_HOURS[name])
    raise ScheduleConfigError(
        "timezone requires tzdata or a fixed-offset zone; "
        "daylight-saving zones are refused rather than guessed"
    )


def local_datetime(utc_ms: int, tz: tzinfo) -> datetime:
    """UTC epoch ms → 配置时区的本地时间。数据库只存 UTC ms，换算在这里。"""
    return datetime.fromtimestamp(int(utc_ms) / 1000, tz=tz)


def parse_hm(value: str) -> tuple[int, int]:
    """解析 ``HH:MM``；格式不符直接拒绝。"""
    if not isinstance(value, str):
        raise ScheduleConfigError("time must be a HH:MM string")
    match = _TIME_RE.fullmatch(value.strip())
    if match is None:
        raise ScheduleConfigError("time must be a HH:MM string within 00:00..23:59")
    return int(match.group(1)), int(match.group(2))


def _at_hm(day: datetime, hour: int, minute: int) -> datetime:
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


def next_occurrence_ms(spec: "ScheduleSpec", now_ms: int) -> int:
    """算出严格晚于 ``now_ms`` 的下一次触发时刻（UTC epoch ms）。"""
    tz = resolve_timezone(spec.timezone, spec.tz_resolver)
    now_local = local_datetime(now_ms, tz)
    hour, minute = parse_hm(spec.at)

    if spec.kind == "weekly":
        wanted = sorted({int(item) for item in spec.weekdays})
        if not wanted:
            raise ScheduleConfigError("weekly schedule requires at least one weekday")
        for offset in range(0, 8):
            candidate_day = (now_local + timedelta(days=offset)).date()
            # Python 的 isoweekday: 周一=1 .. 周日=7，与文档 weekdays 一致。
            if candidate_day.isoweekday() not in wanted:
                continue
            candidate = _at_hm(datetime.combine(candidate_day, datetime.min.time(), tzinfo=tz), hour, minute)
            if candidate > now_local:
                return int(candidate.timestamp() * 1000)
        raise ScheduleConfigError("weekly schedule could not find a next occurrence")

    # daily / window：都是「每天 at 时刻」，window 额外受时间窗口守卫裁剪。
    for offset in range(0, 3):
        candidate_day = (now_local + timedelta(days=offset)).date()
        candidate = _at_hm(datetime.combine(candidate_day, datetime.min.time(), tzinfo=tz), hour, minute)
        if candidate > now_local:
            return int(candidate.timestamp() * 1000)
    raise ScheduleConfigError("daily schedule could not find a next occurrence")


def in_quiet_hours(spec: "ScheduleSpec", now_ms: int) -> bool:
    """是否落在安静时段内。

    ``quiet_start`` / ``quiet_end`` 任一为空即视为未配置。
    支持跨午夜（如 23:00 → 07:00）：此时区间是 ``[start, 24:00) ∪ [00:00, end)``。
    """
    start_raw = spec.quiet_start or ""
    end_raw = spec.quiet_end or ""
    if not start_raw and not end_raw:
        return False
    if not start_raw or not end_raw:
        raise ScheduleConfigError("quiet hours need both quiet_start and quiet_end")
    start = parse_hm(start_raw)
    end = parse_hm(end_raw)
    start_minutes = start[0] * 60 + start[1]
    end_minutes = end[0] * 60 + end[1]
    tz = resolve_timezone(spec.timezone, spec.tz_resolver)
    now_local = local_datetime(now_ms, tz)
    now_minutes = now_local.hour * 60 + now_local.minute
    if start_minutes == end_minutes:
        # 起止相同：按「全天安静」处理，绝不因为区间长度为零就当作没配置放过。
        return True
    if start_minutes < end_minutes:
        return start_minutes <= now_minutes < end_minutes
    return now_minutes >= start_minutes or now_minutes < end_minutes


def local_day_key(spec: "ScheduleSpec", now_ms: int) -> str:
    """本地日期键（``YYYY-MM-DD``），用于每日计数的自然日切换。"""
    tz = resolve_timezone(spec.timezone, spec.tz_resolver)
    return local_datetime(now_ms, tz).date().isoformat()


# ---------------------------------------------------------------------------
# 固定 schema 校验
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleSpec:
    """一条已校验的调度配置。12 个固定字段，未知字段在构造前就被拒绝。"""

    schedule_id: str
    enabled: bool = False
    kind: str = "daily"
    timezone_name: str = "Asia/Shanghai"
    at: str = "08:30"
    weekdays: tuple[int, ...] = ()
    quiet_start: str = ""
    quiet_end: str = ""
    daily_limit: int = 3
    cooldown_seconds: int = 3600
    needs_live_device: bool = False
    next_run_ms: int = 0
    tz_resolver: Callable[[str], tzinfo] | None = None

    @property
    def timezone(self) -> str:
        return self.timezone_name

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any],
        *,
        tz_resolver: Callable[[str], tzinfo] | None = None,
    ) -> "ScheduleSpec":
        """唯一的严格入口：**未知字段拒绝**，缺省字段取固定默认值。"""
        if not isinstance(raw, Mapping):
            raise ScheduleConfigError("schedule spec must be a mapping")
        unknown = sorted(str(key) for key in raw if str(key) not in FIXED_FIELDS)
        if unknown:
            # 只回键名，不回显值。
            raise ScheduleConfigError("unknown schedule field present")
        merged: dict[str, Any] = dict(_DATA_DEFAULT_FIELDS)
        for key, value in raw.items():
            if key != "schedule_id":
                merged[str(key)] = value
        schedule_id = raw.get("schedule_id")
        if not isinstance(schedule_id, str) or not schedule_id.strip():
            raise ScheduleConfigError("schedule_id is required")
        spec = cls(
            schedule_id=schedule_id,
            enabled=merged["enabled"],
            kind=merged["kind"],
            timezone_name=merged["timezone"],
            at=merged["at"],
            weekdays=merged["weekdays"],
            quiet_start=merged["quiet_start"],
            quiet_end=merged["quiet_end"],
            daily_limit=merged["daily_limit"],
            cooldown_seconds=merged["cooldown_seconds"],
            needs_live_device=merged["needs_live_device"],
            next_run_ms=merged["next_run_ms"],
            tz_resolver=tz_resolver,
        )
        spec.validate(tz_resolver=tz_resolver)
        return spec

    def validate(self, *, tz_resolver: Callable[[str], tzinfo] | None = None) -> None:
        if not isinstance(self.enabled, bool):
            raise ScheduleConfigError("enabled must be a boolean")
        if self.kind not in SCHEDULE_KINDS:
            raise ScheduleConfigError("kind must be daily, weekly or window")
        if not isinstance(self.needs_live_device, bool):
            raise ScheduleConfigError("needs_live_device must be a boolean")
        if isinstance(self.daily_limit, bool) or not isinstance(self.daily_limit, int):
            raise ScheduleConfigError("daily_limit must be an integer")
        if not 0 <= self.daily_limit <= 1000:
            raise ScheduleConfigError("daily_limit is out of range")
        if isinstance(self.cooldown_seconds, bool) or not isinstance(self.cooldown_seconds, int):
            raise ScheduleConfigError("cooldown_seconds must be an integer")
        if not 0 <= self.cooldown_seconds <= 86_400:
            raise ScheduleConfigError("cooldown_seconds is out of range")
        if isinstance(self.next_run_ms, bool) or not isinstance(self.next_run_ms, int):
            raise ScheduleConfigError("next_run_ms must be an integer")
        if self.next_run_ms < 0:
            raise ScheduleConfigError("next_run_ms must be non-negative")
        parse_hm(self.at)
        if self.quiet_start or self.quiet_end:
            if not (self.quiet_start and self.quiet_end):
                raise ScheduleConfigError("quiet hours need both quiet_start and quiet_end")
            parse_hm(self.quiet_start)
            parse_hm(self.quiet_end)
        if self.kind == "weekly":
            if not self.weekdays:
                raise ScheduleConfigError("weekly schedule requires weekdays")
            for day in self.weekdays:
                if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 7:
                    raise ScheduleConfigError("weekdays must be integers 1..7")
        elif self.weekdays:
            # daily / window 不允许携带 weekdays，避免语义含糊。
            raise ScheduleConfigError("weekdays are only valid for weekly schedules")
        # 时区必须在注册时就解析成功，而不是等到触发时才发现配错了。
        resolve_timezone(self.timezone_name, tz_resolver or self.tz_resolver)

    def as_dict(self) -> dict[str, Any]:
        """回到 12 个固定字段的 JSON 形状（``timezone`` 用文档字段名）。"""
        return {
            "schedule_id": self.schedule_id,
            "enabled": self.enabled,
            "kind": self.kind,
            "timezone": self.timezone_name,
            "at": self.at,
            "weekdays": list(self.weekdays),
            "quiet_start": self.quiet_start,
            "quiet_end": self.quiet_end,
            "daily_limit": self.daily_limit,
            "cooldown_seconds": self.cooldown_seconds,
            "needs_live_device": self.needs_live_device,
            "next_run_ms": self.next_run_ms,
        }

    def with_next_run(self, next_run_ms: int) -> "ScheduleSpec":
        data = self.as_dict()
        data["next_run_ms"] = int(next_run_ms)
        return ScheduleSpec.from_mapping(data, tz_resolver=self.tz_resolver)


# ---------------------------------------------------------------------------
# 状态机（M10 开发文档「状态机」）
# ---------------------------------------------------------------------------

S_DISABLED = "DISABLED"
S_DUE = "DUE"
S_GUARD_CHECK = "GUARD_CHECK"
S_MODEL_DECISION = "MODEL_DECISION"
S_SILENT = "SILENT"
S_SEND = "SEND"
S_WAIT_DEVICE = "WAIT_DEVICE"
S_EXPIRED = "EXPIRED"
S_CANCELLED = "CANCELLED"
S_FAILED = "FAILED"

SCHEDULE_STATES: frozenset[str] = frozenset({
    S_DISABLED, S_DUE, S_GUARD_CHECK, S_MODEL_DECISION,
    S_SILENT, S_SEND, S_WAIT_DEVICE, S_EXPIRED, S_CANCELLED, S_FAILED,
})

#: 固定迁移表。DISABLED 不是起点之外的入口：启用后才进入 DUE。
STATE_TRANSITIONS: dict[str, frozenset[str]] = {
    S_DISABLED: frozenset({S_DUE}),
    S_DUE: frozenset({S_GUARD_CHECK, S_EXPIRED, S_CANCELLED, S_DISABLED}),
    S_GUARD_CHECK: frozenset({S_MODEL_DECISION, S_SILENT, S_WAIT_DEVICE, S_FAILED, S_CANCELLED}),
    S_MODEL_DECISION: frozenset({S_SILENT, S_SEND, S_WAIT_DEVICE, S_FAILED}),
    S_WAIT_DEVICE: frozenset({S_SEND, S_EXPIRED, S_CANCELLED}),
    # 终态：下次运行会从 DUE 重新开始，而不是从终态"复活"。
    S_SILENT: frozenset({S_DUE}),
    S_SEND: frozenset({S_DUE}),
    S_EXPIRED: frozenset({S_DUE}),
    S_CANCELLED: frozenset({S_DUE}),
    S_FAILED: frozenset({S_DUE}),
}


def can_transition(from_state: str, to_state: str) -> bool:
    return to_state in STATE_TRANSITIONS.get(from_state, frozenset())


# ---------------------------------------------------------------------------
# 存储：既有 API 保持，新增状态列与租约
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Schedule:
    """既有类型，签名保持不变（``test_core.py`` 依赖）。"""

    schedule_id: str
    enabled: bool
    spec: dict


@dataclass(frozen=True)
class ScheduleRow:
    """``schedules`` 表的一行完整视图（含调度状态）。"""

    schedule_id: str
    enabled: bool
    spec: dict
    state: str
    lease_until: int
    today_day: str
    today_count: int
    last_effect_key: str
    last_sent_at: int
    resume_attempts: int

    @property
    def spec_obj(self) -> ScheduleSpec:
        return ScheduleSpec.from_mapping({**self.spec, "schedule_id": self.schedule_id})


STATE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("state", "TEXT NOT NULL DEFAULT 'DISABLED'"),
    ("lease_until", "INTEGER NOT NULL DEFAULT 0"),
    ("today_day", "TEXT NOT NULL DEFAULT ''"),
    ("today_count", "INTEGER NOT NULL DEFAULT 0"),
    ("last_effect_key", "TEXT NOT NULL DEFAULT ''"),
    ("last_sent_at", "INTEGER NOT NULL DEFAULT 0"),
    ("resume_attempts", "INTEGER NOT NULL DEFAULT 0"),
)


class ScheduleStore:
    """``schedules`` 表访问层。

    既有 ``save`` / ``due`` 行为不变；新增状态列通过 ``ALTER TABLE ADD COLUMN``
    补齐，**不新建表**（M12 的固定八张表不能多出第九张）。
    """

    def __init__(self, db: sqlite3.Connection | None = None) -> None:
        self.db = db or sqlite3.connect(":memory:")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS schedules("
            "id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, spec_json TEXT NOT NULL, updated_at INTEGER NOT NULL)"
        )
        self._ensure_state_columns()
        self.db.commit()

    def _ensure_state_columns(self) -> None:
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(schedules)")}
        for name, ddl in STATE_COLUMNS:
            if name not in existing:
                self.db.execute(f"ALTER TABLE schedules ADD COLUMN {name} {ddl}")

    # -- 既有 API（一字未改） ---------------------------------------------
    def save(self, schedule: Schedule) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO schedules(id,enabled,spec_json,updated_at) VALUES(?,?,?,?)",
            (schedule.schedule_id, int(schedule.enabled), json.dumps(schedule.spec, ensure_ascii=False), int(time.time() * 1000)),
        )
        self.db.commit()

    def due(self, now_epoch_ms: int | None = None) -> list[Schedule]:
        now = int(time.time() * 1000) if now_epoch_ms is None else now_epoch_ms
        rows = self.db.execute("SELECT id,enabled,spec_json FROM schedules WHERE enabled=1").fetchall()
        result: list[Schedule] = []
        for schedule_id, enabled, raw in rows:
            spec = json.loads(raw)
            if int(spec.get("next_run_ms", 0)) <= now:
                result.append(Schedule(str(schedule_id), bool(enabled), spec))
        return result

    # -- 新增：严格注册 ---------------------------------------------------
    def register(self, spec: ScheduleSpec, now_ms: int, tz_resolver: Callable[[str], tzinfo] | None = None) -> ScheduleRow:
        """按固定 schema 注册一条调度。已存在则覆盖配置但**保留运行状态**。"""
        spec.validate(tz_resolver=tz_resolver)
        payload = spec.as_dict()
        next_run = int(payload["next_run_ms"]) or next_occurrence_ms(spec, now_ms)
        payload["next_run_ms"] = next_run
        initial_state = S_DUE if spec.enabled else S_DISABLED
        existing = self.row(spec.schedule_id)
        state = initial_state if existing is None else str(existing.state)
        self.db.execute(
            "INSERT OR REPLACE INTO schedules(id,enabled,spec_json,updated_at,state,lease_until,today_day,"
            "today_count,last_effect_key,last_sent_at,resume_attempts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                spec.schedule_id,
                int(spec.enabled),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                int(now_ms),
                state,
                int(existing.lease_until) if existing else 0,
                existing.today_day if existing else "",
                int(existing.today_count) if existing else 0,
                existing.last_effect_key if existing else "",
                int(existing.last_sent_at) if existing else 0,
                int(existing.resume_attempts) if existing else 0,
            ),
        )
        self.db.commit()
        row = self.row(spec.schedule_id)
        if row is None:
            raise ScheduleConfigError("schedule could not be registered")
        return row

    # -- 新增：读取 -------------------------------------------------------
    def row(self, schedule_id: str) -> ScheduleRow | None:
        cursor = self.db.execute(
            "SELECT id,enabled,spec_json,state,lease_until,today_day,today_count,last_effect_key,"
            "last_sent_at,resume_attempts FROM schedules WHERE id=?",
            (str(schedule_id),),
        )
        record = cursor.fetchone()
        if record is None:
            return None
        return ScheduleRow(
            schedule_id=str(record[0]),
            enabled=bool(record[1]),
            spec=json.loads(record[2]),
            state=str(record[3]),
            lease_until=int(record[4]),
            today_day=str(record[5]),
            today_count=int(record[6]),
            last_effect_key=str(record[7]),
            last_sent_at=int(record[8]),
            resume_attempts=int(record[9]),
        )

    def rows_in_states(self, states: frozenset[str] | set[str]) -> list[ScheduleRow]:
        """按调度状态扫描（worker 重启恢复 DUE/WAIT_DEVICE 的入口）。"""
        wanted = sorted({str(item) for item in states})
        if not wanted:
            return []
        placeholders = ",".join("?" for _ in wanted)
        cursor = self.db.execute(
            "SELECT id,enabled,spec_json,state,lease_until,today_day,today_count,last_effect_key,"
            f"last_sent_at,resume_attempts FROM schedules WHERE state IN ({placeholders}) "
            "ORDER BY updated_at ASC, id ASC",
            tuple(wanted),
        )
        rows: list[ScheduleRow] = []
        for record in cursor.fetchall():
            rows.append(
                ScheduleRow(
                    schedule_id=str(record[0]),
                    enabled=bool(record[1]),
                    spec=json.loads(record[2]),
                    state=str(record[3]),
                    lease_until=int(record[4]),
                    today_day=str(record[5]),
                    today_count=int(record[6]),
                    last_effect_key=str(record[7]),
                    last_sent_at=int(record[8]),
                    resume_attempts=int(record[9]),
                )
            )
        return rows

    # -- 新增：状态 -------------------------------------------------------
    def set_state(self, schedule_id: str, state: str, now_ms: int) -> None:
        if state not in SCHEDULE_STATES:
            raise ScheduleConfigError(f"unknown schedule state: {state!r}")
        self.db.execute(
            "UPDATE schedules SET state=?, updated_at=? WHERE id=?",
            (state, int(now_ms), str(schedule_id)),
        )
        self.db.commit()

    def transition(self, schedule_id: str, state: str, now_ms: int) -> ScheduleRow:
        """校验迁移合法性后再落库；非法迁移直接拒绝。"""
        current = self.row(schedule_id)
        if current is None:
            raise ScheduleConfigError("unknown schedule")
        if state != current.state and not can_transition(current.state, state):
            raise ScheduleConfigError(f"illegal schedule transition: {current.state} -> {state}")
        self.set_state(schedule_id, state, now_ms)
        updated = self.row(schedule_id)
        if updated is None:
            raise ScheduleConfigError("schedule disappeared during transition")
        return updated

    # -- 新增：租约（数据库级，只延长不回退） ------------------------------
    def acquire_lease(self, schedule_id: str, now_ms: int, lease_ms: int = SCHEDULE_LEASE_MS) -> bool:
        """抢租约。**唯一**的排他手段，不使用任何进程内锁。

        条件更新 ``WHERE lease_until <= now``：只有租约已过期（或从未获得）才能抢到。
        ``SET lease_until = MAX(lease_until, now + lease_ms)`` 保证只延长、不回退，
        因此两个进程同时抢时只有一个能成功，另一个的 ``rowcount`` 为 0。
        """
        duration = max(1, int(lease_ms))
        target = int(now_ms) + duration
        cursor = self.db.execute(
            "UPDATE schedules SET lease_until = MAX(lease_until, ?), updated_at = ? "
            "WHERE id = ? AND lease_until <= ?",
            (target, int(now_ms), str(schedule_id), int(now_ms)),
        )
        self.db.commit()
        return int(cursor.rowcount) == 1

    def extend_lease(self, schedule_id: str, now_ms: int, lease_ms: int = SCHEDULE_LEASE_MS) -> int:
        """延长租约，返回值是延长后的 ``lease_until``（绝不小于原值）。"""
        target = int(now_ms) + max(1, int(lease_ms))
        self.db.execute(
            "UPDATE schedules SET lease_until = MAX(lease_until, ?), updated_at = ? WHERE id=?",
            (target, int(now_ms), str(schedule_id)),
        )
        self.db.commit()
        row = self.row(schedule_id)
        return 0 if row is None else row.lease_until

    def release_lease(self, schedule_id: str, now_ms: int) -> None:
        """主动释放：把租约设为已过期（``now``），不删除行。"""
        self.db.execute(
            "UPDATE schedules SET lease_until=?, updated_at=? WHERE id=?",
            (int(now_ms), int(now_ms), str(schedule_id)),
        )
        self.db.commit()

    # -- 新增：原子更新（next_run_ms + 今日计数 + last_effect_key） --------
    def record_send(
        self,
        schedule_id: str,
        *,
        spec: ScheduleSpec | Mapping[str, Any],
        next_run_ms: int,
        effect_key: str,
        now_ms: int,
        local_day: str,
    ) -> ScheduleRow:
        """**一次事务**内同时写入 next_run_ms、今日计数与 last_effect_key。

        「原子」的具体落实：
        - 三条字段由**同一条 UPDATE 语句**写出（next_run_ms 内嵌在 spec_json 里）；
        - 该 UPDATE 与状态迁移、today_count 递增在**同一个事务**里提交，
          中途失败不会留下「已计数但没排下一次」或「已排下次但没记 effect」的半截状态。
        """
        payload = dict(spec.as_dict() if isinstance(spec, ScheduleSpec) else dict(spec))
        payload["next_run_ms"] = int(next_run_ms)
        row = self.row(schedule_id)
        if row is None:
            raise ScheduleConfigError("unknown schedule")
        counted = int(row.today_count) if row.today_day == local_day else 0
        sql = (
            "UPDATE schedules SET "
            "spec_json=?, "
            "today_day=?, "
            "today_count=?, "
            "last_effect_key=?, "
            "last_sent_at=?, "
            "state=?, "
            "updated_at=?, "
            "resume_attempts=0 "
            "WHERE id=?"
        )
        params = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            str(local_day),
            counted + 1,
            str(effect_key),
            int(now_ms),
            S_DUE,
            int(now_ms),
            str(schedule_id),
        )
        connection = self.db
        owns_transaction = not connection.in_transaction
        if owns_transaction:
            connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(sql, params)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        updated = self.row(schedule_id)
        if updated is None:
            raise ScheduleConfigError("schedule disappeared during atomic update")
        return updated

    def record_wait(self, schedule_id: str, now_ms: int) -> ScheduleRow:
        """进入 WAIT_DEVICE：**只加一次** resume_attempts 上限保护，不重复建任务。"""
        row = self.row(schedule_id)
        if row is None:
            raise ScheduleConfigError("unknown schedule")
        self.db.execute(
            "UPDATE schedules SET state=?, resume_attempts=resume_attempts+1, updated_at=? WHERE id=?",
            (S_WAIT_DEVICE, int(now_ms), str(schedule_id)),
        )
        self.db.commit()
        updated = self.row(schedule_id)
        if updated is None:
            raise ScheduleConfigError("schedule disappeared during wait record")
        return updated

    def reschedule(self, schedule_id: str, next_run_ms: int, now_ms: int, *, state: str = S_DUE) -> ScheduleRow:
        """只重排下一次触发（不增计数、不记 effect）。"""
        row = self.row(schedule_id)
        if row is None:
            raise ScheduleConfigError("unknown schedule")
        payload = dict(row.spec)
        payload["next_run_ms"] = int(next_run_ms)
        self.db.execute(
            "UPDATE schedules SET spec_json=?, state=?, updated_at=? WHERE id=?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), state, int(now_ms), str(schedule_id)),
        )
        self.db.commit()
        updated = self.row(schedule_id)
        if updated is None:
            raise ScheduleConfigError("schedule disappeared during reschedule")
        return updated
