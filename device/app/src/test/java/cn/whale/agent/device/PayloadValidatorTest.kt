package cn.whale.agent.device

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** M06 固定测试：payload 最终校验闸门（设备端不信任主机已校验）。 */
class PayloadValidatorTest {
    private fun payload(vararg pairs: Pair<String, JsonPrimitive>): JsonObject =
        JsonObject(pairs.associate { it.first to it.second })

    private fun targetPayload(): JsonObject = payload(
        "snapshot_id" to JsonPrimitive("s1"),
        "target_id" to JsonPrimitive("t1"),
        "selector_hash" to JsonPrimitive("h1"),
        "expected_package" to JsonPrimitive("com.example.app"),
    )

    @Test fun emptyPayloadCommandAcceptsEmptyObject() {
        assertTrue(PayloadValidator.validate("device.app.list", payload()).ok)
        assertTrue(PayloadValidator.validate("device.device_status", payload()).ok)
        assertTrue(PayloadValidator.validate("device.ui.home", payload()).ok)
    }

    @Test fun emptyPayloadCommandRejectsAnyField() {
        // 禁止裸坐标作为默认动作。
        val result = PayloadValidator.validate("device.ui.home", payload("click_x" to JsonPrimitive(100)))
        assertFalse(result.ok)
        assertTrue(result.code == CommandErrors.INVALID_PAYLOAD)
    }

    @Test fun undeclaredCommandIsUnknownCommand() {
        val result = PayloadValidator.validate("device.shell.exec", payload())
        assertFalse(result.ok)
        assertTrue(result.code == CommandErrors.UNKNOWN_COMMAND)
    }

    @Test fun undeclaredFieldIsRejected() {
        val result = PayloadValidator.validate(
            "device.app.launch",
            payload("package_name" to JsonPrimitive("com.example.app"), "x" to JsonPrimitive(10), "y" to JsonPrimitive(20)),
        )
        assertFalse(result.ok)
        assertTrue(result.code == CommandErrors.INVALID_PAYLOAD)
    }

    @Test fun missingRequiredFieldIsRejected() {
        assertFalse(PayloadValidator.validate("device.app.launch", payload()).ok)
        assertFalse(PayloadValidator.validate("device.telephony.dial", payload()).ok)
        assertFalse(PayloadValidator.validate("device.sms.send", payload("number" to JsonPrimitive("10086"))).ok)
    }

    @Test fun packageNameLengthBoundsAreEnforced() {
        assertFalse(PayloadValidator.validate("device.app.launch", payload("package_name" to JsonPrimitive("ab"))).ok)
        assertTrue(
            PayloadValidator.validate("device.app.launch", payload("package_name" to JsonPrimitive("com.example.app"))).ok,
        )
        assertFalse(
            PayloadValidator.validate(
                "device.app.launch",
                payload("package_name" to JsonPrimitive("a".repeat(256))),
            ).ok,
        )
    }

    @Test fun longPressDurationRangeIsEnforced() {
        val target = targetPayload()
        assertFalse(
            PayloadValidator.validate(
                "device.ui.long_press",
                JsonObject(target + ("duration_ms" to JsonPrimitive(299))),
            ).ok,
        )
        assertTrue(
            PayloadValidator.validate(
                "device.ui.long_press",
                JsonObject(target + ("duration_ms" to JsonPrimitive(300))),
            ).ok,
        )
        assertTrue(
            PayloadValidator.validate(
                "device.ui.long_press",
                JsonObject(target + ("duration_ms" to JsonPrimitive(30000))),
            ).ok,
        )
        assertFalse(
            PayloadValidator.validate(
                "device.ui.long_press",
                JsonObject(target + ("duration_ms" to JsonPrimitive(30001))),
            ).ok,
        )
    }

    @Test fun scrollDirectionEnumIsEnforced() {
        val target = targetPayload()
        assertTrue(
            PayloadValidator.validate(
                "device.ui.scroll",
                JsonObject(target + ("direction" to JsonPrimitive("down"))),
            ).ok,
        )
        assertFalse(
            PayloadValidator.validate(
                "device.ui.scroll",
                JsonObject(target + ("direction" to JsonPrimitive("diagonal"))),
            ).ok,
        )
        assertFalse(PayloadValidator.validate("device.ui.scroll", target).ok)
    }

    @Test fun mediaPickRangeAndEnumAreEnforced() {
        assertTrue(
            PayloadValidator.validate(
                "device.media.pick",
                payload("media_type" to JsonPrimitive("image"), "max_items" to JsonPrimitive(1)),
            ).ok,
        )
        assertFalse(
            PayloadValidator.validate(
                "device.media.pick",
                payload("media_type" to JsonPrimitive("image"), "max_items" to JsonPrimitive(0)),
            ).ok,
        )
        assertFalse(
            PayloadValidator.validate(
                "device.media.pick",
                payload("media_type" to JsonPrimitive("image"), "max_items" to JsonPrimitive(21)),
            ).ok,
        )
        assertFalse(
            PayloadValidator.validate(
                "device.media.pick",
                payload("media_type" to JsonPrimitive("sticker"), "max_items" to JsonPrimitive(1)),
            ).ok,
        )
    }

    @Test fun uiObserveModeEnumIsEnforced() {
        assertTrue(PayloadValidator.validate("device.ui.observe", payload()).ok)
        assertTrue(PayloadValidator.validate("device.ui.observe", payload("mode" to JsonPrimitive("auto"))).ok)
        assertFalse(PayloadValidator.validate("device.ui.observe", payload("mode" to JsonPrimitive("screenshot"))).ok)
    }

    @Test fun integerFieldRejectsStringLiteral() {
        assertFalse(
            PayloadValidator.validate(
                "device.media.pick",
                payload("media_type" to JsonPrimitive("image"), "max_items" to JsonPrimitive("3")),
            ).ok,
        )
    }

    @Test fun verifiedIntegerValueIsReadable() {
        assertTrue(PayloadValidator.intValue(payload("duration_ms" to JsonPrimitive(500)), "duration_ms") == 500)
        assertTrue(PayloadValidator.intValue(payload("duration_ms" to JsonPrimitive("500")), "duration_ms") == null)
    }

    @Test fun stringValueIgnoresNonStringLiteral() {
        assertTrue(PayloadValidator.stringValue(payload("package_name" to JsonPrimitive(7)), "package_name") == "")
        assertTrue(
            PayloadValidator.stringValue(payload("package_name" to JsonPrimitive("com.example.app")), "package_name") ==
                "com.example.app",
        )
    }
}
