package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M11 OEM 固定测试（纯 JVM，不依赖 Android 运行时）。
 *
 * 对应 `docs/modules/M11-oem-permissions-development.md`「OemProfile」：
 * 识别顺序固定 `MANUFACTURER → BRAND → SDK_INT`；只允许七个家族；
 * 未知品牌走标准 Android 路径，不猜测厂商设置 Intent。
 */
class OemProfileTest {

    private fun identify(manufacturer: String?, brand: String?, sdk: Int = 34): OemProfile =
        OemProfile.identify(manufacturer, brand, sdk)

    // ---------- 第一环：MANUFACTURER ----------

    @Test
    fun manufacturerIsFirstRing() {
        val cases = mapOf(
            "HUAWEI" to OemFamily.HUAWEI,
            "HONOR" to OemFamily.HUAWEI,
            "Xiaomi" to OemFamily.XIAOMI,
            "OPPO" to OemFamily.OPPO,
            "vivo" to OemFamily.VIVO,
            "MEIZU" to OemFamily.MEIZU,
            "Google" to OemFamily.STANDARD,
        )
        for ((raw, expected) in cases) {
            val profile = identify(raw, brand = null, sdk = 34)
            assertEquals("manufacturer=$raw", expected, profile.family)
            assertEquals("manufacturer=$raw", OemRecognitionSource.MANUFACTURER, profile.source)
        }
    }

    @Test
    fun manufacturerWinsOverBrand() {
        // 小米设备常见品牌是 Redmi，但 manufacturer 固定为 Xiaomi：第一环命中就停。
        val profile = identify(manufacturer = "Xiaomi", brand = "Redmi", sdk = 33)
        assertEquals(OemFamily.XIAOMI, profile.family)
        assertEquals(OemRecognitionSource.MANUFACTURER, profile.source)

        // 反向冲突：manufacturer 是标准厂商、brand 是厂商名 —— 仍然以 manufacturer 为准。
        val conflict = identify(manufacturer = "Google", brand = "HUAWEI", sdk = 31)
        assertEquals(OemFamily.STANDARD, conflict.family)
        assertEquals(OemRecognitionSource.MANUFACTURER, conflict.source)
    }

    // ---------- 第二环：BRAND ----------

    @Test
    fun brandIsSecondRingWhenManufacturerUnknown() {
        val profile = identify(manufacturer = "alps", brand = "Redmi", sdk = 30)
        assertEquals(OemFamily.XIAOMI, profile.family)
        assertEquals(OemRecognitionSource.BRAND, profile.source)

        val onePlus = identify(manufacturer = null, brand = "OnePlus", sdk = 29)
        assertEquals(OemFamily.OPPO, onePlus.family)
        assertEquals(OemRecognitionSource.BRAND, onePlus.source)

        val iqoo = identify(manufacturer = "", brand = "iQOO", sdk = 31)
        assertEquals(OemFamily.VIVO, iqoo.family)
        assertEquals(OemRecognitionSource.BRAND, iqoo.source)
    }

    // ---------- 第三环：SDK_INT ----------

    @Test
    fun unknownVendorFallsBackToSdkOnlyAndStandardPaths() {
        val profile = identify(manufacturer = "SomeVendor", brand = "SomeBrand", sdk = 29)
        assertEquals(OemFamily.UNKNOWN, profile.family)
        assertEquals(OemRecognitionSource.SDK_INT_ONLY, profile.source)
        assertEquals(AndroidVersionTier.MODERN, profile.versionTier)

        // 未知品牌不猜厂商设置 Intent：所有权限项都只有标准 AOSP 候选。
        for (kind in PermissionKind.values()) {
            val intents = profile.settingsIntents(kind)
            assertTrue(
                "UNKNOWN 不得出现厂商专属候选: $kind",
                intents.none { it.verification == OemIntentVerification.VENDOR_UNVERIFIED },
            )
            assertTrue("UNKNOWN 不得出现显式组件: $kind", intents.none { it.isExplicitComponent })
        }
    }

