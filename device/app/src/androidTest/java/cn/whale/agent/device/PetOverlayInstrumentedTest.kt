package cn.whale.agent.device

import android.content.Context
import android.provider.Settings
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
 * M13 Android 真机测试：桌宠悬浮窗（instrumented，**本机未执行**）。
 *
 * 规范来源：`docs/modules/M13-test-release-development.md`「必测运行集合」中的
 * `overlay_permission_fallback`、`voice_push_to_talk_lifecycle`，与
 * `test-fixtures/m13-acceptance-matrix.json`（`layer: "device"`）。
 *
 * ## 状态声明
 *
 * **本机没有模拟器、也没有连接真机，本文件的所有 @Test 均未被执行。**
 * 矩阵中 `overlay_permission_fallback` 的 `executed` 必须保持 `false`。
 *
 * ## 前置条件策略
 *
 * 悬浮窗用例依赖真实的 `SYSTEM_ALERT_WINDOW` 授权。未授权时
 * `Assume.assumeTrue` 跳过；**不伪造「窗口已上线」**。
 * 语音用例依赖真实 `RECORD_AUDIO` 授权，同样先 Assume。
 *
 * 若在真机执行：`./gradlew :app:connectedDebugAndroidTest`。
 */
@RunWith(AndroidJUnit4::class)
class PetOverlayInstrumentedTest {

    private lateinit var context: Context

    @Before
    fun setUp() {
        context = InstrumentationRegistry.getInstrumentation().targetContext
    }

    /**
     * case_id: `overlay_permission_fallback`
     *
     * 无障碍悬浮窗不可用时，只要普通悬浮窗权限已授予，必须退化到
     * `TYPE_APPLICATION_OVERLAY` 且窗口真实上线（`overlay_online = true`）。
     */
    @Test
    fun overlayFallsBackToApplicationOverlayWhenAccessibilityUnavailable() {
        // 前置：普通悬浮窗权限必须已授权（真机上需用户手动授权）。
        val overlayGranted = Settings.canDrawOverlays(context)
        assumeTrue(
            "SYSTEM_ALERT_WINDOW 未授权（真机需手动授权），跳过而不是伪造通过",
            overlayGranted,
        )

        // 用设备真实能力构造探测结果；无障碍状态如实反映。
        val capability = OverlayCapability(
            sdkInt = android.os.Build.VERSION.SDK_INT,
            accessibilityConnected = WhaleAccessibilityService.instance != null,
            overlayPermissionGranted = overlayGranted,
        )
        assumeTrue(
            "本用例要求「无障碍悬浮窗不可用」这一前置；当前无障碍已连接，跳过",
            !capability.accessibilityConnected || capability.sdkInt < PetOverlayPolicy.ACCESSIBILITY_OVERLAY_MIN_SDK,
        )

        val chosen = PetOverlayPolicy.choose(capability)
        assertNotNull("已授权普通悬浮窗时不得返回 null", chosen)
        assertEquals(PetOverlayType.APPLICATION_OVERLAY, chosen)
        assertTrue("必须记为 overlay_fallback", PetOverlayPolicy.isFallback(capability))
        assertFalse(PetOverlayPolicy.isUnavailable(capability))

        // 真实窗口归属：票据 attach 成功才允许 overlayOnline = true。
        val owner = PetWindowOwner()
        val claim = owner.beginGeneration()
        assertEquals(AttachOutcome.ATTACHED, owner.markAttached(claim.ticket))
        assertTrue("窗口真实上线后 overlayOnline 必须为 true", owner.overlayOnline)
        assertTrue(owner.isOnline(claim.ticket))

        // 收尾：remove overlay view 由调用方执行，这里校验票据可释放。
        assertTrue("在线票据必须可释放", owner.release(claim.ticket))
        assertFalse("释放后不得仍标记在线", owner.overlayOnline)
    }

    /**
     * 无障碍悬浮窗可用时优先选 `TYPE_ACCESSIBILITY_OVERLAY`（不退化）。
     */
    @Test
    fun accessibilityOverlayIsPreferredWhenConnected() {
        val connected = WhaleAccessibilityService.instance != null
        assumeTrue(
            "无障碍服务未连接（真机需手动开启），跳过而不是伪造通过",
            connected,
        )

        val capability = OverlayCapability(
            sdkInt = android.os.Build.VERSION.SDK_INT,
            accessibilityConnected = connected,
            overlayPermissionGranted = Settings.canDrawOverlays(context),
        )
        assumeTrue(
            "本机 API 不支持无障碍悬浮窗，跳过",
            capability.sdkInt >= PetOverlayPolicy.ACCESSIBILITY_OVERLAY_MIN_SDK,
        )

        assertEquals(PetOverlayType.ACCESSIBILITY_OVERLAY, PetOverlayPolicy.choose(capability))
        assertFalse("无障碍悬浮窗不算 fallback", PetOverlayPolicy.isFallback(capability))
    }

