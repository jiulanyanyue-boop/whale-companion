package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M09 固定测试：桌宠状态机 + 语音录音状态机（**纯 JVM，JUnit4**）。
 *
 * 规范来源 `docs/modules/M09-local-pet-voice-development.md`：
 * - 状态固定：`HIDDEN / IDLE / WORKING / WAITING_CONFIRM / WAITING_USER / SUCCESS / ERROR / OFFLINE`；
 * - 状态只来自 `PetEvent`，固定事件名 `task_started / task_progress / confirm_required / user_required /
 *   task_succeeded / task_failed / task_stopped / device_offline`；
 * - 录音状态固定 `IDLE → PRESSING → LISTENING → TRANSCRIBING → IDLE`，任何状态 → `STOPPED`。
 *
 * 覆盖 M13 验收矩阵用例 `voice_push_to_talk_lifecycle`
 * （`test-fixtures/m13-acceptance-matrix.json`，期望状态序列 `IDLE, PRESSING, LISTENING, TRANSCRIBING, IDLE`，
 * 清理动作「release microphone」）。
 *
 * 本测试不依赖 `WindowManager`、`SystemClock` 或任何 Android 运行时：时钟全部注入。
 */
class PetStateMachineTest {

    // ---------- 测试替身 ----------

    /** 可推进的假时钟（替代 `SystemClock.elapsedRealtime`）。 */
    private class FakeClock(var now: Long = 1_000L) : PetClock {
        override fun nowMs(): Long = now

        fun advance(ms: Long) {
            now += ms
        }
    }

    private fun machine(clock: FakeClock = FakeClock()): PetStateMachine = PetStateMachine(clock)

    // ---------- 1. 状态集合固定 ----------

    /**
     * 桌宠状态集合固定为文档列举的 8 个名字。
     *
     * 文档「宠物状态机」已明确写成「固定 8 个状态，禁止增删」（历史描述曾误称「七态」，以列举为准）；
     * 这个断言把「不能少、不能多、不能改名」钉死。
     */
    @Test
    fun petStateSetIsFixed() {
        val expected = setOf(
            "HIDDEN", "IDLE", "WORKING", "WAITING_CONFIRM",
            "WAITING_USER", "SUCCESS", "ERROR", "OFFLINE",
        )
        assertEquals(expected, PetState.entries.map { it.name }.toSet())
    }

    /** 固定事件名必须是这 8 个 wire 形式，不多不少。 */
    @Test
    fun petEventWireNamesAreFixed() {
        val expected = setOf(
            "task_started", "task_progress", "confirm_required", "user_required",
            "task_succeeded", "task_failed", "task_stopped", "device_offline",
        )
        assertEquals(expected, PetEvent.WIRE_NAMES)
        assertEquals(8, PetEvent.entries.size)
    }

    /** 初始状态是 HIDDEN（窗口还没显示）。 */
    @Test
    fun initialStateIsHidden() {
        assertEquals(PetState.HIDDEN, machine().state)
        assertFalse(machine().isVisible)
    }

    /** `show()` 进入 IDLE：窗口显示即在线空闲。 */
    @Test
    fun showEntersIdle() {
        val m = machine()
        assertEquals(PetState.IDLE, m.show())
        assertTrue(m.isVisible)
    }

    // ---------- 2. 8 个事件 → 状态映射 ----------

    /** 事件 → 目标态映射表（固定，唯一迁移来源）。 */
    @Test
    fun eventsMapToFixedStates() {
        val cases = listOf(
            PetEvent.TASK_STARTED to PetState.WORKING,
            PetEvent.TASK_PROGRESS to PetState.WORKING,
            PetEvent.CONFIRM_REQUIRED to PetState.WAITING_CONFIRM,
            PetEvent.USER_REQUIRED to PetState.WAITING_USER,
            PetEvent.TASK_SUCCEEDED to PetState.SUCCESS,
            PetEvent.TASK_FAILED to PetState.ERROR,
            PetEvent.TASK_STOPPED to PetState.IDLE,
            PetEvent.DEVICE_OFFLINE to PetState.OFFLINE,
        )
        cases.forEach { (event, expected) ->
            val m = machine(clock = FakeClock(now = 0L))
            m.show()
            assertEquals("事件 ${event.wireName} 应映射到 $expected", expected, m.onEvent(event))
            assertEquals("lastEvent 应记录 ${event.wireName}", event, m.lastEvent)
        }
    }

