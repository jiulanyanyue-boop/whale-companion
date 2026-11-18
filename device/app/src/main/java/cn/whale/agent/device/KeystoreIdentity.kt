package cn.whale.agent.device

import java.security.MessageDigest

/**
 * M05 设备身份：Keystore `EC P-256` 密钥的抽象边界 + 指纹计算 + 验签判定。
 *
 * 规范来源：`docs/modules/M05-pairing-transport-development.md`「配对密钥」
 * 与 `M05-pairing-transport-product.md`「配对流程」。
 *
 * 三条硬约束（本文件用**接口设计**把它们变成「写不出来」而不是「别写错」）：
 *
 * 1. **私钥只存在 Android Keystore，绝不导出**：
 *    [DeviceKeyStore] 接口里**没有任何** `privateKey` / `getEncoded()` / `exportKey()`
 *    形状的方法。生产实现 [AndroidKeystoreIdentity] 只持 alias 与 `KeyStore` 句柄，
 *    签名在 Keystore 内部完成，调用方永远拿不到私钥材料。
 * 2. **主机只保存 `device_id`、公钥、SHA-256 指纹**：[PublicIdentity] 就是这三样。
 * 3. **指纹是公钥的 SHA-256**：`sha256(publicKeyEncoded)`，小写十六进制、无分隔符。
 *
 * 本文件里所有**判定逻辑**（[Fingerprints]、[SignatureGate]）都是纯 JVM、可脱离
 * Android 单测；只有 [AndroidKeystoreIdentity] 依赖 Android 运行时，
 * 而它不会被任何单测实例化（单测依赖接口 + JVM 替身）。
 */
object DeviceIdentityRules {

    /** 算法固定：Android Keystore `EC`。 */
    const val KEY_ALGORITHM: String = "EC"

    /** 曲线固定 P-256（secp256r1 / prime256v1）。 */
    const val CURVE: String = "secp256r1"

    /** Keystore 里的固定 alias（首版只支持一个身份密钥）。 */
    const val KEY_ALIAS: String = "whale-device-identity-v1"

    /** 签名算法固定：SHA256withECDSA。 */
    const val SIGNATURE_ALGORITHM: String = "SHA256withECDSA"

    /** 指纹摘要算法固定 SHA-256。 */
    const val FINGERPRINT_ALGORITHM: String = "SHA-256"

    /** 指纹长度固定：SHA-256 = 64 个十六进制字符。 */
    const val FINGERPRINT_HEX_LENGTH: Int = 64

    /** Android Keystore 的 Provider 名（平台内置，字符串固定）。 */
    const val ANDROID_KEYSTORE_PROVIDER: String = "AndroidKeyStore"
}

/**
 * 公钥身份：主机侧唯一保存的三样东西。
 *
 * `publicKeyEncoded` 是公钥编码字节（X.509 `SubjectPublicKeyInfo`）的 Base64 文本；
 * `fingerprint` 是 [Fingerprints.of] 算出的小写十六进制。
 * **没有私钥字段**，也没有任何能推导私钥的字段。
 */
data class PublicIdentity(
    val deviceId: String,
    val publicKeyEncoded: String,
    val fingerprint: String,
) {
    /** 指纹形状是否合规：64 位小写十六进制。 */
    val hasWellFormedFingerprint: Boolean
        get() = fingerprint.length == DeviceIdentityRules.FINGERPRINT_HEX_LENGTH &&
            fingerprint.all { it in '0'..'9' || it in 'a'..'f' }
}

/**
 * Keystore 身份的抽象。
 *
 * 刻意**不提供**任何读取私钥的方法：没有 `getPrivateKey()`、没有 `export()`、
 * 没有返回 `PrivateKey` 的函数。签名走 [sign]，实现内部调用 Keystore。
 */
interface DeviceKeyStore {
    /** 身份是否已经生成。 */
    fun exists(): Boolean

    /** 生成身份密钥（已存在则幂等返回）；返回含指纹的公钥身份。 */
    fun ensureIdentity(deviceId: String): PublicIdentity

    /** 当前公钥身份；未生成时返回 `null`。 */
    fun identity(): PublicIdentity?

    /**
     * 用身份私钥对**已经 canonical 化的字节**签名。
     *
     * 传入的必须是 [CanonicalJson.encodeUnsignedBytes] 的产物，不是原始 JSON 文本——
     * 签名必须覆盖规范化后的字节，两侧才可能验过。
     *
     * @return 签名（生产为 ASN.1 DER 的 Base64）；**绝不返回私钥**
     */
    fun sign(canonicalPayload: ByteArray): String

