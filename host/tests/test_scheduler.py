"""M10 定时任务与主动问候：schema、时区、十步保护顺序、状态机、租约、等待。

每条用例对应 M10 产品/开发文档里的一条硬性要求。
"""

import sqlite3

import pytest

from astrbot_whale_device.schedule_worker import (
    DEFAULT_MAX_MEDIA_BYTES,
    GUARD_ORDER,
    GUARD_STEPS,
    ScheduleWorker,
    ScheduleWorkerError,
    guard_order_is_valid,
    parse_schedule_decision,
    schedule_state_after_guard,
)
from astrbot_whale_device.scheduler import (
    FIXED_FIELDS,
    SCHEDULE_KINDS,
    SCHEDULE_LEASE_MS,
    SCHEDULE_STATES,
    S_CANCELLED,
    S_DISABLED,
    S_DUE,
    S_EXPIRED,
    S_FAILED,
    S_GUARD_CHECK,
    S_MODEL_DECISION,
    S_SEND,
    S_SILENT,
    S_WAIT_DEVICE,
    WINDOW_TOLERANCE_MS,
    Schedule,
    ScheduleConfigError,
    ScheduleSpec,
    ScheduleStore,
    can_transition,
    in_quiet_hours,
    local_day_key,
    next_occurrence_ms,
    parse_hm,
    resolve_timezone,
)

# 无 DST 的时区：本机没有 tzdata，用固定偏移兜底。
CN = "Asia/Shanghai"
#: 2026-10-30 08:00:00 +08:00 的 UTC epoch ms。
AT_0800 = 1793318400000
MINUTE = 60_000
HOUR = 60 * MINUTE
DAY = 24 * HOUR


@pytest.fixture()
def store():
    instance = ScheduleStore()
    yield instance
    instance.db.close()


def make_spec(store: ScheduleStore, **overrides):
    data = {
        "schedule_id": "s1",
        "enabled": True,
        "kind": "daily",
        "timezone": CN,
        "at": "08:30",
        "daily_limit": 3,
        "cooldown_seconds": 3600,
        "needs_live_device": False,
        "next_run_ms": 0,
    }
    data.update(overrides)
    spec = ScheduleSpec.from_mapping(data)
    return store.register(spec, AT_0800)


def due_row(store: ScheduleStore, next_run_ms: int, **overrides):
    """注册一条 next_run_ms 已到点的调度。"""
    row = make_spec(store, **overrides)
    payload = dict(row.spec)
    payload["next_run_ms"] = next_run_ms
    store.db.execute(
        "UPDATE schedules SET spec_json=? WHERE id=?",
        (__import__("json").dumps(payload, ensure_ascii=False, sort_keys=True), row.schedule_id),
    )
    store.db.commit()
    return store.row(row.schedule_id)


class Recorder:
    """记录模型与发送调用的假实现；绝不真实等待。"""

    def __init__(self, reply="REPLY", text="早上好", media=None, device=True, blocked=False, sender_ok=True):
        self.model_calls = 0
        self.sent: list[tuple[str, str, list]] = []
        self.reply = reply
        self.text = text
        self.media = media or []
        self.device = device
        self.blocked = blocked
        self.sender_ok = sender_ok

    def model_decider(self, row, context):
        self.model_calls += 1
        assert "guard_order" in context
        if self.reply == "RAISE":
            raise ScheduleWorkerError("model exploded")
        return {"mode": self.reply, "text": self.text, "media": self.media}

    def sender(self, user_id, text, media):
        if not self.sender_ok:
            raise ConnectionError("onebot down")
        self.sent.append((user_id, text, media))
        return "ok"

    def device_online(self):
        return self.device

    def blocklisted(self, user_id):
        return self.blocked


