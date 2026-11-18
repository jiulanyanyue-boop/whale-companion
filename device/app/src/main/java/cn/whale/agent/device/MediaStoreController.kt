package cn.whale.agent.device

import android.content.Context
import android.content.Intent
import android.net.Uri

/**
 * M08 媒体素材表与分享控制器（`device.media.share` 的实现层，兼任务资产表维护）。
 *
 * 规范来源：`docs/modules/M08-media-product.md`「分享」与「限制」。
 *
 * 固定约束：
 * - 分享前必须同时给出**目标**与**素材元数据**（`asset_id`、mime、size、sha256）；
 * - 外发到 QQ / 社交软件属于**高风险媒体动作**，必须经 `LOCAL_PET` 的风险确认
 *   （`ConfirmLevelPolicy`）或远程模式的既定确认流程，本控制器只标记
 *   [SharePlan.requiresConfirmation]，**自己从不直接外发**；
 * - AI 不能从文件系统任意读取路径：本类只接受已登记 `asset_id`，
 *   由 [AssetStore.fileOf] 复受控路径；没有「传路径」的入口；
 * - 任务资产表在任务结束时清理（[clearTask]），临时素材 24 小时后清理
 *   （[sweepExpired]）；
 * - **不**扫描完整相册生成索引、**不**用 WebSocket 传大文件。
 */
class MediaStoreController(
    private val context: Context,
    private val assets: AssetStore,
    /**
     * 把已登记素材映射成可外发的 `content://` URI。
     *
     * 由宿主注入：本模块不自行声明 `FileProvider`（`AndroidManifest.xml` 由 M06 集成方维护）。
     * 传 null 时分享计划会明确失败，而不是把 `file://` 真实路径塞进 Intent。
     */
    private val contentUriProvider: ((String) -> Uri?)? = null,
) {
    /** 分享计划：给确认 UI 用的完整信息，不含真实路径。 */
    data class SharePlan(
        val evidence: Map<String, String>,
        val target: String,
        val mime: String,
        /** 外发到 QQ/社交软件等第三方：高风险，必须确认。 */
        val requiresConfirmation: Boolean,
        val intent: Intent,
    )

    sealed interface ShareOutcome {
        data class Ready(val plan: SharePlan) : ShareOutcome

        data class Failed(val code: String, val message: String) : ShareOutcome
    }

    /** 一次分享请求；字段完全对应 `device.media.share` 的 payload。 */
    data class Request(
        val assetId: String,
        val target: String = "",
        val confirmed: Boolean = false,
    )

    /**
     * 准备分享。
     *
     * @param request `asset_id` 必须已登记；`target` 为空表示交给系统分享面板选择目标。
     * @return [ShareOutcome.Ready] 只表示「计划已就绪、待确认」，**不代表已外发**。
     */
    fun prepareShare(request: Request): ShareOutcome {
        val entry = try {
            assets.require(request.assetId)
        } catch (e: MediaException) {
            return ShareOutcome.Failed(e.code, e.reason)
        }

        val uri = contentUriProvider?.invoke(entry.assetId)
        if (uri == null) {
            // 没有受控 content:// 出口时拒绝，绝不退化成 file:// 真实路径。
            return ShareOutcome.Failed(
                CommandErrors.NOT_WIRED,
                "share requires a content:// provider; file paths are never exposed",
            )
        }

        val intent = Intent(Intent.ACTION_SEND).apply {
            type = entry.mime
            putExtra(Intent.EXTRA_STREAM, uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
            if (request.target.isNotBlank()) {
                setPackage(request.target)
            }
        }

        return ShareOutcome.Ready(
            SharePlan(
                evidence = assets.evidence(entry),
                target = request.target.ifBlank { SYSTEM_SHARE_SHEET },
                mime = entry.mime,
                // 任何外发都是高风险媒体动作，固定要求确认。
                requiresConfirmation = true,
                intent = intent,
            ),
        )
    }

    /**
     * 确认后真正启动分享面板。
     *
     * 只有 `request.confirmed == true` 才启动；未确认一律 [CommandErrors.ACTION_REJECTED]，
     * 与 M06 `ACTION_REJECTED`（高风险动作未获确认）语义一致。
     */
    fun share(request: Request): ShareOutcome {
        if (!request.confirmed) {
            return ShareOutcome.Failed(
                CommandErrors.ACTION_REJECTED,
                "media share requires explicit user confirmation before sending",
            )
        }
        val prepared = prepareShare(request)
        if (prepared !is ShareOutcome.Ready) return prepared
        val intent = prepared.plan.intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return try {
            context.startActivity(intent)
            prepared
        } catch (t: Throwable) {
            ShareOutcome.Failed(
                CommandErrors.ACTION_REJECTED,
                "share intent could not be started: ${t.javaClass.simpleName}",
            )
        }
    }

    // ---- 任务资产表维护 --------------------------------------------------

    /** 清空某个任务的资产表并删除文件（验收矩阵 `cleanup: clear task asset table`）。 */
    fun clearTask(taskId: String): Int {
        val entries = assets.assetsForTask(taskId)
        entries.forEach { assets.remove(it.assetId) }
        return entries.size
    }

    /** 临时素材 24 小时清理；返回被删的 asset_id。 */
    fun sweepExpired(nowMs: Long = System.currentTimeMillis()): List<String> =
        assets.sweepExpired(nowMs)

    /** 任务当前持有的素材元数据（只读，不含路径）。 */
    fun taskAssets(taskId: String): List<Map<String, String>> =
        assets.assetsForTask(taskId).map { assets.evidence(it) }

    companion object {
        /** `target` 为空时的系统分享面板标记（非外部包名）。 */
        const val SYSTEM_SHARE_SHEET = "system_share_sheet"
    }
}
