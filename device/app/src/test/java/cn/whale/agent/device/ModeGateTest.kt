package cn.whale.agent.device

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * M01 固定测试（纯 JVM，不依赖 Android 运行时）。
 *
 * 对应 `docs/modules/M01-system-modes-development.md`「测试」五条：
 * 1. 默认值是 `REMOTE_B` → [defaultModeIsRemoteB] / [missingModeFallsBackToRemoteB]
 * 2. 本地模式下远程帧返回 `MODE_REJECTED` 且无副作用 → [remoteFrameInLocalModeIsRejectedWithoutInvokingRouter]
 * 3. 远程模式下本地 controller 返回 `MODE_REJECTED` → [localCommandInRemoteModeIsRejected]
 * 4. 模式切换停止旧任务并关闭旧连接 → [modeSwitchStopsTaskAndClosesOldConnection]
 * 5. 损坏的 SharedPreferences 不会切换到本地模式 → [corruptPreferencesNeverSelectLocalPet]
 */
class ModeGateTest {
    @Test fun signedRequestAllowsSmallClockSkewButRejectsFarFuture() {
        val clock = FakeClock(1_000_000L)
        val gate = remoteGate(InMemoryModeStore(AgentMode.REMOTE_B), RecordingExecutor(), clock = clock)
        assertTrue(gate.process(envelope(issuedAtMs = 1_001_200L, counter = 1L, idempotencyKey = "skew-ok")).executed)
        val future = gate.process(envelope(issuedAtMs = 1_006_000L, counter = 2L, idempotencyKey = "skew-future"))
        assertFalse(future.executed)
        assertEquals(CommandErrors.EXPIRED, future.response?.error_code)
    }
    // ---------- 测试替身 ----------

    /** 记录调用次数的假执行器；远程/本地两个入口都注入它，确保断言「没有第二执行器」。 */
    private class RecordingExecutor : CommandExecutor {
        val envelopes = ArrayList<DeviceEnvelope>()
        var response: DeviceResponse? = null

        val invoked: Boolean get() = envelopes.isNotEmpty()

        override fun execute(envelope: DeviceEnvelope): DeviceResponse {
            envelopes.add(envelope)
            return response ?: DeviceResponse(envelope.request_id, envelope.device_id, "ok")
        }
    }

    private class FakeEffectLog : EffectLog {
        val cache = HashMap<String, String>()

        override fun find(idempotencyKey: String): String? = cache[idempotencyKey]

        override fun save(idempotencyKey: String, responseJson: String, nowMs: Long) {
            cache[idempotencyKey] = responseJson
        }
    }

    private class FakeCounterLog : CounterLog {
        val counters = HashMap<String, Long>()

        override fun find(deviceId: String): Long? = counters[deviceId]

        override fun save(deviceId: String, counter: Long) {
            counters[deviceId] = counter
        }
    }

    /** 可推进的假时钟。 */
    private class FakeClock(var now: Long = 1_000_000L) : SessionClock {
        override fun nowMs(): Long = now
    }

    /** 模拟 SharedPreferences 原始存储：可写入缺失/非法/损坏值。 */
    private class FakeRawStorage(var raw: String?) : RawModeStorage {
        override fun readRaw(): String? = raw

        override fun writeRaw(value: String) {
            raw = value
        }
    }

    private class RecordingPendingActions : PendingActionQueue {
        var clearCount = 0

        override fun clear() {
            clearCount++
        }
    }

    private class RecordingRemoteIngress : RemoteIngress {
        var stopCount = 0
        var startCount = 0

        /** 模拟真实 WebSocket 连接状态：关闭后必须为 false。 */
        var socketOpen = true

        override fun stopRemoteIngress() {
            stopCount++
            socketOpen = false
        }

        override fun startRemoteIngress() {
            startCount++
            socketOpen = true
        }
    }

    private class RecordingTaskStopper : TaskStopper {
        var stopCount = 0

        override fun stopCurrentTask() {
            stopCount++
        }
    }

