package cn.whale.agent.device

import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M05 → M01 的验签接线固定测试（纯 JVM）。
 *
 * 这个是本次集成新增的桥接层：M01 的 `RemoteModeGate` 五步顺序不变，
 * 只在第 2 步注入 [FrameSignatureGate]，把「只判 `signature` 非空」升级成
 * 「主机指纹变化检测 + ECDSA 验签」。
 *
 * 覆盖的关键行为：
 * 1. 未注入时退化为只判非空（保持 M01 既有行为，不能让未配对场景全部失败）；
 * 2. 注入后空签名 → `SIGNATURE_REQUIRED`；
 * 3. 注入后签名串格式非法 → `SIGNATURE_REQUIRED`；
 * 4. **主机指纹变化优先于密码学校验**：换身份 + 伪造签名不能被当成普通验签失败；
 * 5. 指纹变化会**先**作废本地身份（回调被调用），再拒绝；
 * 6. `envelopeToCanonicalFields` 的字段集合与协议一致，且能被 `CanonicalJson` 序列化；
 * 7. 验签失败时**不进入执行器**（无副作用）。
 */
class SignatureGateWiringTest {

    private val identity = PublicIdentity(
        deviceId = "device-test-01",
        publicKeyEncoded = "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEtestkey",
        fingerprint = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    )

    private val hostFingerprint = "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"

    private fun envelope(signature: String = "sig"): DeviceEnvelope = DeviceEnvelope(
        request_id = "req-sig-1",
        device_id = "device-test-01",
        command = "device.app.launch",
        deadline_ms = 30_000,
        idempotency_key = "sig-1",
        counter = 1,
        issued_at_ms = 0,
        payload = buildJsonObject { put("package_name", JsonPrimitive("com.android.settings")) },
        signature = signature,
    )

    private class RecordingExecutor : CommandExecutor {
        var invoked = false
        override fun execute(envelope: DeviceEnvelope): DeviceResponse {
            invoked = true
            return DeviceResponse(envelope.request_id, envelope.device_id, "ok")
        }
    }

    private class NoopEffects : EffectLog {
        override fun find(idempotencyKey: String): String? = null
        override fun save(idempotencyKey: String, responseJson: String, nowMs: Long) = Unit
    }

    private class NoopCounters : CounterLog {
        override fun find(deviceId: String): Long? = null
        override fun save(deviceId: String, counter: Long) = Unit
    }

    private class FixedClock(private val value: Long = 1_000_000L) : SessionClock {
        override fun nowMs(): Long = value
    }

    private fun gate(
        executor: CommandExecutor,
        signatureGate: FrameSignatureGate? = null,
    ): RemoteModeGate = RemoteModeGate(
        mode = ModeReader { AgentMode.REMOTE_B },
        executor = executor,
        effects = NoopEffects(),
        counters = NoopCounters(),
        clock = FixedClock(),
        signatureGate = signatureGate,
    )

    // ---------- 1. 未注入：退化为只判非空 ----------

