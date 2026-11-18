package cn.whale.agent.device

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.pm.PackageManager
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.util.Log
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageAnalysis
import androidx.camera.core.ImageProxy
import androidx.camera.core.Preview
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.camera.view.PreviewView
import androidx.core.content.ContextCompat
import androidx.lifecycle.LifecycleOwner
import com.google.zxing.BarcodeFormat
import com.google.zxing.BinaryBitmap
import com.google.zxing.DecodeHintType
import com.google.zxing.MultiFormatReader
import com.google.zxing.PlanarYUVLuminanceSource
import com.google.zxing.common.HybridBinarizer
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * M05 设备端配对界面（Android 侧薄包装）。
 *
 * 规范来源：`docs/modules/M05-pairing-transport-development.md`「配对密钥」「连接状态机」，
 * 六步流程由 [PairingController] 定义、由纯逻辑 [PairingFlow] 编排。
 *
 * ## 本页只做三件事
 *
 * 1. **三条入口**采集配对码：
 *    - **摄像头扫码**（CameraX 预览 + zxing `MultiFormatReader` 解 `QR_CODE`）；
 *    - **粘贴二维码内容**（`whale://pair?…` 整串，含扫码枪/剪贴板来源）；
 *    - **手动输入**两个框（主机地址 `wss://…` + 一次性 token）。
 *    三条入口**全部**走 [QrPayloadRules] 同一套校验，不存在「某一入口少校验一项」。
 * 2. 把用户操作交给 [PairingFlow]，**如实显示**当前在第几步、失败原因是什么（不假成功）。
 * 3. 第 5 步是一个**独立的确认界面**：显示设备名称、设备指纹、主机指纹，
 *    用户必须**逐字核对后点确认**才启用；点「不一致」则整轮作废。
 *
 * ## 固定禁止（违反即返工）
 *
 * - **不自动确认指纹**：没有用户点击就走不到 `PAIRED`（[PairingFlow.confirm] 已强制）。
 * - **不显示假在线**：本页只显示配对状态，连接状态由 M01 页面负责，绝不在此页写 `connected`。
 * - **不把 token 写日志**：[QrPayload.toString] 已脱敏，本页日志只打长度与错误码。
 * - **不碰 M01 `MainActivity`**：本页由显式入口启动，不改任何已有界面。
 * - **不在失败时静默**：任何拒绝都把原因渲染到 `statusText`。
 *
 * ## 摄像头扫码的已知边界（必须如实说明）
 *
 * 摄像头路径**本机无法验证**（无 Android 设备）。因此它的设计原则是「失败也只影响自己」：
 * - 运行时权限被拒 → 提示改用粘贴/手输，**不崩**；
 * - CameraX 绑定失败（无摄像头/被占用/OEM 限制）→ 提示与原因上屏，**不崩**；
 * - 解码失败 → 只更新扫码提示文案，**不打断**流程；
 * - 粘贴与手输路径**完全不依赖**摄像头，任何情况下都可用。
 */
class PairingActivity : Activity(), LifecycleOwner {

    /**
     * CameraX 的 `bindToLifecycle` 需要一个 [LifecycleOwner]。
     *
     * 本页继承 `Activity`（而不是 `ComponentActivity`）是为了用最朴素的
     * `requestPermissions` / `onRequestPermissionsResult` 权限回调——与 [PermissionGuideActivity]
     * 的做法一致，避免引入本仓库没验证过的 Activity 子类接口差异。
     * 因此这里自己维护一个 [LifecycleRegistry]，只把状态如实映射到 Activity 的真实生命周期，
     * **不做任何额外语义**（不产生假事件、不延迟销毁）。
     */
    private val lifecycleRegistry = androidx.lifecycle.LifecycleRegistry(this)

    override val lifecycle: androidx.lifecycle.Lifecycle get() = lifecycleRegistry

    // ---- 纯逻辑层（所有判定都在这里，本页不自己发明规则） ----
    private lateinit var flow: PairingFlow
    private lateinit var identityStore: DeviceIdentityStore

    /** 真实的主机配对通道（主机的 pairing 端点）。 */
    private val pairingClient: PairingClient
        get() = PairingClient(PairingClient.defaultClient(flow.qrCertificateFingerprint))

    // ---- 视图 ----
    private lateinit var stepText: TextView
    private lateinit var statusText: TextView
    private lateinit var identityText: TextView
    private lateinit var scanHintText: TextView
    private lateinit var previewView: PreviewView
    private lateinit var pasteInput: EditText
    private lateinit var manualHostInput: EditText
    private lateinit var manualTokenInput: EditText
    private lateinit var actionContainer: LinearLayout

    // ---- 摄像头 ----
    private var cameraExecutor: ExecutorService? = null
    private var cameraProvider: ProcessCameraProvider? = null
    private var scanner: QrScanner? = null
    private var cameraRunning = false

