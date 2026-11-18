package cn.whale.agent.device

/**
 * M01 模式切换协调器（纯逻辑，Android 效果通过接口注入）。
 *
 * 固定动作顺序（`docs/modules/M01-system-modes-product.md`「模式互斥规则」）：
 * 1. 停止当前任务；
 * 2. 清空未执行动作；
 * 3. 关闭旧入口（切到 `LOCAL_PET` 必须没有 WSS；切回 `REMOTE_B` 由前台设置页启动服务）；
 * 4. 重新检查权限。
 *
 * 切换入口只能有一个：[requestSwitch]。它只接受 [ModeSwitchSource.SETTINGS_PAGE_CLICK]，
 * 模型输出、语音内容、QQ 私聊、群消息、外部 Intent 一律拒绝且**不改变存储模式**。
 */

/** 模式切换来源。只有设置页的明确用户点击合法。 */
enum class ModeSwitchSource {
    SETTINGS_PAGE_CLICK,

    // 以下来源全部非法：不得触发模式切换。
    MODEL_OUTPUT,
    VOICE_COMMAND,
    QQ_MESSAGE,
    GROUP_MESSAGE,
    EXTERNAL_INTENT,
    LOCAL_PET_OVERLAY,
    ;

    val isSettingsClick: Boolean get() = this == SETTINGS_PAGE_CLICK
}

/** 停止当前正在执行的任务（取消进行中的命令与桌宠动作）。 */
fun interface TaskStopper {
    fun stopCurrentTask()
}

/** 未执行动作队列：切换模式时必须清空，禁止旧模式的动作在新模式下补执行。 */
interface PendingActionQueue {
    fun clear()
}

/** 远程入口开关：切本地停服务、切远程从前台设置页启动。 */
interface RemoteIngress {
    /** 关闭远程 WSS 入口，确保本地模式没有 WebSocket 连接。 */
    fun stopRemoteIngress()

    /** 由前台设置页启动远程服务（后台不得自行拉起）。 */
    fun startRemoteIngress()
}

/** 权限检查结果；`LOCAL_PET` 需要无障碍、悬浮窗、录音三项。 */
data class PermissionSnapshot(
    val accessibilityEnabled: Boolean,
    val overlayGranted: Boolean,
    val recordAudioGranted: Boolean,
) {
    val localPetReady: Boolean get() = accessibilityEnabled && overlayGranted && recordAudioGranted

    /** 面向上层展示的缺失项描述；全部就绪时为空列表。 */
    val missingForLocalPet: List<String>
        get() = buildList {
            if (!accessibilityEnabled) add("无障碍服务")
            if (!overlayGranted) add("悬浮窗")
            if (!recordAudioGranted) add("录音")
        }
}

/** 权限重新检查：切换后必须重新读取，不能沿用切换前的缓存。 */
fun interface PermissionChecker {
    fun recheck(): PermissionSnapshot
}

/** 切换结果，供设置页渲染状态与文案。 */
data class ModeSwitchResult(
    val changed: Boolean,
    val mode: AgentMode,
    val taskStopped: Boolean = false,
    val pendingActionsCleared: Boolean = false,
    val remoteIngressClosed: Boolean = false,
    val remoteIngressStarted: Boolean = false,
    val permissions: PermissionSnapshot? = null,
    val rejectionCode: String? = null,
) {
    val localPetReady: Boolean get() = permissions?.localPetReady == true
}

/**
 * 模式切换的唯一入口。
 *
 * @param modeStore 生产实现为 [AgentModeStore]；测试可注入 [InMemoryModeStore]。
 * @param taskStopper 停止当前任务。
 * @param pendingActions 清空未执行动作。
 * @param remoteIngress 关闭/启动远程入口。
 * @param permissionChecker 切换后重新检查权限。
 * @param logger 本地日志（记录被拒绝的非设置页来源；不向外网发送）。
 */
class ModeSwitchCoordinator(
    private val modeStore: ModeStore,
    private val taskStopper: TaskStopper,
    private val pendingActions: PendingActionQueue,
    private val remoteIngress: RemoteIngress,
    private val permissionChecker: PermissionChecker,
    private val logger: (String) -> Unit = {},
) {
    /**
     * 请求切换模式。这是**唯一**允许改变 [ModeStore] 的方法。
     *
     * 非设置页来源不会改变模式，也不会停止任务或关闭连接（避免被模型/语音/消息制造副作用），
     * 返回 [ModeSwitchResult] 且 `changed == false`。
     */
    fun requestSwitch(target: AgentMode, source: ModeSwitchSource): ModeSwitchResult {
        if (!source.isSettingsClick) {
            // 文档：模式切换不能由 QQ 客人、群消息、语音内容或模型输出触发。
            // 这里不新造错误码，只拒绝并记本地日志；模式保持原值。
            logger("mode switch rejected: source=${source.name} is not a settings page click")
            return ModeSwitchResult(changed = false, mode = modeStore.get())
        }

        val current = modeStore.get()
        if (current == target) {
            // 同模式重复点击：不重复做停止/关闭副作用，但仍重新检查权限以刷新界面。
            return ModeSwitchResult(changed = false, mode = current, permissions = permissionChecker.recheck())
        }

        // 1. 停止当前任务。
        runCatching { taskStopper.stopCurrentTask() }

        // 2. 清空未执行动作。
        pendingActions.clear()

        // 3. 关闭旧入口 / 打开新入口。
        //    切到本地：必须关闭远程 WSS 入口，保证本地模式没有连接。
        //    切回远程：旧入口是本应用内的桌宠/本地会话，由 taskStopper 与界面收尾，
        //    远程服务只由下面的「前台设置页启动」重新拉起。
        val closed = if (target.isLocal) {
            runCatching { remoteIngress.stopRemoteIngress() }.isSuccess
        } else {
            // 远程模式本身不持有 WSS 之外的旧入口，此处置为 true 表示「旧入口已确认关闭」。
            true
        }

        // 4. 写存储（先关入口再持久化，避免旧入口按新模式误判）。
        modeStore.set(target)

        var started = false
        if (target.isRemote) {
            started = runCatching { remoteIngress.startRemoteIngress() }.isSuccess
        }

        // 5. 重新检查权限（本地模式需要无障碍、悬浮窗、录音；远程模式依赖配对而非这三项）。
        val permissions = runCatching { permissionChecker.recheck() }.getOrNull()

        return ModeSwitchResult(
            changed = true,
            mode = target,
            taskStopped = true,
            pendingActionsCleared = true,
            remoteIngressClosed = closed,
            remoteIngressStarted = started,
            permissions = permissions,
        )
    }
}

/**
 * 生产用远程入口实现。
 *
 * - [stopRemoteIngress]：`stopService(DeviceConnectionService)`，保证本地模式没有 WSS。
 * - [startRemoteIngress]：由**前台设置页**调用 `startService`；不使用 `startForegroundService`
 *   在后台拉起，也不从网络/模型侧触发。
 *
 * 这两个动作只做 Android 绑定，判定逻辑全部在上面的纯逻辑里。
 */
class AndroidRemoteIngress(
    private val stopService: () -> Unit,
    private val startService: () -> Unit,
) : RemoteIngress {
    override fun stopRemoteIngress() {
        stopService()
    }

    override fun startRemoteIngress() {
        startService()
    }
}
