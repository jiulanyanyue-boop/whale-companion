package cn.whale.agent.device

import android.content.Context
import android.content.ContextWrapper
import androidx.core.content.FileProvider
import androidx.lifecycle.LifecycleOwner
import java.io.File
import java.net.URI

/**
 * M08 媒体依赖的真实实现（[MediaWiring] 的生产版）。
 *
 * 规范来源：`docs/modules/M08-media-development.md`「接线」与 `CommandRouter.kt` 里的
 * `interface MediaWiring`（本类只实现该契约，不改接口语义）。
 *
 * 本类刻意**不含任何 Android 副作用**：
 * - 它只做「语义守卫 + 原样转调」，真实落盘/相机/网络/`startActivity` 全部经
 *   [MediaBackends] 注入；
 * - 因此可以用 JUnit4 在纯 JVM 上真跑（`MediaWiringImplTest`），android.jar 只是桩；
 * - Android 侧装配集中在 [MediaWiringImpl.android] 一处，装配失败就返回 `null`，
 *   由 `CommandRouter` 降级为 `denied/NOT_WIRED`——**绝不用假成功兜底**。
 *
 * 三条硬语义（与 task-2 要求逐条对应）：
 * 1. [pickerAvailable] 如实：只有真的登记了前台 `ActivityResult` 宿主才是 `true`；
 *    没登记就 `false`，`device.media.pick` 固定 `NOT_WIRED`；
 * 2. [uploadEndpoint] 由 `device_connection.wss_url` 推导 https 基址，推不出就是**空串**，
 *    绝不臆测 `http://`；
 * 3. [share] 尊重 `confirmed`：未确认时改问 M04 批准来源（[MediaShareApprovals]），
 *    两处都没批准就**不调**后端，因此不可能 `startActivity`；注入手势期间一律拒绝。
 */
