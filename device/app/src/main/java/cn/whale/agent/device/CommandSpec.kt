package cn.whale.agent.device

/**
 * M06 固定 payload 契约。
 *
 * 本文件是 `contracts/device-commands.schema.json` 在设备端的镜像：
 * 字段名、必填性、类型、长度、数值范围和枚举都必须与 schema 逐字一致。
 * 设备端校验是最终闸门，不能信任主机已经校验过（见 M06 开发文档「payload 校验」）。
 *
 * 修改本文件必须同步三处：
 * 1. `contracts/device-commands.schema.json`
 * 2. `docs/modules/M06-command-runtime-development.md` 的路由表
 * 3. `tools/check-module-contracts.py` 的静态一致性检查（它会真实运行）
 */
enum class PayloadFieldType {
    STRING,
    INTEGER,
    OBJECT,
}

data class PayloadFieldSpec(
    val name: String,
    val type: PayloadFieldType,
    val required: Boolean = false,
    val minLength: Int? = null,
    val maxLength: Int? = null,
    val minValue: Long? = null,
    val maxValue: Long? = null,
    val allowedValues: Set<String>? = null,
)

data class CommandSpec(
    val command: String,
    val fields: List<PayloadFieldSpec> = emptyList(),
    /** schema 中 payload 为 `const {}` 的命令：payload 必须是空对象。 */
    val emptyPayload: Boolean = false,
) {
    fun field(name: String): PayloadFieldSpec? = fields.firstOrNull { it.name == name }

    val declaredFieldNames: Set<String> = fields.map { it.name }.toSet()
}

object CommandSpecs {
    /** `ui_target`：四个动作命令共用的目标指纹字段。 */
    private fun uiTargetFields(): List<PayloadFieldSpec> = listOf(
        PayloadFieldSpec("snapshot_id", PayloadFieldType.STRING, required = true, minLength = 1),
        PayloadFieldSpec("target_id", PayloadFieldType.STRING, required = true, minLength = 1),
        PayloadFieldSpec("selector_hash", PayloadFieldType.STRING, required = true, minLength = 1),
        PayloadFieldSpec("expected_package", PayloadFieldType.STRING, required = true, minLength = 1, maxLength = 255),
        PayloadFieldSpec("expected_state", PayloadFieldType.OBJECT),
    )

    private fun smsFields(): List<PayloadFieldSpec> = listOf(
        PayloadFieldSpec("number", PayloadFieldType.STRING, required = true, minLength = 3, maxLength = 32),
        PayloadFieldSpec("text", PayloadFieldType.STRING, required = true, minLength = 1, maxLength = 5000),
    )

