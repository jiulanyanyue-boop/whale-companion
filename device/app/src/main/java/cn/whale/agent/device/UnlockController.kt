package cn.whale.agent.device

import android.content.Context
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/** Command boundary for opt-in local automatic device unlock. */
class UnlockController(private val context: Context) {
    fun unlock(envelope: DeviceEnvelope): DeviceResponse {
        if (android.os.Looper.myLooper() == android.os.Looper.getMainLooper()) {
            return denied(envelope, CommandErrors.ACTION_REJECTED, "unlock must execute on the command worker")
        }
        val credential = UnlockCredentialStore.load(context)
            ?: return denied(envelope, CommandErrors.NEEDS_UNLOCK, "automatic unlock is not configured on the phone")
        val keyguard = context.getSystemService(Context.KEYGUARD_SERVICE) as? android.app.KeyguardManager
        if (keyguard != null && !keyguard.isDeviceLocked && !keyguard.isKeyguardLocked) return ok(envelope, mapOf("unlocked" to "true", "method" to "already_unlocked"))
        if (WhaleAccessibilityService.instance == null) {
            return denied(envelope, CommandErrors.NO_ACCESSIBILITY, "accessibility service is required for automatic unlock")
        }
        val latch = CountDownLatch(1)
        var success = false
        runCatching { UnlockActivity.request(context, credential) { value -> success = value; latch.countDown() } }
            .onFailure { return denied(envelope, CommandErrors.UNLOCK_FAILED, "could not start the Android unlock screen") }
        if (!latch.await(13, TimeUnit.SECONDS) || !success || keyguard == null || keyguard.isDeviceLocked || keyguard.isKeyguardLocked) {
            UnlockActivity.cancelPending()
            return denied(envelope, CommandErrors.UNLOCK_FAILED, "Android did not confirm the phone was unlocked")
        }
        return ok(envelope, mapOf("unlocked" to "true", "method" to "local_credential"))
    }

    private fun ok(envelope: DeviceEnvelope, evidence: Map<String, String>) =
        DeviceResponse(envelope.request_id, envelope.device_id, "ok", evidence = evidence)
    private fun denied(envelope: DeviceEnvelope, code: String, message: String) =
        DeviceResponse(envelope.request_id, envelope.device_id, "denied", code, message)
}
