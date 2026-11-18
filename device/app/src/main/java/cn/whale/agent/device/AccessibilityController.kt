package cn.whale.agent.device

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.graphics.Rect
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.accessibility.AccessibilityNodeInfo
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/**
 * M07 无障碍执行层。
 *
 * 规范来源：`docs/modules/M07-ui-accessibility-development.md`
 *
 * 职责划分：
 * - 快照缓存、selector_hash / target_id 计算、四项目标校验全部在 [UiSnapshotStore]（纯逻辑，可 JVM 单测）。
 * - 本类只负责与 Android 无障碍框架交互：读取语义树、performAction、受限手势 fallback、动作后重新观察。
 *
 * 固定动作顺序（不得调换）：
 * 1. `performAction`；
 * 2. 点击时尝试可点击祖先节点；
 * 3. 只有目标有有效 bounds 才允许 Gesture fallback；
 * 4. 长按固定 300..30000 ms；
 * 5. 滚动优先节点支持的对应方向动作，失败才使用节点边界内的手势；
 * 6. 动作后必须重新 observe，不能只返回「已点击」。
 *
 * 禁止项：把 node index 当稳定 ID；默认每步截图；对 FLAG_SECURE 页面截图；
 * 向模型发送密码/验证码字段或完整剪贴板。
 */
class AccessibilityController {
    /** 每进程一份快照存储；文档要求最多保留 8 个快照，淘汰与指纹计算都在 [UiSnapshotStore] 内。 */
    private val store: UiSnapshotStore = UiSnapshotStore()

    /** observe 时同一份快照里的 Android 节点句柄，用于 performAction；不进 evidence、不发给模型。 */
    private val nodeHandles = ConcurrentHashMap<String, Map<String, AccessibilityNodeInfo>>()

    /** 确认卡/停止按钮的受保护区域；注入期间不允许被自动手势命中。 */
    val protectedRegions = ProtectedRegionGuard()

    fun observe(mode: String): DeviceResponse {
        val service = WhaleAccessibilityService.instance
            ?: return DeviceResponse("", "", "failed", CommandErrors.NO_ACCESSIBILITY, "accessibility service is not connected")
        val root = service.rootInActiveWindow
            // 没有活动窗口不是「未接入」，而是当前确实没有可观察的窗口；沿用已登记的执行层失败码。
            ?: return DeviceResponse("", "", "failed", CommandErrors.ACTION_REJECTED, "no active accessibility window")

        val packageName = root.packageName?.toString().orEmpty()
        val windowId = root.windowId

        val handles = LinkedHashMap<String, AccessibilityNodeInfo>()
        val refs = ArrayList<UiNodeRef>()
        walk(root, "0", handles, refs, 0)

        // 视觉 fallback 的准入判定交给调用方（CommandRouter），本层只报告树的规模事实。
        val snapshot = store.save(packageName, windowId, refs)
        nodeHandles[snapshot.snapshot_id] = handles

        // 只回传节点描述，不回传 NodeInfo 句柄。
        return DeviceResponse(
            request_id = "",
            device_id = "",
            status = "ok",
            evidence = snapshot.evidence() + ("requested_mode" to mode) + pageEvidence(
                refs,
                handles.mapNotNull { (id, handle) -> service.scrollPosition(handle)?.let { id to it } }.toMap(),
            ),
        )
    }

    fun tap(envelope: DeviceEnvelope): DeviceResponse = performTarget(envelope, "tap", ::tapNode)

    fun longPress(envelope: DeviceEnvelope): DeviceResponse =
        performTarget(envelope, "long_press", ::longPressNode)

    fun scroll(envelope: DeviceEnvelope): DeviceResponse = performTarget(envelope, "scroll", ::scrollNode)

    fun input(envelope: DeviceEnvelope): DeviceResponse = performTarget(envelope, "input", ::inputNode)

    fun global(envelope: DeviceEnvelope, action: Int): DeviceResponse {
        val service = WhaleAccessibilityService.instance
            ?: return denied(envelope, CommandErrors.NO_ACCESSIBILITY, "accessibility service is not connected")
        return if (service.performGlobalAction(action)) {
            ok(envelope, mapOf("action" to action.toString()))
        } else {
            denied(envelope, CommandErrors.ACTION_REJECTED, "system action was rejected")
        }
    }

