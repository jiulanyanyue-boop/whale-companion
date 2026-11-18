"""一键启动的**纯逻辑**：把「缺什么」翻成人话，并幂等地生成配置。

## 为什么需要它

`python -m astrbot_whale_device.standalone --config host.json` 能跑，但它对使用者
不友好：参数要记、路径要对，最要命的是**缺配置时会抛 Python traceback**——
使用者看到的是 `ConfigError: signing secret is not set; export ...`，而不是
「你缺 `WHALE_SIGNING_SECRET`，这样设」。

本模块只做三件事，全部是**无副作用的纯函数**（除了 [ensure_config_file] 写文件）：

1. [check_environment]：Python 版本、`websockets`、三个密钥环境变量——**只看在不在**；
2. [validate_config_file]：调 `HostConfig.load`，把它的异常翻成可照做的中文；
3. [ensure_config_file]：没有配置就生成一份，**已有就一个字都不改**。

真正的启动（装配服务端、轮询 OneBot）仍然由 `standalone.main()` 负责，本模块
**不重复实现**任何启动逻辑，只负责「进门前先把鞋擦干净」。

## 密钥策略：只报名字，绝不报值

这份模块被 `host/run_host.py` 直接打印到终端，所以有一条硬规矩：

> 报告里只允许出现**环境变量名**（如 `WHALE_SIGNING_SECRET`），
> `os.environ` 里的**值**一个字符都不许进返回结构、不许进打印文本。

实现上靠「结构上做不到」而不是「记得别写」：本模块没有任何函数把
`environ[name]` 的返回值塞进 [CheckItem]，判断存在性只用 `bool(env.get(name))`。
`tests/test_quickstart.py` 里有一条专门的泄漏断言盯着这件事。

## 退出码约定（由 run_host 使用）

| 码 | 含义 |
| --- | --- |
| 0 | 全部就绪（或 `--check` 检查通过） |
| 1 | 前置未就绪：缺密钥 / 版本过低 / 配置刚生成待编辑 |
| 2 | 配置文件存在但**不合法**（需要人改配置） |
| 3 | 配置合法但装配/启动失败（例如 TLS 证书文件不存在） |
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .host_config import HostConfig

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 核心包 `pyproject.toml` 的 `requires-python`。注意这**不是** 3.12——
#: 3.12 是「在 AstrBot 里跑」的要求，独立宿主按核心包的口径检查即可。
MIN_PYTHON: tuple[int, int] = (3, 11)

#: 生成模板时默认使用的环境变量名（与 `examples/host.example.json` 一致）。
DEFAULT_SIGNING_SECRET_ENV = "WHALE_SIGNING_SECRET"
DEFAULT_MODEL_KEY_ENV = "WHALE_MODEL_KEY"
DEFAULT_ONEBOT_TOKEN_ENV = "NAPCAT_TOKEN"

#: 生成配置后需要使用者亲手改的字段（打印给人看）。
FIELDS_TO_EDIT: tuple[str, ...] = (
    "owner_user_id —— 主人 QQ 号，纯数字（昵称不算，M04 硬要求）",
    "device_id —— 手机端 device_connection.device_id 必须一模一样",
    "allowed_packages —— 允许被控制的 App 包名",
    "onebot.http_url —— NapCat 的 OneBot v11 HTTP 地址",
    "device_server.port / certfile / keyfile —— 手机连上来的端口，以及生产必填的证书",
)

#: 手机端该填的地址怎么算出来（这是新手最容易卡住的一步）。
#: `device_server.host` 默认为 `0.0.0.0`（监听所有网卡），但手机**不能**填 0.0.0.0，
#: 必须填主机的局域网 IP。这里给出获取方法与填法，避免用户以为「填了就能连」却连不上。
PHONE_ADDRESS_HINT: tuple[str, ...] = (
    "手机端 App 里「主机地址」该填什么：",
    "  1. 在主机上查本机局域网 IP：PowerShell 执行 "
    "`Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '127.*' }`",
    "  2. 取 WLAN/以太网 那一行的 IP（如 192.168.1.100），**不要**用 127.0.0.1 或 0.0.0.0",
    "  3. 手机端填：ws://<那个 IP>:<device_server.port>  （本机试验用 ws://；生产用 wss://）",
    "  4. 手机与主机必须在同一局域网，且主机防火墙要放行该端口",
    "为什么不能填 127.0.0.1：那表示「手机自己」，手机连不到主机。",
    "为什么不能填 0.0.0.0：那是「监听所有网卡」的绑定地址，不是一个可连接的目标地址。",
)

#: 内置配置模板（等价于 `examples/host.example.json`，但去掉所有 `_` 前缀注解——
#: `HostConfig` 拒绝未知键，注解键会让生成出来的配置文件**自己都加载不了**）。
DEFAULT_TEMPLATE: dict[str, Any] = {
    "owner_user_id": "10001",
    "device_id": "device-b-01",
    "allowed_packages": ["com.android.settings", "com.android.chrome"],
    "blocked_packages": [],
    "signing_secret_env": DEFAULT_SIGNING_SECRET_ENV,
    "model": {
        "provider_id": "openai",
        "model_id": "gpt-4o-mini",
        "base_url": "https://api.openai.com",
        "fallback_model_id": "",
        "persona_id": "",
        "language": "zh-CN",
        "max_context_tokens": 8192,
        "tts_provider_id": "",
        "allow_proactive_greeting": False,
    },
    "model_api_key_env": DEFAULT_MODEL_KEY_ENV,
    "onebot": {
        "http_url": "http://127.0.0.1:3000",
        "access_token_env": DEFAULT_ONEBOT_TOKEN_ENV,
    },
    "device_server": {
        # 默认监听 `0.0.0.0` 而不是 `127.0.0.1`：手机是另一个设备，
        # 用 127.0.0.1 手机根本连不上（那只接受本机回环连接）。
        # 这是「填完信息就能启动」的关键默认值——用户不该先踩这个坑。
        # 安全提示：0.0.0.0 会暴露到局域网，生产必须配 TLS 证书。
        "host": "0.0.0.0",
        "port": 8765,
        "certfile": "",
        "keyfile": "",
    },
}

#: 打印用的「怎么设环境变量」（Windows / POSIX 都给，避免使用者猜）。
def _env_fix_text(name: str) -> str:
    return (
        f"PowerShell 里执行：$env:{name} = \"<你的密钥>\"\n"
        f"bash / zsh 里执行：export {name}=\"<你的密钥>\"\n"
        f"只想临时试一次：$env:{name}=\"...\"; python run_host.py"
    )


def _in_host_dir(command: str) -> str:
    """把一条命令包成「在 host 目录下执行」的说明。"""
    return f"在仓库的 host 目录下执行：{command}"


# ---------------------------------------------------------------------------
# 检查项
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckItem:
    """一条人话检查结果。

    `key` 只是**标识**（环境变量名 / 配置字段名 / 检查项名），
    `message` 说明白「缺了什么、为什么必须补」，`fix` 是能直接照做的动作。

    结构里**没有**任何字段可以承载密钥值——这是刻意的：泄漏不是靠自觉，
    而是靠「想放也没地方放」。
    """

    key: str
    message: str
    fix: str
    severity: str = "blocker"

    @property
    def blocking(self) -> bool:
        return self.severity == "blocker"


@dataclass(frozen=True)
class BootstrapResult:
    """`ensure_config_file` 的结果：生成与否 + 要改哪些字段。"""

    path: Path
    created: bool
    fields: tuple[str, ...] = ()

    @property
    def message(self) -> str:
        if not self.created:
            return f"配置文件已存在，保持原样（不覆盖）：{self.path}"
        lines = [f"已生成配置文件：{self.path}", "请先编辑这几个字段："]
        lines += [f"  - {item}" for item in (self.fields or FIELDS_TO_EDIT)]
        # 手机端该填什么地址，是新手最容易卡住的一步，生成配置时就一次讲清楚。
        lines.append("")
        lines += list(PHONE_ADDRESS_HINT)
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Python 版本 / 依赖 / 密钥：check_environment
# ---------------------------------------------------------------------------


def _normalize_version(python_version: Any) -> tuple[int, int]:
    """把 `(3, 15)` / `sys.version_info` / `"3.15.0"` 统一成 `(major, minor)`。"""
    if python_version is None:
        return (sys.version_info[0], sys.version_info[1])
    if isinstance(python_version, str):
        parts = python_version.strip().split(".")
        try:
            return (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
        except ValueError:
            return (0, 0)
    if isinstance(python_version, tuple) or isinstance(python_version, list):
        seq: Sequence[Any] = python_version
        try:
            return (int(seq[0]), int(seq[1]) if len(seq) > 1 else 0)
        except (IndexError, TypeError, ValueError):
            return (0, 0)
    # `sys.version_info` 之类的对象：有 major/minor 属性就走属性。
    try:
        return (int(getattr(python_version, "major")), int(getattr(python_version, "minor")))
    except (AttributeError, TypeError, ValueError):
        return (0, 0)


def _default_websockets_probe() -> bool:
    """真的 import 一次 `websockets`（`find_spec` 会漏掉「装了但装坏」）。"""
    try:
        importlib.import_module("websockets")
        return True
    except Exception:
        return False


def check_environment(
    environ: Mapping[str, str] | None = None,
    python_version: Any = None,
    *,
    config: HostConfig | None = None,
    websockets_available: Callable[[], bool] | None = None,
) -> list[CheckItem]:
    """列出「还缺什么」——**只列缺的**，全就绪时返回空列表。

    @param environ  环境变量表（默认 `os.environ`，测试可注入）
    @param python_version  当前解释器版本；默认 `sys.version_info`
    @param config  已加载的配置；给了就按配置里声明的**环境变量名**去查密钥，
                   没给则退回 `examples/host.example.json` 的默认名
    @param websockets_available  注入的探测函数（测试用），默认真 import

    返回值里**只有环境变量名**，没有任何值。
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    version = _normalize_version(python_version)
    items: list[CheckItem] = []

    # 1) Python 版本
    if version < MIN_PYTHON:
        items.append(
            CheckItem(
                key="python",
                message=(
                    f"当前 Python 是 {version[0]}.{version[1]}，低于要求的 "
                    f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}；核心包 pyproject.toml 写的是 "
                    "requires-python >=3.11，低版本连语法都不兼容。"
                ),
                fix=(
                    "装 Python 3.11 或更高（推荐 3.12）：https://www.python.org/downloads/ ，"
                    "装完确认 `python --version` 至少是 3.11"
                ),
            )
        )

    # 2) websockets（设备服务端靠它，缺了装配阶段才炸，不如提前说）
    probe = websockets_available or _default_websockets_probe
    if not probe():
        items.append(
            CheckItem(
                key="websockets",
                message=(
                    "缺少依赖 websockets —— 手机通过 WSS 连上来全靠它，"
                    "缺了设备服务端根本起不来。"
                ),
                fix=_in_host_dir(
                    "python -m pip install \"websockets>=12,<16\" "
                    "（生产上 WSS 还要 cryptography>=42,<46）"
                ),
            )
        )

    # 3) 密钥：名字来自配置，值永远不读出来
    signing_env = config.signing_secret_env if config is not None else DEFAULT_SIGNING_SECRET_ENV
    if not str(env.get(signing_env, "")).strip():
        items.append(
            CheckItem(
                key=signing_env,
                message=(
                    f"没有设置环境变量 {signing_env} —— 它是设备帧签名密钥，"
                    "缺了宿主会直接拒绝启动（不会静默降级成空密钥）。"
                ),
                fix=(
                    "先生成一个随机密钥：python -c \"import secrets;print(secrets.token_hex(32))\"\n"
                    + _env_fix_text(signing_env)
                ),
            )
        )

    model_env = str(config.model_api_key_env or "") if config is not None else ""
    if model_env and not str(env.get(model_env, "")).strip():
        items.append(
            CheckItem(
                key=model_env,
                message=(
                    f"配置里把模型 API Key 绑定到环境变量 {model_env}，但它没有设置 —— "
                    "调用模型会 401，设备任务永远等不到决策。"
                ),
                fix=(
                    _env_fix_text(model_env)
                    + "\n如果这个模型端点不需要 Key（例如本机 Ollama 走反代），"
                    "就把配置里 model_api_key_env 置空，而不是留一个空变量名。"
                ),
            )
        )

    onebot_env = ""
    if config is not None and config.onebot is not None:
        onebot_env = str(config.onebot.access_token_env or "")
    if onebot_env and not str(env.get(onebot_env, "")).strip():
        items.append(
            CheckItem(
                key=onebot_env,
                message=(
                    f"配置里把 NapCat access token 绑定到环境变量 {onebot_env}，但它没有设置 —— "
                    "宿主会以「无 token」去调 OneBot，NapCat 配了 token 时每次调用都会被拒。"
                ),
                fix=(
                    _env_fix_text(onebot_env)
                    + "\n如果 NapCat 本来就没开 token，"
                    "就把配置里 onebot.access_token_env 置空。"
                ),
            )
        )

    return items


