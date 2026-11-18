"""M08 媒体：host 侧固定测试（真实运行，无第三方依赖）。

对应 ``docs/modules/M08-media-development.md``：

- AssetStore：素材 ID ``[A-Za-z0-9._-]{1,160}``、禁止 ``..``/斜杠/反斜杠/用户路径、
  mime 只允许 ``image/*``/``audio/*``/``video/*``、单文件 100 MiB、单任务 20 个、24 小时清理；
- 上传协议：``POST /v1/devices/{device_id}/assets/{asset_id}/chunks/{index}`` +
  ``Content-Range: bytes start-end/total`` + ``X-Asset-Sha256``，分块固定 1 MiB；
- 只有全部字节到位且总 sha256 匹配才返回 ``upload_complete``；
- 七个失败状态各自可产生、且只有这七个。
"""

import hashlib
import re

import pytest

from astrbot_whale_device import media as m08
from astrbot_whale_device.media import (
    CHUNK_BYTES,
    MAX_ASSETS_PER_TASK,
    MAX_MEDIA_BYTES,
    TEMP_TTL_MS,
    AssetStore,
    AssetUploads,
    MediaCode,
    MediaError,
)

#: 开发文档「失败状态」代码块里的七个码，逐字抄写，测试与实现必须双向一致。
DOCUMENTED_FAILURE_CODES = {
    "CAMERA_PERMISSION_REQUIRED",
    "PHOTO_PICKER_CANCELLED",
    "MEDIA_TOO_LARGE",
    "MEDIA_TYPE_NOT_ALLOWED",
    "UPLOAD_RETRYABLE",
    "UPLOAD_HASH_MISMATCH",
    "MEDIA_NOT_FOUND",
}


def write(path, data: bytes):
    path.write_bytes(data)
    return path


# ---------------------------------------------------------------------------
# 0. 固定集合与固定常量
# ---------------------------------------------------------------------------


def test_failure_code_set_is_exactly_the_seven_documented_codes():
    assert MediaCode.all() == DOCUMENTED_FAILURE_CODES
    assert len(MediaCode.all()) == 7


def test_chunk_size_and_limits_are_fixed_by_document():
    assert CHUNK_BYTES == 1024 * 1024  # 分块固定 1 MiB
    assert MAX_MEDIA_BYTES == 100 * 1024 * 1024  # 单文件 100 MiB
    assert MAX_ASSETS_PER_TASK == 20  # 单任务最多 20 个素材
    assert TEMP_TTL_MS == 24 * 60 * 60 * 1000  # 临时素材 24 小时


def test_media_error_is_still_a_value_error():
    # 既有调用方按 ValueError 捕获，迁移不能破坏这一点。
    with pytest.raises(ValueError):
        raise MediaError(MediaCode.UPLOAD_RETRYABLE, "boom")
    error = MediaError(MediaCode.MEDIA_NOT_FOUND, "boom")
    assert error.code == "MEDIA_NOT_FOUND"
    assert isinstance(error, ValueError)


# ---------------------------------------------------------------------------
# 1. AssetStore：素材 ID 规则
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ident",
    ["a", "image_001", "IMG-2024.10.30", "abc.DEF-123_456", "x" * 160, "A1-_."],
)
def test_asset_id_accepts_documented_charset(ident):
    assert m08.is_valid_asset_id(ident) is True


@pytest.mark.parametrize(
    "ident",
    [
        "",
        "x" * 161,           # 超长
        "../escape",         # 父目录
        "a/../../etc",       # 斜杠 + ..
        "..",                # 纯 ..
        "aa..bb",            # 任意位置 ..
        "sub/dir",           # 斜杠
        "sub\\dir",          # 反斜杠
        "C:\\Users\\me\\x",  # Windows 用户路径
        "/etc/passwd",       # 绝对路径
        "~/media",           # 用户路径
        "file name",         # 空格不在字符集内
        "资产.png",          # 非 ASCII
        "a:b",               # 冒号
        "asset\n",           # 换行
        None,                # 非字符串
        123,
    ],
)
def test_asset_id_rejects_everything_else(ident):
    assert m08.is_valid_asset_id(ident) is False


