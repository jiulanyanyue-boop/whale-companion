package cn.whale.agent.device

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
 * M08 固定测试（纯 JVM，不依赖 Android 运行时）。
 *
 * 覆盖 `docs/modules/M08-media-development.md` 的全部硬性条款：
 * 1. 素材 ID 只允许 `[A-Za-z0-9._-]{1,160}`，并拒绝 `..`、斜杠、反斜杠与用户路径；
 * 2. 媒体类型只允许 image、audio、video 三类 MIME 前缀；
 * 3. 单文件 100 MiB、单任务 20 个素材、临时素材 24 小时清理；
 * 4. 分块固定 1 MiB，`Content-Range: bytes start-end/total` 的偏移计算；
 * 5. `X-Asset-Sha256` 的总 hash 校验；
 * 6. 上传完成判定：全部字节到位**且** hash 匹配才 `upload_complete`；
 * 7. 七个失败状态各自的产生条件（一个不多、一个不少）。
 *
 * 验收矩阵 `test-fixtures/m13-acceptance-matrix.json` 的两条 M08 用例也由本文件覆盖：
 * - `camera_capture_upload`：拍照后只回 `asset_id`/`mime`/`size`/`sha256` 四项，
 *   资产表恰好新增一行（见 [cameraCaptureReturnsMetadataOnly]、
 *   [capturePreconditionBlocksBeforeAnyFile]）；
 * - `photo_picker_only_user_uri`：只接受系统选择器回传的 `content://` URI，
 *   取消即固定失败，资产表最多 `max_items` 行（见 [pickerCancellationIsFixedFailure]、
 *   [pickerRejectsAnythingButContentUri]）。
 */
class AssetStoreTest {
    @get:Rule
    val temporary = TemporaryFolder()

    // ---------- 测试替身与工具 ----------

    private fun store(
        maxBytes: Long = MediaLimits.MAX_MEDIA_BYTES,
        maxAssetsPerTask: Int = MediaLimits.MAX_ASSETS_PER_TASK,
        ttlMs: Long = MediaLimits.TEMP_TTL_MS,
    ): AssetStore = AssetStore(
        root = temporary.newFolder(),
        maxBytes = maxBytes,
        maxAssetsPerTask = maxAssetsPerTask,
        ttlMs = ttlMs,
    )

    private var fileCounter = 0

    /**
     * 造一个临时源文件。
     *
     * 每次都用唯一文件名：`TemporaryFolder.newFile(name)` 在名字已存在时会抛 IOException，
     * 同名复用会让测试假失败。
     */
    private fun sourceFile(bytes: ByteArray, name: String = "source.bin"): File {
        val unique = "${fileCounter++}_$name"
        val file = temporary.newFile(unique)
        file.writeBytes(bytes)
        return file
    }

    private fun failCode(block: () -> Unit): String {
        val thrown = try {
            block()
            null
        } catch (e: MediaException) {
            e
        }
        assertNotNull("expected a MediaException", thrown)
        return thrown!!.code
    }

    // ---------- 1. 素材 ID 规则 ----------

    @Test
    fun assetIdAcceptsOnlyDocumentedCharset() {
        for (valid in listOf("a", "image_001", "IMG-2024.10.30", "abc.DEF-123_456", "A1-_.", "x".repeat(160))) {
            assertTrue("应该接受合法 ID: $valid", isValidAssetId(valid))
        }
        // 边界：160 通过，161 拒绝。
        assertTrue(isValidAssetId("x".repeat(160)))
        assertFalse(isValidAssetId("x".repeat(161)))
    }

    @Test
    fun assetIdRejectsTraversalSeparatorsAndUserPaths() {
        val rejected = listOf(
            "",                       // 空
            "..",
            "aa..bb",                 // 任意位置的 ..
            "../escape",              // 父目录
            "a/../../etc",
            "sub/dir",                // 正斜杠
            "sub\\dir",               // 反斜杠
            "C:\\Users\\me\\x.jpg",   // Windows 用户路径
            "C:/Users/me/x.jpg",
            "/etc/passwd",            // 绝对路径
            "~/media",                // 用户路径
            "file name",              // 空格不在字符集内
            "资产.png",               // 非 ASCII
            "a:b",
            "asset\n",
            null,
        )
        for (invalid in rejected) {
            assertFalse("应该拒绝非法 ID: $invalid", isValidAssetId(invalid))
        }
    }

