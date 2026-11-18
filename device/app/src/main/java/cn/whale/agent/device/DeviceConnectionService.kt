package cn.whale.agent.device

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import androidx.core.app.NotificationCompat
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener

/**
 * M01 远程入口。
 *
 * 固定约束（`docs/modules/M01-system-modes-development.md`「远程闸门」）：
 * - 只在 `REMOTE_B` 建立 WSS；进入 `LOCAL_PET` 时必须断开且不再重连。
 * - `onMessage` 的固定 5 步顺序全部由 [RemoteModeGate] 执行：
 *   解码失败 → `SIGNATURE_REQUIRED` → `MODE_REJECTED`（不执行 router）→ 幂等/deadline/counter → `CommandRouter.execute`。
 * - 错误码一律引用 [CommandErrors]，本文件不出现字符串字面量错误码。
 *
 * 服务本身 `android:exported="false"`，只能由前台设置页 `startService` 启动；
 * 没有任何 Intent extra 能指定模式或命令，模式只来自 [AgentModeStore]。
 */
class DeviceConnectionService : Service() {
    private var connectionClient: OkHttpClient? = null
    @Volatile private var connectionGeneration: Long = 0
    private val reconnectHandler = Handler(Looper.getMainLooper())
    private var reconnectAttempt = 0
    private val reconnectRunnable = Runnable { connectIfConfigured() }
    private lateinit var router: CommandRouter
    private lateinit var database: DeviceDb
    private lateinit var modeStore: AgentModeStore
    private lateinit var identityStore: DeviceIdentityStore
    private lateinit var remoteGate: RemoteModeGate
    private var socket: WebSocket? = null
    @Volatile private var authenticatedHostIdentity: PublicIdentity? = null
    @Volatile private var hostConfirmed = false

    /** 验证主机的帧签名，公钥来自已通过二维码证书固定的 TLS 握手。 */
    private fun buildSignatureGate(): FrameSignatureGate? {
        val confirmed = runCatching { identityStore.load() }.getOrNull() ?: return null
        val pinnedHostFingerprint = confirmed.hostFingerprint
        if (pinnedHostFingerprint.isBlank()) return null
        return M05FrameSignatureGate(
            identity = { authenticatedHostIdentity },
            hostFingerprintProvider = { pinnedHostFingerprint },
            observedHostFingerprintProvider = { pinnedHostFingerprint },
            onHostFingerprintChanged = {
                // 指纹变化：立即作废本地身份并要求人工重新配对，绝不自动接受。
                logLocal("host fingerprint changed: identity invalidated, manual re-pairing required")
                runCatching { identityStore.clear() }
            },
        )
    }

    override fun onCreate() {
        super.onCreate()
        // M08：媒体能力经 MediaWiringImpl 注入；装配失败为 null → media.* 保持 NOT_WIRED。
        router = CommandRouter(applicationContext, MediaWiringImpl.android(this))
        modeStore = AgentModeStore(applicationContext)
        identityStore = SharedPreferencesIdentityStore(
            getSharedPreferences(IDENTITY_PREFERENCES, MODE_PRIVATE),
        )
        // M12：手写 SQLite 替代 Room（kapt 在手工 K2 工具链下生成不出 *_Impl，
        // 见 DeviceStore.kt 顶部说明）。
        // 升级只加表/加索引，绝不清库——processed_effects 与 device_counters 是
        // 「不重做副作用」「counter 只前进」的权威记录。
        database = DeviceDb(applicationContext)
        remoteGate = RemoteModeGate(
            mode = modeStore,
            executor = object : CommandExecutor {
                private val routerExecutor = RouterCommandExecutor(router)
                override fun execute(envelope: DeviceEnvelope): DeviceResponse =
                    if (envelope.command == "host.policy.sync") executePolicySync(envelope)
                    else routerExecutor.execute(envelope)
            },
            effects = SqliteEffectLog(database),
            counters = SqliteCounterLog(database),
            clock = SystemSessionClock,
            log = ::logLocal,
            signatureGate = buildSignatureGate(),
        )
        createChannel()
        startForeground(NOTIFICATION_ID, notification("设备连接待命"))
    }

