# M06 设备命令运行时：详细开发文档

## 固定执行器

入口固定为：

```text
device/.../CommandRouter.kt
```

所有入口必须构造 `DeviceEnvelope` 或等价的已验证内部对象，再调用 `CommandRouter.execute`。不得直接操作 AccessibilityService、CameraX 或 ContentResolver。

`execute` 的闸门顺序固定，不得调换：

```text
协议版本闸门（protocol_version != 1 → UNSUPPORTED_PROTOCOL）
→ 命令白名单闸门（不在 DeviceCommands.ALL → UNKNOWN_COMMAND）
→ payload 最终校验闸门（PayloadValidator → INVALID_PAYLOAD）
→ 串行执行闸门（SerialCommandGate 忙 → queued，不执行任何动作）
→ 路由到实现层
```

协议版本闸门必须先于 payload 校验，因为不同协议版本的 payload 契约可能不同；串行闸门必须在任何副作用之前，禁止“先点再发现忙”。

固定文件：

```text
device/.../CommandSpec.kt        # payload 契约（schema 的设备端镜像）
device/.../PayloadValidator.kt   # payload 最终校验闸门
device/.../SerialCommandGate.kt  # 单 active task 串行闸门
device/.../CommandErrors.kt      # 错误码唯一登记处
```

## 命令路由表

| 命令 | 实现层 | 未接入时结果 |
|---|---|---|
| `app.list` | PackageManager | `ok` |
| `app.launch` | PackageManager Intent | `denied/NO_LAUNCH_INTENT` |
| `system.settings` | Settings Intent | `denied/SETTINGS_FAILED` |
| `telephony.dial` | `ACTION_DIAL` | `denied/DIAL_FAILED` |
| `contacts.search` | Contacts Provider | `denied/CONTACT_PERMISSION_REQUIRED` |
| `sms.compose` | `ACTION_SENDTO` | `denied/SMS_COMPOSE_FAILED` |
| `sms.send` | SmsManager + confirmation | `denied/SMS_PERMISSION_REQUIRED` |
| `ui.*` | AccessibilityController | `denied/NO_ACCESSIBILITY` |
| `ui.observe` | AccessibilityController / ScreenCaptureController | `tree/auto` 返回节点；`vision` 返回真实截图，失败如实返回 |
| `screen.capture` | AccessibilityService.takeScreenshot（Android 11+） | 当前屏幕必须属于允许应用；成功返回有界JPEG图片，锁屏/无障碍不可用/受保护窗口明确失败 |
| `camera.capture` | 内部 CameraCaptureActivity + CameraController | 前台生命周期真实拍摄并返回受控素材元数据及有界JPEG；锁屏/无权限/拍摄失败返回实际失败 |
| `media.*` | MediaController | `queued` 或固定失败（本构建为 `denied/NOT_WIRED`） |

`ui.observe` 的附加约束：`vision` 调用真实截屏后返回 `evidence.mode=vision` 与图片；`auto/tree` 返回真实节点及 `evidence.mode=tree`。不允许用模式标签伪造图像观察。

## payload 校验

主机和设备都必须校验：

- 命令是否在 `DeviceCommands.ALL`。
- payload 是否 object。
- 字段是否在 `contracts/device-commands.schema.json`。
- 字段类型、长度、枚举和范围。
- `app.launch` 的包名是否通过本地白名单。

设备端校验是最终闸门，不能信任主机已经校验过。

`payload` 为 `const {}` 的命令（`app.list`、`screen.capture`、`ui.back`、`ui.home`、`ui.recents`、`ui.notifications`、`device_status`）必须**拒绝**任何字段，防止模型夹带裸坐标。未声明字段一律 `INVALID_PAYLOAD`，不允许“忽略未知字段后继续执行”。

`PayloadValidator` 读取字段值必须经过 `stringValue` / `intValue`，禁止在路由里直接对 `JsonElement` 做 `toString().trim('"')` 一类的手工解包——那会让 `JsonPrimitive` 的字符串字面量与整数混淆。

## 串行执行

同一设备只允许一个 active task。每个命令完成后必须重新观察/确认再执行下一条。并发命令返回 `queued`，不能同时点击。

## 错误码固定集合

协议闸门与路由闸门：

```text
UNKNOWN_COMMAND
INVALID_PAYLOAD
PACKAGE_NOT_ALLOWED
MODE_REJECTED
SIGNATURE_REQUIRED
REPLAY
EXPIRED
PROTOCOL_DECODE_FAILED
UNSUPPORTED_PROTOCOL
```

执行层：

```text
NO_ACCESSIBILITY
STALE_SNAPSHOT
SELECTOR_MISMATCH
FOREGROUND_PACKAGE_MISMATCH
NEEDS_UNLOCK
NOT_WIRED
ACTION_REJECTED
UNLOCK_FAILED
```

权限与失败（路由表逐条要求的码，必须保留具体权限名）：

```text
PERMISSION_REQUIRED
CONTACT_PERMISSION_REQUIRED
SMS_PERMISSION_REQUIRED
NO_LAUNCH_INTENT
SETTINGS_FAILED
DIAL_FAILED
INVALID_NUMBER
LAUNCH_FAILED
SMS_COMPOSE_FAILED
SMS_SEND_FAILED
```

媒体（M08 专有，`camera.*` / `media.*` 四个命令使用）：

```text
CAMERA_PERMISSION_REQUIRED
PHOTO_PICKER_CANCELLED
MEDIA_TOO_LARGE
MEDIA_TYPE_NOT_ALLOWED
UPLOAD_RETRYABLE
UPLOAD_HASH_MISMATCH
MEDIA_NOT_FOUND
```

媒体码的归属约定：这 7 个码由 M08 在 `AssetStore.kt` 的 `MediaErrors` 里定义语义与产生条件，同时**登记到 `CommandErrors.ALL`**，由 `device.camera.capture` / `media.pick` / `media.upload` / `media.share` 四个路由分支在失败时返回。`MediaErrors` 保留为 M08 契约测试的锚点，两处必须逐字一致。

错误码唯一登记处是 `device/.../CommandErrors.kt` 的 `CommandErrors.ALL`。新增错误码必须先写进本节，再登记常量，最后才允许在路由里使用；禁止在路由代码里写错误码字符串字面量。

协约校验：`python tools/check-module-contracts.py` 会核对本节、`CommandErrors.ALL`、`contracts/device-commands.schema.json` 与 `CommandSpec.kt` 四者一致，不一致即失败。

## 禁止项

- 禁止增加“万能动作”命令。
- 禁止使用裸坐标作为默认动作。
- 禁止在失败后自动换另一个未声明工具。
- 禁止截图循环代替系统 API。
- 禁止把异常堆栈直接发给 QQ 用户。
