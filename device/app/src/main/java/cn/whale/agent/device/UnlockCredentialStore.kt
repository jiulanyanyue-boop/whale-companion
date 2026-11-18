package cn.whale.agent.device

import android.content.Context
import android.util.Base64
import java.nio.charset.StandardCharsets
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * 手机本地自动解锁凭据。
 *
 * 凭据只在手机上配置，使用 Android Keystore 生成的 AES-GCM 密钥加密后落盘。
 * 它不会进入命令 payload、日志、截图或主机回包；卸载应用或清除应用数据会使其失效。
 */
object UnlockCredentialStore {
    private const val PREFS = "local_unlock_credentials"
    private const val KEY_ALIAS = "whale.local.unlock.aes"
    private const val CIPHERTEXT = "ciphertext"
    private const val IV = "iv"
    private const val ENABLED = "enabled"
    private const val TYPE = "type"

    data class Credential(val value: String, val type: Type)
    enum class Type { PIN, PASSWORD }

    fun isConfigured(context: Context): Boolean =
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getBoolean(ENABLED, false) && load(context) != null

    fun save(context: Context, credential: Credential) {
        require(credential.value.isNotEmpty()) { "unlock credential must not be empty" }
        val key = getOrCreateKey()
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.ENCRYPT_MODE, key)
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        check(prefs.edit()
            .putString(CIPHERTEXT, Base64.encodeToString(cipher.doFinal(credential.value.toByteArray(StandardCharsets.UTF_8)), Base64.NO_WRAP))
            .putString(IV, Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
            .putString(TYPE, credential.type.name)
            .putBoolean(ENABLED, true)
            .commit()) { "could not save unlock credential" }
    }

    fun clear(context: Context) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().clear().commit()
    }

    fun load(context: Context): Credential? = runCatching {
        val prefs = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        if (!prefs.getBoolean(ENABLED, false)) return null
        val ciphertext = prefs.getString(CIPHERTEXT, null) ?: return null
        val iv = prefs.getString(IV, null) ?: return null
        val type = Type.valueOf(prefs.getString(TYPE, Type.PIN.name) ?: Type.PIN.name)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
        cipher.init(Cipher.DECRYPT_MODE, getOrCreateKey(), GCMParameterSpec(128, Base64.decode(iv, Base64.DEFAULT)))
        Credential(String(cipher.doFinal(Base64.decode(ciphertext, Base64.DEFAULT)), StandardCharsets.UTF_8), type)
    }.getOrNull()

    private fun getOrCreateKey(): SecretKey {
        val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (keyStore.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance("AES", "AndroidKeyStore")
        generator.init(android.security.keystore.KeyGenParameterSpec.Builder(
            KEY_ALIAS,
            android.security.keystore.KeyProperties.PURPOSE_ENCRYPT or android.security.keystore.KeyProperties.PURPOSE_DECRYPT,
        ).setBlockModes(android.security.keystore.KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(android.security.keystore.KeyProperties.ENCRYPTION_PADDING_NONE)
            .build())
        return generator.generateKey()
    }
}
