package cn.whale.agent.device

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject

@Serializable
data class DeviceEnvelope(
    val protocol_version: Int = 1,
    val request_id: String,
    val device_id: String,
    val command: String,
    val deadline_ms: Int,
    val idempotency_key: String,
    val payload: JsonObject = buildJsonObject { },
    val counter: Long = 0,
    val issued_at_ms: Long = 0,
    val signature: String = "",
)

@Serializable
data class DeviceResponse(
    val request_id: String,
    val device_id: String,
    val status: String,
    val error_code: String = "",
    val message: String = "",
    val artifacts: List<Map<String, String>> = emptyList(),
    val evidence: Map<String, String> = emptyMap(),
)

object DeviceCommands {
    val ALL: Set<String> = setOf(
        "device.app.list", "device.app.launch", "device.ui.observe", "device.ui.tap",
        "device.ui.long_press", "device.ui.scroll", "device.ui.input", "device.ui.back",
        "device.ui.home", "device.ui.recents", "device.ui.notifications", "device.system.settings",
        "device.camera.capture", "device.media.pick", "device.media.upload", "device.media.share",
        "device.gallery.list", "device.gallery.read", "device.screen.capture",
        "device.telephony.dial", "device.contacts.search", "device.sms.compose", "device.sms.send",
        "device.device_status",
        "device.system.unlock",
    )
}
