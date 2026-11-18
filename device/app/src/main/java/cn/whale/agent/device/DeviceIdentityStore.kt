package cn.whale.agent.device

/**
 * M05 身份持久化边界。
 *
 * 规范来源：`docs/modules/M05-pairing-transport-development.md`「配对密钥」——
 * 主机只保存 `device_id`、公钥、SHA-256 指纹；**私钥只在 Keystore**。
 *
 * 本文件只存"可以给别人看"的三样东西 + 主机指纹。
 * **没有**任何字段能存私钥：类型上就不存在这个能力。
 *
 * 禁止项对应：`M05`「禁止内存 Map 作为唯一幂等或计数器存储」——
 * 因此这里的实现必须落盘（[SharedPreferencesIdentityStore]），
 * 接口层只约束"可持久化"，不提供纯内存的单例当真相来源。
 */
interface DeviceIdentityStore {
    /** 读出已确认的身份；未配对时返回 `null`。 */
    fun load(): ConfirmedIdentity?

    /** 落盘已确认的身份（配对第 4/5 步之后）。 */
    fun save(identity: ConfirmedIdentity)

    /** 清空身份（吊销或重新配对）。 */
    fun clear()

    /**
     * 读出已固定下来的主机指纹。
     *
     * 单独一个方法是因为「指纹变化检测」需要它独立可用：
     * 即使 [load] 因数据损坏返回 `null`，指纹比对也必须能 fail-closed。
     */
    fun hostFingerprint(): String?
}

/**
 * `SharedPreferences` 实现。
 *
 * 用 `commit()` 而不是 `apply()`：身份与主机指纹是安全判定输入，
 * 必须确认已落盘后才返回，避免进程被杀导致"配对成功但指纹没存"的窗口。
 */
class SharedPreferencesIdentityStore(
    private val prefs: android.content.SharedPreferences,
) : DeviceIdentityStore {

    override fun load(): ConfirmedIdentity? {
        val deviceId = prefs.getString(KEY_DEVICE_ID, null) ?: return null
        val publicKey = prefs.getString(KEY_PUBLIC_KEY, null) ?: return null
        val deviceFingerprint = prefs.getString(KEY_DEVICE_FINGERPRINT, null) ?: return null
        val hostFingerprint = prefs.getString(KEY_HOST_FINGERPRINT, null) ?: return null
        // 指纹形状不合规一律当作"没有有效身份"，绝不拿半截数据去信任。
        if (deviceFingerprint.length != DeviceIdentityRules.FINGERPRINT_HEX_LENGTH) return null
        if (hostFingerprint.length != DeviceIdentityRules.FINGERPRINT_HEX_LENGTH) return null
        return ConfirmedIdentity(
            deviceId = deviceId,
            devicePublicKey = publicKey,
            deviceFingerprint = deviceFingerprint,
            hostFingerprint = hostFingerprint,
            capabilitiesVersion = prefs.getString(KEY_CAPABILITIES_VERSION, "").orEmpty(),
            connectionConfig = prefs.getString(KEY_CONNECTION_CONFIG, "").orEmpty(),
            displayName = prefs.getString(KEY_DISPLAY_NAME, "").orEmpty(),
        )
    }

    override fun save(identity: ConfirmedIdentity) {
        prefs.edit()
            .putString(KEY_DEVICE_ID, identity.deviceId)
            .putString(KEY_PUBLIC_KEY, identity.devicePublicKey)
            .putString(KEY_DEVICE_FINGERPRINT, identity.deviceFingerprint)
            .putString(KEY_HOST_FINGERPRINT, identity.hostFingerprint)
            .putString(KEY_CAPABILITIES_VERSION, identity.capabilitiesVersion)
            .putString(KEY_CONNECTION_CONFIG, identity.connectionConfig)
            .putString(KEY_DISPLAY_NAME, identity.displayName)
            .commit()
    }

    override fun clear() {
        prefs.edit()
            .remove(KEY_DEVICE_ID)
            .remove(KEY_PUBLIC_KEY)
            .remove(KEY_DEVICE_FINGERPRINT)
            .remove(KEY_HOST_FINGERPRINT)
            .remove(KEY_CAPABILITIES_VERSION)
            .remove(KEY_CONNECTION_CONFIG)
            .remove(KEY_DISPLAY_NAME)
            .commit()
    }

    override fun hostFingerprint(): String? =
        prefs.getString(KEY_HOST_FINGERPRINT, null)?.takeIf {
            it.length == DeviceIdentityRules.FINGERPRINT_HEX_LENGTH
        }

    private companion object {
        const val KEY_DEVICE_ID = "device_id"
        const val KEY_PUBLIC_KEY = "public_key"
        const val KEY_DEVICE_FINGERPRINT = "device_fingerprint"
        const val KEY_HOST_FINGERPRINT = "host_fingerprint"
        const val KEY_CAPABILITIES_VERSION = "capabilities_version"
        const val KEY_CONNECTION_CONFIG = "connection_config"
        const val KEY_DISPLAY_NAME = "display_name"
    }
}
