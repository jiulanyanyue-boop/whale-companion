package cn.whale.agent.device

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import androidx.core.content.ContextCompat

/**
 * M09 按住说话入口（LOCAL_PET 语音）。
 *
 * `docs/modules/M09-local-pet-voice-development.md`「语音实现」：
 * > 首版使用 Android `SpeechRecognizer`，调用方式固定为按住说话。`LocalVoiceController` 只负责
 * > 权限、生命周期、识别结果和错误，**不负责执行设备命令**。命令必须经 `CommandRouter`，
 * > 由 `LocalPetController` 承担。
 *
 * ## 本类做的事 / 不做的事
 *
 * 做：录音权限判定、识别器生命周期、把识别文本原样回调、错误回调、状态机推进、**释放麦克风**。
 *
 * 不做（文档明确禁止）：
 * - 不解析 `ignored`、`忽略安全规则` 一类的自然语言指令：识别文本原样交给上层，
 *   语音**不能**改变模式、白名单、确认方式或主人身份；
 * - 不直接调用任何设备 API（不持有 `CommandRouter`，也不持有 `LocalPetController`）；
 * - 不做常驻监听 / 唤醒词 / 后台热词循环：录音只在 [press] 到 [release] 之间存在。
 *
 * ## 录音状态（固定）
 *
 * ```
 * IDLE → PRESSING → LISTENING → TRANSCRIBING → IDLE
 * 任何状态 → STOPPED
 * ```
 *
 * 状态推进由纯逻辑 [VoiceStateMachine] 承担（可脱离 Android 单测）；本类只做 Android 绑定。
 *
 * ## 麦克风释放触发点（全部走 `stop()` 或其等价收尾）
 *
 * 见 [stop] 与 [LocalVoiceController] 的调用点清单（Activity 销毁、屏幕锁定、模式切换、任务停止、
 * 悬浮窗关闭、权限被回收、识别结束/失败）。识别结束与失败也会 `destroy()` 识别器，
 * 因此一次按下只会留下一次录音。
 *
 * 线程：所有方法在主线程调用（[SpeechRecognizer] 要求）。
 *
 * @param onText 识别成功文本；上层负责转成信封并经 `LocalPetController.executeFromVoice` 执行。
 * @param onError 语音层失败（不是命令错误码，不会写进 `DeviceResponse.error_code`）。
 * @param onStateChanged 状态变化回调，供桌宠/设置页渲染。
 */
