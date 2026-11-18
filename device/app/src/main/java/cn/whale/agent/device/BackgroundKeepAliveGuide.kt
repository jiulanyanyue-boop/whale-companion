package cn.whale.agent.device

/**
 * M11 后台存活引导（纯逻辑）。
 *
 * 规范来源：
 * - `docs/modules/M11-oem-permissions-product.md`「首次配置向导」第 5 步、
 *   「验收：后台被系统杀死后能显示恢复建议」；
 * - `docs/modules/M11-oem-permissions-development.md`「后台策略」。
 *
 * 后台策略的硬性约束（全部体现在本文件的类型与返回值里）：
 * - 不使用 Root；
 * - 不要求 Shizuku / ADB 作为首版依赖；
 * - 后台服务只能使用 Android 合法前台服务类型；
 * - 电池优化**仅作为用户可选引导**，[BackgroundKeepAliveAction.canSilentlyApplySystemSettings]
 *   恒为 false —— 本模块没有任何静默修改系统设置的入口。
 *
 * 本文件不引用任何 `android.*` 类型：建议列表与恢复建议都是纯函数输出。
 * 只读探测与跳转在 [PermissionGuideActivity] 内的 `AndroidBatteryOptimizationReader`
 * （`PowerManager.isIgnoringBatteryOptimizations`，只读；本模块没有写入系统设置的入口）。
 */

/**
 * 后台相关的可操作项。
 *
 * [KEEP_FOREGROUND_SERVICE] 是本应用唯一合法的保活手段：`DeviceConnectionService`
 * 已声明 `foregroundServiceType="connectedDevice"`；其余各项都必须由用户手动完成。
 */
enum class BackgroundKeepAliveAction(val title: String) {
    BATTERY_OPTIMIZATION_WHITELIST("允许应用不被电池优化（可选）"),
    OEM_AUTO_START("允许厂商自启动（可选）"),
    OEM_BACKGROUND_RUN("允许后台运行 / 关闭省电限制（可选）"),
    KEEP_FOREGROUND_SERVICE("保持合法前台服务（本应用唯一保活手段）"),
    ;

    /**
     * 是否允许本应用静默修改系统设置。
     *
     * 恒为 `false`：电池优化与自启动只能由用户手工操作，本模块只提供跳转与说明。
     */
    val canSilentlyApplySystemSettings: Boolean get() = false

    /** 是否必须由用户手动完成（全部为 true，本应用从不代为修改）。 */
    val requiresUserAction: Boolean get() = true
}

/** 一项后台引导条目：动作 + 是否需要权限跳转 + 手动路径文案。 */
data class BackgroundKeepAliveStep(
    val action: BackgroundKeepAliveAction,
    val status: PermissionStatus,
    val manualPath: String,
    val settingsIntents: List<OemSettingsIntent>,
    val note: String = "",
) {
    val requiresUserAction: Boolean get() = action.requiresUserAction

    val displayText: String
        get() = buildString {
            append(action.title)
            append("：")
            append(status.label)
            if (status != PermissionStatus.GRANTED && status != PermissionStatus.NOT_APPLICABLE) {
                append("｜手动路径：")
                append(manualPath)
            }
        }
}

/** 整份后台引导计划：步骤顺序固定，且整体不包含任何静默修改入口。 */
data class BackgroundKeepAlivePlan(
    val profile: OemProfile,
    val steps: List<BackgroundKeepAliveStep>,
) {
    /** 恒为 false，作为「不静默修改系统设置」的可断言保证。 */
    val canSilentlyApplySystemSettings: Boolean
        get() = steps.any { it.action.canSilentlyApplySystemSettings }

    /** 需要用户动手的步骤（除已就绪项之外）。 */
    val pendingSteps: List<BackgroundKeepAliveStep>
        get() = steps.filter { it.status != PermissionStatus.GRANTED && it.status != PermissionStatus.NOT_APPLICABLE }

    /** 后台被杀后的恢复建议文案；不假装在线，只给出具体路径。 */
    fun recoveryHint(): String = buildString {
        append("后台服务被系统回收了。")
        if (pendingSteps.isEmpty()) {
            append("当前引导项都已就绪，请重新打开应用；若仍被回收，请检查「省电模式」与「低电量模式」是否在开启。")
            return@buildString
        }
        append("按顺序手动处理：")
        pendingSteps.forEachIndexed { index, step ->
            append("\n")
            append(index + 1)
            append(". ")
            append(step.action.title)
            append(" → ")
            append(step.manualPath)
        }
        append("\n本应用不会自动修改这些系统设置，也不会使用 Root、Shizuku 或 ADB。")
    }
}

