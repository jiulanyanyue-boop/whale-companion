package cn.whale.agent.device

/**
 * M05 配对二维码内容的**解析与校验**（纯函数、纯 JVM、不依赖 Android 运行时）。
 *
 * 规范来源：`docs/modules/M05-pairing-transport-development.md`「配对流程」——
 * 第 1 步主机下发一次性配对码 + 短期 token，第 2 步设备扫码或手输。
 *
 * ## 二维码内容格式（与主机侧约定，**逐字固定**）
 *
 * ```text
 * whale://pair?host=<wss地址>&token=<一次性token>&fp=<主机指纹>
 * ```
 *
 * ## 为什么是自定义 scheme 而不是 https 链接
 *
 * - 扫码枪与摄像头都能读任意文本，不需要 `http` 前缀；
 * - **刻意不用 `http(s)://`**：避免被浏览器/系统识别成可点击链接而误打开；
 * - 内容里**只有主机地址、一次性 token、主机指纹**，**绝不含私钥**，
 *   也不含任何能长期复用的凭据——token 一经配对立刻失效（第 6 步）。
 *
 * ## 校验规则（任一不满足即**拒绝**，并给出可直接展示的中文原因）
 *
 * | 项 | 规则 |
 * |---|---|
 * | scheme | 必须是小写 `whale`，且 host 部分固定为 `pair` 路径语义 |
 * | `host` | 必须是 `wss://`；**仅回环地址**允许明文 `ws://` |
 * | `token` | 非空、不含空白与控制字符、长度在 [MIN_TOKEN_LENGTH, MAX_TOKEN_LENGTH] |
 * | `fp` | 主机指纹：64 位小写十六进制（SHA-256，与 host `pairing.py` 同规则） |
 * | 未知查询参数 | 拒绝（`未知字段拒绝` 是 M05/M06 的一致纪律） |
 * | 重复参数 | 拒绝（`host`/`token`/`fp` 各恰好一次） |
 *
 * **不做的事**（本文件边界）：
 * - 不联网、不校验证书链、不验签——本文件只判「形状是否合法」；
 * - 不把 token 写日志、不落盘（[PairingToken] 的一次性性由 `PairingController` 保障）；
 * - 不产出任何默认值：缺失字段一律报缺失，绝不用空串兜底。
 */

/** 二维码内容解析结果：要么合法，要么带一条可直接展示的中文原因。 */
sealed class QrPayloadResult {
    /** 解析成功。 */
    data class Valid(val payload: QrPayload) : QrPayloadResult()

    /** 拒绝；[reason] 是**已经写好**的中文文案，界面原样展示，不自己再拼句子。 */
    data class Invalid(val reason: String) : QrPayloadResult()
}

/**
 * 一份已校验通过的配对二维码内容。
 *
 * [token] 是**一次性** token：只在本次配对里可用一次，配对完成即失效。
 * 本类不提供 `toString` 的 token 明文（见 [toString] 的脱敏实现）——
 * 避免 token 因为一次日志或无意的字符串拼接就泄露。
 */
data class QrPayload(
    /** 主机地址，`wss://host:port` 或回环 `ws://host:port`。 */
    val host: String,
    /** 一次性配对 token。 */
    val token: String,
    /** 主机身份指纹：64 位小写十六进制。 */
    val hostFingerprint: String,
    /**
     * TLS 证书的 SHA-256 指纹（64 位小写十六进制）；回环明文试验时为空。
     *
     * 用途：**证书固定**。主机用自签名证书时，Android 默认不信任它，
     * 连 WSS 会在 TLS 握手阶段被拒（实测 \self-signed certificate\）。
     * 有了这个值，App 就可以「只信任恰好这一张证书」——
     * 既不降低安全强度（比「信任所有证书」严格得多，中间人换证书立刻失败），
     * 又不需要用户去手机设置里装证书。
     */
    val tlsCertificateFingerprint: String = "",
    val pairingCode: String = "",
    val hostSigningPublicKey: String = "",
) {
    /** 是否走了明文 `ws://`（仅回环允许），界面必须显式警告。 */
    val insecureTransport: Boolean get() = host.lowercase().startsWith("ws://")

    /** 回环地址上使用明文传输：界面要提示「仅本机试验」。 */
    val isLoopback: Boolean get() = QrPayloadRules.isLoopbackHost(host)

    /** 脱敏后的调试文本：token 只留首尾各 4 位，绝不整体进日志。 */
    override fun toString(): String =
        "QrPayload(host=$host, token=${QrPayloadRules.maskToken(token)}, fp=$hostFingerprint" +
            // 证书指纹只留前 8 位：够确认"是不是同一张证书"，又不会把整串带进截图/日志。
            ", tfp=${tlsCertificateFingerprint.take(8)})"
}

