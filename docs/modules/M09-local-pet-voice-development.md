# M09 本地桌宠与语音：详细开发文档

## 固定类

```text
device/.../LocalVoiceController.kt
device/.../LocalPetController.kt
device/.../PetOverlayService.kt
device/.../PetWindowOwner.kt
device/.../PetStateMachine.kt
```

## 语音实现

首版使用 Android `SpeechRecognizer`，调用方式固定为按住说话。`LocalVoiceController` 只负责权限、生命周期、识别结果和错误，不负责执行设备命令。

后续低延迟版本可以替换为 `AudioRecord`：16 kHz、单声道、20 ms 帧、`VOICE_COMMUNICATION`、AEC、噪声底、滞回和 hangover。替换只能发生在 voice adapter 内，不能改动 CommandRouter。

录音状态固定为：

```text
IDLE → PRESSING → LISTENING → TRANSCRIBING → IDLE
任何状态 → STOPPED
```

Activity 销毁、屏幕锁定、模式切换和任务停止都必须调用 `stop()`，释放麦克风。

## 悬浮窗实现

Android 版本允许时使用 `TYPE_ACCESSIBILITY_OVERLAY`；否则使用 `TYPE_APPLICATION_OVERLAY`。只有 `WindowManager.addView` 成功才标记 `overlay_online=true`。

窗口管理必须有 `generation/ticket`：旧 Activity 或旧 Flutter/Compose producer 延迟销毁时，不能移除新一代窗口。

## 宠物状态机

固定 8 个状态，禁止增删：

```text
HIDDEN
IDLE
WORKING
WAITING_CONFIRM
WAITING_USER
SUCCESS
ERROR
OFFLINE
```

注意：本节历史上曾被描述为「七态」，但列举的是上面 8 个名字。**以列举为准，共 8 个**，`PetState` 枚举与固定测试 `petStateSetIsFixed` 都按 8 个钉死。任何后续改动都必须同时改本节、`PetState` 枚举与该测试。

状态只来自 `PetEvent`，固定事件名：`task_started/task_progress/confirm_required/user_required/task_succeeded/task_failed/task_stopped/device_offline`。

非事件入口（不算状态来源，不得用于驱动任务语义）：

- `show()`：窗口显示，进入 `IDLE`（或恢复隐藏期间记录的目标态）；
- `hide()`：窗口隐藏，进入 `HIDDEN`；
- `refresh()`：**低频**回落，只把 `SUCCESS`/`ERROR` 的停留态回落到 `IDLE`，绝不越过 `WAITING_CONFIRM`/`WAITING_USER`/`OFFLINE`。

## 依赖和素材

首版不引入网络动画 SDK、游戏引擎或新的跨端插件。素材由本地 APK 资源提供，尺寸、透明边界、点击区域和基线写入 `pet-pack.json`。外部项目的宠物形象不能直接复制。

`pet_pack.json` 的资源位置固定为 `device/app/src/main/res/raw/pet_pack.json`（**不是 `res/xml`**）：`res/xml` 只接受编译后的 XML 资源，放 JSON 会让 aapt2 打包失败；`res/raw` 才是可以原样打包的 JSON 资源目录。

## 禁止项

- 禁止常驻麦克风作为默认实现。
- 禁止桌宠读取 QQ、剪贴板或屏幕正文。
- 禁止桌宠直接调用设备 API 绕过确认。
- 禁止为了动画引入持续高频定时器。
