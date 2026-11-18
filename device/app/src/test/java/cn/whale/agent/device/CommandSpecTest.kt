package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M06 固定测试：命令声明与契约一致性。
 *
 * 断言设备端 [CommandSpecs] 与 [DeviceCommands.ALL] 完全相等，
 * 并与 `contracts/device-commands.schema.json` 的命令集合一致（schema 侧由
 * `tools/check-module-contracts.py` 在 CI 侧核对）。
 */
class CommandSpecTest {
    @Test fun declaredCommandsMatchCommandWhitelist() {
        assertEquals(DeviceCommands.ALL, CommandSpecs.DECLARED_COMMANDS)
    }

    @Test fun everyDeclaredCommandHasSpec() {
        for (command in DeviceCommands.ALL) {
            assertTrue("missing spec for $command", CommandSpecs.isDeclared(command))
        }
    }

    @Test fun unknownCommandHasNoSpec() {
        assertEquals(null, CommandSpecs.find("device.shell.exec"))
        assertEquals(null, CommandSpecs.find("device.raw_sql"))
        assertEquals(null, CommandSpecs.find("device.http.request"))
    }

    @Test fun forbiddenUniversalCommandsAreNotDeclared() {
        val forbidden = setOf(
            "device.eval", "device.execute_code", "device.raw_sql",
            "device.arbitrary_http", "device.shell", "device.adb",
        )
        for (command in forbidden) {
            assertTrue("forbidden command declared: $command", command !in DeviceCommands.ALL)
        }
    }

    @Test fun emptyPayloadCommandsDeclareNoFields() {
        val emptyPayloadCommands = setOf(
            "device.app.list",
            "device.ui.back",
            "device.ui.home",
            "device.ui.recents",
            "device.ui.notifications",
            "device.device_status",
            "device.screen.capture",
            "device.system.unlock",
        )
        for (command in emptyPayloadCommands) {
            val spec = CommandSpecs.find(command) ?: error("missing spec for $command")
            assertTrue("$command must be empty-payload", spec.emptyPayload)
            assertTrue("$command must declare no field", spec.fields.isEmpty())
        }
    }

    @Test fun sensitiveLockedCommandSetIsUnchanged() {
        // M06 产品文档固定能力清单，不允许遗漏。
        val required = setOf(
            "device.app.list", "device.app.launch", "device.ui.observe", "device.ui.tap",
            "device.ui.long_press", "device.ui.scroll", "device.ui.input", "device.ui.back",
            "device.ui.home", "device.ui.recents", "device.ui.notifications", "device.system.settings",
            "device.camera.capture", "device.media.pick", "device.media.upload", "device.media.share",
            "device.telephony.dial", "device.contacts.search", "device.sms.compose", "device.sms.send",
            "device.device_status",
            "device.system.unlock",
            "device.gallery.list", "device.gallery.read", "device.screen.capture",
        )
        assertEquals(required, DeviceCommands.ALL)
    }
}
