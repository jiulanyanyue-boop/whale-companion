package cn.whale.agent.device

import java.math.BigDecimal
import java.math.BigInteger

/**
 * M05 canonical JSON：与 host 侧 `security.canonical_payload` **字节级一致**的实现。
 *
 * host 侧唯一真相（`host/astrbot_whale_device/security.py`）：
 *
 * ```python
 * json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
 * ```
 *
 * 换成一句人话就是四条规则，少一条签名就永远验不过：
 *
 * 1. **排序键**：对象键按 UTF-16 码点升序（Python 用的是码点序，Kotlin 必须一致）；
 * 2. **无空格**：分隔符固定 `,` 与 `:`，不产出任何空白；
 * 3. **UTF-8 原样**：非 ASCII 字符**不**转成 `\uXXXX`（`ensure_ascii=False`）；
 * 4. **去掉 `signature`**：参与签名的永远是不含签名字段的那份。
 *
 * 转义规则逐条对齐 Python（实测确认，不是猜的）：
 *
 * | 字符 | Python 输出 |
 * |---|---|
 * | `"` / `\` | `\"` / `\\` |
 * | `\b` `\f` `\n` `\r` `\t` | 短形式（`\b` 而非 `\u0008`） |
 * | 其它 `< 0x20` 的控制字符 | `\uXXXX`（小写十六进制，如 `\u0001`） |
 * | `0x7f` (DEL) | **原样**输出（Python 不转义） |
 * | `/` | **原样**输出（不转义成 `\/`） |
 * | 非 ASCII | **原样**输出 |
 *
 * 之所以不用 `kotlinx.serialization` 的 `Json` 直接序列化：它的排序与转义策略都不是
 * Python 的，产物字节不会相同。签名校验只认字节，所以这里手写。
 */
object CanonicalJson {

    /** 不参与签名的字段名（host `canonical_payload` 会剔除它）。 */
    const val SIGNATURE_FIELD: String = "signature"

    /**
     * 把一棵 JSON 值树序列化成 canonical 文本。
     *
     * 支持的值类型：`null`、`Boolean`、整数（`Int/Long/BigInteger`）、
     * 浮点（`Double/Float/BigDecimal`）、`String`、`Map<*, *>`、`Iterable<*>`。
     * 其它类型（包括 Kotlin `data class`）一律拒绝——**绝不擅自反射成 JSON**，
     * 因为反射出来的键序与字段集合不受控。
     */
    fun encode(value: Any?): String {
        val out = StringBuilder(128)
        writeValue(out, value, depth = 0)
        return out.toString()
    }

    /**
     * 去掉 `signature` 后的 canonical 文本；这是**唯一**的签名输入。
     *
     * 只剔除顶层 `signature`（与 host 的 `{k: v for k, v in data.items() if k != "signature"}`
     * 完全一致：host 也只剔除顶层）。
     */
    fun encodeUnsigned(fields: Map<String, Any?>): String =
        encode(fields.filterKeys { it != SIGNATURE_FIELD })

    /** 便于日志与审计使用的字节视图（UTF-8）。 */
    fun encodeUnsignedBytes(fields: Map<String, Any?>): ByteArray =
        encodeUnsigned(fields).toByteArray(Charsets.UTF_8)

    /** 是否含有 `signature` 字段。 */
    fun hasSignature(fields: Map<String, Any?>): Boolean =
        fields.keys.any { it == SIGNATURE_FIELD }

    // ------------------------------------------------------------------
    // 写入器
    // ------------------------------------------------------------------

    private const val MAX_DEPTH = 64

    private fun writeValue(out: StringBuilder, value: Any?, depth: Int) {
        require(depth <= MAX_DEPTH) { "canonical json nesting is too deep" }
        when (value) {
            null -> out.append("null")
            is Boolean -> out.append(if (value) "true" else "false")
            is String -> writeString(out, value)
            is Int -> out.append(value.toString())
            is Long -> out.append(value.toString())
            is Short -> out.append(value.toString())
            is Byte -> out.append(value.toString())
            is BigInteger -> out.append(value.toString())
            is Float -> writeFloating(out, value.toDouble())
            is Double -> writeFloating(out, value)
            is BigDecimal -> writeFloating(out, value.toDouble())
            is Map<*, *> -> writeObject(out, value, depth)
            is Iterable<*> -> writeArray(out, value, depth)
            is Array<*> -> writeArray(out, value.asList(), depth)
            else -> throw IllegalArgumentException(
                "unsupported canonical json value type: ${value.javaClass.name}"
            )
        }
    }

    private fun writeObject(out: StringBuilder, value: Map<*, *>, depth: Int) {
        val keys = value.keys.map {
            it as? String
                ?: throw IllegalArgumentException("canonical json object keys must be strings")
        }.sorted()
        out.append('{')
        var first = true
        for (key in keys) {
            if (!first) out.append(',')
            first = false
            writeString(out, key)
            out.append(':')
            writeValue(out, value[key], depth + 1)
        }
        out.append('}')
    }

    private fun writeArray(out: StringBuilder, value: Iterable<*>, depth: Int) {
        out.append('[')
        var first = true
        for (item in value) {
            if (!first) out.append(',')
            first = false
            writeValue(out, item, depth + 1)
        }
        out.append(']')
    }

    /**
     * 浮点数按 Python `repr` 的风格输出最短可逆表示。
     *
     * 首版协议里的数值全是整数（`counter` / `deadline_ms` / `issued_at_ms`），
     * 浮点只为防御性完整性；非有限值直接拒绝（Python 默认也不产出 `NaN`）。
     */
    private fun writeFloating(out: StringBuilder, value: Double) {
        require(!value.isNaN() && !value.isInfinite()) {
            "canonical json refuses NaN and Infinity"
        }
        // 整数值的双精度按整数输出（Python: json.dumps(1.0) == "1.0"，这里保守用 long 形态）
        if (value == Math.floor(value) && Math.abs(value) < 1e15) {
            out.append(value.toLong().toString())
            return
        }
        out.append(value.toString())
    }

    /**
     * 字符串转义：逐条对齐 Python `json.dumps(..., ensure_ascii=False)`。
     *
     * 注意 `0x7f` 与 `/` **不转义**——这两处是最容易写错的地方。
     */
    private fun writeString(out: StringBuilder, value: String) {
        out.append('"')
        for (ch in value) {
            when (ch) {
                '"' -> out.append("\\\"")
                '\\' -> out.append("\\\\")
                '\n' -> out.append("\\n")
                '\r' -> out.append("\\r")
                '\t' -> out.append("\\t")
                '\b' -> out.append("\\b")
                '\u000C' -> out.append("\\f")
                else -> {
                    if (ch < ' ') {
                        // 其它控制字符 → \uXXXX，小写十六进制，四位定长
                        out.append("\\u")
                        out.append(String.format("%04x", ch.code))
                    } else {
                        // 0x20..0x7e 与所有非 ASCII 原样输出（含 0x7f 与 "/"）
                        out.append(ch)
                    }
                }
            }
        }
        out.append('"')
    }
}
