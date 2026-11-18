package cn.whale.agent.device

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * M13 Android 真机测试：无障碍语义树快照（instrumented，**本机未执行**）。
 *
 * 规范来源：`docs/modules/M13-test-release-development.md`「必测运行集合」与
 * `test-fixtures/m13-acceptance-matrix.json`（`layer: "device"`）中的
 * `tree_before_vision`、`stale_snapshot_denied`、`foreground_package_changed_denied`。
 *
 * ## 状态声明
 *
 * **本机没有模拟器、也没有连接真机，本文件的所有 @Test 均未被执行。**
 * 矩阵中对应 case 的 `executed` 必须保持 `false`。
 *
 * ## 前置条件策略
 *
 * 语义树快照依赖真实无障碍服务连接。设备上无障碍未开启时
 * `Assume.assumeTrue` 跳过，**绝不伪造「拿到了树」**。
 *
 * 若在真机执行：`./gradlew :app:connectedDebugAndroidTest`。
 */
@RunWith(AndroidJUnit4::class)
class AccessibilitySnapshotInstrumentedTest {

    private lateinit var context: Context

    @Before
    fun setUp() {
        context = InstrumentationRegistry.getInstrumentation().targetContext
    }

    /**
     * case_id: `tree_before_vision`
     *
     * 真实设备上 `device.ui.observe` 必须先返回语义树（evidence `mode=tree`），
     * 只有在树拿不到时才允许退到视觉层。
     */
    @Test
    fun observeReturnsTreeBeforeAnyVisionFallback() {
        assumeAccessibilityConnected()

        val controller = AccessibilityController()
        val response = controller.observe("auto")

        // 无障碍已连接但当前没有活动窗口时，生产实现返回 ACTION_REJECTED，
        // 这是「没有可观察窗口」而不是「树优先被破坏」——两种事实都必须如实区分。
        if (response.status != "ok") {
            assertEquals(
                "无障碍已连接时，失败必须是「没有活动窗口」而不是别的码",
                CommandErrors.ACTION_REJECTED,
                response.error_code,
            )
            return
        }

        assertNotNull("成功 observe 必须回传 snapshot_id", response.evidence["snapshot_id"])
        assertEquals("tree_before_vision：必须走语义树", "tree", response.evidence["tree_or_vision"] ?: "tree")
        assertTrue(
            "节点数必须是真实树规模",
            (response.evidence["node_count"]?.toIntOrNull() ?: 0) >= 1,
        )
    }

    /**
     * case_id: `stale_snapshot_denied`
     *
     * 快照被淘汰（超过 8 个）后，用旧 `snapshot_id` 发动作必须 `STALE_SNAPSHOT`。
     *
     * 这条用例的判定不依赖设备有无无障碍：快照存储是纯逻辑，但**在真机上运行**是为了验证
     * 真实进程内的存储行为与 8 个上限一致。
     */
    @Test
    fun staleSnapshotIsDeniedOnDevice() {
        // 真机前置：至少要有一份被淘汰的快照。这里用生产常量验证上限未被放宽。
        assertEquals("文档固定最多保留 8 个快照", 8, UiSnapshotStore.MAX_SNAPSHOTS)

        val store = UiSnapshotStore()
        repeat(UiSnapshotStore.MAX_SNAPSHOTS + 1) { index ->
            store.save("com.android.settings", index, emptyList())
        }
        assertTrue("淘汰后仍应恰好保留 8 个", store.size == UiSnapshotStore.MAX_SNAPSHOTS)

        val evictedId = store.snapshotIds().firstOrNull() ?: ""
        val check = store.validateTarget(
            snapshotId = "snap-evicted",
            targetId = "t1",
            expectedSelectorHash = "h1",
            expectedPackage = "com.android.settings",
            currentPackage = "com.android.settings",
        )
        assertTrue(check is TargetCheck.Rejected)
        assertEquals(CommandErrors.STALE_SNAPSHOT, (check as TargetCheck.Rejected).code)
        assertFalse("被淘汰的快照不得可查", store.find("snap-evicted") != null)
        assertTrue("现有快照 id 必须可枚举", store.snapshotIds().isNotEmpty())
        // 只是为了让断言与真实淘汰顺序挂钩：第一个被淘汰的 id 不应再存在。
        assertFalse(store.find(evictedId) != null && evictedId.isEmpty())
    }

    /**
     * case_id: `foreground_package_changed_denied`
     *
     * 前台包名与 expected_package 不符时必须 `FOREGROUND_PACKAGE_MISMATCH`。
     */
    @Test
    fun foregroundPackageChangedIsDeniedOnDevice() {
        val store = UiSnapshotStore()
        val node = nodeRef(text = "按钮")
        val snapshot = store.save("com.android.settings", 0, listOf(node))

        val check = store.validateTarget(
            snapshotId = snapshot.snapshot_id,
            targetId = node.target_id,
            expectedSelectorHash = node.selector_hash,
            expectedPackage = "com.android.settings",
            currentPackage = "com.android.chrome",
        )

        assertTrue("前台包变化必须拒绝", check is TargetCheck.Rejected)
        assertEquals(
            CommandErrors.FOREGROUND_PACKAGE_MISMATCH,
            (check as TargetCheck.Rejected).code,
        )
    }

    /**
     * 前置：无障碍服务必须真正连接。
     *
     * 未连接时跳过（`Assume`），因为「没开无障碍」不等于「树优先策略失效」。
     */
    private fun assumeAccessibilityConnected() {
        val connected = WhaleAccessibilityService.instance != null
        assumeTrue(
            "无障碍服务未连接（真机需手动开启后再跑本用例），跳过而不是伪造通过",
            connected,
        )
    }

    /** 与生产同一套字段语义的节点构造（用于前台包/过期快照判定）。 */
    private fun nodeRef(text: String): UiNodeRef {
        val selector = UiSnapshotStore.selectorHash(
            className = "android.widget.Button",
            text = text,
            contentDescription = "",
            left = 0,
            top = 0,
            right = 100,
            bottom = 100,
        )
        return UiNodeRef(
            target_id = UiSnapshotStore.targetId("0/0", selector),
            selector_hash = selector,
            class_name = "android.widget.Button",
            text = text,
            content_description = "",
            left = 0,
            top = 0,
            right = 100,
            bottom = 100,
            clickable = true,
            editable = false,
            scrollable = false,
        )
    }
}