    /** wire 形式入口等价于枚举入口：宿主用任务运行时的事件名驱动桌宠。 */
    @Test
    fun wireNameDrivesSameState() {
        val m = machine()
        m.show()
        assertEquals(PetState.WORKING, m.onEventName("task_started"))
        assertEquals(PetState.WAITING_CONFIRM, m.onEventName("confirm_required"))
        assertEquals(PetState.SUCCESS, m.onEventName("task_succeeded"))
    }

    /** 未知事件名被拒绝：返回 `null`，状态与 lastEvent 都不变（桌宠不接受自定义事件）。 */
    @Test
    fun unknownEventNameIsRejected() {
        val m = machine()
        m.show()
        m.onEvent(PetEvent.TASK_STARTED)
        assertEquals(PetState.WORKING, m.state)

        assertNull(m.onEventName("task_paused"))
        assertNull(m.onEventName("ignored_by_user"))
        assertNull(m.onEventName(""))
        assertEquals(PetState.WORKING, m.state)
        assertEquals(PetEvent.TASK_STARTED, m.lastEvent)
    }

    /**
     * `task_progress` 不得抢走等待态：
     * 等待确认 / 等待用户回答时桌宠必须保持清晰可见（产品文档「任务等待确认时保持清晰可见」）。
     */
    @Test
    fun progressDoesNotOverrideWaitingStates() {
        val m = machine()
        m.show()
        m.onEvent(PetEvent.CONFIRM_REQUIRED)
        assertEquals(PetState.WAITING_CONFIRM, m.state)
        m.onEvent(PetEvent.TASK_PROGRESS)
        assertEquals(PetState.WAITING_CONFIRM, m.state)

        val m2 = machine()
        m2.show()
        m2.onEvent(PetEvent.USER_REQUIRED)
        m2.onEvent(PetEvent.TASK_PROGRESS)
        assertEquals(PetState.WAITING_USER, m2.state)
    }

    /** 完整任务链路：started → confirm → succeeded。 */
    @Test
    fun taskLifecycleWithConfirmation() {
        val clock = FakeClock()
        val m = machine(clock)
        m.show()
        m.onEvent(PetEvent.TASK_STARTED)
        assertEquals(PetState.WORKING, m.state)
        m.onEvent(PetEvent.CONFIRM_REQUIRED)
        assertEquals(PetState.WAITING_CONFIRM, m.state)
        m.onEvent(PetEvent.TASK_SUCCEEDED)
        assertEquals(PetState.SUCCESS, m.state)
    }

    /** `task_stopped` 回到在线空闲，不隐藏窗口（停止 ≠ 关闭桌宠）。 */
    @Test
    fun taskStoppedReturnsToIdleNotHidden() {
        val m = machine()
        m.show()
        m.onEvent(PetEvent.TASK_STARTED)
        assertEquals(PetState.IDLE, m.onEvent(PetEvent.TASK_STOPPED))
        assertTrue(m.isVisible)
    }

