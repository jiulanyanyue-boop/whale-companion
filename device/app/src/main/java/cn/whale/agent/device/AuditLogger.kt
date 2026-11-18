package cn.whale.agent.device

/**
 * M12 设备端审计脱敏。
 *
 * 规范来源：`docs/modules/M12-recovery-audit-product.md`「审计内容」与
 * `M12-recovery-audit-development.md`「审计脱敏」。
 *
 * 固定规则（与主机侧 `host/astrbot_whale_device/audit.py` 逐条镜像）：
 *
 * - **只记录**：事件类型、时间、模式、发送者分类、命令名、包名、结果、错误码。
 * - **禁止写入**：密码、验证码、短信正文、完整联系人号码、屏幕节点文本、原始音频。
 * - 电话号码**最多保留后四位**。
 *
 * 全部是**纯函数**：不依赖 `android.util.Log`、无 I/O、无全局状态，可直接 JVM 单测。
 *
 * 采用「默认拒绝」而不是黑名单：只有落进 [ALLOWED_DETAIL_KEYS] 的键才可能被写入。
 * 黑名单永远会漏；白名单不会。
 */
object AuditLogger {

    /** 允许进入审计 detail 的键（白名单，默认拒绝）。 */
    val ALLOWED_DETAIL_KEYS: Set<String> = setOf(
        // 身份分类（原始 QQ 号不进 detail）
        "mode", "message_type", "sender_class", "actor_class",
        // 命令与结果
        "command", "package_name", "result", "status", "error_code", "outcome", "decision",
        // 设备
        "device_id", "connection", "connection_state", "state", "reason",
        // Run 结构
        "run_id", "run_status", "step_index", "command_index", "total_commands",
        "from", "to", "phase", "layer", "count",
        // 时间与幂等
        "updated_at", "created_at", "sent_at", "attempts", "retries", "idempotency_key",
        // 恢复语义
        "resumed", "skipped", "executed", "requires_reconfirmation", "revalidate_required", "lease_until",
    )

    /** 即使误入白名单也必须硬拒的键（纵深防御，与白名单互为双保险）。 */
    val FORBIDDEN_DETAIL_KEYS: Set<String> = setOf(
        "password", "passwd", "pwd", "otp", "code", "verification_code",
        "sms_body", "sms_text", "body", "text",
        "node_text", "nodes", "screen_text", "screen_nodes", "screenshot",
        "raw_audio", "audio", "media", "media_bytes", "media_content",
        "content", "payload", "path", "uri", "file", "clipboard",
        "token", "secret", "api_key", "credential", "credentials",
    )

    /** 值里一旦出现这些字面量，整个值丢弃（宁可少记，不可泄漏）。 */
    private val SENSITIVE_VALUE_MARKERS = listOf(
        "password", "passwd", "验证码", "密码", "短信正文", "otp ", "verification code",
    )

    private const val MAX_DETAIL_KEYS = 24
    private const val MAX_LIST_ITEMS = 20
    private const val MAX_TEXT_CHARS = 512
    private const val MAX_DEPTH = 3

    /** 少于 7 位的数字串不算电话号码，原样保留。 */
    private const val PHONE_MIN_DIGITS = 7

    /** 显式拼接，避免字符串模板里 `$CONST,20` 这种依赖词法细节的脆弱写法。 */
    private val PHONE_RUN = Regex("(?<!\\d)(\\d{" + PHONE_MIN_DIGITS + ",20})(?!\\d)")

    /** 2^53，JS/JSON 安全整数上界；超出即视为标识而非数值内容。 */
    private const val SAFE_LONG_RANGE_MAX = 9_007_199_254_740_992.0
    private const val SAFE_LONG_RANGE_MIN = -9_007_199_254_740_992.0
    private val SAFE_LONG_RANGE = -9_007_199_254_740_992L..9_007_199_254_740_992L

    /**
     * 把长度 ≥ 7 的数字串只留后四位，前面全部打码。
     */
    fun redactPhone(value: String): String {
        if (value.isEmpty()) return value
        return PHONE_RUN.replace(value) { match ->
            val digits = match.groupValues[1]
            "*".repeat(digits.length - 4) + digits.takeLast(4)
        }
    }

    /**
     * 自由文本的最小安全化。
     *
     * 返回 `null` 表示**该值必须整体丢弃**（命中敏感字面量）；
     * 返回原值（可能已打码/截断）表示可安全写入。
     */
    fun redactText(value: String): String? {
        val lowered = value.lowercase()
        if (SENSITIVE_VALUE_MARKERS.any { lowered.contains(it) }) return null
        if (value.length > MAX_TEXT_CHARS) {
            return redactPhone(value.take(MAX_TEXT_CHARS)) + "…"
        }
        return redactPhone(value)
    }