# ---------------------------------------------------------------------------
# 配置：生成（幂等）与校验（说人话）
# ---------------------------------------------------------------------------


def default_template() -> dict[str, Any]:
    """返回内置模板的**深拷贝**（调用方改它不会污染后续生成）。"""
    return copy.deepcopy(DEFAULT_TEMPLATE)


def ensure_config_file(
    path: str | Path,
    template: Mapping[str, Any] | None = None,
) -> BootstrapResult:
    """没有就生成一份配置；**已经有就绝不覆盖**（用户改过的内容不能被脚本吞掉）。

    @param path  目标配置文件路径（一般是 `host/host.json`）
    @param template  模板字典；默认用 [DEFAULT_TEMPLATE]

    幂等：第二次调用返回 `created=False`，文件内容与字节数都不变。
    """
    target = Path(path)
    if target.is_file():
        return BootstrapResult(path=target, created=False, fields=FIELDS_TO_EDIT)

    payload = copy.deepcopy(dict(template)) if template is not None else default_template()
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    target.write_text(text, encoding="utf-8")
    return BootstrapResult(path=target, created=True, fields=FIELDS_TO_EDIT)


# -- 英文异常 → 中文可照做提示 ------------------------------------------------

_RE_ENV_NAME = r"([A-Za-z_][A-Za-z0-9_]*)"

