"""独立宿主的配置面。

定位：`host/astrbot_whale_device/` 是**框架无关**的核心包；AstrBot 插件与「独立宿主」
是它的两个外层宿主。本模块只定义**独立宿主**读什么配置，不碰核心包的任何逻辑。

## 为什么需要它

本机没有 AstrBot（也不打算把核心逻辑绑死在某个框架上），所以要有一个能直接跑起来的
宿主入口。它需要的信息只有四类：

1. **身份**：谁是主人（`owner_user_id`）、本机是哪个设备（`device_id`）、允许/禁止哪些包；
2. **模型**：M02 的 9 个固定配置键（直接复用 `ModelConfig`，不重新发明）+ API Key 的来源；
3. **OneBot**：NapCat 的 HTTP 地址与 access token；
4. **设备服务端**：手机连上来的 WSS 监听地址与 TLS 证书。

## 密钥策略（结构性隔离）

配置里**只写环境变量名**（`*_env`），不写明文密钥。理由与 M02 一致：

- 密钥永远不进仓库、不进日志、不进设备帧、不进返回给 QQ 的消息；
- 配置文件可以安全地提交/分享，因为它本身不含任何秘密；
- 真值由进程环境提供，宿主启动时读取一次，之后只存在于内存闭包里。

`ModelConfig` 本身也**没有**存放密钥的字段——这是双保险：即使有人往配置里塞
`api_key`，`ModelConfig.from_mapping` 也会因为「未知键」拒绝。

## 未知键一律拒绝

与 M02 的 `ModelConfig` 同样 fail-closed：配置里出现任何未声明的键，
直接报错而不是忽略。宁可启动时炸，也不要带着「以为配了其实没配」的状态跑。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .models import ConfigError, ModelConfig

# ---------------------------------------------------------------------------
# 固定校验规则
# ---------------------------------------------------------------------------

#: 主人 QQ 号固定为纯数字（M04：只能精确匹配 `user_id`，昵称一律不算）。
_OWNER_ID_RE = re.compile(r"[0-9]{4,20}")

#: 设备 ID：与 `contracts/device-envelope.schema.json` 的 `device_id` 同规则。
_DEVICE_ID_RE = re.compile(r"[A-Za-z0-9._:-]{3,80}")

#: 包名规则（与 M04 / M11 的白名单同口径：完全匹配，不允许前缀模糊）。
_PACKAGE_RE = re.compile(r"[A-Za-z0-9._]{3,255}")

#: 环境变量名规则。
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")

#: 顶层允许出现的键，多一个都不行。
_ALLOWED_TOP_LEVEL_KEYS = frozenset({
    "owner_user_id",
    "device_id",
    "allowed_packages",
    "blocked_packages",
    "signing_secret_env",
    "model",
    "model_api_key_env",
    "onebot",
    "device_server",
})

_ALLOWED_ONEBOT_KEYS = frozenset({"http_url", "access_token_env"})
_ALLOWED_DEVICE_SERVER_KEYS = frozenset({"host", "port", "certfile", "keyfile", "public_url"})


def _require_env_name(value: Any, what: str) -> str:
    if not isinstance(value, str) or _ENV_NAME_RE.fullmatch(value) is None:
        raise ConfigError(f"{what} must be an environment variable name")
    return value


def _require_package_set(value: Any, what: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"{what} must be a list of package names")
    result: set[str] = set()
    for item in value:
        if not isinstance(item, str) or _PACKAGE_RE.fullmatch(item) is None:
            raise ConfigError(f"{what} contains an invalid package name")
        result.add(item)
    return frozenset(result)


# ---------------------------------------------------------------------------
# 子结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OneBotEndpoint:
    """NapCat / OneBot v11 的 HTTP 端点。

    只存 **access token 的环境变量名**，不存 token 明文。
    """

    http_url: str
    access_token_env: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.http_url, str) or not self.http_url:
            raise ConfigError("onebot.http_url is required")
        if not self.http_url.startswith(("http://", "https://")):
            raise ConfigError("onebot.http_url must start with http:// or https://")
        if self.access_token_env:
            _require_env_name(self.access_token_env, "onebot.access_token_env")

    def access_token(self, environ: Mapping[str, str] | None = None) -> str:
        """从环境读 token；未配置或未设置时返回空串。

        **返回值绝不进日志**——调用方负责只在 `Authorization` 头里使用它。
        """
        if not self.access_token_env:
            return ""
        env = os.environ if environ is None else environ
        return str(env.get(self.access_token_env, ""))


@dataclass(frozen=True)
class DeviceServerEndpoint:
    """手机连上来的 WSS 监听点。

    生产必须给 `certfile` / `keyfile`：M05 明确禁止明文 ws。
    未提供证书时宿主允许启动（便于本机回环测试），但会在启动日志里**显式警告**。
    """

    host: str = "127.0.0.1"
    port: int = 8765
    certfile: str = ""
    keyfile: str = ""
    public_url: str = ""

    def __post_init__(self) -> None:
        if self.public_url:
            from urllib.parse import urlsplit
            try:
                url = urlsplit(self.public_url)
                valid = (url.scheme == "wss" and url.hostname and url.port is not None
                         and 1 <= url.port <= 65534 and url.path.endswith("/device") and ".." not in url.path and "%" not in url.path
                         and not url.username and not url.password and not url.query and not url.fragment)
            except ValueError:
                valid = False
            if not valid:
                raise ConfigError("公网地址须为 wss://域名:端口/device，端口为1至65534；443端口通过同路径配对，其他端口使用相邻的下一端口")
        if not isinstance(self.host, str) or not self.host:
            raise ConfigError("device_server.host is required")
        if isinstance(self.port, bool) or not isinstance(self.port, int):
            raise ConfigError("device_server.port must be an integer")
        # `0` 是合法值：交给操作系统分配空闲端口。本机试验与自动化测试都靠它，
        # 用固定端口会让并行测试撞端口。其它情况必须是 1..65535。
        if not 0 <= self.port <= 65535:
            raise ConfigError("device_server.port is out of range")

    @property
    def uses_tls(self) -> bool:
        return bool(self.certfile and self.keyfile)

    def missing_tls_files(self) -> list[str]:
        """列出声明了但实际不存在的证书文件（启动前应检查）。"""
        missing: list[str] = []
        for path in (self.certfile, self.keyfile):
            if path and not Path(path).is_file():
                missing.append(path)
        return missing


@dataclass(frozen=True)
class HostConfig:
    """独立宿主的完整配置。

    密钥全部以环境变量名的形式持有，见模块 docstring 的「密钥策略」。
    """

    owner_user_id: str
    device_id: str
    model: ModelConfig
    signing_secret_env: str
    allowed_packages: frozenset[str] = field(default_factory=frozenset)
    blocked_packages: frozenset[str] = field(default_factory=frozenset)
    model_api_key_env: str = ""
    onebot: OneBotEndpoint | None = None
    device_server: DeviceServerEndpoint = field(default_factory=DeviceServerEndpoint)

    def __post_init__(self) -> None:
        if not isinstance(self.owner_user_id, str) or _OWNER_ID_RE.fullmatch(self.owner_user_id) is None:
            # M04：主人只能按精确 user_id 识别；不接受昵称、也不接受非数字 id。
            raise ConfigError("owner_user_id must be a numeric QQ id")
        if not isinstance(self.device_id, str) or _DEVICE_ID_RE.fullmatch(self.device_id) is None:
            raise ConfigError("device_id must match [A-Za-z0-9._:-]{3,80}")
        _require_env_name(self.signing_secret_env, "signing_secret_env")
        if self.model_api_key_env:
            _require_env_name(self.model_api_key_env, "model_api_key_env")
        if not isinstance(self.model, ModelConfig):
            raise ConfigError("model must be a ModelConfig")
        if self.allowed_packages & self.blocked_packages:
            # 同一个包既允许又禁止是配置矛盾；宁可启动失败，不要带着歧义跑。
            raise ConfigError("allowed_packages and blocked_packages must not overlap")

    # -- 密钥读取 ---------------------------------------------------------

    def signing_secret(self, environ: Mapping[str, str] | None = None) -> bytes:
        """读设备帧签名密钥；未设置时抛错（不静默用空密钥）。

        空密钥会让「签名」变成装饰品——M05 要求签名覆盖 canonical JSON，
        密钥缺失必须 fail-closed。
        """
        env = os.environ if environ is None else environ
        raw = env.get(self.signing_secret_env, "")
        if not raw:
            raise ConfigError(
                f"signing secret is not set; export {self.signing_secret_env} before starting the host",
            )
        return raw.encode("utf-8")

    def model_api_key(self, environ: Mapping[str, str] | None = None) -> str:
        """读模型 API Key；未配置或未设置时返回空串（部分本地模型不需要 key）。"""
        if not self.model_api_key_env:
            return ""
        env = os.environ if environ is None else environ
        return str(env.get(self.model_api_key_env, ""))

    # -- 加载 -------------------------------------------------------------

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> HostConfig:
        """从 dict 装配；未知键一律拒绝。"""
        if not isinstance(raw, Mapping):
            raise ConfigError("host config must be an object")

        unknown = sorted(str(key) for key in raw if str(key) not in _ALLOWED_TOP_LEVEL_KEYS)
        if unknown:
            raise ConfigError(f"unknown host config keys: {unknown}")

        model_raw = raw.get("model")
        if not isinstance(model_raw, Mapping):
            raise ConfigError("model must be an object")
        # 复用 M02 的校验：未知键、密钥形状的键、base_url 形状都在那里拦。
        model = ModelConfig.from_mapping(model_raw)

        onebot_raw = raw.get("onebot")
        onebot: OneBotEndpoint | None = None
        if onebot_raw is not None:
            if not isinstance(onebot_raw, Mapping):
                raise ConfigError("onebot must be an object")
            unknown_onebot = sorted(str(k) for k in onebot_raw if str(k) not in _ALLOWED_ONEBOT_KEYS)
            if unknown_onebot:
                raise ConfigError(f"unknown onebot keys: {unknown_onebot}")
            onebot = OneBotEndpoint(
                http_url=str(onebot_raw.get("http_url", "")),
                access_token_env=str(onebot_raw.get("access_token_env", "")),
            )

        server_raw = raw.get("device_server")
        if server_raw is not None and not isinstance(server_raw, Mapping):
            raise ConfigError("device_server must be an object")
        server_raw = server_raw or {}
        unknown_server = sorted(str(k) for k in server_raw if str(k) not in _ALLOWED_DEVICE_SERVER_KEYS)
        if unknown_server:
            raise ConfigError(f"unknown device_server keys: {unknown_server}")
        device_server = DeviceServerEndpoint(
            host=str(server_raw.get("host", "127.0.0.1")),
            port=server_raw.get("port", 8765),
            certfile=str(server_raw.get("certfile", "")),
            keyfile=str(server_raw.get("keyfile", "")),
            public_url=str(server_raw.get("public_url", "")).strip(),
        )

        return cls(
            owner_user_id=str(raw.get("owner_user_id", "")),
            device_id=str(raw.get("device_id", "")),
            model=model,
            signing_secret_env=str(raw.get("signing_secret_env", "")),
            allowed_packages=_require_package_set(raw.get("allowed_packages"), "allowed_packages"),
            blocked_packages=_require_package_set(raw.get("blocked_packages"), "blocked_packages"),
            model_api_key_env=str(raw.get("model_api_key_env", "")),
            onebot=onebot,
            device_server=device_server,
        )

    @classmethod
    def load(cls, path: str | Path, *, environ: Mapping[str, str] | None = None) -> HostConfig:
        """从 JSON 文件加载，兼容 Windows 工具写入的 UTF-8 BOM，且不改写文件。

        `environ` 只用于允许调用方注入，**不在这里读取任何密钥**——
        密钥读取发生在 [signing_secret] / [model_api_key]，且返回值不进日志。
        """
        del environ
        file = Path(path)
        if not file.is_file():
            raise ConfigError(f"host config not found: {file}")
        try:
            data = json.loads(file.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"host config is not valid JSON: {exc}") from exc
        return cls.from_mapping(data)

    def loggable(self) -> dict[str, Any]:
        """可安全打印的配置摘要。

        刻意**只列出环境变量名**，不含任何密钥值，也不含 OneBot token。
        模型摘要复用 `ModelConfig.loggable()`（它自己也不含 base_url）。
        """
        return {
            "owner_user_id": self.owner_user_id,
            "device_id": self.device_id,
            "allowed_packages": sorted(self.allowed_packages),
            "blocked_packages": sorted(self.blocked_packages),
            "signing_secret_env": self.signing_secret_env,
            "model_api_key_env": self.model_api_key_env,
            "model": self.model.loggable() if hasattr(self.model, "loggable") else {},
            "onebot": None if self.onebot is None else {
                "http_url": self.onebot.http_url,
                "access_token_env": self.onebot.access_token_env,
            },
            "device_server": {
                "host": self.device_server.host,
                "port": self.device_server.port,
                "uses_tls": self.device_server.uses_tls,
            },
        }
