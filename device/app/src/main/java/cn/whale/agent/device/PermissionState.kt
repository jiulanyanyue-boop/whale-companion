package cn.whale.agent.device

/**
 * M11 权限状态与首次配置向导（纯逻辑）。
 *
 * 规范来源：
 * - `docs/modules/M11-oem-permissions-product.md`
 * - `docs/modules/M11-oem-permissions-development.md`
 *
 * 本文件不引用任何 `android.*` 类型：所有判定都是纯函数——输入 manufacturer/brand/sdkInt
 * 与权限布尔，输出固定枚举，因此可以直接用 JUnit4 在 JVM 上测试，不需要 Android 运行时。
 *
 * Android 侧探测（`Settings.Secure`、`Settings.canDrawOverlays`、
 * `ContextCompat.checkSelfPermission`、`PowerManager`）在
 * [PermissionGuideActivity] 里的 `AndroidPermissionProbe`。
 *
 * 权限原则（产品文档「权限原则」）：
 * - 只在用户开启对应功能时申请；
 * - 相册走系统 Photo Picker，不申请完整存储权限；
 * - 录音默认关闭；
 * - 短信发送、联系人读取、相机分别申请，不合并成一个「全部允许」。
 */

/** M11 统一权限状态，固定五值，禁止扩展（开发文档「权限状态」）。 */
enum class PermissionStatus {
    GRANTED,
    DENIED,
    NOT_APPLICABLE,
    BLOCKED_BY_OEM,
    REQUIRES_USER_ACTION,
    ;

    /** 直接展示给用户的中文状态；界面只用这里的文案，不另造「在线」假状态。 */
    val label: String
        get() = when (this) {
            GRANTED -> "已授权"
            DENIED -> "未授权（可由系统对话框直接申请）"
            NOT_APPLICABLE -> "当前不需要"
            BLOCKED_BY_OEM -> "被系统或厂商拦截（按手动路径操作）"
            REQUIRES_USER_ACTION -> "需要你手动到设置页开启"
        }
}

/**
 * 首次配置向导覆盖的权限项。
 *
 * [minimumSdk] 以下视为 `NOT_APPLICABLE`（该能力在旧系统上不存在）。
 * [systemPickerHandled] 的项（相册）由系统选择器处理，**不需要也不申请**存储权限。
 */
enum class PermissionKind(
    val displayName: String,
    val manifestPermission: String?,
    val minimumSdk: Int,
    val systemPickerHandled: Boolean = false,
) {
    ACCESSIBILITY_SERVICE("无障碍服务", null, 1),
    // `areNotificationsEnabled` 从 API 24 起可用；本应用 minSdk=26，实际永远 applicable。
    NOTIFICATIONS("通知权限", "android.permission.POST_NOTIFICATIONS", 24),
    CAMERA("相机", "android.permission.CAMERA", 1),
    RECORD_AUDIO("录音", "android.permission.RECORD_AUDIO", 1),
    READ_CONTACTS("联系人读取", "android.permission.READ_CONTACTS", 1),
    SEND_SMS("短信发送", "android.permission.SEND_SMS", 1),
    OVERLAY("悬浮窗", "android.permission.SYSTEM_ALERT_WINDOW", 23),
    PHOTO_PICKER("相册（系统 Photo Picker）", null, 19, systemPickerHandled = true),
    BATTERY_OPTIMIZATION("电池优化白名单", null, 23),
    AUTO_START("自启动", null, 1),
    BACKGROUND_RUN("后台运行", null, 1),
    ;

    /**
     * 能否用系统运行时对话框直接申请（`Activity.requestPermissions`）。
     *
     * - 相机 / 录音 / 联系人 / 短信：API 23 起是运行时权限；
     * - 通知：只有 API 33 起才有 `POST_NOTIFICATIONS`，低版本只能跳系统通知设置；
     * - 无障碍 / 悬浮窗 / 电池优化 / 自启动 / 后台运行：特殊权限，只能跳设置页。
     */
    fun isRuntimeRequestable(sdkInt: Int): Boolean = when (this) {
        CAMERA, RECORD_AUDIO, READ_CONTACTS, SEND_SMS -> sdkInt >= 23
        NOTIFICATIONS -> sdkInt >= 33
        OVERLAY, ACCESSIBILITY_SERVICE, PHOTO_PICKER, BATTERY_OPTIMIZATION, AUTO_START, BACKGROUND_RUN -> false
    }
}

