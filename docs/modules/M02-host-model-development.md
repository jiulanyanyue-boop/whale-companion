# M02 主机、模型与人设：详细开发文档

## 模块边界

实现目录固定为：

```text
host/astrbot_whale_device/
├── router.py
├── models.py
├── validation.py
├── plugin.py
└── astrbot_adapter.py        # 下一阶段接入锁定版 AstrBot
```

AstrBot 只通过 `model_decider(message, principal, can_use_device)` 注入模型能力。插件不能读取供应商密钥，不能拼接自定义 URL，不能在消息中执行配置写入。

## 配置字段

配置键固定为：

```text
provider_id
base_url
model_id
fallback_model_id
persona_id
language
max_context_tokens
tts_provider_id
allow_proactive_greeting
```

密钥由 AstrBot Secret Store 保存，禁止写入 `TaskStore`、日志、设备帧和 Android。

## 决策解析

`parse_model_decision` 必须：

1. 只接受 JSON object。
2. 只接受三个 `mode` 值。
3. `REPLY/SILENT` 禁止存在 `device_plan`。
4. `DEVICE_TASK` 必须有 1 到 64 个命令。
5. 每个命令必须有 `command` 和 `payload`，并调用 `validate_device_plan`。
6. 任何异常直接返回失败，不做正则修复、不猜字段、不二次开放式 repair。

## AstrBot 适配器

适配器只做四件事：

- 将 OneBot 事件转换为 `IncomingMessage`。
- 将 `REPLY/SILENT` 转换为 OneBot 发送动作。
- 将 `DEVICE_TASK` 转换为 `DeviceEnvelope`。
- 将设备响应转换为固定的任务结果消息。

适配器不得注册未在 `contracts/` 声明的工具，不得让模型直接访问 `DeviceTransport`。

## 超时和重试

- 模型请求超时：30 秒，最多一次固定备用模型切换。
- 设备命令超时由 `deadline_ms` 控制，不由模型决定上限。
- 失败命令不自动重复，只有相同幂等键的设备重连可以读取原结果。

## 测试

- provider/base_url/model 只来自配置，不来自消息正文。
- 人设改变不影响 `PrincipalGate` 和 `PackagePolicy`。
- 访客上下文序列化结果中不能出现 `device.`、包名、屏幕节点或设备状态。