    /** 删除身份（重新配对时调用）。 */
    fun deleteIdentity()
}

/**
 * 生产实现：Android Keystore `EC P-256`。
 *
 * **私钥永不导出**：本类只持有 alias 与 `KeyStore` 句柄；签名通过
 * `getEntry` 拿到 `PrivateKey` 句柄后立即交给 Keystore 完成，
 * 不调用 `getEncoded()`、不写文件、不进日志、不返回给任何调用方。
 */
class AndroidKeystoreIdentity(
    private val keystoreProvider: String = DeviceIdentityRules.ANDROID_KEYSTORE_PROVIDER,
    private val alias: String = DeviceIdentityRules.KEY_ALIAS,
) : DeviceKeyStore {

    private fun loadStore(): java.security.KeyStore =
        java.security.KeyStore.getInstance(keystoreProvider).apply { load(null) }

    override fun exists(): Boolean = runCatching {
        loadStore().containsAlias(alias)
    }.getOrDefault(false)

    override fun identity(): PublicIdentity? = runCatching {
        val store = loadStore()
        if (!store.containsAlias(alias)) return@runCatching null
        val certificate = store.getCertificate(alias) ?: return@runCatching null
        val encodedBytes = certificate.publicKey.encoded
        PublicIdentity(
            deviceId = "",
            publicKeyEncoded = encodeBase64(encodedBytes),
            fingerprint = Fingerprints.of(encodedBytes),
        )
    }.getOrNull()

    override fun ensureIdentity(deviceId: String): PublicIdentity {
        val store = loadStore()
        if (!store.containsAlias(alias)) {
            val generator = java.security.KeyPairGenerator.getInstance(
                DeviceIdentityRules.KEY_ALGORITHM,
                keystoreProvider,
            )
            val spec = android.security.keystore.KeyGenParameterSpec.Builder(
                alias,
                android.security.keystore.KeyProperties.PURPOSE_SIGN or
                    android.security.keystore.KeyProperties.PURPOSE_VERIFY,
            )
                .setAlgorithmParameterSpec(
                    java.security.spec.ECGenParameterSpec(DeviceIdentityRules.CURVE),
                )
                .setDigests(android.security.keystore.KeyProperties.DIGEST_SHA256)
                .setUserAuthenticationRequired(false)
                .build()
            generator.initialize(spec)
            // 只生成，不读取私钥。私钥材料从未离开 Keystore。
            generator.generateKeyPair()
        }
        val current = identity()
            ?: throw IllegalStateException("keystore identity was not created")
        return PublicIdentity(
            deviceId = deviceId,
            publicKeyEncoded = current.publicKeyEncoded,
            fingerprint = current.fingerprint,
        )
    }

    override fun sign(canonicalPayload: ByteArray): String {
        val store = loadStore()
        val entry = store.getEntry(alias, null) as? java.security.KeyStore.PrivateKeyEntry
            ?: throw IllegalStateException("device identity key is missing")
        // 签名在 Keystore 内部完成：privateKey 只是句柄，其字节从不外流。
        val signature = java.security.Signature.getInstance(DeviceIdentityRules.SIGNATURE_ALGORITHM)
        signature.initSign(entry.privateKey)
        signature.update(canonicalPayload)
        return encodeBase64(signature.sign())
    }

    override fun deleteIdentity() {
        runCatching {
            val store = loadStore()
            if (store.containsAlias(alias)) store.deleteEntry(alias)
        }
    }

    private fun encodeBase64(bytes: ByteArray): String =
        java.util.Base64.getEncoder().encodeToString(bytes)
}

/**
 * 指纹计算：纯函数、纯 JVM；host 侧 `pairing.py` 用同一规则。
 *
 * `fingerprint == sha256(publicKeyBytes)` 的小写十六进制。
 */
object Fingerprints {

    /** 计算指纹。`publicKeyBytes` 是公钥的编码字节（不含任何私钥材料）。 */
    fun of(publicKeyBytes: ByteArray): String {
        val digest = MessageDigest.getInstance(DeviceIdentityRules.FINGERPRINT_ALGORITHM)
        val hash = digest.digest(publicKeyBytes)
        val out = StringBuilder(hash.size * 2)
        for (byte in hash) {
            val value = byte.toInt() and 0xFF
            out.append(HEX[value ushr 4])
            out.append(HEX[value and 0x0F])
        }
        return out.toString()
    }

    /** 指纹的恒定时间比较：避免时序差异泄露信息。 */
    fun matches(expected: String, actual: String): Boolean {
        if (expected.length != actual.length) return false
        var diff = 0
        for (index in expected.indices) {
            diff = diff or (expected[index].code xor actual[index].code)
        }
        return diff == 0
    }

