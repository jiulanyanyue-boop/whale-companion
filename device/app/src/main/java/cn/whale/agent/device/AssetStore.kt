package cn.whale.agent.device

import java.io.File
import java.io.RandomAccessFile
import java.security.MessageDigest
import java.util.UUID
import kotlin.math.max
import kotlin.math.min

/**
 * M08 相机、相册与媒体的固定失败状态（恰好七个）。
 *
 * 规范来源：`docs/modules/M08-media-development.md`「失败状态」。
 *
 * 这七个码目前**尚未**登记进 [CommandErrors.ALL] —— 那是 M06 的唯一错误码登记处，
 * 由 M06 负责人维护，M08 不得私自写入。因此 M08 在自己的模块内提供本常量对象，
 * 供 Controller 与固定测试使用；登记顺序固定为：
 * 1. 先补 `docs/modules/M06-command-runtime-development.md` 的「错误码固定集合」；
 * 2. 再进 `CommandErrors.ALL`；
 * 3. 最后才允许把下面的引用替换成 `CommandErrors` 的常量。
 *
 * 在此之前，M08 之外的任何地方都不得再定义同名常量，也不得散落字符串字面量。
 */
object MediaErrors {
    /** 相机权限未授予；拍摄前被拒，不启动预览、不产生文件。 */
    const val CAMERA_PERMISSION_REQUIRED = "CAMERA_PERMISSION_REQUIRED"

    /** 用户在 Photo Picker / ACTION_OPEN_DOCUMENT 界面取消了选择。 */
    const val PHOTO_PICKER_CANCELLED = "PHOTO_PICKER_CANCELLED"

    /** 单文件超过 100 MiB，或单任务素材数超过 20。 */
    const val MEDIA_TOO_LARGE = "MEDIA_TOO_LARGE"

    /** mime 不在 `image/…`、`audio/…`、`video/…` 三个前缀之内，或无法判定。 */
    const val MEDIA_TYPE_NOT_ALLOWED = "MEDIA_TYPE_NOT_ALLOWED"

    /** 分块偏移/长度/顺序/内容不一致，或上传尚未完成（可续传）。 */
    const val UPLOAD_RETRYABLE = "UPLOAD_RETRYABLE"

    /** 全部字节到齐，但总 sha256 与 `X-Asset-Sha256` 不符。 */
    const val UPLOAD_HASH_MISMATCH = "UPLOAD_HASH_MISMATCH"

    /** asset_id 非法或不存在（含已被 24 小时清理的临时素材）。 */
    const val MEDIA_NOT_FOUND = "MEDIA_NOT_FOUND"

    /** 七个固定失败状态；供契约测试与 M06 登记核对使用。 */
    val ALL: Set<String> = linkedSetOf(
        CAMERA_PERMISSION_REQUIRED,
        PHOTO_PICKER_CANCELLED,
        MEDIA_TOO_LARGE,
        MEDIA_TYPE_NOT_ALLOWED,
        UPLOAD_RETRYABLE,
        UPLOAD_HASH_MISMATCH,
        MEDIA_NOT_FOUND,
    )

    fun isDeclared(code: String): Boolean = code in ALL
}

/**
 * M08 固定数值上限。host 侧 `astrbot_whale_device/media.py` 必须与本对象逐字一致：
 * 单文件 100 MiB、单任务 20 个素材、临时素材 24 小时、分块固定 1 MiB。
 */
object MediaLimits {
    const val MAX_MEDIA_BYTES: Long = 100L * 1024 * 1024
    const val MAX_ASSETS_PER_TASK: Int = 20
    const val TEMP_TTL_MS: Long = 24L * 60 * 60 * 1000
    const val CHUNK_BYTES: Int = 1024 * 1024
}

/** 媒体类别：只用于挑选系统选择器与展示，不参与上传协议。 */
enum class MediaKind {
    IMAGE,
    VIDEO,
    AUDIO,
    ANY,
    ;

    companion object {
        /** `device.media.pick` 的 payload 枚举值（与 schema 逐字一致）。 */
        fun parse(raw: String): MediaKind? = when (raw) {
            "image" -> IMAGE
            "video" -> VIDEO
            "audio" -> AUDIO
            "any" -> ANY
            else -> null
        }

        fun parseOrAny(raw: String): MediaKind = parse(raw) ?: ANY
    }

