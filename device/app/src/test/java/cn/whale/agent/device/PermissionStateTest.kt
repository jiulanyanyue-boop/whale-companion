package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M11 权限状态固定测试（纯 JVM，JUnit4，不依赖 Android 运行时）。
 *
 * 覆盖 `docs/modules/M11-oem-permissions-development.md`：
 * - 统一五值 `GRANTED / DENIED / NOT_APPLICABLE / BLOCKED_BY_OEM / REQUIRES_USER_ACTION`；
 * - 系统设置页 Intent 失败 → `BLOCKED_BY_OEM` + 手动路径；
 * - 向导顺序固定 6 步；
 * - 录音默认关闭、相册走 Photo Picker、相机/录音/联系人/短信分别申请；
 * - 后台不静默修改系统设置。
 */
class PermissionStateTest {

    private val standard = OemProfile.identify("Google", "google", 34)

    /** 可编排的探测替身：按 kind 返回预设结果，未预设的默认 DENIED。 */
    private fun probe(vararg results: Pair<PermissionKind, ProbeResult>): PermissionProbe {
        val map = results.toMap()
        return PermissionProbe { kind -> map[kind] ?: ProbeResult.DENIED }
    }

    // ---------- 五值枚举本身 ----------

    @Test
    fun statusEnumHasExactlyFiveValues() {
        assertEquals(5, PermissionStatus.values().size)
        assertEquals(
            setOf("GRANTED", "DENIED", "NOT_APPLICABLE", "BLOCKED_BY_OEM", "REQUIRES_USER_ACTION"),
            PermissionStatus.values().map { it.name }.toSet(),
        )
        for (status in PermissionStatus.values()) {
            assertTrue("$status 必须有中文文案", status.label.isNotBlank())
        }
    }

    // ---------- 逐条状态产生条件 ----------

    @Test
    fun grantedWhenProbeSaysGranted() {
        val verdict = PermissionState.evaluate(
            kind = PermissionKind.CAMERA,
            probeResult = ProbeResult.GRANTED,
            profile = standard,
            features = PermissionFeaturePolicy(cameraEnabled = true),
        )
        assertEquals(PermissionStatus.GRANTED, verdict.status)
        assertFalse(verdict.canRequestInApp)
        assertTrue("已授权项仍可打开设置页复核", verdict.canOpenSettings)
    }

    @Test
    fun deniedWhenRuntimePermissionIsRequestable() {
        val verdict = PermissionState.evaluate(
            kind = PermissionKind.CAMERA,
            probeResult = ProbeResult.DENIED,
            profile = standard,
            features = PermissionFeaturePolicy(cameraEnabled = true),
        )
        assertEquals(PermissionStatus.DENIED, verdict.status)
        assertTrue("运行时权限可就地申请", verdict.canRequestInApp)
    }

    @Test
    fun requiresUserActionForSpecialPermissions() {
        // 无障碍不是运行时权限，只能跳设置页手动开。
        val accessibility = PermissionState.evaluate(
            kind = PermissionKind.ACCESSIBILITY_SERVICE,
            probeResult = ProbeResult.DENIED,
            profile = standard,
            features = PermissionFeaturePolicy(localPetMode = true),
        )
        assertEquals(PermissionStatus.REQUIRES_USER_ACTION, accessibility.status)
        assertFalse("无障碍不得就地申请", accessibility.canRequestInApp)
        assertTrue("无障碍必须有设置页候选", accessibility.canOpenSettings)

        // 悬浮窗同理（API 23+）。
        val overlay = PermissionState.evaluate(
            kind = PermissionKind.OVERLAY,
            probeResult = ProbeResult.DENIED,
            profile = standard,
            features = PermissionFeaturePolicy(localPetMode = true),
        )
        assertEquals(PermissionStatus.REQUIRES_USER_ACTION, overlay.status)
        assertFalse(overlay.canRequestInApp)
    }

