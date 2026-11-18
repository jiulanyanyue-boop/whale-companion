package cn.whale.agent.device

/**
 * M01 模式闸门（纯逻辑、可注入依赖），供远程与本地两种入口共用。
 *
 * 设计约束（`docs/modules/M01-system-modes-development.md`）：
 * - 两种入口必须共用同一个执行器，禁止复制第二套命令执行器；
 *   因此这里只声明 [CommandExecutor] 抽象，生产实现 [RouterCommandExecutor] 是**唯一**实现，
 *   它直接转调 [CommandRouter.execute]。
 * - 远程闸门顺序固定 5 步：解码失败 → `signature` 为空 → 模式不是 `REMOTE_B` →
 *   幂等键/deadline/counter → 执行器。
 * - 本地闸门固定检查：模式 `LOCAL_PET` → 进程内调用 → 已解锁会话 → 高风险确认 → 执行器。
 * - 所有错误码只能引用 [CommandErrors] 常量，禁止散落字符串字面量。
 *
 * 本文件不依赖 Android `Context`（[RouterCommandExecutor] 除外，它只是薄适配层），
 * 因此固定测试可以直接在 JVM 上运行。
 */

/** 唯一执行器抽象：两种模式的唯一执行路径。 */
fun interface CommandExecutor {
    fun execute(envelope: DeviceEnvelope): DeviceResponse
}

/**
 * M05 验签钩子：把一条远程帧判成 [SignatureCheck]。
 *
 * 单独抽成接口的理由：M01 的闸门顺序归 M01，M05 的密码学与身份归 M05。
 * 注入实现后，第 2 步从「只判非空」升级为「指纹变化检测 + ECDSA 验签」，
 * 而 M01 的五步顺序一行不改。
 */
fun interface FrameSignatureGate {
    fun check(envelope: DeviceEnvelope): SignatureCheck
}

/**
 * 生产实现：桥接 M05 的 [SignatureGate]。
 *
 * M05 的 `SignatureGate.check` 吃的是「去掉 `signature` 前/后的字段表」+ 身份 + 双侧指纹，
 * 这里把 [DeviceEnvelope] 还原成那份字段表，保证 canonical JSON 与主机算出来的**字节一致**。
 *
 * @param identity 设备身份（只含公钥与指纹，无私钥）
 * @param hostFingerprintProvider 当前配对时固定下来的主机指纹
 * @param observedHostFingerprintProvider 本次帧带来的主机指纹（由传输层从 TLS/握手中取得）
 * @param onHostFingerprintChanged 指纹变化回调：**先**清身份并要求人工重配，**再**返回拒绝
 */
class M05FrameSignatureGate(
    private val identity: () -> PublicIdentity?,
    private val hostFingerprintProvider: () -> String,
    private val observedHostFingerprintProvider: () -> String,
    private val verifier: SignatureVerifier = JvmEcdsaVerifier(),
    private val onHostFingerprintChanged: () -> Unit = {},
) : FrameSignatureGate {
    override fun check(envelope: DeviceEnvelope): SignatureCheck {
        val check = SignatureGate.check(
            envelopeFields = envelopeToCanonicalFields(envelope),
            identity = identity(),
            expectedHostFingerprint = hostFingerprintProvider(),
            observedHostFingerprint = observedHostFingerprintProvider(),
            verifier = verifier,
        )
        if (check == SignatureCheck.HOST_FINGERPRINT_CHANGED) {
            // 顺序固定：先作废本地身份（强制人工重配），再让上层返回拒绝。
            runCatching { onHostFingerprintChanged() }
        }
        return check
    }
}

/**
 * [DeviceEnvelope] → canonical 字段表。
 *
 * 手写而不是反射：`CanonicalJson` 明确拒绝把 Kotlin `data class` 反射成 JSON
 * （反射出来的键集合与类型不受控，极易与主机侧 `dict` 产生字节差异）。
 * 这里逐字段搬运，**字段集合与 `contracts/device-envelope.schema.json` 一致**，
 * 值类型也保持 Python 侧能对上的原生类型（整数给 `Long`，`payload` 给 `Map`/`List` 树）。
 *
 * 注意：`payload` 是 `JsonObject`，这里转成普通的 `Map<String, Any?>` / `List` 树，
 * 让 [CanonicalJson] 走它已实现的容器分支，而不是拒绝。
 */
