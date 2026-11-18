package cn.whale.agent.device

/** Polling may wait for a keypad, but a request must never submit a password twice. */
class UnlockAttemptState {
    var submitted: Boolean = false
        private set
    fun canSubmit(): Boolean = !submitted
    fun markSubmitted() { submitted = true }
}
