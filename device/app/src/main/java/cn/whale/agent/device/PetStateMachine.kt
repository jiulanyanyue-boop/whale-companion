package cn.whale.agent.device

/**
 * M09 桌宠状态机（**纯 JVM，零 Android 依赖**）。
 *
 * 规范来源：`docs/modules/M09-local-pet-voice-development.md`「宠物状态机」。
 *
 * 状态集合（**固定 8 个**，禁止增删）：
 * ```
 * HIDDEN / IDLE / WORKING / WAITING_CONFIRM / WAITING_USER / SUCCESS / ERROR / OFFLINE
 * ```
 * 历史描述曾误称「七态」；文档已明确以列举为准、共 8 个。任何改动都必须同时改文档该节、
 * [PetState] 枚举与测试 `petStateSetIsFixed`。
 *
 * 状态**只来自 [PetEvent]**，事件名（wire 形式）固定为：
 * ```
 * task_started / task_progress / confirm_required / user_required
 * task_succeeded / task_failed / task_stopped / device_offline
 * ```
 * 唯一的例外是窗口生命周期入口 [show]、[hide] 与定时回落 [refresh]：
 * - [show] / [hide] 描述「桌宠窗口是否显示」，不是任务状态来源，因此不参与任务语义；
 * - [refresh] 只把 [PetState.SUCCESS] / [PetState.ERROR] 的短暂停留态回落到 [PetState.IDLE]，
 *   不产生任何新状态，也不会把 [PetState.OFFLINE] 之类的判定态改回在线。
 *
 * 注意：本类不驱动动画，不持有定时器；回落靠宿主以**低频**（≥1s）调用 [refresh] 完成，
 * 满足「禁止为动画引入持续高频定时器」。
 */
fun interface PetClock {
    fun nowMs(): Long
}

/** 桌宠状态（固定 8 个，禁止增删）。 */
enum class PetState {
    /** 窗口未显示（尚未打开、用户当天关闭显示、或权限退化到应用内桌宠）。 */
    HIDDEN,

    /** 空闲：在线但没有任务。 */
    IDLE,

    /** 任务执行中。 */
    WORKING,

    /** 等待高风险确认（必须保持清晰可见）。 */
    WAITING_CONFIRM,

    /** 等待用户回答（缺参数、需要选择）。 */
    WAITING_USER,

    /** 任务成功。 */
    SUCCESS,

    /** 任务失败。 */
    ERROR,

    /** 设备离线（主机断开或设备不可用）。 */
    OFFLINE,
    ;

    /** 是否需要在屏幕上显示。 */
    val isVisible: Boolean get() = this != HIDDEN
}

/**
 * 固定 [PetEvent] 集合。
 *
 * [wireName] 是跨模块（M06 任务运行时 → 桌宠）唯一认可的事件名，固定为小写下划线形式；
 * 宿主只允许用这些字符串驱动桌宠，未知事件名一律被拒绝，不会改变状态。
 */
enum class PetEvent(val wireName: String) {
    TASK_STARTED("task_started"),
    TASK_PROGRESS("task_progress"),
    CONFIRM_REQUIRED("confirm_required"),
    USER_REQUIRED("user_required"),
    TASK_SUCCEEDED("task_succeeded"),
    TASK_FAILED("task_failed"),
    TASK_STOPPED("task_stopped"),
    DEVICE_OFFLINE("device_offline"),
    ;

    companion object {
        /** 固定 wire 事件名集合（用于宿主侧校验与单测断言）。 */
        val WIRE_NAMES: Set<String> = entries.map { it.wireName }.toSet()

        /** wire 名 → 事件；未知返回 `null`（拒绝，不改变状态）。 */
        fun fromWire(wireName: String): PetEvent? {
            val normalized = wireName.trim()
            return entries.firstOrNull { it.wireName == normalized }
        }
    }
}

/** 桌宠对外快照：状态 + 最近事件，用于渲染与本地日志（不包含任何消息正文）。 */
data class PetSnapshot(
    val state: PetState,
    val lastEvent: PetEvent? = null,
    val generation: Long = 0L,
)

/**
 * 桌宠状态机。
 *
 * @param clock 可注入时钟（生产用 `SystemClock.elapsedRealtime`，单测注入假时钟）。
 * @param successHoldMs [PetState.SUCCESS] 的停留时长，到期回落到 [PetState.IDLE]。
 * @param errorHoldMs [PetState.ERROR] 的停留时长，到期回落到 [PetState.IDLE]。
 */
