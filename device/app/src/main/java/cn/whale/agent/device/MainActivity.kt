package cn.whale.agent.device

import android.Manifest
import android.app.AlertDialog
import android.content.ComponentName
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.os.Looper
import android.provider.Settings
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.activity.result.ActivityResultLauncher
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import java.io.File
import androidx.activity.result.contract.ActivityResultContract
import androidx.core.content.ContextCompat
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/**
 * M01 设置页：模式入口（唯一允许切换模式的界面）。
 *
 * 固定行为（`docs/modules/M01-system-modes-product.md`）：
 * - 模式切换只能由这里的**明确点击 + 二次确认**触发；不使用 Switch 滑动控件，避免误触。
 * - 首次切到 `LOCAL_PET` 必须显示权限说明（无障碍、悬浮窗、录音），且**不得自动打开麦克风或悬浮窗**。
 * - 权限缺失时界面显示「不可用」，不得显示在线假状态。
 * - 模型输出、语音、QQ 消息都无法到达本页面；本页面不接受任何来自 Intent 的模式参数。
 *
 * 本 Activity 只承载 UI 与权限探测；切换动作、停止任务、关闭旧入口、权限重查都在
 * [ModeSwitchCoordinator] 里完成。
 */
class MainActivity : ComponentActivity() {
    private lateinit var modeStore: AgentModeStore
    private lateinit var session: LocalSessionGate
    private lateinit var coordinator: ModeSwitchCoordinator

    /**
     * M09 语音入口的本地出口：语音文本 → 结构化命令 → [LocalPetController.executeFromVoice]。
     *
     * 只在 `LOCAL_PET` + 权限齐全 + 会话已解锁时可用（见 [canUseVoiceEntry]）。
     */
    private lateinit var localPet: LocalPetController

    /** 设置页内的按住说话控制器（与桌宠 Service 各自持有实例，录音互斥由 `VoiceMicWatch` 保证）。 */
    private var voiceController: LocalVoiceController? = null

    /** 本机设备 id（语音构造信封时使用；来自配对存储，缺失时用固定本机标识）。 */
    private var deviceId: String = ""

    /** 语音请求的单调计数器与幂等键序号（仅本地使用，不参与远程 WSS 的 counter 校验）。 */
    private var voiceSequence: Long = 0L

    private lateinit var modeTitle: TextView
    private lateinit var availability: TextView
    private lateinit var permissionsView: TextView
    private lateinit var switchButton: Button
    private lateinit var unlockButton: Button
    private lateinit var voiceButton: Button
    private lateinit var voiceStatus: TextView
    private lateinit var unlockCredentialInput: EditText
    private lateinit var unlockStatus: TextView

    /**
     * M01 远程连接配置区控件。
     *
     * 写的就是 [DeviceConnectionService] 与 [resolveDeviceId] 读取的那两个键
     * （[ConnectionSettings.PREFERENCES_NAME] 的 `wss_url` / `device_id`），不新造键名。
     */
    private lateinit var connectionUrlInput: EditText
    private lateinit var connectionDeviceIdInput: EditText
    private lateinit var connectionStatus: TextView

    private var permissions: PermissionSnapshot = PermissionSnapshot(false, false, false)

    // ---------- M08 相册选图（`device.media.pick`）的前台宿主登记 ----------

    /**
     * 本页对 `MediaPickerHosts` 的登记凭证。
     *
     * 在 `onStart` 生成并 attach，在 `onStop` 与 `onDestroy` 撤销；`null` 表示本页此刻
     * **没有**登记（例如还没 onStart，或已经 onStop）。重复 onStart（onStop 未发生时被配错
     * 顺序调用）会先撤销旧的再登记新的，绝不叠加两份宿主。
     */
    private var mediaPickerHost: MediaPickerHost? = null

    /**
     * 一次 `device.media.pick` 的在途请求槽。
     *
     * `MediaPickerDelegate.pick` 是**阻塞**语义：它要把用户在选择器里点选的结果交回来。
     * 命令却在 OkHttp 线程同步执行，因此这里用「启动选择器 + 等回调 + 超时如实失败」：
     * `null` = 当前没有在途请求；不为 `null` 时，任何一次 picker 回包都归它。
     */
    private var pendingPicker: PendingPicker? = null

    /**
     * 本次选择要走的合同：`null` 表示走系统 Photo Picker（API 33+），
     * 非 `null` 表示走 `ACTION_OPEN_DOCUMENT`（API 26–32 或音频请求）。
     *
     * Android 要求 `registerForActivityResult` 在 `onCreate` 期间完成注册（注册次数固定），
     * 所以这里**只注册一次**，由这个字段决定本次用哪个合同，而不是每次 pick 都新注册一个。
     */
    private var pickerLegacyIntent: Intent? = null

    /** 注册好的选择器启动器；`onCreate` 里注册、`onDestroy` 清空。 */
    private var pickerLauncher: ActivityResultLauncher<PickVisualMediaRequest>? = null
    private var legacyPickerLauncher: ActivityResultLauncher<Intent>? = null

    /** 选择器回包后要落库的位置：解析用户选中 URI 用的 Controller（在 `onCreate` 装配一次）。 */
    private var pickerController: PhotoPickerController? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WhaleUi.setup(this)
        // M08：选择器启动器必须在 onCreate 内注册（Activity 要求注册次数固定）。
        // 两个启动器分别对应 API 33+ 的 Photo Picker 与 API 26–32（含音频）的 ACTION_OPEN_DOCUMENT。
        pickerController = PhotoPickerController(
            context = this,
            resolver = contentResolver,
            cacheDir = cacheDir,
            // 与 MediaWiringImpl 共用**同一份**受控素材表语义：都落在 filesDir/media_assets。
            assets = AssetStore(File(filesDir, MediaWiringImpl.ANDROID_ASSET_DIR)),
        )
        // 只在创建期注册，注册次数因此固定；到底走哪个合同由 pickerLegacyIntent 在发起时决定。
        pickerLauncher = registerForActivityResult(photoPickerContract()) { uri ->
            completePicker(listOfNotNull(uri))
        }
        legacyPickerLauncher = registerForActivityResult(legacyPickerContract()) { uris ->
            completePicker(uris.orEmpty())
        }
        // 恢复被杀时仍有在途请求的最近一次请求，避免「回包到了却没有人认领」。
        restorePendingPicker(savedInstanceState)

