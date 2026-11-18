package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M09 固定测试：桌宠窗口 `generation/ticket` 语义与悬浮窗退化（**纯 JVM，JUnit4**）。
 *
 * 规范来源 `docs/modules/M09-local-pet-voice-development.md`「悬浮窗实现」：
 * - Android 版本允许时用 `TYPE_ACCESSIBILITY_OVERLAY`，否则用 `TYPE_APPLICATION_OVERLAY`；
 * - **只有 `WindowManager.addView` 成功**才标记 `overlay_online = true`；
 * - 旧 Activity / 旧 producer 延迟销毁时，**不能移除新一代窗口**。
 *
 * 覆盖 M13 验收矩阵用例 `overlay_permission_fallback`
 * （`test-fixtures/m13-acceptance-matrix.json`，前置 `accessibility_overlay=false`、`application_overlay=true`，
 * 期望 `overlay_type = TYPE_APPLICATION_OVERLAY` 且 `overlay_online = true`）。
 *
 * 本测试不构造 `WindowManager`：所有判定都在 [PetWindowOwner] / [PetOverlayPolicy] / [PetDragPolicy] 纯逻辑里，
 * 「addView 成功 / 失败」通过 [PetWindowOwner.markAttached] / [PetWindowOwner.markAttachFailed] 直接模拟。
 */
class PetWindowOwnerTest {

    // ---------- 1. overlay_online 只在 addView 成功后为 true ----------

    /** 全新 owner 未上线。 */
    @Test
    fun overlayOfflineBeforeAnyAttach() {
        val owner = PetWindowOwner()
        assertFalse(owner.overlayOnline)
        assertNull(owner.onlineTicket)
        assertEquals(0L, owner.generation)
    }

    /** addView 成功（markAttached）→ overlay_online = true。 */
    @Test
    fun successfulAddViewMarksOverlayOnline() {
        val owner = PetWindowOwner()
        val claim = owner.beginGeneration()
        assertEquals(AttachOutcome.ATTACHED, owner.markAttached(claim.ticket))
        assertTrue(owner.overlayOnline)
        assertTrue(owner.isOnline(claim.ticket))
        assertEquals(claim.ticket, owner.onlineTicket)
    }

    /**
     * addView 失败（markAttachFailed）→ overlay_online **必须为 false**。
     *
     * 权限缺失、`BadTokenException`、无障碍未连接都走这条路径；绝不能显示假在线。
     */
    @Test
    fun failedAddViewKeepsOverlayOffline() {
        val owner = PetWindowOwner()
        val claim = owner.beginGeneration()
        owner.markAttachFailed(claim.ticket)
        assertFalse(owner.overlayOnline)
        assertNull(owner.onlineTicket)
        assertFalse(owner.isOnline(claim.ticket))
        // 票据仍可重试：同代内换新票据再 attach 能成功。
        val retry = owner.issueTicket()
        assertEquals(AttachOutcome.ATTACHED, owner.markAttached(retry.ticket))
        assertTrue(owner.overlayOnline)
    }

    /** 先成功再失败重试：失败的那次绝不改变已有的在线状态（这里是“本来就没在线”）。 */
    @Test
    fun failedRetryAfterReleaseKeepsOffline() {
        val owner = PetWindowOwner()
        val first = owner.beginGeneration()
        owner.markAttached(first.ticket)
        assertTrue(owner.overlayOnline)

        // 正规收尾：窗口下线，票据作废。
        assertTrue(owner.release(first.ticket))
        assertFalse(owner.overlayOnline)

        // 换新票据但 addView 失败。
        val retry = owner.issueTicket()
        owner.markAttachFailed(retry.ticket)
        assertFalse(owner.overlayOnline)
    }

    /**
     * 未该当的票据（从没 attach 过的旧票据）调用 markAttached 会被判为 SUPERSEDED。
     *
     * 模拟「旧 producer 延迟回调」：它拿着过期票据回来登记，必须被拒。
     */
    @Test
    fun staleTicketAttachIsSuperseded() {
        val owner = PetWindowOwner()
        val first = owner.beginGeneration()
        val firstTicket = first.ticket

        owner.beginGeneration() // 新一代开始，firstTicket 永久失效
        val second = owner.issueTicket()

        assertEquals(AttachOutcome.SUPERSEDED, owner.markAttached(firstTicket))
        assertFalse(owner.overlayOnline)

        assertEquals(AttachOutcome.ATTACHED, owner.markAttached(second.ticket))
        assertTrue(owner.overlayOnline)
    }