    @Test fun withoutGateBlankSignatureIsRejected() {
        val executor = RecordingExecutor()
        val result = gate(executor).process(envelope(signature = ""))
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, result.response?.error_code)
        assertFalse(executor.invoked)
    }

    @Test fun withoutGateNonBlankSignaturePassesStep2() {
        val executor = RecordingExecutor()
        val result = gate(executor).process(envelope(signature = "anything"))
        // 第 2 步放行；后续步（模式为 REMOTE_B、幂等空、deadline 有效、counter 递增）通过后进入执行器。
        assertTrue("非空签名在未注入验签时应放行到执行器", result.executed)
        assertTrue(executor.invoked)
    }

    // ---------- 2/3. 注入后：空签名与非法签名 ----------

    @Test fun injectedGateRejectsBlankSignature() {
        val executor = RecordingExecutor()
        val recordingGate = FrameSignatureGate { SignatureCheck.MISSING }
        val result = gate(executor, recordingGate).process(envelope(signature = ""))
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, result.response?.error_code)
        assertFalse(executor.invoked)
    }

    @Test fun injectedGateRejectsInvalidSignature() {
        val executor = RecordingExecutor()
        val result = gate(executor, FrameSignatureGate { SignatureCheck.INVALID })
            .process(envelope(signature = "not-a-real-signature"))
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, result.response?.error_code)
        assertFalse("验签失败不得进入执行器", executor.invoked)
    }

    // ---------- 4/5. 主机指纹变化：优先判定 + 先作废身份 ----------

    @Test fun hostFingerprintChangeIsReportedAsSignatureRequired() {
        val executor = RecordingExecutor()
        val result = gate(executor, FrameSignatureGate { SignatureCheck.HOST_FINGERPRINT_CHANGED })
            .process(envelope())
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, result.response?.error_code)
        // 消息必须能区分「指纹变了」而不仅是「签名不对」，便于本地排查，
        // 但 error_code 仍是同一个（不泄露给主机）。
        assertTrue(
            "指纹变化要给出可辨识的本地说明",
            result.response?.message?.contains("fingerprint") == true,
        )
        assertFalse(executor.invoked)
    }

    @Test fun fingerprintChangeCallbackRunsBeforeRejection() {
        var cleared = false
        val executor = RecordingExecutor()
        val m05Gate = M05FrameSignatureGate(
            identity = { identity },
            hostFingerprintProvider = { hostFingerprint },
            observedHostFingerprintProvider = { "deadbeef" + hostFingerprint.substring(8) },
            onHostFingerprintChanged = { cleared = true },
        )
        val result = gate(executor, m05Gate).process(envelope())
        assertTrue("指纹变化必须先触发身份作废回调", cleared)
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, result.response?.error_code)
        assertFalse(executor.invoked)
    }

    // ---------- 6. canonical 字段表形状 ----------

    @Test fun envelopeToCanonicalFieldsMatchesProtocolSchema() {
        val fields = envelopeToCanonicalFields(envelope())
        assertEquals(
            setOf(
                "protocol_version", "request_id", "device_id", "command",
                "deadline_ms", "idempotency_key", "counter", "issued_at_ms",
                "payload", "signature",
            ),
            fields.keys,
        )
        // 整数一律给 Long，避免 Kotlin/JSON 侧出现 1 与 1.0 的字节差异。
        assertTrue(fields["protocol_version"] is Long)
        assertTrue(fields["deadline_ms"] is Long)
        assertTrue(fields["counter"] is Long)
        assertTrue(fields["issued_at_ms"] is Long)
        // payload 必须是纯 Kotlin 容器树，才能走 CanonicalJson 的容器分支。
        assertTrue(fields["payload"] is Map<*, *>)
    }

    @Test fun canonicalFieldsAreSerializableByCanonicalJson() {
        val fields = envelopeToCanonicalFields(envelope())
        val canonical = CanonicalJson.encodeUnsigned(fields)
        // 顶层 signature 已被剔除；其余字段按 key 排序、无空格。
        assertFalse("签名输入里不得包含 signature", canonical.contains("\"signature\""))
        assertTrue("必须是紧凑 JSON（无空格分隔符）", canonical.contains("\"command\":\"device.app.launch\""))
        assertTrue("字段名按字典序先出现在前面", canonical.startsWith("{\"command\":"))
    }

    @Test fun canonicalBytesAreUtf8() {
        val fields = envelopeToCanonicalFields(envelope())
        val bytes = CanonicalJson.encodeUnsignedBytes(fields)
        val text = CanonicalJson.encodeUnsigned(fields)
        assertTrue("字节视图必须与文本一一对应", bytes.contentEquals(text.toByteArray(Charsets.UTF_8)))
    }

    // ---------- 7. 验签通过时正常进入执行器 ----------

    @Test fun injectedGateOkPathReachesExecutor() {
        val executor = RecordingExecutor()
        val result = gate(executor, FrameSignatureGate { SignatureCheck.OK }).process(envelope())
        assertTrue("验签通过后必须能进入执行器", result.executed)
        assertTrue(executor.invoked)
    }

    @Test fun unknownHostFingerprintShapeFailsClosed() {
        val m05Gate = M05FrameSignatureGate(
            identity = { null },
            hostFingerprintProvider = { hostFingerprint },
            observedHostFingerprintProvider = { hostFingerprint },
        )
        assertEquals(
            "没有有效身份时必须 fail-closed",
            SignatureCheck.INVALID,
            m05Gate.check(envelope()),
        )
        assertNull(
            "OK 之外的判定一律对应 SIGNATURE_REQUIRED",
            SignatureGate.errorCodeFor(SignatureCheck.INVALID)?.let { null },
        )
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, SignatureGate.errorCodeFor(SignatureCheck.INVALID))
    }
}