fun envelopeToCanonicalFields(envelope: DeviceEnvelope): Map<String, Any?> = linkedMapOf(
    "protocol_version" to envelope.protocol_version.toLong(),
    "request_id" to envelope.request_id,
    "device_id" to envelope.device_id,
    "command" to envelope.command,
    "deadline_ms" to envelope.deadline_ms.toLong(),
    "idempotency_key" to envelope.idempotency_key,
    "counter" to envelope.counter,
    "issued_at_ms" to envelope.issued_at_ms,
    "payload" to jsonElementToPlain(envelope.payload),
    "signature" to envelope.signature,
)

/** `kotlinx.serialization.json.JsonElement` → 纯 Kotlin 容器/原生值树。 */
private fun jsonElementToPlain(element: kotlinx.serialization.json.JsonElement): Any? =
    when (element) {
        is kotlinx.serialization.json.JsonObject ->
            element.mapValues { (_, value) -> jsonElementToPlain(value) }
        is kotlinx.serialization.json.JsonArray ->
            element.map { jsonElementToPlain(it) }
        is kotlinx.serialization.json.JsonNull -> null
        is kotlinx.serialization.json.JsonPrimitive ->
            when {
                element.isString -> element.content
                element.content == "true" -> true
                element.content == "false" -> false
                else -> element.content.toLongOrNull() ?: element.content
            }
        else -> element.toString()
    }

/**
 * 生产执行器适配层。
 *
 * 只做一件事：把信封交给 [CommandRouter.execute]。
 * 这里没有第二份路由实现——远程与本地入口都经过它，满足「唯一执行器」要求。
 */
class RouterCommandExecutor(private val router: CommandRouter) : CommandExecutor {
    override fun execute(envelope: DeviceEnvelope): DeviceResponse = router.execute(envelope)
}

/** 幂等键记录：命中时原样回放历史响应，不得重复执行命令。 */
interface EffectLog {
    fun find(idempotencyKey: String): String?

    fun save(idempotencyKey: String, responseJson: String, nowMs: Long)
}

/** 单调 counter 记录：同一设备 counter 必须严格递增，否则视为重放。 */
interface CounterLog {
    fun find(deviceId: String): Long?

    fun save(deviceId: String, counter: Long)
}

/**
 * 闸门结果。
 *
 * @property executed 是否真正进入了执行器（无副作用用例必须为 false）。
 * @property response 需要回包的新响应；`null` 且 [cachedJson] 为 `null` 时表示「丢弃、不回包」。
 * @property cachedJson 幂等命中时原样回放的历史响应 JSON。
 * @property errorCode 本地日志类错误码（例如 `PROTOCOL_DECODE_FAILED`）；丢弃帧只进本地日志。
 */
data class GateResult(
    val executed: Boolean,
    val response: DeviceResponse? = null,
    val cachedJson: String? = null,
    val errorCode: String? = null,
) {
    /** 是否丢弃且不回包（只有解码失败会这样）。 */
    val isDiscarded: Boolean get() = response == null && cachedJson == null

    /** 是否被闸门拒绝（没有进入执行器，但有回包）。 */
    val isRejected: Boolean get() = !executed && !isDiscarded

    /** 拒绝时对外公开的错误码（优先取响应里的 error_code，其次取本地日志码）。 */
    val deniedCode: String?
        get() {
            if (!isRejected) return null
            val code = response?.error_code
            return if (code.isNullOrBlank()) errorCode else code
        }
}

/**
 * 远程闸门：`DeviceConnectionService.onMessage` 的固定 5 步顺序。
 *
 * 除了「解码失败」这一步，所有拒绝都必须回包给主机；解码失败的帧只会写本地日志。
 *
 * 第 2 步的验签**可注入**（`signatureGate`）：
 * - 传 `null`（默认）时退化为「只判 `signature` 非空」——这是未配对/测试场景的既有行为；
 * - 注入真实实现时走 M05 的 `SignatureGate.check`（含主机指纹变化检测与 ECDSA 验签）。
 * 这样 M01 的闸门顺序一行不改，M05 的密码学能力可以独立演进。
 */