    @Test
    fun notApplicableWhenFeatureDisabled() {
        // 录音默认关闭：未启用语音时判定 NOT_APPLICABLE，且不申请。
        val strict = PermissionFeaturePolicy.STRICT_DEFAULTS
        for (kind in listOf(
            PermissionKind.RECORD_AUDIO,
            PermissionKind.CAMERA,
            PermissionKind.READ_CONTACTS,
            PermissionKind.SEND_SMS,
        )) {
            val verdict = PermissionState.evaluate(kind, ProbeResult.DENIED, standard, strict)
            assertEquals("$kind 默认不应申请", PermissionStatus.NOT_APPLICABLE, verdict.status)
            assertFalse("$kind 不得就地申请", verdict.canRequestInApp)
        }
    }

    @Test
    fun notApplicableBelowMinimumSdk() {
        val legacy = OemProfile.identify(null, null, 22)
        val notification = PermissionState.evaluate(
            kind = PermissionKind.NOTIFICATIONS,
            probeResult = ProbeResult.DENIED,
            profile = legacy,
            features = PermissionFeaturePolicy.GUIDE,
        )
        assertEquals(PermissionStatus.NOT_APPLICABLE, notification.status)

        val overlay = PermissionState.evaluate(
            kind = PermissionKind.OVERLAY,
            probeResult = ProbeResult.DENIED,
            profile = legacy,
            features = PermissionFeaturePolicy.GUIDE,
        )
        assertEquals(PermissionStatus.NOT_APPLICABLE, overlay.status)
    }

    @Test
    fun blockedByOemWhenProbeUnreadable() {
        // 厂商自启动没有可读 API：一律 BLOCKED_BY_OEM，绝不假装已授权或未授权。
        val xiaomi = OemProfile.identify("Xiaomi", "Redmi", 34)
        val verdict = PermissionState.evaluate(
            kind = PermissionKind.AUTO_START,
            probeResult = ProbeResult.unreadable("no public api"),
            profile = xiaomi,
            features = PermissionFeaturePolicy.GUIDE,
        )
        assertEquals(PermissionStatus.BLOCKED_BY_OEM, verdict.status)
        assertFalse(verdict.probeReadable)
        assertTrue("必须给手动路径", verdict.manualPath.isNotBlank())
        assertTrue("展示文案必须含手动路径", verdict.displayText.contains("手动路径"))
    }

    @Test
    fun photoPickerIsNotApplicableOnAnyApi() {
        // 相册走系统 Photo Picker：无论哪个 API，本应用都不申请存储权限。
        for (sdk in listOf(19, 26, 34, 36)) {
            val profile = OemProfile.identify(null, null, sdk)
            val verdict = PermissionState.evaluate(
                kind = PermissionKind.PHOTO_PICKER,
                probeResult = ProbeResult.DENIED,
                profile = profile,
                features = PermissionFeaturePolicy.GUIDE,
            )
            assertEquals("sdk=$sdk", PermissionStatus.NOT_APPLICABLE, verdict.status)
            assertTrue("sdk=$sdk 相册不得有设置页候选", verdict.settingsIntents.isEmpty())
        }
    }

    @Test
    fun settingsLaunchFailureReturnsBlockedByOemWithManualPath() {
        for (manufacturer in listOf("HUAWEI", "Xiaomi", "OPPO", "vivo", "MEIZU", "Google", "UnknownVendor")) {
            val profile = OemProfile.identify(manufacturer, manufacturer, 34)
            for (kind in PermissionKind.values()) {
                val verdict = PermissionState.onSettingsLaunchFailed(kind, profile)
                assertEquals("$manufacturer/$kind", PermissionStatus.BLOCKED_BY_OEM, verdict.status)
                assertTrue("$manufacturer/$kind 手动路径不得为空", verdict.manualPath.isNotBlank())
                assertEquals(profile, verdict.profile)
            }
        }
    }

    // ---------- 权限原则 ----------

