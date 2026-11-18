from __future__ import annotations

import base64
import binascii
import inspect
import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from .models import ImageRef, IncomingMessage, MessageType

#: 出站只允许这五种标准 OneBot v11 消息段（M03 开发文档「出站消息段」）。
OUTBOUND_SEGMENT_TYPES = frozenset({"text", "face", "image", "record", "video"})
#: 需要文件来源校验的消息段类型。
FILE_SEGMENT_TYPES = frozenset({"image", "record", "video"})
#: 入站只读取这些字段；其余字段（at / role / raw_message / token 等）一律忽略。
INBOUND_READ_FIELDS = (
    "post_type",
    "message_type",
    "message_id",
    "user_id",
    "group_id",
    "message",
    "sender",
    "self_id",
)
ALLOWED_MESSAGE_TYPES = frozenset({MessageType.PRIVATE.value, MessageType.GROUP.value})
#: 出站文件不接受 URL / base64 形式：不允许把媒体降级成文本 URL。
URL_PREFIXES = ("http://", "https://", "base64://", "file://", "ftp://", "data:")
#: QQ user_id / message_id 都是十进制数字；昵称一律不能替代 user_id。
_ID_SHAPE = re.compile(r"[0-9]{1,20}")
#: 入站图片：每条消息最多传给模型的张数，以及单张解码后的字节上限。
MAX_INBOUND_IMAGES = 4
MAX_INBOUND_IMAGE_BYTES = 4 * 1024 * 1024
_MAX_BASE64_CHARS = ((MAX_INBOUND_IMAGE_BYTES + 2) // 3) * 4
_MAX_IMAGE_URL_LENGTH = 2048
_URL_FORBIDDEN_CHARS = frozenset('"<>\\^`{|}')
_BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".home",
    ".corp",
    ".arpa",
)
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_HOST_TLD = re.compile(r"[a-z]{2,63}|xn--[a-z0-9-]{1,59}")
_DATA_URL_PREFIX = re.compile(r"data:image/(png|jpeg|jpg|gif|webp);base64,", re.IGNORECASE)


@dataclass(frozen=True)
class OneBotSegment:
    type: str
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": self.data}


class OneBotEventError(ValueError):
    """入站事件被拒绝；`reason` 是固定的机读码，供插件静默丢弃并留痕。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}:{detail}" if detail else reason)


class OneBotMediaError(ValueError):
    """出站消息段或媒体来源不合法；绝不降级为文本 URL。"""


def _scalar_id(value: Any) -> str | None:
    """只接受 int / 非空字符串，并归一化成十进制字符串。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value >= 0 else None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _sniff_image_mime(blob: bytes) -> str | None:
    """按真实文件头判断图片类型；声明的 MIME 不作数。"""
    if blob.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if blob.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if blob.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(blob) >= 12 and blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    return None


def _image_from_base64(payload: str) -> ImageRef | None:
    """严格解码 base64（不容忍空白和非法字符），校验大小与文件头后转成 data URL。"""
    if not payload or len(payload) > _MAX_BASE64_CHARS:
        return None
    try:
        blob = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not blob or len(blob) > MAX_INBOUND_IMAGE_BYTES:
        return None
    mime = _sniff_image_mime(blob)
    if mime is None:
        return None
    encoded = base64.b64encode(blob).decode("ascii")
    return ImageRef(kind="data", url=f"data:{mime};base64,{encoded}", mime=mime, size=len(blob))


def _image_from_data_url(value: str) -> ImageRef | None:
    """data URL 的声明类型必须与真实文件头一致。"""
    match = _DATA_URL_PREFIX.match(value)
    if match is None:
        return None
    declared = match.group(1).lower()
    declared_mime = "image/jpeg" if declared == "jpg" else f"image/{declared}"
    ref = _image_from_base64(value[match.end():])
    if ref is None or ref.mime != declared_mime:
        return None
    return ref


