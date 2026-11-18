package cn.whale.agent.device

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * M06 payload 最终校验闸门。
 *
 * 设备端校验是最终闸门，不能信任主机已经校验过（M06 开发文档「payload 校验」）：
 * - 命令必须在 [DeviceCommands.ALL]；
 * - payload 必须是 object（由 `DeviceEnvelope.payload: JsonObject` 在解码期保证）；
 * - 字段名必须在 [CommandSpecs]（等价于 `contracts/device-commands.schema.json`）；
 * - 字段类型、长度、枚举和范围必须匹配；
 * - `app.launch` 的包名是否通过本地白名单在 [CommandRouter] 里单独检查。
 */
data class PayloadValidation(
    val ok: Boolean,
    val code: String = "",
    val message: String = "",
) {
    companion object {
        val OK = PayloadValidation(true)
    }
}

object PayloadValidator {
    fun validate(command: String, payload: JsonObject): PayloadValidation {
        val spec = CommandSpecs.find(command)
            ?: return reject(CommandErrors.UNKNOWN_COMMAND, "command is not declared")
        if (spec.emptyPayload && payload.isNotEmpty()) {
            return reject(CommandErrors.INVALID_PAYLOAD, "payload must be an empty object")
        }
        for (key in payload.keys) {
            if (key !in spec.declaredFieldNames) {
                return reject(CommandErrors.INVALID_PAYLOAD, "payload field is not declared: $key")
            }
        }
        for (field in spec.fields) {
            val element = payload[field.name]
            if (element == null || element is JsonNull) {
                if (field.required) {
                    return reject(CommandErrors.INVALID_PAYLOAD, "payload field is required: ${field.name}")
                }
                continue
            }
            checkField(field, element)?.let { return reject(CommandErrors.INVALID_PAYLOAD, it) }
        }
        return PayloadValidation.OK
    }

    /** 已通过 [validate] 的 payload 读取字符串字段；非字符串或缺省时回退 [default]。 */
    fun stringValue(payload: JsonObject, key: String, default: String = ""): String {
        val primitive = payload[key] as? JsonPrimitive ?: return default
        if (!primitive.isString) return default
        return primitive.content.ifBlank { default }
    }

    /** 已通过 [validate] 的 payload 读取整数字段；不是整数字面量时返回 null。 */
    fun intValue(payload: JsonObject, key: String): Int? {
        val primitive = payload[key] as? JsonPrimitive ?: return null
        if (primitive.isString) return null
        return primitive.content.toIntOrNull()
    }

    private fun checkField(field: PayloadFieldSpec, element: JsonElement): String? = when (field.type) {
        PayloadFieldType.STRING -> {
            val primitive = element as? JsonPrimitive
            when {
                primitive == null -> "payload field must be a string: ${field.name}"
                !primitive.isString -> "payload field must be a string: ${field.name}"
                else -> lengthFailure(field, primitive.content)
            }
        }
        PayloadFieldType.INTEGER -> {
            val primitive = element as? JsonPrimitive
            when {
                primitive == null || primitive.isString -> "payload field must be an integer: ${field.name}"
                else -> rangeFailure(field, primitive.content.toLongOrNull())
            }
        }
        PayloadFieldType.OBJECT -> {
            if (element is JsonObject) null else "payload field must be an object: ${field.name}"
        }
    }

    private fun lengthFailure(field: PayloadFieldSpec, value: String): String? {
        field.minLength?.let { min ->
            if (value.length < min) return "payload field is shorter than $min: ${field.name}"
        }
        field.maxLength?.let { max ->
            if (value.length > max) return "payload field is longer than $max: ${field.name}"
        }
        field.allowedValues?.let { allowed ->
            if (value !in allowed) return "payload field is not in the declared enum: ${field.name}"
        }
        return null
    }

    private fun rangeFailure(field: PayloadFieldSpec, value: Long?): String? {
        if (value == null) return "payload field is not a valid integer: ${field.name}"
        field.minValue?.let { min ->
            if (value < min) return "payload field is below $min: ${field.name}"
        }
        field.maxValue?.let { max ->
            if (value > max) return "payload field is above $max: ${field.name}"
        }
        return null
    }

    private fun reject(code: String, message: String) = PayloadValidation(false, code, message)
}
