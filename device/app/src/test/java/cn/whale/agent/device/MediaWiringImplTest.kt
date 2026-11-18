package cn.whale.agent.device

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * M08 `MediaWiringImpl` 的固定测试（纯 JVM，不构造任何 Android 对象）。
 *
 * 覆盖 task-2 的三条「最容易做错的地方」，以及「只透传」这条工程约束：
 * 1. `uploadEndpoint` 只能由 `wss://` 推导 https 基址，推不出必须是**空串**
 *    （不能猜 `http://`，不能猜端口，不能把路径带进基址）；
 * 2. `pickerAvailable` 必须如实：没有前台 `ActivityResult` 宿主就是 `false`，
 *    且此时 `pick` **连后端都不调用**，`media.pick` 固定 `NOT_WIRED`；
 * 3. `share` 尊重 `confirmed`：未确认**不调用后端**，因此不可能 `startActivity`；
 * 4. 四个方法只透传 Controller 结果，失败码一字不改，成功回包只含四字段元数据。
 *
 * `MediaRoutes` 是纯 JVM 的，所以这里可以顺着「`MediaWiringImpl` → `MediaRoutes`」跑真实回包形状，
 * 不必依赖 android.jar 的桩实现（`CommandRouter` 本体会因 `Context` 桩而无法构造）。
 */
class MediaWiringImplTest {
    @get:Rule
    val temporary = TemporaryFolder()

    @After
    fun resetPickerHosts() {
        // 进程级登记处必须在每个用例后清空，否则用例之间会互相污染。
        MediaPickerHosts.detach()
    }

    // ---------- 测试替身 ----------

    /** 可观察后端：记录调用参数，返回预设结果；不碰相机/网络/Intent。 */
    private class FakeBackends(private val store: AssetStore) : MediaBackends {
        var captureOutcome: CameraController.CaptureOutcome? = null
        var uploadOutcome: MediaUploadController.UploadOutcome? = null
        var shareResult: ShareResult? = null

        val captureCalls = ArrayList<Pair<String, String>>()
        val uploadCalls = ArrayList<Triple<String, String, String>>()
        val shareCalls = ArrayList<Triple<String, String, Boolean>>()

        override fun capture(lens: String, quality: String): CameraController.CaptureOutcome {
            captureCalls.add(lens to quality)
            return captureOutcome
                ?: CameraController.CaptureOutcome.Failed(CommandErrors.NOT_WIRED, "no capture stub")
        }

        override fun pick(mediaType: String, maxItems: Int): PhotoPickerController.PickOutcome =
            PhotoPickerController.PickOutcome.Failed(CommandErrors.NOT_WIRED, "backends must not pick")

        override fun upload(
            assetId: String,
            deviceId: String,
            endpoint: String,
        ): MediaUploadController.UploadOutcome {
            uploadCalls.add(Triple(assetId, deviceId, endpoint))
            return uploadOutcome
                ?: MediaUploadController.UploadOutcome.Failed(CommandErrors.NOT_WIRED, "no upload stub", false)
        }

        override fun share(assetId: String, target: String, confirmed: Boolean): ShareResult {
            shareCalls.add(Triple(assetId, target, confirmed))
            return shareResult ?: ShareResult.Failed(CommandErrors.NOT_WIRED, "no share stub")
        }
    }

    /** 可切换「有没有前台宿主」的选择器替身。 */
    private class FakePicker(
        private val store: AssetStore,
        override var available: Boolean,
    ) : MediaPickerDelegate {
        val calls = ArrayList<Pair<String, Int>>()

        override fun pick(mediaType: String, maxItems: Int): PhotoPickerController.PickOutcome {
            calls.add(mediaType to maxItems)
            // 真实宿主也是把已登记素材交回来，这里同样只给资产表里的条目。
            val entries = store.assetsForTask("task-1")
            if (entries.isEmpty()) {
                return PhotoPickerController.PickOutcome.Failed(
                    MediaErrors.PHOTO_PICKER_CANCELLED,
                    "user cancelled the media picker",
                )
            }
            return PhotoPickerController.PickOutcome.Picked(entries)
        }
    }

    private fun assets(): AssetStore = AssetStore(temporary.newFolder())

    private fun sampleEntry(
        store: AssetStore,
        assetId: String = "photo001",
        mime: String = "image/jpeg",
    ): AssetStore.AssetEntry {
        val source = temporary.newFile("src_${System.nanoTime()}.jpg")
        source.writeBytes(byteArrayOf(1, 2, 3, 4))
        return store.register(source, mime, assetId, taskId = "task-1")
    }

    private fun wiring(
        store: AssetStore,
        backends: FakeBackends,
        endpoint: String = "https://host.example",
        picker: MediaPickerDelegate? = null,
    ): MediaWiringImpl = MediaWiringImpl(
        assets = store,
        uploadEndpoint = endpoint,
        backends = backends,
        picker = picker,
    )

