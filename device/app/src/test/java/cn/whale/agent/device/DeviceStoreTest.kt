package cn.whale.agent.device

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * `DeviceSchema` 与待恢复任务队列的固定测试。
 *
 * ## 这组测试守住什么
 *
 * 真机上「保存并启动连接」曾经**直接闪退**：
 *
 * ```
 * Cannot find implementation for ...DeviceDatabase.
 * DeviceDatabase_Impl does not exist
 * ```
 *
 * 根因是 Room 的 `*_Impl` 由 kapt 生成，而本项目的构建链没有 Gradle、
 * Kotlin 2.0 的 kapt 在命令行下生成不出 stub。于是那个类**从来没被编出来过**。
 *
 * 更糟的是：482 个单测全绿却完全没发现——因为**没有一个测试碰过数据库**。
 * 这组测试就是补上这个盲区。
 *
 * 但注意：真正的 SQLite 读写需要 Android 运行环境（`SQLiteOpenHelper` 依赖
 * 系统实现，纯 JVM 跑不了）。所以这里测的是**能用纯 Kotlin 测的那部分**：
 * - 建表/迁移的 SQL 语句本身合法（表名、列名、主键、索引）；
 * - `PendingUnlockQueue` 的全部逻辑（用内存假 DAO）；
 * - 三个实体的数据类语义。
 *
 * 真正「打开数据库不崩」由 androidTest（需真机/模拟器）覆盖，
 * 并且已通过 `adb install` + 点按钮实测。
 */
class DeviceStoreTest {

    // -- 建表语句 --------------------------------------------------------

    @Test
    fun `schema creates all three tables`() {
        val sql = DeviceSchema.CREATE_ALL.joinToString("\n")
        assertTrue("processed_effects 应被创建", sql.contains("processed_effects"))
        assertTrue("device_counters 应被创建", sql.contains("device_counters"))
        assertTrue("pending_unlock_tasks 应被创建", sql.contains("pending_unlock_tasks"))
    }

    @Test
    fun `version is two after pending unlock table was added`() {
        // v1：两张表；v2：新增 pending_unlock_tasks。改动版本号要同步改迁移语句。
        assertEquals(2, DeviceSchema.VERSION)
    }

    @Test
    fun `pending unlock table has every column the dao reads`() {
        val sql = DeviceSchema.CREATE_ALL.joinToString("\n")
        // 这九个列名必须与 SqlitePendingUnlockDao 里的列常量逐字一致，
        // 否则查询会拿到 -1 索引并抛 IllegalArgumentException。
        for (column in listOf(
            "idempotencyKey",
            "messageId",
            "command",
            "payloadJson",
            "stepIndex",
            "reason",
            "requiresReconfirmation",
            "createdAt",
            "attemptedAt",
        )) {
            assertTrue("缺列 $column", sql.contains(column))
        }
    }

    @Test
    fun `pending unlock table declares primary key and indexes`() {
        val sql = DeviceSchema.CREATE_ALL.joinToString("\n")
        assertTrue("应由 idempotencyKey 做主键", sql.contains("PRIMARY KEY(idempotencyKey)"))
        assertTrue("应有 messageId 索引", sql.contains("ix_pending_unlock_message"))
        assertTrue("应有 requiresReconfirmation 索引", sql.contains("ix_pending_unlock_reconfirmation"))
    }

    @Test
    fun `upgrade statements are idempotent`() {
        // 全部使用 IF NOT EXISTS：中途中断后重试不会因为"表已存在"直接失败。
        for (statement in DeviceSchema.CREATE_ALL) {
            assertTrue(
                "建表语句必须幂等：$statement",
                statement.contains("IF NOT EXISTS"),
            )
        }
    }

    @Test
    fun `no destructive drop statement anywhere`() {
        // processed_effects 与 device_counters 是「不重做副作用」「counter 只前进」的
        // 权威记录。任何 DROP / DELETE FROM 出现在建表语句里都是安全边界破损。
        for (statement in DeviceSchema.CREATE_ALL) {
            assertTrue("建表语句不得含 DROP：$statement", !statement.contains("DROP"))
            assertTrue("建表语句不得含 DELETE：$statement", !statement.contains("DELETE"))
        }
    }

    // -- 数据类语义 ------------------------------------------------------

    @Test
    fun `pending unlock task defaults attemptedAt to zero`() {
        val task = PendingUnlockTask(
            idempotencyKey = "m1:0",
            messageId = "m1",
            command = "device.ui.tap",
            payloadJson = "{}",
            stepIndex = 0,
            reason = PendingUnlockTask.REASON_NEEDS_UNLOCK,
            requiresReconfirmation = false,
            createdAt = 1000L,
        )
        assertEquals("未尝试的任务 attemptedAt 应为 0", 0L, task.attemptedAt)
    }

    @Test
    fun `reason constants are exactly the supported two`() {
        assertEquals(
            setOf(PendingUnlockTask.REASON_NEEDS_UNLOCK, PendingUnlockTask.REASON_RECONFIRM),
            PendingUnlockTask.REASONS,
        )
    }

    // -- 队列逻辑（用内存假 DAO，纯 JVM 可跑）----------------------------

    /** 内存假 DAO：验证 `PendingUnlockQueue` 逻辑，不需要真 SQLite。 */
    private class FakeDao : PendingUnlockDao {
        private val rows = LinkedHashMap<String, PendingUnlockTask>()

        override fun all(): List<PendingUnlockTask> =
            rows.values.sortedWith(compareBy({ it.createdAt }, { it.stepIndex }))

