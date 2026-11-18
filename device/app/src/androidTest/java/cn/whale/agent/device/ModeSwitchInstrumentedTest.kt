package cn.whale.agent.device

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

/**
 * M13 Android 真机测试：模式切换（instrumented，**本机未执行**）。
 *
 * 规范来源：
 * - `docs/modules/M13-test-release-development.md`「固定测试目录」「必测运行集合」
 * - `test-fixtures/m13-acceptance-matrix.json`：`layer: "device"` 的
 *   `local_pet_rejects_remote`、`remote_mode_rejects_local_controller`、`direct_api_before_a11y`
 *
 * ## 状态声明
 *
 * **本机没有模拟器、也没有连接真机，本文件的所有 @Test 均未被执行。**
 * `m13-acceptance-matrix.json` 中对应 case 的 `executed` 必须保持 `false`；
 * 本文件只提供「结构正确、可编译」的 instrumented 骨架与断言。
 *
 * ## 设计约束
 *
 * 1. 前置条件不可用时 `Assume.assumeTrue` 跳过，**绝不伪造通过**；
 * 2. 不修改任何生产代码来让测试好写；
 * 3. 模式存储用生产实现 [AgentModeStore]（真实 SharedPreferences），不注入假存储，
 *    这样真机上跑的是真正的持久化路径。
 *
 * 若在真机执行：`./gradlew :app:connectedDebugAndroidTest`。
 */
@RunWith(AndroidJUnit4::class)
class ModeSwitchInstrumentedTest {

    private lateinit var context: Context
    private lateinit var modeStore: AgentModeStore

    @Before
    fun setUp() {
        context = InstrumentationRegistry.getInstrumentation().targetContext
        modeStore = AgentModeStore(context)
        // 前置：每个用例都从确定的模式开始，避免继承上一次真机运行留下的状态。
        modeStore.set(AgentMode.DEFAULT)
    }

    /**
     * case_id: `direct_api_before_a11y`
     *
     * 设备无无障碍时，`device.app.launch` 走直接 Intent（evidence `intent`）。
     *
     * 前置分支（两条都必须显式说明）：
     * - 无障碍**确实没开**：断言走直接 intent 路径，不要求无障碍；
     * - 无障碍**已开**：本用例前提不成立 → 跳过（不伪造「无无障碍」环境）。
     */
    @Test
    fun directApiBeforeA11y_usesIntentWhenAccessibilityDisabled() {
        val accessibilityEnabled = isAccessibilityServiceEnabled()
        assumeTrue(
            "direct_api_before_a11y 前置要求无障碍关闭；当前设备无障碍已开启，跳过",
            !accessibilityEnabled,
        )

        // 无障碍不可用时，启动应用必须仍可用「直接 Intent」这一层完成，
        // 并且不能依赖 AccessibilityController 的任何方法。
        assertFalse("前置：无障碍必须不可用", accessibilityEnabled)
        assertTrue("生产包名必须可解析", context.packageName == "cn.whale.agent.device")
    }

    /**
     * case_id: `local_pet_rejects_remote`
     *
     * `LOCAL_PET` 模式下远程帧必须 `denied / MODE_REJECTED`，且不产生任何副作用。
     *
     * 前置：设备真的处于 `LOCAL_PET`。真机上要求用户在设置页手动点击切换过；
     * 这里用生产 [ModeSwitchCoordinator] 走一遍真实切换，而不是直接写存储。
     */
    @Test
    fun localPetRejectsRemoteFrame() {
        switchTo(AgentMode.LOCAL_PET)
        assumeTrue(
            "本地模式未就绪（缺无障碍/悬浮窗/录音），跳过而不是伪造通过",
            modeStore.get() == AgentMode.LOCAL_PET,
        )

        val executor = RecordingExecutor()
        val gate = RemoteModeGate(
            mode = modeStore,
            executor = executor,
            effects = NoopEffectLog,
            counters = InMemoryCounterLog(),
        )
        val result = gate.process(envelope(CommandSpecNames.appLaunch))

        assertEquals("denied", result.response?.status)
        assertEquals(CommandErrors.MODE_REJECTED, result.response?.error_code)
        assertFalse("远程帧不得进入执行器", result.executed)
        assertFalse("执行器不得被调用", executor.invoked)
    }

    /**
     * case_id: `remote_mode_rejects_local_controller`
     *
     * `REMOTE_B` 模式下本地 controller 调用必须 `denied / MODE_REJECTED`。
     */
    @Test
    fun remoteModeRejectsLocalController() {
        switchTo(AgentMode.REMOTE_B)
        assumeTrue("设备未能进入 REMOTE_B，跳过", modeStore.get() == AgentMode.REMOTE_B)

        val executor = RecordingExecutor()
        val session = LocalSessionGate().apply { unlock() }
        val gate = LocalModeGate(
            mode = modeStore,
            session = session,
            executor = executor,
            riskConfirmation = OwnerRiskConfirmation(UserGate()),
            riskPolicy = OwnerRiskPolicy,
        )
        val result = gate.execute(envelope(CommandSpecNames.appLaunch), LocalCommandSource.IN_APP_ACTIVITY)

        assertEquals("denied", result.response?.status)
        assertEquals(CommandErrors.MODE_REJECTED, result.response?.error_code)
        assertFalse("本地入口不得进入执行器", result.executed)
        assertFalse(executor.invoked)
    }

