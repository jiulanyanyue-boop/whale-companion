package cn.whale.agent.device

import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper

/**
 * 设备端持久化：**手写 SQLite**（不用 Room）。
 *
 * 规范来源：`docs/modules/M12-recovery-audit-development.md`「SQLite 表」。
 * 固定三张表：`processed_effects`、`device_counters`、`pending_unlock_tasks`。
 *
 * ## 为什么这里不用 Room
 *
 * 这不是偏好问题，是被**构建链**逼出来的：
 * Room 的 `*_Impl` 由 kapt 注解处理器在编译期生成，而本项目的构建链是
 * 「aapt2 → K2JVMCompiler → D8」的手工流程，**没有 Gradle**。
 * Kotlin 2.0 的 kapt 走 K2 编译器插件（`Kapt4CompilerPluginRegistrar`），
 * 在手工命令行下期望的 `aptMode=stubsAndApt` 参数不会生效，
 * 实测 `exit 0` 但**一个 stub 都不生成**，于是 `_Impl` 永远缺失。
 *
 * 后果是真机上「启动设备连接服务」直接闪退：
 *
 * ```
 * java.lang.RuntimeException: Unable to create service ...DeviceConnectionService:
 * Cannot find implementation for ...DeviceDatabase. DeviceDatabase_Impl does not exist
 * ```
 *
 * 而所有单测都是绿的——因为它们**从不构造 `DeviceDatabase`**
 * （测试只测 `PendingUnlockQueue` 的纯逻辑，DAO 是注入进去的）。
 * 这是「单测全绿但真机崩溃」的经典形态。
 *
 * 与其在手工工具链里硬凑注解处理器（脆弱、缺一个 jar 就 ClassNotFound），
 * 不如直接用 `SQLiteOpenHelper`：三张表、六个方法，手写量很小，
 * 而且**行为完全可控、可单测**。
 *
 * ## 语义没有变
 *
 * - `processed_effects`：幂等键 → 历史响应 JSON。**不重做副作用**的权威记录。
 * - `device_counters`：同一设备的 counter **只前进**（`MAX` 语义）。
 * - `pending_unlock_tasks`：等待解锁/重新确认的任务队列（M12）。
 *
 * **升级绝不清库**：`processed_effects` 与 `device_counters` 一旦被抹掉，
 * 等于允许重放已完成的命令、允许 counter 回退——这在 M05/M06 里是安全边界。
 */

/** 一条已处理效果（幂等记录）。 */
data class ProcessedEffect(
    val idempotencyKey: String,
    val responseJson: String,
    val createdAt: Long,
)

/** 一台设备的单调计数器。 */
data class DeviceCounter(
    val deviceId: String,
    val lastCounter: Long,
)

/**
 * 三张表的建表与版本管理。
 *
 * 版本历史：
 * - v1：`processed_effects` + `device_counters`（M05/M06）。
 * - v2：新增 `pending_unlock_tasks`（M12）。
 */
object DeviceSchema {
    const val VERSION: Int = 2

    /** v1 的两张表（v2 建库时也需要，所以独立出来复用）。 */
    val CREATE_V1: List<String> = listOf(
        """
        CREATE TABLE IF NOT EXISTS processed_effects (
            idempotencyKey TEXT NOT NULL,
            responseJson TEXT NOT NULL,
            createdAt INTEGER NOT NULL,
            PRIMARY KEY(idempotencyKey)
        )
        """.trimIndent(),
        """
        CREATE TABLE IF NOT EXISTS device_counters (
            deviceId TEXT NOT NULL,
            lastCounter INTEGER NOT NULL,
            PRIMARY KEY(deviceId)
        )
        """.trimIndent(),
    )

