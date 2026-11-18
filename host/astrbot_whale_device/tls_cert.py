"""主机侧 TLS 证书自动生成（自签名，供局域网试用）。

## 为什么需要它

M05 有一条硬红线：**禁止生产使用无 TLS 的 WebSocket**。
`device_server.py` 把它实现成了启动闸门——`ssl_context is None` 且绑定地址不是回环时，
直接拒绝启动：

```
plaintext ws:// is forbidden outside loopback; production must use wss://
```

这条闸门是对的，但它与「让手机连上」直接冲突：手机是**另一台设备**，
必须用非回环地址监听（`0.0.0.0`），于是就必然要求 TLS 证书。

手工生成证书对普通用户是过高门槛（要找 openssl、写 CN/SAN、还要让手机信任）。
所以这里做**自动生成**：第一次启动时如果没证书，就用 `cryptography` 生成一对
自签名证书放到配置指定的位置，然后按 `wss://` 启动。

## 诚实说明（非常重要）

自签名证书**不会被手机的信任链认可**。手机端要连接 `wss://` 时，通常需要：
- 把该证书装成「受信任的凭据」（Android 需手动在系统设置里安装），或
- 在客户端显式「信任这个自签名证书」（本项目的设备端**尚未实现**这一点）。

因此本模块生成的证书适用于：
- ✅ 本机/局域网内**试验**（前提是客户端信任它）
- ❌ 生产环境（生产应使用受信任 CA 签发的证书）

本模块不会假装「自签名 = 生产可用」。

## 生成内容

- 算法：ECDSA P-256 + SHA-256（与设备端身份算法族一致，强度足够）
- 有效期：建议短期（默认 2 年），过期后重新生成
- SAN：包含 `localhost`、`127.0.0.1` 以及本机所有局域网 IPv4
  （这正是手机端要连的那个地址，必须写进 SAN，否则严格的客户端会拒）
- 输出：PEM 格式的 `cert.pem` 与 `key.pem`，私钥文件权限尽量收紧
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import pathlib
import socket
from dataclasses import dataclass

#: 生成证书时写进 SAN 的固定条目（回环与常见本机名）。
_FIXED_SAN_DNS = ("localhost",)


class CertificateError(RuntimeError):
    """证书生成失败（缺少依赖、路径不可写、参数非法等）。"""


@dataclass(frozen=True)
class GeneratedCertificate:
    certfile: pathlib.Path
    keyfile: pathlib.Path
    #: 是否本次新建（False 表示复用了已存在的证书）。
    created: bool
    #: 写进 SAN 的地址列表，便于调用方打印「手机该连哪个」。
    subject_alt_names: tuple[str, ...] = ()

    def message(self) -> str:
        if not self.created:
            return f"复用已有证书：{self.certfile}"
        return (
            f"已生成自签名证书：{self.certfile}\n"
            f"  私钥：{self.keyfile}\n"
            f"  覆盖地址（SAN）：{', '.join(self.subject_alt_names)}\n"
            "  注意：自签名证书不被手机信任链认可，试验时需要在手机上手动信任它；"
            "生产环境请换成受信任 CA 签发的证书。"
        )


def _local_ipv4() -> list[str]:
    """本机局域网 IPv4（复用 standalone 里同一套判定，避免两份逻辑漂移）。"""
    from .standalone import local_ipv4_addresses

    return local_ipv4_addresses()


def _require_cryptography():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except Exception as exc:  # pragma: no cover - 只在缺依赖时触发
        raise CertificateError(
            "生成证书需要 cryptography 库；请执行 pip install 'cryptography>=42,<46'"
        ) from exc
    return x509, hashes, serialization, ec, NameOID


def ensure_self_signed_certificate(
    *,
    certfile: str | pathlib.Path,
    keyfile: str | pathlib.Path,
    common_name: str = "whale-agent-host",
    valid_days: int = 730,
    extra_ipv4: list[str] | None = None,
    include_local_addresses: bool = True,
) -> GeneratedCertificate:
    """确保证书存在；缺就生成一对自签名证书，已存在则原样复用。

    幂等：两个文件都在就直接返回 `created=False`，**不覆盖已有的证书**
    （覆盖会让之前已经信任旧证书的手机全部失效）。

    @param extra_ipv4 额外要写进 SAN 的 IPv4（例如你明确的固定 IP）
    @param include_local_addresses 是否自动把本机局域网 IP 写进 SAN
    """
    cert_path = pathlib.Path(certfile)
    key_path = pathlib.Path(keyfile)

    if cert_path.is_file() and key_path.is_file():
        return GeneratedCertificate(
            certfile=cert_path,
            keyfile=key_path,
            created=False,
        )

    if cert_path.is_file() != key_path.is_file():
        # 只有其中一个存在：状态可疑，宁可报错让人看清楚，也不要瞎覆盖。
        raise CertificateError(
            "证书与私钥只存在其一，状态不一致；请确认后删除残留文件再重试："
            f" cert={cert_path.exists()} key={key_path.exists()}",
        )

    x509, hashes, serialization, ec, NameOID = _require_cryptography()

    san_ips: list[str] = []
    if extra_ipv4:
        for raw in extra_ipv4:
            try:
                ipaddress.IPv4Address(raw)
            except ValueError as exc:
                raise CertificateError(f"not a valid IPv4 address: {raw}") from exc
            san_ips.append(raw)
    if include_local_addresses:
        for ip in _local_ipv4():
            if ip not in san_ips:
                san_ips.append(ip)

    try:
        private_key = ec.generate_private_key(ec.SECP256R1())
    except Exception as exc:
        raise CertificateError(f"failed to generate private key: {exc}") from exc

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Whale Agent (self-signed)"),
        ],
    )

    now = _dt.datetime.now(_dt.timezone.utc)
    try:
        builder = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _dt.timedelta(minutes=5))
            .not_valid_after(now + _dt.timedelta(days=valid_days))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        )

        san_entries: list[object] = [
            x509.DNSName(name) for name in _FIXED_SAN_DNS
        ]
        san_entries.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
        for ip in san_ips:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(ip)))
        builder = builder.add_extension(
            x509.SubjectAlternativeName(san_entries),
            critical=False,
        )

        certificate = builder.sign(private_key, hashes.SHA256())
    except Exception as exc:
        raise CertificateError(f"failed to build certificate: {exc}") from exc

    try:
        cert_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.parent.mkdir(parents=True, exist_ok=True)
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(
            private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
        )
    except Exception as exc:
        raise CertificateError(f"failed to write certificate files: {exc}") from exc

    try:
        # 尽量收紧私钥权限（Windows 上 chmod 语义有限，失败不影响可用性）。
        key_path.chmod(0o600)
    except Exception:
        pass

    names = tuple(list(_FIXED_SAN_DNS) + ["127.0.0.1"] + san_ips)
    return GeneratedCertificate(
        certfile=cert_path,
        keyfile=key_path,
        created=True,
        subject_alt_names=names,
    )


def default_certificate_paths(base: str | pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    """默认证书位置：`<base>/certs/host-cert.pem` 与 `<base>/certs/host-key.pem`。"""
    root = pathlib.Path(base) / "certs"
    return root / "host-cert.pem", root / "host-key.pem"