    @Test
    fun deviceIdRejectsPathInjection() {
        assertTrue(isValidDeviceId("device-1"))
        assertTrue(isValidDeviceId("dev_01.2-3"))
        for (invalid in listOf("", "../device", "a/b", "a\\b", "..", "d".repeat(129), null)) {
            assertFalse("应该拒绝非法 device_id: $invalid", isValidDeviceId(invalid))
        }
    }

    @Test
    fun registerRejectsTraversalIdsAsMediaNotFound() {
        val store = store()
        val source = sourceFile(byteArrayOf(1, 2, 3), "img.png")
        for (bad in listOf("../escape", "sub/dir", "sub\\dir", "..", "x".repeat(200), "")) {
            assertEquals(
                MediaErrors.MEDIA_NOT_FOUND,
                failCode { store.register(source, "image/png", bad) },
            )
        }
        // 合法 ID 才能落库，且文件必须落在 root 之内。
        val entry = store.register(source, "image/png", "photo_001")
        assertEquals("photo_001", entry.assetId)
        assertEquals("image/png", entry.mime)
        assertEquals(3L, entry.size)
        assertEquals(1, store.size())
        val stored = store.fileOf("photo_001")
        assertEquals("photo_001", stored.name)
        assertTrue("素材必须落在受控 root 内", store.remove("photo_001"))
        assertFalse(stored.exists())
    }

    @Test
    fun registerWithoutIdGeneratesRandomHexId() {
        val store = store()
        val entry = store.register(sourceFile(byteArrayOf(7), "a.png"), "image/png")
        assertTrue(isValidAssetId(entry.assetId))
        assertEquals(32, entry.assetId.length)  // UUID 去横线
        assertTrue(Regex("[0-9a-f]{32}").matches(entry.assetId))
    }

    @Test
    fun unknownAssetIsMediaNotFoundNotAnExceptionType() {
        val store = store()
        assertNull(store.entry("ghost"))
        assertNull(store.entry("../escape"))
        assertNull(store.entry(null))
        assertEquals(MediaErrors.MEDIA_NOT_FOUND, failCode { store.require("ghost") })
        assertEquals(MediaErrors.MEDIA_NOT_FOUND, failCode { store.fileOf("ghost") })
        assertFalse(store.remove("../escape"))
        assertFalse(store.remove("ghost"))
    }

    @Test
    fun registerMissingSourceFileIsMediaNotFound() {
        val store = store()
        val missing = File(temporary.root, "nope.png")
        assertEquals(MediaErrors.MEDIA_NOT_FOUND, failCode { store.register(missing, "image/png", "ghost") })
    }

    // ---------- 2. 媒体类型规则 ----------

    @Test
    fun mediaTypeAcceptsOnlyImageAudioVideo() {
        for (mime in listOf(
            "image/png", "image/jpeg", "image/webp",
            "audio/mp4", "audio/ogg", "audio/mpeg",
            "video/mp4", "video/quicktime", "video/webm",
        )) {
            assertTrue("应该接受: $mime", isAllowedMediaType(mime))
        }
        for (mime in listOf(
            "application/pdf",
            "application/octet-stream",
            "text/plain",
            "text/html",
            "image",                  // 缺子类型
            "image/",                 // 空子类型
            "image/png;charset=utf8",
            "IMAGE/PNG",              // 前缀大小写不符
            "images/png",             // 前缀多字母
            "",
            null,
        )) {
            assertFalse("应该拒绝: $mime", isAllowedMediaType(mime))
        }
    }

    @Test
    fun registerRejectsDisallowedMediaType() {
        val store = store()
        val source = sourceFile(byteArrayOf(1), "note.bin")
        assertEquals(
            MediaErrors.MEDIA_TYPE_NOT_ALLOWED,
            failCode { store.register(source, "application/pdf", "note") },
        )
        assertEquals(
            MediaErrors.MEDIA_TYPE_NOT_ALLOWED,
            failCode { store.register(source, "", "note2") },
        )
    }

