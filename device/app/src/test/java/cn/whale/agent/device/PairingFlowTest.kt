package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.util.Base64

/**
 * M05 设备端配对流程编排的固定测试（纯 JVM，不依赖 Android 运行时）。
 *
 * 覆盖重点：
 * 1. **六步一步不少**，且顺序不可跳（跳步必被拒绝）；
 * 2. **第 5 步必须用户确认**：`userAccepted=false` 永远到不了 `PAIRED`；
 * 3. **指纹不一致绝不自动接受**（配对码里的 fp 与用户核对值不同 → 拒绝）；
 * 4. **一次性 token 立即失效**，且跨会话（换一个 controller）重放同样被拒；
 * 5. **畸形输入一律拒绝并带明确文案**（不是假成功、也不是空文案）。
 *
 * Keystore 用 JVM `KeyPair` 替身：接口 [DeviceKeyStore] 里没有任何导出私钥的方法，
 * 所以替身也写不出「导出私钥」——私钥不外流这件事由类型系统保证，不靠自觉。
 */
class PairingFlowTest {

    // ---------- 测试替身 ----------

    /** 可推进的假时钟。 */
    private class FakeClock(var now: Long = 1_000_000L) : SessionClock {
        override fun nowMs(): Long = now
    }

    /** JVM `EC P-256` 密钥对替身；**没有任何导出私钥的入口**。 */
    private class FakeKeyStore : DeviceKeyStore {
        private var keyPair: KeyPair? = null
        private var deviceId: String = ""
        var deletedCount: Int = 0
            private set

        override fun exists(): Boolean = keyPair != null

        override fun ensureIdentity(deviceId: String): PublicIdentity {
            if (keyPair == null) {
                val generator = KeyPairGenerator.getInstance("EC")
                generator.initialize(ECGenParameterSpec("secp256r1"))
                keyPair = generator.generateKeyPair()
            }
            this.deviceId = deviceId
            return identity()!!
        }

        override fun identity(): PublicIdentity? {
            val pair = keyPair ?: return null
            val encoded = pair.public.encoded
            return PublicIdentity(
                deviceId = deviceId,
                publicKeyEncoded = Base64.getEncoder().encodeToString(encoded),
                fingerprint = Fingerprints.of(encoded),
            )
        }

        override fun sign(canonicalPayload: ByteArray): String {
            val pair = keyPair ?: throw IllegalStateException("no identity")
            val signature = Signature.getInstance("SHA256withECDSA")
            signature.initSign(pair.private)
            signature.update(canonicalPayload)
            return Base64.getEncoder().encodeToString(signature.sign())
        }

        override fun deleteIdentity() {
            keyPair = null
            deviceId = ""
            deletedCount += 1
        }
    }

    /** 可观察的已失效 token 登记处。 */
    private class SpyConsumedStore : ConsumedTokenStore {
        val keys = LinkedHashSet<String>()

        override fun isConsumed(tokenKey: String): Boolean = tokenKey in keys

        override fun markConsumed(tokenKey: String) {
            keys.add(tokenKey)
        }
    }

    private fun flow(
        clock: FakeClock = FakeClock(),
        keyStore: FakeKeyStore = FakeKeyStore(),
        store: SpyConsumedStore = SpyConsumedStore(),
    ) = PairingFlow(
        controller = PairingController(clock, keyStore, JvmEcdsaVerifier()),
        consumedTokens = store,
        clock = clock,
    )