    /**
     * `device_offline` 是**粘性**终态：进度与停止事件都不会把桌宠显示成在线，
     * 只有新的 `task_started` 才能重新进入工作态。
     */
    @Test
    fun deviceOfflineIsStickyUntilNewTaskStarts() {
        val m = machine()
        m.show()
        m.onEvent(PetEvent.DEVICE_OFFLINE)
        assertEquals(PetState.OFFLINE, m.state)

        // 离线期间进度不得伪造在线。
        assertEquals(PetState.OFFLINE, m.onEvent(PetEvent.TASK_PROGRESS))
        // 离线期间停止也不得伪造空闲。
        assertEquals(PetState.OFFLINE, m.onEvent(PetEvent.TASK_STOPPED))

        // 只有新任务开始才离开离线态。
        assertEquals(PetState.WORKING, m.onEvent(PetEvent.TASK_STARTED))
    }

    // ---------- 3. 隐藏态语义 ----------

    /**
     * 隐藏态收到事件不把窗口拉回来，只记录目标态；`show()` 时恢复。
     *
     * 对应「拖动贴边，拖到底部关闭当天显示」：隐藏期间任务仍在跑，再打开时要显示真实状态。
     */
    @Test
    fun eventsWhileHiddenAreRestoredOnShow() {
        val m = machine()
        // 初始 HIDDEN：事件不改变可见状态。
        assertEquals(PetState.HIDDEN, m.onEvent(PetEvent.TASK_STARTED))
        assertEquals(PetEvent.TASK_STARTED, m.lastEvent)

        // 显示后恢复隐藏期间的目标态，而不是无条件 IDLE。
        assertEquals(PetState.WORKING, m.show())
    }

    /** 隐藏期间没有事件时，`show()` 进入 IDLE。 */
    @Test
    fun showWithoutPendingEventEntersIdle() {
        val m = machine()
        assertEquals(PetState.IDLE, m.show())
    }

    /** `hide()` 只改显示态，不清理最近事件。 */
    @Test
    fun hideKeepsLastEvent() {
        val m = machine()
        m.show()
        m.onEvent(PetEvent.TASK_SUCCEEDED)
        assertEquals(PetState.HIDDEN, m.hide())
        assertEquals(PetEvent.TASK_SUCCEEDED, m.lastEvent)
        assertFalse(m.state.isVisible)
    }

    // ---------- 4. 低频回落（注入时钟，不用定时器） ----------

    /** SUCCESS 到期回落到 IDLE；未到期前保持 SUCCESS。 */
    @Test
    fun successFallsBackToIdleAfterHold() {
        val clock = FakeClock()
        val m = PetStateMachine(clock, successHoldMs = 1_000L, errorHoldMs = 3_000L)
        m.show()
        m.onEvent(PetEvent.TASK_SUCCEEDED)
        assertEquals(PetState.SUCCESS, m.state)

        clock.advance(500L)
        assertEquals(PetState.SUCCESS, m.refresh())

        clock.advance(500L)
        assertEquals(PetState.IDLE, m.refresh())
    }

    /** ERROR 到期回落到 IDLE。 */
    @Test
    fun errorFallsBackToIdleAfterHold() {
        val clock = FakeClock()
        val m = PetStateMachine(clock, successHoldMs = 1_000L, errorHoldMs = 2_000L)
        m.show()
        m.onEvent(PetEvent.TASK_FAILED)
        assertEquals(PetState.ERROR, m.state)

        clock.advance(1_999L)
        assertEquals(PetState.ERROR, m.refresh())

        clock.advance(1L)
        assertEquals(PetState.IDLE, m.refresh())
    }

    /** 等待态与离线态不会被 `refresh()` 回落：它们必须一直可见。 */
    @Test
    fun refreshNeverClearsWaitingOrOffline() {
        val clock = FakeClock()
        val m = PetStateMachine(clock)
        m.show()
        m.onEvent(PetEvent.CONFIRM_REQUIRED)
        clock.advance(60_000L)
        assertEquals(PetState.WAITING_CONFIRM, m.refresh())

        m.onEvent(PetEvent.DEVICE_OFFLINE)
        clock.advance(60_000L)
        assertEquals(PetState.OFFLINE, m.refresh())
    }