/**
 * 一次权限探测的结果。
 *
 * [readable] 为 false 表示当前 Android/OEM 无法读取真实状态（例如标准 API 读不到厂商自启动开关）。
 * 这种情况下判定为 `BLOCKED_BY_OEM`，**绝不假装成已授权**。
 */
data class ProbeResult(
    val granted: Boolean,
    val readable: Boolean = true,
    val note: String = "",
) {
    companion object {
        val GRANTED: ProbeResult = ProbeResult(granted = true)
        val DENIED: ProbeResult = ProbeResult(granted = false)

        fun unreadable(note: String): ProbeResult = ProbeResult(granted = false, readable = false, note = note)
    }
}

/** 可注入的探测接口；生产实现是 `AndroidPermissionProbe`，测试可传 lambda。 */
fun interface PermissionProbe {
    fun read(kind: PermissionKind): ProbeResult
}

/**
 * 功能启用策略：决定「此刻是否需要这项权限」。
 *
 * 产品文档要求「只在用户开启对应功能时申请权限」「录音默认关闭」，
 * 所以默认值里录音 / 相机 / 联系人 / 短信全部关闭，对应项判定为 `NOT_APPLICABLE`。
 */
data class PermissionFeaturePolicy(
    val localPetMode: Boolean = false,
    val voiceEnabled: Boolean = false,
    val cameraEnabled: Boolean = false,
    val contactsEnabled: Boolean = false,
    val smsEnabled: Boolean = false,
    val photoPickerInUse: Boolean = true,
    val notificationsEnabled: Boolean = true,
    val batteryGuidanceEnabled: Boolean = true,
) {
    fun isEnabled(kind: PermissionKind): Boolean = when (kind) {
        PermissionKind.ACCESSIBILITY_SERVICE -> localPetMode
        PermissionKind.OVERLAY -> localPetMode
        PermissionKind.RECORD_AUDIO -> voiceEnabled
        PermissionKind.CAMERA -> cameraEnabled
        PermissionKind.READ_CONTACTS -> contactsEnabled
        PermissionKind.SEND_SMS -> smsEnabled
        PermissionKind.NOTIFICATIONS -> notificationsEnabled
        PermissionKind.PHOTO_PICKER -> photoPickerInUse
        PermissionKind.BATTERY_OPTIMIZATION, PermissionKind.AUTO_START, PermissionKind.BACKGROUND_RUN -> batteryGuidanceEnabled
    }

    companion object {
        /** 严格默认：录音默认关闭，相机/联系人/短信未启用即不申请。 */
        val STRICT_DEFAULTS: PermissionFeaturePolicy = PermissionFeaturePolicy(localPetMode = true)

        /** 向导展示用：全部 applicable，方便显示真实状态；申请动作仍然逐项单独进行。 */
        val GUIDE: PermissionFeaturePolicy = PermissionFeaturePolicy(
            localPetMode = true,
            voiceEnabled = true,
            cameraEnabled = true,
            contactsEnabled = true,
            smsEnabled = true,
            photoPickerInUse = true,
        )
    }
}

/** 单次打开设置页的候选描述，全部是纯数据；真正的 Intent 构造在 Activity 侧。 */
data class OemSettingsIntent(
    val label: String,
    val action: String? = null,
    val componentPackage: String? = null,
    val componentClass: String? = null,
    val requiresPackageUri: Boolean = false,
    val requiresAppPackageExtra: Boolean = false,
    val verification: OemIntentVerification = OemIntentVerification.STANDARD,
) {
    /** 显式组件跳转（厂商专属页）：包名 + 类名必须同时存在。 */
    val isExplicitComponent: Boolean get() = componentPackage != null && componentClass != null
}