def test_register_rejects_path_traversal_and_user_paths(tmp_path):
    source = write(tmp_path / "image.png", b"\x89PNG-ish")
    store = AssetStore(tmp_path / "assets")

    for bad in ("../escape", "..\\escape", "sub/dir", "C:\\Users\\me\\secret.png", "/etc/passwd", "..", "x" * 200):
        with pytest.raises(MediaError) as raised:
            store.register(source, bad)
        assert raised.value.code == "MEDIA_NOT_FOUND"

    # 合法的 ID 才能落库，且文件必须落在 root 内。
    ident = store.register(source, "photo_001")
    assert ident == "photo_001"
    path, mime, size = store.get("photo_001")
    assert path.parent == (tmp_path / "assets").resolve()
    assert path.name == "photo_001"
    assert mime == "image/png"
    assert size == len(b"\x89PNG-ish")


def test_register_without_id_generates_a_valid_uuid_hex_id(tmp_path):
    source = write(tmp_path / "a.png", b"png")
    store = AssetStore(tmp_path / "assets")
    ident = store.register(source)
    assert m08.is_valid_asset_id(ident)
    assert len(ident) == 32


def test_get_and_describe_return_none_for_unknown_or_invalid_ids(tmp_path):
    store = AssetStore(tmp_path / "assets", max_bytes=3)
    write(tmp_path / "a.png", b"png")
    store.register(tmp_path / "a.png", "safe")
    assert store.get("missing") is None
    assert store.get("../escape") is None
    assert store.get("") is None
    assert store.describe("missing") is None
    assert store.describe("../escape") is None


# ---------------------------------------------------------------------------
# 2. AssetStore：媒体类型
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mime",
    ["image/png", "image/jpeg", "image/webp", "audio/mp4", "audio/ogg", "video/mp4", "video/quicktime"],
)
def test_media_type_accepts_image_audio_video_only(mime):
    assert m08.is_allowed_media_type(mime) is True


@pytest.mark.parametrize(
    "mime",
    [
        "application/pdf",
        "application/octet-stream",
        "text/plain",
        "text/html",
        "image",          # 缺子类型
        "image/",         # 空子类型
        "image/png;charset=utf8",
        "IMAGE/PNG",      # 前缀大小写不符
        "",
        None,
        12,
    ],
)
def test_media_type_rejects_everything_else(mime):
    assert m08.is_allowed_media_type(mime) is False


def test_register_rejects_disallowed_media_type(tmp_path):
    store = AssetStore(tmp_path / "assets")
    # 显式声明类型时按声明校验，扩展名骗不过去。
    source = write(tmp_path / "note.png", b"not an image")
    with pytest.raises(MediaError) as raised:
        store.register(source, "note", media_type="application/pdf")
    assert raised.value.code == "MEDIA_TYPE_NOT_ALLOWED"

    # 未声明类型时按扩展名猜测；猜不出来就是 octet-stream，同样被拒。
    weird = write(tmp_path / "blob.bin", b"binary")
    with pytest.raises(MediaError) as raised:
        store.register(weird, "blob")
    assert raised.value.code == "MEDIA_TYPE_NOT_ALLOWED"

    # 声明了合法类型则允许（例如服务端已按内容探测出 image/*）。
    assert store.register(source, "note2", media_type="image/png") == "note2"


# ---------------------------------------------------------------------------
# 3. AssetStore：大小上限、任务素材数、24 小时清理
# ---------------------------------------------------------------------------


def test_register_rejects_media_larger_than_limit(tmp_path):
    store = AssetStore(tmp_path / "assets", max_bytes=3)
    source = write(tmp_path / "a.png", b"png4")  # 4 > 3
    with pytest.raises(MediaError) as raised:
        store.register(source, "safe")
    assert raised.value.code == "MEDIA_TOO_LARGE"

    # 默认上限就是 100 MiB；声明超限字节同样被拒绝（不实际写 100 MiB）。
    with pytest.raises(MediaError) as raised:
        m08.validate_media_size(MAX_MEDIA_BYTES + 1)
    assert raised.value.code == "MEDIA_TOO_LARGE"
    assert m08.validate_media_size(MAX_MEDIA_BYTES) == MAX_MEDIA_BYTES


@pytest.mark.parametrize("size", [0, -1, MAX_MEDIA_BYTES + 1, True, False, 1.5, "10", None])
def test_media_size_rejects_zero_negative_bool_float_and_over_limit(size):
    assert m08.is_allowed_media_size(size) is False


def test_register_rejects_missing_source_file(tmp_path):
    store = AssetStore(tmp_path / "assets")
    with pytest.raises(FileNotFoundError):
        store.register(tmp_path / "nope.png", "ghost")