    private val HEX = "0123456789abcdef".toCharArray()
}

/**
 * 签名校验判定：**判定逻辑**纯 JVM，具体验签实现由调用方注入。
 *
 * 真正的 `Signature.verify` 只出现在 [SignatureVerifier] 的实现里。
 */
enum class SignatureCheck {
    /** 签名有效且指纹匹配。 */
    OK,

    /** `signature` 字段为空。 */
    MISSING,

    /** 签名格式或密码学校验失败。 */
    INVALID,

    /** 主机身份指纹变化：必须人工重新配对，绝不自动接受。 */
    HOST_FINGERPRINT_CHANGED,
}

/** 验签实现抽象（生产用 JDK `Signature`，本机 JVM 也能真跑）。 */
fun interface SignatureVerifier {
    fun verify(publicKeyEncoded: String, canonicalPayload: ByteArray, signature: String): Boolean
}

/**
 * 用 JDK `java.security.Signature` 做的 ECDSA 验签实现。
 *
 * 纯 JVM，本机就能真跑；生产与单测共用同一份实现，避免「测试过了生产不一样」。
 */
class JvmEcdsaVerifier : SignatureVerifier {
    override fun verify(
        publicKeyEncoded: String,
        canonicalPayload: ByteArray,
        signature: String,
    ): Boolean = runCatching {
        val decoded = java.util.Base64.getDecoder().decode(publicKeyEncoded)
        val keySpec = java.security.spec.X509EncodedKeySpec(decoded)
        val factory = java.security.KeyFactory.getInstance(DeviceIdentityRules.KEY_ALGORITHM)
        val publicKey = factory.generatePublic(keySpec)
        val verifier = java.security.Signature.getInstance(DeviceIdentityRules.SIGNATURE_ALGORITHM)
        verifier.initVerify(publicKey)
        verifier.update(canonicalPayload)
        verifier.verify(java.util.Base64.getDecoder().decode(signature))
    }.getOrDefault(false)
}

object SignatureGate {
    /**
     * 固定判定顺序（M05 硬要求 3/4）：
     *
     * 1. `signature` 为空 → [SignatureCheck.MISSING]（对 `SIGNATURE_REQUIRED`）
     * 2. 主机指纹变了 → [SignatureCheck.HOST_FINGERPRINT_CHANGED]（进入 `PAIRING_REQUIRED` 状态）
     * 3. 密码学校验失败 → [SignatureCheck.INVALID]（同样对 `SIGNATURE_REQUIRED`）
     * 4. 全过 → [SignatureCheck.OK]
     *
     * 顺序固定很重要：**指纹变化必须排在密码学校验之前判定**，
     * 否则「换了主机身份但伪造了签名」会被当成普通签名失败，掩盖真实的中间人事件。
     */
    fun check(
        envelopeFields: Map<String, Any?>,
        identity: PublicIdentity?,
        expectedHostFingerprint: String,
        observedHostFingerprint: String,
        verifier: SignatureVerifier,
    ): SignatureCheck {
        val signature = envelopeFields[CanonicalJson.SIGNATURE_FIELD] as? String
        if (signature.isNullOrBlank()) return SignatureCheck.MISSING
        if (identity == null) return SignatureCheck.INVALID

        if (!Fingerprints.matches(expectedHostFingerprint, observedHostFingerprint)) {
            return SignatureCheck.HOST_FINGERPRINT_CHANGED
        }

        val canonical = CanonicalJson.encodeUnsignedBytes(envelopeFields)
        val valid = runCatching {
            verifier.verify(identity.publicKeyEncoded, canonical, signature)
        }.getOrDefault(false)
        return if (valid) SignatureCheck.OK else SignatureCheck.INVALID
    }

    /**
     * 校验结果 → **已登记**错误码。
     *
     * **不新增错误码**（M05 硬要求 8）：`PAIRING_REQUIRED` 只是**状态名**，
     * 作为 `error_code` 一律复用 [CommandErrors.SIGNATURE_REQUIRED]——
     * 对主机而言「你的身份/签名不再被接受」与「没带签名」同属一类拒绝。
     */
    fun errorCodeFor(check: SignatureCheck): String? = when (check) {
        SignatureCheck.OK -> null
        SignatureCheck.MISSING,
        SignatureCheck.INVALID,
        SignatureCheck.HOST_FINGERPRINT_CHANGED -> CommandErrors.SIGNATURE_REQUIRED
    }
}
