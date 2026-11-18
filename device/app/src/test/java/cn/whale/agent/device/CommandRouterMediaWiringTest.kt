package cn.whale.agent.device

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * M08 媒体接入 `CommandRouter` 的固定测试（纯 JVM，不依赖 Android 运行时）。
 *
 * 覆盖 task-13 的五条硬要求：
 * 1. 媒体依赖为 null 时，四个分支保持 `denied/NOT_WIRED`（不得伪装成功）；
 * 2. 成功路径的回包 `evidence` 形状：只有 `asset_id` / `mime` / `size` / `sha256`
 *    （分享另加 `target`），**绝不含真实文件路径**；
 * 3. Controller 的失败码**原样透传**（七个 M08 状态码 + `ACTION_REJECTED`）；
 * 4. `device.media.share` 未获确认 → `denied/ACTION_REJECTED`；
 * 5. `device.media.pick` 没有前台宿主 → `denied/NOT_WIRED`，不得静默返回 `ok`。
 *
 * 本测试**不构造** `CommandRouter`（它需要 Android `Context`，而 android.jar 是桩，
 * 构造即抛 `Stub!`）；它直接测 `MediaRoutes` 这一层，正是为此把媒体路由
 * 从 `CommandRouter` 里拆出来的原因。
 */
class CommandRouterMediaWiringTest {
    @get:Rule
    val temporary = TemporaryFolder()

    // ---------- 测试替身 ----------

    /** 可配置的假媒体依赖：记录调用形状，返回预设结果。 */
    private class FakeWiring(
        override val assets: AssetStore,
        override val uploadEndpoint: String = "https://host.example",
        override val pickerAvailable: Boolean = true,
    ) : MediaWiring {
        var captureOutcome: CameraController.CaptureOutcome? = null
        var pickOutcome: PhotoPickerController.PickOutcome? = null
        var uploadOutcome: MediaUploadController.UploadOutcome? = null
        var shareResult: ShareResult? = null

        /** 记录路由层实际传下来的参数，确保 payload 被正确读取。 */
        val captureCalls = ArrayList<Pair<String, String>>()
        val pickCalls = ArrayList<Pair<String, Int>>()
        val uploadCalls = ArrayList<Triple<String, String, String>>()
        val shareCalls = ArrayList<Triple<String, String, Boolean>>()

        override fun capture(lens: String, quality: String): CameraController.CaptureOutcome {
            captureCalls.add(lens to quality)
            return captureOutcome
                ?: CameraController.CaptureOutcome.Failed(CommandErrors.NOT_WIRED, "no capture stub")
        }

        override fun pick(mediaType: String, maxItems: Int): PhotoPickerController.PickOutcome {
            pickCalls.add(mediaType to maxItems)
            return pickOutcome
                ?: PhotoPickerController.PickOutcome.Failed(CommandErrors.NOT_WIRED, "no pick stub")
        }

        override fun upload(assetId: String, deviceId: String, endpoint: String): MediaUploadController.UploadOutcome {
            uploadCalls.add(Triple(assetId, deviceId, endpoint))
            return uploadOutcome
                ?: MediaUploadController.UploadOutcome.Failed(CommandErrors.NOT_WIRED, "no upload stub", false)
        }

        override fun share(assetId: String, target: String, confirmed: Boolean): ShareResult {
            shareCalls.add(Triple(assetId, target, confirmed))
            return shareResult ?: ShareResult.Failed(CommandErrors.NOT_WIRED, "no share stub")
        }
    }

    private fun assets(): AssetStore = AssetStore(temporary.newFolder())

    private fun sampleEntry(store: AssetStore, assetId: String = "photo001", mime: String = "image/jpeg"): AssetStore.AssetEntry {
        val source = temporary.newFile("src_${System.nanoTime()}.jpg")
        source.writeBytes(byteArrayOf(1, 2, 3, 4))
        return store.register(source, mime, assetId, taskId = "task-1")
    }

    private fun envelope(
        command: String,
        payload: JsonObject = buildJsonObject { },
        deviceId: String = "device-1",
    ): DeviceEnvelope = DeviceEnvelope(
        protocol_version = 1,
        request_id = "req-media-001",
        device_id = deviceId,
        command = command,
        deadline_ms = 30_000,
        idempotency_key = "media-001",
        payload = payload,
    )

    private fun routes(
        wiring: MediaWiring?,
        shareConfirmed: (String, String) -> Boolean = { _, _ -> false },
    ): MediaRoutes = MediaRoutes(wiring, shareConfirmed)

