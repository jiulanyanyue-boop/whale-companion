package cn.whale.agent.device

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * `media.share` 确认接线的固定测试（纯 JVM，不构造任何 Android 对象）。
 *
 * 对应 task-6 的四条硬要求，逐条断言：
 * 1. **没有批准必须拒绝**：未绑定 / 未登记 / 仍在等确认 → `ACTION_REJECTED`，且后端一次都不许被调用；
 * 2. **有批准必须真放行**：`APPROVE_ONCE` + 绑定同一 `(asset_id, target)` → 后端收到 `confirmed=true`；
 * 3. **注入手势期间绝不放行**，且注入期间**不消费**批准（解除注入后仍然有效）；
 * 4. **批准只用一次**：第二次分享必须重新确认，不得借用旧批准；
 * 5. 拒绝 / 停止 / 超时 / 目标不匹配都作废绑定，复用 [UserGate] 的既有语义而不是另造一套。
 */
class MediaShareConfirmationTest {
    @get:Rule
    val temporary = TemporaryFolder()

    @After
    fun detachProcessApproval() {
        // 进程级登记处必须在每个用例后清空，否则用例之间会互相污染。
        MediaShareApprovals.detach()
    }

    // ---------- 测试替身 ----------

    /** 可控时钟：与 `UserGateTest.FakeClock` 同一形态，便于驱动超时分支。 */
    private class FakeClock(var now: Long = 1_000_000L) : SessionClock {
        override fun nowMs(): Long = now
    }

    /** 记录调用参数的可观察后端；不碰相机 / 网络 / Intent。 */
    private class FakeBackends(private val store: AssetStore) : MediaBackends {
        var shareResult: ShareResult? = null
        val shareCalls = ArrayList<Triple<String, String, Boolean>>()

        override fun capture(lens: String, quality: String): CameraController.CaptureOutcome =
            CameraController.CaptureOutcome.Failed(CommandErrors.NOT_WIRED, "no capture stub")

        override fun pick(mediaType: String, maxItems: Int): PhotoPickerController.PickOutcome =
            PhotoPickerController.PickOutcome.Failed(CommandErrors.NOT_WIRED, "backends must not pick")

        override fun upload(
            assetId: String,
            deviceId: String,
            endpoint: String,
        ): MediaUploadController.UploadOutcome =
            MediaUploadController.UploadOutcome.Failed(CommandErrors.NOT_WIRED, "no upload stub", false)

        override fun share(assetId: String, target: String, confirmed: Boolean): ShareResult {
            shareCalls.add(Triple(assetId, target, confirmed))
            return shareResult ?: ShareResult.Failed(CommandErrors.NOT_WIRED, "no share stub")
        }
    }

    // ---------- 工具 ----------

    private fun assets(): AssetStore = AssetStore(temporary.newFolder())

    private fun sampleEntry(
        store: AssetStore,
        assetId: String = "photo001",
        mime: String = "image/jpeg",
    ): AssetStore.AssetEntry {
        val source = temporary.newFile("src_${System.nanoTime()}.jpg")
        source.writeBytes(byteArrayOf(1, 2, 3, 4))
        return store.register(source, mime, assetId, taskId = "task-1")
    }

    private class Fixture(
        val gate: UserGate,
        val clock: FakeClock,
        val confirmation: MediaShareConfirmation,
        val store: AssetStore,
        val backends: FakeBackends,
        val wiring: MediaWiringImpl,
    )

    /**
     * 组装一套「M04 UserGate + 确认适配层 + MediaWiringImpl」的真实接线。
     *
     * `MediaRoutes` 用默认的 `shareConfirmed = { _, _ -> false }`，因此**放行完全依赖 M04 批准**——
     * 这正是生产路径的形状：路由层不给默认放行，媒体层自己问锁屏背后的用户批准。
     */
    private fun fixture(
        registerProcessWide: Boolean = false,
        entryAssetId: String = "photo001",
        successShare: Boolean = true,
    ): Fixture {
        val clock = FakeClock()
        val gate = UserGate(clock = clock)
        val confirmation = MediaShareConfirmation(gate)
        if (registerProcessWide) MediaShareApprovals.attach(confirmation)

        val store = assets()
        val entry = sampleEntry(store, entryAssetId)
        val backends = FakeBackends(store)
        if (successShare) backends.shareResult = ShareResult.Sent(store.evidence(entry), "com.example.chat")

        val wiring = MediaWiringImpl(
            assets = store,
            uploadEndpoint = "https://host.example",
            backends = backends,
            picker = null,
            shareApproval = if (registerProcessWide) null else confirmation,
        )
        return Fixture(gate, clock, confirmation, store, backends, wiring)
    }