/**
 * Intent 候选的验收级别。
 *
 * [VENDOR_UNVERIFIED] 表示该厂商页面路径尚未在真机上验收（M13 测试矩阵未执行），
 * 只在标准候选全部失败后尝试；即使失败也只会落到 `BLOCKED_BY_OEM` + 手动路径，不影响聊天。
 */
enum class OemIntentVerification {
    STANDARD,
    VENDOR_UNVERIFIED,
}

/**
 * 单条权限的判定结果，界面据此渲染。
 *
 * [status] 只会是 [PermissionStatus] 五值之一；[settingsIntents] 为空表示没有可打开的设置页，
 * 此时 [manualPath] 就是唯一可行的用户操作方式。
 */
data class PermissionVerdict(
    val kind: PermissionKind,
    val status: PermissionStatus,
    val profile: OemProfile,
    val settingsIntents: List<OemSettingsIntent>,
    val manualPath: String,
    val probeReadable: Boolean = true,
    val note: String = "",
) {
    /** 是否可以就地弹系统运行时对话框（不合并「全部允许」）。 */
    val canRequestInApp: Boolean get() = status == PermissionStatus.DENIED

    val canOpenSettings: Boolean get() = settingsIntents.isNotEmpty()

    /** 一行展示文案，界面只用它，不自己拼真假状态。 */
    val displayText: String
        get() = buildString {
            append(kind.displayName)
            append("：")
            append(status.label)
            if (status == PermissionStatus.BLOCKED_BY_OEM) {
                append("｜手动路径：")
                append(manualPath)
            }
        }
}

/** 首次配置向导的固定步骤键；顺序不可调换（产品文档「首次配置向导」）。 */
enum class WizardStepKey {
    ACCESSIBILITY,
    NOTIFICATIONS,
    MEDIA_AND_PERSONAL,
    OVERLAY,
    BACKGROUND,
    MODE_AND_ALLOWLIST,
}

/**
 * 向导的一步。
 *
 * [requestEachIndividually] 恒为 true（第 6 步无权限项除外）：相机/录音/联系人/短信必须
 * 分别申请，不得合并成一个「全部允许」。
 */
data class WizardStep(
    val index: Int,
    val key: WizardStepKey,
    val title: String,
    val kinds: List<PermissionKind>,
    val requestEachIndividually: Boolean = true,
)

/** `LOCAL_PET` 三项权限的就绪结果；文案与 M01 现有 `missingForLocalPet` 完全一致。 */
data class LocalPetReadiness(
    val ready: Boolean,
    val missing: List<String>,
    val statuses: Map<PermissionKind, PermissionStatus>,
    val blockedByOem: List<PermissionKind>,
) {
    val summary: String
        get() = if (ready) "本地模式权限就绪" else "缺少权限：" + missing.joinToString("、")
}

/**
 * M11 纯逻辑入口。所有方法都是纯函数：同样的输入永远得到同样的输出，不读 Android 静态字段。
 */
object PermissionState {
    /** 本地模式必需的三项，顺序与 M01 展示文案一致（无障碍、悬浮窗、录音）。 */
    fun localPetRequirements(): List<PermissionKind> = listOf(
        PermissionKind.ACCESSIBILITY_SERVICE,
        PermissionKind.OVERLAY,
        PermissionKind.RECORD_AUDIO,
    )