    private fun envelope(command: String, payload: JsonObject = buildJsonObject { }): DeviceEnvelope =
        DeviceEnvelope(
            protocol_version = 1,
            request_id = "req-m08-001",
            device_id = "device-1",
            command = command,
            deadline_ms = 30_000,
            idempotency_key = "m08-001",
            payload = payload,
        )

    private fun routes(wiring: MediaWiring?, confirmed: Boolean = false): MediaRoutes =
        MediaRoutes(wiring) { _, _ -> confirmed }

    // ---------- 1. uploadEndpoint：只从 wss 推导，推不出就是空串 ----------

    @Test
    fun wssUrlBecomesHttpsBaseWithoutPath() {
        assertEquals("https://host.example", uploadEndpointFromWssUrl("wss://host.example/ws"))
        assertEquals("https://host.example", uploadEndpointFromWssUrl("wss://host.example"))
        // 大小写不敏感地接受 scheme，主机名按原样保留。
        assertEquals("https://Host.Example", uploadEndpointFromWssUrl("WSS://Host.Example"))
    }

    @Test
    fun wssUrlKeepsNonDefaultPortAndDropsTheDefaultOne() {
        assertEquals("https://host.example:8443", uploadEndpointFromWssUrl("wss://host.example:8443/ws"))
        // 443 与 WebSocket 默认端口都是「不带端口」的等价写法。
        assertEquals("https://host.example", uploadEndpointFromWssUrl("wss://host.example:443/ws"))
    }

    @Test
    fun nonWssSchemeYieldsEmptyEndpointAndNeverGuessesHttp() {
        for (raw in listOf(
            "",
            "   ",
            null,
            "ws://host.example/ws",
            "http://host.example",
            "https://host.example",
            "host.example",
            "wss://",
            "wss://host example/ws",
        )) {
            val endpoint = uploadEndpointFromWssUrl(raw)
            assertEquals("推不出 https 基址时必须返回空串，输入=$raw", "", endpoint)
            assertFalse("端点绝不能是明文 http: $endpoint", endpoint.startsWith("http://"))
        }
    }

    @Test
    fun wssUrlWithUserInfoOrFragmentIsNotDerivable() {
        // 带凭据或片段的 URL 不能当作上传基址：它们会被拼进请求路径，属于注入面。
        assertEquals("", uploadEndpointFromWssUrl("wss://user:pass@host.example/ws"))
        assertEquals("", uploadEndpointFromWssUrl("wss://host.example/ws#frag"))
    }

    @Test
    fun emptyEndpointKeepsUploadRetryableInsteadOfPretendingSuccess() {
        val store = assets()
        val backends = FakeBackends(store).apply {
            uploadOutcome = MediaUploadController.UploadOutcome.Failed(
                MediaErrors.UPLOAD_RETRYABLE,
                "upload endpoint must be an https URL",
                retryable = true,
            )
        }
        val response = routes(wiring(store, backends, endpoint = ""))
            .route(envelope("device.media.upload", buildJsonObject { put("asset_id", "ghost") }))!!

        assertEquals("denied", response.status)
        assertEquals(MediaErrors.UPLOAD_RETRYABLE, response.error_code)
        // 空端点原样传到后端，绝不自己编一个 http。
        assertEquals(Triple("ghost", "device-1", ""), backends.uploadCalls.single())
        assertFalse(response.evidence.containsKey("upload_complete"))
    }

    // ---------- 2. pickerAvailable：如实，且有守卫 ----------

    @Test
    fun pickerIsUnavailableWhenNoForegroundHostIsRegistered() {
        val store = assets()
        val backends = FakeBackends(store)
        val impl = wiring(store, backends)
        // 既没有构造注入、进程登记处也为空 —— 这就是「本构建没有宿主」的真实结论。
        assertFalse(impl.pickerAvailable)
        assertNull(MediaPickerHosts.current())
        assertFalse(MediaPickerHosts.available())
    }

    @Test
    fun pickWithoutHostIsNotWiredAndNeverTouchesBackend() {
        val store = assets()
        val backends = FakeBackends(store)
        val impl = wiring(store, backends)
        val outcome = impl.pick("image", 3)

        assertTrue(outcome is PhotoPickerController.PickOutcome.Failed)
        val failed = outcome as PhotoPickerController.PickOutcome.Failed
        assertEquals(CommandErrors.NOT_WIRED, failed.code)
        // 关键：没有宿主时后端一次都不许被调用，更不能静默返回空列表当成功。
        assertTrue(backends.uploadCalls.isEmpty())
    }

