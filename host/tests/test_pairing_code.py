"""主机侧配对码与二维码的固定测试。

覆盖三件事：

1. **配对码形状**：长度、字符集、无易混淆字符——手抄不会看错。
2. **二维码文本**：格式与设备端 `QrPayload.kt` 一致，且**参数可被正确解析**
   （这是最容易做错的地方：把完整 `wss://` 地址整体 URL-encode 之后，
   设备端 `URI(host)` 会解析失败，扫码等于白扫）。
3. **降级不崩**：`qrcode` 是可选库，系统 Python 上通常没有；
   缺了必须只显示配对码，**绝不能让主机启动失败**。
"""

from __future__ import annotations

import sys
import urllib.parse

import pytest

from astrbot_whale_device.pairing_code import (
    CODE_ALPHABET,
    CODE_LENGTH,
    build_presentation,
    format_pairing_banner,
    make_pairing_code,
    qr_payload,
    render_terminal_qr,
)

#: 手抄时最容易看错的字符，配对码里**必须**一个都没有。
CONFUSABLE = set("0O1ILSBQ2Z"[:0]) | {"0", "O", "1", "I", "L", "S"}


class _FakeRecord:
    """模拟 `pairing.py` 的 `PairingTokenRecord`（只用到三个字段）。"""

    def __init__(self, token: str, code: str, expires: int) -> None:
        self.token = token
        self.pairing_code = code
        self.expires_at_ms = expires


# ---------------------------------------------------------------------------
# 配对码形状
# ---------------------------------------------------------------------------


def test_pairing_code_has_documented_length() -> None:
    assert len(make_pairing_code()) == CODE_LENGTH


def test_pairing_code_only_uses_the_documented_alphabet() -> None:
    for _ in range(50):
        code = make_pairing_code()
        assert all(ch in CODE_ALPHABET for ch in code), code


def test_pairing_code_never_contains_confusable_characters() -> None:
    """手抄最怕 `0`/`O`/`1`/`I`——配对码里绝不能出现。"""
    for _ in range(200):
        code = make_pairing_code()
        assert not (set(code) & CONFUSABLE), code


def test_pairing_codes_are_not_all_identical() -> None:
    """同机连续生成不能重复（虽然理论上可能，但 200 次全同说明种子坏了）。"""
    codes = {make_pairing_code() for _ in range(200)}
    assert len(codes) >= 190


def test_pairing_code_length_is_bounded() -> None:
    with pytest.raises(ValueError):
        make_pairing_code(length=2)
    with pytest.raises(ValueError):
        make_pairing_code(length=64)


# ---------------------------------------------------------------------------
# 二维码文本
# ---------------------------------------------------------------------------


def test_qr_payload_uses_documented_format() -> None:
    payload = qr_payload(
        host_url="wss://192.168.1.10:8765/device",
        token="tok-abc_123",
        host_fingerprint="a" * 64,
    )
    assert payload.startswith("whale://pair?")


def test_qr_payload_host_is_parseable_by_uri_rules() -> None:
    """核心断言：`host` 参数必须是**完整的可解析 URL**。

    `urllib.parse` 的规则与 Android `URI` 接近。
    """
    payload = qr_payload(
        host_url="wss://192.168.1.10:8765/device",
        token="tok",
        host_fingerprint="a" * 64,
    )
    parsed = urllib.parse.urlparse(payload)
    assert parsed.scheme == "whale"
    assert parsed.netloc == "pair"

    params = urllib.parse.parse_qs(parsed.query)
    assert params["host"] == ["wss://192.168.1.10:8765/device"]
    assert params["token"] == ["tok"]
    assert params["fp"] == ["a" * 64]


def test_qr_payload_never_percent_encodes_the_host_scheme() -> None:
    """回归：早先版本把 host 整体 encode 成 `wss%3A%2F%2F...`，扫码后填不进去。"""
    payload = qr_payload(
        host_url="wss://10.0.0.5:8765/device",
        token="t",
        host_fingerprint="b" * 64,
    )
    assert "wss%3A%2F%2F" not in payload
    assert "host=wss://" in payload


def test_qr_payload_rejects_blank_host() -> None:
    with pytest.raises(ValueError):
        qr_payload(host_url="", token="t", host_fingerprint="a" * 64)


def test_qr_payload_rejects_blank_token() -> None:
    with pytest.raises(ValueError):
        qr_payload(host_url="wss://h:1/p", token="", host_fingerprint="a" * 64)


def test_qr_payload_rejects_non_websocket_scheme() -> None:
    with pytest.raises(ValueError):
        qr_payload(host_url="http://h:1/p", token="t", host_fingerprint="a" * 64)