    /**
     * CameraX 的实现 provider 是否已显式注册过。
     *
     * 见 [startCamera] 的说明：手工工具链打包不会把 jar 里的 `META-INF/services`
     * 带进 dex，ServiceLoader 因此找不到 `Camera2Config`，必须自己 `configureInstance`。
     * 同一进程只注册一次（重复注册会抛），所以用这个标志位记住。
     */
    private var cameraXConfigured = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        WhaleUi.setup(this)
        identityStore = SharedPreferencesIdentityStore(
            getSharedPreferences(IDENTITY_PREFERENCES, MODE_PRIVATE),
        )
        // 真实的配对通道：把第 4、5、6 步真正送到主机。
        //
        // 没有它，`PairingFlow` 只在本地推进状态机，主机永远收不到设备身份
        // （「界面能走完六步，主机却说这设备没配过」）。
        val remoteChannel = object : PairingFlow.PairingRemote {
            override fun register(
                deviceUrl: String,
                token: String,
                pairingCode: String,
                deviceId: String,
                publicKeyEncoded: String,
                deviceFingerprint: String,
                capabilitiesVersion: String,
                connectionConfig: String,
                displayName: String,
            ): PairingFlow.RemoteResult {
                val outcome = pairingClient.register(
                    deviceUrl = deviceUrl,
                    token = token,
                    pairingCode = pairingCode,
                    deviceId = deviceId,
                    publicKeyEncoded = publicKeyEncoded,
                    deviceFingerprint = deviceFingerprint,
                    capabilitiesVersion = capabilitiesVersion,
                    connectionConfig = connectionConfig,
                    displayName = displayName,
                )
                return when (outcome) {
                    is PairingClient.Outcome.Ok -> PairingFlow.RemoteResult(
                        ok = true,
                        hostFingerprint = outcome.hostFingerprint,
                        pairingState = outcome.pairingState,
                        hostIdentityIsPlaceholder = outcome.hostIdentityIsPlaceholder,
                    )
                    is PairingClient.Outcome.Failed -> PairingFlow.RemoteResult(
                        ok = false,
                        message = outcome.message,
                    )
                }
            }

            override fun confirm(
                deviceUrl: String,
                token: String,
                deviceId: String,
                userAccepted: Boolean,
                observedHostFingerprint: String,
            ): PairingFlow.RemoteResult {
                val outcome = pairingClient.confirm(
                    deviceUrl = deviceUrl,
                    token = token,
                    deviceId = deviceId,
                    userAccepted = userAccepted,
                    observedHostFingerprint = observedHostFingerprint,
                )
                return when (outcome) {
                    is PairingClient.Outcome.Ok -> PairingFlow.RemoteResult(
                        ok = true,
                        message = outcome.message,
                        hostFingerprint = outcome.hostFingerprint,
                        pairingState = outcome.pairingState,
                    )
                    is PairingClient.Outcome.Failed -> PairingFlow.RemoteResult(
                        ok = false,
                        message = outcome.message,
                    )
                }
            }

            override fun finalize(deviceUrl: String, token: String) {
                // 收尾失败不影响配对结果（token 在主机侧本来就用一次即失效）。
                runCatching { pairingClient.finalize(deviceUrl, token) }
            }
        }