#: (正则, key, 中文说明, 怎么改)。顺序敏感：先匹配到的先赢。
_TRANSLATIONS: tuple[tuple[str, str, str, str], ...] = (
    (
        r"host config not found: (?P<path>.+)",
        "config_file",
        "找不到配置文件：{path}",
        "在 host 目录下直接跑 python run_host.py，它会自动生成一份 host.json",
    ),
    (
        r"host config is not valid JSON: (?P<detail>.+)",
        "config_json",
        "配置文件不是合法 JSON：{detail}",
        "用编辑器检查括号/逗号/引号是否配对；也可以删掉 host.json 让 run_host.py 重新生成一份",
    ),
    (
        r"unknown host config keys: (?P<keys>\[.*\])",
        "config_unknown_keys",
        "配置里有宿主不认识的顶层字段：{keys}（宿主拒绝未知键，宁可启动时炸，也不带着错配置跑）",
        "删掉这些字段；合法键名见 examples/host.example.json",
    ),
    (
        r"unknown onebot keys: (?P<keys>\[.*\])",
        "onebot_unknown_keys",
        "onebot 里有不认识的字段：{keys}（只允许 http_url 和 access_token_env）",
        "删掉多余字段；token 只写环境变量名，不要写明文",
    ),
    (
        r"unknown device_server keys: (?P<keys>\[.*\])",
        "device_server_unknown_keys",
        "device_server 里有不认识的字段：{keys}（只允许 host / port / certfile / keyfile）",
        "删掉多余字段",
    ),
    (
        r"host config must be an object",
        "config_shape",
        "配置文件最外层必须是 JSON 对象（{ ... }）",
        "对照 examples/host.example.json 修好结构",
    ),
    (
        r"model must be an object",
        "model",
        "model 必须是 JSON 对象",
        "补上 model 段（9 个固定键，见 examples/host.example.json）",
    ),
    (
        r"onebot must be an object",
        "onebot",
        "onebot 必须是 JSON 对象",
        "改成 {\"http_url\": \"http://127.0.0.1:3000\", \"access_token_env\": \"NAPCAT_TOKEN\"}，"
        "或者整段删掉（删掉就收不到 QQ 消息）",
    ),
    (
        r"device_server must be an object",
        "device_server",
        "device_server 必须是 JSON 对象",
        "改成 {\"host\": \"127.0.0.1\", \"port\": 8765}，或整段删掉用默认值",
    ),
    (
        r"owner_user_id must be a numeric QQ id",
        "owner_user_id",
        "owner_user_id 必须是纯数字的 QQ 号（写昵称不算——主人只按精确 user_id 认，M04 硬要求）",
        "把 owner_user_id 改成你自己的 QQ 号数字，例如 10001",
    ),
    (
        r"device_id must match [^;]+",
        "device_id",
        "device_id 只允许字母、数字和 . _ : -，长度 3~80",
        "改成与手机端 device_connection.device_id 完全相同的串，例如 device-b-01",
    ),
    (
        r"onebot\.http_url is required",
        "onebot_http_url",
        "onebot.http_url 没填——NapCat 的 HTTP 地址是必填的",
        "填 http://127.0.0.1:3000（NapCat 默认的 HTTP 端口）",
    ),
    (
        r"onebot\.http_url must start with http",
        "onebot_http_url",
        "onebot.http_url 必须以 http:// 或 https:// 开头",
        "改成 http://127.0.0.1:3000 这种形式",
    ),
    (
        r"device_server\.host is required",
        "device_server_host",
        "device_server.host 没填",
        "填 127.0.0.1（本机）或 0.0.0.0（局域网里让手机直连；注意风险）",
    ),
    (
        r"device_server\.port must be an integer",
        "device_server_port",
        "device_server.port 必须是整数",
        "填 8765；填 0 表示让操作系统随机挑一个空闲端口",
    ),
    (
        r"device_server\.port is out of range",
        "device_server_port",
        "device_server.port 超出 0~65535",
        "填 8765，或填 0 让系统分配",
    ),
    (
        r"allowed_packages must be a list",
        "allowed_packages",
        "allowed_packages 必须是包名数组",
        "写成 [\"com.android.settings\", \"com.android.chrome\"]",
    ),
    (
        r"blocked_packages must be a list",
        "blocked_packages",
        "blocked_packages 必须是包名数组",
        "没有要禁的就写成 []",
    ),
    (
        r"(?P<what>allowed_packages|blocked_packages) contains an invalid package name",
        "package_name",
        "{what} 里有不合法的包名（只允许字母、数字和点，3~255 位）",
        "把那一项改成像 com.android.settings 这样的完整包名",
    ),
    (
        r"(?P<what>[A-Za-z_.]+) must be an environment variable name",
        "env_name",
        "{what} 只能写**环境变量名**，不能写密钥明文，也不能写空值",
        "写 WHALE_SIGNING_SECRET 这样的变量名；密钥本身放进环境变量",
    ),
    (
        r"allowed_packages and blocked_packages must not overlap",
        "package_overlap",
        "同一个包既在 allowed_packages 又在 blocked_packages 里，配置自相矛盾",
        "把重复的包从其中一侧删掉",
    ),
    (
        r"provider_id must match [^;]+",
        "provider_id",
        "model.provider_id 只允许字母、数字和 . _ -，长度 1~64",
        "填 openai（或你的中转站标识）",
    ),
    (
        r"model_id must match [^;]+",
        "model_id",
        "model.model_id 只允许字母、数字和 . _ : -，长度 1~128",
        "填 gpt-4o-mini 这样的模型名",
    ),
    (
        r"fallback_model_id must match [^;]+",
        "fallback_model_id",
        "model.fallback_model_id 形状不对（不需要就留空字符串）",
        "留空 \"\"，或填一个同形状的模型名",
    ),
    (
        r"language must be a BCP-47 style tag",
        "language",
        "model.language 必须是 BCP-47 语言标签",
        "填 zh-CN",
    ),
    (
        r"max_context_tokens must be an integer",
        "max_context_tokens",
        "model.max_context_tokens 必须是整数",
        "填 8192",
    ),
    (
        r"max_context_tokens is out of the supported range",
        "max_context_tokens",
        "model.max_context_tokens 超出支持范围（512 ~ 2000000）",
        "填 8192",
    ),
    (
        r"allow_proactive_greeting must be a boolean",
        "allow_proactive_greeting",
        "model.allow_proactive_greeting 必须是 true / false",
        "填 false（默认不主动打招呼）",
    ),
    (
        r"base_url must use https",
        "base_url",
        "model.base_url 必须用 https（宿主拒绝明文回传密钥）",
        "改成 https://api.openai.com；本地模型请用 https 反向代理，或把 base_url 留空",
    ),
    (
        r"base_url must be a string",
        "base_url",
        "model.base_url 必须是字符串",
        "填 https://api.openai.com 或留空 \"\"",
    ),
    (
        r"base_url must contain a host",
        "base_url",
        "model.base_url 里没有主机名",
        "改成 https://api.openai.com 这种完整地址",
    ),
    (
        r"base_url must not embed credentials",
        "base_url",
        "model.base_url 里嵌了用户名/密码",
        "把凭据从 URL 里去掉，密钥交给环境变量",
    ),
    (
        r"base_url must not carry secret query",
        "base_url",
        "model.base_url 的查询串里带了疑似密钥的参数",
        "把 key=... 这类参数删掉，密钥交给环境变量",
    ),
    (
        r"base_url must not contain a fragment",
        "base_url",
        "model.base_url 里带了 # 片段",
        "删掉 # 及其后面的内容",
    ),
    (
        r"signing secret is not set; export " + _RE_ENV_NAME,
        "signing_secret",
        "环境变量 {0} 没有设置（设备帧签名密钥）",
        "按 run_host.py 的提示设置它：$env:{0} = \"<随机长串>\"",
    ),
    (
        r"secret material must stay in the Secret Store",
        "secret_material",
        "配置里出现了「疑似密钥」的键——密钥只能放环境变量，不能写进配置文件",
        "把那个键删掉，改用 *_env 写环境变量名",
    ),
)