class LocalVoiceController(
    context: Context,
    private val onText: (String) -> Unit,
    private val onError: (VoiceFailure) -> Unit,
    private val onStateChanged: (VoiceState) -> Unit = {},
) : AutoCloseable {

    private val mainHandler: Handler = Handler(Looper.getMainLooper())
    private val permission: VoicePermission = AndroidVoicePermission(context.applicationContext)
    private val engine: VoiceEngine = SpeechRecognizerEngine(context.applicationContext, mainHandler)
    private val machine = VoiceStateMachine()

    /** 每次按下自增；旧识别器的迟到回调因 attempt 不匹配而丢弃（防止释放后麦克风被复活）。 */
    private var attempt: Int = 0

    /** 当前状态。 */
    val state: VoiceState get() = machine.state

    /** 是否有录音会话在进行（PRESSING / LISTENING / TRANSCRIBING）。 */
    val isRecording: Boolean get() = machine.state.isActive

    /** 录音权限是否已授予（只读探测，不申请、不自动开启）。 */
    fun hasRecordPermission(): Boolean = permission.canRecord()

    /**
     * 按下：开始一次按住说话。
     *
     * 固定检查顺序（**权限在启动录音之前**）：
     * 1. 状态必须是 IDLE；
     * 2. **没有录音权限时直接失败，绝不创建识别器**（产品验收：没录音权限时按住说话不启动录音）；
     * 3. 识别器不可用时失败；
     * 4. 创建识别器并 `startListening`，成功后进入 [VoiceState.LISTENING]。
     *
     * @return `true` 表示已经进入录音；`false` 表示被拒绝（已通过 [onError] 报告原因）。
     */
    fun press(): Boolean {
        if (!machine.press()) {
            // 已在录音中或已 STOPPED：不重复启动第二个识别器。
            onError(VoiceFailure(VoiceFailureKind.BUSY))
            return false
        }
        report()

        if (!permission.canRecord()) {
            fail(VoiceFailure(VoiceFailureKind.PERMISSION_REQUIRED))
            return false
        }
        if (!permission.recognizerAvailable()) {
            fail(VoiceFailure(VoiceFailureKind.RECOGNIZER_UNAVAILABLE))
            return false
        }

        attempt += 1
        val current = attempt
        val started = engine.start(current, callbackFor(current))
        if (!started) {
            fail(VoiceFailure(VoiceFailureKind.RECOGNIZER_UNAVAILABLE))
            return false
        }
        machine.listeningStarted()
        // 进程级登记：模式切换 / 任务停止可以在没有 Service 句柄时也强制释放麦克风。
        VoiceMicWatch.register(this)
        report()
        return true
    }

    /**
     * 松开：结束这一次按住。
     *
     * - 仍在 [VoiceState.PRESSING]（识别器还没真正开录）：直接收尾回 [VoiceState.IDLE]；
     * - 正在 [VoiceState.LISTENING]：`stopListening()` 让平台关闭麦克风，进入 [VoiceState.TRANSCRIBING]
     *   等待最终结果；识别器的结果/错误回调随后把状态收回 [VoiceState.IDLE] 并 `destroy()`。
     */
    fun release() {
        when (machine.state) {
            VoiceState.LISTENING -> {
                engine.stopListening()
                machine.transcribing()
                report()
            }
            VoiceState.PRESSING -> {
                // 还没开录就松手：不产生任何录音，fail() 内部会收尾并销毁识别器。
                fail(VoiceFailure(VoiceFailureKind.EMPTY_RESULT))
            }
            else -> Unit
        }
    }

    /**
     * 停止并**释放麦克风**。任何状态都可以调用，幂等。
     *
     * 必须被以下触发点调用（见 `docs/modules/M09-local-pet-voice-development.md`）：
     * Activity/宿主 `onDestroy`、屏幕锁定（`ACTION_SCREEN_OFF` / `KEYGUARD`）、模式切换、
     * 任务停止、悬浮窗关闭当天显示、录音权限被回收、识别成功或失败之后。
     *
     * 语义：作废当前 attempt（迟到回调全部丢弃）→ `cancel()` + `destroy()` → 状态置
     * [VoiceState.STOPPED]（终止态，再次使用前需新建实例或调用 [reset]）。
     */
    fun stop() {
        attempt += 1
        engine.cancel()
        engine.destroy()
        machine.stop()
        VoiceMicWatch.unregister(this)
        report()
    }

    /** 从 [VoiceState.STOPPED] 回到 [VoiceState.IDLE]，供同一个实例重复使用（Activity 复用场景）。 */
    fun reset() {
        if (machine.reset()) report()
    }

    /** AutoCloseable：宿主 `onDestroy` 可以直接 `close()`，等价于 [stop]。 */
    override fun close() = stop()

    // ---------- 内部 ----------

    private fun callbackFor(expectedAttempt: Int): VoiceEngineCallback = object : VoiceEngineCallback {
        override fun onResults(text: String) {
            if (expectedAttempt != attempt) return
            when (machine.state) {
                VoiceState.LISTENING, VoiceState.TRANSCRIBING -> Unit
                else -> return
            }
            val transcript = text.trim()
            if (transcript.isEmpty()) {
                fail(VoiceFailure(VoiceFailureKind.EMPTY_RESULT))
                return
            }
            // 只回调文本：不执行命令，也不解析「忽略安全规则」这类指令。
            finish(VoiceState.IDLE)
            onText(transcript)
        }

        override fun onSpeechError(errorCode: Int) {
            if (expectedAttempt != attempt) return
            if (!machine.state.isActive) return
            fail(VoiceFailure(VoiceFailureKind.SPEECH_ERROR, errorCode))
        }

        override fun onEndOfSpeech() {
            if (expectedAttempt != attempt) return
            if (machine.transcribing()) report()
        }
    }

    /** 成功或失败之后的统一收尾：销毁识别器（释放麦克风）→ 状态回 IDLE → 通知。 */
    private fun finish(target: VoiceState) {
        attempt += 1
        engine.destroy()
        VoiceMicWatch.unregister(this)
        when (target) {
            VoiceState.IDLE -> machine.finish()
            else -> Unit
        }
        report()
    }

    /** 失败收尾：报告原因、销毁识别器、回到 IDLE。 */
    private fun fail(failure: VoiceFailure) {
        attempt += 1
        engine.cancel()
        engine.destroy()
        VoiceMicWatch.unregister(this)
        machine.fail()
        onError(failure)
        report()
    }

    private fun report() = onStateChanged(machine.state)
}