    private fun isForbiddenKey(key: String): Boolean {
        val name = key.trim().lowercase()
        if (name.isEmpty()) return true
        if (name in FORBIDDEN_DETAIL_KEYS) return true
        // 驼峰/点号变体：screenText / screen.text / rawAudio
        val flat = name.replace("_", "").replace("-", "").replace(".", "")
        return flat in FORBIDDEN_DETAIL_FLAT
    }

    private val FORBIDDEN_DETAIL_FLAT: Set<String> =
        FORBIDDEN_DETAIL_KEYS.map { it.replace("_", "") }.toSet()

    /**
     * 把任意 detail 收敛成白名单内的安全 Map。
     *
     * - 键不在白名单 → 丢弃（默认拒绝）
     * - 命中 [FORBIDDEN_DETAIL_KEYS] → 丢弃（纵深防御）
     * - 值为 null / 空 Map / 空 List / 敏感命中 → 丢弃该键
     * - 字符串截断 + 号码打码；最多 24 键、每层最多 3 层、列表最多 20 项
     *
     * 值只接受 String / Int / Long / Double / Boolean / Map / List；
     * 其它类型（ByteArray、Uri 等）一律不写。
     */
    fun sanitizeDetail(detail: Map<String, Any?>?): Map<String, Any?> {
        if (detail == null) return emptyMap()
        val result = LinkedHashMap<String, Any?>()
        for ((rawKey, rawValue) in detail) {
            if (result.size >= MAX_DETAIL_KEYS) break
            if (rawKey.contains(" ") || rawKey.isBlank()) continue
            if (isForbiddenKey(rawKey)) continue
            if (rawKey !in ALLOWED_DETAIL_KEYS) continue
            val scrubbed = scrub(rawValue, 0) ?: continue
            result[rawKey] = scrubbed
        }
        return result
    }

    /**
     * 泛型入口：接受任意 key 类型的 Map，键统一按 [String] 判定，
     * 避免 `as Map<String, Any?>` 在运行时抛 `ClassCastException`。
     */
    fun sanitizeAnyDetail(detail: Map<*, *>?): Map<String, Any?> {
        if (detail == null) return emptyMap()
        val normalized = LinkedHashMap<String, Any?>()
        for ((rawKey, rawValue) in detail) {
            val key = rawKey?.toString() ?: continue
            normalized[key] = rawValue
        }
        return sanitizeDetail(normalized)
    }

    /** 返回 null 表示「该值必须整体丢弃」。 */
    private fun scrub(value: Any?, depth: Int): Any? {
        if (depth > MAX_DEPTH) return null
        return when (value) {
            null -> null
            is Boolean -> value
            is Int -> value.toLong().takeIf { it in SAFE_LONG_RANGE }
            is Long -> value.takeIf { it in SAFE_LONG_RANGE }
            is Double -> {
                // NaN / Inf 不写；超范围的双精度同样不写。
                if (value.isNaN() || value.isInfinite() || value > SAFE_LONG_RANGE_MAX || value < SAFE_LONG_RANGE_MIN) {
                    null
                } else {
                    value
                }
            }
            is String -> redactText(value)
            is Map<*, *> -> {
                val nested = sanitizeAnyDetail(value)
                if (nested.isEmpty()) null else nested
            }
            is List<*> -> {
                val items = ArrayList<Any?>()
                for (entry in value.take(MAX_LIST_ITEMS)) {
                    val scrubbed = scrub(entry, depth + 1)
                    if (scrubbed != null) {
                        items.add(scrubbed)
                    }
                }
                if (items.isEmpty()) null else items
            }
            else -> null  // ByteArray / Uri / 任意对象：一律不写
        }
    }

    /** 构造一条可直接落库的审计记录（纯函数）。 */
    fun record(
        eventType: String,
        messageId: String = "",
        actorUserId: String = "",
        detail: Map<String, Any?>? = null,
        createdAt: Long = 0L,
    ): AuditRecord {
        require(eventType.isNotBlank()) { "audit eventType is required" }
        return AuditRecord(
            eventType = eventType,
            messageId = messageId,
            actorUserId = actorUserId,
            detail = sanitizeDetail(detail),
            createdAt = createdAt,
        )
    }
}

/**
 * 一条已脱敏的审计记录。
 *
 * [detail] 里的每个值都已过 [AuditLogger.sanitizeDetail]，
 * 因此即使调用方传了敏感字段也进不来。
 */
data class AuditRecord(
    val eventType: String,
    val messageId: String = "",
    val actorUserId: String = "",
    val detail: Map<String, Any?> = emptyMap(),
    val createdAt: Long = 0L,
)
