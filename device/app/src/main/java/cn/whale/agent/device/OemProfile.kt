package cn.whale.agent.device

/**
 * M11 OEM 适配层（纯逻辑）。
 *
 * 规范来源：`docs/modules/M11-oem-permissions-development.md`「OemProfile」。
 *
 * 硬性要求：
 * - 识别顺序固定：`Build.MANUFACTURER` → `Build.BRAND` → `Build.VERSION.SDK_INT`；
 * - 只允许映射到 `STANDARD / HUAWEI / XIAOMI / OPPO / VIVO / MEIZU / UNKNOWN`；
 * - 未知品牌使用标准 Android 路径，**不猜测厂商设置 Intent**；
 * - 所有 OEM 特例集中在 [OemProfile]，命令执行器里不得出现品牌分支。
 *
 * 本文件不引用任何 `android.*` 类型：识别与路径表都是纯函数，生产侧由
 * [PermissionGuideActivity] 把 `Build.MANUFACTURER` / `Build.BRAND` / `Build.VERSION.SDK_INT`
 * 传进来，JVM 测试则直接构造任意组合。
 */

/** 固定七个 OEM 家族，禁止扩展。 */
enum class OemFamily(val displayName: String) {
    STANDARD("标准 Android"),
    HUAWEI("华为 / 荣耀"),
    XIAOMI("小米 / 红米 / POCO"),
    OPPO("OPPO / realme / 一加"),
    VIVO("vivo / iQOO"),
    MEIZU("魅族"),
    UNKNOWN("未识别厂商"),
    ;
}

/** 识别来源：用来证明「顺序固定」以及展示时说明依据。 */
enum class OemRecognitionSource {
    /** 第一环 `Build.MANUFACTURER` 命中。 */
    MANUFACTURER,

    /** 第二环 `Build.BRAND` 命中（manufacturer 未命中时才会走到这里）。 */
    BRAND,

    /** 前两环都未命中，只按 `Build.VERSION.SDK_INT` 选择版本档位；品牌按标准路径处理。 */
    SDK_INT_ONLY,
}

/** 版本档位，只由 `sdkInt` 决定；用于设置页文案与是否存在该 API。 */
enum class AndroidVersionTier(val minSdk: Int, val maxSdk: Int) {
    LEGACY(1, 28),
    MODERN(29, 30),
    S(31, 32),
    TIRAMISU(33, 34),
    VANILLA_ICE_CREAM(35, Int.MAX_VALUE),
    ;

    companion object {
        fun of(sdkInt: Int): AndroidVersionTier {
            val clamped = sdkInt.coerceAtLeast(1)
            return values().first { clamped in it.minSdk..it.maxSdk }
        }
    }
}

/**
 * 一次 OEM 识别结果。
 *
 * @property family 七个固定家族之一。
 * @property manufacturer 归一化后的 `Build.MANUFACTURER`（保留原值小写去空白）。
 * @property brand 归一化后的 `Build.BRAND`。
 * @property sdkInt 夹取到 `[1, Int.MAX_VALUE]` 的 API 级别；非法值按 1 处理。
 * @property source 命中来源，按 `MANUFACTURER → BRAND → SDK_INT` 固定顺序得出。
 */
