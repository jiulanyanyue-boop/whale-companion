package cn.whale.agent.device

import java.net.URI
import java.net.URISyntaxException

/**
 * 设备端远程连接配置的**键名、校验规则与状态判定**（纯逻辑，不依赖 Android，可在纯 JVM 单测）。
 *
 * 键名固定为 `SharedPreferences("device_connection")` 的 `wss_url` 与 `device_id`：
 * 这已经是 [DeviceConnectionService.connectIfConfigured] 与 `MainActivity.resolveDeviceId()`
 * 读取的位置，**不新增键、不改名**——新建一套键就等于两侧读不到同一份配置。
 *
 * 本文件只做「输入校验 + 状态枚举推导」，不持有 Context、不读写磁盘、不启动服务；
 * 落盘与界面显示交给 [MainActivity]，这样规则可以被真跑单测覆盖。
 */
object ConnectionSettings {
    /** 与 [DeviceConnectionService] 和 `MainActivity.resolveDeviceId()` 共用的 prefs 文件名。 */
    const val PREFERENCES_NAME = "device_connection"

    /** 主机 WSS 地址键：`DeviceConnectionService.connectIfConfigured()` 直接读它去建连。 */
    const val KEY_WSS_URL = "wss_url"

    /** 设备 id 键：`MainActivity.resolveDeviceId()` 读它；也必须满足信封 schema 的 device_id 规则。 */
    const val KEY_DEVICE_ID = "device_id"

    /**
     * 主机 TLS 证书的 SHA-256 指纹（64 位小写十六进制）。
     *
     * 用途：**证书固定**。主机用自签名证书时 Android 默认不信任它，
     * 连 WSS 会在 TLS 握手阶段被拒（实测 `self-signed certificate`）。
     * 有了这个值，连接服务就能只信任「恰好这一张」证书——
     * 既不降安全强度（比信任所有证书严格得多），也不需要用户去手机设置里装证书。
     *
     * 空值表示「没有证书」（回环明文试验），此时不启用固定。
     */
    const val KEY_HOST_SIGNING_PUBLIC_KEY = "host_signing_public_key"
    const val KEY_CERT_FINGERPRINT = "cert_fingerprint"

    /**
     * 连接状态痕迹键（**只读**，由连接服务将来写入；本页从不写它）。
     *
     * 服务是另一个组件，Activity 拿不到它的 `hasOpenSocket`。与其在页面上假定「已连接」，
     * 不如：能读到服务留下的痕迹才显示「已连接」，读不到就一律显示「已配置，未连接」。
     */
    const val KEY_STATUS_TRACE = "last_status"
    const val KEY_LAST_ERROR = "last_error"

    /** 只显示故障类别，异常原文可能包含地址或凭据，不直接展示。 */
    fun connectionFailureMessage(error: Throwable): String = when (error) {
        is javax.net.ssl.SSLHandshakeException -> "TLS 握手失败，请检查主机证书，必要时重新扫码配对"
        is javax.net.ssl.SSLPeerUnverifiedException -> "主机证书验证失败，请重新扫码配对"
        is java.net.UnknownHostException -> "无法解析主机地址，请检查连接地址"
        is java.net.ConnectException -> "无法连接主机，请确认电脑已启动主机且网络可达"
        is java.net.SocketTimeoutException -> "连接主机超时，请检查网络与防火墙"
        else -> "连接失败（${error.javaClass.simpleName}），请重新启动连接"
    }

    /** 服务侧的「已连接」痕迹取值。 */
    const val TRACE_CONNECTED = "connected"

    /** 服务侧的「已断开」痕迹取值。 */
    const val TRACE_DISCONNECTED = "disconnected"

    /** `device_id` 合法字符集与长度（与 `contracts/device-envelope.schema.json` 同规则）。 */
    const val DEVICE_ID_PATTERN = "[A-Za-z0-9._:-]{3,80}"

    /** `device_id` 最短长度。 */
    const val MIN_DEVICE_ID_LENGTH = 3

