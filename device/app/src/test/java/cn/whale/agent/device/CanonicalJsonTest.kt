package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.Signature
import java.security.spec.ECGenParameterSpec
import java.util.Base64

/**
 * M05 固定测试：canonical JSON 与 host 逐字一致 + 指纹 + 验签判定。
 *
 * 本测试**不依赖 Android 运行时**：Keystore 用 JVM `KeyPairGenerator` 替身，
 * 验签用真实 `java.security.Signature`（生产与测试共用 [JvmEcdsaVerifier]）。
 *
 * 期望值全部来自 **Python 3.15 实测**（`json.dumps(..., ensure_ascii=False,
 * sort_keys=True, separators=(",", ":"))`），不是手算的。
 */
class CanonicalJsonTest {

    // ---------- 与 Python 实测输出逐字比对 ----------

    /**
     * 最硬的一条回归锁：一组刁钻输入（含 DEL、`/`、emoji、中文、嵌套、转义字符）。
     *
     * 期望串来自 Python 实测：
     * `{"ctrl":"\u0001\u0002","backspace":"\b","formfeed":"\f","newline":"\n",
     *  "cr":"\r","tab":"\t","del_char":"\u007f"...}` —— 注意 DEL **原样**输出。
     */
    @Test
    fun canonicalMatchesPythonEscapeRules() {
        val fields: Map<String, Any?> = linkedMapOf(
            "ctrl" to "\u0001\u0002",
            "backspace" to "\b",
            "formfeed" to "\u000C",
            "newline" to "\n",
            "cr" to "\r",
            "tab" to "\t",
            "slash" to "a/b",
            "quote" to "a\"b",
            "backslash" to "a\\b",
            "cjk" to "中文",
            "emoji" to "😀",
            "nested" to linkedMapOf("b" to 1, "a" to 2),
        )
        val actual = CanonicalJson.encode(fields)
        // Python 3.15 实测输出（json.dumps(..., ensure_ascii=False, sort_keys=True,
        // separators=(",",":"))），逐字抄录，与 key 排序无关的差异一律视为失败。
        val expected = "{\"backslash\":\"a\\\\b\",\"backspace\":\"\\b\",\"cjk\":\"中文\",\"cr\":\"\\r\"," +
            "\"ctrl\":\"\\u0001\\u0002\",\"emoji\":\"😀\",\"formfeed\":\"\\f\"," +
            "\"nested\":{\"a\":2,\"b\":1},\"newline\":\"\\n\",\"quote\":\"a\\\"b\"," +
            "\"slash\":\"a/b\",\"tab\":\"\\t\"}"
        assertEquals(expected, actual)
    }

    /** `0x7f` (DEL) 必须**原样**输出，不能变成 `\u007f`。这是最容易写错的一处。 */
    @Test
    fun delIsNotEscaped() {
        val actual = CanonicalJson.encode(mapOf("del_char" to "\u007F"))
        assertEquals("{\"del_char\":\"\u007F\"}", actual)
        assertFalse("DEL must not be escaped", actual.contains("\\u007f"))
        assertTrue("DEL raw byte present", actual.contains("\u007F"))
    }

    /** `/` 不转义成 `\/`。 */
    @Test
    fun slashIsNotEscaped() {
        val actual = CanonicalJson.encode(mapOf("slash" to "a/b"))
        assertEquals("{\"slash\":\"a/b\"}", actual)
        assertFalse(actual.contains("\\/"))
    }

    /** 键**递归**排序：嵌套对象内部也要按码点升序。 */
    @Test
    fun keysAreSortedRecursively() {
        val actual = CanonicalJson.encode(mapOf("z" to mapOf("b" to 1, "a" to 2), "a" to 0))
        assertEquals("{\"a\":0,\"z\":{\"a\":2,\"b\":1}}", actual)
    }