    val ALL: List<CommandSpec> = listOf(
        // app.list / device_status：payload 固定为空对象。
        CommandSpec("device.app.list", emptyPayload = true),
        CommandSpec("device.screen.capture", emptyPayload = true),
        CommandSpec(
            "device.app.launch",
            fields = listOf(
                PayloadFieldSpec("package_name", PayloadFieldType.STRING, required = true, minLength = 3, maxLength = 255),
            ),
        ),

        // ui.observe：无必填字段，额外字段拒绝。
        CommandSpec(
            "device.ui.observe",
            fields = listOf(
                PayloadFieldSpec(
                    "mode",
                    PayloadFieldType.STRING,
                    allowedValues = setOf("auto", "tree", "vision"),
                ),
                PayloadFieldSpec("package_name", PayloadFieldType.STRING, maxLength = 255),
            ),
        ),
        CommandSpec("device.ui.tap", fields = uiTargetFields()),
        CommandSpec(
            "device.ui.long_press",
            fields = uiTargetFields() + PayloadFieldSpec(
                "duration_ms",
                PayloadFieldType.INTEGER,
                required = true,
                minValue = 300L,
                maxValue = 30000L,
            ),
        ),
        CommandSpec(
            "device.ui.scroll",
            fields = uiTargetFields() + listOf(
                PayloadFieldSpec(
                    "direction",
                    PayloadFieldType.STRING,
                    required = true,
                    allowedValues = setOf("up", "down", "left", "right"),
                ),
                PayloadFieldSpec("amount", PayloadFieldType.INTEGER, minValue = 1L, maxValue = 2000L),
            ),
        ),
        CommandSpec(
            "device.ui.input",
            fields = uiTargetFields() + PayloadFieldSpec(
                "text",
                PayloadFieldType.STRING,
                required = true,
                maxLength = 20000,
            ),
        ),

        // 四个全局动作：payload 固定为空对象。
        CommandSpec("device.ui.back", emptyPayload = true),
        CommandSpec("device.ui.home", emptyPayload = true),
        CommandSpec("device.ui.recents", emptyPayload = true),
        CommandSpec("device.ui.notifications", emptyPayload = true),

        CommandSpec(
            "device.system.settings",
            fields = listOf(
                PayloadFieldSpec("action", PayloadFieldType.STRING, maxLength = 200),
            ),
        ),

        CommandSpec(
            "device.camera.capture",
            fields = listOf(
                PayloadFieldSpec("lens", PayloadFieldType.STRING, allowedValues = setOf("back", "front")),
                PayloadFieldSpec("quality", PayloadFieldType.STRING, allowedValues = setOf("standard", "high")),
            ),
        ),
        CommandSpec(
            "device.media.pick",
            fields = listOf(
                PayloadFieldSpec(
                    "media_type",
                    PayloadFieldType.STRING,
                    required = true,
                    allowedValues = setOf("image", "video", "audio", "any"),
                ),
                PayloadFieldSpec("max_items", PayloadFieldType.INTEGER, required = true, minValue = 1L, maxValue = 20L),
            ),
        ),
        CommandSpec(
            "device.media.upload",
            fields = listOf(
                PayloadFieldSpec("asset_id", PayloadFieldType.STRING, required = true, minLength = 1, maxLength = 160),
            ),
        ),
        CommandSpec("device.gallery.list", fields = listOf(
            PayloadFieldSpec("limit", PayloadFieldType.INTEGER, minValue = 1L, maxValue = 20L),
            PayloadFieldSpec("offset", PayloadFieldType.INTEGER, minValue = 0L, maxValue = 1000L),
        )),
        CommandSpec("device.gallery.read", fields = listOf(
            PayloadFieldSpec("media_id", PayloadFieldType.STRING, required = true, minLength = 1, maxLength = 80),
        )),
        CommandSpec(
            "device.media.share",
            fields = listOf(
                PayloadFieldSpec("asset_id", PayloadFieldType.STRING, required = true, minLength = 1, maxLength = 160),
                PayloadFieldSpec("target", PayloadFieldType.STRING, maxLength = 160),
            ),
        ),

        CommandSpec(
            "device.telephony.dial",
            fields = listOf(
                PayloadFieldSpec("number", PayloadFieldType.STRING, required = true, minLength = 3, maxLength = 32),
            ),
        ),
        CommandSpec(
            "device.contacts.search",
            fields = listOf(
                PayloadFieldSpec("query", PayloadFieldType.STRING, required = true, minLength = 1, maxLength = 120),
            ),
        ),
        CommandSpec("device.sms.compose", fields = smsFields()),
        CommandSpec("device.sms.send", fields = smsFields()),

        CommandSpec("device.device_status", emptyPayload = true),
        // 自动解锁只读取手机本地加密凭据，远程 payload 永远为空。
        CommandSpec("device.system.unlock", emptyPayload = true),
    )

    val BY_COMMAND: Map<String, CommandSpec> = ALL.associateBy { it.command }

    /** 已声明命令集合；与 `DeviceCommands.ALL` 必须完全相等（由 CommandSpecTest 断言）。 */
    val DECLARED_COMMANDS: Set<String> = BY_COMMAND.keys

    fun find(command: String): CommandSpec? = BY_COMMAND[command]

    fun isDeclared(command: String): Boolean = BY_COMMAND.containsKey(command)
}
