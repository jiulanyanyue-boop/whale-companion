package cn.whale.agent.device

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class DevicePolicyTest {
    @Test fun resolvedHomeCanBeCapturedWithoutGrantingAppControl() {
        assertTrue(DevicePolicy.canCaptureScreen("com.huawei.android.launcher", "own", "com.huawei.android.launcher", emptySet(), emptySet()))
        assertFalse(DevicePolicy.canControl("com.huawei.android.launcher", emptySet(), emptySet()))
        assertFalse(DevicePolicy.canCaptureScreen("com.fake.launcher", "own", "com.huawei.android.launcher", emptySet(), emptySet()))
    }

    @Test fun blockedOrUnknownHomeCannotBypassCapturePolicy() {
        assertFalse(DevicePolicy.canCaptureScreen("home", "own", "home", emptySet(), setOf("home")))
        assertFalse(DevicePolicy.canCaptureScreen("", "own", "", emptySet(), emptySet()))
        assertFalse(DevicePolicy.canCaptureScreen("my.wallet", "own", "my.wallet", emptySet(), emptySet()))
    }
    @Test fun allowlistedPackageIsAllowed() {
        assertTrue(DevicePolicy.canControl("com.example.app", setOf("com.example.app"), emptySet()))
    }

    @Test fun packageOutsideAllowlistIsDenied() {
        assertFalse(DevicePolicy.canControl("com.example.other", setOf("com.example.app"), emptySet()))
    }

    @Test fun sensitivePackageIsAlwaysDenied() {
        assertFalse(DevicePolicy.canControl("com.example.wallet", setOf("com.example.wallet"), emptySet()))
    }

    @Test fun systemPackageIsAllowed() {
        assertTrue(DevicePolicy.canControl("com.android.settings", emptySet(), emptySet()))
    }
}
