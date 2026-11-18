package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M05 配对二维码内容解析与校验的固定测试（纯 JVM）。
 *
 * 这条的纪律是 **fail-closed**：凡是形状不对的输入，一律拒绝并给出**可直接展示**的
 * 中文原因，绝不「尽力猜一个值出来」。测试里每一个拒绝用例都同时断言了具体文案。
 */
class QrPayloadTest {

    private companion object {
        val HOST_FP = "a".repeat(64)
        const val TOKEN = "tok-abcdef123456"
        const val HOST = "wss://192.168.1.10:8765"
        val GOOD = "whale://pair?host=$HOST&token=$TOKEN&fp=$HOST_FP"
    }

    private fun invalidReason(text: String?): String {
        val result = QrPayloadRules.parse(text)
        assertTrue("应当被拒绝: $text", result is QrPayloadResult.Invalid)
        return (result as QrPayloadResult.Invalid).reason
    }

    // ---------- 正向 ----------

    @Test
    fun parsesWellFormedPayload() {
        val result = QrPayloadRules.parse(GOOD)
        assertTrue(result is QrPayloadResult.Valid)
        val payload = (result as QrPayloadResult.Valid).payload
        assertEquals(HOST, payload.host)
        assertEquals(TOKEN, payload.token)
        assertEquals(HOST_FP, payload.hostFingerprint)
        assertFalse(payload.insecureTransport)
    }

    @Test
    fun parseRoundTripThroughFormat() {
        val result = QrPayloadRules.parse(GOOD)
        assertTrue(result is QrPayloadResult.Valid)
        val payload = (result as QrPayloadResult.Valid).payload
        assertEquals(GOOD, QrPayloadRules.format(payload))
        // 拼回去的串必须还能再解出来（保证 format 与 parse 是同一套规则）。
        val again = QrPayloadRules.parse(QrPayloadRules.format(payload))
        assertTrue(again is QrPayloadResult.Valid)
        assertEquals(payload, (again as QrPayloadResult.Valid).payload)
    }

    @Test
    fun acceptsSurroundingWhitespaceAndUppercaseScheme() {
        val result = QrPayloadRules.parse("  WHALE://pair?host=$HOST&token=$TOKEN&fp=$HOST_FP  ")
        assertTrue("扫码结果常带换行/空格，scheme 大小写不敏感", result is QrPayloadResult.Valid)
    }

    @Test
    fun acceptsPercentEncodedHostAndToken() {
        // host 里的 `:` 与 `/` 编码后仍应解析回原值。
        val text = "whale://pair?host=wss%3A%2F%2F192.168.1.10%3A8765&token=$TOKEN&fp=$HOST_FP"
        val result = QrPayloadRules.parse(text)
        assertTrue(result is QrPayloadResult.Valid)
        assertEquals(HOST, (result as QrPayloadResult.Valid).payload.host)
    }

    @Test
    fun normalizesUppercaseFingerprintToLowercase() {
        val result = QrPayloadRules.parse("whale://pair?host=$HOST&token=$TOKEN&fp=${"A".repeat(64)}")
        assertTrue(result is QrPayloadResult.Valid)
        assertEquals(HOST_FP, (result as QrPayloadResult.Valid).payload.hostFingerprint)
    }

    @Test
    fun loopbackPlainWsIsAccepted() {
        val result = QrPayloadRules.parse("whale://pair?host=ws://127.0.0.1:8765&token=$TOKEN&fp=$HOST_FP")
        assertTrue("回环地址允许明文 ws://", result is QrPayloadResult.Valid)
        val payload = (result as QrPayloadResult.Valid).payload
        assertTrue(payload.insecureTransport)
        assertTrue(payload.isLoopback)
    }

    @Test
    fun tokenIsMaskedInToString() {
        val result = QrPayloadRules.parse(GOOD)
        assertTrue(result is QrPayloadResult.Valid)
        val payload = (result as QrPayloadResult.Valid).payload
        val text = payload.toString()
        assertFalse("toString 绝不能带完整 token", text.contains(TOKEN))
        assertTrue("但要看得出是哪一个 token", text.contains("tok-") && text.contains("3456"))
    }

