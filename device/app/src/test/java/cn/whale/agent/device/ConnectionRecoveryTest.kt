package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Test

class ConnectionRecoveryTest {
    @Test fun staleConnectedTraceIsNotConfigurationProof() {
        assertEquals(
            ConnectionSettings.ConnectionState.CONFIGURED_SERVICE_STOPPED,
            ConnectionSettings.resolveState("wss://host.example/device", false, ConnectionSettings.TRACE_CONNECTED),
        )
    }

    @Test fun configuredRunningServiceWithoutFreshTraceStaysUnconfirmed() {
        assertEquals(
            ConnectionSettings.ConnectionState.CONFIGURED_SERVICE_RUNNING,
            ConnectionSettings.resolveState("wss://host.example/device", true, ConnectionSettings.TRACE_DISCONNECTED),
        )
    }
}