    @Test
    fun failedRemoteConfirmationDoesNotEnableIdentityAndCanBeRetried() {
        var accept = false
        var registerAccept = false
        val remote = object : PairingFlow.PairingRemote {
            override fun register(deviceUrl: String, token: String, pairingCode: String,
                deviceId: String, publicKeyEncoded: String, deviceFingerprint: String,
                capabilitiesVersion: String, connectionConfig: String, displayName: String
            ): PairingFlow.RemoteResult {
                assertEquals("ABCD2345", pairingCode)
                return PairingFlow.RemoteResult(ok = registerAccept, hostFingerprint = HOST_FP)
            }
            override fun confirm(deviceUrl: String, token: String, deviceId: String,
                userAccepted: Boolean, observedHostFingerprint: String
            ) = PairingFlow.RemoteResult(ok = accept, message = "network unavailable")
            override fun finalize(deviceUrl: String, token: String) = Unit
        }
        val clock = FakeClock()
        val flow = PairingFlow(
            controller = PairingController(clock, FakeKeyStore(), JvmEcdsaVerifier()),
            clock = clock, remote = remote,
        )
        assertTrue(flow.startWithQr(qrText() + "&code=ABCD2345").ok)
        assertTrue(flow.submitCode(flow.pendingToken(), "device-b-01", "test phone").ok)
        assertFalse(flow.prepareRegistration().ok)
        assertEquals(PairingStep.DEVICE_GENERATED_IDENTITY, flow.snapshot().step)
        registerAccept = true
        assertTrue(flow.prepareRegistration().ok)
        assertFalse(flow.confirm(true, HOST_FP).ok)
        assertNull(flow.confirmedIdentity())
        assertFalse(flow.isPaired())
        assertEquals(PairingStep.HOST_SAVED_IDENTITY, flow.snapshot().step)
        accept = true
        assertTrue(flow.confirm(true, HOST_FP).ok)
        assertTrue(flow.finalize().ok)
        assertEquals(PairingStep.TOKEN_REVOKED, flow.snapshot().step)
    }

    private fun tokenKey(token: String): String =
        Fingerprints.of(token.toByteArray(Charsets.UTF_8))

    private fun qrText(
        host: String = "wss://192.168.1.10:8765",
        token: String = "tok-abcdef123456",
        fingerprint: String = HOST_FP,
    ): String = "whale://pair?host=$host&token=$token&fp=$fingerprint"

    // ---------- 第 1 步：收到主机下发的一次性配对码 ----------

    @Test
    fun startWithQrMovesToHostIssuedTokenStep() {
        val flow = flow()
        val outcome = flow.startWithQr(qrText())

        assertTrue("合法二维码必须被接受", outcome.ok)
        assertEquals(PairingStep.HOST_ISSUED_TOKEN, outcome.snapshot.step)
        assertEquals(PairingState.PAIRING, outcome.snapshot.state)
        assertEquals("wss://192.168.1.10:8765", flow.hostUrl)
        assertEquals(HOST_FP, flow.pinnedHostFingerprint)
        assertFalse("wss 不是明文传输", flow.insecureTransport)
        assertEquals("第 1 步：已收到主机的一次性配对码", outcome.snapshot.stepLabel)
    }

    @Test
    fun startWithQrRejectsMalformedInputWithExplicitReason() {
        val flow = flow()
        val cases = mapOf(
            "" to QrPayloadRules.ERROR_EMPTY,
            "https://evil.example/pair" to QrPayloadRules.ERROR_SCHEME,
            "whale://pair?host=wss://h:1&token=tok-abcdef" to QrPayloadRules.ERROR_MISSING_FINGERPRINT,
            "whale://pair?host=ws://10.0.0.5:1&token=tok-abcdef&fp=$HOST_FP" to QrPayloadRules.ERROR_HOST_NOT_WSS,
            "whale://pair?host=wss://h:1&token=t&fp=$HOST_FP" to QrPayloadRules.ERROR_TOKEN_SHAPE,
        )
        for ((input, expectedReason) in cases) {
            val outcome = flow.startWithQr(input)
            assertFalse("畸形输入必须被拒绝: $input", outcome.ok)
            assertEquals("拒绝原因必须明确: $input", expectedReason, outcome.message)
            assertNotNull("拒绝必须带错误码", outcome.errorCode)
        }
    }

    // ---------- 第 2 + 3 步：配对码 + Keystore 身份 ----------