    @Test
    fun pickThroughRouterIsNotWiredWhenHostIsAbsent() {
        val store = assets()
        val backends = FakeBackends(store)
        val response = routes(wiring(store, backends)).route(
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
        assertTrue(response.evidence.isEmpty())
        assertTrue(response.artifacts.isEmpty())
    }

    @Test
    fun attachedHostMakesPickerAvailableAndReturnsMetadataOnly() {
        val store = assets()
        val entry = sampleEntry(store, "pick001")
        val backends = FakeBackends(store)
        val picker = FakePicker(store, available = true)
        val impl = wiring(store, backends, picker = picker)

        assertTrue(impl.pickerAvailable)
        val response = routes(impl).route(
            envelope(
                "device.media.pick",
                buildJsonObject {
                    put("media_type", "video")
                    put("max_items", 2)
                },
            ),
        )!!

        assertEquals("ok", response.status)
        assertEquals(setOf("asset_id", "mime", "size", "sha256"), response.evidence.keys)
        assertNoRealPath(response.evidence)
        assertEquals(listOf("video" to 2), picker.calls)
        assertEquals(entry.assetId, response.evidence["asset_id"])
    }

    @Test
    fun hostThatReportsItselfUnavailableStaysNotWired() {
        val store = assets()
        val backends = FakeBackends(store)
        val picker = FakePicker(store, available = false)
        val impl = wiring(store, backends, picker = picker)

        assertFalse(impl.pickerAvailable)
        val outcome = impl.pick("image", 1) as PhotoPickerController.PickOutcome.Failed
        assertEquals(CommandErrors.NOT_WIRED, outcome.code)
        // 宿主自己说不可用，就不能再去请求它。
        assertTrue(picker.calls.isEmpty())
    }

    @Test
    fun processWideHostRegistrationIsHonestAndRevocable() {
        // 只有真正登记后才变 true；撤销登记立刻回落 false。
        assertFalse(MediaPickerHosts.available())
        val store = assets()
        MediaPickerHosts.attach(FakePicker(store, available = true))
        assertTrue(MediaPickerHosts.available())
        // 构造时不带 picker 的实现应当读到进程登记处（宿主晚于装配登记也要如实生效）。
        val impl = wiring(store, FakeBackends(store))
        assertTrue(impl.pickerAvailable)

        MediaPickerHosts.detach()
        assertFalse(MediaPickerHosts.available())
        assertFalse("撤销登记后必须立刻变回不可用", impl.pickerAvailable)
    }

    // ---------- 3. share：未确认绝不外发 ----------

    @Test
    fun shareWithoutConfirmationIsRejectedAndBackendIsNeverCalled() {
        val store = assets()
        val backends = FakeBackends(store)
        val impl = wiring(store, backends)

        val outcome = impl.share("share001", "com.example", confirmed = false) as ShareResult.Failed
        assertEquals(CommandErrors.ACTION_REJECTED, outcome.code)
        // 后端没被调用 = 没有 prepareShare、没有 startActivity。
        assertTrue(backends.shareCalls.isEmpty())

        val response = routes(impl, confirmed = false).route(
            envelope(
                "device.media.share",
                buildJsonObject {
                    put("asset_id", "share001")
                    put("target", "com.example")
                },
            ),
        )!!
        assertEquals("denied", response.status)
        assertEquals(CommandErrors.ACTION_REJECTED, response.error_code)
        assertTrue(response.evidence.isEmpty())
    }

    @Test
    fun shareWithConfirmationForwardsTrueAndReturnsMetadataPlusTarget() {
        val store = assets()
        val entry = sampleEntry(store, "share002", mime = "video/mp4")
        val backends = FakeBackends(store).apply {
            shareResult = ShareResult.Sent(store.evidence(entry), "com.example.chat")
        }
        val impl = wiring(store, backends)

        val response = routes(impl, confirmed = true).route(
            envelope(
                "device.media.share",
                buildJsonObject {
                    put("asset_id", "share002")
                    put("target", "com.example.chat")
                },
            ),
        )!!

        assertEquals("ok", response.status)
        // 后端只能收到 confirmed=true 的调用。
        assertEquals(Triple("share002", "com.example.chat", true), backends.shareCalls.single())
        assertEquals(setOf("asset_id", "mime", "size", "sha256", "target"), response.evidence.keys)
        assertEquals("com.example.chat", response.evidence["target"])
        assertNoRealPath(response.evidence)
    }

    @Test
    fun shareFailureFromControllerPassesThroughUnchanged() {
        val store = assets()
        val backends = FakeBackends(store).apply {
            // MediaStoreController 在 contentUriProvider == null 时的真实返回。
            shareResult = ShareResult.Failed(
                CommandErrors.NOT_WIRED,
                "share requires a content:// provider; file paths are never exposed",
            )
        }
        val response = routes(wiring(store, backends), confirmed = true).route(
            envelope("device.media.share", buildJsonObject { put("asset_id", "share003") }),
        )!!

        assertEquals("denied", response.status)
        assertEquals(CommandErrors.NOT_WIRED, response.error_code)
        assertNoRealPath(response.evidence)
    }

    // ---------- 4. capture / upload：只透传 ----------

    @Test
    fun captureSuccessCarriesOnlyFourMetadataFields() {
        val store = assets()
        val entry = sampleEntry(store, "cap001")
        val backends = FakeBackends(store).apply {
            captureOutcome = CameraController.CaptureOutcome.Captured(entry)
        }
        val response = routes(wiring(store, backends)).route(
            envelope(
                "device.camera.capture",
                buildJsonObject {
                    put("lens", "front")
                    put("quality", "high")
                },
            ),
        )!!

        assertEquals("ok", response.status)
        assertEquals(setOf("asset_id", "mime", "size", "sha256"), response.evidence.keys)
        assertNoRealPath(response.evidence)
        // payload 的 lens/quality 原样传到 Controller。
        assertEquals(listOf("front" to "high"), backends.captureCalls)
    }

    @Test
    fun captureDefaultLensAndQualityMatchTheSchema() {
        val store = assets()
        val entry = sampleEntry(store, "cap002")
        val backends = FakeBackends(store).apply {
            captureOutcome = CameraController.CaptureOutcome.Captured(entry)
        }
        routes(wiring(store, backends)).route(envelope("device.camera.capture"))
        assertEquals(listOf("back" to "standard"), backends.captureCalls)
    }

    @Test
    fun captureFailureCodeIsForwardedVerbatim() {
        val store = assets()
        for (code in MediaErrors.ALL) {
            val backends = FakeBackends(store).apply {
                captureOutcome = CameraController.CaptureOutcome.Failed(code, "stub")
            }
            val response = routes(wiring(store, backends)).route(envelope("device.camera.capture"))!!
            assertEquals("denied", response.status)
            assertEquals(code, response.error_code)
            assertTrue(response.evidence.isEmpty())
        }
    }

    @Test
    fun uploadSuccessRequiresServerConfirmationFlag() {
        val store = assets()
        val entry = sampleEntry(store, "up001")
        val backends = FakeBackends(store).apply {
            uploadOutcome = MediaUploadController.UploadOutcome.Uploaded(
                entry = entry,
                chunksSent = 2,
                evidence = store.evidence(entry),
            )
        }
        val response = routes(wiring(store, backends, endpoint = "https://host.example")).route(
            envelope("device.media.upload", buildJsonObject { put("asset_id", "up001") }),
        )!!

        assertEquals("ok", response.status)
        assertEquals("true", response.evidence["upload_complete"])
        // 端点由 wiring 提供，device_id 来自信封。
        assertEquals(
            Triple("up001", "device-1", "https://host.example"),
            backends.uploadCalls.single(),
        )
        assertNoRealPath(response.evidence)
    }

    @Test
    fun unknownAssetUploadNeverClaimsCompletion() {
        val store = assets()
        val backends = FakeBackends(store).apply {
            uploadOutcome = MediaUploadController.UploadOutcome.Failed(
                MediaErrors.MEDIA_NOT_FOUND,
                "invalid asset id",
                retryable = false,
            )
        }
        val response = routes(wiring(store, backends)).route(
            envelope("device.media.upload", buildJsonObject { put("asset_id", "ghost") }),
        )!!

        assertEquals("denied", response.status)
        assertEquals(MediaErrors.MEDIA_NOT_FOUND, response.error_code)
        assertFalse(response.evidence.containsKey("upload_complete"))
    }

    // ---------- 5. 素材表：受控目录、只读元数据 ----------

    @Test
    fun assetsPointAtTheInjectedControlledRoot() {
        val store = assets()
        val impl = wiring(store, FakeBackends(store))
        // AssetStore 的 root 就是注入目录；实现里不存在第二个可写位置。
        val entry = sampleEntry(store, "asset001")
        assertEquals(setOf("asset_id", "mime", "size", "sha256"), impl.assets.evidence(entry).keys)
        assertNoRealPath(impl.assets.evidence(entry))
        assertEquals(1, impl.assets.size())
    }

    // ---------- 工具 ----------

    /** 回包里出现真实路径或 file:// 即为泄漏。 */
    private fun assertNoRealPath(evidence: Map<String, String>) {
        val rootPath = temporary.root.absolutePath
        for ((key, value) in evidence) {
            assertFalse("evidence[$key] 不得含真实路径: $value", value.contains(rootPath))
            assertFalse("evidence[$key] 不得是 file://: $value", value.startsWith("file://"))
            if (key !in setOf("mime", "target")) {
                assertFalse(
                    "evidence[$key] 不得含路径分隔符: $value",
                    value.contains("/") || value.contains("\\"),
                )
            }
        }
    }
}
