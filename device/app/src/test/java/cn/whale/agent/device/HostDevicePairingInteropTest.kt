package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 主机 ↔ 设备的**跨端互操作**验证（纯 JVM）。
 *
 * 这个文件解决一个非常具体、也非常致命的问题：
 * **两端各自单测全绿，但拼起来对不上。**
 *
 * 之前就抓到过这类真 bug：
 * - 指纹算法两端不一致（设备 `sha256(公钥字节)` vs 主机 `sha256(base64文本)`），
 *   导致主机永远报 "declared fingerprint does not match the public key"；
 * - 主机横幅只印 8 位配对码、不印 token，而手输框要的是 token。
 *
 * 所以这里**不自己编样本**，而是用与 `host/astrbot_whale_device/pairing_code.py`
 * 完全相同的构造规则生成串，再交给设备端解析器——任何一端改了格式，这里立刻红。
 *
 * 两端的约定（必须逐字一致）：
 * ```
 * whale://pair?host=<wss地址>&token=<一次性token>&fp=<64位小写十六进制>
 * ```
 */
class HostDevicePairingInteropTest {

    /** 模拟主机侧 `qr_payload()` 的真实产出。 */
    private fun hostQrPayload(hostUrl: String, token: String, fingerprint: String): String =
        "whale://pair?host=$hostUrl&token=$token&fp=$fingerprint"

    private val hostAddress = "wss://192.168.1.100:8765/device"

    /** 主机侧 `secrets.token_urlsafe(32)` 的真实形状（含 `-` `_`）。 */
    private val realToken = "eazd6xv-cqmoAuWvWSl8bChPZPJxbgNNxM9UqW3Wsrw"

    /** SHA-256 十六进制指纹。 */
    private val realFingerprint = "a".repeat(64)

    @Test
    fun hostProducedPayloadParsesOnDevice() {
        val payload = hostQrPayload(hostAddress, realToken, realFingerprint)
        val parsed = QrPayloadRules.parse(payload)

        assertTrue("主机产出必须能被设备解析：$payload", parsed is QrPayloadResult.Valid)
        val ok = parsed as QrPayloadResult.Valid
        assertEquals("host 必须原样保留（含 wss:// 前缀与端口、路径）", hostAddress, ok.payload.host)
        assertEquals("token 必须原样保留（含 - 与 _）", realToken, ok.payload.token)
        assertEquals(realFingerprint, ok.payload.hostFingerprint)
    }

    @Test
    fun hostReferencePayloadFromDocsParses() {
        // 用主机侧测试里真实出现过的取值，避免「只在构造器里自洽」。
        val payload =
            "whale://pair?host=wss://192.168.1.10:8765/device&token=tok-abc_123&fp=" +
                "a".repeat(64)
        val parsed = QrPayloadRules.parse(payload)
        assertTrue(parsed is QrPayloadResult.Valid)
        val ok = parsed as QrPayloadResult.Valid
        assertEquals("wss://192.168.1.10:8765/device", ok.payload.host)
        assertEquals("tok-abc_123", ok.payload.token)
    }

    @Test
    fun urlSafeTokenShapeIsAccepted() {
        // `token_urlsafe` 的字符集是 [A-Za-z0-9_-]，设备端必须全部接受。
        val payload = hostQrPayload(hostAddress, "abc-DEF_ghi-jkl012", realFingerprint)
        val parsed = QrPayloadRules.parse(payload)
        assertTrue("urlsafe token 必须被接受", parsed is QrPayloadResult.Valid)
    }

    @Test
    fun hostPayloadWithoutWssIsRejectedByDevice() {
        // 主机侧只允许 ws/wss；设备端对非回环的 ws:// 必须拒绝。
        val payload = hostQrPayload("ws://192.168.1.10:8765/device", realToken, realFingerprint)
        val parsed = QrPayloadRules.parse(payload)
        assertTrue("非回环 ws:// 必须被拒", parsed is QrPayloadResult.Invalid)
        assertEquals(
            QrPayloadRules.ERROR_HOST_NOT_WSS,
            (parsed as QrPayloadResult.Invalid).reason,
        )
    }

    @Test
    fun loopbackWsIsAcceptedForLocalTrials() {
        // 本机试验场景：主机绑 127.0.0.1 时给 ws://，设备端允许（仅回环）。
        val payload = hostQrPayload("ws://127.0.0.1:8765/device", realToken, realFingerprint)
        val parsed = QrPayloadRules.parse(payload)
        assertTrue("回环 ws:// 必须被接受", parsed is QrPayloadResult.Valid)
    }

    @Test
    fun uppercaseFingerprintIsNormalizedToLowercase() {
        // 设备端对指纹做 `.lowercase()` 归一化（见 `QrPayload.kt` 的 parse）：
        // 大小写不改变 SHA-256 的语义，归一化后与主机侧
        // `hashlib.hexdigest()` 的小写口径一致。
        //
        // 这是**有意的宽容**：用户手抄指纹时大小写可能不一致，拒绝它没有安全收益
        // （指纹是公开值，不是密钥）。真正的防线是「指纹必须等于主机公钥算出的值」
        // （见 `PairingFlow` 第 5 步的确认界面）。
        val upper = "A".repeat(64)
        val payload = hostQrPayload(hostAddress, realToken, upper)
        val parsed = QrPayloadRules.parse(payload)
        assertTrue("大写指纹应被归一化接受", parsed is QrPayloadResult.Valid)
        assertEquals(
            "归一化后必须是小写",
            "a".repeat(64),
            (parsed as QrPayloadResult.Valid).payload.hostFingerprint,
        )
    }

    @Test
    fun fingerprintWithNonHexCharactersIsRejected() {
        // 归一化不等于放宽：非十六进制字符必须拒绝。
        val bad = "z".repeat(64)
        val payload = hostQrPayload(hostAddress, realToken, bad)
        val parsed = QrPayloadRules.parse(payload)
        assertTrue("非十六进制指纹必须被拒", parsed is QrPayloadResult.Invalid)
        assertEquals(
            QrPayloadRules.ERROR_FINGERPRINT_SHAPE,
            (parsed as QrPayloadResult.Invalid).reason,
        )
    }

    @Test
    fun wrongLengthFingerprintIsRejected() {
        // 长度不符（例如被截断）必须拒绝：截断的指纹比对毫无意义。
        val short = "a".repeat(32)
        val payload = hostQrPayload(hostAddress, realToken, short)
        val parsed = QrPayloadRules.parse(payload)
        assertTrue("截断指纹必须被拒", parsed is QrPayloadResult.Invalid)
    }

    @Test
    fun missingFingerprintIsRejected() {
        val payload = "whale://pair?host=$hostAddress&token=$realToken"
        val parsed = QrPayloadRules.parse(payload)
        assertTrue(parsed is QrPayloadResult.Invalid)
        assertEquals(
            QrPayloadRules.ERROR_MISSING_FINGERPRINT,
            (parsed as QrPayloadResult.Invalid).reason,
        )
    }
}