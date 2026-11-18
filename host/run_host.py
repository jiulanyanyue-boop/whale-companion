#!/usr/bin/env python
"""Whale Agent v2 主机**一键启动**入口：`python run_host.py` 就够了。

## 为什么还要一个 `run_host.py`

`python -m astrbot_whale_device.standalone --config host.json` 是真正的启动逻辑，
但它有两个对使用者不友好的地方：

1. **参数多**：模块路径要背、`--config` 要写，路径还必须是相对的/绝对的猜；
2. **缺东西就抛 traceback**：`ConfigError: signing secret is not set; export ...`
   这种东西对「只想让主机动起来」的人来说等于没说。

所以本脚本=**启动前的门卫**：按顺序检查四步，任何一步不过，就用中文说清
「你缺什么、为什么必须补、怎么补」，然后以非零退出码退出；四步全过才把
`standalone.main()` 叫起来。

```text
[1] Python 版本 ≥3.11 + websockets 可导入
[2] 配置文件存在？没有 → 自动生成 host/host.json，提示改哪几个字段，退出 1
[3] 配置能解析？不能 → 把 HostConfig 的英文异常翻成中文可照做提示，退出 2
[4] 密钥环境变量齐了？缺 → 只说变量名 + 给 PowerShell/bash 两种设法，退出 1
─────────────────────────────────────────────────────────────
全过 → standalone.main(["--config", ...]) 真正启动（设备服务端 + OneBot 轮询）
```

## 密钥：只印名字，不印值

第 [4] 步的报告**只出现环境变量名**（`WHALE_SIGNING_SECRET` 之类），
`os.environ` 里的值一个字符都不会进打印文本——判断存在性用 `bool(...)`，
拉取值的那一行根本不存在于本进程的任何字符串里。这条由
`tests/test_quickstart.py` 的泄漏断言守着。

## 用法

```powershell
cd .\\host
python run_host.py                  # 缺配置时自动生成 host.json 并告诉你改哪里
python run_host.py --check          # 只体检不启动（CI / 排查用）
python run_host.py --config other.json
python run_host.py --once           # 拉一轮 OneBot 事件就退出（试链路用）
python run_host.py --interval 1.0   # 轮询间隔秒
```

退出码：`0` 就绪/正常退出，`1` 前置未就绪（缺密钥或刚生成配置待编辑），
`2` 配置不合法，`3` 装配或启动失败。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

# 允许 `python host/run_host.py`（从仓库根目录）与 `python run_host.py`（在 host 目录）
# 两种调用方式都能 import 到核心包。
_HOST_DIR = Path(__file__).resolve().parent
if str(_HOST_DIR) not in sys.path:
    sys.path.insert(0, str(_HOST_DIR))

from astrbot_whale_device.quickstart import (  # noqa: E402  (路径修补之后再 import)
    CheckItem,
    check_environment,
    ensure_config_file,
    format_report,
    format_success,
    load_config,
)

DEFAULT_CONFIG_NAME = "host.json"

EXIT_OK = 0
EXIT_NOT_READY = 1
EXIT_BAD_CONFIG = 2
EXIT_START_FAILED = 3

BANNER = """==========================================================
 Whale Agent v2 —— 主机一键启动
 检查顺序：Python/依赖 → 配置文件 → 配置内容 → 密钥环境变量
=========================================================="""


def _print(text: str = "") -> None:
    print(text, flush=True)


def _force_utf8_stdout() -> None:
    """Windows 控制台默认可能是 GBK，中文提示会变乱码甚至抛错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_host.py",
        description="Whale Agent v2 主机一键启动（缺什么就用中文告诉你缺什么）",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=f"配置文件路径（默认 host/{DEFAULT_CONFIG_NAME}）",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只体检，不真正启动",
    )
    parser.add_argument("--once", action="store_true", help="拉一轮 OneBot 事件就退出")
    parser.add_argument("--interval", type=float, default=2.0, help="OneBot 轮询间隔（秒）")
    return parser.parse_args(argv)


