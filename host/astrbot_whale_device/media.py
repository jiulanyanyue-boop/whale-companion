"""M08 媒体资产表与分块上传协议（host 端）。

规范来源：``docs/modules/M08-media-product.md`` 与 ``docs/modules/M08-media-development.md``。

本模块承载 M08 的**服务端**契约，全部为纯标准库实现（无第三方依赖）：

- 素材 ID 只允许 ``[A-Za-z0-9._-]{1,160}``，并额外拒绝 ``..``、``/``、``\\`` 与用户路径；
- 媒体类型只允许 ``image/*``、``audio/*``、``video/*``；
- 单文件上限 100 MiB，单任务最多 20 个素材，临时素材默认 24 小时后清理；
- 分块上传固定 1 MiB，请求形状固定为
  ``POST /v1/devices/{device_id}/assets/{asset_id}/chunks/{index}``
  配 ``Content-Range: bytes start-end/total`` 与 ``X-Asset-Sha256``；
- 只有全部字节到位、总大小与总 sha256 都对上，才返回 ``upload_complete``；
- 失败状态固定七个（见 :class:`MediaCode`），不新增、不改名。

失败码与文档的对应关系（7 个码全部在此，且只在设备端 ``MediaErrors`` 里另有一份镜像）::

    CAMERA_PERMISSION_REQUIRED   相机权限未授予，拍摄前被拒
    PHOTO_PICKER_CANCELLED       用户取消了 Photo Picker / ACTION_OPEN_DOCUMENT
    MEDIA_TOO_LARGE              单文件超过 100 MiB，或单任务素材数超过 20
    MEDIA_TYPE_NOT_ALLOWED       mime 不在 image/* audio/* video/* 之内
    UPLOAD_RETRYABLE             分块偏移/长度/顺序/内容不一致，或上传尚未完成
    UPLOAD_HASH_MISMATCH         全部字节到齐但总 sha256 与 X-Asset-Sha256 不符
    MEDIA_NOT_FOUND              asset_id 不存在、非法，或素材已被清理

注意：ids 非法一律映射到 ``MEDIA_NOT_FOUND``（无法定位到任何素材），不私造新码。
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable


ALLOWED_MEDIA_PREFIXES = ("image/", "audio/", "video/")

ASSET_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,160}")
CONTENT_RANGE_PATTERN = re.compile(r"bytes (\d+)-(\d+)/(\d+)")

MAX_MEDIA_BYTES = 100 * 1024 * 1024
MAX_ASSETS_PER_TASK = 20
CHUNK_BYTES = 1024 * 1024
TEMP_TTL_MS = 24 * 60 * 60 * 1000


class MediaCode(str, Enum):
    """M08 固定失败状态（恰好七个，顺序与开发文档一致）。"""

    CAMERA_PERMISSION_REQUIRED = "CAMERA_PERMISSION_REQUIRED"
    PHOTO_PICKER_CANCELLED = "PHOTO_PICKER_CANCELLED"
    MEDIA_TOO_LARGE = "MEDIA_TOO_LARGE"
    MEDIA_TYPE_NOT_ALLOWED = "MEDIA_TYPE_NOT_ALLOWED"
    UPLOAD_RETRYABLE = "UPLOAD_RETRYABLE"
    UPLOAD_HASH_MISMATCH = "UPLOAD_HASH_MISMATCH"
    MEDIA_NOT_FOUND = "MEDIA_NOT_FOUND"

    @classmethod
    def all(cls) -> frozenset[str]:
        return frozenset(member.value for member in cls)


class MediaError(ValueError):
    """带 M08 固定失败码的媒体错误；``ValueError`` 兼容既有调用方。"""

    def __init__(self, code: MediaCode | str, message: str) -> None:
        self.code = code.value if isinstance(code, MediaCode) else str(code)
        self.reason = message
        super().__init__(f"{self.code}: {message}")


def now_ms() -> int:
    return int(time.time() * 1000)


# ---------------------------------------------------------------------------
# 规则：素材 ID、媒体类型、大小上限
# ---------------------------------------------------------------------------


def is_valid_asset_id(ident: object) -> bool:
    """素材 ID 只允许 ``[A-Za-z0-9._-]{1,160}``，并禁止 ``..`` 与路径分隔符。"""
    if not isinstance(ident, str):
        return False
    if not 1 <= len(ident) <= 160:
        return False
    if "/" in ident or "\\" in ident:
        return False
    if ".." in ident:
        return False
    return ASSET_ID_PATTERN.fullmatch(ident) is not None


def is_valid_device_id(device_id: object) -> bool:
    """上传路径里的 device_id 同样只能是一段安全 token，防止路径注入。"""
    if not isinstance(device_id, str):
        return False
    if not 1 <= len(device_id) <= 128:
        return False
    if "/" in device_id or "\\" in device_id or ".." in device_id:
        return False
    return re.fullmatch(r"[A-Za-z0-9._-]{1,128}", device_id) is not None


def validate_asset_id(ident: object) -> str:
    if not is_valid_asset_id(ident):
        raise MediaError(MediaCode.MEDIA_NOT_FOUND, f"invalid asset id: {ident!r}")
    return str(ident)


def is_allowed_media_type(mime: object) -> bool:
    """媒体类型只允许 ``image/*``、``audio/*``、``video/*``。"""
    if not isinstance(mime, str):
        return False
    if not mime.startswith(ALLOWED_MEDIA_PREFIXES):
        return False
    prefix, _, subtype = mime.partition("/")
    if not prefix or not subtype.strip():
        return False
    if "/" in subtype or " " in subtype or ";" in subtype:
        return False
    return True


def validate_media_type(mime: object) -> str:
    if not is_allowed_media_type(mime):
        raise MediaError(MediaCode.MEDIA_TYPE_NOT_ALLOWED, f"media type is not allowed: {mime!r}")
    return str(mime)


def is_allowed_media_size(size: object, max_bytes: int = MAX_MEDIA_BYTES) -> bool:
    """单文件大小必须落在 ``(0, max_bytes]``。"""
    if not isinstance(size, int) or isinstance(size, bool):
        return False
    return 0 < size <= max_bytes


def validate_media_size(size: object, max_bytes: int = MAX_MEDIA_BYTES) -> int:
    if not is_allowed_media_size(size, max_bytes):
        raise MediaError(
            MediaCode.MEDIA_TOO_LARGE,
            f"media size must be within 1..{max_bytes} bytes: {size!r}",
        )
    return int(size)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 规则：分块偏移与 Content-Range（固定 1 MiB）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContentRange:
    """一段分块在整份素材中的位置；``total`` 是整份素材的字节数。"""

    start: int
    end: int
    total: int
    index: int = 0

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    def header(self) -> str:
        return f"bytes {self.start}-{self.end}/{self.total}"

    def is_last(self, chunk_bytes: int = CHUNK_BYTES) -> bool:
        return self.end == self.total - 1


def chunk_count(total_bytes: int, chunk_bytes: int = CHUNK_BYTES) -> int:
    if total_bytes <= 0:
        return 0
    chunk = max(1, int(chunk_bytes))
    return (total_bytes + chunk - 1) // chunk


def plan_content_range(index: int, total_bytes: int, chunk_bytes: int = CHUNK_BYTES) -> ContentRange:
    """按 ``index`` 计算 ``Content-Range``；越界视为可重试的服务端拒绝。"""
    if total_bytes <= 0:
        raise MediaError(MediaCode.MEDIA_TOO_LARGE, "media total size must be positive")
    if index < 0:
        raise MediaError(MediaCode.UPLOAD_RETRYABLE, f"chunk index must be non-negative: {index}")
    count = chunk_count(total_bytes, chunk_bytes)
    if index >= count:
        raise MediaError(
            MediaCode.UPLOAD_RETRYABLE,
            f"chunk index {index} is out of range for {total_bytes} bytes ({count} chunks)",
        )
    chunk = max(1, int(chunk_bytes))
    start = index * chunk
    end = min(start + chunk - 1, total_bytes - 1)
    return ContentRange(start=start, end=end, total=total_bytes, index=index)


def plan_all_chunks(total_bytes: int, chunk_bytes: int = CHUNK_BYTES) -> list[ContentRange]:
    return [plan_content_range(i, total_bytes, chunk_bytes) for i in range(chunk_count(total_bytes, chunk_bytes))]


def parse_content_range(raw: object, chunk_bytes: int = CHUNK_BYTES) -> ContentRange:
    """严格解析 ``bytes start-end/total``；任何偏差都视为可重试。"""
    if not isinstance(raw, str):
        raise MediaError(MediaCode.UPLOAD_RETRYABLE, "Content-Range header is missing")
    match = CONTENT_RANGE_PATTERN.fullmatch(raw.strip())
    if match is None:
        raise MediaError(MediaCode.UPLOAD_RETRYABLE, f"malformed Content-Range: {raw!r}")
    start, end, total = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if total <= 0:
        raise MediaError(MediaCode.MEDIA_TOO_LARGE, "media total size must be positive")
    if start < 0 or end < start or end >= total:
        raise MediaError(MediaCode.UPLOAD_RETRYABLE, f"Content-Range is outside the asset bounds: {raw!r}")
    chunk = max(1, int(chunk_bytes))
    if start % chunk != 0:
        raise MediaError(
            MediaCode.UPLOAD_RETRYABLE,
            f"chunk start {start} is not aligned to the fixed {chunk}-byte chunk size",
        )
    index = start // chunk
    expected = plan_content_range(index, total, chunk)
    if end != expected.end:
        raise MediaError(
            MediaCode.UPLOAD_RETRYABLE,
            f"Content-Range end {end} does not match the fixed chunk plan {expected.end}",
        )
    return ContentRange(start=start, end=end, total=total, index=index)


def asset_chunk_path(device_id: str, asset_id: str, index: int) -> str:
    """固定上传路径：``/v1/devices/{device_id}/assets/{asset_id}/chunks/{index}``。"""
    if not is_valid_device_id(device_id):
        raise MediaError(MediaCode.MEDIA_NOT_FOUND, f"invalid device id: {device_id!r}")
    validate_asset_id(asset_id)
    if index < 0:
        raise MediaError(MediaCode.UPLOAD_RETRYABLE, f"chunk index must be non-negative: {index}")
    return f"/v1/devices/{device_id}/assets/{asset_id}/chunks/{index}"


def asset_chunk_url(base_url: str, device_id: str, asset_id: str, index: int) -> str:
    """拼出完整 HTTPS URL；非 https 端点直接拒绝（媒体不得明文传输）。"""
    if not isinstance(base_url, str) or not base_url.startswith("https://"):
        raise MediaError(MediaCode.UPLOAD_RETRYABLE, "upload endpoint must be an https URL")
    return base_url.rstrip("/") + asset_chunk_path(device_id, asset_id, index)


# ---------------------------------------------------------------------------
# 规则：sha256 与上传完成判定
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path, block: int = CHUNK_BYTES) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(block)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def is_upload_byte_complete(received_chunks: int, total_bytes: int, chunk_bytes: int = CHUNK_BYTES) -> bool:
    """字节数量是否到位；未到位才是 ``UPLOAD_RETRYABLE``。"""
    return received_chunks >= chunk_count(total_bytes, chunk_bytes)


def verify_upload_hash(path: str | Path, expected_sha256: str) -> bool:
    """总 sha256 是否与 ``X-Asset-Sha256`` 一致；不一致才是 ``UPLOAD_HASH_MISMATCH``。"""
    if not isinstance(expected_sha256, str) or not expected_sha256:
        return False
    return sha256_file(path) == expected_sha256.lower()


# ---------------------------------------------------------------------------
# 素材表
# ---------------------------------------------------------------------------


class AssetStore:
    """设备/主机共享的素材表：``root`` 下只放已校验 ID 的素材文件。

    保留既有构造与 ``register`` / ``get`` 语义（``host/tests/test_core.py`` 依赖），
    增强部分为 sha256、任务归属、24 小时清理与原子上限校验。
    """

    def __init__(
        self,
        root: str | Path,
        db: sqlite3.Connection | None = None,
        max_bytes: int = MAX_MEDIA_BYTES,
        ttl_ms: int = TEMP_TTL_MS,
        max_assets_per_task: int = MAX_ASSETS_PER_TASK,
    ) -> None:
        self.root = Path(root).resolve()
        self.max_bytes = int(max_bytes)
        self.ttl_ms = int(ttl_ms)
        self.max_assets_per_task = int(max_assets_per_task)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = db or sqlite3.connect(":memory:")
        self._ensure_schema()

    # -- schema ------------------------------------------------------------

    def _ensure_schema(self) -> None:
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS assets("
            "id TEXT PRIMARY KEY, path TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, "
            "sha256 TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL DEFAULT '', "
            "created_at_ms INTEGER NOT NULL DEFAULT 0)"
        )
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(assets)")}
        for name, ddl in (
            ("sha256", "sha256 TEXT NOT NULL DEFAULT ''"),
            ("task_id", "task_id TEXT NOT NULL DEFAULT ''"),
            ("created_at_ms", "created_at_ms INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in existing:
                self.db.execute(f"ALTER TABLE assets ADD COLUMN {ddl}")
        self.db.commit()

    # -- 路径安全 ----------------------------------------------------------

    def _target(self, asset_id: str) -> Path:
        """把已校验 ID 映射到 root 内的文件；任何越界都拒绝。"""
        validate_asset_id(asset_id)
        resolved = (self.root / asset_id).resolve()
        if resolved.parent != self.root or os.path.commonpath([str(self.root), str(resolved)]) != str(self.root):
            raise MediaError(MediaCode.MEDIA_NOT_FOUND, f"asset id escapes the store root: {asset_id!r}")
        return resolved

    # -- 写入 --------------------------------------------------------------

    def register(
        self,
        source: str | Path,
        asset_id: str | None = None,
        *,
        task_id: str | None = None,
        media_type: str | None = None,
        sha256_hex: str | None = None,
        now: int | None = None,
    ) -> str:
        """登记一个素材：校验 ID → 存在性 → 大小 → 类型 → 单任务上限，再复制进 root。

        ``MEDIA_TOO_LARGE`` 同时覆盖「单文件超过 100 MiB」与「单任务超过 20 个素材」，
        因为 M08 固定集合里没有独立的「任务素材过多」码，不得私造。
        """
        ident = validate_asset_id(uuid.uuid4().hex if asset_id is None else asset_id)
        source_path = Path(source).resolve()
        if not source_path.is_file():
            raise FileNotFoundError(source_path)

        size = source_path.stat().st_size
        validate_media_size(size, self.max_bytes)

        mime = validate_media_type(media_type) if media_type is not None else None
        if mime is None:
            guessed = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
            mime = validate_media_type(guessed)

        task = task_id or ""
        if task and not self._exists(ident) and self.count_for_task(task) >= self.max_assets_per_task:
            raise MediaError(
                MediaCode.MEDIA_TOO_LARGE,
                f"task {task!r} already holds {self.max_assets_per_task} assets",
            )

        target = self._target(ident)
        shutil.copyfile(source_path, target)
        digest = (sha256_hex or sha256_file(target)).lower()
        created = int(now) if now is not None else now_ms()
        self.db.execute(
            "INSERT OR REPLACE INTO assets(id,path,mime,size,sha256,task_id,created_at_ms) VALUES(?,?,?,?,?,?,?)",
            (ident, str(target), mime, target.stat().st_size, digest, task, created),
        )
        self.db.commit()
        return ident

    def register_bytes(
        self,
        data: bytes,
        asset_id: str,
        media_type: str,
        *,
        task_id: str | None = None,
        now: int | None = None,
    ) -> str:
        """从内存字节登记（上传缓冲完成时使用），不写中间文件。"""
        ident = validate_asset_id(asset_id)
        payload = bytes(data)
        validate_media_size(len(payload), self.max_bytes)
        mime = validate_media_type(media_type)
        task = task_id or ""
        if task and not self._exists(ident) and self.count_for_task(task) >= self.max_assets_per_task:
            raise MediaError(
                MediaCode.MEDIA_TOO_LARGE,
                f"task {task!r} already holds {self.max_assets_per_task} assets",
            )
        target = self._target(ident)
        target.write_bytes(payload)
        created = int(now) if now is not None else now_ms()
        self.db.execute(
            "INSERT OR REPLACE INTO assets(id,path,mime,size,sha256,task_id,created_at_ms) VALUES(?,?,?,?,?,?,?)",
            (ident, str(target), mime, len(payload), sha256_bytes(payload), task, created),
        )
        self.db.commit()
        return ident

    # -- 读取 --------------------------------------------------------------

    def _exists(self, asset_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM assets WHERE id=?", (asset_id,)).fetchone() is not None

    def get(self, asset_id: str) -> tuple[Path, str, int] | None:
        """既有语义：返回 ``(path, mime, size)``；不存在或 ID 非法返回 ``None``。"""
        if not is_valid_asset_id(asset_id):
            return None
        row = self.db.execute("SELECT path,mime,size FROM assets WHERE id=?", (asset_id,)).fetchone()
        return None if row is None else (Path(row[0]), str(row[1]), int(row[2]))

    def describe(self, asset_id: str) -> dict[str, Any] | None:
        """素材元数据；**不含真实路径**，可直接用于协议回包与 OneBot 只传元数据。"""
        if not is_valid_asset_id(asset_id):
            return None
        row = self.db.execute(
            "SELECT id,mime,size,sha256,task_id,created_at_ms FROM assets WHERE id=?",
            (asset_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "asset_id": str(row[0]),
            "mime": str(row[1]),
            "size": int(row[2]),
            "sha256": str(row[3]),
            "task_id": str(row[4]),
            "created_at_ms": int(row[5]),
        }

    def require(self, asset_id: str) -> dict[str, Any]:
        data = self.describe(asset_id)
        if data is None:
            raise MediaError(MediaCode.MEDIA_NOT_FOUND, f"asset is not registered: {asset_id!r}")
        return data

    def path_of(self, asset_id: str) -> Path:
        """受控路径读取：只有已登记素材才给路径，调用方不得把路径外发。"""
        self.require(asset_id)
        return self._target(asset_id)

    def count_for_task(self, task_id: str) -> int:
        if not task_id:
            return 0
        row = self.db.execute("SELECT COUNT(*) FROM assets WHERE task_id=?", (task_id,)).fetchone()
        return 0 if row is None else int(row[0])

    def assets_for_task(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id FROM assets WHERE task_id=? ORDER BY created_at_ms ASC, id ASC",
            (task_id,),
        ).fetchall()
        described: list[dict[str, Any]] = []
        for (ident,) in rows:
            data = self.describe(str(ident))
            if data is not None:
                described.append(data)
        return described

    # -- 删除与清理 --------------------------------------------------------

    def remove(self, asset_id: str) -> bool:
        if not is_valid_asset_id(asset_id):
            return False
        if not self._exists(asset_id):
            return False
        self.db.execute("DELETE FROM assets WHERE id=?", (asset_id,))
        self.db.commit()
        target = self._target(asset_id)
        if target.is_file():
            target.unlink()
        return True

    def sweep_expired(self, at: int | None = None) -> list[str]:
        """删除超过 ``ttl_ms``（默认 24 小时）的临时素材，返回被删 ID。"""
        cutoff = int(at) if at is not None else now_ms()
        rows = self.db.execute(
            "SELECT id FROM assets WHERE created_at_ms > 0 AND created_at_ms <= ?",
            (cutoff - self.ttl_ms,),
        ).fetchall()
        removed: list[str] = []
        for (ident,) in rows:
            if self.remove(str(ident)):
                removed.append(str(ident))
        return removed


# ---------------------------------------------------------------------------
# 分块上传
# ---------------------------------------------------------------------------


@dataclass
class _PendingUpload:
    asset_id: str
    media_type: str
    total_bytes: int
    sha256: str
    task_id: str
    temp_path: Path
    chunk_hashes: dict[int, str] = field(default_factory=dict)

    @property
    def received_chunks(self) -> int:
        return len(self.chunk_hashes)


class AssetUploads:
    """服务端分块上传状态机。

    固定约束（M08 开发文档「上传协议」）：

    - 路径段与头由 :func:`asset_chunk_path` / :class:`ContentRange` 统一生成；
    - 每块必须是固定 1 MiB 的 ``Content-Range``，start 对齐，end 按计划；
    - 乱序到达允许（按 offset 落盘），但同一 index 的重复块必须内容一致，否则
      ``UPLOAD_RETRYABLE``；
    - 只有全部字节到位且总 sha256 匹配，才返回 ``upload_complete``；
    - 失败码严格取自 :class:`MediaCode`，不新增。
    """

    def __init__(
        self,
        store: AssetStore,
        *,
        chunk_bytes: int = CHUNK_BYTES,
        work_dir: str | Path | None = None,
    ) -> None:
        self.store = store
        self.chunk_bytes = int(chunk_bytes)
        self.work_dir = Path(work_dir) if work_dir is not None else store.root / ".chunks"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._pending: dict[str, _PendingUpload] = {}

    # -- 生命周期 ----------------------------------------------------------

    def begin(
        self,
        asset_id: str,
        media_type: str,
        total_bytes: int,
        sha256_hex: str,
        *,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        ident = validate_asset_id(asset_id)
        mime = validate_media_type(media_type)
        total = validate_media_size(total_bytes, self.store.max_bytes)
        if not isinstance(sha256_hex, str) or not re.fullmatch(r"[A-Fa-f0-9]{64}", sha256_hex):
            raise MediaError(MediaCode.UPLOAD_RETRYABLE, "X-Asset-Sha256 must be a 64-hex digest")
        if self._pending.get(ident) is not None:
            raise MediaError(MediaCode.UPLOAD_RETRYABLE, f"upload already in progress: {ident!r}")
        if self.store.describe(ident) is not None:
            raise MediaError(MediaCode.UPLOAD_RETRYABLE, f"asset is already registered: {ident!r}")

        temp = self.work_dir / f"{ident}.part"
        with temp.open("wb") as handle:
            handle.truncate(total)
        pending = _PendingUpload(
            asset_id=ident,
            media_type=mime,
            total_bytes=total,
            sha256=sha256_hex.lower(),
            task_id=task_id or "",
            temp_path=temp,
        )
        self._pending[ident] = pending
        return self.status(ident)

    def status(self, asset_id: str) -> dict[str, Any]:
        pending = self._pending.get(asset_id)
        if pending is None:
            raise MediaError(MediaCode.MEDIA_NOT_FOUND, f"no upload in progress: {asset_id!r}")
        total_chunks = chunk_count(pending.total_bytes, self.chunk_bytes)
        return {
            "asset_id": pending.asset_id,
            "mime": pending.media_type,
            "size": pending.total_bytes,
            "sha256": pending.sha256,
            "received_chunks": pending.received_chunks,
            "total_chunks": total_chunks,
            "upload_complete": pending.received_chunks >= total_chunks,
        }

    def abort(self, asset_id: str) -> bool:
        pending = self._pending.pop(asset_id, None)
        if pending is None:
            return False
        if pending.temp_path.is_file():
            pending.temp_path.unlink()
        return True

    def cleanup_stale(self, at: int | None = None) -> list[str]:
        """清理未完成上传的临时块文件（设备已 24 小时清理，服务端同步）。"""
        removed: list[str] = []
        for ident in list(self._pending):
            temp = self._pending[ident].temp_path
            if not temp.is_file():
                self._pending.pop(ident, None)
                removed.append(ident)
        return removed

    # -- 写入分块 ----------------------------------------------------------

    def write_chunk(
        self,
        asset_id: str,
        index: int,
        data: bytes,
        content_range: str,
        *,
        request_sha256: str | None = None,
    ) -> dict[str, Any]:
        pending = self._pending.get(validate_asset_id(asset_id))
        if pending is None:
            raise MediaError(MediaCode.MEDIA_NOT_FOUND, f"no upload in progress: {asset_id!r}")

        declared = parse_content_range(content_range, self.chunk_bytes)
        if declared.total != pending.total_bytes:
            raise MediaError(
                MediaCode.UPLOAD_RETRYABLE,
                f"chunk total {declared.total} disagrees with begin total {pending.total_bytes}",
            )
        expected = plan_content_range(index, pending.total_bytes, self.chunk_bytes)
        if declared.start != expected.start or declared.end != expected.end:
            raise MediaError(
                MediaCode.UPLOAD_RETRYABLE,
                f"chunk {index} offset {declared.header()} does not match the plan {expected.header()}",
            )
        if request_sha256 is not None and request_sha256.lower() != pending.sha256:
            raise MediaError(
                MediaCode.UPLOAD_HASH_MISMATCH,
                "X-Asset-Sha256 changed between chunks",
            )

        payload = bytes(data)
        if len(payload) != expected.length:
            raise MediaError(
                MediaCode.UPLOAD_RETRYABLE,
                f"chunk {index} carries {len(payload)} bytes, Content-Range declares {expected.length}",
            )

        digest = sha256_bytes(payload)
        existing = pending.chunk_hashes.get(index)
        if existing is not None:
            if existing != digest:
                raise MediaError(
                    MediaCode.UPLOAD_RETRYABLE,
                    f"chunk {index} was already uploaded with different content",
                )
            return self.status(pending.asset_id)

        with pending.temp_path.open("r+b") as handle:
            handle.seek(expected.start)
            handle.write(payload)
        pending.chunk_hashes[index] = digest
        return self.status(pending.asset_id)

    def write_chunk_auto(
        self,
        asset_id: str,
        data: bytes,
        content_range: str,
        *,
        request_sha256: str | None = None,
    ) -> dict[str, Any]:
        """从 ``Content-Range`` 推出 index 再写块（等价于服务端路由解析后的调用）。"""
        declared = parse_content_range(content_range, self.chunk_bytes)
        return self.write_chunk(
            asset_id,
            declared.index,
            data,
            content_range,
            request_sha256=request_sha256,
        )

    # -- 完成 --------------------------------------------------------------

    def complete(self, asset_id: str) -> dict[str, Any]:
        """全部字节到位 → 校验总 sha256 → 登记进 :class:`AssetStore`。

        - 缺块 → ``UPLOAD_RETRYABLE``（可续传，不重复落库）
        - 字节齐但 hash 不符 → ``UPLOAD_HASH_MISMATCH``，同时丢弃这次上传缓冲
        - 成功 → 返回 ``upload_complete=True`` 的元数据（含 asset_id/mime/size/sha256）
        """
        pending = self._pending.get(validate_asset_id(asset_id))
        if pending is None:
            raise MediaError(MediaCode.MEDIA_NOT_FOUND, f"no upload in progress: {asset_id!r}")

        total_chunks = chunk_count(pending.total_bytes, self.chunk_bytes)
        if pending.received_chunks < total_chunks:
            raise MediaError(
                MediaCode.UPLOAD_RETRYABLE,
                f"upload incomplete: {pending.received_chunks}/{total_chunks} chunks",
            )

        actual_size = pending.temp_path.stat().st_size if pending.temp_path.is_file() else 0
        if actual_size != pending.total_bytes:
            raise MediaError(
                MediaCode.UPLOAD_RETRYABLE,
                f"buffered size {actual_size} disagrees with declared {pending.total_bytes}",
            )
        if not verify_upload_hash(pending.temp_path, pending.sha256):
            self.abort(pending.asset_id)
            raise MediaError(
                MediaCode.UPLOAD_HASH_MISMATCH,
                "total sha256 does not match X-Asset-Sha256",
            )

        ident = self.store.register(
            pending.temp_path,
            pending.asset_id,
            task_id=pending.task_id or None,
            media_type=pending.media_type,
            sha256_hex=pending.sha256,
        )
        self.abort(pending.asset_id)
        described = self.store.require(ident)
        return {**described, "upload_complete": True}


def chunk_headers(asset_id: str, index: int, total_bytes: int, sha256_hex: str, chunk_bytes: int = CHUNK_BYTES) -> dict[str, str]:
    """设备端要带的固定头；两侧共用同一实现，避免形状漂移。"""
    validate_asset_id(asset_id)
    planned = plan_content_range(index, total_bytes, chunk_bytes)
    if not isinstance(sha256_hex, str) or not re.fullmatch(r"[A-Fa-f0-9]{64}", sha256_hex):
        raise MediaError(MediaCode.UPLOAD_RETRYABLE, "X-Asset-Sha256 must be a 64-hex digest")
    return {
        "Content-Range": planned.header(),
        "X-Asset-Sha256": sha256_hex.lower(),
        "Content-Type": "application/octet-stream",
    }


def chunk_payload(path: str | Path, index: int, total_bytes: int, chunk_bytes: int = CHUNK_BYTES) -> bytes:
    """按固定分块计划读取第 index 块，长度必须与 Content-Range 完全一致。"""
    planned = plan_content_range(index, total_bytes, chunk_bytes)
    with Path(path).open("rb") as handle:
        handle.seek(planned.start)
        data = handle.read(planned.length)
    if len(data) != planned.length:
        raise MediaError(
            MediaCode.UPLOAD_RETRYABLE,
            f"chunk {index} read {len(data)} bytes, expected {planned.length}",
        )
    return data


def media_evidence(described: dict[str, Any]) -> dict[str, str]:
    """协议与 OneBot 只用这四项元数据，绝不带真实路径。"""
    return {
        "asset_id": str(described["asset_id"]),
        "mime": str(described["mime"]),
        "size": str(described["size"]),
        "sha256": str(described["sha256"]),
    }


def iter_asset_ids(store: AssetStore, task_id: str | None = None) -> Iterable[str]:
    """按任务枚举素材 ID（只读表，绝不扫描外部相册）。"""
    if task_id:
        return [item["asset_id"] for item in store.assets_for_task(task_id)]
    rows = store.db.execute("SELECT id FROM assets ORDER BY created_at_ms ASC, id ASC").fetchall()
    return [str(row[0]) for row in rows]
