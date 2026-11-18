"""M12 恢复算法四步 + 两条 M13 验收用例。

覆盖 ``test-fixtures/m13-acceptance-matrix.json`` 点名的：

- ``host_restart_recovery``： ``runs`` RECEIVED→SUCCEEDED、``effects`` 恰好 1 行、
  审计出现 ``run_resumed``、``resume_only_once``。
- ``device_process_restart_recovery``：设备已落 ``processed_effects`` 时不重复执行、
  审计出现 ``device_recovered``、``no_repeat_execution``。
"""

import json

import pytest

from astrbot_whale_device.audit import AuditLogger
from astrbot_whale_device.models import DeviceResponse
from astrbot_whale_device.recovery import (
    DeviceRecoveryView,
    IDEMPOTENT_SAFE_COMMANDS,
    IRREVERSIBLE_COMMANDS,
    RECONFIRM_COMMANDS,
    RecoveryError,
    RecoveryService,
    is_resumable_without_confirmation,
    plan_is_well_formed,
    plan_steps,
    requires_reconfirmation,
    step_idempotency_key,
)
from astrbot_whale_device.storage import (
    RECOVERABLE_RUN_STATES,
    RUN_FAILED,
    RUN_NEEDS_UNLOCK,
    RUN_PLANNED,
    RUN_RECEIVED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    RUN_WAITING_DEVICE,
    TaskStore,
)

LAUNCH = {"command": "device.app.launch", "payload": {"package_name": "com.android.settings"}}
STATUS = {"command": "device.device_status", "payload": {}}
SMS = {"command": "device.sms.send", "payload": {"number": "13800138000", "text": "你好"}}
SHARE = {"command": "device.media.share", "payload": {"asset_id": "a1", "target": "com.tencent.mobileqq"}}


@pytest.fixture()
def store():
    instance = TaskStore()
    yield instance
    instance.close()


def running_run(store: TaskStore, run_id="r1", message_id="m1", commands=None, status=RUN_RUNNING):
    plan = [LAUNCH] if commands is None else list(commands)
    store.create_run(run_id, message_id, {"commands": plan}, 100)
    if status != RUN_RECEIVED:
        store.transition_run(run_id, RUN_PLANNED, updated_at=101)
    if status not in (RUN_RECEIVED, RUN_PLANNED):
        store.transition_run(run_id, status, updated_at=102)
    return store.run(run_id)


# ---------------------------------------------------------------------------
# 分类表：谁的恢复是安全的
# ---------------------------------------------------------------------------


def test_irreversible_commands_are_exactly_the_documented_three():
    assert IRREVERSIBLE_COMMANDS == {"device.sms.send", "device.media.share", "device.media.upload"}


def test_irreversible_commands_always_need_reconfirmation():
    for command in IRREVERSIBLE_COMMANDS:
        assert requires_reconfirmation(command) is True


def test_sms_and_share_are_in_the_reconfirm_set():
    assert "device.sms.send" in RECONFIRM_COMMANDS
    assert "device.media.share" in RECONFIRM_COMMANDS


@pytest.mark.parametrize("command", sorted(IDEMPOTENT_SAFE_COMMANDS))
def test_idempotent_safe_commands_can_resume(command):
    assert requires_reconfirmation(command) is False


@pytest.mark.parametrize("command", ["device.shell", "device.eval", "", None, "unknown.command"])
def test_unknown_or_forbidden_commands_default_to_reconfirmation(command):
    """默认拒绝：没登记的命令不会被当成安全命令自动恢复。"""
    assert requires_reconfirmation(command) is True


def test_plan_is_resumable_only_if_every_step_is_safe():
    assert is_resumable_without_confirmation([STATUS, LAUNCH]) is True
    assert is_resumable_without_confirmation([STATUS, SMS]) is False
    assert is_resumable_without_confirmation([STATUS, SHARE]) is False
    assert is_resumable_without_confirmation([]) is False


def test_plan_well_formedness_separates_corrupt_from_empty():
    assert plan_is_well_formed({"plan": {"commands": [LAUNCH]}}) is True
    assert plan_is_well_formed({"plan": {"commands": []}}) is True      # 空但结构合法
    assert plan_is_well_formed({"plan": {}}) is False                   # 缺 commands
    assert plan_is_well_formed({"plan": {"commands": ["bad"]}}) is False
    assert plan_is_well_formed({"plan": {"commands": [{"command": "x"}]}}) is False
    assert plan_is_well_formed({"plan": "x"}) is False