    @Test
    fun submitCodeGeneratesDeviceIdentity() {
        val clock = FakeClock()
        val flow = flow(clock)
        flow.startWithQr(qrText())

        val outcome = flow.submitCode("tok-abcdef123456", "device-b-01", "鲸鱼娘")

        assertTrue(outcome.ok)
        assertEquals(PairingStep.DEVICE_GENERATED_IDENTITY, outcome.snapshot.step)
        assertEquals("device-b-01", outcome.snapshot.deviceId)
        assertTrue(
            "设备指纹必须是 64 位小写十六进制",
            Regex("[0-9a-f]{64}").matches(outcome.snapshot.deviceFingerprint),
        )
    }

    @Test
    fun submitCodeRejectsWrongTokenAndKeepsStepBack() {
        val flow = flow()
        flow.startWithQr(qrText(token = "tok-right-123456"))

        val outcome = flow.submitCode("tok-WRONG-123456", "device-b-01", "鲸鱼娘")

        assertFalse("配对码与主机下发的不一致必须拒绝", outcome.ok)
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, outcome.errorCode)
        assertEquals(PairingStep.HOST_ISSUED_TOKEN, outcome.snapshot.step)
        assertFalse("不得假装已生成身份", outcome.snapshot.isPaired)
    }

    @Test
    fun submitCodeRejectsIllegalDeviceId() {
        val flow = flow()
        flow.startWithQr(qrText())

        val outcome = flow.submitCode("tok-abcdef123456", "x", "")

        assertFalse("device_id 过短必须拒绝", outcome.ok)
        assertEquals(CommandErrors.INVALID_PAYLOAD, outcome.errorCode)
        assertTrue("文案要说明规则", outcome.message.contains("3-80"))
    }

    @Test
    fun submitCodeRejectsBeforeAnyCodeWasScanned() {
        val flow = flow()
        val outcome = flow.submitCode("tok-abcdef123456", "device-b-01", "")

        assertFalse("没扫码就直接提交必须拒绝", outcome.ok)
        assertEquals(CommandErrors.INVALID_PAYLOAD, outcome.errorCode)
        assertNull("不该有任何步骤", outcome.snapshot.step)
    }

    // ---------- 第 4 步：主机保存身份 ----------

    @Test
    fun prepareRegistrationRequiresIdentityFirst() {
        val flow = flow()
        flow.startWithQr(qrText())

        val outcome = flow.prepareRegistration()

        assertFalse("没生成身份不能进第 4 步", outcome.ok)
        assertEquals(CommandErrors.INVALID_PAYLOAD, outcome.errorCode)
        assertTrue("失败原因必须是中文且说清顺序", outcome.message.contains("第 3 步"))
        assertEquals(PairingStep.HOST_ISSUED_TOKEN, outcome.snapshot.step)
    }

    @Test
    fun prepareRegistrationSendsHostUrlAsConnectionConfig() {
        val flow = flow()
        flow.startWithQr(qrText(host = "wss://10.0.2.2:8787"))
        flow.submitCode("tok-abcdef123456", "device-b-01", "鲸鱼娘")

        val outcome = flow.prepareRegistration(capabilitiesVersion = "1", displayName = "鲸鱼娘")

        assertTrue(outcome.ok)
        assertEquals(PairingStep.HOST_SAVED_IDENTITY, outcome.snapshot.step)
        // 连接配置必须来自扫码/手输的那个地址，不能由界面另行指定。
        assertEquals("wss://10.0.2.2:8787", outcome.snapshot.hostUrl)
    }

    // ---------- 第 5 步：必须用户确认指纹 ----------

    @Test
    fun confirmRequiresExplicitUserAcceptance() {
        val flow = flow()
        flow.startWithQr(qrText())
        flow.submitCode("tok-abcdef123456", "device-b-01", "鲸鱼娘")
        flow.prepareRegistration()

        val outcome = flow.confirm(userAccepted = false)

        assertFalse("用户不确认就不能启用（绝不自动过）", outcome.ok)
        assertEquals(CommandErrors.ACTION_REJECTED, outcome.errorCode)
        // controller 语义固定：用户拒绝 → 转 PAIRING_REQUIRED，且 token 一并作废。
        assertEquals(PairingState.PAIRING_REQUIRED, outcome.snapshot.state)
        assertNull("拒绝后不留在任何一步", outcome.snapshot.step)
        assertFalse(outcome.snapshot.isPaired)
        assertNull("userAccepted=false 不得写 confirmed", flow.confirmedIdentity())
    }

    @Test
    fun confirmRejectsWhenHostFingerprintDiffersFromScannedOne() {
        val flow = flow()
        flow.startWithQr(qrText())
        flow.submitCode("tok-abcdef123456", "device-b-01", "鲸鱼娘")
        flow.prepareRegistration()

        // 用户在确认页抄来一个**不同**的主机指纹：这是中间人信号，绝不自动接受。
        val outcome = flow.confirm(userAccepted = true, hostFingerprintInput = "b".repeat(64))

        assertFalse("指纹不一致必须拒绝", outcome.ok)
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, outcome.errorCode)
        assertFalse(outcome.snapshot.isPaired)
        assertNull(flow.confirmedIdentity())
    }

    @Test
    fun confirmAcceptsMatchingFingerprintAndEnablesDevice() {
        val flow = flow()
        flow.startWithQr(qrText())
        flow.submitCode("tok-abcdef123456", "device-b-01", "鲸鱼娘")
        flow.prepareRegistration()

        val outcome = flow.confirm(userAccepted = true, hostFingerprintInput = HOST_FP)

        assertTrue(outcome.ok)
        assertEquals(PairingStep.USER_CONFIRMED_FINGERPRINT, outcome.snapshot.step)
        assertTrue("确认后设备才算启用", outcome.snapshot.isPaired)
        assertEquals(PairingState.PAIRED_OFFLINE, outcome.snapshot.state)
        val confirmed = flow.confirmedIdentity()
        assertNotNull(confirmed)
        assertEquals(HOST_FP, confirmed!!.hostFingerprint)
        assertEquals("device-b-01", confirmed.deviceId)
    }

    @Test
    fun manualEntryHasNoFingerprintSoConfirmNeedsUserInput() {
        val flow = flow()
        // 手输路径没有二维码里的 fp：先只说「这两个框写得对」。
        val started = flow.startWithManual("wss://192.168.1.10:8765", "tok-abcdef123456")
        assertTrue(started.ok)
        assertEquals("", flow.pinnedHostFingerprint)

        flow.submitCode("tok-abcdef123456", "device-b-01", "鲸鱼娘")
        flow.prepareRegistration()

        val missing = flow.confirm(userAccepted = true)
        assertFalse("手输路径没指纹就没法确认", missing.ok)
        assertEquals(CommandErrors.INVALID_PAYLOAD, missing.errorCode)
        assertTrue("文案要说清去哪抄指纹", missing.message.contains("64 位"))

        val typed = flow.confirm(userAccepted = true, hostFingerprintInput = HOST_FP)
        assertTrue("用户填入主机指纹后可以完成", typed.ok)
        assertTrue(typed.snapshot.isPaired)
    }

    // ---------- 第 6 步：一次性 token 立即失效 ----------

    @Test
    fun finalizeRevokesTokenAndMarksItConsumed() {
        val clock = FakeClock()
        val store = SpyConsumedStore()
        val flow = flow(clock, store = store)
        flow.startWithQr(qrText(token = "tok-once-123456"))
        flow.submitCode("tok-once-123456", "device-b-01", "鲸鱼娘")
        flow.prepareRegistration()
        flow.confirm(userAccepted = true, hostFingerprintInput = HOST_FP)

        val outcome = flow.finalize()

        assertTrue(outcome.ok)
        assertEquals(PairingStep.TOKEN_REVOKED, outcome.snapshot.step)
        assertTrue("token 必须登记为已失效", store.isConsumed(tokenKey("tok-once-123456")))
    }

    @Test
    fun finalizeBeforeConfirmIsRejected() {
        val flow = flow()
        flow.startWithQr(qrText())
        flow.submitCode("tok-abcdef123456", "device-b-01", "鲸鱼娘")
        flow.prepareRegistration()

        val outcome = flow.finalize()

        assertFalse("没确认指纹不能算配对完成", outcome.ok)
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, outcome.errorCode)
        assertEquals(PairingStep.HOST_SAVED_IDENTITY, outcome.snapshot.step)
    }

    @Test
    fun reusedQrIsRejectedInANewSession() {
        val store = SpyConsumedStore()
        val text = qrText(token = "tok-reuse-123456")

        val first = flow(store = store)
        first.startWithQr(text)
        first.submitCode("tok-reuse-123456", "device-b-01", "鲸鱼娘")

        // 新会话 = 新 controller：一次性性必须**跨会话**成立，否则用户重扫就能再配一次。
        val second = flow(store = store)
        val replay = second.startWithQr(text)

        assertFalse("已用过的配对码重放必须拒绝", replay.ok)
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, replay.errorCode)
        assertTrue("文案要点明一次性", replay.message.contains("一次性"))
        assertNull(replay.snapshot.step)
    }

    @Test
    fun rescanningSameQrWithinSameSessionIsRejected() {
        val flow = flow()
        val text = qrText(token = "tok-rescan-123456")
        flow.startWithQr(text)
        flow.submitCode("tok-rescan-123456", "device-b-01", "鲸鱼娘")

        // 同一会话里再扫一次：不能因此造出一枚新的未消耗 token。
        val again = flow.startWithQr(text)

        assertFalse("同一会话内重扫必须拒绝", again.ok)
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, again.errorCode)
    }

    // ---------- 全流程与步骤顺序 ----------

    @Test
    fun sixStepsRunInOrderAndEndPaired() {
        val clock = FakeClock()
        val store = SpyConsumedStore()
        val flow = flow(clock, store = store)

        val steps = ArrayList<PairingStep?>()
        val step1 = flow.startWithQr(qrText())
        assertTrue(step1.ok)
        steps.add(step1.snapshot.step)

        steps.add(flow.submitCode("tok-abcdef123456", "device-b-01", "鲸鱼娘").snapshot.step)
        steps.add(flow.prepareRegistration().snapshot.step)
        steps.add(flow.confirm(userAccepted = true, hostFingerprintInput = HOST_FP).snapshot.step)
        steps.add(flow.finalize().snapshot.step)

        assertEquals(
            listOf(
                PairingStep.HOST_ISSUED_TOKEN,
                PairingStep.DEVICE_GENERATED_IDENTITY,
                PairingStep.HOST_SAVED_IDENTITY,
                PairingStep.USER_CONFIRMED_FINGERPRINT,
                PairingStep.TOKEN_REVOKED,
            ),
            steps,
        )
        assertTrue(flow.isPaired())
        assertEquals(PairingState.PAIRED_OFFLINE, flow.controllerState())
    }

    @Test
    fun skippingStepsIsRejected() {
        val flow = flow()
        // 直接确认：没有身份、没有第 4 步，必须拒绝而不是空过。
        // 流程顺序错误优先报，文案要指向「第 4 步之后」。
        val outcome = flow.confirm(userAccepted = true, hostFingerprintInput = HOST_FP)

        assertFalse("跳步必须拒绝", outcome.ok)
        assertEquals(CommandErrors.INVALID_PAYLOAD, outcome.errorCode)
        assertTrue("要指出应先做第 4 步", outcome.message.contains("第 4 步"))
        assertEquals(PairingState.UNPAIRED, outcome.snapshot.state)
        assertNull("跳步后不该有任何步骤", outcome.snapshot.step)
    }

    @Test
    fun stepLabelCoversAllSixSteps() {
        val labels = PairingStep.values().map { step ->
            PairingFlowSnapshot(
                state = PairingState.PAIRING,
                step = step,
                deviceId = "", deviceFingerprint = "", hostFingerprint = "",
                displayName = "", hostUrl = "", isPaired = false, insecureTransport = false,
            ).stepLabel
        }
        assertEquals(6, labels.size)
        for ((index, label) in labels.withIndex()) {
            assertTrue("第 ${index + 1} 步文案要标明步号: $label", label.contains("第 ${index + 1} 步"))
        }
    }

    private companion object {
        /** 固定的主机指纹测试值：64 位小写十六进制（SHA-256 形状）。 */
        val HOST_FP = "a".repeat(64)
    }
}

