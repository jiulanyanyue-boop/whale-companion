# M12 持久化、恢复与审计：详细开发文档

## SQLite 表

主机固定表：

```text
owner_config
devices
inbox
runs
effects
schedules
assets
audit_events
```

Android 固定表：

```text
processed_effects
device_counters
pending_unlock_tasks
```

## Run 状态

```text
RECEIVED → PLANNED → RUNNING → SUCCEEDED
                         ├── WAITING_DEVICE
                         ├── NEEDS_UNLOCK
                         ├── FAILED
                         ├── DENIED
                         └── CANCELLED
```

每次状态迁移必须带 `run_id`、`message_id`、`step_index`、`updated_at` 和错误码。

## 恢复算法

1. 启动时扫描 `RUNNING/WAITING_DEVICE/NEEDS_UNLOCK`。
2. 如果 effect 已存在，标记该 step 完成，不重做副作用。
3. 如果没有 effect，只有幂等安全的命令允许恢复；短信发送、媒体分享和发布动作必须重新确认。
4. 设备计数器和租约不回退。

## 审计脱敏

`AuditLogger` 禁止写入：密码、验证码、短信正文、完整联系人号码、屏幕节点文本和原始音频。电话号码最多保留后四位。

## 禁止项

- 禁止只把状态保存在内存。
- 禁止用日志文本判断任务是否完成。
- 禁止恢复不可逆副作用而不重新确认。
- 禁止把用户媒体内容写入审计表。
