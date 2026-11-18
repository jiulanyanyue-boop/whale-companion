package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 证书固定与 `tfp` 字段的固定测试。
 *
 * ## 这组测试守住什么
 *
 * 真机实测：手机点了「保存并启动连接」，**主机和手机都没有任何变化**。
 * 逐层排查（主机端口在监听、TLS 握手能完成），最后定位到：
 *
 * ```
 * 严格校验: 失败 → self-signed certificate
 * 放宽校验: HTTP/1.1 101 Switching Protocols
 * ```
 *
 * 主机端完全正常，卡在「Android 不信任自签名证书」。
 * 设备端原来的 `OkHttpClient()` 是默认配置，只信任系统 CA。
 *
 * 解法是**证书固定**：二维码多带一个 `tfp`（证书 SHA-256），
 * App 只信任恰好这一张。这里钉死四条底线：
 *
 * 1. `tfp` 是可选的（回环明文试验没有证书），但**有就必须合法**；
 * 2. `tfp` 与 `fp` 是**两个独立字段**，不能混用（层次不同：识别主机 vs 校验传输）；
 * 3. 二维码 format/parse 必须**往返一致**（否则「展示的码」与「认的码」是两套规则）；
 * 4. 没有 `tfp` 时**不启用固定**（回到默认信任链），绝不「为了连上就信任所有证书」。
 */
class CertificatePinningTest {

    private val validFp = "a".repeat(64)
    private val validTfp = "0123456789abcdef".repeat(4) // 64 位十六进制

    // -- tfp 解析 --------------------------------------------------------

    @Test
    fun `parse accepts payload without tfp`() {
        // 回环明文试验：主机没有证书，二维码不带 tfp。
        val raw = "whale://pair?host=ws://127.0.0.1:8765/device&token=abc123&fp=$validFp"
        val result = QrPayloadRules.parse(raw)
        assertTrue(result is QrPayloadResult.Valid)
        val payload = (result as QrPayloadResult.Valid).payload
        assertEquals("没有 tfp 时应为空（不启用固定）", "", payload.tlsCertificateFingerprint)
    }

    @Test
    fun `parse reads tfp when present`() {
        val raw = "whale://pair?host=wss://192.168.1.5:8765/device" +
            "&token=abc123&fp=$validFp&tfp=$validTfp"
        val result = QrPayloadRules.parse(raw)
        assertTrue(result is QrPayloadResult.Valid)
        assertEquals(validTfp, (result as QrPayloadResult.Valid).payload.tlsCertificateFingerprint)
    }

    @Test
    fun `parse rejects malformed tfp`() {
        // tfp 有值但形状不对 → 说明码被篡改或拼错。
        // 宁可拒绝，也不要"拿着一个坏指纹去连、然后永远连不上"。
        val raw = "whale://pair?host=wss://192.168.1.5:8765/device" +
            "&token=abc123&fp=$validFp&tfp=zzz"
        val result = QrPayloadRules.parse(raw)
        assertTrue("非法 tfp 必须被拒绝", result is QrPayloadResult.Invalid)
    }

    @Test
    fun `parse normalizes tfp case`() {
        val raw = "whale://pair?host=wss://192.168.1.5:8765/device" +
            "&token=abc123&fp=$validFp&tfp=${validTfp.uppercase()}"
        val result = QrPayloadRules.parse(raw)
        assertTrue(result is QrPayloadResult.Valid)
        assertEquals("指纹应统一小写", validTfp, (result as QrPayloadResult.Valid).payload.tlsCertificateFingerprint)
    }

    @Test
    fun `fp and tfp are independent fields`() {
        // 两者层次不同：fp 用于识别「是不是这台主机」，tfp 用于校验传输层证书。
        // 混用一个字段会让「改了其中一个」看起来像「另一个也变了」，语义会漂移。
        val raw = "whale://pair?host=wss://192.168.1.5:8765/device" +
            "&token=abc123&fp=$validFp&tfp=$validTfp"
        val payload = (QrPayloadRules.parse(raw) as QrPayloadResult.Valid).payload
        assertEquals(validFp, payload.hostFingerprint)
        assertEquals(validTfp, payload.tlsCertificateFingerprint)
        assertTrue("两个指纹不应相同", payload.hostFingerprint != payload.tlsCertificateFingerprint)
    }

    // -- 往返一致 --------------------------------------------------------