    private fun envelope(
        command: String = "device.app.list",
        signature: String = "test-signature",
        payload: JsonObject = buildJsonObject { },
        counter: Long = 1L,
        idempotencyKey: String = "m01-001",
        issuedAtMs: Long = 0L,
        deadlineMs: Int = 30_000,
        requestId: String = "req-m01-001",
        deviceId: String = "device-b-01",
    ): DeviceEnvelope = DeviceEnvelope(
        protocol_version = 1,
        request_id = requestId,
        device_id = deviceId,
        command = command,
        deadline_ms = deadlineMs,
        idempotency_key = idempotencyKey,
        payload = payload,
        counter = counter,
        issued_at_ms = issuedAtMs,
        signature = signature,
    )

    private fun remoteGate(
        modeStore: ModeStore,
        executor: CommandExecutor,
        effects: EffectLog = FakeEffectLog(),
        counters: CounterLog = FakeCounterLog(),
        clock: SessionClock = FakeClock(),
        log: (String) -> Unit = {},
    ): RemoteModeGate = RemoteModeGate(modeStore, executor, effects, counters, clock, log)

    private fun localGate(
        modeStore: ModeStore,
        session: LocalSessionGate,
        executor: CommandExecutor,
        riskConfirmation: RiskConfirmation? = null,
        riskPolicy: RiskPolicy = RiskPolicies.INTERIM_FALLBACK,
    ): LocalModeGate = LocalModeGate(modeStore, session, executor, riskConfirmation, riskPolicy)

    // ---------- 测试 1：默认值是 REMOTE_B ----------

    @Test
    fun defaultModeIsRemoteB() {
        assertEquals(AgentMode.REMOTE_B, AgentMode.DEFAULT)
        assertEquals(AgentMode.REMOTE_B, AgentMode.parseOrRemote(null))
        assertEquals(AgentMode.REMOTE_B, AgentMode.parseOrRemote(""))
        assertEquals(AgentMode.REMOTE_B, AgentMode.parseOrRemote("   "))
        assertEquals(AgentMode.REMOTE_B, InMemoryModeStore().get())

        // 空存储（从未写入过 mode）也必须回退 REMOTE_B。
        val store = RawModeStore(FakeRawStorage(null))
        assertEquals(AgentMode.REMOTE_B, store.get())
    }

    @Test
    fun missingModeFallsBackToRemoteB() {
        assertEquals(AgentMode.REMOTE_B, RawModeStore(FakeRawStorage("")).get())
        assertEquals(AgentMode.REMOTE_B, AgentMode.parseOrRemote("REMOTE_A"))
        // 只有两个字面值与枚举名完全相等时才改变结果。
        assertEquals(AgentMode.LOCAL_PET, AgentMode.parseOrRemote("LOCAL_PET"))
        assertEquals(AgentMode.REMOTE_B, AgentMode.parseOrRemote("local_pet"))
    }

    // ---------- 测试 2：本地模式下远程帧 MODE_REJECTED 且无副作用 ----------

    @Test
    fun remoteFrameInLocalModeIsRejectedWithoutInvokingRouter() {
        val modeStore = InMemoryModeStore(AgentMode.LOCAL_PET)
        val executor = RecordingExecutor()
        val effects = FakeEffectLog()
        val counters = FakeCounterLog()
        val gate = remoteGate(modeStore, executor, effects, counters)

        val frame = envelope(
            command = "device.app.launch",
            payload = buildJsonObject { put("package_name", "com.android.settings") },
        )
        val result = gate.process(frame)

        assertEquals("denied", result.response?.status)
        assertEquals(CommandErrors.MODE_REJECTED, result.response?.error_code)
        assertFalse("router 不得被调用", result.executed)
        assertFalse("router 不得被调用", executor.invoked)

        // 无副作用：幂等表与 counter 表都不能被写入。
        assertTrue(effects.cache.isEmpty())
        assertTrue(counters.counters.isEmpty())
    }