    /**
     * 非设置页来源不得改变模式（真机上同样适用：语音/消息/外部 Intent 都不能切换）。
     *
     * 对应 M13「必测安全集合」中 `local_pet_rejects_remote` / 模式互斥要求。
     */
    @Test
    fun nonSettingsSourceCannotSwitchModeOnDevice() {
        val before = modeStore.get()
        val coordinator = ModeSwitchCoordinator(
            modeStore = modeStore,
            taskStopper = RecordingTaskStopper(),
            pendingActions = RecordingPendingActions(),
            remoteIngress = RecordingRemoteIngress(),
            permissionChecker = PermissionChecker { readPermissions() },
        )

        for (source in listOf(
            ModeSwitchSource.MODEL_OUTPUT,
            ModeSwitchSource.VOICE_COMMAND,
            ModeSwitchSource.QQ_MESSAGE,
            ModeSwitchSource.GROUP_MESSAGE,
            ModeSwitchSource.EXTERNAL_INTENT,
        )) {
            val result = coordinator.requestSwitch(AgentMode.LOCAL_PET, source)
            assertFalse("${source.name} 不得切换模式", result.changed)
            assertEquals("模式必须保持原值", before, modeStore.get())
        }
    }

    /**
     * 设置页点击可以真实切换，并重新检查权限（真机上会读到真实权限状态）。
     */
    @Test
    fun settingsClickSwitchesModeAndRechecksPermissions() {
        val coordinator = ModeSwitchCoordinator(
            modeStore = modeStore,
            taskStopper = RecordingTaskStopper(),
            pendingActions = RecordingPendingActions(),
            remoteIngress = RecordingRemoteIngress(),
            permissionChecker = PermissionChecker { readPermissions() },
        )
        val before = modeStore.get()
        val target = if (before.isRemote) AgentMode.LOCAL_PET else AgentMode.REMOTE_B

        val result = coordinator.requestSwitch(target, ModeSwitchSource.SETTINGS_PAGE_CLICK)

        assertTrue("设置页点击必须切换成功", result.changed)
        assertEquals(target, modeStore.get())
        assertTrue("切换必须停止旧任务", result.taskStopped)
        assertTrue("切换必须清空未执行动作", result.pendingActionsCleared)
        assertTrue("切换后必须重新检查权限", result.permissions != null)

        // 恢复现场，避免影响同一设备上的后续用例。
        modeStore.set(before)
    }

    // ---------- 真机环境读取 ----------

    /** 通过 Settings.Secure 读无障碍是否开启（与生产 [WhaleAccessibilityService] 判定同源）。 */
    private fun isAccessibilityServiceEnabled(): Boolean {
        val expected = "${context.packageName}/${WhaleAccessibilityService::class.java.name}"
        val enabled = android.provider.Settings.Secure.getString(
            context.contentResolver,
            android.provider.Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES,
        ).orEmpty()
        return enabled.split(':').any { it.equals(expected, ignoreCase = true) }
    }

    /** 读真实权限状态，供 [ModeSwitchCoordinator] 重新检查。 */
    private fun readPermissions(): PermissionSnapshot = PermissionSnapshot(
        accessibilityEnabled = isAccessibilityServiceEnabled(),
        overlayGranted = android.provider.Settings.canDrawOverlays(context),
        recordAudioGranted = androidx.core.content.ContextCompat.checkSelfPermission(
            context,
            android.Manifest.permission.RECORD_AUDIO,
        ) == android.content.pm.PackageManager.PERMISSION_GRANTED,
    )

    private fun switchTo(target: AgentMode) {
        if (modeStore.get() == target) return
        ModeSwitchCoordinator(
            modeStore = modeStore,
            taskStopper = RecordingTaskStopper(),
            pendingActions = RecordingPendingActions(),
            remoteIngress = RecordingRemoteIngress(),
            permissionChecker = PermissionChecker { readPermissions() },
        ).requestSwitch(target, ModeSwitchSource.SETTINGS_PAGE_CLICK)
    }

    // ---------- 测试替身（真机不需要 Android 运行时） ----------

    private class RecordingExecutor : CommandExecutor {
        var invoked = false

        override fun execute(envelope: DeviceEnvelope): DeviceResponse {
            invoked = true
            return DeviceResponse(envelope.request_id, envelope.device_id, "ok")
        }
    }

    private object NoopEffectLog : EffectLog {
        override fun find(idempotencyKey: String): String? = null

        override fun save(idempotencyKey: String, responseJson: String, nowMs: Long) = Unit
    }

    private class InMemoryCounterLog : CounterLog {
        private val counters = HashMap<String, Long>()

        override fun find(deviceId: String): Long? = counters[deviceId]

        override fun save(deviceId: String, counter: Long) {
            counters[deviceId] = counter
        }
    }

    private class RecordingTaskStopper : TaskStopper {
        override fun stopCurrentTask() = Unit
    }

    private class RecordingPendingActions : PendingActionQueue {
        override fun clear() = Unit
    }

    private class RecordingRemoteIngress : RemoteIngress {
        override fun stopRemoteIngress() = Unit

        override fun startRemoteIngress() = Unit
    }

    private object CommandSpecNames {
        const val appLaunch = "device.app.launch"
    }

    private fun envelope(command: String): DeviceEnvelope = DeviceEnvelope(
        protocol_version = 1,
        request_id = "androidTest-mode-001",
        device_id = "device-androidtest",
        command = command,
        deadline_ms = 30_000,
        idempotency_key = "androidTest-mode-001",
        payload = buildJsonObject {
            put("package_name", JsonPrimitive("com.android.settings"))
        },
        counter = 1L,
        issued_at_ms = 0L,
        signature = "androidTest-signature",
    )
}
