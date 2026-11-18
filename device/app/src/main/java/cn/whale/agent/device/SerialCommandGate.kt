package cn.whale.agent.device

import java.util.concurrent.atomic.AtomicBoolean

/**
 * M06 串行执行闸门。
 *
 * 开发文档「串行执行」：同一设备只允许一个 active task；并发命令返回 `queued`，不能同时点击。
 * 每个命令完成后必须重新观察/确认再执行下一条。
 *
 * 纯 JVM 逻辑，不依赖 Android，便于固定测试；由 [CommandRouter] 在协议无关层使用。
 */
class SerialCommandGate {
    private val busy = AtomicBoolean(false)

    val isBusy: Boolean get() = busy.get()

    /**
     * 尝试进入执行区间。
     *
     * @return true 表示取得执行权，调用方必须随后 [release]；false 表示已有活动任务。
     */
    fun tryEnter(): Boolean = busy.compareAndSet(false, true)

    fun release() {
        busy.set(false)
    }

    /**
     * 在串行区间内执行 [block]。未取得执行权时不执行 block，返回 `queued` 结果。
     *
     * 结果必须仍带 `request_id` 与 `device_id`（M06 产品文档「结果」）。
     */
    fun <T> run(block: () -> T): T? {
        if (!tryEnter()) return null
        return try {
            block()
        } finally {
            release()
        }
    }

    companion object {
        /** 并发被拒绝时统一返回 `queued`，不能伪造 `ok`。 */
        fun queued(envelope: DeviceEnvelope): DeviceResponse = DeviceResponse(
            request_id = envelope.request_id,
            device_id = envelope.device_id,
            status = "queued",
            error_code = "",
            message = "another task is active on this device; retry after it finishes",
        )
    }
}
