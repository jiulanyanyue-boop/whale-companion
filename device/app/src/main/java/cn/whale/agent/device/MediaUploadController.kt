package cn.whale.agent.device

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.util.concurrent.TimeUnit

/**
 * M08 分块上传控制器（`device.media.upload` 的实现层）。
 *
 * 规范来源：`docs/modules/M08-media-development.md`「上传协议」。
 *
 * 固定请求形状（不可改）：
 *
 * ```text
 * POST /v1/devices/{device_id}/assets/{asset_id}/chunks/{index}
 * Content-Range: bytes start-end/total
 * X-Asset-Sha256: sha256
 * ```
 *
 * 固定约束：
 * - 分块固定 1 MiB（[MediaLimits.CHUNK_BYTES]），偏移与 `Content-Range` 一律由
 *   [planContentRange] 计算，本类不自己拼数字；
 * - 媒体**只走 HTTPS**，非 https 端点直接 [MediaErrors.UPLOAD_RETRYABLE]；
 * - **绝不**用 WebSocket 传大文件：本类只依赖 OkHttp 的同步 HTTP 调用；
 * - 分块级重试（幂等重发同一 index + 同一 sha256）不会重复触发 OneBot 发送：
 *   本类只返回元数据，**从不**调用任何消息发送路径；
 * - 「上传完成」只认**服务端**回包里的 `upload_complete=true`。本地 hash 自检
 *   只用于发现本地文件漂移，绝不能自己给自己盖章；
 * - 任一失败都映射到七个固定状态之一。
 *
 * 纯 JVM 逻辑（分块计划、`Content-Range`、sha256、完成判定）都在
 * `AssetStore.kt` 的顶层函数里，已由 `AssetStoreTest` 真实覆盖；
 * 本类只负责网络与状态机编排。
 */
