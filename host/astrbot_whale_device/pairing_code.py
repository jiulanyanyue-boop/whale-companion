"""主机侧配对码与二维码输出（M05 配对流程第 1 步）。

## 它做什么

主机启动后要能给出「手机该扫的那个东西」。本模块负责：

1. 生成**一次性配对码**（短、人可抄，不含易混淆字符）；
2. 构造**二维码文本**（格式与设备端 `QrPayload.kt` 严格一致）；
3. 在终端里渲染二维码（`qrcode` 可选，缺失时**优雅降级**为只显示配对码）。

## 与 `pairing.py` 的分工

`pairing.py` 管**存储与语义**（`PairingTokenStore.issue/consume/revoke/is_usable`，
一次性失效、TTL、指纹比对都在那里）。本模块只负责**展示层**：把 token 变成
人能抄的码、变成二维码文本、画到终端里。

严格区分「生成」与「消费」：本模块**只生成**，不消费。消费必须由
`pairing.py` 的 `consume` / `submit_code` 完成——那是安全边界。

## 二维码格式（必须与设备端一致）

```
whale://pair?host=<wss地址>&token=<一次性token>&fp=<主机指纹>
```

- `host`：由 `standalone.phone_facing_urls()` 算出的**可连接地址**
  （绝不是 `0.0.0.0` / `127.0.0.1`——那两种手机连不上）
- `token` 与 `fp` 全部 **URL encode**
- 设备端 `QrPayload` 会校验 scheme / host 协议 / token 非空 / fp 形状

## 密钥边界

本模块**从不**接触私钥、签名密钥或模型 API Key。`pairing.py` 的 `HostIdentity`
只有公钥与指纹，这里传进来的也只有那个。测试里专门断言 token 不出现在任何
「可打印摘要」里。
"""

from __future__ import annotations

import base64
import hashlib
import pathlib
import re
import secrets
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable

#: 配对码字符集。
#:
#: 刻意排除了全部「手抄容易看错」的成对字符：
#:
#: | 排除 | 因为易与 | 混淆 |
#: |---|---|---|
#: | `0` | `O` | 零与字母 O |
#: | `1` | `I` `L` | 一与字母 I/L |
#: | `5` | `S` | 五与 S |
#: | `2` | `Z` | 二与 Z |
#:
#: 剩下 27 个字符：`3 4 6 7 8 9 A B C D E F G H J K M N P Q R T U V W X Y`
CODE_ALPHABET = "346789ABCDEFGHJKMNPQRTUVWXY"

#: 配对码长度。
#:
#: 27 个字符取 8 位 ≈ 2.8e11 种组合；配合 10 分钟 TTL 与「一次性失效」，
#: 猜中的概率可忽略。手抄 8 位是可接受的负担。
CODE_LENGTH = 8

#: 二维码 scheme 与路径（与设备端 `QrPayload` 约定一致）。
QR_SCHEME = "whale"
QR_PATH = "pair"


def make_pairing_code(*, length: int = CODE_LENGTH) -> str:
    """生成一个人类友好的配对码（大写字母 + 数字，无易混淆字符）。"""
    if length < 4 or length > 32:
        raise ValueError("pairing code length must be within 4..32")
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(length))


def default_code_factory(length: int = CODE_LENGTH) -> Callable[[], str]:
    """给 `PairingTokenStore.issue(code_factory=...)` 用的工厂。"""
    return lambda: make_pairing_code(length=length)


@dataclass(frozen=True)
class PairingPresentation:
    """一次配对要展示给用户的东西（不含任何私钥/密钥）。"""

    pairing_code: str
    token: str
    expires_at_ms: int
    host_fingerprint: str
    qr_payload: str
    phone_urls: tuple[str, ...]

    @property
    def ttl_minutes(self) -> float:
        return 0.0

    def loggable(self) -> dict[str, Any]:
        """可安全打印的摘要。

        **刻意不含 token**：token 是配对凭据，打印到日志等于把凭据留在磁盘上。
        二维码文本里含 token，因为那是给用户扫的；但它只在终端显示一次，
        不进任何审计/日志表。
        """
        return {
            "pairing_code": self.pairing_code,
            "expires_at_ms": self.expires_at_ms,
            "host_fingerprint": self.host_fingerprint,
            "phone_urls": list(self.phone_urls),
        }