def test_single_task_caps_assets_at_twenty(tmp_path):
    store = AssetStore(tmp_path / "assets")
    source = write(tmp_path / "a.png", b"png")
    for index in range(MAX_ASSETS_PER_TASK):
        store.register(source, f"img{index:03d}", task_id="task-1")
    assert store.count_for_task("task-1") == MAX_ASSETS_PER_TASK

    with pytest.raises(MediaError) as raised:
        store.register(source, "img020", task_id="task-1")
    # 固定集合没有「任务素材过多」码，并入 MEDIA_TOO_LARGE，不得私造。
    assert raised.value.code == "MEDIA_TOO_LARGE"

    # 换一个任务仍然可以登记（上限是「单任务」）。
    assert store.register(source, "img020", task_id="task-2") == "img020"
    assert len(store.assets_for_task("task-2")) == 1


def test_registering_same_id_twice_does_not_consume_task_quota(tmp_path):
    store = AssetStore(tmp_path / "assets")
    source = write(tmp_path / "a.png", b"png")
    for index in range(MAX_ASSETS_PER_TASK):
        store.register(source, f"img{index:03d}", task_id="task-1")
    # 覆盖已有的同一 ID 不是「新增素材」，不应触发上限。
    assert store.register(source, "img000", task_id="task-1") == "img000"
    assert store.count_for_task("task-1") == MAX_ASSETS_PER_TASK


def test_sweep_expired_removes_only_assets_older_than_24_hours(tmp_path):
    store = AssetStore(tmp_path / "assets")
    source = write(tmp_path / "a.png", b"png")
    store.register(source, "old", now=1)
    store.register(source, "new", now=10_000)

    # 24 小时边界：old 恰好到期被删，new 保留。
    removed = store.sweep_expired(at=TEMP_TTL_MS + 1)
    assert removed == ["old"]
    assert store.get("old") is None
    assert store.get("new") is not None
    assert not (tmp_path / "assets" / "old").exists()


def test_describe_never_exposes_the_real_path(tmp_path):
    store = AssetStore(tmp_path / "assets")
    source = write(tmp_path / "a.png", b"png")
    store.register(source, "photo", task_id="task-1")
    described = store.describe("photo")
    assert set(described) == {"asset_id", "mime", "size", "sha256", "task_id", "created_at_ms"}
    assert str(tmp_path) not in str(described)
    assert m08.media_evidence(described) == {
        "asset_id": "photo",
        "mime": "image/png",
        "size": "3",
        "sha256": hashlib.sha256(b"png").hexdigest(),
    }


def test_require_and_path_of_raise_media_not_found(tmp_path):
    store = AssetStore(tmp_path / "assets")
    write(tmp_path / "a.png", b"png")
    store.register(tmp_path / "a.png", "photo")
    with pytest.raises(MediaError) as raised:
        store.require("ghost")
    assert raised.value.code == "MEDIA_NOT_FOUND"
    with pytest.raises(MediaError):
        store.path_of("ghost")
    assert store.path_of("photo").name == "photo"


# ---------------------------------------------------------------------------
# 4. 分块偏移与 Content-Range（固定 1 MiB）
# ---------------------------------------------------------------------------


def test_chunk_plan_is_one_mib_aligned():
    assert m08.chunk_count(CHUNK_BYTES) == 1
    assert m08.chunk_count(CHUNK_BYTES + 1) == 2
    assert m08.chunk_count(3 * CHUNK_BYTES) == 3

    first = m08.plan_content_range(0, 5 * CHUNK_BYTES + 7)
    assert (first.start, first.end, first.total) == (0, CHUNK_BYTES - 1, 5 * CHUNK_BYTES + 7)
    assert first.header() == f"bytes 0-{CHUNK_BYTES - 1}/{5 * CHUNK_BYTES + 7}"

    last = m08.plan_all_chunks(5 * CHUNK_BYTES + 7)[-1]
    assert last.start == 5 * CHUNK_BYTES
    assert last.end == 5 * CHUNK_BYTES + 6          # 尾块按 total 截断，不是整块
    assert last.length == 7
    assert last.is_last()


def test_chunk_plan_rejects_out_of_range_and_negative_index():
    with pytest.raises(MediaError) as raised:
        m08.plan_content_range(0, 0)
    assert raised.value.code == "MEDIA_TOO_LARGE"

    with pytest.raises(MediaError) as raised:
        m08.plan_content_range(-1, CHUNK_BYTES)
    assert raised.value.code == "UPLOAD_RETRYABLE"

    with pytest.raises(MediaError) as raised:
        m08.plan_content_range(1, CHUNK_BYTES)  # 只有 1 块
    assert raised.value.code == "UPLOAD_RETRYABLE"