/** 二维码格式的固定常量与纯校验函数。 */
object QrPayloadRules {

    /** 固定 scheme。 */
    const val SCHEME = "whale"

    /** 固定 authority：`whale://pair`。 */
    const val AUTHORITY = "pair"

    /** 固定前缀，`whale://pair?`。 */
    const val PREFIX = "$SCHEME://$AUTHORITY?"

    /** `host` 参数名。 */
    const val PARAM_HOST = "host"

    /** `token` 参数名。 */
    const val PARAM_TOKEN = "token"

    /** `fp` 参数名。 */
    const val PARAM_FP = "fp"


    /** `tfp` 参数名（TLS 证书指纹，用于证书固定）。 */
    const val PARAM_TFP = "tfp"

    /** 允许出现的参数名（未知参数一律拒绝）。 */
    val ALLOWED_PARAMS: List<String> = listOf(PARAM_HOST, PARAM_TOKEN, PARAM_FP, PARAM_TFP, "code", "hpk")

    /** token 最短长度：主机侧 `secrets.token_urlsafe(32)` 约 43 字符，手工码通常 6 位数字。 */
    const val MIN_TOKEN_LENGTH: Int = 6

    /** token 最长长度：给未来更长的 token 留余量，但不接受无穷长输入。 */
    const val MAX_TOKEN_LENGTH: Int = 256

    /** 指纹长度：SHA-256 = 64 位十六进制（与 [DeviceIdentityRules.FINGERPRINT_HEX_LENGTH] 相同）。 */
    const val FINGERPRINT_HEX_LENGTH: Int = DeviceIdentityRules.FINGERPRINT_HEX_LENGTH

    /** 回环主机名白名单（明文 `ws://` 只允许这些）。 */
    private val LOOPBACK_HOSTS = setOf("127.0.0.1", "localhost", "::1", "[::1]")

    private val tokenRegex = Regex("[A-Za-z0-9._~+/=-]{$MIN_TOKEN_LENGTH,$MAX_TOKEN_LENGTH}")

    private val fingerprintRegex = Regex("[0-9a-f]{$FINGERPRINT_HEX_LENGTH}")

    // ── 拒绝文案（固定字符串，供界面与测试共用） ──────────────────────

    const val ERROR_EMPTY = "二维码内容为空：请重新扫码，或改用「手动输入」"
    const val ERROR_SCHEME = "不是本应用的配对码：内容必须以 whale://pair? 开头"
    const val ERROR_MISSING_HOST = "配对码缺少主机地址（host）：请让主机重新生成配对码"
    const val ERROR_MISSING_TOKEN = "配对码缺少一次性 token：请让主机重新生成配对码"
    const val ERROR_MISSING_FINGERPRINT = "配对码缺少主机指纹（fp）：无法确认主机身份，请让主机重新生成"
    const val ERROR_DUPLICATE_PARAM = "配对码里「%s」出现了多次：请让主机重新生成配对码"
    const val ERROR_UNKNOWN_PARAM = "配对码里有无法识别的字段「%s」：可能是旧版或伪造的配对码"
    const val ERROR_HOST_NOT_WSS = "主机地址必须用加密的 wss://（只有本机回环地址才允许 ws://）"
    const val ERROR_HOST_MALFORMED = "主机地址不是合法 URL：%s"
    const val ERROR_HOST_NO_HOSTNAME = "主机地址缺少主机名，例如 wss://192.168.1.10:8765"
    const val ERROR_TOKEN_SHAPE = "一次性 token 形状不合法：只允许字母、数字与 . _ ~ + / = ，长度 $MIN_TOKEN_LENGTH-$MAX_TOKEN_LENGTH"
    const val ERROR_FINGERPRINT_SHAPE = "主机指纹必须是 $FINGERPRINT_HEX_LENGTH 位小写十六进制（SHA-256）"

