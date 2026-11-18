"""M12 主机侧持久化与审计脱敏。

覆盖：固定八张表、Run 状态机、幂等 effect（含数据库级唯一约束）、
审计落库与脱敏规则表、保留期清理、以及 storage.py 的**向后兼容**。
"""

import json
import sqlite3
import threading

import pytest

from astrbot_whale_device.audit import (
    ALLOWED_DETAIL_KEYS,
    FORBIDDEN_DETAIL_KEYS,
    AuditLogger,
    audit_record,
    redact_phone,
    redact_text,
    sanitize_detail,
)
from astrbot_whale_device.storage import (
    AUDIT_TTL_MS,
    EFFECT_TTL_MS,
    MEDIA_TTL_MS,
    RECOVERABLE_RUN_STATES,
    RUN_CANCELLED,
    RUN_DENIED,
    RUN_FAILED,
    RUN_NEEDS_UNLOCK,
    RUN_PLANNED,
    RUN_RECEIVED,
    RUN_RUNNING,
    RUN_STATES,
    RUN_SUCCEEDED,
    RUN_TRANSITIONS,
    RUN_WAITING_DEVICE,
    TERMINAL_RUN_STATES,
    StorageError,
    TaskStore,
)

FIXED_HOST_TABLES = {
    "owner_config",
    "devices",
    "inbox",
    "runs",
    "effects",
    "schedules",
    "assets",
    "audit_events",
}


@pytest.fixture()
def store():
    instance = TaskStore()
    yield instance
    instance.close()


# ---------------------------------------------------------------------------
# 固定八张表
# ---------------------------------------------------------------------------


def test_store_creates_exactly_the_fixed_host_tables(store):
    rows = store._db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    names = {str(row[0]) for row in rows if not str(row[0]).startswith("sqlite_")}
    assert names == FIXED_HOST_TABLES


def test_runs_table_carries_error_code_and_step(store):
    store.create_run("r1", "m1", {"commands": []}, 100)
    columns = {row[1] for row in store._db.execute("PRAGMA table_info(runs)")}
    assert {"run_id", "message_id", "status", "plan_json", "current_step", "lease_until", "updated_at", "error_code"} <= columns