    private fun executePolicySync(envelope: DeviceEnvelope): DeviceResponse {
        val policy = PolicySync.parse(envelope.protocol_version, envelope.payload)
            ?: return DeviceResponse(
                request_id = envelope.request_id,
                device_id = envelope.device_id,
                status = "denied",
                error_code = CommandErrors.INVALID_PAYLOAD,
                message = "invalid device policy",
            )
        val preferences = getSharedPreferences("device_policy", MODE_PRIVATE)
        val saved = preferences.edit()
            .putStringSet("allowlist", policy.allowedPackages)
            .putStringSet("blocked_packages", policy.blockedPackages)
            .commit()
        if (!saved) {
            return DeviceResponse(
                request_id = envelope.request_id,
                device_id = envelope.device_id,
                status = "failed",
                message = "device policy could not be saved",
            )
        }
        router.allowlist = policy.allowedPackages
        router.blockedPackages = policy.blockedPackages
        hostConfirmed = true
        recordConnectionTrace(true)
        val allowedJson = kotlinx.serialization.json.JsonArray(
            policy.allowedPackages.sorted().map { kotlinx.serialization.json.JsonPrimitive(it) },
        ).toString()
        return DeviceResponse(
            request_id = envelope.request_id,
            device_id = envelope.device_id,
            status = "ok",
            message = "device policy synchronized",
            evidence = mapOf("policy_synced" to "true", "allowed_packages" to allowedJson),
        )
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // 用户再次启动或配对配置变化时，必须重新读取配置并释放旧连接。
        reconnectHandler.removeCallbacks(reconnectRunnable)
        closeSocket("connection restarted")
        connectIfConfigured()
        return START_STICKY
    }

    /** 远程入口是否处于已连接状态；本地模式下必须为 false。 */
    val hasOpenSocket: Boolean get() = socket != null && hostConfirmed

    /**
     * 把连接状态落盘，供 MainActivity 如实回显。
     *
     * 为什么需要它：Activity 与 Service 是不同组件，Activity 拿不到 `hasOpenSocket`。
     * 若不落盘，设置页只能显示「已配置」而无法区分「连上了没有」，
     * 或者更糟——假定在线造假状态（M01 明令禁止）。
     *
     * 键名固定来自 [ConnectionSettings]：`device_connection` 的 `last_status`，
     * 值为 `connected` / `disconnected`。**只写这两个值**，不写第三种「中间态」，
     * 因为「正在连接」不是可观测事实，写它等于编造。
     */
    private fun recordConnectionTrace(connected: Boolean, error: String = "") {
        runCatching {
            getSharedPreferences(ConnectionSettings.PREFERENCES_NAME, MODE_PRIVATE)
                .edit()
                .putString(
                    ConnectionSettings.KEY_STATUS_TRACE,
                    if (connected) ConnectionSettings.TRACE_CONNECTED else ConnectionSettings.TRACE_DISCONNECTED,
                )
                .putString(ConnectionSettings.KEY_LAST_ERROR, error)
                .apply()
        }
        if (error.isNotBlank()) logLocal(error)
    }

