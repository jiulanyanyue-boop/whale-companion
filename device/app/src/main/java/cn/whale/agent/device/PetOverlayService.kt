package cn.whale.agent.device

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.res.Configuration
import android.graphics.Color
import android.graphics.PixelFormat
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.provider.Settings
import android.view.Gravity
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.TextView
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.decodeFromString

/**
 * M09 本地桌宠悬浮窗服务。
 *
 * 规范来源：`docs/modules/M09-local-pet-voice-product.md`、`M09-local-pet-voice-development.md`。
 *
 * ## 职责边界
 *
 * 做：显示/隐藏**状态指示器**窗口、拖拽贴边、拖到底部关闭当天显示、长按按住说话、
 * 低频（≥1s）刷新停留态、把识别文本交回宿主。
 *
 * **不做**（文档禁止项，逐条对应）：
 * - 不读取聊天正文、QQ、剪贴板或屏幕正文：本类没有任何正文来源，UI 只显示 [PetState] 文案；
 * - 不直接调用设备 API：不持有 `CommandRouter` / `LocalPetController` / `AccessibilityController`；
 *   单击只打开本地会话，识别文本通过回调交给宿主，由宿主经
 *   `LocalPetController.executeFromVoice`（来源 `LocalCommandSource.LOCAL_VOICE`）执行；
 * - 不发网络请求：本文件 import 里没有任何网络类型；
 * - 不用高频定时器：动画节奏取自素材包 `motion.idle_tick_ms`（下限 [PetPack.MIN_IDLE_TICK_MS] = 1000ms），
 *   且只在窗口真正在线时运行；
 * - 不常驻麦克风：录音只存在于长按期间（`press()` → 松手 `release()`/取消 `stop()`）。
 *
 * ## overlay_online 语义
 *
 * **只有 `WindowManager.addView` 成功**才 `overlay_online = true`。全部判定与代际（generation/ticket）
 * 在纯逻辑 [PetWindowOwner] 里；本类只是薄绑定层：addView 抛异常 →
 * [PetWindowOwner.markAttachFailed] → 对外仍为 `false` → 退化到应用内桌宠 + 本地通知，
 * 绝不显示假在线状态。
 *
 * ## 启动方式
 *
 * 只由前台设置页/用户显式操作 `startService`（`AndroidManifest` 里 `exported=false`）。
 * 不接受任何 Intent 参数，尤其不接受「执行命令 / 改模式 / 传入文本」这类载荷。
 */
class PetOverlayService : Service() {

    private lateinit var windowManager: WindowManager
    private lateinit var owner: PetWindowOwner
    private lateinit var machine: PetStateMachine
    private lateinit var preferences: PetPreferences

    /** 素材包几何参数（`res/raw/pet_pack.json`）；解析失败时退回 [PetPack.DEFAULT] 并只记本地日志。 */
    private lateinit var pack: PetPack

    private val mainHandler = Handler(Looper.getMainLooper())

    /** 当前窗口 View；`null` 表示没有 View 持有，在线判定以 [PetWindowOwner.overlayOnline] 为准。 */
    private var overlayView: View? = null

    /** 当前 View 对应的票据。收尾时用它判断能否 `removeView`。 */
    private var overlayTicket: WindowTicket? = null

    private var voice: LocalVoiceController? = null

    /** 实际使用的窗口类型（状态与审计用）。 */
    @Volatile
    var overlayType: PetOverlayType? = null
        private set

    private var idleTicker: Runnable? = null

    /**
     * 识别文本出口：**必须**由宿主注入。
     *
     * 未注入时按住说话会走 [onVoiceResultDropped]（明确提示），**绝不静默丢弃**。
     */
    private var voiceResultSink: ((String) -> Unit)? = null

    /** 最近一次语音结果的处理状态，供桌宠渲染与用户提示（不存任何正文）。 */
    private var lastVoiceNotice: String? = null

