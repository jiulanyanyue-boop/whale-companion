package cn.whale.agent.device

import java.security.MessageDigest
import java.security.cert.CertificateException
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager
import okhttp3.OkHttpClient

/**
 * TLS 证书固定：**只信任指纹完全匹配的那一张证书**。
 *
 * ## 为什么需要它
 *
 * 真机实测：手机点了「保存并启动连接」，主机端口在监听、TLS 握手也能完成
 * （`HTTP/1.1 101 Switching Protocols`），但**主机和手机都没有任何变化**。
 * 逐层排查后结论是：
 *
 * ```
 * 严格校验: 失败 → self-signed certificate
 * 放宽校验: HTTP/1.1 101 Switching Protocols
 * ```
 *
 * 主机端完全正常，卡在「Android 不信任自签名证书」——
 * 设备端原来的 `OkHttpClient()` 是默认配置，只信任系统 CA。
 *
 * ## 为什么用证书固定，而不是「信任所有证书」
 *
 * - **信任所有证书**：任何能劫持局域网流量的人都能冒充主机，
 *   拿到配对 token 与后续全部指令。这是把安全边界整个拆掉。
 * - **证书固定**：只接受指纹**恰好等于**二维码里 `tfp` 的那一张。
 *   中间人换了证书 → 指纹不匹配 → 握手直接失败。
 *   这比默认的「只信系统 CA」更适合自签名场景，而且不需要用户去装证书。
 *
 * 安全强度取决于指纹从哪来：它来自**用户扫码**（主机屏幕上显示的二维码），
 * 与主机身份指纹 `fp` 是两个独立字段，两者都被校验。
 *
 * ## 边界
 *
 * - 没有 `tfp` 时（回环明文 `ws://` 试验）**不启用固定**，用默认信任链。
 * - 指纹匹配失败时抛出 [CertificateException]，**绝不静默降级**：
 *   宁可连不上，也不假装成功——否则用户会以为「加密连上了」。
 */
object CertificatePinning {

    /**
     * 构造一个只信任 [fingerprint] 对应证书的 [OkHttpClient]。
     *
     * @param fingerprint 证书的 SHA-256 指纹（64 位小写十六进制，来自二维码 `tfp`）
     * @return 带固定信任管理器的 client；[fingerprint] 为空时返回默认 client
     *   （回环明文试验场景，本来就没有 TLS）
     */
    fun clientFor(
        fingerprint: String,
        onTrustedCertificate: (X509Certificate) -> Unit = {},
    ): OkHttpClient {
        val expected = fingerprint.trim().lowercase()
        if (expected.isEmpty()) {
            // 没有证书指纹：说明是回环明文试验，不需要 TLS 固定。
            return OkHttpClient()
        }

        val trustManager = PinnedTrustManager(expected, onTrustedCertificate)
        val context = SSLContext.getInstance("TLS")
        context.init(null, arrayOf(trustManager), null)

        return OkHttpClient.Builder()
            .sslSocketFactory(context.socketFactory, trustManager)
            // 主机证书的 CN 通常是 IP 或局域网名，而自签名证书的 SAN 未必覆盖
            // 用户实际填的那个 IP。**指纹已在 TrustManager 里逐字节校验过**，
            // 所以这里放宽主机名匹配是安全的：证书本身必须是那一张，
            // 换一张就通不过上面的指纹比对。
            .hostnameVerifier { _, _ -> true }
            .build()
    }

    /** 指纹比对用的信任管理器。只接受指纹完全匹配的服务端证书。 */
    private class PinnedTrustManager(
        private val expected: String,
        private val onTrustedCertificate: (X509Certificate) -> Unit,
    ) : X509TrustManager {

        override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {
            throw CertificateException("client certificates are not used by this app")
        }

        override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
            val leaf = chain?.firstOrNull()
                ?: throw CertificateException("server presented no certificate")
            val actual = sha256Hex(leaf)
            if (actual != expected) {
                // 明确告诉「期望什么、实际是什么」的前缀——完整指纹不进日志（可能被截图）。
                throw CertificateException(
                    "TLS certificate fingerprint mismatch: " +
                        "expected ${expected.take(16)}…, got ${actual.take(16)}…",
                )
            }
            // 只在精确指纹匹配后交付证书；OkHttp 的链清理器可能清空 handshake.peerCertificates。
            onTrustedCertificate(leaf)
        }

        override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
    }

    /** 证书 DER 的 SHA-256，小写十六进制（与 `openssl x509 -fingerprint -sha256` 同义）。 */
    fun sha256Hex(certificate: X509Certificate): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(certificate.encoded)
        val builder = StringBuilder(digest.size * 2)
        for (byte in digest) {
            builder.append(String.format("%02x", byte))
        }
        return builder.toString()
    }
}
