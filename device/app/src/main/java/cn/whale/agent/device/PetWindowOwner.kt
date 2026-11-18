package cn.whale.agent.device

/**
 * M09 桌宠窗口的 `generation/ticket` 归属管理（**纯 JVM，零 Android 依赖**）。
 *
 * 规范来源：`docs/modules/M09-local-pet-voice-development.md`「悬浮窗实现」：
 * > 窗口管理必须有 `generation/ticket`：旧 Activity 或旧 Flutter/Compose producer 延迟销毁时，
 * > 不能移除新一代窗口。
 *
 * ## 术语
 *
 * - **generation（代）**：一次「窗口接管」。每次 [beginGeneration] 递增一代；新代一开始，
 *   旧代的**所有**票据永久失效。Activity 重建、Service 重启、应用内桌宠升级为悬浮窗，都是新的一代。
 * - **ticket（票据）**：一次 `WindowManager.addView` 尝试的唯一标识。票据带全局单调序号，
 *   因此不同代、同代不同次尝试之间都绝不相等。
 *
 * ## 调用契约
 *
 * ```kotlin
 * val claim = owner.beginGeneration()            // 新代 + 新票据 + 需要摘掉的旧窗口
 * claim.superseded?.let { windowManager.removeView(oldView) }
 * val ticket = claim.ticket
 * try {
 *     windowManager.addView(view, params)
 * } catch (t: Throwable) {
 *     owner.markAttachFailed(ticket)             // overlayOnline 保持 false，绝不假在线
 *     return
 * }
 * when (owner.markAttached(ticket)) {
 *     AttachOutcome.ATTACHED   -> 正常显示
 *     AttachOutcome.SUPERSEDED -> windowManager.removeView(view)  // 已被新代/新票据取代，必须自己摘掉
 * }
 * ```
 *
 * 收尾时：
 * ```kotlin
 * if (owner.release(ticket)) windowManager.removeView(view)
 * ```
 * [release] 返回 `false` 表示票据过期（旧 Activity 的 `onDestroy` 延迟执行到新一代窗口已 attach 之后），
 * **绝不能**动窗口，否则会摘掉新一代窗口。
 *
 * 只有 [markAttached] 返回 [AttachOutcome.ATTACHED] 时 [overlayOnline] 才会变 `true`。
 *
 * 线程：主线程调用；内部用同步块保护，允许 `onDestroy` 的收尾线程并发调用。
 */
class PetWindowOwner {

    /** 当前代。从 0 起，[beginGeneration] 后 ≥1。 */
    @Volatile
    var generation: Long = 0L
        private set

    /** 当前在线票据；`null` 表示没有窗口在线。 */
    @Volatile
    var onlineTicket: WindowTicket? = null
        private set

    /** 本代最新票据（可能尚未 attach 成功，用于失败重试判重）。 */
    @Volatile
    var latestTicket: WindowTicket? = null
        private set

    /**
     * 是否已有窗口真正上线。
     *
     * **只有 `WindowManager.addView` 成功**（[markAttached] 返回 [AttachOutcome.ATTACHED]）才为 `true`；
     * 权限缺失、addView 抛异常、票据被取代，一律保持 `false`。
     */
    @Volatile
    var overlayOnline: Boolean = false
        private set

    @Volatile
    private var ticketCounter: Long = 0L

    /**
     * 开始新的一代：新窗口接管时调用（Service 首次 show、Activity 重建后重新挂载）。
     *
     * @return [WindowClaim]：新代号、本代第一张票据、以及上一代仍在线**需要由调用方 removeView** 的旧票据。
     */
    @Synchronized
    fun beginGeneration(): WindowClaim {
        val superseded = onlineTicket
        generation += 1L
        ticketCounter += 1L
        val ticket = WindowTicket(generation = generation, ticket = ticketCounter)
        latestTicket = ticket
        onlineTicket = null
        overlayOnline = false
        return WindowClaim(generation = generation, ticket = ticket, superseded = superseded)
    }

    /**
     * 在当前代内重新发放票据（`addView` 失败后重试、或同代内重建 View 时调用）。
     *
     * @return [WindowClaim]：新票据 + 原在线窗口票据（调用方必须自己摘掉它，否则会有两层窗口）。
     */
    @Synchronized
    fun issueTicket(): WindowClaim {
        val superseded = onlineTicket
        ticketCounter += 1L
        val ticket = WindowTicket(generation = generation, ticket = ticketCounter)
        latestTicket = ticket
        onlineTicket = null
        overlayOnline = false
        return WindowClaim(generation = generation, ticket = ticket, superseded = superseded)
    }

