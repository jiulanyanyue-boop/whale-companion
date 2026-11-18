# M05 配对与设备传输：详细开发文档

## 固定组件

```text
host/astrbot_whale_device/transport.py
host/astrbot_whale_device/protocol.py
host/astrbot_whale_device/storage.py
device/.../DeviceConnectionService.kt
device/.../DeviceStore.kt
device/.../PairingController.kt          # 进行中，task-12
device/.../KeystoreIdentity.kt           # 进行中，task-12
```

## Envelope 字段

固定字段：

```json
{
  "protocol_version": 1,
  "request_id": "uuid",
  "device_id": "device-id",
  "command": "device.*",
  "deadline_ms": 30000,
  "idempotency_key": "task-step-key",
  "counter": 1,
  "issued_at_ms": 0,
  "payload": {},
  "signature": "..."
}
```

未知字段拒绝。`deadline_ms` 固定范围 100 到 300000。计数器只能递增。

## 配对密钥

- 算法固定为 Android Keystore `EC P-256` 或平台支持的等价强算法。
- 私钥只存在 Android Keystore，不导出。
- 主机只保存公钥和 SHA-256 指纹。
- 每次请求签名覆盖去掉 `signature` 后的 canonical JSON。
- 主机身份也必须固定指纹；指纹变化进入 `PAIRING_REQUIRED`，不能自动接受。

### canonical JSON 的字节级规范（强制）

签名两端必须产出**字节完全相同**的串，否则签名永远验不过。基准实现是主机侧
`host/astrbot_whale_device/security.py` 的 `canonical_payload`：

```python
json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
```

由此确定的规则（2026-10-30 在本机 Python 3.15 上实测确认，Kotlin 侧 `CanonicalJson` 必须逐条对齐）：

| 情形 | 输出 | 备注 |
|---|---|---|
| 键顺序 | 递归按 key 排序 | 嵌套对象内部也要排序 |
| 分隔符 | `,` 与 `:` **不带空格** | `separators=(",", ":")` |
| 控制字符 `< 0x20` | `\uXXXX`（**小写**、四位补零） | 例如 `\u0001` |
| `\b \f \n \r \t` | **短形式**（`\b`、`\f`、`\n`、`\r`、`\t`） | 不是 `\u0008` 之类 |
| `0x7f`（DEL） | **原样输出，不转义** | 最易写错的一处 |
| `/` | **不转义** | 不是 `\/` |
| `"` | `\"` | |
| `\` | `\\` | |
| 非 ASCII（中文、emoji） | **原样 UTF-8 字节** | `ensure_ascii=False` |
| 整数 | `1`（无小数点） | 不做数值规范化 |
| 布尔 / null | `true` / `false` / `null`（小写） | |
| 空对象 / 空数组 | `{}` / `[]` | 不带空格 |
| `signature` 字段 | **只剔除顶层**，嵌套同名键保留 | |

验证方式：`CanonicalJsonTest` 必须内置一组刁钻输入（含 DEL、`/`、emoji、中文、转义字符、嵌套对象），
与 Python 实测输出逐字比对，并同时比较 `toByteArray(Charsets.UTF_8)` 证明字节一致。

## 连接状态机

```text
UNPAIRED → PAIRING → PAIRED_OFFLINE → CONNECTING → ONLINE
ONLINE → BACKOFF → CONNECTING
任何状态 → REVOKED
```

退避固定为 1、2、4、8、16、30 秒，最大 30 秒；成功收到 heartbeat 后清零。

## 会话恢复

设备端 Room 表固定：

```text
processed_effects(idempotencyKey PRIMARY KEY, responseJson, createdAt)
device_counters(deviceId PRIMARY KEY, lastCounter)
```

后续多来源会话增加 `epoch/seq/lease_token`，旧 lease 的写请求必须返回 `STALE_LEASE`。

## 媒体通道

图片、音频、视频不走 WebSocket。使用同一 TLS/身份配置的 HTTPS 分块上传，固定分块 1 MiB，单文件上限 100 MiB，上传完成后才发送 `asset_id`。

## 禁止项

- 禁止手机开放 HTTP 入站端口。
- 禁止生产使用无 TLS 的 WebSocket。
- 禁止只校验 `device_id` 不校验公钥指纹。
- 禁止内存 Map 作为唯一幂等或计数器存储。
- 禁止自动信任身份指纹变化。