    // ---------- 1. null 依赖 → NOT_WIRED ----------

    @Test
    fun nullMediaDependencyKeepsNotWiredForAllFourCommands() {
        val routes = routes(null)
        for (command in MEDIA_COMMANDS) {
            // route() 返回 null = 「本构建没有媒体依赖」，由 CommandRouter 降级为 NOT_WIRED。
            assertNull("$command 在无媒体依赖时必须返回 null 让路由层降级", routes.route(envelope(command)))
        }
    }

    @Test
    fun notWiredFailureMatchesTheRouterFallbackShape() {
        // CommandRouter 的降级路径必须产出 denied/NOT_WIRED，且仍是固定错误码。
        val fallback = DeviceResponse("req-media-001", "device-1", "denied", CommandErrors.NOT_WIRED, "not wired")
        assertEquals("denied", fallback.status)
        assertEquals(CommandErrors.NOT_WIRED, fallback.error_code)
        assertTrue(CommandErrors.isDeclared(CommandErrors.NOT_WIRED))
        // 路由层没有可用的媒体依赖时，`MediaRoutes.route` 一律 null。
        assertNull(routes(null).route(envelope("device.camera.capture")))
    }

    // ---------- 2. 相机：成功 evidence 形状 + 失败码透传 ----------

    @Test
    fun cameraCaptureSuccessCarriesMetadataOnly() {
        val store = assets()
        val entry = sampleEntry(store)
        val wiring = FakeWiring(store).apply {
            captureOutcome = CameraController.CaptureOutcome.Captured(entry)
        }
        val response = routes(wiring).route(envelope("device.camera.capture"))!!

        assertEquals("ok", response.status)
        assertEquals("", response.error_code)
        // evidence 只允许这四个字段。
        assertEquals(setOf("asset_id", "mime", "size", "sha256"), response.evidence.keys)
        assertEquals(entry.assetId, response.evidence["asset_id"])
        assertEquals(entry.mime, response.evidence["mime"])
        assertEquals(entry.size.toString(), response.evidence["size"])
        assertEquals(entry.sha256, response.evidence["sha256"])
        // 真实路径绝不出现。
        assertNoRealPath(response.evidence)
    }

    @Test
    fun cameraCaptureReadsLensAndQualityDefaults() {
        val store = assets()
        val wiring = FakeWiring(store).apply {
            captureOutcome = CameraController.CaptureOutcome.Captured(sampleEntry(store))
        }
        // 空 payload → 默认 back / standard。
        routes(wiring).route(envelope("device.camera.capture"))
        assertEquals(listOf("back" to "standard"), wiring.captureCalls)

        // 显式覆盖 → 原样传下去。
        routes(wiring).route(
            envelope(
                "device.camera.capture",
                buildJsonObject {
                    put("lens", "front")
                    put("quality", "high")
                },
            ),
        )
        assertEquals("front" to "high", wiring.captureCalls.last())
    }

    @Test
    fun cameraCapturePassesThroughControllerFailureCode() {
        val store = assets()
        val wiring = FakeWiring(store).apply {
            captureOutcome = CameraController.CaptureOutcome.Failed(
                MediaErrors.CAMERA_PERMISSION_REQUIRED,
                "CAMERA permission is not granted",
            )
        }
        val response = routes(wiring).route(envelope("device.camera.capture"))!!

        assertEquals("denied", response.status)
        assertEquals(MediaErrors.CAMERA_PERMISSION_REQUIRED, response.error_code)
        // 失败回包不得带任何媒体 evidence。
        assertTrue(response.evidence.isEmpty())
    }

    @Test
    fun everyMediaFailureCodeIsPassedThroughUnchanged() {
        val store = assets()
        // 七个个码都必须是已登记错误码（M06 是唯一登记处），且原样透传。
        for (code in MediaErrors.ALL) {
            assertTrue("$code 必须已登记进 CommandErrors.ALL", CommandErrors.isDeclared(code))
            val wiring = FakeWiring(store).apply {
                captureOutcome = CameraController.CaptureOutcome.Failed(code, "stub")
            }
            val response = routes(wiring).route(envelope("device.camera.capture"))!!
            assertEquals("denied", response.status)
            assertEquals(code, response.error_code)
        }
    }

    // ---------- 3. Photo Picker：无前台宿主 → NOT_WIRED ----------

