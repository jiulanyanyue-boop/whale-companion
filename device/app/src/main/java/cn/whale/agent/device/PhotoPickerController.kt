package cn.whale.agent.device

import android.app.Activity
import android.content.ContentResolver
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.OpenableColumns
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContract
import androidx.activity.result.contract.ActivityResultContracts
import java.io.File

/**
 * M08 相册控制器（`device.media.pick` 的实现层）。
 *
 * 规范来源：`docs/modules/M08-media-product.md`「相册」与
 * `docs/modules/M08-media-development.md`「Photo Picker」。
 *
 * 固定行为：
 * - **API 33+**：只用 [ActivityResultContracts.PickVisualMedia]（单选）或
 *   [ActivityResultContracts.PickMultipleVisualMedia]（多选），由系统 Photo Picker 返回用户
 *   主动选择的 URI；**不**申请 `READ_MEDIA_IMAGES`、**不**查询整个媒体库；
 * - **API 26–32**：只用 `ACTION_OPEN_DOCUMENT`，且**必须**带
 *   [Intent.CATEGORY_OPENABLE] 与 [Intent.FLAG_GRANT_READ_URI_PERMISSION]；
 * - 结果只写进**当前任务**的资产表（[AssetStore] 的 `taskId` 归属），
 *   **不**建全局媒体索引、**不**把 URI 转成相册扫描；
 * - 用户取消 → [MediaErrors.PHOTO_PICKER_CANCELLED]，不产生任何素材；
 * - 选中项先复制进 app cache，再由 [AssetStore.register] 做大小/类型/单任务数量校验，
 *   只有通过的才进资产表，临时 cache 文件随即删除；
 * - 只接受系统选择器回传的 `content://` URI。模型给出的本地文件路径永远进不来，
 *   本类也**没有**接受任意路径的入口。
 *
 * 接线形态：本类提供「合同 / Intent 构造」与「结果处理」两半。前台 Activity 用
 * [singlePickerContract] / [multiplePickerContract] 注册
 * `registerForActivityResult(...)`，把回传的 URI 列表交给 [onPicked]；
 * 具体接线由 Lead 在 MainActivity 侧完成。
 */
