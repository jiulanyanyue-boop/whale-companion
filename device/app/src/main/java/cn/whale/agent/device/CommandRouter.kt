package cn.whale.agent.device

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.ContactsContract
import android.provider.Settings
import android.telephony.SmsManager
import androidx.core.content.ContextCompat
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * M06 唯一执行器（两种模式共用）。
 *
 * 固定执行顺序：
 * 1. 协议版本闸门；
 * 2. 命令白名单闸门（`DeviceCommands.ALL` + [CommandSpecs]）；
 * 3. payload 最终校验闸门（[PayloadValidator]）；
 * 4. 串行闸门（同一设备只允许一个 active task，并发返回 `queued`）；
 * 5. 路由到具体实现层。
 *
 * 执行优先级固定为：系统 API / Intent → 应用专用适配器 → 无障碍语义树。
 * 模型不能选择执行层；设备按命令、能力档案与失败原因自动选择（M06 产品文档）。
 */
class CommandRouter(
    private val context: Context,
    /**
     * M08 媒体依赖注入点。
     *
     * 为 null 时四个 media 分支保持 `NOT_WIRED`（不伪装成功）。本类**不**在这里
     * new CameraX / OkHttp：媒体依赖由宿主（MainActivity / DeviceConnectionService）
     * 构造后注入，这样 `CommandRouter` 仍然是可测的纯路由层。
     */
    private val media: MediaWiring? = null,
    /**
     * 分享确认回调，语义为 `(assetId, target) -> 是否已获用户明确确认`。
     *
     * 默认值**一律 false**：`device.media.share` 的 payload 里没有 `confirmed` 字段，
     * 模型无法自我确认。只有宿主接入 M04 UserGate / ConfirmLevelPolicy 之后，
     * 才能传入真实确认结果。
     */
    private val shareConfirmed: (String, String) -> Boolean = { _, _ -> false },
) {
    @Volatile
    var allowlist: Set<String> = emptySet()

    @Volatile
    var blockedPackages: Set<String> = emptySet()

    private val accessibility = AccessibilityController()

    /** M08 四个媒体分支的路由逻辑（不持有 Context，可在纯 JVM 上真跑测试）。 */
    private val mediaRoutes = MediaRoutes(media, shareConfirmed)

    /** 串行执行闸门：并发命令返回 `queued`，不能同时点击。 */
    private val serialGate = SerialCommandGate()

    init {
        val preferences = context.getSharedPreferences("device_policy", Context.MODE_PRIVATE)
        allowlist = preferences.getStringSet("allowlist", emptySet()).orEmpty()
        blockedPackages = preferences.getStringSet("blocked_packages", emptySet()).orEmpty()
    }

    fun execute(envelope: DeviceEnvelope): DeviceResponse {
        if (envelope.protocol_version != 1) {
            return denied(envelope, CommandErrors.UNSUPPORTED_PROTOCOL, "unsupported protocol")
        }
        if (envelope.command !in DeviceCommands.ALL) {
            return denied(envelope, CommandErrors.UNKNOWN_COMMAND, "command is not declared")
        }
        val validation = PayloadValidator.validate(envelope.command, envelope.payload)
        if (!validation.ok) {
            return denied(envelope, validation.code, validation.message)
        }
        if (!serialGate.tryEnter()) {
            return SerialCommandGate.queued(envelope)
        }
        return try {
            route(envelope)
        } finally {
            serialGate.release()
        }
    }

    private fun route(envelope: DeviceEnvelope): DeviceResponse = when (envelope.command) {
        "device.device_status" -> {
            val battery = context.registerReceiver(null, android.content.IntentFilter(Intent.ACTION_BATTERY_CHANGED))
            val level = battery?.getIntExtra(android.os.BatteryManager.EXTRA_LEVEL, -1) ?: -1
            val scale = battery?.getIntExtra(android.os.BatteryManager.EXTRA_SCALE, 100) ?: 100
            val batteryStatus = battery?.getIntExtra(android.os.BatteryManager.EXTRA_STATUS, -1) ?: -1
            val locked = (context.getSystemService(Context.KEYGUARD_SERVICE) as? android.app.KeyguardManager)?.isDeviceLocked == true
            val homePackage = context.packageManager.resolveActivity(Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME), PackageManager.MATCH_DEFAULT_ONLY)?.activityInfo?.packageName.orEmpty()
            ok(envelope, mapOf("package" to context.packageName, "home_package" to homePackage, "model" to android.os.Build.MODEL, "android_version" to android.os.Build.VERSION.RELEASE,
                "sdk" to android.os.Build.VERSION.SDK_INT.toString(), "battery_percent" to (if (level >= 0 && scale > 0) level * 100 / scale else -1).toString(),
                "charging" to (batteryStatus == android.os.BatteryManager.BATTERY_STATUS_CHARGING || batteryStatus == android.os.BatteryManager.BATTERY_STATUS_FULL).toString(),
                "screen_locked" to locked.toString(), "accessibility_connected" to (WhaleAccessibilityService.instance != null).toString(),
                "screenshot_supported" to (android.os.Build.VERSION.SDK_INT >= 30).toString()))
        }
        "device.system.unlock" -> UnlockController(context).unlock(envelope)
        "device.app.list" -> listApps(envelope)
        "device.gallery.list" -> galleryList(envelope)
        "device.gallery.read" -> galleryRead(envelope)
        "device.screen.capture" -> ScreenCaptureController(context).capture(envelope, allowlist, blockedPackages)
        "device.app.launch" -> launch(envelope)
        "device.system.settings" -> openSettings(envelope)
        "device.ui.observe" -> observe(envelope)
        "device.ui.tap" -> accessibility.tap(envelope)
        "device.ui.long_press" -> accessibility.longPress(envelope)
        "device.ui.scroll" -> accessibility.scroll(envelope)
        "device.ui.input" -> accessibility.input(envelope)
        "device.ui.back" -> accessibility.global(envelope, android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_BACK)
        "device.ui.home" -> accessibility.global(envelope, android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_HOME)
        "device.ui.recents" -> accessibility.global(envelope, android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_RECENTS)
        "device.ui.notifications" -> accessibility.global(envelope, android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS)
        "device.telephony.dial" -> dial(envelope)
        "device.contacts.search" -> searchContacts(envelope)
        "device.sms.compose" -> composeSms(envelope)
        "device.sms.send" -> sendSms(envelope)
        // M06 路由表：camera.* / media.* 由 [MediaRoutes] 承接。
        // 未注入媒体依赖、或依赖内缺少前台宿主时，一律固定失败，不得伪装成功。
        "device.camera.capture", "device.media.pick", "device.media.upload", "device.media.share" ->
            mediaRoutes.route(envelope)
                ?: denied(envelope, CommandErrors.NOT_WIRED, "media route is not available in this build")
        else -> denied(envelope, CommandErrors.NOT_WIRED, "command is declared but not wired in this build")
    }

    private fun galleryList(envelope: DeviceEnvelope): DeviceResponse = try {
        val limit = PayloadValidator.intValue(envelope.payload, "limit") ?: 20
        val offset = PayloadValidator.intValue(envelope.payload, "offset") ?: 0
        DeviceResponse(envelope.request_id, envelope.device_id, "ok", artifacts = DeviceGalleryController(context).list(limit, offset))
    } catch (t: Throwable) {
        denied(envelope, if (t is GalleryPermissionException) CommandErrors.PERMISSION_REQUIRED else CommandErrors.MEDIA_NOT_FOUND,
            t.message ?: "gallery access failed")
    }

    private fun galleryRead(envelope: DeviceEnvelope): DeviceResponse = try {
        val mediaId = PayloadValidator.stringValue(envelope.payload, "media_id")
        DeviceResponse(envelope.request_id, envelope.device_id, "ok", evidence = DeviceGalleryController(context).preview(mediaId))
    } catch (t: Throwable) {
        denied(envelope, if (t is GalleryPermissionException) CommandErrors.PERMISSION_REQUIRED else CommandErrors.MEDIA_NOT_FOUND,
            t.message ?: "image preview failed")
    }

    /**
     * `device.ui.observe`。
     *
     * Tree mode returns semantic nodes; vision mode returns a real screenshot.
     */
    private fun observe(envelope: DeviceEnvelope): DeviceResponse {
        val requested = PayloadValidator.stringValue(envelope.payload, "mode", "auto")
        if (requested == "vision") return ScreenCaptureController(context).capture(envelope, allowlist, blockedPackages)
        val observed = accessibility.observe(requested).copyIds(envelope)
        return observed.copy(
            evidence = observed.evidence + ("mode" to "tree"),
        )
    }

    private fun listApps(envelope: DeviceEnvelope): DeviceResponse {
        val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
        val manager = context.packageManager
        val apps = manager.queryIntentActivities(intent, PackageManager.MATCH_ALL)
            .distinctBy { it.activityInfo.packageName }
            .map {
                val packageName = it.activityInfo.packageName
                val name = runCatching { it.activityInfo.applicationInfo.loadLabel(manager).toString().trim() }
                    .getOrDefault("").ifBlank { packageName }
                packageName to name
            }
            .sortedWith(compareBy({ it.second.lowercase() }, { it.first }))
        val catalog = buildJsonArray {
            apps.forEach { (packageName, name) ->
                add(buildJsonObject {
                    put("package_name", packageName)
                    put("label", name)
                })
            }
        }
        return DeviceResponse(
            envelope.request_id,
            envelope.device_id,
            "ok",
            evidence = mapOf(
                "packages" to apps.joinToString("\n") { it.first },
                "apps" to catalog.toString(),
            ),
        )
    }

    private fun launch(envelope: DeviceEnvelope): DeviceResponse {
        val packageName = PayloadValidator.stringValue(envelope.payload, "package_name")
        if (!DevicePolicy.canControl(packageName, allowlist, blockedPackages)) {
            return denied(envelope, CommandErrors.PACKAGE_NOT_ALLOWED, "target package is not allowed")
        }
        val intent = context.packageManager.getLaunchIntentForPackage(packageName)
            ?: return denied(envelope, CommandErrors.NO_LAUNCH_INTENT, "package has no launch intent")
        intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            context.startActivity(intent)
            ok(envelope, mapOf("package" to packageName))
        } catch (t: Throwable) {
            denied(envelope, CommandErrors.LAUNCH_FAILED, t.javaClass.simpleName)
        }
    }

    private fun openSettings(envelope: DeviceEnvelope): DeviceResponse {
        val action = PayloadValidator.stringValue(envelope.payload, "action", Settings.ACTION_SETTINGS)
        val intent = Intent(action).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            context.startActivity(intent)
            ok(envelope, mapOf("action" to action))
        } catch (t: Throwable) {
            denied(envelope, CommandErrors.SETTINGS_FAILED, t.javaClass.simpleName)
        }
    }

    private fun dial(envelope: DeviceEnvelope): DeviceResponse {
        val number = PayloadValidator.stringValue(envelope.payload, "number")
        if (number.isBlank()) {
            return denied(envelope, CommandErrors.INVALID_NUMBER, "number is empty")
        }
        val intent = Intent(Intent.ACTION_DIAL, Uri.parse("tel:${Uri.encode(number)}"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            context.startActivity(intent)
            ok(envelope, mapOf("number" to number))
        } catch (t: Throwable) {
            denied(envelope, CommandErrors.DIAL_FAILED, t.javaClass.simpleName)
        }
    }

    private fun searchContacts(envelope: DeviceEnvelope): DeviceResponse {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.READ_CONTACTS) != PackageManager.PERMISSION_GRANTED) {
            return denied(envelope, CommandErrors.CONTACT_PERMISSION_REQUIRED, "READ_CONTACTS is not granted")
        }
        val query = PayloadValidator.stringValue(envelope.payload, "query")
        val rows = ArrayList<Map<String, String>>()
        val cursor = context.contentResolver.query(
            ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
            arrayOf(ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME, ContactsContract.CommonDataKinds.Phone.NUMBER),
            "${ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME} LIKE ? OR ${ContactsContract.CommonDataKinds.Phone.NUMBER} LIKE ?",
            arrayOf("%$query%", "%$query%"),
            "${ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME} ASC",
        )
        cursor?.use {
            val nameIndex = it.getColumnIndex(ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME)
            val numberIndex = it.getColumnIndex(ContactsContract.CommonDataKinds.Phone.NUMBER)
            while (it.moveToNext() && rows.size < 20) {
                rows.add(mapOf("name" to it.getString(nameIndex).orEmpty(), "number" to it.getString(numberIndex).orEmpty()))
            }
        }
        return DeviceResponse(envelope.request_id, envelope.device_id, "ok", artifacts = rows)
    }

    private fun composeSms(envelope: DeviceEnvelope): DeviceResponse {
        val number = PayloadValidator.stringValue(envelope.payload, "number")
        val text = PayloadValidator.stringValue(envelope.payload, "text")
        val intent = Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:${Uri.encode(number)}"))
            .putExtra("sms_body", text)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            context.startActivity(intent)
            ok(envelope, mapOf("number" to number))
        } catch (t: Throwable) {
            denied(envelope, CommandErrors.SMS_COMPOSE_FAILED, t.javaClass.simpleName)
        }
    }

    private fun sendSms(envelope: DeviceEnvelope): DeviceResponse {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.SEND_SMS) != PackageManager.PERMISSION_GRANTED) {
            return denied(envelope, CommandErrors.SMS_PERMISSION_REQUIRED, "SEND_SMS is not granted")
        }
        val number = PayloadValidator.stringValue(envelope.payload, "number")
        val text = PayloadValidator.stringValue(envelope.payload, "text")
        return try {
            SmsManager.getDefault().sendTextMessage(number, null, text, null, null)
            ok(envelope, mapOf("number" to number))
        } catch (t: Throwable) {
            denied(envelope, CommandErrors.SMS_SEND_FAILED, t.javaClass.simpleName)
        }
    }

    private fun DeviceResponse.copyIds(envelope: DeviceEnvelope): DeviceResponse =
        copy(request_id = envelope.request_id, device_id = envelope.device_id)

    private fun ok(envelope: DeviceEnvelope, evidence: Map<String, String>): DeviceResponse =
        DeviceResponse(envelope.request_id, envelope.device_id, "ok", evidence = evidence)

    private fun denied(envelope: DeviceEnvelope, code: String, message: String): DeviceResponse =
        DeviceResponse(envelope.request_id, envelope.device_id, "denied", code, message)
}

