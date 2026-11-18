package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M07 固定测试：快照缓存、指纹计算与动作前四项校验。
 *
 * 对应 `docs/modules/M07-ui-accessibility-development.md`：
 * - Snapshot 字段与上限：最多 8 个快照、节点上限 3000、深度上限 40。
 * - selector_hash 由 className|text|contentDescription|bounds 的 SHA-256 取 24 字符。
 * - target_id 由 treePath|selector_hash 生成。
 * - 动作前校验 snapshot / target / selector / expected_package / 当前前台包。
 * - 产品验收：旧 snapshot 重放必定失败。
 */
class UiSnapshotStoreTest {
    /** 单调递增的假时钟：保证同一包名的多次快照得到不同 snapshot_id。 */
    private var tickCounter = 0L

    private fun node(
        path: String,
        className: String = "android.widget.Button",
        text: String = "确定",
        contentDescription: String = "",
        left: Int = 0,
        top: Int = 0,
        right: Int = 100,
        bottom: Int = 50,
        clickable: Boolean = true,
        editable: Boolean = false,
        scrollable: Boolean = false,
    ): UiNodeRef {
        val selector = UiSnapshotStore.selectorHash(className, text, contentDescription, left, top, right, bottom)
        return UiNodeRef(
            target_id = UiSnapshotStore.targetId(path, selector),
            selector_hash = selector,
            class_name = className,
            text = text,
            content_description = contentDescription,
            left = left,
            top = top,
            right = right,
            bottom = bottom,
            clickable = clickable,
            editable = editable,
            scrollable = scrollable,
        )
    }

    // ---------- 指纹计算 ----------

    @Test fun selectorHashIsTwentyFourChars() {
        val hash = UiSnapshotStore.selectorHash("android.widget.Button", "确定", "", 0, 0, 100, 50)
        assertEquals(24, hash.length)
    }

    @Test fun selectorHashIsStableForSameInput() {
        val a = UiSnapshotStore.selectorHash("android.widget.Button", "确定", "", 0, 0, 100, 50)
        val b = UiSnapshotStore.selectorHash("android.widget.Button", "确定", "", 0, 0, 100, 50)
        assertEquals(a, b)
    }

    @Test fun selectorHashChangesWhenBoundsChange() {
        val a = UiSnapshotStore.selectorHash("android.widget.Button", "确定", "", 0, 0, 100, 50)
        val b = UiSnapshotStore.selectorHash("android.widget.Button", "确定", "", 0, 0, 100, 51)
        assertNotEquals(a, b)
    }

    @Test fun selectorHashChangesWhenTextChanges() {
        val a = UiSnapshotStore.selectorHash("android.widget.Button", "确定", "", 0, 0, 100, 50)
        val b = UiSnapshotStore.selectorHash("android.widget.Button", "取消", "", 0, 0, 100, 50)
        assertNotEquals(a, b)
    }

    @Test fun selectorHashChangesWhenContentDescriptionChanges() {
        val a = UiSnapshotStore.selectorHash("android.widget.Button", "确定", "", 0, 0, 100, 50)
        val b = UiSnapshotStore.selectorHash("android.widget.Button", "确定", "关闭", 0, 0, 100, 50)
        assertNotEquals(a, b)
    }

    @Test fun targetIdDiffersByTreePath() {
        val selector = UiSnapshotStore.selectorHash("android.widget.Button", "确定", "", 0, 0, 100, 50)
        val a = UiSnapshotStore.targetId("0/0", selector)
        val b = UiSnapshotStore.targetId("0/1", selector)
        assertNotEquals(a, b)
    }

    // ---------- 缓存上限与淘汰 ----------

    @Test fun storeRetainsAtMostEightSnapshots() {
        val store = UiSnapshotStore(clock = { tickCounter++ })
        repeat(12) { index ->
            store.save("com.example.app", index, listOf(node("0")))
        }
        assertEquals(UiSnapshotStore.MAX_SNAPSHOTS, store.size)
    }

