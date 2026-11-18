from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping
from urllib.parse import urlsplit


class Principal(str, Enum):
    OWNER = "OWNER"
    GUEST = "GUEST"
    UNKNOWN = "UNKNOWN"


class MessageType(str, Enum):
    PRIVATE = "private"
    GROUP = "group"


class DecisionMode(str, Enum):
    REPLY = "REPLY"
    SILENT = "SILENT"
    DEVICE_TASK = "DEVICE_TASK"


@dataclass(frozen=True)
class ImageRef:
    """入站图片引用，只由 onebot 入站校验构造。

    kind="url"：形状已校验的公网 https 地址。主机不下载，由模型服务商自行访问。
    kind="data"：已按真实文件头验证的 PNG/JPEG/GIF/WebP data URL。
    url 不进 repr，避免 base64 或带签名参数的链接进入日志。
    """

    kind: str
    url: str = field(repr=False)
    mime: str = ""
    size: int = 0


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    user_id: str
    text: str
    message_type: MessageType
    channel: str = "onebot"
    group_id: str | None = None
    sender_name: str = ""
    is_bot: bool = False
    #: 已通过校验、随本条消息发给视觉模型的图片；默认空，旧调用方不受影响。
    images: tuple[ImageRef, ...] = ()
    #: 消息里有图片段但无法安全传递（来源不合格或超出张数）的张数。
    images_omitted: int = 0


@dataclass(frozen=True)
class DeviceEnvelope:
    protocol_version: int
    request_id: str
    device_id: str
    command: str
    deadline_ms: int
    idempotency_key: str
    payload: dict[str, Any]
    counter: int = 0
    issued_at_ms: int = 0
    signature: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "device_id": self.device_id,
            "command": self.command,
            "deadline_ms": self.deadline_ms,
            "idempotency_key": self.idempotency_key,
            "counter": self.counter,
            "issued_at_ms": self.issued_at_ms,
            "payload": self.payload,
            "signature": self.signature,
        }


@dataclass(frozen=True)
class DeviceResponse:
    request_id: str
    device_id: str
    status: str
    error_code: str = ""
    message: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "device_id": self.device_id,
            "status": self.status,
            "error_code": self.error_code,
            "message": self.message,
            "artifacts": self.artifacts,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class DevicePlan:
    commands: list[dict[str, Any]]


@dataclass(frozen=True)
class ModelDecision:
    mode: DecisionMode
    text: str = ""
    media: list[dict[str, Any]] = field(default_factory=list)
    device_plan: DevicePlan | None = None


# ---------------------------------------------------------------------------
# M02 固定配置面：配置键就是这九个，没有第十个。
# ---------------------------------------------------------------------------

FIXED_CONFIG_KEYS: tuple[str, ...] = (
    "provider_id",
    "base_url",
    "model_id",
    "fallback_model_id",
    "persona_id",
    "language",
    "max_context_tokens",
    "tts_provider_id",
    "allow_proactive_greeting",
)

# 任何命中这些词形的键都只能是 Secret Store 的资产，一律不得进入 host 配置载荷、
# 任务存储、日志或设备帧。这里只比对「键名」，永远不读取、不回显键值。
# 判断分三步：精确名 -> 后缀 -> 包含形态。不能简单做子串匹配，否则
# `max_context_tokens` 这类合法的数量配置键会被 "token" 误伤。
SECRET_KEY_EXACT: frozenset[str] = frozenset({
    "token",
    "apikey",
    "secret",
    "password",
    "credentials",
    "credential",
    "authorization",
    "auth",
    "bearer",
})
SECRET_KEY_SUFFIXES: tuple[str, ...] = (
    "_key",
    "_secret",
    "_token",
    "_password",
    "_credential",
    "_credentials",
    "_auth",
)
SECRET_KEY_INFIXES: tuple[str, ...] = (
    "api_key",
    "api-key",
    "access_token",
    "refresh_token",
    "bearer_token",
    "auth_token",
    "secret_key",
    "private_key",
    "signing_key",
    "authorization",
)
# 文档固定配置里可能含 SECRET_KEY_INFIXES 字样的键，属于已审核的数量/布尔配置。
NON_SECRET_FIXED_KEYS: frozenset[str] = frozenset({"max_context_tokens"})

# 超时策略：模型侧固定 30 秒、备用模型最多切一次；设备侧的 deadline 上限由主机定，
# 绝不由模型输出决定（见 M02 开发文档「超时和重试」）。
MODEL_TIMEOUT_MS = 30_000
MODEL_FALLBACK_ATTEMPTS = 1
DEVICE_DEADLINE_MIN_MS = 100
DEVICE_DEADLINE_MAX_MS = 30_000
MAX_PLAN_COMMANDS = 64

_PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_LANGUAGE_RE = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?")


class ConfigError(ValueError):
    """配置面违规。消息里只能出现键名，不得出现任何键值。"""


def is_secret_key(key: str) -> bool:
    """判断键名是否只可能属于 Secret Store。不读值、不回显值。"""
    name = str(key).strip().lower()
    if not name:
        return False
    if name in NON_SECRET_FIXED_KEYS:
        return False
    if name in SECRET_KEY_EXACT:
        return True
    if name.endswith(SECRET_KEY_SUFFIXES):
        return True
    if any(marker in name for marker in SECRET_KEY_INFIXES):
        return True
    # 去掉分隔符后再查一次，兜住 apiKey / accessToken 这类驼峰写法。
    flat = name.replace("_", "").replace("-", "").replace(".", "")
    return flat in {"apikey", "accesstoken", "refreshtoken", "bearertoken", "authortoken", "auth", "authorization"}