    // ---------- 拒绝：scheme / 结构 ----------

    @Test
    fun rejectsEmptyInput() {
        assertEquals(QrPayloadRules.ERROR_EMPTY, invalidReason(null))
        assertEquals(QrPayloadRules.ERROR_EMPTY, invalidReason(""))
        assertEquals(QrPayloadRules.ERROR_EMPTY, invalidReason("   "))
    }

    @Test
    fun rejectsWrongScheme() {
        assertEquals(QrPayloadRules.ERROR_SCHEME, invalidReason("https://192.168.1.10/pair?host=x"))
        assertEquals(QrPayloadRules.ERROR_SCHEME, invalidReason("whale:/pair?host=x"))
        assertEquals(QrPayloadRules.ERROR_SCHEME, invalidReason("http://pair?host=x"))
        // 近似 authority 也必须拒绝：`pairing` 不是 `pair`。
        assertEquals(QrPayloadRules.ERROR_SCHEME, invalidReason("whale://pairing?host=$HOST&token=$TOKEN&fp=$HOST_FP"))
        // 缺 `?` 同样是结构不对。
        assertEquals(QrPayloadRules.ERROR_SCHEME, invalidReason("whale://pair"))
    }

    // ---------- 拒绝：缺字段 ----------

    @Test
    fun rejectsMissingFields() {
        assertEquals(
            QrPayloadRules.ERROR_MISSING_HOST,
            invalidReason("whale://pair?token=$TOKEN&fp=$HOST_FP"),
        )
        assertEquals(
            QrPayloadRules.ERROR_MISSING_TOKEN,
            invalidReason("whale://pair?host=$HOST&fp=$HOST_FP"),
        )
        assertEquals(
            QrPayloadRules.ERROR_MISSING_FINGERPRINT,
            invalidReason("whale://pair?host=$HOST&token=$TOKEN"),
        )
    }

    @Test
    fun rejectsDuplicateAndUnknownParams() {
        assertEquals(
            QrPayloadRules.ERROR_DUPLICATE_PARAM.format("token"),
            invalidReason("whale://pair?host=$HOST&token=$TOKEN&token=$TOKEN&fp=$HOST_FP"),
        )
        assertEquals(
            QrPayloadRules.ERROR_UNKNOWN_PARAM.format("x"),
            invalidReason("whale://pair?host=$HOST&token=$TOKEN&fp=$HOST_FP&x=1"),
        )
        // 没有 `=` 的裸片段不能被悄悄忽略。
        assertEquals(
            QrPayloadRules.ERROR_UNKNOWN_PARAM.format("junk"),
            invalidReason("whale://pair?host=$HOST&token=$TOKEN&fp=$HOST_FP&junk"),
        )
    }

    // ---------- 拒绝：host ----------

    @Test
    fun rejectsNonWssHostAndNonLoopbackPlainWs() {
        assertEquals(
            QrPayloadRules.ERROR_HOST_NOT_WSS,
            invalidReason("whale://pair?host=http://192.168.1.10:8765&token=$TOKEN&fp=$HOST_FP"),
        )
        // 明文 ws:// 只允许回环：局域网地址一律拒绝（配对码是外部输入，不能自称主机就放行明文）。
        assertEquals(
            QrPayloadRules.ERROR_HOST_NOT_WSS,
            invalidReason("whale://pair?host=ws://192.168.1.10:8765&token=$TOKEN&fp=$HOST_FP"),
        )
        assertEquals(
            QrPayloadRules.ERROR_HOST_NO_HOSTNAME,
            invalidReason("whale://pair?host=wss://&token=$TOKEN&fp=$HOST_FP"),
        )
        assertEquals(
            QrPayloadRules.ERROR_HOST_NO_HOSTNAME,
            invalidReason("whale://pair?host=wss://:8765&token=$TOKEN&fp=$HOST_FP"),
        )
    }