        modeStore = AgentModeStore(this)
        session = LocalSessionGate(SystemSessionClock)
        coordinator = ModeSwitchCoordinator(
            modeStore = modeStore,
            // 模式切换必须停止当前任务并释放麦克风（M09 文档：Activity 销毁/锁屏/模式切换/任务停止都要 stop()）。
            taskStopper = TaskStopper {
                // M09 已经提供进程级录音登记表；这里不持有 Service 句柄也能释放麦克风。
                VoiceMicWatch.releaseAll()
            },
            pendingActions = pendingActions(),
            remoteIngress = AndroidRemoteIngress(
                stopService = { stopDeviceConnectionService() },
                startService = { startDeviceConnectionService() },
            ),
            permissionChecker = PermissionChecker { currentPermissionSnapshot() },
            logger = { message -> android.util.Log.w(LOG_TAG, message) },
        )
        permissions = currentPermissionSnapshot()
        // M09 语音出口：语音文本经本地闸门执行；本 Activity 是它的注入方。
        localPet = LocalPetController(this, session)
        deviceId = resolveDeviceId()
        // M09 接线：把语音出口注入存活的桌宠 Service（未运行时不做事）。
        // 桌宠长按说话产生的文本也会走同一条 [handleVoiceText] 路径。
        PetOverlayService.bindVoiceSink { text -> handleVoiceText(text) }
        setContentView(buildDashboard())
        render(ModeSwitchResult(changed = false, mode = modeStore.get(), permissions = permissions))
    }

    /**
     * 把主内容包进 `ScrollView`。
     *
     * 为什么必须包：本页内容随功能增加已经超过一屏（模式区 + 连接配置区的
     * 输入框、三个按钮、排错提示）。`LinearLayout` 直接 `setContentView` 时，
     * **超出屏幕的部分不会滚动也不会被裁掉提示**——用户看到的是半截界面，
     * 下方的「保存并启动连接」「扫码配对」按钮根本点不到。
     *
     * 这个缺陷在真机（华为 CTR-AL20 / 1080x2388）上才暴露：
     * 编译、单测、截图工具都正常，只有真的用手指往上滑才发现页面纹丝不动。
     */
    private fun wrapInScrollView(content: LinearLayout): android.widget.ScrollView =
        android.widget.ScrollView(this).apply {
            isFillViewport = true
            addView(
                content,
                android.widget.FrameLayout.LayoutParams(
                    android.widget.FrameLayout.LayoutParams.MATCH_PARENT,
                    android.widget.FrameLayout.LayoutParams.WRAP_CONTENT,
                ),
            )
        }

    private fun buildDashboard(): View {
        val shell = WhaleUi.column(this)
        shell.setBackgroundColor(WhaleUi.background)
        val pages = android.widget.FrameLayout(this)
        shell.addView(pages, LinearLayout.LayoutParams(-1, 0, 1f))
        val home = buildLayout()
        val connection = WhaleUi.page(this).apply {
            addView(WhaleUi.title(this@MainActivity, "连接电脑"))
            addView(WhaleUi.text(this@MainActivity, "扫码完成配对后，就可以从电脑使用这部手机。"))
            addConnectionSection(this)
        }
        val homePage = WhaleUi.scroll(this, home)
        val connectionPage = WhaleUi.scroll(this, connection).apply { visibility = View.GONE }
        pages.addView(homePage)
        pages.addView(connectionPage)
        val nav = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            setPadding(WhaleUi.dp(context, 16), WhaleUi.dp(context, 6), WhaleUi.dp(context, 16), WhaleUi.dp(context, 10))
            setBackgroundColor(WhaleUi.surface)
        }
        val homeButton = WhaleUi.button(this, "首页", true) { }
        val connectionButton = WhaleUi.button(this, "电脑连接") { }
        fun select(connectedPage: Boolean) {
            homePage.visibility = if (connectedPage) View.GONE else View.VISIBLE
            connectionPage.visibility = if (connectedPage) View.VISIBLE else View.GONE
            WhaleUi.styleButton(homeButton, !connectedPage)
            WhaleUi.styleButton(connectionButton, connectedPage)
            homeButton.layoutParams = LinearLayout.LayoutParams(0, -2, 1f)
            connectionButton.layoutParams = LinearLayout.LayoutParams(0, -2, 1f)
        }
        homeButton.setOnClickListener { select(false) }
        connectionButton.setOnClickListener { select(true) }
        nav.addView(homeButton, LinearLayout.LayoutParams(0, -2, 1f))
        nav.addView(connectionButton, LinearLayout.LayoutParams(0, -2, 1f))
        shell.addView(nav)
        return shell
    }

    private fun buildLayout(): LinearLayout = WhaleUi.page(this).apply {
        addView(WhaleUi.text(this@MainActivity, "Whale 手机助手", 13f))
        addView(WhaleUi.title(this@MainActivity, "你好，让手机帮上忙"))
        addView(WhaleUi.text(this@MainActivity, "连接电脑，或在手机上与桌宠互动。"))
        val hero = WhaleUi.card(this@MainActivity, "我的手机", hero = true)
        modeTitle = WhaleUi.text(this@MainActivity, "", 23f, android.graphics.Color.WHITE)
        hero.addView(modeTitle)
        availability = WhaleUi.text(this@MainActivity, "", 14f, android.graphics.Color.WHITE)
        hero.addView(availability)
        hero.addView(WhaleUi.button(this@MainActivity, "扫一扫，连接电脑") {
            startActivity(Intent(this@MainActivity, PairingActivity::class.java))
        })
        addView(hero)
        val mode = WhaleUi.card(this@MainActivity, "使用方式")
        mode.addView(WhaleUi.text(this@MainActivity, "远程控制适合从电脑和 QQ 使用；本地桌宠在这部手机上陪伴你。"))
        switchButton = WhaleUi.button(this@MainActivity, "切换使用方式") { onSwitchClicked() }
        mode.addView(switchButton)
        addView(mode)
        val permissions = WhaleUi.card(this@MainActivity, "权限与准备")
        permissionsView = WhaleUi.text(this@MainActivity, "")
        permissions.addView(permissionsView)
        permissions.addView(WhaleUi.button(this@MainActivity, "管理所需权限") {
            startActivity(Intent(this@MainActivity, PermissionGuideActivity::class.java))
        })
        permissions.addView(WhaleUi.button(this@MainActivity, "允许小鲸鱼查看相册") {
            val permission = if (android.os.Build.VERSION.SDK_INT >= 33) Manifest.permission.READ_MEDIA_IMAGES else Manifest.permission.READ_EXTERNAL_STORAGE
            requestPermissions(arrayOf(permission), GALLERY_PERMISSION_REQUEST)
        })
        addView(permissions)
        val autoUnlock = WhaleUi.card(this@MainActivity, "锁屏时自动解锁")
        autoUnlock.addView(WhaleUi.text(this@MainActivity, "仅在你明确开启后使用。密码只在本机加密保存，不会发送给电脑或 AI；图案锁暂不支持。"))
        unlockCredentialInput = EditText(this@MainActivity).apply {
            hint = "输入 PIN 或锁屏密码"
            inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD
            setSingleLine(true)
            isSaveEnabled = false
            if (android.os.Build.VERSION.SDK_INT >= 26) importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS
        }
        autoUnlock.addView(unlockCredentialInput)
        unlockStatus = WhaleUi.text(this@MainActivity, if (UnlockCredentialStore.isConfigured(this@MainActivity)) "自动解锁已开启" else "自动解锁已关闭")
        autoUnlock.addView(unlockStatus)
        autoUnlock.addView(WhaleUi.button(this@MainActivity, "保存并允许自动解锁") { saveUnlockCredential() })
        autoUnlock.addView(WhaleUi.button(this@MainActivity, "关闭自动解锁") { clearUnlockCredential() })
        autoUnlock.addView(WhaleUi.button(this@MainActivity, "测试自动解锁（5 秒后）") {
            unlockStatus.text = "请在 5 秒内按电源键锁屏，随后会自动尝试一次"
            val testId = "local-unlock-" + java.util.UUID.randomUUID()
            Thread {
                Thread.sleep(5000)
                val result = UnlockController(applicationContext).unlock(DeviceEnvelope(
                    request_id = testId, device_id = resolveDeviceId(), command = "device.system.unlock",
                    deadline_ms = 20000, idempotency_key = testId,
                ))
                runOnUiThread { unlockStatus.text = if (result.status == "ok") "测试成功：手机已解锁" else "测试失败：" + result.message }
            }.start()
        })
        addView(autoUnlock)
        val local = WhaleUi.card(this@MainActivity, "和桌宠说句话")
        unlockButton = WhaleUi.button(this@MainActivity, "解锁本次互动") { onUnlockClicked() }
        local.addView(unlockButton)
        voiceButton = WhaleUi.button(this@MainActivity, "按住说话", true) { }
        voiceButton.setOnTouchListener { _, event -> onVoiceTouch(event) }
        local.addView(voiceButton)
        voiceStatus = WhaleUi.text(this@MainActivity, "")
        local.addView(voiceStatus)
        addView(local)
    }

    private fun saveUnlockCredential() {
        val value = unlockCredentialInput.text?.toString().orEmpty()
        if (value.isBlank()) {
            unlockStatus.text = "请输入 PIN 或锁屏密码"
            return
        }
        runCatching {
            UnlockCredentialStore.save(
                this,
                UnlockCredentialStore.Credential(
                    value,
                    if (value.all { it.isDigit() }) UnlockCredentialStore.Type.PIN else UnlockCredentialStore.Type.PASSWORD,
                ),
            )
        }.onSuccess {
            unlockCredentialInput.text?.clear()
            unlockStatus.text = "已允许自动解锁（密码已加密保存在本机）"
        }.onFailure {
            unlockStatus.text = "保存失败，请确认系统支持安全存储"
        }
    }

    private fun clearUnlockCredential() {
        UnlockCredentialStore.clear(this)
        unlockCredentialInput.text?.clear()
        unlockStatus.text = "自动解锁已关闭"
    }

    private fun addConnectionSection(root: LinearLayout) {
        val summary = WhaleUi.card(this, "连接状态")
        connectionStatus = WhaleUi.text(this, "", 16f, WhaleUi.ink)
        summary.addView(connectionStatus)
        summary.addView(WhaleUi.button(this, "扫一扫，连接电脑", true) {
            startActivity(Intent(this, PairingActivity::class.java))
        })
        summary.addView(WhaleUi.button(this, "启动连接") { onSaveAndStartConnection() })
        summary.addView(WhaleUi.button(this, "停止连接") {
            stopDeviceConnectionService()
            renderConnectionState()
        })
        root.addView(summary)
        val advanced = WhaleUi.card(this, "手动连接")
        advanced.addView(WhaleUi.text(this, "已扫码配对时无需修改。仅在需要更换连接地址时使用。"))
        advanced.addView(WhaleUi.text(this, "电脑连接地址"))
        connectionUrlInput = WhaleUi.input(this, "例如 wss://192.168.1.10:8765/device").apply {
            setSingleLine()
            setText(currentConnectionPreferences().getString(ConnectionSettings.KEY_WSS_URL, "").orEmpty())
        }
        advanced.addView(connectionUrlInput)
        advanced.addView(WhaleUi.text(this, "设备标识（配对时自动生成）"))
        connectionDeviceIdInput = WhaleUi.input(this, "设备标识").apply {
            setSingleLine()
            setText(currentConnectionPreferences().getString(ConnectionSettings.KEY_DEVICE_ID, "").orEmpty())
        }
        advanced.addView(connectionDeviceIdInput)
        advanced.addView(WhaleUi.button(this, "保存并重新连接", true) { onSaveAndStartConnection() })
        root.addView(WhaleUi.collapsible(this, "手动连接设置", advanced))
        val help = WhaleUi.card(this, "连接不上？")
        help.addView(WhaleUi.text(this, "确认电脑已启动主机，手机与电脑的网络可以互通。若提示证书变化，请重新扫码配对。"))
        help.addView(WhaleUi.button(this, "刷新连接状态") { renderConnectionState() })
        root.addView(help)
        renderConnectionState()
    }

    /**
     * 模式切换按钮：只处理用户点击，先弹二次确认对话框。
     *
     * 首次切到本地模式时，对话框正文包含权限说明；这里**不会**请求或启动麦克风/悬浮窗。
     */
    private fun onSwitchClicked() {
        val current = modeStore.get()
        val target = if (current.isLocal) AgentMode.REMOTE_B else AgentMode.LOCAL_PET
        val introNeeded = target.isLocal && !localPetIntroShown()
        val latest = currentPermissionSnapshot()
        val body = buildString {
            append(if (target.isLocal) "切换到「本地桌宠（LOCAL_PET）」？" else "切换到「远程控制（REMOTE_B）」？")
            append("\n\n")
            if (target.isLocal) {
                append("切换会：停止当前任务、清空未执行动作、关闭远程 WebSocket 连接、重新检查权限。")
                if (introNeeded || !latest.localPetReady) {
                    append("\n\n本地模式需要的权限（不会自动开启）：")
                    if (!latest.accessibilityEnabled) append("\n• 无障碍服务：用于点击、滚动、输入")
                    if (!latest.overlayGranted) append("\n• 悬浮窗：用于显示桌宠")
                    if (!latest.recordAudioGranted) append("\n• 录音：仅按住说话时使用，不做常驻监听")
                    if (latest.localPetReady) append("（当前三项都已具备）")
                }
            } else {
                append("切换会：停止当前任务、清空未执行动作、结束本地交互会话、由本页启动远程连接服务。")
            }
        }
        AlertDialog.Builder(this)
            .setTitle("确认切换模式")
            .setMessage(body)
            .setNegativeButton("取消", null)
            .setPositiveButton("确认切换") { _, _ -> performSwitch(target, introNeeded) }
            .show()
    }

    private fun performSwitch(target: AgentMode, introNeeded: Boolean) {
        // 来源固定为设置页点击；其他来源在 coordinator 内被拒绝。
        val result = coordinator.requestSwitch(target, ModeSwitchSource.SETTINGS_PAGE_CLICK)
        permissions = result.permissions ?: currentPermissionSnapshot()
        if (result.changed && target == AgentMode.LOCAL_PET) {
            // 切到本地模式必须没有远程连接；并且本地会话需要重新解锁。
            session.lock()
        }
        if (result.changed && target == AgentMode.REMOTE_B) {
            session.lock()
        }
        render(result)
        renderConnectionState()
        // 首次切到 LOCAL_PET：显示权限说明（不自动开启任何权限）。
        if (result.changed && target == AgentMode.LOCAL_PET && introNeeded) {
            showPermissionIntro(permissions, introNeeded = true)
        }
    }

    /** 用户显式解锁本地交互会话；超时后需再次点按。 */
    private fun onUnlockClicked() {
        if (!modeStore.get().isLocal) {
            session.lock()
            render(ModeSwitchResult(changed = false, mode = modeStore.get(), permissions = permissions))
            return
        }
        if (!permissions.localPetReady) {
            // 权限缺失时不允许伪造可用状态，也不启动任何采集。
            render(ModeSwitchResult(changed = false, mode = modeStore.get(), permissions = permissions))
            return
        }
        session.unlock()
        render(ModeSwitchResult(changed = false, mode = modeStore.get(), permissions = permissions))
    }

    // ---------- M09 按住说话 ----------

    /** 语音入口是否可用：`LOCAL_PET` + 三项权限齐全。会话未解锁不算不可用，按下时再提示。 */
    private fun canUseVoiceEntry(): Boolean = modeStore.get().isLocal && permissions.localPetReady

    /**
     * 按住说话的手势处理：按下开始、松开停止、取消释放麦克风。
     *
     * 权限/模式不满足时**不启动录音**，只给提示。
     */
    private fun onVoiceTouch(event: android.view.MotionEvent): Boolean {
        if (!canUseVoiceEntry()) {
            if (event.actionMasked == android.view.MotionEvent.ACTION_DOWN) {
                voiceStatus.text = voiceUnavailableReason()
            }
            return false
        }
        when (event.actionMasked) {
            android.view.MotionEvent.ACTION_DOWN -> {
                startVoiceFromSettings()
                return true
            }
            android.view.MotionEvent.ACTION_UP -> {
                voiceController?.release()
                return true
            }
            android.view.MotionEvent.ACTION_CANCEL -> {
                // 手势被系统抢走：立即释放麦克风（stop() 会 cancel + destroy 识别器）。
                voiceController?.stop()
                return true
            }
        }
        return false
    }

    /** 不可用原因（明确提示，不假装可用）。 */
    private fun voiceUnavailableReason(): String = when {
        !modeStore.get().isLocal -> "当前不是本地桌宠模式，按住说话不可用"
        !permissions.recordAudioGranted -> "未授予录音权限，按住说话不会启动录音"
        !permissions.localPetReady -> "缺少权限：${permissions.missingForLocalPet.joinToString("、")}"
        else -> "按住说话不可用"
    }

    /** 开始一次按住说话；识别文本经 [handleVoiceText] 走本地闸门。 */
    private fun startVoiceFromSettings() {
        if (!session.isUnlocked) {
            // 会话锁定：提示而不是硬跑（M01 固定行为）。
            voiceStatus.text = "本地会话未解锁，请先点击「解锁本地交互会话」"
            return
        }
        val controller = voiceController ?: LocalVoiceController(
            context = this,
            onText = { text -> handleVoiceText(text) },
            onError = { failure -> voiceStatus.text = failure.message },
            onStateChanged = { state -> onVoiceStateChanged(state) },
        ).also { voiceController = it }
        controller.reset()
        if (!controller.press()) {
            // 失败原因已通过 onError 显示。
            render(ModeSwitchResult(changed = false, mode = modeStore.get(), permissions = permissions))
        }
    }

    private fun onVoiceStateChanged(state: VoiceState) {
        voiceStatus.text = when (state) {
            VoiceState.IDLE -> if (voiceStatus.text.isNullOrBlank()) "按住说话：准备好了" else voiceStatus.text
            VoiceState.PRESSING -> "正在准备录音…"
            VoiceState.LISTENING -> "正在听…（松开结束）"
            VoiceState.TRANSCRIBING -> "识别中…"
            VoiceState.STOPPED -> "录音已停止，麦克风已释放"
        }
    }

    /**
     * 语音文本 → 结构化命令 → `LocalPetController.executeFromVoice`。
     *
     * **硬约束（M09 产品文档）**：语音文本不能切换模式、不能改白名单、不能提权限。
     * 实现上靠三点保证：
     * 1. 只接受结构化 JSON（`command` + `payload`），自由文本一律拒绝；
     * 2. `command` 必须已在 [DeviceCommands.ALL]/[CommandSpecs] 白名单内；
     * 3. payload 必须通过 [PayloadValidator]（与主机侧同一套契约）。
     * 因此「忽略安全规则」「切成远程模式」这类语音**永远不会**变成动作。
     *
     * 失败时明确提示，不静默丢弃。
     */
    private fun handleVoiceText(text: String) {
        val parsed = parseVoiceCommand(text)
        when (parsed) {
            is VoiceParseResult.Rejected -> voiceStatus.text = parsed.userMessage
            is VoiceParseResult.Parsed -> {
                val response = localPet.executeFromVoice(parsed.envelope)
                voiceStatus.text = if (response.status == "ok") {
                    "已执行（来源：本地语音）"
                } else {
                    "未执行：${response.error_code.ifBlank { response.message }}"
                }
            }
        }
    }

    /** 语音命令解析结果（本 Activity 内部使用，不新增对外错误码）。 */
    private sealed class VoiceParseResult {
        data class Parsed(val envelope: DeviceEnvelope) : VoiceParseResult()

        data class Rejected(val userMessage: String) : VoiceParseResult()
    }

    /**
     * 解析语音文本为信封。
     *
     * 只接受 `{"command":"...","payload":{...}}` 形式；自由文本、未声明命令、非法 payload 都拒绝。
     */
    private fun parseVoiceCommand(text: String): VoiceParseResult {
        val trimmed = text.trim()
        if (trimmed.isEmpty()) return VoiceParseResult.Rejected("没有听清，请再说一次")

        val root = runCatching {
            kotlinx.serialization.json.Json.parseToJsonElement(trimmed) as? JsonObject
        }.getOrNull() ?: return VoiceParseResult.Rejected("语音只能下发结构化命令；自由文本不会被当作指令执行")

        val command = (root["command"] as? JsonPrimitive)?.takeIf { it.isString }?.content?.trim().orEmpty()
        if (command.isEmpty()) return VoiceParseResult.Rejected("语音命令缺少 command 字段")
        if (command !in DeviceCommands.ALL || !CommandSpecs.isDeclared(command)) {
            return VoiceParseResult.Rejected("该命令不在本地白名单内")
        }

        val payload = when (val raw = root["payload"]) {
            null -> buildJsonObject { }
            is JsonObject -> raw
            else -> return VoiceParseResult.Rejected("命令参数必须是对象")
        }
        val validation = PayloadValidator.validate(command, payload)
        if (!validation.ok) {
            return VoiceParseResult.Rejected("命令参数不符合契约：${validation.message}")
        }

        voiceSequence += 1L
        val sequence = voiceSequence
        val envelope = DeviceEnvelope(
            request_id = "voice-$sequence-${System.currentTimeMillis()}",
            device_id = deviceId,
            command = command,
            // 30s 是本地语音的固定有效期；本地入口不需要跨网络重放窗口。
            deadline_ms = LOCAL_VOICE_DEADLINE_MS,
            idempotency_key = "voice-$deviceId-$sequence",
            payload = payload,
            counter = sequence,
            issued_at_ms = System.currentTimeMillis(),
            // 本地语音不走签名：签名只用于远程 WSS 来源（M05）。
            signature = "",
        )
        return VoiceParseResult.Parsed(envelope)
    }

    /**
     * 本机设备 id。
     *
     * ⚠️ 未配对时的**占位值**：`local-device` 仅用于本地语音构造信封，
     * 让 `LocalCommandSource.LOCAL_VOICE` 路径在没有配对的情况下也能跑通。
     *
     * 配对后**必须**改为 M05 `PairingController` / `KeystoreIdentity` 派生的真实 `device_id`：
     * 一旦本机完成配对（`device_connection.device_id` 有值），这里就自动用它；
     * 若配对流程改用了别的存储键，接上时请同步改成本文件这一处（不要在多处各读一份）。
     * 不联网查询、不上报。
     */
    private fun resolveDeviceId(): String = getSharedPreferences("device_connection", MODE_PRIVATE)
        .getString("device_id", "")
        .orEmpty()
        .ifBlank { LOCAL_DEVICE_PLACEHOLDER }

    private fun render(result: ModeSwitchResult) {
        val mode = result.mode
        modeTitle.text = if (mode.isLocal) "本地桌宠" else "远程控制"

        // 可用性判断：只显示真实状态，绝不显示「在线」假状态。
        availability.text = if (mode.isLocal) {
            if (permissions.localPetReady) {
                if (session.isUnlocked) "本地模式：可用（会话已解锁）" else "本地模式：可用，但会话未解锁（点击下方按钮解锁）"
            } else {
                "本地模式：不可用（缺少权限：${permissions.missingForLocalPet.joinToString("、")}）"
            }
        } else {
            if (remoteAddressConfigured()) {
                "电脑配对已保存，可在连接页启动连接。"
            } else {
                "还未连接电脑，先扫一扫完成配对。"
            }
        }

        permissionsView.text = buildString {
            append("权限：无障碍=")
            append(if (permissions.accessibilityEnabled) "已开启" else "未开启")
            append("｜悬浮窗=")
            append(if (permissions.overlayGranted) "已授权" else "未授权")
            append("｜录音=")
            append(if (permissions.recordAudioGranted) "已授权" else "未授权")
        }

        switchButton.text = if (mode.isLocal) "切换到远程控制" else "切换到本地桌宠"
        unlockButton.isEnabled = mode.isLocal && permissions.localPetReady
        unlockButton.text = if (session.isUnlocked) "本次互动已解锁" else "解锁本次互动"
        // 语音入口：只在 LOCAL_PET + 权限齐全时启用；锁定会话时按钮仍可点，按下会给提示。
        voiceButton.isEnabled = mode.isLocal && permissions.localPetReady
        if (!voiceButton.isEnabled) {
            voiceStatus.text = voiceUnavailableReason()
        }
        if (result.rejectionCode != null) {
            availability.text = "切换被拒绝：${result.rejectionCode}"
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == GALLERY_PERMISSION_REQUEST && ::permissionsView.isInitialized) {
            permissionsView.text = if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) "相册访问已授权" else "相册访问未授权"
        }
    }

    /**
     * 权限探测：无障碍服务是否已在系统设置中启用。
     *
     * 以 `Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES` 为准（它对应用可读且是权威来源）；
     * 只读探测，不主动开启任何服务。
     */
    private fun isAccessibilityEnabled(): Boolean {
        val component = ComponentName(this, WhaleAccessibilityService::class.java)
        val enabled = Settings.Secure.getString(contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
            ?: return false
        return enabled.split(':').any { flat -> ComponentName.unflattenFromString(flat) == component }
    }

    private fun isOverlayGranted(): Boolean = Settings.canDrawOverlays(this)

    private fun isRecordAudioGranted(): Boolean =
        ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED

    private fun currentPermissionSnapshot(): PermissionSnapshot = PermissionSnapshot(
        accessibilityEnabled = isAccessibilityEnabled(),
        overlayGranted = isOverlayGranted(),
        recordAudioGranted = isRecordAudioGranted(),
    )

    /** 远程入口是否已配置主机地址；未配置时界面显示不可用，不显示在线假状态。 */
    private fun remoteAddressConfigured(): Boolean = getSharedPreferences("device_connection", MODE_PRIVATE)
        .getString("wss_url", "")
        .orEmpty()
        .isNotBlank()

    private fun startDeviceConnectionService() {
        // 只由前台设置页启动；服务本身 exported=false，且不接受任何模式参数。
        startService(Intent(this, DeviceConnectionService::class.java))
    }

    private fun stopDeviceConnectionService() {
        stopService(Intent(this, DeviceConnectionService::class.java))
    }

    /**
     * 连接配置的 SharedPreferences 句柄。
     *
     * 文件名与键名一律取 [ConnectionSettings] 的常量：那正是
     * [DeviceConnectionService.connectIfConfigured] 与 [resolveDeviceId] 读取的位置，
     * 避免「界面写一处、服务读另一处」而让服务永远读不到地址。
     */
    private val connectionPreferencesListener =
        android.content.SharedPreferences.OnSharedPreferenceChangeListener { _, key ->
            if (key == ConnectionSettings.KEY_STATUS_TRACE || key == ConnectionSettings.KEY_LAST_ERROR) {
                runOnUiThread { renderConnectionState() }
            }
        }

    private fun currentConnectionPreferences(): android.content.SharedPreferences =
        getSharedPreferences(ConnectionSettings.PREFERENCES_NAME, MODE_PRIVATE)

    /**
     * 「保存并启动」：先整体校验，**校验通过才落盘**，落盘成功后再启动服务。
     *
     * 第 7 条硬性要求：地址为空或非法时**不启动任何服务**（拒绝并给出明确原因，不静默保存）。
     * 明文 `ws://` 允许保存但显式警告不安全；生产应使用 `wss://`。
     */
    private fun onSaveAndStartConnection() {
        val url = connectionUrlInput.text?.toString().orEmpty()
        val id = connectionDeviceIdInput.text?.toString().orEmpty()
        when (val result = ConnectionSettings.validate(url, id)) {
            is ConnectionSettings.ValidationResult.Rejected -> {
                // 不保存、不启动；把全部问题一次性告诉用户。
                connectionStatus.text = "未保存：" + result.errors.joinToString("；")
            }

            is ConnectionSettings.ValidationResult.Accepted -> {
                currentConnectionPreferences().edit()
                    .putString(ConnectionSettings.KEY_WSS_URL, result.wssUrl)
                    .putString(ConnectionSettings.KEY_DEVICE_ID, result.deviceId)
                    .apply()
                // 设备 id 用同一个键，配对/语音信封都读它，落盘后同步内存值。
                deviceId = resolveDeviceId()
                // 放回规范化后的值，让用户看到实际保存的内容。
                connectionUrlInput.setText(result.wssUrl)
                connectionDeviceIdInput.setText(result.deviceId)
                if (modeStore.get().isLocal) {
                    onSwitchClicked()
                } else {
                    startDeviceConnectionService()
                }
                renderConnectionState()
                if (result.insecureTransport) {
                    connectionStatus.append("\n" + ConnectionSettings.WARNING_INSECURE_TRANSPORT)
                }
            }
        }
    }

    /**
     * 如实渲染连接状态。
     *
     * **绝不假定 connected**：本 Activity 与 [DeviceConnectionService] 是两个组件，
     * 拿不到它的 `hasOpenSocket`。证据只有两条——
     * 1. 服务是否在运行（`ActivityManager.getRunningServices`，只读探测）；
     * 2. 服务方是否留下 `device_connection.last_status` 痕迹。
     * 两者缺一就显示「未连接 / 状态由服务管理」，而不会显示「已连接」。
     *
     * 本 Activity 只读该痕迹键，从不写它（写入属于服务方职责，本任务无权改该文件）。
     */
    private fun renderConnectionState() {
        if (!::connectionStatus.isInitialized) return
        if (modeStore.get().isLocal) {
            connectionStatus.text = "当前为本地桌宠模式，远程连接已停用。请到首页切换到远程控制。"
            return
        }
        val prefs = currentConnectionPreferences()
        val url = prefs.getString(ConnectionSettings.KEY_WSS_URL, "").orEmpty()
        val state = ConnectionSettings.resolveState(
            wssUrl = url,
            serviceRunning = isDeviceConnectionServiceRunning(),
            statusTrace = prefs.getString(ConnectionSettings.KEY_STATUS_TRACE, null),
        )
        connectionStatus.text = when (state) {
            ConnectionSettings.ConnectionState.NOT_CONFIGURED ->
                "还未配对，扫一扫连接电脑。"

            ConnectionSettings.ConnectionState.CONFIGURED_SERVICE_STOPPED ->
                "连接状态：已配置但未连接（连接服务未运行）"

            ConnectionSettings.ConnectionState.CONFIGURED_SERVICE_RUNNING ->
                "连接状态：" + prefs.getString(ConnectionSettings.KEY_LAST_ERROR, "").orEmpty()
                    .ifBlank { "正在连接主机，等待连接结果…" }

            ConnectionSettings.ConnectionState.CONNECTED ->
                "已连接电脑"
        }
        if (::availability.isInitialized && modeStore.get().isRemote) {
            availability.text = when (state) {
                ConnectionSettings.ConnectionState.CONNECTED -> "电脑已连接，可以接收你的操作。"
                ConnectionSettings.ConnectionState.NOT_CONFIGURED -> "还未连接电脑，先扫一扫完成配对。"
                else -> "配对信息已保存，在连接页启动连接。"
            }
        }
    }

    /**
     * 连接服务是否在运行（只读探测）。
     *
     * `getRunningServices` 在新系统上只返回本应用自己的服务，对本应用自身够用；
     * 它是**弱证据**，所以只用来区分「服务没跑（必未连接）」与「服务在跑（状态未知）」，
     * 绝不用来推断「已连接」。探测失败按「没在跑」处理，避免假在线。
     */
    private fun isDeviceConnectionServiceRunning(): Boolean = runCatching {
        val manager = getSystemService(android.app.ActivityManager::class.java)
        val target = ComponentName(this, DeviceConnectionService::class.java)
        manager.getRunningServices(Int.MAX_VALUE).any { it.service == target }
    }.getOrDefault(false)

    /** 本地模式必须没有未执行动作；M06 任务运行时接入后替换为真实队列。 */
    private fun pendingActions(): PendingActionQueue = object : PendingActionQueue {
        override fun clear() {
            // 目前本地入口是即时应答，没有排队动作；此处保底清理并记录。
            android.util.Log.i(LOG_TAG, "pending actions cleared on mode switch")
        }
    }

    private fun localPetIntroShown(): Boolean = getSharedPreferences(UI_PREFERENCES, MODE_PRIVATE)
        .getBoolean(UI_KEY_LOCAL_PET_INTRO, false)

    private fun markLocalPetIntroShown() {
        getSharedPreferences(UI_PREFERENCES, MODE_PRIVATE)
            .edit()
            .putBoolean(UI_KEY_LOCAL_PET_INTRO, true)
            .apply()
    }

    /** 打开系统无障碍设置（仅供用户手动开启，不自动开启）。 */
    private fun openAccessibilitySettings() {
        startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
    }

    /** 打开悬浮窗授权页（仅供用户手动开启，不自动开启）。 */
    private fun openOverlaySettings() {
        runCatching {
            startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
        }
    }

    /**
     * 权限说明对话框：只提供跳转入口，**不会**自动开启无障碍、悬浮窗或麦克风。
     *
     * 对应产品文档「首次切换到 LOCAL_PET 必须显示权限说明，不得自动打开麦克风或悬浮窗」。
     */
    private fun showPermissionIntro(latest: PermissionSnapshot, introNeeded: Boolean) {
        val message = buildString {
            append("本地桌宠模式需要你手动开启以下权限，应用不会自动开启：\n")
            append("\n• 无障碍服务：")
            append(if (latest.accessibilityEnabled) "已开启" else "未开启（用于点击、滚动、输入）")
            append("\n• 悬浮窗：")
            append(if (latest.overlayGranted) "已授权" else "未授权（用于显示桌宠）")
            append("\n• 录音：")
            append(if (latest.recordAudioGranted) "已授权" else "未授权（仅按住说话时使用，非常驻监听）")
            if (introNeeded) append("\n\n首次切换只显示说明；你可以在下方按钮打开系统设置自行开启。")
        }
        AlertDialog.Builder(this)
            .setTitle("本地模式权限说明")
            .setMessage(message)
            .setItems(arrayOf("打开无障碍设置", "打开悬浮窗设置", "知道了")) { _, which ->
                when (which) {
                    0 -> openAccessibilitySettings()
                    1 -> openOverlaySettings()
                    else -> markLocalPetIntroShown()
                }
            }
            .show()
    }

    /**
     * Activity 进入前台：登记 picker 宿主。
     *
     * 与 [onStop] 的 `detachMediaPickerHost()` **成对**：只有在前台期间
     * `pickerAvailable` 才为 true（后台不能被当成「能接 ActivityResult」）。
     */
    override fun onStart() {
        super.onStart()
        attachMediaPickerHost()
    }

    /**
     * Activity 离开前台：撤销 picker 宿主登记。
     *
     * 在途请求随之作废（见 `detachMediaPickerHost`），避免「回包到了却没人认领」。
     */
    override fun onStop() {
        detachMediaPickerHost()
        super.onStop()
    }

    /**
     * Activity 回到前台：重读一次连接配置与状态痕迹（不缓存旧结论，避免显示过期状态）。
     *
     * 只刷新显示与输入框回填，不做任何连接动作。
     */
    override fun onResume() {
        super.onResume()
        currentConnectionPreferences().registerOnSharedPreferenceChangeListener(connectionPreferencesListener)
        // Existing pairing is enough to recover after the desktop bridge restarts;
        // do not make the user scan again just to restart the foreground service.
        if (::modeStore.isInitialized && modeStore.get().isRemote && remoteAddressConfigured() && !isDeviceConnectionServiceRunning()) {
            startDeviceConnectionService()
        }
        if (::connectionUrlInput.isInitialized) {
            connectionUrlInput.setText(
                currentConnectionPreferences().getString(ConnectionSettings.KEY_WSS_URL, "").orEmpty(),
            )
            connectionDeviceIdInput.setText(
                currentConnectionPreferences().getString(ConnectionSettings.KEY_DEVICE_ID, "").orEmpty(),
            )
            renderConnectionState()
        }
    }

    /**
     * Activity 销毁：**必须释放麦克风**（M09 文档：Activity 销毁要 `stop()`）。
     *
     * 同时锁定本地会话，避免离开界面后旧会话窗口继续有效。
     */
    override fun onPause() {
        currentConnectionPreferences().unregisterOnSharedPreferenceChangeListener(connectionPreferencesListener)
        super.onPause()
    }

    override fun onDestroy() {
        // M08：撤销 picker 宿主登记——已销毁的 Activity 绝不能再被当成「能接 ActivityResult」。
        detachMediaPickerHost()
        // 解绑语音出口：Activity 结束后桌宠长按说话不再回调到已销毁的实例。
        PetOverlayService.bindVoiceSink(null)
        runCatching { voiceController?.stop() }
        voiceController = null
        // 进程级兜底：确保没有任何录音实例残留。
        VoiceMicWatch.releaseAll()
        super.onDestroy()
    }

    // ------------------------------------------------------------------
    // M08 相册选图：ActivityResult 合同
    // ------------------------------------------------------------------

    /**
     * API 33+ 的 Photo Picker 单选合同。
     *
     * `registerForActivityResult` 必须在 `onCreate` 期间调用（注册次数固定），
     * 所以合同本身做成方法、但**注册只发生一次**（见 `onCreate`）。
     */
    private fun photoPickerContract(): ActivityResultContract<PickVisualMediaRequest, Uri?> =
        ActivityResultContracts.PickVisualMedia()

    /**
     * API 26–32（以及音频请求）使用的 `ACTION_OPEN_DOCUMENT` 合同。
     *
     * 返回 `List<Uri>`，与 `PhotoPickerController.urisFromResult` 的语义一致：
     * 取消/无数据时返回空列表（**不当成功**）。
     */
    private fun legacyPickerContract(): ActivityResultContract<Intent, List<Uri>> =
        object : ActivityResultContract<Intent, List<Uri>>() {
            override fun createIntent(context: android.content.Context, input: Intent): Intent = input

            override fun parseResult(resultCode: Int, intent: Intent?): List<Uri> {
                // 复用生产 Controller 的解析，避免「测试与生产两套语义」。
                val controller = pickerController ?: return emptyList()
                return PhotoPickerController.urisFromResult(resultCode, intent).orEmpty()
            }
        }

    // ------------------------------------------------------------------
    // M08 相册选图：前台宿主登记与请求生命周期
    // ------------------------------------------------------------------

    /**
     * 当前是否真的配好了「能接 ActivityResult 的宿主」。
     *
     * 只有 Activity 处于 STARTED 之后、且启动器已注册时才为 true。
     * 这是 `MediaWiringImpl.pickerAvailable` 的唯一真相来源——**不猜、不假设**。
     */
    private val pickerHostReady: Boolean
        get() = pickerLauncher != null && !isFinishing && !isDestroyed

    /**
     * 登记本页为 `MediaPickerHosts` 的宿主。
     *
     * 必须与 [detachMediaPickerHost] **成对**调用；不成对会让 `pickerAvailable` 假真
     * （已销毁的页仍被当成可用），从而导致 `media.pick` 在无人接 result 时挂住。
     */
    private fun attachMediaPickerHost() {
        if (!pickerHostReady) return
        // 重复 onStart（极端时序）时先撤销旧的，**绝不叠加两份宿主**。
        mediaPickerHost?.let { MediaPickerHosts.detach() }
        val host = MediaPickerHost(
            isAvailable = { pickerHostReady },
            onPick = ::startPickerForRequest,
            parse = { request, uris ->
                val controller = pickerController
                    ?: return@MediaPickerHost PhotoPickerController.PickOutcome.Failed(
                        CommandErrors.NOT_WIRED,
                        "picker controller is not ready",
                    )
                // 生产 Controller 负责解析 content:// 并落到受控素材表。
                controller.onPicked(request, uris)
            },
        )
        mediaPickerHost = host
        MediaPickerHosts.attach(host)
    }

    /** 撤销登记。幂等：多次调用不会出错，只保证「最终没有登记」。 */
    private fun detachMediaPickerHost() {
        mediaPickerHost?.let { MediaPickerHosts.detach() }
        mediaPickerHost = null
        // 在途请求随之作废：没人会再接它的回包了。
        pendingPicker?.resolve(emptyList())
        pendingPicker = null
    }

    /**
     * `MediaPickerDelegate.pick` 的实现：**阻塞等待**用户选择，超时如实失败。
     *
     * 为什么是阻塞：`MediaRouter` 在 OkHttp 线程上同步执行命令，而选择器是异步回调。
     * 这里用「启动 → 等待 → 超时」把异步收敛成同步：
     * - 用户选完 → 回真实结果；
     * - 超时 → `PHOTO_PICKER_CANCELLED`（语义就是「没拿到用户的选择结果」），
     *   **绝不当成成功，也不让在途请求滞留**。
     *
     * 等待期间**不持有任何锁**，避免 `detach()` 被卡住（那会在 Activity 销毁时死锁）。
     */
    private fun startPickerForRequest(mediaType: String, maxItems: Int): List<Uri>? {
        if (!pickerHostReady) return null

        val request = PhotoPickerController.Request(
            mediaType = mediaType,
            maxItems = maxItems,
        )
        val slot = PendingPicker(request)
        pendingPicker?.resolve(emptyList())
        pendingPicker = slot

        try {
            // 选择器只能在主线程启动。
            val launched = runOnMain {
                launchPicker(request)
            }
            if (!launched) {
                pendingPicker = null
                return null
            }
        } catch (t: Throwable) {
            pendingPicker = null
            return null
        }

        // 等用户点完；超时即视为「没拿到选择结果」。
        val uris = slot.await(PICKER_TIMEOUT_MS)
        pendingPicker = null
        return uris
    }

    /** 按合同启动选择器：API 33+ 走 Photo Picker，更低版本或音频走 `ACTION_OPEN_DOCUMENT`。 */
    private fun launchPicker(request: PhotoPickerController.Request) {
        val controller = pickerController ?: return
        val maxItems = request.maxItems.coerceAtLeast(1)

        // 音频在系统 Photo Picker 上不支持，统一走 OPEN_DOCUMENT；多选同理。
        val useLegacy = !controller.supportsPhotoPicker() || maxItems > 1
        if (useLegacy) {
            pendingPicker?.expectedCount = maxItems
            legacyPickerLauncher?.launch(controller.buildLegacyIntent(request))
        } else {
            pendingPicker?.expectedCount = 1
            pickerLauncher?.launch(controller.pickerRequest(request.mediaType))
        }
    }

    /** 选择器回包：把 URI 交给 Controller 落库，并唤醒等待方。 */
    private fun completePicker(uris: List<Uri>) {
        val slot = pendingPicker ?: return
        pendingPicker = null
        slot.resolve(uris)
    }

    /**
     * 进程被杀后恢复在途请求：`savedInstanceState` 里存过的请求没有意义
     * （等待它的线程已经不在了），所以这里只做「清理」，不伪造回包。
     */
    private fun restorePendingPicker(state: Bundle?) {
        if (state == null) return
        pendingPicker?.resolve(emptyList())
        pendingPicker = null
    }

    /** 切到主线程执行并等待结果（`runOnUiThread` 本身不阻塞，这里需要知道它有没有真的跑）。 */
    private fun runOnMain(block: () -> Unit): Boolean {
        if (Looper.myLooper() == Looper.getMainLooper()) {
            block()
            return true
        }
        val done = CountDownLatch(1)
        val ok = AtomicBoolean(false)
        runOnUiThread {
            runCatching {
                block()
                ok.set(true)
            }
            done.countDown()
        }
        return done.await(1, TimeUnit.SECONDS) && ok.get()
    }

    /**
     * 一次 `device.media.pick` 的在途请求。
     *
     * `MediaPickerDelegate.pick` 是阻塞语义，而选择器回包是异步的——本类负责把
     * 「异步回调」收敛成「同步返回值」，并在超时时如实返回空结果（由上层判为取消）。
     */
    private class PendingPicker(val request: PhotoPickerController.Request) {
        /** 期望的选择个数；多选时用于判断是否已收齐（当前实现取回包即完成）。 */
        @Volatile
        var expectedCount: Int = 1

        private val latch = CountDownLatch(1)
        private val result = AtomicReference<List<Uri>>(null)

        fun resolve(uris: List<Uri>) {
            result.set(uris)
            latch.countDown()
        }

        fun await(timeoutMs: Long): List<Uri>? {
            val completed = latch.await(timeoutMs, TimeUnit.MILLISECONDS)
            // 超时 → 返回 null，由上层映射成 `PHOTO_PICKER_CANCELLED`，**不伪造成功**。
            return if (completed) result.get() else null
        }
    }

    /**
     * `MediaPickerHosts` 需要的宿主实现。
     *
     * `isAvailable` 直接查 Activity 实时状态（不缓存），这样 Activity 一旦进入
     * 销毁流程，`pickerAvailable` 会立刻回落 false。
     *
     * @param parse 把用户选中的 URI 交给生产 Controller 落库并转成 `PickOutcome`
     */
    private class MediaPickerHost(
        private val isAvailable: () -> Boolean,
        private val onPick: (String, Int) -> List<Uri>?,
        private val parse: (PhotoPickerController.Request, List<Uri>) -> PhotoPickerController.PickOutcome,
    ) : MediaPickerDelegate {
        override val available: Boolean
            get() = isAvailable()

        override fun pick(mediaType: String, maxItems: Int): PhotoPickerController.PickOutcome {
            if (!available) {
                // 没有宿主就是 NOT_WIRED：这不是「先占位」，是「真没有前台页面」。
                return PhotoPickerController.PickOutcome.Failed(
                    CommandErrors.NOT_WIRED,
                    "no foreground activity can receive the picker result",
                )
            }
            val uris = onPick(mediaType, maxItems)
                ?: return PhotoPickerController.PickOutcome.Failed(
                    MediaErrors.PHOTO_PICKER_CANCELLED,
                    "picker timed out or was dismissed before returning a result",
                )
            if (uris.isEmpty()) {
                return PhotoPickerController.PickOutcome.Failed(
                    MediaErrors.PHOTO_PICKER_CANCELLED,
                    "user cancelled the media picker",
                )
            }
            val request = PhotoPickerController.Request(mediaType = mediaType, maxItems = maxItems)
            return parse(request, uris)
        }
    }


    private companion object {
        private const val GALLERY_PERMISSION_REQUEST = 841
        const val LOG_TAG = "WhaleDevice"

        /** 仅存放 UI 引导标记，绝不存放模式值（模式固定存放在 `agent_mode` 的 `mode` 字段）。 */
        const val UI_PREFERENCES = "agent_mode_ui"
        const val UI_KEY_LOCAL_PET_INTRO = "local_pet_intro_shown"

        /**
         * 未配对时的 `device_id` 占位值。
         *
         * 配对后必须换成 M05 `KeystoreIdentity` 派生的真实 id（见 [resolveDeviceId]）。
         */
        const val LOCAL_DEVICE_PLACEHOLDER = "local-device"

        /** 本地语音请求的固定有效期（毫秒）。本地入口不经网络，30s 足够完成一次识别+校验。 */
        const val LOCAL_VOICE_DEADLINE_MS = 30_000

        /**
         * 等用户在相册里选图的超时（毫秒）。
         *
         * 为什么给 30 秒而不是几秒：用户要翻相册、找图、可能还要滚很久，
         * 10 秒会大量误判成「取消」。超时后如实返回 `PHOTO_PICKER_CANCELLED`，
         * **不伪造成功、也不让在途请求滞留**。
         */
        const val PICKER_TIMEOUT_MS = 30_000L
    }
}