/**
 * 电池优化的只读探测结果（由 Android 侧注入，纯 JVM 测试直接构造）。
 *
 * [readable] 为 false 表示当前系统无法读取该状态，此时判定为 `BLOCKED_BY_OEM`，
 * 而不是当作已豁免。
 */
data class BatteryOptimizationProbe(
    val ignoringBatteryOptimizations: Boolean,
    val readable: Boolean = true,
    val note: String = "",
)

/**
 * 后台引导的纯逻辑构建器。
 *
 * 输入：OEM 识别结果 + 电池优化只读探测；输出：固定顺序的引导计划。
 * 没有任何写系统设置的 API —— 这是文档「不能静默修改系统设置」的结构性保证。
 */
object BackgroundKeepAliveGuide {
    private const val BACKGROUND_SERVICE_TYPE = "connectedDevice"

    /** 本应用合法的前台服务声明说明；供界面展示与文档核对。 */
    const val FOREGROUND_SERVICE_TYPE: String = BACKGROUND_SERVICE_TYPE

    /**
     * 构建引导计划。顺序固定：电池优化 → 厂商自启动 → 厂商后台运行 → 前台服务。
     *
     * 版本门控：电池优化白名单 API 23 起才有；低版本该项判定 `NOT_APPLICABLE`，
     * 但厂商自启动/后台运行仍然给出（它们只依赖 OEM，不依赖 API）。
     */
    fun plan(
        profile: OemProfile,
        battery: BatteryOptimizationProbe,
        features: PermissionFeaturePolicy = PermissionFeaturePolicy.STRICT_DEFAULTS,
    ): BackgroundKeepAlivePlan {
        val steps = listOf(
            batteryStep(profile, battery, features),
            vendorStep(profile, BackgroundKeepAliveAction.OEM_AUTO_START),
            vendorStep(profile, BackgroundKeepAliveAction.OEM_BACKGROUND_RUN),
            foregroundStep(),
        )
        return BackgroundKeepAlivePlan(profile = profile, steps = steps)
    }

    private fun batteryStep(
        profile: OemProfile,
        battery: BatteryOptimizationProbe,
        features: PermissionFeaturePolicy,
    ): BackgroundKeepAliveStep {
        val status = when {
            profile.sdkInt < 23 -> PermissionStatus.NOT_APPLICABLE
            !features.isEnabled(PermissionKind.BATTERY_OPTIMIZATION) -> PermissionStatus.NOT_APPLICABLE
            !battery.readable -> PermissionStatus.BLOCKED_BY_OEM
            battery.ignoringBatteryOptimizations -> PermissionStatus.GRANTED
            else -> PermissionStatus.REQUIRES_USER_ACTION
        }
        return BackgroundKeepAliveStep(
            action = BackgroundKeepAliveAction.BATTERY_OPTIMIZATION_WHITELIST,
            status = status,
            manualPath = profile.manualPath(PermissionKind.BATTERY_OPTIMIZATION),
            settingsIntents = profile.settingsIntents(PermissionKind.BATTERY_OPTIMIZATION),
            note = battery.note,
        )
    }

    private fun vendorStep(
        profile: OemProfile,
        action: BackgroundKeepAliveAction,
    ): BackgroundKeepAliveStep {
        val kind = when (action) {
            BackgroundKeepAliveAction.OEM_AUTO_START -> PermissionKind.AUTO_START
            BackgroundKeepAliveAction.OEM_BACKGROUND_RUN -> PermissionKind.BACKGROUND_RUN
            else -> error("not a vendor step: $action")
        }
        val intents = profile.settingsIntents(kind)
        // 厂商自启动/后台运行没有可读的权威 API：有候选页时一律 REQUIRES_USER_ACTION，
        // 连标准设置页都没有候选时给 BLOCKED_BY_OEM + 手动路径，绝不假装已知状态。
        val status = if (intents.isNotEmpty()) {
            PermissionStatus.REQUIRES_USER_ACTION
        } else {
            PermissionStatus.BLOCKED_BY_OEM
        }
        return BackgroundKeepAliveStep(
            action = action,
            status = status,
            manualPath = profile.manualPath(kind),
            settingsIntents = intents,
            note = if (profile.family == OemFamily.UNKNOWN) {
                "未识别厂商：不猜测厂商设置页，只给标准路径"
            } else {
                ""
            },
        )
    }

    private fun foregroundStep(): BackgroundKeepAliveStep = BackgroundKeepAliveStep(
        action = BackgroundKeepAliveAction.KEEP_FOREGROUND_SERVICE,
        status = PermissionStatus.GRANTED,
        manualPath = "无需设置：$BACKGROUND_SERVICE_TYPE 前台服务按 Android 合法类型运行，仅在需要时由前台设置页启动",
        settingsIntents = emptyList(),
        note = "不申请忽略电池优化以外的任何特权：不使用 Root，不要求 Shizuku/ADB",
    )
}
