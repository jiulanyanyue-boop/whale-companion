package cn.whale.agent.device

import android.content.Context

/**
 * M01 本地入口（桌宠、应用内按钮、按住说话）。
 *
 * 固定检查全部委托给 [LocalModeGate]：模式 `LOCAL_PET` → 进程内来源 → 已解锁会话 →
 * 高风险确认 → 唯一执行器 [CommandRouter.execute]。
 *
 * 本类**不接受网络帧**，也不暴露 `exported` 的 Intent 接口（见 AndroidManifest 中
 * 只有 `MainActivity` 是 exported 的 launcher 入口，没有本地执行 Activity/Service）。
 * 禁止使用 ADB、shell、动态代码或任意 HTTP 作为本地入口。
 *
 * 高风险确认的默认接线：M04 已落地 [UserGate] + [OwnerRiskConfirmation] + [OwnerRiskPolicy]，
 * 因此本类默认就用 M04 的生产实现，而不是 M01 的过渡兜底 [RiskPolicies.INTERIM_FALLBACK]。
 * 测试可以显式注入替身；生产路径不需要调用方记得传参，避免「忘了传就少一道确认」。
 */
class LocalPetController(
    context: Context,
    private val session: LocalSessionGate,
    riskConfirmation: RiskConfirmation? = null,
    riskPolicy: RiskPolicy? = null,
    /** 本地确认卡渲染器；默认无 UI（由 M09 桌宠 overlay 或 Activity 注入具体实现）。 */
    confirmRenderer: ConfirmCardRenderer = NoopConfirmCardRenderer,
) {
    private val modeStore: ModeStore = AgentModeStore(context)
    // M08：媒体能力经 MediaWiringImpl 注入；装配失败为 null → media.* 保持 NOT_WIRED。
    private val router = CommandRouter(context, MediaWiringImpl.android(context))

    /** M04 确认状态机：默认实例，供 [OwnerRiskConfirmation] 与本类共用同一份批准状态。 */
    private val userGate: UserGate = UserGate(renderer = confirmRenderer)

    /**
     * M08 媒体分享的确认来源。
     *
     * 接上以后 `device.media.share` 才可能真的放行——否则 `MediaWiringImpl.share`
     * 会 fail-closed 返回 `ACTION_REJECTED`（这是安全底线，不是缺陷）。
     *
     * 与 [userGate] 共用**同一个实例**：这样「弹确认卡」与「判批准」看的是同一份状态，
     * 不会出现「卡上点了确认、分享却认为没确认」的错位。
     */
    private val shareApproval: MediaShareConfirmation = MediaShareApprovals.attachFrom(userGate)

    /** 高风险命令的最终守卫：没有用户批准就不放行。 */
    private val confirmation: RiskConfirmation = riskConfirmation ?: OwnerRiskConfirmation(userGate)

    /** 高风险清单归属 M04；默认用 [OwnerRiskPolicy]，不再用 M01 的过渡兜底。 */
    private val policy: RiskPolicy = riskPolicy ?: OwnerRiskPolicy

    private val gate = LocalModeGate(
        mode = modeStore,
        session = session,
        executor = RouterCommandExecutor(router),
        riskConfirmation = confirmation,
        riskPolicy = policy,
    )

    /** 桌宠/应用内按钮触发的显式点击。 */
    fun execute(envelope: DeviceEnvelope): DeviceResponse = executeFrom(envelope, LocalCommandSource.IN_APP_ACTIVITY)

    /** 按住说话产生的本地语音文本转成的信封。 */
    fun executeFromVoice(envelope: DeviceEnvelope): DeviceResponse = executeFrom(envelope, LocalCommandSource.LOCAL_VOICE)

    /**
     * 带来源的本地调用。
     *
     * 非进程内来源（隐式 Intent、外部应用、网络帧）会被 [LocalModeGate] 拒绝，
     * 不会到达 [CommandRouter.execute]。
     */
    fun executeFrom(envelope: DeviceEnvelope, source: LocalCommandSource): DeviceResponse {
        val result = gate.execute(envelope, source)
        return result.response ?: noResponse(envelope, result.errorCode)
    }

    /** 用户显式解锁本地交互会话（由设置页/桌宠界面的明确点击调用）。 */
    fun unlockSession() {
        session.unlock()
    }

    /** 用户结束本地会话或界面离开前台时调用。 */
    fun lockSession() {
        session.lock()
        // 会话结束即作废全部未使用的确认批准，防止旧批准被下一次任务借用。
        clearApprovals()
    }

    /** 模式切换或任务终止时调用：清空全部确认批准。 */
    fun clearApprovals() {
        (confirmation as? OwnerRiskConfirmation)?.clearApprovals()
        // 分享批准同样作废：否则「上次没用的分享批准」会被下一次任务借用。
        shareApproval.clear()
    }

    /** 无障碍注入手势前后必须成对调用；注入期间确认卡不可点击。 */
    fun setInjecting(injecting: Boolean) {
        (confirmation as? OwnerRiskConfirmation)?.setInjecting(injecting)
    }

    /** 供 UI 查询当前确认卡是否可点击（注入期间恒为 false）。 */
    fun isConfirmClickable(requestId: String): Boolean = userGate.isClickable(requestId)

    /**
     * 为一次媒体分享请求「弹出确认卡」并把它绑定到具体素材与目标。
     *
     * 这是 `device.media.share` 真正可用的前提：`MediaWiringImpl.share` 会问
     * [MediaShareApprovals]「这份素材分享到这个目标，用户批准过吗」——
     * 没有绑定就一律视为未批准（fail-closed）。
     *
     * @param requestId 本次确认的标识（与 `userGate.request(...)` 用同一个）
     * @param assetId 要分享的素材
     * @param target 分享目标（例如外部社交应用）
     * @return 绑定是否建立（`false` 表示已存在冲突绑定或参数非法，此时分享仍会被拒）
     */
    fun bindShareConfirmation(requestId: String, assetId: String, target: String): Boolean =
        shareApproval.bind(requestId = requestId, assetId = assetId, target = target)

    /** 宿主销毁时撤销分享批准登记（避免已销毁的宿主还被认为是有效批准来源）。 */
    fun detachShareApprovals() {
        MediaShareApprovals.detach()
    }

    private fun noResponse(envelope: DeviceEnvelope, code: String?): DeviceResponse =
        DeviceResponse(
            envelope.request_id,
            envelope.device_id,
            "denied",
            code ?: CommandErrors.ACTION_REJECTED,
            "local command was rejected",
        )
}