    @Test
    fun remoteFrameInLocalModeIsRejectedEvenWhenModeChangesLate() {
        val modeStore = InMemoryModeStore(AgentMode.LOCAL_PET)
        val executor = RecordingExecutor()
        val gate = remoteGate(modeStore, executor)

        val first = gate.process(envelope(idempotencyKey = "m01-early"))
        assertEquals(CommandErrors.MODE_REJECTED, first.response?.error_code)

        modeStore.set(AgentMode.LOCAL_PET)
        val second = gate.process(envelope(idempotencyKey = "m01-late"))
        assertEquals(CommandErrors.MODE_REJECTED, second.response?.error_code)
        assertFalse(executor.invoked)
    }

    // ---------- 测试 3：远程模式下本地调用 MODE_REJECTED ----------

    @Test
    fun localCommandInRemoteModeIsRejected() {
        val modeStore = InMemoryModeStore(AgentMode.REMOTE_B)
        val executor = RecordingExecutor()
        val session = LocalSessionGate(FakeClock()).apply { unlock() }
        val gate = localGate(modeStore, session, executor)

        val command = envelope(
            command = "device.app.launch",
            payload = buildJsonObject { put("package_name", "com.android.settings") },
        )
        val result = gate.execute(command, LocalCommandSource.IN_APP_ACTIVITY)

        assertEquals("denied", result.response?.status)
        assertEquals(CommandErrors.MODE_REJECTED, result.response?.error_code)
        assertFalse(result.executed)
        assertFalse(executor.invoked)
    }

    // ---------- 测试 4：模式切换停止旧任务并关闭旧连接 ----------

    @Test
    fun modeSwitchStopsTaskAndClosesOldConnection() {
        val modeStore = InMemoryModeStore(AgentMode.REMOTE_B)
        val stopper = RecordingTaskStopper()
        val pending = RecordingPendingActions()
        val ingress = RecordingRemoteIngress()
        val systemReady = PermissionSnapshot(true, true, true)
        val coordinator = ModeSwitchCoordinator(
            modeStore = modeStore,
            taskStopper = stopper,
            pendingActions = pending,
            remoteIngress = ingress,
            permissionChecker = PermissionChecker { systemReady },
        )

        assertTrue("切换前远程入口是打开的", ingress.socketOpen)
        val result = coordinator.requestSwitch(AgentMode.LOCAL_PET, ModeSwitchSource.SETTINGS_PAGE_CLICK)

        assertTrue(result.changed)
        assertEquals(AgentMode.LOCAL_PET, modeStore.get())
        assertTrue("必须停止旧任务", result.taskStopped)
        assertEquals(1, stopper.stopCount)
        assertTrue("必须清空未执行动作", result.pendingActionsCleared)
        assertEquals(1, pending.clearCount)
        assertTrue("必须关闭旧入口", result.remoteIngressClosed)
        assertEquals(1, ingress.stopCount)
        assertFalse("本地模式必须没有 WSS", ingress.socketOpen)
        assertFalse("本地模式不得启动服务", result.remoteIngressStarted)
        assertEquals(0, ingress.startCount)
        assertTrue("必须重新检查权限", result.permissions != null)
        assertTrue(result.localPetReady)
    }

    @Test
    fun switchBackToRemoteRestartsServiceFromSettingsPage() {
        val modeStore = InMemoryModeStore(AgentMode.LOCAL_PET)
        val ingress = RecordingRemoteIngress().apply { socketOpen = false }
        val coordinator = ModeSwitchCoordinator(
            modeStore = modeStore,
            taskStopper = RecordingTaskStopper(),
            pendingActions = RecordingPendingActions(),
            remoteIngress = ingress,
            permissionChecker = PermissionChecker { PermissionSnapshot(false, false, false) },
        )

        val result = coordinator.requestSwitch(AgentMode.REMOTE_B, ModeSwitchSource.SETTINGS_PAGE_CLICK)

        assertTrue(result.changed)
        assertEquals(AgentMode.REMOTE_B, modeStore.get())
        assertTrue(result.remoteIngressStarted)
        assertEquals(1, ingress.startCount)
    }