class MediaUploadController(
    private val assets: AssetStore,
    private val client: OkHttpClient = defaultClient(),
) {
    /** 上传结果：成功必须是服务端确认的 `upload_complete`。 */
    sealed interface UploadOutcome {
        /** 服务端已确认整份素材上传完成；[evidence] 只含 asset_id/mime/size/sha256。 */
        data class Uploaded(
            val entry: AssetStore.AssetEntry,
            val chunksSent: Int,
            val evidence: Map<String, String>,
        ) : UploadOutcome

        data class Failed(
            val code: String,
            val message: String,
            val retryable: Boolean,
        ) : UploadOutcome
    }

    /** 单块发送结果（内部用，不与对外结果混淆）。 */
    private sealed interface ChunkResult {
        /** 块已被接收，但整份素材尚未完成。 */
        object Accepted : ChunkResult

        /** 该块让整份素材完成，服务端已确认。 */
        data class Completed(val evidence: Map<String, String>) : ChunkResult

        data class Rejected(val failure: UploadOutcome.Failed) : ChunkResult
    }

    /**
     * 上传一份已登记素材。
     *
     * @param deviceId 已配对的设备 ID（上传路径的第一段，必须是安全 token）。
     * @param endpoint 主机 HTTPS 基址，例如 `https://host.example`。
     * @param maxAttemptsPerChunk 每块的最大尝试次数；重试保持同一 index 与同一 sha256，
     *   因此服务端可以按 offset 幂等接收，不会产生重复素材。
     */
    fun upload(
        assetId: String,
        deviceId: String,
        endpoint: String,
        maxAttemptsPerChunk: Int = DEFAULT_ATTEMPTS_PER_CHUNK,
    ): UploadOutcome {
        val entry = try {
            assets.require(assetId)
        } catch (e: MediaException) {
            return failed(e)
        }
        val file = try {
            assets.fileOf(entry.assetId)
        } catch (e: MediaException) {
            return failed(e)
        }

        // 路径与端点先校验，避免为一次注定失败的请求去读 100 MiB。
        if (!isValidDeviceId(deviceId)) {
            return UploadOutcome.Failed(MediaErrors.MEDIA_NOT_FOUND, "invalid device id", retryable = false)
        }
        if (!endpoint.startsWith("https://")) {
            return UploadOutcome.Failed(
                MediaErrors.UPLOAD_RETRYABLE,
                "upload endpoint must be an https URL",
                retryable = true,
            )
        }
        if (!isAllowedMediaSize(entry.size)) {
            return UploadOutcome.Failed(MediaErrors.MEDIA_TOO_LARGE, "asset exceeds the size limit", false)
        }

        val total = entry.size
        val totalChunks = chunkCount(total)

        // 本地漂移自检：登记时的 sha256 必须与磁盘现状一致，否则这份素材已被破坏。
        // 注意这只用于**本地**校验，不能用来宣告服务端上传完成。
        val localSha = sha256(file)
        if (localSha != entry.sha256) {
            return UploadOutcome.Failed(
                MediaErrors.UPLOAD_HASH_MISMATCH,
                "local asset hash drifted from the registered digest",
                retryable = false,
            )
        }

        var sent = 0
        var serverComplete = false
        for (index in 0 until totalChunks) {
            var lastFailure: UploadOutcome.Failed? = null
            var done = false
            var attempt = 1
            val attempts = maxAttemptsPerChunk.coerceAtLeast(1)
            while (attempt <= attempts && !done) {
                val chunk = try {
                    readChunk(file, index, total)
                } catch (e: MediaException) {
                    return failed(e)
                }
                val headers = try {
                    chunkHeaders(entry.assetId, index, total, localSha)
                } catch (e: MediaException) {
                    return failed(e)
                }

                when (val result = sendChunk(deviceId, entry.assetId, index, endpoint, chunk, headers)) {
                    is ChunkResult.Accepted -> done = true
                    is ChunkResult.Completed -> {
                        done = true
                        serverComplete = true
                    }
                    is ChunkResult.Rejected -> {
                        lastFailure = result.failure
                        // 类型/大小/hash 不符属于确定性失败，重发只会重复炸。
                        if (!result.failure.retryable) return result.failure
                        attempt++
                        if (attempt <= attempts) sleepBeforeRetry(attempt)
                    }
                }
            }
            if (!done) {
                return lastFailure ?: UploadOutcome.Failed(
                    MediaErrors.UPLOAD_RETRYABLE,
                    "chunk $index was never accepted by the host",
                    retryable = true,
                )
            }
            sent++
            if (serverComplete) break
        }

        // 全部块发完也不等于完成：必须由服务端确认。没有确认就是可续传，不是 ok。
        if (!serverComplete) {
            return UploadOutcome.Failed(
                MediaErrors.UPLOAD_RETRYABLE,
                "host did not confirm upload_complete after $sent/$totalChunks chunks",
                retryable = true,
            )
        }
        return UploadOutcome.Uploaded(
            entry = entry,
            chunksSent = sent,
            evidence = assets.evidence(entry) + ("upload_complete" to "true"),
        )
    }

    /**
     * 发送单块并按服务端回包分类。
     *
     * 服务端回包约定（与 host `AssetUploads.status` 一致）：
     * - 2xx 且 `"upload_complete":true`：整份素材到位，**只有**这时才算完成；
     * - 2xx 且 `"upload_complete":false`：该块被接收，继续下一块；
     * - 409 / 412：offset 或 hash 冲突 → [MediaErrors.UPLOAD_RETRYABLE]；
     * - 404 / 413 / 415 / 422：确定性失败，不重试；
     * - 其他 5xx 与传输异常：可重试。
     */
    private fun sendChunk(
        deviceId: String,
        assetId: String,
        index: Int,
        endpoint: String,
        chunk: ByteArray,
        headers: Map<String, String>,
    ): ChunkResult {
        val url = try {
            assetChunkUrl(endpoint, deviceId, assetId, index)
        } catch (e: MediaException) {
            return ChunkResult.Rejected(failed(e))
        }
        val request = Request.Builder()
            .url(url)
            .post(chunk.toRequestBody(BINARY))
            .header("Content-Range", headers.getValue("Content-Range"))
            .header("X-Asset-Sha256", headers.getValue("X-Asset-Sha256"))
            .header("Content-Type", headers.getValue("Content-Type"))
            .build()

        return try {
            client.newCall(request).execute().use { response ->
                classify(response, assetId, index)
            }
        } catch (t: Throwable) {
            // 网络层异常一律可重试，且不暴露堆栈给用户。
            ChunkResult.Rejected(
                UploadOutcome.Failed(
                    MediaErrors.UPLOAD_RETRYABLE,
                    "chunk $index transport failure: ${t.javaClass.simpleName}",
                    retryable = true,
                ),
            )
        }
    }

    private fun classify(response: Response, assetId: String, index: Int): ChunkResult {
        val body = runCatching { response.body?.string().orEmpty() }.getOrDefault("")
        val confirmed = hasUploadCompleteFlag(body)

        if (response.isSuccessful) {
            if (!confirmed) return ChunkResult.Accepted
            // 服务端说完成了，但它认的素材我们必须也有；否则是服务端状态错乱。
            val entry = assets.entry(assetId)
                ?: return ChunkResult.Rejected(
                    UploadOutcome.Failed(
                        MediaErrors.MEDIA_NOT_FOUND,
                        "host confirmed completion for an asset this device does not hold",
                        retryable = false,
                    ),
                )
            return ChunkResult.Completed(assets.evidence(entry))
        }

        return ChunkResult.Rejected(
            when (response.code) {
                409, 412 -> UploadOutcome.Failed(
                    MediaErrors.UPLOAD_RETRYABLE,
                    "chunk $index rejected as offset/hash conflict",
                    retryable = true,
                )

                404 -> UploadOutcome.Failed(
                    MediaErrors.MEDIA_NOT_FOUND,
                    "asset is unknown to the host",
                    retryable = false,
                )

                413 -> UploadOutcome.Failed(
                    MediaErrors.MEDIA_TOO_LARGE,
                    "host rejected the asset size",
                    retryable = false,
                )

                415 -> UploadOutcome.Failed(
                    MediaErrors.MEDIA_TYPE_NOT_ALLOWED,
                    "host rejected the media type",
                    retryable = false,
                )

                422 -> UploadOutcome.Failed(
                    MediaErrors.UPLOAD_HASH_MISMATCH,
                    "host rejected the asset sha256",
                    retryable = false,
                )

                else -> UploadOutcome.Failed(
                    MediaErrors.UPLOAD_RETRYABLE,
                    "host returned HTTP ${response.code} for chunk $index",
                    retryable = true,
                )
            },
        )
    }

    /**
     * 解析 `"upload_complete": true`；容忍空格与顺序差异，但**不**接受 `false`。
     *
     * 只看这个布尔字段，不猜别的字段名，避免把 `"received_chunks":20` 误当完成信号。
     */
    private fun hasUploadCompleteFlag(body: String): Boolean {
        val match = UPLOAD_COMPLETE_REGEX.find(body) ?: return false
        return match.groupValues[1].equals("true", ignoreCase = true)
    }

    private fun failed(e: MediaException): UploadOutcome.Failed =
        UploadOutcome.Failed(e.code, e.reason, retryable = e.code == MediaErrors.UPLOAD_RETRYABLE)

    private fun sleepBeforeRetry(attempt: Int) {
        // 指数退避但封顶，且不长时间占住串行闸门。
        val delay = minOf(RETRY_BASE_MS * attempt.toLong(), RETRY_MAX_MS)
        Thread.sleep(delay)
    }

    companion object {
        private const val DEFAULT_ATTEMPTS_PER_CHUNK = 3
        private const val RETRY_BASE_MS = 20L
        private const val RETRY_MAX_MS = 200L

        private val BINARY = "application/octet-stream".toMediaType()

        /** `"upload_complete"` 后面跟随的布尔字面量。 */
        private val UPLOAD_COMPLETE_REGEX = Regex("\"upload_complete\"\\s*:\\s*(true|false)")

        /**
         * 只做 HTTPS，超时短，避免占用串行闸门。
         *
         * 关掉连接失败重试：重试语义由 [upload] 的分块循环显式控制，
         * 否则「一次上传对应一次发送」的对应关系会被 OkHttp 的透明重试打乱。
         */
        fun defaultClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(5, TimeUnit.SECONDS)
            .readTimeout(10, TimeUnit.SECONDS)
            .writeTimeout(10, TimeUnit.SECONDS)
            .retryOnConnectionFailure(false)
            .build()
    }
}
