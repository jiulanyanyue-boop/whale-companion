package cn.whale.agent.device

/**
 * 「这一次分享」的批准判定契约（M08 [MediaWiringImpl.share] 的确认来源）。
 *
 * 设计原则：**不新造一套确认语义**。真值只有一处——M04 [UserGate] 的状态机；
 * 本接口只是把它投影成「某个 `(asset_id, target)` 这一刻能不能放行」。
 *
 * 三个固定规则（与 `docs/modules/M04-owner-security-product.md` 一致）：
 * 1. 默认 **fail-closed**：没有绑定、没有真实批准、状态不是批准态，一律 `false`；
 * 2. 注入手势期间（[injecting]）一律 `false`，且**不消费**批准——
 *    否则 AI 可以在注入过程中借走一次用户批准；
 * 3. 批准**只用一次**：[approve] 返回 `true` 的同时结算并注销绑定，
 *    与 [UserGate.consume] 的「APPROVED_ONCE → READY」语义逐字对齐。
 */
interface MediaShareApproval {
    /** 是否正在注入手势（由无障碍注入层驱动 [UserGate.setInjecting]）。 */
    val injecting: Boolean

    /**
     * 本次分享是否已有有效批准。
     *
     * @return `true` 表示「这一份素材发往这个目标」已被真实用户批准，并且**本次调用已消费掉它**；
     *   任何不确定情况（未绑定、仍在等确认、已拒绝/停止/超时、注入中、重复使用）都返回 `false`。
     */
    fun approve(assetId: String, target: String): Boolean
}

/**
 * 进程级 [MediaShareApproval] 登记处（默认**空**）。
 *
 * 与 [MediaPickerHosts] 同一形态：宿主（前台 Activity / 本地入口）在拿到 M04 [UserGate] 后
 * `attach` 一次即可，[MediaWiringImpl] 晚于装配也会读到。
 *
 * 没有登记时不是「降级为放行」，而是**保持 fail-closed**：
 * [MediaWiringImpl.share] 只会退回到路由层注入的确认回调（其默认值恒 `false`）。
 */
object MediaShareApprovals {
    @Volatile
    private var approval: MediaShareApproval? = null

    /** 由持有 M04 [UserGate] 的宿主登记；重复登记以最后一次为准。 */
    fun attach(approval: MediaShareApproval) {
        this.approval = approval
    }

    /** 会话结束 / 模式切换 / Activity 销毁时撤销登记。 */
    fun detach() {
        approval = null
    }

    /** 当前登记的批准来源；没有就是 `null`。 */
    fun current(): MediaShareApproval? = approval

    /** 本进程是否真的接上了 M04 的批准来源。 */
    fun attached(): Boolean = approval != null

    /**
     * 一行生产接线：由持有 M04 [UserGate] 的宿主调用。
     *
     * ```kotlin
     * // 前台 Activity / 本地入口，与 UserGate 同一个实例
     * private val shareApproval = MediaShareApprovals.attachFrom(userGate)
     * // 弹确认卡时绑定「这一份素材 + 这个目标」
     * shareApproval.bind(requestId, assetId, target)
     * ```
     *
     * @return 登记好的 [MediaShareConfirmation]；宿主必须留着它才能 [MediaShareConfirmation.bind]。
     */
    fun attachFrom(userGate: UserGate): MediaShareConfirmation =
        MediaShareConfirmation(userGate).also { approval = it }
}

/**
 * 把 M04 [UserGate] 的批准状态接成 [MediaShareApproval]（**不依赖 Android**，纯 JVM 可测）。
 *
 * ---------- 与 [UserGate] 的对应关系 ----------
 * ```
 * 宿主: userGate.request(requestId, "device.media.share", …)   // 弹确认卡
 * 宿主: confirmation.bind(requestId, assetId, target)          // 把这次确认绑到「这一份素材 + 这个目标」
 * 用户: userGate.onUserAction(requestId, APPROVE_ONCE)        // 真实点击
 * 路由: wiring.share(assetId, target, confirmed=false)
 *       └→ confirmation.approve(assetId, target)
 *          ├ 注入中？            → false（不消费）
 *          ├ 仍在 NEED_CONFIRM？ → false（保留绑定，等用户点）
 *          ├ 不是批准态？        → false，并注销绑定（拒绝/停止/超时都作废）
 *          └ 真批准             → userGate.consume(requestId) + 注销绑定 → true（只用一次）
 * ```
 *
 * 为什么绑定 `(assetId, target)` 而不是只按命令名放行：
 * `device.media.share` 是命令级高风险，但「哪一份素材发给谁」才是真正的授权对象。
 * 只按命令名放行会让一次批准覆盖任意后续分享，等于放宽确认范围。
 *
 * 线程安全：状态判定与消费都在 [lock] 内完成，锁的获取顺序固定为 `[lock] → [UserGate]`，
 * 而 [UserGate] 从不回调本类，故无死锁。注入状态在锁外再判一次，
 * 防止「判定通过后、执行前」恰好开始注入的窗口被借用。
 */
