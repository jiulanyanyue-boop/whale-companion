package cn.whale.agent.device

import java.net.URI
import java.util.concurrent.TimeUnit
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

/**
 * 配对 HTTP 客户端：把主机侧的 pair 端点包装成纯逻辑可测的调用。
 *
 * ## 它补的是哪一段
 *
 * 之前设备端 `PairingFlow.prepareRegistration` **只推进本地状态机**，
 * 一个网络请求都不发；主机侧虽然有完整的 `PairingService`，却没有任何端点去调它。
 * 结果就是「界面能走完六步，但主机根本不知道有设备来配对」。
 *
 * 本类负责把第 3、4、5 步真正送到主机：
 *
 * ```
 * POST /pair/register  → 主机校验配对码 + 保存设备公钥与指纹（第 2、3、4 步）
 * POST /pair/confirm   → 用户确认指纹后启用（第 5 步）
 * POST /pair/finalize  → 让一次性 token 失效（第 6 步，异常收尾时也调）
 * GET  /pair/hostinfo  → 取主机指纹（手输路径下没有二维码 fp 时用）
 * ```
 *
 * ## 端点地址怎么来
 *
 * 二维码里只有**设备通道**地址（`wss://host:8765/device`）。配对端点是
 * 同一台主机的另一个端口（约定为 **设备端口 + 1**，见 `host_gui` 的启动逻辑）。
 * 这里做**纯字符串推导**，不合网络、不猜：
 *
 * ```
 * wss://192.168.1.10:8765/device  →  https://192.168.1.10:8766/pair
 * ws://127.0.0.1:8765/device      →  http://127.0.0.1:8766/pair   （仅回环允许明文）
 * ```
 *
 * 推导不出来就返回 `null`，由调用方**如实报失败**，绝不瞎拼一个地址。
 *
 * ## 安全边界
 *
 * - **只对同一台主机发**：端点地址完全从二维码里的 host 推导，不接受额外输入；
 * - **非回环地址必须 `wss`**：明文 `ws://` 只允许回环（与本项目 M05 一致）；
 * - **token 绝不进日志**：所有异常消息里只出现状态码与主机给出的原因，不带 token；
 * - **不重试**：一次调用一次请求（重试会让「一次性 token」语义变模糊）。
 */
