package cn.whale.agent.device

import java.security.MessageDigest

/**
 * M07 快照缓存与指纹计算（纯逻辑，不依赖 Android 运行时）。
 *
 * 规范来源：`docs/modules/M07-ui-accessibility-development.md`
 *
 * 固定约束：
 * - Snapshot 字段固定：snapshot_id、package_name、window_id、node_count、
 *   nodes[target_id, selector_hash, class, text, contentDescription, bounds,
 *   clickable, editable, scrollable]。
 * - `snapshot_id` 只在当前进程与短期缓存内有效，**最多保留 8 个**。
 * - 节点最多 3000 个，树深度最多 40。
 * - `selector_hash` 固定由 `className | text | contentDescription | bounds` canonical
 *   拼接后 SHA-256 截取 24 字符。
 * - `target_id` 固定由 `treePath | selector_hash` 生成。
 *
 * 禁止项（文档明示）：
 * - 禁止把 node index 当成稳定 ID（所以 target_id 由 treePath 参与计算，且动作前要复核 selector）。
 * - 禁止向模型发送密码/验证码字段或完整剪贴板。
 */

/** 单个节点的语义描述。字段顺序与文档一致。 */
data class UiNodeRef(
    val target_id: String,
    val selector_hash: String,
    val class_name: String,
    val text: String,
    val content_description: String,
    val left: Int,
    val top: Int,
    val right: Int,
    val bottom: Int,
    val clickable: Boolean,
    val editable: Boolean,
    val scrollable: Boolean,
) {
    val hasValidBounds: Boolean get() = right > left && bottom > top

    /** 文档固定输出形态：以 `|` 分隔，便于 evidence 回传与断言。 */
    fun toEvidenceLine(): String =
        "$target_id|$selector_hash|$class_name|$text|$content_description|$left,$top,$right,$bottom|$clickable|$editable|$scrollable"
}

/** 一份快照：节点集合 + 归属包名 + 窗口 ID。 */
data class UiSnapshot(
    val snapshot_id: String,
    val package_name: String,
    val window_id: Int,
    val nodes: List<UiNodeRef>,
) {
    val node_count: Int get() = nodes.size

    private val byTargetId: Map<String, UiNodeRef> = nodes.associateBy { it.target_id }

    fun node(targetId: String): UiNodeRef? = byTargetId[targetId]

    fun evidence(): Map<String, String> = mapOf(
        "snapshot_id" to snapshot_id,
        "package_name" to package_name,
        "window_id" to window_id.toString(),
        "node_count" to node_count.toString(),
        "nodes" to nodes.joinToString("\n") { it.toEvidenceLine() },
    )
}

/** 目标校验结果。只有 [Ok] 允许继续执行动作。 */
sealed class TargetCheck {
    data class Ok(val node: UiNodeRef) : TargetCheck()

    data class Rejected(val code: String, val message: String) : TargetCheck()
}

/**
 * 受保护区域登记：确认卡与停止按钮所在的安全窗口。
 *
 * 文档「安全防注入」：注入期间，确认卡和停止按钮**不允许被自动手势命中**。
 * 这里把保护判定做成纯逻辑，便于 JVM 单测；由 [AccessibilityController] 在动作前查询。
 */
class ProtectedRegionGuard {
    @Volatile
    private var protectedPackage: String = ""

    @Volatile
    private var active: Boolean = false

    /** 确认卡出现时登记其所属包名；确认结束后必须 [release]。 */
    fun publish(packageName: String) {
        protectedPackage = packageName
        active = true
    }

    fun release() {
        protectedPackage = ""
        active = false
    }

    val isActive: Boolean get() = active

    /**
     * 目标是否已被保护。
     *
     * @param targetPackage 目标节点所属包名（即当前前台包）。
     * @return true 表示命中受保护区域，动作必须被拒绝。
     */
    fun isProtectedTarget(targetPackage: String): Boolean =
        active && protectedPackage.isNotEmpty() && targetPackage == protectedPackage
}

/**
 * 快照缓存。
 *
 * @param maxSnapshots 固定 8（文档要求）；测试可注入更小值以验证淘汰顺序。
 */