class MediaWiringImpl(
    override val assets: AssetStore,
    override val uploadEndpoint: String,
    private val backends: MediaBackends,
    /**
     * Photo Picker 的前台宿主。
     *
     * `null` 表示本进程没有任何 Activity 注册 `registerForActivityResult` 来接收选择结果，
     * 此时 [pickerAvailable] 必须为 `false`，`media.pick` 只能 `NOT_WIRED`。
     */
    private val picker: MediaPickerDelegate? = null,
    /**
     * 分享批准的来源（M04 [UserGate] 经 [MediaShareConfirmation] 投影）。
     *
     * `null` 表示本实例没被直接注入，此时读进程登记处 [MediaShareApprovals]；
     * 两处都没有 → 只认路由层注入的 `confirmed`（其默认值恒 `false`），
     * 于是分享固定 `ACTION_REJECTED`——**没有批准就是拒绝**，绝不降级成放行。
     */
    private val shareApproval: MediaShareApproval? = null,
) : MediaWiring {

    /** 真实宿主登记决定；每次读取都重新求值，前台 Activity 销毁后自动变回 `false`。 */
    override val pickerAvailable: Boolean get() = effectivePicker()?.available == true

    /** 注入的宿主优先；没注入时读进程登记处，保证宿主晚于装配登记也能立刻生效。 */
    private fun effectivePicker(): MediaPickerDelegate? = picker ?: MediaPickerHosts.current()

    /** 相机：只透传 Controller 的结果，不在这里改码、不吞异常。 */
    override fun capture(lens: String, quality: String): CameraController.CaptureOutcome =
        backends.capture(lens, quality)

    /** 相册：没有前台宿主时连后端都不碰，直接固定失败。 */
    override fun pick(mediaType: String, maxItems: Int): PhotoPickerController.PickOutcome {
        val host = effectivePicker()
        if (host == null || !host.available) {
            return PhotoPickerController.PickOutcome.Failed(
                CommandErrors.NOT_WIRED,
                "media pick requires a foreground activity to receive the picker result",
            )
        }
        return host.pick(mediaType, maxItems)
    }

    /** 上传：把 asset_id / device_id / 端点原样交给上传控制器（端点为空时由其返回可重试失败）。 */
    override fun upload(
        assetId: String,
        deviceId: String,
        endpoint: String,
    ): MediaUploadController.UploadOutcome = backends.upload(assetId, deviceId, endpoint)

    /** 注入的批准来源优先；没注入时读进程登记处，保证宿主晚于装配登记也能立刻生效。 */
    private fun effectiveApproval(): MediaShareApproval? = shareApproval ?: MediaShareApprovals.current()

    /**
     * 分享：**这一处**就是 `confirmed` 的唯一来源（其余分支不动）。
     *
     * 三条固定语义：
     * 1. 注入手势期间一律拒绝，且**不消费**批准——防止 AI 在注入时借走用户批准（M04 既有语义）；
     * 2. `confirmed` 仍尊重路由层注入的回调（宿主可在那里放行自己已确认的分享）；
     * 3. `confirmed` 为 `false` 时改问 M04 批准来源；没有登记、没有绑定、仍在等确认、
     *    已拒绝/超时/用过一次，全部落到 `false` → `ACTION_REJECTED`，且**不调用后端**，
     *    因此不可能 `startActivity`。
     */
    override fun share(assetId: String, target: String, confirmed: Boolean): ShareResult {
        val approval = effectiveApproval()
        if (approval?.injecting == true) {
            return ShareResult.Failed(
                CommandErrors.ACTION_REJECTED,
                "media share is rejected while a gesture is being injected",
            )
        }
        // 真批准：路由层回调已确认，或 M04 批准来源对「这一份素材 + 这个目标」放行（一次性消费）。
        val approved = confirmed || approval?.approve(assetId, target) == true
        if (!approved) {
            return ShareResult.Failed(
                CommandErrors.ACTION_REJECTED,
                "media share requires explicit user confirmation before sending",
            )
        }
        return backends.share(assetId, target, true)
    }

    companion object {
        /** 受控素材目录：`context.filesDir/media_assets`，与 `res/xml/media_share_paths.xml` 逐字一致。 */
        const val ANDROID_ASSET_DIR: String = "media_assets"

        /** 分享出口 authority，与 `AndroidManifest.xml` 中 FileProvider 的声明逐字一致。 */
        const val SHARE_AUTHORITY: String = "cn.whale.agent.device.media"

        /** 上传端点推导不出时的占位：空串（路由层据此返回固定失败）。 */
        const val NO_UPLOAD_ENDPOINT: String = ""

        /** 进程级 [AssetStore] 缓存：先建、后建都指向同一份素材表，Activity 重建不会丢登记。 */
        private val assetsLock = Any()
        private var cachedAssets: AssetStore? = null
        private var cachedRoot: String = ""

        /**
         * Android 侧装配：任何一步失败都返回 `null`（降级为 `NOT_WIRED`），绝不抛给调用方。
         *
         * 调用方（`DeviceConnectionService` / `LocalPetController`）只把它填进
         * `CommandRouter(context, media)` 的第二个参数。
         */
        fun android(context: Context): MediaWiringImpl? = runCatching { build(context) }.getOrNull()

        private fun build(context: Context): MediaWiringImpl {
            val app = context.applicationContext ?: context
            val store = sharedAssets(app)
            // CameraX 必须绑定前台生命周期：拿 context 链上最近的 LifecycleOwner 作为相机上下文。
            // 拿不到（例如前台服务里的 applicationContext）就退化为 app，相机分支会诚实返回 NOT_WIRED。
            val camera = CameraController(lifecycleHostOf(context) ?: app, store)
            val uploader = MediaUploadController(store)
            val mediaStore = MediaStoreController(app, store) { assetId ->
                // 只经 FileProvider 出 content:// URI；拿不到就 null，控制器会拒绝而不是暴露 file://。
                runCatching {
                    FileProvider.getUriForFile(app, SHARE_AUTHORITY, store.fileOf(assetId))
                }.getOrNull()
            }
            val backends = object : MediaBackends {
                override fun capture(lens: String, quality: String): CameraController.CaptureOutcome =
                    if (lifecycleHostOf(context) != null) camera.capture(CameraController.Request(lens = lens, quality = quality))
                    else RemoteCameraCapture.capture(app, store, lens, quality)

                override fun pick(mediaType: String, maxItems: Int): PhotoPickerController.PickOutcome =
                    // 没有登记的宿主时本方法不会被调用（见 MediaWiringImpl.pick 的守卫）。
                    PhotoPickerController.PickOutcome.Failed(
                        CommandErrors.NOT_WIRED,
                        "photo picker host is not attached in this build",
                    )

                override fun upload(
                    assetId: String,
                    deviceId: String,
                    endpoint: String,
                ): MediaUploadController.UploadOutcome = uploader.upload(assetId, deviceId, endpoint)

                override fun share(assetId: String, target: String, confirmed: Boolean): ShareResult =
                    when (val outcome = mediaStore.share(MediaStoreController.Request(assetId, target, confirmed))) {
                        is MediaStoreController.ShareOutcome.Ready ->
                            ShareResult.Sent(outcome.plan.evidence, outcome.plan.target)

                        is MediaStoreController.ShareOutcome.Failed ->
                            ShareResult.Failed(outcome.code, outcome.message)
                    }
            }
            return MediaWiringImpl(
                assets = store,
                uploadEndpoint = uploadEndpointFromWssUrl(readWssUrl(app)),
                backends = backends,
                picker = MediaPickerHosts.current(),
                // 宿主若已登记 M04 批准来源就立刻生效；没有登记保持 null（fail-closed）。
                shareApproval = MediaShareApprovals.current(),
            )
        }

        /** 受控目录下的单例 [AssetStore]：`filesDir/media_assets`，绝不落外部存储。 */
        private fun sharedAssets(app: Context): AssetStore = synchronized(assetsLock) {
            val root = File(app.filesDir, ANDROID_ASSET_DIR)
            val path = root.absolutePath
            val existing = cachedAssets
            if (existing != null && cachedRoot == path) {
                existing
            } else {
                AssetStore(root).also {
                    cachedAssets = it
                    cachedRoot = path
                }
            }
        }

        /** 从 `device_connection` 读 `wss_url`；读不到就是空串（随后推导为空端点）。 */
        private fun readWssUrl(app: Context): String = runCatching {
            // 键名只认 [ConnectionSettings]：同一个键写两处字符串，改名时必然两侧读不到。
            app.getSharedPreferences(ConnectionSettings.PREFERENCES_NAME, Context.MODE_PRIVATE)
                .getString(ConnectionSettings.KEY_WSS_URL, "")
                .orEmpty()
        }.getOrDefault("")

        /** context 链上最近的 [LifecycleOwner]；没有就 `null`。 */
        private fun lifecycleHostOf(context: Context): Context? {
            var current: Context? = context
            while (current != null) {
                if (current is LifecycleOwner) return current
                current = (current as? ContextWrapper)?.baseContext
            }
            return null
        }
    }
}

