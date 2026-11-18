"""M12 恢复算法：主机重启 / 设备进程被杀之后，把未完成的任务安全收拢。

规范来源：``docs/modules/M12-recovery-audit-development.md``「恢复算法」，四步固定：

1. 启动时扫描 ``RUNNING / WAITING_DEVICE / NEEDS_UNLOCK``。
2. 如果 effect 已存在 → 标记该 step 完成，**不重做副作用**。
3. 如果没有 effect → 只有**幂等安全**的命令允许恢复；
   短信发送、媒体分享和**发布动作**必须**重新确认**。
4. 设备计数器和租约**不回退**。

本模块只做「判断与记账」，不直接发设备帧。真正的执行由调用方的
``executor(next_step) -> DeviceResponse`` 注入，因此恢复逻辑可以完全离线测试，
也不会绕过 :class:`~astrbot_whale_device.security.PackagePolicy`。

关键不变量（拿掉任何一条，恢复都会变危险）：

- **幂等键只有一个来源**：:func:`step_idempotency_key(run, step_index)`
  = ``f"{message_id}:{step_index}"``。它必须与 ``plugin.py`` 执行计划时用的键
  **逐字一致**，否则「effect 已存在」的判断会失效、命令会被执行第二次。
- **执行前先查 effect，执行后再写 effect**，且写用的是 ``add_effect``（不存在才写）。
- **不可逆命令永不被自动执行**：一律转到 ``NEEDS_UNLOCK`` 并要求重新确认。
- **计数器与租约只增不减**。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .audit import AuditLogger
from .storage import (
    RECOVERABLE_RUN_STATES,
    RUN_FAILED,
    RUN_NEEDS_UNLOCK,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    RUN_WAITING_DEVICE,
    StorageError,
    TaskStore,
)


# ---------------------------------------------------------------------------
# 命令分类：哪些能自动恢复，哪些必须重新确认
# ---------------------------------------------------------------------------

#: **不可逆副作用**：短信发送、媒体分享（外发到第三方）、发布类动作。
#: 这几类命令无论看起来多安全，恢复时都**不得自动执行**。
IRREVERSIBLE_COMMANDS: frozenset[str] = frozenset({
    "device.sms.send",
    "device.media.share",
    "device.media.upload",   # 外发素材到第三方端点
})

#: 需要重新确认的命令（比不可逆集合更宽）。
RECONFIRM_COMMANDS: frozenset[str] = IRREVERSIBLE_COMMANDS | frozenset({
    "device.telephony.dial",  # 触达真实号码
    "device.sms.compose",     # 打开编辑界面且载荷含号码与正文
    "device.ui.tap",          # 可能是「发布/删除/付款」按钮——命令名判定不出来
    "device.ui.long_press",   # 同上；长按常绑定不可逆操作
    "device.ui.input",        # 可能是密码/验证码/支付字段
    "device.contacts.search", # 读取联系人（隐私面）
    "device.media.pick",      # 选取用户媒体
    "device.gallery.list",    # 读取相册索引
    "device.gallery.read",    # 读取受限照片预览
})

#: 明确幂等安全的命令：重启后自动恢复是安全的。
#:
#: 判定标准只有一条：**重复执行不会产生新的、用户可见的副作用**。
#: ``device.app.launch`` 在这里是必须的——它最坏只是把应用再拉到前台，
#: 且 M13 验收用例 ``host_restart_recovery`` 点名要求它在重启后能被恢复。
IDEMPOTENT_SAFE_COMMANDS: frozenset[str] = frozenset({
    "device.app.list",
    "device.app.launch",
    "device.device_status",
    "device.system.unlock",
    "device.ui.observe",
    "device.screen.capture",
    "device.ui.back",
    "device.ui.home",
    "device.ui.recents",
    "device.ui.notifications",
    "device.ui.scroll",
    "device.system.settings",
    "device.camera.capture",
})


def requires_reconfirmation(command: str, *, expected_package: str = "") -> bool:
    """恢复时是否需要重新确认。

    判定顺序**固定**：
    1. 不可逆集合 / 扩展确认集合 → 需要确认（优先，不能被任何规则绕过）；
    2. 幂等安全集合 → 不需要确认；
    3. 其它（含未知命令）→ 需要确认（**默认拒绝**，未登记不等于安全）。
    """
    if not isinstance(command, str) or not command:
        return True
    if command in RECONFIRM_COMMANDS:
        return True
    if command in IDEMPOTENT_SAFE_COMMANDS:
        return False
    return True


def is_resumable_without_confirmation(commands: list[Mapping[str, Any]]) -> bool:
    """整段计划能否在恢复时无人值守地继续：**每一条**都必须幂等安全。"""
    if not commands:
        return False
    for item in commands:
        if not isinstance(item, Mapping):
            return False
        if requires_reconfirmation(str(item.get("command", ""))):
            return False
    return True


def step_idempotency_key(message_id: str, step_index: int) -> str:
    """恢复用的幂等键。必须与 ``plugin.py`` 的 ``f"{message_id}:{index}"`` 一致。"""
    return f"{message_id}:{int(step_index)}"


# ---------------------------------------------------------------------------
# 结果类型
# ---------------------------------------------------------------------------


class RecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class StepPlan:
    """恢复视角下的一个 step。"""

    run_id: str
    message_id: str
    step_index: int
    command: str
    payload: dict[str, Any]
    idempotency_key: str

    @property
    def needs_reconfirmation(self) -> bool:
        return requires_reconfirmation(self.command, expected_package=str(self.payload.get("expected_package", "")))


@dataclass
class RunRecovery:
    """一条 run 的恢复决定。"""

    run_id: str
    message_id: str
    previous_status: str
    action: str  # resume / recomplete / reconfirm / already_done
    resumed_steps: list[str] = field(default_factory=list)
    skipped_steps: list[str] = field(default_factory=list)
    reconfirm_steps: list[str] = field(default_factory=list)
    next_step: StepPlan | None = None
    final_status: str = ""

    @property
    def executed_any(self) -> bool:
        """是否真的执行了任何副作用（验收用例要求重启后不重复执行）。"""
        return bool(self.resumed_steps)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "message_id": self.message_id,
            "previous_status": self.previous_status,
            "action": self.action,
            "resumed_steps": list(self.resumed_steps),
            "skipped_steps": list(self.skipped_steps),
            "reconfirm_steps": list(self.reconfirm_steps),
            "next_step": None if self.next_step is None else {
                "step_index": self.next_step.step_index,
                "command": self.next_step.command,
                "idempotency_key": self.next_step.idempotency_key,
            },
            "final_status": self.final_status,
        }


def plan_steps(run: Mapping[str, Any]) -> list[StepPlan]:
    """把 run 的 plan 展开成 step 列表。

    形状不合规的 step 直接丢弃；调用方必须用 :func:`plan_is_well_formed`
    区分「计划本身损坏」与「计划合法但为空」，前者要判失败。
    """
    plan = run.get("plan")
    if not isinstance(plan, Mapping):
        return []
    commands = plan.get("commands")
    if not isinstance(commands, list):
        return []
    message_id = str(run.get("message_id", ""))
    steps: list[StepPlan] = []
    for index, item in enumerate(commands):
        if not isinstance(item, Mapping):
            continue
        name = item.get("command")
        payload = item.get("payload")
        if not isinstance(name, str) or not name or not isinstance(payload, Mapping):
            continue
        steps.append(
            StepPlan(
                run_id=str(run.get("run_id", "")),
                message_id=message_id,
                step_index=index,
                command=name,
                payload=dict(payload),
                idempotency_key=step_idempotency_key(message_id, index),
            )
        )
    return steps


def plan_is_well_formed(run: Mapping[str, Any]) -> bool:
    """计划是否**结构上**合法（哪怕只有 0 条命令也算合法）。"""
    plan = run.get("plan")
    if not isinstance(plan, Mapping):
        return False
    commands = plan.get("commands")
    if not isinstance(commands, list):
        return False
    for item in commands:
        if not isinstance(item, Mapping):
            return False
        if not isinstance(item.get("command"), str) or not item.get("command"):
            return False
        if not isinstance(item.get("payload"), Mapping):
            return False
    return True


# ---------------------------------------------------------------------------
# 恢复器
# ---------------------------------------------------------------------------


class RecoveryService:
    """M12 恢复算法的唯一实现。

    ``executor`` 由调用方注入：它拿到一个 :class:`StepPlan`，返回设备响应
    （建议用 ``DeviceResponse``）。**恢复器自己从不直接访问 DeviceTransport**。
    """

    def __init__(
        self,
        store: TaskStore,
        *,
        device_id: str = "",
        audit: AuditLogger | None = None,
        executor: Callable[[StepPlan], Any] | None = None,
        clock: Callable[[], int] | None = None,
        lease_ms: int = 60_000,
    ) -> None:
        self.store = store
        self.device_id = device_id
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.lease_ms = max(1, int(lease_ms))
        self.audit = audit or AuditLogger(store, clock=self.clock)
        self._executor = executor

    # -- 第 1 步：扫描 -----------------------------------------------------
    def scan(self) -> list[dict[str, Any]]:
        """扫描 ``RUNNING / WAITING_DEVICE / NEEDS_UNLOCK``，按更新时间升序。"""
        return self.store.runs_in_status(RECOVERABLE_RUN_STATES)

    # -- 主入口 -----------------------------------------------------------
    def recover_all(self) -> dict[str, Any]:
        """启动恢复。返回汇总（含每一条 run 的决定），并保证幂等。"""
        now = self.clock()
        scanned = self.scan()
        recovered: list[RunRecovery] = []
        for run in scanned:
            recovered.append(self.recover_run(run, now=now))
        return {
            "scanned": len(scanned),
            "runs": [item.as_dict() for item in recovered],
            "executed": sum(1 for item in recovered if item.executed_any),
        }

    def recover_run(self, run: Mapping[str, Any], *, now: int | None = None) -> RunRecovery:
        """恢复单条 run，严格按文档四步走。"""
        stamp = self.clock() if now is None else int(now)
        run_id = str(run.get("run_id", ""))
        message_id = str(run.get("message_id", ""))
        previous = str(run.get("status", ""))

        # 第 4 步的前置：租约**只延长不回退**。恢复进程先占住这条 run。
        self.store.claim_lease(run_id, stamp + self.lease_ms, stamp)

        steps = plan_steps(run)
        outcome = RunRecovery(run_id=run_id, message_id=message_id, previous_status=previous, action="already_done")

        if not plan_is_well_formed(run):
            # 计划损坏：不执行任何东西，判失败（绝不猜一个 plan 出来）。
            self.store.transition_run(run_id, RUN_FAILED, error_code="INVALID_PAYLOAD", updated_at=stamp)
            outcome.final_status = RUN_FAILED
            outcome.action = "failed"
            return outcome

        if not steps:
            # 结构合法但没有命令：同样是错误状态，不能当成成功。
            self.store.transition_run(run_id, RUN_FAILED, error_code="INVALID_PAYLOAD", updated_at=stamp)
            outcome.final_status = RUN_FAILED
            outcome.action = "failed"
            return outcome

        # 第 2 步：逐 step 查 effect。已存在 → 标记完成，不重做副作用。
        start_from = int(run.get("current_step", 0))
        pending: list[StepPlan] = []
        for step in steps:
            if self.store.has_effect(step.idempotency_key):
                outcome.skipped_steps.append(step.idempotency_key)
                continue
            if step.step_index < start_from:
                # current_step 已越过但 effect 缺失：说明该步既没落 effect 也没完成，
                # 恢复期一律不补跑（补跑才是真正的重复副作用风险）。
                outcome.skipped_steps.append(step.idempotency_key)
                continue
            pending.append(step)

        if not pending:
            # 所有 step 都有 effect → 直接收敛为成功，不执行任何东西。
            self.store.transition_run(run_id, RUN_SUCCEEDED, step_index=len(steps), updated_at=stamp)
            for key in outcome.skipped_steps:
                self.audit.record(
                    "step_already_done",
                    message_id=message_id,
                    detail={"run_id": run_id, "idempotency_key": key, "resumed": False},
                    created_at=stamp,
                )
            self.audit.record(
                "run_resumed",
                message_id=message_id,
                detail={
                    "run_id": run_id,
                    "run_status": RUN_SUCCEEDED,
                    "resumed": False,
                    "skipped": len(outcome.skipped_steps),
                    "executed": False,
                    "from": previous,
                    "to": RUN_SUCCEEDED,
                },
                created_at=stamp,
            )
            outcome.action = "recomplete"
            outcome.final_status = RUN_SUCCEEDED
            return outcome

        # 第 3 步：没有 effect 的 step，只有幂等安全才允许恢复。
        reconfirm = [step for step in pending if step.needs_reconfirmation]
        resumable = [step for step in pending if not step.needs_reconfirmation]
        outcome.reconfirm_steps = [step.idempotency_key for step in reconfirm]

        if reconfirm:
            # 不可逆副作用：**不执行**，转到 NEEDS_UNLOCK 等主人重新确认。
            first = reconfirm[0]
            self.store.transition_run(
                run_id,
                RUN_NEEDS_UNLOCK,
                step_index=first.step_index,
                error_code="NEEDS_UNLOCK",
                updated_at=stamp,
            )
            self.audit.record(
                "run_recovery_reconfirm",
                message_id=message_id,
                detail={
                    "run_id": run_id,
                    "command": first.command,
                    "step_index": first.step_index,
                    "requires_reconfirmation": True,
                    "executed": False,
                    "run_status": RUN_NEEDS_UNLOCK,
                    "from": previous,
                    "to": RUN_NEEDS_UNLOCK,
                    "error_code": "NEEDS_UNLOCK",
                },
                created_at=stamp,
            )
            outcome.action = "reconfirm"
            outcome.final_status = RUN_NEEDS_UNLOCK
            outcome.next_step = first
            return outcome

        # 全部幂等安全：可以继续。真正执行由注入的 executor 完成。
        first = resumable[0]
        outcome.next_step = first
        if self._executor is not None:
            for step in resumable:
                self._execute_step(step, message_id=message_id, now=stamp, outcome=outcome)
                if outcome.final_status == RUN_FAILED:
                    break
        else:
            # 没有执行器时恢复只做「记账与状态推进」，**不能假装执行过**。
            outcome.action = "resume"
            outcome.final_status = RUN_RUNNING

        if outcome.final_status != RUN_FAILED:
            if all(self.store.has_effect(step.idempotency_key) for step in steps):
                latest = self.store.run(run_id) or {}
                from_status = str(latest.get("status", RUN_RUNNING))
                allowed = from_status in (RUN_RUNNING, RUN_WAITING_DEVICE, RUN_NEEDS_UNLOCK)
                if allowed:
                    self.store.transition_run(run_id, RUN_SUCCEEDED, step_index=len(steps), updated_at=stamp)
                    outcome.final_status = RUN_SUCCEEDED
                    outcome.action = "resume" if outcome.resumed_steps else outcome.action
            self.audit.record(
                "run_resumed",
                message_id=message_id,
                detail={
                    "run_id": run_id,
                    "run_status": outcome.final_status,
                    "resumed": bool(outcome.resumed_steps),
                    "executed": bool(outcome.resumed_steps),
                    "skipped": len(outcome.skipped_steps),
                    "from": previous,
                    "to": outcome.final_status,
                },
                created_at=stamp,
            )
        return outcome

    def _execute_step(self, step: StepPlan, *, message_id: str, now: int, outcome: RunRecovery) -> None:
        """执行一个已判定为幂等安全的 step，并把结果写成 effect（不存在才写）。"""
        assert self._executor is not None
        # 执行前再查一次：并发恢复或旧进程可能刚写过。
        if self.store.has_effect(step.idempotency_key):
            outcome.skipped_steps.append(step.idempotency_key)
            return
        response = self._executor(step)
        status = str(getattr(response, "status", "failed"))
        error_code = str(getattr(response, "error_code", "") or "")
        result_payload = _response_payload(response)
        # ``add_effect``：不存在才写。重放的旧进程结果不会覆盖本次结果。
        self.store.add_effect(step.idempotency_key, result_payload, now)
        outcome.resumed_steps.append(step.idempotency_key)
        self.store.advance_step(step.run_id, step.step_index, now)
        if status != "ok":
            self.store.transition_run(
                step.run_id,
                RUN_FAILED,
                step_index=step.step_index,
                error_code=error_code or "ACTION_REJECTED",
                updated_at=now,
            )
            outcome.final_status = RUN_FAILED
            outcome.action = "failed"


def _response_payload(response: Any) -> str:
    """把设备响应收敛成可落库的 JSON 文本；不含任何本地路径。"""
    import json

    if hasattr(response, "as_dict"):
        data: Any = response.as_dict()
    elif isinstance(response, Mapping):
        data = dict(response)
    else:
        data = {"status": "unknown"}
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 设备侧恢复（主机视角）：已落 effect 就绝不重发
# ---------------------------------------------------------------------------


class DeviceRecoveryView:
    """设备进程被杀之后的**主机侧**判定。

    设备端 ``processed_effects`` 表是真相来源；主机无法直接读它，所以设备重连后必须
    先上报「我已经处理过哪些幂等键」。本视图据此决定哪些 step 跳过、哪些可以补发。

    规则与主机侧完全对称：**有 effect → 不重发；无 effect 且幂等安全 → 可补发；
    不可逆 → 重新确认。**
    """

    def __init__(self, store: TaskStore, *, audit: AuditLogger | None = None, clock: Callable[[], int] | None = None) -> None:
        self.store = store
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.audit = audit or AuditLogger(store, clock=self.clock)

    def reconcile(
        self,
        run_id: str,
        device_processed_keys: list[str],
        *,
        device_id: str = "",
        counter: int | None = None,
    ) -> dict[str, Any]:
        """把设备上报的已处理键与主机记录对账。

        单调性保证：``record_device_counter`` 只会把计数器往上推，**不回退**。
        """
        stamp = self.clock()
        run = self.store.run(run_id)
        if run is None:
            raise RecoveryError(f"unknown run: {run_id!r}")
        message_id = str(run["message_id"])
        processed = {str(key) for key in device_processed_keys}

        skipped: list[str] = []
        replayed: list[str] = []
        reconfirm: list[str] = []
        for step in plan_steps(run):
            already = step.idempotency_key in processed
            if already:
                skipped.append(step.idempotency_key)
                if self.store.has_effect(step.idempotency_key):
                    replayed.append(step.idempotency_key)
            elif step.needs_reconfirmation:
                reconfirm.append(step.idempotency_key)

        before = self.store.device_counter(device_id) if device_id else 0
        after = self.store.record_device_counter(device_id, int(counter), stamp) if device_id and counter is not None else before
        if device_id and counter is not None and after != before:
            self.audit.record(
                "device_counter_advanced",
                message_id=message_id,
                detail={
                    "device_id": device_id,
                    "step_index": before,
                    "count": after,
                    "resumed": True,
                },
                created_at=stamp,
            )
        self.audit.record(
            "device_recovered",
            message_id=message_id,
            detail={
                "run_id": run_id,
                "device_id": device_id,
                "skipped": len(skipped),
                "executed": False,
                "requires_reconfirmation": bool(reconfirm),
            },
            created_at=stamp,
        )
        return {
            "run_id": run_id,
            "skipped": sorted(skipped),
            "replayed": sorted(replayed),
            "reconfirm": sorted(reconfirm),
            "counter": after,
            "no_repeat_execution": bool(skipped) and not reconfirm,
        }
