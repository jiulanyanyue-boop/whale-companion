package cn.whale.agent.device

object DevicePolicy {
    private val sensitiveMarkers = listOf("alipay", "wallet", "bank", "password", "authenticator", "otp", "keepass")
    private val safeSystemPackages = setOf(
        "android", "com.android.systemui", "com.android.settings", "com.google.android.permissioncontroller",
    )

    fun isSensitive(packageName: String): Boolean {
        val value = packageName.lowercase()
        return sensitiveMarkers.any { value.contains(it) }
    }

    fun isSafeSystemPackage(packageName: String): Boolean = packageName in safeSystemPackages

    fun canControl(packageName: String, allowlist: Set<String>, blocked: Set<String>): Boolean {
        if (packageName.isBlank()) return false
        if (packageName in blocked || isSensitive(packageName)) return false
        return packageName in allowlist || isSafeSystemPackage(packageName)
    }

    /** Permit capturing the resolved default launcher without widening app controls. */
    fun canCaptureScreen(packageName: String, ownPackage: String, homePackage: String, allowlist: Set<String>, blocked: Set<String>): Boolean {
        if (packageName.isBlank() || packageName in blocked || isSensitive(packageName)) return false
        return packageName == ownPackage || (homePackage.isNotBlank() && packageName == homePackage) || canControl(packageName, allowlist, blocked)
    }
}