    /**
     * 锁屏接收器：屏幕熄灭 / 用户离屏时**必须释放麦克风**。
     *
     * 文档「Activity 销毁、屏幕锁定、模式切换和任务停止都必须调用 `stop()`」。
     * 用系统广播而不是靠配置变化，避免依赖 `configChanges` 的偶然回调。
     */
    private val screenReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            when (intent?.action) {
                Intent.ACTION_SCREEN_OFF -> {
                    stopVoice(cancel = true)
                    renderPet()
                }
            }
        }
    }

    private var receiverRegistered = false

    /** 低频刷新：只推进停留态回落（SUCCESS/ERROR → IDLE）与文案，间隔取自素材包（默认 1s）。 */
    private val idleTick = object : Runnable {
        override fun run() {
            if (!owner.overlayOnline) {
                idleTicker = null
                return
            }
            machine.refresh()
            renderPet()
            mainHandler.postDelayed(this, pack.motion.idleTickMs)
        }
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        windowManager = getSystemService(Context.WINDOW_SERVICE) as WindowManager
        owner = PetWindowOwner()
        machine = PetStateMachine(PetClock { SystemClockEpoch.elapsedMs() })
        preferences = PetPreferences(this)
        pack = PetPackLoader.load(this)
        // 登记存活实例，让 MainActivity 能注入语音出口（弱引用，onDestroy 清空）。
        liveServiceRef = java.lang.ref.WeakReference(this)
        registerScreenReceiver()
    }

    /**
     * 启动/刷新桌宠。
     *
     * 只认「开始显示」这个动作，**不读取任何 Intent 载荷**：桌宠不是第二个入口，不能被外部遥控。
     */
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startInForegroundIfNeeded()
        showOverlay()
        // 不 STICKY：进程被回收后不自行拉起悬浮窗，避免后台偷跑。
        return START_NOT_STICKY
    }

    // ---------- 窗口显示 ----------

    /**
     * 显示悬浮窗（幂等）。
     *
     * 选型固定（[PetOverlayPolicy]）：无障碍悬浮窗 → 普通悬浮窗 → 都不可用则退化。
     */
    private fun showOverlay() {
        if (owner.overlayOnline) return
        if (preferences.isHiddenToday()) {
            // 用户当天已拖到底部关闭显示：当天不再自动弹出。
            stopSelf()
            return
        }

        val type = PetOverlayPolicy.choose(currentCapability())
        if (type == null) {
            // 两种悬浮窗都不可用：退化到应用内桌宠 + 通知（不伪造在线）。
            notifyOverlayUnavailable()
            stopSelf()
            return
        }
        overlayType = type

        // 新一代 + 本代票据 + 需要摘掉的旧窗口票据。
        val claim = if (owner.generation == 0L) owner.beginGeneration() else owner.issueTicket()
        detachOldView(claim)

        val view = buildView()
        val ticket = claim.ticket
        try {
            windowManager.addView(view, layoutParams(type))
        } catch (_: Throwable) {
            // addView 失败（权限被回收 / BadToken / 无障碍断开）：绝不标记在线。
            owner.markAttachFailed(ticket)
            overlayType = null
            notifyOverlayUnavailable()
            return
        }

        overlayView = view
        overlayTicket = ticket
        when (owner.markAttached(ticket)) {
            AttachOutcome.ATTACHED -> {
                machine.show()
                renderPet()
                startIdleTicker()
            }
            AttachOutcome.SUPERSEDED -> {
                // 已被更新的一代/更新的票据取代：自己摘掉刚加进去的 View。
                runCatching { windowManager.removeView(view) }
                overlayView = null
                overlayTicket = null
            }
        }
    }

    /**
     * 摘掉上一轮留下的 View。
     *
     * 三种情况：
     * 1. `claim.superseded == overlayTicket`：票据被明确取代，`owner` 已不再认为它在线，
     *    **必须由调用方直接 `removeView`**（此时不能走 [PetWindowOwner.release]，它会返回 false）；
     * 2. 票据仍是 current：走正规收尾 [PetWindowOwner.release]；
     * 3. 票据已过期但不在 superseded 名单：属于旧代遗留，同样直接移除自己的 View。
     */
    private fun detachOldView(claim: WindowClaim) {
        val oldView = overlayView ?: return
        val oldTicket = overlayTicket
        overlayView = null
        overlayTicket = null
        if (oldTicket == null) {
            // 没有票据对应的旧 View：保守移除，避免残影。
            runCatching { windowManager.removeView(oldView) }
            return
        }
        if (claim.superseded == oldTicket) {
            runCatching { windowManager.removeView(oldView) }
            return
        }
        if (owner.isCurrent(oldTicket)) {
            if (owner.release(oldTicket)) runCatching { windowManager.removeView(oldView) }
        } else {
            runCatching { windowManager.removeView(oldView) }
        }
    }

    /** 悬浮窗能力探测（真实读取系统状态，不伪造）。 */
    private fun currentCapability(): OverlayCapability = OverlayCapability(
        sdkInt = Build.VERSION.SDK_INT,
        accessibilityConnected = WhaleAccessibilityService.instance != null,
        overlayPermissionGranted = Settings.canDrawOverlays(this),
    )

    /**
     * 窗口参数。
     *
     * - `ACCESSIBILITY_OVERLAY`：仅在无障碍已连接时使用，挂在无障碍窗口层之上，无需另授悬浮窗权限；
     * - `APPLICATION_OVERLAY`：`SYSTEM_ALERT_WINDOW` 授权后的兜底路径。
     *
     * 两者都带 `FLAG_NOT_FOCUSABLE`，因此**不抢输入法焦点**，也就不遮挡输入框与确认卡。
     */
    private fun layoutParams(type: PetOverlayType): WindowManager.LayoutParams {
        val windowType = when (type) {
            PetOverlayType.ACCESSIBILITY_OVERLAY -> WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY
            PetOverlayType.APPLICATION_OVERLAY -> WindowManager.LayoutParams.TYPE_APPLICATION_OVERLAY
        }
        return WindowManager.LayoutParams(
            WindowManager.LayoutParams.WRAP_CONTENT,
            WindowManager.LayoutParams.WRAP_CONTENT,
            windowType,
            WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
            PixelFormat.TRANSLUCENT,
        ).apply {
            gravity = Gravity.TOP or Gravity.START
            x = preferences.offsetX()
            y = preferences.offsetY()
        }
    }

    private fun startIdleTicker() {
        if (idleTicker != null) return
        idleTicker = idleTick
        mainHandler.postDelayed(idleTick, pack.motion.idleTickMs)
    }

    private fun stopIdleTicker() {
        idleTicker?.let { mainHandler.removeCallbacks(it) }
        idleTicker = null
    }

    // ---------- 视图与交互 ----------

    /**
     * 构建桌宠视图。
     *
     * 只显示状态文案 + 纯色圆形底：无网络素材、不复制外部项目形象
     * （「素材由本地 APK 资源提供」；首版不引入网络动画 SDK）。
     *
     * 尺寸/内边距/点击区域下限/文案基线全部取自 `res/raw/pet_pack.json`（[PetPack]），
     * 保证「尺寸、透明边界、点击区域、基线写入 pet-pack.json」这条硬要求真的生效。
     */
    private fun buildView(): View = TextView(this).apply {
        setTextColor(Color.WHITE)
        textSize = 13f
        // 尺寸：窗口最小尺寸取素材包 width/height（dp→px），与点击区域下限取较大值。
        val windowWidthPx = this@PetOverlayService.dp(pack.window.widthDp)
        val windowHeightPx = this@PetOverlayService.dp(pack.window.heightDp)
        minWidth = maxOf(windowWidthPx, this@PetOverlayService.dp(pack.tapArea.minWidthDp))
        minHeight = maxOf(windowHeightPx, this@PetOverlayService.dp(pack.tapArea.minHeightDp))
        // 透明边界（px）：圆形底之外留白，保证触摸命中有余量。
        val bounds = pack.window.transparentBounds
        // 文案基线：基线比例描述文字中心占窗口高度的比例，换算成底部留白让文字落在该位置。
        includeFontPadding = false
        val baselineBottomPx = (windowHeightPx * (1.0f - pack.baseline.textBaselineRatio))
            .coerceAtLeast(0f)
            .toInt()
        // 只调一次 setPadding：底部取「透明边界」与「基线留白」的较大值。
        setPadding(bounds.leftPx, bounds.topPx, bounds.rightPx, maxOf(bounds.bottomPx, baselineBottomPx))
        background = GradientDrawable().apply {
            shape = GradientDrawable.OVAL
            setColor(0xCC1F2937.toInt())
            setStroke(2, 0x66FFFFFF)
        }
        setOnTouchListener(petTouchListener())
    }

    /**
     * 触摸处理：单击 = 打开本地会话；长按 = 按住说话（松手即释放麦克风）；拖动 = 移动窗口。
     *
     * 长按**不执行任何命令**：识别文本经 [voiceResultSink] 交给宿主。
     */
    private fun petTouchListener(): View.OnTouchListener {
        var downX = 0f
        var downY = 0f
        var startX = 0
        var startY = 0
        var dragging = false
        var longPressing = false

        val longPressRunnable = Runnable {
            longPressing = true
            startVoice()
        }

        return View.OnTouchListener { view, event ->
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    downX = event.rawX
                    downY = event.rawY
                    startX = preferences.offsetX()
                    startY = preferences.offsetY()
                    dragging = false
                    longPressing = false
                    mainHandler.postDelayed(longPressRunnable, LONG_PRESS_MS)
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = (event.rawX - downX).toInt()
                    val dy = (event.rawY - downY).toInt()
                    if (!dragging && (kotlin.math.abs(dx) > DRAG_SLOP_PX || kotlin.math.abs(dy) > DRAG_SLOP_PX)) {
                        dragging = true
                        // 拖动不是按住说话：取消长按并释放麦克风。
                        mainHandler.removeCallbacks(longPressRunnable)
                        if (longPressing) stopVoice(cancel = true)
                        longPressing = false
                    }
                    if (dragging) moveOverlay(view, startX + dx, startY + dy)
                    true
                }
                MotionEvent.ACTION_UP -> {
                    mainHandler.removeCallbacks(longPressRunnable)
                    val wasLongPress = longPressing
                    longPressing = false
                    when {
                        wasLongPress -> stopVoice()          // 松手：进入 TRANSCRIBING，等结果后 destroy
                        dragging -> onDragFinished(view)     // 拖完松手：贴边或关闭
                        else -> openLocalSession()           // 单击：只打开本地会话
                    }
                    dragging = false
                    true
                }
                MotionEvent.ACTION_CANCEL -> {
                    // 被系统抢走手势：释放麦克风、回弹位置，不做任何“关闭/打开”副作用。
                    mainHandler.removeCallbacks(longPressRunnable)
                    if (longPressing) stopVoice(cancel = true)
                    longPressing = false
                    if (dragging) moveOverlay(view, startX, startY)
                    dragging = false
                    true
                }
                else -> false
            }
        }
    }

    private fun moveOverlay(view: View, x: Int, y: Int) {
        val params = view.layoutParams as? WindowManager.LayoutParams ?: return
        params.x = x
        params.y = y
        runCatching { windowManager.updateViewLayout(view, params) }
    }

    /**
     * 松手后：贴边吸附；若「拖到底部」则关闭当天显示。
     *
     * 判定在纯逻辑 [PetDragPolicy]；这里只负责持久化与窗口收尾。
     */
    private fun onDragFinished(view: View) {
        val params = view.layoutParams as? WindowManager.LayoutParams ?: return
        val metrics = resources.displayMetrics
        val height = view.height.coerceAtLeast(1)
        if (PetDragPolicy.shouldCloseOnRelease(params.y, height, metrics.heightPixels)) {
            preferences.markHiddenToday()
            hideOverlay()
            stopSelf()
            return
        }
        params.x = PetDragPolicy.snapLeftPx(params.x, view.width, metrics.widthPixels, EDGE_MARGIN_PX)
        runCatching { windowManager.updateViewLayout(view, params) }
        preferences.saveOffset(params.x, params.y)
    }

    /** 隐藏悬浮窗（保留 Service）；只有票据仍是当前在线票据才会真的摘窗口。 */
    private fun hideOverlay() {
        stopIdleTicker()
        stopVoice(cancel = true)
        machine.hide()
        val ticket = overlayTicket
        val view = overlayView
        overlayView = null
        overlayTicket = null
        if (ticket != null && view != null) releaseView(view, ticket)
    }

    /**
     * 正规收尾：只有 [PetWindowOwner.release] 返回 `true` 才 `removeView`。
     *
     * `false` 表示票据过期——例如旧 Activity/旧 producer 的 `onDestroy` 延迟执行到新一代窗口
     * 已经 attach 之后；此时**绝不能**动窗口，否则会摘掉新一代窗口。
     */
    private fun releaseView(view: View, ticket: WindowTicket) {
        if (owner.release(ticket)) {
            runCatching { windowManager.removeView(view) }
        }
    }

    /**
     * 单击桌宠：只请求打开本地会话。
     *
     * 发送一个**不含参数**的显式 Intent 给 `MainActivity`；桌宠不直接执行命令。
     */
    private fun openLocalSession() {
        runCatching {
            startActivity(
                Intent(this, MainActivity::class.java).apply {
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP)
                },
            )
        }
    }

    // ---------- 语音（按住说话） ----------

    /**
     * 开始按住说话。
     *
     * [LocalVoiceController.press] 内部**先查权限**：没有录音权限时不启动录音（产品验收）。
     */
    private fun startVoice() {
        val controller = voice ?: LocalVoiceController(
            context = this,
            onText = { text -> dispatchVoiceResult(text) },
            onError = { failure -> onVoiceFailure(failure) },
            onStateChanged = { renderPet() },
        ).also { voice = it }
        controller.reset()
        if (!controller.press()) {
            // 权限缺失 / 识别器不可用：press() 已通过 onError 报告，这里不再启动第二个识别器。
            renderPet()
        }
    }

    /**
     * 语音文本出口的**唯一**分发点。
     *
     * - 已注入 sink → 交给宿主（宿主负责结构化解析 + `LocalPetController.executeFromVoice`）；
     * - 未注入 sink → [onVoiceResultDropped] 明确提示，**不静默丢弃**（M09 硬要求）。
     */
    private fun dispatchVoiceResult(text: String) {
        val sink = voiceResultSink
        if (sink == null) {
            onVoiceResultDropped("语音入口未接线，本次识别结果未被处理")
            return
        }
        runCatching { sink.invoke(text) }.onFailure {
            onVoiceResultDropped("语音结果处理失败：${it.javaClass.simpleName}")
        }
    }

    /**
     * 语音结果被丢弃 / 处理失败：**必须**有明确反馈。
     *
     * 状态机切到 [PetState.ERROR]（桌宠可见地报错），并写本地日志；不上报、不联网、不写正文。
     */
    private fun onVoiceResultDropped(reason: String) {
        lastVoiceNotice = reason
        machine.onEvent(PetEvent.TASK_FAILED)
        renderPet()
        android.util.Log.w(LOG_TAG, "voice result not executed: $reason")
    }

    /**
     * 结束按住说话并**释放麦克风**。
     *
     * 松手 → [LocalVoiceController.release]（LISTENING → TRANSCRIBING，结果回来后自动 `destroy()`）；
     * 取消/拖动/锁屏/销毁 → [LocalVoiceController.stop]（立即 `cancel()` + `destroy()` → STOPPED）。
     */
    private fun stopVoice(cancel: Boolean = false) {
        val controller = voice ?: return
        if (controller.state == VoiceState.STOPPED) return
        if (cancel || controller.state == VoiceState.PRESSING) {
            controller.stop()
        } else {
            controller.release()
        }
    }

    /** 语音失败只做本地提示：不新增错误码、不写 `DeviceResponse.error_code`。 */
    private fun onVoiceFailure(failure: VoiceFailure) {
        renderPet()
        android.util.Log.w(LOG_TAG, "voice failure: ${failure.kind.name} ${failure.message}")
    }

    /**
     * 宿主接入点：把识别文本交给上层。
     *
     * 宿主（`MainActivity`）拿到文本后，经 `LocalVoiceCommandBridge` 转成
     * `DeviceEnvelope` 并调用 `LocalPetController.executeFromVoice`
     * （来源固定 `LocalCommandSource.LOCAL_VOICE`）。**本 Service 从不执行命令**，
     * 也不持有 `CommandRouter`/`LocalPetController`。
     *
     * 传 `null` 表示解绑；此后按住说话会走 [onVoiceResultDropped] 明确提示，而不是静默丢弃。
     */
    fun setVoiceResultSink(sink: ((String) -> Unit)?) {
        voiceResultSink = sink
    }

    // ---------- 状态渲染 ----------

    /**
     * 把 [PetState] 渲染成文案。
     *
     * 低频率：只由事件、1s idle tick、语音状态变化驱动；没有任何高频动画循环。
     */
    private fun renderPet() {
        val view = overlayView as? TextView ?: return
        val state = machine.state
        val voiceState = voice?.state
        val notice = lastVoiceNotice
        view.text = when {
            voiceState == VoiceState.PRESSING -> "准备录音…"
            voiceState == VoiceState.LISTENING -> "正在听…"
            voiceState == VoiceState.TRANSCRIBING -> "识别中…"
            // 失败提示优先于普通状态文案：语音被丢弃必须让用户看见。
            notice != null && state == PetState.ERROR -> notice
            else -> state.label
        }
        // 等待确认 / 等待回答必须清晰可见：只调不透明度，不依赖动画。
        view.alpha = if (state == PetState.WAITING_CONFIRM || state == PetState.WAITING_USER) 1.0f else 0.92f
    }

    /** 由宿主（M06 任务运行时 / M01 模式切换）转发的固定事件。 */
    fun emitPetEvent(event: PetEvent) {
        machine.onEvent(event)
        renderPet()
    }

    // ---------- 通知与生命周期 ----------

    /**
     * 悬浮窗不可用时的本地通知。
     *
     * 产品文档「权限失败时退化到应用内桌宠和通知」：只发一条通知，不申请权限、不重试。
     */
    private fun notifyOverlayUnavailable() {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        ensureChannel(manager, CHANNEL_STATUS, "桌宠状态", NotificationManager.IMPORTANCE_LOW)
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_STATUS)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        val notification: Notification = builder
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle("桌宠未显示")
            .setContentText("悬浮窗不可用，已在应用内显示桌宠状态")
            .build()
        runCatching { manager.notify(NOTIFICATION_ID_STATUS, notification) }
    }

    /**
     * 前台化：悬浮窗服务必须前台运行，避免被系统回收导致窗口残留。
     *
     * 通知只含固定文案，不携带任何用户数据。
     */
    private fun startInForegroundIfNeeded() {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        ensureChannel(manager, CHANNEL_SERVICE, "桌宠服务", NotificationManager.IMPORTANCE_MIN)
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_SERVICE)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        val notification: Notification = builder
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle("桌宠运行中")
            .setContentText("状态指示器已开启")
            .setOngoing(true)
            .build()
        runCatching { startForeground(FOREGROUND_ID, notification) }
    }

    private fun ensureChannel(manager: NotificationManager, id: String, name: String, importance: Int) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        runCatching { manager.createNotificationChannel(NotificationChannel(id, name, importance)) }
    }

    /**
     * 注册屏幕熄灭接收器（**只为释放麦克风**，不接收任何其他广播）。
     *
     * Android 14（API 34）起动态注册非系统广播必须显式声明导出标志，否则 `registerReceiver`
     * 直接抛 `SecurityException`；这里固定 `RECEIVER_NOT_EXPORTED`，不接受外部应用触发。
     */
    private fun registerScreenReceiver() {
        if (receiverRegistered) return
        runCatching {
            val filter = IntentFilter(Intent.ACTION_SCREEN_OFF)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                registerReceiver(screenReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
            } else {
                @Suppress("UnspecifiedRegisterReceiverFlag")
                registerReceiver(screenReceiver, filter)
            }
            receiverRegistered = true
        }
    }

    private fun unregisterScreenReceiver() {
        if (!receiverRegistered) return
        runCatching { unregisterReceiver(screenReceiver) }
        receiverRegistered = false
    }

    /** 方向变化等配置变化：释放可能存在的录音，重绘文案。 */
    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        // 配置变化时系统可能已重建输入通道：保守释放麦克风，避免录音跨界面存活。
        if (voice?.isRecording == true) stopVoice(cancel = true)
        renderPet()
    }

    /**
     * Service 销毁：释放麦克风、摘窗口、停低频定时器、注销接收器。
     *
     * [PetWindowOwner.forceRelease] 兜底，保证即使票据判定混乱也不会留下幽灵窗口。
     */
    override fun onDestroy() {
        stopIdleTicker()
        runCatching { voice?.stop() }
        voice = null
        machine.hide()
        val view = overlayView
        overlayView = null
        overlayTicket = null
        if (view != null) {
            owner.forceRelease()
            runCatching { windowManager.removeView(view) }
        }
        unregisterScreenReceiver()
        // 清空存活登记：Service 结束后 bindVoiceSink 不再持有本实例。
        liveServiceRef = null
        runCatching { stopForeground(true) }
        super.onDestroy()
    }

    companion object {
        private const val LOG_TAG = "WhalePetOverlay"
        private const val CHANNEL_STATUS = "pet_overlay"
        private const val CHANNEL_SERVICE = "pet_service"
        private const val NOTIFICATION_ID_STATUS = 4109
        private const val FOREGROUND_ID = 4108

        /**
         * 当前存活的 Service 实例（弱引用，避免泄漏）。
         *
         * 由 [onCreate] 登记、[onDestroy] 清空；`MainActivity` 通过 [bindVoiceSink] 注入语音出口，
         * 不需要持有 Service 强引用。同进程内访问：不跨进程、不写磁盘、不发网络。
         */
        private var liveServiceRef: java.lang.ref.WeakReference<PetOverlayService>? = null

        /**
         * 把语音结果出口注入当前存活的桌宠 Service。
         *
         * Service 未运行时静默不做事（不是错误：桌宠没开时本就没有语音入口）。
         * 传 `null` 表示解绑。
         */
        fun bindVoiceSink(sink: ((String) -> Unit)?) {
            liveServiceRef?.get()?.setVoiceResultSink(sink)
        }

        /**
         * 空闲刷新间隔的**兜底默认值**：真正生效值取自 `pet_pack.json` 的 `motion.idle_tick_ms`，
         * 且不得小于 [PetPack.MIN_IDLE_TICK_MS]（强制低频，禁止高频定时器）。
         */
        const val IDLE_TICK_MS: Long = PetPack.MIN_IDLE_TICK_MS

        /** 长按判定：按下超过该时长才算「按住说话」。 */
        const val LONG_PRESS_MS: Long = 500L

        /** 拖拽判定阈值，避免轻微抖动被当成拖动。 */
        const val DRAG_SLOP_PX: Int = 24

        /** 贴边保留间距（px）。 */
        const val EDGE_MARGIN_PX: Int = 8
    }
}