/**
 * 分享分支的路由结果。
 *
 * 刻意**不**复用 `MediaStoreController.ShareOutcome`：那个类型里含 `Intent`，
 * 而 `CommandRouter` 这一层只需要「已确认并已外发」的元数据。把 Intent 留在
 * 宿主接线层，`MediaRoutes` 才能在纯 JVM 上被真跑测试（android.jar 是桩，
 * 构造 `Intent` 会抛 `Stub!`）。
 */
sealed interface ShareResult {
    /** 已确认、已真正外发；[evidence] 只含 asset_id/mime/size/sha256。 */
    data class Sent(val evidence: Map<String, String>, val target: String) : ShareResult

    data class Failed(val code: String, val message: String) : ShareResult
}

/**
 * M08 媒体依赖的注入契约。
 *
 * 宿主（MainActivity / DeviceConnectionService）用真实 Controller 实现它；测试用假实现。
 * `CommandRouter` 只吃这个接口，因此**不**需要在路由层里 new CameraX / OkHttp，
 * 保持纯路由层的可测性。
 *
 * [assets] 用于把素材元数据转成回包 evidence（只含 asset_id/mime/size/sha256）。
 */
interface MediaWiring {
    val assets: AssetStore

    /** 上传端点（https 基址）；非 https 时上传分支返回 `UPLOAD_RETRYABLE`。 */
    val uploadEndpoint: String

