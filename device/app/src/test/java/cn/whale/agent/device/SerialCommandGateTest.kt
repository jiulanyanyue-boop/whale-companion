package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M06 固定测试：串行执行闸门。
 *
 * 同一设备只允许一个 active task；并发命令返回 `queued`，不能同时点击。
 */
class SerialCommandGateTest {
    @Test fun secondEntryIsRejectedWhileBusy() {
        val gate = SerialCommandGate()
        assertTrue(gate.tryEnter())
        assertTrue(gate.isBusy)
        assertFalse(gate.tryEnter())
        gate.release()
        assertFalse(gate.isBusy)
        assertTrue(gate.tryEnter())
        gate.release()
    }

    @Test fun concurrentRunReturnsNull() {
        val gate = SerialCommandGate()
        assertTrue(gate.run { "first" } == "first")
        assertTrue(gate.tryEnter())
        assertTrue(gate.run { "second" } == null)
        gate.release()
    }

    @Test fun blockedRunDoesNotExecuteBlock() {
        val gate = SerialCommandGate()
        assertTrue(gate.tryEnter())
        var executed = false
        gate.run { executed = true }
        assertFalse("block must not run while busy", executed)
        gate.release()
    }

    @Test fun releaseHappensEvenWhenBlockThrows() {
        val gate = SerialCommandGate()
        val thrown = runCatching {
            gate.run<Unit> { throw IllegalStateException("boom") }
        }
        assertTrue(thrown.isFailure)
        assertFalse("gate must be released on failure", gate.isBusy)
    }

    @Test fun queuedResponseKeepsRequestAndDeviceIds() {
        val envelope = DeviceEnvelope(
            request_id = "req-1",
            device_id = "device-1",
            command = "device.ui.tap",
            deadline_ms = 30000,
            idempotency_key = "k-1",
        )
        val response = SerialCommandGate.queued(envelope)
        assertEquals("queued", response.status)
        assertEquals("req-1", response.request_id)
        assertEquals("device-1", response.device_id)
        assertEquals("", response.error_code)
    }
}
