package cn.whale.agent.device
import org.junit.Assert.*
import org.junit.Test
class UnlockAttemptStateTest {
    @Test fun waitingForWindowDoesNotConsumeSubmission() {
        val state = UnlockAttemptState()
        repeat(10) { assertTrue(state.canSubmit()) }
    }
    @Test fun afterSubmissionFurtherPollsCannotTypeAgain() {
        val state = UnlockAttemptState()
        state.markSubmitted()
        repeat(40) { assertFalse(state.canSubmit()) }
    }
}