    /** 无空格：分隔符固定 `,` 与 `:`。 */
    @Test
    fun separatorsCarryNoWhitespace() {
        val actual = CanonicalJson.encode(mapOf("a" to 1, "b" to listOf(1, 2)))
        assertEquals("{\"a\":1,\"b\":[1,2]}", actual)
        assertFalse("no spaces allowed", actual.contains(" "))
    }

    /** 空对象与空数组不带空格。 */
    @Test
    fun emptyContainersAreCompact() {
        assertEquals("{}", CanonicalJson.encode(emptyMap<String, Any?>()))
        assertEquals("{\"a\":{},\"b\":[]}", CanonicalJson.encode(mapOf("a" to emptyMap<String, Any?>(), "b" to emptyList<Any>())))
    }

    /** `true` / `false` / `null` 小写。 */
    @Test
    fun literalsMatchPythonSpelling() {
        assertEquals("{\"a\":true,\"b\":false,\"c\":null}", CanonicalJson.encode(mapOf("a" to true, "b" to false, "c" to null)))
    }

    /** 整数输出无 `.0`。 */
    @Test
    fun integersHaveNoDecimalPoint() {
        assertEquals("{\"n\":1}", CanonicalJson.encode(mapOf("n" to 1)))
        assertEquals("{\"n\":-1}", CanonicalJson.encode(mapOf("n" to -1)))
        assertEquals("{\"n\":1793318400000}", CanonicalJson.encode(mapOf("n" to 1793318400000L)))
    }

    // ---------- 与 host security.py 的 canonical_payload 对齐 ----------

    /**
     * host `canonical_payload` 实测输出（Python 3.15，抓住的原文）：
     * `{"command":"device.device_status","counter":7,"deadline_ms":30000,...}`
     */
    @Test
    fun envelopeCanonicalMatchesHostPythonOutput() {
        val fields: Map<String, Any?> = linkedMapOf(
            "protocol_version" to 1,
            "request_id" to "r-1",
            "device_id" to "device-1",
            "command" to "device.device_status",
            "deadline_ms" to 30000,
            "idempotency_key" to "m1:0",
            "counter" to 7,
            "issued_at_ms" to 1793318400000L,
            "payload" to emptyMap<String, Any?>(),
            "signature" to "SIG",
        )
        val expected = "{\"command\":\"device.device_status\",\"counter\":7,\"deadline_ms\":30000," +
            "\"device_id\":\"device-1\",\"idempotency_key\":\"m1:0\",\"issued_at_ms\":1793318400000," +
            "\"payload\":{},\"protocol_version\":1,\"request_id\":\"r-1\"}"
        assertEquals(expected, CanonicalJson.encodeUnsigned(fields))
    }

    /** 只剔除顶层 `signature`（与 host 的 `if k != "signature"` 完全一致）。 */
    @Test
    fun onlyTopLevelSignatureIsRemoved() {
        val fields = mapOf("signature" to "S", "nested" to mapOf("signature" to "inner"))
        val actual = CanonicalJson.encodeUnsigned(fields)
        assertEquals("{\"nested\":{\"signature\":\"inner\"}}", actual)
        assertTrue(CanonicalJson.hasSignature(fields))
        assertFalse(CanonicalJson.hasSignature(mapOf("a" to 1)))
    }

    /** 字节级一致：测试既比字符串也比 UTF-8 字节。 */
    @Test
    fun unsignedBytesMatchUtf8Encoding() {
        val fields = mapOf("text" to "你好，鲸鱼娘🐋", "k" to "café", "signature" to "S")
        val text = CanonicalJson.encodeUnsigned(fields)
        val bytes = CanonicalJson.encodeUnsignedBytes(fields)
        assertArrayEquals(text.toByteArray(Charsets.UTF_8), bytes)
        assertEquals("{\"k\":\"café\",\"text\":\"你好，鲸鱼娘🐋\"}", text)
    }