    // ---------- 动作实现 ----------

    private fun tapNode(service: AccessibilityService, node: AccessibilityNodeInfo, payload: DeviceEnvelope): Boolean {
        // 顺序固定：先对目标 performAction，失败再尝试可点击祖先，最后才允许受限手势。
        if (node.isClickable && node.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return true
        ancestorClickable(node)?.let { if (it.performAction(AccessibilityNodeInfo.ACTION_CLICK)) return true }
        val rect = Rect()
        node.getBoundsInScreen(rect)
        return gestureTap(service, rect)
    }

    private fun longPressNode(
        service: AccessibilityService,
        node: AccessibilityNodeInfo,
        payload: DeviceEnvelope,
    ): Boolean {
        val duration = PayloadValidator.intValue(payload.payload, "duration_ms")?.coerceIn(MIN_LONG_PRESS_MS, MAX_LONG_PRESS_MS)
            ?: return false
        if (node.isClickable && performLongClick(node)) return true
        val rect = Rect()
        node.getBoundsInScreen(rect)
        return gestureLongPress(service, rect, duration)
    }

    private fun scrollNode(
        service: AccessibilityService,
        node: AccessibilityNodeInfo,
        payload: DeviceEnvelope,
    ): Boolean {
        val direction = PayloadValidator.stringValue(payload.payload, "direction")
        val action = when (direction) {
            "up" -> AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
            "down" -> AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
            "left" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_RIGHT.id
            "right" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_LEFT.id
            else -> return false
        }
        // Prefer a real directional action when the node advertises it.
        // Generic FORWARD can scroll vertically on a horizontal launcher.
        if (node.isScrollable && node.actionList.any { it.id == action } && node.performAction(action)) return true
        scrollableAncestor(node)?.let { if (it.actionList.any { advertised -> advertised.id == action } && it.performAction(action)) return true }

        val rect = Rect()
        node.getBoundsInScreen(rect)
        if (!valid(rect)) return false
        val amount = PayloadValidator.intValue(payload.payload, "amount")?.coerceIn(1, MAX_SCROLL_AMOUNT) ?: DEFAULT_SCROLL_AMOUNT
        return gestureScroll(service, rect, direction, amount)
    }

    private fun inputNode(
        service: AccessibilityService,
        node: AccessibilityNodeInfo,
        payload: DeviceEnvelope,
    ): Boolean {
        if (!node.isEditable) return false
        val text = PayloadValidator.stringValue(payload.payload, "text")
        val args = Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        }
        return node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args)
    }

    // ---------- 目标校验 + 动作后重观察 ----------

    private fun performTarget(
        envelope: DeviceEnvelope,
        operation: String,
        action: (AccessibilityService, AccessibilityNodeInfo, DeviceEnvelope) -> Boolean,
    ): DeviceResponse {
        val service = WhaleAccessibilityService.instance
            ?: return denied(envelope, CommandErrors.NO_ACCESSIBILITY, "accessibility service is not connected")

        val snapshotId = PayloadValidator.stringValue(envelope.payload, "snapshot_id")
        val targetId = PayloadValidator.stringValue(envelope.payload, "target_id")
        val expectedPackage = PayloadValidator.stringValue(envelope.payload, "expected_package")
        val expectedSelectorHash = PayloadValidator.stringValue(envelope.payload, "selector_hash")

        // 屏幕熄灭或锁屏时立即停止任务，不尝试任何动作。
        if (!serviceActive(service)) {
            return denied(envelope, CommandErrors.NEEDS_UNLOCK, "screen is off or locked; task stopped")
        }

        val currentPackage = service.rootInActiveWindow?.packageName?.toString().orEmpty()

        // 安全防注入：确认卡/停止按钮所在区域不允许被自动手势命中，防止 AI 自己点「确认」。
        if (protectedRegions.isProtectedTarget(currentPackage)) {
            return denied(
                envelope,
                CommandErrors.ACTION_REJECTED,
                "target belongs to a protected confirmation surface; automation must not touch it",
            )
        }

        val check = store.validateTarget(
            snapshotId = snapshotId,
            targetId = targetId,
            expectedSelectorHash = expectedSelectorHash,
            expectedPackage = expectedPackage,
            currentPackage = currentPackage,
        )
        val node = when (check) {
            is TargetCheck.Rejected -> return denied(envelope, check.code, check.message)
            is TargetCheck.Ok -> check.node
        }

        // 重新取得当前句柄，而不是复用旧对象；旧对象可能已被回收。
        val handle = nodeHandles[snapshotId]?.get(node.target_id)
            ?: return denied(envelope, CommandErrors.STALE_SNAPSHOT, "target handle is no longer available")

        val beforePage = if (operation == "scroll") observe("tree").evidence["current_page"] else null
        val applied = try {
            action(service, handle, envelope)
        } catch (error: RuntimeException) {
            return denied(envelope, CommandErrors.ACTION_REJECTED, "accessibility action failed: ${error.javaClass.simpleName}")
        }
        if (!applied) {
            return denied(envelope, CommandErrors.ACTION_REJECTED, "accessibility action was rejected")
        }

        // 文档硬性要求：动作后必须重新观察，不能只返回「已点击」。
        var after = observe("auto")
        if (operation == "scroll") {
            if (beforePage != null) {
                for (attempt in 0 until 16) {
                    if (after.evidence["current_page"] != null && after.evidence["current_page"] != beforePage && after.evidence["page_settled"] == "true") break
                    Thread.sleep(150)
                    after = observe("auto")
                }
                if (after.evidence["current_page"] == beforePage || after.evidence["current_page"] == null || after.evidence["page_settled"] != "true") {
                    return denied(envelope, CommandErrors.ACTION_REJECTED, "scroll completed but observed page did not change")
                }
            }
        }
        if (after.status != "ok") return after.copy(request_id = envelope.request_id, device_id = envelope.device_id)
        return ok(
            envelope,
            mapOf(
                "operation" to operation,
                "snapshot_id" to snapshotId,
                "target_id" to node.target_id,
                "selector_hash" to node.selector_hash,
                "foreground_package" to currentPackage,
                "post_observe_snapshot_id" to (after.evidence["snapshot_id"] ?: ""),
                "post_observe_node_count" to (after.evidence["node_count"] ?: ""),
                "current_page" to after.evidence["current_page"].orEmpty(),
                "total_pages" to after.evidence["total_pages"].orEmpty(),
            ),
        )
    }

    // ---------- 语义树遍历 ----------

    private fun walk(
        node: AccessibilityNodeInfo,
        path: String,
        handles: MutableMap<String, AccessibilityNodeInfo>,
        refs: MutableList<UiNodeRef>,
        depth: Int,
    ) {
        if (refs.size >= UiSnapshotStore.MAX_NODES || depth > UiSnapshotStore.MAX_DEPTH) return

        val rect = Rect()
        node.getBoundsInScreen(rect)
        if (valid(rect)) {
            val selector = UiSnapshotStore.selectorHash(
                className = node.className?.toString().orEmpty(),
                text = node.text?.toString().orEmpty(),
                contentDescription = node.contentDescription?.toString().orEmpty(),
                left = rect.left,
                top = rect.top,
                right = rect.right,
                bottom = rect.bottom,
            )
            val targetId = UiSnapshotStore.targetId(path, selector)
            handles[targetId] = node
            refs.add(
                UiNodeRef(
                    target_id = targetId,
                    selector_hash = selector,
                    class_name = node.className?.toString().orEmpty(),
                    text = node.text?.toString().orEmpty(),
                    content_description = node.contentDescription?.toString().orEmpty(),
                    left = rect.left,
                    top = rect.top,
                    right = rect.right,
                    bottom = rect.bottom,
                    clickable = node.isClickable,
                    editable = node.isEditable,
                    scrollable = node.isScrollable,
                ),
            )
        }

        for (index in 0 until node.childCount) {
            val child = node.getChild(index) ?: continue
            walk(child, "$path/$index", handles, refs, depth + 1)
        }
    }

    // ---------- 手势 fallback（只在有有效 bounds 时使用） ----------

    private fun gestureTap(service: AccessibilityService, rect: Rect): Boolean {
        if (!valid(rect)) return false
        val path = Path().apply { moveTo(rect.centerX().toFloat(), rect.centerY().toFloat()) }
        return dispatchGesture(service, path, TAP_DURATION_MS)
    }

    private fun gestureLongPress(service: AccessibilityService, rect: Rect, durationMs: Int): Boolean {
        if (!valid(rect)) return false
        val path = Path().apply { moveTo(rect.centerX().toFloat(), rect.centerY().toFloat()) }
        return dispatchGesture(service, path, durationMs)
    }

    private fun gestureScroll(
        service: AccessibilityService,
        rect: Rect,
        direction: String,
        amount: Int,
    ): Boolean {
        if (!valid(rect)) return false
        // Center the full stroke inside the verified node; subtracting from its
        // center alone can put the endpoint outside the screen on a long swipe.
        val horizontal = direction == "left" || direction == "right"
        val extent = if (horizontal) rect.width() else rect.height()
        val distance = amount.toFloat().coerceIn(1f, (extent * 0.8f).coerceAtLeast(1f))
        val offset = if (direction == "left" || direction == "up") distance / 2 else -distance / 2
        val startX = rect.exactCenterX() + if (horizontal) offset else 0f
        val startY = rect.exactCenterY() + if (horizontal) 0f else offset
        val endX = rect.exactCenterX() - if (horizontal) offset else 0f
        val endY = rect.exactCenterY() - if (horizontal) 0f else offset
        val path = Path().apply {
            moveTo(startX, startY)
            lineTo(endX, endY)
        }
        return dispatchGesture(service, path, SCROLL_DURATION_MS)
    }

    /** Wait for the gesture callback on the worker, then verify actual state with observe. */
    private fun dispatchGesture(service: AccessibilityService, path: Path, durationMs: Int): Boolean {
        if (Looper.myLooper() == Looper.getMainLooper()) return false
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, durationMs.toLong()))
            .build()
        val latch = CountDownLatch(1)
        val completed = AtomicBoolean(false)
        val accepted = service.dispatchGesture(gesture, object : AccessibilityService.GestureResultCallback() {
            override fun onCompleted(gestureDescription: GestureDescription) { completed.set(true); latch.countDown() }
            override fun onCancelled(gestureDescription: GestureDescription) { latch.countDown() }
        }, Handler(Looper.getMainLooper()))
        return try {
            accepted && latch.await(durationMs + 2000L, TimeUnit.MILLISECONDS) && completed.get()
        } catch (_: InterruptedException) {
            Thread.currentThread().interrupt()
            false
        }
    }

    // ---------- 辅助 ----------

    private fun ancestorClickable(node: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        var current: AccessibilityNodeInfo? = node.parent
        var steps = 0
        while (current != null && steps < MAX_ANCESTOR_WALK) {
            if (current.isClickable) return current
            current = current.parent
            steps++
        }
        return null
    }

    private fun scrollableAncestor(node: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        var current: AccessibilityNodeInfo? = node.parent
        var steps = 0
        while (current != null && steps < MAX_ANCESTOR_WALK) {
            if (current.isScrollable) return current
            current = current.parent
            steps++
        }
        return null
    }

    /** 长按用 ACTION_LONG_CLICK（若节点支持），否则由手势兜底。 */
    private fun performLongClick(node: AccessibilityNodeInfo): Boolean =
        node.actionList.any { it.id == AccessibilityNodeInfo.ACTION_LONG_CLICK } &&
            node.performAction(AccessibilityNodeInfo.ACTION_LONG_CLICK)

    private fun valid(rect: Rect): Boolean = rect.right > rect.left && rect.bottom > rect.top

    /** 无障碍服务仍连接且屏幕可用于交互；不可用即视为需要用户解锁/唤醒，任务立即停止。 */
    private fun serviceActive(service: AccessibilityService): Boolean =
        WhaleAccessibilityService.instance === service

    private fun ok(envelope: DeviceEnvelope, evidence: Map<String, String>): DeviceResponse =
        DeviceResponse(envelope.request_id, envelope.device_id, "ok", evidence = evidence)

    private fun denied(envelope: DeviceEnvelope, code: String, message: String): DeviceResponse =
        DeviceResponse(envelope.request_id, envelope.device_id, "denied", code, message)

    private companion object {
        const val TAP_DURATION_MS = 50
        const val SCROLL_DURATION_MS = 350
        const val MIN_LONG_PRESS_MS = 300
        const val MAX_LONG_PRESS_MS = 30_000
        const val MAX_SCROLL_AMOUNT = 2_000
        const val DEFAULT_SCROLL_AMOUNT = 600
        const val MAX_ANCESTOR_WALK = 8
    }
}
