package cn.whale.agent.device

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M04 固定测试：确认状态机（纯 JVM，注入假时钟与假渲染器，不依赖 Android 运行时）。
 *
 * 对应 `docs/modules/M04-owner-security-product.md`「确认级别 / 人工接管 / 验收」与
 * `docs/modules/M04-owner-security-development.md`「确认状态机」：
 *
 * 1. 状态机全分支 `READY → NEED_CONFIRM → APPROVED_ONCE / APPROVED_TASK / DENIED / STOPPED / TIMEOUT`；
 * 2. **超时默认拒绝**，不默认继续；
 * 3. 注入手势期间确认卡 `isClickable` 恒 `false`，且旧批准不得被借用；
 * 4. 时钟回拨 fail-closed；
 * 5. 人工接管「需要你操作」路径（只允许「继续」，不读取输入内容）。
 */
class UserGateTest {

    // ---------- 测试替身 ----------

    /** 可推进的假时钟。 */
    private class FakeClock(var now: Long = 1_000_000L) : SessionClock {
        override fun nowMs(): Long = now
    }

    /** 记录渲染调用的假确认卡渲染器（替换真实 Android UI）。 */
    private class FakeRenderer : ConfirmCardRenderer {
        val shown = ArrayList<PendingConfirmation>()
        val clickability = ArrayList<Pair<String, Boolean>>()
        val dismissed = ArrayList<Pair<String, ConfirmState>>()

        override fun show(pending: PendingConfirmation) {
            shown.add(pending)
        }

        override fun setClickable(requestId: String, clickable: Boolean) {
            clickability.add(requestId to clickable)
        }

        override fun dismiss(requestId: String, state: ConfirmState) {
            dismissed.add(requestId to state)
        }

        fun lastClickable(requestId: String): Boolean? =
            clickability.lastOrNull { it.first == requestId }?.second
    }

    private fun gate(
        clock: FakeClock = FakeClock(),
        renderer: FakeRenderer = FakeRenderer(),
        timeoutMs: Long = UserGate.DEFAULT_CONFIRM_TIMEOUT_MS,
    ): UserGate = UserGate(clock, renderer, timeoutMs)

    private fun payload(vararg pairs: Pair<String, String>): JsonObject = buildJsonObject {
        pairs.forEach { (key, value) -> put(key, value) }
    }

    // ---------- 1. READY：无需确认的命令 ----------

    @Test
    fun readyStateIsDefaultAndLowRiskCommandIsAllowedDirectly() {
        val userGate = gate()
        assertEquals(ConfirmState.READY, userGate.state("r-unknown"))
        assertNull(userGate.pending("r-unknown"))

        // 普通命令 → ALLOW_NORMAL：不弹卡、直接放行。
        val decision = userGate.request(
            requestId = "r1",
            command = "device.app.launch",
            payloadSummary = "package_name",
            requestedLevel = ConfirmLevel.ALLOW_NORMAL,
            payload = payload("package_name" to "com.example.app"),
        )
        assertTrue(decision is UserGate.Decision.Allowed)
        assertEquals(ConfirmState.READY, userGate.state("r1"))
    }

    // ---------- 2. NEED_CONFIRM ----------

    @Test
    fun highRiskCommandOpensConfirmCard() {
        val renderer = FakeRenderer()
        val userGate = gate(renderer = renderer)

        val decision = userGate.request(
            requestId = "r2",
            command = "device.sms.send",
            payloadSummary = "number, text",
            requestedLevel = ConfirmLevel.ALLOW_NORMAL,
            payload = payload("number" to "10086", "text" to "hi"),
            taskId = "t1",
        )

        assertTrue(decision is UserGate.Decision.NeedConfirm)
        val pending = (decision as UserGate.Decision.NeedConfirm).pending
        assertEquals("device.sms.send", pending.command)
        assertEquals("number, text", pending.payloadSummary)
        // 固定下限把请求的 ALLOW_NORMAL 顶成每步确认。
        assertEquals(ConfirmLevel.CONFIRM_EVERY_STEP, pending.level)
        assertTrue(pending.clickable)
        assertEquals(ConfirmState.NEED_CONFIRM, userGate.state("r2"))
        assertTrue(userGate.isClickable("r2"))
        assertEquals(1, renderer.shown.size)
        assertTrue("普通确认卡应可点击", renderer.lastClickable("r2") == true)
    }