/** 单调时钟：桌宠停留态用 `elapsedRealtime`，不受系统时间跳变影响。 */
object SystemClockEpoch {
    fun elapsedMs(): Long = android.os.SystemClock.elapsedRealtime()
}

/** dp → px（调用点必须已持有 `Context`/`Resources`）。 */
private fun Context.dp(value: Int): Int =
    (value * resources.displayMetrics.density).toInt()

// ---------------------------------------------------------------------------
// 素材包（`res/raw/pet_pack.json`）
// ---------------------------------------------------------------------------

/**
 * 素材包几何参数。
 *
 * 规范：`docs/modules/M09-local-pet-voice-development.md`「依赖和素材」——
 * 「素材由本地 APK 资源提供，尺寸、透明边界、点击区域和基线写入 `pet-pack.json`」。
 * 资源位置固定 `res/raw/pet_pack.json`（`res/xml` 只收 XML，放 JSON 会被 aapt2 拒）。
 *
 * 全部字段都有默认值：JSON 缺字段或整个文件损坏时退回 [DEFAULT]，**绝不崩溃**、绝不联网取素材。
 */
@Serializable
data class PetPack(
    @SerialName("pack_id")
    val packId: String = "whale_pet_local_v1",
    val version: Int = 1,
    val window: Window = Window(),
    /** JSON 里是 `tap_area`。 */
    @SerialName("tap_area")
    val tapArea: TapArea = TapArea(),
    val baseline: Baseline = Baseline(),
    val motion: Motion = Motion(),
) {
    @Serializable
    data class Window(
        @SerialName("width_dp")
        val widthDp: Int = 88,
        @SerialName("height_dp")
        val heightDp: Int = 88,
        @SerialName("transparent_bounds")
        val transparentBounds: TransparentBounds = TransparentBounds(),
    )

    @Serializable
    data class TransparentBounds(
        @SerialName("left_px")
        val leftPx: Int = 12,
        @SerialName("top_px")
        val topPx: Int = 12,
        @SerialName("right_px")
        val rightPx: Int = 12,
        @SerialName("bottom_px")
        val bottomPx: Int = 12,
    )

    @Serializable
    data class TapArea(
        @SerialName("min_width_dp")
        val minWidthDp: Int = 48,
        @SerialName("min_height_dp")
        val minHeightDp: Int = 48,
    )

    @Serializable
    data class Baseline(
        @SerialName("text_baseline_ratio")
        val textBaselineRatio: Float = 0.62f,
    )

    @Serializable
    data class Motion(
        @SerialName("idle_tick_ms")
        val idleTickMs: Long = 1_000L,
    )

    companion object {
        /** 兜底配置：与首次硬编码版本一致，保证资源损坏时行为不变。 */
        val DEFAULT = PetPack()

        /** 空闲刷新下限：**强制低频**，即使 JSON 写了更小的值也不允许突破。 */
        const val MIN_IDLE_TICK_MS: Long = 1_000L
    }
}