    // ---------- 2. generation/ticket 语义 ----------

    /** 每次 beginGeneration 代号 +1，且新代一开始旧代全部失效。 */
    @Test
    fun beginGenerationBumpsGenerationAndInvalidatesOld() {
        val owner = PetWindowOwner()
        val g1 = owner.beginGeneration()
        assertEquals(1L, g1.generation)
        assertTrue(owner.isCurrent(g1.ticket))

        val g2 = owner.beginGeneration()
        assertEquals(2L, g2.generation)
        assertFalse(owner.isCurrent(g1.ticket))
        assertTrue(owner.isStale(g1.ticket))
        assertTrue(owner.isCurrent(g2.ticket))
    }

    /** 同一代内最新票据才是 current，旧票据自动作废。 */
    @Test
    fun onlyLatestTicketInGenerationIsCurrent() {
        val owner = PetWindowOwner()
        val claim = owner.beginGeneration()
        val first = claim.ticket
        val second = owner.issueTicket().ticket

        assertFalse(owner.isCurrent(first))
        assertTrue(owner.isCurrent(second))
        // 票据序号全局单调，跨代也不重复。
        assertTrue(second.ticket > first.ticket)
        assertEquals(claim.generation, second.generation)
    }

    /** 不同代的票据序号永不相等（防止延迟回调误判）。 */
    @Test
    fun ticketSequenceIsGloballyMonotonic() {
        val owner = PetWindowOwner()
        val t1 = owner.beginGeneration().ticket
        val t2 = owner.beginGeneration().ticket
        val t3 = owner.issueTicket().ticket
        assertTrue(t1.ticket < t2.ticket)
        assertTrue(t2.ticket < t3.ticket)
        assertFalse(t1 == t2)
        assertFalse(t2 == t3)
    }

    /**
     * **核心用例**：旧 Activity / 旧 producer 延迟销毁，不能移除新一代窗口。
     *
     * 场景：Activity A 建了窗口（票据 tA）；Activity A 被销毁前，Activity B 已经接管并成功 attach 了
     * 新窗口（票据 tB）；随后 A 的 `onDestroy` 才迟到执行，拿着 tA 调 release。
     * 此时 release 必须返回 `false`，调用方就**不会** `removeView`，新窗口保住。
     */
    @Test
    fun lateDestroyFromOldActivityCannotRemoveNewWindow() {
        val owner = PetWindowOwner()

        // Activity A：attach 成功。
        val claimA = owner.beginGeneration()
        assertEquals(AttachOutcome.ATTACHED, owner.markAttached(claimA.ticket))
        assertTrue(owner.overlayOnline)
        val ticketA = claimA.ticket

        // Activity B 接管：新一代 + 新票据，B 的 addView 成功。
        val claimB = owner.beginGeneration()
        assertEquals(2L, claimB.generation)
        assertEquals(AttachOutcome.ATTACHED, owner.markAttached(claimB.ticket))
        assertTrue(owner.overlayOnline)
        assertEquals(claimB.ticket, owner.onlineTicket)

        // A 迟到销毁：拿着旧票据收尾 → 必须被拒绝，窗口（B 的）仍然在线。
        assertFalse("旧代票据不得移除新一代窗口", owner.release(ticketA))
        assertTrue(owner.overlayOnline)
        assertEquals(claimB.ticket, owner.onlineTicket)

        // B 正常收尾时才真的下线。
        assertTrue(owner.release(claimB.ticket))
        assertFalse(owner.overlayOnline)
    }

    /**
     * 同一代内的旧票据延迟销毁，也不能摘掉该代最新票据的窗口。
     *
     * 场景：同代重试（tAttempt1 失败 → tAttempt2 成功），attempt1 的迟到收尾不得动窗口。
     */
    @Test
    fun lateDestroyFromSupersededTicketInSameGenerationCannotRemoveWindow() {
        val owner = PetWindowOwner()
        val attempt1 = owner.beginGeneration()
        val attempt2 = owner.issueTicket()
        assertEquals(AttachOutcome.ATTACHED, owner.markAttached(attempt2.ticket))
        assertTrue(owner.overlayOnline)

        assertFalse(owner.release(attempt1.ticket))
        assertTrue(owner.overlayOnline)
        assertEquals(attempt2.ticket, owner.onlineTicket)

        assertTrue(owner.release(attempt2.ticket))
        assertFalse(owner.overlayOnline)
    }