    /** 明文传输警告（界面必须显示，不静默）。 */
    const val WARNING_INSECURE = "警告：这是没加密的 ws:// 连接，只适合在本机试验；正式使用请让主机提供 wss://"

    /** 主机名是否属于回环白名单。 */
    fun isLoopbackHost(hostUrl: String): Boolean {        val authority = hostUrl.substringAfter("://", "").substringBefore('/').substringBefore('?')
        val name = authority.substringBeforeLast(':', authority).trim().lowercase()
        return name in LOOPBACK_HOSTS
    }

    /** 指纹形状是否合规：64 位小写十六进制（SHA-256）。区别于 [DeviceIdentityRules] 的常量，这里是判定。 */
    fun isWellFormedFingerprint(value: String?): Boolean =
        value != null && fingerprintRegex.matches(value.trim().lowercase())

    /** token 形状是否合规（**只判形状**，不代表它能与主机对上）。 */
    fun isTokenShape(value: String?): Boolean = value != null && tokenRegex.matches(value.trim())

    /** token 脱敏：只留首尾各 4 位。短 token 一律整体打码。 */    fun maskToken(token: String): String {
        if (token.length <= 8) return "****"
        return token.take(4) + "****" + token.takeLast(4)
    }

    /**
     * 解析并校验一段二维码文本。
     *
     * 这是**唯一入口**：扫码结果、粘贴内容、手输拼出来的串，全部走这里，
     * 保证「三条通道同一套校验」，不会出现「手输少校验一项」的漏洞。
     */
    fun parse(raw: String?): QrPayloadResult {
        val text = raw?.trim().orEmpty()
        if (text.isEmpty()) return QrPayloadResult.Invalid(ERROR_EMPTY)

        val schemeEnd = text.indexOf("://")
        if (schemeEnd < 0 || !text.substring(0, schemeEnd).equals(SCHEME, ignoreCase = true)) {
            return QrPayloadResult.Invalid(ERROR_SCHEME)
        }
        // scheme 之后的 authority 必须**恰好**是 `pair`，且紧随 `?`；
        // 用 startsWith 而不是 contains，避免 `whale://pairing?…` 这类近似串混进来。
        val afterScheme = text.substring(schemeEnd + 3)
        if (!afterScheme.startsWith("$AUTHORITY?")) {
            return QrPayloadResult.Invalid(ERROR_SCHEME)
        }
        val query = afterScheme.substring(AUTHORITY.length + 1)

        val fields = LinkedHashMap<String, String>()
        val duplicated = LinkedHashSet<String>()
        if (query.isNotEmpty()) {
            for (pair in query.split('&')) {
                if (pair.isEmpty()) continue
                val separator = pair.indexOf('=')
                if (separator < 0) {
                    // 没有 `=` 的片段：当作未知字段处理，绝不悄悄忽略。
                    return QrPayloadResult.Invalid(ERROR_UNKNOWN_PARAM.format(pair))
                }
                val name = pair.substring(0, separator).trim()
                val value = decodePercent(pair.substring(separator + 1))
                if (name !in ALLOWED_PARAMS) {
                    return QrPayloadResult.Invalid(ERROR_UNKNOWN_PARAM.format(name))
                }
                if (fields.containsKey(name)) duplicated.add(name) else fields[name] = value
            }
        }
        if (duplicated.isNotEmpty()) {
            return QrPayloadResult.Invalid(ERROR_DUPLICATE_PARAM.format(duplicated.first()))
        }
        fields[PARAM_HOST] ?: return QrPayloadResult.Invalid(ERROR_MISSING_HOST)
        fields[PARAM_TOKEN] ?: return QrPayloadResult.Invalid(ERROR_MISSING_TOKEN)
        fields[PARAM_FP] ?: return QrPayloadResult.Invalid(ERROR_MISSING_FINGERPRINT)

        val hostCheck = checkHost(fields.getValue(PARAM_HOST))
        if (hostCheck is HostCheck.Invalid) return QrPayloadResult.Invalid(hostCheck.reason)
        val normalizedHost = (hostCheck as HostCheck.Valid).url

        val token = fields.getValue(PARAM_TOKEN).trim()
        if (!tokenRegex.matches(token)) return QrPayloadResult.Invalid(ERROR_TOKEN_SHAPE)

        val fingerprint = fields.getValue(PARAM_FP).trim().lowercase()
        if (!fingerprintRegex.matches(fingerprint)) {
            return QrPayloadResult.Invalid(ERROR_FINGERPRINT_SHAPE)
        }

        // \	fp\ 是可选的：回环明文试验没有证书，主机不会带这个参数。
        // 有就必须是合法 64 位十六进制——形状不对说明码被篡改或拼错，
        // 宁可拒绝也不要「拿着一个坏指纹去连、然后证书校验永远失败」。
        val certFingerprint = fields[PARAM_TFP]?.trim()?.lowercase().orEmpty()
        if (certFingerprint.isNotEmpty() && !fingerprintRegex.matches(certFingerprint)) {
            return QrPayloadResult.Invalid(ERROR_FINGERPRINT_SHAPE)
        }
        return QrPayloadResult.Valid(
            QrPayload(
                host = normalizedHost,
                token = token,
                hostFingerprint = fingerprint,
                tlsCertificateFingerprint = certFingerprint,
                pairingCode = fields["code"].orEmpty(),
                hostSigningPublicKey = fields["hpk"].orEmpty(),
            ),
        )
    }