    /** 走完整真实链路：弹卡 → 绑定 → 用户点击，返回 gate 的状态。 */
    private fun askUser(
        fixture: Fixture,
        requestId: String,
        assetId: String,
        target: String,
        action: ConfirmUserAction = ConfirmUserAction.APPROVE_ONCE,
    ): ConfirmState {
        val decision = fixture.gate.request(
            requestId = requestId,
            command = "device.media.share",
            payloadSummary = "asset_id, target",
            requestedLevel = ConfirmLevel.ALLOW_NORMAL,
            payload = buildJsonObject {
                put("asset_id", assetId)
                put("target", target)
            },
        )
        // 命令级固定下限压住请求级别：media.share 一定需要确认，绝不能 ALLOW_NORMAL 直接放行。
        assertTrue("media.share 必须弹确认卡，实际=$decision", decision is UserGate.Decision.NeedConfirm)
        fixture.confirmation.bind(requestId, assetId, target)
        return fixture.gate.onUserAction(requestId, action)
    }

    private fun envelope(command: String, payload: JsonObject = buildJsonObject { }): DeviceEnvelope =
        DeviceEnvelope(
            protocol_version = 1,
            request_id = "req-share-001",
            device_id = "device-1",
            command = command,
            deadline_ms = 30_000,
            idempotency_key = "share-001",
            payload = payload,
        )

    private fun shareEnvelope(assetId: String, target: String): DeviceEnvelope =
        envelope(
            "device.media.share",
            buildJsonObject {
                put("asset_id", assetId)
                put("target", target)
            },
        )

    // ---------- 1. 没有批准必须 fail-closed ----------

    @Test
    fun shareWithoutAnyApprovalIsRejectedAndBackendIsNeverCalled() {
        val f = fixture()
        assertFalse(MediaShareApprovals.attached())
        assertTrue("未绑定任何请求时不得放行", f.confirmation.boundRequestIds().isEmpty())

        val outcome = f.wiring.share("photo001", "com.example.chat", confirmed = false) as ShareResult.Failed
        assertEquals(CommandErrors.ACTION_REJECTED, outcome.code)
        // 后端没被调用 = 没有 prepareShare、没有 startActivity。
        assertTrue(f.backends.shareCalls.isEmpty())
    }

    @Test
    fun pendingConfirmationIsNotYetAnApproval() {
        val f = fixture()
        // 只弹卡、还没点：必须拒绝，但这个「拒绝」不能把绑定吃掉。
        val decision = f.gate.request(
            "r-pending",
            "device.media.share",
            "asset_id, target",
            ConfirmLevel.ALLOW_NORMAL,
            buildJsonObject { put("asset_id", "photo001"); put("target", "com.example.chat") },
        )
        assertTrue(decision is UserGate.Decision.NeedConfirm)
        f.confirmation.bind("r-pending", "photo001", "com.example.chat")

        val outcome = f.wiring.share("photo001", "com.example.chat", confirmed = false) as ShareResult.Failed
        assertEquals(CommandErrors.ACTION_REJECTED, outcome.code)
        assertTrue(f.backends.shareCalls.isEmpty())
        // 绑定保留：用户点完「仅本次」之后仍然能放行。
        assertTrue(f.confirmation.boundRequestIds().contains("r-pending"))

        assertEquals(ConfirmState.APPROVED_ONCE, f.gate.onUserAction("r-pending", ConfirmUserAction.APPROVE_ONCE))
        val sent = f.wiring.share("photo001", "com.example.chat", confirmed = false)
        assertTrue("用户真实点击后必须真放行，实际=$sent", sent is ShareResult.Sent)
    }