def test_step_key_matches_plugin_convention():
    """幂等键必须与 plugin.py 的 ``f"{message_id}:{index}"`` 逐字一致。"""
    assert step_idempotency_key("m1", 0) == "m1:0"
    assert step_idempotency_key("m1", 3) == "m1:3"


def test_plan_steps_drops_malformed_items():
    run = {"run_id": "r1", "message_id": "m1", "plan": {"commands": [LAUNCH, "bad", {"command": "x"}, {"payload": {}}]}}
    steps = plan_steps(run)
    assert [step.command for step in steps] == ["device.app.launch"]
    assert steps[0].idempotency_key == "m1:0"


# ---------------------------------------------------------------------------
# 第 1 步：扫描三个非终态
# ---------------------------------------------------------------------------


def test_scan_only_picks_the_three_recoverable_states(store):
    store.create_run("a", "m-a", {"commands": []}, 100, status=RUN_RUNNING)
    store.create_run("b", "m-b", {"commands": []}, 101, status=RUN_WAITING_DEVICE)
    store.create_run("c", "m-c", {"commands": []}, 102, status=RUN_NEEDS_UNLOCK)
    store.create_run("d", "m-d", {"commands": []}, 103, status=RUN_SUCCEEDED)
    store.create_run("e", "m-e", {"commands": []}, 104, status=RUN_FAILED)

    service = RecoveryService(store, clock=lambda: 500)
    scanned = [item["run_id"] for item in service.scan()]
    assert scanned == ["a", "b", "c"]
    assert set(RECOVERABLE_RUN_STATES) == {"RUNNING", "WAITING_DEVICE", "NEEDS_UNLOCK"}


# ---------------------------------------------------------------------------
# 第 2 步：effect 已存在 → 标记完成，不重做副作用
# ---------------------------------------------------------------------------


def test_existing_effect_means_no_replay(store):
    executed: list[str] = []

    def executor(step):
        executed.append(step.idempotency_key)
        return DeviceResponse(step.idempotency_key, "d1", "ok")

    run = running_run(store, commands=[LAUNCH])
    store.add_effect("m1:0", '{"status":"ok"}', 200)

    service = RecoveryService(store, clock=lambda: 300, executor=executor)
    outcome = service.recover_run(run)
    assert outcome.skipped_steps == ["m1:0"]
    assert outcome.resumed_steps == []
    assert executed == []
    assert store.run("r1")["status"] == RUN_SUCCEEDED


def test_partial_effect_only_resumes_the_missing_step(store):
    executed: list[str] = []

    def executor(step):
        executed.append(step.idempotency_key)
        return DeviceResponse(step.idempotency_key, "d1", "ok")

    run = running_run(store, commands=[STATUS, LAUNCH])
    store.add_effect("m1:0", '{"status":"ok"}', 200)

    service = RecoveryService(store, clock=lambda: 300, executor=executor)
    outcome = service.recover_run(run)
    assert outcome.skipped_steps == ["m1:0"]
    assert executed == ["m1:1"]
    assert store.effect("m1:1") is not None
    assert store.run("r1")["status"] == RUN_SUCCEEDED


# ---------------------------------------------------------------------------
# 第 3 步：没有 effect 时，不可逆命令必须重新确认
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", [SMS, SHARE, {"command": "device.media.upload", "payload": {"asset_id": "a1"}}])
def test_irreversible_step_is_never_executed_on_recovery(store, command):
    executed: list[str] = []

    def executor(step):
        executed.append(step.command)
        return DeviceResponse(step.idempotency_key, "d1", "ok")

    run = running_run(store, commands=[command])
    service = RecoveryService(store, clock=lambda: 300, executor=executor)
    outcome = service.recover_run(run)

    assert executed == []                       # 绝不自动执行
    assert outcome.action == "reconfirm"
    assert outcome.final_status == RUN_NEEDS_UNLOCK
    assert store.run("r1")["status"] == RUN_NEEDS_UNLOCK
    assert store.run("r1")["error_code"] == "NEEDS_UNLOCK"
    assert outcome.reconfirm_steps == ["m1:0"]


def test_recovery_audit_marks_reconfirmation(store):
    run = running_run(store, commands=[SMS])
    service = RecoveryService(store, clock=lambda: 300)
    service.recover_run(run)

    events = store.audit_events("run_recovery_reconfirm")
    assert len(events) == 1
    assert events[0]["detail"]["requires_reconfirmation"] is True
    assert events[0]["detail"]["executed"] is False
    assert events[0]["detail"]["command"] == "device.sms.send"