    /** `device_id` 最长长度。 */
    const val MAX_DEVICE_ID_LENGTH = 80

    /** 允许的两种传输前缀；`ws://` 不加密，只用于本机试验。 */
    private val ALLOWED_SCHEMES = setOf("ws", "wss")

    private val deviceIdRegex = Regex(DEVICE_ID_PATTERN)

    // ── 校验结果的文案（供调用方展示，不静默吞错） ─────────────────

    const val ERROR_URL_EMPTY = "主机地址不能为空：请填写 ws:// 或 wss:// 地址"
    const val ERROR_URL_SCHEME = "地址必须以 ws:// 或 wss:// 开头"
    const val ERROR_URL_MALFORMED = "地址不是合法 URL：%s"
    const val ERROR_URL_NO_HOST = "地址缺少主机名（ws:// 后必须紧跟主机，例如 ws://10.0.2.2:8787）"
    const val ERROR_DEVICE_ID_EMPTY = "设备 ID 不能为空：需要 3-80 位字母、数字或 . _ : -"
    const val ERROR_DEVICE_ID_SHAPE = "设备 ID 只能由字母、数字与 . _ : - 组成，长度 3-80"
    const val WARNING_INSECURE_TRANSPORT = "警告：ws:// 不加密，仅可对可信本机试验；生产请用 wss://"

    /** `wss_url` 校验结果。 */
    sealed class UrlCheck {
        /** 合法地址；[insecure] 为 true 表示是明文的 `ws://`，界面必须显式警告。 */
        data class Valid(val normalizedUrl: String, val insecure: Boolean) : UrlCheck()

        /** 非法地址；[message] 直接可展示，绝不静默保存。 */
        data class Invalid(val message: String) : UrlCheck()
    }

    /** `device_id` 校验结果。 */
    sealed class DeviceIdCheck {
        /** 合法 id（已 trim）。 */
        data class Valid(val normalizedId: String) : DeviceIdCheck()

        /** 非法 id；[message] 直接可展示。 */
        data class Invalid(val message: String) : DeviceIdCheck()
    }

    /** 一次「保存并启动」前的整体校验。 */
    sealed class ValidationResult {
        /** 两项都合法，可以落盘并启动服务。 */
        data class Accepted(
            val wssUrl: String,
            val deviceId: String,
            val insecureTransport: Boolean,
        ) : ValidationResult()

        /** 至少一项不合法；[errors] 全部展示给用户，**此时不得保存也不得启动服务**。 */
        data class Rejected(val errors: List<String>) : ValidationResult()
    }

    /**
     * 界面能诚实显示的三档状态（第四档 `NOT_CONFIGURED` 之外都带明确限定语）。
     *
     * 只由「配置是否存在 + 服务是否在运行 + 服务留下的痕迹」推导，
     * 因此 [NOT_CONFIGURED] 之外的 [CONNECTED] 必须有服务痕迹作证据。
     */
    enum class ConnectionState {
        /** 没有主机地址：连失败都谈不上，界面显示「未配置」。 */
        NOT_CONFIGURED,

        /** 已配置，但连接服务没在运行 → 一定是未连接。 */
        CONFIGURED_SERVICE_STOPPED,

        /** 已配置，服务在运行，但没有「已连接」痕迹 → 只显示未确认，绝不显示在线。 */
        CONFIGURED_SERVICE_RUNNING,

        /** 已配置、服务在运行、且服务留下了 `last_status=connected` 痕迹。 */
        CONNECTED,
    }

