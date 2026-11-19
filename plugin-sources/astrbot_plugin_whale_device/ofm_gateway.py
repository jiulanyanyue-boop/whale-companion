"""读取同机 Our Free Model 的回环网关配置，不改写 DSH 设置。"""

import json
import os
from pathlib import Path


OFM_KEY_ENV = "WHALE_OFM_MODEL_KEY"


def read_ofm_gateway() -> tuple[str, str] | None:
    """只接受已启用的 IPv4 回环转发；返回 Base URL 与独立凭据供内部使用。"""
    root = Path(os.environ.get("DSH_HOME") or str(Path.home() / ".dsh"))
    path = root / "our-free-model" / "settings.json"
    try:
        settings = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise ValueError("无法读取本机 Our Free Model 转发设置。") from None
    forward = settings.get("forward") if isinstance(settings, dict) else None
    if not isinstance(forward, dict):
        raise ValueError("Our Free Model 本地转发设置不完整。")
    if settings.get("enabled") is False or forward.get("enabled") is not True:
        raise ValueError("Our Free Model 本地转发未启用，请在 DSH → Our Free Model → 网关设置中开启。")
    port, key = forward.get("port"), settings.get("forwardKey")
    if forward.get("host", "127.0.0.1") != "127.0.0.1" or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Our Free Model 需使用 127.0.0.1 和有效的本地转发端口。")
    if not isinstance(key, str) or not key or not all(33 <= ord(c) <= 126 for c in key):
        raise ValueError("Our Free Model 本地转发凭据未就绪，请在 DSH 网关设置检查。")
    return f"http://127.0.0.1:{port}/v1", key
