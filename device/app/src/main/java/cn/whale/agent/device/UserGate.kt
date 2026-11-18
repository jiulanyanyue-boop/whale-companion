package cn.whale.agent.device

import kotlinx.serialization.json.JsonObject

/**
 * M04 确认状态机与人工接管（纯逻辑：注入时钟与确认卡渲染器，可在 JVM 单测里跑）。
 *
 * 规范来源：`docs/modules/M04-owner-security-product.md`、`M04-owner-security-development.md`。
 *
 * 固定状态：
 * ```
 * READY
 *  → NEED_CONFIRM
 *  → APPROVED_ONCE / APPROVED_TASK / DENIED / STOPPED / TIMEOUT
 * ```
 *
 * 本文件明确不做的事（文档禁止）：
 * - 不实现密码/验证码/支付密码/认证器的自动填写，也不读取用户输入内容；
 * - 不把语音识别结果当身份凭据，不接受「我是主人」这类文本授权；
 * - 不让人设或模型把确认级别放宽——只接受 [ConfirmLevelPolicy.effectiveLevel] 的结果。
 */

/** 确认状态机的固定状态。 */
enum class ConfirmState {
    /** 空闲，没有待确认的请求。 */
    READY,

    /** 确认卡已展示，等待用户点「拒绝 / 仅本次 / 本任务内允许」。 */
    NEED_CONFIRM,

    /** 用户点了「仅本次」（人工接管场景下即「继续」）。 */
    APPROVED_ONCE,

    /** 用户点了「本任务内允许」。 */
    APPROVED_TASK,

    /** 用户点了「拒绝」。 */
    DENIED,

    /** 用户点了「停止」，或任务被中止/模式切换。 */
    STOPPED,

    /** 超时。**默认拒绝，不默认继续**。 */
    TIMEOUT,
    ;

    /** 是否已终态（终态不允许再推进）。 */
    val isTerminal: Boolean get() = this != READY && this != NEED_CONFIRM

    /** 是否允许执行本次命令。 */
    val isApproved: Boolean get() = this == APPROVED_ONCE || this == APPROVED_TASK
}

/** 用户在确认卡上的四种动作；**只有真实点击**才能进入这个类型。 */
enum class ConfirmUserAction {
    /** 拒绝：本次不执行，任务终止。 */
    DENY,

    /** 仅本次（人工接管场景下为「继续」）。 */
    APPROVE_ONCE,

    /** 本任务内允许同一命令。人工接管卡上不提供这个按钮。 */
    APPROVE_TASK,

    /** 停止整个任务。 */
    STOP,
}

/** 一次待确认请求的不可变快照，供 UI 渲染与测试断言。 */
data class PendingConfirmation(
    val requestId: String,
    val command: String,
    /** 载荷摘要：只有字段名或「需要你操作」，不含正文。 */
    val payloadSummary: String,
    val level: ConfirmLevel,
    val openedAtMs: Long,
    /** 截止时间；到点即 [ConfirmState.TIMEOUT]。 */
    val expiresAtMs: Long,
    /** 是否为「需要你操作」的人工接管场景（密码/验证码/支付/安全设置）。 */
    val manualTakeover: Boolean = false,
    /** 确认卡是否可点击。注入手势执行期间必须为 `false`。 */
    val clickable: Boolean = true,
)

/**
 * 确认卡渲染器：**必须**由 Android 原生 UI 或无障碍 overlay 实现。
 *
 * 状态机只调用它，不自己「假装」用户确认；渲染器也不能自行返回确认结果——
 * 用户动作只能通过 [UserGate.onUserAction] 进入。
 */
interface ConfirmCardRenderer {
    /** 展示确认卡（原生视图或 overlay）。 */
    fun show(pending: PendingConfirmation)

    /** 更新可点击性；注入手势执行期间传 `false`，防止 AI 自己点「确认」。 */
    fun setClickable(requestId: String, clickable: Boolean)

    /** 收起确认卡。 */
    fun dismiss(requestId: String, state: ConfirmState)
}

/** 什么都不做的渲染器：纯逻辑测试与尚未接 UI 的阶段的默认值。 */
object NoopConfirmCardRenderer : ConfirmCardRenderer {
    override fun show(pending: PendingConfirmation) = Unit

    override fun setClickable(requestId: String, clickable: Boolean) = Unit

    override fun dismiss(requestId: String, state: ConfirmState) = Unit
}