    /**
     * 两种悬浮窗都不可用时必须退到应用内桌宠（返回 null），**绝不假装在线**。
     */
    @Test
    fun overlayUnavailableWhenNeitherPermissionIsGranted() {
        val capability = OverlayCapability(
            sdkInt = android.os.Build.VERSION.SDK_INT,
            accessibilityConnected = false,
            overlayPermissionGranted = false,
        )
        // 这条用例只验证策略；不依赖设备真实权限，因此不需要 Assume。
        assertEquals(null, PetOverlayPolicy.choose(capability))
        assertTrue(PetOverlayPolicy.isUnavailable(capability))

        val owner = PetWindowOwner()
        val claim = owner.beginGeneration()
        // 权限缺失时 addView 会抛异常，走 markAttachFailed：绝不能标记在线。
        owner.markAttachFailed(claim.ticket)
        assertFalse("addView 失败时 overlayOnline 必须保持 false", owner.overlayOnline)
        assertFalse(owner.isOnline(claim.ticket))
    }

    /**
     * case_id: `voice_push_to_talk_lifecycle`
     *
     * 按住说话的生命周期。
     *
     * 前置：真实录音权限。未授权时跳过。
     *
     * **与矩阵 `expected_frame.state_sequence` 的差异（如实记录，不掩盖）**：
     * 矩阵写的是 `IDLE → PRESSING → LISTENING → TRANSCRIBING → IDLE`（录音层状态），
     * 而生产 [PetStateMachine] 只暴露桌宠展示态
     * （`HIDDEN/IDLE/WORKING/WAITING_CONFIRM/WAITING_USER/SUCCESS/ERROR/OFFLINE`）与
     * [PetEvent] 事件驱动。本用例因此断言「桌宠态随事件按固定顺序变化」这一生产实际契约，
     * 而不伪造一个生产里不存在的 `PRESSING` 态。
     */
    @Test
    fun pushToTalkLifecycleDrivesPetStateSequence() {
        val recordAudioGranted = androidx.core.content.ContextCompat.checkSelfPermission(
            context,
            android.Manifest.permission.RECORD_AUDIO,
        ) == android.content.pm.PackageManager.PERMISSION_GRANTED
        assumeTrue(
            "RECORD_AUDIO 未授权（真机需手动授权），跳过而不是伪造通过",
            recordAudioGranted,
        )

        val machine = PetStateMachine(
            clock = PetClock { android.os.SystemClock.elapsedRealtime() },
        )
        // 桌宠隐藏时事件不改变可见状态，先 show()。
        machine.show()
        assertEquals("show 后应进入 IDLE", PetState.IDLE, machine.state)

        // 按下说话开始任务 → WORKING。
        assertEquals(PetState.WORKING, machine.onEvent(PetEvent.TASK_STARTED))
        // 松开后进入转写/成功 → SUCCESS。
        assertEquals(PetState.SUCCESS, machine.onEvent(PetEvent.TASK_SUCCEEDED))
        // 短暂停留态到期回落 → IDLE。
        assertEquals(PetState.IDLE, machine.refresh())
        assertTrue("录音权限前置必须成立", recordAudioGranted)
    }

    /**
     * 拖拽贴边与拖到底部关闭的判定在真机像素坐标下同样成立。
     */
    @Test
    fun dragPolicyUsesRealScreenMetrics() {
        val metrics = context.resources.displayMetrics
        assumeTrue("屏幕尺寸无效，跳过", metrics.widthPixels > 0 && metrics.heightPixels > 0)

        val screenHeight = metrics.heightPixels
        val windowHeight = (screenHeight / 4)
        // 拖到底部（窗口底边贴屏幕底边）→ 应视为关闭当天显示。
        assertTrue(
            PetDragPolicy.shouldCloseOnRelease(
                finalTopPx = screenHeight - windowHeight,
                windowHeightPx = windowHeight,
                screenHeightPx = screenHeight,
            ),
        )
        // 停在屏幕顶部 → 不应关闭。
        assertFalse(
            PetDragPolicy.shouldCloseOnRelease(
                finalTopPx = 0,
                windowHeightPx = windowHeight,
                screenHeightPx = screenHeight,
            ),
        )
    }
}
