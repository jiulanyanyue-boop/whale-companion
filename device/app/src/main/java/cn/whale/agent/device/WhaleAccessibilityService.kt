package cn.whale.agent.device

import android.accessibilityservice.AccessibilityService
import android.content.Intent
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import java.util.concurrent.ConcurrentHashMap

/**
 * M07 无障碍服务。
 *
 * 规范来源：`docs/modules/M07-ui-accessibility-development.md`
 *
 * 职责最小化：只维护服务连接状态，供 [AccessibilityController] 判断「无障碍是否可用」。
 * 语义树读取、动作执行、快照缓存都在 [AccessibilityController] 与 [UiSnapshotStore]。
 *
 * 安全约束：服务解绑必须立即让任务停止——所以 [instance] 在 [onUnbind] / [onDestroy]
 * 被清空后，[AccessibilityController] 的所有动作都会返回 `NO_ACCESSIBILITY`，不会「再多点几下」。
 */
class WhaleAccessibilityService : AccessibilityService() {
    private val scrollPositions = ConcurrentHashMap<String, Pair<Int, Int>>()

    fun scrollPosition(node: AccessibilityNodeInfo): Pair<Int, Int>? =
        node.viewIdResourceName?.let { scrollPositions["${node.windowId}:$it"] }

    companion object {
        @Volatile
        var instance: WhaleAccessibilityService? = null
            private set
    }

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        UnlockAccessibilityBridge.attach(this)
    }

    /**
     * 无障碍服务解绑（系统关闭或被用户撤销权限）时，清空单例，
     * 让所有后续动作立即失败，而不是继续向已失效的窗口派发手势。
     */
    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event?.eventType != AccessibilityEvent.TYPE_VIEW_SCROLLED) return
        val source = event.source ?: return
        val viewId = source.viewIdResourceName ?: return
        if (scrollPositions.size > 64) scrollPositions.clear()
        scrollPositions["${source.windowId}:$viewId"] = event.scrollX to event.scrollY
    }

    // Android interrupts feedback without disconnecting the service.
    // https://developer.android.com/reference/android/accessibilityservice/AccessibilityService#onInterrupt()
    override fun onInterrupt() = Unit

    override fun onUnbind(intent: Intent?): Boolean {
        UnlockAccessibilityBridge.detach(this)
        if (instance === this) instance = null
        return super.onUnbind(intent)
    }

    override fun onDestroy() {
        UnlockAccessibilityBridge.detach(this)
        if (instance === this) instance = null
        super.onDestroy()
    }
}