    @Test
    fun repeatedRequestWhilePendingDoesNotResetTimeout() {
        val clock = FakeClock(now = 1_000_000L)
        val userGate = gate(clock = clock)

        userGate.request("r3", "device.system.settings", "action", ConfirmLevel.ALLOW_NORMAL, payload("action" to "display"))
        val firstExpiry = userGate.pending("r3")!!.expiresAtMs

        clock.now += 30_000L
        val again = userGate.request("r3", "device.system.settings", "action", ConfirmLevel.ALLOW_NORMAL, payload("action" to "display"))

        assertTrue(again is UserGate.Decision.NeedConfirm)
        assertEquals("重复请求不得重置超时", firstExpiry, userGate.pending("r3")!!.expiresAtMs)
    }

    // ---------- 3. APPROVED_ONCE ----------

    @Test
    fun approveOnceAllowsExactlyOneExecution() {
        val userGate = gate()
        userGate.request("r4", "device.sms.send", "number, text", ConfirmLevel.ALLOW_NORMAL, payload("number" to "1", "text" to "x"))

        val state = userGate.onUserAction("r4", ConfirmUserAction.APPROVE_ONCE)
        assertEquals(ConfirmState.APPROVED_ONCE, state)
        assertTrue(state.isApproved)
        // 批准后卡不可点击。
        assertFalse(userGate.isClickable("r4"))

        // 执行完结算 → 回到 READY。
        assertEquals(ConfirmState.APPROVED_ONCE, userGate.consume("r4"))
        assertEquals(ConfirmState.READY, userGate.state("r4"))

        // 同一请求不得复用批准。
        val second = userGate.request("r4", "device.sms.send", "number, text", ConfirmLevel.ALLOW_NORMAL, payload("number" to "1", "text" to "x"))
        assertTrue(second is UserGate.Decision.NeedConfirm)
        assertEquals(ConfirmState.NEED_CONFIRM, userGate.state("r4"))
    }

    // ---------- 4. APPROVED_TASK ----------

    @Test
    fun approveTaskSkipsFirstStepButNeverEveryStep() {
        // 首步确认命令：本任务内批准后不再重复弹卡。
        val userGate = gate()
        userGate.request(
            "r5", "device.ui.input", "target_id", ConfirmLevel.ALLOW_NORMAL,
            payload = payload("target_id" to "search_box"),
            taskId = "t1",
        )
        assertEquals(ConfirmState.APPROVED_TASK, userGate.onUserAction("r5", ConfirmUserAction.APPROVE_TASK))

        val second = userGate.request(
            "r6", "device.ui.input", "target_id", ConfirmLevel.ALLOW_NORMAL,
            payload = payload("target_id" to "search_box"),
            taskId = "t1",
        )
        assertTrue("首步确认命令在本任务内应跳过弹卡", second is UserGate.Decision.Allowed)

        // 每步确认命令：即使本任务内批准过，下一步仍要弹卡。
        val smsGate = gate()
        smsGate.request("s1", "device.sms.send", "number, text", ConfirmLevel.ALLOW_NORMAL, taskId = "t2")
        assertEquals(ConfirmState.APPROVED_TASK, smsGate.onUserAction("s1", ConfirmUserAction.APPROVE_TASK))
        val nextSms = smsGate.request("s2", "device.sms.send", "number, text", ConfirmLevel.ALLOW_NORMAL, taskId = "t2")
        assertTrue("每步确认命令不得跳过弹卡", nextSms is UserGate.Decision.NeedConfirm)
    }