/**
 * 四个媒体能力的可注入后端。
 *
 * 生产装配用真实 Controller（`CameraController` / `MediaUploadController` /
 * `MediaStoreController`），测试用假实现；[MediaWiringImpl] 本身因此保持纯 JVM 可测。
 */
interface MediaBackends {
    fun capture(lens: String, quality: String): CameraController.CaptureOutcome

    fun pick(mediaType: String, maxItems: Int): PhotoPickerController.PickOutcome

    fun upload(assetId: String, deviceId: String, endpoint: String): MediaUploadController.UploadOutcome

    fun share(assetId: String, target: String, confirmed: Boolean): ShareResult
}

/**
 * Photo Picker 的前台宿主契约。
 *
 * 只有真的 Activity 用 `registerForActivityResult` 注册过 M08 的选择器契约、并且此刻
 * 处于前台，[available] 才允许为 `true`。实现方（前台 Activity）负责在拿到
 * `ActivityResult` 之后调用 `PhotoPickerController.onPicked`，把 URI 变成已登记素材。
 */
interface MediaPickerDelegate {
    val available: Boolean

    fun pick(mediaType: String, maxItems: Int): PhotoPickerController.PickOutcome
}

/**
 * 前台 Activity 的登记处（进程级，默认**空**）。
 *
 * 当前构建没有任何 Activity 注册 M08 的选择器契约，因此 [available] 恒为 `false`，
 * `device.media.pick` 如实返回 `NOT_WIRED`。这不是「先占位」：它是「没有宿主」的
 * 真实结论，只有宿主真的接上之后才会变成 `true`。
 */
object MediaPickerHosts {
    @Volatile
    private var delegate: MediaPickerDelegate? = null

    /** 由前台 Activity 在 `onCreate` 后登记；重复登记以最后一次为准。 */
    fun attach(delegate: MediaPickerDelegate) {
        this.delegate = delegate
    }

    /** Activity 销毁或失去前台能力时撤销登记。 */
    fun detach() {
        delegate = null
    }

    /** 当前登记的宿主；没有就是 `null`。 */
    fun current(): MediaPickerDelegate? = delegate

    /** 本进程是否真有可接收 `ActivityResult` 的宿主。 */
    fun available(): Boolean = delegate?.available == true
}

/**
 * 由 `device_connection.wss_url` 推导上传用的 https 基址。
 *
 * 规则（纯函数，可在 JVM 上断言）：
 * - 只接受 `wss://`；`ws://` / `http://` / `https://` / 空串 / 畸形串一律返回空串，
 *   **不**做「猜一个 http」的降级（媒体不得明文传输）；
 * - 非默认端口保留（`wss://host:8443` → `https://host:8443`），443 省略；
 * - 含 userinfo 或主机名非法的 URL 视为不可推导，返回空串；
 * - 返回值不带路径：上传路径由 `assetChunkPath` 拼。
 */
fun uploadEndpointFromWssUrl(wssUrl: String?): String {
    val raw = wssUrl?.trim().orEmpty()
    if (raw.isEmpty()) return ""
    if (!raw.startsWith("wss://", ignoreCase = true)) return ""
    val parsed = try {
        URI(raw)
    } catch (t: Throwable) {
        return ""
    }
    if (parsed.userInfo != null || parsed.fragment != null) return ""
    val host = parsed.host?.trim().orEmpty()
    if (host.isEmpty()) return ""
    val port = parsed.port
    return when {
        port <= 0 || port == HTTPS_DEFAULT_PORT -> "https://$host"
        // URI 已保证 port 在 0..65535 内。
        else -> "https://$host:$port"
    }
}

private const val HTTPS_DEFAULT_PORT = 443