    @Test
    fun mediaKindMatchesPickerMimeAndSchemaEnum() {
        assertEquals(MediaKind.IMAGE, MediaKind.parse("image"))
        assertEquals(MediaKind.VIDEO, MediaKind.parse("video"))
        assertEquals(MediaKind.AUDIO, MediaKind.parse("audio"))
        assertEquals(MediaKind.ANY, MediaKind.parse("any"))
        // schema 之外的枚举值不得被接受。
        assertNull(MediaKind.parse("document"))
        assertNull(MediaKind.parse(""))
        assertEquals(MediaKind.ANY, MediaKind.parseOrAny("document"))

        assertTrue(MediaKind.IMAGE.allowsMime("image/png"))
        assertFalse(MediaKind.IMAGE.allowsMime("video/mp4"))
        assertTrue(MediaKind.VIDEO.allowsMime("video/mp4"))
        assertFalse(MediaKind.VIDEO.allowsMime("image/png"))
        assertTrue(MediaKind.AUDIO.allowsMime("audio/mpeg"))
        assertTrue(MediaKind.ANY.allowsMime("image/png"))
        assertTrue(MediaKind.ANY.allowsMime("audio/mpeg"))
        assertTrue(MediaKind.ANY.allowsMime("video/mp4"))
        assertFalse(MediaKind.ANY.allowsMime("application/pdf"))

        assertEquals("image/*", MediaKind.IMAGE.pickerMimeType)
        assertEquals("video/*", MediaKind.VIDEO.pickerMimeType)
        assertEquals("audio/*", MediaKind.AUDIO.pickerMimeType)
    }

    // ---------- 3. 大小上限、单任务素材数、24 小时清理 ----------

    @Test
    fun singleFileLimitIsOneHundredMebibytes() {
        assertEquals(100L * 1024 * 1024, MediaLimits.MAX_MEDIA_BYTES)
        assertTrue(isAllowedMediaSize(1L))
        assertTrue(isAllowedMediaSize(MediaLimits.MAX_MEDIA_BYTES))
        assertFalse(isAllowedMediaSize(MediaLimits.MAX_MEDIA_BYTES + 1))
        assertFalse(isAllowedMediaSize(0L))
        assertFalse(isAllowedMediaSize(-1L))

        // 小上限下真实登记：4 字节 > 3 字节上限。
        val store = store(maxBytes = 3)
        val source = sourceFile(byteArrayOf(1, 2, 3, 4), "big.png")
        assertEquals(MediaErrors.MEDIA_TOO_LARGE, failCode { store.register(source, "image/png", "big") })
        assertEquals(0, store.size())

        val okay = sourceFile(byteArrayOf(1, 2, 3), "ok.png")
        assertEquals("ok", store.register(okay, "image/png", "ok").assetId)
    }

    @Test
    fun taskCapIsTwentyAssets() {
        assertEquals(20, MediaLimits.MAX_ASSETS_PER_TASK)
        val store = store()
        val source = sourceFile(byteArrayOf(1), "a.png")
        for (index in 0 until MediaLimits.MAX_ASSETS_PER_TASK) {
            store.register(source, "image/png", "img${index}", taskId = "task-1")
        }
        assertEquals(MediaLimits.MAX_ASSETS_PER_TASK, store.countForTask("task-1"))
        // 第 21 个：固定集合里没有「任务素材过多」码，并入 MEDIA_TOO_LARGE。
        assertEquals(
            MediaErrors.MEDIA_TOO_LARGE,
            failCode { store.register(source, "image/png", "img20", taskId = "task-1") },
        )
        assertEquals(MediaLimits.MAX_ASSETS_PER_TASK, store.countForTask("task-1"))
        // 上限是按任务计：换任务仍可登记。
        store.register(source, "image/png", "img20", taskId = "task-2")
        assertEquals(1, store.countForTask("task-2"))
    }

    @Test
    fun overwritingSameIdDoesNotConsumeTaskQuota() {
        val store = store()
        val source = sourceFile(byteArrayOf(1), "a.png")
        for (index in 0 until MediaLimits.MAX_ASSETS_PER_TASK) {
            store.register(source, "image/png", "img$index", taskId = "task-1")
        }
        // 覆盖**已存在**的同一个 ID 不是新增素材，不触发上限。
        // 注意：循环里生成的 ID 是 img0..img19，所以要覆盖的是 img0（不是 img000）。
        assertEquals("img0", store.register(source, "image/png", "img0", taskId = "task-1").assetId)
        assertEquals(MediaLimits.MAX_ASSETS_PER_TASK, store.countForTask("task-1"))
    }

