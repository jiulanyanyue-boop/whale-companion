# M01 系统模式与设备角色：详细开发文档

## 固定实现

Android 包名固定为 `cn.whale.agent.device`。模式枚举固定为：

```kotlin
enum class AgentMode { REMOTE_B, LOCAL_PET }
```

实现文件固定：

- `device/app/src/main/java/cn/whale/agent/device/AgentMode.kt`
- `MainActivity.kt`：模式设置入口。
- `DeviceConnectionService.kt`：只在 `REMOTE_B` 建立 WSS。
- `LocalPetController.kt`：只在 `LOCAL_PET` 接受本地调用。
- `CommandRouter.kt`：两种模式唯一执行器。

## 存储

SharedPreferences 名称固定为 `agent_mode`，字段固定为：

```text
mode = REMOTE_B | LOCAL_PET
```

缺失、非法或损坏时回退 `REMOTE_B`。不得回退到 `LOCAL_PET`。

## 远程闸门

`DeviceConnectionService.onMessage` 固定顺序：

1. JSON 反序列化失败：丢弃并记录 `PROTOCOL_DECODE_FAILED`。
2. `signature` 为空：返回 `SIGNATURE_REQUIRED`。
3. `AgentModeStore.get() != REMOTE_B`：返回 `MODE_REJECTED`，不得执行 router。
4. 检查幂等键、deadline、counter。
5. 调用 `CommandRouter.execute`。

切换到本地模式时，设置页必须 `stopService(DeviceConnectionService)`；切回远程时从前台设置页启动服务。

## 本地闸门

`LocalPetController.execute` 固定检查：

- 当前模式必须为 `LOCAL_PET`。
- Activity/桌宠调用必须来自当前应用进程，禁止通过隐式 Intent 暴露执行接口。
- 本地会话必须处于已解锁状态；远程模式可调用 `device.system.unlock`，但只使用手机端已加密保存的凭据。
- 高风险命令必须先经过 `UserGate`，再调用 `CommandRouter`。

## 禁止项

- 禁止增加第三个模式。
- 禁止让模型输出 `mode` 来切换系统模式。
- 禁止把远程模式和本地模式复制成两套命令执行器。
- 禁止在本地模式继续保持远程 WebSocket 连接。
- 禁止使用 ADB、shell、动态代码或任意 HTTP 作为本地入口。

## 测试

固定测试：

1. 默认值是 `REMOTE_B`。
2. 本地模式下远程帧返回 `MODE_REJECTED` 且无副作用。
3. 远程模式下本地 controller 返回 `MODE_REJECTED`。
4. 模式切换停止旧任务并关闭旧连接。
5. 损坏的 SharedPreferences 不会切换到本地模式。