    /** 时钟回拨不得提前把停留态打断。 */
    @Test
    fun clockRollbackDoesNotEndHoldEarly() {
        val clock = FakeClock(now = 10_000L)
        val m = PetStateMachine(clock, successHoldMs = 1_000L, errorHoldMs = 1_000L)
        m.show()
        m.onEvent(PetEvent.TASK_SUCCEEDED)
        clock.now = 9_000L
        assertEquals(PetState.SUCCESS, m.refresh())
    }

    // ---------- 5. 快照 ----------

    /** 快照只含状态/最近事件/代号，不带任何正文内容。 */
    @Test
    fun snapshotCarriesNoBodyText() {
        val m = machine()
        m.show()
        m.onEvent(PetEvent.TASK_STARTED)
        val snapshot = m.snapshot(generation = 3L)
        assertEquals(PetState.WORKING, snapshot.state)
        assertEquals(PetEvent.TASK_STARTED, snapshot.lastEvent)
        assertEquals(3L, snapshot.generation)
    }

    // ---------- 6. 语音录音状态机（M13 用例 voice_push_to_talk_lifecycle） ----------

    /**
     * 按住说话固定序列：`IDLE → PRESSING → LISTENING → TRANSCRIBING → IDLE`。
     *
     * 对应 M13 矩阵 `voice_push_to_talk_lifecycle` 的 `expected_frame.state_sequence`；
     * `finish()` 是识别结果回来后的收尾，等价「release microphone」（识别器已 destroy）。
     */
    @Test
    fun pushToTalkFollowsFixedSequence() {
        val vm = VoiceStateMachine()
        assertEquals(VoiceState.IDLE, vm.state)

        assertTrue(vm.press())
        assertEquals(VoiceState.PRESSING, vm.state)
        assertTrue(vm.state.isActive)

        assertTrue(vm.listeningStarted())
        assertEquals(VoiceState.LISTENING, vm.state)

        assertTrue(vm.transcribing())
        assertEquals(VoiceState.TRANSCRIBING, vm.state)

        assertTrue(vm.finish())
        assertEquals(VoiceState.IDLE, vm.state)
        assertFalse(vm.state.isActive)
    }

    /** 录音只在按住期间存在：IDLE / STOPPED 都不是录音态。 */
    @Test
    fun recordingOnlyExistsBetweenPressAndRelease() {
        val vm = VoiceStateMachine()
        assertFalse(vm.state.isActive)
        vm.press()
        assertTrue(vm.state.isActive)
        vm.listeningStarted()
        vm.transcribing()
        assertTrue(vm.state.isActive)
        vm.finish()
        assertFalse(vm.state.isActive)
    }

    /** 重复按下不会启动第二个录音会话（状态停在 PRESSING，返回 false）。 */
    @Test
    fun repeatedPressIsRejected() {
        val vm = VoiceStateMachine()
        assertTrue(vm.press())
        assertFalse(vm.press())
        assertEquals(VoiceState.PRESSING, vm.state)
    }

    /** 非法迁移不改变状态：没按下就不能进入 LISTENING/TRANSCRIBING。 */
    @Test
    fun illegalTransitionsKeepState() {
        val vm = VoiceStateMachine()
        assertFalse(vm.listeningStarted())
        assertEquals(VoiceState.IDLE, vm.state)
        assertFalse(vm.transcribing())
        assertEquals(VoiceState.IDLE, vm.state)
        assertFalse(vm.finish())
        assertEquals(VoiceState.IDLE, vm.state)
    }

