package cn.whale.agent.device

import android.app.Activity
import android.app.KeyguardManager
import android.content.Context
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock

/** Foreground host kept alive while Android displays its separate credential window. */
class UnlockActivity : Activity() {
    private val handler = Handler(Looper.getMainLooper())
    private val attempt = UnlockAttemptState()
    private var finished = false
    private var startedAt = 0L

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        active = this
        if (android.os.Build.VERSION.SDK_INT >= 27) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        } else {
            window.addFlags(android.view.WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                android.view.WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON)
        }
        startedAt = SystemClock.elapsedRealtime()
        if (requestCredential == null) { finishRequest(false); return }
        android.util.Log.i("WhaleUnlock", "request_started")
        // Must wait until our window is visible before requesting the secure keyguard UI.
        handler.postDelayed({
            if (finished) return@postDelayed
            (getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager).requestDismissKeyguard(this,
                object : KeyguardManager.KeyguardDismissCallback() {
                    override fun onDismissSucceeded() { if (!finished) finishRequest(isUnlocked()) }
                    override fun onDismissError() { android.util.Log.i("WhaleUnlock", "dismiss_error") }
                    override fun onDismissCancelled() { android.util.Log.i("WhaleUnlock", "dismiss_cancelled") }
                })
            poll()
        }, 300)
    }

    private fun poll() {
        if (finished) return
        if (isUnlocked()) { finishRequest(true); return }
        if (SystemClock.elapsedRealtime() - startedAt >= TIMEOUT_MS) { finishRequest(false); return }
        val credential = requestCredential
        if (credential == null || UnlockCredentialStore.load(this) != credential) { finishRequest(false); return }
        if (attempt.canSubmit()) {
            when (UnlockAccessibilityBridge.fill(credential)) {
                UnlockAccessibilityBridge.Result.WAITING -> Unit
                UnlockAccessibilityBridge.Result.SUBMITTED -> {
                    attempt.markSubmitted()
                    android.util.Log.i("WhaleUnlock", "credential_submitted_once")
                }
                UnlockAccessibilityBridge.Result.PARTIAL_FAILURE -> {
                    attempt.markSubmitted()
                    finishRequest(false)
                    return
                }
            }
        }
        handler.postDelayed({ poll() }, 300)
    }

    private fun isUnlocked(): Boolean =
        (getSystemService(Context.KEYGUARD_SERVICE) as? KeyguardManager)?.let { !it.isDeviceLocked && !it.isKeyguardLocked } == true

    private fun finishRequest(success: Boolean) {
        if (finished) return
        finished = true
        handler.removeCallbacksAndMessages(null)
        // Wrong/unsupported credentials must not be retried by the next model request.
        if (!success && attempt.submitted) UnlockCredentialStore.clear(this)
        android.util.Log.i("WhaleUnlock", if (success) "confirmed_unlocked" else "not_unlocked")
        val callback = requestResult
        requestCredential = null
        requestResult = null
        callback?.invoke(success)
        finish()
    }

    override fun onDestroy() {
        if (!finished) finishRequest(false)
        if (active === this) active = null
        super.onDestroy()
    }

    companion object {
        private const val TIMEOUT_MS = 10_000L
        private var active: UnlockActivity? = null
        @Volatile private var requestCredential: UnlockCredentialStore.Credential? = null
        @Volatile private var requestResult: ((Boolean) -> Unit)? = null
        @Synchronized
        fun request(context: Context, credential: UnlockCredentialStore.Credential, callback: (Boolean) -> Unit) {
            check(requestResult == null) { "unlock request already active" }
            requestCredential = credential
            requestResult = callback
            try {
                context.startActivity(android.content.Intent(context, UnlockActivity::class.java)
                    .addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK))
            } catch (error: Exception) {
                requestCredential = null
                requestResult = null
                throw error
            }
        }

        fun cancelPending() {
            Handler(Looper.getMainLooper()).post {
                val activity = active
                if (activity != null) activity.finishRequest(false)
                else {
                    val callback = requestResult
                    requestResult = null
                    requestCredential = null
                    callback?.invoke(false)
                }
            }
        }
    }
}