def _is_public_hostname(host: str) -> bool:
    if not host or len(host) > 253 or host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # IP 字面量一律拒绝（含公网 IP），排除 127.0.0.1 / 10.x / [::1] 等形状。
        return False
    labels = host.split(".")
    if len(labels) < 2:
        return False
    if not all(_HOST_LABEL.fullmatch(label) for label in labels[:-1]):
        return False
    # 顶级域必须是字母或 punycode，挡住 127.1、0x7f.1 这类会被解析成 IP 的写法。
    return _HOST_TLD.fullmatch(labels[-1]) is not None


def _image_from_https_url(value: str) -> ImageRef | None:
    """只做形状校验，不发请求：主机不访问该地址，由模型服务商自行拉取。"""
    if len(value) > _MAX_IMAGE_URL_LENGTH or not value.isascii():
        return None
    if any(ord(ch) < 0x21 or ord(ch) == 0x7F or ch in _URL_FORBIDDEN_CHARS for ch in value):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or "@" in parts.netloc:
        return None
    if port not in (None, 443):
        return None
    host = (parts.hostname or "").rstrip(".")
    if not _is_public_hostname(host):
        return None
    return ImageRef(kind="url", url=value)


def _image_from_segment(data: dict[str, Any]) -> ImageRef | None:
    """只认 base64:// 、data:image/...;base64, 和公网 https 链接。

    本地路径、file://、http://、内网地址一律不取，主机也绝不读文件或发起抓取。
    """
    for key in ("file", "url"):
        value = data.get(key)
        if not isinstance(value, str):
            continue
        value = value.strip()
        lowered = value[:9].lower()
        if value.startswith("base64://"):
            ref = _image_from_base64(value[len("base64://"):])
        elif lowered.startswith("data:"):
            ref = _image_from_data_url(value)
        elif lowered.startswith("https://"):
            ref = _image_from_https_url(value)
        else:
            continue
        if ref is not None:
            return ref
    return None


def _text_from_body(raw: Any) -> str:
    """兼容旧入口：只返回文本（含媒体占位符）。"""
    return _parse_body(raw)[0]


def _parse_body(raw: Any) -> tuple[str, tuple[ImageRef, ...], int]:
    """message 只允许字符串或 OneBot segment 数组。

    返回 (文本, 可安全传给模型的图片, 无法安全传递的图片张数)。
    """
    if isinstance(raw, str):
        return raw, (), 0
    if not isinstance(raw, list):
        raise OneBotEventError("invalid_message_body", type(raw).__name__)
    parts: list[str] = []
    images: list[ImageRef] = []
    omitted = 0
    for segment in raw:
        if not isinstance(segment, dict):
            raise OneBotEventError("invalid_message_segment", "segment is not an object")
        kind = segment.get("type")
        if not isinstance(kind, str) or not kind:
            raise OneBotEventError("invalid_message_segment", "segment type is not a string")
        data = segment.get("data", {})
        if not isinstance(data, dict):
            raise OneBotEventError("invalid_segment_data", kind)
        if kind == "text":
            value = data.get("text", "")
            if not isinstance(value, str):
                raise OneBotEventError("invalid_segment_data", "text segment text must be a string")
            parts.append(value)
        elif kind == "image":
            # 占位符照旧保留（兼容旧文本）；能安全传递的图片另外挂到 images，不参与权限判定。
            parts.append("[image]")
            ref = _image_from_segment(data)
            if ref is not None and len(images) < MAX_INBOUND_IMAGES:
                images.append(ref)
            else:
                omitted += 1
        elif kind in FILE_SEGMENT_TYPES or kind == "face":
            # record / video / face 仍只保留占位符，不参与权限判定。
            parts.append(f"[{kind}]")
        # at / reply / json / 未知段：不产出文本，也不影响 owner 判定。
    return "".join(parts), tuple(images), omitted


