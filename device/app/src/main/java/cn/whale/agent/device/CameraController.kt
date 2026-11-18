package cn.whale.agent.device

import android.Manifest
import android.content.Context
import android.content.ContextWrapper
import android.content.pm.PackageManager
import android.util.Size
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.util.Base64
import java.io.ByteArrayOutputStream
import androidx.core.content.ContextCompat
import androidx.camera.core.CameraSelector
import androidx.camera.core.ImageCapture
import androidx.camera.core.ImageCaptureException
import androidx.camera.core.resolutionselector.ResolutionSelector
import androidx.camera.core.resolutionselector.ResolutionStrategy
import androidx.camera.lifecycle.ProcessCameraProvider
import androidx.lifecycle.LifecycleOwner
import java.io.File
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executor
import java.util.concurrent.TimeUnit

/**
 * M08 相机控制器（`device.camera.capture` 的实现层）。
 *
 * 规范来源：`docs/modules/M08-media-product.md`「相机」与
 * `docs/modules/M08-media-development.md`「CameraX」。
 *
 * 固定行为：
 * - 使用 [ProcessCameraProvider] 绑定前台生命周期，不自己造 Camera2 会话；
 * - 用 [ImageCapture] 把照片存到 **app cache** 下的**随机文件名**（文件名不含时间戳与用户内容）；
 * - 默认 `LENS_FACING_BACK`、JPEG、标准质量；payload 的 `lens` / `quality` 只能在这两个
 *   已声明枚举里覆盖；
 * - Store the original in [AssetStore] and return metadata plus a bounded JPEG preview.
 * - 真实路径**永不**进入 [DeviceResponse]，也不进入任何 OneBot 字段；
 * - 没有 `CAMERA` 权限时在启动相机前返回 [MediaErrors.CAMERA_PERMISSION_REQUIRED]，
 *   不产生预览、不产生文件、不落库；
 * - 拿不到前台 Activity / 相机失败时返回固定失败，**不**用截图或视觉兜底伪装成功。
 *
 * CameraX binding, capture, and callbacks run on the main executor. The command
 * worker waits with a deadline, so it never blocks the lifecycle callback thread.
 */
