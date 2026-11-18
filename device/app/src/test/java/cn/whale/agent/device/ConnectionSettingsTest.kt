package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 设备端远程连接配置的纯 JVM 单测（不依赖 Android 运行时，可直接在 `verify-kotlin.ps1` 里真跑）。
 *
 * 覆盖点：
 * 1. `wss_url` 合法/非法（`wss://`、`ws://` 明文警告、非 ws 方案、空值、缺主机、畸形 URL）；
 * 2. `device_id` 规则 `[A-Za-z0-9._:-]{3,80}`（含长度边界与非法字符）；
 * 3. 整体校验：任一失败即拒绝，两项都过才接受；
 * 4. 状态推导：未配置 / 服务未运行 / 服务在跑无痕迹 / 服务留下 connected 痕迹；
 * 5. 键名常量必须与设备端读取点一致（防以后有人另造键名）。
 */
class ConnectionSettingsTest {

    // ---------- 键名契约 ----------

    @Test
    fun preferenceNameMatchesServiceReader() {
        // DeviceConnectionService.connectIfConfigured() 读的就是这个 prefs 文件名。
        assertEquals("device_connection", ConnectionSettings.PREFERENCES_NAME)
    }

    @Test
    fun urlKeyMatchesServiceReader() {
        assertEquals("wss_url", ConnectionSettings.KEY_WSS_URL)
    }

    @Test
    fun deviceIdKeyMatchesActivityReader() {
        assertEquals("device_id", ConnectionSettings.KEY_DEVICE_ID)
    }

    // ---------- wss_url 校验 ----------

    @Test
    fun validWssUrlIsAcceptedAndNotInsecure() {
        val check = ConnectionSettings.validateUrl("wss://host.example.com/device")
        assertTrue(check is ConnectionSettings.UrlCheck.Valid)
        val valid = check as ConnectionSettings.UrlCheck.Valid
        assertEquals("wss://host.example.com/device", valid.normalizedUrl)
        assertFalse(valid.insecure)
    }

    @Test
    fun plainWsUrlIsAcceptedButFlaggedInsecure() {
        val check = ConnectionSettings.validateUrl("ws://10.0.2.2:8787")
        assertTrue(check is ConnectionSettings.UrlCheck.Valid)
        assertTrue((check as ConnectionSettings.UrlCheck.Valid).insecure)
    }

    @Test
    fun uppercaseSchemeIsAccepted() {
        val check = ConnectionSettings.validateUrl("WSS://Host.Example.com/x")
        assertTrue(check is ConnectionSettings.UrlCheck.Valid)
        assertFalse((check as ConnectionSettings.UrlCheck.Valid).insecure)
    }

    @Test
    fun surroundingWhitespaceIsTrimmedBeforeSaving() {
        val check = ConnectionSettings.validateUrl("  wss://host.example.com  ")
        assertTrue(check is ConnectionSettings.UrlCheck.Valid)
        assertEquals("wss://host.example.com", (check as ConnectionSettings.UrlCheck.Valid).normalizedUrl)
    }

    @Test
    fun emptyUrlIsRejected() {
        for (blank in listOf("", "   ", null)) {
            val check = ConnectionSettings.validateUrl(blank)
            assertTrue(check is ConnectionSettings.UrlCheck.Invalid)
            assertEquals(
                ConnectionSettings.ERROR_URL_EMPTY,
                (check as ConnectionSettings.UrlCheck.Invalid).message,
            )
        }
    }

    @Test
    fun nonWebSocketSchemeIsRejected() {
        for (bad in listOf("https://host.example.com", "http://host.example.com", "host.example.com")) {
            val check = ConnectionSettings.validateUrl(bad)
            assertTrue(check is ConnectionSettings.UrlCheck.Invalid)
            assertEquals(
                ConnectionSettings.ERROR_URL_SCHEME,
                (check as ConnectionSettings.UrlCheck.Invalid).message,
            )
        }
    }

    @Test
    fun schemeWithoutHostIsRejected() {
        for (bad in listOf("ws://", "wss://", "ws://:8787")) {
            val check = ConnectionSettings.validateUrl(bad)
            assertTrue(check is ConnectionSettings.UrlCheck.Invalid)
            assertEquals(
                ConnectionSettings.ERROR_URL_NO_HOST,
                (check as ConnectionSettings.UrlCheck.Invalid).message,
            )
        }
    }

    @Test
    fun malformedUrlIsRejectedWithMessage() {
        // 含空格 → URI 解析失败，不能静默保存。
        val check = ConnectionSettings.validateUrl("wss://host example.com/reply")
        assertTrue(check is ConnectionSettings.UrlCheck.Invalid)
        val message = (check as ConnectionSettings.UrlCheck.Invalid).message
        assertTrue(message.startsWith("地址不是合法 URL"))
    }

    // ---------- device_id 校验 ----------

    @Test
    fun deviceIdAcceptsAllowedCharacterSet() {
        for (ok in listOf("abc", "dev-01", "dev_01", "dev.01", "dev:01", "A".repeat(80), "a1B2.c-3_4:5")) {
            val check = ConnectionSettings.validateDeviceId(ok)
            assertTrue(ok, check is ConnectionSettings.DeviceIdCheck.Valid)
            assertEquals(ok, (check as ConnectionSettings.DeviceIdCheck.Valid).normalizedId)
        }
    }