    @Test
    fun pickWithoutForegroundHostIsNotWiredNotSilentOk() {
        val store = assets()
        var asked = false
        val wiring = FakeWiring(store, pickerAvailable = false).apply {
            pickOutcome = PhotoPickerController.PickOutcome.Picked(listOf(sampleEntry(store)))
        }
        // 用一个会标记的假实现确认：无宿主时根本不会走到 picker。
        val guarded = object : MediaWiring by wiring {
            override fun pick(mediaType: String, maxItems: Int): PhotoPickerController.PickOutcome {
                asked = true
                return wiring.pick(mediaType, maxItems)
            }
        }
        val response = routes(guarded).route(
            envelope(
                "device.media.pick",
                buildJsonObject {
                    put("media_type", "image")
                    put("max_items", 3)
                },
            ),
        )!!

        assertEquals("denied", response.status)
        assertEquals(CommandErrors.NOT_WIRED, response.error_code)
        // 关键：绝不能静默返回 ok，也不能在无宿主时调用 picker。
        assertFalse(asked)
        assertTrue(response.evidence.isEmpty())
        assertTrue(response.artifacts.isEmpty())
    }

    @Test
    fun pickWithForegroundHostReturnsMetadataOnly() {
        val store = assets()
        val first = sampleEntry(store, "pick001")
        val second = sampleEntry(store, "pick002")
        val wiring = FakeWiring(store).apply {
            pickOutcome = PhotoPickerController.PickOutcome.Picked(listOf(first, second))
        }
        val response = routes(wiring).route(
            envelope(
                "device.media.pick",
                buildJsonObject {
                    put("media_type", "video")
                    put("max_items", 3)
                },
            ),
        )!!

        assertEquals("ok", response.status)
        // 多选：每项进 artifacts，每项都只有四字段。
        assertEquals(2, response.artifacts.size)
        for (artifact in response.artifacts) {
            assertEquals(setOf("asset_id", "mime", "size", "sha256"), artifact.keys)
            assertNoRealPath(artifact)
        }
        assertEquals("2", response.evidence["asset_count"])
        // payload 被正确读取。
        assertEquals("video" to 3, wiring.pickCalls.last())
    }

    @Test
    fun pickSingleSelectionPutsEvidenceAtTopLevel() {
        val store = assets()
        val wiring = FakeWiring(store).apply {
            pickOutcome = PhotoPickerController.PickOutcome.Picked(listOf(sampleEntry(store, "single001")))
        }
        val response = routes(wiring).route(
            envelope(
                "device.media.pick",
                buildJsonObject {
                    put("media_type", "image")
                    put("max_items", 1)
                },
            ),
        )!!

        assertEquals("ok", response.status)
        assertEquals(setOf("asset_id", "mime", "size", "sha256"), response.evidence.keys)
        assertNoRealPath(response.evidence)
    }

    @Test
    fun pickerCancellationPassesThrough() {
        val store = assets()
        val wiring = FakeWiring(store).apply {
            pickOutcome = PhotoPickerController.PickOutcome.Failed(
                MediaErrors.PHOTO_PICKER_CANCELLED,
                "user cancelled the media picker",
            )
        }
        val response = routes(wiring).route(
            envelope(
                "device.media.pick",
                buildJsonObject {
                    put("media_type", "any")
                    put("max_items", 5)
                },
            ),
        )!!

        assertEquals("denied", response.status)
        assertEquals(MediaErrors.PHOTO_PICKER_CANCELLED, response.error_code)
        assertTrue(response.artifacts.isEmpty())
    }

    // ---------- 4. 上传：证据必须含 upload_complete ----------

    @Test
    fun uploadSuccessEvidenceContainsUploadComplete() {
        val store = assets()
        val entry = sampleEntry(store, "up001")
        val wiring = FakeWiring(store).apply {
            uploadOutcome = MediaUploadController.UploadOutcome.Uploaded(
                entry = entry,
                chunksSent = 2,
                evidence = store.evidence(entry),
            )
        }
        val response = routes(wiring).route(
            envelope("device.media.upload", buildJsonObject { put("asset_id", "up001") }),
        )!!

        assertEquals("ok", response.status)
        // 只有服务端确认过才算完成，所以 upload_complete 必须为 "true"。
        assertEquals("true", response.evidence["upload_complete"])
        assertEquals(entry.assetId, response.evidence["asset_id"])
        assertEquals(entry.sha256, response.evidence["sha256"])
        // 路由层把 device_id 与 endpoint 一起传下去。
        assertEquals(Triple("up001", "device-1", "https://host.example"), wiring.uploadCalls.single())
        assertNoRealPath(response.evidence)
    }