    @Test fun oldestSnapshotIsEvictedFirst() {
        val store = UiSnapshotStore(clock = { tickCounter++ })
        val first = store.save("com.example.app", 0, listOf(node("0")))
        repeat(UiSnapshotStore.MAX_SNAPSHOTS) { index ->
            store.save("com.example.app", index + 1, listOf(node("0")))
        }
        // 第 1 个快照应已被淘汰，最近写入的仍在缓存中。
        assertNull(store.find(first.snapshot_id))
        assertEquals(UiSnapshotStore.MAX_SNAPSHOTS, store.size)
    }

    @Test fun evictedSnapshotIsStaleNotFound() {
        val store = UiSnapshotStore(clock = { tickCounter++ })
        val first = store.save("com.example.app", 0, listOf(node("0")))
        repeat(UiSnapshotStore.MAX_SNAPSHOTS + 1) { index ->
            store.save("com.example.app", index + 1, listOf(node("0")))
        }
        val check = store.validateTarget(
            snapshotId = first.snapshot_id,
            targetId = first.nodes.first().target_id,
            expectedSelectorHash = first.nodes.first().selector_hash,
            expectedPackage = "com.example.app",
            currentPackage = "com.example.app",
        )
        assertTrue(check is TargetCheck.Rejected)
        assertEquals(CommandErrors.STALE_SNAPSHOT, (check as TargetCheck.Rejected).code)
    }

    // ---------- 动作前四项校验 ----------

    @Test fun validTargetPassesAllChecks() {
        val store = UiSnapshotStore(clock = { tickCounter++ })
        val snapshot = store.save("com.android.settings", 7, listOf(node("0/1")))
        val target = snapshot.nodes.first()

        val check = store.validateTarget(
            snapshotId = snapshot.snapshot_id,
            targetId = target.target_id,
            expectedSelectorHash = target.selector_hash,
            expectedPackage = "com.android.settings",
            currentPackage = "com.android.settings",
        )
        assertTrue(check is TargetCheck.Ok)
        assertEquals(target, (check as TargetCheck.Ok).node)
    }

    @Test fun missingSnapshotIsStale() {
        val store = UiSnapshotStore()
        val check = store.validateTarget(
            snapshotId = "snap-does-not-exist",
            targetId = "any",
            expectedSelectorHash = "any",
            expectedPackage = "com.android.settings",
            currentPackage = "com.android.settings",
        )
        assertEquals(CommandErrors.STALE_SNAPSHOT, (check as TargetCheck.Rejected).code)
    }

    @Test fun unknownTargetIsStale() {
        val store = UiSnapshotStore(clock = { tickCounter++ })
        val snapshot = store.save("com.android.settings", 7, listOf(node("0/1")))
        val check = store.validateTarget(
            snapshotId = snapshot.snapshot_id,
            targetId = "target-not-in-snapshot",
            expectedSelectorHash = snapshot.nodes.first().selector_hash,
            expectedPackage = "com.android.settings",
            currentPackage = "com.android.settings",
        )
        assertEquals(CommandErrors.STALE_SNAPSHOT, (check as TargetCheck.Rejected).code)
    }

    @Test fun wrongSelectorIsSelectorMismatch() {
        val store = UiSnapshotStore(clock = { tickCounter++ })
        val snapshot = store.save("com.android.settings", 7, listOf(node("0/1")))
        val check = store.validateTarget(
            snapshotId = snapshot.snapshot_id,
            targetId = snapshot.nodes.first().target_id,
            expectedSelectorHash = "deadbeefdeadbeefdeadbeef",
            expectedPackage = "com.android.settings",
            currentPackage = "com.android.settings",
        )
        assertEquals(CommandErrors.SELECTOR_MISMATCH, (check as TargetCheck.Rejected).code)
    }

    @Test fun blankSelectorIsSelectorMismatch() {
        val store = UiSnapshotStore(clock = { tickCounter++ })
        val snapshot = store.save("com.android.settings", 7, listOf(node("0/1")))
        val check = store.validateTarget(
            snapshotId = snapshot.snapshot_id,
            targetId = snapshot.nodes.first().target_id,
            expectedSelectorHash = "",
            expectedPackage = "com.android.settings",
            currentPackage = "com.android.settings",
        )
        assertEquals(CommandErrors.SELECTOR_MISMATCH, (check as TargetCheck.Rejected).code)
    }

