# QQ 来电语音：Linux 音频桥 + 现有 AstrBot

当前本机使用 Ubuntu WSL2，Windows 继续承载现有 AstrBot、聊天模型、本地识别和临时中文音色。Linux 只负责 QQ 通话与隔离虚拟音频设备。长期常驻部署可将相同桥接组件放在 Linux 主机，但目前客户端只允许本机回环连接；不能直接填写远程公网地址。

## 当前能力与验收边界

- 仅配置中的主人 QQ 来电可以自动接听，且 AI 工作进程必须持续发送心跳。其他来电保持未接听。
- 以桥接回执 `connected` 确认接通；`accepted`、HTTP 存活及进程已启动均不算接通。
- 兼容本机 NapCat 插件白名单时只添加固定的通话桥插件 ID，保留其余插件检查并备份原文件；版本锚点改变则停止修改。原生登录只在明确失败回执后重试，避免启动通知导致反复登录。
- 来电语音按句切分，复用语音条识别服务，再调用现有聊天模型与主人专属人设，返回音频到 QQ 麦克风。
- 插话停止当前播放并作废旧回复。挂断或更换通话后丢弃未播放回复。只有音频播放完成才进入本次通话上下文。
- 目前没有主动拨号、原生挂断、来电拒接 API，不能向 AI 宣称已有这些能力。通话上下文暂存在内存中，不自动写入长期记忆，也不执行设备工具。
- 目前已验证原生 `postMessage` 接口加载、回环鉴权、虚拟音频完整往返及自动化测试。真实来电、远端收听、插话效果、重连和延迟仍需登录后的端到端验收。

## 日常使用

QQ 登录统一在现有 AstrBot 的“Whale 主机 → QQ 助手”中管理。点击“重新获取二维码”即可刷新，页面打开时会定期更新二维码。通话与文字聊天共用同一 Linux QQ 身份，不再要求两边各自登录。Linux 登录服务固定使用 `6109`，不占用原 Windows 服务的 `6099`。

现有账号控制器、自动登录偏好、登录/下线命令与状态展示继续复用；Linux 只是其可选运行后端。OneBot 经本机转发接入原来的 `whale_qq` 适配器，因此聊天、人设、记忆和工具权限沿用原链路。没有启用 Linux 后端的安装仍默认使用 Windows 登录服务。

在项目根目录使用项目自带 Python：

```powershell
runtime/astrbot-venv/Scripts/python.exe tools/qq-call/manage.py start
runtime/astrbot-venv/Scripts/python.exe tools/qq-call/manage.py status
runtime/astrbot-venv/Scripts/python.exe tools/qq-call/manage.py login-code
```

最后一条命令仅供排错，会通过与 AstrBot 相同的账号控制器获取新码，保存到 `.verify/qq-call-login.png`。日常直接扫描 AstrBot 页面中的码。首次切换至 Linux QQ 时可能使同一账号的旧 Windows QQ 下线。

```powershell
runtime/astrbot-venv/Scripts/python.exe tools/qq-call/manage.py enable
```

启用配置在下次 AstrBot 启动时生效。重启 AstrBot 后，用主人账号给助手发起 QQ 私人语音通话。先确认来电接通、双方音频、识别与回复，再检查打断和挂断后停止发声。

停止本工具启动的进程：

```powershell
runtime/astrbot-venv/Scripts/python.exe tools/qq-call/manage.py stop
```

不停止其他 QQ 进程。Windows/WSL 重启后需要重新启动音频桥。当前未安装开机自启。

## 安装与维护

运行文件保存在 Ubuntu 的 `~/.local/share/whale-qq-call/`。Linux QQ 使用官方 DEB 解包到该目录，不覆盖系统 QQ。Windows 侧凭据只保存在插件私有数据目录，导出器排除 `.token` 文件。

桥接源码来自 [maibot-qq-voice-call](https://github.com/ClaudiaGardner/maibot-qq-voice-call)，固定提交 `22f30c021cd3170f75af9dff66cc959a07ebda4b`，上游许可证 GPL-3.0-only。`prepare.py` 调用其可逆安装器，随后添加主人 QQ 与 AI 心跳检查，并纠正原生模块就绪检测。上游源码与其许可证保存在下载目录，不把 QQ 或 AVSDK 二进制加入源码导出。

本次使用官方 Linux QQ `3.2.34-53644` 与本机已有 NapCat `4.18.42`。安装记录含实际 NapCat SHA-256。QQ 包从腾讯官方配置中的地址取得，按官方下载页的 GetSign 流程取得临时下载链接。QQ 或 NapCat 版本变化后需要重新进行全链路验收，不能只检查服务端口。

Ubuntu 依赖：`pulseaudio pulseaudio-utils xvfb xauth libnss3 libatk-bridge2.0-0 libgtk-3-0 libgbm1 libasound2t64 libxss1 libxkbcommon0 libdrm2 libnotify4 libsecret-1-0 libxtst6 libpulse-mainloop-glib0 libopengl0 nodejs unzip ca-certificates`。

已将官方 QQ 包解包到 `~/.local/share/whale-qq-call/qq-package` 后，用 Linux Python 运行：

```bash
python3 /mnt/c/projects/whale-companion/tools/qq-call/prepare.py \
  --project /mnt/c/projects/whale-companion \
  --upstream /mnt/c/projects/whale-companion/.verify/qq-voice-upstream
```

`--upstream` 必须是上述固定提交的源码。`prepare.py` 会从当前安装的 NapCat 复制代码与 Linux 原生模块，不复制账号、配置或登录态。首次安装保留 QQ 原始 `package.json`，上游安装器另保留 Loader 基线。

## 开发验证

```bash
python3 /mnt/c/projects/whale-companion/tools/qq-call/test_gateway.py
python3 /mnt/c/projects/whale-companion/tools/qq-call/probe_audio.py
```

音频探针使用内存中的测试通话状态，仅检验虚拟音频，不拨打 QQ，不连接模型。网关仅监听 `127.0.0.1:6112`，原生模块与桥分别使用 `6111`、`6110`；三者都需要独立本机凭据。音频和转写不写入网关日志。