def qr_payload(
    *,
    host_url: str,
    token: str,
    host_fingerprint: str,
    certificate_fingerprint_value: str = "",
    pairing_code: str = "",
) -> str:
    """构造二维码文本：`whale://pair?host=...&token=...&fp=...`。

    所有参数值都 URL encode（token 是 `token_urlsafe` 生成的，可能含 `-` `_`；
    fp 是十六进制）。**不编码会让二维码在某些扫描器上被截断。**

    @param host_url 手机该连的完整地址（`wss://ip:port/path`）
    """
    host = (host_url or "").strip()
    if not host:
        raise ValueError("host_url is required for the pairing QR")
    if not (host.startswith("wss://") or host.startswith("ws://")):
        raise ValueError("host_url must start with wss:// or ws://")
    token_value = (token or "").strip()
    if not token_value:
        raise ValueError("token is required for the pairing QR")

    params = {
        "host": host,
        "token": token_value,
        "fp": (host_fingerprint or "").strip(),
    }
    if pairing_code:
        params["code"] = pairing_code
    # `tfp`：TLS 证书的 SHA-256 指纹，只在有证书时出现（回环明文试验不需要）。
    #
    # 设备端拿它做证书固定：只信任**恰好这一张**证书。
    # 这不是"降低安全"，恰恰相反——它比默认的"信任所有系统 CA 之外一律拒绝"
    # 更适合自签名场景，也比"信任所有证书"严格得多（中间人换了证书立刻失败）。
    cert_fp = (certificate_fingerprint_value or "").strip()
    if cert_fp:
        params["tfp"] = cert_fp

    query = urllib.parse.urlencode(
        params,
        # `safe` 里保留 URL 结构字符：`host` 参数本身就是一个完整 URL，
        # 全量 encode 会变成 `wss%3A%2F%2F...`，设备端解析后拿到的不是 http(s) URL，
        # `URI()` 解析会失败 → 扫码后地址填不进去。
        # 只 encode 会破坏 query 分隔的字符（`&` `#` `+` 空格等）。
        quote_via=urllib.parse.quote,
        safe=":/?=&._-~",
    )
    return f"{QR_SCHEME}://{QR_PATH}?{query}"


def certificate_fingerprint(certfile: str | pathlib.Path) -> str:
    """算 PEM 证书的 SHA-256 指纹（64 位小写十六进制）。

    为什么要这个：Android 默认不信任自签名证书，设备端连 WSS 会在 TLS
    握手阶段被拒（实测 `ssl.SSLCertVerificationError: self-signed certificate`）。
    把这张证书的指纹放进二维码，设备端就能"只信任恰好这一张"——
    既不降安全强度（比"信任所有证书"严格得多），也不需要用户装证书。

    指纹定义与 Android/浏览器一致：对 **DER 编码**的证书做 SHA-256（不是对 PEM 文本）。
    这很重要：PEM 有换行与头部，对文本做哈希得到的值与系统显示的对不上。
    """
    path = pathlib.Path(certfile)
    text = path.read_text(encoding="utf-8")
    match = re.search(
        r"-----BEGIN CERTIFICATE-----(.*?)-----END CERTIFICATE-----",
        text,
        re.S,
    )
    if not match:
        raise ValueError(f"no PEM certificate found in {certfile}")
    der = base64.b64decode("".join(match.group(1).split()))
    return hashlib.sha256(der).hexdigest()