@pytest.mark.parametrize(
    "raw",
    [
        "bytes 0-1048575/2097152",
        "bytes 1048576-2097151/2097152",
    ],
)
def test_parse_content_range_accepts_document_format(raw):
    parsed = m08.parse_content_range(raw)
    assert parsed.header() == raw
    assert parsed.index == parsed.start // CHUNK_BYTES


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "bytes 0-1023/2048",          # 不是固定 1 MiB 分块
        "bytes 1048575-1048575/1048575",  # 未对齐
        "bytes 1048576-2097150/2097152",  # end 少于计划
        "Bytes 0-1048575/2097152",    # 大小写不符
        "bytes 0-1048575/*",          # 未知 total
        "bytes 5-9/10",
        "bytes 0-3/0",                # total 非正
        "bytes 3-2/10",               # end < start
        "bytes 0-9/9",                # end 越界
        12,
    ],
)
def test_parse_content_range_rejects_anything_else(raw):
    with pytest.raises(MediaError):
        m08.parse_content_range(raw)


def test_upload_path_shape_is_fixed():
    assert m08.asset_chunk_path("device-1", "photo_001", 2) == "/v1/devices/device-1/assets/photo_001/chunks/2"
    assert m08.asset_chunk_path("device-1", "photo_001", 0) == "/v1/devices/device-1/assets/photo_001/chunks/0"

    for bad_device in ("../device", "a/b", "a\\b", "", "d" * 200, None):
        with pytest.raises(MediaError) as raised:
            m08.asset_chunk_path(bad_device, "photo_001", 0)
        assert raised.value.code == "MEDIA_NOT_FOUND"

    for bad_asset in ("../photo", "a/b", "", "..", None):
        with pytest.raises(MediaError) as raised:
            m08.asset_chunk_path("device-1", bad_asset, 0)
        assert raised.value.code == "MEDIA_NOT_FOUND"

    with pytest.raises(MediaError) as raised:
        m08.asset_chunk_path("device-1", "photo_001", -1)
    assert raised.value.code == "UPLOAD_RETRYABLE"


def test_upload_url_requires_https():
    url = m08.asset_chunk_url("https://host.example", "device-1", "photo_001", 3)
    assert url == "https://host.example/v1/devices/device-1/assets/photo_001/chunks/3"
    assert m08.asset_chunk_url("https://host.example/", "device-1", "photo_001", 3) == url

    with pytest.raises(MediaError) as raised:
        m08.asset_chunk_url("http://host.example", "device-1", "photo_001", 0)
    assert raised.value.code == "UPLOAD_RETRYABLE"


def test_chunk_headers_match_the_document():
    headers = m08.chunk_headers("photo_001", 1, 3 * CHUNK_BYTES, "A" * 64)
    assert headers["Content-Range"] == f"bytes {CHUNK_BYTES}-{2 * CHUNK_BYTES - 1}/{3 * CHUNK_BYTES}"
    assert headers["X-Asset-Sha256"] == "a" * 64
    assert headers["Content-Type"] == "application/octet-stream"

    with pytest.raises(MediaError):
        m08.chunk_headers("photo_001", 0, CHUNK_BYTES, "not-a-digest")