class PairingClient(
    private val http: OkHttpClient = defaultClient(),
    private val timeoutSeconds: Long = DEFAULT_TIMEOUT_SECONDS,
) {

    /** 一次配对调用的结果。 */
    sealed interface Outcome {
        /** 主机接受了。`payload` 是主机返回的 JSON 字段（已解析为字符串映射）。 */
        data class Ok(
            val deviceFingerprint: String = "",
            val hostFingerprint: String = "",
            val pairingState: String = "",
            val message: String = "",
            val hostIdentityIsPlaceholder: Boolean = false,
        ) : Outcome

        /**
         * 主机拒绝了（或网络失败）。
         *
         * `code` 是主机给的协议错误码（如 `SIGNATURE_REQUIRED`），`message` 是主机给的
         * 人话原因；网络失败时 `code` 为 `null`、`message` 是本地原因。
         */
        data class Failed(val code: String?, val message: String) : Outcome
    }

    // -- 地址推导（纯函数，可单测）----------------------------------------

    /**
     * 从设备通道地址推导配对端点基址；推导不出返回 `null`。
     *
     * @param deviceUrl 二维码里的 `host` 字段，形如 `wss://192.168.1.10:8765/device`
     */
    fun pairingBaseUrl(deviceUrl: String?): String? {
        val raw = deviceUrl?.trim().orEmpty()
        if (raw.isEmpty()) return null
        if (!raw.startsWith("wss://", ignoreCase = true) &&
            !raw.startsWith("ws://", ignoreCase = true)
        ) {
            return null
        }
        val parsed = runCatching { URI(raw) }.getOrNull() ?: return null
        val host = parsed.host?.trim().orEmpty()
        if (host.isEmpty()) return null

        val port = parsed.port
        if (port <= 0 || port > 65534) return null

        // 配对端点固定为「设备端口 + 1」。这是主机侧的启动约定，
        // 两端必须一致；改一处要同时改 `host_gui._on_start`。
        val pairingPort = if (port == 443) 443 else port + 1
        val scheme = if (parsed.scheme.equals("wss", ignoreCase = true)) "https" else "http"
        val authority = if (host.contains(":") && !host.startsWith("[")) "[$host]" else host
        val prefix = parsed.path.orEmpty().removeSuffix("/device").trimEnd('/')
        return "$scheme://$authority:$pairingPort$prefix/pair"
    }

    // -- 各步动作 ---------------------------------------------------------

    /** 第 2、3、4 步：提交配对码 + 设备公钥，让主机保存身份。 */
    fun register(
        deviceUrl: String?,
        token: String,
        pairingCode: String,
        deviceId: String,
        publicKeyEncoded: String,
        deviceFingerprint: String,
        capabilitiesVersion: String,
        connectionConfig: String,
        displayName: String,
    ): Outcome {
        val base = pairingBaseUrl(deviceUrl)
            ?: return Outcome.Failed(null, "无法从主机地址推导出配对端点（地址不合法或端口缺失）")
        val body = buildJsonObjectOf(
            "token" to token,
            "pairing_code" to pairingCode,
            "device_id" to deviceId,
            "public_key" to publicKeyEncoded,
            "device_fingerprint" to deviceFingerprint,
            "capabilities_version" to capabilitiesVersion,
            "connection_config" to connectionConfig,
            "display_name" to displayName,
        )
        return post("$base/register", body) { fields ->
            Outcome.Ok(
                deviceFingerprint = fields["device_fingerprint"].orEmpty(),
                hostFingerprint = fields["host_fingerprint"].orEmpty(),
                pairingState = fields["pairing_state"].orEmpty(),
                message = fields["message"].orEmpty(),
                hostIdentityIsPlaceholder = fields["host_identity_is_placeholder"] == "true",
            )
        }
    }

    /** 第 5 步：用户确认指纹后启用。`userAccepted=false` 时主机必须保持未配对。 */
    fun confirm(
        deviceUrl: String?,
        token: String,
        deviceId: String,
        userAccepted: Boolean,
        observedHostFingerprint: String,
    ): Outcome {
        val base = pairingBaseUrl(deviceUrl)
            ?: return Outcome.Failed(null, "无法从主机地址推导出配对端点")
        val body = buildJsonObjectOf(
            "token" to token,
            "device_id" to deviceId,
            "user_accepted" to if (userAccepted) "true" else "false",
            "observed_host_fingerprint" to observedHostFingerprint,
        )
        return post("$base/confirm", body) { fields ->
            Outcome.Ok(
                hostFingerprint = fields["host_fingerprint"].orEmpty(),
                pairingState = fields["pairing_state"].orEmpty(),
                message = fields["message"].orEmpty(),
            )
        }
    }

    /** 第 6 步：让一次性 token 失效（异常收尾时也要调，避免 token 留着）。 */
    fun finalize(deviceUrl: String?, token: String): Outcome {
        val base = pairingBaseUrl(deviceUrl)
            ?: return Outcome.Failed(null, "无法从主机地址推导出配对端点")
        return post("$base/finalize", buildJsonObjectOf("token" to token)) {
            Outcome.Ok(message = "token 已失效")
        }
    }

    /** 取主机指纹（手输路径没有二维码指纹时，界面拿它给用户核对）。 */
    fun hostInfo(deviceUrl: String?): Outcome {
        val base = pairingBaseUrl(deviceUrl)
            ?: return Outcome.Failed(null, "无法从主机地址推导出配对端点")
        val request = runCatching {
            Request.Builder().url("$base/hostinfo").get().build()
        }.getOrNull() ?: return Outcome.Failed(null, "主机地址不合法")
        return execute(request) { fields ->
            Outcome.Ok(
                hostFingerprint = fields["host_fingerprint"].orEmpty(),
                hostIdentityIsPlaceholder = fields["host_identity_is_placeholder"] == "true",
            )
        }
    }

    // -- 内部 ------------------------------------------------------------

    private fun post(
        url: String,
        body: String,
        onOk: (Map<String, String>) -> Outcome.Ok,
    ): Outcome {
        val request = runCatching {
            Request.Builder()
                .url(url)
                .post(body.toRequestBody(JSON_MEDIA_TYPE))
                .build()
        }.getOrNull() ?: return Outcome.Failed(null, "主机地址不合法")
        return execute(request, onOk)
    }

    private fun execute(
        request: Request,
        onOk: (Map<String, String>) -> Outcome.Ok,
    ): Outcome {
        return try {
            http.newCall(request).execute().use { response ->
                val raw = response.body?.string().orEmpty()
                val fields = parseFlatJson(raw)
                if (!response.isSuccessful) {
                    // 主机用 400 表示「配对被拒」，并把协议错误码放在 body 里。
                    val code = fields["code"]
                    val message = fields["message"].orEmpty().ifEmpty { "主机返回 ${response.code}" }
                    return Outcome.Failed(code, message)
                }
                if (fields["ok"] == "false") {
                    return Outcome.Failed(fields["code"], fields["message"].orEmpty().ifEmpty { "主机未接受" })
                }
                onOk(fields)
            }
        } catch (t: Throwable) {
            // 网络层失败：只报类别，不把 URL 里的 token 带出去。
            Outcome.Failed(null, "连不上主机的配对端点（${t.javaClass.simpleName}）")
        }
    }

    companion object {
        /** 配对请求的超时：配对是一次性动作，给 15 秒足够（用户在扫码/确认上花的时间不算在内）。 */
        const val DEFAULT_TIMEOUT_SECONDS: Long = 15L

        private val JSON_MEDIA_TYPE = "application/json; charset=utf-8".toMediaType()

        fun defaultClient(certificateFingerprint: String = ""): OkHttpClient =
            CertificatePinning.clientFor(certificateFingerprint).newBuilder()
            .followRedirects(false)
            .followSslRedirects(false)
            .connectTimeout(DEFAULT_TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .readTimeout(DEFAULT_TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .writeTimeout(DEFAULT_TIMEOUT_SECONDS, TimeUnit.SECONDS)
            .retryOnConnectionFailure(false)
            .build()

        /**
         * 把主机返回的**扁平 JSON**解析成字符串映射。
         *
         * 为什么不用 kotlinx.serialization：这里的响应结构是「十几个 string/bool 字段」，
         * 而且**字段可能随主机版本增删**。用严格 data class 会在主机多回一个字段时直接崩；
         * 逐字符扫描则只取认识的键、忽略其余，容错性更好。
         *
         * 只支持扁平对象（主机侧响应就是扁平的），遇到嵌套对象/数组时跳过该值。
         */
        fun parseFlatJson(raw: String): Map<String, String> {
            val text = raw.trim()
            if (!text.startsWith("{") || !text.endsWith("}")) return emptyMap()
            val result = mutableMapOf<String, String>()
            var index = 1
            while (index < text.length - 1) {
                // 跳过空白与逗号
                while (index < text.length && (text[index] == ' ' || text[index] == ',' || text[index] == '\n' || text[index] == '\r')) {
                    index++
                }
                if (index >= text.length - 1) break
                if (text[index] != '"') { index++; continue }
                val keyEnd = text.indexOf('"', index + 1)
                if (keyEnd < 0) break
                val key = text.substring(index + 1, keyEnd)
                index = keyEnd + 1
                while (index < text.length && text[index] != ':') index++
                index++
                while (index < text.length && text[index] == ' ') index++
                if (index >= text.length) break

                val valueStart = index
                val value = when {
                    text[index] == '"' -> {
                        // 字符串：找到未转义的结束引号
                        var scan = index + 1
                        val sb = StringBuilder()
                        while (scan < text.length) {
                            val ch = text[scan]
                            if (ch == '\\' && scan + 1 < text.length) {
                                sb.append(text[scan + 1])
                                scan += 2
                                continue
                            }
                            if (ch == '"') break
                            sb.append(ch)
                            scan++
                        }
                        index = scan + 1
                        sb.toString()
                    }
                    text[index] == '{' || text[index] == '[' -> {
                        // 嵌套结构：跳过（本类不消费嵌套值）
                        var depth = 0
                        while (index < text.length) {
                            when (text[index]) {
                                '{', '[' -> depth++
                                '}', ']' -> {
                                    depth--
                                    if (depth == 0) { index++; break }
                                }
                            }
                            index++
                        }
                        ""
                    }
                    else -> {
                        var scan = index
                        while (scan < text.length && text[scan] != ',' && text[scan] != '}') scan++
                        val tokenRaw = text.substring(valueStart, scan).trim()
                        index = scan
                        tokenRaw
                    }
                }
                result[key] = value
            }
            return result
        }

        /**
         * 拼出扁平 JSON 请求体。
         *
         * 所有值都按字符串转义；`true` / `false` / 数字这类由调用方显式传入裸值文本，
         * 由 `rawKeys` 决定哪些不加引号（主机侧 `user_accepted` 需要真布尔）。
         */
        private fun buildJsonObjectOf(vararg pairs: Pair<String, String>): String {
            val bareValueKeys = setOf("user_accepted")
            val body = pairs.joinToString(",") { (key, value) ->
                val encoded = if (key in bareValueKeys && (value == "true" || value == "false")) {
                    value
                } else {
                    "\"${escapeJson(value)}\""
                }
                "\"${escapeJson(key)}\":$encoded"
            }
            return "{$body}"
        }

        private fun escapeJson(value: String): String {
            val sb = StringBuilder(value.length)
            for (ch in value) {
                when (ch) {
                    '"' -> sb.append("\\\"")
                    '\\' -> sb.append("\\\\")
                    '\n' -> sb.append("\\n")
                    '\r' -> sb.append("\\r")
                    '\t' -> sb.append("\\t")
                    else -> if (ch.code < 0x20) {
                        sb.append("\\u").append(String.format("%04x", ch.code))
                    } else {
                        sb.append(ch)
                    }
                }
            }
            return sb.toString()
        }
    }
}