    @Test
    fun nonSettingsSourceCannotSwitchMode() {
        val modeStore = InMemoryModeStore(AgentMode.REMOTE_B)
        val stopper = RecordingTaskStopper()
        val pending = RecordingPendingActions()
        val ingress = RecordingRemoteIngress()
        val coordinator = ModeSwitchCoordinator(
            modeStore = modeStore,
            taskStopper = stopper,
            pendingActions = pending,
            remoteIngress = ingress,
            permissionChecker = PermissionChecker { PermissionSnapshot(true, true, true) },
        )

        // 模型输出、语音、QQ 消息、群消息、外部 Intent 一律不得切换模式。
        for (source in listOf(
            ModeSwitchSource.MODEL_OUTPUT,
            ModeSwitchSource.VOICE_COMMAND,
            ModeSwitchSource.QQ_MESSAGE,
            ModeSwitchSource.GROUP_MESSAGE,
            ModeSwitchSource.EXTERNAL_INTENT,
        )) {
            val result = coordinator.requestSwitch(AgentMode.LOCAL_PET, source)
            assertFalse("${source.name} 不得切换模式", result.changed)
            assertEquals(AgentMode.REMOTE_B, modeStore.get())
            assertNull(result.rejectionCode)
        }
        // 也没有产生任何副作用。
        assertEquals(0, stopper.stopCount)
        assertEquals(0, pending.clearCount)
        assertEquals(0, ingress.stopCount)
        assertEquals(0, ingress.startCount)
    }

    @Test
    fun remoteGateRejectsFrameOnceModeSwitchedToLocal() {
        val modeStore = InMemoryModeStore(AgentMode.REMOTE_B)
        val executor = RecordingExecutor()
        val gate = remoteGate(modeStore, executor)

        // 切换前：允许执行。
        val before = gate.process(envelope(idempotencyKey = "m01-before", counter = 1L))
        assertTrue(before.executed)

        // 设置页切换到本地模式。
        val coordinator = ModeSwitchCoordinator(
            modeStore = modeStore,
            taskStopper = RecordingTaskStopper(),
            pendingActions = RecordingPendingActions(),
            remoteIngress = RecordingRemoteIngress(),
            permissionChecker = PermissionChecker { PermissionSnapshot(true, true, true) },
        )
        coordinator.requestSwitch(AgentMode.LOCAL_PET, ModeSwitchSource.SETTINGS_PAGE_CLICK)

        // 切换后：旧入口无法执行动作。
        val after = gate.process(envelope(idempotencyKey = "m01-after", counter = 2L))
        assertEquals(CommandErrors.MODE_REJECTED, after.response?.error_code)
        assertFalse(after.executed)
        assertEquals(1, executor.envelopes.size)
    }

    // ---------- 测试 5：损坏的 SharedPreferences 不会切到本地 ----------

    @Test
    fun corruptPreferencesNeverSelectLocalPet() {
        // 后缀损坏：像是未来版本或手改错误写出的值。
        assertEquals(AgentMode.REMOTE_B, RawModeStore(FakeRawStorage("LOCAL_PET_XXXX")).get())
        // 非法枚举名。
        assertEquals(AgentMode.REMOTE_B, RawModeStore(FakeRawStorage("LOCAL")).get())
        // JSON 残留 / 编码损坏。
        assertEquals(AgentMode.REMOTE_B, RawModeStore(FakeRawStorage("{\"mode\":\"LOCAL_PET\"}")).get())
        assertEquals(AgentMode.REMOTE_B, RawModeStore(FakeRawStorage("\u0000LOCAL_PET")).get())
        // 读取抛异常（类型写坏）也必须回退 REMOTE_B。
        val throwingStorage = object : RawModeStorage {
            override fun readRaw(): String? = throw IllegalStateException("SharedPreferences type mismatch")
            override fun writeRaw(value: String) = Unit
        }
        assertEquals(AgentMode.REMOTE_B, RawModeStore(throwingStorage).get())
    }