/**
 * 进程内「当前谁在占用麦克风」的登记处。
 *
 * 用来满足文档硬要求：Activity 销毁、屏幕锁定、**模式切换**、**任务停止**都必须 `stop()` 释放麦克风，
 * 但这些调用点（M01 的 `ModeSwitchCoordinator`、M06 的任务运行时）不一定持有 Service/Controller 引用。
 * 它们只需要调用 [releaseAll]，就能强制释放所有正在录音的实例。
 *
 * 约束：
 * - 只在进程内使用（静态字段），不跨进程、不写磁盘、不发网络；
 * - 同一时刻通常只有一个录音实例，但用弱引用集合以容忍泄漏；
 * - 不引入任何常驻麦克风或热词监听——这里只登记**已经**开始的录音。
 */
object VoiceMicWatch {
    private val active = java.util.Collections.newSetFromMap(
        java.util.WeakHashMap<LocalVoiceController, Boolean>(),
    )

    fun register(controller: LocalVoiceController) {
        synchronized(active) { active.add(controller) }
    }

    fun unregister(controller: LocalVoiceController) {
        synchronized(active) { active.remove(controller) }
    }

    /** 是否还有录音会话存在（用于验收与本地日志，不对外暴露）。 */
    val isRecording: Boolean get() = synchronized(active) { active.isNotEmpty() }

    /**
     * 强制释放所有录音（模式切换、任务停止、锁屏、Activity 销毁）。
     *
     * 先取出快照并清空集合，**再**逐个 `stop()`：`stop()` 内部会回调 [unregister]，
     * 若此时仍持有集合锁就会重入，因此锁只在快照阶段使用。
     *
     * @return 被停止的实例数量。
     */
    fun releaseAll(): Int {
        val snapshot = synchronized(active) {
            val copy = active.toList()
            active.clear()
            copy
        }
        snapshot.forEach { runCatching { it.stop() } }
        return snapshot.size
    }
}

// ---------------------------------------------------------------------------
// 纯逻辑：录音状态机（零 Android 依赖，可在 JVM 上单测）
// ---------------------------------------------------------------------------

/** 录音状态（固定集合）。 */
enum class VoiceState {
    IDLE,
    PRESSING,
    LISTENING,
    TRANSCRIBING,
    STOPPED,
    ;

    /** 是否有麦克风会话存在（PRESSING 尚未开录，但属于一次按下）。 */
    val isActive: Boolean get() = this == PRESSING || this == LISTENING || this == TRANSCRIBING
}

/**
 * 录音状态机（纯逻辑）。
 *
 * 固定迁移（`docs/modules/M09-local-pet-voice-development.md`）：
 * ```
 * IDLE → PRESSING → LISTENING → TRANSCRIBING → IDLE
 * 任何状态 → STOPPED
 * ```
 *
 * 非法迁移一律返回 `false` 且**不改变状态**，因此上层无法把状态机推成文档之外的状态。
 * [STOPPED] 是终止态，只能 [reset] 回到 [IDLE]。
 */
class VoiceStateMachine {