    @Test
    fun sdkIntIsClampedAndTiered() {
        val cases = mapOf(
            0 to AndroidVersionTier.LEGACY,
            -5 to AndroidVersionTier.LEGACY,
            26 to AndroidVersionTier.LEGACY,
            28 to AndroidVersionTier.LEGACY,
            29 to AndroidVersionTier.MODERN,
            30 to AndroidVersionTier.MODERN,
            31 to AndroidVersionTier.S,
            32 to AndroidVersionTier.S,
            33 to AndroidVersionTier.TIRAMISU,
            34 to AndroidVersionTier.TIRAMISU,
            35 to AndroidVersionTier.VANILLA_ICE_CREAM,
            36 to AndroidVersionTier.VANILLA_ICE_CREAM,
        )
        for ((raw, tier) in cases) {
            val profile = identify(manufacturer = null, brand = null, sdk = raw)
            assertEquals("sdk=$raw", tier, profile.versionTier)
        }
        // 非法 sdkInt 夹取到 1，不允许出现 0 或负数导致档位落空。
        assertEquals(1, identify(manufacturer = null, brand = null, sdk = -5).sdkInt)
    }

    // ---------- 归一化 ----------

    @Test
    fun namesAreNormalizedCaseAndWhitespaceInsensitive() {
        for (raw in listOf("HUAWEI", "huawei", " Huawei ", "HuaWei\t")) {
            assertEquals("raw=$raw", OemFamily.HUAWEI, identify(raw, null, 31).family)
        }
        // null / 空串 / 纯空白都不能命中任何家族，只能落到 UNKNOWN。
        for (raw in listOf(null, "", "   ", "\u0000")) {
            assertEquals("raw=$raw", OemFamily.UNKNOWN, identify(raw, null, 31).family)
        }
    }

    // ---------- 七个家族与版本门控 ----------

    @Test
    fun sevenFamiliesOnly() {
        assertEquals(7, OemFamily.values().size)
        assertEquals(
            setOf("STANDARD", "HUAWEI", "XIAOMI", "OPPO", "VIVO", "MEIZU", "UNKNOWN"),
            OemFamily.values().map { it.name }.toSet(),
        )
    }

    @Test
    fun versionGatesOnNotificationsAndOverlay() {
        val low = identify("Google", "google", 26)
        assertFalse("API 26 没有 POST_NOTIFICATIONS", low.hasPostNotificationsPermission)
        assertTrue("API 26 仍有运行时权限对话框", low.supportsRuntimePermissionDialog)

        val high = identify("Google", "google", 33)
        assertTrue(high.hasPostNotificationsPermission)

        // API 23 以下没有悬浮窗授权页候选。
        val legacy = identify("Google", "google", 22)
        assertTrue("API 22 没有悬浮窗候选", legacy.settingsIntents(PermissionKind.OVERLAY).isEmpty())
        assertTrue("API 22 没有电池优化候选", legacy.settingsIntents(PermissionKind.BATTERY_OPTIMIZATION).isEmpty())
    }

    @Test
    fun runtimeDialogSupportStartsAtApi23() {
        assertTrue(identify(null, null, 23).supportsRuntimePermissionDialog)
        assertFalse(identify(null, null, 22).supportsRuntimePermissionDialog)
    }

    // ---------- 候选顺序与验收级别 ----------

    @Test
    fun standardCandidatesComeBeforeVendorCandidates() {
        val profile = identify("Xiaomi", "Redmi", 34)
        val intents = profile.settingsIntents(PermissionKind.AUTO_START)
        assertTrue("必须至少有一个标准候选", intents.isNotEmpty())
        assertEquals(OemIntentVerification.STANDARD, intents.first().verification)

        val vendor = profile.settingsIntents(PermissionKind.AUTO_START)
            .filter { it.verification == OemIntentVerification.VENDOR_UNVERIFIED }
        assertTrue("小米必须有自启动兜底候选", vendor.isNotEmpty())
        assertTrue(
            "厂商候选必须排在标准候选之后",
            intents.indexOfFirst { it.verification == OemIntentVerification.VENDOR_UNVERIFIED } >=
                intents.indexOfLast { it.verification == OemIntentVerification.STANDARD },
        )
        for (candidate in vendor) {
            assertTrue("厂商候选必须是显式组件", candidate.isExplicitComponent)
            assertNotNull(candidate.componentPackage)
            assertNotNull(candidate.componentClass)
        }
    }

