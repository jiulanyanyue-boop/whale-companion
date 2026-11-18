package cn.whale.agent.device

import android.content.Context

/**
 * M01 固定模式枚举。
 *
 * 只有两个模式，禁止增加第三个（`docs/modules/M01-system-modes-development.md`「固定实现」）。
 * 模式切换只能来自设置页的明确用户点击，不能由模型输出、语音内容、QQ/群消息触发。
 */
enum class AgentMode {
    REMOTE_B,
    LOCAL_PET,
    ;

    val isRemote: Boolean get() = this == REMOTE_B

    val isLocal: Boolean get() = this == LOCAL_PET

    companion object {
        /** 存储位置固定：SharedPreferences 名 `agent_mode`。 */
        const val PREFERENCES_NAME: String = "agent_mode"

        /** 存储字段固定：`mode`。 */
        const val PREFERENCES_KEY: String = "mode"

        /** 默认模式固定为 `REMOTE_B`；缺失、非法或损坏一律回退到它，绝不回退 `LOCAL_PET`。 */
        val DEFAULT: AgentMode = REMOTE_B

        /**
         * 原始字符串 → [AgentMode] 的纯解析，供 JVM 单测直接调用（不需要 Android 运行时）。
         *
         * 只有与枚举名完全相等的字符串（忽略首尾空白）才被接受；
         * `null`、空串、大小写不符、拼写错误、未来版本写入的未知值全部回退 [DEFAULT]。
         */
        fun parseOrRemote(raw: String?): AgentMode {
            val normalized = raw?.trim().orEmpty()
            if (normalized.isEmpty()) return DEFAULT
            return values().firstOrNull { it.name == normalized } ?: DEFAULT
        }
    }
}

/** 可注入的模式读取器：闸门只依赖它，不直接依赖 SharedPreferences。 */
fun interface ModeReader {
    fun get(): AgentMode
}

/** 可写模式存储；只有设置页点击后经 `ModeSwitchCoordinator` 才能调用 [set]。 */
interface ModeStore : ModeReader {
    fun set(mode: AgentMode)
}

/**
 * 原始存储读写抽象。
 *
 * 把 SharedPreferences 的「原始字符串读写」抽出来，[AgentModeStore] 只负责解析与回退，
 * 于是「缺失/非法/损坏值」这类场景可以在纯 JVM 测试里直接构造，不需要 Android 运行时。
 */
interface RawModeStorage {
    /** 读取原始值；任何异常都必须由存储层自己吞掉并返回 null（损坏等价于缺失）。 */
    fun readRaw(): String?

    fun writeRaw(value: String)
}

/**
 * 生产实现：SharedPreferences 名固定 `agent_mode`，字段固定 `mode`。
 *
 * 读取永远经过 [AgentMode.parseOrRemote]，所以任何损坏值（类型错误、非法字符串）
 * 都只会退化成 `REMOTE_B`，不会让设备意外进入本地模式。
 */
class AgentModeStore(context: Context) : ModeStore {
    private val preferences = context.getSharedPreferences(AgentMode.PREFERENCES_NAME, Context.MODE_PRIVATE)

    private val storage = object : RawModeStorage {
        override fun readRaw(): String? = try {
            preferences.getString(AgentMode.PREFERENCES_KEY, null)
        } catch (_: Throwable) {
            // 类型被写坏（例如存成了 Int）时同样退化为默认模式，不能让读取抛异常打断闸门。
            null
        }

        override fun writeRaw(value: String) {
            preferences.edit().putString(AgentMode.PREFERENCES_KEY, value).apply()
        }
    }

    override fun get(): AgentMode = AgentMode.parseOrRemote(storage.readRaw())

    override fun set(mode: AgentMode) {
        storage.writeRaw(mode.name)
    }
}

/**
 * 基于任意 [RawModeStorage] 的存储实现：解析与回退规则与 [AgentModeStore] 完全一致。
 *
 * 生产代码只用 [AgentModeStore]；本类用于纯 JVM 固定测试（模拟缺失/非法/损坏的 SharedPreferences）。
 */
class RawModeStore(private val storage: RawModeStorage) : ModeStore {
    override fun get(): AgentMode = AgentMode.parseOrRemote(
        try {
            storage.readRaw()
        } catch (_: Throwable) {
            null
        },
    )

    override fun set(mode: AgentMode) {
        storage.writeRaw(mode.name)
    }
}

/**
 * 纯 JVM 测试替身：行为与 [AgentModeStore] 一致，但不依赖 Android。
 *
 * 仅用于固定测试；生产代码必须使用 [AgentModeStore]。
 */
class InMemoryModeStore(initial: AgentMode = AgentMode.DEFAULT) : ModeStore {
    @Volatile
    private var mode: AgentMode = initial

    override fun get(): AgentMode = mode

    override fun set(mode: AgentMode) {
        this.mode = mode
    }
}