    var state: VoiceState = VoiceState.IDLE
        private set

    /** IDLE → PRESSING（重复按下返回 false，不覆盖进行中的录音）。 */
    fun press(): Boolean = move(from = setOf(VoiceState.IDLE), to = VoiceState.PRESSING)

    /** PRESSING → LISTENING（识别器已真正开始）。 */
    fun listeningStarted(): Boolean =
        move(from = setOf(VoiceState.PRESSING), to = VoiceState.LISTENING)

    /** LISTENING → TRANSCRIBING（松手或识别器报告语音结束）。 */
    fun transcribing(): Boolean =
        move(from = setOf(VoiceState.LISTENING), to = VoiceState.TRANSCRIBING)

    /** PRESSING/LISTENING/TRANSCRIBING → IDLE（识别成功或静默结束）。 */
    fun finish(): Boolean =
        move(from = setOf(VoiceState.PRESSING, VoiceState.LISTENING, VoiceState.TRANSCRIBING), to = VoiceState.IDLE)

    /** 任何非终止态 → IDLE（失败）。 */
    fun fail(): Boolean =
        move(from = setOf(VoiceState.PRESSING, VoiceState.LISTENING, VoiceState.TRANSCRIBING), to = VoiceState.IDLE)

    /** 任何状态 → STOPPED（释放麦克风）；重复调用返回 false。 */
    fun stop(): Boolean = move(from = VoiceState.entries.toSet() - VoiceState.STOPPED, to = VoiceState.STOPPED)

    /** STOPPED → IDLE。 */
    fun reset(): Boolean = move(from = setOf(VoiceState.STOPPED), to = VoiceState.IDLE)

    private fun move(from: Set<VoiceState>, to: VoiceState): Boolean {
        if (state !in from) return false
        state = to
        return true
    }
}

// ---------------------------------------------------------------------------
// 语音失败原因（本地提示种类，**不是**命令错误码）
// ---------------------------------------------------------------------------

/**
 * 语音失败种类。
 *
 * 冻结要求：**不新增错误码**。这里只是本地提示分类，绝不写入 `DeviceResponse.error_code`；
 * 对外真正需要命令错误码时，只复用已登记的 [CommandErrors.PERMISSION_REQUIRED]。
 */
enum class VoiceFailureKind {
    /** 没有 `RECORD_AUDIO` 权限：按住说话不启动录音。 */
    PERMISSION_REQUIRED,

    /** 系统没有可用的 `SpeechRecognizer`。 */
    RECOGNIZER_UNAVAILABLE,

    /** 识别结果为空（只说了一个气声，或松手太快）。 */
    EMPTY_RESULT,

    /** 识别器返回错误码。 */
    SPEECH_ERROR,

    /** 正在录音中又按下，或已 `STOPPED` 后重复按下。 */
    BUSY,
    ;

    /** 可对接到命令管线的错误码；只有权限问题映射到已登记常量，其余为 `null`。 */
    val commandErrorCode: String?
        get() = if (this == PERMISSION_REQUIRED) CommandErrors.PERMISSION_REQUIRED else null
}

/**
 * 语音层失败描述。
 *
 * @property kind 分类。
 * @property speechErrorCode `SpeechRecognizer.ERROR_*` 原始值（仅 [VoiceFailureKind.SPEECH_ERROR]）。
 */
data class VoiceFailure(
    val kind: VoiceFailureKind,
    val speechErrorCode: Int? = null,
) {
    /** 直接可展示的文案（`res/values/strings.xml` 由 M11 维护，这里先内联，后续可迁移为资源）。 */
    val message: String
        get() = when (kind) {
            VoiceFailureKind.PERMISSION_REQUIRED -> "没有录音权限，按住说话不会启动录音"
            VoiceFailureKind.RECOGNIZER_UNAVAILABLE -> "本机没有可用的语音识别服务"
            VoiceFailureKind.EMPTY_RESULT -> "没有听清，请再说一次"
            VoiceFailureKind.SPEECH_ERROR -> "语音识别失败（错误码 ${speechErrorCode ?: -1}）"
            VoiceFailureKind.BUSY -> "正在录音，请松开后再按"
        }
}

