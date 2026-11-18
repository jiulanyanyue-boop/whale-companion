# Whale Companion · 小鲸伴

基于 AstrBot 的 QQ 助手、Android 手机控制和桌面整合开发包。保留四个插件和 AstrBot 定制补丁，用于另一台电脑继续开发。

## 新电脑安装（Windows 10/11 x64）

安装 Git、Python 3.12（包含 Windows Python Launcher）、Microsoft Edge WebView2 Runtime，确保可访问 GitHub 和 PyPI（可选本地模型另需访问模型下载站）。在 PowerShell 执行：

~~~powershell
git clone https://github.com/jiulanyanyue-boop/whale-companion.git
cd whale-companion
$ownerQQ = Read-Host "输入主人 QQ 号"
powershell -ExecutionPolicy Bypass -File .\Install.ps1 -OwnerQQ $ownerQQ
.\dist-exe\WhaleAgentHost.exe
~~~

安装器创建隔离 Python 环境，获取固定版本 AstrBot、应用补丁、装配四个插件、安装依赖、生成新凭据并构建 EXE。首次下载需要网络和磁盘空间。已经存在的本地配置不随 Git 更新覆盖。

首次打开后，在 AstrBot 设置自己的模型和 API key，登录助手 QQ，安装 Android 客户端并重新配对手机。主人 QQ 与助手 QQ 是两个角色。登录态、设备身份、锁屏凭据需要在新设备重新建立，不能从公开仓库恢复。

这是源码整合包，不含已构建 EXE/APK，也不是已在所有电脑验收的免配置安装包。

## 复用 AstrBot 官方安装 EXE

可以用 [官方 Launcher](https://github.com/AstrBotDevs/astrbot-launcher) 安装普通 AstrBot 作为独立插件宿主，但官方安装器不包含本仓库框架补丁与桌面服务。完整整合模式使用 Install.ps1，避免两套实例争用端口。不要复制旧电脑的安装目录、账号数据库或内嵌环境。

## 开发与更新

- host/：主机服务、桌面、权限、设备协议和回归测试。
- device/：Android 工程；用 Android Studio 打开，安装 SDK 36、JDK 17，使用 Gradle 8.9 构建 `:app:assembleDebug`。旧手工脚本需要 ANDROID_HOME、JAVA_HOME、WHALE_KOTLIN_LIB 和其声明的 SDK 34 工具链。
- plugin-sources/：四个插件的维护源码，安装器复制到 runtime/astrbot/data/plugins。
- patches/astrbot.patch：固定上游版本上的改动，不能省略或盲目用于新版。
- tools/qq-call/：可选 WSL2 Ubuntu 通话后端；需另装 Linux QQ、NapCat 和音频依赖，见其 README。
- docs/modules/：模块规范，历史验收文字只代表原环境。

关闭本项目服务后重新执行 Install.ps1 更新。修改插件请编辑 plugin-sources，再重新安装；不要只编辑会被覆盖的 runtime 副本。本地配置和身份保存在 Git 忽略的目录。

~~~powershell
.\.verify\setup-venv\Scripts\python.exe -m pip install -e .\host[test]
.\.verify\setup-venv\Scripts\python.exe -m pytest .\host\tests -q
.\.verify\setup-venv\Scripts\python.exe tools/check-release.py
~~~

## 可选配置和验收边界

- 已有私有素材仓库访问权时，在安装命令末尾添加 `-IncludePrivateMemes`。Git 会使用你的 GitHub 登录态获取图片和检索描述，并在本机重建索引。普通安装无需访问私有仓库。
- 需要使用 LivingMemory 的本地向量检索时，可在安装命令末尾加 `-DownloadEmbeddingModel` 预下载模型，或在 AstrBot 配置其他嵌入服务。模型下载站不可用时可先完成普通安装，之后再配置检索。默认不采集未提及机器人的群消息；有需要可以在 LivingMemory 设置中开启。

- 语音条复用 NapCat/AstrBot STT、TTS。本地识别模型需显式执行 runtime/astrbot-venv/Scripts/python.exe tools/setup-voice.py --download-model；该步骤生成系统测试音色，需要 Windows 中文语音，可自行配置有授权的音色。
- QQ 来电桥仍需 WSL2/Linux 安装和真实通话验收，接口及虚拟音频测试不等同真实通话成功。
- 公网需要自己的域名、TLS 和隧道，不提供旧机器服务器或 SSH 身份。表情、母图、图片服务及即梦凭据自行配置（凭据文件通过 `WHALE_JIMENG_CREDENTIALS` 指定），本地检索测试不等同参考图生成成功。
- DSH 功能需另装并启动 DSH，登录自己的渠道账号。
- APK 在新电脑生成自己的 debug 签名，覆盖旧签名可能需卸载，卸载会清除手机本地数据。正式发布请自行管理 release 签名。
- 实机功能需实机确认，验证记录见 VERIFICATION.md。

## 开源与隐私

新增代码 AGPL-3.0-or-later；保留第三方版权及许可，见 LICENSE、NOTICE、THIRD_PARTY_NOTICES.md、MODIFICATIONS.md。AGPL 允许商用，并要求适用情形开放对应源码。用户可通过主机页面底部链接或 QQ `/whale_source` 获取。对外运行修改版时，请将两处源码链接更新为实际部署版本的对应源码。

不包含模型 key、QQ 登录态、聊天/人设数据库、锁屏凭据、证书私钥、SSH 身份、真实域名或私人素材。运行目录由 .gitignore 排除，不要强制加入 Git。
