# 模块文档索引

这些文档是 Whale Agent v2 的实际开发规范。每个模块固定两份：

- `*-product.md`：用户可见功能、边界、流程、验收行为。
- `*-development.md`：目录、类、协议字段、状态机、依赖、测试和禁止项。

开发顺序固定为：

1. `M01` 系统模式与设备角色
2. `M04` 主人、安全与确认
3. `M05` 配对与传输
4. `M06` 固定命令运行时
5. `M07` UI 感知与无障碍
6. `M02` AstrBot、模型和人设
7. `M03` OneBot、QQ 和媒体消息
8. `M08` 相机、相册和媒体
9. `M09` 本地桌宠和语音
10. `M10` 定时任务
11. `M11` OEM 与权限向导
12. `M12` 持久化、恢复和审计
13. `M13` 测试、基准和发布

任何模块新增能力都必须同时修改该模块的两份文档、`contracts/`、主机实现、Android 实现和验收夹具。没有对应文档和固定测试的能力不得实现。

模式字段固定由 `contracts/device-mode.schema.json` 定义；设备命令和请求信封仍分别由 `device-commands.schema.json` 与 `device-envelope.schema.json` 定义。

## 外部项目吸收边界

- [`ccch1mneyyy/dsh-go`](https://github.com/ccch1mneyyy/dsh-go)：吸收无障碍 driver、动作前重新观察、桌宠状态指示、语音门控和用户 handoff 的设计思想；不直接复制 AGPL 源码或非代码素材。
- Droidrun/Mobilerun：吸收“固定设备控制入口、结构化工具、任务轨迹和模型供应商解耦”；不引入 ADB 作为生产执行路径。
- Microsoft UFO MobileAgent：吸收数据采集与动作执行分离、有限状态机、缓存失效和多设备编排；不引入双端口 ADB MCP。
- Open-AutoGLM：吸收敏感操作确认、人工接管、HarmonyOS/HDC 兼容测试和多模态 fallback；不复制坐标优先的 ADB 架构。
- AppAgent：吸收应用能力档案、探索文档和 app-specific guidance；不允许模型自行生成新工具。
- AndroidWorld：吸收动态任务、初始化/成功检查/清理三段式验收；作为测试方法，不作为运行时依赖。
- [`ccch1mneyyy/pi_pilot`](https://github.com/ccch1mneyyy/pi_pilot)：吸收 `epoch/seq/lease`、事件重放和快照恢复；用于设备会话层，不改变固定设备命令协议。

许可证和代码复用必须单独审查。外部仓库只提供设计参考，除非明确完成许可证兼容评估，否则只能重写行为，不能复制实现。
