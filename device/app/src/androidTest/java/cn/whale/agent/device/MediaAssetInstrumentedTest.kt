package cn.whale.agent.device

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * M13 Android 真机测试：媒体素材（instrumented，**本机未执行**）。
 *
 * 规范来源：`docs/modules/M13-test-release-development.md`「必测运行集合」与
 * `test-fixtures/m13-acceptance-matrix.json`（`layer: "device"`）中的
 * `camera_capture_upload`、`photo_picker_only_user_uri`。
 *
 * ## 状态声明
 *
 * **本机没有模拟器、也没有连接真机，本文件的所有 @Test 均未被执行。**
 * 矩阵中上述 case 的 `executed` 必须保持 `false`；这里只提供可编译的真机骨架。
 *
 * ## 前置条件策略
 *
 * - 相机用例要求真实 `CAMERA` 权限；未授权 → `Assume.assumeTrue` 跳过。
 * - Photo Picker 用例要求 API 33+ 且用户真实选择过；无法在无人工介入的自动化里
 *   伪造「用户选择了」这一事实，因此该用例在无人值守环境**必须跳过**，绝不伪造成功。
 *
 * 若在真机执行：`./gradlew :app:connectedDebugAndroidTest`。
 */
@RunWith(AndroidJUnit4::class)
class MediaAssetInstrumentedTest {

    private lateinit var context: Context
    private lateinit var assetStore: AssetStore
    private lateinit var cacheDir: File

    @Before
    fun setUp() {
        context = InstrumentationRegistry.getInstrumentation().targetContext
        cacheDir = File(context.cacheDir, "androidTest-media").apply { mkdirs() }
        // 真实受控目录：与生产同一套 AssetStore 实现。
        assetStore = AssetStore(File(context.filesDir, "androidTest-assets"))
    }

    /**
     * case_id: `camera_capture_upload`
     *
     * 真机拍摄必须产出 `asset_id`/`mime`/`size`/`sha256` 四项证据，且只落受控目录。
     */
    @Test
    fun cameraCaptureProducesAssetEvidence() {
        val cameraGranted = androidx.core.content.ContextCompat.checkSelfPermission(
            context,
            android.Manifest.permission.CAMERA,
        ) == android.content.pm.PackageManager.PERMISSION_GRANTED
        assumeTrue(
            "CAMERA 未授权（真机需手动授权），跳过而不是伪造通过",
            cameraGranted,
        )

        val controller = CameraController(context, assetStore)
        val outcome = controller.capture(CameraController.Request(lens = "back", quality = "standard", taskId = "androidTest-camera"))

        when (outcome) {
            is CameraController.CaptureOutcome.Failed -> {
                // 真机上没有前台 Activity 可绑定 CameraX 时返回 NOT_WIRED：
                // 这是「相机不可用」的如实失败，绝不伪造拍摄成功。
                assertTrue(
                    "相机失败必须是已登记失败码：${outcome.code}",
                    outcome.code == CommandErrors.NOT_WIRED || MediaErrors.isDeclared(outcome.code),
                )
            }

            is CameraController.CaptureOutcome.Captured -> {
                val evidence = assetStore.evidence(outcome.entry)
                assertTrue("必须回传 asset_id", evidence["asset_id"]?.isNotBlank() == true)
                assertTrue("必须回传 mime", evidence["mime"]?.startsWith("image/") == true)
                assertTrue("必须回传 size", evidence["size"]?.toLongOrNull() ?: 0L > 0L)
                assertTrue("必须回传 sha256", (evidence["sha256"]?.length ?: 0) == 64)
                // 证据里绝不能出现真实文件路径。
                assertFalse("证据不得泄露真实路径", evidence.values.any { it.contains(cacheDir.absolutePath) })

                // cleanup：删除 asset + cache file。
                assertTrue(assetStore.remove(outcome.entry.assetId))
            }
        }
    }

