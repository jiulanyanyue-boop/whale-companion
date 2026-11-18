package cn.whale.agent.device

/**
 * M05 配对与连接状态机（纯 JVM 可测，不依赖 Android 运行时）。
 *
 * 规范来源：
 * - `docs/modules/M05-pairing-transport-product.md`「配对流程」「断线行为」
 * - `docs/modules/M05-pairing-transport-development.md`「连接状态机」
 *
 * 两张状态机各管一件事，**不混在一起**：
 *
 * 1. 配对状态机（[PairingState]）：`UNPAIRED → PAIRING → PAIRED_OFFLINE → …`
 * 2. 连接状态机（[ConnectionState]）：`PAIRED_OFFLINE → CONNECTING → ONLINE → BACKOFF → CONNECTING`
 *
 * 退避固定 1、2、4、8、16、30 秒，最大 30 秒；收到 heartbeat 后**清零**。
 *
 * 错误码纪律：`PAIRING_REQUIRED` 只作为**状态名**使用；任何返回给主机的
 * `error_code` 都从 [CommandErrors] 里取，本文件不新增任何错误码。
 */

// ---------------------------------------------------------------------------
// 状态枚举
// ---------------------------------------------------------------------------

/** 配对状态。 */
enum class PairingState {
    /** 从未配对过。 */
    UNPAIRED,

    /** 正在配对（已输入配对码，等待主机确认）。 */
    PAIRING,

    /** 已配对但当前离线（正常状态，重连即可，**不需要重新配对**）。 */
    PAIRED_OFFLINE,

    /** 需要人工重新配对：主机身份指纹变化、租约过期或设备被吊销。**绝不自动接受**。 */
    PAIRING_REQUIRED,

    /** 已吊销：任何状态都可进入，且不可离开。 */
    REVOKED,
    ;

    /** 终态：吊销后不可恢复。 */
    val isTerminal: Boolean get() = this == REVOKED
}

/** 连接状态。 */
enum class ConnectionState {
    CONNECTING,
    ONLINE,

    /** 退避等待中（等待时间由 [BackoffPolicy] 给出）。 */
    BACKOFF,

    /** 已断开但可重连。 */
    PAIRED_OFFLINE,
    ;

    val isUsable: Boolean get() = this == ONLINE
}

/** 配对六步（顺序与文档一致，一步都不能跳）。 */
enum class PairingStep(val index: Int, val label: String) {
    /** 1. 主机生成一次性配对码 + 短期 token。 */
    HOST_ISSUED_TOKEN(1, "host-issued-token"),

    /** 2. B 扫码或手动输入配对码。 */
    DEVICE_ENTERED_CODE(2, "device-entered-code"),

    /** 3. B 生成 Android Keystore 身份密钥。 */
    DEVICE_GENERATED_IDENTITY(3, "device-generated-identity"),

    /** 4. 主机保存 device_id / 公钥 / 能力版本 / 连接配置。 */
    HOST_SAVED_IDENTITY(4, "host-saved-identity"),

    /** 5. 双方显示设备名称与指纹，用户确认后才启用。 */
    USER_CONFIRMED_FINGERPRINT(5, "user-confirmed-fingerprint"),

    /** 6. 配对完成后一次性 token 立即失效。 */
    TOKEN_REVOKED(6, "token-revoked"),
}

/**
 * 退避策略：固定 1、2、4、8、16、30 秒，最大 30 秒。
 *
 * 作为 `object` 暴露是为了让测试直接断言序列；状态由 [PairingController] 持有，
 * **不放在进程内 Timer** 里（禁止项：不得用进程内计时器作为唯一调度状态）。
 */
object BackoffPolicy {

    /** 固定退避序列（秒）。 */
    val DELAYS_SECONDS: List<Int> = listOf(1, 2, 4, 8, 16, 30)

    /** 最大退避（秒）。 */
    const val MAX_DELAY_SECONDS: Int = 30

    /** 达到上限后保持不变。 */
    fun delayFor(attempt: Int): Int {
        if (attempt <= 0) return DELAYS_SECONDS.first()
        val index = (attempt - 1).coerceAtMost(DELAYS_SECONDS.lastIndex)
        return DELAYS_SECONDS[index]
    }

    fun delayMsFor(attempt: Int): Long = delayFor(attempt).toLong() * 1000L
}