    /** 该类别允许的 mime 前缀；[ANY] 时三个前缀都允许。 */
    fun allowsMime(mime: String): Boolean = when (this) {
        IMAGE -> mime.startsWith("image/")
        VIDEO -> mime.startsWith("video/")
        AUDIO -> mime.startsWith("audio/")
        ANY -> isAllowedMediaType(mime)
    }

    /** 该类别对应的 MIME 过滤串；[ANY] 时放开为全部媒体。 */
    val pickerMimeType: String
        get() = when (this) {
            IMAGE -> "image/*"
            VIDEO -> "video/*"
            AUDIO -> "audio/*"
            ANY -> "*/*"
        }
}

/** 带 M08 固定失败码的媒体异常；[code] 必须取自 [MediaErrors]。 */
class MediaException(val code: String, val reason: String) : IllegalArgumentException("$code: $reason") {
    init {
        require(code in MediaErrors.ALL) { "undocumented M08 failure code: $code" }
    }
}

/**
 * 设备端素材表 + 全部纯 JVM 媒体规则（M08）。
 *
 * 本类不依赖任何 Android 类型，因此可以用 JUnit4 在主机上真实运行：
 * 素材 ID 正则、媒体类型、大小上限、单任务数量上限、24 小时清理、
 * 分块偏移与 `Content-Range`、sha256、上传完成判定。
 *
 * 安全约束（`docs/modules/M08-media-development.md`）：
 * - 素材 ID 只允许 `[A-Za-z0-9._-]{1,160}`，并且额外拒绝 `..`、斜杠、反斜杠与用户路径；
 * - 文件只落在受控 `root` 之内，ID 到路径的映射必须做越界复查；
 * - 媒体类型只允许 image、audio、video 三类前缀；
 * - [AssetStore.evidence] 只给 `asset_id`、`mime`、`size`、`sha256`，**绝不**把真实路径发给模型。
 */