    /**
     * 首次配置向导的固定顺序：无障碍 → 通知 → 相机/录音/联系人/短信 → 悬浮窗 →
     * 电池优化/自启动/后台运行 → 模式和白名单。
     */
    val firstRunWizard: List<WizardStep> = listOf(
        WizardStep(
            index = 1,
            key = WizardStepKey.ACCESSIBILITY,
            title = "无障碍服务",
            kinds = listOf(PermissionKind.ACCESSIBILITY_SERVICE),
        ),
        WizardStep(
            index = 2,
            key = WizardStepKey.NOTIFICATIONS,
            title = "通知权限",
            kinds = listOf(PermissionKind.NOTIFICATIONS),
        ),
        WizardStep(
            index = 3,
            key = WizardStepKey.MEDIA_AND_PERSONAL,
            title = "相机 / 录音 / 联系人 / 短信（逐项单独申请）",
            kinds = listOf(
                PermissionKind.CAMERA,
                PermissionKind.RECORD_AUDIO,
                PermissionKind.READ_CONTACTS,
                PermissionKind.SEND_SMS,
            ),
            requestEachIndividually = true,
        ),
        WizardStep(
            index = 4,
            key = WizardStepKey.OVERLAY,
            title = "悬浮窗权限（本地桌宠需要）",
            kinds = listOf(PermissionKind.OVERLAY),
        ),
        WizardStep(
            index = 5,
            key = WizardStepKey.BACKGROUND,
            title = "电池优化 / 自启动 / 后台运行（可选引导）",
            kinds = listOf(
                PermissionKind.BATTERY_OPTIMIZATION,
                PermissionKind.AUTO_START,
                PermissionKind.BACKGROUND_RUN,
            ),
        ),
        WizardStep(
            index = 6,
            key = WizardStepKey.MODE_AND_ALLOWLIST,
            title = "模式和白名单",
            kinds = emptyList(),
            requestEachIndividually = false,
        ),
    )

    /**
     * 单条权限判定。五种状态的产生条件（固定顺序，先命中先返回）：
     *
     * 1. `sdkInt < kind.minimumSdk` → `NOT_APPLICABLE`（系统上不存在该能力）；
     * 2. 功能未启用（录音默认关闭等）→ `NOT_APPLICABLE`（不申请）；
     * 3. 相册走系统 Photo Picker → `NOT_APPLICABLE`（本应用不持有、也不申请存储权限）；
     * 4. 探测结果不可读（厂商拦截或标准 API 读不到）→ `BLOCKED_BY_OEM`；
     * 5. 探测为已授权 → `GRANTED`；
     * 6. 可由系统运行时对话框申请 → `DENIED`；
     * 7. 有可打开的设置页 → `REQUIRES_USER_ACTION`；
     * 8. 既不能申请也没有设置页 → `BLOCKED_BY_OEM`（只能按手动路径操作）。
     */
    fun evaluate(
        kind: PermissionKind,
        probeResult: ProbeResult,
        profile: OemProfile,
        features: PermissionFeaturePolicy = PermissionFeaturePolicy.STRICT_DEFAULTS,
    ): PermissionVerdict {
        val intents = profile.settingsIntents(kind)
        val manualPath = profile.manualPath(kind)
        val status = when {
            profile.sdkInt < kind.minimumSdk -> PermissionStatus.NOT_APPLICABLE
            !features.isEnabled(kind) -> PermissionStatus.NOT_APPLICABLE
            kind.systemPickerHandled -> PermissionStatus.NOT_APPLICABLE
            !probeResult.readable -> PermissionStatus.BLOCKED_BY_OEM
            probeResult.granted -> PermissionStatus.GRANTED
            kind.isRuntimeRequestable(profile.sdkInt) -> PermissionStatus.DENIED
            intents.isNotEmpty() -> PermissionStatus.REQUIRES_USER_ACTION
            else -> PermissionStatus.BLOCKED_BY_OEM
        }
        return PermissionVerdict(
            kind = kind,
            status = status,
            profile = profile,
            settingsIntents = intents,
            manualPath = manualPath,
            probeReadable = probeResult.readable,
            note = probeResult.note,
        )
    }

