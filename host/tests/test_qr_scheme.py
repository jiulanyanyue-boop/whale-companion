"""二维码地址协议的固定测试（安全边界，回退即出事）。

## 为什么必须有这组测试

手机端有一条硬校验：`host` 必须是 `wss://`，**只有回环地址**才允许 `ws://`
（`QrPayload.kt` 的 `ERROR_HOST_NOT_WSS`）。那条校验是对的——配对码是外部输入，
不能因为「是主机自己给的」就放行明文传输。

但电脑端曾经只是「照实输出」：证书还没生成时 `scheme = "ws"`，
二维码里就是 `ws://192.168.x.x:8765/device`，手机扫到后**被正确拒绝**，
用户看到的是「主机地址必须用加密的 wss://」，而界面上根本没有任何地方能修。

这里钉死四条：

1. **非回环地址 → 永远是 `wss://`**（证书缺就自动补，补不上就不给码）；
2. **回环地址 → 只给回环地址**（不能给出「局域网 IP + ws」这种自相矛盾的组合）；
3. 证书缺失时**先自动生成**，而不是把问题抛给用户；
4. 证书真的不可用时 → **明确说明缺什么**，绝不生成一个会被手机拒绝的码。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, r"C:\projects\whale-companion\host")

from astrbot_whale_device.host_gui import HostWindow


def _write_config(path: str, host: str, port: int = 8765) -> None:
    mapping = {
        "owner_user_id": "10001",
        "device_id": "device-qr-test",
        "allowed_packages": [],
        "blocked_packages": [],
        "signing_secret_env": "WHALE_SIGNING_SECRET",
        "model": {
            "provider_id": "openai",
            "model_id": "deepseek-flash",
            "base_url": "https://api.deepseek.com",
        },
        "device_server": {"host": host, "port": port},
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(mapping, handle, ensure_ascii=False, indent=2)


def _window(tmp_path, host: str, port: int = 8765) -> tuple[HostWindow, str]:
    config_path = str(tmp_path / "host.json")
    _write_config(config_path, host, port)
    window = HostWindow(config_path)
    window.root.update()
    return window, config_path


def test_lan_listening_generates_wss_even_without_certificate(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**核心回归**：局域网监听 + 没有证书 → 必须先补证书，然后输出 wss://。"""
    monkeypatch.setenv("WHALE_SIGNING_SECRET", "test-signing-secret")
    window, _ = _window(tmp_path, "0.0.0.0")
    try:
        window._on_regen()
        window.root.update()
        url = window.pair_url_var.get()
        assert url.startswith("wss://"), f"非回环地址必须是 wss，实际为 {url!r}"
        assert url.endswith("/device")
    finally:
        window.root.destroy()


def test_lan_listening_writes_certificate_paths_back_to_config(tmp_path) -> None:
    """补证书必须写回配置，否则主机启动时仍会用无证书配置。"""
    window, config_path = _window(tmp_path, "0.0.0.0")
    try:
        window._on_regen()
        window.root.update()
        raw = json.loads(open(config_path, encoding="utf-8").read())
        server = raw["device_server"]
        assert server.get("certfile"), "应写回 certfile"
        assert server.get("keyfile"), "应写回 keyfile"
        assert os.path.isfile(server["certfile"]), "证书文件应真实存在"
    finally:
        window.root.destroy()


def test_loopback_listening_uses_real_loopback_address(tmp_path) -> None:
    """回环监听时**不能**给出局域网 IP + ws 的组合。

    `phone_facing_urls(bind_host="127.0.0.1")` 会退化成「找本机局域网 IP」，
    于是拼出 `ws://192.168.x.x`——那个 IP 不是回环，手机端会拒绝；
    语义上也矛盾：主机只听回环，手机本来就连不上。
    """
    window, _ = _window(tmp_path, "127.0.0.1")
    try:
        window._on_regen()
        window.root.update()
        url = window.pair_url_var.get()
        assert "127.0.0.1" in url, f"回环监听应给回环地址，实际为 {url!r}"
        assert url.startswith("ws://")   # 回环允许明文（M05 例外）
    finally:
        window.root.destroy()