    // ---------- 拒绝：token / 指纹形状 ----------

    @Test
    fun rejectsMalformedToken() {
        assertEquals(
            QrPayloadRules.ERROR_TOKEN_SHAPE,
            invalidReason("whale://pair?host=$HOST&token=t&fp=$HOST_FP"),
        )
        assertEquals(
            QrPayloadRules.ERROR_TOKEN_SHAPE,
            invalidReason("whale://pair?host=$HOST&token=has%20space%20in%20it&fp=$HOST_FP"),
        )
        assertEquals(
            QrPayloadRules.ERROR_TOKEN_SHAPE,
            invalidReason("whale://pair?host=$HOST&token=${"x".repeat(300)}&fp=$HOST_FP"),
        )
    }

    @Test
    fun rejectsMalformedFingerprint() {
        assertEquals(
            QrPayloadRules.ERROR_FINGERPRINT_SHAPE,
            invalidReason("whale://pair?host=$HOST&token=$TOKEN&fp=${"a".repeat(63)}"),
        )
        assertEquals(
            QrPayloadRules.ERROR_FINGERPRINT_SHAPE,
            invalidReason("whale://pair?host=$HOST&token=$TOKEN&fp=${"g".repeat(64)}"),
        )
        assertEquals(
            QrPayloadRules.ERROR_FINGERPRINT_SHAPE,
            invalidReason("whale://pair?host=$HOST&token=$TOKEN&fp=a:b:c"),
        )
    }

    // ---------- 手动输入入口 ----------

    @Test
    fun manualEntryAcceptsHostAndTokenWithoutFingerprint() {
        val checked = QrPayloadRules.checkManualEntry(HOST, TOKEN)
        assertTrue(checked is QrPayloadRules.ManualEntryCheck.Valid)
        val valid = checked as QrPayloadRules.ManualEntryCheck.Valid
        assertEquals(HOST, valid.host)
        assertEquals(TOKEN, valid.token)
        assertFalse(valid.insecure)
    }

    @Test
    fun manualEntryFlagsPlainWs() {
        val checked = QrPayloadRules.checkManualEntry("ws://127.0.0.1:8765", TOKEN)
        assertTrue(checked is QrPayloadRules.ManualEntryCheck.Valid)
        assertTrue((checked as QrPayloadRules.ManualEntryCheck.Valid).insecure)
    }

    @Test
    fun manualEntryCollectsAllErrors() {
        val checked = QrPayloadRules.checkManualEntry("192.168.1.10:8765", "")
        assertTrue(checked is QrPayloadRules.ManualEntryCheck.Invalid)
        val errors = (checked as QrPayloadRules.ManualEntryCheck.Invalid).errors
        assertEquals("两个框都错要一次说清，别让用户改两轮", 2, errors.size)
        assertTrue(errors[0].startsWith(QrPayloadRules.ERROR_HOST_NOT_WSS))
        assertEquals(QrPayloadRules.ERROR_MISSING_TOKEN, errors[1])
    }

    @Test
    fun manualQueryIsNotAFullPayloadBecauseFingerprintIsMissing() {
        // 手输拼出来的串必然缺 fp：这正是「手输不伪造主机指纹」的证明。
        val query = QrPayloadRules.buildManualQuery(HOST, TOKEN)
        assertEquals(
            QrPayloadRules.ERROR_MISSING_FINGERPRINT,
            invalidReason(query),
        )
    }

    @Test
    fun manualQueryEncodesSpecialCharacters() {
        val query = QrPayloadRules.buildManualQuery("wss://192.168.1.10:8765", "tok a+b/c")
        assertTrue("空格必须被编码", query.contains("%20"))
        assertTrue("不能出现裸空格", !query.substringAfter("?").contains(' '))
        // host 是 URL：`:` 与 `/` 保持原样，二维码才短、才读得稳。
        assertTrue("URL 的 :// 不该被编码", query.substringAfter("host=").contains("wss://192.168.1.10:8765"))
    }

