package cn.whale.agent.device

/**
 * 可注入时钟：让 [LocalSessionGate] 在 JVM 单测里不依赖 `System.currentTimeMillis()`。
 */
fun interface SessionClock {
    fun nowMs(): Long
}

/** 生产时钟。 */
object SystemSessionClock : SessionClock {
    override fun nowMs(): Long = System.currentTimeMillis()
}

/**
 * M01 本地「已解锁交互会话」状态（纯逻辑，可注入时钟）。
 *
 * 依据 `docs/modules/M01-system-modes-development.md`「本地闸门」：
 * - 本地会话必须处于已解锁状态，否则返回 `NEEDS_UNLOCK`。
 * - 会话有超时；超时后必须重新解锁，不得沿用旧会话。
 *
 * 明确不做的事（文档禁止）：本类**不实现**密码、支付密码、验证码或认证器的自动填写，
 * 只维护「本次交互会话是否已由用户在设置页/桌宠界面解锁」这一个布尔状态与时间戳。
 * 真正的解锁 UI 属于 M04 主人与安全模块。
 *
 * 线程：调用方通常在主线程/单线程处理本地入口，字段用 `@Volatile` 保证可见性即可。
 */
class LocalSessionGate(
    private val clock: SessionClock = SystemSessionClock,
    /** 会话有效期，默认 10 分钟；超时后一次本地命令都执行不了，必须重新解锁。 */
    private val timeoutMs: Long = DEFAULT_TIMEOUT_MS,
) {
    @Volatile
    private var unlockedAtMs: Long? = null

    /** 最近一次用户交互（按住说话、点击桌宠）时间；只用于刷新，不延长到无限期。 */
    @Volatile
    private var lastInteractionMs: Long? = null

    val isUnlocked: Boolean
        get() = unlockedAtMs != null && !isExpired()

    /**
     * 用户显式解锁（由主界面/桌宠的明确点击调用）。
     *
     * 记录当前时间作为会话起点，并清除上一次的过期状态。
     */
    fun unlock() {
        val now = clock.nowMs()
        unlockedAtMs = now
        lastInteractionMs = now
    }

    /** 用户主动结束会话、模式切换或后台化时调用；之后本地命令一律 `NEEDS_UNLOCK`。 */
    fun lock() {
        unlockedAtMs = null
        lastInteractionMs = null
    }

    /** 用户的一次本地交互：仅在会话仍然有效时刷新活跃时间，不会复活已过期会话。 */
    fun onUserInteraction() {
        if (unlockedAtMs == null || isExpired()) {
            lock()
            return
        }
        lastInteractionMs = clock.nowMs()
    }

    /**
     * 本地命令入口的固定检查。
     *
     * @return [SessionDecision.Allowed] 表示可继续；否则携带 `NEEDS_UNLOCK`。
     */
    fun check(): SessionDecision {
        if (unlockedAtMs == null) {
            return SessionDecision.denied(CommandErrors.NEEDS_UNLOCK, "local session is not unlocked")
        }
        if (isExpired()) {
            lock()
            return SessionDecision.denied(CommandErrors.NEEDS_UNLOCK, "local session expired; unlock again")
        }
        return SessionDecision.Allowed
    }

    private fun isExpired(): Boolean {
        val startedAt = unlockedAtMs ?: return true
        val now = clock.nowMs()
        // 时钟回拨（now < startedAt）视为过期，宁可多要一次解锁，也不放宽窗口。
        return now < startedAt || now - startedAt > timeoutMs
    }

    companion object {
        const val DEFAULT_TIMEOUT_MS: Long = 10 * 60 * 1000L
    }
}

/** 会话判定结果；[Allowed] 不带错误码，[Denied] 必须引用 [CommandErrors] 常量。 */
sealed class SessionDecision {
    data object Allowed : SessionDecision()

    data class Denied(val code: String, val message: String) : SessionDecision()

    val isAllowed: Boolean get() = this is Allowed

    val errorCode: String? get() = (this as? Denied)?.code

    companion object {
        fun denied(code: String, message: String): Denied = Denied(code, message)
    }
}