    private fun connectIfConfigured() {
        // 本地模式：必须没有 WSS。先断开再返回，不进入重连路径。
        if (!modeStore.get().isRemote) {
            closeSocket("local pet mode")
            recordConnectionTrace(false)
            return
        }
        val prefs = getSharedPreferences(ConnectionSettings.PREFERENCES_NAME, MODE_PRIVATE)
        val url = prefs.getString(ConnectionSettings.KEY_WSS_URL, "").orEmpty()
        if (url.isBlank() || socket != null) return

        // 证书固定：主机用自签名证书时 Android 默认不信任，握手会被拒
        // （实测 `self-signed certificate`，用户看到的就是「连了但两边都没变化」）。
        // 二维码里的 `tfp` 就是那张证书的指纹，这里只信任**恰好这一张**。
        val certFingerprint = prefs
            .getString(ConnectionSettings.KEY_CERT_FINGERPRINT, "")
            .orEmpty()
        val generation = connectionGeneration
        val verifiedCertificate = java.util.concurrent.atomic.AtomicReference<java.security.cert.X509Certificate>()
        connectionClient?.dispatcher?.executorService?.shutdown()
        connectionClient?.connectionPool?.evictAll()
        val httpClient = CertificatePinning.clientFor(certFingerprint) { verifiedCertificate.set(it) }
            .newBuilder().pingInterval(15, java.util.concurrent.TimeUnit.SECONDS).build()
        connectionClient = httpClient
        socket = httpClient.newWebSocket(Request.Builder().url(url).build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (generation != connectionGeneration) {
                    webSocket.cancel()
                    return
                }
                // TLS 已校验二维码中的证书指纹；用该主机证书公钥验证主机签名。
                val certificate = verifiedCertificate.get()
                val confirmed = identityStore.load()
                val qrKey = prefs.getString(ConnectionSettings.KEY_HOST_SIGNING_PUBLIC_KEY, "").orEmpty()
                val encoded = runCatching {
                    if (qrKey.isNotBlank()) {
                        val bytes = java.util.Base64.getDecoder().decode(qrKey)
                        java.security.KeyFactory.getInstance("EC").generatePublic(java.security.spec.X509EncodedKeySpec(bytes)).encoded
                    } else {
                        if (certFingerprint.isBlank()) null else certificate?.publicKey?.encoded
                    }
                }.getOrNull()
                if (encoded == null || confirmed == null) {
                    webSocket.close(CLOSE_NORMAL, "paired TLS identity required")
                    recordConnectionTrace(false, "无法取得已验证的主机证书或配对身份，请重新扫码配对")
                    return
                }
                authenticatedHostIdentity = PublicIdentity(
                    deviceId = confirmed.deviceId,
                    publicKeyEncoded = java.util.Base64.getEncoder().encodeToString(encoded),
                    fingerprint = Fingerprints.of(encoded),
                )
                // 第一个心跳把已打开的连接与设备 ID 绑定，电脑才能识别是哪部手机。
                webSocket.send(ProtocolCodec.encode(DeviceResponse(
                    request_id = "heartbeat", device_id = confirmed.deviceId,
                    status = "ok", message = "heartbeat",
                )))
                reconnectAttempt = 0
                reconnectHandler.removeCallbacks(reconnectRunnable)
                recordConnectionTrace(false, "已接通电脑，等待电脑确认设备权限…")
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                if (generation != connectionGeneration) return
                val result = remoteGate.processRaw(text)
                val cached = result.cachedJson
                if (cached != null) {
                    // 幂等命中：原样回放历史响应，不重复执行。
                    webSocket.send(cached)
                    return
                }
                val response = result.response
                if (response != null) {
                    webSocket.send(ProtocolCodec.encode(response))
                    return
                }
                // 解码失败：丢弃且不回包；错误码只进本地日志（已在 RemoteModeGate 内记录）。
                if (result.errorCode != null) {
                    logLocal("${result.errorCode}: remote frame discarded without reply")
                }
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, null)
                if (socket === webSocket) {
                    socket = null
                    hostConfirmed = false
                    authenticatedHostIdentity = null
                    recordConnectionTrace(false, "主机关闭了连接，请重新启动连接")
                    scheduleReconnect()
                }
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                if (socket === webSocket) {
                    socket = null
                    hostConfirmed = false
                    authenticatedHostIdentity = null
                    recordConnectionTrace(false, "连接已关闭，请重新启动连接")
                    scheduleReconnect()
                }
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (socket === webSocket) {
                    socket = null
                    hostConfirmed = false
                    authenticatedHostIdentity = null
                    recordConnectionTrace(false, ConnectionSettings.connectionFailureMessage(t))
                    scheduleReconnect()
                }
            }
        })
    }

    private fun closeSocket(reason: String) {
        connectionGeneration++
        hostConfirmed = false
        reconnectHandler.removeCallbacks(reconnectRunnable)
        authenticatedHostIdentity = null
        val previous = socket
        socket = null
        previous?.cancel()
        connectionClient?.dispatcher?.executorService?.shutdown()
        connectionClient?.connectionPool?.evictAll()
        connectionClient = null
        recordConnectionTrace(false)
    }

    /** Retry an existing pairing after a host restart or transient network failure. */
    private fun scheduleReconnect() {
        if (!modeStore.get().isRemote) return
        reconnectHandler.removeCallbacks(reconnectRunnable)
        reconnectAttempt = (reconnectAttempt + 1).coerceAtMost(8)
        val delayMs = (2000L shl (reconnectAttempt - 1)).coerceAtMost(30000L)
        reconnectHandler.postDelayed(reconnectRunnable, delayMs)
    }

    private fun createChannel() {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, "设备连接", NotificationManager.IMPORTANCE_LOW),
        )
    }

    private fun notification(text: String): Notification =
        NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.stat_sys_upload)
            .setContentTitle("Whale Device Agent")
            .setContentText(text)
            .setOngoing(true)
            .build()

    /** 本地日志：只写本机 logcat，不进入任何回包字段，也不发往网络。 */
    private fun logLocal(message: String) {
        android.util.Log.w(LOG_TAG, message)
    }

    override fun onDestroy() {
        reconnectHandler.removeCallbacks(reconnectRunnable)
        closeSocket("service stopped")
        if (::database.isInitialized) database.close()
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private companion object {
        const val CHANNEL_ID = "device"
        const val NOTIFICATION_ID = 1001
        const val LOG_TAG = "WhaleDevice"

        /**
         * M05 身份落盘位置（设备身份公钥、指纹和主机指纹）。
         *
         * 单独一个 prefs 文件，与 `agent_mode`（模式）分开：
         * 身份数据只在配对/验签链路读写，模式切换不应触碰它。
         */
        const val IDENTITY_PREFERENCES = "device_identity"
        const val CLOSE_NORMAL = 1000
    }
}