// ---------------------------------------------------------------------------
// Android 绑定层（薄壳，全部判定已在上面的纯逻辑里）
// ---------------------------------------------------------------------------

/** 权限/能力探测（可替换，便于单测与 OEM 差异）。 */
interface VoicePermission {
    /** 是否已授予 `RECORD_AUDIO`。只读探测，不申请。 */
    fun canRecord(): Boolean

    /** 系统是否有可用的语音识别服务。 */
    fun recognizerAvailable(): Boolean
}

/** 生产实现：只读探测，绝不主动申请权限（申请由设置页/系统弹窗承担）。 */
class AndroidVoicePermission(private val context: Context) : VoicePermission {
    override fun canRecord(): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED

    override fun recognizerAvailable(): Boolean = SpeechRecognizer.isRecognitionAvailable(context)
}

/** 识别器回调（已从 [RecognitionListener] 的 `Bundle` 里解出纯文本与错误码）。 */
interface VoiceEngineCallback {
    fun onResults(text: String)

    fun onSpeechError(errorCode: Int)

    fun onEndOfSpeech()
}

/**
 * 语音引擎抽象。
 *
 * 后续低延迟版本可以替换为 `AudioRecord`（16 kHz、单声道、20 ms 帧、`VOICE_COMMUNICATION`、AEC），
 * 替换只发生在本接口实现内，`CommandRouter` 与 [LocalVoiceController] 的业务逻辑不动。
 */
interface VoiceEngine {
    /** 开始一次识别；返回 `false` 表示启动失败。 */
    fun start(attempt: Int, callback: VoiceEngineCallback): Boolean

    /** 松开时调用：让平台停止收音（不等同 destroy）。 */
    fun stopListening()

    /** 取消本次识别。 */
    fun cancel()

    /** 释放识别器与麦克风；幂等。 */
    fun destroy()
}

/**
 * 生产实现：`SpeechRecognizer` 薄壳。
 *
 * 每次都新建识别器实例（`createSpeechRecognizer` → `setRecognitionListener` → `startListening`），
 * 结束时 `cancel()` + `destroy()`，因此**不会**留下常驻麦克风。
 */
class SpeechRecognizerEngine(
    private val context: Context,
    private val mainHandler: Handler,
) : VoiceEngine {

    private var recognizer: SpeechRecognizer? = null

    override fun start(attempt: Int, callback: VoiceEngineCallback): Boolean {
        destroy()
        val listener = object : RecognitionListener {
            override fun onResults(results: Bundle?) {
                val text = results?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)?.firstOrNull().orEmpty()
                callback.onResults(text)
            }

            override fun onError(error: Int) {
                callback.onSpeechError(error)
            }

            override fun onEndOfSpeech() {
                callback.onEndOfSpeech()
            }

            override fun onReadyForSpeech(params: Bundle?) = Unit
            override fun onBeginningOfSpeech() = Unit
            override fun onRmsChanged(rmsdB: Float) = Unit
            override fun onBufferReceived(buffer: ByteArray?) = Unit
            override fun onPartialResults(partialResults: Bundle?) = Unit
            override fun onEvent(eventType: Int, params: Bundle?) = Unit
        }

        return try {
            val instance = SpeechRecognizer.createSpeechRecognizer(context)
            instance.setRecognitionListener(listener)
            recognizer = instance
            instance.startListening(
                Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
                    putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
                    // 首版不做部分结果：桌宠只需要最终文本。
                    putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, false)
                },
            )
            true
        } catch (_: Throwable) {
            destroy()
            false
        }
    }

    override fun stopListening() {
        runCatching { recognizer?.stopListening() }
    }

    override fun cancel() {
        runCatching { recognizer?.cancel() }
    }

    override fun destroy() {
        val instance = recognizer ?: return
        recognizer = null
        runCatching { instance.cancel() }
        runCatching { instance.destroy() }
    }
}