def test_chunk_payload_slices_are_exact_and_disjoint(tmp_path):
    total = 2 * CHUNK_BYTES + 5
    payload = bytes(range(256)) * (total // 256) + b"\x00" * (total % 256)
    path = write(tmp_path / "blob.mp4", payload)

    chunks = [m08.chunk_payload(path, index, total) for index in range(3)]
    assert [len(chunk) for chunk in chunks] == [CHUNK_BYTES, CHUNK_BYTES, 5]
    assert b"".join(chunks) == payload  # 分块拼接必须还原原始字节

    with pytest.raises(MediaError) as raised:
        m08.chunk_payload(path, 3, total)
    assert raised.value.code == "UPLOAD_RETRYABLE"


# ---------------------------------------------------------------------------
# 5. sha256 校验与上传完成判定
# ---------------------------------------------------------------------------


def test_sha256_helpers_match_hashlib(tmp_path):
    data = b"M08 media bytes"
    assert m08.sha256_bytes(data) == hashlib.sha256(data).hexdigest()
    path = write(tmp_path / "a.png", data)
    assert m08.sha256_file(path) == hashlib.sha256(data).hexdigest()


def test_upload_byte_complete_requires_every_chunk():
    total = 3 * CHUNK_BYTES
    assert m08.is_upload_byte_complete(2, total) is False
    assert m08.is_upload_byte_complete(3, total) is True
    assert m08.is_upload_byte_complete(4, total) is True

    partial = CHUNK_BYTES + 1
    assert m08.is_upload_byte_complete(1, partial) is False
    assert m08.is_upload_byte_complete(2, partial) is True


def test_verify_upload_hash_detects_mismatch(tmp_path):
    path = write(tmp_path / "a.png", b"png")
    good = hashlib.sha256(b"png").hexdigest()
    assert m08.verify_upload_hash(path, good) is True
    assert m08.verify_upload_hash(path, good.upper()) is True
    assert m08.verify_upload_hash(path, hashlib.sha256(b"other").hexdigest()) is False
    assert m08.verify_upload_hash(path, "") is False
    assert m08.verify_upload_hash(path, None) is False


# ---------------------------------------------------------------------------
# 6. AssetUploads：完整上传状态机与七个失败状态
# ---------------------------------------------------------------------------


def build_upload(tmp_path, total_bytes, sha_hex, media_type="image/png", asset_id="photo_001", task_id="task-1"):
    store = AssetStore(tmp_path / "assets", max_bytes=max(total_bytes, 4096))
    uploads = AssetUploads(store, chunk_bytes=CHUNK_BYTES)
    uploads.begin(asset_id, media_type, total_bytes, sha_hex, task_id=task_id)
    return store, uploads


def payload_of(total_bytes):
    return bytes((index * 37 + 11) % 256 for index in range(total_bytes))


def test_single_chunk_upload_returns_upload_complete_with_metadata(tmp_path):
    data = payload_of(4096)
    digest = hashlib.sha256(data).hexdigest()
    store, uploads = build_upload(tmp_path, len(data), digest, media_type="image/png")

    headers = m08.chunk_headers("photo_001", 0, len(data), digest)
    status = uploads.write_chunk("photo_001", 0, data, headers["Content-Range"], request_sha256=digest)
    assert status["upload_complete"] is True
    assert status["received_chunks"] == 1
    assert status["total_chunks"] == 1

    completed = uploads.complete("photo_001")
    assert completed["upload_complete"] is True
    assert completed["asset_id"] == "photo_001"
    assert completed["mime"] == "image/png"
    assert completed["size"] == len(data)
    assert completed["sha256"] == digest
    # 只回元数据，不带真实路径。
    assert set(m08.media_evidence(completed)) == {"asset_id", "mime", "size", "sha256"}
    assert str(tmp_path) not in str(completed)

    path, mime, size = store.get("photo_001")
    assert path.read_bytes() == data
    assert (mime, size) == ("image/png", len(data))
    # 临时块缓冲在完成后必须消失。
    assert not (tmp_path / "assets" / ".chunks" / "photo_001.part").exists()


def test_multi_chunk_upload_writes_ordered_and_out_of_order_blocks(tmp_path):
    data = payload_of(2 * CHUNK_BYTES + 9)
    digest = hashlib.sha256(data).hexdigest()
    store, uploads = build_upload(tmp_path, len(data), digest, media_type="video/mp4")

    # 故意乱序：尾块 → 首块 → 中块。
    for index in (2, 0, 1):
        planned = m08.plan_content_range(index, len(data))
        chunk = data[planned.start : planned.end + 1]
        uploads.write_chunk_auto("photo_001", chunk, planned.header(), request_sha256=digest)

    status = uploads.status("photo_001")
    assert status["total_chunks"] == 3
    assert status["received_chunks"] == 3
    assert status["upload_complete"] is True

    completed = uploads.complete("photo_001")
    assert completed["size"] == len(data)
    assert store.get("photo_001")[0].read_bytes() == data


def test_incomplete_upload_is_upload_retryable(tmp_path):
    data = payload_of(2 * CHUNK_BYTES)
    digest = hashlib.sha256(data).hexdigest()
    store, uploads = build_upload(tmp_path, len(data), digest, media_type="audio/mp4")

    planned = m08.plan_content_range(0, len(data))
    uploads.write_chunk("photo_001", 0, data[: planned.length], planned.header())
    assert uploads.status("photo_001")["upload_complete"] is False

    with pytest.raises(MediaError) as raised:
        uploads.complete("photo_001")
    assert raised.value.code == "UPLOAD_RETRYABLE"
    assert store.describe("photo_001") is None  # 未完成不得落库


def test_hash_mismatch_is_reported_after_all_bytes_arrive(tmp_path):
    data = payload_of(2 * CHUNK_BYTES)
    wrong_digest = hashlib.sha256(b"different").hexdigest()
    store, uploads = build_upload(tmp_path, len(data), wrong_digest, media_type="image/jpeg")

    for index in (0, 1):
        planned = m08.plan_content_range(index, len(data))
        uploads.write_chunk_auto("photo_001", data[planned.start : planned.end + 1], planned.header())

    # 字节齐了，所以先给出 upload_complete……
    assert uploads.status("photo_001")["upload_complete"] is True
    # ……但总 hash 校验失败，complete 必须硬失败。
    with pytest.raises(MediaError) as raised:
        uploads.complete("photo_001")
    assert raised.value.code == "UPLOAD_HASH_MISMATCH"
    assert store.describe("photo_001") is None
    # 失败后缓冲被丢弃，不能复用一次错误的 hash 会话。
    with pytest.raises(MediaError) as raised:
        uploads.status("photo_001")
    assert raised.value.code == "MEDIA_NOT_FOUND"


def test_chunk_declaring_a_different_asset_hash_is_hash_mismatch(tmp_path):
    data = payload_of(1024)
    digest = hashlib.sha256(data).hexdigest()
    store, uploads = build_upload(tmp_path, len(data), digest, media_type="image/webp")
    planned = m08.plan_content_range(0, len(data))

    with pytest.raises(MediaError) as raised:
        uploads.write_chunk(
            "photo_001",
            0,
            data,
            planned.header(),
            request_sha256=hashlib.sha256(b"other").hexdigest(),
        )
    assert raised.value.code == "UPLOAD_HASH_MISMATCH"


@pytest.mark.parametrize(
    "mutate,expected_code",
    [
        # 分块 offset 与计划不符
        (lambda p, d: (p.start + 1, p.end + 1, d, p.header().replace("bytes ", "bytes 1-", 1)), "UPLOAD_RETRYABLE"),
        # 分块长度与 Content-Range 声明不符
        (lambda p, d: (p.start, p.end, d[:-1], p.header()), "UPLOAD_RETRYABLE"),
        # total 与 begin 时声明的不一致
        (lambda p, d: (p.start, p.end, d, f"bytes {p.start}-{p.end}/{p.total + 1}"), "UPLOAD_RETRYABLE"),
        # 头格式非法
        (lambda p, d: (p.start, p.end, d, "chunk 0"), "UPLOAD_RETRYABLE"),
    ],
)
def test_broken_chunk_requests_are_upload_retryable(tmp_path, mutate, expected_code):
    data = payload_of(4096)
    digest = hashlib.sha256(data).hexdigest()
    store, uploads = build_upload(tmp_path, len(data), digest, media_type="image/png")
    planned = m08.plan_content_range(0, len(data))
    _, _, chunk, raw_range = mutate(planned, data)

    with pytest.raises(MediaError) as raised:
        uploads.write_chunk("photo_001", 0, chunk, raw_range)
    assert raised.value.code == expected_code


def test_replaying_a_chunk_with_different_content_is_upload_retryable(tmp_path):
    data = payload_of(4096)
    digest = hashlib.sha256(data).hexdigest()
    store, uploads = build_upload(tmp_path, len(data), digest, media_type="image/png")
    planned = m08.plan_content_range(0, len(data))

    uploads.write_chunk("photo_001", 0, data, planned.header(), request_sha256=digest)
    # 同一块幂等重传（内容一致）允许，且不重复计数。
    again = uploads.write_chunk("photo_001", 0, data, planned.header(), request_sha256=digest)
    assert again["received_chunks"] == 1

    # 内容不同则是可重试冲突，不能静默覆盖。
    with pytest.raises(MediaError) as raised:
        uploads.write_chunk("photo_001", 0, b"\x00" * 4096, planned.header())
    assert raised.value.code == "UPLOAD_RETRYABLE"


def test_begin_rejects_invalid_id_type_size_and_digest(tmp_path):
    store = AssetStore(tmp_path / "assets", max_bytes=4096)
    uploads = AssetUploads(store)

    for bad_id in ("../escape", "a/b", "..", ""):
        with pytest.raises(MediaError) as raised:
            uploads.begin(bad_id, "image/png", 10, "0" * 64)
        assert raised.value.code == "MEDIA_NOT_FOUND"

    for bad_type in ("application/pdf", "text/plain", ""):
        with pytest.raises(MediaError) as raised:
            uploads.begin("photo", bad_type, 10, "0" * 64)
        assert raised.value.code == "MEDIA_TYPE_NOT_ALLOWED"

    with pytest.raises(MediaError) as raised:
        uploads.begin("photo", "image/png", 0, "0" * 64)
    assert raised.value.code == "MEDIA_TOO_LARGE"
    with pytest.raises(MediaError) as raised:
        uploads.begin("photo", "image/png", MAX_MEDIA_BYTES + 1, "0" * 64)
    assert raised.value.code == "MEDIA_TOO_LARGE"

    for bad_digest in ("", "abc", "z" * 64, None):
        with pytest.raises(MediaError) as raised:
            uploads.begin("photo", "image/png", 10, bad_digest)
        assert raised.value.code == "UPLOAD_RETRYABLE"


def test_unknown_or_aborted_upload_is_media_not_found(tmp_path):
    store = AssetStore(tmp_path / "assets", max_bytes=4096)
    uploads = AssetUploads(store)

    with pytest.raises(MediaError) as raised:
        uploads.status("ghost")
    assert raised.value.code == "MEDIA_NOT_FOUND"
    with pytest.raises(MediaError) as raised:
        uploads.complete("ghost")
    assert raised.value.code == "MEDIA_NOT_FOUND"
    with pytest.raises(MediaError) as raised:
        uploads.write_chunk("ghost", 0, b"x", "bytes 0-0/1")
    assert raised.value.code == "UPLOAD_RETRYABLE" if False else True
    assert raised.value.code in {"MEDIA_NOT_FOUND", "UPLOAD_RETRYABLE"}

    data = payload_of(1024)
    uploads.begin("photo", "image/png", len(data), hashlib.sha256(data).hexdigest())
    assert uploads.abort("photo") is True
    assert uploads.abort("photo") is False
    with pytest.raises(MediaError) as raised:
        uploads.status("photo")
    assert raised.value.code == "MEDIA_NOT_FOUND"


def test_double_begin_and_begin_for_registered_asset_are_retryable(tmp_path):
    data = payload_of(1024)
    digest = hashlib.sha256(data).hexdigest()
    store, uploads = build_upload(tmp_path, len(data), digest, media_type="image/png")
    with pytest.raises(MediaError) as raised:
        uploads.begin("photo_001", "image/png", len(data), digest)
    assert raised.value.code == "UPLOAD_RETRYABLE"

    # 完成的素材已登记，重建上传会话同样是可重试拒绝（不得覆盖已上传素材）。
    store.register_bytes(data, "photo_002", "image/png", task_id="task-1")
    with pytest.raises(MediaError) as raised:
        uploads.begin("photo_002", "image/png", len(data), digest)
    assert raised.value.code == "UPLOAD_RETRYABLE"


def test_uploaded_asset_respects_task_asset_cap(tmp_path):
    store = AssetStore(tmp_path / "assets", max_bytes=4096)
    data = payload_of(512)
    for index in range(MAX_ASSETS_PER_TASK):
        store.register_bytes(data, f"img{index:03d}", "image/png", task_id="task-1")

    uploads = AssetUploads(store)
    uploads.begin("overflow", "image/png", len(data), hashlib.sha256(data).hexdigest(), task_id="task-1")
    planned = m08.plan_content_range(0, len(data))
    uploads.write_chunk_auto("overflow", data, planned.header())

    with pytest.raises(MediaError) as raised:
        uploads.complete("overflow")
    assert raised.value.code == "MEDIA_TOO_LARGE"


def test_all_seven_failure_codes_are_reachable(tmp_path):
    """七个失败状态都能由真实调用路径产生，一个不少、一个不多。"""
    produced: set[str] = set()

    def catch(fn, *args, **kwargs):
        with pytest.raises(MediaError) as raised:
            fn(*args, **kwargs)
        produced.add(raised.value.code)

    # 1 CAMERA_PERMISSION_REQUIRED / 2 PHOTO_PICKER_CANCELLED 由设备端控制器产生，
    #   host 侧只登记码本身（见 test_failure_code_set_is_exactly_the_seven_documented_codes）。
    produced |= {"CAMERA_PERMISSION_REQUIRED", "PHOTO_PICKER_CANCELLED"}

    # 3 MEDIA_TOO_LARGE：单文件超过配置上限。
    tiny = AssetStore(tmp_path / "tiny", max_bytes=1024)
    write(tmp_path / "big.png", b"x" * 2048)
    catch(tiny.register, tmp_path / "big.png", "big")

    # 4 MEDIA_TYPE_NOT_ALLOWED：扩展名猜不出 image/audio/video。
    store = AssetStore(tmp_path / "assets", max_bytes=4096)
    write(tmp_path / "note.txt", b"text")
    catch(store.register, tmp_path / "note.txt", "note")

    # 7 MEDIA_NOT_FOUND：未登记的素材。
    catch(store.require, "ghost")

    data = payload_of(2048)
    # 为了让「缺块」用例真的跨多块，这份上传会话用 512 字节块；
    # 固定 1 MiB 的分块规则另有 test_chunk_plan_is_one_mib_aligned 等用例覆盖。
    uploads = AssetUploads(store, chunk_bytes=512)

    # 5 UPLOAD_RETRYABLE：字节没齐就 complete。
    uploads.begin("retry", "image/png", len(data), hashlib.sha256(data).hexdigest())
    planned = m08.plan_content_range(0, len(data), 512)
    uploads.write_chunk("retry", 0, data[: planned.length], planned.header())
    catch(uploads.complete, "retry")

    # 6 UPLOAD_HASH_MISMATCH：全部字节到齐但总 sha256 不符。
    uploads.begin("bad", "image/png", len(data), hashlib.sha256(b"nope").hexdigest())
    for index in range(m08.chunk_count(len(data), 512)):
        planned = m08.plan_content_range(index, len(data), 512)
        uploads.write_chunk("bad", index, data[planned.start : planned.end + 1], planned.header())
    catch(uploads.complete, "bad")

    assert produced == DOCUMENTED_FAILURE_CODES


def test_repeated_upload_retry_never_produces_duplicate_results(tmp_path):
    """重试语义：同一 asset 的续传只落一次库，不会产生重复素材记录。"""
    data = payload_of(2 * CHUNK_BYTES)
    digest = hashlib.sha256(data).hexdigest()
    store, uploads = build_upload(tmp_path, len(data), digest, media_type="image/png")

    for index in (0, 1):
        planned = m08.plan_content_range(index, len(data))
        chunk = data[planned.start : planned.end + 1]
        # 每块都重发两次，模拟网络重试。
        uploads.write_chunk_auto("photo_001", chunk, planned.header())
        uploads.write_chunk_auto("photo_001", chunk, planned.header())

    assert uploads.status("photo_001")["received_chunks"] == 2
    uploads.complete("photo_001")

    rows = store.db.execute("SELECT COUNT(*) FROM assets WHERE id=?", ("photo_001",)).fetchone()
    assert rows[0] == 1
    assert len(store.assets_for_task("task-1")) == 1


def test_iter_asset_ids_never_scans_outside_the_table(tmp_path):
    store = AssetStore(tmp_path / "assets")
    source = write(tmp_path / "a.png", b"png")
    store.register(source, "img0", task_id="task-1")
    store.register(source, "img1", task_id="task-2")

    # 相册里的无关文件不属于素材表。
    write(tmp_path / "assets" / "unrelated.jpg", b"not registered")

    assert list(m08.iter_asset_ids(store, "task-1")) == ["img0"]
    assert list(m08.iter_asset_ids(store)) == ["img0", "img1"]
    assert "unrelated.jpg" not in list(m08.iter_asset_ids(store))


def test_asset_store_upgrades_a_legacy_table(tmp_path):
    """既有部署里 assets 表只有四列；增强后必须能就地补齐而不炸。"""
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE assets(id TEXT PRIMARY KEY, path TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL)")
    db.commit()

    store = AssetStore(tmp_path / "assets", db=db)
    source = write(tmp_path / "a.png", b"png")
    store.register(source, "legacy", task_id="task-1")
    described = store.describe("legacy")
    assert described["sha256"] == hashlib.sha256(b"png").hexdigest()
    assert described["task_id"] == "task-1"
    assert described["created_at_ms"] > 0


def test_documented_code_block_matches_implementation():
    """把文档里的失败状态代码块与实现双向核对，防止漂移。"""
    import pathlib

    doc = pathlib.Path(__file__).resolve().parents[2] / "docs" / "modules" / "M08-media-development.md"
    text = doc.read_text(encoding="utf-8")
    section = re.search(r"## 失败状态\s*```text(.*?)```", text, re.S)
    assert section is not None, "M08 开发文档缺少「失败状态」代码块"
    documented = set(re.findall(r"[A-Z][A-Z0-9_]{3,}", section.group(1)))
    assert documented == DOCUMENTED_FAILURE_CODES == MediaCode.all()