    /** 任何状态 → STOPPED：Activity 销毁、屏幕锁定、模式切换、任务停止都能随时释放麦克风。 */
    @Test
    fun anyStateCanStop() {
        // IDLE → STOPPED
        VoiceStateMachine().let { vm ->
            assertTrue(vm.stop())
            assertEquals(VoiceState.STOPPED, vm.state)
        }
        // PRESSING → STOPPED
        driveTo(VoiceState.PRESSING).let { vm ->
            assertTrue(vm.stop())
            assertEquals(VoiceState.STOPPED, vm.state)
        }
        // LISTENING → STOPPED
        driveTo(VoiceState.LISTENING).let { vm ->
            assertTrue(vm.stop())
            assertEquals(VoiceState.STOPPED, vm.state)
        }
        // TRANSCRIBING → STOPPED
        driveTo(VoiceState.TRANSCRIBING).let { vm ->
            assertTrue(vm.stop())
            assertEquals(VoiceState.STOPPED, vm.state)
        }
    }

    /** STOPPED 是终止态：重复 stop 返回 false，press 被拒，只有 reset 能回到 IDLE。 */
    @Test
    fun stoppedIsTerminalUntilReset() {
        val vm = driveTo(VoiceState.LISTENING)
        assertTrue(vm.stop())
        assertEquals(VoiceState.STOPPED, vm.state)

        assertFalse(vm.stop())
        assertEquals(VoiceState.STOPPED, vm.state)

        // 已停止实例不能再开录音（必须新建或显式 reset）。
        assertFalse(vm.press())
        assertEquals(VoiceState.STOPPED, vm.state)
        assertFalse(vm.state.isActive)

        assertTrue(vm.reset())
        assertEquals(VoiceState.IDLE, vm.state)
        assertTrue(vm.press())
    }

    /** 识别失败（空结果/识别器错误/权限不足）回到 IDLE，麦克风随之释放。 */
    @Test
    fun failureReturnsToIdle() {
        val vm = driveTo(VoiceState.LISTENING)
        assertTrue(vm.fail())
        assertEquals(VoiceState.IDLE, vm.state)
        assertFalse(vm.state.isActive)
    }

    /** 驱动状态机到指定状态（只走文档允许的路径）。 */
    private fun driveTo(target: VoiceState): VoiceStateMachine {
        val vm = VoiceStateMachine()
        when (target) {
            VoiceState.IDLE -> Unit
            VoiceState.PRESSING -> vm.press()
            VoiceState.LISTENING -> {
                vm.press()
                vm.listeningStarted()
            }
            VoiceState.TRANSCRIBING -> {
                vm.press()
                vm.listeningStarted()
                vm.transcribing()
            }
            VoiceState.STOPPED -> vm.stop()
        }
        assertEquals(target, vm.state)
        return vm
    }

    // ---------- 7. 语音失败种类不新增错误码 ----------

    /**
     * 语音失败种类只有一个映射到已登记错误码，且必须是 `PERMISSION_REQUIRED`。
     *
     * 硬约束「禁止新增错误码」：语音层失败不得往 `DeviceResponse.error_code` 里塞新字符串。
     */
    @Test
    fun voiceFailureKindsDoNotInventErrorCodes() {
        val mapped = VoiceFailureKind.entries.filter { it.commandErrorCode != null }
        assertEquals(listOf(VoiceFailureKind.PERMISSION_REQUIRED), mapped)
        assertEquals(CommandErrors.PERMISSION_REQUIRED, VoiceFailureKind.PERMISSION_REQUIRED.commandErrorCode)
        VoiceFailureKind.entries.filter { it != VoiceFailureKind.PERMISSION_REQUIRED }.forEach {
            assertNull("${it.name} 不应映射到命令错误码", it.commandErrorCode)
        }
        // 所有映射值必须是已登记错误码。
        mapped.forEach { assertTrue(it.commandErrorCode!! in CommandErrors.ALL) }
    }

    /** 没有录音权限时的失败文案明确表达「不启动录音」（产品验收项）。 */
    @Test
    fun permissionFailureExplainsNoRecording() {
        val failure = VoiceFailure(VoiceFailureKind.PERMISSION_REQUIRED)
        assertTrue(failure.message.contains("不会启动录音"))
        assertNull(failure.speechErrorCode)
    }
}