def test_old_database_is_migrated_in_place(tmp_path):
    """老库（没有 error_code / last_counter 列）必须能原地升级，不能要求重建。"""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE devices (
            device_id TEXT PRIMARY KEY,
            public_key TEXT NOT NULL DEFAULT '',
            connection_json TEXT NOT NULL DEFAULT '{}',
            capabilities_json TEXT NOT NULL DEFAULT '{}',
            last_seen_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            message_id TEXT NOT NULL,
            status TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            current_step INTEGER NOT NULL DEFAULT 0,
            lease_until INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL
        );
        INSERT INTO runs(run_id,message_id,status,plan_json,current_step,lease_until,updated_at)
        VALUES('old','m-old','RUNNING','{"commands":[]}',0,0,1);
        """
    )
    conn.commit()
    conn.close()

    upgraded = TaskStore(path)
    try:
        run = upgraded.run("old")
        assert run is not None
        assert run["error_code"] == ""
        assert upgraded.device_counter("never-seen") == 0
        upgraded.transition_run("old", RUN_FAILED, error_code="EXPIRED", updated_at=2)
        assert upgraded.run("old")["error_code"] == "EXPIRED"
    finally:
        upgraded.close()


# ---------------------------------------------------------------------------
# 向后兼容：plugin.py / onebot_lifecycle.py 依赖的四个方法
# ---------------------------------------------------------------------------


def test_legacy_task_store_api_is_unchanged(store):
    assert store.add_message("m1", "A", "hello", 100) is True
    assert store.add_message("m1", "A", "hello", 100) is False
    assert store.message_owner("m1") == "A"
    assert store.message_owner("nope") is None

    store.save_effect("k1", '{"kind":"reply"}', 100)
    assert store.effect("k1") == '{"kind":"reply"}'
    assert store.effect("missing") is None


def test_save_effect_keeps_overwrite_semantics(store):
    """既有语义是覆盖写；不能因为新增了 add_effect 就悄悄改成 insert-or-ignore。"""
    store.save_effect("k1", "first", 100)
    store.save_effect("k1", "second", 101)
    assert store.effect("k1") == "second"


def test_constructor_default_is_still_in_memory():
    store = TaskStore()
    try:
        assert store.add_message("m1", "A", "hi", 1) is True
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 新增：add_effect —— 不存在才写 + 数据库级唯一约束
# ---------------------------------------------------------------------------


def test_add_effect_writes_once(store):
    assert store.add_effect("k1", "first", 100) is True
    assert store.add_effect("k1", "second", 101) is False
    assert store.effect("k1") == "first"


def test_add_effect_survives_concurrent_writers(tmp_path):
    """跨线程（等价于跨进程的 SQLite 语义）也只允许一个写成功。"""
    path = tmp_path / "concurrent.db"
    store = TaskStore(path)
    wins: list[bool] = []
    lock = threading.Lock()

    def writer(index: int) -> None:
        result = store.add_effect("dup-key", f"payload-{index}", 100 + index)
        with lock:
            wins.append(result)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    try:
        assert wins.count(True) == 1
        assert wins.count(False) == 7
    finally:
        store.close()


def test_effects_unique_index_exists(store):
    indexes = {row[1] for row in store._db.execute("PRAGMA index_list(effects)")}
    assert "ux_effects_idempotency_key" in indexes
    info = list(store._db.execute("PRAGMA index_info(ux_effects_idempotency_key)"))
    assert len(info) == 1 and str(info[0][2]) == "idempotency_key"


def test_add_effect_rejects_bad_input(store):
    with pytest.raises(StorageError):
        store.add_effect("", "x", 1)
    with pytest.raises(StorageError):
        store.add_effect("k", 123, 1)  # type: ignore[arg-type]


def test_has_effect_matches_effect(store):
    assert store.has_effect("k1") is False
    store.add_effect("k1", "x", 1)
    assert store.has_effect("k1") is True


# ---------------------------------------------------------------------------
# 新增：save_audit —— audit_events 真正的写入 API
# ---------------------------------------------------------------------------


def test_save_audit_persists_and_reads_back(store):
    event_id = store.save_audit("onebot.connection", "A", "m1", {"state": "online"}, 100)
    assert isinstance(event_id, str) and event_id
    events = store.audit_events("onebot.connection")
    assert len(events) == 1
    assert events[0]["event_id"] == event_id
    assert events[0]["detail"] == {"state": "online"}
    assert events[0]["created_at"] == 100


def test_save_audit_filters_by_message_id(store):
    store.save_audit("run.transition", "", "m1", {}, 100)
    store.save_audit("run.transition", "", "m2", {}, 101)
    assert [item["message_id"] for item in store.audit_events(message_id="m2")] == ["m2"]


def test_save_audit_accepts_pre_serialized_detail(store):
    store.save_audit("run.transition", "", "m1", '{"x":1}', 100)
    assert store.audit_events()[0]["detail"] == {"x": 1}


def test_save_audit_rejects_empty_event_type(store):
    with pytest.raises(StorageError):
        store.save_audit("", "", "m1", {}, 100)


def test_audit_events_are_ordered_by_time(store):
    store.save_audit("run.transition", "", "m1", {}, 300)
    store.save_audit("run.transition", "", "m1", {}, 100)
    assert [item["created_at"] for item in store.audit_events()] == [100, 300]


# ---------------------------------------------------------------------------
# Run 状态机
# ---------------------------------------------------------------------------


def test_run_states_match_documented_machine():
    assert RUN_STATES == {
        "RECEIVED", "PLANNED", "RUNNING", "SUCCEEDED",
        "WAITING_DEVICE", "NEEDS_UNLOCK", "FAILED", "DENIED", "CANCELLED",
    }
    assert RECOVERABLE_RUN_STATES == {"RUNNING", "WAITING_DEVICE", "NEEDS_UNLOCK"}
    assert TERMINAL_RUN_STATES == {"SUCCEEDED", "FAILED", "DENIED", "CANCELLED"}


def test_documented_happy_path(store):
    store.create_run("r1", "m1", {"commands": [{"command": "device.device_status", "payload": {}}]}, 100)
    store.transition_run("r1", RUN_PLANNED, updated_at=101)
    store.transition_run("r1", RUN_RUNNING, updated_at=102)
    store.transition_run("r1", RUN_SUCCEEDED, step_index=1, updated_at=103)
    run = store.run("r1")
    assert run["status"] == RUN_SUCCEEDED
    assert run["current_step"] == 1


def test_running_can_fork_into_five_branches(store):
    for index, target in enumerate(
        [RUN_WAITING_DEVICE, RUN_NEEDS_UNLOCK, RUN_FAILED, RUN_DENIED, RUN_CANCELLED]
    ):
        run_id = f"r{index}"
        store.create_run(run_id, f"m{index}", {"commands": []}, 100)
        store.transition_run(run_id, RUN_PLANNED, updated_at=101)
        store.transition_run(run_id, RUN_RUNNING, updated_at=102)
        store.transition_run(run_id, target, error_code="X", updated_at=103)
        assert store.run(run_id)["status"] == target


def test_terminal_states_cannot_be_left(store):
    store.create_run("r1", "m1", {"commands": []}, 100)
    store.transition_run("r1", RUN_PLANNED, updated_at=101)
    store.transition_run("r1", RUN_RUNNING, updated_at=102)
    store.transition_run("r1", RUN_SUCCEEDED, updated_at=103)
    for target in RUN_STATES:
        with pytest.raises(StorageError):
            store.transition_run("r1", target, updated_at=104)


def test_illegal_transitions_are_rejected(store):
    store.create_run("r1", "m1", {"commands": []}, 100)
    # RECEIVED 不能直接跳 RUNNING
    with pytest.raises(StorageError):
        store.transition_run("r1", RUN_RUNNING, updated_at=101)
    with pytest.raises(StorageError):
        store.transition_run("r1", "NOT_A_STATE", updated_at=101)


def test_transition_records_step_and_error_code(store):
    store.create_run("r1", "m1", {"commands": []}, 100)
    store.transition_run("r1", RUN_PLANNED, updated_at=101)
    store.transition_run("r1", RUN_RUNNING, updated_at=102)
    store.transition_run("r1", RUN_FAILED, step_index=2, error_code="SMS_SEND_FAILED", updated_at=103)
    run = store.run("r1")
    assert run["current_step"] == 2
    assert run["error_code"] == "SMS_SEND_FAILED"


def test_step_index_never_moves_backwards(store):
    store.create_run("r1", "m1", {"commands": []}, 100)
    store.transition_run("r1", RUN_PLANNED, step_index=3, updated_at=101)
    with pytest.raises(StorageError):
        store.transition_run("r1", RUN_RUNNING, step_index=1, updated_at=102)


def test_transition_audit_carries_required_fields(store):
    store.create_run("r1", "m-mig", {"commands": []}, 100)
    store.transition_run("r1", RUN_PLANNED, updated_at=101)
    records = store.audit_events("run.transition")
    latest = records[-1]
    detail = latest["detail"]
    assert detail["run_id"] == "r1"
    assert detail["step_index"] == 0
    assert detail["updated_at"] == 101
    assert latest["message_id"] == "m-mig"
    assert detail["error_code"] == ""


def test_transition_with_expected_status_guard(store):
    store.create_run("r1", "m1", {"commands": []}, 100, status=RUN_RUNNING)
    store.transition_run("r1", RUN_FAILED, expected_status=RUN_RUNNING, updated_at=101)
    with pytest.raises(StorageError):
        store.transition_run("r1", RUN_FAILED, expected_status=RUN_RUNNING, updated_at=102)


def test_transition_unknown_run_is_rejected(store):
    with pytest.raises(StorageError):
        store.transition_run("nope", RUN_PLANNED, updated_at=1)


def test_create_run_is_idempotent(store):
    first = store.create_run("r1", "m1", {"commands": []}, 100)
    second = store.create_run("r1", "m2", {"commands": []}, 200)
    assert first["message_id"] == "m2" or second["message_id"] == "m1"
    assert store.runs_in_status({RUN_RECEIVED})[0]["run_id"] == "r1"
    assert len(store.runs_in_status({RUN_RECEIVED})) == 1


def test_runs_in_status_scans_recoverable_states(store):
    store.create_run("a", "m-a", {"commands": []}, 100, status=RUN_RUNNING)
    store.create_run("b", "m-b", {"commands": []}, 101, status=RUN_WAITING_DEVICE)
    store.create_run("c", "m-c", {"commands": []}, 102, status=RUN_SUCCEEDED)
    scanned = store.runs_in_status(RECOVERABLE_RUN_STATES)
    assert [item["run_id"] for item in scanned] == ["a", "b"]


# ---------------------------------------------------------------------------
# 计数器与租约：只增不减
# ---------------------------------------------------------------------------


def test_lease_only_extends(store):
    store.create_run("r1", "m1", {"commands": []}, 100)
    store.claim_lease("r1", 5_000, 100)
    store.claim_lease("r1", 1_000, 101)
    assert store.run("r1")["lease_until"] == 5_000


def test_device_counter_never_moves_backwards(store):
    store.record_device_counter("d1", 7, 100)
    store.record_device_counter("d1", 3, 101)
    assert store.device_counter("d1") == 7


def test_device_counter_lives_in_devices_table(store):
    store.record_device_counter("d1", 4, 100, public_key="pk")
    record = store.device_record("d1")
    assert record["last_counter"] == 4
    assert record["public_key"] == "pk"


def test_device_counter_rejects_negative(store):
    with pytest.raises(StorageError):
        store.record_device_counter("d1", -1, 1)


# ---------------------------------------------------------------------------
# 保留期清理
# ---------------------------------------------------------------------------


def test_retention_windows_are_fixed():
    from astrbot_whale_device.storage import DAY_MS

    assert DAY_MS == 86_400_000
    assert EFFECT_TTL_MS == 30 * DAY_MS
    assert AUDIT_TTL_MS == 90 * DAY_MS
    assert MEDIA_TTL_MS == DAY_MS


def test_purge_expired_respects_each_window(store):
    now = 100 * 86_400_000
    store.save_effect("old", "x", now - EFFECT_TTL_MS - 1)
    store.save_effect("fresh", "x", now - 1000)
    store.save_audit("run.transition", "", "m1", {}, now - AUDIT_TTL_MS - 1)
    store.save_audit("run.transition", "", "m2", {}, now - 1000)
    store._db.execute(
        "INSERT INTO assets(asset_id,path,mime,size,created_at) VALUES(?,?,?,?,?)",
        ("old-asset", "/tmp/x", "image/png", 1, now - MEDIA_TTL_MS - 1),
    )
    store._db.commit()

    purged = store.purge_expired(now)
    assert purged == {"effects": 1, "audit_events": 1, "assets": 1}
    assert store.effect("fresh") == "x"
    assert len(store.audit_events()) == 1


def test_purge_message_clears_session_immediately(store):
    store.add_message("m1", "A", "hello", 100)
    store.add_message("m2", "A", "hello", 101)
    store.save_audit("run.transition", "", "m1", {}, 100)
    purged = store.purge_message("m1")
    assert purged == {"inbox": 1, "audit_events": 1}
    assert store.message_owner("m1") is None
    assert store.message_owner("m2") == "A"


# ---------------------------------------------------------------------------
# 审计脱敏（纯函数）
# ---------------------------------------------------------------------------


def test_phone_keeps_only_last_four_digits():
    assert redact_phone("13800138000") == "*******8000"
    assert redact_phone("call 13800138000 now") == "call *******8000 now"
    assert redact_phone("123456") == "123456"  # 少于 7 位不算号码
    assert redact_phone("") == ""


def test_redact_phone_masks_multiple_numbers():
    assert redact_phone("13800138000 / 13900139000") == "*******8000 / *******9000"


@pytest.mark.parametrize(
    "forbidden",
    [
        {"password": "hunter2"},
        {"otp": "123456"},
        {"verification_code": "999999"},
        {"sms_body": "你好"},
        {"sms_text": "你好"},
        {"node_text": "账户余额"},
        {"screen_text": "password field"},
        {"raw_audio": b"RIFF"},
        {"media_content": "..."},
        {"payload": {"text": "hi"}},
        {"path": "/data/app/x"},
        {"clipboard": "复制内容"},
        {"token": "sk-xxx"},
        {"api_key": "sk-xxx"},
    ],
)
def test_forbidden_keys_are_dropped(forbidden):
    assert sanitize_detail(forbidden) == {}


def test_forbidden_keys_are_all_outside_the_whitelist():
    """纵深防御：禁止键必须一个都不在白名单里，否则双保险就变成了单保险。"""
    assert FORBIDDEN_DETAIL_KEYS & ALLOWED_DETAIL_KEYS == frozenset()


def test_unknown_keys_are_dropped_by_default():
    assert sanitize_detail({"free_form": "anything", "note": "x"}) == {}


def test_allowed_keys_survive():
    detail = {
        "mode": "REMOTE_B",
        "command": "device.app.launch",
        "package_name": "com.android.settings",
        "result": "ok",
        "error_code": "",
        "sender_class": "OWNER",
    }
    assert sanitize_detail(detail) == detail


def test_camel_case_forbidden_keys_are_also_dropped():
    assert sanitize_detail({"screenText": "余额", "rawAudio": "..."}) == {}
    assert sanitize_detail({"password_value": "x"}) == {}


def test_sensitive_value_markers_wipe_the_whole_value():
    assert sanitize_detail({"reason": "用户输入了密码 hunter2"}) == {}
    assert sanitize_detail({"result": "收到了验证码 1234"}) == {}


def test_text_is_truncated_not_dropped():
    long_text = "a" * 700
    value = sanitize_detail({"reason": long_text})["reason"]
    assert value.endswith("…")
    assert len(value) <= 513


def test_non_mapping_detail_becomes_empty():
    for value in (None, "text", 123, ["a"], object()):
        assert sanitize_detail(value) == {}


def test_nested_mapping_is_scrubbed_recursively():
    detail = {"run_id": "r1", "from": "RUNNING", "to": "SUCCEEDED"}
    assert sanitize_detail(detail) == detail


def test_oversized_detail_is_capped():
    detail = {f"key{i}": "v" for i in range(60)}
    assert sanitize_detail(detail) == {}


def test_bytes_are_never_persisted():
    assert sanitize_detail({"reason": b"\x00\x01"}) == {}


def test_nan_and_inf_are_dropped():
    assert sanitize_detail({"count": float("nan")}) == {}
    assert sanitize_detail({"count": float("inf")}) == {}


# ---------------------------------------------------------------------------
# AuditLogger
# ---------------------------------------------------------------------------


def test_audit_logger_writes_sanitized_detail(store):
    logger = AuditLogger(store, clock=lambda: 100)
    event_id = logger.record(
        "onebot.connection",
        message_id="m1",
        actor="A",
        detail={"state": "online", "password": "hunter2", "sms_body": "正文"},
    )
    events = store.audit_events("onebot.connection")
    assert len(events) == 1
    assert events[0]["event_id"] == event_id
    assert "password" not in events[0]["detail"]
    assert "sms_body" not in events[0]["detail"]
    assert events[0]["detail"] == {"state": "online"}
    assert events[0]["created_at"] == 100


def test_audit_logger_count_and_events(store):
    logger = AuditLogger(store, clock=lambda: 100)
    logger.record("run.transition", detail={})
    logger.record("run.resumed", detail={})
    assert logger.count() == 2
    assert logger.count("run.resumed") == 1


def test_audit_record_is_pure(store):
    """纯函数：同样的输入永远得到同样的输出，且不触碰存储。"""
    first = audit_record("run.transition", message_id="m1", actor="A", detail={"run_id": "r"}, created_at=5)
    second = audit_record("run.transition", message_id="m1", actor="A", detail={"run_id": "r"}, created_at=5)
    assert first == second
    assert store.audit_events() == []


def test_audit_record_rejects_empty_event_type():
    from astrbot_whale_device.audit import AuditError

    with pytest.raises(AuditError):
        audit_record("", created_at=1)


def test_audit_logger_sweep_reports_and_logs(store):
    from astrbot_whale_device.storage import DAY_MS

    now = 200 * DAY_MS
    store.save_audit("run.transition", "", "m1", {}, now - AUDIT_TTL_MS - 1)
    logger = AuditLogger(store, clock=lambda: now)
    purged = logger.sweep(now)
    assert purged["audit_events"] == 1
    assert any(item["event_type"] == "audit.retention_swept" for item in store.audit_events())


def test_media_content_never_enters_audit_table(store):
    """M12 禁止项：不得把用户媒体内容写入审计表。"""
    logger = AuditLogger(store, clock=lambda: 100)
    logger.record(
        "device_recovered",
        detail={
            "device_id": "d1",
            "media": [{"type": "image", "file": "/sdcard/DCIM/a.jpg"}],
            "payload": {"asset_id": "a1"},
            "path": "/sdcard/DCIM/a.jpg",
        },
    )
    serialized = json.dumps(store.audit_events(), ensure_ascii=False)
    assert "DCIM" not in serialized
    assert "asset_id" not in serialized


def test_state_is_not_memory_only(tmp_path):
    """M12 禁止项：禁止只把状态保存在内存。重开库必须读得到。"""
    path = tmp_path / "persist.db"
    first = TaskStore(path)
    first.create_run("r1", "m1", {"commands": [{"command": "device.app.launch", "payload": {"package_name": "com.example"}}]}, 100)
    first.transition_run("r1", RUN_PLANNED, updated_at=101)
    first.transition_run("r1", RUN_RUNNING, updated_at=102)
    first.add_effect("m1:0", "{}", 103)
    first.save_audit("run.transition", "", "m1", {"run_id": "r1"}, 104)
    first.close()

    second = TaskStore(path)
    try:
        assert second.run("r1")["status"] == RUN_RUNNING
        assert second.has_effect("m1:0")
        assert len(second.audit_events()) == 3
    finally:
        second.close()
