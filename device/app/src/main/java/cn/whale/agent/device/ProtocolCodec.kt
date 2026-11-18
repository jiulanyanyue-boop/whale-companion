package cn.whale.agent.device

import kotlinx.serialization.json.Json

object ProtocolCodec {
    private val json = Json {
        ignoreUnknownKeys = false
        encodeDefaults = true
        explicitNulls = false
    }

    fun encode(envelope: DeviceEnvelope): String = json.encodeToString(DeviceEnvelope.serializer(), envelope)

    fun decode(raw: String): DeviceEnvelope = json.decodeFromString(DeviceEnvelope.serializer(), raw)

    fun encode(response: DeviceResponse): String = json.encodeToString(DeviceResponse.serializer(), response)
}
