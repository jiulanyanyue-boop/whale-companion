package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 配对客户端与主机 `/pair` 端点的**互操作**验证（纯 JVM）。
 *
 * ## 这个文件守住什么
 *
 * 之前设备端第 4 步 `prepareRegistration` **只推进本地状态机、不发任何网络请求**，
 * 主机侧 `PairingService` 有完整能力却没有任何端点。结果「界面能走完六步，
 * 主机却不知道有设备来配对」。补上之后，风险点变成**两端格式对不上**：
 *
 * - 端点地址怎么从 `wss://host:8765/device` 推导出来（端口 +1 的约定）；
 * - 主机返回的扁平 JSON 能不能被正确解析（字段增删不该崩）；
 * - 拒绝路径（错码、用户拒绝、token 复用）会不会被误判成成功。
 *
 * 这里**不联网**：只验证推导逻辑与 JSON 解析（这两块是真正的易错点）。
 * 真实 HTTP 往返由 `host` 侧的 `pair_check.py` 与真机验证覆盖。
 */
class PairingClientTest {

    private val client = PairingClient(http = okhttp3.OkHttpClient())

    // -------------------------------------------------------------------
    // 端点地址推导
    // -------------------------------------------------------------------

    @Test
    fun `public HTTPS pairing keeps proxy prefix on port 443`() {
        assertEquals("https://host.example.com:443/whale/pair", client.pairingBaseUrl("wss://host.example.com:443/whale/device"))
    }

    @Test
    fun pairingBaseUrlDerivesPortPlusOne() {
        // 主机侧 `host_gui._on_start` 的约定：配对端口 = 设备端口 + 1。
        // 两端必须一致，改一处要同时改另一处。
        assertEquals(
            "https://192.168.1.10:8766/pair",
            client.pairingBaseUrl("wss://192.168.1.10:8765/device"),
        )
    }

    @Test
    fun pairingBaseUrlWorksForLoopback() {
        assertEquals(
            "http://127.0.0.1:8766/pair",
            client.pairingBaseUrl("ws://127.0.0.1:8765/device"),
        )
    }

    @Test
    fun pairingBaseUrlReturnsNullWithoutPort() {
        // 没有显式端口时无法推导 +1，必须如实返回 null（让调用方报失败），
        // 绝不瞎拼一个默认端口。
        assertNull(client.pairingBaseUrl("wss://192.168.1.10/device"))
    }

    @Test
    fun pairingBaseUrlRejectsNonWebSocketSchemes() {
        assertNull(client.pairingBaseUrl("http://192.168.1.10:8765/device"))
        assertNull(client.pairingBaseUrl("https://192.168.1.10:8765/device"))
    }

    @Test
    fun pairingBaseUrlRejectsBlankAndMalformed() {
        assertNull(client.pairingBaseUrl(null))
        assertNull(client.pairingBaseUrl("   "))
        assertNull(client.pairingBaseUrl("not a url"))
    }

    @Test
    fun pairingBaseUrlRejectsOutOfRangePort() {
        // 65535 + 1 会越界，必须拒绝而不是拼出一个非法端口。
        assertNull(client.pairingBaseUrl("wss://192.168.1.10:65535/device"))
    }

    // -------------------------------------------------------------------
    // 扁平 JSON 解析
    // -------------------------------------------------------------------

    @Test
    fun parseFlatJsonReadsSuccessPayload() {
        val raw =
            """{"ok": true, "device_id": "phone-01", "device_fingerprint": "${"a".repeat(64)}", """ +
                """"host_fingerprint": "${"b".repeat(64)}", "host_identity_is_placeholder": false}"""
        val fields = PairingClient.parseFlatJson(raw)
        assertEquals("true", fields["ok"])
        assertEquals("phone-01", fields["device_id"])
        assertEquals("a".repeat(64), fields["device_fingerprint"])
        assertEquals("b".repeat(64), fields["host_fingerprint"])
        assertEquals("false", fields["host_identity_is_placeholder"])
    }

