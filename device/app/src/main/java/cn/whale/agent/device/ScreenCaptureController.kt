package cn.whale.agent.device

import android.accessibilityservice.AccessibilityService
import android.app.KeyguardManager
import android.content.Context
import android.graphics.Bitmap
import android.os.Build
import android.os.Looper
import android.util.Base64
import android.view.Display
import java.io.ByteArrayOutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

/** Capture an actual display image using the user's enabled accessibility service. */
class ScreenCaptureController(private val context: Context) {
    fun capture(envelope: DeviceEnvelope, allowlist: Set<String>, blocked: Set<String>): DeviceResponse {
        fun failed(code: String, message: String) = DeviceResponse(envelope.request_id, envelope.device_id, "failed", code, message)
        if (Build.VERSION.SDK_INT < 30) return failed(CommandErrors.NOT_WIRED, "screen capture requires Android 11 or newer")
        val keyguard = context.getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager
        if (keyguard?.isDeviceLocked == true) return failed(CommandErrors.NEEDS_UNLOCK, "unlock the phone before taking a screenshot")
        val service = WhaleAccessibilityService.instance
            ?: return failed(CommandErrors.NO_ACCESSIBILITY, "accessibility service is not connected")
        val activePackage = service.rootInActiveWindow?.packageName?.toString().orEmpty()
        val homeIntent = android.content.Intent(android.content.Intent.ACTION_MAIN).addCategory(android.content.Intent.CATEGORY_HOME)
        val homePackage = context.packageManager.resolveActivity(homeIntent, android.content.pm.PackageManager.MATCH_DEFAULT_ONLY)?.activityInfo?.packageName.orEmpty()
        if (!DevicePolicy.canCaptureScreen(activePackage, context.packageName, homePackage, allowlist, blocked)) {
            return failed(CommandErrors.PACKAGE_NOT_ALLOWED, "current screen package is not allowed")
        }
        if (Looper.myLooper() == Looper.getMainLooper()) return failed(CommandErrors.ACTION_REJECTED, "capture must run on the command worker")
        val latch = CountDownLatch(1)
        val response = AtomicReference<DeviceResponse>()
        try {
            service.takeScreenshot(Display.DEFAULT_DISPLAY, service.mainExecutor, object : AccessibilityService.TakeScreenshotCallback {
                override fun onSuccess(screenshot: AccessibilityService.ScreenshotResult) {
                    val hardware = screenshot.hardwareBuffer
                    var bitmap: Bitmap? = null
                    try {
                        if (service.rootInActiveWindow?.packageName?.toString() != activePackage || keyguard?.isDeviceLocked == true) {
                            throw IllegalStateException("foreground screen changed during capture")
                        }
                        val wrapped = Bitmap.wrapHardwareBuffer(hardware, screenshot.colorSpace)
                            ?: throw IllegalStateException("screen image unavailable")
                        bitmap = wrapped.copy(Bitmap.Config.ARGB_8888, false)
                        wrapped.recycle()
                        var image = bitmap ?: throw IllegalStateException("screen image unavailable")
                        if (maxOf(image.width, image.height) > 2560) {
                            val scale = 2560f / maxOf(image.width, image.height)
                            val resized = Bitmap.createScaledBitmap(image, (image.width * scale).toInt().coerceAtLeast(1), (image.height * scale).toInt().coerceAtLeast(1), true)
                            image.recycle()
                            bitmap = resized
                            image = resized
                        }
                        var quality = 92
                        var bytes = ByteArrayOutputStream().also { image.compress(Bitmap.CompressFormat.JPEG, quality, it) }.toByteArray()
                        while (bytes.size > 600 * 1024 && quality > 20) {
                            quality = (quality - 10).coerceAtLeast(20)
                            bytes = ByteArrayOutputStream().also { image.compress(Bitmap.CompressFormat.JPEG, quality, it) }.toByteArray()
                        }
                        if (bytes.size > 600 * 1024) throw IllegalStateException("screenshot exceeds image limit")
                        response.set(DeviceResponse(envelope.request_id, envelope.device_id, "ok", evidence = mapOf(
                            "image_id" to "screen:${envelope.request_id}", "mime" to "image/jpeg", "width" to image.width.toString(), "height" to image.height.toString(),
                            "package" to activePackage, "image_base64" to Base64.encodeToString(bytes, Base64.NO_WRAP), "mode" to "vision",
                        )))
                    } catch (error: Throwable) {
                        response.set(failed(CommandErrors.ACTION_REJECTED, "screenshot could not be encoded: ${error.javaClass.simpleName}"))
                    } finally {
                        bitmap?.recycle()
                        hardware.close()
                        latch.countDown()
                    }
                }
                override fun onFailure(errorCode: Int) {
                    response.set(failed(CommandErrors.ACTION_REJECTED, "Android screen capture failed ($errorCode); protected windows cannot be captured"))
                    latch.countDown()
                }
            })
            if (!latch.await(8, TimeUnit.SECONDS)) return failed(CommandErrors.ACTION_REJECTED, "screenshot timed out")
        } catch (error: Throwable) {
            return failed(CommandErrors.ACTION_REJECTED, "screen capture failed: ${error.javaClass.simpleName}")
        }
        return response.get() ?: failed(CommandErrors.ACTION_REJECTED, "screenshot did not return an image")
    }
}