        flow = PairingFlow(
            controller = PairingController(
                clock = SystemSessionClock,
                keyStore = AndroidKeystoreIdentity(),
                verifier = JvmEcdsaVerifier(),
            ),
            consumedTokens = SharedPreferencesConsumedTokenStore(
                getSharedPreferences(CONSUMED_TOKENS_PREFERENCES, MODE_PRIVATE),
            ),
            clock = SystemSessionClock,
            remote = remoteChannel,
        )
        setContentView(buildLayout())
        render(PairingFlowOutcome(ok = true, message = "", errorCode = null, snapshot = flow.snapshot()))
    }

    override fun onResume() {
        super.onResume()
        runCatching { lifecycleRegistry.currentState = androidx.lifecycle.Lifecycle.State.RESUMED }
    }

    override fun onPause() {
        stopCamera("onPause")
        runCatching { lifecycleRegistry.currentState = androidx.lifecycle.Lifecycle.State.STARTED }
        super.onPause()
    }

    override fun onDestroy() {
        stopCamera("onDestroy")
        runCatching { lifecycleRegistry.currentState = androidx.lifecycle.Lifecycle.State.DESTROYED }
        cameraExecutor?.shutdown()
        cameraExecutor = null
        scanner = null
        // 释放配对后台线程：Activity 结束后不该留着它去操作已销毁的界面。
        pairingExecutor.shutdownNow()
        super.onDestroy()
    }

    // -----------------------------------------------------------------
    // 布局
    // -----------------------------------------------------------------

    private fun buildLayout(): ScrollView {
        val root = WhaleUi.page(this)
        root.addView(WhaleUi.button(this, "‹  返回首页") { finish() }.apply {
            background = android.graphics.drawable.ColorDrawable(android.graphics.Color.TRANSPARENT)
            gravity = android.view.Gravity.START or android.view.Gravity.CENTER_VERTICAL
        })
        root.addView(WhaleUi.title(this, "连接你的电脑"))
        root.addView(WhaleUi.text(this, "在电脑打开 Whale 主机，再扫描配对二维码。"))
        val progress = WhaleUi.card(this, "配对进度")
        stepText = WhaleUi.text(this, "", 16f, WhaleUi.ink)
        statusText = WhaleUi.text(this, "")
        identityText = WhaleUi.text(this, "", 12f).apply { setTextIsSelectable(true) }
        progress.addView(stepText)
        progress.addView(statusText)
        progress.addView(identityText)
        root.addView(progress)
        val scan = WhaleUi.card(this, "扫一扫", hero = true)
        scanHintText = WhaleUi.text(this, "将电脑上的二维码放入取景框。", 14f, android.graphics.Color.WHITE)
        scan.addView(scanHintText)
        previewView = PreviewView(this).apply {
            layoutParams = LinearLayout.LayoutParams(-1, dp(240))
            visibility = View.GONE
        }
        scan.addView(previewView)
        scan.addView(WhaleUi.button(this, "打开相机扫码") { startCameraIfPermitted() })
        root.addView(scan)
        val alternatives = WhaleUi.card(this, "其他连接方式")
        alternatives.addView(WhaleUi.text(this, "相机无法使用时，可以粘贴二维码内容，或手动填写。"))
        pasteInput = WhaleUi.input(this, "粘贴电脑上的配对内容").apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_MULTI_LINE
            minLines = 2
            maxLines = 4
        }
        alternatives.addView(pasteInput)
        alternatives.addView(WhaleUi.button(this, "使用配对内容") { submitPasted() })
        manualHostInput = WhaleUi.input(this, "电脑连接地址").apply { inputType = InputType.TYPE_CLASS_TEXT }
        manualTokenInput = WhaleUi.input(this, "一次性配对码").apply { inputType = InputType.TYPE_CLASS_TEXT }
        alternatives.addView(manualHostInput)
        alternatives.addView(manualTokenInput)
        alternatives.addView(WhaleUi.button(this, "手动配对") { submitManual() })
        root.addView(WhaleUi.collapsible(this, "相机无法使用？选择其他方式", alternatives))
        actionContainer = WhaleUi.card(this, "下一步")
        root.addView(actionContainer)
        root.addView(WhaleUi.text(this, "配对前请核对电脑与手机上的指纹。只有你确认后，电脑才能使用这部手机。", 12f))
        return WhaleUi.scroll(this, root)
    }

    private fun sectionTitle(text: String): TextView = TextView(this).apply {
        this.text = text
        textSize = 15f
        setPadding(0, 20, 0, 6)
    }

    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density).toInt()

    // -----------------------------------------------------------------
    // 渲染：唯一的状态来源是 PairingFlow 的快照
    // -----------------------------------------------------------------

    private fun render(outcome: PairingFlowOutcome) {
        val snapshot = outcome.snapshot
        stepText.text = buildString {
            append(snapshot.stepLabel)
            append("　（")
            append(if (snapshot.isPaired) "已确认" else "等待完成")
            append("）")
        }
        statusText.text = outcome.message
        if (snapshot.step == PairingStep.HOST_ISSUED_TOKEN) statusText.text = "已收到配对码，继续完成连接。"
        // 失败一律用醒目文案；成功也写清楚，绝不靠颜色暗示「大概成功了吧」。
        statusText.setTextColor(
            if (outcome.ok) COLOR_OK else COLOR_FAIL,
        )
        identityText.visibility = if (snapshot.deviceFingerprint.isEmpty()) View.GONE else View.VISIBLE
        identityText.text = buildString {
            append("设备 ID：")
            append(snapshot.deviceId.ifEmpty { "（尚未生成）" })
            append("\n设备指纹：")
            append(snapshot.deviceFingerprint.ifEmpty { "（尚未生成）" })
            if (snapshot.hostFingerprint.isNotEmpty()) {
                append("\n主机指纹：")
                append(snapshot.hostFingerprint)
            }
            if (snapshot.hostUrl.isNotEmpty()) {
                append("\n主机地址：")
                append(snapshot.hostUrl)
            }
            if (snapshot.insecureTransport) {
                append("\n")
                append(QrPayloadRules.WARNING_INSECURE)
            }
        }
        renderActions(snapshot)
    }

    /** 按**当前真实步骤**决定给哪些按钮：没到的步不给按钮，避免用户跳过确认。 */
    private fun renderActions(snapshot: PairingFlowSnapshot) {
        actionContainer.removeAllViews()
        when (snapshot.step) {
            PairingStep.HOST_ISSUED_TOKEN -> {
                actionContainer.addView(Button(this).apply {
                    text = getString(R.string.m05_action_generate_identity)
                    setOnClickListener { generateIdentity() }
                })
            }
            PairingStep.HOST_SAVED_IDENTITY -> {
                actionContainer.addView(Button(this).apply {
                    text = getString(R.string.m05_action_open_confirm)
                    setOnClickListener { openConfirmationPage() }
                })
            }
            // 第 2 步在 controller 里与第 3 步同一次调用完成，界面不会停在这里；
            // 但 `when` 必须穷尽，因此这两个分支给同一条「继续」按钮，绝不静默空过。
            PairingStep.DEVICE_ENTERED_CODE,
            PairingStep.DEVICE_GENERATED_IDENTITY -> {
                actionContainer.addView(Button(this).apply {
                    text = getString(R.string.m05_action_host_saved)
                    setOnClickListener { prepareRegistration() }
                })
            }
            PairingStep.USER_CONFIRMED_FINGERPRINT -> {
                actionContainer.addView(Button(this).apply {
                    text = getString(R.string.m05_action_finalize)
                    setOnClickListener { finalizePairing() }
                })
            }
            PairingStep.TOKEN_REVOKED -> {
                identityText.visibility = View.VISIBLE
                actionContainer.addView(TextView(this).apply {
                    text = getString(R.string.m05_done_note)
                    textSize = 13f
                })
            }
            null -> Unit
        }
        WhaleUi.polish(actionContainer)
        actionContainer.visibility = if (actionContainer.childCount == 0) View.GONE else View.VISIBLE
    }

    // -----------------------------------------------------------------
    // 三条入口
    // -----------------------------------------------------------------

    /** 摄像头扫码：先查运行时权限；被拒就走粘贴/手输，不崩也不假扫码。 */
    private fun startCameraIfPermitted() {
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            // 一次只申请一项；拒绝后本页仍可用（粘贴与手输不需要摄像头）。
            runCatching { requestPermissions(arrayOf(Manifest.permission.CAMERA), REQUEST_CAMERA) }
                .onFailure { Log.w(LOG_TAG, "camera permission request failed", it) }
            scanHintText.text = getString(R.string.m05_scan_need_permission)
            return
        }
        startCamera()
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray,
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode != REQUEST_CAMERA) return
        val granted = grantResults.isNotEmpty() &&
            grantResults.all { it == PackageManager.PERMISSION_GRANTED }
        Log.i(LOG_TAG, "camera permission granted=$granted")
        if (granted) {
            startCamera()
        } else {
            // 明确告诉用户下一步怎么做，不反复弹窗。
            scanHintText.text = getString(R.string.m05_scan_permission_denied)
        }
    }

    /**
     * 打开摄像头。
     *
     * **状态诚实纪律**：绑定**成功之后**才把 `cameraRunning` 置真、才显示「正在扫码」。
     * 绑定失败时状态保持关闭并给出原因——绝不先亮「正在扫码」再去失败，
     * 那会让用户对着黑屏以为程序在干活。
     */
    private fun startCamera() {
        if (cameraRunning) return
        // executor 只用于把帧交给 zxing；结果由 mainHandler 切回主线程。
        if (cameraExecutor == null) cameraExecutor = Executors.newSingleThreadExecutor()
        scanHintText.text = getString(R.string.m05_scan_starting)
        previewView.visibility = View.VISIBLE

        // ⚠️ 先显式初始化 CameraX 的实现 provider，再取 ProcessCameraProvider。
        //
        // 为什么必须显式：CameraX 默认靠 jar 里的 `META-INF/services` + ServiceLoader
        // 在运行时发现 `Camera2Config` 这类实现。手工工具链（aapt2 → D8 → apksigner，
        // 无 AGP）打包时，**jar 里的 `META-INF/services` 不会进 dex**，ServiceLoader
        // 找不到任何 provider，`ProcessCameraProvider.getInstance()` 抛：
        //   IllegalStateException: CameraX is not configured properly. The most likely
        //   cause is you did not include a default implementation such as 'camera-camera2'.
        // 而 `camera-camera2` 的类**其实全都在 dex 里**（烂的是发现机制）。
        // 这条只有真机（华为 CTR-AL20 / API 31）点「打开摄像头扫码」时才暴露。
        //
        // `configureInstance` 等价于 AGP 自动生成的 CameraXConfig，且不依赖打包工具的
        // services 合并能力。失败时如实记日志，不阻断后续（后续 getInstance 会给出
        // 自己的失败原因，界面照样如实显示）。
        runCatching {
            if (!cameraXConfigured) {
                ProcessCameraProvider.configureInstance(
                    androidx.camera.camera2.Camera2Config.defaultConfig(),
                )
                cameraXConfigured = true
            }
        }.onFailure { Log.w(LOG_TAG, "cameraX configure failed", it) }

        val provider = ProcessCameraProvider.getInstance(this)
        provider.addListener(
            {
                runCatching { bindCamera(provider.get()) }
                    .onSuccess {
                        cameraRunning = true
                        scanHintText.text = getString(R.string.m05_scan_running)
                    }
                    .onFailure { error ->
                        // 无摄像头 / 被占用 / OEM 限制：如实说明，绝不假装「正在扫码」。
                        Log.w(LOG_TAG, "camera bind failed", error)
                        scanHintText.text = getString(
                            R.string.m05_scan_unavailable,
                            error.javaClass.simpleName,
                        )
                        cameraRunning = false
                        previewView.visibility = View.GONE
                    }
            },
            ContextCompat.getMainExecutor(this),
        )
    }

    private fun bindCamera(provider: ProcessCameraProvider) {
        cameraProvider = provider
        provider.unbindAll()
        val preview = Preview.Builder().build().also { it.setSurfaceProvider(previewView.surfaceProvider) }
        val analysis = ImageAnalysis.Builder()
            .setBackpressureStrategy(ImageAnalysis.STRATEGY_KEEP_ONLY_LATEST)
            .build()
        val decoder = QrScanner { text ->
            mainHandler.post { onQrDecoded(text) }
        }
        scanner = decoder
        val executor = cameraExecutor ?: throw IllegalStateException("camera executor is missing")
        analysis.setAnalyzer(executor, decoder)
        provider.bindToLifecycle(this, CameraSelector.DEFAULT_BACK_CAMERA, preview, analysis)
    }

    /**
     * 解到一段二维码文本：立刻停掉摄像头再走校验（不再持续解码，避免重复触发）。
     *
     * **扫码成功后自动往下走**：第 2、3 步（生成 Keystore 身份）与第 4 步（备好主机材料）
     * 全部自动完成，用户只需要在**第 5 步**核对指纹时点一次「一致，确认启用」。
     *
     * 为什么这么做：用户的诉求是「主机显示二维码，手机扫一下就完事」。
     * 生成身份、填 device_id、点下一步这些都不是需要人判断的事，
     * 让用户点四五个按钮纯属把实现细节推给用户。
     * 唯一保留的人工确认点是第 5 步指纹比对——那是「确认连的确实是这台主机」的
     * 安全判断，必须由人看一眼。
     */
    private fun onQrDecoded(text: String) {
        stopCamera("decoded")
        pasteInput.setText(text)
        val outcome = flow.startWithQr(text)
        render(outcome)
        if (!outcome.ok) {
            scanHintText.text = getString(R.string.m05_scan_decode_rejected, outcome.message)
            return
        }
        // 第 1 步成功 → 自动推进第 2、3 步 → 自动推进第 4 步 → 停在第 5 步等用户确认指纹。
        advanceToConfirmationAutomatically()
    }

    /**
     * 扫码后自动完成第 2、3、4 步，然后停在第 5 步（指纹确认）。
     *
     * 每一步都检查真实状态：任何一步失败就停在那一步（`render` 已把原因写进界面），
     * **绝不**带着失败状态继续往下走。
     *
     * 停在第 5 步是**故意的安全边界**：生成身份、保存材料都由程序完成，
     * 但「这台主机到底是不是你要连的那台」必须由人看一眼指纹后点头。
     */
    private fun advanceToConfirmationAutomatically() {
        // 第 2、3 步：生成 Keystore 身份（device_id 与 display_name 都自动决定）。
        render(flow.submitCode(
            enteredCode = flow.pendingToken(),
            deviceId = resolveOrCreateDeviceId(),
            displayName = BuildDisplayNameSuggestion.of(this),
        ))
        if (flow.snapshot().step != PairingStep.DEVICE_GENERATED_IDENTITY) return

        // 第 4 步：把主机要保存的材料备好 —— 这一步会**真的发 HTTP 给主机**，
        // 所以放到后台线程；结果回到主线程后再判断要不要开确认页。
        runPairingAction(
            busyHint = "正在把设备身份交给主机…",
            action = { flow.prepareRegistration(displayName = flow.snapshot().displayName) },
        ) { outcome ->
            if (outcome == null) return@runPairingAction
            if (flow.snapshot().step != PairingStep.HOST_SAVED_IDENTITY) return@runPairingAction
            // 第 5 步：打开指纹确认页；**不自动通过**，等用户点「一致，确认启用」。
            openConfirmationPage()
        }
    }

    private fun stopCamera(reason: String) {
        if (!cameraRunning && scanner == null) return
        runCatching { cameraProvider?.unbindAll() }.onFailure {
            Log.w(LOG_TAG, "camera unbind failed ($reason)", it)
        }
        scanner = null
        cameraRunning = false
        previewView.visibility = View.GONE
    }

    private fun submitPasted() {
        val outcome = flow.startWithQr(pasteInput.text.toString())
        render(outcome)
    }

    private fun submitManual() {
        val outcome = flow.startWithManual(
            manualHostInput.text.toString(),
            manualTokenInput.text.toString(),
        )
        render(outcome)
    }

    // -----------------------------------------------------------------
    // 第 2~4 步
    // -----------------------------------------------------------------

    /**
     * 第 2 + 3 步：配对码 + 生成 Keystore 身份。
     *
     * **零弹窗**：`device_id` 与 `display_name` 都自动决定。
     *
     * 为什么不再问用户：
     * - `device_id` 只是个「这台手机在本主机里的名字」，规则是 `[A-Za-z0-9._:-]{3,80}`，
     *   没有任何需要人来判断的语义。问用户等于把内部实现细节推给用户，
     *   而用户还得先知道「什么是 device_id、去哪找、两边必须一致」才能填对。
     * - `display_name` 直接用机型名（`BuildDisplayNameSuggestion`），本就是为了好认。
     *
     * 保留的安全边界没有变：**第 5 步的指纹确认仍然必须由用户亲自点头**，
     * 那是「确认连的是哪台主机」的唯一人类判断点，绝不能自动过。
     */
    private fun generateIdentity() {
        val deviceId = resolveOrCreateDeviceId()
        val displayName = BuildDisplayNameSuggestion.of(this)
        render(flow.submitCode(
            enteredCode = flow.pendingToken(),
            deviceId = deviceId,
            displayName = displayName,
        ))
        // 成功就自动往下走第 4 步（那一步会发网络请求，同样要走后台线程）。
        if (flow.snapshot().step == PairingStep.DEVICE_GENERATED_IDENTITY) {
            runPairingAction(
                busyHint = "正在把设备身份交给主机…",
                action = { flow.prepareRegistration(displayName = flow.snapshot().displayName) },
            )
        }
    }

    /**
     * 取本机 `device_id`：已有就用，没有就**自动生成一个并落盘**。
     *
     * 落盘的键必须是 `device_connection` 的 `device_id`——
     * `MainActivity.resolveDeviceId()` 与 `DeviceConnectionService` 读的是同一个键，
     * 三处各读一份会漂移，所以这里写一次、别处都读它。
     */
    private fun resolveOrCreateDeviceId(): String {
        val existing = defaultDeviceId().trim()
        if (existing.isNotEmpty()) return existing

        // 生成一个合法且够随机的 id（规则 `[A-Za-z0-9._:-]{3,80}`）。
        // 用 SecureRandom：`device_id` 会进设备帧与主机记录，不该是可预测的。
        val random = java.security.SecureRandom()
        val alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
        val suffix = buildString {
            repeat(DEVICE_ID_RANDOM_LEN) { append(alphabet[random.nextInt(alphabet.length)]) }
        }
        val generated = "device-$suffix"

        getSharedPreferences(ConnectionSettings.PREFERENCES_NAME, MODE_PRIVATE)
            .edit()
            .putString(ConnectionSettings.KEY_DEVICE_ID, generated)
            .apply()
        return generated
    }

    /** 第 4 步：主机保存身份（本机只把材料备好；真正的落库由主机 `pairing.py`）。 */
    private fun prepareRegistration() {
        runPairingAction(
            busyHint = "正在把设备身份交给主机…",
            action = { flow.prepareRegistration(displayName = flow.snapshot().displayName) },
        )
    }

    // -----------------------------------------------------------------
    // 第 5 步：指纹确认（独立界面，绝不自动过）
    // -----------------------------------------------------------------

    /**
     * 打开确认页：把**设备名称与两个指纹**都摆出来，用户必须明确点「一致，确认」。
     *
     * 手输路径没有二维码指纹时，这里让用户从主机屏幕抄主机指纹；抄错由 [PairingFlow.confirm] 拒绝。
     */
    private fun openConfirmationPage() {
        val snapshot = flow.snapshot()
        val fingerprintInput = EditText(this).apply {
            hint = getString(R.string.m05_confirm_fingerprint_hint)
            inputType = InputType.TYPE_CLASS_TEXT
            setText(snapshot.hostFingerprint)
        }
        val body = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 16, 48, 0)
        }
        body.addView(TextView(this).apply {
            text = buildString {
                append(getString(R.string.m05_confirm_body))
                append("\n\n设备名称：")
                append(snapshot.displayName.ifEmpty { "（未填）" })
                append("\n设备 ID：")
                append(snapshot.deviceId)
                append("\n\n【本机指纹】")
                append("\n")
                append(snapshot.deviceFingerprint)
                append("\n\n【主机指纹】")
                append("\n")
                append(snapshot.hostFingerprint.ifEmpty { "（配对码未带，请照主机屏幕抄写）" })
                append("\n\n请把上面两个指纹与主机屏幕上显示的值")
                append("逐字比对；不完全一致请点「不一致」。")
            }
            textSize = 13f
        })
        body.addView(fingerprintInput)

        AlertDialog.Builder(this)
            .setTitle(getString(R.string.m05_confirm_title))
            .setView(body)
            .setPositiveButton(getString(R.string.m05_confirm_match)) { _, _ ->
                // 第 5 步会真的通知主机「用户确认了」，同样必须走后台线程。
                runPairingAction(
                    busyHint = "正在通知主机确认指纹…",
                    action = {
                        flow.confirm(
                            userAccepted = true,
                            hostFingerprintInput = fingerprintInput.text.toString(),
                            displayName = flow.snapshot().displayName,
                        )
                    },
                ) { outcome ->
                    if (outcome != null && outcome.ok) finalizePairing()
                }
            }
            .setNegativeButton(getString(R.string.m05_confirm_mismatch)) { _, _ ->
                // 「不一致」= 明确拒绝：token 作废、状态转 PAIRING_REQUIRED，绝不自动接受。
                runPairingAction(
                    busyHint = "正在作废本次配对…",
                    action = {
                        flow.confirm(
                            userAccepted = false,
                            displayName = flow.snapshot().displayName,
                        )
                    },
                )
            }
            .setNeutralButton(getString(R.string.m05_confirm_cancel), null)
            .show()
    }

    // -----------------------------------------------------------------
    // 第 6 步：token 立即失效 + 落盘身份
    // -----------------------------------------------------------------

    private fun finalizePairing() {
        val outcome = flow.finalize()
        render(outcome)
        if (outcome.ok) persistConfirmedIdentity()
    }

    /**
     * 把已确认身份落盘，并同步 M01 读取的 `device_connection`（`wss_url` + `device_id`）。
     *
     * 两处都写是**必须**的，因为它们服务于不同链路：
     * - [DeviceIdentityStore]（`device_identity`）是 M05 验签链路读身份与主机指纹的地方；
     * - `device_connection` 是 `DeviceConnectionService.connectIfConfigured()` 读 `wss_url`、
     *   `MainActivity.resolveDeviceId()` 读 `device_id` 的地方。
     * 只写一处就会出现「已配对但连不上 / 连上了但 device_id 还是占位值」。
     *
     * 本方法**只写键值**，不改任何已有界面与已有逻辑。
     */
    private fun persistConfirmedIdentity() {
        val confirmed = flow.confirmedIdentity() ?: return
        runCatching { identityStore.save(confirmed) }.onFailure {
            Log.e(LOG_TAG, "save device identity failed", it)
            statusText.text = getString(R.string.m05_save_identity_failed, it.javaClass.simpleName)
            return
        }
        runCatching {
            getSharedPreferences(ConnectionSettings.PREFERENCES_NAME, MODE_PRIVATE)
                .edit()
                .putString(ConnectionSettings.KEY_WSS_URL, confirmed.connectionConfig)
                .putString(ConnectionSettings.KEY_DEVICE_ID, confirmed.deviceId)
                .putString(ConnectionSettings.KEY_CERT_FINGERPRINT, flow.qrCertificateFingerprint)
                .putString(ConnectionSettings.KEY_HOST_SIGNING_PUBLIC_KEY, flow.qrHostSigningPublicKey)
                .commit().also { check(it) { "connection settings were not saved" } }
        }.onFailure {
            Log.e(LOG_TAG, "save connection settings failed", it)
            statusText.text = getString(R.string.m05_save_connection_failed, it.javaClass.simpleName)
            return
        }

        // 远程模式自动连接；本地模式保留用户选择并提示如何切换。
        startConnectionServiceAndReturn()
    }

    /**
     * 启动设备连接服务，然后自动回到主界面。
     *
     * 为什么不在本页等连接结果：连接是长驻的（Service 持有 WSS），
     * 配对页的职责到「身份已确认、配置已落盘」就结束了。
     * 把它交给 `DeviceConnectionService`（`START_STICKY`）才能后台常驻。
     */
    private fun startConnectionServiceAndReturn() {
        val localMode = AgentModeStore(applicationContext).get().isLocal
        if (localMode) {
            statusText.text = "配对已保存。当前为本地桌宠模式，请到首页切换到远程控制后连接电脑。"
            statusText.setTextColor(COLOR_OK)
        } else {
            runCatching {
                val intent = android.content.Intent(this, DeviceConnectionService::class.java)
                ContextCompat.startForegroundService(this, intent)
                statusText.text = "配对完成，正在连接主机…"
                statusText.setTextColor(COLOR_OK)
            }.onFailure {
                Log.e(LOG_TAG, "start connection service failed", it)
                statusText.text = "配对完成，但启动连接服务失败：${it.javaClass.simpleName}"
                statusText.setTextColor(COLOR_FAIL)
                return
            }
        }

        // 显示结果后返回主界面；本地模式多留一些时间阅读切换提示。
        mainHandler.postDelayed({
            if (!isFinishing && !isDestroyed) {
                startActivity(android.content.Intent(this, MainActivity::class.java).apply {
                    addFlags(android.content.Intent.FLAG_ACTIVITY_CLEAR_TOP or
                        android.content.Intent.FLAG_ACTIVITY_SINGLE_TOP)
                })
                finish()
            }
        }, if (localMode) 3000L else 1200L)
    }

    // -----------------------------------------------------------------
    // 小工具
    // -----------------------------------------------------------------

    /** 简单的单行输入对话框；取消返回 `null`（绝不兜底成空串假装用户填了）。 */
    /**
     * 弹一个文本输入对话框，**用户确认后**通过 [onResult] 回调结果。
     *
     * 为什么用回调而不是返回值：`AlertDialog.show()` 是异步的，它立刻返回、
     * 用户还没点按钮。早先的实现是 `var result: String? = null` + `show()` +
     * `return result`，于是**每次调用都立刻返回 `null`**——调用方以为用户取消了，
     * 第 2、3 步因此永远走不动，而编译、单测、日志全都正常。
     * 这类「异步被当成同步」的缺陷只有真机点按钮才会暴露。
     *
     * @param onResult 用户点「确定」得到文本；点「取消」或关闭对话框得到 `null`
     */
    private fun askText(
        title: String,
        message: String,
        initial: String,
        onResult: (String?) -> Unit,
    ) {
        val input = EditText(this).apply {
            setText(initial)
            inputType = InputType.TYPE_CLASS_TEXT
        }
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 16, 48, 0)
            addView(TextView(this@PairingActivity).apply { text = message; textSize = 12f })
            addView(input)
        }
        AlertDialog.Builder(this)
            .setTitle(title)
            .setView(box)
            .setPositiveButton(getString(android.R.string.ok)) { _, _ ->
                onResult(input.text.toString())
            }
            .setNegativeButton(getString(android.R.string.cancel)) { _, _ ->
                onResult(null)
            }
            .setOnCancelListener { onResult(null) }
            .show()
    }

    /** 默认 `device_id`：优先沿用 `device_connection` 里已有的值（与 M01 同一处）。 */
    private fun defaultDeviceId(): String =
        getSharedPreferences(ConnectionSettings.PREFERENCES_NAME, MODE_PRIVATE)
            .getString(ConnectionSettings.KEY_DEVICE_ID, "")
            .orEmpty()

    private companion object {
        /** 自动生成的 device_id 随机后缀长度。8 位在去混淆字符集下足够唯一。 */
        const val DEVICE_ID_RANDOM_LEN = 8

        const val LOG_TAG = "WhaleDeviceM05"
        const val REQUEST_CAMERA = 4405

        /** M05 身份落盘位置：与 `DeviceConnectionService.IDENTITY_PREFERENCES` 逐字相同。 */
        const val IDENTITY_PREFERENCES = "device_identity"

        /** 已失效 token 的登记位置（只存 token 的 SHA-256 指纹，不存明文）。 */
        const val CONSUMED_TOKENS_PREFERENCES = "device_pairing_consumed"

        val COLOR_OK = 0xFF1B5E20.toInt()
        val COLOR_FAIL = 0xFFB00020.toInt()
    }

    private val mainHandler = Handler(Looper.getMainLooper())

    /**
     * 配对网络调用的后台执行器。
     *
     * 为什么必须有它：第 4、5 步现在会**真的发 HTTP 请求给主机**（`PairingClient`），
     * 单次超时最长 15 秒。直接在按钮回调里同步调用会阻塞主线程：
     * 界面卡住、系统在 5 秒后判 ANR、严重时直接闪退。
     * 而这类问题在单测里**完全看不出来**（单测跑在没有 Looper 的 JVM 里）。
     *
     * 线程模型：网络调用在 [pairingExecutor] 上跑，结果一律 `mainHandler.post`
     * 回到主线程再 `render(...)`——UI 组件的任何操作都不允许在后台线程做。
     */
    private val pairingExecutor: java.util.concurrent.ExecutorService by lazy {
        java.util.concurrent.Executors.newSingleThreadExecutor { runnable ->
            Thread(runnable, "whale-pairing").apply { isDaemon = true }
        }
    }

    /**
     * 在后台执行一次配对动作，并把结果安全地送回主线程。
     *
     * @param busyHint 执行期间在状态栏显示的提示（让用户知道「正在和主机通信」，
     *   而不是对着没反应的按钮乱点）
     * @param action 真正要做的事（内部会发网络请求）
     * @param onResult 在主线程收到结果；`null` 表示动作内发生了未预期异常
     */
    private fun runPairingAction(
        busyHint: String,
        action: () -> PairingFlowOutcome,
        onResult: (PairingFlowOutcome?) -> Unit = {},
    ) {
        if (busyHint.isNotEmpty()) {
            statusText.text = busyHint
            statusText.setTextColor(COLOR_OK)
        }
        pairingExecutor.execute {
            val outcome = runCatching { action() }.getOrNull()
            mainHandler.post {
                if (isFinishing || isDestroyed) return@post
                if (outcome == null) {
                    // 兜底：异常时如实说「失败了」，绝不假装成功。
                    statusText.text = "与主机通信时出错：请检查主机是否在运行、地址是否填对"
                    statusText.setTextColor(COLOR_FAIL)
                    onResult(null)
                    return@post
                }
                render(outcome)
                onResult(outcome)
            }
        }
    }

}