/**
 * M04 确认状态机（含人工接管）。
 *
 * ---------- 典型调用顺序 ----------
 * ```kotlin
 * val userGate = UserGate(SystemSessionClock, nativeRenderer)
 *
 * when (val decision = userGate.request(requestId, command, summary, requestedLevel, payload, taskId = taskId)) {
 *     UserGate.Decision.Allowed        -> executeNow()
 *     is UserGate.Decision.NeedConfirm -> { /* 等用户真实点击 */ }
 *     is UserGate.Decision.Rejected    -> { /* 一律不执行 */ }
 * }
 * userGate.onUserAction(requestId, ConfirmUserAction.APPROVE_ONCE) // 用户点击
 * userGate.consume(requestId)                                     // 执行完结算
 * ```
 *
 * ---------- 固定规则 ----------
 * 1. 超时默认 [ConfirmState.TIMEOUT]，**绝不默认继续**；
 * 2. 注入手势执行期间 [setInjecting] 传 `true`，确认卡强制不可点击，
 *    AI 无法在注入手势时点自己的确认卡；
 * 3. 只接受「真实用户动作」[ConfirmUserAction]，没有「命令字符串就能授权」的入口；
 * 4. 人工接管（[request] 的 `requiresManualTakeover`）只提供「继续」（[ConfirmUserAction.APPROVE_ONCE]），
 *    不提供「本任务内允许」，且**不读取也不保存任何输入内容**；
 * 5. 语音/远程消息/模型输出都不能推进状态，只能走 [onUserAction]。
 *
 * 线程安全：所有状态变更都在 [lock] 内完成；本地入口通常是单线程，够用。
 */
class UserGate(
    private val clock: SessionClock = SystemSessionClock,
    private val renderer: ConfirmCardRenderer = NoopConfirmCardRenderer,
    /** 单次确认的默认超时；到点即 TIMEOUT。 */
    private val defaultTimeoutMs: Long = DEFAULT_CONFIRM_TIMEOUT_MS,
    /** 时钟回拨容忍度；超过即视为异常并 fail-closed。 */
    private val maxClockSkewMs: Long = MAX_CLOCK_SKEW_MS,
) {
    /** 一次确认请求的内部记录。 */
    private class Record(
        val requestId: String,
        val command: String,
        val payloadSummary: String,
        val level: ConfirmLevel,
        val openedAtMs: Long,
        val expiresAtMs: Long,
        var state: ConfirmState,
        var clickable: Boolean,
        var injecting: Boolean,
        val manualTakeover: Boolean,
        val taskId: String?,
    )

    private val lock = Any()
    private val records = LinkedHashMap<String, Record>()

    /** 任务级批准：taskId → 已经「本任务内允许」的命令集合。 */
    private val taskApprovals = HashMap<String, MutableSet<String>>()
    private var injectingNow = false

    /** 当前确认卡状态（没有记录时是 [ConfirmState.READY]）。 */
    fun state(requestId: String): ConfirmState = synchronized(lock) {
        records[requestId]?.state ?: ConfirmState.READY
    }

    /** 是否正在注入手势（注入期间确认卡一律不可点击）。 */
    fun isInjecting(): Boolean = synchronized(lock) { injectingNow }

    /** 当前是否可点击（注入手势期间恒为 `false`）。 */
    fun isClickable(requestId: String): Boolean = synchronized(lock) {
        val record = records[requestId] ?: return false
        record.clickable && !record.injecting && !injectingNow && record.state == ConfirmState.NEED_CONFIRM
    }

    /** 该请求是否是人工接管（「需要你操作」）。 */
    fun isManualTakeover(requestId: String): Boolean = synchronized(lock) {
        records[requestId]?.manualTakeover == true
    }

    /**
     * 全局注入手势开关（由无障碍注入层在执行手势前后调用）。
     *
     * 打开时把**所有** NEED_CONFIRM 记录设为不可点击，关闭时恢复。
     * 这样即使 AI 在注入过程中「顺手」点一下坐标，也点不到确认卡。
     */
    fun setInjecting(injecting: Boolean) {
        synchronized(lock) {
            injectingNow = injecting
            for (record in records.values) {
                record.injecting = injecting
                if (record.state == ConfirmState.NEED_CONFIRM) {
                    record.clickable = !injecting
                    renderer.setClickable(record.requestId, record.clickable)
                }
            }
        }
    }

    /** 请求结果。 */
    sealed class Decision {
        /** 无需确认，可直接执行本次命令。 */
        data object Allowed : Decision()

        /** 用户拒绝 / 超时 / 停止 / 策略 DENY：绝不执行。 */
        data class Rejected(val state: ConfirmState, val errorCode: String, val message: String) : Decision()

        /** 已展示确认卡（人工接管时为「需要你操作」），等待**用户真实点击**。 */
        data class NeedConfirm(val pending: PendingConfirmation) : Decision()
    }

    /**
     * 提交一次命令的确认请求。
     *
     * @param requestedLevel 应用级/人设级「请求级别」；它**不能**降低固定下限。
     * @param payload 实际 payload，用于 [ConfirmLevelPolicy.floorFor] 的 payload 级细化；
     *   `null` 表示调用方拿不到 payload → fail-closed（按更严的下限处理）。
     * @param requiresManualTakeover 密码/验证码/支付/安全设置 → `true`，卡上显示「需要你操作」。
     * @param taskId 当前任务 id；`APPROVED_TASK` 只在这个任务内生效，任务切换后必须重新确认。
     *   传 `null` 表示没有任务上下文 → 永远不享受任务级批准。
     */
    fun request(
        requestId: String,
        command: String,
        payloadSummary: String,
        requestedLevel: ConfirmLevel,
        payload: JsonObject? = null,
        requiresManualTakeover: Boolean = false,
        taskId: String? = null,
    ): Decision {
        if (requestId.isBlank()) {
            return Decision.Rejected(ConfirmState.DENIED, CommandErrors.INVALID_PAYLOAD, "request id is required")
        }

        // 生效级别：固定下限压住请求级别，人设/模型无法放宽。
        val level = ConfirmLevelPolicy.effectiveLevel(requestedLevel, command, payload)

        synchronized(lock) {
            val existing = records[requestId]

            // 固定 DENY：不展示确认卡，也不执行；同时关掉可能还挂着的旧卡。
            if (level == ConfirmLevel.DENY) {
                if (existing != null && existing.state == ConfirmState.NEED_CONFIRM) finish(existing, ConfirmState.DENIED)
                records[requestId] = newRecord(
                    requestId = requestId,
                    command = command,
                    payloadSummary = payloadSummary,
                    level = level,
                    state = ConfirmState.DENIED,
                    clickable = false,
                )
                return Decision.Rejected(ConfirmState.DENIED, CommandErrors.ACTION_REJECTED, "command is denied by policy")
            }

            // 无需确认：直接放行本次命令；旧卡一并收起。
            if (level == ConfirmLevel.ALLOW_NORMAL) {
                if (existing != null && existing.state == ConfirmState.NEED_CONFIRM) {
                    existing.state = ConfirmState.READY
                    existing.clickable = false
                    renderer.setClickable(requestId, false)
                    renderer.dismiss(requestId, ConfirmState.READY)
                    records.remove(requestId)
                }
                return Decision.Allowed
            }

            // 同一请求已在等待：直接返回当前卡，**不重置**超时。
            if (existing != null && existing.state == ConfirmState.NEED_CONFIRM) {
                return Decision.NeedConfirm(existing.toPending())
            }

            val manual = requiresManualTakeover

            // 首步确认：本任务已批准过同一命令，且不是人工接管 → 直接放行，不再重复弹卡。
            val taskApproved = !manual && taskId != null && taskApprovals[taskId]?.contains(command) == true
            if (level == ConfirmLevel.CONFIRM_FIRST_STEP && taskApproved) return Decision.Allowed

            // 「仅本次」批准只放行一次：新请求必须重新确认。
            if (existing != null && existing.state == ConfirmState.APPROVED_ONCE) {
                existing.state = ConfirmState.READY
            }

            val now = clock.nowMs()
            val record = newRecord(
                requestId = requestId,
                command = command,
                payloadSummary = if (manual) MANUAL_TAKEOVER_SUMMARY else payloadSummary,
                level = level,
                state = ConfirmState.NEED_CONFIRM,
                clickable = true,
                openedAtMs = now,
                expiresAtMs = now + defaultTimeoutMs,
                manualTakeover = manual,
                taskId = taskId,
            )
            records[requestId] = record
            val pending = record.toPending()
            renderer.show(pending)
            renderer.setClickable(requestId, pending.clickable)
            return Decision.NeedConfirm(pending)
        }
    }

    /**
     * **真实用户动作**的唯一入口（原生 UI 的按钮回调）。
     *
     * 非用户来源（语音、远程消息、模型输出）不得调用本方法。
     *
     * @return 动作落地后的状态；超时 / 注入手势期间 / 未知请求都返回拒绝类状态。
     */
    fun onUserAction(requestId: String, action: ConfirmUserAction): ConfirmState = synchronized(lock) {
        val record = records[requestId] ?: return ConfirmState.DENIED

        // 超时优先：到点后用户再点也不算数（默认拒绝，不默认继续）。
        if (isTimedOut(record)) {
            finish(record, ConfirmState.TIMEOUT)
            return ConfirmState.TIMEOUT
        }

        // 注入手势期间确认卡不可点击：任何「点击」都必须被丢弃，防止 AI 自己点确认。
        if (injectingNow || record.injecting) return record.state

        // 已终态 / 非等待态：不再改变。
        if (record.state != ConfirmState.NEED_CONFIRM) return record.state

        when (action) {
            ConfirmUserAction.DENY -> finish(record, ConfirmState.DENIED)
            ConfirmUserAction.STOP -> finish(record, ConfirmState.STOPPED)
            ConfirmUserAction.APPROVE_ONCE -> finish(record, ConfirmState.APPROVED_ONCE)
            ConfirmUserAction.APPROVE_TASK -> {
                // 人工接管卡只有「继续」，没有「本任务内允许」。
                if (record.manualTakeover) return record.state
                finish(record, ConfirmState.APPROVED_TASK)
                val task = record.taskId
                if (task != null) taskApprovals.getOrPut(task) { LinkedHashSet() }.add(record.command)
            }
        }
        record.state
    }

    /**
     * 结算一次请求：调用方执行完命令后必须调用，避免 APPROVED_ONCE 被复用。
     *
     * [ConfirmLevel.CONFIRM_EVERY_STEP] 的场景执行完立即回到 READY，下一步要重新确认。
     *
     * @return 结算前的状态。
     */
    fun consume(requestId: String): ConfirmState = synchronized(lock) {
        val record = records[requestId] ?: return ConfirmState.READY
        val before = record.state
        when (before) {
            ConfirmState.APPROVED_ONCE -> record.state = ConfirmState.READY
            ConfirmState.APPROVED_TASK -> {
                if (record.level == ConfirmLevel.CONFIRM_EVERY_STEP) record.state = ConfirmState.READY
            }
            else -> Unit
        }
        record.clickable = false
        before
    }

    /** 超时结算；尚未超时则返回原状态。 */
    fun checkTimeout(requestId: String): ConfirmState = synchronized(lock) {
        val record = records[requestId] ?: return ConfirmState.READY
        if (record.state == ConfirmState.NEED_CONFIRM && isTimedOut(record)) {
            finish(record, ConfirmState.TIMEOUT)
        }
        record.state
    }

    /** 结算所有到点请求（入口每轮调用一次即可）；返回被超时的 requestId。 */
    fun checkAllTimeouts(): List<String> = synchronized(lock) {
        val expired = ArrayList<String>()
        for (record in records.values) {
            if (record.state == ConfirmState.NEED_CONFIRM && isTimedOut(record)) {
                finish(record, ConfirmState.TIMEOUT)
                expired.add(record.requestId)
            }
        }
        expired
    }

    /** 结束一个任务：该任务的「本任务内允许」全部失效。 */
    fun endTask(taskId: String) {
        synchronized(lock) { taskApprovals.remove(taskId) }
    }

    /** 停止全部待确认请求（模式切换/任务中止）：一律 STOPPED，任务级批准一并清空。 */
    fun stopAll(): Int = synchronized(lock) {
        var count = 0
        for (record in records.values) {
            if (record.state == ConfirmState.NEED_CONFIRM) {
                finish(record, ConfirmState.STOPPED)
                count++
            }
        }
        injectingNow = false
        taskApprovals.clear()
        count
    }

    /** 当前待确认快照（测试与渲染重放用）。 */
    fun pending(requestId: String): PendingConfirmation? = synchronized(lock) {
        records[requestId]?.takeIf { it.state == ConfirmState.NEED_CONFIRM }?.toPending()
    }

    private fun newRecord(
        requestId: String,
        command: String,
        payloadSummary: String,
        level: ConfirmLevel,
        state: ConfirmState,
        clickable: Boolean = true,
        openedAtMs: Long = clock.nowMs(),
        expiresAtMs: Long = clock.nowMs() + defaultTimeoutMs,
        manualTakeover: Boolean = false,
        taskId: String? = null,
    ): Record = Record(
        requestId = requestId,
        command = command,
        payloadSummary = payloadSummary,
        level = level,
        openedAtMs = openedAtMs,
        expiresAtMs = expiresAtMs,
        state = state,
        clickable = clickable && !injectingNow,
        injecting = injectingNow,
        manualTakeover = manualTakeover,
        taskId = taskId,
    )

    private fun isTimedOut(record: Record): Boolean {
        val now = clock.nowMs()
        // 时钟回拨：视为异常，直接 fail-closed（宁可超时也不放宽）。
        if (now < record.openedAtMs - maxClockSkewMs) return true
        return now >= record.expiresAtMs
    }

    private fun finish(record: Record, state: ConfirmState) {
        record.state = state
        record.clickable = false
        renderer.setClickable(record.requestId, false)
        renderer.dismiss(record.requestId, state)
    }

    private fun Record.toPending(): PendingConfirmation = PendingConfirmation(
        requestId = requestId,
        command = command,
        payloadSummary = payloadSummary,
        level = level,
        openedAtMs = openedAtMs,
        expiresAtMs = expiresAtMs,
        manualTakeover = manualTakeover,
        clickable = clickable && state == ConfirmState.NEED_CONFIRM && !injectingNow && !injecting,
    )

    companion object {
        /** 单次确认默认 60 秒；到点 TIMEOUT。 */
        const val DEFAULT_CONFIRM_TIMEOUT_MS: Long = 60_000L

        /** 时钟回拨容忍度。 */
        const val MAX_CLOCK_SKEW_MS: Long = 5_000L

        /** 人工接管卡上只显示这句提示，不搬运任何输入内容。 */
        const val MANUAL_TAKEOVER_SUMMARY: String = "需要你操作"
    }
}

/**
 * M04 提供的 [RiskConfirmation] 生产实现，供 Lead 注入 M01 的 [LocalModeGate]。
 *
 * M01 调用点只有 `confirm(command, payloadSummary): Boolean` 且**同步**返回，
 * 而真实产品里确认必须异步等用户点击。因此拆成两层：
 *
 * - [UserGate] 展示确认卡、等待用户点击、处理超时（异步、可测）；
 * - [OwnerRiskConfirmation] 只拿得到命令名与载荷摘要（**拿不到 payload**），
 *   于是按命令级 fail-closed：五条高风险命令必须「已有该命令的批准」才返回 `true`，
 *   否则 `false` → M01 返回 `ACTION_REJECTED`（沿用已登记错误码，不新增）。
 *
 * 生产接线：先在本地入口用 [UserGate.request] 收集用户批准，再把本实例注入 M01
 * 作为最后一道「有批准才放行」的守卫；[setInjecting] 转给 [UserGate]，
 * 保证注入手势期间确认卡不可点击、也不借用旧批准放行。
 */
class OwnerRiskConfirmation(
    private val userGate: UserGate,
) : RiskConfirmation {
    /** 命令 → 任务级批准。 */
    private val taskApprovals = HashMap<String, String>()

    /** 命令 → 剩余「仅本次」批准次数。 */
    private val onceApprovals = HashMap<String, Int>()

    private val lock = Any()

    /** 用户完成确认后登记批准：`taskId` 为空表示仅本次。 */
    fun grant(command: String, taskId: String? = null) {
        synchronized(lock) {
            if (taskId.isNullOrBlank()) {
                onceApprovals[command] = (onceApprovals[command] ?: 0) + 1
            } else {
                taskApprovals[command] = taskId
            }
        }
    }

    /** 任务结束/模式切换时清空全部批准。 */
    fun clearApprovals() {
        synchronized(lock) {
            taskApprovals.clear()
            onceApprovals.clear()
        }
    }

    /** 转给 [UserGate]：注入手势期间确认卡不可点击。 */
    fun setInjecting(injecting: Boolean) = userGate.setInjecting(injecting)

    override fun confirm(command: String, payloadSummary: String): Boolean {
        // 非高风险命令不需要确认（普通命令走 ALLOW_NORMAL，不经过这里）。
        if (!ConfirmLevelPolicy.isHighRisk(command)) return true

        // 注入手势期间一律不放行，且**不消费**批准（否则用户批准会被 AI 借走一次）。
        if (userGate.isInjecting()) return false

        return synchronized(lock) {
            if (taskApprovals.containsKey(command)) {
                true
            } else {
                val left = onceApprovals[command] ?: 0
                when {
                    left <= 0 -> false
                    left == 1 -> {
                        onceApprovals.remove(command)
                        true
                    }
                    else -> {
                        onceApprovals[command] = left - 1
                        true
                    }
                }
            }
        }
    }
}
