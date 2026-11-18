"""主机图形界面（tkinter，标准库，无额外依赖）。

## 为什么需要它

终端方案有两个硬伤，都是真实用户会在第一次使用时就撞上的：

1. **二维码显示不出来**。终端里的二维码是用 `█` `▀` 这类方块字符拼出来的，
   而 Windows 默认终端字体（Consolas / 新宋体）不保证这些字符等宽且可渲染，
   于是用户看到的是一堆错位的乱码块，甚至白光光的空白。
   这不是「字太小」，是**终端根本不是一个可靠的图形输出设备**。
2. **配置全靠改 JSON**。主人 QQ、白名单应用、模型 Key 这些让用户在记事本里
   手改 `host.json`，改错一个引号就启动失败，且没有任何校验提示。

所以这里用 tkinter 做一个真正的窗口：

- **二维码用 Canvas 逐格画方块**，不依赖终端字符，也不依赖 Pillow；
- **配置项做成表单**，带校验与友好提示；
- **启动/停止**按钮 + 实时状态；
- 未装 `qrcode` 时**如实降级**（显示配对码与地址，不假装画得出二维码）。

## 依赖边界

- `tkinter`：Python 标准库，Windows 官方发行版自带。
- `qrcode`：**可选**。缺了只影响「画二维码」这一步，其余功能全部可用。
  `qrcode.get_matrix()` 直接给布尔矩阵，所以**不需要 Pillow**。

## 安全边界

- 密钥（签名密钥、模型 Key、NapCat token）**只从环境变量读**，界面只显示
  「是否已就绪」，**绝不回显值**。
- 生成的配对码是**一次性**的（10 分钟 TTL，用后失效），由 `pairing.py` 保证。
"""

from __future__ import annotations

import json
import os
import secrets as _secrets
import sqlite3
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass
import customtkinter as ctk
from typing import Any, Callable

import pathlib

from .gui_secrets import (
    DEFAULT_SIGNING_ENV,
    StoredSecrets,
    ensure_secrets_ignored,
    generate_signing_secret,
    mask,
)
from .host_config import HostConfig
from .pairing import PairingTokenStore
from .pairing_code import (
    build_presentation,
    default_code_factory,
    format_pairing_banner,
    render_terminal_qr,
)
from .standalone import phone_facing_urls

#: 配对码有效期（与 `pairing.PAIRING_TOKEN_TTL_MS` 一致：10 分钟）。
PAIRING_TTL_MS = 10 * 60 * 1000

#: 二维码在界面上的目标边长（像素）。足够手机摄像头对焦，又不撑破窗口。
QR_PIXEL_SIZE = 260


# ---------------------------------------------------------------------------
# 配置表单模型（纯逻辑，可单测）
# ---------------------------------------------------------------------------


@dataclass
class FormState:
    """界面上的可编辑配置。**不含任何密钥**（密钥只走环境变量）。"""

    owner_user_id: str = ""
    device_id: str = ""
    allowed_packages: str = ""
    blocked_packages: str = ""
    onebot_http_url: str = ""
    device_server_host: str = "0.0.0.0"
    device_server_port: str = "8765"
    model_base_url: str = ""
    model_id: str = "gpt-4o-mini"
    signing_secret_env: str = "WHALE_SIGNING_SECRET"
    model_api_key_env: str = "WHALE_MODEL_KEY"
    onebot_token_env: str = "NAPCAT_TOKEN"

    @classmethod
    def from_config(cls, config: HostConfig) -> FormState:
        """从已解析的 `HostConfig` 填充表单。"""
        server = config.device_server
        onebot = config.onebot
        return cls(
            owner_user_id=config.owner_user_id,
            device_id=config.device_id,
            allowed_packages="\n".join(config.allowed_packages),
            blocked_packages="\n".join(config.blocked_packages),
            onebot_http_url=getattr(onebot, "http_url", "") if onebot else "",
            device_server_host=server.host,
            device_server_port=str(server.port),
            model_base_url=config.model.base_url,
            model_id=config.model.model_id,
            signing_secret_env=config.signing_secret_env,
            model_api_key_env=getattr(config, "model_api_key_env", "") or "",
            onebot_token_env=getattr(onebot, "access_token_env", "") if onebot else "",
        )

    def to_mapping(self) -> dict[str, Any]:
        """转回 `HostConfig.from_mapping` 认的字典结构。

        包名支持一行一个（也接受逗号/空格分隔）——用户通常是从各种地方粘贴过来的，
        强制「必须用 JSON 数组」属于把格式细节推给用户。
        """
        mapping: dict[str, Any] = {
            "owner_user_id": self.owner_user_id.strip(),
            # device_id：空则用占位合法值（后续由手机同步真实 id）。
            "device_id": _fallback(self.device_id.strip(), "device-pending"),
            "allowed_packages": split_packages(self.allowed_packages),
            "blocked_packages": split_packages(self.blocked_packages),
            "signing_secret_env": self.signing_secret_env.strip() or "WHALE_SIGNING_SECRET",
            "model": {
                "provider_id": "openai",
                # model_id 为空时用占位值：用户可能想先启动主机、稍后选模型。
                "model_id": _fallback(self.model_id.strip(), "unset"),
                "base_url": self.model_base_url.strip(),
            },
            "device_server": {
                "host": self.device_server_host.strip() or "0.0.0.0",
                "port": _port_or_zero(self.device_server_port),
            },
        }
        if self.model_api_key_env.strip():
            mapping["model_api_key_env"] = self.model_api_key_env.strip()
        if self.onebot_http_url.strip():
            mapping["onebot"] = {
                "http_url": self.onebot_http_url.strip(),
                "access_token_env": self.onebot_token_env.strip(),
            }
        return mapping


def _fallback(value: str, default: str) -> str:
    """空值兜底：用户没填就用默认值。

    为什么要这层：界面只暴露 4 个输入框，其余字段在 FormState 里是空串，
    而 HostConfig 对它们有硬性校验。直接把空串提交上去，
    用户会看到一条英文正则报错（如 device_id must match [A-Za-z0-9._:-]{3,80}），
    而他**根本没有对应的输入框**——这是产品缺陷，不该由用户承担。
    """
    text = (value or "").strip()
    return text if text else default


def split_packages(raw: str) -> list[str]:
    """把多行/逗号/空格分隔的包名拆成列表，去空、去重、保序。"""
    tokens: list[str] = []
    for chunk in (raw or "").replace(",", "\n").replace("，", "\n").splitlines():
        for piece in chunk.split():
            value = piece.strip()
            if value and value not in tokens:
                tokens.append(value)
    return tokens


def validate_model_url(raw: str) -> tuple[bool, str]:
    """校验模型 URL，返回 `(是否可用, 中文提示)`。

    为什么单独做这一层：`ModelConfig` 对 https 的要求是**硬性**的（M02 规定，
    连回环地址也不例外），但它抛的是英文 `base_url must use https`——
    用户既看不懂，也不知道该怎么改（换成哪个地址？本地模型怎么办？）。
    这里把最常见的三种情况翻成可照做的中文。

    允许留空：部分部署只用本地模型、根本不调远程模型。
    """
    value = (raw or "").strip()
    if not value:
        return True, ""
    lowered = value.lower()
    if lowered.startswith("http://"):
        return False, (
            "模型 URL 必须用 https（http 明文传输会被拒绝，这是 M02 的硬性规定）。\n\n"
            "改成 https:// 开头即可，例如：https://api.openai.com\n"
            "若你用本机模型（如 Ollama），请通过 https 反代接入，"
            "而不是直接填 http://127.0.0.1。"
        )
    if not lowered.startswith("https://"):
        return False, (
            "模型 URL 必须以 https:// 开头。\n\n"
            "例如：https://api.openai.com 或 https://api.deepseek.com"
        )
    return True, ""


def _port_or_zero(raw: str) -> int:
    """端口容错：非数字就回 0（交给 `HostConfig` 校验，那里会给出人话错误）。"""
    try:
        return int(str(raw).strip())
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# 二维码绘制（Canvas，纯 tkinter，不需要 Pillow）
# ---------------------------------------------------------------------------


def qr_matrix(text: str) -> list[list[bool]] | None:
    """算出二维码布尔矩阵；`qrcode` 缺失时返回 `None`（如实降级，不抛异常）。"""
    try:
        import qrcode  # type: ignore[import-not-found]
    except Exception:
        return None
    try:
        qr = qrcode.QRCode(border=1)
        qr.add_data(text)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        return [[bool(cell) for cell in row] for row in matrix]
    except Exception:
        return None


def draw_qr_on_canvas(canvas: tk.Canvas, matrix: list[list[bool]], size: int = QR_PIXEL_SIZE) -> None:
    """把布尔矩阵逐格画成黑色方块。

    为什么逐格画而不是用图片：`qrcode` 生成 PNG 需要 Pillow，
    而 Pillow 不是本项目依赖。Canvas 画方块零依赖，且缩放清晰。
    """
    canvas.delete("all")
    rows = len(matrix)
    if rows == 0:
        return
    cell = max(1, size // rows)
    for y, row in enumerate(matrix):
        for x, dark in enumerate(row):
            if not dark:
                continue
            canvas.create_rectangle(
                x * cell,
                y * cell,
                (x + 1) * cell,
                (y + 1) * cell,
                fill="#000000",
                outline="",
            )


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppEntry:
    package_name: str
    label: str


def app_entries(evidence: dict[str, Any]) -> list[AppEntry]:
    """Read names separately from the identifiers used to save configuration."""
    raw = evidence.get("apps")
    if raw is None:
        return [AppEntry(p.strip(), p.strip()) for p in str(evidence.get("packages", "")).splitlines() if p.strip()]
    rows = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(rows, list):
        raise ValueError("手机返回的应用列表格式不正确")
    entries = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("package_name"), str):
            raise ValueError("手机返回的应用信息缺少包名")
        package = row["package_name"].strip()
        if not package:
            continue
        label = row.get("label")
        label = label.strip() if isinstance(label, str) else ""
        entries[package] = AppEntry(package, label or package)
    return sorted(entries.values(), key=lambda app: (app.label.casefold(), app.package_name))


