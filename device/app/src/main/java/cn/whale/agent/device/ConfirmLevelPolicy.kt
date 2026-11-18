package cn.whale.agent.device

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * M04 固定四级确认级别（`docs/modules/M04-owner-security-product.md`「确认级别」）。
 *
 * ```
 * DENY
 * CONFIRM_EVERY_STEP
 * CONFIRM_FIRST_STEP
 * ALLOW_NORMAL
 * ```
 *
 * `strictness` 越大越严格：**确认级别只能变严，不能变宽**。
 * 这就是 `docs/modules/M04-owner-security-product.md`「系统设置、安装器、短信发送、删除、
 * 发布、支付、凭据输入固定至少 `CONFIRM_EVERY_STEP`，不能被人设或模型降低」的落地形式。
 *
 * 注意：[DENY] 的 `strictness` 最高，但语义不是「更严格的确认」，而是「根本不执行」；
 * 所以 [ConfirmLevelPolicy.effectiveLevel] 对 [DENY] 单独短路，绝不把它放宽成别的级别。
 */
enum class ConfirmLevel(val strictness: Int) {
    DENY(3),
    CONFIRM_EVERY_STEP(2),
    CONFIRM_FIRST_STEP(1),
    ALLOW_NORMAL(0),
    ;

    /** 取两个级别里更严格的一个。 */
    fun atLeast(other: ConfirmLevel): ConfirmLevel = if (strictness >= other.strictness) this else other

    companion object {
        /**
         * 解析配置/人设里写下的字符串。
         *
         * 无法识别的值一律 [DENY]（fail-closed）：既不是「猜一个接近的」，也不是「退回默认放行」。
         * 大小写必须完全一致，避免 `allow_normal` 这类变体被当成 [ALLOW_NORMAL]。
         */
        fun parseOrDeny(raw: String?): ConfirmLevel {
            val value = raw?.trim().orEmpty()
            return entries.firstOrNull { it.name == value } ?: DENY
        }
    }
}

/**
 * M04 确认级别策略：固定不可降低清单 + 高风险命令权威集合。
 *
 * 规范来源只有 `docs/modules/M04-owner-security-product.md` 与
 * `docs/modules/M04-owner-security-development.md`。本对象**不提供**任何「放宽」入口：
 * 调用方只能在 [ConfirmLevelPolicy.effectiveLevel] 里拿到「请求级别与固定下限里更严的那个」。
 *
 * 人设、模型输出、语音、远程消息都**不能**参与这里的判定；它们最多是「请求级别」的输入，
 * 而请求级别永远会被固定下限压住。
 */
object ConfirmLevelPolicy {
    /**
     * M04 权威高风险清单，与开发文档「高风险命令」逐条一致：
     *
     * ```
     * device.sms.send
     * device.system.settings
     * device.media.share       # 当目标是外部社交应用
     * device.ui.input          # 目标是密码/验证码/支付字段
     * device.ui.tap            # 目标是发布/删除/付款按钮
     * ```
     *
     * M01 的 [RiskPolicy] 只有命令名参数、拿不到 payload，因此命令级判定必须 **fail-closed**
     * （整条命令都算高风险），payload 级细化在 [floorFor] 里做。宁可多要一次确认，也不放过一条。
     */
    val HIGH_RISK_COMMANDS: Set<String> = linkedSetOf(
        "device.sms.send",
        "device.system.settings",
        "device.media.share",
        "device.ui.input",
        "device.ui.tap",
    )

    /** 「固定至少 `CONFIRM_EVERY_STEP`」清单里按命令名直接锁死的项：系统设置、短信发送。 */
    val PINNED_EVERY_STEP_COMMANDS: Set<String> = linkedSetOf(
        "device.sms.send",
        "device.system.settings",
    )

    /** 凭据输入（密码/验证码/支付字段）。 */
    val CREDENTIAL_MARKERS: List<String> = listOf(
        "password", "passwd", "pwd", "pin", "otp", "verification", "verify",
        "captcha", "credential", "secret", "token", "cvv", "card_number", "account_number",
    )

    /** 支付（付款按钮、金额确认、支付类应用）。 */
    val PAYMENT_MARKERS: List<String> = listOf(
        "payment", "checkout", "pay", "transfer", "amount", "wallet", "bank", "alipay", "weixin_pay",
    )

    /** 删除、发布、提交类按钮。 */
    val DESTRUCTIVE_MARKERS: List<String> = listOf(
        "delete", "remove", "uninstall", "clear_data", "reset", "publish", "post", "submit", "send_now",
    )

    /** 安装器（安装/卸载类应用与动作）。 */
    val INSTALLER_MARKERS: List<String> = listOf(
        "packageinstaller", "installer", "install_package", "install_app",
    )

    /** 安全设置（隐私、权限、开发者选项、无障碍、锁屏）。 */
    val SECURITY_MARKERS: List<String> = listOf(
        "security", "privacy", "permission", "developer", "unknown_sources",
        "accessibility", "device_admin", "lock_screen", "screen_lock",
    )

    /**
     * 判定时**只看**这些「目标指纹」类字段，刻意排除 `text` / `textValue` 等正文类字段：
     * 短信正文和用户输入内容不参与风险判定，也不进日志（文档禁止保存敏感内容）。
     */
    private val MATCH_FIELDS: List<String> = listOf(
        "target_id", "selector_hash", "expected_package", "package_name",
        "target", "action", "label", "content_description",
    )

    /** 单条 payload 最多参与判定的字段数，避免超长 payload 被用来拖慢判定。 */
    private const val MAX_MATCH_FIELDS: Int = 12