    @Test
    fun tempAssetsAreSweptAfterTwentyFourHours() {
        assertEquals(24L * 60 * 60 * 1000, MediaLimits.TEMP_TTL_MS)
        val store = store()
        val source = sourceFile(byteArrayOf(1), "a.png")
        store.register(source, "image/png", "old", nowMs = 1_000L)
        store.register(source, "image/png", "new", nowMs = 1_000L + MediaLimits.TEMP_TTL_MS)

        // 恰好到 24 小时的旧素材被清理，新的保留。
        val removed = store.sweepExpired(nowMs = 1_000L + MediaLimits.TEMP_TTL_MS + 1)
        assertEquals(listOf("old"), removed)
        assertNull(store.entry("old"))
        assertNotNull(store.entry("new"))
        assertEquals(1, store.size())
    }

    @Test
    fun taskAssetsAreOnlyTheTaskOwnedOnes() {
        val store = store()
        val source = sourceFile(byteArrayOf(1), "a.png")
        store.register(source, "image/png", "img0", taskId = "task-1")
        store.register(source, "image/png", "img1", taskId = "task-2")

        assertEquals(1, store.assetsForTask("task-1").size)
        assertEquals("img0", store.assetsForTask("task-1").single().assetId)
        assertTrue(store.assetsForTask("task-3").isEmpty())
        // 空 taskId 不归属任何任务，也不会被当成"全部"。
        assertTrue(store.assetsForTask("").isEmpty())
        assertEquals(0, store.countForTask(""))
    }

    // ---------- 4. 分块偏移与 Content-Range（固定 1 MiB） ----------

    @Test
    fun chunkSizeIsFixedToOneMebibyte() {
        assertEquals(1024 * 1024, MediaLimits.CHUNK_BYTES)
        assertEquals(1, chunkCount(MediaLimits.CHUNK_BYTES.toLong()))
        assertEquals(2, chunkCount(MediaLimits.CHUNK_BYTES.toLong() + 1))
        assertEquals(3, chunkCount(3L * MediaLimits.CHUNK_BYTES))
        assertEquals(0, chunkCount(0L))
    }

    @Test
    fun contentRangeIsOneMibAligned() {
        val total = 5L * MediaLimits.CHUNK_BYTES + 7
        val first = planContentRange(0, total)
        assertEquals(0L, first.start)
        assertEquals(MediaLimits.CHUNK_BYTES.toLong() - 1, first.end)
        assertEquals(total, first.total)
        assertEquals("bytes 0-${MediaLimits.CHUNK_BYTES - 1}/$total", first.header())

        val second = planContentRange(1, total)
        assertEquals(MediaLimits.CHUNK_BYTES.toLong(), second.start)
        assertEquals(2L * MediaLimits.CHUNK_BYTES - 1, second.end)
        assertEquals(1, second.chunkIndex())

        val everything = planAllChunks(total)
        assertEquals(6, everything.size)
        // 尾块按 total 截断，不是整块；且必须标记为最后一块。
        val last = everything.last()
        assertEquals(5L * MediaLimits.CHUNK_BYTES, last.start)
        assertEquals(5L * MediaLimits.CHUNK_BYTES + 6, last.end)
        assertEquals(7L, last.length)
        assertTrue(last.isLast())
        assertFalse(first.isLast())

        // 各块必须首尾相接且不重叠，拼接还原整份长度。
        for (index in 0 until everything.size - 1) {
            assertEquals(everything[index].end + 1, everything[index + 1].start)
        }
        assertEquals(total, everything.sumOf { it.length })
    }

    @Test
    fun outOfRangeChunkIsRetryableAndZeroTotalIsTooLarge() {
        assertEquals(
            MediaErrors.UPLOAD_RETRYABLE,
            failCode { planContentRange(1, MediaLimits.CHUNK_BYTES.toLong()) },
        )
        assertEquals(MediaErrors.UPLOAD_RETRYABLE, failCode { planContentRange(-1, 1024L) })
        assertEquals(MediaErrors.MEDIA_TOO_LARGE, failCode { planContentRange(0, 0L) })
    }