class MediaShareConfirmation(
    private val userGate: UserGate,
) : MediaShareApproval {
    /** 一次绑定的不可变记录。 */
    private class Binding(
        val requestId: String,
        val assetId: String,
        val target: String,
    )

    private val lock = Any()
    private val bindings = LinkedHashMap<String, Binding>()

    /** 直接转发 [UserGate.isInjecting]：注入期间确认卡不可点击，也不许借用批准。 */
    override val injecting: Boolean get() = userGate.isInjecting()

    /**
     * 把一条待确认请求绑到「这一份素材 + 这个目标」。
     *
     * @return `false` 表示参数不全（空 id / 空目标），此时**没有建立任何绑定**，
     *   后续 [approve] 必然拒绝——宁可多要一次确认，也不放过一条。
     */
    fun bind(requestId: String, assetId: String, target: String): Boolean {
        if (requestId.isBlank() || assetId.isBlank() || target.isBlank()) return false
        synchronized(lock) { bindings[requestId] = Binding(requestId, assetId, target) }
        return true
    }

    /** 解除一条绑定（用户拒绝、超时、任务切换、宿主主动撤销）。 */
    fun unbind(requestId: String) {
        synchronized(lock) { bindings.remove(requestId) }
    }

    /** 清空全部绑定（会话结束 / 模式切换）。 */
    fun clear() {
        synchronized(lock) { bindings.clear() }
    }

    /**
     * 当前**仍然有效**的绑定请求 id（只读快照，供测试与审计）。
     *
     * 「有效」= 仍在等用户点击（[ConfirmState.NEED_CONFIRM]）或已批准（[ConfirmState.isApproved]）。
     * 读的时候顺手按 [UserGate.checkTimeout] 结算一次超时，并把已拒绝 / 停止 / 超时 / 已结算
     * 的绑定直接摘掉——这样被拒绝的批准不会留在表里，也不可能被后来的分享复用。
     */
    fun boundRequestIds(): List<String> = synchronized(lock) {
        val valid = ArrayList<String>()
        val iterator = bindings.entries.iterator()
        while (iterator.hasNext()) {
            val entry = iterator.next()
            val state = userGate.checkTimeout(entry.value.requestId)
            when {
                state == ConfirmState.NEED_CONFIRM || state.isApproved -> valid.add(entry.key)
                // READY / DENIED / STOPPED / TIMEOUT：绑定已无意义，作废。
                else -> iterator.remove()
            }
        }
        valid
    }

    override fun approve(assetId: String, target: String): Boolean {
        // 锁外先判一次：注入中连绑定都不查，避免无谓地推动状态机。
        if (userGate.isInjecting()) return false

        if (assetId.isBlank() || target.isBlank()) return false

        synchronized(lock) {
            // 锁内再判一次：防止「锁外判定通过 → 进锁前开始注入」的窗口。
            if (userGate.isInjecting()) return false

            val binding = bindings.values.firstOrNull {
                it.assetId == assetId && it.target == target
            } ?: return false

            // 复用 M04 的既有超时语义：到点即 TIMEOUT，默认拒绝、绝不默认继续。
            val state = userGate.checkTimeout(binding.requestId)

            // 还在等用户点击：**不是拒绝**，绑定保留，等真实点击后再放行。
            if (state == ConfirmState.NEED_CONFIRM) return false

            // 拒绝 / 停止 / 超时 / 已结算过：绑定作废，不得复用。
            if (!state.isApproved) {
                bindings.remove(binding.requestId)
                return false
            }

            // 真批准：先按 UserGate 语义结算（APPROVED_ONCE → READY），再销绑定，保证只用一次。
            userGate.consume(binding.requestId)
            bindings.remove(binding.requestId)
            return true
        }
    }
}
