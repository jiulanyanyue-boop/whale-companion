# M04 主人、安全与确认：详细开发文档

## 固定组件

```text
host/astrbot_whale_device/security.py
host/astrbot_whale_device/validation.py
device/.../DevicePolicy.kt
device/.../UserGate.kt              # 已实现
device/.../AuditLogger.kt           # M12
```

## 主机规则

`PrincipalGate.resolve` 固定使用：

1. `is_bot == true` → `UNKNOWN`。
2. `message_type == GROUP` → `GUEST`。
3. 私聊 `user_id == owner_user_id` → `OWNER`。
4. 其他 → `GUEST`。

只有 `OWNER + PRIVATE` 才能 `can_control=true`。

## Android 包策略

敏感标记固定包含：

```text
alipay wallet bank password authenticator otp keepass
```

系统安全包固定集合来自 `DevicePolicy.safeSystemPackages`。包名必须完全匹配，不允许前缀模糊匹配。

## 确认状态机

```text
READY
 → NEED_CONFIRM
 → APPROVED_ONCE / APPROVED_TASK / DENIED / STOPPED / TIMEOUT
```

确认卡必须由 Android 原生 UI 或无障碍 overlay 渲染。注入手势执行期间确认卡必须设置为不可点击，防止 AI 自己点击“确认”。

## 高风险命令

以下命令永远进入确认或人工接管：

```text
device.sms.send
device.system.settings
device.media.share       # 当目标是外部社交应用
device.ui.input          # 目标是密码/验证码/支付字段
device.ui.tap            # 目标是发布/删除/付款按钮
```

## 签名和重放

生产实现必须使用 Android Keystore 设备身份、公钥绑定、时间窗、单调计数器和幂等键。当前 Python HMAC 只允许测试桥接，不得作为最终公网方案。

## 禁止项

- 禁止把“主人”作为模型字段。
- 禁止把语音识别结果当作身份凭据。
- 未开启手机端「允许自动解锁」时禁止自动填写锁屏密码；开启后仅从 Android Keystore 解密本机凭据，禁止通过协议传入密码。
- 禁止保存密码、验证码、支付密码或完整屏幕敏感内容。
- 禁止用一个全局 `ALLOW_ALL` 绕过应用和命令级规则。