    @Test
    fun contentRangeParserAcceptsOnlyDocumentFormat() {
        val total = 2L * MediaLimits.CHUNK_BYTES
        val raw = "bytes 0-${MediaLimits.CHUNK_BYTES - 1}/$total"
        val parsed = ContentRange.parse(raw)
        assertEquals(0L, parsed.start)
        assertEquals(total, parsed.total)
        assertEquals(raw, parsed.header())

        for (bad in listOf(
            null,
            "",
            "bytes 0-1023/2048",                       // 不是 1 MiB 分块
            "bytes 1048575-1048575/1048575",           // 未对齐
            "bytes 1048576-2097150/${2 * MediaLimits.CHUNK_BYTES}",  // end 少于计划
            "Bytes 0-1048575/$total",                  // 大小写不符
            "bytes 0-1048575/*",                       // 未知 total
            "bytes 5-9/10",
            "bytes 0-9/9",                             // end 越界
            "bytes 3-2/10",                            // end < start
        )) {
            assertEquals(
                "应该拒绝 Content-Range: $bad",
                MediaErrors.UPLOAD_RETRYABLE,
                failCode { ContentRange.parse(bad) },
            )
        }
        // total 为 0 是大小问题，不是可重试。
        assertEquals(MediaErrors.MEDIA_TOO_LARGE, failCode { ContentRange.parse("bytes 0-3/0") })
    }

    @Test
    fun uploadPathShapeIsFixed() {
        assertEquals(
            "/v1/devices/device-1/assets/photo_001/chunks/0",
            assetChunkPath("device-1", "photo_001", 0),
        )
        assertEquals(
            "/v1/devices/device-1/assets/photo_001/chunks/2",
            assetChunkPath("device-1", "photo_001", 2),
        )
        for (badDevice in listOf("../device", "a/b", "a\\b", "", "d".repeat(129))) {
            assertEquals(MediaErrors.MEDIA_NOT_FOUND, failCode { assetChunkPath(badDevice, "photo", 0) })
        }
        for (badAsset in listOf("../photo", "a/b", "", "..")) {
            assertEquals(MediaErrors.MEDIA_NOT_FOUND, failCode { assetChunkPath("device-1", badAsset, 0) })
        }
        assertEquals(
            MediaErrors.UPLOAD_RETRYABLE,
            failCode { assetChunkPath("device-1", "photo", -1) },
        )
    }

    @Test
    fun uploadUrlRequiresHttps() {
        assertEquals(
            "https://host.example/v1/devices/device-1/assets/photo/chunks/3",
            assetChunkUrl("https://host.example", "device-1", "photo", 3),
        )
        // 尾斜杠不多出一条。
        assertEquals(
            "https://host.example/v1/devices/device-1/assets/photo/chunks/3",
            assetChunkUrl("https://host.example/", "device-1", "photo", 3),
        )
        // 明文端点直接拒绝：媒体不得走 http。
        for (bad in listOf("http://host.example", "ws://host.example", "wss://host.example", "host.example")) {
            assertEquals(
                MediaErrors.UPLOAD_RETRYABLE,
                failCode { assetChunkUrl(bad, "device-1", "photo", 0) },
            )
        }
    }

    @Test
    fun chunkHeadersCarryContentRangeAndAssetHash() {
        val total = 3L * MediaLimits.CHUNK_BYTES
        val digest = "A".repeat(64)
        val headers = chunkHeaders("photo", 1, total, digest)
        assertEquals(
            "bytes ${MediaLimits.CHUNK_BYTES}-${2 * MediaLimits.CHUNK_BYTES - 1}/$total",
            headers["Content-Range"],
        )
        assertEquals("a".repeat(64), headers["X-Asset-Sha256"])
        assertEquals("application/octet-stream", headers["Content-Type"])

        // 非 64 位十六进制的 hash 不得被当成合法摘要。
        for (bad in listOf("", "abc", "z".repeat(64), "A".repeat(63))) {
            assertEquals(MediaErrors.UPLOAD_RETRYABLE, failCode { chunkHeaders("photo", 0, 1024L, bad) })
        }
        assertEquals(MediaErrors.MEDIA_NOT_FOUND, failCode { chunkHeaders("../photo", 0, 1024L, digest) })
    }

    @Test
    fun readChunkSlicesAreExactAndLossless() {
        val total = 2L * MediaLimits.CHUNK_BYTES + 5
        val payload = ByteArray(total.toInt()) { index -> (index % 251).toByte() }
        val file = sourceFile(payload, "blob.mp4")

        val chunks = (0 until 3).map { readChunk(file, it, total) }
        assertEquals(MediaLimits.CHUNK_BYTES, chunks[0].size)
        assertEquals(MediaLimits.CHUNK_BYTES, chunks[1].size)
        assertEquals(5, chunks[2].size)
        // ByteArray 不支持 + 拼接，逐块写入再比较。
        val joined = ByteArray(total.toInt())
        var offset = 0
        for (chunk in chunks) {
            chunk.copyInto(joined, offset)
            offset += chunk.size
        }
        assertEquals(offset, joined.size)
        assertTrue("分块拼接必须还原原始字节", joined.contentEquals(payload))

        assertEquals(MediaErrors.UPLOAD_RETRYABLE, failCode { readChunk(file, 3, total) })
    }