class AssetStore(
    private val root: File,
    private val maxBytes: Long = MediaLimits.MAX_MEDIA_BYTES,
    private val maxAssetsPerTask: Int = MediaLimits.MAX_ASSETS_PER_TASK,
    private val ttlMs: Long = MediaLimits.TEMP_TTL_MS,
) {
    private val entries = LinkedHashMap<String, AssetEntry>()

    init {
        if (!root.isDirectory && !root.mkdirs()) {
            throw MediaException(MediaErrors.MEDIA_NOT_FOUND, "asset store root is not usable: ${root.name}")
        }
    }

    /** 已登记素材的元数据（不含真实路径）。 */
    data class AssetEntry(
        val assetId: String,
        val mime: String,
        val size: Long,
        val sha256: String,
        val taskId: String,
        val createdAtMs: Long,
    )

    // ---- 登记 ----------------------------------------------------------

    /**
     * 登记一个素材：ID → 存在性 → 大小 → 类型 → 单任务上限，然后复制进受控 root。
     *
     * @param source 调用方拿到的临时文件（CameraX 的 cache 文件或 Picker 已复制出的缓存）。
     *   它只在本方法内部使用，不会出现在任何回包。
     * @param mime 已由 ContentResolver 探测出的 mime；必须落在三个允许前缀内。
     * @param assetId 为 null 时自动生成 32 位十六进制随机 ID。
     * @return 只含 `asset_id`、`mime`、`size`、`sha256` 的元数据。
     */
    @Synchronized
    fun register(
        source: File,
        mime: String,
        assetId: String? = null,
        taskId: String? = null,
        nowMs: Long = System.currentTimeMillis(),
    ): AssetEntry {
        val ident = requireValidAssetId(assetId ?: UUID.randomUUID().toString().replace("-", ""))
        if (!source.isFile) {
            throw MediaException(MediaErrors.MEDIA_NOT_FOUND, "source media file is missing")
        }
        val size = source.length()
        requireAllowedSize(size)
        val type = requireAllowedMediaType(mime)
        val task = taskId.orEmpty()
        if (task.isNotEmpty() && entries[ident] == null && countForTask(task) >= maxAssetsPerTask) {
            throw MediaException(
                MediaErrors.MEDIA_TOO_LARGE,
                "task already holds $maxAssetsPerTask assets",
            )
        }

        val target = resolveWithinRoot(ident)
        source.copyTo(target, overwrite = true)
        val digest = sha256(target)
        val entry = AssetEntry(
            assetId = ident,
            mime = type,
            size = target.length(),
            sha256 = digest,
            taskId = task,
            createdAtMs = nowMs,
        )
        entries[ident] = entry
        return entry
    }

    /** 从内存字节登记（分块上传缓冲完成时使用），不写中间临时文件。 */
    @Synchronized
    fun registerBytes(
        data: ByteArray,
        mime: String,
        assetId: String? = null,
        taskId: String? = null,
        nowMs: Long = System.currentTimeMillis(),
    ): AssetEntry {
        val ident = requireValidAssetId(assetId ?: UUID.randomUUID().toString().replace("-", ""))
        requireAllowedSize(data.size.toLong())
        val type = requireAllowedMediaType(mime)
        val task = taskId.orEmpty()
        if (task.isNotEmpty() && entries[ident] == null && countForTask(task) >= maxAssetsPerTask) {
            throw MediaException(
                MediaErrors.MEDIA_TOO_LARGE,
                "task already holds $maxAssetsPerTask assets",
            )
        }
        val target = resolveWithinRoot(ident)
        target.writeBytes(data)
        val entry = AssetEntry(
            assetId = ident,
            mime = type,
            size = data.size.toLong(),
            sha256 = sha256(data),
            taskId = task,
            createdAtMs = nowMs,
        )
        entries[ident] = entry
        return entry
    }

    // ---- 读取 ----------------------------------------------------------

    /** 素材元数据；非法或未登记 ID 返回 null（不抛异常，便于路由层统一转失败状态）。 */
    @Synchronized
    fun entry(assetId: String?): AssetEntry? {
        if (!isValidAssetId(assetId)) return null
        return entries[assetId!!]
    }

    /** 必须有该素材，否则 `MEDIA_NOT_FOUND`。 */
    @Synchronized
    fun require(assetId: String): AssetEntry {
        val ident = requireValidAssetId(assetId)
        return entries[ident]
            ?: throw MediaException(MediaErrors.MEDIA_NOT_FOUND, "asset is not registered")
    }

    /** 协议与 OneBot 只用这四项元数据，**绝不带真实路径**。 */
    fun evidence(entry: AssetEntry): Map<String, String> = mapOf(
        "asset_id" to entry.assetId,
        "mime" to entry.mime,
        "size" to entry.size.toString(),
        "sha256" to entry.sha256,
    )

    /** 受控读取：只有已登记素材才给出 root 内路径；调用方不得外发。 */
    @Synchronized
    fun fileOf(assetId: String): File {
        val entry = require(assetId)
        val file = resolveWithinRoot(entry.assetId)
        if (!file.isFile) {
            throw MediaException(MediaErrors.MEDIA_NOT_FOUND, "asset file is missing")
        }
        return file
    }

    @Synchronized
    fun countForTask(taskId: String): Int {
        if (taskId.isEmpty()) return 0
        return entries.values.count { it.taskId == taskId }
    }

    @Synchronized
    fun assetsForTask(taskId: String): List<AssetEntry> {
        if (taskId.isEmpty()) return emptyList()
        return entries.values.filter { it.taskId == taskId }.sortedBy { it.createdAtMs }
    }

    // ---- 删除与清理 -----------------------------------------------------

    @Synchronized
    fun remove(assetId: String?): Boolean {
        if (!isValidAssetId(assetId)) return false
        val removed = entries.remove(assetId) ?: return false
        val file = resolveWithinRoot(removed.assetId)
        if (file.isFile) file.delete()
        return true
    }

    /** 临时素材默认 24 小时后清理；返回被删的 ID 列表。 */
    @Synchronized
    fun sweepExpired(nowMs: Long = System.currentTimeMillis()): List<String> {
        val cutoff = nowMs - ttlMs
        val expired = entries.values.filter { it.createdAtMs <= cutoff }.map { it.assetId }
        expired.forEach { remove(it) }
        return expired
    }

    @Synchronized
    fun size(): Int = entries.size

    // ---- 内部 ----------------------------------------------------------

    private fun resolveWithinRoot(assetId: String): File {
        val candidate = File(root, assetId)
        val canonicalRoot = root.canonicalFile
        val canonicalTarget = candidate.canonicalFile
        val parent = canonicalTarget.parentFile
        if (parent == null || parent.path != canonicalRoot.path) {
            throw MediaException(MediaErrors.MEDIA_NOT_FOUND, "asset id escapes the store root")
        }
        return canonicalTarget
    }

    private fun requireValidAssetId(assetId: String): String =
        if (isValidAssetId(assetId)) assetId else throw MediaException(
            MediaErrors.MEDIA_NOT_FOUND,
            "invalid asset id",
        )

    private fun requireAllowedSize(size: Long): Long {
        if (!isAllowedMediaSize(size, maxBytes)) {
            throw MediaException(
                MediaErrors.MEDIA_TOO_LARGE,
                "media size must be within 1..$maxBytes bytes",
            )
        }
        return size
    }

    private fun requireAllowedMediaType(mime: String): String =
        if (isAllowedMediaType(mime)) mime else throw MediaException(
            MediaErrors.MEDIA_TYPE_NOT_ALLOWED,
            "media type is not allowed: $mime",
        )

    companion object {
        /** 素材 ID 只允许 `[A-Za-z0-9._-]{1,160}`。 */
        private val ASSET_ID_REGEX = Regex("[A-Za-z0-9._-]{1,160}")

        /** 设备 ID 只允许同样的安全 token 集合，防止上传路径注入。 */
        private val DEVICE_ID_REGEX = Regex("[A-Za-z0-9._-]{1,128}")

        /** 允许的媒体类型前缀（只允许 image、audio、video 三类）。 */
        val ALLOWED_MEDIA_PREFIXES: List<String> = listOf("image/", "audio/", "video/")
    }
}