def test_qr_payload_special_characters_survive_round_trip() -> None:
    """token 里可能含 `-` `_`；它们在 query 里必须不被破坏。"""
    token = "abc-DEF_ghi-jkl"
    payload = qr_payload(
        host_url="wss://h:1/p",
        token=token,
        host_fingerprint="a" * 64,
    )
    params = urllib.parse.parse_qs(urllib.parse.urlparse(payload).query)
    assert params["token"] == [token]


# ---------------------------------------------------------------------------
# 展示结构
# ---------------------------------------------------------------------------


def test_build_presentation_uses_first_phone_url() -> None:
    record = _FakeRecord("tok", "ABCD2345", 1_700_000_000_000)
    presentation = build_presentation(
        record=record,
        host_fingerprint="f" * 64,
        phone_urls=["wss://192.168.1.10:8765/device", "wss://10.0.0.5:8765/device"],
    )
    assert presentation.pairing_code == "ABCD2345"
    assert presentation.phone_urls[0] == "wss://192.168.1.10:8765/device"
    assert "wss://192.168.1.10:8765/device" in presentation.qr_payload


def test_build_presentation_rejects_when_no_address() -> None:
    """没有任何可连接地址时必须报错，而不是给一个连不上的二维码。"""
    with pytest.raises(ValueError):
        build_presentation(
            record=_FakeRecord("tok", "ABCD2345", 1),
            host_fingerprint="f" * 64,
            phone_urls=[],
        )


def test_loggable_never_contains_token() -> None:
    """token 是配对凭据，**绝不能**进任何可打印摘要。"""
    record = _FakeRecord("super-secret-token", "ABCD2345", 1_700_000_000_000)
    presentation = build_presentation(
        record=record,
        host_fingerprint="f" * 64,
        phone_urls=["wss://h:1/p"],
    )
    rendered = repr(presentation.loggable())
    assert "super-secret-token" not in rendered
    assert "ABCD2345" in rendered  # 配对码可以显示


# ---------------------------------------------------------------------------
# 终端渲染与降级
# ---------------------------------------------------------------------------


def test_render_returns_none_when_qrcode_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """`qrcode` 缺失（系统 Python 的常态）必须返回 None，**不抛异常**。"""
    monkeypatch.setitem(sys.modules, "qrcode", None)
    assert render_terminal_qr("whale://pair?host=wss://h:1/p") is None


def test_banner_degrades_gracefully_without_qrcode() -> None:
    record = _FakeRecord("tok", "ABCD2345", 1_700_000_000_000)
    presentation = build_presentation(
        record=record,
        host_fingerprint="f" * 64,
        phone_urls=["wss://192.168.1.10:8765/device"],
    )
    banner = format_pairing_banner(presentation, qr_text=None, ttl_seconds=600)

    # 降级时必须明确说明「没装 qrcode，请手输」，并给出配对码与地址。
    assert "没装 qrcode" in banner
    assert "ABCD2345" in banner
    assert "wss://192.168.1.10:8765/device" in banner


def test_banner_with_qrcode_includes_both_qr_and_code() -> None:
    record = _FakeRecord("tok", "ABCD2345", 1_700_000_000_000)
    presentation = build_presentation(
        record=record,
        host_fingerprint="f" * 64,
        phone_urls=["wss://h:1/p"],
    )
    banner = format_pairing_banner(presentation, qr_text="[]\n[]\n", ttl_seconds=600)
    assert "[]" in banner
    assert "ABCD2345" in banner


def test_banner_prints_token_because_manual_entry_requires_it() -> None:
    """手输路径**必须**给出 token。

    这里曾经断言「横幅不出现 token」，但那是个错误决定：
    设备端 `PairingController.submit_code(token, pairing_code)` 要逐字比对
    **两个值**，只给 8 位配对码的话，用户照抄必然对不上（手输框要的是 token）。

    安全性并没有因此变差：token 本来就在二维码里（给扫描器读），
    横幅只是同一份凭据的另一种呈现方式，两者都在**一次性、10 分钟 TTL** 之内。
    真正不能出现 token 的地方是 `loggable()`（会落审计/日志）。
    """
    record = _FakeRecord("manual-entry-token-value", "ABCD2345", 1_700_000_000_000)
    presentation = build_presentation(
        record=record,
        host_fingerprint="f" * 64,
        phone_urls=["wss://h:1/p"],
    )
    banner = format_pairing_banner(presentation, qr_text=None, ttl_seconds=600)
    assert "manual-entry-token-value" in banner
    assert "ABCD2345" in banner


def test_loggable_still_hides_the_token() -> None:
    """与横幅相反：`loggable()` 会进审计，**绝不能**带 token。"""
    record = _FakeRecord("must-not-be-logged", "ABCD2345", 1_700_000_000_000)
    presentation = build_presentation(
        record=record,
        host_fingerprint="f" * 64,
        phone_urls=["wss://h:1/p"],
    )
    rendered = repr(presentation.loggable())
    assert "must-not-be-logged" not in rendered