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

### 桌面启动故障排查

2026-11-19 修复了 Windows UTF-8 BOM 配置导致的启动失败：现在 `host.json` 同时接受有 BOM 和无 BOM 的 UTF-8，读取不会重写配置。桌面会等 WebView2 初始页面加载后再启动服务，避免快速失败提示被初始加载页覆盖。失败页提供重试和日志位置，不再只剩“正在启动”。

- 桌面启动异常看 `runtime/logs/desktop-shell.log`，只记录异常类型和栈位置，不记录异常消息、源码行或局部变量，避免凭据进入诊断日志。
- AstrBot 子进程输出看 `runtime/logs/desktop-launcher.log`；后端详细运行情况看 `runtime/logs/astrbot.log`。分享日志前仍需自行检查后端及第三方插件输出中的私人信息。
- 缺少模型 API key 影响模型加载与对话，不应该阻止打开管理页面；启动后在“模型提供商”设置自己的凭据。
- 新电脑必须安装 WebView2 Runtime，不能把安装了 Edge 浏览器当成已满足条件，见 [微软部署说明](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)。本项目没有捆绑完整的 WebView2 Runtime。
- 在新电脑重新克隆并运行安装器，不要搬运旧电脑的 Python 虚拟环境。当前配置中的证书路径及虚拟环境依赖安装位置，移动整个安装目录不是受支持的迁移方式。
- 安装目录应允许当前用户写入配置、浏览器档案和日志；不要同时启动另一套占用 6185 或 8767 的 AstrBot/Whale。

安装器原本生成无 BOM 的 UTF-8，因此全新安装不必然遇到本次故障；旧版本配置被 Windows 工具另存为带 BOM 后仍可能触发。新电脑需要使用包含本修复的源码重新构建，仅复制旧 EXE 或拉取尚未更新的远端版本不会获得修复。本机验证范围和跨机器边界见 `VERIFICATION.md`。

### Whale 主机侧栏入口

“Whale 主机”和“STS2 联机”是同一插件的两个独立页面。固定版 AstrBot 原平铺菜单只取排序后的第一页，新增 `sts2` 后会隐藏原 `whale` 入口，但主机页面和配置并未删除。安装器现在会应用一个固定摘要校验的菜单兼容补丁，平铺模式显示所有页面；分组模式、标题解析、置顶偏好和其他页面行为保持原样，不需要清除浏览器数据或重新配置账号。

已有安装只需更新源码后执行以下局部修复，无需重装依赖或重建桌面 EXE：

~~~powershell
.\.verify\setup-venv\Scripts\python.exe .\tools\patch_dashboard_sidebar.py --dashboard .\runtime\astrbot\astrbot\dashboard\dist --source .\runtime\astrbot\dashboard\src\composables\usePluginSidebarItems.ts
~~~

修复不停止后台。普通浏览器重新加载管理页即可；已打开的桌面壳若仍显示旧菜单，须退出托盘应用后重新打开以载入新脚本，这会短暂重连服务。补丁遇到未知或手动修改过的前端资产时会拒绝修改，不应强行套用到其他版本。

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

## Slay the Spire 2 联机

打开 AstrBot 的“插件 → Whale 主机 → STS2 联机”。独立视图位于 `views/sts2`，支持两种模式，所有安装和启动操作都在同一页，不另起 Python MCP server。

点击“一键配置当前模式”：优先使用已保存目录，否则读取 Windows Steam 注册表和 `libraryfolders.vdf` 自动定位；下载并校验 STS2MCP，写入已选 API 端口（默认 15526），开启主人私聊游戏工具。LAN 模式还会安装固定 LanSpire v0.11.0。不会启动游戏、修改存档、改防火墙或安装 VPN。找不到游戏时，填写包含 `SlayTheSpire2.exe` 的目录、保存，再点一键配置。变更 API 端口后重新安装/更新 STS2MCP。

### 本机双人 FastMP

1. 保持 Steam 运行，游戏使用窗口模式，关闭已有游戏窗口。选择“本机双人”，一键配置。
2. 点击“启动 AI 房主”；首次允许游戏加载 STS2_MCP，按游戏提示关闭后仍从 Whale 重启。
3. 第一个窗口进入“多人 → 创建 / Host”建房。
4. 点击“启动本机加入端”，第二个窗口进入“多人 → 加入 / Join”，自动连接 `127.0.0.1:33771`。
5. 双方选角色、准备并开局。AI 控制第一个窗口的本地玩家，你控制第二个窗口。