// ---------------------------------------------------------------------------
// 纯规则函数：素材 ID / 设备 ID / 媒体类型 / 大小
// ---------------------------------------------------------------------------

/** 素材 ID 只允许 `[A-Za-z0-9._-]{1,160}`，并禁止 `..`、斜杠、反斜杠与用户路径。 */
fun isValidAssetId(assetId: String?): Boolean {
    if (assetId == null) return false
    if (assetId.length !in 1..160) return false
    if (assetId.contains('/') || assetId.contains('\\')) return false
    if (assetId.contains("..")) return false
    return Regex("[A-Za-z0-9._-]{1,160}").matches(assetId)
}

/** 上传路径里的 `device_id` 同样只能是一段安全 token。 */
fun isValidDeviceId(deviceId: String?): Boolean {
    if (deviceId == null) return false
    if (deviceId.length !in 1..128) return false
    if (deviceId.contains('/') || deviceId.contains('\\')) return false
    if (deviceId.contains("..")) return false
    return Regex("[A-Za-z0-9._-]{1,128}").matches(deviceId)
}

/** 媒体类型只允许 image、audio、video 三类前缀，且子类型必须非空。 */
fun isAllowedMediaType(mime: String?): Boolean {
    if (mime == null) return false
    val prefix = AssetStore.ALLOWED_MEDIA_PREFIXES.firstOrNull { mime.startsWith(it) } ?: return false
    val subtype = mime.substring(prefix.length)
    if (subtype.isBlank()) return false
    if (subtype.contains('/') || subtype.contains(' ') || subtype.contains(';')) return false
    return true
}

/** 单文件大小必须落在 `1..maxBytes`；默认上限 100 MiB。 */
fun isAllowedMediaSize(bytes: Long, maxBytes: Long = MediaLimits.MAX_MEDIA_BYTES): Boolean =
    bytes in 1..maxBytes

// ---------------------------------------------------------------------------
// 纯规则：分块偏移与 Content-Range（固定 1 MiB）
// ---------------------------------------------------------------------------

/** 一段分块在整份素材中的位置；[total] 是整份素材的字节数。 */
data class ContentRange(val start: Long, val end: Long, val total: Long) {
    val length: Long get() = end - start + 1

    /** 固定请求头形状：`Content-Range: bytes start-end/total`。 */
    fun header(): String = "bytes $start-$end/$total"