    @Test
    fun deniedByUserInvalidatesBindingForGood() {
        val f = fixture()
        assertEquals(ConfirmState.DENIED, askUser(f, "r-deny", "photo001", "com.example.chat", ConfirmUserAction.DENY))
        assertTrue("拒绝后绑定必须作废", f.confirmation.boundRequestIds().isEmpty())

        val outcome = f.wiring.share("photo001", "com.example.chat", confirmed = false) as ShareResult.Failed
        assertEquals(CommandErrors.ACTION_REJECTED, outcome.code)
        assertTrue(f.backends.shareCalls.isEmpty())
    }

    @Test
    fun stoppedSessionInvalidatesBinding() {
        val f = fixture()
        assertEquals(ConfirmState.STOPPED, askUser(f, "r-stop", "photo001", "com.example.chat", ConfirmUserAction.STOP))
        assertTrue(f.confirmation.boundRequestIds().isEmpty())
        val outcome = f.wiring.share("photo001", "com.example.chat", confirmed = false) as ShareResult.Failed
        assertEquals(CommandErrors.ACTION_REJECTED, outcome.code)
    }

    @Test
    fun timedOutConfirmationIsRejectedLikeM04() {
        val f = fixture()
        f.gate.request(
            "r-timeout",
            "device.media.share",
            "asset_id, target",
            ConfirmLevel.ALLOW_NORMAL,
            buildJsonObject { put("asset_id", "photo001"); put("target", "com.example.chat") },
        )
        f.confirmation.bind("r-timeout", "photo001", "com.example.chat")
        // 越过 UserGate 默认 60 秒超时：超时必须默认拒绝，不得默认继续。
        f.clock.now += UserGate.DEFAULT_CONFIRM_TIMEOUT_MS + 1_000

        val outcome = f.wiring.share("photo001", "com.example.chat", confirmed = false) as ShareResult.Failed
        assertEquals(CommandErrors.ACTION_REJECTED, outcome.code)
        assertTrue(f.backends.shareCalls.isEmpty())
        assertTrue("超时后绑定必须作废", f.confirmation.boundRequestIds().isEmpty())
        assertEquals(ConfirmState.TIMEOUT, f.gate.state("r-timeout"))
    }

    @Test
    fun approvalForAnotherTargetOrAssetDoesNotLeak() {
        val f = fixture()
        assertEquals(
            ConfirmState.APPROVED_ONCE,
            askUser(f, "r-scope", "photo001", "com.example.chat"),
        )
        // 同一命令但目标不同：批准只覆盖它绑定的那一对，不能扩大成命令级放行。
        val otherTarget = f.wiring.share("photo001", "com.other.app", confirmed = false) as ShareResult.Failed
        assertEquals(CommandErrors.ACTION_REJECTED, otherTarget.code)
        val otherAsset = f.wiring.share("photo002", "com.example.chat", confirmed = false) as ShareResult.Failed
        assertEquals(CommandErrors.ACTION_REJECTED, otherAsset.code)
        assertTrue(f.backends.shareCalls.isEmpty())

        // 目标/素材与绑定一致时仍然放行一次。
        assertTrue(f.wiring.share("photo001", "com.example.chat", confirmed = false) is ShareResult.Sent)
    }

    // ---------- 2. 有批准必须真放行 ----------

    @Test
    fun approvedShareIsActuallySentWithMetadata() {
        val f = fixture()
        assertEquals(ConfirmState.APPROVED_ONCE, askUser(f, "r-ok", "photo001", "com.example.chat"))

        val response = MediaRoutes(f.wiring, { _, _ -> false }).route(shareEnvelope("photo001", "com.example.chat"))!!
        assertEquals("ok", response.status)
        assertEquals(Triple("photo001", "com.example.chat", true), f.backends.shareCalls.single())
        assertEquals(setOf("asset_id", "mime", "size", "sha256", "target"), response.evidence.keys)
        assertEquals("com.example.chat", response.evidence["target"])
        assertFalse("evidence 不得泄漏真实路径", response.evidence.values.any { it.contains(temporary.root.absolutePath) })
    }

