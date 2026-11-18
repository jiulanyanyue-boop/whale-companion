package cn.whale.agent.device

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

data class SyncedDevicePolicy(
    val allowedPackages: Set<String>,
    val blockedPackages: Set<String>,
)

object PolicySync {
    private val packageName = Regex("^[A-Za-z_][A-Za-z0-9_]*(\\.[A-Za-z_][A-Za-z0-9_]*)+$")
    private val expectedKeys = setOf("allowed_packages", "blocked_packages")

    fun parse(protocolVersion: Int, payload: JsonObject): SyncedDevicePolicy? {
        if (protocolVersion != 1 || payload.keys != expectedKeys) return null

        fun readPackages(key: String): Set<String>? {
            val values = payload[key] as? JsonArray ?: return null
            if (values.size > 500) return null
            val packages = values.map { value ->
                val primitive = value as? JsonPrimitive ?: return null
                if (!primitive.isString) return null
                val name = primitive.content
                if (name.length !in 3..255 || !packageName.matches(name)) return null
                name
            }
            return packages.toSet()
        }

        val allowed = readPackages("allowed_packages") ?: return null
        val blocked = readPackages("blocked_packages") ?: return null
        if (allowed.intersect(blocked).isNotEmpty()) return null
        return SyncedDevicePolicy(allowed, blocked)
    }
}