class CameraController(
    private val context: Context,
    private val assets: AssetStore,
) {
    /** 拍摄结果：要么成功元数据，要么固定失败码。 */
    sealed interface CaptureOutcome {
        data class Captured(val entry: AssetStore.AssetEntry, val preview: Map<String, String> = emptyMap()) : CaptureOutcome

        data class Failed(val code: String, val message: String) : CaptureOutcome
    }

    /** 拍摄请求；字段完全对应 `device.camera.capture` 的 payload。 */
    data class Request(
        val lens: String = "back",
        val quality: String = "standard",
        val taskId: String = "",
        val awaitMs: Long = DEFAULT_AWAIT_MS,
    )

    private val captureExecutor: Executor = ContextCompat.getMainExecutor(context)
    private val callbackExecutor: Executor = ContextCompat.getMainExecutor(context)

    /** 相机权限是否已授予。 */
    fun hasCameraPermission(): Boolean =
        ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED

    /**
     * 执行一次拍摄。
     *
     * @return [CaptureOutcome.Captured] 时 `entry` 可直接用 `assets.evidence(entry)` 回包。
     */
    fun capture(request: Request): CaptureOutcome {
        if (!hasCameraPermission()) {
            return CaptureOutcome.Failed(
                MediaErrors.CAMERA_PERMISSION_REQUIRED,
                "CAMERA permission is not granted",
            )
        }
        // CameraX 必须绑定前台生命周期；没有就没有相机，明确失败而不是伪造成功。
        val owner = lifecycleOwner()
            ?: return CaptureOutcome.Failed(
                CommandErrors.NOT_WIRED,
                "camera requires a foreground activity to bind CameraX",
            )

        // The manual APK packager does not merge Camera2's manifest provider.
        // Supply the backend explicitly before obtaining the process singleton.
        runCatching { ProcessCameraProvider.configureInstance(androidx.camera.camera2.Camera2Config.defaultConfig()) }
        val providerFuture = ProcessCameraProvider.getInstance(context)
        val provider = try {
            providerFuture.get(CAMERA_PROVIDER_TIMEOUT_MS, TimeUnit.MILLISECONDS)
        } catch (t: Throwable) {
            return CaptureOutcome.Failed(
                CommandErrors.NOT_WIRED,
                "camera provider is unavailable: ${t.javaClass.simpleName}",
            )
        }

        val tempFile = cacheFile()
        val latch = CountDownLatch(1)
        // 回调线程写、调用方线程读；用 @Volatile 语义的持有者避免可见性问题。
        val result = ResultHolder()

        captureExecutor.execute {
            try {
                val imageCapture = buildImageCapture(request)
                provider.unbindAll()
                provider.bindToLifecycle(
                    owner,
                    CameraSelector.Builder()
                        .requireLensFacing(lensFacing(request.lens))
                        .build(),
                    imageCapture,
                )
                imageCapture.takePicture(
                    ImageCapture.OutputFileOptions.Builder(tempFile).build(),
                    callbackExecutor,
                    object : ImageCapture.OnImageSavedCallback {
                        override fun onImageSaved(outputFileResults: ImageCapture.OutputFileResults) {
                            // 只复制进受控 AssetStore；随后删除 cache 临时文件。
                            result.entry = try {
                                assets.register(
                                    source = tempFile,
                                    mime = MIME_JPEG,
                                    taskId = request.taskId.ifEmpty { null },
                                )
                            } catch (e: MediaException) {
                                result.failure = CaptureOutcome.Failed(e.code, e.reason)
                                null
                            }
                            latch.countDown()
                        }

                        override fun onError(exception: ImageCaptureException) {
                            result.failure = mapCaptureException(exception)
                            latch.countDown()
                        }
                    },
                )
            } catch (t: Throwable) {
                result.failure = CaptureOutcome.Failed(
                    CommandErrors.NOT_WIRED,
                    "camera bind failed: ${t.javaClass.simpleName}",
                )
                latch.countDown()
            }
        }

        val completed = awaitLatch(latch, request.awaitMs)
        tempFile.delete()
        captureExecutor.execute { runCatching { provider.unbindAll() } }

        if (!completed) {
            return CaptureOutcome.Failed(
                CommandErrors.NOT_WIRED,
                "camera capture timed out after ${request.awaitMs} ms",
            )
        }
        result.failure?.let { return it }
        val entry = result.entry
            ?: return CaptureOutcome.Failed(MediaErrors.MEDIA_NOT_FOUND, "capture produced no asset")
        return try {
            val file = assets.fileOf(entry.assetId)
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeFile(file.absolutePath, bounds)
            var image = BitmapFactory.decodeFile(file.absolutePath, BitmapFactory.Options().apply { inSampleSize = galleryDecodeSample(bounds.outWidth, bounds.outHeight, 2560) })
                ?: return CaptureOutcome.Failed(MediaErrors.MEDIA_NOT_FOUND, "captured image cannot be read")
            if (maxOf(image.width, image.height) > 2560) {
                val scale = 2560f / maxOf(image.width, image.height)
                val resized = Bitmap.createScaledBitmap(image, (image.width * scale).toInt().coerceAtLeast(1), (image.height * scale).toInt().coerceAtLeast(1), true)
                image.recycle()
                image = resized
            }
            try {
                var quality = 92
                var bytes = ByteArrayOutputStream().also { image.compress(Bitmap.CompressFormat.JPEG, quality, it) }.toByteArray()
                while (bytes.size > 600 * 1024 && quality > 20) {
                    quality = (quality - 10).coerceAtLeast(20)
                    bytes = ByteArrayOutputStream().also { image.compress(Bitmap.CompressFormat.JPEG, quality, it) }.toByteArray()
                }
                if (bytes.size > 600 * 1024) return CaptureOutcome.Failed(MediaErrors.MEDIA_TOO_LARGE, "captured preview exceeds image limit")
                CaptureOutcome.Captured(entry, mapOf("image_id" to entry.assetId, "image_base64" to Base64.encodeToString(bytes, Base64.NO_WRAP), "mime" to MIME_JPEG, "width" to image.width.toString(), "height" to image.height.toString()))
            } finally { image.recycle() }
        } catch (error: Throwable) {
            CaptureOutcome.Failed(MediaErrors.MEDIA_NOT_FOUND, "capture preview failed: ${error.javaClass.simpleName}")
        }
    }

    /** 有界轮询等待回调；不阻塞 callbackExecutor 线程，因此不会自锁。 */
    private fun awaitLatch(latch: CountDownLatch, awaitMs: Long): Boolean {
        val deadline = System.currentTimeMillis() + awaitMs.coerceAtLeast(1)
        while (System.currentTimeMillis() < deadline) {
            if (latch.count == 0L) return true
            Thread.sleep(AWAIT_POLL_MS)
        }
        return latch.count == 0L
    }

    private fun buildImageCapture(request: Request): ImageCapture {
        val quality = if (request.quality == "high") JPEG_HIGH_QUALITY else JPEG_STANDARD_QUALITY
        val builder = ImageCapture.Builder()
            .setCaptureMode(ImageCapture.CAPTURE_MODE_MINIMIZE_LATENCY)
            .setFlashMode(ImageCapture.FLASH_MODE_AUTO)
            .setJpegQuality(quality)
        if (request.quality == "high") {
            builder.setResolutionSelector(
                ResolutionSelector.Builder()
                    .setResolutionStrategy(
                        ResolutionStrategy(
                            Size(LONG_SIDE_HIGH, SHORT_SIDE_HIGH),
                            ResolutionStrategy.FALLBACK_RULE_CLOSEST_HIGHER_THEN_LOWER,
                        ),
                    )
                    .build(),
            )
        }
        return builder.build()
    }

    private fun lensFacing(lens: String): Int =
        if (lens == "front") CameraSelector.LENS_FACING_FRONT else CameraSelector.LENS_FACING_BACK

    /** 在 app cache 下生成随机文件名；绝不把真实路径当 ID 使用。 */
    private fun cacheFile(): File {
        val dir = File(context.cacheDir, CACHE_DIR).apply { mkdirs() }
        return File(dir, "capture_${UUID.randomUUID().toString().replace("-", "")}.jpg")
    }

    /** 找到可用的 [LifecycleOwner]：CameraX 必须绑定前台 Activity 的生命周期。 */
    private fun lifecycleOwner(): LifecycleOwner? {
        var current: Context? = context
        while (current != null) {
            if (current is LifecycleOwner) return current
            current = (current as? ContextWrapper)?.baseContext
        }
        return null
    }

    /** 把 CameraX 异常收敛到固定失败状态，绝不外泄堆栈。 */
    private fun mapCaptureException(exception: ImageCaptureException): CaptureOutcome.Failed =
        when (exception.imageCaptureError) {
            ImageCapture.ERROR_FILE_IO,
            ImageCapture.ERROR_CAPTURE_FAILED,
            ImageCapture.ERROR_CAMERA_CLOSED,
            ImageCapture.ERROR_INVALID_CAMERA,
            -> CaptureOutcome.Failed(MediaErrors.UPLOAD_RETRYABLE, "camera capture failed; retry the capture")

            else -> CaptureOutcome.Failed(
                CommandErrors.NOT_WIRED,
                "camera capture is not available in this build",
            )
        }

    /** 回调线程与调用方线程之间传递结果的可变持有者。 */
    private class ResultHolder {
        @Volatile
        var entry: AssetStore.AssetEntry? = null

        @Volatile
        var failure: CaptureOutcome.Failed? = null
    }

    companion object {
        /** 固定 JPEG mime。 */
        private const val MIME_JPEG = "image/jpeg"

        /** 标准质量对应 85。 */
        private const val JPEG_STANDARD_QUALITY = 85

        /** `quality=high` 对应 95。 */
        private const val JPEG_HIGH_QUALITY = 95

        /** `quality=high` 时的目标分辨率。 */
        private const val LONG_SIDE_HIGH = 1920
        private const val SHORT_SIDE_HIGH = 1080

        private const val CACHE_DIR = "media_capture"
        private const val DEFAULT_AWAIT_MS = 5_000L
        private const val CAMERA_PROVIDER_TIMEOUT_MS = 3_000L
        private const val AWAIT_POLL_MS = 10L
    }
}