    /** 校验主机地址。空值、非 ws/wss 方案、非法 URL、缺主机名一律拒绝并给出明确文案。 */
    fun validateUrl(raw: String?): UrlCheck {
        val trimmed = raw?.trim().orEmpty()
        if (trimmed.isEmpty()) return UrlCheck.Invalid(ERROR_URL_EMPTY)
        val lower = trimmed.lowercase()
        if (!lower.startsWith("ws://") && !lower.startsWith("wss://")) {
            return UrlCheck.Invalid(ERROR_URL_SCHEME)
        }
        // `URI` 对 `ws://` 与 `ws://:8787` 会直接抛「Expected authority」，
        // 这类输入的真实问题就是「没有主机名」，所以先按 authority 手工判一次，
        // 给出比解析异常更明确的文案（解析异常文案依赖 JDK 内部措辞，不该当作契约）。
        val authority = trimmed
            .substringAfter("://")
            .substringBefore('/')
            .substringBefore('?')
            .substringBefore('#')
        val trimmedAuthority = authority.trim()
        val portOnly = Regex(":\\d*").matches(trimmedAuthority)
        if (trimmedAuthority.isEmpty() || portOnly) return UrlCheck.Invalid(ERROR_URL_NO_HOST)
        val uri = try {
            URI(trimmed)
        } catch (exception: URISyntaxException) {
            return UrlCheck.Invalid(ERROR_URL_MALFORMED.format(exception.reason.orEmpty()))
        }
        val scheme = uri.scheme?.lowercase().orEmpty()
        if (scheme !in ALLOWED_SCHEMES) return UrlCheck.Invalid(ERROR_URL_SCHEME)
        val host = uri.host
        if (host.isNullOrBlank()) return UrlCheck.Invalid(ERROR_URL_NO_HOST)
        return UrlCheck.Valid(normalizedUrl = trimmed, insecure = scheme == "ws")
    }

    /** 校验 `device_id`：`[A-Za-z0-9._:-]{3,80}`，空值同样拒绝（空 id 无法通过信封 schema）。 */
    fun validateDeviceId(raw: String?): DeviceIdCheck {
        val trimmed = raw?.trim().orEmpty()
        if (trimmed.isEmpty()) return DeviceIdCheck.Invalid(ERROR_DEVICE_ID_EMPTY)
        if (!deviceIdRegex.matches(trimmed)) return DeviceIdCheck.Invalid(ERROR_DEVICE_ID_SHAPE)
        return DeviceIdCheck.Valid(normalizedId = trimmed)
    }

    /** 保存前整体校验：两项都通过才 [ValidationResult.Accepted]。 */
    fun validate(wssUrl: String?, deviceId: String?): ValidationResult {
        val errors = ArrayList<String>()
        val urlCheck = validateUrl(wssUrl)
        val idCheck = validateDeviceId(deviceId)
        if (urlCheck is UrlCheck.Invalid) errors.add(urlCheck.message)
        if (idCheck is DeviceIdCheck.Invalid) errors.add(idCheck.message)
        if (errors.isNotEmpty()) return ValidationResult.Rejected(errors)
        val acceptedUrl = urlCheck as UrlCheck.Valid
        val acceptedId = idCheck as DeviceIdCheck.Valid
        return ValidationResult.Accepted(
            wssUrl = acceptedUrl.normalizedUrl,
            deviceId = acceptedId.normalizedId,
            insecureTransport = acceptedUrl.insecure,
        )
    }

    /**
     * 从「有没有配置 / 服务在不在跑 / 服务留下什么痕迹」推导显示状态。
     *
     * 关键规则：`serviceRunning == false` 时永远不可能是 [ConnectionState.CONNECTED]，
     * 痕迹缺失或取值未知也一律不升级成 [ConnectionState.CONNECTED]——
     * 宁可显示「未连接」，也不假显示「已连接」。
     */
    fun resolveState(
        wssUrl: String?,
        serviceRunning: Boolean,
        statusTrace: String?,
    ): ConnectionState {
        val configured = wssUrl?.trim().orEmpty().isNotEmpty()
        if (!configured) return ConnectionState.NOT_CONFIGURED
        if (!serviceRunning) return ConnectionState.CONFIGURED_SERVICE_STOPPED
        val trace = statusTrace?.trim()?.lowercase().orEmpty()
        return if (trace == TRACE_CONNECTED) {
            ConnectionState.CONNECTED
        } else {
            ConnectionState.CONFIGURED_SERVICE_RUNNING
        }
    }
}