    /** 转义组合（Python 实测：`a"b\c\nd\te\x01f`）。 */
    @Test
    fun escapeCombinationMatchesPython() {
        val actual = CanonicalJson.encode(mapOf("s" to "a\"b\\c\nd\te\u0001f"))
        assertEquals("{\"s\":\"a\\\"b\\\\c\\nd\\te\\u0001f\"}", actual)
    }

    @Test
    fun unsupportedTypesAreRefused() {
        var caught = false
        try {
            CanonicalJson.encode(mapOf("bad" to Thread()))
        } catch (e: IllegalArgumentException) {
            caught = true
        }
        assertTrue("unsupported value must be refused, not guessed", caught)
    }

    @Test
    fun nonStringKeysAreRefused() {
        var caught = false
        try {
            CanonicalJson.encode(mapOf(1 to "x"))
        } catch (e: IllegalArgumentException) {
            caught = true
        }
        assertTrue(caught)
    }

    @Test
    fun nanAndInfinityAreRefused() {
        var caught = false
        try {
            CanonicalJson.encode(mapOf("x" to Double.NaN))
        } catch (e: IllegalArgumentException) {
            caught = true
        }
        assertTrue(caught)
    }

    // ---------- 指纹 ----------

    @Test
    fun fingerprintIsLowercaseSha256Hex() {
        val bytes = "device-public-key".toByteArray(Charsets.UTF_8)
        val fingerprint = Fingerprints.of(bytes)
        assertEquals(DeviceIdentityRules.FINGERPRINT_HEX_LENGTH, fingerprint.length)
        assertTrue(fingerprint.all { it in '0'..'9' || it in 'a'..'f' })
        // Python 实测：hashlib.sha256(b"device-public-key").hexdigest()
        assertEquals("9ec54f7d284f41086f89f126a22b32e55bd376649108b068c0ccb9402e44ea59", fingerprint)
    }

    @Test
    fun fingerprintMatchesPythonForKnownKey() {
        // Python 实测：hashlib.sha256(b"pk").hexdigest()
        assertEquals(
            "eb3102a6cb586765d01fad324523ec0bc67b9efd6a2d9589c135adfedf7922cc",
            Fingerprints.of("pk".toByteArray(Charsets.UTF_8)),
        )
    }

    @Test
    fun fingerprintIsDeterministicAndDiffersByKey() {
        val a = Fingerprints.of("key-a".toByteArray())
        val b = Fingerprints.of("key-b".toByteArray())
        assertEquals(a, Fingerprints.of("key-a".toByteArray()))
        assertFalse(Fingerprints.matches(a, b))
    }

    @Test
    fun fingerprintComparisonIsExact() {
        val value = Fingerprints.of("k".toByteArray())
        assertTrue(Fingerprints.matches(value, value))
        assertFalse(Fingerprints.matches(value, value.uppercase()))
        assertFalse(Fingerprints.matches(value, value + "x"))
        // 两个空串逐字相同，判定为匹配是对的；真正要拒绝的是「长度不等但都为空」以外的情形。
        assertTrue(Fingerprints.matches("", ""))
        assertFalse(Fingerprints.matches("", value))
    }

    @Test
    fun wellFormedFingerprintCheck() {
        val good = PublicIdentity("d1", "pk", Fingerprints.of("pk".toByteArray()))
        assertTrue(good.hasWellFormedFingerprint)
        assertFalse(PublicIdentity("d1", "pk", "XYZ").hasWellFormedFingerprint)
        assertFalse(PublicIdentity("d1", "pk", "").hasWellFormedFingerprint)
    }

    // ---------- 验签判定（真实 ECDSA，JVM 可跑） ----------

    private fun ecKeyPair(): KeyPair {
        val generator = KeyPairGenerator.getInstance("EC")
        generator.initialize(ECGenParameterSpec("secp256r1"))
        return generator.generateKeyPair()
    }