    fun isLast(): Boolean = end == total - 1

    /** 这段所属的分块序号；必须与生成该段的 [chunkBytes] 一致。 */
    fun chunkIndex(chunkBytes: Int = MediaLimits.CHUNK_BYTES): Int =
        (start / max(1, chunkBytes).toLong()).toInt()

    companion object {
        /** 严格解析 `bytes start-end/total`；任何偏差都视为可重试。 */
        fun parse(raw: String?, chunkBytes: Int = MediaLimits.CHUNK_BYTES): ContentRange {
            val text = raw?.trim()
            if (text == null || text.isEmpty()) {
                throw MediaException(MediaErrors.UPLOAD_RETRYABLE, "Content-Range header is missing")
            }
            val match = CONTENT_RANGE_REGEX.matchEntire(text)
                ?: throw MediaException(MediaErrors.UPLOAD_RETRYABLE, "malformed Content-Range")
            val start = match.groupValues[1].toLongOrNull()
            val end = match.groupValues[2].toLongOrNull()
            val total = match.groupValues[3].toLongOrNull()
            if (start == null || end == null || total == null) {
                throw MediaException(MediaErrors.UPLOAD_RETRYABLE, "Content-Range is not numeric")
            }
            if (total <= 0) {
                throw MediaException(MediaErrors.MEDIA_TOO_LARGE, "media total size must be positive")
            }
            if (start < 0 || end < start || end >= total) {
                throw MediaException(MediaErrors.UPLOAD_RETRYABLE, "Content-Range is outside the asset bounds")
            }
            val chunk = max(1, chunkBytes)
            if (start % chunk != 0L) {
                throw MediaException(
                    MediaErrors.UPLOAD_RETRYABLE,
                    "chunk start is not aligned to the fixed $chunk-byte chunk size",
                )
            }
            val planned = planContentRange((start / chunk).toInt(), total, chunk)
            if (end != planned.end) {
                throw MediaException(
                    MediaErrors.UPLOAD_RETRYABLE,
                    "Content-Range end does not match the fixed chunk plan",
                )
            }
            return planned
        }

        private val CONTENT_RANGE_REGEX = Regex("bytes (\\d+)-(\\d+)/(\\d+)")
    }
}

/** 固定 1 MiB 分块时，整份素材需要多少块。 */
fun chunkCount(totalBytes: Long, chunkBytes: Int = MediaLimits.CHUNK_BYTES): Int {
    if (totalBytes <= 0) return 0
    val chunk = max(1, chunkBytes).toLong()
    return ((totalBytes + chunk - 1) / chunk).toInt()
}

/** 按 index 算出这段的 `Content-Range`；越界视为可重试。 */
fun planContentRange(
    index: Int,
    totalBytes: Long,
    chunkBytes: Int = MediaLimits.CHUNK_BYTES,
): ContentRange {
    if (totalBytes <= 0) {
        throw MediaException(MediaErrors.MEDIA_TOO_LARGE, "media total size must be positive")
    }
    if (index < 0) {
        throw MediaException(MediaErrors.UPLOAD_RETRYABLE, "chunk index must be non-negative")
    }
    val count = chunkCount(totalBytes, chunkBytes)
    if (index >= count) {
        throw MediaException(
            MediaErrors.UPLOAD_RETRYABLE,
            "chunk index $index is out of range for $totalBytes bytes ($count chunks)",
        )
    }
    val chunk = max(1, chunkBytes).toLong()
    val start = index.toLong() * chunk
    val end = min(start + chunk - 1, totalBytes - 1)
    return ContentRange(start = start, end = end, total = totalBytes)
}

/** 全部块的计划（顺序即上传顺序）。 */
fun planAllChunks(totalBytes: Long, chunkBytes: Int = MediaLimits.CHUNK_BYTES): List<ContentRange> =
    (0 until chunkCount(totalBytes, chunkBytes)).map { planContentRange(it, totalBytes, chunkBytes) }