    /** 已收尾的票据不能复活：release 后再 attach 同票据被判 SUPERSEDED。 */
    @Test
    fun releasedTicketCannotBeReattached() {
        val owner = PetWindowOwner()
        val claim = owner.beginGeneration()
        owner.markAttached(claim.ticket)
        assertTrue(owner.release(claim.ticket))
        assertFalse(owner.overlayOnline)

        assertEquals(AttachOutcome.SUPERSEDED, owner.markAttached(claim.ticket))
        assertFalse(owner.overlayOnline)
    }

    /** 收尾后 release 再次调用返回 false（幂等且不产生任何新状态）。 */
    @Test
    fun releaseIsIdempotent() {
        val owner = PetWindowOwner()
        val claim = owner.beginGeneration()
        owner.markAttached(claim.ticket)
        assertTrue(owner.release(claim.ticket))
        assertFalse(owner.release(claim.ticket))
        assertFalse(owner.overlayOnline)
    }

    /** `beginGeneration` 会报告上一代仍在线的旧票据，供调用方先摘掉，避免两层窗口。 */
    @Test
    fun beginGenerationReportsSupersededTicket() {
        val owner = PetWindowOwner()
        val claimA = owner.beginGeneration()
        owner.markAttached(claimA.ticket)
        assertTrue(owner.overlayOnline)

        val claimB = owner.beginGeneration()
        assertEquals(claimA.ticket, claimB.superseded)
        // 新一代开始时旧窗口立即不再被认为在线（必须由调用方 removeView）。
        assertFalse(owner.overlayOnline)
    }

    /** `issueTicket` 同样报告原在线票据（同代替换窗口场景）。 */
    @Test
    fun issueTicketReportsPreviousOnlineTicket() {
        val owner = PetWindowOwner()
        val claimA = owner.beginGeneration()
        owner.markAttached(claimA.ticket)
        val claimB = owner.issueTicket()
        assertEquals(claimA.ticket, claimB.superseded)
    }

    /** `forceRelease` 是兜底收尾：返回需要摘掉的票据，之后一律离线。 */
    @Test
    fun forceReleaseReturnsOnlineTicket() {
        val owner = PetWindowOwner()
        val claim = owner.beginGeneration()
        owner.markAttached(claim.ticket)
        assertEquals(claim.ticket, owner.forceRelease())
        assertFalse(owner.overlayOnline)
        assertNull(owner.onlineTicket)
        assertNull(owner.latestTicket)
        assertNull(owner.forceRelease())
    }

    // ---------- 3. 悬浮窗类型选择（M13 用例 overlay_permission_fallback） ----------

    /**
     * 无障碍可用时优先 `TYPE_ACCESSIBILITY_OVERLAY`（限制最少、无需另授悬浮窗）。
     */
    @Test
    fun accessibilityConnectedPrefersAccessibilityOverlay() {
        val capability = OverlayCapability(
            sdkInt = 34,
            accessibilityConnected = true,
            overlayPermissionGranted = true,
        )
        assertEquals(PetOverlayType.ACCESSIBILITY_OVERLAY, PetOverlayPolicy.choose(capability))
        assertFalse(PetOverlayPolicy.isFallback(capability))
    }

    /**
     * **M13 用例 `overlay_permission_fallback`**：无障碍不可用、普通悬浮窗已授权
     * → 退化为 `TYPE_APPLICATION_OVERLAY`。
     *
     * 用 `android.application_overlay` 这个语义名对应 Android 层的 `TYPE_APPLICATION_OVERLAY`；
     * 真正的数值映射在 [PetOverlayService.layoutParams] 里。
     */
    @Test
    fun overlayPermissionFallbackToApplicationOverlay() {
        val capability = OverlayCapability(
            sdkInt = 34,
            accessibilityConnected = false,
            overlayPermissionGranted = true,
        )
        assertEquals(PetOverlayType.APPLICATION_OVERLAY, PetOverlayPolicy.choose(capability))
        assertTrue(PetOverlayPolicy.isFallback(capability))
        assertFalse(PetOverlayPolicy.isUnavailable(capability))
        assertFalse(PetOverlayType.APPLICATION_OVERLAY.isAccessibilityOverlay)
    }