    /** 主机地址校验结果。 */
    sealed class HostCheck {
        data class Valid(val url: String) : HostCheck()
        data class Invalid(val reason: String) : HostCheck()
    }

    /**
     * 校验主机地址：`wss://` 一律允许，`ws://` **只有回环地址**允许。
     *
     * 与 [ConnectionSettings.validateUrl] 的差别是**更严**：那个允许任意 `ws://`（带警告），
     * 这里只允许回环——因为配对码是**外部输入**，不能因为它自称主机就接受明文传输。
     */
    fun checkHost(rawHost: String): HostCheck {
        val url = rawHost.trim()
        if (url.isEmpty()) return HostCheck.Invalid(ERROR_MISSING_HOST)
        val lower = url.lowercase()
        val isWss = lower.startsWith("wss://")
        val isWs = lower.startsWith("ws://")
        if (!isWss && !isWs) return HostCheck.Invalid(ERROR_HOST_NOT_WSS)
        if (isWs && !isLoopbackHost(url)) return HostCheck.Invalid(ERROR_HOST_NOT_WSS)

        val authority = url.substringAfter("://", "")
            .substringBefore('/')
            .substringBefore('?')
            .substringBefore('#')
            .trim()
        // `wss://` 与 `wss://:8765` 都是「没有主机名」，给明确文案而不是解析异常。
        if (authority.isEmpty() || Regex(":\\d*").matches(authority)) {
            return HostCheck.Invalid(ERROR_HOST_NO_HOSTNAME)
        }
        val uri = try {
            java.net.URI(url)
        } catch (exception: java.net.URISyntaxException) {
            return HostCheck.Invalid(ERROR_HOST_MALFORMED.format(exception.reason.orEmpty()))
        }
        if (uri.host.isNullOrBlank()) return HostCheck.Invalid(ERROR_HOST_NO_HOSTNAME)
        return HostCheck.Valid(url)
    }

    /**
     * 手动输入两个框（主机地址 + 一次性 token）时**没有二维码里的指纹**，
     * 因此手工路径**不伪造指纹**：主机指纹必须由主机在确认页给出、由用户逐字比对。
     *
     * 本函数把「手输两个框」拼成与二维码**同一条**解析路径，
     * 只是不带 `fp`——所以它必然返回 [QrPayloadResult.Invalid] 的「缺 fp」，
     * 调用方据此走 [ManualEntryCheck] 分支，**不绕过**指纹确认。
     */
    fun buildManualQuery(hostInput: String, tokenInput: String): String =
        "$PREFIX$PARAM_HOST=${encodeQueryValue(hostInput.trim())}&$PARAM_TOKEN=${encodeQueryValue(tokenInput.trim())}"

    /** 手动输入校验结果：主机与 token 形状是否合法（**不**含主机指纹，故不等于可配对成功）。 */
    sealed class ManualEntryCheck {
        /** 形状合法；[insecure] 为真时必须显示明文警告。 */
        data class Valid(val host: String, val token: String, val insecure: Boolean) : ManualEntryCheck()

        /** 不合法；[errors] 全部展示。 */
        data class Invalid(val errors: List<String>) : ManualEntryCheck()
    }

