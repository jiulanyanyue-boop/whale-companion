package cn.whale.agent.device

/**
 * M05 设备端配对流程编排（**纯 JVM、可脱离 Android 单测**）。
 *
 * 本文件把 [PairingController] 的六步按界面实际操作顺序串起来，并补上界面必须的两件事：
 *
 * 1. **入口校验**：扫码 / 粘贴 / 手输三条通道的输入全部走 [QrPayloadRules] 同一套校验，
 *    不会出现「某一入口少校验一项」的漏洞；
 * 2. **一次性 token 的跨会话拒绝**：`PairingController` 的 [PairingToken] 只管本次会话，
 *    用户关掉页面再重扫同一张二维码会得到**新的** controller——因此本流程把
 *    「已失效 token 的指纹」记进 [ConsumedTokenStore]，**重放一律拒绝**。
 *
 * ## 六步落地位置（一步都不少，且不跳步）
 *
 * | 步 | [PairingStep] | 落地方法 | 界面动作 |
 * |---|---|---|---|
 * | 1 | `HOST_ISSUED_TOKEN` | [startWithQr] / [startWithManual] 内部调用 `acceptHostToken` | 用户扫码或输入 |
 * | 2 | `DEVICE_ENTERED_CODE` | [submitCode] | 「生成身份并继续」 |
 * | 3 | `DEVICE_GENERATED_IDENTITY` | [submitCode]（内部 `ensureIdentity`，私钥留在 Keystore） | 同上 |
 * | 4 | `HOST_SAVED_IDENTITY` | [prepareRegistration] | 「主机已保存，去确认」 |
 * | 5 | `USER_CONFIRMED_FINGERPRINT` | [confirm]（**必须** `userAccepted=true`，绝不自动过） | 确认页「我确认」 |
 * | 6 | `TOKEN_REVOKED` | [finalize]（内部 `revoke` + 写入 [ConsumedTokenStore]） | 成功后立即落库 |
 *
 * ## 明确不做的事
 *
 * - **不**自动确认指纹：第 5 步永远需要一次显式 `userAccepted=true`；
 * - **不**在失败时假装成功：任何拒绝都原样带上 `PairingController` 的 `error_code` 与原因；
 * - **不**落盘任何私钥：本文件只接触 [PublicIdentity] 与 [ConfirmedIdentity]；
 * - **不**自己发明错误码：一律复用 [CommandErrors]。
 */