_COMPILED_TRANSLATIONS = tuple(
    (re.compile(pattern), key, zh, fix) for pattern, key, zh, fix in _TRANSLATIONS
)


def explain_config_error(message: str) -> CheckItem:
    """把 `HostConfig` 抛出的英文异常翻译成中文可照做的 [CheckItem]。

    匹配不到时给一条兜底：保留原始英文（它不是密钥，没有泄漏风险），
    否则「未知的有趣错误」会变成一句没用的「配置错误」。
    """
    text = str(message)
    for pattern, key, zh, fix in _COMPILED_TRANSLATIONS:
        match = pattern.search(text)
        if match is None:
            continue
        groups = match.groupdict()
        rendered = zh
        for name, value in groups.items():
            rendered = rendered.replace("{" + name + "}", str(value))
        # `{0}` 这种位置占位（用于重复同一个环境变量名）
        if groups:
            first = next(iter(groups.values()))
            rendered = rendered.replace("{0}", str(first))
            fix = fix.replace("{0}", str(first))
        return CheckItem(key=key, message=rendered, fix=fix)
    return CheckItem(
        key="config",
        message=f"配置不合法，宿主原话是：{text}",
        fix="对照 examples/host.example.json 逐字段检查；改完再跑一次 python run_host.py",
    )