def parse_event(event: dict[str, Any]) -> IncomingMessage:
    """把 OneBot v11 事件收敛成主机模型。

    只读取 INBOUND_READ_FIELDS 里的字段。拒绝矩阵（reason 码）：
    invalid_event / non_message_post_type / invalid_message_type /
    missing_message_id / missing_user_id / invalid_message_body /
    invalid_message_segment / invalid_segment_data。
    """
    if not isinstance(event, dict):
        raise OneBotEventError("invalid_event", "event must be an object")
    if event.get("post_type") != "message":
        raise OneBotEventError("non_message_post_type", str(event.get("post_type")))
    raw_type = event.get("message_type")
    if not isinstance(raw_type, str) or raw_type not in ALLOWED_MESSAGE_TYPES:
        raise OneBotEventError("invalid_message_type", str(raw_type))
    message_id = _scalar_id(event.get("message_id"))
    if message_id is None:
        raise OneBotEventError("missing_message_id")
    user_id = _scalar_id(event.get("user_id"))
    if user_id is None:
        # 昵称绝不替代 user_id：缺 user_id 就是拒绝，哪怕 sender.nickname 完整。
        raise OneBotEventError("missing_user_id")
    text, images, images_omitted = _parse_body(event.get("message", None))
    self_id = _scalar_id(event.get("self_id"))
    is_bot = self_id is not None and self_id == user_id
    message_type = MessageType(raw_type)
    group_id = _scalar_id(event.get("group_id")) if message_type is MessageType.GROUP else None
    sender = event.get("sender")
    nickname = sender.get("nickname") if isinstance(sender, dict) else None
    sender_name = nickname.strip()[:64] if isinstance(nickname, str) else ""
    return IncomingMessage(
        message_id=message_id,
        user_id=user_id,
        text=text,
        message_type=message_type,
        channel="onebot",
        group_id=group_id,
        sender_name=sender_name,
        is_bot=is_bot,
        images=images,
        images_omitted=images_omitted,
    )


def try_parse_event(event: dict[str, Any]) -> tuple[IncomingMessage | None, str]:
    """丢弃式入口：被拒绝的事件返回 (None, reason)，由插件静默处理。"""
    try:
        return parse_event(event), ""
    except OneBotEventError as exc:
        return None, exc.reason


def _require_numeric_id(value: Any, field: str) -> str:
    text = _scalar_id(value)
    if text is None:
        raise OneBotMediaError(f"{field} is required")
    if _ID_SHAPE.fullmatch(text) is None:
        raise OneBotMediaError(f"{field} must be a numeric QQ id; nicknames are not accepted")
    return text


def _accepts_keyword(callable_object: Any, keyword: str) -> bool:
    """探测平台回调是否接受某个关键字参数；探测失败按「不接受」处理。"""
    try:
        signature = inspect.signature(callable_object)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.name == keyword and parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