    @Test
    fun taskApprovalIsScopedToTaskId() {
        val userGate = gate()
        userGate.request("r7", "device.ui.tap", "target_id", ConfirmLevel.ALLOW_NORMAL, payload("target_id" to "ok_button"), taskId = "t1")
        userGate.onUserAction("r7", ConfirmUserAction.APPROVE_TASK)

        // 换个任务 → 必须重新确认。
        val otherTask = userGate.request("r8", "device.ui.tap", "target_id", ConfirmLevel.ALLOW_NORMAL, payload("target_id" to "ok_button"), taskId = "t9")
        assertTrue(otherTask is UserGate.Decision.NeedConfirm)

        // 没有任务上下文 → 永远不享受任务级批准。
        val noTask = userGate.request("r9", "device.ui.tap", "target_id", ConfirmLevel.ALLOW_NORMAL, payload("target_id" to "ok_button"), taskId = null)
        assertTrue(noTask is UserGate.Decision.NeedConfirm)

        // 任务结束后失效。
        userGate.endTask("t1")
        val afterEnd = userGate.request("r10", "device.ui.tap", "target_id", ConfirmLevel.ALLOW_NORMAL, payload("target_id" to "ok_button"), taskId = "t1")
        assertTrue(afterEnd is UserGate.Decision.NeedConfirm)
    }

    // ---------- 5. DENIED ----------

    @Test
    fun denyStopsExecutionAndIsTerminal() {
        val userGate = gate()
        userGate.request("r11", "device.sms.send", "number, text", ConfirmLevel.ALLOW_NORMAL)

        val state = userGate.onUserAction("r11", ConfirmUserAction.DENY)
        assertEquals(ConfirmState.DENIED, state)
        assertFalse(state.isApproved)
        assertTrue(state.isTerminal)

        // 终态后任何动作都不再改变状态。
        assertEquals(ConfirmState.DENIED, userGate.onUserAction("r11", ConfirmUserAction.APPROVE_ONCE))
        assertEquals(ConfirmState.DENIED, userGate.state("r11"))
        assertFalse("拒绝后不得可点击", userGate.isClickable("r11"))
    }

    @Test
    fun policyDenyRejectsWithoutShowingCard() {
        val renderer = FakeRenderer()
        val userGate = gate(renderer = renderer)

        val decision = userGate.request("r12", "device.sms.send", "number, text", ConfirmLevel.DENY, payload("number" to "1", "text" to "x"))

        assertTrue(decision is UserGate.Decision.Rejected)
        val rejected = decision as UserGate.Decision.Rejected
        assertEquals(ConfirmState.DENIED, rejected.state)
        // 不新增错误码：沿用 ACTION_REJECTED。
        assertEquals(CommandErrors.ACTION_REJECTED, rejected.errorCode)
        assertTrue("策略拒绝不该弹确认卡", renderer.shown.isEmpty())
        assertEquals(ConfirmState.DENIED, userGate.state("r12"))
    }

    @Test
    fun blankRequestIdIsRejectedWithDeclaredErrorCode() {
        val userGate = gate()
        val decision = userGate.request("  ", "device.sms.send", "s", ConfirmLevel.ALLOW_NORMAL)
        assertTrue(decision is UserGate.Decision.Rejected)
        assertEquals(CommandErrors.INVALID_PAYLOAD, (decision as UserGate.Decision.Rejected).errorCode)
    }

    // ---------- 6. STOPPED ----------

    @Test
    fun stopActionAndStopAllTerminatePendingRequests() {
        val userGate = gate()
        userGate.request("r13", "device.sms.send", "s", ConfirmLevel.ALLOW_NORMAL)
        assertEquals(ConfirmState.STOPPED, userGate.onUserAction("r13", ConfirmUserAction.STOP))
        assertFalse(userGate.state("r13").isApproved)

        // 模式切换/任务中止：全部待确认请求一律 STOPPED。
        val multi = gate()
        multi.request("a1", "device.sms.send", "s", ConfirmLevel.ALLOW_NORMAL)
        multi.request("a2", "device.system.settings", "s", ConfirmLevel.ALLOW_NORMAL)
        assertEquals(2, multi.stopAll())
        assertEquals(ConfirmState.STOPPED, multi.state("a1"))
        assertEquals(ConfirmState.STOPPED, multi.state("a2"))
    }

    // ---------- 7. TIMEOUT：默认拒绝 ----------

