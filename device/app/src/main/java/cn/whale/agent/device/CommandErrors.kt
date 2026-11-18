package cn.whale.agent.device

/**
 * M06 固定错误码集合。
 *
 * 这些值同时约束 M01 模式闸门、M06 命令运行时和 M07 无障碍动作层。
 * 任何新错误码都必须先写入 docs/modules/M06-command-runtime-development.md，
 * 再回到这里登记，禁止在路由代码里散落字符串字面量。
 */
object CommandErrors {
    // M06 文档原有固定集合。
    const val UNKNOWN_COMMAND = "UNKNOWN_COMMAND"
    const val INVALID_PAYLOAD = "INVALID_PAYLOAD"
    const val PACKAGE_NOT_ALLOWED = "PACKAGE_NOT_ALLOWED"
    const val MODE_REJECTED = "MODE_REJECTED"
    const val SIGNATURE_REQUIRED = "SIGNATURE_REQUIRED"
    const val REPLAY = "REPLAY"
    const val EXPIRED = "EXPIRED"
    const val NO_ACCESSIBILITY = "NO_ACCESSIBILITY"
    const val STALE_SNAPSHOT = "STALE_SNAPSHOT"
    const val SELECTOR_MISMATCH = "SELECTOR_MISMATCH"
    const val FOREGROUND_PACKAGE_MISMATCH = "FOREGROUND_PACKAGE_MISMATCH"
    const val PERMISSION_REQUIRED = "PERMISSION_REQUIRED"
    const val NEEDS_UNLOCK = "NEEDS_UNLOCK"
    const val NOT_WIRED = "NOT_WIRED"

    // M06 文档路由表已规定、但原固定集合遗漏的错误码。
    const val NO_LAUNCH_INTENT = "NO_LAUNCH_INTENT"
    const val SETTINGS_FAILED = "SETTINGS_FAILED"
    const val DIAL_FAILED = "DIAL_FAILED"
    const val SMS_COMPOSE_FAILED = "SMS_COMPOSE_FAILED"

    // 具体权限失败码，由路由表要求的权限名决定；命中时同时视为 PERMISSION_REQUIRED 家族。
    const val CONTACT_PERMISSION_REQUIRED = "CONTACT_PERMISSION_REQUIRED"
    const val SMS_PERMISSION_REQUIRED = "SMS_PERMISSION_REQUIRED"

    // 入口闸门与协议层错误码（M01 远程闸门 / 协议版本闸门）。
    const val PROTOCOL_DECODE_FAILED = "PROTOCOL_DECODE_FAILED"
    const val UNSUPPORTED_PROTOCOL = "UNSUPPORTED_PROTOCOL"

    // 执行层错误码。
    const val INVALID_NUMBER = "INVALID_NUMBER"
    const val LAUNCH_FAILED = "LAUNCH_FAILED"
    const val SMS_SEND_FAILED = "SMS_SEND_FAILED"
    const val ACTION_REJECTED = "ACTION_REJECTED"
    const val UNLOCK_FAILED = "UNLOCK_FAILED"

    // M08 媒体专有码：语义与产生条件由 AssetStore.kt 的 MediaErrors 定义，
    // 这里登记为全局唯一错误码集合，供 camera.* / media.* 路由分支返回。
    // 两处必须逐字一致（契约脚本与 M06 文档会核对）。
    const val CAMERA_PERMISSION_REQUIRED = "CAMERA_PERMISSION_REQUIRED"
    const val PHOTO_PICKER_CANCELLED = "PHOTO_PICKER_CANCELLED"
    const val MEDIA_TOO_LARGE = "MEDIA_TOO_LARGE"
    const val MEDIA_TYPE_NOT_ALLOWED = "MEDIA_TYPE_NOT_ALLOWED"
    const val UPLOAD_RETRYABLE = "UPLOAD_RETRYABLE"
    const val UPLOAD_HASH_MISMATCH = "UPLOAD_HASH_MISMATCH"
    const val MEDIA_NOT_FOUND = "MEDIA_NOT_FOUND"

    /** 所有已声明的错误码。 */
    val ALL: Set<String> = setOf(
        UNKNOWN_COMMAND,
        INVALID_PAYLOAD,
        PACKAGE_NOT_ALLOWED,
        MODE_REJECTED,
        SIGNATURE_REQUIRED,
        REPLAY,
        EXPIRED,
        NO_ACCESSIBILITY,
        STALE_SNAPSHOT,
        SELECTOR_MISMATCH,
        FOREGROUND_PACKAGE_MISMATCH,
        PERMISSION_REQUIRED,
        NEEDS_UNLOCK,
        NOT_WIRED,
        NO_LAUNCH_INTENT,
        SETTINGS_FAILED,
        DIAL_FAILED,
        SMS_COMPOSE_FAILED,
        CONTACT_PERMISSION_REQUIRED,
        SMS_PERMISSION_REQUIRED,
        PROTOCOL_DECODE_FAILED,
        UNSUPPORTED_PROTOCOL,
        INVALID_NUMBER,
        LAUNCH_FAILED,
        SMS_SEND_FAILED,
        ACTION_REJECTED,
        UNLOCK_FAILED,
        CAMERA_PERMISSION_REQUIRED,
        PHOTO_PICKER_CANCELLED,
        MEDIA_TOO_LARGE,
        MEDIA_TYPE_NOT_ALLOWED,
        UPLOAD_RETRYABLE,
        UPLOAD_HASH_MISMATCH,
        MEDIA_NOT_FOUND,
    )

    /** 公开给 QQ 用户的失败说明种类；异常堆栈只进本地验收日志，不进入该字段。 */
    fun isDeclared(code: String): Boolean = code in ALL
}