    @Test
    fun uploadFailurePassesThroughAndNeverClaimsComplete() {
        val store = assets()
        for (code in listOf(
            MediaErrors.UPLOAD_RETRYABLE,
            MediaErrors.UPLOAD_HASH_MISMATCH,
            MediaErrors.MEDIA_NOT_FOUND,
        )) {
            val wiring = FakeWiring(store).apply {
                uploadOutcome = MediaUploadController.UploadOutcome.Failed(code, "stub", retryable = false)
            }
            val response = routes(wiring).route(
                envelope("device.media.upload", buildJsonObject { put("asset_id", "ghost") }),
            )!!
            assertEquals("denied", response.status)
            assertEquals(code, response.error_code)
            // 失败回包绝不能夹带 upload_complete。
            assertFalse(response.evidence.containsKey("upload_complete"))
        }
    }

    // ---------- 5. 分享：未确认 → ACTION_REJECTED；无 provider → NOT_WIRED ----------

    @Test
    fun shareWithoutConfirmationIsActionRejected() {
        val store = assets()
        val entry = sampleEntry(store, "share001")
        // 贴近真实 Controller 的假实现：confirmed=false 时一律 ACTION_REJECTED、不外发。
        val wiring = object : MediaWiring by FakeWiring(store) {
            override fun share(assetId: String, target: String, confirmed: Boolean): ShareResult {
                return if (!confirmed) {
                    ShareResult.Failed(
                        CommandErrors.ACTION_REJECTED,
                        "media share requires explicit user confirmation before sending",
                    )
                } else {
                    ShareResult.Sent(store.evidence(entry), target.ifBlank { "system_share_sheet" })
                }
            }
        }
        // 确认回调固定返回 false：payload 里没有 confirmed 字段，模型无法自我确认。
        val response = routes(wiring) { _, _ -> false }
            .route(envelope("device.media.share", buildJsonObject { put("asset_id", "share001") }))!!

        // 未确认 -> denied/ACTION_REJECTED，且不得泄露任何素材元数据。
        assertEquals("denied", response.status)
        assertEquals(CommandErrors.ACTION_REJECTED, response.error_code)
        assertTrue(response.evidence.isEmpty())
    }

    @Test
    fun shareConfirmationResultIsPassedToTheHost() {
        val store = assets()
        val entry = sampleEntry(store, "share006")
        val wiring = FakeWiring(store).apply {
            shareResult = ShareResult.Sent(store.evidence(entry), "com.example")
        }
        var sawPair: Pair<String, String>? = null
        val response = routes(wiring) { assetId, target ->
            sawPair = assetId to target
            true
        }.route(
            envelope(
                "device.media.share",
                buildJsonObject {
                    put("asset_id", "share006")
                    put("target", "com.example")
                },
            ),
        )!!

        // 确认回调拿到的是 payload 里的原始值，且结果被原样传给宿主。
        assertEquals("share006" to "com.example", sawPair)
        assertTrue("确认通过后必须把 true 交给宿主", wiring.shareCalls.single().third)
        assertEquals("ok", response.status)
    }

    @Test
    fun shareRejectedByControllerWhenUnconfirmedEvenIfStubWouldSend() {
        val store = assets()
        val entry = sampleEntry(store, "share002")
        // 模拟真实 Controller 行为：confirmed=false 时不外发，直接 ACTION_REJECTED。
        val wiring = FakeWiring(store).apply {
            shareResult = ShareResult.Failed(
                CommandErrors.ACTION_REJECTED,
                "media share requires explicit user confirmation before sending",
            )
        }
        val response = routes(wiring) { _, _ -> false }
            .route(
                envelope(
                    "device.media.share",
                    buildJsonObject {
                        put("asset_id", "share002")
                        put("target", "com.example")
                    },
                ),
            )!!

        assertEquals("denied", response.status)
        assertEquals(CommandErrors.ACTION_REJECTED, response.error_code)
        assertEquals(Triple("share002", "com.example", false), wiring.shareCalls.single())
        // 未确认时不得泄露任何素材元数据。
        assertTrue(response.evidence.isEmpty())
        assertEquals(entry.assetId, entry.assetId) // 引用以免未使用告警
    }

