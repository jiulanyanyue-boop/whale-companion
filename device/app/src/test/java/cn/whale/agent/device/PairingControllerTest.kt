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
 * M05 固定测试：配对六步、连接状态机、退避、token 一次性、指纹变化。
 *
 * 纯 JVM：Keystore 用 JVM `KeyPair` 替身（不依赖 Android 运行时），
 * 验签用真实 `java.security.Signature`。
 */
class PairingControllerTest {

    // ---------- 测试替身 ----------

    /** 可推进的假时钟。 */
    private class FakeClock(var now: Long = 1_000_000L) : SessionClock {
        override fun nowMs(): Long = now
    }

    /**
     * Keystore 替身：用 JVM `EC P-256` 密钥对模拟。
     *
     * 刻意**不实现**任何"导出私钥"的方法——接口里就没有，所以替身也写不出来。
     */
    private class FakeKeyStore : DeviceKeyStore {
        private var keyPair: KeyPair? = null
        private var deviceId: String = ""
        var deleted: Int = 0

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
            deleted += 1
            }
    }

    private class FakeCode(var next: String = "000000")

    private fun controller(
        clock: FakeClock = FakeClock(),
        keyStore: DeviceKeyStore = FakeKeyStore(),
    ): PairingController = PairingController(clock, keyStore, JvmEcdsaVerifier())

    /** 走到"已配对"的完整六步。 */
    private fun pairedController(
        clock: FakeClock = FakeClock(),
        keyStore: DeviceKeyStore = FakeKeyStore(),
        hostFingerprint: String = "a".repeat(64),
    ): PairingController {
        val controller = controller(clock, keyStore)
        controller.acceptHostToken("tok-1", clock.now + 600_000)
        controller.submitCodeAndCreateIdentity("tok-1", "device-1", "鲸鱼娘")
        controller.prepareHostRegistration("caps-1", "wss://host.example.com/ws")
        controller.confirmFingerprints(hostFingerprint, userAccepted = true, displayName = "鲸鱼娘")
        controller.finalizePairing()
        return controller
    }

    // ---------- 六步配对 ----------

    @Test
    fun sixStepsRunInOrder() {
        val clock = FakeClock()
        val controller = controller(clock)

        val step1 = controller.acceptHostToken("tok-1", clock.now + 600_000)
        assertEquals(PairingStep.HOST_ISSUED_TOKEN, step1.step)
        assertEquals(PairingState.PAIRING, controller.state)

        val steps = ArrayList<PairingStep?>()
        steps.add(controller.submitCodeAndCreateIdentity("tok-1", "device-1").step)
        steps.add(controller.prepareHostRegistration("caps", "cfg").step)
        steps.add(controller.confirmFingerprints("a".repeat(64), userAccepted = true).step)
        steps.add(controller.finalizePairing().step)

        assertEquals(PairingStep.DEVICE_GENERATED_IDENTITY, steps[0])
        assertEquals(PairingStep.HOST_SAVED_IDENTITY, steps[1])
        assertEquals(PairingStep.USER_CONFIRMED_FINGERPRINT, steps[2])
        assertEquals(PairingStep.TOKEN_REVOKED, steps[3])
        assertTrue(controller.isPaired)
        assertEquals(PairingState.PAIRED_OFFLINE, controller.state)
    }

    @Test
    fun pairingStepOrderIsFixed() {
        assertEquals(listOf(1, 2, 3, 4, 5, 6), PairingStep.entries.map { it.index })
        assertEquals(6, PairingStep.entries.size)
    }

    @Test
    fun wrongCodeIsRejected() {
        val clock = FakeClock()
        val controller = controller(clock)
        controller.acceptHostToken("tok-1", clock.now + 600_000)
        val outcome = controller.submitCodeAndCreateIdentity("wrong", "device-1")
        assertTrue(outcome.isRejected)
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, outcome.errorCode)
    }

    @Test
    fun expiredTokenIsRejected() {
        val clock = FakeClock()
        val controller = controller(clock)
        controller.acceptHostToken("tok-1", clock.now + 1_000)
        clock.now += 2_000
        val outcome = controller.submitCodeAndCreateIdentity("tok-1", "device-1")
        assertTrue(outcome.isRejected)
        assertEquals(CommandErrors.EXPIRED, outcome.errorCode)
    }

    @Test
    fun missingTokenIsRejected() {
        val controller = controller()
        val outcome = controller.submitCodeAndCreateIdentity("tok-1", "device-1")
        assertTrue(outcome.isRejected)
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, outcome.errorCode)
    }

    @Test
    fun blankDeviceIdIsRejected() {
        val clock = FakeClock()
        val controller = controller(clock)
        controller.acceptHostToken("tok-1", clock.now + 600_000)
        val outcome = controller.submitCodeAndCreateIdentity("tok-1", "")
        assertTrue(outcome.isRejected)
        assertEquals(CommandErrors.INVALID_PAYLOAD, outcome.errorCode)
    }

    @Test
    fun malformedHostFingerprintIsRejected() {
        val clock = FakeClock()
        val controller = controller(clock)
        controller.acceptHostToken("tok-1", clock.now + 600_000)
        controller.submitCodeAndCreateIdentity("tok-1", "device-1")
        controller.prepareHostRegistration("caps", "cfg")
        val outcome = controller.confirmFingerprints("short", userAccepted = true)
        assertTrue(outcome.isRejected)
        assertEquals(CommandErrors.INVALID_PAYLOAD, outcome.errorCode)
    }

    /** 用户没点确认 → 不启用，且转 PAIRING_REQUIRED。 */
    @Test
    fun userRejectionKeepsDeviceUnpaired() {
        val clock = FakeClock()
        val controller = controller(clock)
        controller.acceptHostToken("tok-1", clock.now + 600_000)
        controller.submitCodeAndCreateIdentity("tok-1", "device-1")
        controller.prepareHostRegistration("caps", "cfg")
        val outcome = controller.confirmFingerprints("a".repeat(64), userAccepted = false)
        assertTrue(outcome.isRejected)
        assertEquals(CommandErrors.ACTION_REJECTED, outcome.errorCode)
        assertEquals(PairingState.PAIRING_REQUIRED, controller.state)
        assertFalse(controller.isPaired)
    }

    @Test
    fun finalizeBeforeConfirmIsRejected() {
        val clock = FakeClock()
        val controller = controller(clock)
        controller.acceptHostToken("tok-1", clock.now + 600_000)
        controller.submitCodeAndCreateIdentity("tok-1", "device-1")
        val outcome = controller.finalizePairing()
        assertTrue(outcome.isRejected)
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, outcome.errorCode)
    }

    // ---------- token 一次性 ----------

    @Test
    fun tokenIsSingleUse() {
        val clock = FakeClock()
        val controller = controller(clock)
        controller.acceptHostToken("tok-1", clock.now + 600_000)
        val first = controller.submitCodeAndCreateIdentity("tok-1", "device-1")
        assertTrue(first.accepted)

        // 同一次配对流程里重复提交同一 token：必须被拒。
        val second = controller.submitCodeAndCreateIdentity("tok-1", "device-1")
        assertTrue(second.isRejected)
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, second.errorCode)
    }

    @Test
    fun tokenIsRevokedImmediatelyAfterPairing() {
        val token = PairingToken("tok", expiresAtMs = 0)
        assertFalse(token.isUsed)
        assertTrue(token.consume(1000))
        assertTrue(token.isUsed)
        // 立即失效：第二次消耗必然失败。
        assertFalse(token.consume(1000))
        assertFalse(token.consume(2000))
    }

    @Test
    fun revokeMakesTokenUnusableEvenIfNeverConsumed() {
        val token = PairingToken("tok", expiresAtMs = 0)
        token.revoke()
        assertFalse(token.consume(1))
    }

    @Test
    fun finalizeIsIdempotentAndKeepsTokenDead() {
        val clock = FakeClock()
        val controller = equipped(clock)
        assertEquals(PairingStep.TOKEN_REVOKED, controller.finalizePairing().step)
        // 再调一次不能复活 token。
        val again = controller.submitCodeAndCreateIdentity("tok-1", "device-2")
        assertTrue(again.isRejected)
    }

    private fun equipped(clock: FakeClock): PairingController {
        val controller = controller(clock)
        controller.acceptHostToken("tok-1", clock.now + 600_000)
        controller.submitCodeAndCreateIdentity("tok-1", "device-1")
        controller.prepareHostRegistration("caps", "cfg")
        controller.confirmFingerprints("a".repeat(64), userAccepted = true)
        return controller
    }

    // ---------- 连接状态机 ----------

    @Test
    fun connectionStateMachine() {
        val clock = FakeClock()
        val controller = pairedController(clock)
        assertEquals(ConnectionState.PAIRED_OFFLINE, controller.connection)

        assertEquals(ConnectionState.CONNECTING, controller.beginConnect().connectionState)
        assertEquals(ConnectionState.ONLINE, controller.markOnline().connectionState)

        // ONLINE → BACKOFF → CONNECTING
        val offline = controller.onDisconnected()
        assertEquals(ConnectionState.BACKOFF, offline.connectionState)
        assertEquals(ConnectionState.CONNECTING, controller.retryAfterBackoff().connectionState)
    }

    @Test
    fun unpairedDeviceCannotConnect() {
        val controller = controller()
        val outcome = controller.beginConnect()
        assertTrue(outcome.isRejected)
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, outcome.errorCode)
    }

    @Test
    fun revokedPairingIsTerminal() {
        val clock = FakeClock()
        val controller = pairedController(clock)
        val outcome = controller.revoke()
        assertEquals(PairingState.REVOKED, outcome.pairingState)
        assertTrue(controller.state.isTerminal)
        // 吊销后不能再开始配对。
        val reissue = controller.acceptHostToken("tok-2", clock.now + 600_000)
        assertTrue(reissue.isRejected)
        assertFalse(controller.isPaired)
    }

    // ---------- 退避 ----------

    @Test
    fun backoffSequenceIsFixed() {
        assertEquals(listOf(1, 2, 4, 8, 16, 30), BackoffPolicy.DELAYS_SECONDS)
        assertEquals(1, BackoffPolicy.delayFor(1))
        assertEquals(2, BackoffPolicy.delayFor(2))
        assertEquals(4, BackoffPolicy.delayFor(3))
        assertEquals(8, BackoffPolicy.delayFor(4))
        assertEquals(16, BackoffPolicy.delayFor(5))
        assertEquals(30, BackoffPolicy.delayFor(6))
        // 封顶 30 秒，不再增长。
        assertEquals(30, BackoffPolicy.delayFor(7))
        assertEquals(30, BackoffPolicy.delayFor(100))
        assertEquals(30, BackoffPolicy.MAX_DELAY_SECONDS)
    }

    @Test
    fun backoffMsMatchesSeconds() {
        assertEquals(1_000L, BackoffPolicy.delayMsFor(1))
        assertEquals(30_000L, BackoffPolicy.delayMsFor(6))
    }

    @Test
    fun disconnectsWalkTheBackoffCurve() {
        val clock = FakeClock()
        val controller = pairedController(clock)
        controller.beginConnect()
        controller.markOnline()

        val delays = ArrayList<Long>()
        for (attempt in 1..7) {
            controller.onDisconnected()
            delays.add(controller.currentBackoffMs())
            controller.retryAfterBackoff()
        }
        assertEquals(listOf(1_000L, 2_000L, 4_000L, 8_000L, 16_000L, 30_000L, 30_000L), delays)
    }

    /** heartbeat 后退避**清零**：再来一次断线又从 1 秒开始。 */
    @Test
    fun heartbeatResetsBackoff() {
        val clock = FakeClock()
        val controller = pairedController(clock)
        controller.beginConnect()
        controller.markOnline()

        // 连续三轮「断开 → 退避 → 重试」，但每轮都停在 BACKOFF 才读当前退避值。
        val observed = ArrayList<Long>()
        repeat(3) {
            controller.onDisconnected()
            observed.add(controller.currentBackoffMs())
            controller.retryAfterBackoff()
        }
        assertEquals(listOf(1_000L, 2_000L, 4_000L), observed)
        assertEquals(3, controller.backoffAttempts)

        // 此时处于 CONNECTING，没有待等待的退避。
        assertEquals(0L, controller.currentBackoffMs())

        controller.onHeartbeat()
        assertEquals(0, controller.backoffAttempts)
        assertEquals(ConnectionState.ONLINE, controller.connection)

        controller.onDisconnected()
        assertEquals(1, controller.backoffAttempts)
        assertEquals(1_000L, controller.currentBackoffMs())
    }

    @Test
    fun noBackoffWhenNotInBackoffState() {
        val controller = pairedController()
        assertEquals(0L, controller.currentBackoffMs())
    }

    // ---------- 主机指纹变化：绝不自动接受 ----------

    @Test
    fun hostFingerprintChangeForcesRepairing() {
        val clock = FakeClock()
        val keyStore = FakeKeyStore()
        val controller = pairedController(clock, keyStore)

        val outcome = controller.onHostFingerprintChanged("b".repeat(64))
        assertTrue(outcome.isRejected)
        assertEquals(PairingState.PAIRING_REQUIRED, controller.state)
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, outcome.errorCode)
        assertFalse(controller.isPaired)
        // 身份材料被清掉：重新配对必须换新密钥。
        assertNull(controller.currentIdentity)
        assertEquals(1, keyStore.deleted)
    }

    @Test
    fun unchangedFingerprintIsNoOp() {
        val clock = FakeClock()
        val controller = pairedController(clock, hostFingerprint = "a".repeat(64))
        val outcome = controller.onHostFingerprintChanged("a".repeat(64))
        assertTrue(outcome.accepted)
        assertEquals(PairingState.PAIRED_OFFLINE, controller.state)
        assertTrue(controller.isPaired)
    }

    @Test
    fun leaseExpiryForcesRepairing() {
        val controller = pairedController()
        val outcome = controller.onLeaseExpired()
        assertTrue(outcome.isRejected)
        assertEquals(PairingState.PAIRING_REQUIRED, controller.state)
        assertEquals(CommandErrors.EXPIRED, outcome.errorCode)
    }

    // ---------- 验签接入 ----------

    @Test
    fun envelopeSignatureIsVerifiedThroughController() {
        val clock = FakeClock()
        val keyStore = FakeKeyStore()
        val controller = pairedController(clock, keyStore, hostFingerprint = "a".repeat(64))
        val identity = controller.currentIdentity!!
        val fields = mapOf("command" to "device.device_status", "counter" to 1)
        val signature = keyStore.sign(CanonicalJson.encodeUnsignedBytes(fields))

        val outcome = controller.checkEnvelopeSignature(
            envelopeFields = fields + mapOf("signature" to signature),
            observedHostFingerprint = "a".repeat(64),
        )
        assertTrue(outcome.accepted)
        assertNull(outcome.errorCode)
    }

    @Test
    fun tamperedEnvelopeIsRejected() {
        val clock = FakeClock()
        val keyStore = FakeKeyStore()
        val controller = pairedController(clock, keyStore, hostFingerprint = "a".repeat(64))
        val fields = mapOf("command" to "device.device_status", "counter" to 1)
        val signature = keyStore.sign(CanonicalJson.encodeUnsignedBytes(fields))

        val outcome = controller.checkEnvelopeSignature(
            envelopeFields = mapOf("command" to "device.device_status", "counter" to 2, "signature" to signature),
            observedHostFingerprint = "a".repeat(64),
        )
        assertTrue(outcome.isRejected)
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, outcome.errorCode)
    }

    @Test
    fun hostFingerprintChangeDuringEnvelopeCheck() {
        val clock = FakeClock()
        val keyStore = FakeKeyStore()
        val controller = pairedController(clock, keyStore, hostFingerprint = "a".repeat(64))
        val outcome = controller.checkEnvelopeSignature(
            envelopeFields = mapOf("signature" to "x"),
            observedHostFingerprint = "b".repeat(64),
        )
        assertTrue(outcome.isRejected)
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, outcome.errorCode)
        assertEquals(PairingState.PAIRING_REQUIRED, controller.state)
    }

    // ---------- 错误码纪律 ----------

    /**
     * 每个拒绝路径返回的码都必须已在 [CommandErrors] 里登记。
     *
     * `PAIRING_REQUIRED` 只是**状态名**，绝不作为 `error_code` 出现。
     */
    @Test
    fun everyRejectionUsesRegisteredErrorCodeOnly() {
        val codes = listOf(
            CommandErrors.SIGNATURE_REQUIRED,
            CommandErrors.EXPIRED,
            CommandErrors.INVALID_PAYLOAD,
            CommandErrors.ACTION_REJECTED,
            CommandErrors.PERMISSION_REQUIRED,
        )
        for (code in codes) {
            assertTrue("$code must be registered", CommandErrors.isDeclared(code))
        }
        assertFalse("PAIRING_REQUIRED is a state name, never an error code", CommandErrors.isDeclared("PAIRING_REQUIRED"))
    }

    @Test
    fun pairingStateMachineIsFixed() {
        assertEquals(
            setOf("UNPAIRED", "PAIRING", "PAIRED_OFFLINE", "PAIRING_REQUIRED", "REVOKED"),
            PairingState.entries.map { it.name }.toSet(),
        )
        assertTrue(PairingState.REVOKED.isTerminal)
        assertFalse(PairingState.PAIRED_OFFLINE.isTerminal)
    }

    @Test
    fun controllerStartsUnpaired() {
        val controller = controller()
        assertEquals(PairingState.UNPAIRED, controller.state)
        assertNull(controller.step)
        assertNull(controller.currentIdentity)
        assertFalse(controller.isPaired)
    }

    @Test
    fun confirmedIdentityCarriesBothFingerprints() {
        val clock = FakeClock()
        val controller = pairedController(clock, hostFingerprint = "a".repeat(64))
        val confirmed = controller.confirmedIdentity
        assertNotNull(confirmed)
        assertEquals("device-1", confirmed!!.deviceId)
        assertEquals("a".repeat(64), confirmed.hostFingerprint)
        assertTrue(confirmed.deviceFingerprint.length == 64)
    }

    @Test
    fun canonicalBytesUseOnlyUnsignedFields() {
        val controller = controller()
        val fields = mapOf("a" to 1, "signature" to "S")
        assertEquals("{\"a\":1}", String(controller.canonicalBytes(fields), Charsets.UTF_8))
    }
}