def test_safe_step_after_irreversible_is_one_block(store):
    """只要计划里有一条不可逆命令，整段就停在重新确认，不偏科地执行前半段。"""
    executed: list[str] = []

    def executor(step):
        executed.append(step.command)
        return DeviceResponse(step.idempotency_key, "d1", "ok")

    run = running_run(store, commands=[STATUS, SMS])
    service = RecoveryService(store, clock=lambda: 300, executor=executor)
    service.recover_run(run)
    assert executed == []
    assert store.run("r1")["status"] == RUN_NEEDS_UNLOCK


# ---------------------------------------------------------------------------
# 第 4 步：计数器与租约不回退
# ---------------------------------------------------------------------------


def test_recovery_extends_lease_and_never_shrinks_it(store):
    run = running_run(store)
    store.claim_lease("r1", 9_000, 200)
    service = RecoveryService(store, clock=lambda: 300, lease_ms=60_000)
    service.recover_run(run)
    assert store.run("r1")["lease_until"] >= 9_000


def test_recovery_never_resets_device_counter(store):
    store.record_device_counter("d1", 42, 200)
    service = RecoveryService(store, clock=lambda: 300)
    service.recover_all()
    assert store.device_counter("d1") == 42


# ---------------------------------------------------------------------------
# 失败与边界
# ---------------------------------------------------------------------------


def test_corrupt_plan_fails_without_execution(store):
    executed: list[str] = []

    def executor(step):
        executed.append(step.idempotency_key)
        return DeviceResponse(step.idempotency_key, "d1", "ok")

    run = running_run(store, commands=[])
    service = RecoveryService(store, clock=lambda: 300, executor=executor)
    outcome = service.recover_run(run)
    assert executed == []
    assert store.run("r1")["status"] == RUN_FAILED
    assert store.run("r1")["error_code"] == "INVALID_PAYLOAD"


def test_executor_failure_stops_the_rest(store):
    executed: list[str] = []

    def executor(step):
        executed.append(step.idempotency_key)
        return DeviceResponse(step.idempotency_key, "d1", "failed", error_code="LAUNCH_FAILED")

    run = running_run(store, commands=[STATUS, LAUNCH])
    service = RecoveryService(store, clock=lambda: 300, executor=executor)
    outcome = service.recover_run(run)
    assert executed == ["m1:0"]
    assert store.run("r1")["status"] == RUN_FAILED
    assert store.run("r1")["error_code"] == "LAUNCH_FAILED"


def test_executor_writes_effect_via_add_effect(store):
    """恢复期的 effect 必须用「不存在才写」，不能被后到的结果覆盖。"""
    run = running_run(store, commands=[STATUS])
    store.add_effect("m1:0", '{"status":"first"}', 200)
    service = RecoveryService(store, clock=lambda: 300, executor=lambda step: DeviceResponse("x", "d1", "ok"))
    service.recover_run(run)
    assert store.effect("m1:0") == '{"status":"first"}'


def test_recover_all_is_idempotent(store):
    """连跑两次 recover_all，不得产生第二次副作用。"""
    executed: list[str] = []

    def executor(step):
        executed.append(step.idempotency_key)
        return DeviceResponse(step.idempotency_key, "d1", "ok")

    store.create_run("r1", "m1", {"commands": [STATUS]}, 100, status=RUN_RUNNING)
    service = RecoveryService(store, clock=lambda: 300, executor=executor)
    first = service.recover_all()
    second = service.recover_all()

    assert executed == ["m1:0"]
    assert first["executed"] == 1
    assert second["scanned"] == 0


# ---------------------------------------------------------------------------
# M13 验收用例 1：host_restart_recovery
# ---------------------------------------------------------------------------