        override fun awaitingReconfirmation(): List<PendingUnlockTask> =
            all().filter { it.requiresReconfirmation }

        override fun forMessage(messageId: String): List<PendingUnlockTask> =
            all().filter { it.messageId == messageId }.sortedBy { it.stepIndex }

        override fun find(key: String): PendingUnlockTask? = rows[key]

        override fun count(): Int = rows.size

        override fun save(task: PendingUnlockTask) {
            rows[task.idempotencyKey] = task
        }

        override fun remove(key: String) {
            rows.remove(key)
        }

        override fun removeForMessage(messageId: String) {
            rows.entries.removeIf { it.value.messageId == messageId }
        }

        override fun clearAll() {
            rows.clear()
        }
    }

    @Test
    fun `enqueue persists a task with all fields`() {
        val queue = PendingUnlockQueue(FakeDao())
        val task = queue.enqueue(
            idempotencyKey = "msg1:0",
            messageId = "msg1",
            command = "device.sms.send",
            payloadJson = """{"to":"10086"}""",
            stepIndex = 0,
            reason = PendingUnlockTask.REASON_NEEDS_UNLOCK,
            requiresReconfirmation = true,
            nowMs = 500L,
        )
        assertNotNull(queue.find("msg1:0"))
        assertEquals("device.sms.send", task.command)
        assertTrue("不可逆命令必须要求重新确认", task.requiresReconfirmation)
    }

    @Test
    fun `enqueue rejects unsupported reason`() {
        val queue = PendingUnlockQueue(FakeDao())
        var thrown = false
        try {
            queue.enqueue(
                idempotencyKey = "k",
                messageId = "m",
                command = "c",
                payloadJson = "{}",
                stepIndex = 0,
                reason = "SOMETHING_ELSE",
                requiresReconfirmation = false,
                nowMs = 1L,
            )
        } catch (expected: IllegalArgumentException) {
            thrown = true
        }
        assertTrue("非法 reason 必须被拒绝", thrown)
    }

    @Test
    fun `markAttempted does not move step index`() {
        val queue = PendingUnlockQueue(FakeDao())
        queue.enqueue(
            idempotencyKey = "m:2",
            messageId = "m",
            command = "c",
            payloadJson = "{}",
            stepIndex = 2,
            reason = PendingUnlockTask.REASON_RECONFIRM,
            requiresReconfirmation = true,
            nowMs = 10L,
        )
        queue.markAttempted("m:2", nowMs = 99L)
        val after = queue.find("m:2")
        assertNotNull(after)
        assertEquals("stepIndex 只前进不回退", 2, after!!.stepIndex)
        assertEquals(99L, after.attemptedAt)
    }

    @Test
    fun `awaitingReconfirmation only returns irreversible tasks`() {
        val queue = PendingUnlockQueue(FakeDao())
        queue.enqueue("a:0", "a", "c1", "{}", 0, PendingUnlockTask.REASON_NEEDS_UNLOCK, false, 1L)
        queue.enqueue("b:0", "b", "c2", "{}", 0, PendingUnlockTask.REASON_NEEDS_UNLOCK, true, 2L)
        val awaiting = queue.awaitingReconfirmation()
        assertEquals(1, awaiting.size)
        assertEquals("b", awaiting.first().messageId)
    }

    @Test
    fun `complete removes the task`() {
        val queue = PendingUnlockQueue(FakeDao())
        queue.enqueue("k:0", "k", "c", "{}", 0, PendingUnlockTask.REASON_NEEDS_UNLOCK, false, 1L)
        queue.complete("k:0")
        assertNull(queue.find("k:0"))
        assertEquals(0, queue.size())
    }

    @Test
    fun `dropForMessage removes only that message`() {
        val queue = PendingUnlockQueue(FakeDao())
        queue.enqueue("m1:0", "m1", "c", "{}", 0, PendingUnlockTask.REASON_NEEDS_UNLOCK, false, 1L)
        queue.enqueue("m1:1", "m1", "c", "{}", 1, PendingUnlockTask.REASON_NEEDS_UNLOCK, false, 2L)
        queue.enqueue("m2:0", "m2", "c", "{}", 0, PendingUnlockTask.REASON_NEEDS_UNLOCK, false, 3L)
        queue.dropForMessage("m1")
        assertEquals(1, queue.size())
        assertEquals("m2", queue.pending().first().messageId)
    }

    @Test
    fun `clearAll empties the queue`() {
        val queue = PendingUnlockQueue(FakeDao())
        queue.enqueue("m1:0", "m1", "c", "{}", 0, PendingUnlockTask.REASON_NEEDS_UNLOCK, false, 1L)
        queue.enqueue("m2:0", "m2", "c", "{}", 0, PendingUnlockTask.REASON_NEEDS_UNLOCK, false, 2L)
        queue.clearAll()
        assertEquals(0, queue.size())
    }

    @Test
    fun `same key overwrites instead of duplicating`() {
        val queue = PendingUnlockQueue(FakeDao())
        queue.enqueue("k:0", "k", "c1", "{}", 0, PendingUnlockTask.REASON_NEEDS_UNLOCK, false, 1L)
        queue.enqueue("k:0", "k", "c2", "{}", 0, PendingUnlockTask.REASON_NEEDS_UNLOCK, false, 1L)
        assertEquals("同键必须幂等覆盖", 1, queue.size())
        assertEquals("c2", queue.find("k:0")!!.command)
    }
}