    /**
     * 是否有前台宿主可以接收 `ActivityResult`。
     *
     * Photo Picker 的 `onPicked` 必须由前台 Activity 的回调驱动；没有宿主时
     * `device.media.pick` 固定返回 `NOT_WIRED`，**不得**静默返回 ok。
     */
    val pickerAvailable: Boolean

    fun capture(lens: String, quality: String): CameraController.CaptureOutcome

    fun pick(mediaType: String, maxItems: Int): PhotoPickerController.PickOutcome

    fun upload(assetId: String, deviceId: String, endpoint: String): MediaUploadController.UploadOutcome

    /**
     * 分享。实现方负责真正的确认与 `startActivity`（例如调用
     * `MediaStoreController.share(...)`），只把结果元数据回传给路由层。
     */
    fun share(assetId: String, target: String, confirmed: Boolean): ShareResult
}

/**
 * `device.camera.capture` / `device.media.pick` / `device.media.upload` / `device.media.share`
 * 四个路由分支的实现。
 *
 * 全部失败都透传 Controller 返回的固定状态码（M08 七个码 + `NOT_WIRED` / `ACTION_REJECTED`），
 * 不新增码、不伪装成功。回包 evidence **只含** `asset_id` / `mime` / `size` / `sha256`
 * （分享另加 `target`），**绝不**含真实文件路径。
 */