    @Test
    fun photoPickerDoesNotRequestStoragePermission() {
        val verdict = PermissionState.evaluate(
            kind = PermissionKind.PHOTO_PICKER,
            probeResult = ProbeResult.DENIED,
            profile = standard,
            features = PermissionFeaturePolicy.GUIDE,
        )
        assertEquals(PermissionStatus.NOT_APPLICABLE, verdict.status)
        assertFalse("相册不得就地申请", verdict.canRequestInApp)
        assertTrue("相册不得有设置页候选", verdict.settingsIntents.isEmpty())
        assertTrue(PermissionKind.PHOTO_PICKER.systemPickerHandled)
        assertEquals("相册不得声明清单权限", null, PermissionKind.PHOTO_PICKER.manifestPermission)
    }

    @Test
    fun recordAudioIsOffByDefault() {
        val strict = PermissionFeaturePolicy.STRICT_DEFAULTS
        assertFalse("录音默认关闭", strict.voiceEnabled)
        // 即使探测说已授权，若功能未开启也仍是 NOT_APPLICABLE（不因历史授权而自动启用）。
        assertEquals(
            PermissionStatus.NOT_APPLICABLE,
            PermissionState.evaluate(PermissionKind.RECORD_AUDIO, ProbeResult.GRANTED, standard, strict).status,
        )
    }

    @Test
    fun mediaAndPersonalPermissionsAreSeparateKinds() {
        val kinds = listOf(
            PermissionKind.CAMERA,
            PermissionKind.RECORD_AUDIO,
            PermissionKind.READ_CONTACTS,
            PermissionKind.SEND_SMS,
        )
        assertEquals("四项必须是四个独立枚举值", 4, kinds.toSet().size)
        assertEquals(
            "各自独立的清单权限，不能合并",
            setOf(
                "android.permission.CAMERA",
                "android.permission.RECORD_AUDIO",
                "android.permission.READ_CONTACTS",
                "android.permission.SEND_SMS",
            ),
            kinds.mapNotNull { it.manifestPermission }.toSet(),
        )

        // 逐项判定：只开相机时，其余三项仍为 NOT_APPLICABLE（不存在「全部允许」）。
        val onlyCamera = PermissionFeaturePolicy(cameraEnabled = true)
        val step = PermissionState.firstRunWizard.first { it.key == WizardStepKey.MEDIA_AND_PERSONAL }
        assertTrue("第 3 步必须逐项申请", step.requestEachIndividually)
        val verdicts = PermissionState.evaluateStep(
            step = step,
            probe = probe(
                PermissionKind.CAMERA to ProbeResult.GRANTED,
                PermissionKind.RECORD_AUDIO to ProbeResult.DENIED,
                PermissionKind.READ_CONTACTS to ProbeResult.DENIED,
                PermissionKind.SEND_SMS to ProbeResult.DENIED,
            ),
            profile = standard,
            features = onlyCamera,
        )
        assertEquals(4, verdicts.size)
        assertEquals(PermissionStatus.GRANTED, verdicts.first { it.kind == PermissionKind.CAMERA }.status)
        for (kind in kinds - PermissionKind.CAMERA) {
            assertEquals("$kind 未启用即不申请", PermissionStatus.NOT_APPLICABLE, verdicts.first { it.kind == kind }.status)
        }
    }

    // ---------- 向导顺序 ----------

    @Test
    fun wizardOrderIsFixed() {
        val keys = PermissionState.firstRunWizard.map { it.key }
        assertEquals(
            listOf(
                WizardStepKey.ACCESSIBILITY,
                WizardStepKey.NOTIFICATIONS,
                WizardStepKey.MEDIA_AND_PERSONAL,
                WizardStepKey.OVERLAY,
                WizardStepKey.BACKGROUND,
                WizardStepKey.MODE_AND_ALLOWLIST,
            ),
            keys,
        )
        assertEquals("索引必须连续递增", listOf(1, 2, 3, 4, 5, 6), PermissionState.firstRunWizard.map { it.index })
        assertEquals("第 1 步只能是无障碍", listOf(PermissionKind.ACCESSIBILITY_SERVICE), PermissionState.firstRunWizard.first().kinds)
        assertEquals(
            "第 6 步是模式与白名单，不含权限项",
            emptyList<PermissionKind>(),
            PermissionState.firstRunWizard.last().kinds,
        )
    }