    @Test
    fun timeoutDeniesByDefault() {
        val clock = FakeClock(now = 1_000_000L)
        val renderer = FakeRenderer()
        val userGate = gate(clock = clock, renderer = renderer, timeoutMs = 60_000L)

        userGate.request("r14", "device.sms.send", "number, text", ConfirmLevel.ALLOW_NORMAL)
        assertEquals(ConfirmState.NEED_CONFIRM, userGate.state("r14"))

        clock.now += 60_001L
        assertEquals(ConfirmState.TIMEOUT, userGate.checkTimeout("r14"))
        assertFalse("超时默认拒绝，不默认继续", userGate.state("r14").isApproved)
        assertTrue(renderer.dismissed.any { it.first == "r14" && it.second == ConfirmState.TIMEOUT })

        // 超时后再点「确认」也不算数。
        assertEquals(ConfirmState.TIMEOUT, userGate.onUserAction("r14", ConfirmUserAction.APPROVE_ONCE))
    }

    @Test
    fun actionAfterTimeoutIsIgnoredAndDoesNotApprove() {
        val clock = FakeClock(now = 1_000_000L)
        val userGate = gate(clock = clock, timeoutMs = 30_000L)

        userGate.request("r15", "device.system.settings", "action", ConfirmLevel.ALLOW_NORMAL)
        clock.now += 40_000L

        // 直接点批准：超时优先，落成 TIMEOUT 而不是 APPROVED_ONCE。
        assertEquals(ConfirmState.TIMEOUT, userGate.onUserAction("r15", ConfirmUserAction.APPROVE_ONCE))
        assertFalse(userGate.state("r15").isApproved)
    }

    @Test
    fun checkAllTimeoutsSweepsExpiredCards() {
        val clock = FakeClock(now = 1_000_000L)
        val userGate = gate(clock = clock, timeoutMs = 10_000L)

        userGate.request("t1", "device.sms.send", "s", ConfirmLevel.ALLOW_NORMAL)
        clock.now += 5_000L
        userGate.request("t2", "device.ui.tap", "s", ConfirmLevel.ALLOW_NORMAL, payload("target_id" to "delete"))

        clock.now += 6_000L
        val expired = userGate.checkAllTimeouts()
        assertEquals(listOf("t1"), expired)
        assertEquals(ConfirmState.TIMEOUT, userGate.state("t1"))
        assertEquals("还没到点的确认卡不得被提前超时", ConfirmState.NEED_CONFIRM, userGate.state("t2"))
    }

    // ---------- 8. 时钟回拨 fail-closed ----------

    @Test
    fun clockRollbackFailsClosed() {
        val clock = FakeClock(now = 1_000_000L)
        val userGate = gate(clock = clock, timeoutMs = 60_000L)

        userGate.request("r16", "device.sms.send", "s", ConfirmLevel.ALLOW_NORMAL)

        // 时钟大幅回拨（超过容忍度）→ 视为异常，直接拒绝。
        clock.now = clock.now - UserGate.MAX_CLOCK_SKEW_MS - 1_000L
        assertEquals(ConfirmState.TIMEOUT, userGate.checkTimeout("r16"))

        val action = userGate.onUserAction("r16", ConfirmUserAction.APPROVE_ONCE)
        assertEquals(ConfirmState.TIMEOUT, action)
        assertFalse(action.isApproved)
    }

    @Test
    fun smallClockJitterIsTolerated() {
        val clock = FakeClock(now = 1_000_000L)
        val userGate = gate(clock = clock, timeoutMs = 60_000L)

        userGate.request("r17", "device.sms.send", "s", ConfirmLevel.ALLOW_NORMAL)
        clock.now = clock.now - 1_000L // 容忍度内的抖动

        assertEquals(ConfirmState.NEED_CONFIRM, userGate.checkTimeout("r17"))
        assertEquals(ConfirmState.APPROVED_ONCE, userGate.onUserAction("r17", ConfirmUserAction.APPROVE_ONCE))
    }

    // ---------- 9. 注入手势期间不可点击 ----------