class MediaRoutes(
    private val media: MediaWiring?,
    private val shareConfirmed: (String, String) -> Boolean,
) {
    /**
     * @return `null` 表示「本构建没有可用的媒体依赖」，由 `CommandRouter` 统一降级为
     *   `denied/NOT_WIRED`。
     */
    fun route(envelope: DeviceEnvelope): DeviceResponse? {
        val wiring = media ?: return null
        return when (envelope.command) {
            "device.camera.capture" -> capture(envelope, wiring)
            "device.media.pick" -> pick(envelope, wiring)
            "device.media.upload" -> upload(envelope, wiring)
            "device.media.share" -> share(envelope, wiring)
            else -> null
        }
    }

    private fun capture(envelope: DeviceEnvelope, wiring: MediaWiring): DeviceResponse {
        // 默认后置 / 标准质量；取值已由 PayloadValidator 限制在 back|front、standard|high。
        val lens = PayloadValidator.stringValue(envelope.payload, "lens", "back")
        val quality = PayloadValidator.stringValue(envelope.payload, "quality", "standard")
        return when (val outcome = wiring.capture(lens, quality)) {
            is CameraController.CaptureOutcome.Captured ->
                ok(envelope, wiring.assets.evidence(outcome.entry) + outcome.preview)

            is CameraController.CaptureOutcome.Failed ->
                denied(envelope, outcome.code, outcome.message)
        }
    }

    private fun pick(envelope: DeviceEnvelope, wiring: MediaWiring): DeviceResponse {
        // 没有前台宿主就拿不到 ActivityResult：固定失败，绝不静默 ok。
        if (!wiring.pickerAvailable) {
            return denied(
                envelope,
                CommandErrors.NOT_WIRED,
                "media pick requires a foreground activity to receive the picker result",
            )
        }
        val mediaType = PayloadValidator.stringValue(envelope.payload, "media_type", "any")
        val maxItems = PayloadValidator.intValue(envelope.payload, "max_items") ?: 1
        return when (val outcome = wiring.pick(mediaType, maxItems)) {
            is PhotoPickerController.PickOutcome.Picked -> {
                val evidences = outcome.entries.map { wiring.assets.evidence(it) }
                // 单选：evidence 直接是那一项的四字段；多选：逐项进 artifacts。
                val evidence = if (evidences.size == 1) {
                    evidences.single()
                } else {
                    mapOf("asset_count" to evidences.size.toString())
                }
                DeviceResponse(
                    envelope.request_id,
                    envelope.device_id,
                    "ok",
                    artifacts = evidences,
                    evidence = evidence,
                )
            }

            is PhotoPickerController.PickOutcome.Failed -> denied(envelope, outcome.code, outcome.message)
        }
    }

    private fun upload(envelope: DeviceEnvelope, wiring: MediaWiring): DeviceResponse {
        val assetId = PayloadValidator.stringValue(envelope.payload, "asset_id")
        return when (val outcome = wiring.upload(assetId, envelope.device_id, wiring.uploadEndpoint)) {
            is MediaUploadController.UploadOutcome.Uploaded ->
                // 上传完成的唯一证据就是服务端确认；这里统一回填 upload_complete=true。
                ok(envelope, outcome.evidence + ("upload_complete" to "true"))

            is MediaUploadController.UploadOutcome.Failed ->
                denied(envelope, outcome.code, outcome.message)
        }
    }

    private fun share(envelope: DeviceEnvelope, wiring: MediaWiring): DeviceResponse {
        val assetId = PayloadValidator.stringValue(envelope.payload, "asset_id")
        val target = PayloadValidator.stringValue(envelope.payload, "target")
        // 确认结果只能来自宿主注入的确认回调；payload 里没有 confirmed 字段，
        // 模型无法自我确认，默认回调一律 false。
        val confirmed = shareConfirmed(assetId, target)
        return when (val outcome = wiring.share(assetId, target, confirmed)) {
            is ShareResult.Sent ->
                // 成功回包：四字段元数据 + 目标；真实路径永不出现。
                ok(envelope, outcome.evidence + ("target" to outcome.target))

            is ShareResult.Failed -> denied(envelope, outcome.code, outcome.message)
        }
    }

    private fun ok(envelope: DeviceEnvelope, evidence: Map<String, String>): DeviceResponse =
        DeviceResponse(envelope.request_id, envelope.device_id, "ok", evidence = evidence)

    private fun denied(envelope: DeviceEnvelope, code: String, message: String): DeviceResponse =
        DeviceResponse(envelope.request_id, envelope.device_id, "denied", code, message)
}