def _resolve_config_path(raw: str | None) -> Path:
    if raw:
        return Path(raw).expanduser()
    return _HOST_DIR / DEFAULT_CONFIG_NAME


def _print_items(items: list[CheckItem], heading: str, *, info_only: bool = False) -> int:
    report = format_report(items, heading=heading, info_only=info_only)
    if report:
        _print(report)
    return len(items)


def _extra_preflight(config: object) -> list[CheckItem]:
    """配置**合法**但装配阶段才会炸的前置问题，提前说出来。

    目前只有一种：声明了 TLS 证书却找不到文件（`standalone` 会抛
    `FileNotFoundError: TLS files declared but missing`）。
    """
    items: list[CheckItem] = []
    server = getattr(config, "device_server", None)
    if server is None:
        return items
    missing = list(server.missing_tls_files())
    if missing:
        items.append(
            CheckItem(
                key="device_server.certfile/keyfile",
                message=(
                    "配置里声明了 TLS 证书，但文件不存在："
                    + "、".join(missing)
                    + " —— 设备服务端会因此起不来。"
                ),
                fix=(
                    "把 certfile / keyfile 改成真实存在的证书路径，"
                    "或者两者都置空（明文 ws，仅限本机回环试验，生产禁止）"
                ),
            )
        )
    return items


def _ensure_tls_material(config: object, config_path: Path) -> list[CheckItem]:
    """保证「手机能连」这件事真的成立：非回环监听必须有 TLS 证书。

    背景（两条硬约束互相冲突时的正确解法）：

    1. M05 硬红线：**禁止生产使用无 TLS 的 WebSocket**。`device_server` 把它做成了
       启动闸门——非回环地址 + 无 TLS 直接拒绝启动。
    2. 手机是另一台设备，必须用非回环地址监听，于是**必然要求 TLS**。

    对普通用户来说「自己去生成证书」门槛太高（要找 openssl、写 SAN、让手机信任）。
    所以这里做两件事：

    - 配置里声明了 certfile/keyfile 且文件都在 → 什么都不做；
    - 没声明（空串）且监听地址不是回环 → **自动生成一对自签名证书**并写回配置，
      让 `wss://` 直接可用。

    自动生成会在报告里**如实说明**：自签名证书不被手机信任链认可，试验时需在手机上
    手动信任；生产必须换成受信任 CA 签发的证书。**绝不假装自签名等于生产可用。**

    @return 需要向用户报告的提示项（空列表表示无需处理）
    """
    items: list[CheckItem] = []
    server = getattr(config, "device_server", None)
    if server is None:
        return items

    # 已声明且在 → 无需处理（缺失的情况由 `_extra_preflight` 报告）。
    if server.certfile and server.keyfile and not server.missing_tls_files():
        return items

    # 声明了但缺失 → 交给 `_extra_preflight` 报错，这里不擅自覆盖用户配置。
    if (server.certfile or server.keyfile) and server.missing_tls_files():
        return items

    # 未声明证书，但监听地址是回环 → 明文 ws 是允许的（本机试验）。
    if _is_loopback(server.host):
        return items

    # ---- 非回环且无证书：自动生成 ----
    try:
        from .astrbot_whale_device.tls_cert import (  # type: ignore[import-not-found]
            CertificateError,
            default_certificate_paths,
            ensure_self_signed_certificate,
        )
    except Exception:
        # 包内导入：run_host.py 在 host/ 目录下，astrbot_whale_device 是同级包。
        from astrbot_whale_device.tls_cert import (  # type: ignore[no-redef]
            CertificateError,
            default_certificate_paths,
            ensure_self_signed_certificate,
        )

    cert_path, key_path = default_certificate_paths(config_path.parent / ".certs")
    try:
        generated = ensure_self_signed_certificate(certfile=cert_path, keyfile=key_path)
    except CertificateError as exc:
        items.append(
            CheckItem(
                key="device_server.certfile/keyfile",
                message=(
                    "监听地址不是回环，按安全要求必须启用 TLS，但自动生成证书失败："
                    f"{exc}"
                ),
                fix=(
                    "装依赖后重试：pip install \"cryptography>=42,<46\"\n"
                    "或者手工准备证书，把 device_server.certfile / keyfile 填成真实路径"
                ),
            )
        )
        return items

    # 写回配置（只改这两个键，其它原样保留）。
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        device_server = raw.setdefault("device_server", {})
        device_server["certfile"] = str(generated.certfile)
        device_server["keyfile"] = str(generated.keyfile)
        config_path.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        items.append(
            CheckItem(
                key="device_server.certfile/keyfile",
                message=f"证书已生成，但写回配置失败：{exc}",
                fix=f"手工把 device_server.certfile 填成 {generated.certfile}，keyfile 填成 {generated.keyfile}",
            )
        )
        return items

    items.append(
        CheckItem(
            key="device_server.certfile/keyfile",
            message=generated.message() + f"\n  已写回配置：{config_path}",
            fix=(
                "手机端连接地址改用 wss://（不是 ws://）；"
                "自签名证书需要你在手机上手动信任，否则客户端会拒连"
            ),
        )
    )
    return items