    /** 两种悬浮窗都不可用 → `null`（退化到应用内桌宠与通知，不伪造在线）。 */
    @Test
    fun noOverlayCapabilityReturnsNull() {
        val capability = OverlayCapability(
            sdkInt = 34,
            accessibilityConnected = false,
            overlayPermissionGranted = false,
        )
        assertNull(PetOverlayPolicy.choose(capability))
        assertTrue(PetOverlayPolicy.isUnavailable(capability))
        assertFalse(PetOverlayPolicy.isFallback(capability))
    }

    /** 低版本（< 22）即便无障碍已连接也不能用无障碍悬浮窗，只能退化。 */
    @Test
    fun oldSdkCannotUseAccessibilityOverlay() {
        val capability = OverlayCapability(
            sdkInt = 21,
            accessibilityConnected = true,
            overlayPermissionGranted = true,
        )
        assertEquals(PetOverlayType.APPLICATION_OVERLAY, PetOverlayPolicy.choose(capability))
    }

    /** 无障碍悬浮窗仅需无障碍连接：不必另授 `SYSTEM_ALERT_WINDOW`。 */
    @Test
    fun accessibilityOverlayDoesNotRequireSystemAlertWindow() {
        val capability = OverlayCapability(
            sdkInt = 30,
            accessibilityConnected = true,
            overlayPermissionGranted = false,
        )
        assertEquals(PetOverlayType.ACCESSIBILITY_OVERLAY, PetOverlayPolicy.choose(capability))
        assertFalse(PetOverlayPolicy.isFallback(capability))
    }

    // ---------- 4. 拖拽贴边 / 拖到底部关闭（纯逻辑） ----------

    /** 拖到底部附近松手 → 关闭当天显示。 */
    @Test
    fun dragToBottomClosesForToday() {
        val screenHeight = 2_000
        val windowHeight = 200
        // 顶边 1750 + 高度 200 = 底边 1950，距屏幕底部 50px，小于阈值（2000*0.12=240）。
        assertTrue(PetDragPolicy.shouldCloseOnRelease(1_750, windowHeight, screenHeight))
    }

    /** 停在屏幕中部松手 → 不关闭。 */
    @Test
    fun dragToMiddleDoesNotClose() {
        assertFalse(PetDragPolicy.shouldCloseOnRelease(800, 200, 2_000))
    }

    /** 非法尺寸输入直接不关闭（防御性）。 */
    @Test
    fun invalidDimensionsDoNotClose() {
        assertFalse(PetDragPolicy.shouldCloseOnRelease(0, 0, 0))
        assertFalse(PetDragPolicy.shouldCloseOnRelease(0, 0, 2_000))
    }

    /** 贴边吸附：拖到左侧吸左边，拖到右侧吸右边。 */
    @Test
    fun snapToNearestHorizontalEdge() {
        val screenWidth = 1_080
        val windowWidth = 200
        val margin = 8
        assertEquals(8, PetDragPolicy.snapLeftPx(20, windowWidth, screenWidth, margin))
        assertEquals(
            screenWidth - windowWidth - margin,
            PetDragPolicy.snapLeftPx(screenWidth - 60, windowWidth, screenWidth, margin),
        )
    }

    /** 贴边坐标不得越界。 */
    @Test
    fun snapClampsOutOfBounds() {
        val screenWidth = 1_080
        val windowWidth = 200
        val margin = 8
        val snapped = PetDragPolicy.snapLeftPx(-500, windowWidth, screenWidth, margin)
        assertTrue(snapped >= margin)
        assertTrue(snapped <= screenWidth - windowWidth - margin)
    }

    /** 阈值下限固定 48px，防止极小屏幕上阈值退化为 0。 */
    @Test
    fun closeThresholdHasFloor() {
        assertEquals(48, PetDragPolicy.closeThresholdPx(100))
        assertEquals(240, PetDragPolicy.closeThresholdPx(2_000))
    }
}