class OneBotSender:
    """出站边界：AstrBot 平台提供实际 send 回调，这里只做段构造与来源校验。"""

    def __init__(
        self,
        send_private_message,
        *,
        asset_store: Any = None,
        allowed_media_roots: Iterable[str | Path] = (),
    ) -> None:
        self._send_private_message = send_private_message
        self.asset_store = asset_store
        self.allowed_media_roots = tuple(Path(root).resolve() for root in allowed_media_roots)
        self._accepts_idempotency_key = _accepts_keyword(send_private_message, "idempotency_key")

    def _call_platform(self, user_id: str, segments: list[dict[str, Any]], idempotency_key: str | None) -> Any:
        if self._accepts_idempotency_key:
            return self._send_private_message(user_id=user_id, message=segments, idempotency_key=idempotency_key)
        # 平台回调不支持幂等键参数时退回两参数调用。
        return self._send_private_message(user_id=user_id, message=segments)

    # ---- 媒体来源 -------------------------------------------------------
    def _asset_path(self, asset_id: Any) -> str:
        if self.asset_store is None:
            raise OneBotMediaError("media asset requires an AssetStore")
        ident = str(asset_id).strip()
        if not ident:
            raise OneBotMediaError("media asset id is empty")
        info = self.asset_store.get(ident)
        if info is None:
            raise OneBotMediaError(f"unknown media asset: {ident}")
        if isinstance(info, (str, Path)):
            path = Path(info)
        elif isinstance(info, tuple) and info:
            path = Path(info[0])
        elif isinstance(info, dict) and info.get("path"):
            path = Path(str(info["path"]))
        else:
            raise OneBotMediaError("AssetStore returned an unsupported record")
        if not path.is_file():
            raise OneBotMediaError("media asset file is missing on disk")
        return str(path)

    def _allowed_local_path(self, raw: str) -> str:
        if not self.allowed_media_roots:
            raise OneBotMediaError(
                "local media is only allowed under an explicitly allowed media directory"
            )
        path = Path(raw).expanduser().resolve()
        for root in self.allowed_media_roots:
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if not path.is_file():
                raise OneBotMediaError("media file does not exist")
            return str(path)
        raise OneBotMediaError("media file is outside the allowed media directories")

    def _resolve_media_file(self, item: dict[str, Any], kind: str) -> str:
        if "asset_id" in item:
            return self._asset_path(item["asset_id"])
        raw = item.get("file")
        if not isinstance(raw, str) or not raw.strip():
            raise OneBotMediaError(f"{kind} segment requires file")
        value = raw.strip()
        if value.lower().startswith(URL_PREFIXES):
            raise OneBotMediaError(
                f"{kind} segment refuses URL media; use an AssetStore asset or an allowed local file"
            )
        if value.startswith("asset:"):
            return self._asset_path(value[len("asset:"):])
        return self._allowed_local_path(value)

    def _face_id(self, item: dict[str, Any]) -> str:
        value = item.get("id")
        if isinstance(value, bool):
            raise OneBotMediaError("face segment requires a numeric id")
        if isinstance(value, int):
            return str(value)
        if isinstance(value, str) and value.strip().isdigit():
            return value.strip()
        raise OneBotMediaError("face segment requires a numeric id")

    # ---- 段构造 ---------------------------------------------------------
    def build_outbound_segments(
        self,
        user_id: str,
        text: str = "",
        media: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """只产出 text / face / image / record / video 五种段，未知段直接抛错。"""
        normalized_user = _require_numeric_id(user_id, "user_id")
        del normalized_user  # 只为校验；段本身与收件人无关
        if text is not None and not isinstance(text, str):
            raise OneBotMediaError("text must be a string")
        segments: list[dict[str, Any]] = []
        if text:
            segments.append(OneBotSegment("text", {"text": text}).as_dict())
        for item in media or []:
            if not isinstance(item, dict):
                raise OneBotMediaError("media segment must be an object")
            kind = item.get("type")
            if not isinstance(kind, str) or kind not in OUTBOUND_SEGMENT_TYPES:
                raise OneBotMediaError(f"unsupported media segment: {kind!r}")
            if kind == "text":
                value = item.get("text")
                if not isinstance(value, str) or not value:
                    raise OneBotMediaError("text segment requires text")
                segments.append(OneBotSegment("text", {"text": value}).as_dict())
            elif kind == "face":
                segments.append(OneBotSegment("face", {"id": self._face_id(item)}).as_dict())
            else:
                segments.append(
                    OneBotSegment(kind, {"file": self._resolve_media_file(item, kind)}).as_dict()
                )
        return segments

    def send_private(
        self,
        user_id: str,
        text: str = "",
        media: list[dict[str, Any]] | None = None,
        *,
        idempotency_key: str | None = None,
    ) -> Any:
        normalized_user = _require_numeric_id(user_id, "user_id")
        segments = self.build_outbound_segments(normalized_user, text, media)
        if not segments:
            raise OneBotMediaError("message would contain no segment")
        # 幂等键随段一起送到平台：重试的每一次都用同一个键，平台侧可据此去重。
        return self._call_platform(normalized_user, segments, idempotency_key)