class PairingFlow(
    private val controller: PairingController = PairingController(),
    private val consumedTokens: ConsumedTokenStore = InMemoryConsumedTokenStore(),
    private val clock: SessionClock = SystemSessionClock,
    /**
     * 把第 4、5 步真正送到主机的通道。
     *
     * `null`（默认）= **纯本地模式**：只推进本地状态机，不发网络请求。
     * 这样 480+ 个已有单测不必联网，「没有主机的场景」也仍可跑通。
     * 真实配对由 `PairingActivity` 注入 `PairingClient` 的实现，届时每一步都要求主机确认。
     */
    private val remote: PairingRemote? = null,
) {

    /**
     * 远程配对通道的抽象。
     *
     * 为什么单独抽一层、而不是在 `PairingFlow` 里直接 new `PairingClient`：
     * `PairingFlow` 是**纯 JVM 可测**的逻辑层（482 个用例跑在无网络环境里），
     * 直接依赖 OkHttp 会让整个测试套件必须起网络。抽成接口后测试可注入假实现，
     * 生产由 `PairingActivity` 注入真实的 `PairingClient`。
     */
    interface PairingRemote {
        /** 第 4 步：让主机保存设备身份。 */
        fun register(
            deviceUrl: String,
            token: String,
            pairingCode: String,
            deviceId: String,
            publicKeyEncoded: String,
            deviceFingerprint: String,
            capabilitiesVersion: String,
            connectionConfig: String,
            displayName: String,
        ): RemoteResult

        /** 第 5 步：用户确认指纹后让主机启用。 */
        fun confirm(
            deviceUrl: String,
            token: String,
            deviceId: String,
            userAccepted: Boolean,
            observedHostFingerprint: String,
        ): RemoteResult

        /** 第 6 步：让一次性 token 失效。 */
        fun finalize(deviceUrl: String, token: String)
    }

    /** 远程调用结果。`ok=false` 时 `message` 是**主机给出的**人话原因。 */
    data class RemoteResult(
        val ok: Boolean,
        val message: String = "",
        val hostFingerprint: String = "",
        val pairingState: String = "",
        val hostIdentityIsPlaceholder: Boolean = false,
    )

    /** 本次会话里已解析通过的主机地址（第 5 步写连接配置时用）。 */
    var hostUrl: String = ""
        private set

    /** 二维码里带来的主机指纹；手输路径没有它，必须由用户逐字核对后填写。 */
    var pinnedHostFingerprint: String = ""

    /**
     * 主机 TLS 证书的 SHA-256 指纹（来自二维码 \	fp\）；手输路径为空。
     *
     * 用途：证书固定。主机用自签名证书时 Android 默认不信任，
     * 需要这个值才能连上（详见 \CertificatePinning\）。
     */
    private var registrationCode: String = ""

    var qrCertificateFingerprint: String = ""
        internal set

    /** 本次会话是否走明文 `ws://`（仅回环允许），界面必须显式警告。 */
    var insecureTransport: Boolean = false
        private set

    /** 当前真实状态快照（界面只读这一份，不自己猜）。 */
    fun snapshot(): PairingFlowSnapshot = PairingFlowSnapshot(
        state = controller.state,
        step = controller.step,
        deviceId = controller.currentIdentity?.deviceId.orEmpty(),
        deviceFingerprint = controller.currentIdentity?.fingerprint.orEmpty(),
        hostFingerprint = pinnedHostFingerprint,
        displayName = controller.confirmedIdentity?.displayName.orEmpty(),
        hostUrl = hostUrl,
        isPaired = controller.isPaired,
        insecureTransport = insecureTransport,
    )

    // -----------------------------------------------------------------
    // 第 1 步：主机下发一次性配对码（设备侧接收）
    // -----------------------------------------------------------------

    /**
     * 扫码 / 粘贴入口：解析二维码内容，然后登记主机下发的一次性 token。
     *
     * 拒绝分三类，各有明确文案：格式不合法（[QrPayloadRules] 给原因）、
     * 该 token 已被用过（一次性性）、controller 自己拒绝（状态不对）。
     */
    var qrHostSigningPublicKey: String = ""
        private set

    fun startWithQr(rawQrText: String?): PairingFlowOutcome {
        return when (val parsed = QrPayloadRules.parse(rawQrText)) {
            is QrPayloadResult.Invalid -> reject(parsed.reason, CommandErrors.INVALID_PAYLOAD)
            is QrPayloadResult.Valid -> beginSession(
                token = parsed.payload.token,
                host = parsed.payload.host,
                hostFingerprint = parsed.payload.hostFingerprint,
                insecure = parsed.payload.insecureTransport,
                // 证书指纹随二维码一起来：主机自签名证书要靠它才能被信任
                // （详见 `CertificatePinning`）。手输路径没有这个值。
                certFingerprint = parsed.payload.tlsCertificateFingerprint,
                pairingCode = parsed.payload.pairingCode,
                signingPublicKey = parsed.payload.hostSigningPublicKey,
            )
        }
    }

    /**
     * 手动输入入口：主机地址 + 一次性 token 两个框。
     *
     * 与扫码**共用** [QrPayloadRules] 的 host/token 校验；**不带**主机指纹——
     * 指纹必须在第 5 步由用户填写并与主机侧显示的值比对，本方法不替用户放行。
     */
    fun startWithManual(hostInput: String?, tokenInput: String?): PairingFlowOutcome {
        return when (val checked = QrPayloadRules.checkManualEntry(hostInput, tokenInput)) {
            is QrPayloadRules.ManualEntryCheck.Invalid ->
                reject(checked.errors.joinToString("；"), CommandErrors.INVALID_PAYLOAD)
            is QrPayloadRules.ManualEntryCheck.Valid -> beginSession(
                token = checked.token,
                host = checked.host,
                hostFingerprint = "",
                insecure = checked.insecure,
            )
        }
    }

    /** 两条入口最终都汇到这一处：先查一次性性，再登记给 [PairingController]。 */
    private fun beginSession(
        token: String,
        host: String,
        hostFingerprint: String,
        insecure: Boolean,
        certFingerprint: String = "",
        pairingCode: String = "",
        signingPublicKey: String = "",
    ): PairingFlowOutcome {
        // 一次性 token 的跨会话拒绝：曾经配过/用过的一律不再接受。
        if (consumedTokens.isConsumed(consumedKeyOf(token))) {
            return reject(
                "这个配对码已经用过了：一次性配对码不能重复使用，请让主机重新生成",
                CommandErrors.SIGNATURE_REQUIRED,
            )
        }
        // 令牌有效期由主机决定（host 侧 `PAIRING_TOKEN_TTL_MS` = 10 分钟）。
        // 设备拿不到主机的绝对过期时刻，因此取「本机收到时刻 + 同一 TTL」，
        // 宁可早一点过期也不延长主机的有效期。
        val expiresAtMs = clock.nowMs() + PAIRING_TOKEN_TTL_MS
        val outcome = controller.acceptHostToken(token, expiresAtMs)
        if (outcome.isRejected) {
            return reject(outcome.message.ifEmpty { "主机下发的一次性配对码未被接受" }, outcome.errorCode)
        }
        rememberToken(token)
        hostUrl = host
        pinnedHostFingerprint = hostFingerprint
        qrCertificateFingerprint = certFingerprint
        qrHostSigningPublicKey = signingPublicKey
        registrationCode = pairingCode.ifEmpty { token }
        insecureTransport = insecure
        return accept(
            message = if (hostFingerprint.isEmpty()) {
                "已记下主机地址与一次性 token；第 5 步需要你手动核对主机指纹"
            } else {
                "已读到配对码：主机地址、一次性 token 与主机指纹都在"
            },
            errorCode = outcome.errorCode,
        )
    }

    // -----------------------------------------------------------------
    // 第 2 + 3 步：输入配对码，生成 Keystore 身份
    // -----------------------------------------------------------------

    /**
     * 第 2、3 步合并执行：校验配对码 → 生成 / 复用 Keystore 身份。
     *
     * [deviceId] 与 [displayName] 来自界面；`device_id` 形状按 [ConnectionSettings] 同规则校验，
     * 避免生成一个连信封 schema 都过不去的 id。
     */
    fun submitCode(
        enteredCode: String,
        deviceId: String,
        displayName: String,
    ): PairingFlowOutcome {
        // 界面上的「配对码」就是二维码里的 token；这里先按同一规则判形状，
        // 免得用户手抄错一个字符就得到一个含义模糊的「不匹配」。
        if (enteredCode.trim().isEmpty()) {
            return reject("配对码不能为空", CommandErrors.INVALID_PAYLOAD)
        }
        if (pinnedHostFingerprint.isEmpty() && hostUrl.isEmpty()) {
            return reject("请先扫码或输入主机地址与配对码", CommandErrors.INVALID_PAYLOAD)
        }
        val idCheck = ConnectionSettings.validateDeviceId(deviceId)
        if (idCheck is ConnectionSettings.DeviceIdCheck.Invalid) {
            return reject(idCheck.message, CommandErrors.INVALID_PAYLOAD)
        }
        val outcome = controller.submitCodeAndCreateIdentity(
            enteredCode = enteredCode,
            deviceId = (idCheck as ConnectionSettings.DeviceIdCheck.Valid).normalizedId,
            displayName = displayName,
        )
        return if (outcome.isRejected) {
            reject(describe(outcome), outcome.errorCode)
        } else {
            // 第 2 步一过，controller 里的 [PairingToken] 已被 consume()（一次性性的真实时刻）。
            // 立刻登记，保证「同一张码在本会话里重扫」也会被 beginSession 拦下——
            // 否则重新 acceptHostToken 会造出一枚**新的**未消耗 token，等于把一次性码变可重用。
            consumedTokens.markConsumed(consumedKeyOf(enteredCode.trim()))
            accept("身份密钥已就绪（私钥留在 Keystore，不会被导出）")
        }
    }

    // -----------------------------------------------------------------
    // 第 4 步：主机保存 device_id / 公钥 / 能力版本 / 连接配置
    // -----------------------------------------------------------------

    /**
     * 第 4 步：把要交给主机的材料准备好（本机侧不落库，落库由主机 `pairing.py` 负责）。
     *
     * 连接配置固定为设备实际要连的 [hostUrl]，**不**接受界面另行填写其他地址——
     * 否则「确认页显示的是 A 主机，实际连的是 B」这种替换就无从发现。
     */
    fun prepareRegistration(
        capabilitiesVersion: String = DEFAULT_CAPABILITIES_VERSION,
        displayName: String = "",
    ): PairingFlowOutcome {
        // 先做中文预检：否则用户会看到 controller 的英文原始原因，界面上「失败原因明确」就打折了。
        if (controller.step != PairingStep.DEVICE_GENERATED_IDENTITY) {
            return reject(
                "第 4 步必须在第 3 步之后：请先输入配对码并生成设备身份密钥，再让主机保存身份",
                CommandErrors.INVALID_PAYLOAD,
            )
        }
        var placeholderIdentity = false
        val channel = remote
        if (channel != null) {
            val identity = controller.currentIdentity
                ?: return reject("设备身份还没有生成", CommandErrors.SIGNATURE_REQUIRED)
            val result = channel.register(
                deviceUrl = hostUrl,
                token = pendingToken(),
                pairingCode = registrationCode,
                deviceId = identity.deviceId,
                publicKeyEncoded = identity.publicKeyEncoded,
                deviceFingerprint = identity.fingerprint,
                capabilitiesVersion = capabilitiesVersion,
                connectionConfig = hostUrl,
                displayName = displayName,
            )
            if (!result.ok) {
                return reject(result.message.ifEmpty { "主机没有接受本次配对" }, CommandErrors.SIGNATURE_REQUIRED)
            }
            if (result.hostFingerprint.isNotEmpty() && pinnedHostFingerprint.isEmpty()) {
                pinnedHostFingerprint = result.hostFingerprint
            }
            placeholderIdentity = result.hostIdentityIsPlaceholder
        }
        // 主机接受后才推进本地状态；网络失败仍停在第 3 步，可重试。
        val outcome = controller.prepareHostRegistration(
            capabilitiesVersion = capabilitiesVersion,
            connectionConfig = hostUrl,
            displayName = displayName,
        )
        if (outcome.isRejected) return reject(describe(outcome), outcome.errorCode)
        return accept(
            "主机已保存本设备身份" +
                if (placeholderIdentity) "（注意：主机尚未配置身份密钥，指纹是占位值）" else "",
        )
    }

    // -----------------------------------------------------------------
    // 第 5 步：双方显示设备名称与指纹，用户确认后才启用
    // -----------------------------------------------------------------

    /**
     * 第 5 步：**用户确认**。没有 `userAccepted=true` 就走不到 `PAIRED`。
     *
     * [hostFingerprintInput] 是用户在确认页看到（或从主机屏幕抄来）的主机指纹。
     * 若本次会话带过二维码指纹（[pinnedHostFingerprint]），两者**必须一致**——
     * 不一致说明「扫码拿到的主机」与「用户核对的主机」不是同一台，**绝不自动接受**。
     */
    fun confirm(
        userAccepted: Boolean,
        hostFingerprintInput: String = "",
        capabilitiesVersion: String = DEFAULT_CAPABILITIES_VERSION,
        displayName: String = "",
    ): PairingFlowOutcome {
        // 顺序预检排在最前：流程走错了比「字段格式不对」更根本，先修顺序再谈格式。
        if (controller.step != PairingStep.HOST_SAVED_IDENTITY) {
            return reject(
                "第 5 步必须在第 4 步之后：请先让主机保存本设备身份，再确认指纹",
                CommandErrors.INVALID_PAYLOAD,
            )
        }
        if (!userAccepted) {
            // 用户拒绝：不写 confirmed、不进 PAIRED；controller 会把 token 一并作废。
            // controller 会**先**判指纹形状、**后**判 userAccepted，所以这里必须给一个形状合规的值，
            // 否则拒绝路径根本到不了「作废 token + 转 PAIRING_REQUIRED」那一段。
            // userAccepted=false 分支里 controller 从不写 confirmed，因此这个占位值不落任何持久状态。
            controller.confirmFingerprints(
                hostFingerprint = pinnedHostFingerprint.takeIf { QrPayloadRules.isWellFormedFingerprint(it) }
                    ?: REJECTION_PLACEHOLDER_FINGERPRINT,
                userAccepted = false,
                capabilitiesVersion = capabilitiesVersion,
                connectionConfig = hostUrl,
                displayName = displayName,
            )
            return reject(
                "你点了「不一致/取消」：本次配对没有启用，一次性 token 已作废，请核对主机屏幕后重新配对",
                CommandErrors.ACTION_REJECTED,
            )
        }
        val typed = hostFingerprintInput.trim().lowercase()
        val pinned = pinnedHostFingerprint.trim().lowercase()
        if (pinned.isNotEmpty() && typed.isNotEmpty() && pinned != typed) {
            // 两个来源的主机指纹不同：这是最能说明「中间人」的信号，绝不自动接受。
            return reject(
                "你填的主机指纹与配对码里的不一致：请重新核对主机屏幕上的指纹（可能是主机身份变化）",
                CommandErrors.SIGNATURE_REQUIRED,
            )
        }
        val effective = typed.ifEmpty { pinned }
        if (effective.isEmpty()) {
            return reject(
                "需要填写主机指纹（64 位小写十六进制）：在主机屏幕上能看到，抄过来逐字比对",
                CommandErrors.INVALID_PAYLOAD,
            )
        }
        // 用户填的形状必须当场判，不能靠 controller 的英文原因兜底。
        if (!QrPayloadRules.isWellFormedFingerprint(effective)) {
            return reject(
                QrPayloadRules.ERROR_FINGERPRINT_SHAPE,
                CommandErrors.INVALID_PAYLOAD,
            )
        }
        // ---- 远程：让主机也把「已确认」落库 ----
        //
        // 本地 controller 只维护**本机**的确认状态；主机的 `PairingService` 必须收到
        // `user_accepted=true` 才会把设备状态推到已配对。少了这一步，
        // 设备端显示「已启用」，主机侧却仍是「未配对」，两边说的不是一回事。
        val channel = remote
        if (channel != null) {
            val token = pendingToken()
                ?: return reject("这次配对没有可用的 token：请重新扫码", CommandErrors.INVALID_PAYLOAD)
            val result = channel.confirm(
                deviceUrl = hostUrl,
                token = token,
                deviceId = controller.currentIdentity?.deviceId.orEmpty(),
                userAccepted = true,
                observedHostFingerprint = effective,
            )
            if (!result.ok) {
                return reject(
                    result.message.ifEmpty { "主机没有确认这次配对" },
                    CommandErrors.SIGNATURE_REQUIRED,
                )
            }
        }

        val outcome = controller.confirmFingerprints(
            hostFingerprint = effective,
            userAccepted = true,
            capabilitiesVersion = capabilitiesVersion,
            connectionConfig = hostUrl,
            displayName = displayName,
        )
        if (outcome.isRejected) {
            return reject(describe(outcome), outcome.errorCode)
        }

        // 第 6 步在这里一并做：配对成功就把一次性 token 作废（不可逆）。
        // 失败路径已由主机自己 finalize，不重复调用。
        if (channel != null) runCatching { channel.finalize(hostUrl, pendingToken()) }

        pinnedHostFingerprint = effective
        return accept("指纹已确认，主机已启用本设备（一次性 token 已失效）")
    }

    // -----------------------------------------------------------------
    // 第 6 步：一次性 token 立即失效
    // -----------------------------------------------------------------

    /**
     * 第 6 步：配对完成后一次性 token **立即失效**（不可逆），并登记进 [ConsumedTokenStore]。
     *
     * 登记的作用超出本次会话：即使进程重启，重放同一张二维码也会被 [beginSession] 拒绝。
     */
    fun finalize(): PairingFlowOutcome {
        val outcome = controller.finalizePairing()
        if (outcome.isRejected) {
            return reject(describe(outcome), outcome.errorCode)
        }
        // 双保险：即使第 2 步的登记因为某种原因没发生，第 6 步也一定落地。
        consumedTokenValue?.let { consumedTokens.markConsumed(consumedKeyOf(it)) }
        return accept("配对完成：一次性 token 已立即失效，重复使用会被拒绝")
    }

    /** 记住本次会话实际使用的 token（第 6 步登记用）。由第 1 步写入。 */
    private var consumedTokenValue: String? = null

    /** 本次会话里已登记的一次性 token（供界面在「生成身份」时回填；从不写日志）。 */
    fun pendingToken(): String = consumedTokenValue.orEmpty()

    /** 已确认的身份（配对成功后由界面落盘；未配对时为 `null`）。 */
    fun confirmedIdentity(): ConfirmedIdentity? = controller.confirmedIdentity

    /** 六步是否全部走完。 */
    fun isPaired(): Boolean = controller.isPaired

    /** 内部 controller（只供界面读退避/连接状态，**不**用于绕过本流程的步骤顺序）。 */
    fun controllerState(): PairingState = controller.state

    private fun consumedKeyOf(token: String): String =
        Fingerprints.of(token.toByteArray(Charsets.UTF_8))

    private fun describe(outcome: PairingOutcome): String =
        outcome.message.ifEmpty { "配对流程拒绝：" + (outcome.errorCode ?: CommandErrors.INVALID_PAYLOAD) }

    private fun accept(message: String, errorCode: String? = null): PairingFlowOutcome =
        PairingFlowOutcome(
            ok = true,
            message = message,
            errorCode = errorCode,
            snapshot = snapshot(),
        )

    private fun reject(message: String, errorCode: String? = null): PairingFlowOutcome =
        PairingFlowOutcome(
            ok = false,
            message = message,
            errorCode = errorCode ?: CommandErrors.INVALID_PAYLOAD,
            snapshot = snapshot(),
        )

    companion object {
        /**
         * 一次性 token 有效期（毫秒）：与 host `pairing.py` 的 `PAIRING_TOKEN_TTL_MS` 相同（10 分钟）。
         *
         * 设备侧只把它当作「本机收到时刻之后的保守上限」，用来本地提前拦截过期 token；
         * 真正的权威判定仍在主机侧。
         */
        const val PAIRING_TOKEN_TTL_MS: Long = 10 * 60 * 1000L

        /** 能力版本默认值：与 `contracts/device-*` 的协议版本一起演进。 */
        const val DEFAULT_CAPABILITIES_VERSION: String = "1"

        /**
         * 「用户拒绝」分支专用的形状合规占位指纹。
         *
         * 它**只**用来通过 `PairingController.confirmFingerprints` 的形状校验，
         * 好让控制流走到「作废 token + 转 `PAIRING_REQUIRED`」那一段；
         * `userAccepted=false` 时 controller 绝不把它写进 `confirmed`，所以它不会成为任何持久状态。
         * 手输路径没有二维码指纹（[pinnedHostFingerprint] 为空）时也会用到它。
         */
        val REJECTION_PLACEHOLDER_FINGERPRINT: String = "0".repeat(DeviceIdentityRules.FINGERPRINT_HEX_LENGTH)
    }

    /**
     * 显式登记本次会话的 token（[beginSession] 在成功时调用）。
     *
     * 单独一个内部方法是为了让 `consumedTokenValue` 只有一个写入口，
     * 避免以后有人从别处塞一个 token 进来绕过 [startWithQr] / [startWithManual]。
     */
    private fun rememberToken(token: String) {
        consumedTokenValue = token
    }
}

