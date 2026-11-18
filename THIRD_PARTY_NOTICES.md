# 第三方来源与许可

| 组件 | 来源 | 许可与处理 |
|---|---|---|
| AstrBot | https://github.com/AstrBotDevs/AstrBot | AGPL-3.0；提交见 UPSTREAM.json，原许可在 third-party/astrbot/LICENSE，修改在 patches/astrbot.patch |
| LivingMemory | https://github.com/lxfight-s-Astrbot-Plugins/astrbot_plugin_livingmemory | AGPL-3.0；保留 plugin-sources/astrbot_plugin_livingmemory/LICENSE 及原作者信息；含本地修改 |
| 心念 | https://github.com/AstraSolis/astrbot_proactive_reply | AGPL-3.0；保留 plugin-sources/astrbot_proactive_reply/LICENSE 及 AstraSolis 署名；本地安装源码快照 |
| QQ 通话桥 | https://github.com/ClaudiaGardner/maibot-qq-voice-call | GPL-3.0-only，固定提交 22f30c021cd3170f75af9dff66cc959a07ebda4b；提供获取及适配工具，不打包其运行二进制 |
| NapCat | https://github.com/NapNeko/NapCatQQ | 安装时从原项目取得，遵守下载版本自带许可；不含本机运行副本 |
| QQ / AVSDK | 腾讯官方发行 | 专有组件，不在源码仓库内，用户从官方渠道取得 |
| Lucide / 前端依赖 | LivingMemory 内含供应商文件 | 原 LICENSE/NOTICE 随源码保留 |

Python、Android 与前端依赖清单保留在 pyproject.toml、requirements.txt、Gradle、package.json，各依赖遵守原许可。没有给上游代码重新授权。新增整合代码使用根目录 AGPL-3.0-or-later；上游有更具体许可的文件以原许可为准。

向他人分发修改版或通过网络提供修改版服务时，应按适用许可提供对应源码及修改说明。Whale 主机页面提供仓库源码链接；部署自己的修改版时应改为对应版本的源码链接。源码公开不包括密钥、账号、聊天记录或私人素材。