/** 固定上传路径：`/v1/devices/{device_id}/assets/{asset_id}/chunks/{index}`。 */
fun assetChunkPath(deviceId: String, assetId: String, index: Int): String {
    if (!isValidDeviceId(deviceId)) {
        throw MediaException(MediaErrors.MEDIA_NOT_FOUND, "invalid device id")
    }
    if (!isValidAssetId(assetId)) {
        throw MediaException(MediaErrors.MEDIA_NOT_FOUND, "invalid asset id")
    }
    if (index < 0) {
        throw MediaException(MediaErrors.UPLOAD_RETRYABLE, "chunk index must be non-negative")
    }
    return "/v1/devices/$deviceId/assets/$assetId/chunks/$index"
}

/** 拼出完整 HTTPS URL；非 https 端点直接拒绝（媒体不得明文传输）。 */
fun assetChunkUrl(baseUrl: String, deviceId: String, assetId: String, index: Int): String {
    if (!baseUrl.startsWith("https://")) {
        throw MediaException(MediaErrors.UPLOAD_RETRYABLE, "upload endpoint must be an https URL")
    }
    return baseUrl.trimEnd('/') + assetChunkPath(deviceId, assetId, index)
}

/** 每块必须带的固定头：`Content-Range` 与 `X-Asset-Sha256`。 */
fun chunkHeaders(
    assetId: String,
    index: Int,
    totalBytes: Long,
    sha256Hex: String,
    chunkBytes: Int = MediaLimits.CHUNK_BYTES,
): Map<String, String> {
    if (!isValidAssetId(assetId)) {
        throw MediaException(MediaErrors.MEDIA_NOT_FOUND, "invalid asset id")
    }
    if (!isSha256Hex(sha256Hex)) {
        throw MediaException(MediaErrors.UPLOAD_RETRYABLE, "X-Asset-Sha256 must be a 64-hex digest")
    }
    val planned = planContentRange(index, totalBytes, chunkBytes)
    return linkedMapOf(
        "Content-Range" to planned.header(),
        "X-Asset-Sha256" to sha256Hex.lowercase(),
        "Content-Type" to "application/octet-stream",
    )
}

/** 按固定分块计划读取第 index 块；长度必须与 `Content-Range` 完全一致。 */
fun readChunk(file: File, index: Int, totalBytes: Long, chunkBytes: Int = MediaLimits.CHUNK_BYTES): ByteArray {
    val planned = planContentRange(index, totalBytes, chunkBytes)
    RandomAccessFile(file, "r").use { raf ->
        raf.seek(planned.start)
        val buffer = ByteArray(planned.length.toInt())
        var read = 0
        while (read < buffer.size) {
            val step = raf.read(buffer, read, buffer.size - read)
            if (step <= 0) break
            read += step
        }
        if (read != buffer.size) {
            throw MediaException(
                MediaErrors.UPLOAD_RETRYABLE,
                "chunk $index read $read bytes, expected ${buffer.size}",
            )
        }
        return buffer
    }
}

// ---------------------------------------------------------------------------
// 纯规则：sha256 与上传完成判定
// ---------------------------------------------------------------------------

/** 64 位十六进制摘要的严格判定（`X-Asset-Sha256` 必须先过这一关）。 */
fun isSha256Hex(value: String?): Boolean =
    value != null && Regex("[A-Fa-f0-9]{64}").matches(value)

/** 字节数组的 sha256（小写十六进制）。 */
fun sha256(data: ByteArray): String {
    val digest = MessageDigest.getInstance("SHA-256").digest(data)
    return digest.toHex()
}

/** 文件内容的 sha256，流式读取（不会把 100 MiB 一次性读进内存）。 */
fun sha256(file: File): String {
    val digest = MessageDigest.getInstance("SHA-256")
    file.inputStream().use { stream ->
        val buffer = ByteArray(64 * 1024)
        while (true) {
            val read = stream.read(buffer)
            if (read <= 0) break
            digest.update(buffer, 0, read)
        }
    }
    return digest.digest().toHex()
}

/**
 * 字节数组转小写十六进制；按无符号 8 位处理。
 *
 * 手写而不是 `"%02x".format(...)`：后者依赖参数装箱/提升的语义，一旦传入的是
 * 已提升为 `Int` 的负值（例如 `byte.toInt() or 0`），就会输出 8 个 hex 字符，
 * 摘要长度被破坏。这里显式 `and 0xFF`，不受装箱路径影响。
 */