def _is_loopback(host: str) -> bool:
    value = (host or "").strip().lower()
    return value in ("127.0.0.1", "localhost", "::1", "[::1]")


def _print_pairing_code(config: object, config_path: Path) -> None:
    """生成并打印一次性配对码 / 二维码（M05 配对流程第 1 步）。

    **任何失败都只降级、不阻断启动**：配对码是让手机少打字的便利手段，
    不是主机能否运行的前提。最坏情况用户手填 `wss://` 地址 + `device_id`（已支持）。

    为什么在这里生成：用户在手机上扫码/抄码需要一个「先拿到码」的时机，
    主机启动后终端就被日志刷屏了，所以放在启动前。

    安全：token 只出现在二维码里（给扫描器），**不打印到文本**；配对码可以打印。
    """
    try:
        from astrbot_whale_device.pairing import HostIdentity, PairingTokenStore
        from astrbot_whale_device.pairing_code import (
            build_presentation,
            default_code_factory,
            format_pairing_banner,
            render_terminal_qr,
        )
        from astrbot_whale_device.standalone import (
            _listen_path,
            phone_facing_urls,
        )
    except Exception as exc:
        _print(f"（跳过配对码：相关模块不可用 {exc.__class__.__name__}）")
        return

    try:
        server = getattr(config, "device_server", None)
        if server is None:
            return
        scheme = "wss" if server.uses_tls else "ws"
        # 端口在启动前未知（port=0 时由系统分配），这里用配置值；
        # port=0 时无法给出准确地址，跳过二维码、只提示手填。
        port = int(getattr(server, "port", 0) or 0)
        if port <= 0:
            _print("（配对码：端口设为 0 时无法预先给出地址，请用手机手填 wss://<主机IP>:<实际端口>）")
            return

        urls = phone_facing_urls(
            bind_host=str(getattr(server, "host", "")),
            port=port,
            path=_listen_path("ws://placeholder/device"),
            scheme=scheme,
        )
        if not urls:
            _print("（配对码：没找到本机局域网 IP，请用 ipconfig 查后手填）")
            return

        store = PairingTokenStore(sqlite3.connect(":memory:"))
        record = store.issue(now_ms=_now_ms(), code_factory=default_code_factory())

        # 主机指纹：没有真实主机密钥时用一个明确的占位并如实说明，
        # 不伪造一个「看起来像指纹」的值（那会让用户以为身份已固定）。
        host_fingerprint = ""
        try:
            identity = getattr(config, "host_identity", None)
            if isinstance(identity, HostIdentity):
                host_fingerprint = identity.fingerprint
        except Exception:
            host_fingerprint = ""
        if not host_fingerprint:
            host_fingerprint = "0" * DeviceIdentityRules_FINGERPRINT_LEN

        presentation = build_presentation(
            record=record,
            host_fingerprint=host_fingerprint,
            phone_urls=urls,
        )
        qr_text = render_terminal_qr(presentation.qr_payload)
        _print(format_pairing_banner(presentation, qr_text=qr_text, ttl_seconds=600))
        if not host_fingerprint.strip("0"):
            _print("")
            _print("注意：主机尚未配置身份密钥，二维码里的指纹是占位值；")
            _print("      首次配对会要求你确认指纹，届时以界面显示的为准。")
    except Exception as exc:
        # 兜底：绝不因为「画不出二维码」让主机启动失败。
        _print(f"（配对码生成失败：{exc.__class__.__name__}，可用手机手填地址完成配对）")