class PhotoPickerController(
    private val context: Context,
    private val resolver: ContentResolver,
    private val cacheDir: File,
    private val assets: AssetStore,
) {
    /** 选择结果：成功是已登记素材列表，失败是固定失败码。 */
    sealed interface PickOutcome {
        data class Picked(val entries: List<AssetStore.AssetEntry>) : PickOutcome

        data class Failed(val code: String, val message: String) : PickOutcome
    }

    /** 一次选择请求；字段完全对应 `device.media.pick` 的 payload。 */
    data class Request(
        val mediaType: String = "any",
        val maxItems: Int = 1,
        val taskId: String = "",
    )

    /** Photo Picker 只在 API 33+ 内置；更低版本一律走 `ACTION_OPEN_DOCUMENT`。 */
    fun supportsPhotoPicker(): Boolean = Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU

    /** API 33+ 单选合同；`maxItems` 必须为 1。 */
    fun singlePickerContract(): ActivityResultContract<PickVisualMediaRequest, Uri?> =
        ActivityResultContracts.PickVisualMedia()

    /** API 33+ 多选合同；`maxItems` 必须 > 1，且不超过单任务上限 20。 */
    fun multiplePickerContract(maxItems: Int): ActivityResultContract<PickVisualMediaRequest, List<Uri>> {
        require(maxItems in 2..MediaLimits.MAX_ASSETS_PER_TASK) {
            "PickMultipleVisualMedia requires 2..${MediaLimits.MAX_ASSETS_PER_TASK} items"
        }
        return ActivityResultContracts.PickMultipleVisualMedia(maxItems)
    }

    /** 由 payload 的 `media_type` 得到 Photo Picker 的媒体类型请求。 */
    fun pickerRequest(mediaType: String): PickVisualMediaRequest {
        val media = when (MediaKind.parseOrAny(mediaType)) {
            MediaKind.IMAGE -> ActivityResultContracts.PickVisualMedia.ImageOnly
            MediaKind.VIDEO -> ActivityResultContracts.PickVisualMedia.VideoOnly
            // 音频与 any：Photo Picker 只有 image / video / image+video 三档，
            // 音频因此走 API 26-32 的 ACTION_OPEN_DOCUMENT 路径（见 buildLegacyIntent）。
            MediaKind.AUDIO, MediaKind.ANY -> ActivityResultContracts.PickVisualMedia.ImageAndVideo
        }
        return PickVisualMediaRequest.Builder().setMediaType(media).build()
    }

    /**
     * 本请求是否必须走 `ACTION_OPEN_DOCUMENT`：低于 API 33，或请求音频。
     *
     * Photo Picker 不支持音频，若要选音频就必须用 OPEN_DOCUMENT（仍带 CATEGORY_OPENABLE
     * 与读权限授权），这是文档允许的唯一替代路径。
     */
    fun requiresLegacyDocumentPicker(request: Request): Boolean =
        !supportsPhotoPicker() || MediaKind.parseOrAny(request.mediaType) == MediaKind.AUDIO

    /** API 26–32（以及音频请求）使用的 `ACTION_OPEN_DOCUMENT` Intent。 */
    fun buildLegacyIntent(request: Request): Intent {
        val kind = MediaKind.parseOrAny(request.mediaType)
        return Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            // 文档硬性要求：必须带 CATEGORY_OPENABLE 与 FLAG_GRANT_READ_URI_PERMISSION。
            addCategory(Intent.CATEGORY_OPENABLE)
            type = kind.pickerMimeType
            putExtra(Intent.EXTRA_ALLOW_MULTIPLE, request.maxItems > 1)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
    }

    /**
     * 系统是否真的有文档选择器可解析该 Intent。
     *
     * 没有可解析的 Activity 时不能伪造成功：由调用方转成固定失败，而不是空等用户点击。
     */
    fun canResolveLegacyPicker(request: Request): Boolean {
        val intent = buildLegacyIntent(request)
        val resolver = context.packageManager
        return resolver.resolveActivity(intent, android.content.pm.PackageManager.MATCH_DEFAULT_ONLY) != null
    }

    /**
     * 处理选择器回传的 URI。
     *
     * @param uris 系统回传的 `content://` URI；空列表或 null 即「用户取消」。
     * @return 已登记进当前任务资产表的素材；超出 [Request.maxItems] 的多余项被忽略。
     */
    fun onPicked(request: Request, uris: List<Uri>?): PickOutcome {
        if (uris.isNullOrEmpty()) {
            return PickOutcome.Failed(MediaErrors.PHOTO_PICKER_CANCELLED, "user cancelled the media picker")
        }
        val limited = uris.take(request.maxItems.coerceIn(1, MediaLimits.MAX_ASSETS_PER_TASK))
        val entries = ArrayList<AssetStore.AssetEntry>()
        val tempFiles = ArrayList<File>()

        try {
            val kind = MediaKind.parseOrAny(request.mediaType)
            for (uri in limited) {
                // 只接受系统选择器给的 content:// URI；模型给的本地路径永远进不来。
                if (uri.scheme != ContentResolver.SCHEME_CONTENT) {
                    return PickOutcome.Failed(
                        MediaErrors.MEDIA_TYPE_NOT_ALLOWED,
                        "only content:// URIs from the system picker are accepted",
                    )
                }
                val mime = resolver.getType(uri)
                    ?: return PickOutcome.Failed(
                        MediaErrors.MEDIA_TYPE_NOT_ALLOWED,
                        "picker returned a URI without a media type",
                    )
                if (!isAllowedMediaType(mime) || !kind.allowsMime(mime)) {
                    return PickOutcome.Failed(
                        MediaErrors.MEDIA_TYPE_NOT_ALLOWED,
                        "picked media type is not allowed for this request",
                    )
                }
                entries.add(copyIntoStore(uri, mime, request, tempFiles))
            }
            return PickOutcome.Picked(entries)
        } catch (e: MediaException) {
            // 大小/类型/数量校验失败由 AssetStore 抛出，原样映射成固定状态码。
            return PickOutcome.Failed(e.code, e.reason)
        } catch (t: Throwable) {
            return PickOutcome.Failed(
                MediaErrors.MEDIA_NOT_FOUND,
                "picker URI could not be read: ${t.javaClass.simpleName}",
            )
        } finally {
            tempFiles.forEach { if (it.isFile) it.delete() }
        }
    }

    /** 把 `content://` 内容复制到 app cache，再交给 [AssetStore] 做最终校验。 */
    private fun copyIntoStore(
        uri: Uri,
        mime: String,
        request: Request,
        tempFiles: MutableList<File>,
    ): AssetStore.AssetEntry {
        // 先用 OpenableColumns.SIZE 早退，避免把超大内容整块读进内存。
        declaredSize(uri)?.let { declared ->
            if (!isAllowedMediaSize(declared)) {
                throw MediaException(MediaErrors.MEDIA_TOO_LARGE, "picked media exceeds the size limit")
            }
        }

        val dir = File(cacheDir, CACHE_DIR).apply { mkdirs() }
        val suffix = mime.substringAfter('/', "").takeIf { it.isNotBlank() }?.let { ".$it" }.orEmpty()
        val temp = File(dir, "pick_${System.nanoTime()}$suffix")
        resolver.openInputStream(uri)?.use { input ->
            temp.outputStream().use { output -> input.copyTo(output) }
        } ?: throw MediaException(MediaErrors.MEDIA_NOT_FOUND, "picker URI could not be opened")
        tempFiles.add(temp)

        // 以实际字节数为准，不只信 OpenableColumns（提供者可能谎报）。
        val actualSize = temp.length()
        if (!isAllowedMediaSize(actualSize)) {
            throw MediaException(MediaErrors.MEDIA_TOO_LARGE, "picked media exceeds the size limit")
        }

        return assets.register(
            source = temp,
            mime = mime,
            taskId = request.taskId.ifEmpty { null },
        )
    }

    private fun declaredSize(uri: Uri): Long? = runCatching {
        resolver.query(uri, arrayOf(OpenableColumns.SIZE), null, null, null)?.use { cursor ->
            val index = cursor.getColumnIndex(OpenableColumns.SIZE)
            if (index >= 0 && cursor.moveToFirst() && !cursor.isNull(index)) cursor.getLong(index) else null
        }
    }.getOrNull()

    companion object {
        private const val CACHE_DIR = "media_pick"

        /** 从 `ActivityResult` 的 `Intent` 取出被选中的 URI（含多选 clipData 路径）。 */
        fun urisFromResult(resultCode: Int, data: Intent?): List<Uri>? {
            if (resultCode != Activity.RESULT_OK || data == null) return null
            val clip = data.clipData
            if (clip != null) {
                return (0 until clip.itemCount).map { clip.getItemAt(it).uri }
            }
            return data.data?.let { listOf(it) }
        }
    }
}