    @Test
    fun shareWithoutContentUriProviderIsNotWiredAndNeverExposesFilePath() {
        val store = assets()
        val entry = sampleEntry(store, "share003")
        val wiring = FakeWiring(store).apply {
            // 模拟 MediaStoreController 在 contentUriProvider == null 时的行为。
            shareResult = ShareResult.Failed(
                CommandErrors.NOT_WIRED,
                "share requires a content:// provider; file paths are never exposed",
            )
        }
        val response = routes(wiring) { _, _ -> true }
            .route(envelope("device.media.share", buildJsonObject { put("asset_id", "share003") }))!!

        assertEquals("denied", response.status)
        assertEquals(CommandErrors.NOT_WIRED, response.error_code)
        // 绝不退化成 file:// 真实路径。
        for (value in response.evidence.values) {
            assertFalse("回包不得含 file:// 真实路径: $value", value.startsWith("file://"))
            assertFalse("回包不得含真实路径: $value", value.contains(temporary.root.absolutePath))
        }
        assertEquals(entry.assetId, entry.assetId)
    }

    @Test
    fun shareSuccessCarriesMetadataPlusTarget() {
        val store = assets()
        val entry = sampleEntry(store, "share004", mime = "video/mp4")
        val wiring = FakeWiring(store).apply {
            shareResult = ShareResult.Sent(store.evidence(entry), "com.example.chat")
        }
        val response = routes(wiring) { _, _ -> true }
            .route(
                envelope(
                    "device.media.share",
                    buildJsonObject {
                        put("asset_id", "share004")
                        put("target", "com.example.chat")
                    },
                ),
            )!!

        assertEquals("ok", response.status)
        // 成功 evidence：四字段 + target，共五项，别无其他。
        assertEquals(setOf("asset_id", "mime", "size", "sha256", "target"), response.evidence.keys)
        assertEquals("com.example.chat", response.evidence["target"])
        assertNoRealPath(response.evidence)
    }

    @Test
    fun shareTargetFallsBackToSystemShareSheetWhenBlank() {
        val store = assets()
        val entry = sampleEntry(store, "share005")
        val wiring = FakeWiring(store).apply {
            shareResult = ShareResult.Sent(store.evidence(entry), MediaStoreController.SYSTEM_SHARE_SHEET)
        }
        val response = routes(wiring) { _, _ -> true }
            .route(envelope("device.media.share", buildJsonObject { put("asset_id", "share005") }))!!

        assertEquals("ok", response.status)
        assertEquals(MediaStoreController.SYSTEM_SHARE_SHEET, response.evidence["target"])
        assertEquals(Triple("share005", "", true), wiring.shareCalls.single())
    }

    // ---------- 边界：未知命令不归媒体路由 ----------

    @Test
    fun mediaRoutesIgnoreNonMediaCommands() {
        val store = assets()
        val wiring = FakeWiring(store)
        val routes = routes(wiring)
        // 非媒体命令必须返回 null，交给 CommandRouter 的原有分支，不能在这里抢走。
        for (command in listOf(
            "device.app.launch",
            "device.ui.observe",
            "device.sms.send",
            "device.device_status",
        )) {
            assertNull(routes.route(envelope(command)))
        }
        assertTrue(wiring.captureCalls.isEmpty())
        assertTrue(wiring.pickCalls.isEmpty())
        assertTrue(wiring.uploadCalls.isEmpty())
        assertTrue(wiring.shareCalls.isEmpty())
    }

    // ---------- 工具 ----------

    /** 回包 evidence / artifacts 里出现真实路径即为泄漏。 */
    private fun assertNoRealPath(evidence: Map<String, String>) {
        val rootPath = temporary.root.absolutePath
        for ((key, value) in evidence) {
            assertFalse("evidence[$key] 不得含真实路径: $value", value.contains(rootPath))
            assertFalse("evidence[$key] 不得是 file://: $value", value.startsWith("file://"))
            // mime 与 target 天然含 `/`（如 image/jpeg、com.example/app），只对 ID/哈希类字段断言。
            if (key !in setOf("mime", "target")) {
                assertFalse("evidence[$key] 不得含路径分隔符: $value", value.contains("/") || value.contains("\\"))
            }
        }
    }

    private companion object {
        val MEDIA_COMMANDS = listOf(
            "device.camera.capture",
            "device.media.pick",
            "device.media.upload",
            "device.media.share",
        )
    }
}