/**
 * 素材包读取器。
 *
 * - 只读本地 `res/raw`，不发任何网络请求；
 * - `ignoreUnknownKeys = true`：素材包允许出现未来版本新增字段，不因此解析失败；
 * - 任何异常（文件缺失、JSON 损坏、字段类型不符）都退回 [PetPack.DEFAULT]，只记本地日志。
 */
object PetPackLoader {

    /** 资源名固定（`res/raw/pet_pack.json`）。 */
    const val RESOURCE_NAME: String = "pet_pack"

    private val json = Json {
        ignoreUnknownKeys = true
        isLenient = true
        coerceInputValues = true
    }

    fun load(context: Context): PetPack {
        val raw = runCatching {
            context.resources.openRawResource(
                context.resources.getIdentifier(RESOURCE_NAME, "raw", context.packageName),
            ).use { it.bufferedReader().readText() }
        }.getOrNull()
        if (raw.isNullOrBlank()) return PetPack.DEFAULT

        val parsed = runCatching { json.decodeFromString<PetPack>(raw) }.getOrElse {
            android.util.Log.w("WhalePetOverlay", "pet_pack.json 解析失败，使用默认几何参数: ${it.javaClass.simpleName}")
            PetPack.DEFAULT
        }
        // 强制低频：空闲刷新间隔不得小于下限。
        return if (parsed.motion.idleTickMs < PetPack.MIN_IDLE_TICK_MS) {
            parsed.copy(motion = parsed.motion.copy(idleTickMs = PetPack.MIN_IDLE_TICK_MS))
        } else {
            parsed
        }
    }
}