    @Test
    fun corruptPreferencesDoNotAuthorizeRemoteFramesEither() {
        // 更好：损坏值退化为 REMOTE_B，因此远程入口仍按远程模式判定，本地入口仍然拒绝。
        val store = RawModeStore(FakeRawStorage("LOCAL_PET_XXXX"))
        val executor = RecordingExecutor()
        val session = LocalSessionGate(FakeClock()).apply { unlock() }
        val local = localGate(store, session, executor).execute(
            envelope(),
            LocalCommandSource.IN_APP_ACTIVITY,
        )

        assertEquals(CommandErrors.MODE_REJECTED, local.response?.error_code)
        assertFalse(executor.invoked)
        assertEquals(AgentMode.REMOTE_B, store.get())
    }

    // ---------- 其余 M01 边界：闸门顺序、签名、会话、高风险 ----------

    @Test
    fun decodeFailureIsDiscardedAndLoggedWithoutReply() {
        val logs = ArrayList<String>()
        val gate = remoteGate(
            modeStore = InMemoryModeStore(AgentMode.REMOTE_B),
            executor = RecordingExecutor(),
            log = { logs.add(it) },
        )
        val result = gate.processRaw("{not-json")

        assertTrue(result.isDiscarded)
        assertNull(result.response)
        assertNull(result.cachedJson)
        assertEquals(CommandErrors.PROTOCOL_DECODE_FAILED, result.errorCode)
        assertTrue(logs.any { it.contains(CommandErrors.PROTOCOL_DECODE_FAILED) })
    }

    @Test
    fun blankSignatureIsRejectedBeforeModeCheck() {
        val modeStore = InMemoryModeStore(AgentMode.LOCAL_PET)
        val executor = RecordingExecutor()
        val gate = remoteGate(modeStore, executor)

        val result = gate.process(envelope(signature = ""))

        // 顺序固定：signature 检查在模式检查之前。
        assertEquals(CommandErrors.SIGNATURE_REQUIRED, result.response?.error_code)
        assertFalse(result.executed)
        assertFalse(executor.invoked)
    }

    @Test
    fun replayAndExpiredAreRejectedBeforeExecution() {
        val modeStore = InMemoryModeStore(AgentMode.REMOTE_B)
        val executor = RecordingExecutor()
        val clock = FakeClock(now = 1_000_000L)
        val counters = FakeCounterLog().apply { save("device-b-01", 5L) }
        val gate = remoteGate(modeStore, executor, counters = counters, clock = clock)

        val replay = gate.process(envelope(counter = 5L, idempotencyKey = "m01-replay"))
        assertEquals(CommandErrors.REPLAY, replay.response?.error_code)
        assertFalse(executor.invoked)

        val expired = gate.process(
            envelope(
                idempotencyKey = "m01-expired",
                counter = 6L,
                issuedAtMs = 0L,
                deadlineMs = 50,
            ),
        )
        assertEquals(CommandErrors.EXPIRED, expired.response?.error_code)
        assertFalse(executor.invoked)
    }

    @Test
    fun idempotentReplayReturnsCachedResponseWithoutExecuting() {
        val modeStore = InMemoryModeStore(AgentMode.REMOTE_B)
        val executor = RecordingExecutor()
        val effects = FakeEffectLog()
        val gate = remoteGate(modeStore, executor, effects = effects)

        val first = gate.process(envelope(idempotencyKey = "m01-idem", counter = 1L))
        assertTrue(first.executed)

        val second = gate.process(envelope(idempotencyKey = "m01-idem", counter = 2L))
        assertEquals(1, executor.envelopes.size)
        assertFalse(second.executed)
        assertEquals(effects.cache["m01-idem"], second.cachedJson)
    }