    @Test
    fun wizardStepEchoesRealStatusPerItem() {
        val profile = OemProfile.identify("HUAWEI", "HUAWEI", 33)
        val batteryStep = PermissionState.firstRunWizard.first { it.key == WizardStepKey.BACKGROUND }
        assertEquals(
            listOf(
                PermissionKind.BATTERY_OPTIMIZATION,
                PermissionKind.AUTO_START,
                PermissionKind.BACKGROUND_RUN,
            ),
            batteryStep.kinds,
        )
        val verdicts = PermissionState.evaluateStep(
            batteryStep,
            probe(
                PermissionKind.BATTERY_OPTIMIZATION to ProbeResult.GRANTED,
                PermissionKind.AUTO_START to ProbeResult.unreadable("vendor only"),
                PermissionKind.BACKGROUND_RUN to ProbeResult.unreadable("vendor only"),
            ),
            profile,
            PermissionFeaturePolicy.GUIDE,
        )
        assertEquals(PermissionStatus.GRANTED, verdicts[0].status)
        assertEquals(PermissionStatus.BLOCKED_BY_OEM, verdicts[1].status)
        assertEquals(PermissionStatus.BLOCKED_BY_OEM, verdicts[2].status)
        // BLOCKED_BY_OEM 的项必须带手动路径，界面才能给出具体菜单。
        for (verdict in verdicts.filter { it.status == PermissionStatus.BLOCKED_BY_OEM }) {
            assertTrue(verdict.displayText.contains(verdict.manualPath))
        }
    }

    // ---------- 给 M01 收敛的 API ----------

    @Test
    fun localPetReadinessMatchesM01Contract() {
        val ready = PermissionState.localPetReadiness(true, true, true)
        assertTrue(ready.ready)
        assertTrue("就绪时缺失项为空", ready.missing.isEmpty())
        assertEquals("本地模式权限就绪", ready.summary)

        val missingAll = PermissionState.localPetReadiness(false, false, false)
        assertFalse(missingAll.ready)
        // 文案必须与 M01 `PermissionSnapshot.missingForLocalPet` 完全一致，Lead 才能直接替换。
        assertEquals(listOf("无障碍服务", "悬浮窗", "录音"), missingAll.missing)
        assertEquals("缺少权限：无障碍服务、悬浮窗、录音", missingAll.summary)

        val onlyOverlay = PermissionState.localPetReadiness(true, false, true)
        assertFalse(onlyOverlay.ready)
        assertEquals(listOf("悬浮窗"), onlyOverlay.missing)

        assertEquals(setOf(PermissionKind.ACCESSIBILITY_SERVICE, PermissionKind.OVERLAY, PermissionKind.RECORD_AUDIO), PermissionState.localPetRequirements().toSet())
    }

    @Test
    fun readinessWithProbeTreatsUnreadableAsNotReady() {
        val profile = OemProfile.identify("Xiaomi", "Redmi", 34)
        val readiness = PermissionState.localPetReadiness(
            probe = probe(
                PermissionKind.ACCESSIBILITY_SERVICE to ProbeResult.GRANTED,
                PermissionKind.OVERLAY to ProbeResult.GRANTED,
                PermissionKind.RECORD_AUDIO to ProbeResult.unreadable("blocked"),
            ),
            profile = profile,
        )
        assertFalse("不可读不得算就绪", readiness.ready)
        assertEquals(listOf(PermissionKind.RECORD_AUDIO), readiness.blockedByOem)
        assertEquals(listOf("录音"), readiness.missing)
    }

    // ---------- 后台策略 ----------

    @Test
    fun backgroundPlanNeverSilentlyChangesSystemSettings() {
        val profile = OemProfile.identify("Xiaomi", "Redmi", 34)
        val plan = BackgroundKeepAliveGuide.plan(
            profile = profile,
            battery = BatteryOptimizationProbe(ignoringBatteryOptimizations = false),
        )
        assertFalse("不得存在静默修改系统设置的入口", plan.canSilentlyApplySystemSettings)
        for (step in plan.steps) {
            assertFalse("${step.action} 不得静默修改系统设置", step.action.canSilentlyApplySystemSettings)
            assertTrue("${step.action} 必须由用户手动操作", step.requiresUserAction)
        }
        assertTrue("引导计划必须有内容", plan.steps.isNotEmpty())
    }