private fun ByteArray.toHex(): String {
    val builder = StringBuilder(size * 2)
    for (byte in this) {
        val value = byte.toInt() and 0xFF
        builder.append(HEX_DIGITS[value shr 4]).append(HEX_DIGITS[value and 0x0F])
    }
    return builder.toString()
}

private const val HEX_DIGITS = "0123456789abcdef"

/** 字节数量是否到位；未到位才是 [MediaErrors.UPLOAD_RETRYABLE]。 */
fun isUploadByteComplete(
    receivedChunks: Int,
    totalBytes: Long,
    chunkBytes: Int = MediaLimits.CHUNK_BYTES,
): Boolean = receivedChunks >= chunkCount(totalBytes, chunkBytes)

/** 总 sha256 是否与 `X-Asset-Sha256` 一致；不一致才是 [MediaErrors.UPLOAD_HASH_MISMATCH]。 */
fun verifyUploadHash(file: File, expectedSha256: String?): Boolean {
    if (!isSha256Hex(expectedSha256)) return false
    return sha256(file) == expectedSha256!!.lowercase()
}

/**
 * 上传完成判定：`null` 表示**全部字节到位且总 hash 匹配**，可以回 `upload_complete`；
 * 否则返回七个固定失败码之一。
 *
 * 顺序固定（先字节后 hash），因为「缺块」与「hash 不符」是两种不同的处置：
 * 前者可续传（[MediaErrors.UPLOAD_RETRYABLE]），后者必须丢弃缓冲重传整份
 * （[MediaErrors.UPLOAD_HASH_MISMATCH]）。
 */
fun uploadCompletionFailure(
    receivedChunks: Int,
    totalBytes: Long,
    file: File,
    expectedSha256: String?,
): String? {
    if (!isUploadByteComplete(receivedChunks, totalBytes)) return MediaErrors.UPLOAD_RETRYABLE
    if (!file.isFile) return MediaErrors.MEDIA_NOT_FOUND
    if (file.length() != totalBytes) return MediaErrors.UPLOAD_RETRYABLE
    if (!verifyUploadHash(file, expectedSha256)) return MediaErrors.UPLOAD_HASH_MISMATCH
    return null
}

/**
 * 拍摄前置判定：没有 `CAMERA` 权限时必须固定返回
 * [MediaErrors.CAMERA_PERMISSION_REQUIRED]，且**不启动相机、不产生文件**。
 *
 * 抽成纯函数是为了让这条失败状态可以在 JVM 上真实断言，而不是只能靠人工点按。
 */
fun capturePreconditionFailure(cameraGranted: Boolean, hasLifecycleOwner: Boolean): String? = when {
    !cameraGranted -> MediaErrors.CAMERA_PERMISSION_REQUIRED
    // 没有前台生命周期就绑不上 CameraX；明确失败，不用截图兜底伪装成功。
    !hasLifecycleOwner -> CommandErrors.NOT_WIRED
    else -> null
}

/**
 * 选择器结果判定：没有任何选中项即用户取消，固定返回
 * [MediaErrors.PHOTO_PICKER_CANCELLED]，且不产生任何素材。
 */
fun pickerCancellationFailure(selectedUriCount: Int): String? =
    if (selectedUriCount <= 0) MediaErrors.PHOTO_PICKER_CANCELLED else null

/**
 * 素材来源的形状判定：**只**接受系统选择器回传的 `content://` URI。
 *
 * 这一条把「模型给出的本地文件路径」挡在模块入口之外：
 * 绝对路径、`file://`、Windows 路径、`~` 路径与网络共享路径一律不接受。
 * 纯 JVM 判定，因此可以直接断言数百万种恶意形状都不通过。
 */
fun isPickerUriSchemeAllowed(raw: String?): Boolean {
    if (raw == null) return false
    val trimmed = raw.trim()
    if (trimmed.isEmpty()) return false
    // 必须是小写的 content:// 前缀，且后面要有实际内容。
    if (!trimmed.startsWith("content://")) return false
    val authorityAndPath = trimmed.removePrefix("content://")
    if (authorityAndPath.isEmpty()) return false
    // 拒绝路径穿越与反斜杠（提供者实现可能把它们当文件路径解释）。
    if (authorityAndPath.contains("..") || authorityAndPath.contains('\\')) return false
    return true
}
