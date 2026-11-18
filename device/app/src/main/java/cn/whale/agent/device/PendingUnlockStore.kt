package cn.whale.agent.device

/**
 * M12 设备端「等待解锁 / 等待重新确认」任务表：`pending_unlock_tasks`。
 *
 * 规范来源：`docs/modules/M12-recovery-audit-development.md`「SQLite 表」——
 * Android 固定表为 `processed_effects`、`device_counters`、`pending_unlock_tasks`。
 *
 * 语义固定：
 *
 * - 设备锁屏、或命令需要主人重新确认时，任务**不执行**，落一行到本表等用户解锁/确认；
 * - 解锁后按 `createdAt` 升序逐个恢复，`stepIndex` **只前进不回退**；
 * - 恢复不可逆副作用（短信发送、媒体外发、点击/输入）必须**重新确认**，
 *   绝不能因为「表里有记录」就直接执行；
 * - 「用户可立即清理本地会话和媒体」→ 提供 `clearAll()` 与按任务清理。
 *
 * 落库而不是放内存，是文档「禁止只把状态保存在内存」的硬要求。
 *
 * ## 关于 Room
 *
 * 这里**不用 Room 注解**（原来是 `@Entity` / `@Dao`）。原因见 `DeviceStore.kt`
 * 顶部说明：kapt 在无 Gradle 的 K2 手工工具链下生成不出 `*_Impl`，
 * 导致真机启动服务时闪退。现在数据类就是普通 Kotlin 数据类，
 * 持久化由 `SqlitePendingUnlockDao` 手写完成。**语义与表结构一字未变**。
 */

/** 一条等待解锁/确认的任务。 */
data class PendingUnlockTask(
    /** 幂等键 `<message_id>:<step_index>`，与 `processed_effects` 对齐，避免重复恢复。 */
    val idempotencyKey: String,
    /** 原始消息 ID，便于按任务清理与审计关联。 */
    val messageId: String,
    /** 命令名，恢复前必须再次按 M04 确认级别复核。 */
    val command: String,
    /** payload 的 JSON 文本；**不含密码/验证码正文**，仅用于重放已校验的字段。 */
    val payloadJson: String,
    /** 该步在任务里的下标，只前进不回退。 */
    val stepIndex: Int,
    /** 落库原因：`NEEDS_UNLOCK`（锁屏）或 `RECONFIRM`（需重新确认）。 */
    val reason: String,
    /** 是否必须重新确认后才允许执行（不可逆副作用恒为 true）。 */
    val requiresReconfirmation: Boolean,
    val createdAt: Long,
    /** 最近一次尝试时间；`0` 表示尚未尝试。 */
    val attemptedAt: Long = 0L,
) {
    companion object {
        /** 锁屏导致等待解锁。 */
        const val REASON_NEEDS_UNLOCK: String = CommandErrors.NEEDS_UNLOCK

        /** 命令需要主人重新确认。 */
        const val REASON_RECONFIRM: String = "RECONFIRM"

        /** 固定合法原因集合。 */
        val REASONS: Set<String> = setOf(REASON_NEEDS_UNLOCK, REASON_RECONFIRM)
    }
}

/**
 * 待恢复任务的存储抽象。
 *
 * 保留接口而不是直接绑死 SQLite 实现，是为了让 [PendingUnlockQueue] 的
 * 逻辑能在纯 JVM 单测里跑（注入假实现即可，不需要 Android 的 Cursor）。
 */
interface PendingUnlockDao {
    /** 按落库顺序取出待恢复任务（`createdAt` 升序，同刻用 `stepIndex` 稳定排序）。 */
    fun all(): List<PendingUnlockTask>

    /** 只取必须重新确认的那些（恢复前要先过 M04 确认）。 */
    fun awaitingReconfirmation(): List<PendingUnlockTask>

    /** 取某个任务的全部待恢复步。 */
    fun forMessage(messageId: String): List<PendingUnlockTask>

    fun find(key: String): PendingUnlockTask?

    /** 计数（纪律用；不做分页，首版任务规模受 M06 的 64 步上限约束）。 */
    fun count(): Int

    /** 落库。同键重复写入视为幂等覆盖，不会产生两条待恢复记录。 */
    fun save(task: PendingUnlockTask)

    /** 恢复成功或用户明确放弃后删除。 */
    fun remove(key: String)

    fun removeForMessage(messageId: String)

    /** 用户可立即清理本地会话与媒体对应的待恢复任务。 */
    fun clearAll()
}

/**
 * `pending_unlock_tasks` 的存储边界。
 *
 * 纯逻辑 + 可注入 DAO，便于 JVM 单测；生产入口见 [sqlitePendingUnlockQueue]。
 */
class PendingUnlockQueue(private val dao: PendingUnlockDao) {

    /**
     * 登记一个等待解锁/确认的步。
     *
     * @param reason 只能取 [PendingUnlockTask.REASON_NEEDS_UNLOCK] 或
     *   [PendingUnlockTask.REASON_RECONFIRM]；非法值直接拒绝，避免写出无法解释的行。
     * @param requiresReconfirmation 不可逆命令必须传 `true`。
     */
    fun enqueue(
        idempotencyKey: String,
        messageId: String,
        command: String,
        payloadJson: String,
        stepIndex: Int,
        reason: String,
        requiresReconfirmation: Boolean,
        nowMs: Long,
    ): PendingUnlockTask {
        require(reason in PendingUnlockTask.REASONS) { "unsupported pending reason: $reason" }
        require(idempotencyKey.isNotBlank()) { "idempotencyKey is required" }
        val task = PendingUnlockTask(
            idempotencyKey = idempotencyKey,
            messageId = messageId,
            command = command,
            payloadJson = payloadJson,
            stepIndex = stepIndex,
            reason = reason,
            requiresReconfirmation = requiresReconfirmation,
            createdAt = nowMs,
        )
        dao.save(task)
        return task
    }

    fun pending(): List<PendingUnlockTask> = dao.all()

    fun awaitingReconfirmation(): List<PendingUnlockTask> = dao.awaitingReconfirmation()

    fun forMessage(messageId: String): List<PendingUnlockTask> = dao.forMessage(messageId)

    fun find(idempotencyKey: String): PendingUnlockTask? = dao.find(idempotencyKey)

    fun size(): Int = dao.count()

    /** 标记已尝试（不改 `stepIndex`，不产生副作用）。 */
    fun markAttempted(idempotencyKey: String, nowMs: Long) {
        val existing = dao.find(idempotencyKey) ?: return
        dao.save(existing.copy(attemptedAt = nowMs))
    }

    fun complete(idempotencyKey: String) {
        dao.remove(idempotencyKey)
    }

    fun dropForMessage(messageId: String) {
        dao.removeForMessage(messageId)
    }

    /** 用户主动清理本地会话：整表清空，不保留任何待恢复步。 */
    fun clearAll() {
        dao.clearAll()
    }
}

/**
 * 生产构建入口。
 *
 * 用工厂函数而不是子类：`PendingUnlockQueue` 本身已经完整（逻辑全部在纯 Kotlin 里），
 * 生产与测试的区别只在「DAO 从哪来」，不需要再派生一个空壳子类
 * （Kotlin 的 class 默认 final，硬要继承还得把父类打开，属无谓的复杂度）。
 *
 * @param dao 来自 [SqlitePendingUnlockDao]
 */
fun sqlitePendingUnlockQueue(dao: PendingUnlockDao): PendingUnlockQueue = PendingUnlockQueue(dao)