    /** v2 新增的待恢复任务表（含两个索引，与 Room 时代的 schema 保持一致）。 */
    val CREATE_V2: List<String> = listOf(
        """
        CREATE TABLE IF NOT EXISTS pending_unlock_tasks (
            idempotencyKey TEXT NOT NULL,
            messageId TEXT NOT NULL,
            command TEXT NOT NULL,
            payloadJson TEXT NOT NULL,
            stepIndex INTEGER NOT NULL,
            reason TEXT NOT NULL,
            requiresReconfirmation INTEGER NOT NULL,
            createdAt INTEGER NOT NULL,
            attemptedAt INTEGER NOT NULL,
            PRIMARY KEY(idempotencyKey)
        )
        """.trimIndent(),
        "CREATE INDEX IF NOT EXISTS ix_pending_unlock_message ON pending_unlock_tasks(messageId)",
        "CREATE INDEX IF NOT EXISTS ix_pending_unlock_reconfirmation " +
            "ON pending_unlock_tasks(requiresReconfirmation)",
    )

    val CREATE_ALL: List<String> = CREATE_V1 + CREATE_V2
}

/** [EffectLog] 的 SQLite 实现：幂等键 → 历史响应 JSON。 */
class SqliteEffectLog(private val helper: DeviceDb) : EffectLog {
    override fun find(idempotencyKey: String): String? =
        helper.readableDatabase.rawQuery(
            "SELECT responseJson FROM processed_effects WHERE idempotencyKey = ?",
            arrayOf(idempotencyKey),
        ).use { cursor: android.database.Cursor ->
            if (cursor.moveToFirst()) cursor.getString(0) else null
        }

    override fun save(idempotencyKey: String, responseJson: String, nowMs: Long) {
        helper.writableDatabase.execSQL(
            "INSERT OR REPLACE INTO processed_effects(idempotencyKey, responseJson, createdAt) " +
                "VALUES(?, ?, ?)",
            arrayOf(idempotencyKey, responseJson, nowMs.toString()),
        )
    }
}

/**
 * [CounterLog] 的 SQLite 实现：同一设备 counter **只前进**。
 *
 * `INSERT OR REPLACE` 会让 counter 回退（新值直接覆盖旧值），
 * 所以这里用 `MAX` 显式保护——counter 只能增，任何回退请求都不落库。
 */
class SqliteCounterLog(private val helper: DeviceDb) : CounterLog {
    override fun find(deviceId: String): Long? =
        helper.readableDatabase.rawQuery(
            "SELECT lastCounter FROM device_counters WHERE deviceId = ?",
            arrayOf(deviceId),
        ).use { cursor: android.database.Cursor ->
            if (cursor.moveToFirst()) cursor.getLong(0) else null
        }

    override fun save(deviceId: String, counter: Long) {
        helper.writableDatabase.execSQL(
            """
            INSERT INTO device_counters(deviceId, lastCounter) VALUES(?, ?)
            ON CONFLICT(deviceId) DO UPDATE SET lastCounter =
                CASE WHEN excluded.lastCounter > device_counters.lastCounter
                     THEN excluded.lastCounter
                     ELSE device_counters.lastCounter END
            """.trimIndent(),
            arrayOf(deviceId, counter.toString()),
        )
    }
}

/** [PendingUnlockDao] 的 SQLite 实现（表结构见 [DeviceSchema.CREATE_V2]）。 */
class SqlitePendingUnlockDao(private val helper: DeviceDb) : PendingUnlockDao {
    override fun all(): List<PendingUnlockTask> =
        queryRows("SELECT * FROM pending_unlock_tasks ORDER BY createdAt ASC, stepIndex ASC")

    override fun awaitingReconfirmation(): List<PendingUnlockTask> = queryRows(
        "SELECT * FROM pending_unlock_tasks WHERE requiresReconfirmation = 1 " +
            "ORDER BY createdAt ASC, stepIndex ASC",
    )

    override fun forMessage(messageId: String): List<PendingUnlockTask> = queryRows(
        "SELECT * FROM pending_unlock_tasks WHERE messageId = ? ORDER BY stepIndex ASC",
        arrayOf(messageId),
    )

    override fun find(key: String): PendingUnlockTask? =
        queryRows("SELECT * FROM pending_unlock_tasks WHERE idempotencyKey = ?", arrayOf(key))
            .firstOrNull()

    override fun count(): Int =
        helper.readableDatabase.rawQuery("SELECT COUNT(*) FROM pending_unlock_tasks", null).use { cursor: android.database.Cursor ->
            if (cursor.moveToFirst()) cursor.getInt(0) else 0
        }