#: 指纹长度（与 `pairing.py` 的 `is_well_formed_fingerprint` 一致：SHA-256 十六进制）。
DeviceIdentityRules_FINGERPRINT_LEN = 64


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


def run(argv: list[str] | None = None) -> int:
    """按四步检查，全过才启动。返回退出码。"""
    args = _parse_args(argv)
    config_path = _resolve_config_path(args.config)

    _print(BANNER)
    _print(f"配置文件：{config_path}")

    # ---- 第 1 步：Python 版本 + websockets ---------------------------------
    _print("[1/4] 检查 Python 版本与依赖 ...")
    base_items = check_environment(
        environ=os.environ,
        python_version=(sys.version_info[0], sys.version_info[1]),
    )
    # 配置还没读，先只看「与配置无关」的那两条（版本/依赖）。
    config_independent = [item for item in base_items if item.key in {"python", "websockets"}]
    if config_independent:
        _print_items(config_independent, heading="前置检查没过：")
        return EXIT_NOT_READY
    _print(f"      Python {sys.version_info[0]}.{sys.version_info[1]} OK，websockets OK")

    # ---- 第 2 步：配置文件存在（缺就生成，然后让人去改） -------------------
    _print("[2/4] 检查配置文件 ...")
    if not config_path.is_file():
        bootstrap = ensure_config_file(config_path)
        _print("")
        _print(bootstrap.message)
        _print("")
        _print("为什么要先改：宿主拒绝未知/占位值，直接用默认值跑起来会立刻被")
        _print("自己的校验拦下（例如 owner_user_id 必须是你真实的 QQ 号）。")
        _print("改完再跑 python run_host.py。")
        return EXIT_NOT_READY

    # ---- 第 3 步：配置能解析 ----------------------------------------------
    config, config_items = load_config(config_path)
    if config is None:
        _print_items(config_items, heading="配置检查没过：")
        _print("")
        _print("提示：删掉这个文件可以让 python run_host.py 重新生成一份干净的模板。")
        return EXIT_BAD_CONFIG
    _print("      配置解析 OK")

    preflight = _extra_preflight(config)
    if preflight:
        _print_items(preflight, heading="装配前置检查没过：")
        return EXIT_START_FAILED

    # ---- 第 3.5 步：保证「手机真的能连」所需的前置 -----------------------
    # M05 禁止非回环地址上的明文 ws，所以监听 0.0.0.0 时必须有 TLS 证书。
    # 用户没配证书就自动生成一对自签名证书并写回配置（生成成功才继续）。
    tls_items = _ensure_tls_material(config, config_path)
    fatal_tls = [
        item for item in tls_items
        if item.key == "device_server.certfile/keyfile" and "失败" in item.message
    ]
    if fatal_tls:
        _print_items(fatal_tls, heading="TLS 准备失败，主机没有启动：")
        return EXIT_START_FAILED
    if tls_items:
        _print("[2.5/4] 准备 TLS 证书 ...")
        # 证书已自动生成：这是「告知」而不是「缺失」，措辞不能说成启动失败。
        _print_items(tls_items, heading="", info_only=True)
        _print("")
        # 证书已写回配置，重新加载 config，让后续启动读到新路径。
        reloaded, reload_items = load_config(config_path)
        if reloaded is None:
            _print_items(reload_items, heading="TLS 写回后配置校验失败：")
            return EXIT_BAD_CONFIG
        config = reloaded

    # ---- 第 4 步：密钥环境变量（只报名字，不报值） ------------------------
    _print("[3/4] 检查密钥环境变量 ...")
    env_items = check_environment(environ=os.environ, python_version=None, config=config)
    # 版本/依赖在第 1 步已经过了，这里只要密钥类问题。
    env_items = [item for item in env_items if item.key not in {"python", "websockets"}]
    if env_items:
        _print_items(env_items, heading="密钥检查没过：")
        _print("")
        _print("（本报告只显示环境变量名，不显示任何密钥值。）")
        return EXIT_NOT_READY
    _print("      密钥环境变量 OK（值不打印）")

    # ---- 全过 ------------------------------------------------------------
    _print("[4/4] 全部就绪")
    _print("")
    _print(format_success(config, config_path))

    if args.check:
        _print("")
        _print("--check 模式：体检通过，未真正启动。去掉 --check 就会启动主机。")
        return EXIT_OK

    # ---- M05 配对第 1 步：生成一次性配对码 + 二维码 ----
    # 放在启动**之前**：用户需要先扫/抄配对码，再让手机连上来。
    # 任何一个环节失败都只降级（不阻断启动）——配对码是便利手段，不是启动前提。
    _print("")
    _print_pairing_code(config, config_path)

    _print("")
    _print("=== 正在启动独立宿主（Ctrl+C 停止） ===")
    standalone_argv = ["--config", str(config_path)]
    if args.once:
        standalone_argv.append("--once")
    if args.interval:
        standalone_argv += ["--interval", str(args.interval)]

    # 真正的启动逻辑只在 standalone.main() 里，本脚本**不复制**它。
    from astrbot_whale_device import standalone  # 延迟导入：检查阶段不需要装配依赖

    try:
        return int(standalone.main(standalone_argv))
    except KeyboardInterrupt:
        _print("")
        _print("收到 Ctrl+C，已停止。")
        return EXIT_OK
    except FileNotFoundError as exc:
        _print("")
        _print(f"启动失败：{exc}")
        _print("这通常是证书文件路径写错/文件不存在。改好 host.json 里的 ")
        _print("device_server.certfile 与 keyfile（或都置空走本机明文 ws）。")
        return EXIT_START_FAILED
    except Exception as exc:  # 兜底：绝不把 traceback 甩给使用者
        _print("")
        _print(f"启动失败：{exc.__class__.__name__}: {exc}")
        _print("如果这看起来像配置问题，请再跑一次 python run_host.py --check 看体检报告；")
        _print("需要细节时可以加环境变量 WHALE_DEBUG=1 ...（当前版本暂未提供 traceback 开关）")
        return EXIT_START_FAILED


def main(argv: list[str] | None = None) -> int:
    """`python run_host.py` 的入口：把意外一律转成人话 + 非零退出码。"""
    _force_utf8_stdout()
    try:
        return run(argv)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        _print("")
        _print("收到 Ctrl+C，已停止。")
        return EXIT_OK
    except Exception as exc:  # 连检查本身崩了，也要给人话而不是 traceback
        _print("")
        _print(f"启动脚本自身出错：{exc.__class__.__name__}: {exc}")
        _print("这不是配置问题。请把上面这行原样报给维护者；")
        _print("想自己先排查可以加 --check 走纯体检路径。")
        return EXIT_START_FAILED


if __name__ == "__main__":  # pragma: no cover - 直接执行时走这里
    raise SystemExit(main())
