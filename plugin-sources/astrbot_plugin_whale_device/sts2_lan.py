"""STS2 LAN 实验模式的发布包安装、配置与好友加入说明。"""

from __future__ import annotations

import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

LAN_VERSION = "v0.11.0"
LAN_URL = "https://github.com/Madlezz/LanSpire/releases/download/v0.11.0/LanSpire_v0.11.0.zip"
LAN_SHA256 = "dd126cd803a738a202132a2deba9fe31ce5e027690ebd1095b2de333574e70c2"
LAN_ASSETS = ("LanSpire.dll", "LanSpire.json")
LAN_FILES = (*LAN_ASSETS, "LICENSE")
LAN_PORT = 33771
MAX_PACKAGE_BYTES = 8 * 1024 * 1024
LAN_LICENSE = """MIT License

Copyright (c) 2026 Madlezz

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


class STS2LanError(RuntimeError):
    """表示 LAN Mod 安装或共享配置不能安全使用。"""


def validate_share_address(value: Any) -> str:
    """接受可分享的 IPv4 或 DNS 域名，不解析 DNS，不接受 URL、端口和 IPv6。"""
    if not isinstance(value, str) or len(value) > 253:
        raise ValueError("分享地址需为 IPv4 或域名，不包含协议和端口")
    value = value.strip()
    if not value:
        return ""
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        # 拒绝歧义数字地址、单标签主机名及可改变配置语义的特殊字符。
        labels = value.lower().split(".")
        if (
            len(labels) < 2 or not re.search(r"[a-z]", labels[-1])
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)
            or value.lower().endswith((".localhost", ".local", ".internal"))
        ):
            raise ValueError("请填写有效 IPv4 或 ASCII 域名，不包含协议、路径或端口")
        return value.lower()
    if (
        address.is_loopback or address.is_unspecified or int(address) < 16777216
        or address.is_multicast or address.is_link_local or address.is_reserved
    ):
        raise ValueError("不能分享回环、链路本地、组播或保留 IPv4")
    return str(address)


def validate_public_address(value: Any) -> str:
    """公网入口拒绝明确的内网/CGNAT IPv4；域名可保存但不承诺解析或可达。"""
    value = validate_share_address(value)
    if not value:
        return value
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        return value
    if not address.is_global:
        raise ValueError("公网入口不能填写内网或 CGNAT IPv4，请填写实际公网 IPv4 或域名")
    return value


def validate_game_port(value: Any) -> int:
    """外部游戏端口独立于本机监听端口，禁止误转发默认 MCP 端口。"""
    if type(value) is not int or not 1024 <= value <= 65535 or value == 15526:
        raise ValueError("游戏外部端口需为 1024 至 65535 的整数，且不能使用 MCP 15526")
    return value


class STS2LanManager:
    """管理受管 LanSpire 文件，不开放 MCP API、不自动改变系统网络。"""

    def __init__(self, data_dir: Path):
        """在已有插件数据目录内保存安装记录和原始配置备份。"""
        self.data_dir = data_dir
        self.record_path = data_dir / "sts2-lan-install.json"
        self.backup_path = data_dir / "lan-config-before-whale.json"

    def config_path(self) -> Path:
        """使用上游 Windows 默认 user:// 配置路径，不伪造实例隔离。"""
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise STS2LanError("lan_appdata_unavailable")
        return Path(appdata) / "SlayTheSpire2" / "LanSpire" / "lan_config.json"

    def _config(self) -> dict[str, Any]:
        """严格读取共享配置，损坏时拒绝覆盖而不是创建空配置。"""
        path = self.config_path()
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise STS2LanError("lan_config_invalid") from exc
        if not isinstance(value, dict):
            raise STS2LanError("lan_config_invalid")
        return value

    def _record(self) -> dict[str, Any]:
        """记录缺失或损坏时不推断第三方文件属于 Whale。"""
        try:
            value = json.loads(self.record_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _bool_setting(self, config: dict[str, Any], key: str, default: bool) -> bool:
        """兼容上游设置页写出的布尔字符串；未知类型拒绝按安全配置使用。"""
        value = config.get(key, default)
        if type(value) is bool:
            return value
        if type(value) is int:
            return value != 0
        if isinstance(value, str) and value.casefold() in {"true", "false"}:
            return value.casefold() == "true"
        raise STS2LanError("lan_config_invalid")

    def _targets(self, game_dir: Path) -> dict[str, Path]:
        """只允许 mods/LanSpire 内固定文件，拒绝重解析目录和文件。"""
        mods = (game_dir / "mods").resolve()
        directory = mods / "LanSpire"
        if directory.resolve().parent != mods or directory.is_symlink():
            raise STS2LanError("lan_install_path_unsafe")
        targets = {name: directory / name for name in LAN_FILES}
        if any(path.is_symlink() or path.resolve().parent != directory.resolve() for path in targets.values()):
            raise STS2LanError("lan_install_path_unsafe")
        return targets

    def _assert_owned(self, game_dir: Path, targets: dict[str, Path]) -> None:
        """安装和卸载前验证所有权与旧摘要，不覆盖或删除用户替换的文件。"""
        record = self._record()
        if record.get("game_dir") and record["game_dir"] != str(game_dir):
            raise STS2LanError("uninstall_previous_lan_directory_first")
        hashes = record.get("sha256", {}) if record.get("game_dir") == str(game_dir) else {}
        if not isinstance(hashes, dict):
            hashes = {}
        for name, path in targets.items():
            if path.exists() and (
                not path.is_file() or name not in hashes
                or not hmac.compare_digest(hashlib.sha256(path.read_bytes()).hexdigest(), str(hashes[name]))
            ):
                raise STS2LanError("lan_unmanaged_or_modified_file")

    def _package(self) -> dict[str, bytes]:
        """下载固定发布包并核对 SHA-256，只读取两个白名单成员而不解压路径。"""
        try:
            request = urllib.request.Request(LAN_URL, headers={"User-Agent": "WhaleCompanion"})
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read(MAX_PACKAGE_BYTES + 1)
        except (OSError, TimeoutError) as exc:
            raise STS2LanError("lan_download_failed") from exc
        if len(data) > MAX_PACKAGE_BYTES or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), LAN_SHA256):
            raise STS2LanError("lan_package_checksum_mismatch")
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                result = {}
                for name in LAN_ASSETS:
                    info = archive.getinfo("LanSpire/" + name)
                    if info.file_size > MAX_PACKAGE_BYTES:
                        raise STS2LanError("lan_package_invalid")
                    result[name] = archive.read(info)
                manifest = json.loads(result["LanSpire.json"].decode("utf-8-sig"))
                if manifest.get("id") != "LanSpire" or manifest.get("version") != LAN_VERSION:
                    raise STS2LanError("lan_package_invalid")
                result["LICENSE"] = LAN_LICENSE.encode("utf-8")
                return result
        except (ValueError, KeyError, zipfile.BadZipFile) as exc:
            raise STS2LanError("lan_package_invalid") from exc

    def _write_atomic(self, path: Path, data: bytes) -> None:
        """在目标同目录原子替换文件，失败时清除自己创建的临时文件。"""
        import tempfile

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".whale-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def install(self, game_dir: Path) -> dict[str, Any]:
        """安装固定 LAN 发布文件和三人配置；不启动游戏、不触碰游戏存档。"""
        mods = game_dir / "mods"
        conflicts = ("STS2LanPc", "SlayTheSpire2.LAN.Multiplayer", "SlayTheSpire2.LAN.Multiplayer.Reforged")
        if any((mods / name).exists() for name in conflicts):
            raise STS2LanError("lan_other_network_mod_present")
        config = self._config()
        if self._bool_setting(config, "lan_use_custom_player_id", False):
            raise STS2LanError("lan_shared_custom_identity_unsupported")
        targets = self._targets(game_dir)
        self._assert_owned(game_dir, targets)
        downloaded = self._package()
        config.update(lan_multiplayer_enabled=True, lan_use_custom_player_id=False,
                      lan_join_host="127.0.0.1", lan_join_port=LAN_PORT,
                      max_multiplayer_players=4, max_multiplayer_enabled=True)
        config_path = self.config_path()
        changes = {targets[name]: data for name, data in downloaded.items()}
        changes[config_path] = json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8")
        changes[self.record_path] = json.dumps({
            "game_dir": str(game_dir), "release": LAN_VERSION,
            "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in downloaded.items()},
        }, indent=2).encode("utf-8")
        previous = {path: path.read_bytes() if path.exists() else None for path in changes}
        config_backup = previous[config_path]
        if config_backup is not None and not self.backup_path.exists():
            self._write_atomic(self.backup_path, config_backup)
        written = []
        try:
            for path, data in changes.items():
                self._write_atomic(path, data)
                written.append(path)
        except OSError as exc:
            rollback_failed = False
            for path in reversed(written):
                try:
                    old_data = previous[path]
                    if old_data is None:
                        path.unlink(missing_ok=True)
                    else:
                        self._write_atomic(path, old_data)
                except OSError:
                    rollback_failed = True
            raise STS2LanError("lan_install_rollback_failed" if rollback_failed else "lan_install_failed") from exc
        return {"installed": True, "release": LAN_VERSION}

    def assert_ready(self, game_dir: Path) -> None:
        """启动前检查当前目录的受管 Mod 和共享身份配置，拒绝未完成的安装。"""
        targets = self._targets(game_dir)
        if self._record().get("game_dir") != str(game_dir) or not all(p.is_file() for p in targets.values()):
            raise STS2LanError("install_lan_before_launch")
        self._assert_owned(game_dir, targets)
        config = self._config()
        if self._bool_setting(config, "lan_use_custom_player_id", False):
            raise STS2LanError("lan_shared_custom_identity_unsupported")
        if (
            not self._bool_setting(config, "lan_multiplayer_enabled", False)
            or str(config.get("max_multiplayer_players")) != "4"
            or not self._bool_setting(config, "max_multiplayer_enabled", False)
        ):
            raise STS2LanError("lan_config_not_ready")

    def uninstall(self, game_dir: Path) -> dict[str, Any]:
        """只删除摘要匹配的受管 DLL/manifest，保留游戏配置及原始备份。"""
        if self._record().get("game_dir") != str(game_dir):
            raise STS2LanError("no_managed_lan_installation")
        targets = self._targets(game_dir)
        self._assert_owned(game_dir, targets)
        for path in targets.values():
            path.unlink(missing_ok=True)
        self.record_path.unlink(missing_ok=True)
        return {"installed": False, "config_preserved": True}

    def status(self, game_dir: Path | None) -> dict[str, Any]:
        """返回非敏感 LAN 状态，不返回配置中的房间口令。"""
        ready, error = False, ""
        if game_dir is not None:
            try:
                self.assert_ready(game_dir)
                ready = True
            except (STS2LanError, OSError) as exc:
                error = str(exc)
        record = self._record()
        return {
            "ready": ready, "error": error, "version": LAN_VERSION,
            "managed_install": game_dir is not None and record.get("game_dir") == str(game_dir),
            "port": LAN_PORT, "max_players": 4,
            "release_url": "https://github.com/Madlezz/LanSpire/releases/tag/" + LAN_VERSION,
            "config_backup": str(self.backup_path) if self.backup_path.exists() else "",
        }

    def addresses(self) -> list[dict[str, str]]:
        """只枚举本机网卡 IPv4，不探测外网、不推断 VPN 路由可达性。"""
        import socket

        try:
            import psutil
            rows = []
            for interface, addresses in psutil.net_if_addrs().items():
                for address in addresses:
                    if address.family != socket.AF_INET:
                        continue
                    try:
                        value = validate_share_address(address.address)
                    except ValueError:
                        continue
                    rows.append({"interface": interface, "address": value})
            return rows
        except (ImportError, OSError):
            return []
