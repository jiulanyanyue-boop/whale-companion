"""M10 持久 worker：十步保护顺序、状态机、设备等待与补执行。

规范来源：``docs/modules/M10-scheduler-development.md``「保护顺序」「Worker」「禁止项」。

**保护顺序是本模块的全部意义所在**（十步，顺序固定，一步都不能提前）::

    ① 启用状态  → ② 时间窗口 → ③ 安静时段 → ④ 黑名单 → ⑤ 每日上限
    → ⑥ 冷却 → ⑦ 设备在线 → ⑧ 模型 → ⑨ 媒体大小 → ⑩ OneBot 发送

其中最关键的一条：**⑧ 模型必须排在 ①–⑦ 全部通过之后**。
:data:`GUARD_ORDER` 是这条纪律的机读形式，测试直接对它断言，
任何人在 :meth:`ScheduleWorker._run_guards` 里插队都会被抓到。

其余硬约束：

- **不做进程内 Timer 作为唯一调度状态**：状态全在 ``schedules`` 表里，
  进程重启后 :meth:`ScheduleWorker.start` 从 ``DUE`` / ``WAIT_DEVICE`` 恢复。
- **租约是数据库级的**：复用 M12 的「条件 UPDATE + MAX 只延长不回退」思路，
  不是内存锁；两个 worker 同时跑只有一个能拿到。
- **原子更新**：发送成功后 ``next_run_ms`` / 今日计数 / ``last_effect_key``
  由 :meth:`ScheduleStore.record_send` 的单条 UPDATE + 单事务写出。
- **AI 返回 SILENT 是正常结果**：不产生空消息、不发送。
- **设备离线只等待，不轮询截图**；恢复在线后**只补执行一次**。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .scheduler import (
    SCHEDULE_LEASE_MS,
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
    ScheduleConfigError,
    ScheduleRow,
    ScheduleSpec,
    ScheduleStore,
    in_quiet_hours,
    local_day_key,
    next_occurrence_ms,
)


#: 十步保护顺序的机读形式。序列化到日志/审计时可逐条比对。
GUARD_ORDER: tuple[str, ...] = (
    "enabled",
    "time_window",
    "quiet_hours",
    "blocklist",
    "daily_limit",
    "cooldown",
    "device_online",
    "model",
    "media_size",
    "onebot_send",
)

#: ① ~ ⑦ 是 guard；⑧ 模型必须严格排在这些之后。
GUARD_STEPS: frozenset[str] = frozenset(GUARD_ORDER[:7])

#: 单个媒体体积上限默认 8 MiB（与 M08 的 100 MiB 是两回事：主动问候不推大文件）。
DEFAULT_MAX_MEDIA_BYTES = 8 * 1024 * 1024

#: 设备离线时最多等待多少轮；超过即 EXPIRED，绝不无限等。
DEFAULT_MAX_WAIT_RESUMES = 1


class ScheduleWorkerError(RuntimeError):
    pass


@dataclass
class GuardResult:
    """单步守卫的结果。``step`` 是 :data:`GUARD_ORDER` 里的名字。"""

    step: str
    passed: bool
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"step": self.step, "passed": self.passed, "reason": self.reason}


@dataclass
class TickResult:
    """一条调度一次 tick 的完整决定。"""

    schedule_id: str
    state: str
    sent: bool = False
    reason: str = ""
    guards: list[GuardResult] = field(default_factory=list)
    model_calls: int = 0
    effect_key: str = ""
    next_run_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "schedule_id": self.schedule_id,
            "state": self.state,
            "sent": self.sent,
            "reason": self.reason,
            "guards": [item.as_dict() for item in self.guards],
            "model_calls": self.model_calls,
            "effect_key": self.effect_key,
            "next_run_ms": self.next_run_ms,
        }


@dataclass
class ModelOutcome:
    """模型决策的规范化结果。``mode`` 只认 REPLY / SILENT。"""

    mode: str
    text: str = ""
    media: list[dict[str, Any]] = field(default_factory=list)

    @property
    def is_silent(self) -> bool:
        return self.mode == "SILENT"

    @property
    def is_reply(self) -> bool:
        return self.mode == "REPLY"


def parse_schedule_decision(raw: Any) -> ModelOutcome:
    """解析模型输出，**只接受 REPLY / SILENT**。

    定时任务语境下模型无权产出 DEVICE_TASK（主动问候不得操作设备），
    因此这里比 M02 更严：出现任何别的 mode 直接失败。
    解析失败一律抛 :class:`ScheduleWorkerError`，不做修复、不猜字段。
    """
    if isinstance(raw, Mapping):
        data: Any = dict(raw)
    elif isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ScheduleWorkerError("schedule decision must be JSON") from exc
    else:
        raise ScheduleWorkerError("schedule decision must be text or an object")
    if not isinstance(data, dict):
        raise ScheduleWorkerError("schedule decision must be an object")
    mode = data.get("mode")
    if mode not in {"REPLY", "SILENT"}:
        # DEVICE_TASK 在主动问候里是越权：模型不能借定时任务操作设备。
        raise ScheduleWorkerError("schedule decision mode must be REPLY or SILENT")
    text = data.get("text", "")
    media = data.get("media", [])
    if not isinstance(text, str):
        raise ScheduleWorkerError("schedule decision text must be a string")
    if not isinstance(media, list):
        raise ScheduleWorkerError("schedule decision media must be a list")
    cleaned: list[dict[str, Any]] = []
    for item in media:
        if not isinstance(item, Mapping):
            raise ScheduleWorkerError("schedule media item must be an object")
        cleaned.append({str(key): value for key, value in item.items()})
    if mode == "SILENT" and (text or cleaned):
        # SILENT 不得夹带内容：静默就是静默，不能"顺手"发一条。
        raise ScheduleWorkerError("SILENT decision must not carry text or media")
    return ModelOutcome(mode=str(mode), text=text, media=cleaned)


def _media_bytes(item: Mapping[str, Any]) -> int:
    value = item.get("size", item.get("bytes", 0))
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


class ScheduleWorker:
    """持久 worker。所有外部依赖都可注入，测试里不出现真实等待。

    注入点：
    - ``clock``：返回当前 UTC epoch ms。
    - ``model_decider``：``(schedule_row, prompt_context) -> raw``。
    - ``sender``：``(user_id, text, media) -> Any``（实际是 OneBot 发送）。
    - ``device_online``：``() -> bool``。
    - ``blocklisted``：``(user_id) -> bool``。
    - ``sleep``：保留注入位，供宿主实现常驻循环；测试传 no-op，绝不真睡。
    """

    def __init__(
        self,
        store: ScheduleStore,
        *,
        model_decider: Callable[[ScheduleRow, dict[str, Any]], Any] | None = None,
        sender: Callable[[str, str, list[dict[str, Any]]], Any] | None = None,
        device_online: Callable[[], bool] | None = None,
        blocklisted: Callable[[str], bool] | None = None,
        clock: Callable[[], int] | None = None,
        sleep: Callable[[float], None] | None = None,
        target_user_id: str = "",
        max_media_bytes: int = DEFAULT_MAX_MEDIA_BYTES,
        lease_ms: int = SCHEDULE_LEASE_MS,
        max_wait_resumes: int = DEFAULT_MAX_WAIT_RESUMES,
        tz_resolver: Callable[[str], Any] | None = None,
    ) -> None:
        self.store = store
        self._decider = model_decider
        self._sender = sender
        self._device_online = device_online
        self._blocklisted = blocklisted
        self.clock = clock or (lambda: int(time.time() * 1000))
        self.sleep = sleep or (lambda _seconds: None)
        self.target_user_id = target_user_id
        self.max_media_bytes = int(max_media_bytes)
        self.lease_ms = max(1, int(lease_ms))
        self.max_wait_resumes = max(0, int(max_wait_resumes))
        self.tz_resolver = tz_resolver

    # -----------------------------------------------------------------
    # 入口
    # -----------------------------------------------------------------
    def start(self) -> dict[str, Any]:
        """启动恢复：从 ``DUE`` / ``WAIT_DEVICE`` 重新捡起未完成的调度。

        不建任何进程内计时器作为真相来源；状态只在 ``schedules`` 表里。
        """
        now = self.clock()
        resumed = []
        for row in self.store.rows_in_states(frozenset({S_WAIT_DEVICE})):
            outcome = self.resume_waiting(row)
            resumed.append(outcome.as_dict())
        return {"resumed_waiting": resumed, "now_ms": now}

    def tick(self) -> list[TickResult]:
        """一次轮询：把所有**已到点**（``DUE`` 且 ``next_run_ms <= now``）的调度处理一遍。

        状态是 ``DUE`` 但 ``next_run_ms`` 还在未来的调度，只是"排队中"，
        不能算到点——否则每轮都会白跑一遍守卫。
        """
        now = self.clock()
        results: list[TickResult] = []
        for row in self.store.rows_in_states(frozenset({S_DUE})):
            if int(row.spec.get("next_run_ms", 0)) > now:
                continue
            results.append(self.run_due(row, now=now))
        return results

    def run_due(self, row: ScheduleRow, *, now: int | None = None) -> TickResult:
        """处理一条到点的调度，严格按十步顺序。"""
        now = self.clock() if now is None else int(now)
        spec = row.spec_obj
        result = TickResult(schedule_id=row.schedule_id, state=row.state)

        # 租约：抢不到说明另一个 worker 正在处理这条，直接放弃本轮（不报错）。
        if not self.store.acquire_lease(row.schedule_id, now, self.lease_ms):
            result.reason = "lease_held"
            result.state = row.state
            return result

        try:
            return self._run_locked(row, spec, result, now)
        finally:
            # 无论成败都放掉租约，避免一条坏调度把整条时间线堵死 60 秒。
            self.store.release_lease(row.schedule_id, self.clock())

    def _run_locked(self, row: ScheduleRow, spec: ScheduleSpec, result: TickResult, now: int) -> TickResult:
        # ① ~ ⑦：守卫。**任何一步失败都在调用模型之前返回。**
        guard = self._run_guards(row, spec, now)
        result.guards = guard
        failed = next((item for item in guard if not item.passed), None)
        if failed is not None:
            return self._handle_guard_failure(row, spec, result, failed, now)

        # ⑧ 模型
        result.state = S_MODEL_DECISION
        self.store.set_state(row.schedule_id, S_MODEL_DECISION, now)
        outcome = self._decide(row, spec)
        result.model_calls = 1
        # 把 model 也记进 guards：这样 result.guards 的顺序就是完整的十步流水账，
        # 审计与测试都能逐条比对 GUARD_ORDER，看有没有人插队。
        result.guards.append(GuardResult("model", True, outcome.mode.lower()))

        if outcome.is_silent:
            # SILENT 是正常结果：不产生空消息，只重排下一次。
            result.state = S_SILENT
            result.reason = "model_silent"
            nxt = next_occurrence_ms(spec, now)
            self.store.reschedule(row.schedule_id, nxt, now, state=S_DUE)
            result.next_run_ms = nxt
            return result

        # ⑨ 媒体大小
        media = outcome.media
        oversized = self._oversized_media(media)
        if oversized is not None:
            result.state = S_FAILED
            result.reason = "media_too_large"
            result.guards.append(GuardResult("media_size", False, "media_too_large", {"index": oversized}))
            nxt = next_occurrence_ms(spec, now)
            self.store.reschedule(row.schedule_id, nxt, now, state=S_DUE)
            result.next_run_ms = nxt
            return result
        result.guards.append(GuardResult("media_size", True))

        # ⑩ OneBot 发送
        effect_key = self._effect_key(row, spec, now)
        if not self._send(row, outcome, effect_key):
            result.state = S_FAILED
            result.reason = "send_failed"
            nxt = next_occurrence_ms(spec, now)
            self.store.reschedule(row.schedule_id, nxt, now, state=S_DUE)
            result.next_run_ms = nxt
            return result

        result.guards.append(GuardResult("onebot_send", True))
        # 原子更新：next_run_ms + 今日计数 + last_effect_key 一次写完。
        nxt = next_occurrence_ms(spec, now)
        local_day = local_day_key(spec, now)
        self.store.record_send(
            row.schedule_id,
            spec=spec,
            next_run_ms=nxt,
            effect_key=effect_key,
            now_ms=now,
            local_day=local_day,
        )
        result.state = S_SEND
        result.sent = True
        result.effect_key = effect_key
        result.next_run_ms = nxt
        return result

    # -----------------------------------------------------------------
    # ①~⑦ 守卫（顺序即 GUARD_ORDER 前七项）
    # -----------------------------------------------------------------
    def _run_guards(self, row: ScheduleRow, spec: ScheduleSpec, now: int) -> list[GuardResult]:
        guards: list[GuardResult] = []

        # ① 启用状态
        enabled = bool(spec.enabled)
        guards.append(GuardResult("enabled", enabled, "" if enabled else "not_enabled"))
        if not enabled:
            self.store.set_state(row.schedule_id, S_DISABLED, now)
            return guards

        # ② 时间窗口：到点太久就不再补发，只重排（避免攒批轰炸）。
        due_at = int(spec.next_run_ms)
        within_window = due_at <= now and (now - due_at) <= WINDOW_TOLERANCE_MS
        guards.append(
            GuardResult(
                "time_window",
                within_window,
                "" if within_window else "outside_time_window",
                {"late_ms": now - due_at},
            )
        )
        if not within_window:
            return guards

        # ③ 安静时段
        quiet = in_quiet_hours(spec, now)
        guards.append(GuardResult("quiet_hours", not quiet, "quiet_hours" if quiet else ""))
        if quiet:
            return guards

        # ④ 黑名单
        blocked = bool(self._blocklisted(self.target_user_id)) if self._blocklisted else False
        guards.append(GuardResult("blocklist", not blocked, "blocklisted" if blocked else ""))
        if blocked:
            return guards

        # ⑤ 每日上限（按配置时区的自然日重新计数）
        day = local_day_key(spec, now)
        count = row.today_count if row.today_day == day else 0
        under_limit = int(spec.daily_limit) > 0 and count < int(spec.daily_limit)
        guards.append(
            GuardResult(
                "daily_limit",
                under_limit,
                "" if under_limit else "daily_limit_reached",
                {"today_count": count, "daily_limit": int(spec.daily_limit)},
            )
        )
        if not under_limit:
            return guards

        # ⑥ 冷却
        since_last = now - int(row.last_sent_at)
        cooldown_ms = int(spec.cooldown_seconds) * 1000
        cooled = int(row.last_sent_at) == 0 or since_last >= cooldown_ms
        guards.append(
            GuardResult(
                "cooldown",
                cooled,
                "" if cooled else "cooldown_active",
                {"since_last_ms": since_last, "cooldown_ms": cooldown_ms},
            )
        )
        if not cooled:
            return guards

        # ⑦ 设备在线（只有需要实时素材时才要求设备在线）
        if spec.needs_live_device:
            online = bool(self._device_online()) if self._device_online else False
            guards.append(
                GuardResult("device_online", online, "" if online else "device_offline")
            )
            if not online:
                return guards
        else:
            guards.append(GuardResult("device_online", True, "not_required"))
        return guards

    def _handle_guard_failure(
        self,
        row: ScheduleRow,
        spec: ScheduleSpec,
        result: TickResult,
        failed: GuardResult,
        now: int,
    ) -> TickResult:
        result.state = S_GUARD_CHECK
        result.reason = failed.reason
        if failed.step == "enabled":
            result.state = S_DISABLED
            return result
        if failed.step == "device_online":
            # 设备离线：进入 WAIT_DEVICE，**不重复建任务**，也不轮询截图。
            self.store.record_wait(row.schedule_id, now)
            result.state = S_WAIT_DEVICE
            return result
        # 其余守卫失败都只是"这轮不发，等下一次"，状态回到 DUE。
        self.store.set_state(row.schedule_id, S_GUARD_CHECK, now)
        result.state = S_GUARD_CHECK
        return result

    # -----------------------------------------------------------------
    # ⑧ 模型
    # -----------------------------------------------------------------
    def _decide(self, row: ScheduleRow, spec: ScheduleSpec) -> ModelOutcome:
        if self._decider is None:
            raise ScheduleWorkerError("no model decider is bound")
        context = {
            "schedule_id": row.schedule_id,
            "kind": spec.kind,
            "timezone": spec.timezone,
            "guard_order": list(GUARD_ORDER),
            # 明确告诉模型：配额、安静时段、黑名单不可由模型输出改变。
            "model_cannot_change": ["daily_limit", "quiet_hours", "blocklist"],
        }
        raw = self._decider(row, context)
        return parse_schedule_decision(raw)

    # -----------------------------------------------------------------
    # ⑨ 媒体
    # -----------------------------------------------------------------
    def _oversized_media(self, media: list[dict[str, Any]]) -> int | None:
        for index, item in enumerate(media):
            if _media_bytes(item) > self.max_media_bytes:
                return index
        return None

    # -----------------------------------------------------------------
    # ⑩ 发送
    # -----------------------------------------------------------------
    def _effect_key(self, row: ScheduleRow, spec: ScheduleSpec, now: int) -> str:
        """固定 effect key：同一条调度在同一触发时刻只可能有一个。"""
        return f"schedule:{row.schedule_id}:{int(spec.next_run_ms)}"

    def _send(self, row: ScheduleRow, outcome: ModelOutcome, effect_key: str) -> bool:
        if self._sender is None:
            raise ScheduleWorkerError("no OneBot sender is bound")
        try:
            self._sender(self.target_user_id, outcome.text, outcome.media)
        except Exception:  # noqa: BLE001 - 发送失败即 FAILED，不在这里重试轰炸
            return False
        del row, effect_key
        return True

    # -----------------------------------------------------------------
    # 设备离线等待与补执行（只补一次）
    # -----------------------------------------------------------------
    def resume_waiting(self, row: ScheduleRow) -> TickResult:
        """设备重新上线后处理 ``WAIT_DEVICE`` 条目：**只补执行一次**。"""
        now = self.clock()
        result = TickResult(schedule_id=row.schedule_id, state=row.state)
        spec = row.spec_obj

        if not self.store.acquire_lease(row.schedule_id, now, self.lease_ms):
            result.reason = "lease_held"
            return result
        try:
            online = bool(self._device_online()) if self._device_online else False
            if not online:
                result.reason = "device_still_offline"
                return result

            if int(row.resume_attempts) > self.max_wait_resumes:
                # 已经补过一次（或超过上限）→ EXPIRED，绝不再补第二次。
                self.store.transition(row.schedule_id, S_EXPIRED, now)
                result.state = S_EXPIRED
                result.reason = "resume_limit_reached"
                nxt = next_occurrence_ms(spec, now)
                self.store.reschedule(row.schedule_id, nxt, now, state=S_DUE)
                result.next_run_ms = nxt
                return result

            # 补执行走完整的十步守卫（不含设备在线这一步的等待语义）。
            # ``run_due`` 会自己抢租约；这里必须先把本层租约放掉，
            # 否则同一条调度会因为"自己占着租约"而判成 lease_held。
            self.store.release_lease(row.schedule_id, now)
            resumed = self.run_due(self.store.row(row.schedule_id) or row, now=now)
            resumed.reason = resumed.reason or "resumed"
            return resumed
        finally:
            self.store.release_lease(row.schedule_id, self.clock())

    # -----------------------------------------------------------------
    # 宿主常驻循环（可选；状态仍以数据库为准）
    # -----------------------------------------------------------------
    def run_forever(self, *, interval_seconds: float = 30.0, max_ticks: int | None = None) -> int:
        """宿主可直接调用；``sleep`` 可注入，测试里传 no-op 且给 ``max_ticks``。"""
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            self.start()
            self.tick()
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            self.sleep(max(0.0, float(interval_seconds)))
        return ticks


def guard_order_is_valid() -> bool:
    """自检：``GUARD_ORDER`` 必须是十步、无重复、模型紧跟守卫之后。"""
    if len(GUARD_ORDER) != 10 or len(set(GUARD_ORDER)) != 10:
        return False
    model_index = GUARD_ORDER.index("model")
    guard_indices = [GUARD_ORDER.index(step) for step in GUARD_ORDER if step in GUARD_STEPS]
    return model_index > max(guard_indices)


def schedule_state_after_guard(step: str) -> str:
    """守卫失败后的状态映射（供审计与测试断言）。"""
    if step == "enabled":
        return S_DISABLED
    if step == "device_online":
        return S_WAIT_DEVICE
    if step == "time_window":
        return S_EXPIRED
    return S_GUARD_CHECK