/** 一次流程推进的结果；[snapshot] 是推进之后的**真实**状态，界面原样渲染。 */
data class PairingFlowOutcome(
    val ok: Boolean,
    val message: String,
    val errorCode: String?,
    val snapshot: PairingFlowSnapshot,
)

/** 界面只读的状态快照：所有字段都来自 controller 的真实状态，没有一处是猜的。 */
data class PairingFlowSnapshot(
    val state: PairingState,
    val step: PairingStep?,
    val deviceId: String,
    val deviceFingerprint: String,
    val hostFingerprint: String,
    val displayName: String,
    val hostUrl: String,
    val isPaired: Boolean,
    val insecureTransport: Boolean,
) {
    /** 当前步骤的中文说明；`null` 表示还没进入任何一步。 */
    val stepLabel: String
        get() = when (step) {
            null -> "尚未开始配对"
            PairingStep.HOST_ISSUED_TOKEN -> "第 1 步：已收到主机的一次性配对码"
            PairingStep.DEVICE_ENTERED_CODE -> "第 2 步：配对码已输入"
            PairingStep.DEVICE_GENERATED_IDENTITY -> "第 3 步：已生成设备身份密钥"
            PairingStep.HOST_SAVED_IDENTITY -> "第 4 步：主机已保存本设备身份"
            PairingStep.USER_CONFIRMED_FINGERPRINT -> "第 5 步：指纹已确认，设备已启用"
            PairingStep.TOKEN_REVOKED -> "第 6 步：配对完成，一次性 token 已失效"
        }
}

/**
 * 「已失效 / 已用过 token」的登记处。
 *
 * 存在的理由：一次性性必须跨会话成立。`PairingController` 的 [PairingToken] 只活在
 * 一次流程里；用户关页面再重扫同一张码，controller 是新的，但**同一张码不能再用**。
 *
 * 实现只存 **token 的 SHA-256 指纹**（[Fingerprints.of]），不存明文——
 * 已失效 token 也没必要以明文留在磁盘上。
 */
interface ConsumedTokenStore {
    fun isConsumed(tokenKey: String): Boolean
    fun markConsumed(tokenKey: String)
}

/** 内存实现：供单测与非持久场景；**不作为生产唯一真相**（生产用 SharedPreferences 实现，见 `PairingActivity.kt`）。 */
class InMemoryConsumedTokenStore : ConsumedTokenStore {
    private val keys = LinkedHashSet<String>()

    override fun isConsumed(tokenKey: String): Boolean = tokenKey in keys

    override fun markConsumed(tokenKey: String) {
        keys.add(tokenKey)
    }
}
