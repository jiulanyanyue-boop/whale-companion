"""STS2 专用 frp UDP 中转：固定资产、私有部署包与显式进程管理。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import subprocess
import tempfile
import threading
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from .sts2_lan import LAN_PORT, validate_game_port, validate_public_address
except ImportError:
    from sts2_lan import LAN_PORT, validate_game_port, validate_public_address

FRP_VERSION = "0.72.0"
FRP_BASE = "https://github.com/fatedier/frp/releases/download/v" + FRP_VERSION + "/"
FRP_WINDOWS = "frp_0.72.0_windows_amd64"
FRP_WINDOWS_SHA256 = "5197c1b69564089cdc229fc1260a0e25db1e722b1b7db45fe1542c3c43edf9ae"
FRP_LINUX_SHA256 = "daf719b32ca2778770bb92955e231a07414d754378b9af328aa2642862479804"
MAX_PACKAGE_BYTES = 48 * 1024 * 1024


class STS2RelayError(RuntimeError):
    """表示中转未配置、资产不匹配或受管进程不能安全操作。"""


def relay_settings(config: dict[str, Any]) -> dict[str, Any]:
    """从普通设置提取非敏感服务器信息，所有本机目标均由代码固定。"""
    host = validate_public_address(config.get("sts2_relay_host", ""))
    control = config.get("sts2_relay_control_port", 7000)
    remote = validate_game_port(config.get("sts2_public_port", LAN_PORT))
    if type(control) is not int or not 1024 <= control <= 65535 or control == 15526:
        raise ValueError("中转控制端口需为 1024 至 65535 的整数，且不能使用 MCP 15526")
    if config.get("sts2_api_port", 15526) in (control, remote):
        raise ValueError("中转控制端口和外部游戏端口不能使用当前 MCP API 端口")
    return {"host": host, "control_port": control, "remote_port": remote}


class STS2RelayManager:
    """只创建单个游戏 UDP 代理，不开放通用隧道、远程执行或管理面板。"""

    def __init__(self, data_dir: Path, config: dict[str, Any]):
        """凭据与生成物全部放入已忽略的插件运行数据，不写入配置 schema。"""
        self.root = data_dir / "relay"
        self.data_dir = data_dir
        self.config = config
        self.record_path = self.root / "deployment.json"
        self.process_path = self.root / "process.json"
        self.install_path = self.root / "installation.json"
        self.exe = self.root / "bin" / "frpc.exe"
        self._lock = threading.RLock()
        self._child: subprocess.Popen | None = None

    def _read(self, path: Path) -> dict[str, Any]:
        """读取私有记录，损坏记录不作为操作其他进程或文件的依据。"""
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (ValueError, OSError):
            return {}

    def _write(self, path: Path, data: bytes) -> None:
        """原子写入自己的文件；POSIX 使用私有权限，Windows 沿用用户数据目录 ACL。"""
        if (
            self.root.is_symlink() or self.root.resolve().parent != self.data_dir.resolve()
            or not path.resolve().is_relative_to(self.root.resolve()) or path.is_symlink()
        ):
            raise STS2RelayError("relay_files_modified")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".whale-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(data)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _json(self, path: Path, value: dict[str, Any]) -> None:
        """持久化私有元数据，不将 token 放进命令行或状态返回值。"""
        self._write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))

    def _deployment(self) -> tuple[dict[str, Any], Path]:
        """只允许读取当前受管版本目录，拒绝任意路径和配置被替换的部署。"""
        record = self._read(self.record_path)
        revision = record.get("revision", "")
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{24}", revision):
            raise STS2RelayError("relay_prepare_first")
        directory = self.root / revision
        if directory.is_symlink() or directory.resolve().parent != self.root.resolve():
            raise STS2RelayError("relay_files_modified")
        hashes = record.get("sha256", {})
        if not isinstance(hashes, dict):
            raise STS2RelayError("relay_files_modified")
        for name in ("frpc.toml", "ca.crt", "server-bundle.zip"):
            path = directory / name
            expected = str(hashes.get(name, ""))
            if (
                path.is_symlink() or not path.is_file()
                or not hmac.compare_digest(hashlib.sha256(path.read_bytes()).hexdigest(), expected)
            ):
                raise STS2RelayError("relay_files_modified")
        if record.get("settings") != relay_settings(self.config):
            raise STS2RelayError("relay_settings_changed")
        try:
            expires = datetime.fromisoformat(str(record.get("expires", "")))
            if expires.tzinfo is None:
                raise ValueError("missing timezone")
        except ValueError as exc:
            raise STS2RelayError("relay_files_modified") from exc
        if expires <= datetime.now(timezone.utc):
            raise STS2RelayError("relay_certificate_expired")
        return record, directory

    def _process(self):
        """严格核对 PID、创建时间、exe 和完整参数，防止 PID 复用或接管手动 frpc。"""
        import psutil

        record = self._read(self.process_path)
        try:
            pid = record.get("pid")
            if type(pid) is not int or pid <= 0:
                return None
            process = psutil.Process(pid)
            revision = record.get("revision", "")
            if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{24}", revision):
                return None
            command = [str(self.exe), "-c", str(self.root / revision / "frpc.toml")]
            if (
                abs(process.create_time() - float(record.get("created", 0))) > 0.01
                or Path(process.exe()).resolve() != self.exe.resolve()
                or process.cmdline() != command
            ):
                return None
            return process
        except psutil.NoSuchProcess:
            return None
        except (psutil.AccessDenied, ValueError, OSError) as exc:
            raise STS2RelayError("relay_process_identity_unavailable") from exc

    def _assert_stopped(self) -> None:
        """更新资产或生成新部署前必须先显式停止现有受管客户端。"""
        if self._process() is not None:
            raise STS2RelayError("relay_stop_before_changes")

    def installed(self) -> bool:
        """安装状态以受管记录及实际 exe 摘要为准，不运行二进制查询版本。"""
        record = self._read(self.install_path)
        return (
            self.exe.is_file() and not self.exe.is_symlink()
            and record.get("version") == FRP_VERSION
            and hmac.compare_digest(hashlib.sha256(self.exe.read_bytes()).hexdigest(), str(record.get("exe_sha256", "")))
            and (self.exe.parent / "LICENSE").is_file()
            and hmac.compare_digest(hashlib.sha256((self.exe.parent / "LICENSE").read_bytes()).hexdigest(), str(record.get("license_sha256", "")))
        )

    def install(self) -> dict[str, Any]:
        """仅下载官方固定 ZIP，校验完整摘要后读取白名单 exe 和原 LICENSE。"""
        with self._lock:
            self._assert_stopped()
            if any(path.exists() for path in (self.exe, self.exe.parent / "LICENSE", self.install_path)) and not self.installed():
                raise STS2RelayError("relay_unmanaged_client")
            if self.installed():
                return {"installed": True, "version": FRP_VERSION}
            try:
                req = urllib.request.Request(FRP_BASE + FRP_WINDOWS + ".zip", headers={"User-Agent": "WhaleCompanion"})
                with urllib.request.urlopen(req, timeout=60) as response:
                    data = response.read(MAX_PACKAGE_BYTES + 1)
                if len(data) > MAX_PACKAGE_BYTES or not hmac.compare_digest(hashlib.sha256(data).hexdigest(), FRP_WINDOWS_SHA256):
                    raise STS2RelayError("relay_package_checksum_mismatch")
                with zipfile.ZipFile(io.BytesIO(data)) as archive:
                    files = {}
                    for name in ("frpc.exe", "LICENSE"):
                        member = archive.getinfo(FRP_WINDOWS + "/" + name)
                        if member.file_size > MAX_PACKAGE_BYTES:
                            raise STS2RelayError("relay_package_invalid")
                        files[name] = archive.read(member)
            except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
                raise STS2RelayError("relay_download_failed") from exc
            if not files["frpc.exe"].startswith(b"MZ"):
                raise STS2RelayError("relay_package_invalid")
            # exe 最后写入；任何失败都清除本次新建的文件，不覆盖未知安装。
            written = []
            try:
                self._json(self.install_path, {
                    "version": FRP_VERSION, "exe_sha256": hashlib.sha256(files["frpc.exe"]).hexdigest(),
                    "license_sha256": hashlib.sha256(files["LICENSE"]).hexdigest(),
                })
                written.append(self.install_path)
                for name in ("LICENSE", "frpc.exe"):
                    path = self.exe.parent / name
                    self._write(path, files[name])
                    written.append(path)
            except OSError:
                for path in reversed(written):
                    path.unlink(missing_ok=True)
                raise
            return {"installed": True, "version": FRP_VERSION}

    def _certificates(self, host: str) -> tuple[bytes, bytes, bytes, str]:
        """为本套中转签发专用服务器证书；根私钥不落盘、不加入部署包。"""
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

        now = datetime.now(timezone.utc)
        expires = now + timedelta(days=825)
        ca_key = ec.generate_private_key(ec.SECP256R1())
        server_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Whale STS2 Private Relay CA")])
        ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
              .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
              .not_valid_before(now - timedelta(minutes=5)).not_valid_after(expires)
              .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
              .add_extension(x509.KeyUsage(True, False, False, False, False, True, True, False, False), critical=True)
              .sign(ca_key, hashes.SHA256()))
        try:
            san = x509.IPAddress(ipaddress.IPv4Address(host))
        except ValueError:
            san = x509.DNSName(host)
        cert = (x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Whale STS2 Relay")]))
                .issuer_name(ca_name).public_key(server_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=5)).not_valid_after(expires)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([san]), critical=False)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                .sign(ca_key, hashes.SHA256()))
        return (
            ca.public_bytes(serialization.Encoding.PEM), cert.public_bytes(serialization.Encoding.PEM),
            server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
            expires.isoformat(),
        )

    def prepare(self) -> dict[str, Any]:
        """显式生成配套客户端配置与可下载服务端包，不连接或部署远程服务器。"""
        with self._lock:
            self._assert_stopped()
            settings = relay_settings(self.config)
            if not settings["host"]:
                raise STS2RelayError("relay_host_required")
            ca, cert, key, expires = self._certificates(settings["host"])
            token = secrets.token_urlsafe(48)
            revision = secrets.token_hex(12)
            directory = self.root / revision
            self.root.mkdir(parents=True, exist_ok=True)
            if self.root.is_symlink() or self.root.resolve().parent != self.data_dir.resolve():
                raise STS2RelayError("relay_files_modified")
            directory.mkdir(mode=0o700, exist_ok=False)
            quoted_host, quoted_token = json.dumps(settings["host"]), json.dumps(token)
            client = f'''serverAddr = {quoted_host}
serverPort = {settings["control_port"]}
loginFailExit = true
transport.protocol = "tcp"
auth.method = "token"
auth.token = {quoted_token}
transport.tls.enable = true
transport.tls.serverName = {quoted_host}
transport.tls.trustedCaFile = {json.dumps(str(directory / "ca.crt"))}
log.to = {json.dumps(str(directory / "frpc.log"))}
log.level = "info"
log.maxDays = 3
udpPacketSize = 1500

[[proxies]]
name = "whale-sts2"
type = "udp"
localIP = "127.0.0.1"
localPort = {LAN_PORT}
remotePort = {settings["remote_port"]}
'''.encode("utf-8")
            server = f'''bindAddr = "0.0.0.0"
bindPort = {settings["control_port"]}
auth.method = "token"
auth.token = {quoted_token}
transport.tls.force = true
transport.tls.certFile = "./server.crt"
transport.tls.keyFile = "./server.key"
allowPorts = [{{ single = {settings["remote_port"]} }}]
maxPortsPerClient = 1
udpPacketSize = 1500
log.to = "./frps.log"
log.level = "info"
log.maxDays = 3
'''.encode("utf-8")
            script = f'''#!/bin/sh
# 下载并校验固定 Linux x64 版本；不会启动服务或修改防火墙。
set -eu
cd -- "$(dirname -- "$0")"
umask 077
test "$(uname -s)" = Linux && test "$(uname -m)" = x86_64 || {{ printf '%s\\n' 'Linux x64 required'; exit 1; }}
test ! -e ./frps || {{ printf '%s\\n' 'frps already exists; refusing overwrite'; exit 1; }}
work=$(mktemp -d ./whale-download.XXXXXX)
trap 'rm -rf -- "$work"' EXIT HUP INT TERM
curl --fail --location --proto '=https' --tlsv1.2 '{FRP_BASE}frp_{FRP_VERSION}_linux_amd64.tar.gz' -o "$work/frp.tar.gz"
printf '%s  %s\\n' '{FRP_LINUX_SHA256}' "$work/frp.tar.gz" | sha256sum -c -
tar -xzf "$work/frp.tar.gz" -C "$work" 'frp_{FRP_VERSION}_linux_amd64/frps' 'frp_{FRP_VERSION}_linux_amd64/LICENSE'
install -m 700 "$work/frp_{FRP_VERSION}_linux_amd64/frps" ./frps
install -m 600 "$work/frp_{FRP_VERSION}_linux_amd64/LICENSE" ./LICENSE.frp
chmod 600 ./frps.toml ./server.key ./server.crt
printf '%s\\n' 'Installed only. Start manually in this directory: ./frps -c ./frps.toml'
'''.encode("utf-8")
            readme = f'''# Whale STS2 服务器中转包（私有）

仅适用于 Linux x64 公网服务器。frp {FRP_VERSION}，来源 https://github.com/fatedier/frp ，Apache-2.0。
本包包含随机认证 token 和服务器私钥，不要发给朋友、公开上传或提交 Git。
客户端与本包配套，专用 CA 仅在本机 frpc 配置中信任，不安装到系统信任库。
服务器证书有效期至 {expires}；到期前在 Whale 重新生成并部署完整新包。

1. 通过自己的 SSH/SFTP 将 ZIP 传到私有目录，使用 umask 077 后解压。
2. 安装 curl、tar、sha256sum；在解压目录执行 sh install.sh，仅下载校验并安装 frps。
3. 在云安全组和服务器防火墙手动允许 TCP {settings["control_port"]}（房主连接）和 UDP {settings["remote_port"]}（朋友加入）。控制端口建议只允许房主出口 IP。
4. 在此目录手动执行 ./frps -c ./frps.toml。需要长期运行时自行配置 systemd，并将 WorkingDirectory 指向此目录；不要将密钥放在网站目录。
5. 回到 Whale 安装并启动中转客户端，检查本机 frpc.log 与服务端 frps.log 中代理注册结果。
6. AI 在 Host LAN 建房；本机玩家加入 127.0.0.1:{LAN_PORT}，朋友加入 {settings["host"]}:{settings["remote_port"]}（UDP）。

无需家宽入站端口映射，不使用虚拟局域网。只代理 UDP 游戏流量，不代理 MCP API 或 Whale 管理端。
进程运行不代表 UDP 可达；本机双开身份、当前游戏版本兼容、真实三人同步均待实机验收。
停止：本机页面停止客户端；服务器前台 Ctrl+C，或停止自行配置的服务。不要保留无人管理的公网服务。
重新生成会创建全新 token/证书，旧包不能配新客户端；旧版本私有文件保留供恢复，需自行妥善清理。
'''.encode("utf-8")
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, data in {"frps.toml": server, "server.crt": cert, "server.key": key, "install.sh": script, "README.md": readme}.items():
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = 0o100600 << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    archive.writestr(info, data)
            files = {"frpc.toml": client, "ca.crt": ca, "server-bundle.zip": buffer.getvalue()}
            for name, data in files.items():
                self._write(directory / name, data)
            self._json(self.record_path, {
                "revision": revision, "settings": settings, "expires": expires,
                "sha256": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
            })
            return self.download()

    def download(self) -> dict[str, Any]:
        """只在显式下载请求返回私有部署包；普通轮询绝不包含凭据或包内容。"""
        with self._lock:
            record, directory = self._deployment()
            return {"filename": "whale-sts2-server-" + record["revision"] + ".zip",
                    "bundle_base64": base64.b64encode((directory / "server-bundle.zip").read_bytes()).decode("ascii")}

    def start(self) -> dict[str, Any]:
        """只在页面点击时启动摘要与受管记录匹配的客户端，不声称联机可用。"""
        import psutil

        with self._lock:
            self._assert_stopped()
            if self.config.get("sts2_transport") != "lan" or self.config.get("sts2_connection") != "relay":
                raise STS2RelayError("relay_select_mode_first")
            record, directory = self._deployment()
            if not self.installed():
                raise STS2RelayError("relay_install_first")
            command = [str(self.exe), "-c", str(directory / "frpc.toml")]
            child = subprocess.Popen(command, cwd=directory, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._child = child
            try:
                process = psutil.Process(child.pid)
                self._json(self.process_path, {"pid": child.pid, "created": process.create_time(), "revision": record["revision"]})
            except Exception:
                if child.poll() is None:
                    child.terminate()
                    child.wait(timeout=5)
                raise STS2RelayError("relay_start_failed") from None
            return {"started": True, "connection_verified": False}

    def stop(self) -> dict[str, Any]:
        """仅停止身份匹配的受管 frpc，超时保留记录供用户再次操作。"""
        import psutil

        with self._lock:
            process = self._process()
            if process is not None:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except psutil.NoSuchProcess:
                    pass
                except (psutil.TimeoutExpired, psutil.AccessDenied) as exc:
                    raise STS2RelayError("relay_stop_failed") from exc
            self.process_path.unlink(missing_ok=True)
            if self._child is not None:
                self._child.poll()
                self._child = None
            return {"stopped": True}

    def status(self) -> dict[str, Any]:
        """公开非敏感状态；进程存在不能代替服务器登录、代理注册或游戏 UDP 验收。"""
        error, prepared, directory = "", False, None
        try:
            _, directory = self._deployment()
            prepared = True
        except (STS2RelayError, ValueError, OSError) as exc:
            error = str(exc)
        try:
            process = self._process()
            installed = self.installed()
        except (STS2RelayError, OSError) as exc:
            process, installed, error = None, False, str(exc)
        return {
            "version": FRP_VERSION, "installed": installed, "prepared": prepared,
            "running": process is not None, "pid": process.pid if process else None,
            "error": error, "connection_verified": False,
            "bundle_path": str(directory / "server-bundle.zip") if directory else "",
            "log_path": str(directory / "frpc.log") if directory else "",
        }