    @Test
    fun deviceIdRejectsTooShortOrTooLong() {
        val short = ConnectionSettings.validateDeviceId("ab")
        assertTrue(short is ConnectionSettings.DeviceIdCheck.Invalid)
        assertEquals(
            ConnectionSettings.ERROR_DEVICE_ID_SHAPE,
            (short as ConnectionSettings.DeviceIdCheck.Invalid).message,
        )
        val long = ConnectionSettings.validateDeviceId("a".repeat(81))
        assertTrue(long is ConnectionSettings.DeviceIdCheck.Invalid)
        assertEquals(
            ConnectionSettings.ERROR_DEVICE_ID_SHAPE,
            (long as ConnectionSettings.DeviceIdCheck.Invalid).message,
        )
    }

    @Test
    fun deviceIdRejectsIllegalCharacters() {
        for (bad in listOf("dev 01", "dev/01", "设备01", "dev@01", "dev#1", "dev\n01")) {
            val check = ConnectionSettings.validateDeviceId(bad)
            assertTrue(bad, check is ConnectionSettings.DeviceIdCheck.Invalid)
        }
    }

    @Test
    fun emptyDeviceIdIsRejected() {
        for (blank in listOf("", "  ", null)) {
            val check = ConnectionSettings.validateDeviceId(blank)
            assertTrue(check is ConnectionSettings.DeviceIdCheck.Invalid)
            assertEquals(
                ConnectionSettings.ERROR_DEVICE_ID_EMPTY,
                (check as ConnectionSettings.DeviceIdCheck.Invalid).message,
            )
        }
    }

    @Test
    fun deviceIdIsTrimmed() {
        val check = ConnectionSettings.validateDeviceId("  dev-01  ")
        assertTrue(check is ConnectionSettings.DeviceIdCheck.Valid)
        assertEquals("dev-01", (check as ConnectionSettings.DeviceIdCheck.Valid).normalizedId)
    }

    // ---------- 整体校验 ----------

    @Test
    fun bothValidIsAccepted() {
        val result = ConnectionSettings.validate("ws://10.0.2.2:8787", "dev-01")
        assertTrue(result is ConnectionSettings.ValidationResult.Accepted)
        val accepted = result as ConnectionSettings.ValidationResult.Accepted
        assertEquals("ws://10.0.2.2:8787", accepted.wssUrl)
        assertEquals("dev-01", accepted.deviceId)
        assertTrue(accepted.insecureTransport)
    }

    @Test
    fun emptyUrlRejectsWholeSaving() {
        // 第 7 条硬性要求：没有地址绝不启动服务，所以整体校验必须先拒绝。
        val result = ConnectionSettings.validate("", "dev-01")
        assertTrue(result is ConnectionSettings.ValidationResult.Rejected)
        assertEquals(
            listOf(ConnectionSettings.ERROR_URL_EMPTY),
            (result as ConnectionSettings.ValidationResult.Rejected).errors,
        )
    }

    @Test
    fun bothInvalidCollectsBothMessages() {
        val result = ConnectionSettings.validate("http://x", "")
        assertTrue(result is ConnectionSettings.ValidationResult.Rejected)
        assertEquals(
            listOf(ConnectionSettings.ERROR_URL_SCHEME, ConnectionSettings.ERROR_DEVICE_ID_EMPTY),
            (result as ConnectionSettings.ValidationResult.Rejected).errors,
        )
    }

    @Test
    fun insecureWarningTextMentionsWss() {
        // 明文 ws:// 必须显式警告，文案里要出现 wss:// 作为正确做法。
        assertTrue(ConnectionSettings.WARNING_INSECURE_TRANSPORT.contains("ws://"))
        assertTrue(ConnectionSettings.WARNING_INSECURE_TRANSPORT.contains("wss://"))
    }

    // ---------- 状态推导（不许假在线） ----------

    @Test
    fun noUrlMeansNotConfiguredRegardlessOfTrace() {
        assertEquals(
            ConnectionSettings.ConnectionState.NOT_CONFIGURED,
            ConnectionSettings.resolveState("", serviceRunning = true, statusTrace = ConnectionSettings.TRACE_CONNECTED),
        )
        assertEquals(
            ConnectionSettings.ConnectionState.NOT_CONFIGURED,
            ConnectionSettings.resolveState(null, serviceRunning = false, statusTrace = null),
        )
    }

    @Test
    fun configuredButServiceStoppedIsNeverConnected() {
        // 服务没在跑时，即使痕迹里残留 connected 也不能显示已连接。
        assertEquals(
            ConnectionSettings.ConnectionState.CONFIGURED_SERVICE_STOPPED,
            ConnectionSettings.resolveState(
                "wss://host.example.com",
                serviceRunning = false,
                statusTrace = ConnectionSettings.TRACE_CONNECTED,
            ),
        )
    }

    @Test
    fun serviceRunningWithoutConnectedTraceStaysUnconfirmed() {
        for (trace in listOf(null, "", ConnectionSettings.TRACE_DISCONNECTED, "weird-value")) {
            assertEquals(
                ConnectionSettings.ConnectionState.CONFIGURED_SERVICE_RUNNING,
                ConnectionSettings.resolveState("wss://host.example.com", true, trace),
            )
        }
    }

    @Test
    fun connectedRequiresServiceRunningAndConnectedTrace() {
        assertEquals(
            ConnectionSettings.ConnectionState.CONNECTED,
            ConnectionSettings.resolveState(
                "wss://host.example.com",
                serviceRunning = true,
                statusTrace = "  CONNECTED  ",
            ),
        )
    }

    @Test
    fun deviceIdPatternMatchesSchemaRule() {
        assertEquals("[A-Za-z0-9._:-]{3,80}", ConnectionSettings.DEVICE_ID_PATTERN)
        val regex = Regex(ConnectionSettings.DEVICE_ID_PATTERN)
        assertTrue(regex.matches("dev-01"))
        assertFalse(regex.matches("de"))
        assertFalse(regex.matches("dev/01"))
    }
}