    /**
     * `addView` 成功后的**唯一**登记点。
     *
     * @return [AttachOutcome.ATTACHED] 表示本票据成为唯一在线窗口；
     *         [AttachOutcome.SUPERSEDED] 表示票据已过期（旧代或旧票据），调用方必须立即 `removeView` 自己的 View。
     */
    @Synchronized
    fun markAttached(ticket: WindowTicket): AttachOutcome {
        if (!isCurrent(ticket)) return AttachOutcome.SUPERSEDED
        onlineTicket = ticket
        overlayOnline = true
        return AttachOutcome.ATTACHED
    }

    /**
     * `addView` 抛出异常后的登记点。
     *
     * 权限失败、`BadTokenException`、无障碍未连接都走这里：**绝不**标记在线。
     * 票据仍保留为 current，允许同代内换新票据重试。
     */
    @Synchronized
    fun markAttachFailed(ticket: WindowTicket) {
        if (!isCurrent(ticket)) return
        if (onlineTicket == ticket) return
        overlayOnline = onlineTicket != null
    }

    /**
     * 正常收尾（用户关闭显示、Service onDestroy、权限退化到应用内桌宠）。
     *
     * @return `true` 时调用方**才可以** `removeView`；`false` 表示票据已过期，绝不能动窗口。
     */
    @Synchronized
    fun release(ticket: WindowTicket): Boolean {
        // 只有「当前在线窗口」的票据才允许摘窗口；旧代/旧票据一律不碰窗口。
        if (ticket != onlineTicket) return false
        onlineTicket = null
        overlayOnline = false
        // 收尾后该票据永久失效，避免延迟回调把它重新 attach 回来。
        if (latestTicket == ticket) latestTicket = null
        return true
    }

    /**
     * 强制下线（`Service.onDestroy` 兜底，不依赖票据是否 current）。
     *
     * @return 需要调用方 `removeView` 的票据；`null` 表示本来就没有窗口在线。
     */
    @Synchronized
    fun forceRelease(): WindowTicket? {
        val ticket = onlineTicket
        onlineTicket = null
        latestTicket = null
        overlayOnline = false
        return ticket
    }

    /** 票据是否属于当前一代的当前票据（可 attach / 可重试）。 */
    fun isCurrent(ticket: WindowTicket): Boolean =
        ticket.generation == generation && ticket == latestTicket

    /** 票据是否已被取代（旧 Activity / 旧 producer / 旧的重试）。 */
    fun isStale(ticket: WindowTicket): Boolean = !isCurrent(ticket)

    /** 票据是否就是当前在线窗口。 */
    fun isOnline(ticket: WindowTicket): Boolean = onlineTicket == ticket && overlayOnline
}

/**
 * 窗口票据。
 *
 * @property generation 发放时的代号，跨代一律失效（旧 Activity 延迟销毁也摘不掉新一代窗口）。
 * @property ticket 全局单调序号，保证同代内的旧票据不会再次生效。
 */
data class WindowTicket(val generation: Long, val ticket: Long)

/**
 * [PetWindowOwner.beginGeneration] / [PetWindowOwner.issueTicket] 的结果。
 *
 * @property ticket 本次要 attach 的新票据。
 * @property superseded 仍在线、需要调用方 `removeView` 的旧票据（可能为 `null`）。
 */
data class WindowClaim(
    val generation: Long,
    val ticket: WindowTicket,
    val superseded: WindowTicket? = null,
)

/** `addView` 之后的归属判定。 */
enum class AttachOutcome {
    /** 本票据成为唯一在线窗口，`overlayOnline = true`。 */
    ATTACHED,

    /** 票据已过期：调用方必须立即移除自己刚加进去的 View。 */
    SUPERSEDED,
}

// ---------------------------------------------------------------------------
// 悬浮窗类型选择（纯逻辑，不引用 WindowManager 常量）
// ---------------------------------------------------------------------------

/**
 * 桌宠窗口类型。
 *
 * 规范（`docs/modules/M09-local-pet-voice-development.md`）：
 * > Android 版本允许时使用 `TYPE_ACCESSIBILITY_OVERLAY`；否则使用 `TYPE_APPLICATION_OVERLAY`。
 *
 * 这里只表达「选哪种」，真正的 `WindowManager.LayoutParams.TYPE_*` 数值由
 * [PetOverlayService] 在 Android 层映射，便于本类保持纯 JVM 可测。
 */
enum class PetOverlayType {
    /** 无障碍已连接且平台支持（API 22+ 的 `TYPE_ACCESSIBILITY_OVERLAY`），限制最少、最不易被系统遮挡。 */
    ACCESSIBILITY_OVERLAY,

    /** 兜底：普通悬浮窗，需要用户手动授权 `SYSTEM_ALERT_WINDOW`。 */
    APPLICATION_OVERLAY,
    ;

    /** 是否是「无障碍悬浮窗」（用于审计事件 `overlay_fallback` 的判定）。 */
    val isAccessibilityOverlay: Boolean get() = this == ACCESSIBILITY_OVERLAY
}