class PetStateMachine(
    private val clock: PetClock,
    private val successHoldMs: Long = DEFAULT_SUCCESS_HOLD_MS,
    private val errorHoldMs: Long = DEFAULT_ERROR_HOLD_MS,
) {
    @Volatile
    var state: PetState = PetState.HIDDEN
        private set

    @Volatile
    var lastEvent: PetEvent? = null
        private set

    /** 窗口隐藏期间收到的事件目标态；`show()` 时恢复，避免隐藏期间丢任务状态。 */
    private var pendingFromHidden: PetState? = null

    /** [PetState.SUCCESS] / [PetState.ERROR] 进入时刻；用于低频 [refresh] 计算回落。 */
    private var stampedAtMs: Long = 0L

    /** 是否有有效时间戳；用独立标志而不是「时间戳 > 0」，避免开机瞬间 `elapsedRealtime()==0` 永不回落。 */
    private var hasStamp: Boolean = false

    /** 是否有可见状态（不代表窗口是否真的加成功，那个由 `PetWindowOwner.overlayOnline` 决定）。 */
    val isVisible: Boolean get() = state.isVisible

    /**
     * 唯一的事件入口。
     *
     * 隐藏态下事件**不改变可见状态**（桌宠没显示，事件不该把它拉回来），只把目标态记为
     * [pendingFromHidden]，等下次 `show()` 恢复；[lastEvent] 始终更新，供本地日志与“最近事件”展示。
     *
     * @return 处理后的当前状态。
     */
    fun onEvent(event: PetEvent): PetState {
        lastEvent = event
        val target = targetStateOf(event)
        if (state == PetState.HIDDEN) {
            pendingFromHidden = target
            return state
        }
        apply(target)
        return state
    }

    /**
     * wire 形式的事件入口。
     *
     * @return 新状态；**未知事件名返回 `null` 且不改变任何状态**（桌宠不接受自定义事件）。
     */
    fun onEventName(wireName: String): PetState? {
        val event = PetEvent.fromWire(wireName) ?: return null
        onEvent(event)
        return state
    }

    /**
     * 窗口 UI 可用（`WindowManager.addView` 成功）后的生命周期入口。
     *
     * 若隐藏期间收到过事件，恢复那个目标态；否则进入 [PetState.IDLE]。
     */
    fun show(): PetState {
        if (state != PetState.HIDDEN) return state
        val restored = pendingFromHidden ?: PetState.IDLE
        pendingFromHidden = null
        apply(restored)
        return state
    }

    /**
     * 窗口消失的生命周期入口（用户拖到底部关闭当天显示、权限退化、Service 结束）。
     *
     * 只负责显示态，不清理 [lastEvent]；隐藏期间到达的事件会被记成 pending，下次 `show()` 恢复。
     */
    fun hide(): PetState {
        state = PetState.HIDDEN
        pendingFromHidden = null
        return state
    }

    /**
     * 低频刷新（宿主 ≥1s 调用一次）：把超时停留态 [PetState.SUCCESS] / [PetState.ERROR] 回落到 [PetState.IDLE]。
     *
     * 回落不会越过 [PetState.WAITING_CONFIRM] / [PetState.WAITING_USER] / [PetState.OFFLINE]。
     */
    fun refresh(): PetState {
        when (state) {
            PetState.SUCCESS -> if (expired(successHoldMs)) apply(PetState.IDLE)
            PetState.ERROR -> if (expired(errorHoldMs)) apply(PetState.IDLE)
            else -> Unit
        }
        return state
    }

    /** 当前快照（不含任何正文内容）。 */
    fun snapshot(generation: Long = 0L): PetSnapshot = PetSnapshot(state, lastEvent, generation)

    /**
     * 固定映射表：事件 → 目标态。**唯一**的状态迁移来源。
     *
     * 两条额外语义（不新增事件、不改状态集合）：
     * 1. `task_progress` 不得抢走等待态：`WAITING_CONFIRM` / `WAITING_USER` 必须保持清晰可见；
     * 2. `OFFLINE` 是粘性终态：设备离线时到达的 `task_progress` / `task_stopped` 不会把桌宠显示成在线，
     *    只有新的 `task_started`（或窗口重新 `show()`）才能离开离线态。
     */
    private fun targetStateOf(event: PetEvent): PetState = when (event) {
        PetEvent.TASK_STARTED -> PetState.WORKING
        PetEvent.TASK_PROGRESS -> when (state) {
            PetState.WAITING_CONFIRM, PetState.WAITING_USER, PetState.OFFLINE -> state
            else -> PetState.WORKING
        }
        PetEvent.CONFIRM_REQUIRED -> PetState.WAITING_CONFIRM
        PetEvent.USER_REQUIRED -> PetState.WAITING_USER
        PetEvent.TASK_SUCCEEDED -> PetState.SUCCESS
        PetEvent.TASK_FAILED -> PetState.ERROR
        // 任务停止：回到在线空闲；但离线时停止不得伪造在线。
        PetEvent.TASK_STOPPED -> if (state == PetState.OFFLINE) PetState.OFFLINE else PetState.IDLE
        PetEvent.DEVICE_OFFLINE -> PetState.OFFLINE
    }

    private fun apply(target: PetState) {
        state = target
        when (target) {
            PetState.SUCCESS, PetState.ERROR -> {
                stampedAtMs = clock.nowMs()
                hasStamp = true
            }
            else -> {
                stampedAtMs = 0L
                hasStamp = false
            }
        }
    }

    private fun expired(holdMs: Long): Boolean {
        if (!hasStamp) return false
        val now = clock.nowMs()
        // 时钟回拨视为未到期，避免把停留态提前打断。
        if (now < stampedAtMs) return false
        return now - stampedAtMs >= holdMs
    }

    companion object {
        const val DEFAULT_SUCCESS_HOLD_MS: Long = 1_500L
        const val DEFAULT_ERROR_HOLD_MS: Long = 3_000L
    }
}
