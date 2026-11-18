# M06 设备命令运行时：产品功能文档

## 固定能力

```text
device.app.list
device.app.launch
device.ui.observe
device.screen.capture
device.ui.tap
device.ui.long_press
device.ui.scroll
device.ui.input
device.ui.back
device.ui.home
device.ui.recents
device.ui.notifications
device.system.settings
device.camera.capture
device.media.pick
device.gallery.list
device.gallery.read
device.media.upload
device.media.share
device.telephony.dial
device.contacts.search
device.sms.compose
device.sms.send
device.device_status
device.system.unlock
```

## 执行优先级

```text
系统 API / Intent
→ 应用专用适配器
→ 无障碍语义树
→ 树 + 标记图
→ 视觉模式
→ 暂停或人工接管
```

模型不能选择执行层。设备根据命令、应用能力档案和失败原因自动选择。

## 结果

只返回：`ok`、`denied`、`queued`、`failed`、`needs_unlock`。每个结果必须有 `request_id`、`device_id`、错误码、消息、artifact 和 evidence。

## 用户体验

- 成功：告诉用户完成了什么。
- 拒绝：告诉用户是权限、白名单、风险还是状态原因。
- 等待解锁：不继续尝试，不保存密码。
- 无法确认：暂停，不“多点几下”。

## 禁止能力

不提供 `shell`、`adb`、`eval`、`execute_code`、`raw_sql`、`arbitrary_http` 或任意未声明工具。
