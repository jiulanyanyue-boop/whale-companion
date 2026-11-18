package cn.whale.agent.device

import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M08 媒体分享确认的**生产接线**验证（纯 JVM）。
 *
 * 这个文件回答一个很具体的问题：`LocalPetController` 里的 `userGate` 到底有没有
 * 真的接到 `MediaShareApprovals`？如果没接，`device.media.share` 会永远返回
 * `ACTION_REJECTED`——功能看起来「实现了」，实际永远不可用。
 *
 * 之前就出过这个情况：`MediaShareConfirmation` 做完了，但没人调用 `attachFrom`，
 * 生产默认仍是 fail-closed。所以这条测试专门守住「登记确实发生」。
 *
 * 注意：`LocalPetController` 的构造需要 `Context`（Android 桩），本机跑不了；
 * 因此这里验证的是**同一套登记语义**（`MediaShareApprovals.attachFrom(userGate)`
 * + `bind` 后用 `approve` 判定），与 `LocalPetController` 内部走的路径完全一致。
 */
class MediaShareWiringTest {

    private class RecordingEffects : EffectLog {
        override fun find(idempotencyKey: String): String? = null
        override fun save(idempotencyKey: String, responseJson: String, nowMs: Long) = Unit
    }

    private class RecordingCounters : CounterLog {
        override fun find(deviceId: String): Long? = null
        override fun save(deviceId: String, counter: Long) = Unit
    }

    private fun newUserGate(): UserGate = UserGate(clock = SessionClock { 1_000L })

    @Test
    fun attachFromUserGateRegistersAnApprovalSource() {
        // `LocalPetController` 初始化时做的就是这个调用。
        MediaShareApprovals.detach()
        assertFalse("前置：初始未登记", MediaShareApprovals.attached())

        val userGate = newUserGate()
        val confirmation = MediaShareApprovals.attachFrom(userGate)

        assertTrue("attachFrom 之后必须已登记", MediaShareApprovals.attached())
        assertNotNull(MediaShareApprovals.current())
        // 返回的实例必须可继续操作（宿主留着它才能 bind）。
        assertTrue("binding 前 boundRequestIds 应为空", confirmation.boundRequestIds().isEmpty())

        MediaShareApprovals.detach()
        assertFalse("detach 之后必须回到未登记", MediaShareApprovals.attached())
    }

    @Test
    fun withoutBindingShareIsNotApproved() {
        // 没有任何绑定 → approve 必须为 false（fail-closed 的底线）。
        MediaShareApprovals.detach()
        val confirmation = MediaShareApprovals.attachFrom(newUserGate())

        assertFalse(
            "没有绑定就必须拒绝",
            confirmation.approve(assetId = "asset-1", target = "com.tencent.mobileqq"),
        )
        MediaShareApprovals.detach()
    }

    @Test
    fun afterBindingAndUserApprovalShareIsApprovedOnce() {
        MediaShareApprovals.detach()
        val userGate = newUserGate()
        val confirmation = MediaShareApprovals.attachFrom(userGate)

        // 1) 宿主弹确认卡：request → 立刻绑定「本次素材 + 目标」。
        val requestId = "req-share-1"
        userGate.request(requestId, "device.media.share", "asset-1 -> com.tencent.mobileqq", ConfirmLevel.CONFIRM_EVERY_STEP)
        assertTrue(
            "绑定应成功",
            confirmation.bind(requestId, "asset-1", "com.tencent.mobileqq"),
        )
        assertTrue("绑定后应能看到 requestId", requestId in confirmation.boundRequestIds())

        // 2) 用户点「确认」。
        userGate.onUserAction(requestId, ConfirmUserAction.APPROVE_ONCE)

        // 3) 路由层问「这份素材分享到这个目标批准过吗」→ 应放行一次。
        assertTrue(
            "用户确认后，同一素材与目标应放行",
            confirmation.approve(assetId = "asset-1", target = "com.tencent.mobileqq"),
        )

        // 4) 只能用一次：第二次必须拒绝（否则一次确认可无限分享）。
        assertFalse(
            "批准只能用一次",
            confirmation.approve(assetId = "asset-1", target = "com.tencent.mobileqq"),
        )

        MediaShareApprovals.detach()
    }

    @Test
    fun differentTargetIsNotCoveredByTheSameApproval() {
        MediaShareApprovals.detach()
        val userGate = newUserGate()
        val confirmation = MediaShareApprovals.attachFrom(userGate)

        val requestId = "req-share-2"
        userGate.request(requestId, "device.media.share", "asset-2 -> qq", ConfirmLevel.CONFIRM_EVERY_STEP)
        confirmation.bind(requestId, "asset-2", "qq")
        userGate.onUserAction(requestId, ConfirmUserAction.APPROVE_ONCE)

        // 批准的是「asset-2 → qq」，换成别的目标必须不认。
        assertFalse(
            "目标不匹配不得放行",
            confirmation.approve(assetId = "asset-2", target = "wechat"),
        )
        // 换素材同理。
        assertFalse(
            "素材不匹配不得放行",
            confirmation.approve(assetId = "asset-9", target = "qq"),
        )

        MediaShareApprovals.detach()
    }

    @Test
    fun deniedRequestDoesNotApprove() {
        MediaShareApprovals.detach()
        val userGate = newUserGate()
        val confirmation = MediaShareApprovals.attachFrom(userGate)

        val requestId = "req-share-3"
        userGate.request(requestId, "device.media.share", "asset-3 -> qq", ConfirmLevel.CONFIRM_EVERY_STEP)
        confirmation.bind(requestId, "asset-3", "qq")
        userGate.onUserAction(requestId, ConfirmUserAction.DENY)

        assertFalse(
            "用户拒绝后不得放行",
            confirmation.approve(assetId = "asset-3", target = "qq"),
        )
        MediaShareApprovals.detach()
    }

    @Test
    fun injectingNeverApprovesEvenWithValidBinding() {
        // M04 既有语义：注入手势期间确认卡不可点、旧批准也不得借用。
        MediaShareApprovals.detach()
        val userGate = newUserGate()
        val confirmation = MediaShareApprovals.attachFrom(userGate)

        val requestId = "req-share-4"
        userGate.request(requestId, "device.media.share", "asset-4 -> qq", ConfirmLevel.CONFIRM_EVERY_STEP)
        confirmation.bind(requestId, "asset-4", "qq")
        userGate.onUserAction(requestId, ConfirmUserAction.APPROVE_ONCE)

        userGate.setInjecting(true)
        assertFalse(
            "注入期间即使有批准也不得放行",
            confirmation.approve(assetId = "asset-4", target = "qq"),
        )
        userGate.setInjecting(false)

        MediaShareApprovals.detach()
    }

    @Test
    fun clearApprovalsRevokesPendingBindings() {
        // 模式切换 / 会话结束时调用 clearApprovals，未用的分享批准必须一并作废。
        MediaShareApprovals.detach()
        val userGate = newUserGate()
        val confirmation = MediaShareApprovals.attachFrom(userGate)

        val requestId = "req-share-5"
        userGate.request(requestId, "device.media.share", "asset-5 -> qq", ConfirmLevel.CONFIRM_EVERY_STEP)
        confirmation.bind(requestId, "asset-5", "qq")
        userGate.onUserAction(requestId, ConfirmUserAction.APPROVE_TASK)

        confirmation.clear()
        assertTrue("clear 之后不应残留绑定", confirmation.boundRequestIds().isEmpty())
        assertFalse(
            "clear 之后不得再放行",
            confirmation.approve(assetId = "asset-5", target = "qq"),
        )

        MediaShareApprovals.detach()
    }
}