    /** 单字段参与判定的最大长度。 */
    private const val MAX_MATCH_FIELD_LENGTH: Int = 512

    /**
     * 固定下限：命令 + payload 允许达到的最宽级别。
     *
     * @param payload `null` 表示「调用方拿不到 payload」（例如 M01 的 [RiskConfirmation]），
     *   此时按 **fail-closed** 取更严的下限，绝不假设「看不出来就是安全的」。
     */
    fun floorFor(command: String, payload: JsonObject? = null): ConfirmLevel {
        val text = matchText(payload)
        val unknown = payload == null
        return when (command) {
            // 系统设置、短信发送：命令级直接锁死。
            "device.sms.send",
            "device.system.settings",
            -> ConfirmLevel.CONFIRM_EVERY_STEP

            // 凭据输入：密码/验证码/支付字段 → 每步确认；普通文本输入也至少首步确认。
            "device.ui.input" -> if (unknown || containsAny(text, CREDENTIAL_MARKERS + PAYMENT_MARKERS)) {
                ConfirmLevel.CONFIRM_EVERY_STEP
            } else {
                ConfirmLevel.CONFIRM_FIRST_STEP
            }

            // 点击：删除/发布/付款/安装类按钮 → 每步确认；普通按钮至少首步确认。
            "device.ui.tap" -> if (unknown || containsAny(text, DESTRUCTIVE_MARKERS + PAYMENT_MARKERS + INSTALLER_MARKERS)) {
                ConfirmLevel.CONFIRM_EVERY_STEP
            } else {
                ConfirmLevel.CONFIRM_FIRST_STEP
            }

            // 媒体外发：目标是外部社交应用 → 每步确认；没有目标信息时同样 fail-closed。
            "device.media.share" -> {
                val target = (payload?.get("target") as? JsonPrimitive)?.content.orEmpty()
                if (unknown || target.isNotBlank()) ConfirmLevel.CONFIRM_EVERY_STEP else ConfirmLevel.CONFIRM_FIRST_STEP
            }

            // 安装器：启动安装/卸载类应用也属于「安装器」这一固定项。
            "device.app.launch" -> if (payload != null && containsAny(text, INSTALLER_MARKERS)) {
                ConfirmLevel.CONFIRM_EVERY_STEP
            } else {
                ConfirmLevel.ALLOW_NORMAL
            }

            // 其他命令不在 M04 固定清单里，按应用级配置执行（应用白名单由 DevicePolicy 负责）。
            else -> ConfirmLevel.ALLOW_NORMAL
        }
    }

    /**
     * 最终生效级别 = 「请求级别」与「固定下限」里更严的那个。
     *
     * 三种短路：
     * 1. 请求级别是 [ConfirmLevel.DENY] → 保持 [ConfirmLevel.DENY]（用户的拒绝不能被放宽）；
     * 2. 固定下限是 [ConfirmLevel.DENY] → 保持 [ConfirmLevel.DENY]；
     * 3. 其余情况取 `max(请求, 下限)`，因此人设/模型把级别调到 [ConfirmLevel.ALLOW_NORMAL]
     *    也压不过系统设置、短信发送等固定项。
     */
    fun effectiveLevel(
        requested: ConfirmLevel,
        command: String,
        payload: JsonObject? = null,
    ): ConfirmLevel {
        if (requested == ConfirmLevel.DENY) return ConfirmLevel.DENY
        val floor = floorFor(command, payload)
        if (floor == ConfirmLevel.DENY) return ConfirmLevel.DENY
        return requested.atLeast(floor)
    }

    /** 该命令是否属于 M04 高风险清单（供 [OwnerRiskPolicy] / 审计使用）。 */
    fun isHighRisk(command: String): Boolean = command in HIGH_RISK_COMMANDS

    /** 该命令在当前 payload 下是否被锁在「每步确认」。 */
    fun isPinnedToEveryStep(command: String, payload: JsonObject? = null): Boolean =
        floorFor(command, payload) == ConfirmLevel.CONFIRM_EVERY_STEP

    private fun matchText(payload: JsonObject?): String {
        if (payload == null) return ""
        return MATCH_FIELDS
            .take(MAX_MATCH_FIELDS)
            .mapNotNull { key -> (payload[key] as? JsonPrimitive)?.content }
            .joinToString(" ")
            .take(MAX_MATCH_FIELD_LENGTH)
            .lowercase()
    }

    private fun containsAny(text: String, markers: List<String>): Boolean =
        text.isNotEmpty() && markers.any { marker -> text.contains(marker) }
}

/**
 * M04 提供的高风险判定策略生产实现。
 *
 * 直接替换 M01 的过渡默认值 `RiskPolicies.INTERIM_FALLBACK`：
 *
 * ```kotlin
 * LocalModeGate(mode, session, executor, userGateRiskConfirmation, OwnerRiskPolicy)
 * ```
 *
 * 与过渡值的区别只有两点：清单归属 M04（唯一规范来源），并且判定只认文档列出的五条命令。
 * 由于 [RiskPolicy] 拿不到 payload，`device.media.share` / `device.ui.input` / `device.ui.tap`
 * 在这里一律视为高风险（fail-closed），payload 级的下调（例如普通按钮只首步确认）
 * 由 [ConfirmLevelPolicy] 在确认状态机内部按实际 payload 完成——**M01 的闸门顺序一行都不用改**。
 */
object OwnerRiskPolicy : RiskPolicy {
    override fun isHighRisk(command: String): Boolean = ConfirmLevelPolicy.isHighRisk(command)
}