class UiSnapshotStore(
    private val maxSnapshots: Int = MAX_SNAPSHOTS,
    private val idGenerator: (String, Long) -> String = { packageName, tick -> sha256("$packageName:$tick").take(24) },
    private val clock: () -> Long = { System.nanoTime() },
) {
    private val snapshots = LinkedHashMap<String, UiSnapshot>()

    /** 当前缓存中的快照数量（测试断言淘汰用）。 */
    val size: Int get() = snapshots.size

    fun snapshotIds(): List<String> = snapshots.keys.toList()

    /**
     * 保存快照并按 FIFO 淘汰到上限。
     *
     * 文档要求「最多保留 8 个」，所以写入后立刻淘汰最早的，不同步等待。
     */
    fun save(packageName: String, windowId: Int, nodes: List<UiNodeRef>): UiSnapshot {
        val id = idGenerator(packageName, clock())
        val snapshot = UiSnapshot(id, packageName, windowId, nodes)
        snapshots[id] = snapshot
        while (snapshots.size > maxSnapshots) {
            val oldest = snapshots.keys.first()
            snapshots.remove(oldest)
        }
        return snapshot
    }

    fun find(snapshotId: String): UiSnapshot? = snapshots[snapshotId]

    fun clear() = snapshots.clear()

    /**
     * 动作前四项校验（文档「动作执行前重新取得当前节点并校验」）。
     *
     * 顺序固定为：snapshot 存在 → target 存在 → selector 一致 → 快照包名与 expected 一致
     * → 当前前台包与 expected 一致。任一不符立即返回对应错误码，绝不「再点一下试试」。
     *
     * @param currentPackage 当前前台包名；调用方负责传入实时值（不缓存）。
     */
    fun validateTarget(
        snapshotId: String,
        targetId: String,
        expectedSelectorHash: String,
        expectedPackage: String,
        currentPackage: String,
    ): TargetCheck {
        val snapshot = snapshots[snapshotId]
            ?: return TargetCheck.Rejected(
                CommandErrors.STALE_SNAPSHOT,
                "snapshot is no longer available",
            )

        val node = snapshot.node(targetId)
            ?: return TargetCheck.Rejected(
                CommandErrors.STALE_SNAPSHOT,
                "target is not present in snapshot",
            )

        if (expectedSelectorHash.isBlank() || expectedSelectorHash != node.selector_hash) {
            return TargetCheck.Rejected(
                CommandErrors.SELECTOR_MISMATCH,
                "target selector does not match snapshot",
            )
        }

        if (expectedPackage.isNotBlank() && snapshot.package_name != expectedPackage) {
            return TargetCheck.Rejected(
                CommandErrors.FOREGROUND_PACKAGE_MISMATCH,
                "snapshot package does not match expected package",
            )
        }

        if (expectedPackage.isNotBlank() && currentPackage != expectedPackage) {
            return TargetCheck.Rejected(
                CommandErrors.FOREGROUND_PACKAGE_MISMATCH,
                "current foreground package does not match expected package",
            )
        }

        return TargetCheck.Ok(node)
    }

    companion object {
        /** 文档固定：最多保留 8 个快照。 */
        const val MAX_SNAPSHOTS: Int = 8

        /** 文档固定：节点最多 3000 个。 */
        const val MAX_NODES: Int = 3000

        /** 文档固定：树深度最多 40。 */
        const val MAX_DEPTH: Int = 40

        /** selector_hash 截取长度：24 字符。 */
        const val SELECTOR_HASH_LENGTH: Int = 24

        fun sha256(value: String): String = MessageDigest.getInstance("SHA-256")
            .digest(value.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }

        /**
         * selector_hash：由 `className | text | contentDescription | bounds` canonical
         * 拼接后 SHA-256 截取 24 字符。
         *
         * bounds 用 `left,top,right,bottom` 固定顺序，避免不同渲染路径产生不同字符串。
         */
        fun selectorHash(
            className: String,
            text: String,
            contentDescription: String,
            left: Int,
            top: Int,
            right: Int,
            bottom: Int,
        ): String = sha256(
            "$className|$text|$contentDescription|$left,$top,$right,$bottom",
        ).take(SELECTOR_HASH_LENGTH)

        /** target_id：由 `treePath | selector_hash` 生成。 */
        fun targetId(treePath: String, selectorHash: String): String = sha256("$treePath|$selectorHash")
    }
}
