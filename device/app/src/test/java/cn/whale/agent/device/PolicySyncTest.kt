package cn.whale.agent.device

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class PolicySyncTest {
    private fun payload(allowed: String, blocked: String = "[]"): JsonObject = Json.parseToJsonElement(
        "{\"allowed_packages\":$allowed,\"blocked_packages\":$blocked}",
    ) as JsonObject

    @Test
    fun acceptsPackageArraysAndReturnsSets() {
        val policy = PolicySync.parse(1, payload("[\"com.example.app\"]", "[\"com.other.app\"]"))
        assertEquals(setOf("com.example.app"), policy?.allowedPackages)
        assertEquals(setOf("com.other.app"), policy?.blockedPackages)
    }

    @Test
    fun rejectsWrongVersionExtraKeysInvalidNamesAndOverlap() {
        assertNull(PolicySync.parse(2, payload("[]")))
        assertNull(PolicySync.parse(1, Json.parseToJsonElement("{\"allowed_packages\":[],\"blocked_packages\":[],\"extra\":true}") as JsonObject))
        assertNull(PolicySync.parse(1, payload("[1]")))
        assertNull(PolicySync.parse(1, payload("[\"bad\"]")))
        assertNull(PolicySync.parse(1, payload("[\"com.example.app\"]", "[\"com.example.app\"]")))
    }

    @Test
    fun rejectsMoreThanFiveHundredEntries() {
        val entries = (1..501).joinToString(",") { "\"com.example.app$it\"" }
        assertNull(PolicySync.parse(1, payload("[$entries]")))
    }

    @Test
    fun acceptsClearingPolicyAndRejectsOversizedPackageNames() {
        assertEquals(emptySet<String>(), PolicySync.parse(1, payload("[]"))?.allowedPackages)
        val oversized = "com." + "x".repeat(252)
        assertNull(PolicySync.parse(1, payload("[\"$oversized\"]")))
    }
}