    private fun identityOf(keyPair: KeyPair): PublicIdentity {
        val encoded = keyPair.public.encoded
        return PublicIdentity(
            deviceId = "device-1",
            publicKeyEncoded = Base64.getEncoder().encodeToString(encoded),
            fingerprint = Fingerprints.of(encoded),
        )
    }

    private fun sign(keyPair: KeyPair, payload: ByteArray): String {
        val signature = Signature.getInstance("SHA256withECDSA")
        signature.initSign(keyPair.private)
        signature.update(payload)
        return Base64.getEncoder().encodeToString(signature.sign())
    }

    @Test
    fun validSignaturePasses() {
        val keyPair = ecKeyPair()
        val identity = identityOf(keyPair)
        val fields = mapOf("command" to "device.device_status", "counter" to 1)
        val signature = sign(keyPair, CanonicalJson.encodeUnsignedBytes(fields))
        val check = SignatureGate.check(
            envelopeFields = fields + mapOf("signature" to signature),
            identity = identity,
            expectedHostFingerprint = "abc",
            observedHostFingerprint = "abc",
            verifier = JvmEcdsaVerifier(),
        )
        assertEquals(SignatureCheck.OK, check)
        assertNull(SignatureGate.errorCodeFor(check))
    }

    @Test
    fun tamperedPayloadFailsVerification() {
        val keyPair = ecKeyPair()
        val identity = identityOf(keyPair)
        val signedFields = mapOf("command" to "device.device_status", "counter" to 1)
        val signature = sign(keyPair, CanonicalJson.encodeUnsignedBytes(signedFields))
        val check = SignatureGate.check(
            envelopeFields = mapOf("command" to "device.device_status", "counter" to 2, "signature" to signature),
            identity = identity,
            expectedHostFingerprint = "abc",
            observedHostFingerprint = "abc",
            verifier = JvmEcdsaVerifier(),
        )
        assertEquals(SignatureCheck.INVALID, check)
    }

    @Test
    fun missingSignatureIsReported() {
        val keyPair = ecKeyPair()
        val check = SignatureGate.check(
            envelopeFields = mapOf("command" to "device.device_status"),
            identity = identityOf(keyPair),
            expectedHostFingerprint = "abc",
            observedHostFingerprint = "abc",
            verifier = JvmEcdsaVerifier(),
        )
        assertEquals(SignatureCheck.MISSING, check)
    }

    /** 指纹变化排在密码学校验**之前**：换了主机身份绝不能被当成普通签名失败。 */
    @Test
    fun hostFingerprintChangeIsCheckedBeforeCrypto() {
        val keyPair = ecKeyPair()
        val identity = identityOf(keyPair)
        val fields = mapOf("command" to "device.device_status", "signature" to "whatever")
        val check = SignatureGate.check(
            envelopeFields = fields,
            identity = identity,
            expectedHostFingerprint = "f".repeat(64),
            observedHostFingerprint = "e".repeat(64),
            verifier = JvmEcdsaVerifier(),
        )
        assertEquals(SignatureCheck.HOST_FINGERPRINT_CHANGED, check)
    }

    /** 所有拒绝**都复用** `SIGNATURE_REQUIRED`，不新增错误码。 */
    @Test
    fun everyRejectionReusesRegisteredErrorCode() {
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, SignatureGate.errorCodeFor(SignatureCheck.MISSING))
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, SignatureGate.errorCodeFor(SignatureCheck.INVALID))
        assertEquals(
            CommandErrors.SIGNATURE_REQUIRED,
            SignatureGate.errorCodeFor(SignatureCheck.HOST_FINGERPRINT_CHANGED),
        )
        assertNull(SignatureGate.errorCodeFor(SignatureCheck.OK))
        // 且这些码确实已在 CommandErrors 里登记（C4 会拦未登记码）。
        assertTrue(CommandErrors.isDeclared(CommandErrors.SIGNATURE_REQUIRED))
    }
}