    @Test
    fun backgroundPlanStatusesFollowRealProbe() {
        val profile = OemProfile.identify("Xiaomi", "Redmi", 34)
        val granted = BackgroundKeepAliveGuide.plan(
            profile = profile,
            battery = BatteryOptimizationProbe(ignoringBatteryOptimizations = true),
        )
        assertEquals(
            PermissionStatus.GRANTED,
            granted.steps.first { it.action == BackgroundKeepAliveAction.BATTERY_OPTIMIZATION_WHITELIST }.status,
        )

        val unreadable = BackgroundKeepAliveGuide.plan(
            profile = profile,
            battery = BatteryOptimizationProbe(ignoringBatteryOptimizations = false, readable = false),
        )
        assertEquals(
            PermissionStatus.BLOCKED_BY_OEM,
            unreadable.steps.first { it.action == BackgroundKeepAliveAction.BATTERY_OPTIMIZATION_WHITELIST }.status,
        )
    }

    @Test
    fun backgroundRecoveryHintGivesConcretePaths() {
        val profile = OemProfile.identify("HUAWEI", "HUAWEI", 34)
        val plan = BackgroundKeepAliveGuide.plan(
            profile = profile,
            battery = BatteryOptimizationProbe(ignoringBatteryOptimizations = false),
        )
        val hint = plan.recoveryHint()
        assertTrue("恢复建议必须说明后台被回收", hint.contains("回收"))
        assertTrue("必须含具体手动路径", hint.contains("手机管家") || hint.contains("电池"))
        assertTrue("必须声明不使用 Root/Shizuku/ADB", hint.contains("Root"))
        assertTrue("有未完成项时必须给出步骤", plan.pendingSteps.isNotEmpty())
    }

    @Test
    fun standardFamilyBackgroundPlanHasNoVendorGuesses() {
        val profile = OemProfile.identify("Google", "google", 34)
        val plan = BackgroundKeepAliveGuide.plan(
            profile = profile,
            battery = BatteryOptimizationProbe(ignoringBatteryOptimizations = false),
        )
        for (step in plan.steps) {
            assertTrue(
                "STANDARD 不得猜测厂商页: ${step.action}",
                step.settingsIntents.none { it.verification == OemIntentVerification.VENDOR_UNVERIFIED },
            )
        }
        // 标准 Android 没有厂商自启动页：只能给应用详情页 + 明确说明「没有厂商自启动开关」，
        // 绝不假装 GRANTED。
        val autoStart = plan.steps.first { it.action == BackgroundKeepAliveAction.OEM_AUTO_START }
        assertEquals(PermissionStatus.REQUIRES_USER_ACTION, autoStart.status)
        assertTrue(autoStart.manualPath.contains("没有厂商自启动开关"))
        assertTrue("必须仍有可打开的标准设置页", autoStart.settingsIntents.isNotEmpty())
    }

    @Test
    fun foregroundServiceStepNeedsNoSettings() {
        val profile = OemProfile.identify("Google", "google", 34)
        val plan = BackgroundKeepAliveGuide.plan(profile, BatteryOptimizationProbe(false))
        val step = plan.steps.first { it.action == BackgroundKeepAliveAction.KEEP_FOREGROUND_SERVICE }
        assertEquals(PermissionStatus.GRANTED, step.status)
        assertTrue("前台服务不需要跳设置页", step.settingsIntents.isEmpty())
        assertEquals("connectedDevice", BackgroundKeepAliveGuide.FOREGROUND_SERVICE_TYPE)
    }

    // ---------- 不新增错误码 ----------

    @Test
    fun permissionStatesAreNotErrorCodes() {
        // M11 只产出权限状态枚举；错误码仍然只来自 CommandErrors（禁止新增）。
        for (status in PermissionStatus.values()) {
            assertFalse(
                "权限状态 ${status.name} 不得混入 CommandErrors",
                CommandErrors.isDeclared(status.name),
            )
        }
    }
}