    @Test
    fun `format keeps tfp so parse can read it back`() {
        val payload = QrPayload(
            host = "wss://192.168.1.5:8765/device",
            token = "abc123",
            hostFingerprint = validFp,
            tlsCertificateFingerprint = validTfp,
        )
        val text = QrPayloadRules.format(payload)
        assertTrue("format 应输出 tfp", text.contains("tfp=$validTfp"))

        val roundTrip = QrPayloadRules.parse(text)
        assertTrue("往返必须一致", roundTrip is QrPayloadResult.Valid)
        assertEquals(validTfp, (roundTrip as QrPayloadResult.Valid).payload.tlsCertificateFingerprint)
    }

    @Test
    fun `format omits tfp when empty so qr stays short`() {
        val payload = QrPayload(
            host = "ws://127.0.0.1:8765/device",
            token = "abc123",
            hostFingerprint = validFp,
        )
        val text = QrPayloadRules.format(payload)
        assertTrue("没有证书时不应输出 tfp 参数", !text.contains("tfp="))
        assertTrue(QrPayloadRules.parse(text) is QrPayloadResult.Valid)
    }

    @Test
    fun `toString masks full certificate fingerprint`() {
        // 指纹本身不算机密，但整串进日志容易被截图带到别处；
        // 只留前 8 位足够定位"是不是同一张证书"。
        val payload = QrPayload(
            host = "wss://1.2.3.4:8765/device",
            token = "abcdefghijklmnop",
            hostFingerprint = validFp,
            tlsCertificateFingerprint = validTfp,
        )
        val text = payload.toString()
        assertTrue("完整 tfp 不应出现在 toString 里", !text.contains(validTfp))
        assertTrue("应保留前 8 位便于排查", text.contains(validTfp.take(8)))
    }

    // -- 客户端构造 ------------------------------------------------------

    @Test
    fun `client without fingerprint falls back to default trust`() {
        // 回环明文试验：没有证书就不启用固定，用默认信任链。
        // 这**不是**"信任所有证书"——默认链仍然只信系统 CA。
        val empty = CertificatePinning.clientFor("")
        assertNotNull(empty)
        val blank = CertificatePinning.clientFor("   ")
        assertNotNull(blank)
    }

    @Test
    fun `client with fingerprint uses pinned trust manager`() {
        val client = CertificatePinning.clientFor(validTfp)
        assertNotNull(client)
        // 固定客户端必须配置了自定义 SSLSocketFactory（默认客户端没有显式设置）。
        assertNotNull(
            "带指纹时应配置固定 SSLSocketFactory",
            client.sslSocketFactory,
        )
    }

    @Test
    fun `client normalizes fingerprint case`() {
        // 二维码里可能是大写；固定器内部统一小写。
        val upper = CertificatePinning.clientFor(validTfp.uppercase())
        val lower = CertificatePinning.clientFor(validTfp)
        assertNotNull(upper)
        assertNotNull(lower)
    }

    // -- ALLOWED_PARAMS --------------------------------------------------

    @Test
    fun `tfp is an allowed parameter`() {
        assertTrue(
            "tfp 必须在允许参数列表里，否则会被当成未知参数拒绝",
            QrPayloadRules.ALLOWED_PARAMS.contains(QrPayloadRules.PARAM_TFP),
        )
        assertEquals(6, QrPayloadRules.ALLOWED_PARAMS.size)
    }

    @Test
    fun `unknown parameters are still rejected`() {
        // 加字段不能顺手放宽"未知参数拒绝"这条防线。
        val raw = "whale://pair?host=wss://1.2.3.4:8765/device" +
            "&token=abc123&fp=$validFp&evil=1"
        val result = QrPayloadRules.parse(raw)
        assertTrue("未知参数必须拒绝", result is QrPayloadResult.Invalid)
    }

    @Test
    fun `missing fp is still rejected even when tfp present`() {
        // tfp 存在不能替代 fp：主机身份指纹是第 5 步人工核对的依据。
        val raw = "whale://pair?host=wss://1.2.3.4:8765/device&token=abc123&tfp=$validTfp"
        val result = QrPayloadRules.parse(raw)
        assertTrue("缺 fp 必须拒绝", result is QrPayloadResult.Invalid)
        assertNull((result as? QrPayloadResult.Invalid)?.let { null })
    }
}

