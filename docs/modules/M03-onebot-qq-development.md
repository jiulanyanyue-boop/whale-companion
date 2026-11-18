# M03 OneBot 与 QQ：详细开发文档

## 固定适配层

实现文件固定为：

```text
host/astrbot_whale_device/onebot.py
host/astrbot_whale_device/plugin.py
```

首版 OneBot v11 连接参数由 AstrBot 管理。插件只接收事件和发送回调，不自行创建第二套 QQ 客户端。

## 入站字段

`parse_event` 只读取：

```text
post_type
message_type
message_id
user_id
group_id
message
sender.nickname
self_id
```

缺少 `message_id` 或 `user_id` 直接拒绝。`message_type` 只允许 `private/group`。`message` 只允许字符串或 OneBot segment 数组。

## 出站消息段

`OneBotSender.send_private` 只允许：

```json
{"type":"text","data":{"text":"..."}}
{"type":"face","data":{"id":"..."}}
{"type":"image","data":{"file":"..."}}
{"type":"record","data":{"file":"..."}}
{"type":"video","data":{"file":"..."}}
```

未知 segment 直接抛错，不降级为文本 URL。文件必须来自 `AssetStore` 或明确允许的本地素材目录。

## 事件处理顺序

1. 丢弃非 message 事件。
2. 丢弃机器人自发消息。
3. 写入 `inbox(message_id,user_id)`。
4. 检查 message_id 的所有者是否一致。
5. `PrincipalGate` 判定主人/客人/群聊。
6. 生成不含设备工具的客人上下文，或含设备工具的主人上下文。
7. 保存最终 effect，重复事件只返回已保存 effect。

## 掉线恢复

- OneBot 连接状态写入审计和 `devices`/主机状态。
- 发送失败只重试网络层，最多 3 次，使用相同消息幂等键。
- 不重新调用模型生成第二条内容。
- 重新登录后先查询已发送消息 effect，再恢复未发送的固定出站动作。

## 禁止项

- 禁止用 Android QQ UI 收消息作为默认实现。
- 禁止在群聊中根据 `@`、昵称或群主身份获得设备权限。
- 禁止用昵称替代 `user_id`。
- 禁止把 QQ 消息内容直接拼入设备命令。