    @Test
    fun confirmCardIsNotClickableWhileInjectingGestures() {
        val renderer = FakeRenderer()
        val userGate = gate(renderer = renderer)

        userGate.request("r18", "device.sms.send", "s", ConfirmLevel.ALLOW_NORMAL)
        assertTrue(userGate.isClickable("r18"))

        userGate.setInjecting(true)
        assertTrue(userGate.isInjecting())
        assertFalse("注入手势期间确认卡必须不可点击", userGate.isClickable("r18"))
        assertTrue("渲染器收到的可点击性必须是 false", renderer.lastClickable("r18") == false)

        // AI 在注入期间「点」确认卡必须被丢弃，状态仍是 NEED_CONFIRM。
        assertEquals(ConfirmState.NEED_CONFIRM, userGate.onUserAction("r18", ConfirmUserAction.APPROVE_ONCE))
        assertEquals(ConfirmState.NEED_CONFIRM, userGate.state("r18"))

        // 注入结束后恢复可点击，用户真实点击才生效。
        userGate.setInjecting(false)
        assertTrue(userGate.isClickable("r18"))
        assertEquals(ConfirmState.APPROVED_ONCE, userGate.onUserAction("r18", ConfirmUserAction.APPROVE_ONCE))
    }

    @Test
    fun pendingCardShownDuringInjectionIsNotClickable() {
        val renderer = FakeRenderer()
        val userGate = gate(renderer = renderer)
        userGate.setInjecting(true)

        val decision = userGate.request("r19", "device.sms.send", "s", ConfirmLevel.ALLOW_NORMAL)
        assertTrue(decision is UserGate.Decision.NeedConfirm)
        val pending = (decision as UserGate.Decision.NeedConfirm).pending
        assertFalse("注入期间新建的确认卡也不可点击", pending.clickable)
        assertFalse(userGate.isClickable("r19"))
    }

    @Test
    fun oldApprovalCannotBeBorrowedDuringInjection() {
        // 已有批准 + 注入手势 → 不得放行。
        val userGate = gate()
        val confirmation = OwnerRiskConfirmation(userGate)
        confirmation.grant("device.sms.send")

        assertTrue("注入前已批准应放行", confirmation.confirm("device.sms.send", "number, text"))

        // 仅本次批准已被消费，重新登记后进入注入期，必须拒绝。
        confirmation.grant("device.sms.send")
        userGate.setInjecting(true)
        assertFalse(
            "注入手势期间不得借用旧批准",
            confirmation.confirm("device.sms.send", "number, text"),
        )
        userGate.setInjecting(false)
        assertTrue("注入结束后旧批准仍可用", confirmation.confirm("device.sms.send", "number, text"))
    }

    // ---------- 10. 人工接管：「需要你操作」 ----------

    @Test
    fun manualTakeoverShowsPromptAndIsNotClickable() {
        val renderer = FakeRenderer()
        val userGate = gate(renderer = renderer)

        val decision = userGate.request(
            requestId = "m1",
            command = "device.ui.input",
            payloadSummary = "text, target_id",
            requestedLevel = ConfirmLevel.ALLOW_NORMAL,
            payload = payload("target_id" to "password_field", "text" to "secret"),
            requiresManualTakeover = true,
        )

        assertTrue(decision is UserGate.Decision.NeedConfirm)
        val pending = (decision as UserGate.Decision.NeedConfirm).pending
        assertTrue(pending.manualTakeover)
        assertTrue(userGate.isManualTakeover("m1"))
        // 只显示「需要你操作」，不搬运任何输入内容。
        assertEquals(UserGate.MANUAL_TAKEOVER_SUMMARY, pending.payloadSummary)
        assertFalse(pending.payloadSummary.contains("secret"))
        // 用户必须能点「继续」，因此卡本身可点击；但注入手势期间会被强制关掉。
        assertTrue(pending.clickable)
        assertTrue(userGate.isClickable("m1"))
        userGate.setInjecting(true)
        assertFalse("注入手势期间人工接管卡也不可点击", userGate.isClickable("m1"))
        userGate.setInjecting(false)
        assertTrue(userGate.isClickable("m1"))
    }

    @Test
    fun manualTakeoverOnlyAcceptsContinueNotTaskApproval() {
        val userGate = gate()
        userGate.request(
            "m2", "device.ui.input", "text, target_id", ConfirmLevel.ALLOW_NORMAL,
            payload = payload("target_id" to "password_field"),
            requiresManualTakeover = true,
            taskId = "t1",
        )

        // 人工接管卡上没有「本任务内允许」：这个动作必须被忽略。
        assertEquals(ConfirmState.NEED_CONFIRM, userGate.onUserAction("m2", ConfirmUserAction.APPROVE_TASK))
        assertEquals(ConfirmState.NEED_CONFIRM, userGate.state("m2"))

        // 用户点「继续」→ 只允许继续下一步。
        assertEquals(ConfirmState.APPROVED_ONCE, userGate.onUserAction("m2", ConfirmUserAction.APPROVE_ONCE))
        assertTrue(userGate.state("m2").isApproved)
    }