    @Test
    fun parseFlatJsonReadsRejectionPayload() {
        val raw = """{"ok": false, "code": "SIGNATURE_REQUIRED", "message": "pairing code does not match"}"""
        val fields = PairingClient.parseFlatJson(raw)
        assertEquals("false", fields["ok"])
        assertEquals("SIGNATURE_REQUIRED", fields["code"])
        assertEquals("pairing code does not match", fields["message"])
    }

    @Test
    fun parseFlatJsonSurvivesExtraUnknownFields() {
        // 主机后续版本多回字段时，客户端**不能崩**（这是选逐字符扫描而非 data class 的理由）。
        val raw = """{"ok": true, "future_field": "whatever", "another": 42}"""
        val fields = PairingClient.parseFlatJson(raw)
        assertTrue(fields.containsKey("ok"))
        assertEquals("whatever", fields["future_field"])
        assertEquals("42", fields["another"])
    }

    @Test
    fun parseFlatJsonSkipsNestedObjectsWithoutCrashing() {
        val raw = """{"ok": true, "nested": {"a": 1}, "after": "x"}"""
        val fields = PairingClient.parseFlatJson(raw)
        assertEquals("true", fields["ok"])
        assertEquals("x", fields["after"])
    }

    @Test
    fun parseFlatJsonHandlesEscapedQuotes() {
        val raw = """{"message": "he said \"no\" firmly"}"""
        val fields = PairingClient.parseFlatJson(raw)
        assertEquals("he said \"no\" firmly", fields["message"])
    }

    @Test
    fun parseFlatJsonReturnsEmptyForNonObject() {
        assertEquals(emptyMap<String, String>(), PairingClient.parseFlatJson(""))
        assertEquals(emptyMap<String, String>(), PairingClient.parseFlatJson("[]"))
        assertEquals(emptyMap<String, String>(), PairingClient.parseFlatJson("plain text"))
    }

    @Test
    fun parseFlatJsonHandlesChineseValues() {
        // 主机侧回的是 `ensure_ascii=False` 的中文文案，必须原样读出。
        val raw = """{"ok": false, "message": "配对码已过期，请让主机重新生成"}"""
        val fields = PairingClient.parseFlatJson(raw)
        assertEquals("配对码已过期，请让主机重新生成", fields["message"])
    }

    // -------------------------------------------------------------------
    // 失败不伪装成功
    // -------------------------------------------------------------------

    @Test
    fun registerFailsLoudlyWhenEndpointCannotBeDerived() {
        // 地址推导不出来时必须立刻失败，而不是「看起来成功了」。
        val outcome = client.register(
            deviceUrl = "wss://192.168.1.10/device", // 没有端口
            token = "t",
            pairingCode = "C",
            deviceId = "d",
            publicKeyEncoded = "k",
            deviceFingerprint = "f",
            capabilitiesVersion = "1",
            connectionConfig = "c",
            displayName = "n",
        )
        assertTrue(outcome is PairingClient.Outcome.Failed)
        val failed = outcome as PairingClient.Outcome.Failed
        assertEquals(null, failed.code) // 本地原因，没有主机错误码
        assertTrue(failed.message.contains("配对端点"))
    }

    @Test
    fun confirmFailsLoudlyWhenEndpointCannotBeDerived() {
        val outcome = client.confirm(
            deviceUrl = "http://192.168.1.10:8765/device",
            token = "t",
            deviceId = "d",
            userAccepted = true,
            observedHostFingerprint = "f",
        )
        assertTrue(outcome is PairingClient.Outcome.Failed)
    }

    @Test
    fun hostInfoFailsLoudlyWhenEndpointCannotBeDerived() {
        val outcome = client.hostInfo("")
        assertTrue(outcome is PairingClient.Outcome.Failed)
    }
}