def load_config(path: str | Path) -> tuple[HostConfig | None, list[CheckItem]]:
    """加载配置；失败时返回 `(None, [人话问题])`，**绝不抛 traceback**。

    复用 `HostConfig.load` 的全部校验（未知键、密钥形状、base_url……），
    本函数只负责把异常换成中文。
    """
    target = Path(path)
    if not target.is_file():
        return None, [
            CheckItem(
                key="config_file",
                message=f"找不到配置文件：{target}",
                fix="在 host 目录下直接跑 python run_host.py，它会自动生成一份 host.json",
            )
        ]
    try:
        return HostConfig.load(target), []
    except Exception as exc:  # ConfigError / OSError / UnicodeDecodeError 都要接
        return None, [explain_config_error(str(exc))]


def validate_config_file(path: str | Path) -> list[CheckItem]:
    """只要问题列表（[load_config] 的薄封装），给人读的版本。"""
    _, items = load_config(path)
    return items


# ---------------------------------------------------------------------------
# 报告渲染
# ---------------------------------------------------------------------------


def format_report(
    items: Sequence[CheckItem],
    *,
    heading: str = "",
    info_only: bool = False,
) -> str:
    """把检查项渲染成给人看的文本；没有缺失时返回空串。

    只输出 `key` / `message` / `fix`——三样都是本模块自己写的常量或配置字段名，
    **不含任何环境变量的值**。

    @param info_only `True` 表示这些项**不是阻塞问题**，只是一段需要告知用户的说明
        （例如「已自动生成自签名证书」）。此时**不能**说「还缺 N 项，主机没有启动」——
        那会让用户以为启动失败了。
    """
    if not items:
        return ""
    lines: list[str] = []
    if heading:
        lines.append(heading)
    if info_only:
        lines.append(f"有 {len(items)} 项需要你知道（不阻塞启动）：")
    else:
        lines.append(f"还缺 {len(items)} 项，主机没有启动：")
    for index, item in enumerate(items, start=1):
        lines.append(f"  {index}. [{item.key}] {item.message}")
        fix_lines = item.fix.splitlines() or [""]
        for offset, fix_line in enumerate(fix_lines):
            prefix = "     注意：" if info_only and offset == 0 else "     怎么补：" if offset == 0 else "             "
            lines.append(prefix + fix_line)
    if not info_only:
        lines.append("改完这些，再跑一次 python run_host.py 就会真正启动。")
    return "\n".join(lines)


def format_success(config: HostConfig, path: str | Path) -> str:
    """全部就绪时的「要启动了」文案（同样只出现变量名）。"""
    onebot = config.onebot
    return "\n".join(
        (
            f"配置 OK：{path}",
            f"  主人 QQ：{config.owner_user_id}    设备 ID：{config.device_id}",
            f"  签名密钥来自环境变量：{config.signing_secret_env}（值已就绪，不打印）",
            f"  模型密钥来自环境变量：{config.model_api_key_env or '（未配置，不发 Authorization）'}",
            "  OneBot：" + (
                "未配置（只起设备服务端，收不到 QQ 消息）" if onebot is None
                else f"{onebot.http_url}（token 来自 {onebot.access_token_env or '（无）'}）"
            ),
            f"  设备服务端：{config.device_server.host}:{config.device_server.port}"
            + ("（WSS）" if config.device_server.uses_tls else "（明文 ws，仅限本机试验）"),
        )
    )