    /**
     * 手动输入路径的独立校验：只判主机地址与 token，**不假装**有主机指纹。
     *
     * 返回 [ManualEntryCheck.Valid] 只代表「这两个框写得对」；
     * 主机指纹仍然必须由用户在确认页填写/核对，缺失时 [PairingController.confirmFingerprints]
     * 会自己拒绝——本函数不替它放行。
     */
    fun checkManualEntry(hostInput: String?, tokenInput: String?): ManualEntryCheck {
        val errors = ArrayList<String>()
        val host = hostInput?.trim().orEmpty()
        val token = tokenInput?.trim().orEmpty()
        val hostCheck = checkHost(host)
        if (hostCheck is HostCheck.Invalid) errors.add(hostCheck.reason)
        if (token.isEmpty()) {
            errors.add(ERROR_MISSING_TOKEN)
        } else if (!tokenRegex.matches(token)) {
            errors.add(ERROR_TOKEN_SHAPE)
        }
        if (errors.isNotEmpty()) return ManualEntryCheck.Invalid(errors)
        return ManualEntryCheck.Valid(
            host = host,
            token = token,
            insecure = host.lowercase().startsWith("ws://"),
        )
    }

    /**
     * 把解析出的字段拼回标准二维码文本（供「复制/再展示」与测试往返验证）。
     *
     * 编码策略与主机侧生成二维码时的写法**一致**：
     * - `host` 是 URL，`:` 与 `/` 必须原样保留（编码成 `%3A%2F%2F` 会让二维码变长变密、也更难读）；
     * - `token` 的字符集已被 [tokenRegex] 限死，其中 `+` `/` `=` 在 query 值里无需转义；
     * - 只有会破坏 query 结构的 `&`、`%`、`#`、空格等才做百分号编码。
     *
     * 结果必须能被 [parse] 原样解回（往返一致），否则「展示的码」与「认的码」就是两套规则。
     */
    fun format(payload: QrPayload): String = buildString {
        append("$PREFIX$PARAM_HOST=${encodeQueryValue(payload.host)}")
        append("&$PARAM_TOKEN=${encodeQueryValue(payload.token)}")
        append("&$PARAM_FP=${encodeQueryValue(payload.hostFingerprint)}")
        if (payload.hostSigningPublicKey.isNotEmpty()) append("&hpk=${encodeQueryValue(payload.hostSigningPublicKey)}")
        if (payload.pairingCode.isNotEmpty()) append("&code=${encodeQueryValue(payload.pairingCode)}")
        // `tfp` 只在有证书时输出：回环明文试验没有证书，多一个空参数只是噪音。
        if (payload.tlsCertificateFingerprint.isNotEmpty()) {
            append("&$PARAM_TFP=${encodeQueryValue(payload.tlsCertificateFingerprint)}")
        }
    }

    /** 只对会破坏 `key=value&…` 结构的字符编码；其余保持原样（二维码更短更好读）。 */
    private fun encodeQueryValue(value: String): String {
        val builder = StringBuilder(value.length)
        for (char in value) {
            val mustEncode = char == '&' || char == '%' || char == '?' || char == '#' ||
                char == ' ' || char == '+' || char == '='
            if (!mustEncode) {
                builder.append(char)
                continue
            }
            builder.append('%')
            builder.append(HEX[char.code ushr 4])
            builder.append(HEX[char.code and 0x0F])
        }
        return builder.toString()
    }

    /** 百分号解码：`+` **不**解释为空格（token 里 `+` 是合法字符）。非法 `%` 序列原样保留。 */
    private fun decodePercent(value: String): String {
        if (!value.contains('%')) return value
        val builder = StringBuilder(value.length)
        var index = 0
        while (index < value.length) {
            val char = value[index]
            if (char == '%' && index + 2 < value.length) {
                val high = hexValue(value[index + 1])
                val low = hexValue(value[index + 2])
                if (high >= 0 && low >= 0) {
                    builder.append(((high shl 4) or low).toChar())
                    index += 3
                    continue
                }
            }
            builder.append(char)
            index += 1
        }
        return builder.toString()
    }

    private fun hexValue(char: Char): Int = when (char) {
        in '0'..'9' -> char - '0'
        in 'a'..'f' -> char - 'a' + 10
        in 'A'..'F' -> char - 'A' + 10
        else -> -1
    }

    private val HEX = "0123456789ABCDEF".toCharArray()
}