// ---------------------------------------------------------------------------
// 配对一次性 token
// ---------------------------------------------------------------------------

/**
 * 一次性配对 token。
 *
 * M05 硬要求 5 的最后一步：**配对完成后一次性 token 立即失效**。
 * 本类把「失效」做成不可逆操作，且用 [used] 标记而不是靠删除——
 * 删除会让「重复使用」退化成「找不到」，难以区分「用过了」与「从没发过」。
 */
class PairingToken(
    val token: String,
    private val expiresAtMs: Long,
) {
    private var consumed: Boolean = false

    /** 是否已经被使用过（一次性性）。 */
    val isUsed: Boolean get() = consumed

    /** 是否已过期。 */
    fun isExpired(nowMs: Long): Boolean = expiresAtMs > 0 && nowMs >= expiresAtMs

    /**
     * 消耗 token。
     *
     * @return `true` 表示本次成功消耗；`false` 表示已过期或已被用过。
     * 一旦返回过 `true`，后续任何调用都返回 `false`——这就是「立即失效」。
     */
    fun consume(nowMs: Long): Boolean {
        if (consumed) return false
        if (isExpired(nowMs)) return false
        consumed = true
        return true
    }

    /** 强制失效（配对完成第 6 步）。即使未被消耗也标记为不可用。 */
    fun revoke() {
        consumed = true
    }
}

// ---------------------------------------------------------------------------
// 指纹确认记录
// ---------------------------------------------------------------------------

/**
 * 双方显示后由用户确认的身份信息。
 *
 * 落库的话由 [DeviceIdentityStore] 负责；本类只承载比对语义。
 */
data class ConfirmedIdentity(
    val deviceId: String,
    val devicePublicKey: String,
    val deviceFingerprint: String,
    val hostFingerprint: String,
    val capabilitiesVersion: String,
    val connectionConfig: String,
    val displayName: String,
)

// ---------------------------------------------------------------------------
// 结果类型
// ---------------------------------------------------------------------------

/**
 * 一次状态推进的结果。
 *
 * @property errorCode 需要回给主机的错误码（**只会**是 [CommandErrors] 已登记的值）；
 *   `null` 表示没有拒绝。
 */
data class PairingOutcome(
    val pairingState: PairingState,
    val connectionState: ConnectionState,
    val step: PairingStep? = null,
    val accepted: Boolean = true,
    val errorCode: String? = null,
    val message: String = "",
) {
    val isRejected: Boolean get() = !accepted
}

// ---------------------------------------------------------------------------
// 控制器
// ---------------------------------------------------------------------------

/**
 * 配对与连接控制器。
 *
 * 依赖全部注入：`clock`（时间）、`keyStore`（身份）、`verifier`（验签），
 * 因此可以在 JVM 上跑完整流程，不碰 Android 运行时。
 *
 * **拒绝排队纪律**：任何拒绝都返回 `CommandErrors` 里的码，绝不新增。
 */