    @Test
    fun standardFamilyHasNoVendorCandidates() {
        val profile = identify("Google", "google", 34)
        for (kind in PermissionKind.values()) {
            assertTrue(
                "STANDARD 不得出现厂商候选: $kind",
                profile.settingsIntents(kind).none { it.verification == OemIntentVerification.VENDOR_UNVERIFIED },
            )
        }
    }

    @Test
    fun everyVendorFamilyProvidesAutoStartFallback() {
        val families = mapOf(
            "HUAWEI" to OemFamily.HUAWEI,
            "Xiaomi" to OemFamily.XIAOMI,
            "OPPO" to OemFamily.OPPO,
            "vivo" to OemFamily.VIVO,
            "MEIZU" to OemFamily.MEIZU,
        )
        for ((manufacturer, family) in families) {
            val profile = identify(manufacturer, null, 33)
            assertEquals(family, profile.family)
            val vendor = profile.settingsIntents(PermissionKind.AUTO_START)
                .filter { it.verification == OemIntentVerification.VENDOR_UNVERIFIED }
            assertTrue("$family 必须有自启动兜底候选", vendor.isNotEmpty())
            assertTrue("$family 必须有后台运行兜底候选", profile.settingsIntents(PermissionKind.BACKGROUND_RUN).isNotEmpty())
        }
    }

    // ---------- 手动路径 ----------

    @Test
    fun manualPathsAreBrandAndVersionAware() {
        val huawei = identify("HUAWEI", "HUAWEI", 34)
        val vivo = identify("vivo", "vivo", 34)
        val standard = identify("Google", "google", 30)

        assertTrue("华为路径必须含手机管家", huawei.manualPath(PermissionKind.AUTO_START).contains("手机管家"))
        assertTrue("vivo 路径必须含 i管家或电池", vivo.manualPath(PermissionKind.BATTERY_OPTIMIZATION).contains("后台耗电"))
        assertTrue(
            "标准 Android 必须明确没有厂商自启动开关",
            standard.manualPath(PermissionKind.AUTO_START).contains("没有厂商自启动开关"),
        )
    }

    @Test
    fun manualPathsAreNeverBlank() {
        for (manufacturer in listOf("HUAWEI", "Xiaomi", "OPPO", "vivo", "MEIZU", "Google", "UnknownVendor")) {
            val profile = identify(manufacturer, manufacturer, 34)
            for (kind in PermissionKind.values()) {
                assertTrue(
                    "$manufacturer/$kind 的手动路径不得为空",
                    profile.manualPath(kind).isNotBlank(),
                )
            }
        }
    }

    @Test
    fun photoPickerManualPathSaysNoStoragePermission() {
        val profile = identify("Google", "google", 34)
        val path = profile.manualPath(PermissionKind.PHOTO_PICKER)
        assertTrue(path.contains("Photo Picker"))
        assertTrue(path.contains("不申请存储权限"))
    }

    @Test
    fun notificationPathMentionsApi33PermissionOnlyOn33Plus() {
        val modern = identify("Google", "google", 33)
        assertTrue(modern.manualPath(PermissionKind.NOTIFICATIONS).contains("Android 13"))

        // API 31/32 还没有 POST_NOTIFICATIONS 运行时权限：不能把 13 的文案套上去。
        val s = identify("Google", "google", 31)
        val sPath = s.manualPath(PermissionKind.NOTIFICATIONS)
        assertFalse("API 31 不得声称有独立通知权限", sPath.contains("独立运行时权限"))
        assertTrue(sPath.contains("Whale Device Agent"))
    }

    // ---------- 展示文案 ----------

    @Test
    fun displayNameContainsBrandAndApi() {
        val unknown = identify("SomeVendor", "SomeBrand", 29)
        assertTrue(unknown.displayName.contains("未识别厂商"))
        // 名称统一归一化为小写，展示同一份归一化值，便于和手动路径对照。
        assertTrue(unknown.displayName.contains("somevendor"))
        assertTrue(unknown.displayName.contains("29"))

        val huawei = identify("HUAWEI", "HUAWEI", 34)
        assertTrue(huawei.displayName.contains("华为"))
        assertFalse("已识别厂商不必列 manufacturer", huawei.displayName.contains("manufacturer="))
    }
}