    @Test
    fun manualTakeoverTimedOutDoesNotContinue() {
        val clock = FakeClock(now = 1_000_000L)
        val userGate = gate(clock = clock, timeoutMs = 30_000L)

        userGate.request(
            "m3", "device.ui.input", "text", ConfirmLevel.ALLOW_NORMAL,
            requiresManualTakeover = true,
        )
        clock.now += 31_000L

        assertEquals(ConfirmState.TIMEOUT, userGate.checkTimeout("m3"))
        assertFalse("人工接管超时不得继续", userGate.state("m3").isApproved)
    }

    // ---------- 11. OwnerRiskConfirmation / OwnerRiskPolicy 可注入 M01 ----------

    @Test
    fun ownerRiskPolicyMatchesM04AuthoritativeList() {
        // 与 M04 文档「高风险命令」逐条一致，可直接替换 RiskPolicies.INTERIM_FALLBACK。
        for (command in ConfirmLevelPolicy.HIGH_RISK_COMMANDS) {
            assertTrue("$command 必须判为高风险", OwnerRiskPolicy.isHighRisk(command))
        }
        for (command in listOf("device.app.list", "device.app.launch", "device.ui.observe", "device.ui.back")) {
            assertFalse("$command 不是 M04 高风险命令", OwnerRiskPolicy.isHighRisk(command))
        }
        // 命令集合与 M01 的过渡默认值一致（替换不改变既有闸门行为）：逐命令比对。
        for (command in DeviceCommands.ALL) {
            assertEquals(
                "M04 清单与 M01 过渡清单在 $command 上不一致",
                RiskPolicies.INTERIM_FALLBACK.isHighRisk(command),
                OwnerRiskPolicy.isHighRisk(command),
            )
        }
    }

    @Test
    fun ownerRiskConfirmationRequiresApprovalAndConsumesOnce() {
        val userGate = gate()
        val confirmation = OwnerRiskConfirmation(userGate)

        // 没有批准 → 拒绝（M01 会回 ACTION_REJECTED）。
        assertFalse(confirmation.confirm("device.sms.send", "number, text"))

        // 非高风险命令直接放行。
        assertTrue(confirmation.confirm("device.app.launch", "package_name"))

        // 仅本次批准只放行一次。
        confirmation.grant("device.sms.send")
        assertTrue(confirmation.confirm("device.sms.send", "number, text"))
        assertFalse("仅本次批准不得复用", confirmation.confirm("device.sms.send", "number, text"))

        // 任务级批准可复用。
        confirmation.grant("device.system.settings", taskId = "t1")
        assertTrue(confirmation.confirm("device.system.settings", "action"))
        assertTrue(confirmation.confirm("device.system.settings", "action"))

        // 清空后一律拒绝。
        confirmation.clearApprovals()
        assertFalse(confirmation.confirm("device.system.settings", "action"))
    }

    @Test
    fun ownerRiskConfirmationRefusesDuplicatesEveryStep() {
        // 每步确认的命令即使有任务级批准，也要求逐步批准：这里验证「一次批准只够一次」。
        val userGate = gate()
        val confirmation = OwnerRiskConfirmation(userGate)
        confirmation.grant("device.ui.tap")
        assertTrue(confirmation.confirm("device.ui.tap", "target_id"))
        assertFalse(confirmation.confirm("device.ui.tap", "target_id"))
    }

    @Test
    fun rendererDismissIsCalledWithFinalState() {
        val renderer = FakeRenderer()
        val userGate = gate(renderer = renderer)

        userGate.request("d1", "device.sms.send", "s", ConfirmLevel.ALLOW_NORMAL)
        userGate.onUserAction("d1", ConfirmUserAction.DENY)

        assertEquals("d1" to ConfirmState.DENIED, renderer.dismissed.single())
        assertTrue("终态后渲染器必须收到不可点击", renderer.lastClickable("d1") == false)
        assertNull("终态后不应再有待确认快照", userGate.pending("d1"))
    }
}