def secret_keys_in(payload: dict[str, Any]) -> list[str]:
    """返回命中的敏感键名（只返回名字，绝不返回值）。"""
    return sorted(key for key in payload if is_secret_key(str(key)))


@dataclass(frozen=True)
class ModelConfig:
    """M02 的九个固定配置键，逐键显式绑定。

    密钥不在这个对象里：provider/base_url/model 只来自这份配置，永远不来自
    消息正文；密钥由 AstrBot Secret Store 持有，本类连存放的字段都不提供。
    """

    provider_id: str
    model_id: str
    base_url: str = ""
    fallback_model_id: str = ""
    persona_id: str = ""
    language: str = "zh-CN"
    max_context_tokens: int = 8_192
    tts_provider_id: str = ""
    allow_proactive_greeting: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.provider_id, str) or _PROVIDER_ID_RE.fullmatch(self.provider_id) is None:
            raise ConfigError("provider_id must match [A-Za-z0-9._-]{1,64}")
        if not isinstance(self.model_id, str) or _MODEL_ID_RE.fullmatch(self.model_id) is None:
            raise ConfigError("model_id must match [A-Za-z0-9._:-]{1,128}")
        if self.fallback_model_id and _MODEL_ID_RE.fullmatch(str(self.fallback_model_id)) is None:
            raise ConfigError("fallback_model_id must match [A-Za-z0-9._:-]{1,128}")
        if self.persona_id and not isinstance(self.persona_id, str):
            raise ConfigError("persona_id must be a string")
        if self.tts_provider_id and not isinstance(self.tts_provider_id, str):
            raise ConfigError("tts_provider_id must be a string")
        if not isinstance(self.language, str) or _LANGUAGE_RE.fullmatch(self.language) is None:
            raise ConfigError("language must be a BCP-47 style tag")
        if isinstance(self.max_context_tokens, bool) or not isinstance(self.max_context_tokens, int):
            raise ConfigError("max_context_tokens must be an integer")
        if not 512 <= self.max_context_tokens <= 2_000_000:
            raise ConfigError("max_context_tokens is out of the supported range")
        if not isinstance(self.allow_proactive_greeting, bool):
            raise ConfigError("allow_proactive_greeting must be a boolean")
        self._validate_base_url()

    def _validate_base_url(self) -> None:
        url = self.base_url
        if not url:
            return
        if not isinstance(url, str):
            raise ConfigError("base_url must be a string")
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise ConfigError("base_url must use https")
        if not parts.hostname:
            raise ConfigError("base_url must contain a host")
        if parts.username or parts.password:
            raise ConfigError("base_url must not embed credentials")
        if parts.query:
            query_keys = {pair.split("=", 1)[0].lower() for pair in parts.query.split("&") if pair}
            if any(is_secret_key(key) for key in query_keys):
                raise ConfigError("base_url must not carry secret query parameters")
        if parts.fragment:
            raise ConfigError("base_url must not contain a fragment")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "ModelConfig":
        """从配置载荷构造。唯一入口：未知键与密钥键一律拒绝。"""
        if not isinstance(raw, Mapping):
            raise ConfigError("configuration must be a mapping")
        unknown = sorted(str(key) for key in raw if str(key) not in FIXED_CONFIG_KEYS)
        if unknown:
            raise ConfigError("unknown configuration key present")
        leaked = secret_keys_in(dict(raw))
        if leaked:
            raise ConfigError("secret material must stay in the Secret Store")
        return cls(
            provider_id=str(raw["provider_id"]),
            model_id=str(raw["model_id"]),
            base_url=str(raw.get("base_url", "") or ""),
            fallback_model_id=str(raw.get("fallback_model_id", "") or ""),
            persona_id=str(raw.get("persona_id", "") or ""),
            language=str(raw.get("language", "zh-CN") or "zh-CN"),
            max_context_tokens=raw.get("max_context_tokens", 8_192),
            tts_provider_id=str(raw.get("tts_provider_id", "") or ""),
            allow_proactive_greeting=raw.get("allow_proactive_greeting", False),
        )

    def as_dict(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in FIXED_CONFIG_KEYS}

    def loggable(self) -> dict[str, Any]:
        """可进日志的摘要：不含 base_url（自定义中转站与私有网关属于运行细节）。"""
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "fallback_model_id": self.fallback_model_id,
            "persona_id": self.persona_id,
            "language": self.language,
            "tts_provider_id": self.tts_provider_id,
            "allow_proactive_greeting": self.allow_proactive_greeting,
        }

    def device_deadline_ms(self, requested_ms: int | None = None) -> int:
        """设备命令超时上限由主机夹紧，模型无法通过输出放宽。"""
        value = DEVICE_DEADLINE_MAX_MS if requested_ms is None else requested_ms
        if isinstance(value, bool) or not isinstance(value, int):
            return DEVICE_DEADLINE_MAX_MS
        return max(DEVICE_DEADLINE_MIN_MS, min(value, DEVICE_DEADLINE_MAX_MS))