两个窗口都带 `--fastmp`，使用 ENet 本机传输，不是 Steam 邀请房间。参考 [sts2-local-coop](https://github.com/ing-gom/sts2-local-coop)，本项目未实机验收双开。

### 本机双开加远程朋友：LAN 实验模式

1. 在“STS2 联机”页选择“本机双开 + 远程朋友 · LAN 实验模式”，点击“一键配置当前模式”。固定安装 [LanSpire v0.11.0](https://github.com/Madlezz/LanSpire/releases/tag/v0.11.0)，发布 ZIP 的固定 SHA-256 见 UPSTREAM.json，仅安装 DLL/manifest 和原 MIT 许可，不执行上游脚本。
2. 首次在游戏中启用 STS2_MCP 和 LanSpire，按提示重启。不要与其他 LAN 联机 Mod 同时启用。
3. 点击“启动 AI 房主”，为 AI 手动选一个独立游戏档案，在多人页选择“创建局域网 / Host LAN”，进入角色选择房间。
4. 点击“启动本机加入端”，为你自己选另一个游戏档案，在“加入局域网 / Join LAN”中填写 `127.0.0.1:33771`，点击 LAN 加入按钮。LAN 模式不会带 `--fastmp`，避免绕过 LanSpire 的 LAN 流程。
5. 页面“远程朋友加入”选择入口：家宽公网直连、服务器 UDP 中转或同一局域网，填写地址与外部游戏端口后保存，再复制好友说明。异地公网联机不要求虚拟局域网；已有 VPN 仍可使用其地址。网卡候选仅本机枚举，不代表公网或已确认可达。
6. 朋友使用相同游戏、LanSpire 和其他游戏性 Mod 版本，在 Join LAN 中填写分享地址和外部 UDP 端口，不使用 Steam 邀请。本机加入端仍固定 `127.0.0.1:33771`。Whale 不自动配置路由器、虚拟局域网或系统防火墙。
7. 确认房间里出现三个不同玩家，再分别选择角色、准备并开局。容量配置为 4，支持尝试 AI、你、朋友三人的组合；不要在第三人到齐前开局。地图、共同事件、宝箱和回合结束可能需要所有玩家投票。

LanSpire 的共享配置在 `%APPDATA%/SlayTheSpire2/LanSpire/lan_config.json`，上游会动态重读。Whale 不在两个进程启动间切换自定义身份；发现已有自定义 LAN 玩家 ID 时拒绝自动配置，需先在游戏内关闭该选项再退出重试。两个窗口必须使用不同游戏档案；Whale 没有隔离整个游戏用户目录，不宣称已经解决所有双开存档/身份行为。已有配置在插件数据目录 `sts2/lan-config-before-whale.json` 备份，页面显示路径；卸载只移除摘要仍匹配的受管 LAN 文件，保留配置和备份。

LanSpire 自述仅对部分旧游戏版本完成编译检查，完整多人流程仍在验证；固定版不等于保证兼容当前游戏。该组合是实验路线，双开身份、三人加入、存档和同步均未实机验收。STS2MCP 目前没有 LanSpire 新菜单按钮的专用接口，因此 LAN 开房、加入和选档由用户手动操作，AI 只在多人局中继续调用现有动作；菜单工具不会误走 Steam 开局。

### 家宽公网直连（无需 VPN）

1. 在 LanSpire 模式下选择“家宽公网 IPv4 / 域名直连”。填写实际公网 IPv4 或解析到它的 ASCII 域名，外部游戏端口默认 33771，保存。
2. 对照路由器 WAN IPv4 和运营商实际公网分配，确认不是内网、CGNAT（100.64/10）或未处理的双重 NAT。Whale 不自动探测公网 IP，也不保证运营商允许 UDP 入站。
3. 在路由器手动配置 **外部 UDP 端口 → 房主内网 IPv4:33771**；给房主内网地址做 DHCP 保留，并在主机防火墙允许游戏 UDP 入站。外部端口可以与本机端口不同，不要映射 MCP API 或管理页面。
4. AI 在 Host LAN 建房，你的另一窗口加入 `127.0.0.1:33771`；远程朋友在 Join LAN 填公网地址及外部端口，不用 VPN 或 frp。复制说明会自动带上已保存入口。

TCP 端口检测不能证明游戏 UDP 可用，真正验收需由外网朋友实际入房。本机不使用公网入口回连，因此不依赖路由器 NAT loopback。动态公网 IPv4 变化后需更新分享地址或自行维护 DDNS。

### 自有公网服务器 UDP 中转（预备方案）

这条路线无需家宽入站映射，不是虚拟局域网。数据路径为：朋友 → 服务器 UDP 外部端口 → frp TCP/TLS 通道 → 房主本机 UDP 33771。只代理游戏，不代理 MCP 或 Whale 管理端。frp UDP 配置依据[官方文档](https://github.com/fatedier/frp/tree/v0.72.0)。

1. 在同一 STS2 页选择服务器中转，填服务器公网 IPv4/域名、控制端口 TCP 7000 和外部游戏 UDP 33771（均可改），保存。
2. 点击“生成新部署包并下载”。本机离线生成随机 token、专用 CA 签发的服务器证书、配套 frpc 配置和 Linux x64 服务端 ZIP。客户端固定校验专用 CA，不跳过服务器证书验证；不向系统安装根证书。生成和下载不代表已部署服务器。
3. 用自己的 SSH/SFTP 将 ZIP 上传到私有目录，以 `umask 077` 解压；执行 `sh install.sh`，脚本下载官方 frp 0.72.0 Linux x64 包，核对固定 SHA-256 后安装 frps 与原 Apache-2.0 LICENSE。已有 frps 时拒绝覆盖，不自动启动服务或修改防火墙。
4. 手动在云安全组和服务器防火墙放行控制 TCP 端口与外部游戏 UDP 端口，控制端口建议限制房主出口 IP。解压目录执行 `./frps -c ./frps.toml`；长期运行可自行配 systemd，`WorkingDirectory` 必须为解压目录。
5. 回页面点击“安装中转客户端”，从官方发布获取 Windows x64 固定包并核对摘要，不执行下载的脚本。然后点击“启动中转”，按页面显示的本机日志路径及服务器 `frps.log` 检查登录和代理注册。客户端进程运行不等于 UDP 可达或联机成功。
6. 按 LanSpire 步骤启动 AI 房主与本机加入端；朋友填服务器地址和外部 UDP 端口，不需要安装 frp。页面可复制不含凭据的好友说明。

服务端配置只允许所选单个远端端口，不开放 frp 管理面板；客户端只有固定游戏 UDP 代理。token、服务器私钥和部署包保存在被忽略的插件数据 `sts2/relay/` 中，普通状态接口不返回内容；部署包只在明确生成/下载操作返回。它是私有管理资料，不要发给朋友或提交 Git。Windows 文件权限沿用用户数据目录 ACL，并非额外加密的凭据库。

重复下载当前部署包不改变凭据；“生成新部署包”会更换 token/证书，需要完整重新部署，旧私有版本保留供恢复。证书有效期 825 天，包内 README 给出到期时间。改服务器地址或端口后旧配置不能直接启动，需重新生成并部署。页面可停止受管中转，插件正常退出也会停止它；不自动随应用启动，不接管手动 frpc，不自动部署远端。异常断电/强制结束进程后仍需检查残留进程。源码与本机运行副本已装配，不表示服务器已部署或真实远程三人联机已经验收。

### AI 功能与边界

在主人 QQ 私聊说“读取当前 STS2 多人局，只控制你自己的角色，决定并执行动作，每次操作后重新读取状态”。当前是对话工具调用，不是持续后台打牌循环。

本次接入多人状态和 29 个白名单动作：出牌/目标/药水、结束及撤回回合投票、奖励、地图、事件、营火、商店、卡牌/遗物/组合选择、宝箱和水晶球事件。没有开放档案删除、单人局操控或 Wiki 工具。[STS2MCP 文档](https://github.com/Gennadiyev/STS2MCP/blob/main/docs/raw-full.md)将多人支持标为 beta。

MCP API 始终只使用回环地址，不能把 15526 转发或分享给朋友。两个窗口共享 Mod 配置，由先启动的 AI 房主占用 API 端口；加入端监听同端口可能失败，上游捕获该错误。不要关闭房主后继续让 AI 调用，以免控制目标混淆。停止按钮仅处理 PID、创建时间、可执行路径和启动参数匹配的受管进程，不终止手动启动的窗口。

## 开发检查

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