class PairingController(
    private val clock: SessionClock = SystemSessionClock,
    private val keyStore: DeviceKeyStore? = null,
    private val verifier: SignatureVerifier = JvmEcdsaVerifier(),
) {

    private var pairingState: PairingState = PairingState.UNPAIRED
    private var connectionState: ConnectionState = ConnectionState.PAIRED_OFFLINE
    private var currentStep: PairingStep? = null
    private var identity: PublicIdentity? = null
    private var confirmed: ConfirmedIdentity? = null
    private var token: PairingToken? = null
    private var backoffAttempt: Int = 0

    /** 有几轮退避了（测试用；不代表任何持久状态）。 */
    val backoffAttempts: Int get() = backoffAttempt

    val state: PairingState get() = pairingState

    val connection: ConnectionState get() = connectionState

    val step: PairingStep? get() = currentStep

    val currentIdentity: PublicIdentity? get() = identity

    val confirmedIdentity: ConfirmedIdentity? get() = confirmed

    // -----------------------------------------------------------------
    // 配对六步
    // -----------------------------------------------------------------

    /**
     * 第 1 步：主机下发一次性配对码与短期 token。
     *
     * 只在 `UNPAIRED` 或 `PAIRING_REQUIRED` 时允许；已配对状态下重新下发会先转回配对流程，
     * 避免「已配对的设备被静默换身份」。
     */
    fun acceptHostToken(tokenValue: String, expiresAtMs: Long): PairingOutcome {
        if (pairingState == PairingState.REVOKED) {
            return reject("pairing is revoked")
        }
        val value = tokenValue.trim()
        if (value.isEmpty()) {
            return reject("pairing token is empty", CommandErrors.SIGNATURE_REQUIRED)
        }
        token = PairingToken(value, expiresAtMs)
        pairingState = PairingState.PAIRING
        currentStep = PairingStep.HOST_ISSUED_TOKEN
        return PairingOutcome(pairingState, connectionState, currentStep)
    }

    /**
     * 第 2 步 + 第 3 步：B 输入配对码并生成 Keystore 身份密钥。
     *
     * token 必须与主机下发的一致、且未过期、且未被用过，否则拒绝。
     */
    fun submitCodeAndCreateIdentity(
        enteredCode: String,
        deviceId: String,
        displayName: String = "",
    ): PairingOutcome {
        val current = token ?: return reject("no pairing token was issued", CommandErrors.SIGNATURE_REQUIRED)
        if (pairingState != PairingState.PAIRING) {
            return reject("pairing is not in progress", CommandErrors.SIGNATURE_REQUIRED)
        }
        if (current.token != enteredCode.trim()) {
            return reject("pairing code does not match the issued token", CommandErrors.SIGNATURE_REQUIRED)
        }
        if (current.isExpired(clock.nowMs())) {
            return reject("pairing token expired", CommandErrors.EXPIRED)
        }
        if (!current.consume(clock.nowMs())) {
            // 走到这里说明已被用过（或此刻刚好过期）：一次性性必须成立。
            return reject("pairing token was already used", CommandErrors.SIGNATURE_REQUIRED)
        }
        if (deviceId.isBlank()) {
            return reject("device id is required", CommandErrors.INVALID_PAYLOAD)
        }
        val store = keyStore
            ?: return reject("device keystore is not available", CommandErrors.PERMISSION_REQUIRED)

        // 第 2 步完成：配对码已输入。
        currentStep = PairingStep.DEVICE_ENTERED_CODE
        // 第 3 步：生成 Keystore 身份密钥（私钥留在 Keystore，这里只拿到公钥与指纹）。
        identity = store.ensureIdentity(deviceId)
        currentStep = PairingStep.DEVICE_GENERATED_IDENTITY
        return PairingOutcome(pairingState, connectionState, currentStep)
    }

    /**
     * 第 4 步：主机保存 device_id / 公钥 / 能力版本 / 连接配置。
     *
     * 本机只负责「准备好要交给主机的材料」；真正落库由主机侧 `pairing.py`。
     */
    fun prepareHostRegistration(
        capabilitiesVersion: String,
        connectionConfig: String,
        displayName: String = "",
    ): PairingOutcome {
        val current = identity ?: return reject("device identity is missing", CommandErrors.SIGNATURE_REQUIRED)
        if (currentStep != PairingStep.DEVICE_GENERATED_IDENTITY) {
            return reject("device identity was not created in this pairing flow", CommandErrors.INVALID_PAYLOAD)
        }
        if (current.publicKeyEncoded.isBlank() || !current.hasWellFormedFingerprint) {
            return reject("device public key or fingerprint is malformed", CommandErrors.INVALID_PAYLOAD)
        }
        currentStep = PairingStep.HOST_SAVED_IDENTITY
        return PairingOutcome(pairingState, connectionState, currentStep, message = current.fingerprint)
    }

    /**
     * 第 5 步：双方显示设备名称与指纹，用户确认后才启用。
     *
     * `hostFingerprint` 必须非空且形状合规：**主机身份也必须固定指纹**。
     * 用户确认前不写 [confirmed]、不进入 PAIRED 可用状态。
     */
    fun confirmFingerprints(
        hostFingerprint: String,
        userAccepted: Boolean,
        capabilitiesVersion: String = "",
        connectionConfig: String = "",
        displayName: String = "",
    ): PairingOutcome {
        val current = identity ?: return reject("device identity is missing", CommandErrors.SIGNATURE_REQUIRED)
        if (currentStep != PairingStep.HOST_SAVED_IDENTITY) {
            return reject("host has not saved this device identity yet", CommandErrors.INVALID_PAYLOAD)
        }
        val host = hostFingerprint.trim().lowercase()
        if (host.isEmpty() || host.length != DeviceIdentityRules.FINGERPRINT_HEX_LENGTH) {
            return reject("host fingerprint must be a sha-256 hex digest", CommandErrors.INVALID_PAYLOAD)
        }
        if (!userAccepted) {
            // 用户没在确认页点"我确认"：配对不启用，token 也一并作废。
            token?.revoke()
            pairingState = PairingState.PAIRING_REQUIRED
            currentStep = null
            return reject("fingerprint was not confirmed by the user", CommandErrors.ACTION_REJECTED)
        }
        confirmed = ConfirmedIdentity(
            deviceId = current.deviceId,
            devicePublicKey = current.publicKeyEncoded,
            deviceFingerprint = current.fingerprint,
            hostFingerprint = host,
            capabilitiesVersion = capabilitiesVersion,
            connectionConfig = connectionConfig,
            displayName = displayName,
        )
        currentStep = PairingStep.USER_CONFIRMED_FINGERPRINT
        pairingState = PairingState.PAIRED_OFFLINE
        connectionState = ConnectionState.PAIRED_OFFLINE
        return PairingOutcome(pairingState, connectionState, currentStep)
    }

    /**
     * 第 6 步：配对完成后一次性 token **立即失效**。
     *
     * 必须在第 5 步确认之后调用；重复调用不会复活 token。
     */
    fun finalizePairing(): PairingOutcome {
        val current = token
        if (currentStep != PairingStep.USER_CONFIRMED_FINGERPRINT) {
            return reject("pairing has not been confirmed yet", CommandErrors.SIGNATURE_REQUIRED)
        }
        // 无论是否已被消耗，一律强制失效——"立即失效"必须是幂等的不可逆操作。
        current?.revoke()
        currentStep = PairingStep.TOKEN_REVOKED
        pairingState = PairingState.PAIRED_OFFLINE
        return PairingOutcome(pairingState, connectionState, currentStep)
    }

    /** 六步是否全部走完。 */
    val isPaired: Boolean
        get() = pairingState != PairingState.UNPAIRED &&
            pairingState != PairingState.PAIRING &&
            pairingState != PairingState.PAIRING_REQUIRED &&
            pairingState != PairingState.REVOKED &&
            confirmed != null

    // -----------------------------------------------------------------
    // 连接状态机
    // -----------------------------------------------------------------

    /** 开始连接：`PAIRED_OFFLINE → CONNECTING`。 */
    fun beginConnect(): PairingOutcome {
        if (!isPaired) {
            return reject("device is not paired", CommandErrors.SIGNATURE_REQUIRED)
        }
        connectionState = ConnectionState.CONNECTING
        return PairingOutcome(pairingState, connectionState)
    }

    /** 连接成功：`CONNECTING → ONLINE`，同时**清零退避**。 */
    fun markOnline(): PairingOutcome {
        if (!isPaired) return reject("device is not paired", CommandErrors.SIGNATURE_REQUIRED)
        connectionState = ConnectionState.ONLINE
        backoffAttempt = 0
        return PairingOutcome(pairingState, connectionState)
    }

    /**
     * 收到 heartbeat：退避**清零**（文档明确要求）。
     *
     * heartbeat 是"连接确实活着"的证据，因此它同时把状态推进到 ONLINE。
     */
    fun onHeartbeat(): PairingOutcome {
        if (!isPaired) return reject("device is not paired", CommandErrors.SIGNATURE_REQUIRED)
        backoffAttempt = 0
        connectionState = ConnectionState.ONLINE
        return PairingOutcome(pairingState, connectionState)
    }

    /** 连接断开：`ONLINE → BACKOFF`，返回本轮该等的毫秒数。 */
    fun onDisconnected(): PairingOutcome {
        connectionState = ConnectionState.BACKOFF
        backoffAttempt += 1
        val delay = BackoffPolicy.delayMsFor(backoffAttempt)
        return PairingOutcome(
            pairingState,
            connectionState,
            message = delay.toString(),
        )
    }

    /** 退避后的下一次连接：`BACKOFF → CONNECTING`。 */
    fun retryAfterBackoff(): PairingOutcome {
        if (pairingState == PairingState.REVOKED) {
            return reject("pairing is revoked")
        }
        connectionState = ConnectionState.CONNECTING
        return PairingOutcome(pairingState, connectionState)
    }

    /** 当前应等待的退避毫秒数（0 表示无需等待）。 */
    fun currentBackoffMs(): Long =
        if (connectionState == ConnectionState.BACKOFF) BackoffPolicy.delayMsFor(backoffAttempt) else 0L

    // -----------------------------------------------------------------
    // 身份变化与吊销
    // -----------------------------------------------------------------

    /**
     * 主机身份指纹变化：进入 `PAIRING_REQUIRED`，**绝不自动接受**。
     *
     * 这是 M05 最不能妥协的一条：指纹变化意味着对端可能被替换，
     * 必须人工重新配对，不能因为「签名能验过」就继续信任。
     */
    fun onHostFingerprintChanged(observedHostFingerprint: String): PairingOutcome {
        val expected = confirmed?.hostFingerprint
        if (expected != null && Fingerprints.matches(expected, observedHostFingerprint.trim().lowercase())) {
            return PairingOutcome(pairingState, connectionState)
        }
        pairingState = PairingState.PAIRING_REQUIRED
        connectionState = ConnectionState.PAIRED_OFFLINE
        currentStep = null
        // 身份材料一并清掉：重新配对时必须换一把新密钥。
        keyStore?.deleteIdentity()
        identity = null
        confirmed = null
        token?.revoke()
        backoffAttempt = 0
        return PairingOutcome(
            pairingState = pairingState,
            connectionState = connectionState,
            accepted = false,
            errorCode = CommandErrors.SIGNATURE_REQUIRED,
            message = "host fingerprint changed; manual re-pairing is required",
        )
    }

    /** 租约过期同样要求人工重新配对。 */
    fun onLeaseExpired(): PairingOutcome {
        pairingState = PairingState.PAIRING_REQUIRED
        connectionState = ConnectionState.PAIRED_OFFLINE
        currentStep = null
        confirmed = null
        backoffAttempt = 0
        return PairingOutcome(
            pairingState = pairingState,
            connectionState = connectionState,
            accepted = false,
            errorCode = CommandErrors.EXPIRED,
            message = "session lease expired; manual re-pairing is required",
        )
    }

    /** 任何状态 → `REVOKED`（终态）。 */
    fun revoke(): PairingOutcome {
        pairingState = PairingState.REVOKED
        connectionState = ConnectionState.PAIRED_OFFLINE
        currentStep = null
        identity = null
        confirmed = null
        token?.revoke()
        backoffAttempt = 0
        runCatching { keyStore?.deleteIdentity() }
        return PairingOutcome(pairingState, connectionState, accepted = false, message = "pairing revoked")
    }

    /**
     * 校验一个已解码信封的签名。
     *
     * 直接委托 [SignatureGate]，错误码只会是 `SIGNATURE_REQUIRED`。
     */
    fun checkEnvelopeSignature(
        envelopeFields: Map<String, Any?>,
        observedHostFingerprint: String,
    ): PairingOutcome {
        val check = SignatureGate.check(
            envelopeFields = envelopeFields,
            identity = identity,
            expectedHostFingerprint = confirmed?.hostFingerprint.orEmpty(),
            observedHostFingerprint = observedHostFingerprint,
            verifier = verifier,
        )
        if (check == SignatureCheck.HOST_FINGERPRINT_CHANGED) {
            // 指纹变化：先处置状态，再按已登记码拒绝。
            onHostFingerprintChanged(observedHostFingerprint)
            return PairingOutcome(
                pairingState = pairingState,
                connectionState = connectionState,
                accepted = false,
                errorCode = CommandErrors.SIGNATURE_REQUIRED,
                message = "host fingerprint changed",
            )
        }
        val code = SignatureGate.errorCodeFor(check)
        return PairingOutcome(
            pairingState = pairingState,
            connectionState = connectionState,
            accepted = code == null,
            errorCode = code,
            message = if (code == null) "" else check.name.lowercase(),
        )
    }

    /**
     * 计算某份信封字段的 canonical 字节（供签名侧使用）。
     *
     * 与 host `canonical_payload` 字节级一致，见 [CanonicalJson]。
     */
    fun canonicalBytes(fields: Map<String, Any?>): ByteArray = CanonicalJson.encodeUnsignedBytes(fields)

    private fun reject(message: String, code: String = CommandErrors.SIGNATURE_REQUIRED): PairingOutcome =
        PairingOutcome(
            pairingState = pairingState,
            connectionState = connectionState,
            accepted = false,
            errorCode = code,
            message = message,
        )
}