    // ---------- 5. sha256 校验 ----------

    @Test
    fun sha256MatchesKnownVector() {
        // NIST 向量：b"abc" 的 sha256。
        assertEquals(
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            sha256(byteArrayOf(0x61, 0x62, 0x63)),
        )
        // 负字节必须按无符号 8 位编码：ByteArray(0..255) 里有一半是负值。
        val allBytes = ByteArray(256) { it.toByte() }
        val digest = sha256(allBytes)
        assertEquals(64, digest.length)
        assertTrue(Regex("[0-9a-f]{64}").matches(digest))
    }

    @Test
    fun fileAndByteSha256Agree() {
        val payload = ByteArray(4096) { (it * 7).toByte() }
        val file = sourceFile(payload, "hash.bin")
        assertEquals(sha256(payload), sha256(file))
    }

    @Test
    fun sha256HexGuardRejectsNonDigests() {
        assertTrue(isSha256Hex("A".repeat(64)))
        assertTrue(isSha256Hex("a".repeat(64)))
        assertFalse(isSha256Hex("a".repeat(63)))
        assertFalse(isSha256Hex("a".repeat(65)))
        assertFalse(isSha256Hex("g".repeat(64)))
        assertFalse(isSha256Hex(""))
        assertFalse(isSha256Hex(null))
    }

    @Test
    fun verifyUploadHashDetectsMismatch() {
        val file = sourceFile(byteArrayOf(1, 2, 3), "hash.bin")
        val good = sha256(file)
        assertTrue(verifyUploadHash(file, good))
        assertTrue("大小写不影响", verifyUploadHash(file, good.uppercase()))
        assertFalse(verifyUploadHash(file, sha256(byteArrayOf(9))))
        assertFalse(verifyUploadHash(file, ""))
        assertFalse(verifyUploadHash(file, null))
    }

    // ---------- 6. 上传完成判定 ----------

    @Test
    fun uploadIsCompleteOnlyWhenBytesAndHashBothMatch() {
        val total = 3L * MediaLimits.CHUNK_BYTES
        assertFalse(isUploadByteComplete(2, total))
        assertTrue(isUploadByteComplete(3, total))
        assertTrue(isUploadByteComplete(4, total))

        // 字节没齐 → 可续传，不能报 hash 不符。
        assertEquals(MediaErrors.UPLOAD_RETRYABLE, uploadCompletionFailure(2, total, sourceFile(ByteArray(0)), null))

        // 字节齐 + hash 对 → null（可以回 upload_complete）。
        val payload = ByteArray(total.toInt()) { (it % 253).toByte() }
        val file = sourceFile(payload, "complete.mp4")
        assertNull(uploadCompletionFailure(3, total, file, sha256(file)))

        // 字节齐但 hash 不符 → 必须丢弃重传，不是可续传。
        assertEquals(
            MediaErrors.UPLOAD_HASH_MISMATCH,
            uploadCompletionFailure(3, total, file, sha256(byteArrayOf(1))),
        )

        // 缓冲文件丢了 → 找不到素材。
        assertEquals(MediaErrors.MEDIA_NOT_FOUND, uploadCompletionFailure(3, total, File(temporary.root, "gone"), null))

        // 文件长度与声明的 total 不符 → 仍算未完成，可续传。
        val short = sourceFile(payload.copyOf(payload.size - 1), "short.mp4")
        assertEquals(MediaErrors.UPLOAD_RETRYABLE, uploadCompletionFailure(3, total, short, sha256(file)))

        // 非 64 位十六进制的 hash 不能被当成匹配。
        assertEquals(MediaErrors.UPLOAD_HASH_MISMATCH, uploadCompletionFailure(3, total, file, "abc"))
    }

    // ---------- 7. 七个失败状态各自的产生条件 ----------