def build_presentation(
    *,
    record: Any,
    host_fingerprint: str,
    phone_urls: list[str],
    cert_fingerprint: str = "",
) -> PairingPresentation:
    """把 `pairing.py` 的 `PairingTokenRecord` 变成「可以展示给用户」的东西。

    @param record `PairingTokenRecord`（有 `token` / `pairing_code` / `expires_at_ms`）
    @param phone_urls `standalone.phone_facing_urls()` 的结果；取第一个作为二维码里的地址
    """
    urls = tuple(url for url in (phone_urls or []) if url)
    if not urls:
        raise ValueError(
            "no phone-facing URL available; host has no local IPv4 address to advertise",
        )
    payload = qr_payload(
        host_url=urls[0],
        token=str(getattr(record, "token", "")),
        host_fingerprint=host_fingerprint,
        certificate_fingerprint_value=cert_fingerprint,
        pairing_code=str(getattr(record, "pairing_code", "")),
    )
    return PairingPresentation(
        pairing_code=str(getattr(record, "pairing_code", "")),
        token=str(getattr(record, "token", "")),
        expires_at_ms=int(getattr(record, "expires_at_ms", 0)),
        host_fingerprint=host_fingerprint,
        qr_payload=payload,
        phone_urls=urls,
    )


def render_terminal_qr(text: str) -> str | None:
    """在终端里画二维码。

    **`qrcode` 缺失时返回 `None`，绝不抛异常。**

    为什么这么设计：`qrcode` 不在项目依赖里（`pyproject.toml` 没声明），
    系统 Python 3.15 上通常没有。让「没装可选库」导致主机启动失败是不可接受的——
    配对码本身就能手输，二维码只是加速手段。
    """
    try:
        import qrcode  # type: ignore[import-not-found]
    except Exception:
        return None

    try:
        qr = qrcode.QRCode(border=1)
        qr.add_data(text)
        qr.make(fit=True)
        import io

        buffer = io.StringIO()
        qr.print_ascii(out=buffer, invert=True)
        rendered = buffer.getvalue()
    except UnicodeEncodeError:
        # `print_ascii` 在 GBK 控制台会因 `▀` / `█` 等方块字符炸掉。
        # 这时改用纯 ASCII 的 `##` / 空格，宁可丑一点也要能显示。
        try:
            rendered = _render_ascii_fallback(qr)
        except Exception:
            return None
    except Exception:
        # 库装上了但渲染失败：同样降级，不让主流程挂掉。
        return None

    return rendered if rendered.strip() else None


def _render_ascii_fallback(qr: Any) -> str:
    """把二维码矩阵画成纯 ASCII（`##` 表示深色，`  ` 表示浅色）。

    只用于「控制台编码不支持方块字符」的环境（典型是 Windows GBK）。
    """
    matrix = qr.get_matrix()
    lines: list[str] = []
    for row in matrix:
        lines.append("".join("##" if cell else "  " for cell in row))
    return "\n".join(lines) + "\n"


def format_pairing_banner(
    presentation: PairingPresentation,
    *,
    qr_text: str | None,
    ttl_seconds: int,
) -> str:
    """拼出给人看的终端说明（中文）。"""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("手机配对：用 App 里的「配对」页面扫下面这个二维码，")
    lines.append("或者在 App 里手动输入。")
    lines.append("=" * 60)
    lines.append("")
    if qr_text:
        lines.extend(qr_text.rstrip("\n").splitlines())
        lines.append("")
    else:
        lines.append("（本机没装 qrcode，无法画二维码。用下面的两组值在 App 里手动输入：）")
        lines.append("")
    lines.append(f"配对码：{presentation.pairing_code}")
    lines.append("")
    # 手输路径需要**两个**值：`submit_code(token, code)` 要逐字比对配对码，
    # 而 token 又是绑定的凭据。只印 8 位码会让用户照抄后必然对不上
    # （设备端手输框要的是 token）。
    lines.append("一次性 token（App 里「一次性 token」框填这个）：")
    lines.append(f"    {presentation.token}")
    lines.append("")
    lines.append(f"有效期：{ttl_seconds // 60} 分钟内有效，只能使用一次")
    lines.append("")
    lines.append("手机端「主机地址」应填：")
    for url in presentation.phone_urls:
        lines.append(f"    {url}")
    lines.append("")
    lines.append("提示：配对完成后配对码与 token 立即失效，不能重复使用。")
    return "\n".join(lines)