def test_acceptance_host_restart_recovery(store):
    """``runs`` RECEIVED→SUCCEEDED、``effects`` 1 行、审计 ``run_resumed``、只恢复一次。

    场景：主机在 run 中途重启，该 step 尚未落 effect。恢复后执行一次、只执行一次。
    """
    executed: list[str] = []

    def executor(step):
        executed.append(step.idempotency_key)
        return DeviceResponse(step.idempotency_key, "device-1", "ok")

    store.create_run("run-1", "m-restart", {"commands": [LAUNCH]}, 100)
    store.transition_run("run-1", RUN_PLANNED, updated_at=101)
    store.transition_run("run-1", RUN_RUNNING, updated_at=102)

    service = RecoveryService(store, clock=lambda: 200, executor=executor)
    first = service.recover_all()

    assert first["scanned"] == 1
    assert executed == ["m-restart:0"]           # resume_only_once
    rows = store._db.execute("SELECT COUNT(*) FROM effects WHERE idempotency_key='m-restart:0'").fetchone()
    assert int(rows[0]) == 1                      # effects 恰好 1 行

    run = store.run("run-1")
    assert run["status"] == RUN_SUCCEEDED         # RECEIVED→...→SUCCEEDED
    assert run["current_step"] == 1

    resumed = store.audit_events("run_resumed")
    assert len(resumed) == 1
    assert resumed[0]["detail"]["run_id"] == "run-1"
    assert resumed[0]["detail"]["executed"] is True

    # 再"重启"一次：不得重复执行。
    second = service.recover_all()
    assert second["scanned"] == 0
    assert executed == ["m-restart:0"]

    # cleanup
    store._db.execute("DELETE FROM runs")
    store._db.commit()


# ---------------------------------------------------------------------------
# M13 验收用例 2：device_process_restart_recovery
# ---------------------------------------------------------------------------


def test_acceptance_device_process_restart_recovery(store):
    """设备已落 ``processed_effects`` → 不重复执行、审计 ``device_recovered``。"""
    store.create_run("run-2", "m-dev", {"commands": [LAUNCH]}, 100, status=RUN_RUNNING)
    store.add_effect("m-dev:0", '{"status":"ok"}', 150)

    view = DeviceRecoveryView(store, clock=lambda: 300)
    result = view.reconcile(
        "run-2",
        device_processed_keys=["m-dev:0"],
        device_id="device-1",
        counter=5,
    )

    assert result["skipped"] == ["m-dev:0"]
    assert result["replayed"] == ["m-dev:0"]
    assert result["no_repeat_execution"] is True
    assert result["reconfirm"] == []
    assert store.device_counter("device-1") == 5

    events = store.audit_events("device_recovered")
    assert len(events) == 1
    assert events[0]["detail"]["executed"] is False
    assert events[0]["detail"]["skipped"] == 1

    # 重连两次：计数器不回退（no_repeat_execution 仍成立）
    second = view.reconcile("run-2", device_processed_keys=["m-dev:0"], device_id="device-1", counter=4)
    assert second["counter"] == 5

    # cleanup
    store._db.execute("DELETE FROM effects WHERE idempotency_key='m-dev:0'")
    store._db.commit()


def test_device_reconcile_flags_irreversible_steps(store):
    store.create_run("run-3", "m-x", {"commands": [SMS]}, 100, status=RUN_RUNNING)
    view = DeviceRecoveryView(store, clock=lambda: 300)
    result = view.reconcile("run-3", device_processed_keys=[], device_id="device-1")
    assert result["reconfirm"] == ["m-x:0"]
    assert result["no_repeat_execution"] is False


def test_device_reconcile_unknown_run_raises(store):
    view = DeviceRecoveryView(store, clock=lambda: 300)
    with pytest.raises(RecoveryError):
        view.reconcile("nope", device_processed_keys=[], device_id="device-1")


def test_no_executor_never_pretends_to_have_executed(store):
    """没注入 executor 时，恢复只能记账，不能假装执行过。"""
    run = running_run(store, commands=[STATUS])
    service = RecoveryService(store, clock=lambda: 300)
    outcome = service.recover_run(run)
    assert outcome.resumed_steps == []
    assert outcome.action == "resume"
    assert store.effect("m1:0") is None


# ---------------------------------------------------------------------------
# 审计表里不得出现用户内容
# ---------------------------------------------------------------------------


def test_recovery_audit_contains_no_user_content(store):
    store.create_run("run-4", "m-audit", {"commands": [SMS]}, 100, status=RUN_RUNNING)
    logger = AuditLogger(store, clock=lambda: 300)
    service = RecoveryService(store, clock=lambda: 300, audit=logger)
    service.recover_run(store.run("run-4"))

    serialized = json.dumps(store.audit_events(), ensure_ascii=False)
    assert "13800138000" not in serialized      # 完整号码不落库
    assert "你好" not in serialized              # 短信正文不落库
    assert "device.sms.send" in serialized       # 命令名是允许记录的