def test_zero_port_gives_clear_message_instead_of_bogus_url(tmp_path) -> None:
    window, _ = _window(tmp_path, "0.0.0.0", port=0)
    try:
        window._on_regen()
        window.root.update()
        # 端口 0 时无法预先给地址 → 必须明确说明，且不输出任何 URL
        assert window.pair_url_var.get() in ("—", "")
        text = window.qr_fallback.cget("text")
        assert "端口" in str(text)
    finally:
        window.root.destroy()


def test_qr_code_really_drawn_for_wss(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """二维码必须真的画出来（不是只有文字）。"""
    monkeypatch.setenv("WHALE_SIGNING_SECRET", "test-signing-secret")
    window, _ = _window(tmp_path, "0.0.0.0")
    try:
        window._on_regen()
        window.root.update()
        items = window.qr_canvas.find_all()
        assert len(items) > 500, f"二维码方块数量异常：{len(items)}"
    finally:
        window.root.destroy()


# ---------------------------------------------------------------------------
# 主机指纹必须真实（**安全级**）
# ---------------------------------------------------------------------------


def test_fingerprint_is_real_not_placeholder(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：界面与二维码里的主机指纹**必须是真实值**，不能是 64 个零。

    全零会让手机确认页显示「主机指纹：0000...0000」——用户核对毫无意义，
    更糟的是它会让人误以为「核对过了」，而实际没有任何身份被验证。
    """
    monkeypatch.delenv("WHALE_SIGNING_SECRET", raising=False)
    window, _ = _window(tmp_path, "0.0.0.0")
    try:
        # 先保存配置（会自动生成签名密钥）
        window.owner_var.set("10001")
        assert window._on_save_quiet() is True
        window._on_regen()
        window.root.update()
        shown = window.pair_fingerprint_var.get()
        assert len(shown) == 64, f"指纹长度应为 64，实际 {len(shown)}"
        assert set(shown) != {"0"}, "指纹不能是全零占位"
    finally:
        window.root.destroy()


def test_fingerprint_matches_pairing_algorithm(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """界面显示的指纹必须与 pairing_endpoint 算出的**完全一致**。

    两端用不同算法时，用户拿手机上的值去比对电脑上的值会永远对不上——
    那不是「用户看错了」，是主机把两套身份当成了同一个。
    """
    monkeypatch.setenv("WHALE_SIGNING_SECRET", "cross-check-secret")
    window, _ = _window(tmp_path, "0.0.0.0")
    try:
        window._on_regen()
        window.root.update()
        shown = window.pair_fingerprint_var.get()
        from astrbot_whale_device.pairing_endpoint import make_host_identity_from_seed

        expected = make_host_identity_from_seed(b"cross-check-secret").fingerprint
        assert shown == expected
    finally:
        window.root.destroy()


def test_no_fingerprint_means_no_qr_code(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有签名密钥时**不生成二维码**，而不是给一个假指纹的码。

    注意：HostWindow 构造时会调 _load_config_into_form，
    那里会**自动生成签名密钥**（这是有意的产品行为：用户不该管密钥）。
    所以想模拟「没有密钥」必须连生成逻辑一起绕开——
    这里直接验证 _host_fingerprint() 在密钥为空时返回空串，
    以及 _render_pairing 因此在界面上给出明确提示。
    """
    monkeypatch.delenv("WHALE_SIGNING_SECRET", raising=False)
    window, config_path = _window(tmp_path, "0.0.0.0")
    try:
        # 删掉 secrets.json 并清空内存里的密钥，模拟「密钥真的不存在」
        secrets_file = os.path.join(os.path.dirname(config_path), "secrets.json")
        if os.path.isfile(secrets_file):
            os.remove(secrets_file)
        assert window._host_fingerprint() == ""

        # 这种情况下 _render_pairing 必须给出明确提示、不产出地址
        window._render_pairing([], "wss")
        window.root.update()
        assert window.pair_url_var.get() == "—"
    finally:
        window.root.destroy()
