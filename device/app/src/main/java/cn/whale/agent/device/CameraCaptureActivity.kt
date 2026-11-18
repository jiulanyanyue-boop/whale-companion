package cn.whale.agent.device

import android.Manifest
import android.app.KeyguardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.widget.TextView
import androidx.activity.ComponentActivity
import androidx.core.content.ContextCompat
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference

/** A single internal foreground lifecycle for a validated remote camera request. */
class CameraCaptureActivity : ComponentActivity() {
    private var request: RemoteCameraCapture.Request? = null
    private val started = AtomicBoolean(false)

    override fun onCreate(state: Bundle?) {
        super.onCreate(state)
        request = RemoteCameraCapture.claim()
        if (request == null) { finish(); return }
        setContentView(TextView(this).apply { text = "小鲸鱼正在拍照…"; textSize = 22f; gravity = android.view.Gravity.CENTER })
    }

    override fun onResume() {
        super.onResume()
        val current = request ?: return
        if (!started.compareAndSet(false, true)) return
        if (current.result.get() != null) { finish(); return }
        Thread({
            val result = try {
                CameraController(this, current.assets).capture(CameraController.Request(lens = current.lens, quality = current.quality))
            } catch (error: Throwable) {
                CameraController.CaptureOutcome.Failed(CommandErrors.ACTION_REJECTED, "camera capture failed: ${error.javaClass.simpleName}")
            }
            current.complete(result)
            runOnUiThread { finish() }
        }, "whale-camera-capture").start()
    }

    override fun onDestroy() {
        request?.complete(CameraController.CaptureOutcome.Failed(CommandErrors.ACTION_REJECTED, "camera foreground activity closed"))
        super.onDestroy()
    }
}

/** The Activity can only consume an in-process request already admitted by CommandRouter. */
object RemoteCameraCapture {
    class Request(val assets: AssetStore, val lens: String, val quality: String) {
        val claimed = AtomicBoolean(false)
        val latch = CountDownLatch(1)
        val result = AtomicReference<CameraController.CaptureOutcome>()
        fun complete(value: CameraController.CaptureOutcome) {
            if (result.compareAndSet(null, value)) latch.countDown()
        }
    }
    private val pending = AtomicReference<Request>()

    fun claim(): Request? = pending.get()?.takeIf { it.result.get() == null && it.claimed.compareAndSet(false, true) }

    fun capture(context: Context, assets: AssetStore, lens: String, quality: String): CameraController.CaptureOutcome {
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
            return CameraController.CaptureOutcome.Failed(MediaErrors.CAMERA_PERMISSION_REQUIRED, "CAMERA permission is not granted")
        }
        if ((context.getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager)?.isDeviceLocked == true) {
            return CameraController.CaptureOutcome.Failed(CommandErrors.NEEDS_UNLOCK, "unlock the phone before taking a photo")
        }
        val request = Request(assets, lens, quality)
        if (!pending.compareAndSet(null, request)) return CameraController.CaptureOutcome.Failed(CommandErrors.ACTION_REJECTED, "camera is busy")
        return try {
            context.startActivity(Intent(context, CameraCaptureActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
            if (!request.latch.await(22, TimeUnit.SECONDS)) {
                request.complete(CameraController.CaptureOutcome.Failed(CommandErrors.ACTION_REJECTED, "camera foreground launch or capture timed out"))
            }
            request.result.get() ?: CameraController.CaptureOutcome.Failed(CommandErrors.ACTION_REJECTED, "camera returned no image")
        } catch (error: Throwable) {
            CameraController.CaptureOutcome.Failed(CommandErrors.ACTION_REJECTED, "camera foreground launch failed: ${error.javaClass.simpleName}")
        } finally { pending.compareAndSet(request, null) }
    }
}