/**
 * 悬浮窗能力探测结果（由 Android 层填充，单测可直接构造）。
 *
 * @property sdkInt 当前 API 级别。
 * @property accessibilityConnected 无障碍服务是否已连接（决定能否用 `TYPE_ACCESSIBILITY_OVERLAY`）。
 * @property overlayPermissionGranted `SYSTEM_ALERT_WINDOW` 是否已授权（决定能否用 `TYPE_APPLICATION_OVERLAY`）。
 */
data class OverlayCapability(
    val sdkInt: Int,
    val accessibilityConnected: Boolean,
    val overlayPermissionGranted: Boolean,
)

/**
 * 悬浮窗选型策略（纯函数）。
 *
 * 优先级固定：无障碍悬浮窗 → 普通悬浮窗 → [null]（**退化到应用内桌宠与通知**，
 * 产品文档「权限失败时退化到应用内桌宠和通知」，绝不假装在线）。
 *
 * @return 选中的窗口类型；`null` 表示两种悬浮窗都不可用。
 */
object PetOverlayPolicy {

    /** `TYPE_ACCESSIBILITY_OVERLAY` 从 API 22 起可用。 */
    const val ACCESSIBILITY_OVERLAY_MIN_SDK: Int = 22

    /**
     * 选择窗口类型。
     *
     * 注意：`TYPE_APPLICATION_OVERLAY` 只在 API 26+ 存在；本工程 `minSdk = 26`，
     * 因此低版本仅用于纯逻辑测试的表达，实际设备都满足。
     */
    fun choose(capability: OverlayCapability): PetOverlayType? {
        val accessibilityUsable =
            capability.accessibilityConnected && capability.sdkInt >= ACCESSIBILITY_OVERLAY_MIN_SDK
        if (accessibilityUsable) return PetOverlayType.ACCESSIBILITY_OVERLAY
        if (capability.overlayPermissionGranted) return PetOverlayType.APPLICATION_OVERLAY
        return null
    }

    /** 是否发生了「无障碍不可用 → 普通悬浮窗」的退化（审计事件 `overlay_fallback`）。 */
    fun isFallback(capability: OverlayCapability): Boolean =
        choose(capability) == PetOverlayType.APPLICATION_OVERLAY

    /** 是否完全没有悬浮窗能力（应退化到应用内桌宠 + 通知）。 */
    fun isUnavailable(capability: OverlayCapability): Boolean = choose(capability) == null
}

/**
 * 拖拽贴边 / 拖到底部关闭的判定（纯逻辑，输入是已换算的像素坐标）。
 *
 * 产品文档「动画规则」：拖动贴边，拖到底部关闭当天显示。
 * 这里只判定「松手后是否视为关闭」，不写任何持久化——「当天」标记由宿主存进
 * SharedPreferences（键名由 M09 自己维护，不碰 M01/M11）。
 *
 * @property windowHeight 窗口高度（px）。
 * @property screenHeight 屏幕可用高度（px）。
 * @property closeThreshold 距底部多少 px 以内算「拖到底部」。
 */
object PetDragPolicy {

    /** 默认关闭阈值：屏幕高度的 12%，至少 48px。 */
    fun closeThresholdPx(screenHeight: Int): Int = maxOf(48, (screenHeight * 0.12f).toInt())

    /**
     * 松手时是否应关闭当天显示。
     *
     * @param finalTopPx 松手时窗口顶边 y 坐标（px）。
     * @param windowHeightPx 窗口高度（px）。
     * @param screenHeightPx 屏幕可用高度（px）。
     */
    fun shouldCloseOnRelease(finalTopPx: Int, windowHeightPx: Int, screenHeightPx: Int): Boolean {
        if (screenHeightPx <= 0 || windowHeightPx <= 0) return false
        val distanceToBottom = screenHeightPx - (finalTopPx + windowHeightPx)
        return distanceToBottom <= closeThresholdPx(screenHeightPx)
    }

    /**
     * 贴边吸附后目标顶边坐标（仅水平吸附，垂直不动）。
     *
     * @param proposedLeftPx 拖拽后建议的左边界。
     * @param windowWidthPx 窗口宽度。
     * @param screenWidthPx 屏幕宽度。
     * @param edgeMarginPx 与屏幕边缘保留的间距。
     */
    fun snapLeftPx(proposedLeftPx: Int, windowWidthPx: Int, screenWidthPx: Int, edgeMarginPx: Int = 0): Int {
        if (screenWidthPx <= 0) return 0
        val maxLeft = maxOf(edgeMarginPx, screenWidthPx - windowWidthPx - edgeMarginPx)
        val clamped = proposedLeftPx.coerceIn(minOf(edgeMarginPx, maxLeft), maxLeft)
        val center = screenWidthPx / 2f
        val windowCenter = clamped + windowWidthPx / 2f
        return if (windowCenter < center) edgeMarginPx else maxLeft
    }
}