    @Test
    fun unlockedLocalSessionIsRequiredForLocalCommands() {
        val modeStore = InMemoryModeStore(AgentMode.LOCAL_PET)
        val executor = RecordingExecutor()
        val clock = FakeClock(now = 1_000_000L)
        val session = LocalSessionGate(clock)
        val gate = localGate(modeStore, session, executor)
        val command = envelope()

        val locked = gate.execute(command, LocalCommandSource.IN_APP_ACTIVITY)
        assertEquals(CommandErrors.NEEDS_UNLOCK, locked.response?.error_code)
        assertFalse(executor.invoked)

        session.unlock()
        val allowed = gate.execute(command, LocalCommandSource.IN_APP_ACTIVITY)
        assertTrue(allowed.executed)

        // 超时后必须重新解锁。
        clock.now += LocalSessionGate.DEFAULT_TIMEOUT_MS + 1
        val expired = gate.execute(command, LocalCommandSource.IN_APP_ACTIVITY)
        assertEquals(CommandErrors.NEEDS_UNLOCK, expired.response?.error_code)
        assertFalse(session.isUnlocked)
    }

    @Test
    fun outOfProcessSourceIsRejected() {
        val modeStore = InMemoryModeStore(AgentMode.LOCAL_PET)
        val executor = RecordingExecutor()
        val session = LocalSessionGate(FakeClock()).apply { unlock() }
        val gate = localGate(modeStore, session, executor)

        for (source in listOf(LocalCommandSource.IMPLICIT_INTENT, LocalCommandSource.EXTERNAL_APP, LocalCommandSource.NETWORK_FRAME)) {
            val result = gate.execute(envelope(), source)
            assertEquals(CommandErrors.PERMISSION_REQUIRED, result.response?.error_code)
            assertFalse(result.executed)
        }
        assertFalse(executor.invoked)
    }

    @Test
    fun highRiskCommandRequiresConfirmationBeforeExecution() {
        val modeStore = InMemoryModeStore(AgentMode.LOCAL_PET)
        val executor = RecordingExecutor()
        val session = LocalSessionGate(FakeClock()).apply { unlock() }
        val seen = ArrayList<String>()
        val gate = localGate(
            modeStore = modeStore,
            session = session,
            executor = executor,
            riskConfirmation = RiskConfirmation { command, summary ->
                seen.add("$command|$summary")
                false
            },
        )
        val command = envelope(
            command = "device.sms.send",
            payload = buildJsonObject {
                put("number", "10086")
                put("text", "hello")
            },
        )

        val denied = gate.execute(command, LocalCommandSource.IN_APP_ACTIVITY)
        assertEquals(CommandErrors.ACTION_REJECTED, denied.response?.error_code)
        assertFalse(executor.invoked)
        assertEquals("device.sms.send|number, text", seen.single())

        // 用户确认后才执行。
        val approving = localGate(
            modeStore = modeStore,
            session = session,
            executor = executor,
            riskConfirmation = RiskConfirmation { _, _ -> true },
        )
        val allowed = approving.execute(command, LocalCommandSource.IN_APP_ACTIVITY)
        assertTrue(allowed.executed)
        assertEquals(1, executor.envelopes.size)
    }

    @Test
    fun ordinaryCommandSkipsConfirmation() {
        val modeStore = InMemoryModeStore(AgentMode.LOCAL_PET)
        val executor = RecordingExecutor()
        val session = LocalSessionGate(FakeClock()).apply { unlock() }
        val gate = localGate(
            modeStore = modeStore,
            session = session,
            executor = executor,
            riskConfirmation = null,
            riskPolicy = RiskPolicies.NONE,
        )

        val result = gate.execute(envelope(), LocalCommandSource.IN_APP_ACTIVITY)
        assertTrue(result.executed)
    }

    @Test
    fun riskPolicyIsInjectableForM04() {
        // M04 接入时可以整体替换清单：这里让所有命令都视为高风险，M01 顺序保持不变。
        val modeStore = InMemoryModeStore(AgentMode.LOCAL_PET)
        val executor = RecordingExecutor()
        val session = LocalSessionGate(FakeClock()).apply { unlock() }
        val gate = localGate(
            modeStore = modeStore,
            session = session,
            executor = executor,
            riskConfirmation = null,
            riskPolicy = RiskPolicy { true },
        )

        val result = gate.execute(envelope(), LocalCommandSource.IN_APP_ACTIVITY)
        assertEquals(CommandErrors.PERMISSION_REQUIRED, result.response?.error_code)
        assertFalse(executor.invoked)
    }
}