/** 状态文案（与固定状态集合一一对应，不含任何正文内容）。 */
private val PetState.label: String
    get() = when (this) {
        PetState.HIDDEN -> "已隐藏"
        PetState.IDLE -> "空闲"
        PetState.WORKING -> "执行中"
        PetState.WAITING_CONFIRM -> "等待确认"
        PetState.WAITING_USER -> "等待回答"
        PetState.SUCCESS -> "成功"
        PetState.ERROR -> "失败"
        PetState.OFFLINE -> "离线"
    }

/**
 * 桌宠本地偏好：只存窗口位置与「当天已关闭」，**不存任何正文/消息内容**。
 *
 * 「当天」用日期字符串比较，不依赖定时器；跨天自动恢复显示。
 */
class PetPreferences(context: Context) {
    private val preferences = context.getSharedPreferences(PREFERENCES_NAME, Context.MODE_PRIVATE)

    fun offsetX(): Int = preferences.getInt(KEY_X, 0)

    fun offsetY(): Int = preferences.getInt(KEY_Y, DEFAULT_Y)

    fun saveOffset(x: Int, y: Int) {
        preferences.edit().putInt(KEY_X, x).putInt(KEY_Y, y).apply()
    }

    /** 用户当天已拖到底部关闭显示。 */
    fun isHiddenToday(): Boolean = preferences.getString(KEY_HIDDEN_DAY, null) == today()

    fun markHiddenToday() {
        preferences.edit().putString(KEY_HIDDEN_DAY, today()).apply()
    }

    /** 用户显式再次打开桌宠（设置页点击）时清除「当天关闭」标记。 */
    fun clearHiddenToday() {
        preferences.edit().remove(KEY_HIDDEN_DAY).apply()
    }

    private fun today(): String {
        val calendar = java.util.Calendar.getInstance()
        return "%04d-%02d-%02d".format(
            calendar.get(java.util.Calendar.YEAR),
            calendar.get(java.util.Calendar.MONTH) + 1,
            calendar.get(java.util.Calendar.DAY_OF_MONTH),
        )
    }

    private companion object {
        const val PREFERENCES_NAME = "pet_overlay_ui"
        const val KEY_X = "offset_x"
        const val KEY_Y = "offset_y"
        const val KEY_HIDDEN_DAY = "hidden_day"
        const val DEFAULT_Y = 600
    }
}