class RemoteModeGate(
    private val mode: ModeReader,
    private val executor: CommandExecutor,
    private val effects: EffectLog,
    private val counters: CounterLog,
    private val clock: SessionClock = SystemSessionClock,
    private val log: (String) -> Unit = {},
    /** M05 验签钩子；`null` 表示只判非空（未配对/测试）。 */
    private val signatureGate: FrameSignatureGate? = null,
) {
    /** 入口：先解码，再进入固定 5 步流程。 */
    fun processRaw(raw: String): GateResult {
        val envelope = try {
            ProtocolCodec.decode(raw)
        } catch (t: Throwable) {
            // 第 1 步：JSON 反序列化失败 → 丢弃并记录 PROTOCOL_DECODE_FAILED，不回包。
            log("${CommandErrors.PROTOCOL_DECODE_FAILED} ${t.javaClass.simpleName}: remote frame discarded")
            return GateResult(executed = false, errorCode = CommandErrors.PROTOCOL_DECODE_FAILED)
        }
        return process(envelope)
    }

    /** 已解码信封的固定 5 步流程。 */
    fun process(envelope: DeviceEnvelope): GateResult {
        // 第 2 步：验签。
        // - 未注入 signatureGate：退化为只判空（保持 M01 既有行为）。
        // - 注入后：指纹变化优先于密码学校验（换身份 + 伪造签名不能被当成普通验签失败）。
        val check = signatureGate?.check(envelope) ?: if (envelope.signature.isBlank()) {
            SignatureCheck.MISSING
        } else {
            SignatureCheck.OK
        }
        if (check != SignatureCheck.OK) {
            return reject(
                envelope,
                CommandErrors.SIGNATURE_REQUIRED,
                if (check == SignatureCheck.HOST_FINGERPRINT_CHANGED) {
                    "host identity fingerprint changed; manual re-pairing required"
                } else {
                    "request signature is required"
                },
            )
        }

        // 第 3 步：当前模式不是 REMOTE_B → MODE_REJECTED，且绝不调用执行器。
        if (!mode.get().isRemote) {
            return reject(envelope, CommandErrors.MODE_REJECTED, "remote control is disabled in local pet mode")
        }

        // 第 4 步：幂等键、deadline、counter。任何副作用检查都在执行器之前完成。
        val duplicate = effects.find(envelope.idempotency_key)
        if (duplicate != null) {
            return GateResult(executed = false, cachedJson = duplicate)
        }

        val now = clock.nowMs()
        val age = if (envelope.issued_at_ms == 0L) 0L else now - envelope.issued_at_ms
        // Signed requests can arrive with small wall-clock skew between the phone
        // and computer. Counter and idempotency checks still reject replay.
        if (envelope.deadline_ms !in MIN_DEADLINE_MS..MAX_DEADLINE_MS || age < -5_000L || age > envelope.deadline_ms.toLong()) {
            return reject(envelope, CommandErrors.EXPIRED, "request deadline or issued_at_ms is invalid")
        }

        val previous = counters.find(envelope.device_id) ?: -1L
        if (envelope.counter <= previous) {
            return reject(envelope, CommandErrors.REPLAY, "counter is not increasing")
        }
        counters.save(envelope.device_id, envelope.counter)

        // 第 5 步：唯一执行器。
        val response = executor.execute(envelope)
        val encoded = ProtocolCodec.encode(response)
        effects.save(envelope.idempotency_key, encoded, now)
        return GateResult(executed = true, response = response)
    }

    private fun reject(envelope: DeviceEnvelope, code: String, message: String): GateResult =
        GateResult(
            executed = false,
            response = DeviceResponse(envelope.request_id, envelope.device_id, "denied", code, message),
        )

    companion object {
        const val MIN_DEADLINE_MS: Int = 100
        const val MAX_DEADLINE_MS: Int = 300_000
    }
}

/** 本地调用来源。只有本应用进程内的来源允许执行；其他来源一律拒绝。 */
enum class LocalCommandSource(val inProcess: Boolean) {
    /** 本应用 Activity/设置页内的显式点击。 */
    IN_APP_ACTIVITY(true),

    /** 本应用桌宠悬浮窗内的显式点击或按住说话。 */
    LOCAL_PET_OVERLAY(true),

    /** 本应用进程内的语音识别结果（按住说话，非法常驻麦克风）。 */
    LOCAL_VOICE(true),

    /** 通过隐式 Intent 到达的外部调用：禁止。 */
    IMPLICIT_INTENT(false),

    /** 其他应用直接调用：禁止。 */
    EXTERNAL_APP(false),

    /** 网络帧误入本地入口：禁止。 */
    NETWORK_FRAME(false),
    ;

    companion object {
        val IN_PROCESS_SOURCES: List<LocalCommandSource> = LocalCommandSource.entries.filter { it.inProcess }
    }
}