class HostWindow:
    """主机控制面板。"""

    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        self.server_thread: threading.Thread | None = None
        self.server_stop = threading.Event()
        self.host_obj: Any = None
        self.device_rpc = None
        self.qq_connection = None
        self._qq_connect_pending = False
        self._closing = False
        self._last_qq_state = ""
        self._tray = None
        self.qq_onboarding = None
        self._qq_managed_login = False
        self._qq_login_phase = 'idle'
        self._qq_last_qrcode = ''
        self._qq_webui_auth_retry_at = 0.0
        self._qq_webui_auth_backoff = 1.0
        self.qq_account_controller = None

        ctk.set_appearance_mode("light")
        # CTk already receives Windows display scaling; keep app geometry inside the work area.
        ctk.set_widget_scaling(1.0)
        ctk.set_window_scaling(1.0)
        self.root = ctk.CTk()
        from .desktop_components import notice
        self.show_info = lambda title, text: notice(self.root, title, text)
        self.show_error = lambda title, text: notice(self.root, title, text, error=True)
        self.root.title("Whale Agent 主机")
        self.root.geometry("960x680")
        self.root.minsize(860, 560)

        self._app_rows: list[AppEntry] = []
        self._build_ui()
        self._fit_initial_window()
        if self._astrbot_device_mode_enabled():
            try:
                from .desktop_components import button
                control = getattr(self, "astrbot_control", None)
                if control is not None:
                    button(control, "打开 AstrBot WebUI", self._open_astrbot_webui, True).pack(anchor="w", pady=(10, 0))
            except Exception:
                pass
        self.root.after(350, self._fit_initial_window)
        self._load_config_into_form()
        self._connected_device_id = ""
        self._fetching_apps = False
        self.root.after(500, self._poll_device_connection)
        self.root.after(400, self._poll_qq)
        self.root.protocol("WM_DELETE_WINDOW", self._hide_to_tray)
        self._load_qq_preferences()
        self.root.after(1000, self._restore_qq_connection)

    def _fit_initial_window(self):
        """Keep the first window inside the desktop work area at any DPI."""
        root = self.root
        root.update_idletasks()
        left, top, right, bottom = 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()
        if os.name == "nt":
            try:
                import ctypes
                from ctypes import wintypes
                area = wintypes.RECT()
                if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(area), 0):
                    left, top, right, bottom = area.left, area.top, area.right, area.bottom
            except (AttributeError, OSError):
                pass
        # CTk scales dimensions but uses physical pixels for window positions.
        # Tk can still report its temporary 100x100 creation window here.
        # Use the requested size and CTk's active DPI scale instead.
        logical_width, logical_height = 960, 680
        scale = root._get_window_scaling()
        border = max(0, root.winfo_rootx() - root.winfo_x())
        title = max(0, root.winfo_rooty() - root.winfo_y())
        width = min(logical_width, max(1, int((right - left - 2 * border - 32) / scale)))
        height = min(logical_height, max(1, int((bottom - top - title - border - 32) / scale)))
        root.minsize(min(860, width), min(560, height))
        x = left + max(0, int((right - left - width * scale - 2 * border) / 2))
        y = top + max(0, int((bottom - top - height * scale - title - border) / 2))
        root.geometry(f"{width}x{height}+{x}+{y}")

    def _wire_device_requests(self, host):
        from .desktop_device_transport import DesktopDeviceTransport
        host.plugin.transport = DesktopDeviceTransport(host, self.config_path)

    def _apply_host_settings(self):
        host = self.host_obj
        if host is None: return
        from dataclasses import replace
        from .security import PackagePolicy, PrincipalGate
        from .astrbot_adapter import ModelGateway, make_model_decider
        from .model_client import make_model_transport
        config = HostConfig.load(self.config_path)
        secrets = StoredSecrets.load(self.config_path)
        gateway = ModelGateway(config.model, make_model_transport(
            api_key=secrets.model_api_key or config.model_api_key(), base_url=config.model.base_url or None))
        host.config = config
        host.gateway = gateway
        plugin = host.plugin
        plugin.config = replace(plugin.config, owner_user_id=config.owner_user_id,
            device_id=config.device_id, allowed_packages=set(config.allowed_packages),
            blocked_packages=set(config.blocked_packages))
        plugin.policy = PackagePolicy(set(config.allowed_packages), set(config.blocked_packages))
        plugin.gate = PrincipalGate(config.owner_user_id)
        plugin.router.gate = plugin.gate
        plugin.router.package_policy = plugin.policy
        plugin.router.model_decider = make_model_decider(gateway)

    def _load_qq_preferences(self) -> None:
        from .qq_settings import QQSettings
        try:
            config = QQSettings.load(self.config_path)
        except Exception:
            config = QQSettings()
            self._log("QQ 连接配置无效，请在 QQ 接入页重新填写。")
        self.qq_url_var.set(config.ws_url)
        self.qq_auto_reply_var.set(config.auto_reply)
        self.qq_webui_url_var.set(config.webui_url)
        self.qq_launcher_var.set(config.launcher_path)
        self.qq_qq_path_var.set(config.qq_path)
        self._qq_managed_login = config.managed_login
        state = self._qq_controller().status()
        if not (self._qq_controller().path.exists()):
            self._qq_controller().set_auto_login(config.auto_login)
            state = self._qq_controller().status()
        if hasattr(self, 'qq_auto_login_var'):
            self.qq_auto_login_var.set(bool(state.get('auto_login')))
        secrets = StoredSecrets.load(self.config_path)
        self.qq_token_var.set(secrets.onebot_token)
        self.qq_webui_token_var.set(secrets.napcat_webui_token)

    def _save_qq_preferences(self, auto_connect: bool):
        from .qq_settings import QQSettings
        config = QQSettings(self.qq_url_var.get().strip(), bool(self.qq_auto_reply_var.get()), auto_connect,
            webui_url=self.qq_webui_url_var.get().strip(), launcher_path=self.qq_launcher_var.get().strip(),
            managed_login=self._qq_managed_login, qq_path=self.qq_qq_path_var.get().strip(), auto_login=bool(self.qq_auto_login_var.get()) if hasattr(self,'qq_auto_login_var') else False)
        config.save(self.config_path)
        secrets = StoredSecrets.load(self.config_path)
        secrets.onebot_token = self.qq_token_var.get().strip()
        secrets.napcat_webui_token = self.qq_webui_token_var.get().strip()
        secrets.save(self.config_path)
        return config

    def _open_astrbot_webui(self):
        import webbrowser
        webbrowser.open("http://127.0.0.1:6185")

    def _astrbot_device_mode_enabled(self) -> bool:
        marker = pathlib.Path(self.config_path).resolve().parent / "astrbot-device-mode.json"
        return os.environ.get("WHALE_ASTRBOT_DEVICE_MODE") == "1" or marker.is_file()

    def _restore_qq_connection(self):
        if self._astrbot_device_mode_enabled():
            self._log("AstrBot接入模式已启用，桌面端不会启动第二个QQ消息连接。")
            if self.host_obj is None and not (self.server_thread and self.server_thread.is_alive()):
                self._on_start()
            try:
                import socket, subprocess
                from .qq_settings import QQSettings
                repo_root = pathlib.Path(self.config_path).resolve().parent.parent
                self._qq_controller().clear_logout_suppression()
                if self._qq_controller().status().get('auto_login'):
                    self._qq_controller().login()
                with socket.socket() as probe:
                    probe.settimeout(0.3)
                    running = probe.connect_ex(("127.0.0.1", 6185)) == 0
                if not running:
                    python_runtime = repo_root / "runtime" / "astrbot-venv" / "Scripts" / "python.exe"
                    launcher = repo_root / "tools" / "run-astrbot.py"
                    if python_runtime.is_file() and launcher.is_file():
                        subprocess.Popen([str(python_runtime), str(launcher)], cwd=str(repo_root),
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                        self._log("正在启动 AstrBot 和本地记忆服务。")
                    else:
                        self._log("找不到 AstrBot 运行环境，请检查安装文件。")
            except Exception as exc:
                self._log(f"AstrBot 自动启动失败：{type(exc).__name__}")
            return
        from .qq_settings import QQSettings
        try:
            if QQSettings.load(self.config_path).auto_connect:
                if self._qq_managed_login:
                    self.navigate('QQ接入')
                    self._on_qq_login_start()
                else:
                    self._on_qq_connect()
        except Exception:
            self._log("QQ 自动连接未启动，请检查 QQ 接入设置。")

    def _qq_controller(self):
        from .qq_account import QQAccountController
        if self.qq_account_controller is None:
            self.qq_account_controller = QQAccountController(self.config_path) if self._astrbot_device_mode_enabled() else QQAccountController(self.config_path,
                start_login=self._on_qq_login_start, stop_login=self._on_qq_login_stop)
        return self.qq_account_controller

    def _on_qq_manual_login(self):
        self._qq_controller().login()

    def _on_qq_auto_login(self):
        enabled = bool(self.qq_auto_login_var.get())
        self._qq_managed_login = enabled
        try:
            self._qq_controller().set_auto_login(enabled)
            from .qq_settings import QQSettings
            previous = QQSettings.load(self.config_path)
            self._save_qq_preferences(previous.auto_connect)
            self._log('启动 EXE 时自动登录已开启。' if enabled else '启动 EXE 时自动登录已关闭。')
        except Exception:
            self._log('自动登录设置未能保存。')

    def _on_qq_logout(self):
        try:
            self._qq_controller().cancel_login()
            if not self._astrbot_device_mode_enabled():self._on_qq_login_stop()
            self._log('助手 QQ 下线请求已提交；自动登录偏好保持不变。')
        except Exception:
            self._log('助手 QQ 下线请求未能完成。')
    def _on_qq_login_start(self):
        """Prepare NapCat and display its QQ QR code without leaving this page."""
        if self._astrbot_device_mode_enabled():
            self._qq_controller().login()
            self._qq_set_login_phase('starting','正在请求登录助手 QQ，扫码或缓存账号确认后由 AstrBot 接收消息。')
            self._log('助手 QQ 登录请求已提交。')
            return
        try:
            from .qq_onboarding import QQOnboarding
            if self.qq_onboarding is None:
                self.qq_onboarding = QQOnboarding(self.config_path)
            if self.qq_onboarding.running:
                self.qq_tabs.set('登录助手')
                if self._qq_login_phase == 'error':
                    self._on_qq_connect()
                elif self._qq_login_phase == 'idle':
                    self.qq_login_hint.configure(text='上一段准备正在取消，请稍后再点击开始。')
                return
            self._qq_managed_login = True
            if not self.qq_token_var.get().strip():
                self.qq_token_var.set(_secrets.token_urlsafe(32))
            config = self._save_qq_preferences(True)
            self.qq_tabs.set('登录助手')
            self._qq_set_login_phase('preparing', '正在准备登录服务，进度和输出会显示在本页。')
            self.qq_onboarding.start(webui_url=config.webui_url,
                webui_token=self.qq_webui_token_var.get().strip(),
                onebot_url=config.ws_url, onebot_token=self.qq_token_var.get().strip(),
                launcher_path=config.launcher_path, qq_path=config.qq_path, totp_code=self.qq_totp_var.get().strip())
            self.qq_totp_var.set('')
            self._log('QQ 登录流程已开始。')
        except Exception as exc:
            from .qq_onboarding import OnboardingError
            message = str(exc) if isinstance(exc, (OnboardingError, ValueError)) else '无法开始登录，请查看本页运行日志并重试。'
            self._qq_set_login_phase('error', message)
            self._log(message)

    def _on_qq_qr_refresh(self):
        if self._astrbot_device_mode_enabled():
            self._fetch_astrbot_login_qr(refresh=True)
            return
        if self.qq_onboarding is not None:
            self.qq_onboarding.refresh_qrcode()
            self._log('正在刷新 QQ 登录二维码…')

    def _on_qq_login_stop(self):
        self._on_qq_disconnect()
        if self.qq_onboarding is not None:
            self.qq_onboarding.stop(stop_service=True)
        self._qq_clear_login_qr('登录服务已停止')
        self._qq_set_login_phase('idle', '点击“准备并显示登录码”可重新开始。')
        self._log('已停止本程序启动的 QQ 登录服务。')

    def _on_qq_pick_launcher(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(parent=self.root, title='选择已有 NapCat 启动文件',
            filetypes=[('NapCat 启动文件', '*.bat *.cmd *.exe *.ps1'), ('所有文件', '*.*')])
        if path:
            self.qq_launcher_var.set(path)
            self._on_qq_save_settings()

    def _on_qq_save_settings(self):
        from .qq_settings import QQSettings
        try:
            previous = QQSettings.load(self.config_path)
            self._save_qq_preferences(previous.auto_connect)
            self._log('QQ 接入设置已保存。')
        except ValueError as exc:
            self.qq_login_hint.configure(text=str(exc))
            self._log(str(exc))
        except Exception:
            self._log('QQ 接入设置未能保存，请检查文件夹权限。')

    def _on_qq_pick_qq(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(parent=self.root, title='选择电脑上已安装的 QQ.exe',
            filetypes=[('桌面 QQ', 'QQ.exe'), ('可执行文件', '*.exe')])
        if path:
            self.qq_qq_path_var.set(path)
            self._on_qq_save_settings()

    def _on_qq_clear_logs(self):
        self.qq_log_text.configure(state='normal')
        self.qq_log_text.delete('1.0', 'end')
        self.qq_log_text.configure(state='disabled')
        self.qq_recent_log.configure(text='日志显示已清空，后续记录会继续更新。')

    def _qq_clear_login_qr(self, text):
        self._qq_last_qrcode = ''
        self._qq_webui_auth_retry_at = 0.0
        self._qq_webui_auth_backoff = 1.0
        canvas = self.qq_qr_canvas
        canvas.delete('all')
        size = self.qq_qr_size
        canvas.create_text(size // 2, size // 2, text=text, width=size - 24,
            font=('Microsoft YaHei UI', 11), fill='#7B8D83', justify='center')

    def _qq_set_login_phase(self, phase, detail=''):
        from .desktop_components import GREEN, MUTED
        self._qq_login_phase = phase
        progress = {'idle': 0, 'preparing': .12, 'starting': .3, 'auth_required': .35,
            'waiting_qrcode': .5, 'qrcode_scanned': .65, 'initializing': .75,
            'configuring': .85, 'connecting': .92, 'connected': 1}
        titles = {'idle': '尚未开始', 'preparing': '正在准备登录组件', 'starting': '正在启动登录服务',
            'auth_required': '需要验证已有服务', 'waiting_qrcode': '请用手机 QQ 扫码',
            'qrcode_scanned': '已扫码，请在手机上确认', 'initializing': '登录成功，正在初始化',
            'configuring': '正在自动接通消息', 'connecting': '正在确认消息通道',
            'connected': '已登录并接通消息', 'error': '登录流程需要处理'}
        amount = progress.get(phase, 0)
        self.qq_login_progress.set(amount)
        self.qq_login_progress_label.configure(text=titles.get(phase, '正在等待登录服务'))
        if detail:
            self.qq_login_hint.configure(text=detail)
        for index, (control, text) in enumerate(zip(self.qq_login_steps,
                ('准备电脑登录服务', '手机扫码并确认登录', '自动接通消息通道'))):
            complete = amount >= (.5, .75, 1)[index]
            active = not complete and index == (0 if amount < .5 else 1 if amount < .75 else 2)
            control.configure(text=('✓  ' if complete else '●  ' if active else '○  ') + text,
                text_color=GREEN if complete or active else MUTED)
        busy = phase not in ('idle', 'error', 'auth_required')
        self.qq_login_start_btn.configure(state='disabled' if busy else 'normal',
            text='重试登录' if phase in ('error', 'auth_required') else '准备并显示登录码')
        can_refresh = phase in ('waiting_qrcode', 'qrcode_scanned')
        self.qq_qr_refresh_btn.configure(state='normal' if can_refresh else 'disabled')
        self.qq_stop_login_btn.configure(state='normal' if self.qq_onboarding is not None else 'disabled')

    def _poll_qq_onboarding(self):
        if self.qq_onboarding is None:
            return
        for event in self.qq_onboarding.poll_events():
            kind = event.get('type')
            if kind == 'log':
                self._log(event.get('message', ''))
            elif kind == 'progress':
                if event.get('detail'):
                    self.qq_login_progress_label.configure(text=event['detail'])
            elif kind == 'phase':
                self._qq_set_login_phase(event['phase'], event.get('detail', ''))
                if event['phase'] in ('initializing', 'configuring'):
                    self._qq_clear_login_qr('登录已确认，正在接通消息')
            elif kind == 'qrcode':
                url = event.get('url', '')
                if url and url != self._qq_last_qrcode:
                    matrix = qr_matrix(url)
                    if matrix is not None:
                        draw_qr_on_canvas(self.qq_qr_canvas, matrix, self.qq_qr_size)
                        self._qq_last_qrcode = url
                        self.qq_qr_caption.configure(text='用手机 QQ 扫码并确认登录')
                    else:
                        self._qq_set_login_phase('error', '二维码组件不可用，请重新安装最新版程序。')
            elif kind == 'configured':
                if event.get('ws_url'):
                    self.qq_url_var.set(event['ws_url'])
                self._qq_clear_login_qr('助手已登录')
                self.qq_qr_caption.configure(text='正在确认消息连接')
                self._qq_set_login_phase('connecting', '登录已完成，正在确认助手账号与消息通道。')
                self._on_qq_connect()
            elif kind == 'error':
                self._qq_clear_login_qr('登录需要处理')
                self._qq_set_login_phase('auth_required' if event.get('auth_required') else 'error', event['message'])
                self._log(event['message'])
                if event.get('auth_required'):
                    self.qq_tabs.set('高级设置')

    def _on_qq_connect(self):
        if self._astrbot_device_mode_enabled():
            self._log("AstrBot接入模式已启用，请在AstrBot连接QQ。")
            return
        try:
            self._save_qq_preferences(True)
            if self.host_obj is None:
                self._qq_connect_pending = True
                self.qq_status_label.configure(text="正在启动主机…")
                self._on_start()
                if self.server_thread is None or not self.server_thread.is_alive():
                    self._qq_connect_pending = False
                    self.qq_status_label.configure(text="请先完成总览账号与模型设置")
                    if self._qq_managed_login:
                        self._qq_set_login_phase('error', '助手登录流程可在本页完成；接收消息还需先保存主人 QQ 和模型设置，然后重试连接。')
                return
            if self.qq_connection is not None:
                self.qq_connection.stop()
                self.qq_connection = None
            self._apply_host_settings()
            from .qq_connection import QQConnection
            self.qq_connection = QQConnection(self.host_obj, self.qq_url_var.get().strip(),
                access_token=self.qq_token_var.get().strip(), auto_reply=bool(self.qq_auto_reply_var.get()))
            self.qq_connection.start()
            self._log("QQ 消息连接已启动。")
        except Exception:
            if self.qq_connection is not None:
                self.qq_connection.stop()
            self.qq_connection = None
            self._qq_connect_pending = False
            self.qq_status_label.configure(text="无法启动 QQ 连接")
            self.qq_detail_label.configure(text="请检查 WebSocket 地址、主机设置与 Token。")
            self._log("QQ 连接启动失败，请检查接入配置。")

    def _on_qq_disconnect(self):
        if self.qq_onboarding is not None:
            self.qq_onboarding.stop(stop_service=False)
        self._qq_connect_pending = False
        if self.qq_connection is not None:
            self.qq_connection.stop()
            self.qq_connection = None
        try: self._save_qq_preferences(False)
        except Exception: pass
        self.qq_status_label.configure(text="已断开 QQ")
        self.qq_account_label.configure(text="尚未读取助手账号")
        self.qq_detail_label.configure(text="点击连接可重新接入。")
        if self._qq_managed_login:
            self._qq_set_login_phase('idle', '消息处理已停止。登录服务仍可保留，点击准备登录可重新接入。')
        self._log("QQ 消息连接已断开。")

    def _on_qq_auto_reply(self):
        enabled = bool(self.qq_auto_reply_var.get())
        if self.qq_connection is not None:
            self.qq_connection.set_auto_reply(enabled)
        try:
            from .qq_settings import QQSettings
            previous = QQSettings.load(self.config_path)
            self._save_qq_preferences(previous.auto_connect)
        except Exception:
            self._log("自动回复开关未能保存，下次启动请重新检查。")
        self._log("QQ 自动处理已开启。" if enabled else "QQ 自动处理已暂停，不执行新消息或设备指令。")

    def _fetch_astrbot_login_qr(self,refresh=False):
        if getattr(self,'_qq_qr_fetching',False):return
        if time.monotonic()<getattr(self,'_qq_webui_auth_retry_at',0.0):return
        self._qq_qr_fetching=True
        def worker():
            try:
                from .napcat_webui import NapCatWebUIClient
                from .qq_settings import QQSettings
                path=pathlib.Path(self.config_path).resolve().parent/'qq-data'/'NapCat'/'config'/'webui.json'
                token=json.loads(path.read_text(encoding='utf-8-sig')).get('token','')
                client=getattr(self,'_qq_webui_client',None)
                if client is None:
                    client=NapCatWebUIClient(QQSettings.load(self.config_path).webui_url,timeout=3)
                    client.authenticate(token)
                    self._qq_webui_client=client
                    self._qq_webui_auth_backoff=1.0
                    self._qq_webui_auth_retry_at=0.0
                if refresh:client.refresh_qrcode()
                status=client.login_status();url=status.get('qr_url','')
                def apply():
                    self._qq_qr_fetching=False
                    if url and not status.get('logged_in') and url!=self._qq_last_qrcode:
                        matrix=qr_matrix(url)
                        if matrix is not None:
                            draw_qr_on_canvas(self.qq_qr_canvas,matrix,self.qq_qr_size)
                            self._qq_last_qrcode=url
                            self._qq_set_login_phase('waiting_qrcode','请使用手机 QQ 扫码并确认。')
                self.root.after(0,apply)
            except Exception:
                self._qq_webui_client=None
                self._qq_webui_auth_retry_at=time.monotonic()+self._qq_webui_auth_backoff
                self._qq_webui_auth_backoff=min(self._qq_webui_auth_backoff*2.0,60.0)
                self._qq_qr_fetching=False
        threading.Thread(target=worker,daemon=True).start()

    def _poll_astrbot_account(self):
        from .desktop_components import GREEN,MUTED
        status=self._qq_controller().status()
        online=bool(status.get('online'))
        self.qq_status_label.configure(text='已登录' if online else '未登录',text_color=GREEN if online else MUTED)
        self.qq_account_label.configure(text=str(status.get('account') or '助手 QQ 未登录'))
        self.qq_auto_login_var.set(bool(status.get('auto_login')))
        self.qq_detail_label.configure(text=status.get('error') or ('消息由 AstrBot 接收。' if online else '手机服务和 AstrBot 继续运行。'))
        if online:
            self._qq_set_login_phase('connected','助手 QQ 已登录，消息由 AstrBot 接收。')
            if self._qq_last_qrcode:self._qq_clear_login_qr('助手已登录')
        elif status.get('logout_requested'):
            self._qq_clear_login_qr('助手 QQ 已下线' if not status.get('online') else '正在下线')
            self._qq_set_login_phase('idle','点击登录按钮可重新上线。')
        elif status.get('login_in_progress'):
            self._fetch_astrbot_login_qr()

    def _poll_tray_events(self):
        if self._tray is None:
            return False
        import queue
        try:
            while True:
                event = self._tray.events.get_nowait()
                if event == "show":
                    self.root.deiconify()
                    self.root.lift()
                elif event == "exit":
                    self._exit_application()
                    return True
        except queue.Empty:
            return False
    def _poll_qq(self):
        if self._closing:
            return
        if self._poll_tray_events():
            return
        if self._astrbot_device_mode_enabled():
            self._poll_astrbot_account()
            self.root.after(1000,self._poll_qq)
            return
        if self._closing: return
        try:
            self._poll_qq_onboarding()
            if self.qq_connection is not None:
                state = self.qq_connection.snapshot()
                labels = {"stopped":"已断开 QQ", "connecting":"正在连接 QQ…", "connected":"QQ 已连接",
                    "reconnecting":"QQ 连接中断，正在重连…", "offline":"助手 QQ 未登录或已离线", "error":"QQ 连接异常"}
                self.qq_status_label.configure(text=labels.get(state["state"], "等待 QQ 连接"))
                account = str(state.get("account_id") or "")
                self.qq_account_label.configure(text=(f"{state.get('nickname') or '助手账号'}（{account}）" if account else "尚未读取助手账号"))
                self.qq_detail_label.configure(text=state.get("last_error") or
                    (("已连接，正在自动处理新消息。" if state.get("auto_reply") else "消息通道已连接，自动回复已暂停。") if state["state"] == "connected" else "请确认 NapCat 已登录 QQ 并启用正向 WebSocket。"))
                self.qq_activity_label.configure(text=f"已接收 {state.get('received',0)} 条 / 已回复 {state.get('replied',0)} 条")
                if self._qq_managed_login and state['state'] == 'connected' and self._qq_login_phase != 'connected':
                    self._qq_clear_login_qr('助手已登录')
                    self._qq_set_login_phase('connected', '助手已登录。开启上方自动回复后，即可接收和处理新消息。')
                    self.qq_qr_caption.configure(text='助手已登录并接通消息')
                if state["state"] != self._last_qq_state:
                    self._last_qq_state = state["state"]
                    self._log(labels.get(state["state"], state["state"]))
            active = self.qq_connection is not None or self._qq_connect_pending
            self.qq_connect_btn.configure(state="disabled" if active else "normal")
            self.qq_disconnect_btn.configure(state="normal" if active else "disabled")
            if self._tray is not None:
                import queue
                try:
                    while True:
                        event = self._tray.events.get_nowait()
                        if event == "show": self.root.deiconify(); self.root.lift()
                        elif event == "exit": self._exit_application(); return
                except queue.Empty: pass
        except Exception:
            self.qq_detail_label.configure(text="连接状态读取失败，请断开后重试。")
        finally:
            if not self._closing:
                self.root.after(400, self._poll_qq)

    def _hide_to_tray(self):
        try:
            if self._tray is None:
                from .desktop_tray import DesktopTray
                self._tray = DesktopTray()
                self._tray.start()
            self.qq_runtime_label.configure(text="关闭窗口后将在系统托盘继续运行；从托盘菜单可退出。")
            self.root.withdraw()
        except Exception:
            self._tray = None
            self.show_error("后台运行不可用", "无法创建系统托盘图标，窗口将保持打开。")

    def _exit_application(self):
        self._closing = True
        self._qq_connect_pending = False
        if self.qq_connection is not None:
            self.qq_connection.stop()
        if self.qq_onboarding is not None:
            self.qq_onboarding.stop(stop_service=True)
        self.server_stop.set()
        if self.host_obj is not None:
            self.host_obj.stop()
        if self._tray is not None:
            self._tray.stop()
        self.root.destroy()

    # -- UI 构建 ---------------------------------------------------------

    def _build_ui(self) -> None:
        from .desktop_ui import build
        build(self)

    # -- 日志 ------------------------------------------------------------

    def _log(self, message: str) -> None:
        safe = str(message).rstrip()
        import re
        safe = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', safe)
        for name in ('qq_token_var', 'qq_webui_token_var', 'model_key_var', 'signing_secret_var', 'qq_totp_var'):
            variable = getattr(self, name, None)
            secret = variable.get().strip() if variable is not None else ''
            if secret:
                safe = safe.replace(secret, '[已隐藏]')
        for box in (getattr(self, 'log_text', None), getattr(self, 'qq_log_text', None)):
            if box is None:
                continue
            at_bottom = box.yview()[1] >= .98
            box.configure(state=tk.NORMAL)
            box.insert(tk.END, safe + "\n")
            lines = int(box.index('end-1c').split('.')[0])
            if lines > 1000:
                box.delete('1.0', f'{lines - 1000 + 1}.0')
            if at_bottom:
                box.see(tk.END)
            box.configure(state=tk.DISABLED)
        if hasattr(self, 'qq_recent_log'):
            latest = safe.splitlines()[-1] if safe else ''
            if latest.startswith('配置文件不存在：'):
                latest = '首次使用：请先保存主人 QQ 和模型配置。'
            self.qq_recent_log.configure(text=latest[:75])

    # -- 配置读写 --------------------------------------------------------

    def _load_config_into_form(self) -> None:
        # 先把已保存的密钥读回界面：用户不该每次重启都要重新粘一遍 Key。
        self._reload_secrets_into_form()
        if not os.path.isfile(self.config_path):
            self._log(f"配置文件不存在：{self.config_path}（填写后点「保存配置」会创建）")
            self._refresh_pairing_placeholder()
            return
        try:
            config = HostConfig.load(self.config_path)
        except Exception as exc:
            self._log(f"配置读取失败：{exc}")
            self._refresh_pairing_placeholder()
            return
        form = FormState.from_config(config)
        self.owner_var.set(form.owner_user_id)
        self.model_base_var.set(form.model_base_url)
        self.model_id_var.set(form.model_id)
        self._set_allowed_packages(split_packages(form.allowed_packages))
        self._log("配置已载入。")

    def _reload_secrets_into_form(self) -> None:
        """把 `secrets.json` 里的密钥读回界面控件（已保存的 Key 不必每次重填）。"""
        try:
            stored = StoredSecrets.load(self.config_path)
        except Exception as exc:
            self._log(f"密钥文件读取失败：{exc}")
            return
        self.model_key_var.set(stored.model_api_key)
        self.signing_secret_var.set(stored.signing_secret)
        # 首次使用且没有签名密钥 → 自动生成一个，避免用户卡在「缺少签名密钥」。
        if stored.ensure_signing_secret():
            self.signing_secret_var.set(stored.signing_secret)
            stored.save(self.config_path)
            self._log("已自动生成设备签名密钥（无需手动设置，已保存到 secrets.json）")
            ensure_secrets_ignored(pathlib.Path(self.config_path).resolve().parent / ".gitignore")

    def _persist_secrets_from_form(self) -> pathlib.Path:
        """把界面上的密钥写进 `secrets.json`，并注入当前进程环境变量。

        注入之后 `HostConfig.signing_secret()` / `model_api_key()` 照旧从环境变量读——
        核心包一行都不用改，安全边界（明文不进 `host.json`）也保持不变。
        """
        stored = StoredSecrets.load(self.config_path)
        stored.model_api_key = self.model_key_var.get().strip()
        stored.signing_secret = self.signing_secret_var.get().strip()
        if not stored.signing_secret:
            stored.signing_secret = generate_signing_secret()
            self.signing_secret_var.set(stored.signing_secret)
        saved = stored.save(self.config_path)
        applied = stored.apply_to_environ()
        ensure_secrets_ignored(pathlib.Path(self.config_path).resolve().parent / ".gitignore")
        if applied:
            self._log(f"密钥已就绪（注入环境变量：{', '.join(applied)}；值不显示）")
        else:
            # 环境变量里已经有值 → 说明用户显式设过，尊重它。
            self._log("密钥已保存（当前进程已有同名环境变量，保持不变）")
        return saved

    def _current_form(self) -> FormState:
        # 界面只收集「用户该填的 4 项」；其余（device_id、监听地址、端口、
        # NapCat 地址、签名密钥变量名）全部由程序决定合理默认值——
        # 它们要么不该由用户决定（监听地址/端口），要么程序能自己定（device_id）。
        #
        # ⚠️ `device_id` **必须**在这里补上。`HostConfig` 要求它匹配
        # `[A-Za-z0-9._:-]{3,80}`，而 `FormState` 的默认值是空串——
        # 早先把输入框删掉后忘了补默认值，于是每次「保存配置」都被拒绝：
        #   device_id must match [A-Za-z0-9._:-]{3,80}
        # 用户看到的是一条英文正则，完全不知道该怎么办（而且他也没有这个输入框）。
        return FormState(
            owner_user_id=self.owner_var.get(),
            device_id=self._current_device_id(),
            allowed_packages=self._selectedOrTypedPackages(),
            model_base_url=self.model_base_var.get(),
            model_id=self.model_id_var.get(),
        )

    def _current_device_id(self) -> str:
        """取本机要用的 `device_id`：沿用已保存的，没有就生成一个合法的。

        为什么要沿用：手机扫码后会把自己生成的 `device_id` 同步给主机；
        如果这里每次随机生成一个新的，双方就对不上了（主机记录的是旧 id，
        帧里带的是新 id，`device_register` 会判成"未知设备"）。
        """
        existing = ""
        if os.path.isfile(self.config_path):
            try:
                existing = HostConfig.load(self.config_path).device_id.strip()
            except Exception:
                existing = ""
        if existing:
            return existing
        # 生成一个合法且够随机的 id（规则 `[A-Za-z0-9._:-]{3,80}`）。
        alphabet = "abcdefghijkmnpqrstuvwxyz23456789"
        suffix = "".join(_secrets.choice(alphabet) for _ in range(8))
        return f"device-{suffix}"

    def _on_save(self) -> None:
        self._save_config(show_dialog=True)

    def _on_save_quiet(self) -> bool:
        """保存配置但不弹错误框（供自动化验证与内部调序用）。

        与 `_on_save` 共用同一份 `_save_config`，避免两处校验规则漂移。
        """
        return self._save_config(show_dialog=False)

    def _save_config(self, *, show_dialog: bool) -> bool:
        """保存配置与密钥；返回是否成功。

        `show_dialog=False` 时不弹窗、只写日志——自动化验证需要这个，
        否则脚本会被模态对话框卡死。
        """
        self._last_save_error = ""
        form = self._current_form()
        try:
            # 先构造 `HostConfig` 做校验，**校验通过才落盘**，避免写出一个自己都读不回来的文件。
            HostConfig.from_mapping(form.to_mapping())
        except Exception as exc:
            if show_dialog:
                self.show_error("配置有问题", str(exc))
            self._last_save_error = "请先在总览填写 QQ 号，并完成模型设置。"
            self._log(f"保存被拒绝：{exc}")
            return False
        mapping = form.to_mapping()
        if pathlib.Path(self.config_path).is_file():
            previous = json.loads(pathlib.Path(self.config_path).read_text(encoding="utf-8-sig"))
            previous.update({key: mapping[key] for key in ("owner_user_id", "device_id", "allowed_packages")})
            previous["model"].update({key: mapping["model"][key] for key in ("model_id", "base_url")})
            mapping = previous
        try:
            os.makedirs(os.path.dirname(self.config_path) or ".", exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as handle:
                json.dump(mapping, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        except Exception as exc:
            if show_dialog:
                self.show_error("保存失败", str(exc))
            self._last_save_error = "配置文件无法写入，请检查文件夹权限。"
            self._log(f"保存失败：{exc}")
            return False
        self._log(f"配置已保存：{self.config_path}")
        saved_secrets = self._persist_secrets_from_form()
        self._log(f"密钥已保存：{saved_secrets}")
        self._load_config_into_form()
        self._on_regen()
        self._apply_host_settings()
        return True
        return self._save_config(show_dialog=False)

    def _on_open_config(self) -> None:
        try:
            os.startfile(self.config_path)  # type: ignore[attr-defined]
        except Exception as exc:
            self.show_info("打开失败", f"{exc}\n\n路径：{self.config_path}")

    # -- 配对码 / 二维码 --------------------------------------------------

    def _refresh_pairing_placeholder(self) -> None:
        self.qr_canvas.delete("all")
        self.qr_canvas.create_text(131, 112, text="▣", fill="#238653", font=("Segoe UI", 42))
        self.qr_canvas.create_text(131, 174, text="配置完成后生成二维码", fill="#6D8076", font=("Microsoft YaHei UI", 10))
        self.qr_fallback.configure(
            text="还不能生成配对码：请先填好配置并点「保存配置」。",
        )
        self.pair_code_var.set("—")
        self.pair_url_var.set("—")
        self.pair_ttl_var.set("—")
        self.pair_fingerprint_var.set("—")

    def _on_regen(self) -> None:
        """重新生成一次性配对码，并画二维码。"""
        if not os.path.isfile(self.config_path):
            self._refresh_pairing_placeholder()
            return
        try:
            config = HostConfig.load(self.config_path)
        except Exception as exc:
            self._log(f"配置读取失败：{exc}")
            self._refresh_pairing_placeholder()
            return

        server = config.device_server
        # 二维码里的 scheme **必须与实际监听协议一致**，而且优先安全。
        #
        # 手机端有一条硬校验：`host` 必须是 `wss://`，**只有回环地址**才允许 `ws://`
        # （见 `QrPayload.kt` 的 `ERROR_HOST_NOT_WSS`）。那条校验是对的——
        # 配对码是外部输入，不能因为「是主机自己给的」就放行明文传输。
        #
        # 所以电脑端不能「照实输出 ws://」了事：只要监听在非回环地址上，
        # 就必须先把证书准备好，二维码里出的永远是 `wss://`。
        # 实测踩过：证书还没生成时生成二维码 → 手机扫到 `ws://192.168.x.x`
        # → 被手机端正确拒绝（「主机地址必须用加密的 wss://」）。
        is_loopback = server.host in ("127.0.0.1", "localhost", "::1", "[::1]")
        if not is_loopback and not server.uses_tls:
            # 先补证书（会写回配置），再重新加载，让 scheme 变成 wss。
            hint = self._ensure_tls_certificate()
            if hint:
                self._log(hint)
            try:
                config = HostConfig.load(self.config_path)
                server = config.device_server
            except Exception as exc:
                self._log(f"补证书后重新加载配置失败：{exc}")

        scheme = "wss" if server.uses_tls else "ws"

        # ⚠️ 回环监听时**也不能**给出「局域网 IP + ws」的组合。
        #
        # phone_facing_urls(bind_host="127.0.0.1") 会退化成「找出本机局域网 IP」，
        # 于是拼出 ws://192.168.1.100:8765/device——那个 IP 不是回环地址，
        # 手机端会按 ERROR_HOST_NOT_WSS 正确拒绝。
        # 语义上也自相矛盾：主机只听回环，手机却拿到局域网地址，本来就连不上。
        #
        # 所以回环监听时**只给回环地址**（ws://127.0.0.1 仅同机试验能用，
        # 但语义诚实：手机连不到时界面会说「本机试验」而不是给个假地址）。
        if is_loopback:
            port = int(server.port or 0)
            if port <= 0:
                self.qr_canvas.delete("all")
                self.qr_fallback.configure(text="端口设为 0 时无法预先给出地址。")
                return
            urls = [f"{scheme}://127.0.0.1:{port}/device"]
            self._render_pairing(urls, scheme)
            return

        if scheme == "ws" and not is_loopback:
            # 兜底：证书仍不可用（例如缺 cryptography）时**不生成明文二维码**。
            self.qr_canvas.delete("all")
            self.qr_fallback.configure(
                text=(
                    "无法生成配对二维码：监听在局域网地址上必须启用 wss 加密，"
                    "但当前没有可用证书。\n\n"
                    "请在「高级设置」里配置 certfile / keyfile，"
                    '或安装 cryptography 后重试（pip install "cryptography>=42,<46"）。'
                ),
            )
            self.pair_code_var.set("—")
            self.pair_url_var.set("—")
            self.pair_ttl_var.set("—")
            self.pair_fingerprint_var.set("—")
            return
        port = int(server.port or 0)
        if port <= 0:
            self.qr_canvas.delete("all")
            self.qr_fallback.configure(
                text="端口设为 0 时无法预先给出地址。请在配置里填一个固定端口（例如 8765）。",
            )
            return

        urls = phone_facing_urls(
            bind_host=server.host,
            port=port,
            path="/device",
            scheme=scheme,
        )
        if not urls:
            self.qr_canvas.delete("all")
            self.qr_fallback.configure(
                text="没找到本机局域网 IP：请确认电脑已连上 WiFi/网线。",
            )
            return

        self._render_pairing(urls, scheme)

    def _render_pairing(self, urls: list[str], scheme: str) -> None:
        """根据已确定的地址列表生成配对码并画二维码。

        **地址与 scheme 由调用方定死**（`_on_regen` 已保证「非回环必 wss」、
        「回环只给回环地址」），本方法只负责渲染，不再做协议判断——
        两处判断逻辑重复会漂移，那是安全边界的隐患。
        """
        if not urls:
            self.qr_canvas.delete("all")
            self.qr_fallback.configure(text="没有可用的连接地址：请检查监听设置。")
            return

        # 主机指纹**必须用真实值**，不能是全零占位。
        #
        # 全零会让手机确认页显示「主机指纹：0000...0000」——用户核对毫无意义，
        # 更糟的是它会让人误以为「核对过了」，而实际没有任何身份被验证。
        #
        # 真实指纹由签名密钥派生（`make_host_identity_from_seed`），
        # 与 `pairing_endpoint` 启动时用的**完全同一套算法**，所以两端算出的值必然一致。
        host_fingerprint = self._host_fingerprint()
        if not host_fingerprint:
            self.qr_canvas.delete("all")
            self.qr_fallback.configure(
                text=(
                    "还不能生成配对码：缺少设备签名密钥。\n\n"
                    "请先点「保存配置」——它会自动生成签名密钥，再回来生成二维码。"
                ),
            )
            self.pair_code_var.set("—")
            self.pair_url_var.set("—")
            self.pair_ttl_var.set("—")
            self.pair_fingerprint_var.set("—")
            return

        # 展示的 token 必须与 HTTP 配对端点使用同一个持久库。
        pairing_db = os.path.join(os.path.dirname(os.path.abspath(self.config_path)), "pairing.sqlite3")
        connection = sqlite3.connect(pairing_db)
        try:
            store = PairingTokenStore(connection)
            record = store.issue(now_ms=int(time.time() * 1000), code_factory=default_code_factory())
        finally:
            connection.close()
        presentation = build_presentation(
            record=record,
            host_fingerprint=host_fingerprint,
            phone_urls=urls,
            cert_fingerprint=self._certificate_fingerprint(),
        )

        matrix = qr_matrix(presentation.qr_payload)
        if matrix is None:
            # `qrcode` 缺失：如实说明「画不出二维码」，并把可手输的信息给全。
            self.qr_canvas.delete("all")
            self.qr_fallback.configure(
                text=(
                    "没装 qrcode 库，无法画二维码（这不影响配对，可用手输）。\n"
                    "安装命令：pip install qrcode\n\n"
                    f"一次性 token：\n{presentation.token}"
                ),
            )
        else:
            self.qr_fallback.configure(text="")
            draw_qr_on_canvas(self.qr_canvas, matrix, QR_PIXEL_SIZE)

        self.pair_code_var.set(presentation.pairing_code)
        self.pair_url_var.set(presentation.phone_urls[0])
        self.pair_ttl_var.set("10 分钟（用一次即失效）")
        self.pair_fingerprint_var.set(host_fingerprint)
        note = "（本机回环试验：手机连不到这个地址，仅供同机调试）" if scheme == "ws" and "127.0.0.1" in urls[0] else ""
        self._log(f"已生成配对码 {presentation.pairing_code}（10 分钟内有效，一次有效）{note}")

    def _certificate_fingerprint(self) -> str:
        """读主机 TLS 证书的 SHA-256 指纹；没有证书时返回空串。

        这个值会进二维码的 `tfp` 字段，设备端用它做**证书固定**：
        只信任恰好这一张证书。这样自签名证书也能连上，而且比「信任所有证书」严格得多。

        为什么不用 `fp`：`fp` 是主机**身份**指纹（第 5 步人工核对「是不是这台主机」用），
        `tfp` 是**传输层**证书指纹。两者是不同层次的安全凭据，混用会让语义漂移。
        """
        try:
            # 每次都从磁盘读：证书路径是「启动/生成二维码时」写回配置的，
            # 用缓存的对象可能还没有那个路径（早先就踩过这个坑）。
            config = HostConfig.load(self.config_path)
            server = getattr(config, "device_server", None)
            certfile = str(getattr(server, "certfile", "") or "")
            if not certfile or not os.path.isfile(certfile):
                return ""
            from .pairing_code import certificate_fingerprint as _cert_fp

            return _cert_fp(certfile)
        except Exception:
            # 算不出指纹不阻断二维码生成：设备端会回退到严格校验，
            # 结果是「连不上」而不是「静默降级」——宁可连不上也不偷偷放宽。
            return ""

    def _host_fingerprint(self) -> str:
        """算出**真实**的主机指纹；没有签名密钥时返回空串。

        算法与 `pairing_endpoint` 启动时用的一致（`make_host_identity_from_seed`
        对签名密钥做 sha256 再 base64 编码），所以手机端拿到的值与主机运行时相同。

        为什么不用环境变量里的值、而是也读 `secrets.json`：
        用户在界面上填的密钥在「保存配置」时写进 `secrets.json` 并注入环境变量；
        但「重新生成配对码」可能在保存之前就点了——那时环境变量还是空的。
        """
        try:
            stored = StoredSecrets.load(self.config_path)
        except Exception:
            stored = None
        key = ""
        if stored is not None:
            key = stored.signing_secret.strip()
        if not key:
            key = os.environ.get(DEFAULT_SIGNING_ENV, "").strip()
        if not key:
            return ""
        try:
            from .pairing_endpoint import make_host_identity_from_seed

            return make_host_identity_from_seed(key.encode("utf-8")).fingerprint
        except Exception:
            return ""

    # -- 启动 / 停止 ------------------------------------------------------

    def _on_check_env(self) -> None:
        self._log("密钥环境变量检查（只显示是否就绪，不显示值）：")
        for name in (
            "WHALE_SIGNING_SECRET",
            "WHALE_MODEL_KEY",
            "NAPCAT_TOKEN",
        ):
            ready = bool(os.environ.get(name, "").strip())
            self._log(f"  {name}：{'已设置' if ready else '未设置'}")

    # -- 模型列表（按 URL 拉取，单选）--------------------------------------

    def _on_fetch_models(self) -> None:
        """按模型 URL 拉取模型列表，填进下拉框让用户单选。

        为什么值得做：模型 ID 必须与服务商实际提供的名字**逐字一致**，
        让用户凭记忆手打等于把「猜对一个字符串」的负担推给他。
        拉不到（服务商没实现该端点 / 没有 key / 网络不通）时**如实降级**：
        下拉框仍可自由输入，并明确告诉用户「可以手填」，不假装没有模型。
        """
        base_url = self.model_base_var.get().strip()
        if not base_url:
            self.show_info("先填模型 URL", "请先填写模型 URL（例如 https://api.openai.com）")
            return

        api_key_env = "WHALE_MODEL_KEY"
        # 把界面上刚填的 Key 立刻注入环境变量——用户填完就能拉，不必先保存。
        typed_key = self.model_key_var.get().strip()
        if typed_key:
            os.environ[api_key_env] = typed_key
        elif not os.environ.get(api_key_env, "").strip():
            self.model_hint.configure(
                text="需要模型 Key 才能拉列表：请在「② 模型 Key」里填写后重试（本地模型可留空）"
            )
            self._log("拉取模型列表需要 Key：请填写「② 模型 Key」")
            return
        self._log(f"正在从 {base_url} 拉取模型列表…")
        self.model_hint.configure(text="正在拉取…")

        def _work() -> None:
            from .model_client import ModelClientError, fetch_model_ids

            try:
                ids = fetch_model_ids(
                    api_key=lambda _p: os.environ.get(api_key_env, ""),
                    base_url=base_url,
                )
                self.root.after(0, lambda: self._on_models_fetched(ids))
            except ModelClientError as exc:
                self.root.after(0, lambda e=exc: self._on_models_failed(str(e)))
            except Exception as exc:  # 兜底：绝不因为拉列表失败而卡住界面
                self.root.after(
                    0, lambda e=exc: self._on_models_failed(f"{e.__class__.__name__}: {e}")
                )

        threading.Thread(target=_work, daemon=True).start()

    def _on_models_fetched(self, ids: list[str]) -> None:
        if not ids:
            self._on_models_failed("服务商返回了空列表")
            return
        self.model_id_combo.configure(values=ids)
        # 已选中的模型若在列表里就保留；否则选第一个（避免用户面对空选择框）。
        current = self.model_id_var.get().strip()
        if current not in ids:
            self.model_id_var.set(ids[0])
        self.model_hint.configure(text=f"已拉到 {len(ids)} 个模型，点下拉框选择")
        self._log(f"模型列表：{len(ids)} 个（已填进下拉框）")

    def _on_models_failed(self, reason: str) -> None:
        # 如实降级：下拉框仍可手填，提示写明原因。
        self.model_hint.configure(
            text=f"拉取失败：{reason}\n可以直接在上面手填模型 ID（例如 gpt-4o-mini）"
        )
        self._log(f"模型列表拉取失败：{reason}（可手填模型 ID）")

    # -- 应用列表（从手机拉取，多选）--------------------------------------

    def _poll_device_connection(self) -> None:
        if self._closing: return
        try:
            self._update_device_connection()
        except Exception as exc:
            self.fetch_apps_btn.configure(state=tk.DISABLED)
            self.app_count_label.configure(text=f"连接状态读取失败：{exc.__class__.__name__}")
        finally:
            self.root.after(500, self._poll_device_connection)

    def _update_device_connection(self) -> None:
        host = self.host_obj
        online = [] if host is None else [
            c.device_id for c in host.device_server.connections()
            if c.state == "ONLINE" and c.device_id
        ]
        # 只有已确认配对的身份才能成为控制目标。
        paired = set()
        db_path = pathlib.Path(self.config_path).resolve().parent / "pairing.sqlite3"
        if online and db_path.is_file():
            with sqlite3.connect(str(db_path)) as db:
                paired = {r[0] for r in db.execute(
                    "SELECT device_id FROM paired_devices WHERE state = ?", ("PAIRED_OFFLINE",)
                )}
        candidates = sorted(set(online) & paired)
        configured = host.config.device_id if host is not None else ""
        selected = configured if configured in candidates else candidates[0] if len(candidates) == 1 else ""
        previous = self._connected_device_id
        if selected and selected != configured:
            from dataclasses import replace
            path = pathlib.Path(self.config_path)
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            data["device_id"] = selected
            temp = path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temp, path)
            host.config = replace(host.config, device_id=selected)
            host.plugin.config = replace(host.plugin.config, device_id=selected)
        self._connected_device_id = selected
        if hasattr(self, "device_badge"):
            self.device_badge.set("手机已连接" if selected else "等待手机连接" if host else "主机未启动")
        self.fetch_apps_btn.configure(state=tk.NORMAL if selected and not self._fetching_apps else tk.DISABLED)
        if selected != previous:
            if selected:
                self.app_count_label.configure(text="手机已连接，可以拉取应用列表")
                self._log(f"手机已连接：{selected}")
            else:
                self.app_count_label.configure(text="手机已断开，等待重新连接")
                self._log("手机已断开")
        elif not selected:
            self.app_count_label.configure(text="等待已配对手机上报身份" if host else "先启动主机并连接手机")

    def _on_fetch_apps(self) -> None:
        """从**已连接的手机**拉取应用列表，填进多选列表框。

        走的是设备契约里已有的 `device.app.list` 命令：
        主机通过已配对的 WSS 通道下发，手机用 `PackageManager` 枚举可启动应用。
        手机没连上时按钮是灰的（初始 `state=DISABLED`），旁边写明
        「先启动并配对手机」——而不是让用户点了没反应。
        """
        host = self.host_obj
        self._update_device_connection()
        if host is None or not self._connected_device_id:
            self.show_info("手机还没连上", "请先点「启动主机」并让手机扫码配对。")
            return
        if self._fetching_apps:
            return
        self._fetching_apps = True
        self.fetch_apps_btn.configure(state=tk.DISABLED)
        self._log("正在从手机拉取应用列表…")
        self.app_count_label.configure(text="拉取中…")

        def _work() -> None:
            try:
                packages = self._request_app_list(host)
                self.root.after(0, lambda: self._on_apps_fetched(packages))
            except Exception as exc:
                self.root.after(
                    0, lambda e=exc: self._on_apps_failed(f"{e.__class__.__name__}: {e}")
                )

        threading.Thread(target=_work, daemon=True).start()

    def _request_app_list(self, host: Any) -> list[AppEntry]:
        """通过设备通道下发 `device.app.list` 并解析回包。

        应用名称用于展示，包名用于保存配置；兼容旧版手机的 packages 字段。
        """
        response = host.request_device("device.app.list", {}, device_id=self._connected_device_id)
        if response.status != "ok":
            raise RuntimeError(f"{response.error_code}: {response.message}")
        evidence = getattr(response, "evidence", None) or {}
        return app_entries(evidence)

    def _set_allowed_packages(self, packages: list[str]) -> None:
        self.app_picker.load(packages)
        self.allowed_text.delete("1.0", tk.END)

    def _on_apps_fetched(self, apps: list[AppEntry]) -> None:
        self._fetching_apps = False
        self._update_device_connection()
        if not apps:
            self._on_apps_failed("手机没有返回任何应用")
            return
        self._app_rows = apps
        self.app_picker.set_apps(apps)
        self.app_count_label.configure(text=f"已获取 {len(apps)} 个应用，勾选后保存")
        self._log(f"已获取 {len(apps)} 个应用")

    def _on_apps_failed(self, reason: str) -> None:
        self._fetching_apps = False
        self._update_device_connection()
        self.app_count_label.configure(text="拉取失败，可手填")
        self._log(f"应用列表拉取失败：{reason}（下方可手填包名）")

    def _selectedOrTypedPackages(self) -> str:
        """把「列表选中项」与「手填框」合并成一行一个的文本。

        两个来源都保留的理由：列表来自真实手机（准确），手填方便在没连手机时
        先把配置写好。合并时去重，避免同一包名出现两次。
        """
        picked = self.app_picker.packages()
        typed = split_packages(self.allowed_text.get("1.0", tk.END))
        merged: list[str] = []
        for name in list(picked) + typed:
            if name and name not in merged:
                merged.append(name)
        return "\n".join(merged)

    # -- 高级设置 ---------------------------------------------------------

    def _on_advanced(self) -> None:
        """把「用户不该操心」的项放进一个只读对话框，而不是摆满主界面。

        这些值仍写在配置文件里、也确实可改，但它们**不该是默认流程的一部分**。
        """
        try:
            config = HostConfig.load(self.config_path)
        except Exception:
            self.show_info("高级设置", "请先保存配置，才能查看高级项。")
            return
        onebot_url = "（未配置）"
        if config.onebot is not None:
            onebot_url = getattr(config.onebot, "http_url", "（未配置）")
        lines = [
            "以下项由程序自动决定，一般不需要改：",
            "",
            f"设备 ID（手机生成后自动同步）：{config.device_id}",
            f"设备服务端监听地址：{config.device_server.host}",
            f"设备服务端端口：{config.device_server.port}",
            f"配对端点端口：{int(config.device_server.port) + 1}",
            f"NapCat 地址：{onebot_url}",
            "",
            "密钥全部从环境变量读（界面不显示、不保存明文）：",
            f"  签名密钥：{config.signing_secret_env}",
            f"  模型 Key：{getattr(config, 'model_api_key_env', '') or '（未设置）'}",
            "",
            f"配置文件：{self.config_path}",
        ]
        self.show_info("高级设置（只读）", "\n".join(lines))

    def _ensure_tls_certificate(self) -> str:
        """确保证书存在（缺就自动生成自签名证书并写回配置），返回给用户看的说明。

        **为什么必须做**：界面默认监听 `0.0.0.0`（手机在局域网里要连它），
        而 `device_server` 有条硬闸门：非回环地址 + 无 TLS 直接拒绝启动
        （`plaintext ws:// is forbidden outside loopback`）。那条闸门是对的
        （M05 禁止明文 ws），但它让「点启动就报错」变成默认体验。

        对普通用户来说「自己去生成证书」门槛过高：要找 openssl、写 SAN、
        让手机信任。所以这里自动生成，并把「自签名不被信任」如实说清楚。
        """
        try:
            config = HostConfig.load(self.config_path)
        except Exception as exc:
            return f"跳过 TLS 检查（配置读取失败：{exc}）"

        server = config.device_server
        # 已有证书且文件都在 → 什么都不做。
        if server.uses_tls and not server.missing_tls_files():
            return ""
        # 声明了证书但文件缺失 → 交给用户（不擅自覆盖他的配置）。
        if server.missing_tls_files():
            return (
                "配置里声明了 TLS 证书但文件不存在："
                + "、".join(server.missing_tls_files())
                + "（请在「高级设置」里修正，或清空这两项以自动生成）"
            )
        # 回环地址 → 明文 ws 允许，不需要证书。
        if server.host in ("127.0.0.1", "localhost", "::1", "[::1]"):
            return ""

        from .tls_cert import CertificateError, default_certificate_paths, ensure_self_signed_certificate

        cert_path, key_path = default_certificate_paths(
            pathlib.Path(self.config_path).resolve().parent / ".certs"
        )
        try:
            generated = ensure_self_signed_certificate(certfile=cert_path, keyfile=key_path)
        except CertificateError as exc:
            return f"自动生成 TLS 证书失败：{exc}（请在「高级设置」里手工配置证书）"

        # 写回配置（只改这两个键，其它原样保留）。
        try:
            import json as _json

            raw = _json.loads(pathlib.Path(self.config_path).read_text(encoding="utf-8"))
            server_raw = raw.setdefault("device_server", {})
            server_raw["certfile"] = str(generated.certfile)
            server_raw["keyfile"] = str(generated.keyfile)
            pathlib.Path(self.config_path).write_text(
                _json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except Exception as exc:
            return f"证书已生成但写回配置失败：{exc}（文件在 {generated.certfile}）"

        note = "已自动生成自签名 TLS 证书（wss 加密已启用）"
        if generated.created:
            note += "；试验时需在手机上信任该证书，生产环境请换受信任 CA 签发的证书"
        return note

    def _on_start(self) -> None:
        if self.server_thread is not None and self.server_thread.is_alive():
            self._log("主机已在运行。")
            return
        if self._qq_connect_pending:
            if not self._on_save_quiet():
                return
        else:
            self._on_save()
        if not os.path.isfile(self.config_path):
            return
        try:
            config = HostConfig.load(self.config_path)
        except Exception as exc:
            self.show_error("配置有问题", str(exc))
            return

        # 密钥准备：**不再要求用户去设环境变量**。
        #
        # 之前这里会弹「缺少签名密钥，请设置环境变量」——普通用户做不到，
        # 等于把产品卡在最后一步。现在改成：
        #   1. 界面上填的模型 Key 写回 secrets.json 并注入环境变量；
        #   2. 签名密钥没有就**自动生成**（用户没能力判断「什么样的随机串算够安全」）；
        #   3. HostConfig 照旧从环境变量读，核心包一行不改。
        self._persist_secrets_from_form()
        secret_name = config.signing_secret_env
        if not os.environ.get(secret_name, "").strip():
            self.show_error(
                "签名密钥不可用",
                f"自动生成失败（环境变量 {secret_name} 仍为空）。\n"
                f"请检查 {self.config_path} 所在目录是否可写。",
            )
            return

        # TLS 证书：0.0.0.0 监听必须配证书
        #
        # 这里曾是一个真实阻塞点：界面默认监听 0.0.0.0（手机在局域网里要连它），
        # 而 device_server 有一条硬闸门——非回环地址 + 无 TLS 直接拒绝启动：
        #   plaintext ws:// is forbidden outside loopback; production must use wss://
        # 那条闸门是对的（M05 禁止明文 ws），但它让「点启动就报错」成为默认体验。
        #
        # 解法：没配证书就自动生成一对自签名证书并写回配置。
        # 如实告知用户：自签名证书不被手机信任链认可，试验时需在手机上手动信任。
        tls_hint = self._ensure_tls_certificate()
        if tls_hint:
            self._log(tls_hint)
            # 证书路径是**写回 host.json** 的，而上面那个 `config` 是写回**之前**
            # 加载的快照——它仍然 `certfile=""`。不重新加载就会用无证书配置
            # 在 `0.0.0.0` 上启动，被 device_server 的「非回环必须有 TLS」闸门拒绝
            # （表现：点「启动主机」立刻失败，日志里是 plaintext ws:// is forbidden）。
            try:
                config = HostConfig.load(self.config_path)
            except Exception as exc:
                self.show_error("配置读取失败", str(exc))
                return

        # 延迟导入：避免窗口构造阶段就把服务端依赖拉进来。
        from .pairing_endpoint import (
            build_pairing_service,
            make_host_identity_from_seed,
            start_pairing_server,
        )
        from .standalone import StandaloneHost

        self.server_stop.clear()
        self.start_btn.configure(state=tk.DISABLED)
        self.stop_btn.configure(state=tk.NORMAL)
        self._log("正在启动主机…")

        def _run() -> None:
            pairing_server = None
            rpc_server = None
            host = None
            try:
                from .storage import TaskStore
                host = StandaloneHost(config, store=TaskStore(str(pathlib.Path(self.config_path).resolve().parent / "runtime.sqlite3")))
                self._wire_device_requests(host)
                # 注意：`port=0` 时 start() 返回真实端口，这里必须用它更新显示。
                actual_port = host.start()

                # ---- 配对端点：设备扫码后要能真的把身份送到主机 ----
                #
                # 这一步之前是**完全缺失**的：设备端走完「扫码 + 生成身份」后
                # 只推进本地状态机，一个网络请求都不发；主机端 `pairing.py` 有完整的
                # `PairingService` 却没有任何 HTTP 端点去调它。结果就是「界面能走完，
                # 但主机根本不知道有设备来配对」。
                #
                # 这里用「主机的签名密钥」派生一个稳定的主机身份：
                # 同一台主机、同一个密钥 → 指纹永远不变（用户不必每次重新核对）。
                secret = os.environ.get(config.signing_secret_env, "").encode("utf-8")
                host_identity = make_host_identity_from_seed(secret)
                pairing_db = os.path.join(
                    os.path.dirname(os.path.abspath(self.config_path)),
                    "pairing.sqlite3",
                )
                service = build_pairing_service(pairing_db, host_identity=host_identity)
                # 配对端口固定为 WSS 端口 +1，便于用户理解与放行防火墙。
                pairing_port = int(config.device_server.port or 0)
                pairing_port = pairing_port + 1 if pairing_port > 0 else 0
                import ssl
                pairing_tls = None
                if config.device_server.uses_tls:
                    pairing_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    pairing_tls.load_cert_chain(config.device_server.certfile, config.device_server.keyfile)
                pairing_server, _thread, actual_pairing_port = start_pairing_server(
                    host=config.device_server.host,
                    port=pairing_port,
                    service=service,
                    host_identity=host_identity,
                    ssl_context=pairing_tls,
                )
                from .astrbot_native_bridge import AstrBotDeviceRPC, ensure_rpc_token
                rpc_server = AstrBotDeviceRPC(host, ensure_rpc_token(self.config_path), config_path=self.config_path)
                rpc_server.start()
                self.device_rpc = rpc_server

                self.host_obj = host
                self.root.after(
                    0,
                    lambda: self._on_started(host, actual_port, actual_pairing_port, host_identity),
                )
                while not self.server_stop.is_set():
                    time.sleep(0.2)
                if rpc_server is not None:
                    rpc_server.stop()
                    self.device_rpc = None
                if pairing_server is not None:
                    pairing_server.shutdown()
                    pairing_server.server_close()
                host.stop()
                self.root.after(0, self._on_stopped)
            except Exception as exc:  # 兜底：把真实原因写到界面，不吞掉
                if rpc_server is not None:
                    try:
                        rpc_server.stop()
                    except Exception:
                        pass
                    self.device_rpc = None
                if host is not None:
                    try:
                        host.stop()
                    except Exception:
                        pass
                if pairing_server is not None:
                    try:
                        pairing_server.shutdown()
                        pairing_server.server_close()
                    except Exception:
                        pass
                self.root.after(0, lambda e=exc: self._on_start_failed(e))

        self.server_thread = threading.Thread(target=_run, daemon=True)
        self.server_thread.start()

    def _on_started(
        self,
        host: Any,
        port: int,
        pairing_port: int,
        host_identity: Any,
    ) -> None:
        self._log(f"主机已启动：{host.device_url}（端口 {port}）")
        pairing_scheme = "https" if host.device_url.startswith("wss:") else "http"
        self._log(f"配对端点：{pairing_scheme}://127.0.0.1:{pairing_port}/pair/（局域网地址见二维码）")
        self._log(f"主机指纹：{host_identity.fingerprint}")
        self._log("AstrBot设备桥已就绪：127.0.0.1:8767（Bearer认证）")
        self._log("手机可以扫码配对了。")
        # 启动后刷新一次二维码：里面的地址与端口必须和实际监听一致。
        self._on_regen()
        if self._qq_connect_pending:
            self._qq_connect_pending = False
            self._on_qq_connect()

    def _on_start_failed(self, exc: Exception) -> None:
        self._qq_connect_pending = False
        self._log(f"启动失败：{exc}")
        self.show_error("启动失败", str(exc))
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)

    def _on_stop(self) -> None:
        if self.qq_onboarding is not None:
            self.qq_onboarding.stop(stop_service=False)
        if self.qq_connection is not None:
            self.qq_connection.stop()
            self.qq_connection = None
        self.server_stop.set()
        self._log("正在停止主机…")

    def _on_stopped(self) -> None:
        self.host_obj = None
        self.start_btn.configure(state=tk.NORMAL)
        self.stop_btn.configure(state=tk.DISABLED)
        self._log("主机已停止。")

    # -- 生命周期 ---------------------------------------------------------

    def run(self) -> None:
        self.root.mainloop()


def main(argv: list[str] | None = None) -> int:
    """命令行入口：`python -m astrbot_whale_device.host_gui [--config host.json]`"""
    args = list(sys.argv[1:] if argv is None else argv)
    config_path = ""
    index = 0
    while index < len(args):
        if args[index] == "--config" and index + 1 < len(args):
            config_path = args[index + 1]
            index += 2
            continue
        index += 1
    if not config_path:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "host.json")
        config_path = os.path.normpath(config_path)

    window = HostWindow(config_path)
    window.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())