/**
 * 摄像头帧 → zxing 解码。
 *
 * 只做「把 `ImageProxy` 的 Y 平面交给 zxing 的 `MultiFormatReader`」这一件事：
 * - 只认 `QR_CODE`（配对码就是二维码，放开其他格式只会多解出无关内容）；
 * - **只上报第一个成功解到的内容**（[reported] 标记），之后不再重复回调；
 * - 解码失败是常态（焦距未对、灯暗），因此失败**只记调试日志**，不打扰用户；
 * - `ImageProxy` 一定在 `finally` 里 `close()`，否则 CameraX 会停止继续送帧。
 */
private class QrScanner(
    private val onDecoded: (String) -> Unit,
) : ImageAnalysis.Analyzer {

    private val reader = MultiFormatReader().apply {
        setHints(
            mapOf(
                DecodeHintType.POSSIBLE_FORMATS to listOf(BarcodeFormat.QR_CODE),
                DecodeHintType.TRY_HARDER to true,
            ),
        )
    }

    @Volatile
    private var reported = false

    override fun analyze(image: ImageProxy) {
        try {
            if (reported) return
            val text = decode(image) ?: return
            reported = true
            onDecoded(text)
        } catch (error: Throwable) {
            Log.w("WhaleDeviceM05", "qr frame skipped: ${error.javaClass.simpleName}")
        } finally {
            runCatching { image.close() }
        }
    }

    private fun decode(image: ImageProxy): String? {
        // 只取 Y 平面：灰度足够解二维码，也不需要转换色彩空间。
        val plane = image.planes.firstOrNull() ?: return null
        val buffer = plane.buffer
        val data = ByteArray(buffer.remaining())
        buffer.get(data)
        val source = PlanarYUVLuminanceSource(
            data,
            plane.rowStride,
            image.height,
            0,
            0,
            image.width,
            image.height,
            false,
        )
        val bitmap = BinaryBitmap(HybridBinarizer(source))
        // zxing 用异常表示「这一帧没解到」，属正常控制流。
        return runCatching { reader.decode(bitmap).text }.getOrNull()
    }
}