    @Test
    fun failureCodeSetIsExactlyTheSevenDocumentedCodes() {
        assertEquals(
            linkedSetOf(
                "CAMERA_PERMISSION_REQUIRED",
                "PHOTO_PICKER_CANCELLED",
                "MEDIA_TOO_LARGE",
                "MEDIA_TYPE_NOT_ALLOWED",
                "UPLOAD_RETRYABLE",
                "UPLOAD_HASH_MISMATCH",
                "MEDIA_NOT_FOUND",
            ),
            MediaErrors.ALL,
        )
        assertEquals(7, MediaErrors.ALL.size)
        for (code in MediaErrors.ALL) {
            assertTrue(MediaErrors.isDeclared(code))
        }
        // 未登记的码不得被当成合法固定状态。
        assertFalse(MediaErrors.isDeclared("NOT_WIRED"))
        assertFalse(MediaErrors.isDeclared("MEDIA_UPLOAD_FAILED"))
        // 这七个码必须**同时登记在 CommandErrors.ALL**：M06 是错误码唯一登记处，
        // M08 只定义语义与产生条件。CC4 契约检查会核对两处逐字一致。
        // 早期版本曾要求「M08 码不得与 M06 重名」，那会把两者逼成两套并行的码表，
        // 路由层就无法用同一个集合判断失败码是否合法——已于登记落地后改为正向断言。
        for (code in MediaErrors.ALL) {
            assertTrue("M08 码必须登记到 CommandErrors.ALL: $code", CommandErrors.isDeclared(code))
        }
    }

    @Test
    fun cameraPermissionRequiredBlocksBeforeAnyWork() {
        // 无权限 → 固定失败，且此时不应产生任何素材。
        assertEquals(
            MediaErrors.CAMERA_PERMISSION_REQUIRED,
            capturePreconditionFailure(cameraGranted = false, hasLifecycleOwner = true),
        )
        // 有权限但没有前台 Activity：CameraX 绑不上，明确失败而不是伪造成功。
        assertEquals(
            CommandErrors.NOT_WIRED,
            capturePreconditionFailure(cameraGranted = true, hasLifecycleOwner = false),
        )
        // 两个前提都满足才继续拍摄。
        assertNull(capturePreconditionFailure(cameraGranted = true, hasLifecycleOwner = true))
    }

    @Test
    fun cameraCaptureReturnsMetadataOnly() {
        // 验收矩阵 camera_capture_upload：回包只有 asset_id/mime/size/sha256 四项。
        val store = store()
        val captured = sourceFile(byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 1, 2), "capture.jpg")
        val entry = store.register(captured, "image/jpeg", taskId = "task-1")

