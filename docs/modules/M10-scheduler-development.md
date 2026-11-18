# M10 定时任务与主动问候：详细开发文档

## 固定类

```text
host/astrbot_whale_device/scheduler.py
host/astrbot_whale_device/storage.py
host/astrbot_whale_device/plugin.py
```

## Schedule schema

```json
{
  "schedule_id": "uuid",
  "enabled": true,
  "kind": "daily|weekly|window",
  "timezone": "Asia/Shanghai",
  "at": "08:30",
  "weekdays": [1,2,3,4,5],
  "quiet_start": "23:00",
  "quiet_end": "07:00",
  "daily_limit": 3,
  "cooldown_seconds": 3600,
  "needs_live_device": false,
  "next_run_ms": 0
}
```

未知字段拒绝。时间统一按配置时区计算，数据库保存 UTC epoch ms。

### 时区解析（三级，禁止猜测）

`resolve_timezone(name)` 固定三级顺序：

1. `zoneinfo` 真实时区数据（装了 `tzdata` 或系统自带 tzdata 时可用）；
2. 无 DST 的固定偏移表（`Asia/Shanghai` 这类常年不变偏移的时区）；
3. **显式拒绝**（抛出 `ScheduleConfigError`）。

硬性要求：

- **有 DST 的时区（`America/New_York`、`Europe/London`、`Australia/Sydney` 等）在缺时区数据时必须拒绝，绝不猜偏移。** 主动问候错一小时，比不问候糟糕得多。
- 解析器可由调用方注入（`resolve_timezone(name, resolver=...)`），便于在没有时区数据的部署里换成宿主提供的实现。
- 时区配错必须在**注册时**就报错，不是等到触发那一刻。

工程注记：真实 `zoneinfo.ZoneInfo` 的 `utcoffset(None)` 返回 `None`（没有具体时刻时偏移不确定），这是标准行为；断言偏移必须给一个具体时刻，否则测的是实现类型而不是行为。相关测试实例与「测试环境无关性」通则见 `docs/modules/M13-test-release-development.md`。

## 状态机

```text
DISABLED → DUE → GUARD_CHECK → MODEL_DECISION
MODEL_DECISION → SILENT | SEND | WAIT_DEVICE | FAILED
WAIT_DEVICE → SEND | EXPIRED | CANCELLED
```

每次成功发送后原子更新 `next_run_ms`、今日计数和 `last_effect_key`。

## 保护顺序

严格按：启用状态 → 时间窗口 → 安静时段 → 黑名单 → 每日上限 → 冷却 → 设备在线 → 模型 → 媒体大小 → OneBot 发送。

## Worker

实现持久 worker；主机重启后从 SQLite 恢复 `DUE/WAIT_DEVICE`。同一 `schedule_id` 使用数据库租约，租约 60 秒，避免多进程重复。

## 禁止项

- 禁止使用进程内 Timer 作为唯一调度状态。
- 禁止在 guard 通过前调用模型。
- 禁止通过模型输出覆盖配额、安静时段或黑名单。
- 禁止设备离线时轮询截图。