    override fun save(task: PendingUnlockTask) {
        helper.writableDatabase.execSQL(
            """
            INSERT OR REPLACE INTO pending_unlock_tasks(
                idempotencyKey, messageId, command, payloadJson, stepIndex,
                reason, requiresReconfirmation, createdAt, attemptedAt
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """.trimIndent(),
            arrayOf(
                task.idempotencyKey,
                task.messageId,
                task.command,
                task.payloadJson,
                task.stepIndex.toString(),
                task.reason,
                if (task.requiresReconfirmation) "1" else "0",
                task.createdAt.toString(),
                task.attemptedAt.toString(),
            ),
        )
    }

    override fun remove(key: String) {
        helper.writableDatabase.execSQL(
            "DELETE FROM pending_unlock_tasks WHERE idempotencyKey = ?",
            arrayOf(key),
        )
    }

    override fun removeForMessage(messageId: String) {
        helper.writableDatabase.execSQL(
            "DELETE FROM pending_unlock_tasks WHERE messageId = ?",
            arrayOf(messageId),
        )
    }

    override fun clearAll() {
        helper.writableDatabase.execSQL("DELETE FROM pending_unlock_tasks")
    }

    private fun queryRows(sql: String, args: Array<String>? = null): List<PendingUnlockTask> =
        helper.readableDatabase.rawQuery(sql, args).use { cursor: android.database.Cursor ->
            val rows = mutableListOf<PendingUnlockTask>()
            val columns = PendingUnlockColumns
            while (cursor.moveToNext()) {
                rows += PendingUnlockTask(
                    idempotencyKey = cursor.getString(cursor.getColumnIndexOrThrow(columns.KEY)),
                    messageId = cursor.getString(cursor.getColumnIndexOrThrow(columns.MESSAGE_ID)),
                    command = cursor.getString(cursor.getColumnIndexOrThrow(columns.COMMAND)),
                    payloadJson = cursor.getString(cursor.getColumnIndexOrThrow(columns.PAYLOAD)),
                    stepIndex = cursor.getInt(cursor.getColumnIndexOrThrow(columns.STEP_INDEX)),
                    reason = cursor.getString(cursor.getColumnIndexOrThrow(columns.REASON)),
                    requiresReconfirmation = cursor.getInt(
                        cursor.getColumnIndexOrThrow(columns.REQUIRES_RECONFIRMATION),
                    ) != 0,
                    createdAt = cursor.getLong(cursor.getColumnIndexOrThrow(columns.CREATED_AT)),
                    attemptedAt = cursor.getLong(cursor.getColumnIndexOrThrow(columns.ATTEMPTED_AT)),
                )
            }
            rows
        }

    /** 列名集中定义：改名时只改这里，避免 SQL 与解析器漂移。 */
    private object PendingUnlockColumns {
        const val KEY = "idempotencyKey"
        const val MESSAGE_ID = "messageId"
        const val COMMAND = "command"
        const val PAYLOAD = "payloadJson"
        const val STEP_INDEX = "stepIndex"
        const val REASON = "reason"
        const val REQUIRES_RECONFIRMATION = "requiresReconfirmation"
        const val CREATED_AT = "createdAt"
        const val ATTEMPTED_AT = "attemptedAt"
    }
}

/**
 * 数据库句柄。
 *
 * `onUpgrade` **只加表/加索引，绝不删数据**：`processed_effects` 与
 * `device_counters` 是「不重做副作用」「counter 只前进」的权威记录，
 * 用 destructive migration 抹掉它们等于允许重放已完成命令（M05/M06 的安全边界）。
 */
class DeviceDb(context: Context, name: String = "whale-device.db") :
    SQLiteOpenHelper(context, name, null, DeviceSchema.VERSION) {

    override fun onCreate(db: SQLiteDatabase) {
        DeviceSchema.CREATE_ALL.forEach { db.execSQL(it) }
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        // v1 → v2：只加表，不动既有两张表的数据。
        if (oldVersion < 2) {
            DeviceSchema.CREATE_V2.forEach { db.execSQL(it) }
        }
        // 用 IF NOT EXISTS 的语句，重复执行也安全（防止半途中断后重试失败）。
        DeviceSchema.CREATE_V1.forEach { db.execSQL(it) }
    }
}