    // ---------- 与主机侧 `pairing_code.py` 的互操作（用主机真实产出的格式） ----------

    /**
     * 主机侧 `host/astrbot_whale_device/pairing_code.py:qr_payload()` 实际产出的串形状：
     * - `host` 带路径（`wss://ip:port/device`），且 `:` `/` **不被 encode**；
     * - `token` 是 `secrets.token_urlsafe(32)` → 43 个字符，含 `-` `_`；
     * - `fp` 是 64 位十六进制。
     *
     * 这条用例是「设备端必须认得主机端产出的东西」的直接证据，
     * 样本值取自本机真实调用（2026-11-02）。
     */
    @Test
    fun parsesRealHostGeneratedPayload() {
        val hostToken = "adf_OH1e4MOfR7HqSDkfSvhMFmB8rpvdoxrCF4FAvLA"
        val text = "whale://pair?host=wss://192.168.1.10:8765/device" +
            "&token=$hostToken&fp=${"a".repeat(64)}"
        val result = QrPayloadRules.parse(text)
        assertTrue("主机真实产出的格式必须能解", result is QrPayloadResult.Valid)
        val payload = (result as QrPayloadResult.Valid).payload
        assertEquals("wss://192.168.1.10:8765/device", payload.host)
        assertEquals(hostToken, payload.token)
        assertEquals(43, payload.token.length)
        assertFalse(payload.insecureTransport)
    }

    @Test
    fun parsesRealHostPayloadOnLoopbackPlainWs() {
        val text = "whale://pair?host=ws://127.0.0.1:8765/device" +
            "&token=adf_OH1e4MOfR7HqSDkfSvhMFmB8rpvdoxrCF4FAvLA&fp=${"a".repeat(64)}"
        val result = QrPayloadRules.parse(text)
        assertTrue(result is QrPayloadResult.Valid)
        assertTrue((result as QrPayloadResult.Valid).payload.insecureTransport)
    }

    @Test
    fun hostHumanReadablePairingCodeIsNotTheToken() {
        // 主机侧还有一枚 8 位「人可抄」码（`make_pairing_code()`，字符集排除易混字符）。
        // 它**不等于**二维码里的 token —— 这一点必须在测试里钉死，
        // 否则设备端很容易误以为「用户手抄 8 位码就等于 token」而做出走不通的界面。
        val humanCode = "XTKEBA66"
        val token = "adf_OH1e4MOfR7HqSDkfSvhMFmB8rpvdoxrCF4FAvLA"
        assertTrue("8 位码本身是合法 token 形状（可被当 token 传进来）", QrPayloadRules.isTokenShape(humanCode))
        assertTrue("但它与真实 token 不同", humanCode != token)
        // 用 8 位码当 token 拼出来的码依然「形状合法」——形状合法不等于能与主机对上。
        val text = "whale://pair?host=wss://192.168.1.10:8765/device&token=$humanCode&fp=${"a".repeat(64)}"
        assertTrue(QrPayloadRules.parse(text) is QrPayloadResult.Valid)
    }

    // ---------- 回环判定 ----------

    @Test
    fun loopbackDetectionIsStrict() {
        assertTrue(QrPayloadRules.isLoopbackHost("ws://127.0.0.1:8765"))
        assertTrue(QrPayloadRules.isLoopbackHost("ws://localhost:8765"))
        assertTrue(QrPayloadRules.isLoopbackHost("wss://[::1]:8765"))
        assertFalse(QrPayloadRules.isLoopbackHost("ws://192.168.1.10:8765"))
        // 前缀相近但不同的主机名不能被当成回环。
        assertFalse(QrPayloadRules.isLoopbackHost("ws://127.0.0.1.evil.com:8765"))
        assertFalse(QrPayloadRules.isLoopbackHost("ws://localhost.evil.com:8765"))
    }
}