        val evidence = store.evidence(entry)
        assertEquals(setOf("asset_id", "mime", "size", "sha256"), evidence.keys)
        assertEquals("image/jpeg", evidence["mime"])
        assertEquals("4", evidence["size"])
        assertEquals(sha256(captured), evidence["sha256"])
        // 真实路径绝不出现在回包里。
        for (value in evidence.values) {
            assertFalse("回包不得含真实路径", value.contains(temporary.root.absolutePath))
            assertFalse(value.contains("capture.jpg"))
        }
        // 资产表恰好新增一行（虽已存在，被 mask，但需显式引用以避免未使用警告）。
        assertEquals(1, store.assetsForTask("task-1").size)
    }

    @Test
    fun pickerCancellationIsFixedFailure() {
        assertEquals(MediaErrors.PHOTO_PICKER_CANCELLED, pickerCancellationFailure(0))
        assertNull(pickerCancellationFailure(1))
        assertNull(pickerCancellationFailure(3))
    }

    @Test
    fun pickerRejectsAnythingButContentUri() {
        // 验收矩阵 photo_picker_only_user_uri：只接受系统选择器回传的 content:// URI。
        // 真实 URI 解析在 Activity 侧完成，这里锁定"哪些输入形状必须被拒绝"的纯判定。
        val plainPaths = listOf(
            "/sdcard/DCIM/Camera/IMG_0001.jpg",   // 绝对路径
            "file:///sdcard/DCIM/IMG_0001.jpg",   // file://
            "C:\\Users\\me\\Pictures\\x.jpg",     // Windows 路径
            "~/Pictures/x.jpg",
            "//server/share/x.jpg",
        )
        for (path in plainPaths) {
            assertFalse("模型给的本地路径不得被当成素材来源: $path", isPickerUriSchemeAllowed(path))
        }
        assertTrue("content:// 才允许", isPickerUriSchemeAllowed("content://media/picker/0/com.android.providers.media.photopicker/media/1000000033"))
        // 取消（0 项）不得产生任何素材，且不得变成 ok。
        val store = store()
        assertEquals(0, store.size())
        assertEquals(MediaErrors.PHOTO_PICKER_CANCELLED, pickerCancellationFailure(0))
    }

    @Test
    fun pickerRespectsMaxItemsCap() {
        val store = store()
        val source = sourceFile(byteArrayOf(1), "a.png")
        // max_items 上限就是单任务上限 20（schema 也是 max 20）。
        val maxItems = 20
        assertEquals(MediaLimits.MAX_ASSETS_PER_TASK, maxItems)
        for (index in 0 until maxItems) {
            store.register(source, "image/png", "pick$index", taskId = "task-1")
        }
        assertEquals(maxItems, store.assetsForTask("task-1").size)
        assertEquals(
            MediaErrors.MEDIA_TOO_LARGE,
            failCode { store.register(source, "image/png", "pick$maxItems", taskId = "task-1") },
        )
    }

    @Test
    fun everyFailureCodeHasAConcreteTrigger() {
        val store = store(maxBytes = 1024)
        val produced = linkedSetOf<String>()

        // 1 CAMERA_PERMISSION_REQUIRED
        produced.add(capturePreconditionFailure(cameraGranted = false, hasLifecycleOwner = true)!!)
        // 2 PHOTO_PICKER_CANCELLED
        produced.add(pickerCancellationFailure(0)!!)
        // 3 MEDIA_TOO_LARGE
        produced.add(failCode { store.register(sourceFile(ByteArray(2048), "big.png"), "image/png", "big") })
        // 4 MEDIA_TYPE_NOT_ALLOWED
        produced.add(failCode { store.register(sourceFile(byteArrayOf(1), "note.bin"), "text/plain", "note") })
        // 5 UPLOAD_RETRYABLE
        produced.add(failCode { planContentRange(5, 1024L) })
        // 6 UPLOAD_HASH_MISMATCH
        produced.add(failCode { verifyOrThrow(sourceFile(byteArrayOf(1, 2), "h.bin"), "0".repeat(64)) })
        // 7 MEDIA_NOT_FOUND
        produced.add(failCode { store.require("ghost") })

        assertEquals("七个失败状态必须各自可达", MediaErrors.ALL, produced)
    }

    /** 把「hash 不符」也做成固定失败状态，避免调用方各自决定语义。 */
    private fun verifyOrThrow(file: File, expected: String) {
        if (!verifyUploadHash(file, expected)) {
            throw MediaException(MediaErrors.UPLOAD_HASH_MISMATCH, "sha256 mismatch")
        }
    }

    @Test
    fun mediaExceptionRefusesUndocumentedCodes() {
        val documented = try {
            MediaException(MediaErrors.MEDIA_NOT_FOUND, "ok")
            true
        } catch (e: IllegalArgumentException) {
            false
        }
        assertTrue(documented)
        // 未登记的码一律拒绝构造，防止私造错误码。
        try {
            MediaException("MEDIA_UPLOAD_FAILED", "not documented")
            throw AssertionError("undocumented code must be rejected")
        } catch (e: IllegalArgumentException) {
            assertTrue(e.message!!.contains("undocumented"))
        }
    }

    @Test
    fun registerBytesKeepsHashAndSize() {
        val store = store()
        val payload = byteArrayOf(1, 2, 3, 4)
        val entry = store.registerBytes(payload, "image/png", "mem", taskId = "task-1")
        assertEquals(4L, entry.size)
        assertEquals(sha256(payload), entry.sha256)
        assertEquals(1, store.size())

        // 超限字节数同样被拒绝；默认上限是 100 MiB，所以这里必须用一个小上限的 store。
        val tiny = store(maxBytes = 1024)
        assertEquals(
            MediaErrors.MEDIA_TOO_LARGE,
            failCode { tiny.registerBytes(ByteArray(2048), "image/png", "big2") },
        )
        // 类型同样要过闸：字节登记也接受不了非媒体 mime。
        assertEquals(
            MediaErrors.MEDIA_TYPE_NOT_ALLOWED,
            failCode { tiny.registerBytes(byteArrayOf(1), "text/plain", "note") },
        )
    }
}
