# M13 测试、基准与发布：详细开发文档

## 固定测试目录

```text
host/tests/
device/app/src/test/
device/app/src/androidTest/
test-fixtures/
docs/modules/
```

## 每条用例格式

```text
case_id
input_message
sender_user_id
mode
expected_command
complete_payload
device_preconditions
expected_frame
expected_status
expected_db_rows
expected_audit_events
cleanup
```

缺任一字段的验收用例无效。

落地位置固定为 `test-fixtures/m13-acceptance-matrix.json`：该文件逐条给出上表 12 个字段，并用 `executed` 字段诚实标注是否已在当前环境真实执行，用 `covered_by` 指向承载断言的测试文件。`covered_by` 指向的文件必须真实存在，否则 `tools/check-module-contracts.py` 的 C10 检查直接失败。

`executed` 的判定规则（不得含糊）：

- `true` 只允许用于本机真实执行过的 Python 侧用例（`cd host && python -m pytest`）。
- `false` 表示 Kotlin/Android 侧只有源码与断言，**未经 JDK 17 + Android SDK 36 + Gradle Wrapper 编译与执行**。禁止把这类用例记作通过，也禁止在报告里写「已通过测试」。

## 必测安全集合

```text
owner_can_control
guest_prompt_injection_silent
group_never_controls
duplicate_message_once
duplicate_request_once
expired_signature_denied
wrong_host_fingerprint_denied
allowlist_outside_denied
sensitive_package_denied
local_pet_rejects_remote
remote_mode_rejects_local_controller
```

## 必测运行集合

```text
direct_api_before_a11y
tree_before_vision
stale_snapshot_denied
foreground_package_changed_denied
camera_capture_upload
photo_picker_only_user_uri
voice_push_to_talk_lifecycle
overlay_permission_fallback
screen_lock_needs_unlock
host_restart_recovery
device_process_restart_recovery
onebot_relogin_no_duplicate
```

## 外部基准吸收

AndroidWorld 的初始化/任务/成功检查/清理四段式用于动态任务夹具；AppAgent 的应用能力档案用于记录包名、入口、语义树质量和已验收动作；Open-AutoGLM 的人工接管用于高风险动作；Droidrun/Mobile Agent 的任务轨迹用于失败复盘。它们都是测试和数据方法，不作为运行时依赖。

固定吸收表：

| 来源 | 吸收内容 | 不引入 |
|---|---|---|
| `ccch1mneyyy/dsh-go` | driver、串行 worker、动作后重观察、桌宠状态、语音门控、handoff | AGPL 源码、非代码素材、其运行时依赖 |
| `ccch1mneyyy/pi_pilot` | `epoch/seq/lease`、ring replay、snapshot fallback、旧 lease fencing | 另一套设备命令协议 |
| Droidrun/Mobilerun | 固定工具入口、任务轨迹、模型供应商解耦 | ADB 作为生产执行路径 |
| Open-AutoGLM | 人工接管、敏感动作确认、多模态 fallback | 坐标优先和无限截图循环 |
| AppAgent | app capability profile、探索文档、应用专用提示 | 模型生成新工具 |
| AndroidWorld | 动态任务初始化/成功检查/清理 | 运行时依赖和云端服务 |
| Maestro | 语义选择器、可重复 UI 测试思路 | 以测试 DSL 替代生产命令协议 |

## 构建和版本

- JDK 17。
- Gradle Wrapper 8.9。
- Android Gradle Plugin 8.7.3。
- `compileSdk/targetSdk 36`、`minSdk 26`。
- 所有依赖锁版本，禁止动态版本和自动升级。

## 测试的环境无关性（强制）

测试断言**不得把本机环境当作规范**。同一条用例在「装了某个可选数据包」与「没装」两种环境下都必须通过；做不到时，必须显式按环境分支断言，并在 docstring 里说明两条分支各自要求什么。

已踩到的实例（`host/tests/test_scheduler.py`）：时区解析依赖 `zoneinfo` 的 IANA 数据，某些环境缺 `tzdata` 包。

- **错误写法**：`with pytest.raises(ScheduleConfigError): resolve_timezone("America/New_York")` —— 把「本机没装 tzdata」当成产品规范，装了 `tzdata` 之后这条立刻变成假失败。
- **正确写法**：先探测本机是否有真实时区数据，有则断言解析成功且**夏令时/冬令时偏移必须不同**（证明用的是真实 DST 规则而非固定偏移），无则断言显式拒绝。
- **唯一不变的硬要求**是「绝不猜测偏移」：没有数据时必须拒绝，不允许静默退化成某个猜出来的固定偏移。主动问候错一小时，比不问候糟糕得多。

另一个同类陷阱：真实 `zoneinfo.ZoneInfo` 的 `utcoffset(None)` 返回 `None`（无具体时刻时偏移不确定），固定偏移的 `datetime.timezone` 则始终有值。断言偏移时必须给一个**具体时刻**，否则测的是实现类型而不是行为。

## 发布禁止项

- 禁止以“模拟器通过”替代 OEM 真机矩阵。
- 禁止以截图演示替代协议和数据库断言。
- 禁止引入未写入本目录的插件、MCP、ADB 或脚本。
- 禁止把外部仓库的 AGPL/非许可证代码直接复制进生产包。