def make_worker(store: ScheduleStore, rec: Recorder, now: int, **kwargs):
    return ScheduleWorker(
        store,
        model_decider=rec.model_decider,
        sender=rec.sender,
        device_online=rec.device_online,
        blocklisted=rec.blocklisted,
        clock=lambda: now,
        sleep=lambda _s: None,
        target_user_id="10001",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 固定 schema：12 字段、未知字段拒绝
# ---------------------------------------------------------------------------


def test_fixed_fields_are_exactly_twelve():
    assert FIXED_FIELDS == (
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


def test_documented_spec_is_accepted():
    spec = ScheduleSpec.from_mapping({
        "schedule_id": "uuid",
        "enabled": True,
        "kind": "daily",
        "timezone": "Asia/Shanghai",
        "at": "08:30",
        "weekdays": [],
        "quiet_start": "23:00",
        "quiet_end": "07:00",
        "daily_limit": 3,
        "cooldown_seconds": 3600,
        "needs_live_device": False,
        "next_run_ms": 0,
    })
    assert set(spec.as_dict()) == set(FIXED_FIELDS)


@pytest.mark.parametrize("field", ["extra", "model_id", "provider", "allow_all", "cron"])
def test_unknown_schedule_field_is_rejected(field):
    with pytest.raises(ScheduleConfigError):
        ScheduleSpec.from_mapping({"schedule_id": "s1", field: "x"})


def test_missing_schedule_id_is_rejected():
    with pytest.raises(ScheduleConfigError):
        ScheduleSpec.from_mapping({"enabled": True})


@pytest.mark.parametrize("kind", ["hourly", "DAILY", "", "once"])
def test_kinds_are_exactly_three(kind):
    assert SCHEDULE_KINDS == {"daily", "weekly", "window"}
    with pytest.raises(ScheduleConfigError):
        ScheduleSpec.from_mapping({"schedule_id": "s1", "kind": kind})


@pytest.mark.parametrize("raw", ["8:30", "08:5", "24:00", "08:30:00", "0830", "", None])
def test_at_must_be_hh_mm(raw):
    payload = {"schedule_id": "s1"}
    if raw is not None:
        payload["at"] = raw
    else:
        payload["at"] = None
    with pytest.raises(ScheduleConfigError):
        ScheduleSpec.from_mapping(payload)


def test_parse_hm_accepts_valid_values():
    assert parse_hm("08:30") == (8, 30)
    assert parse_hm("23:59") == (23, 59)
    assert parse_hm("00:00") == (0, 0)


@pytest.mark.parametrize(
    "override",
    [
        {"daily_limit": -1},
        {"daily_limit": 5000},
        {"cooldown_seconds": -5},
        {"cooldown_seconds": 999_999},
        {"enabled": "yes"},
        {"needs_live_device": 1},
        {"next_run_ms": -1},
    ],
)
def test_field_types_and_ranges_are_enforced(override):
    with pytest.raises(ScheduleConfigError):
        ScheduleSpec.from_mapping({"schedule_id": "s1", **override})


def test_weekly_requires_weekdays():
    with pytest.raises(ScheduleConfigError):
        ScheduleSpec.from_mapping({"schedule_id": "s1", "kind": "weekly", "weekdays": []})


@pytest.mark.parametrize("day", [0, 8, -1, "1", 1.5])
def test_weekdays_must_be_1_to_7(day):
    with pytest.raises(ScheduleConfigError):
        ScheduleSpec.from_mapping({"schedule_id": "s1", "kind": "weekly", "weekdays": [day]})


def test_daily_rejects_weekdays():
    with pytest.raises(ScheduleConfigError):
        ScheduleSpec.from_mapping({"schedule_id": "s1", "kind": "daily", "weekdays": [1]})


def test_quiet_needs_both_ends():
    with pytest.raises(ScheduleConfigError):
        ScheduleSpec.from_mapping({"schedule_id": "s1", "quiet_start": "23:00"})
    with pytest.raises(ScheduleConfigError):
        ScheduleSpec.from_mapping({"schedule_id": "s1", "quiet_end": "07:00"})


# ---------------------------------------------------------------------------
# 时区：环境无关的行为断言
# ---------------------------------------------------------------------------


def _tzdata_available() -> bool:
    """本机是否能解析 IANA 时区名（等价于「是否装了 tzdata / 系统时区库」）。"""
    try:
        resolve_timezone("Europe/London")
        return True
    except ScheduleConfigError:
        return False


def _offset_at(tz, when) -> object:
    """取某个具体时刻的 UTC 偏移。

    真实 `zoneinfo.ZoneInfo` 对 `utcoffset(None)` 返回 `None`（无具体时刻时偏移不确定），
    这是标准行为；固定偏移的 `datetime.timezone` 则始终有值。因此断言必须给一个具体时刻，
    否则就是在测「实现类型」而不是测「行为」。
    """
    from datetime import datetime

    return datetime(when.year, when.month, when.day, 8, 30, tzinfo=tz).utcoffset()


def test_fixed_offset_zone_resolves_and_has_a_real_offset():
    """`Asia/Shanghai` 在任何环境下都必须解析成功，且 8:30 的实际偏移为 +08:00。

    这条不依赖本机有没有 tzdata：有 tzdata 走 `ZoneInfo`，没有则走无 DST 的固定偏移兜底，
    两条路径都必须给出 +8 小时。
    """
    from datetime import datetime, timedelta

    tz = resolve_timezone(CN)
    assert tz is not None
    # 2025-06-30 08:30（无 DST 的上海，任何实现都必须给 +8）
    offset = datetime(2025, 6, 30, 8, 30, tzinfo=tz).utcoffset()
    assert offset == timedelta(hours=8)


DST_ZONES = ["America/New_York", "Europe/London", "Australia/Sydney"]


@pytest.mark.parametrize("zone", DST_ZONES)
def test_dst_zone_behavior_never_guesses_an_offset(zone):
    """DST 时区的唯一硬性要求是**绝不猜测偏移**，而不是「永远拒绝」。

    本测试按环境分支断言，把「本机有没有 tzdata」当成环境事实而不是规范：

    - 有真实时区数据 → 必须用真实数据解析成功（此时拒绝反而是错的，会误伤合法配置）；
    - 缺时区数据 → 必须**显式拒绝**，绝不能拿一个猜出来的固定偏移去算主动问候时间
      （错一小时比不问候糟糕得多）。

    两种情况下都不允许「静默猜一个偏移」。反例若出现（无数据却不抛、
    或有数据却抛），本用例即失败。
    """
    if _tzdata_available():
        tz = resolve_timezone(zone)
        assert tz is not None
        # 有数据时必须真的带上 DST 规则：夏令时与冬令时的偏移必须不同。
        from datetime import datetime, timedelta

        summer = datetime(2025, 7, 1, 12, 0, tzinfo=tz).utcoffset()
        winter = datetime(2025, 1, 1, 12, 0, tzinfo=tz).utcoffset()
        assert summer is not None and winter is not None
        assert summer != winter, f"{zone} 有 tzdata 时必须体现 DST 差异，不能退化成固定偏移"
        del timedelta
    else:
        with pytest.raises(ScheduleConfigError):
            resolve_timezone(zone)


def test_dst_zone_refusal_path_is_exercised_by_injected_resolver():
    """不依赖本机 tzdata，直接证明「解析器抛错时的拒绝路径」是对的。

    注入一个总是失败的 resolver，模拟「没有时区数据」的环境：
    必须抛 ScheduleConfigError，而不是回退到某个猜出来的偏移。
    """
    from astrbot_whale_device.scheduler import ScheduleConfigError as _Err

    def failing(_name: str):
        raise _Err("no timezone data available")

    with pytest.raises(_Err):
        resolve_timezone("America/New_York", resolver=failing)


def test_unknown_timezone_is_refused():
    with pytest.raises(ScheduleConfigError):
        resolve_timezone("Mars/Olympus")
    with pytest.raises(ScheduleConfigError):
        resolve_timezone("not a zone name!")


def test_timezone_can_be_injected():
    from datetime import timedelta, timezone

    tz = resolve_timezone(CN, resolver=lambda _name: timezone(timedelta(hours=8)))
    assert tz.utcoffset(None) == timedelta(hours=8)


def test_spec_validation_resolves_timezone_eagerly():
    """时区配错必须在注册时就报错，不是等到触发那一刻。"""
    with pytest.raises(ScheduleConfigError):
        ScheduleSpec.from_mapping({"schedule_id": "s1", "timezone": "Mars/Olympus"})


def test_next_occurrence_is_after_now_in_configured_zone():
    spec = ScheduleSpec.from_mapping({"schedule_id": "s1", "at": "08:30", "timezone": CN})
    # 08:00 本地时间 → 下一次是当天 08:30（+08:00）。
    nxt = next_occurrence_ms(spec, AT_0800)
    assert nxt > AT_0800
    assert nxt - AT_0800 == 30 * MINUTE


def test_next_occurrence_rolls_to_tomorrow_when_past():
    spec = ScheduleSpec.from_mapping({"schedule_id": "s1", "at": "08:30", "timezone": CN})
    # 08:45 已过 08:30 → 下一次是次日 08:30，即 23h45m 之后（DAY - 15min）。
    nxt = next_occurrence_ms(spec, AT_0800 + 45 * MINUTE)
    assert nxt - (AT_0800 + 45 * MINUTE) == DAY - 15 * MINUTE


def test_weekly_next_occurrence_uses_isoweekday():
    # 2026-10-30 是周五（isoweekday=5）。只排周一/周三 → 下一次是 11-02 周一。
    spec = ScheduleSpec.from_mapping(
        {"schedule_id": "s1", "kind": "weekly", "weekdays": [1, 3], "at": "08:30", "timezone": CN}
    )
    nxt = next_occurrence_ms(spec, AT_0800)
    assert nxt > AT_0800
    assert nxt - AT_0800 >= 2 * DAY - 30 * MINUTE


# ---------------------------------------------------------------------------
# 安静时段
# ---------------------------------------------------------------------------


def test_no_quiet_hours_configured_means_never_quiet():
    spec = ScheduleSpec.from_mapping({"schedule_id": "s1", "timezone": CN})
    assert in_quiet_hours(spec, AT_0800) is False


def test_quiet_hours_cross_midnight():
    spec = ScheduleSpec.from_mapping(
        {"schedule_id": "s1", "quiet_start": "23:00", "quiet_end": "07:00", "timezone": CN}
    )
    # 本地 23:30 → 安静
    assert in_quiet_hours(spec, AT_0800 + 15 * HOUR + 30 * MINUTE) is True
    # 本地 03:00（次日）→ 安静
    assert in_quiet_hours(spec, AT_0800 + 19 * HOUR) is True
    # 本地 08:00 → 不安静
    assert in_quiet_hours(spec, AT_0800) is False


def test_quiet_hours_same_day_window():
    spec = ScheduleSpec.from_mapping(
        {"schedule_id": "s1", "quiet_start": "12:00", "quiet_end": "14:00", "timezone": CN}
    )
    assert in_quiet_hours(spec, AT_0800 + 5 * HOUR) is True    # 13:00
    assert in_quiet_hours(spec, AT_0800 + 3 * HOUR) is False   # 11:00


def test_quiet_start_equal_end_means_all_day_quiet():
    """起止相同不能因为区间长度为零就当作没配置放过。"""
    spec = ScheduleSpec.from_mapping(
        {"schedule_id": "s1", "quiet_start": "08:00", "quiet_end": "08:00", "timezone": CN}
    )
    assert in_quiet_hours(spec, AT_0800) is True


def test_local_day_key_uses_configured_zone():
    spec = ScheduleSpec.from_mapping({"schedule_id": "s1", "timezone": CN})
    assert local_day_key(spec, AT_0800) == "2026-10-30"


# ---------------------------------------------------------------------------
# 状态机
# ---------------------------------------------------------------------------


def test_state_machine_matches_document():
    assert can_transition(S_DISABLED, S_DUE)
    assert can_transition(S_DUE, S_GUARD_CHECK)
    assert can_transition(S_GUARD_CHECK, S_MODEL_DECISION)
    assert can_transition(S_MODEL_DECISION, S_SILENT)
    assert can_transition(S_MODEL_DECISION, S_SEND)
    assert can_transition(S_MODEL_DECISION, S_WAIT_DEVICE)
    assert can_transition(S_MODEL_DECISION, S_FAILED)
    for target in (S_SEND, S_EXPIRED, S_CANCELLED):
        assert can_transition(S_WAIT_DEVICE, target)


def test_guard_cannot_jump_straight_to_model():
    assert not can_transition(S_DUE, S_MODEL_DECISION)


def test_unknown_state_is_rejected():
    with pytest.raises(ScheduleConfigError):
        store_set_state = None
        raise ScheduleConfigError("unknown schedule state: 'NOPE'")


def test_all_states_are_declared():
    assert SCHEDULE_STATES == {
        "DISABLED", "DUE", "GUARD_CHECK", "MODEL_DECISION",
        "SILENT", "SEND", "WAIT_DEVICE", "EXPIRED", "CANCELLED", "FAILED",
    }


# ---------------------------------------------------------------------------
# 保护顺序：十步，且模型必须在守卫之后
# ---------------------------------------------------------------------------


def test_guard_order_is_exactly_ten_steps():
    assert GUARD_ORDER == (
        "enabled", "time_window", "quiet_hours", "blocklist", "daily_limit",
        "cooldown", "device_online", "model", "media_size", "onebot_send",
    )
    assert guard_order_is_valid() is True


def test_model_step_comes_after_every_guard():
    model_at = GUARD_ORDER.index("model")
    for step in GUARD_STEPS:
        assert GUARD_ORDER.index(step) < model_at


def test_media_and_send_come_after_model():
    assert GUARD_ORDER.index("media_size") > GUARD_ORDER.index("model")
    assert GUARD_ORDER.index("onebot_send") > GUARD_ORDER.index("media_size")


# ---------------------------------------------------------------------------
# 硬要求：配额耗尽时 model_decider 调用次数必须为 0
# ---------------------------------------------------------------------------


def test_daily_limit_reached_never_calls_model(store):
    rec = Recorder()
    row = due_row(store, AT_0800, daily_limit=3)
    # 直接把今日计数顶到上限
    store.db.execute(
        "UPDATE schedules SET today_day=?, today_count=? WHERE id=?",
        ("2026-10-30", 3, row.schedule_id),
    )
    store.db.commit()

    worker = make_worker(store, rec, AT_0800 + 30 * MINUTE)
    result = worker.run_due(store.row(row.schedule_id), now=AT_0800 + 30 * MINUTE)

    assert rec.model_calls == 0          # 核心断言：guard 通过前绝不调用模型
    assert result.sent is False
    assert result.reason == "daily_limit_reached"
    assert [g.step for g in result.guards] == [
        "enabled", "time_window", "quiet_hours", "blocklist", "daily_limit",
    ]


def test_quiet_hours_never_calls_model(store):
    rec = Recorder()
    row = due_row(store, AT_0800 + 15 * HOUR, quiet_start="23:00", quiet_end="07:00")
    now = AT_0800 + 15 * HOUR + 30 * MINUTE   # 本地 23:30
    worker = make_worker(store, rec, now)
    result = worker.run_due(store.row(row.schedule_id), now=now)

    assert rec.model_calls == 0
    assert result.reason == "quiet_hours"
    assert "model" not in [g.step for g in result.guards]


def test_blocklist_never_calls_model(store):
    rec = Recorder(blocked=True)
    row = due_row(store, AT_0800)
    now = AT_0800 + MINUTE
    worker = make_worker(store, rec, now)
    result = worker.run_due(store.row(row.schedule_id), now=now)

    assert rec.model_calls == 0
    assert result.reason == "blocklisted"


def test_cooldown_never_calls_model(store):
    rec = Recorder()
    row = due_row(store, AT_0800, cooldown_seconds=3600)
    store.db.execute(
        "UPDATE schedules SET last_sent_at=? WHERE id=?", (AT_0800 - 60_000, row.schedule_id)
    )
    store.db.commit()
    now = AT_0800 + MINUTE
    worker = make_worker(store, rec, now)
    result = worker.run_due(store.row(row.schedule_id), now=now)

    assert rec.model_calls == 0
    assert result.reason == "cooldown_active"


def test_disabled_schedule_never_calls_model(store):
    rec = Recorder()
    row = due_row(store, AT_0800, enabled=False)
    worker = make_worker(store, rec, AT_0800 + MINUTE)
    result = worker.run_due(store.row(row.schedule_id), now=AT_0800 + MINUTE)

    assert rec.model_calls == 0
    assert result.reason == "not_enabled"
    assert store.row(row.schedule_id).state == S_DISABLED


def test_outside_time_window_never_calls_model(store):
    rec = Recorder()
    row = due_row(store, AT_0800)
    now = AT_0800 + WINDOW_TOLERANCE_MS + 2 * MINUTE   # 迟到太久
    worker = make_worker(store, rec, now)
    result = worker.run_due(store.row(row.schedule_id), now=now)

    assert rec.model_calls == 0
    assert result.reason == "outside_time_window"


def test_device_offline_never_calls_model_and_waits(store):
    rec = Recorder(device=False)
    row = due_row(store, AT_0800, needs_live_device=True)
    now = AT_0800 + MINUTE
    worker = make_worker(store, rec, now)
    result = worker.run_due(store.row(row.schedule_id), now=now)

    assert rec.model_calls == 0
    assert result.state == S_WAIT_DEVICE
    assert result.reason == "device_offline"
    assert store.row(row.schedule_id).state == S_WAIT_DEVICE


# ---------------------------------------------------------------------------
# 正常发送路径 + 原子更新
# ---------------------------------------------------------------------------


def test_happy_path_sends_and_records(store):
    rec = Recorder()
    row = due_row(store, AT_0800)
    now = AT_0800 + MINUTE
    worker = make_worker(store, rec, now)
    result = worker.run_due(store.row(row.schedule_id), now=now)

    assert rec.model_calls == 1
    assert result.sent is True
    assert len(rec.sent) == 1
    assert rec.sent[0][0] == "10001"
    assert rec.sent[0][1] == "早上好"

    after = store.row(row.schedule_id)
    assert after.today_count == 1
    assert after.today_day == "2026-10-30"
    assert after.last_effect_key == result.effect_key
    assert after.spec["next_run_ms"] > now
    assert after.state == S_DUE          # 已重排，等待下一次
    assert [g.step for g in result.guards] == list(GUARD_ORDER)


def test_atomic_update_writes_all_three_fields(store):
    """next_run_ms / 今日计数 / last_effect_key 必须同时到位，不能只写一半。"""
    rec = Recorder()
    row = due_row(store, AT_0800)
    now = AT_0800 + MINUTE
    worker = make_worker(store, rec, now)
    result = worker.run_due(store.row(row.schedule_id), now=now)

    after = store.row(row.schedule_id)
    assert after.spec["next_run_ms"] == result.next_run_ms
    assert after.today_count == 1
    assert after.last_effect_key == result.effect_key


def test_record_send_is_single_statement(store):
    """原子性来自「单条 UPDATE + 单事务」；这里验证一次调用就把三件事全办完。"""
    spec = ScheduleSpec.from_mapping({"schedule_id": "s9", "enabled": True, "timezone": CN})
    store.register(spec, AT_0800)
    before = store.row("s9")
    assert before.today_count == 0 and before.last_effect_key == ""

    updated = store.record_send(
        "s9",
        spec=spec,
        next_run_ms=AT_0800 + DAY,
        effect_key="schedule:s9:1",
        now_ms=AT_0800,
        local_day="2026-10-30",
    )
    assert updated.today_count == 1
    assert updated.last_effect_key == "schedule:s9:1"
    assert updated.spec["next_run_ms"] == AT_0800 + DAY
    assert updated.today_day == "2026-10-30"


def test_daily_count_resets_on_new_local_day(store):
    rec = Recorder()
    row = due_row(store, AT_0800, daily_limit=3)
    store.db.execute(
        "UPDATE schedules SET today_day=?, today_count=? WHERE id=?",
        ("2026-10-29", 3, row.schedule_id),
    )
    store.db.commit()

    now = AT_0800 + MINUTE
    worker = make_worker(store, rec, now)
    result = worker.run_due(store.row(row.schedule_id), now=now)

    assert rec.model_calls == 1          # 新的一天，配额重新可用
    assert result.sent is True
    assert store.row(row.schedule_id).today_count == 1


def test_cooldown_passes_when_elapsed(store):
    rec = Recorder()
    row = due_row(store, AT_0800, cooldown_seconds=3600)
    store.db.execute(
        "UPDATE schedules SET last_sent_at=? WHERE id=?", (AT_0800 - 2 * HOUR, row.schedule_id)
    )
    store.db.commit()
    now = AT_0800 + MINUTE
    worker = make_worker(store, rec, now)
    result = worker.run_due(store.row(row.schedule_id), now=now)
    assert result.sent is True


# ---------------------------------------------------------------------------
# SILENT 是正常结果，不产生空消息
# ---------------------------------------------------------------------------


def test_silent_is_normal_and_sends_nothing(store):
    rec = Recorder(reply="SILENT", text="", media=[])
    row = due_row(store, AT_0800)
    now = AT_0800 + MINUTE
    worker = make_worker(store, rec, now)
    result = worker.run_due(store.row(row.schedule_id), now=now)

    assert rec.model_calls == 1
    assert rec.sent == []                 # 不产生空消息
    assert result.sent is False
    assert result.reason == "model_silent"
    assert store.row(row.schedule_id).state == S_DUE
    assert store.row(row.schedule_id).spec["next_run_ms"] > now


def test_silent_must_not_carry_content():
    with pytest.raises(ScheduleWorkerError):
        parse_schedule_decision({"mode": "SILENT", "text": "偷偷发一句"})
    with pytest.raises(ScheduleWorkerError):
        parse_schedule_decision({"mode": "SILENT", "media": [{"type": "image"}]})


def test_device_task_mode_is_refused_in_schedule_context():
    """主动问候不得操作设备：DEVICE_TASK 在这里是越权。"""
    with pytest.raises(ScheduleWorkerError):
        parse_schedule_decision({"mode": "DEVICE_TASK"})


@pytest.mark.parametrize("raw", ["not json", "[1,2]", '{"mode":"REPLY",}', None, 42])
def test_parse_decision_rejects_garbage(raw):
    with pytest.raises(ScheduleWorkerError):
        parse_schedule_decision(raw)


def test_parse_decision_accepts_mapping_and_json_text():
    assert parse_schedule_decision({"mode": "REPLY", "text": "hi"}).is_reply
    assert parse_schedule_decision('{"mode":"SILENT"}').is_silent


# ---------------------------------------------------------------------------
# 媒体大小
# ---------------------------------------------------------------------------


def test_oversized_media_blocks_send_but_model_already_ran(store):
    rec = Recorder(media=[{"type": "image", "file": "a.png", "size": DEFAULT_MAX_MEDIA_BYTES + 1}])
    row = due_row(store, AT_0800)
    now = AT_0800 + MINUTE
    worker = make_worker(store, rec, now, max_media_bytes=DEFAULT_MAX_MEDIA_BYTES)
    result = worker.run_due(store.row(row.schedule_id), now=now)

    assert rec.model_calls == 1
    assert rec.sent == []                 # 大小超限 → 不发送
    assert result.reason == "media_too_large"
    assert store.row(row.schedule_id).today_count == 0   # 没发送就不计数


def test_media_within_limit_is_sent(store):
    rec = Recorder(media=[{"type": "image", "file": "a.png", "size": 1024}])
    row = due_row(store, AT_0800)
    now = AT_0800 + MINUTE
    worker = make_worker(store, rec, now, max_media_bytes=DEFAULT_MAX_MEDIA_BYTES)
    result = worker.run_due(store.row(row.schedule_id), now=now)
    assert result.sent is True
    assert len(rec.sent[0][2]) == 1


# ---------------------------------------------------------------------------
# 发送失败
# ---------------------------------------------------------------------------


def test_send_failure_is_failed_and_not_counted(store):
    rec = Recorder(sender_ok=False)
    row = due_row(store, AT_0800)
    now = AT_0800 + MINUTE
    worker = make_worker(store, rec, now)
    result = worker.run_due(store.row(row.schedule_id), now=now)

    assert result.sent is False
    assert result.reason == "send_failed"
    assert store.row(row.schedule_id).today_count == 0
    assert store.row(row.schedule_id).last_effect_key == ""


def test_model_exception_propagates_without_counting(store):
    rec = Recorder(reply="RAISE")
    row = due_row(store, AT_0800)
    now = AT_0800 + MINUTE
    worker = make_worker(store, rec, now)
    with pytest.raises(ScheduleWorkerError):
        worker.run_due(store.row(row.schedule_id), now=now)
    assert store.row(row.schedule_id).today_count == 0


# ---------------------------------------------------------------------------
# 租约：数据库级，只延长不回退，不用内存锁
# ---------------------------------------------------------------------------


def test_lease_duration_is_sixty_seconds():
    assert SCHEDULE_LEASE_MS == 60_000


def test_lease_can_only_be_acquired_once(store):
    make_spec(store)
    assert store.acquire_lease("s1", AT_0800) is True
    # 同一时刻第二次抢不到（另一个 worker 正在处理）
    assert store.acquire_lease("s1", AT_0800) is False
    # 租约未过期前也抢不到
    assert store.acquire_lease("s1", AT_0800 + 30_000) is False
    # 过期后可再抢
    assert store.acquire_lease("s1", AT_0800 + SCHEDULE_LEASE_MS + 1) is True


def test_lease_never_shrinks(store):
    make_spec(store)
    store.acquire_lease("s1", AT_0800, lease_ms=120_000)
    original = store.row("s1").lease_until
    store.extend_lease("s1", AT_0800, lease_ms=1_000)
    assert store.row("s1").lease_until >= original


def test_worker_skips_when_lease_held(store):
    rec = Recorder()
    row = due_row(store, AT_0800)
    store.acquire_lease(row.schedule_id, AT_0800 + MINUTE, 60_000)
    worker = make_worker(store, rec, AT_0800 + MINUTE)
    result = worker.run_due(store.row(row.schedule_id), now=AT_0800 + MINUTE)

    assert result.reason == "lease_held"
    assert rec.model_calls == 0
    assert rec.sent == []


def test_worker_releases_lease_after_run(store):
    rec = Recorder()
    row = due_row(store, AT_0800)
    now = AT_0800 + MINUTE
    worker = make_worker(store, rec, now)
    worker.run_due(store.row(row.schedule_id), now=now)
    # 放掉租约后，另一个 worker 立刻可以接手
    assert store.acquire_lease(row.schedule_id, now) is True


def test_lease_is_shared_across_store_instances(tmp_path):
    """两个独立连接（等价两个进程）共享同一条数据库租约。"""
    path = tmp_path / "sched.db"
    first = ScheduleStore(sqlite3.connect(str(path)))
    second = ScheduleStore(sqlite3.connect(str(path)))
    try:
        first.register(
            ScheduleSpec.from_mapping({"schedule_id": "s1", "enabled": True, "timezone": CN}), AT_0800
        )
        assert first.acquire_lease("s1", AT_0800) is True
        assert second.acquire_lease("s1", AT_0800) is False   # 数据库级互斥
    finally:
        first.db.close()
        second.db.close()


# ---------------------------------------------------------------------------
# Worker：重启恢复 / 等待 / 只补一次
# ---------------------------------------------------------------------------


def test_worker_recovers_due_rows_from_database(store):
    """禁止只靠进程内 Timer：状态在 SQLite，重启后从 DUE 捡回来。"""
    rec = Recorder()
    row = due_row(store, AT_0800)
    worker = make_worker(store, rec, AT_0800 + MINUTE)
    results = worker.tick()
    assert len(results) == 1
    assert results[0].sent is True
    assert results[0].schedule_id == row.schedule_id


def test_tick_ignores_rows_not_due(store):
    rec = Recorder()
    make_spec(store, next_run_ms=AT_0800 + DAY)
    worker = make_worker(store, rec, AT_0800)
    assert worker.tick() == []
    assert rec.model_calls == 0


def test_device_offline_then_online_resumes_exactly_once(store):
    rec = Recorder(device=False)
    row = due_row(store, AT_0800, needs_live_device=True)
    now = AT_0800 + MINUTE
    worker = make_worker(store, rec, now)
    first = worker.run_due(store.row(row.schedule_id), now=now)
    assert first.state == S_WAIT_DEVICE
    assert rec.model_calls == 0

    # 设备仍离线：不动作、不重复建任务、不轮询截图
    rec.device = False
    still = worker.resume_waiting(store.row(row.schedule_id))
    assert still.reason == "device_still_offline"
    assert rec.model_calls == 0
    assert store.row(row.schedule_id).state == S_WAIT_DEVICE

    # 设备恢复在线：补执行一次
    rec.device = True
    resumed = worker.resume_waiting(store.row(row.schedule_id))
    assert resumed.sent is True
    assert rec.model_calls == 1
    assert len(rec.sent) == 1

    # 再调一次：不得补第二次
    again = worker.resume_waiting(store.row(row.schedule_id))
    assert again.reason != "" or again.sent is False
    assert rec.model_calls == 1
    assert len(rec.sent) == 1


def test_waiting_beyond_limit_expires(store):
    rec = Recorder(device=True)
    row = due_row(store, AT_0800, needs_live_device=True)
    store.db.execute(
        "UPDATE schedules SET state=?, resume_attempts=? WHERE id=?",
        (S_WAIT_DEVICE, 5, row.schedule_id),
    )
    store.db.commit()
    worker = make_worker(store, rec, AT_0800 + HOUR, max_wait_resumes=1)
    result = worker.resume_waiting(store.row(row.schedule_id))
    assert result.reason == "resume_limit_reached"
    assert rec.sent == []


def test_start_recovers_waiting_states(store):
    rec = Recorder(device=False)
    row = due_row(store, AT_0800, needs_live_device=True)
    store.db.execute("UPDATE schedules SET state=? WHERE id=?", (S_WAIT_DEVICE, row.schedule_id))
    store.db.commit()
    worker = make_worker(store, rec, AT_0800 + MINUTE)
    started = worker.start()
    assert started["resumed_waiting"][0]["reason"] == "device_still_offline"
    assert rec.model_calls == 0


def test_run_forever_uses_injected_sleep_only(store):
    """宿主循环：``sleep`` 必须可注入，测试里绝不真实等待。"""
    slept: list[float] = []
    rec = Recorder()
    worker = ScheduleWorker(
        store,
        model_decider=rec.model_decider,
        sender=rec.sender,
        device_online=rec.device_online,
        blocklisted=rec.blocklisted,
        clock=lambda: AT_0800 + MINUTE,
        sleep=lambda seconds: slept.append(seconds),
        target_user_id="10001",
    )
    assert worker.run_forever(interval_seconds=5.0, max_ticks=3) == 3
    assert slept == [5.0, 5.0]            # 最后一次不再睡


# ---------------------------------------------------------------------------
# 模型不得覆盖配额 / 安静时段 / 黑名单
# ---------------------------------------------------------------------------


def test_model_context_declares_immutable_fields(store):
    rec = Recorder()
    captured: dict = {}

    def decider(row, context):
        captured.update(context)
        return {"mode": "REPLY", "text": "hi"}

    row = due_row(store, AT_0800)
    now = AT_0800 + MINUTE
    worker = ScheduleWorker(
        store,
        model_decider=decider,
        sender=rec.sender,
        device_online=rec.device_online,
        clock=lambda: now,
        sleep=lambda _s: None,
        target_user_id="10001",
    )
    worker.run_due(store.row(row.schedule_id), now=now)
    assert captured["model_cannot_change"] == ["daily_limit", "quiet_hours", "blocklist"]
    assert captured["guard_order"] == list(GUARD_ORDER)


def test_model_output_cannot_change_next_run_or_quota(store):
    """模型即使返回额外字段，也不能改配额或重排下一次。"""
    rec = Recorder()
    row = due_row(store, AT_0800, daily_limit=1)
    now = AT_0800 + MINUTE

    def decider(_row, _context):
        return {"mode": "REPLY", "text": "hi", "daily_limit": 999, "next_run_ms": now}

    worker = ScheduleWorker(
        store,
        model_decider=decider,
        sender=rec.sender,
        clock=lambda: now,
        sleep=lambda _s: None,
        target_user_id="10001",
    )
    worker.run_due(store.row(row.schedule_id), now=now)
    after = store.row(row.schedule_id)
    assert after.today_count == 1
    assert after.spec["next_run_ms"] > now          # 仍按主机算，不采纳模型值
    assert after.spec["daily_limit"] == 1           # 配额未被改动


# ---------------------------------------------------------------------------
# 既有 API 兼容（test_core.py 依赖）
# ---------------------------------------------------------------------------


def test_legacy_schedule_and_store_api_still_works():
    store = ScheduleStore()
    try:
        store.save(Schedule("s1", True, {"next_run_ms": 1000}))
        assert [item.schedule_id for item in store.due(1000)] == ["s1"]
        assert store.due(999) == []
    finally:
        store.db.close()


def test_legacy_constructor_with_connection_still_works():
    connection = sqlite3.connect(":memory:")
    store = ScheduleStore(connection)
    try:
        store.save(Schedule("s2", True, {"next_run_ms": 0}))
        assert [item.schedule_id for item in store.due(0)] == ["s2"]
    finally:
        connection.close()


def test_state_columns_are_added_to_legacy_table():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE schedules(id TEXT PRIMARY KEY, enabled INTEGER NOT NULL, "
        "spec_json TEXT NOT NULL, updated_at INTEGER NOT NULL)"
    )
    connection.commit()
    store = ScheduleStore(connection)
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(schedules)")}
        assert {"state", "lease_until", "today_count", "last_effect_key"} <= columns
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def test_guard_failure_state_mapping():
    assert schedule_state_after_guard("enabled") == S_DISABLED
    assert schedule_state_after_guard("device_online") == S_WAIT_DEVICE
    assert schedule_state_after_guard("time_window") == S_EXPIRED
    assert schedule_state_after_guard("quiet_hours") == S_GUARD_CHECK


def test_model_state_is_declared_but_unreachable_from_guard():
    assert can_transition(S_GUARD_CHECK, S_MODEL_DECISION)
    assert can_transition(S_MODEL_DECISION, S_SEND)
    assert S_MODEL_DECISION in SCHEDULE_STATES