data class OemProfile(
    val family: OemFamily,
    val manufacturer: String,
    val brand: String,
    val sdkInt: Int,
    val source: OemRecognitionSource,
) {
    val versionTier: AndroidVersionTier get() = AndroidVersionTier.of(sdkInt)

    /** 是否支持运行时权限对话框（API 23 起）。 */
    val supportsRuntimePermissionDialog: Boolean get() = sdkInt >= 23

    /** 通知权限是独立运行时权限（API 33 起才有 `POST_NOTIFICATIONS`）。 */
    val hasPostNotificationsPermission: Boolean get() = sdkInt >= 33

    /** 界面展示用的一句话，包含品牌与版本，便于用户对照手动路径。 */
    val displayName: String
        get() = buildString {
            append(family.displayName)
            if (family == OemFamily.UNKNOWN) {
                append("（manufacturer=")
                append(manufacturer.ifBlank { "空" })
                append("，brand=")
                append(brand.ifBlank { "空" })
                append("）")
            }
            append("｜Android API ")
            append(sdkInt)
        }

    /**
     * 该权限项的设置页候选，**顺序固定**：标准 AOSP 候选在前，厂商专属候选在后。
     *
     * 厂商候选全部标记 [OemIntentVerification.VENDOR_UNVERIFIED]：M13 真机矩阵尚未执行，
     * 因此调用方必须先试标准候选，全部失败后再试厂商候选，最后才降级为
     * `BLOCKED_BY_OEM` + [manualPath]。
     *
     * 未识别厂商（[OemFamily.UNKNOWN]）**只返回标准候选**，绝不猜测厂商组件。
     */
    fun settingsIntents(kind: PermissionKind): List<OemSettingsIntent> = buildList {
        addAll(standardIntents(kind))
        addAll(vendorIntents(kind))
    }

    /**
     * 手动路径文案：品牌 + 版本对应的真实菜单路径。
     *
     * 这是设置页 Intent 全部失败时唯一给用户的出路，必须具体到菜单层级，
     * 不能写成「去设置里找找」。
     */
    fun manualPath(kind: PermissionKind): String = when (kind) {
        PermissionKind.ACCESSIBILITY_SERVICE -> accessibilityPath()
        PermissionKind.NOTIFICATIONS -> notificationPath()
        PermissionKind.OVERLAY -> overlayPath()
        PermissionKind.BATTERY_OPTIMIZATION -> batteryPath()
        PermissionKind.AUTO_START -> autoStartPath()
        PermissionKind.BACKGROUND_RUN -> backgroundRunPath()
        PermissionKind.CAMERA, PermissionKind.RECORD_AUDIO, PermissionKind.READ_CONTACTS, PermissionKind.SEND_SMS ->
            runtimePermissionPath(kind)
        PermissionKind.PHOTO_PICKER ->
            "无需设置：相册由系统 Photo Picker 直接选择，本应用不申请存储权限"
    }

    // ---------------- 标准 AOSP 候选（所有家族都先用这套） ----------------

    private fun standardIntents(kind: PermissionKind): List<OemSettingsIntent> = when (kind) {
        PermissionKind.ACCESSIBILITY_SERVICE -> listOf(
            OemSettingsIntent(label = "系统无障碍设置", action = ACTION_ACCESSIBILITY_SETTINGS),
        )
        PermissionKind.NOTIFICATIONS -> buildList {
            if (sdkInt >= 26) {
                add(
                    OemSettingsIntent(
                        label = "本应用通知设置",
                        action = ACTION_APP_NOTIFICATION_SETTINGS,
                        requiresAppPackageExtra = true,
                    ),
                )
            }
            add(
                OemSettingsIntent(
                    label = "应用详情页（权限入口）",
                    action = ACTION_APPLICATION_DETAILS_SETTINGS,
                    requiresPackageUri = true,
                ),
            )
        }
        PermissionKind.OVERLAY -> if (sdkInt >= 23) {
            listOf(
                OemSettingsIntent(
                    label = "悬浮窗授权页",
                    action = ACTION_MANAGE_OVERLAY_PERMISSION,
                    requiresPackageUri = true,
                ),
            )
        } else {
            emptyList()
        }
        PermissionKind.BATTERY_OPTIMIZATION -> if (sdkInt >= 23) {
            listOf(
                OemSettingsIntent(
                    label = "电池优化列表",
                    action = ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS,
                ),
            )
        } else {
            emptyList()
        }
        // 自启动 / 后台运行在标准 Android 上没有对应设置页，只能给应用详情页。
        PermissionKind.AUTO_START, PermissionKind.BACKGROUND_RUN -> listOf(
            OemSettingsIntent(
                label = "应用详情页",
                action = ACTION_APPLICATION_DETAILS_SETTINGS,
                requiresPackageUri = true,
            ),
        )
        PermissionKind.CAMERA, PermissionKind.RECORD_AUDIO, PermissionKind.READ_CONTACTS, PermissionKind.SEND_SMS -> listOf(
            OemSettingsIntent(
                label = "应用详情页（权限入口）",
                action = ACTION_APPLICATION_DETAILS_SETTINGS,
                requiresPackageUri = true,
            ),
        )
        PermissionKind.PHOTO_PICKER -> emptyList()
    }

    // ---------------- 厂商专属候选（全部未验收，仅作兜底） ----------------

    private fun vendorIntents(kind: PermissionKind): List<OemSettingsIntent> = when (family) {
        OemFamily.HUAWEI -> huaweiIntents(kind)
        OemFamily.XIAOMI -> xiaomiIntents(kind)
        OemFamily.OPPO -> oppoIntents(kind)
        OemFamily.VIVO -> vivoIntents(kind)
        OemFamily.MEIZU -> meizuIntents(kind)
        // 未知品牌：只走标准路径，不猜厂商组件（开发文档硬性要求）。
        OemFamily.STANDARD, OemFamily.UNKNOWN -> emptyList()
    }

    private fun vendor(
        label: String,
        packageName: String,
        className: String,
    ): OemSettingsIntent = OemSettingsIntent(
        label = label,
        componentPackage = packageName,
        componentClass = className,
        verification = OemIntentVerification.VENDOR_UNVERIFIED,
    )

    /** 华为 / 荣耀：设置 → 应用启动管理、电池 → 应用耗电管理。 */
    private fun huaweiIntents(kind: PermissionKind): List<OemSettingsIntent> = when (kind) {
        PermissionKind.AUTO_START, PermissionKind.BACKGROUND_RUN -> listOf(
            vendor("华为手机管家·应用启动管理", "com.huawei.systemmanager", "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity"),
            vendor("荣耀手机管家·应用启动管理", "com.hihonor.systemmanager", "com.hihonor.systemmanager.startupmgr.ui.StartupNormalAppListActivity"),
        )
        PermissionKind.BATTERY_OPTIMIZATION -> listOf(
            vendor("华为手机管家·受保护应用", "com.huawei.systemmanager", "com.huawei.systemmanager.optimize.process.ProtectActivity"),
            vendor("荣耀手机管家·受保护应用", "com.hihonor.systemmanager", "com.hihonor.systemmanager.optimize.process.ProtectActivity"),
        )
        else -> emptyList()
    }

    /** 小米 / 红米 / POCO：安全中心 → 应用管理 → 自启动。 */
    private fun xiaomiIntents(kind: PermissionKind): List<OemSettingsIntent> = when (kind) {
        PermissionKind.AUTO_START, PermissionKind.BACKGROUND_RUN -> listOf(
            vendor("MIUI 安全中心·自启动管理", "com.miui.securitycenter", "com.miui.permcenter.autostart.AutoStartManagementActivity"),
        )
        PermissionKind.BATTERY_OPTIMIZATION -> listOf(
            vendor("MIUI 省电与电池", "com.miui.securitycenter", "com.miui.powercenter.PowerSettings"),
        )
        else -> emptyList()
    }

    /** OPPO / realme / 一加：ColorOS 自启动与耗电管理。 */
    private fun oppoIntents(kind: PermissionKind): List<OemSettingsIntent> = when (kind) {
        PermissionKind.AUTO_START, PermissionKind.BACKGROUND_RUN -> listOf(
            vendor("ColorOS 自启动管理", "com.coloros.safecenter", "com.coloros.safecenter.permission.startup.StartupAppListActivity"),
            vendor("ColorOS 12+ 自启动管理", "com.oplus.safecenter", "com.oplus.safecenter.permission.startup.StartupAppListActivity"),
        )
        PermissionKind.BATTERY_OPTIMIZATION -> listOf(
            vendor("ColorOS 耗电管理", "com.coloros.oppoguardelf", "com.coloros.powermanager.fuelgaue.PowerUsageModelActivity"),
        )
        else -> emptyList()
    }

    /** vivo / iQOO：i管家自启动与后台耗电管理。 */
    private fun vivoIntents(kind: PermissionKind): List<OemSettingsIntent> = when (kind) {
        PermissionKind.AUTO_START, PermissionKind.BACKGROUND_RUN -> listOf(
            vendor("vivo 权限管理·自启动", "com.vivo.permissionmanager", "com.vivo.permissionmanager.activity.BgStartUpManagerActivity"),
            vendor("iQOO 安全中心·自启动", "com.iqoo.secure", "com.iqoo.secure.ui.phoneoptimize.BgStartUpManager"),
        )
        PermissionKind.BATTERY_OPTIMIZATION -> listOf(
            vendor("vivo 后台高耗电", "com.vivo.abe", "com.vivo.applicationbehaviorengine.ui.ExcessivePowerManagerActivity"),
        )
        else -> emptyList()
    }

    /** 魅族：手机管家 → 权限管理 → 自启动。 */
    private fun meizuIntents(kind: PermissionKind): List<OemSettingsIntent> = when (kind) {
        PermissionKind.AUTO_START, PermissionKind.BACKGROUND_RUN -> listOf(
            vendor("Flyme 手机管家·自启动", "com.meizu.safe", "com.meizu.safe.permission.PermissionMainActivity"),
        )
        else -> emptyList()
    }

    // ---------------- 手动路径文案 ----------------

    private fun accessibilityPath(): String = "设置 → 无障碍 → 已下载的服务 → Whale Device Agent → 开启"

    private fun notificationPath(): String = when (versionTier) {
        AndroidVersionTier.LEGACY -> "设置 → 应用和通知 → Whale Device Agent → 通知 → 允许通知"
        AndroidVersionTier.MODERN, AndroidVersionTier.S ->
            "设置 → 应用 → Whale Device Agent → 通知 → 允许通知（Android 12 及以下没有独立通知权限，只能手动开关）"
        AndroidVersionTier.TIRAMISU, AndroidVersionTier.VANILLA_ICE_CREAM ->
            "设置 → 通知 → 应用通知 → Whale Device Agent → 允许通知" +
                "（Android 13 起为独立运行时权限，只能由用户手动确认）"
    }

    private fun overlayPath(): String = "设置 → 应用 → Whale Device Agent → 显示在其他应用上层（悬浮窗）→ 允许"

    private fun batteryPath(): String = when (family) {
        OemFamily.HUAWEI -> "设置 → 电池 → 应用启动管理 → Whale Device Agent → 手动管理（关闭自动启动、允许后台活动）→ 设置 → 电池 → 更多电池设置 → 关闭「休眠时始终保持网络连接」对它的限制"
        OemFamily.XIAOMI -> "设置 → 应用设置 → 应用管理 → Whale Device Agent → 省电策略 → 无限制；并在最近任务卡片下拉加锁"
        OemFamily.OPPO -> "设置 → 电池 → 应用耗电管理 → Whale Device Agent → 允许后台运行、允许自动启动"
        OemFamily.VIVO -> "设置 → 电池 → 后台耗电管理 → Whale Device Agent → 允许后台高耗电；i管家 → 应用管理 → 自启动 → 允许"
        OemFamily.MEIZU -> "手机管家 → 权限管理 → 自启动 → Whale Device Agent → 允许；设置 → 电量管理 → 关闭「待机优化」对它的限制"
        OemFamily.STANDARD, OemFamily.UNKNOWN -> "设置 → 电池 → 电池优化 → 找到 Whale Device Agent → 不优化（仅可选引导，系统不会自动修改）"
    }

    private fun autoStartPath(): String = when (family) {
        OemFamily.HUAWEI -> "手机管家 → 应用启动管理 → Whale Device Agent → 关闭「自动管理」，手动开启「允许自启动」"
        OemFamily.XIAOMI -> "安全中心 → 应用管理 → 权限 → 自启动 → Whale Device Agent → 允许"
        OemFamily.OPPO -> "手机管家 → 权限隐私 → 自启动管理 → Whale Device Agent → 允许"
        OemFamily.VIVO -> "i管家 → 应用管理 → 自启动 → Whale Device Agent → 允许"
        OemFamily.MEIZU -> "手机管家 → 权限管理 → 自启动 → Whale Device Agent → 允许"
        // 标准 Android / 未识别品牌：没有厂商自启动开关，不做任何猜测。
        OemFamily.STANDARD, OemFamily.UNKNOWN -> "标准 Android 没有厂商自启动开关：只需在「设置 → 电池 → 电池优化」中允许 Whale Device Agent 不优化；如厂商另有手机管家，请在其内部手动查找「自启动」"
    }

    private fun backgroundRunPath(): String = when (family) {
        OemFamily.HUAWEI -> "设置 → 电池 → 更多电池设置 → 应用启动管理 → Whale Device Agent → 允许后台活动"
        OemFamily.XIAOMI -> "设置 → 应用设置 → 应用管理 → Whale Device Agent → 省电策略 → 无限制"
        OemFamily.OPPO -> "设置 → 电池 → 应用耗电管理 → Whale Device Agent → 允许后台运行"
        OemFamily.VIVO -> "设置 → 电池 → 后台耗电管理 → Whale Device Agent → 允许后台高耗电"
        OemFamily.MEIZU -> "手机管家 → 权限管理 → 后台运行 → Whale Device Agent → 允许"
        OemFamily.STANDARD, OemFamily.UNKNOWN -> "设置 → 应用 → Whale Device Agent → 电池 → 不受限制（前台服务本身不是后台常驻，仅在需要时运行）"
    }

    private fun runtimePermissionPath(kind: PermissionKind): String = buildString {
        append("设置 → 应用 → Whale Device Agent → 权限 → ")
        append(kind.displayName)
        append(" → 允许")
        if (kind == PermissionKind.RECORD_AUDIO) {
            append("（录音默认关闭，只在按住说话时使用，不做常驻监听）")
        }
    }

    companion object {
        // 标准 AOSP action 字面量；与 `android.provider.Settings` 常量逐一对应，
        // 在纯 JVM 单测里也可断言，避免引入 android.jar 才能测。
        const val ACTION_ACCESSIBILITY_SETTINGS: String = "android.settings.ACCESSIBILITY_SETTINGS"
        const val ACTION_APP_NOTIFICATION_SETTINGS: String = "android.settings.APP_NOTIFICATION_SETTINGS"
        const val ACTION_APPLICATION_DETAILS_SETTINGS: String = "android.settings.APPLICATION_DETAILS_SETTINGS"
        const val ACTION_MANAGE_OVERLAY_PERMISSION: String = "android.settings.action.MANAGE_OVERLAY_PERMISSION"
        const val ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS: String =
            "android.settings.IGNORE_BATTERY_OPTIMIZATION_SETTINGS"

        /** 通知设置页要求携带的应用包名 extra（`Settings.EXTRA_APP_PACKAGE`）。 */
        const val EXTRA_APP_PACKAGE: String = "android.provider.extra.APP_PACKAGE"

        /** `MANUFACTURER` 第一环映射表。 */
        private val manufacturerMap: Map<String, OemFamily> = mapOf(
            "huawei" to OemFamily.HUAWEI,
            "honor" to OemFamily.HUAWEI,
            "xiaomi" to OemFamily.XIAOMI,
            "redmi" to OemFamily.XIAOMI,
            "poco" to OemFamily.XIAOMI,
            "oppo" to OemFamily.OPPO,
            "realme" to OemFamily.OPPO,
            "oneplus" to OemFamily.OPPO,
            "vivo" to OemFamily.VIVO,
            "iqoo" to OemFamily.VIVO,
            "meizu" to OemFamily.MEIZU,
            // 明确的标准 Android 厂商：走 AOSP 路径，不套用任何厂商特例。
            "google" to OemFamily.STANDARD,
            "motorola" to OemFamily.STANDARD,
            "sony" to OemFamily.STANDARD,
            "nokia" to OemFamily.STANDARD,
            "samsung" to OemFamily.STANDARD,
            "lge" to OemFamily.STANDARD,
            "lg" to OemFamily.STANDARD,
            "fairphone" to OemFamily.STANDARD,
            "nothing" to OemFamily.STANDARD,
            "asus" to OemFamily.STANDARD,
            "aosp" to OemFamily.STANDARD,
            "android" to OemFamily.STANDARD,
        )

        /**
         * 固定识别顺序 `MANUFACTURER → BRAND → SDK_INT`。
         *
         * 1. `manufacturer` 归一化后命中映射表 → 该家族（来源 MANUFACTURER）；
         * 2. 否则 `brand` 归一化后命中 → 该家族（来源 BRAND）；
         * 3. 都未命中 → 家族为 UNKNOWN（来源 SDK_INT_ONLY），只按 `sdkInt` 选版本档位，
         *    设置页一律走标准 AOSP 路径，**不猜测厂商 Intent**。
         *
         * 归一化：小写、去首尾空白、去掉厂商名里的常见后缀噪声前先按整体匹配，
         * 因此 "HUAWEI" / " Huawei " / "huawei" 视为同名。
         */
        fun identify(manufacturer: String?, brand: String?, sdkInt: Int): OemProfile {
            val normalizedManufacturer = normalize(manufacturer)
            val normalizedBrand = normalize(brand)
            val clampedSdk = sdkInt.coerceAtLeast(1)

            val byManufacturer = manufacturerMap[normalizedManufacturer]
            if (byManufacturer != null) {
                return OemProfile(
                    family = byManufacturer,
                    manufacturer = normalizedManufacturer,
                    brand = normalizedBrand,
                    sdkInt = clampedSdk,
                    source = OemRecognitionSource.MANUFACTURER,
                )
            }

            val byBrand = manufacturerMap[normalizedBrand]
            if (byBrand != null) {
                return OemProfile(
                    family = byBrand,
                    manufacturer = normalizedManufacturer,
                    brand = normalizedBrand,
                    sdkInt = clampedSdk,
                    source = OemRecognitionSource.BRAND,
                )
            }

            return OemProfile(
                family = OemFamily.UNKNOWN,
                manufacturer = normalizedManufacturer,
                brand = normalizedBrand,
                sdkInt = clampedSdk,
                source = OemRecognitionSource.SDK_INT_ONLY,
            )
        }

        /** 归一化：小写 + 去掉空白；`null` 视为空串。 */
        fun normalize(value: String?): String = value?.trim()?.lowercase().orEmpty()
    }
}