    /**
     * 相机权限缺失时必须返回 `CAMERA_PERMISSION_REQUIRED`，且不产生文件、不落库。
     *
     * 这条用例在**无相机权限**的设备上才有意义；已授权时跳过。
     */
    @Test
    fun cameraWithoutPermissionFailsWithoutSideEffects() {
        val cameraGranted = androidx.core.content.ContextCompat.checkSelfPermission(
            context,
            android.Manifest.permission.CAMERA,
        ) == android.content.pm.PackageManager.PERMISSION_GRANTED
        assumeTrue(
            "本用例要求「无 CAMERA 权限」这一前置；当前已授权，跳过",
            !cameraGranted,
        )

        val controller = CameraController(context, assetStore)
        val before = assetStore.assetsForTask("androidTest-camera-noperm").size
        val outcome = controller.capture(
            CameraController.Request(taskId = "androidTest-camera-noperm"),
        )

        assertTrue(outcome is CameraController.CaptureOutcome.Failed)
        assertEquals(
            MediaErrors.CAMERA_PERMISSION_REQUIRED,
            (outcome as CameraController.CaptureOutcome.Failed).code,
        )
        assertEquals("失败时不得落库", before, assetStore.assetsForTask("androidTest-camera-noperm").size)
    }

    /**
     * case_id: `photo_picker_only_user_uri`
     *
     * 只接受系统选择器回传的 `content://` URI；本应用不申请存储权限、不扫描相册。
     */
    @Test
    fun photoPickerOnlyAcceptsUserSelectedUri() {
        val controller = PhotoPickerController(
            context = context,
            resolver = context.contentResolver,
            cacheDir = cacheDir,
            assets = assetStore,
        )

        // 前置 1：真机必须支持 Photo Picker（API 33+），否则该用例的前提不成立。
        assumeTrue(
            "API 低于 33 时无内置 Photo Picker，跳过（走 ACTION_OPEN_DOCUMENT 分支）",
            controller.supportsPhotoPicker(),
        )
        // 前置 2：自动化环境无法“真实点选”系统 Photo Picker。
        // 没有用户真实选择结果时，绝不能把空结果当成通过。
        assumeTrue(
            "photo_picker_only_user_uri 需要真实用户在选择器里点选；无人值守环境跳过，不伪造成功",
            false,
        )

        assertTrue("不可达分支：仅用于说明前置", controller.supportsPhotoPicker())
    }

    /**
     * 素材大小/mime 限制在真机文件系统上同样生效（不只依赖纯 JVM 规则）。
     */
    @Test
    fun assetStoreEnforcesSizeAndTypeOnDeviceStorage() {
        val tooBig = File(cacheDir, "too-big.bin").apply {
            writeBytes(ByteArray(8))
        }
        val smallStore = AssetStore(File(context.filesDir, "androidTest-assets-small"), maxBytes = 3)
        val exception = runCatching { smallStore.register(tooBig, "image/png", taskId = "androidTest-limit") }
        assertTrue("超限必须抛 MediaException", exception.exceptionOrNull() is MediaException)
        assertEquals(
            MediaErrors.MEDIA_TOO_LARGE,
            (exception.exceptionOrNull() as MediaException).code,
        )

        val badType = runCatching { assetStore.register(tooBig, "application/x-msdownload") }
        assertTrue("非法 mime 必须抛 MediaException", badType.exceptionOrNull() is MediaException)
        assertEquals(
            MediaErrors.MEDIA_TYPE_NOT_ALLOWED,
            (badType.exceptionOrNull() as MediaException).code,
        )

        // cleanup：删除 cache 文件。
        assertTrue(tooBig.delete() || !tooBig.exists())
    }

    /**
     * 任务资产表必须在 `cleanup: clear task asset table` 后归零。
     */
    @Test
    fun taskAssetTableIsClearable() {
        val countBefore = assetStore.countForTask("androidTest-clear")
        val controller = MediaStoreController(context, assetStore)
        assertEquals("空任务表清理应返回 0", 0, countBefore)
        assertEquals(0, controller.clearTask("androidTest-clear"))
        assertTrue(controller.taskAssets("androidTest-clear").isEmpty())
        assertNull(assetStore.entry("androidTest-missing-asset"))
    }
}