/**
 * 高风险命令的本地确认接口（最小本地接口，只留调用点）。
 *
 * 真正的 `UserGate` 由 M04 主人与安全模块实现；M01 只约定「先确认、后执行」的顺序，
 * 不在这里写完整确认状态机，也不做密码/验证码填写（文档明确禁止）。
 */
fun interface RiskConfirmation {
    fun confirm(command: String, payloadSummary: String): Boolean
}

/** 确认卡上的载荷摘要：只列字段名，不搬运正文（短信正文等敏感内容不进日志）。 */
object RiskConfirmationSummary {
    fun payloadSummary(envelope: DeviceEnvelope): String =
        envelope.payload.keys.sorted().joinToString(", ")
}

/**
 * 高风险判定策略（可注入）。
 *
 * **清单归属 M04**：`docs/modules/M04-*.md` 的「高风险命令」小节定义哪些命令必须确认；
 * M01 只保证「先确认、后执行」的顺序，不私造规范。M04 接入时替换本接口实现即可，
 * 不需要改动 [LocalModeGate] 的任何顺序逻辑。
 */
fun interface RiskPolicy {
    fun isHighRisk(command: String): Boolean
}

object RiskPolicies {
    /** 不做确认（仅用于 M01 单测验证「普通命令不触发确认」路径）。 */
    val NONE: RiskPolicy = RiskPolicy { false }

    /**
     * 临时兜底清单：**等待 M04 `UserGate` 接管**，不得与 M04 文档清单冲突。
     *
     * 目前只包含 M04 已明示的五类高风险命令：外发短信、改动系统设置、媒体外发、
     * 输入（密码/验证码/支付字段所在的输入动作）与点击（发布/删除/付款按钮所在的点击动作）。
     * M04 落地后必须替换为 M04 的清单实现，本对象只作为 fail-closed 的过渡默认值。
     */
    val INTERIM_FALLBACK: RiskPolicy = RiskPolicy { command ->
        command in setOf(
            "device.sms.send",
            "device.system.settings",
            "device.media.share",
            "device.ui.input",
            "device.ui.tap",
        )
    }
}

/** 本地闸门：`LocalPetController.execute` 的固定检查顺序。 */
class LocalModeGate(
    private val mode: ModeReader,
    private val session: LocalSessionGate,
    private val executor: CommandExecutor,
    private val riskConfirmation: RiskConfirmation? = null,
    private val riskPolicy: RiskPolicy = RiskPolicies.INTERIM_FALLBACK,
) {
    fun execute(envelope: DeviceEnvelope, source: LocalCommandSource): GateResult {
        // 1. 模式必须为 LOCAL_PET。
        if (!mode.get().isLocal) {
            return reject(envelope, CommandErrors.MODE_REJECTED, "local pet mode is disabled")
        }

        // 2. 调用必须来自本应用进程；隐式 Intent / 外部应用 / 网络帧一律拒绝。
        if (!source.inProcess) {
            return reject(envelope, CommandErrors.PERMISSION_REQUIRED, "local ingress must come from this app process")
        }

        // 3. 本地会话必须已解锁；超时返回 NEEDS_UNLOCK 要求重新解锁。
        val decision = session.check()
        if (decision is SessionDecision.Denied) {
            return reject(envelope, decision.code, decision.message)
        }

        // 4. 高风险命令必须先经过确认接口，再调用执行器（清单由 M04 注入的 [RiskPolicy] 决定）。
        if (riskPolicy.isHighRisk(envelope.command)) {
            val confirmation = riskConfirmation
                // 未接入确认实现时 fail-closed：拒绝执行，绝不降级成「无确认直接执行」。
                ?: return reject(envelope, CommandErrors.PERMISSION_REQUIRED, "high risk command requires confirmation")
            val approved = confirmation.confirm(envelope.command, RiskConfirmationSummary.payloadSummary(envelope))
            if (!approved) {
                // 用户拒绝/超时：任务终止。沿用已登记错误码，不新增私造码。
                return reject(envelope, CommandErrors.ACTION_REJECTED, "high risk command was not confirmed")
            }
        }

        // 5. 唯一执行器。
        return GateResult(executed = true, response = executor.execute(envelope))
    }

    private fun reject(envelope: DeviceEnvelope, code: String, message: String): GateResult =
        GateResult(
            executed = false,
            response = DeviceResponse(envelope.request_id, envelope.device_id, "denied", code, message),
        )
}