/** 设备名称建议值：厂商 + 型号，用户可改。只读系统属性，不上报。 */
object BuildDisplayNameSuggestion {
    fun of(context: Context): String = runCatching {
        val manufacturer = android.os.Build.MANUFACTURER.orEmpty()
        val model = android.os.Build.MODEL.orEmpty()
        listOf(manufacturer, model).filter { it.isNotBlank() }.joinToString(" ")
    }.getOrDefault("")
}

/**
 * 已失效 token 的 `SharedPreferences` 登记处（生产实现）。
 *
 * 只存 token 的 **SHA-256 指纹**，不存明文：已失效的 token 没必要留明文在磁盘上，
 * 而指纹足够用来判「这张码是不是用过」。
 *
 * 用 `commit()` 而不是 `apply()`：一次性性属安全判定输入，
 * 必须确认已落盘再返回，避免进程被杀后「配对成功但失效记录没写」的窗口。
 */
class SharedPreferencesConsumedTokenStore(
    private val prefs: android.content.SharedPreferences,
) : ConsumedTokenStore {

    override fun isConsumed(tokenKey: String): Boolean = prefs.contains(tokenKey)

    override fun markConsumed(tokenKey: String) {
        prefs.edit().putBoolean(tokenKey, true).commit()
    }
}

