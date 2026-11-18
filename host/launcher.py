"""Windows desktop entry point for the embedded AstrBot dashboard."""
from __future__ import annotations
import argparse
import ctypes
import os
import sys
from typing import Any, Callable

APP_MUTEX_NAME = r"Local\WhaleAgentDesktop"
WINDOW_TITLE = "Whale · AstrBot"
ERROR_TITLE = "Whale · AstrBot 启动失败"
ERROR_DEPENDENCIES = "桌面运行组件不完整，请重新安装或联系管理员。"
ERROR_LEGACY = "旧版调试界面无法启动，请检查桌面组件是否完整。"
ERROR_STARTUP = "AstrBot 桌面窗口启动失败，请检查配置和运行组件。"

class SingleInstance:
    """Own the desktop mutex and route duplicate launches to the first window."""
    def __init__(self, win32: Any | None = None) -> None:
        self._win32 = win32 if win32 is not None else ctypes.WinDLL("kernel32", use_last_error=True)
        self.handle: Any = None
        self.is_primary = False

    def acquire(self) -> bool:
        create_mutex = self._win32.CreateMutexW
        create_mutex.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        create_mutex.restype = ctypes.c_void_p
        close_handle = self._win32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        ctypes.set_last_error(0)
        handle = create_mutex(None, False, APP_MUTEX_NAME)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.handle = handle
        self.is_primary = ctypes.get_last_error() != 183
        if not self.is_primary:
            close_handle(handle)
            self.handle = None
        return self.is_primary

    def close(self) -> None:
        if self.handle:
            self._win32.CloseHandle(self.handle)
            self.handle = None


def _base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "dist-exe"))


def _show_error(message: str) -> None:
    try:
        box = ctypes.WinDLL("user32", use_last_error=True).MessageBoxW
        box.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
        box.restype = ctypes.c_int
        box(None, message, ERROR_TITLE, 0x10)
    except Exception:
        pass


def _wake_existing_window(win32: Any | None = None) -> bool:
    api = win32 if win32 is not None else ctypes.WinDLL("user32", use_last_error=True)
    find = api.FindWindowW
    find.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    find.restype = ctypes.c_void_p
    hwnd = find(None, WINDOW_TITLE)
    if not hwnd:
        return False
    show = api.ShowWindow
    show.argtypes = [ctypes.c_void_p, ctypes.c_int]
    show.restype = ctypes.c_int
    show(hwnd, 9)  # SW_RESTORE
    foreground = api.SetForegroundWindow
    foreground.argtypes = [ctypes.c_void_p]
    foreground.restype = ctypes.c_int
    foreground(hwnd)
    return True


def _run(config_path: str, legacy_ui: bool, run_embedded: Callable[[str], Any] | None = None) -> int:
    if legacy_ui:
        from astrbot_whale_device.host_gui import HostWindow
        HostWindow(config_path).run()
    else:
        if run_embedded is None:
            from astrbot_whale_device.embedded_window import run_embedded
        run_embedded(config_path)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-ui", action="store_true", help="使用旧版 Tk 调试界面")
    args = parser.parse_args(argv)
    instance = None
    try:
        if os.name == "nt":
            instance = SingleInstance()
            if not instance.acquire():
                _wake_existing_window()
                return 0
        base = _base_dir()
        if not getattr(sys, "frozen", False):
            host_dir = os.path.dirname(os.path.abspath(__file__))
            if host_dir not in sys.path:
                sys.path.insert(0, host_dir)
        return _run(os.path.join(base, "host.json"), args.legacy_ui)
    except ImportError:
        _show_error(ERROR_LEGACY if args.legacy_ui else ERROR_DEPENDENCIES)
        return 1
    except Exception:
        _show_error(ERROR_LEGACY if args.legacy_ui else ERROR_STARTUP)
        return 1
    finally:
        if instance is not None:
            instance.close()

if __name__ == "__main__":
    raise SystemExit(main())