    @Test
    fun taskLevelApprovalAlsoWiresThroughTheGate() {
        val f = fixture()
        // media.share 在带 target 的 payload 下固定为「每步确认」，因此 APPROVE_TASK 也只放行本次。
        val state = askUser(f, "r-task", "photo001", "com.example.chat", ConfirmUserAction.APPROVE_TASK)
        assertTrue("用户点了本任务内允许，状态=$state", state.isApproved)
        assertTrue(f.wiring.share("photo001", "com.example.chat", confirmed = false) is ShareResult.Sent)
    }

    @Test
    fun routerSideConfirmationStillWorksAsASecondSource() {
        // 宿主自己确认过的分享（路由层 confirmed=true）不被削弱：后端照旧只收到 true。
        val f = fixture()
        assertTrue(f.wiring.share("photo001", "com.example.chat", confirmed = true) is ShareResult.Sent)
        assertEquals(Triple("photo001", "com.example.chat", true), f.backends.shareCalls.single())
        // 而且不能顺手「偷用」M04 的批准：没有任何绑定时 boundRequestIds 仍为空。
        assertTrue(f.confirmation.boundRequestIds().isEmpty())
    }

    @Test
    fun processWideRegistrationIsHonouredAndRevocable() {
        val f = fixture(registerProcessWide = true)
        // 构造时没直接注入，但进程登记处已登记 → 晚于装配登记也能读到。
        assertTrue(MediaShareApprovals.attached())
        assertEquals(ConfirmState.APPROVED_ONCE, askUser(f, "r-proc", "photo001", "com.example.chat"))
        assertTrue(f.wiring.share("photo001", "com.example.chat", confirmed = false) is ShareResult.Sent)

        MediaShareApprovals.detach()
        assertFalse(MediaShareApprovals.attached())
    }

    @Test
    fun attachedApprovalHelperWiresTheGateInOneLine() {
        val clock = FakeClock()
        val gate = UserGate(clock = clock)
        val confirmation = MediaShareApprovals.attachFrom(gate)
        assertNotNull(MediaShareApprovals.current())
        assertTrue(MediaShareApprovals.attached())

        val store = assets()
        val entry = sampleEntry(store, "photo001")
        val backends = FakeBackends(store).apply {
            shareResult = ShareResult.Sent(store.evidence(entry), "com.example.chat")
        }
        val wiring = MediaWiringImpl(store, "https://host.example", backends, null, null)

        assertEquals(
            ConfirmState.APPROVED_ONCE,
            askUser(Fixture(gate, clock, confirmation, store, backends, wiring), "r-helper", "photo001", "com.example.chat"),
        )
        assertTrue(wiring.share("photo001", "com.example.chat", confirmed = false) is ShareResult.Sent)
    }

    // ---------- 3. 注入手势期间绝不放行 ----------

    @Test
    fun injectingRejectsShareAndDoesNotConsumeTheApproval() {
        val f = fixture()
        assertEquals(ConfirmState.APPROVED_ONCE, askUser(f, "r-inject", "photo001", "com.example.chat"))

        // 无障碍注入层开始注入：任何「顺手的分享」都必须被丢弃。
        f.gate.setInjecting(true)
        assertTrue(f.confirmation.injecting)
        val blocked = f.wiring.share("photo001", "com.example.chat", confirmed = false) as ShareResult.Failed
        assertEquals(CommandErrors.ACTION_REJECTED, blocked.code)
        assertTrue(f.backends.shareCalls.isEmpty())
        // 关键：注入期间不得「借走」批准 —— 绑定与状态都必须原样保留。
        assertTrue(f.confirmation.boundRequestIds().contains("r-inject"))
        assertEquals(ConfirmState.APPROVED_ONCE, f.gate.state("r-inject"))

        // 注入结束，批准仍然有效且仍然只用一次。
        f.gate.setInjecting(false)
        assertTrue(f.wiring.share("photo001", "com.example.chat", confirmed = false) is ShareResult.Sent)
    }