    @Test fun foregroundPackageChangedIsRejected() {
        val store = UiSnapshotStore(clock = { tickCounter++ })
        val snapshot = store.save("com.android.settings", 7, listOf(node("0/1")))
        val target = snapshot.nodes.first()
        val check = store.validateTarget(
            snapshotId = snapshot.snapshot_id,
            targetId = target.target_id,
            expectedSelectorHash = target.selector_hash,
            expectedPackage = "com.android.settings",
            currentPackage = "com.android.chrome",
        )
        assertEquals(CommandErrors.FOREGROUND_PACKAGE_MISMATCH, (check as TargetCheck.Rejected).code)
    }

    @Test fun snapshotPackageMismatchIsRejected() {
        val store = UiSnapshotStore(clock = { tickCounter++ })
        val snapshot = store.save("com.android.chrome", 7, listOf(node("0/1")))
        val target = snapshot.nodes.first()
        val check = store.validateTarget(
            snapshotId = snapshot.snapshot_id,
            targetId = target.target_id,
            expectedSelectorHash = target.selector_hash,
            expectedPackage = "com.android.settings",
            currentPackage = "com.android.settings",
        )
        assertEquals(CommandErrors.FOREGROUND_PACKAGE_MISMATCH, (check as TargetCheck.Rejected).code)
    }

    // ---------- evidence 形状 ----------

    @Test fun evidenceHasDocumentedFields() {
        val store = UiSnapshotStore(clock = { tickCounter++ })
        val nodes = listOf(node("0"), node("0/0", text = "二级"))
        val snapshot = store.save("com.android.settings", 3, nodes)
        val evidence = snapshot.evidence()

        assertEquals(
            setOf("snapshot_id", "package_name", "window_id", "node_count", "nodes"),
            evidence.keys,
        )
        assertEquals("2", evidence["node_count"])
        assertEquals("3", evidence["window_id"])
        assertEquals(2, evidence["nodes"]!!.split("\n").size)
    }

    @Test fun evidenceLineHasDocumentedNodeFields() {
        val store = UiSnapshotStore(clock = { tickCounter++ })
        val snapshot = store.save("com.android.settings", 3, listOf(node("0")))
        val cells = snapshot.nodes.first().toEvidenceLine().split("|")
        // target_id|selector_hash|class|text|contentDescription|bounds|clickable|editable|scrollable
        assertEquals(9, cells.size)
        assertEquals("0,0,100,50", cells[5])
    }

    @Test fun nodeWithoutValidBoundsIsMarked() {
        val flat = node("0", left = 0, top = 0, right = 0, bottom = 0)
        assertFalse(flat.hasValidBounds)
        assertTrue(node("0").hasValidBounds)
    }

    // ---------- 文档上限常量 ----------

    @Test fun documentedLimitsMatchConstants() {
        assertEquals(8, UiSnapshotStore.MAX_SNAPSHOTS)
        assertEquals(3000, UiSnapshotStore.MAX_NODES)
        assertEquals(40, UiSnapshotStore.MAX_DEPTH)
        assertEquals(24, UiSnapshotStore.SELECTOR_HASH_LENGTH)
    }

    // ---------- 安全防注入：确认卡不可被自动手势命中 ----------

    @Test fun protectedRegionBlocksTargetsInConfirmCardPackage() {
        val guard = ProtectedRegionGuard()
        guard.publish("cn.whale.agent.device")
        assertTrue(guard.isActive)
        assertTrue(guard.isProtectedTarget("cn.whale.agent.device"))
    }

    @Test fun unprotectedPackageIsAllowedWhenConfirmCardActive() {
        val guard = ProtectedRegionGuard()
        guard.publish("cn.whale.agent.device")
        // 第三方应用页面不受保护，否则确认卡期间整个设备都动不了。
        assertFalse(guard.isProtectedTarget("com.android.settings"))
    }

    @Test fun releasedGuardProtectsNothing() {
        val guard = ProtectedRegionGuard()
        guard.publish("cn.whale.agent.device")
        guard.release()
        assertFalse(guard.isActive)
        assertFalse(guard.isProtectedTarget("cn.whale.agent.device"))
    }

    @Test fun guardIsInactiveByDefault() {
        val guard = ProtectedRegionGuard()
        assertFalse(guard.isActive)
        assertFalse(guard.isProtectedTarget("cn.whale.agent.device"))
    }
}