    /** 逐项判定一个向导步骤（第 3 步必须逐项，不合并申请）。 */
    fun evaluateStep(
        step: WizardStep,
        probe: PermissionProbe,
        profile: OemProfile,
        features: PermissionFeaturePolicy = PermissionFeaturePolicy.STRICT_DEFAULTS,
    ): List<PermissionVerdict> = step.kinds.map { kind ->
        evaluate(kind, probe.read(kind), profile, features)
    }

    /**
     * 系统设置页 Intent 全部失败时的判定：文档要求返回 `BLOCKED_BY_OEM` 并显示手动路径。
     */
    fun onSettingsLaunchFailed(
        kind: PermissionKind,
        profile: OemProfile,
        note: String = "所有候选设置页都无法在当前设备打开",
    ): PermissionVerdict = PermissionVerdict(
        kind = kind,
        status = PermissionStatus.BLOCKED_BY_OEM,
        profile = profile,
        settingsIntents = profile.settingsIntents(kind),
        manualPath = profile.manualPath(kind),
        probeReadable = true,
        note = note,
    )

    /**
     * 给 M01 收敛用的就绪判定：输入三个布尔，输出 `ready` / `missing`。
     *
     * `missing` 文案与 `ModeSwitchCoordinator.PermissionSnapshot.missingForLocalPet`
     * 完全一致（无障碍服务、悬浮窗、录音），Lead 接线时可直接替换原有实现。
     */
    fun localPetReadiness(
        accessibilityEnabled: Boolean,
        overlayGranted: Boolean,
        recordAudioGranted: Boolean,
    ): LocalPetReadiness {
        fun statusOf(granted: Boolean): PermissionStatus =
            if (granted) PermissionStatus.GRANTED else PermissionStatus.DENIED

        return LocalPetReadiness(
            ready = accessibilityEnabled && overlayGranted && recordAudioGranted,
            missing = buildList<String> {
                if (!accessibilityEnabled) add(PermissionKind.ACCESSIBILITY_SERVICE.displayName)
                if (!overlayGranted) add(PermissionKind.OVERLAY.displayName)
                if (!recordAudioGranted) add(PermissionKind.RECORD_AUDIO.displayName)
            },
            statuses = mapOf(
                PermissionKind.ACCESSIBILITY_SERVICE to statusOf(accessibilityEnabled),
                PermissionKind.OVERLAY to statusOf(overlayGranted),
                PermissionKind.RECORD_AUDIO to statusOf(recordAudioGranted),
            ),
            blockedByOem = emptyList(),
        )
    }

    /**
     * 带 OEM 上下文的就绪判定：除布尔外，把「探测不可读」算作未就绪并记录 `blockedByOem`，
     * 这样界面既能显示缺失项，也能给出厂商手动路径。
     */
    fun localPetReadiness(
        probe: PermissionProbe,
        profile: OemProfile,
        features: PermissionFeaturePolicy = PermissionFeaturePolicy.STRICT_DEFAULTS,
    ): LocalPetReadiness {
        val policy = features.copy(localPetMode = true, voiceEnabled = true)
        val verdicts = localPetRequirements().map { kind ->
            evaluate(kind, probe.read(kind), profile, policy)
        }
        // `NOT_APPLICABLE` 只可能来自「本机 API 上不存在该能力」（minSdk 门控），
        // 属于天然满足，不算缺失；`BLOCKED_BY_OEM` 与 `DENIED` 都必须算未就绪。
        fun satisfied(verdict: PermissionVerdict): Boolean =
            verdict.status == PermissionStatus.GRANTED || verdict.status == PermissionStatus.NOT_APPLICABLE

        return LocalPetReadiness(
            ready = verdicts.all { satisfied(it) },
            missing = verdicts.filterNot { satisfied(it) }.map { it.kind.displayName },
            statuses = verdicts.associate { it.kind to it.status },
            blockedByOem = verdicts
                .filter { it.status == PermissionStatus.BLOCKED_BY_OEM }
                .map { it.kind },
        )
    }
}