    @Test
    fun injectingUserActionIsDiscardedByUserGate() {
        val f = fixture()
        f.gate.request(
            "r-inject-click",
            "device.media.share",
            "asset_id, target",
            ConfirmLevel.ALLOW_NORMAL,
            buildJsonObject { put("asset_id", "photo001"); put("target", "com.example.chat") },
        )
        f.confirmation.bind("r-inject-click", "photo001", "com.example.chat")
        f.gate.setInjecting(true)
        assertEquals(
            "注入期间「点击」必须被 UserGate 丢弃",
            ConfirmState.NEED_CONFIRM,
            f.gate.onUserAction("r-inject-click", ConfirmUserAction.APPROVE_ONCE),
        )
        val outcome = f.wiring.share("photo001", "com.example.chat", confirmed = false) as ShareResult.Failed
        assertEquals(CommandErrors.ACTION_REJECTED, outcome.code)

        f.gate.setInjecting(false)
        assertEquals(ConfirmState.APPROVED_ONCE, f.gate.onUserAction("r-inject-click", ConfirmUserAction.APPROVE_ONCE))
        assertTrue(f.wiring.share("photo001", "com.example.chat", confirmed = false) is ShareResult.Sent)
    }

    // ---------- 4. 批准只用一次 ----------

    @Test
    fun approvalIsConsumedExactlyOnce() {
        val f = fixture()
        assertEquals(ConfirmState.APPROVED_ONCE, askUser(f, "r-once", "photo001", "com.example.chat"))

        assertTrue(f.wiring.share("photo001", "com.example.chat", confirmed = false) is ShareResult.Sent)
        // 与 UserGate.consume 对齐：APPROVED_ONCE → READY，绑定同时注销。
        assertEquals(ConfirmState.READY, f.gate.state("r-once"))
        assertTrue(f.confirmation.boundRequestIds().isEmpty())

        val second = f.wiring.share("photo001", "com.example.chat", confirmed = false) as ShareResult.Failed
        assertEquals(CommandErrors.ACTION_REJECTED, second.code)
        assertEquals("后端只能被调用一次", 1, f.backends.shareCalls.size)
    }

    @Test
    fun bindRejectsBlankIdentifiersInsteadOfGrantingWideApproval() {
        val f = fixture(registerProcessWide = true)
        // 空 id / 空素材 / 空目标：不建立绑定，随后必然拒绝（不得变成「任意分享都放行」）。
        assertFalse(f.confirmation.bind("", "photo001", "com.example.chat"))
        assertFalse(f.confirmation.bind("r-blank", "  ", "com.example.chat"))
        assertFalse(f.confirmation.bind("r-blank2", "photo001", ""))
        assertTrue(f.confirmation.boundRequestIds().isEmpty())

        val outcome = f.wiring.share("photo001", "com.example.chat", confirmed = false) as ShareResult.Failed
        assertEquals(CommandErrors.ACTION_REJECTED, outcome.code)
        assertTrue(f.backends.shareCalls.isEmpty())
    }

    @Test
    fun unbindAndClearRevokePendingApprovals() {
        val f = fixture(registerProcessWide = true)
        assertEquals(ConfirmState.APPROVED_ONCE, askUser(f, "r-revoke", "photo001", "com.example.chat"))
        // 会话结束 / 模式切换：宿主主动撤销，批准立即失效。
        f.confirmation.clear()
        assertTrue(f.confirmation.boundRequestIds().isEmpty())
        val outcome = f.wiring.share("photo001", "com.example.chat", confirmed = false) as ShareResult.Failed
        assertEquals(CommandErrors.ACTION_REJECTED, outcome.code)
    }

    @Test
    fun unbindSingleRequestOnlyDropsThatBinding() {
        val f = fixture()
        assertEquals(ConfirmState.APPROVED_ONCE, askUser(f, "r-keep", "photo001", "com.example.chat"))
        assertEquals(
            ConfirmState.APPROVED_ONCE,
            askUser(f, "r-drop", "photo002", "com.example.chat"),
        )
        f.confirmation.unbind("r-drop")
        assertFalse("被撤销的绑定不得再放行", f.wiring.share("photo002", "com.example.chat", confirmed = false) is ShareResult.Sent)
        assertTrue("未被撤销的绑定仍然有效", f.wiring.share("photo001", "com.example.chat", confirmed = false) is ShareResult.Sent)
    }
}
