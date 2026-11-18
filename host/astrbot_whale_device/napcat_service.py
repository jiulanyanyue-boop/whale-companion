"""Prepare and supervise a user-mode NapCat Windows Shell installation."""
from __future__ import annotations

import hashlib
import ctypes
from ctypes import wintypes
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

GITHUB_RELEASE_API = "https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest"
ASSET_NAME = "NapCat.Shell.Windows.Node.zip"
ASSET_NAMES = (ASSET_NAME, "NapCat.Shell.zip")
MAX_ARCHIVE_SIZE = 200 * 1024 * 1024
MAX_EXPANDED_SIZE = 600 * 1024 * 1024
MAX_FILES = 20000
EVENT_LIMIT = 500
LOG_LIMIT = 500


class NapCatService:
    """NapCat lifecycle helper. Long prepare_install work belongs on a UI worker thread."""

    def __init__(self, data_dir: str | os.PathLike):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.install_dir = self.data_dir / "napcat"
        self._events: queue.Queue[dict] = queue.Queue(maxsize=EVENT_LIMIT)
        self._seq = 0
        self._lock = threading.RLock()
        self._prepare_lock = threading.Lock()
        self._prepare_result: dict | None = None
        self._proc: subprocess.Popen | None = None
        self._owned_pid: int | None = None
        self._state = "idle"
        self._error = ""
        self._cancel = threading.Event()
        self._reader: threading.Thread | None = None
        self._external_running = False
        self._selected_launcher: Path | None = None
        self._job = None

    def _event(self, level: str, message: str) -> None:
        message = str(message)
        if re.search(r"(?i)(authorization\s*:|bearer\s+|credential|qrcode|qr[_ -]?url|scan.{0,15}qr|q\.qq\.com|ptlogin|qlogin)", message):
            message = "[NapCat login/credential output redacted]"
        elif re.search(r'''(?i)\b(?:token|ticket|authkey)\b["']?\s*[:=]''', message):
            message = "[NapCat token output redacted]"
        message = re.sub(r"(?i)(token\s*[:=]\s*)[^\s,;]+", r"\1[redacted]", message)
        message = re.sub(r"(?i)(qr|qrcode|ticket|authkey|credential)(\s*[:=]\s*)[^\s,;]+", r"\1\2[redacted]", message)
        message = re.sub(r"(?i)https?://[^\s]+", "[login URL redacted]", message)
        if message and re.fullmatch(r"[A-Za-z0-9+/=_-]{8,}", message):
            message = "[possible QR payload redacted]"
        if re.search(r'[▀▄█]{2}', message):
            message = '[登录二维码内容已隐藏]'
        message = message[:LOG_LIMIT]
        with self._lock:
            self._seq += 1
            item = {"seq": self._seq, "timestamp": time.time(), "level": level, "message": message}
            try:
                self._events.put_nowait(item)
            except queue.Full:
                try:
                    self._events.get_nowait()
                except queue.Empty:
                    pass
                self._events.put_nowait(item)

    def poll_events(self, limit: int = 100) -> list[dict]:
        result = []
        for _ in range(max(0, min(int(limit), EVENT_LIMIT))):
            try:
                result.append(self._events.get_nowait())
            except queue.Empty:
                break
        return result

    def _release_assets(self):
        request = urllib.request.Request(GITHUB_RELEASE_API, headers={"User-Agent": "WhaleAgentHost", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.load(response)
        assets = []
        for name in ASSET_NAMES:
            asset = next((item for item in release.get('assets', []) if item.get('name') == name), None)
            if not asset:
                raise RuntimeError('官方发行版缺少登录组件：' + name)
            if not asset.get('browser_download_url', '').startswith('https://github.com/NapNeko/NapCatQQ/releases/download/'):
                raise RuntimeError('官方发行版下载地址无效')
            if not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', asset.get('digest', '')):
                raise RuntimeError('官方发行包未提供有效的 SHA-256，未执行安装')
            if not 0 < asset.get('size', 0) <= MAX_ARCHIVE_SIZE:
                raise RuntimeError('官方登录组件体积不符合预期')
            assets.append(asset)
        return release.get('tag_name', 'unknown'), assets

    def _download_asset(self, asset, progress, cancel_event):
        cache = self.data_dir / '.napcat-downloads'
        cache.mkdir(parents=True, exist_ok=True)
        expected = asset['digest'].split(':')[1].lower()
        target = cache / (expected[:16] + '-' + asset['name'])
        part = target.with_suffix('.zip.part')
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
            return target
        for attempt in range(4):
            if self._cancel.is_set() or (cancel_event and cancel_event.is_set()):
                raise InterruptedError('准备已取消')
            downloaded = part.stat().st_size if part.exists() else 0
            if downloaded > asset['size']:
                part.unlink()
                downloaded = 0
            if downloaded == asset['size']:
                break
            headers = {'User-Agent': 'WhaleAgentHost', 'Range': f'bytes={downloaded}-'}
            try:
                with urllib.request.urlopen(urllib.request.Request(asset['browser_download_url'], headers=headers), timeout=25) as response:
                    partial = response.status == 206
                    if partial and not response.headers.get('Content-Range', '').startswith(f'bytes {downloaded}-'):
                        raise RuntimeError('登录组件续传位置不符合预期')
                    if not partial:
                        downloaded = 0
                    with part.open('ab' if partial else 'wb') as output:
                        while True:
                            if self._cancel.is_set() or (cancel_event and cancel_event.is_set()):
                                raise InterruptedError('准备已取消')
                            block = response.read(256 * 1024)
                            if not block:
                                break
                            downloaded += len(block)
                            if downloaded > asset['size']:
                                raise RuntimeError('登录组件大小超过官方标注，已取消安装')
                            output.write(block)
                            if progress:
                                progress({'phase': 'download', 'downloaded': downloaded, 'total': asset['size'], 'asset': asset['name']})
                if downloaded == asset['size']:
                    break
            except (OSError, TimeoutError):
                if attempt == 3:
                    raise
                self._event('info', '登录组件下载中断，正在从已有进度继续。')
        if not part.is_file() or part.stat().st_size != asset['size']:
            raise RuntimeError('登录组件下载未完成，下次重试会继续下载')
        if hashlib.sha256(part.read_bytes()).hexdigest() != expected:
            part.unlink()
            raise RuntimeError('登录组件 SHA-256 校验失败，未执行安装')
        part.replace(target)
        return target

    @staticmethod
    def _safe_extract(archive: Path, target: Path) -> None:
        root = target.resolve()
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_FILES:
                raise RuntimeError("安装包文件数量超过限制")
            if sum(i.file_size for i in infos) > MAX_EXPANDED_SIZE:
                raise RuntimeError("安装包展开体积超过限制")
            for info in infos:
                name = info.filename.replace("\\", "/")
                parts = Path(name).parts
                if name.startswith("/") or any(p in ("..", "") for p in parts) or (parts and ":" in parts[0]):
                    raise RuntimeError("安装包包含非法路径")
                destination = (root / Path(*parts)).resolve()
                if destination != root and root not in destination.parents:
                    raise RuntimeError("安装包路径超出目标目录")
                mode = (info.external_attr >> 16) & 0xF000
                if mode == 0xA000:
                    raise RuntimeError("安装包包含不支持的符号链接")
            zf.extractall(root)

    def prepare_install(self, progress: Callable[[dict], None] | None = None, cancel_event: threading.Event | None = None, *, archives=None) -> dict:
        """Download and extract the current official Windows Node release; safe to call repeatedly."""
        with self._prepare_lock:
            self._cancel.clear()
            if self._prepare_result and self._prepare_result.get("ok"):
                return dict(self._prepare_result)
            if self.install_dir.is_dir() and any(self.install_dir.rglob("napcat.mjs")):
                self._prepare_result = {"ok": True, "version": "existing", "install_dir": str(self.install_dir)}
                self._state = "installed"
                self._event("info", "检测到现有 NapCat 安装，保留原文件")
                return dict(self._prepare_result)
            if self.install_dir.exists():
                self._error = "目标目录已存在且不是可识别的 NapCat 安装，为避免覆盖已存在文件已停止准备"
                self._event("error", self._error)
                return {"ok": False, "error": self._error}
            self._state = "preparing"
            self._error = ""
            if self._cancel.is_set() or (cancel_event and cancel_event.is_set()):
                return {"ok": False, "error": "准备已取消"}
            self._event("info", "正在检查 NapCat 官方 Windows 发行版")
            stage = None
            try:
                tag, assets = self._release_assets()
                self.data_dir.mkdir(parents=True, exist_ok=True)
                sources = {}
                for asset in assets:
                    if archives and asset['name'] in archives:
                        source = Path(archives[asset['name']]).resolve()
                        if hashlib.sha256(source.read_bytes()).hexdigest() != asset['digest'].split(':')[1].lower():
                            raise RuntimeError('本地登录组件 SHA-256 校验失败')
                    else:
                        source = self._download_asset(asset, progress, cancel_event)
                    sources[asset['name']] = source
                if self._cancel.is_set() or (cancel_event and cancel_event.is_set()):
                    raise InterruptedError('准备已取消')
                stage = Path(tempfile.mkdtemp(prefix='napcat-stage-', dir=self.data_dir))
                self._safe_extract(sources[ASSET_NAME], stage)
                self._safe_extract(sources['NapCat.Shell.zip'], stage / 'napcat')
                required = ('node.exe', 'index.js', 'wrapper.node', 'package.json', 'config.json', 'napcat/napcat.mjs')
                if not all((stage / name).is_file() for name in required):
                    raise RuntimeError('官方登录组件缺少必要文件，未执行安装')
                if self._cancel.is_set() or (cancel_event and cancel_event.is_set()):
                    raise InterruptedError('准备已取消')
                stage.replace(self.install_dir)
                stage = None
                self._prepare_result = {"ok": True, "version": tag, "install_dir": str(self.install_dir)}
                self._state = "installed"
                self._event("info", f"NapCat {tag} 准备完成")
                if progress:
                    progress({"phase": "complete", "downloaded": 1, "total": 1})
                return dict(self._prepare_result)
            except Exception as exc:
                self._state = "error"
                self._error = "NapCat 准备失败"
                self._event("error", f"NapCat 准备失败：{exc.__class__.__name__}: {exc}")
                self._prepare_result = {"ok": False, "error": self._error}
                return dict(self._prepare_result)
            finally:
                if stage is not None and stage.parent == self.data_dir and stage.name.startswith('napcat-stage-'):
                    shutil.rmtree(stage, ignore_errors=True)

    def _find_qq(self) -> Path | None:
        candidates = []
        for key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(key)
            if base:
                candidates.extend((Path(base) / "Tencent/QQ/QQ.exe", Path(base) / "Tencent/QQNT/QQ.exe"))
        candidates.extend((Path.home() / "AppData/Local/Programs/QQ/QQ.exe", Path.home() / "AppData/Roaming/Tencent/QQNT/QQ.exe"))
        return next((p.resolve() for p in candidates if p.is_file()), None)

    def _find_launcher(self, requested: str | os.PathLike | None) -> Path | None:
        if requested:
            candidate = Path(requested).expanduser().resolve()
            if not candidate.is_file():
                return None
            if candidate.suffix.lower() == ".bat":
                candidate = candidate.with_name("NapCatWinBootMain.exe")
            return candidate if candidate.is_file() else None
        for name in ("node.exe", "NapCatWinBootMain.exe", "launcher-user.bat", "launcher-win10-user.bat"):
            found = next(self.install_dir.rglob(name), None) if self.install_dir.exists() else None
            if found:
                resolved = found.resolve()
                return resolved.with_name("NapCatWinBootMain.exe") if resolved.suffix.lower() == ".bat" else resolved
        return None

    def _onebot_port_open(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", 3001), timeout=0.2):
                return True
        except OSError:
            return False

    def _port_open(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", 6099), timeout=0.25):
                return True
        except OSError:
            return False

    def start(self, launcher_path: str | os.PathLike | None = None, qq_path: str | os.PathLike | None = None) -> bool:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                return True
            if self._port_open():
                self._external_running = True
                self._state = "external"
                self._event("info", "检测到已有 NapCat WebUI 服务，不会重复启动或接管")
                return True
            if self._cancel.is_set():
                self._cancel.clear()
            launcher = self._find_launcher(launcher_path)
            if not launcher:
                self._error = "未找到 NapCat Windows launcher"
                self._event("error", self._error)
                return False
            is_node = launcher.name.lower() == "node.exe"
            if is_node and not all(launcher.with_name(name).is_file() for name in ('index.js', 'wrapper.node')):
                self._error = '登录组件不完整，请重新准备官方安装包'
                return False
            qq = Path(qq_path).expanduser().resolve() if qq_path else self._find_qq()
            if not is_node and (not qq or not qq.is_file()):
                self._error = "未找到桌面 QQ，请选择 QQ.exe"
                self._event("error", self._error)
                return False
            hook = launcher.with_name("NapCatWinBootHook.dll")
            if not is_node and not hook.is_file():
                self._error = "NapCat Windows 注入组件不存在"
                return False
            env = os.environ.copy()
            for name in ('NAPCAT_QUICK_ACCOUNT', 'NAPCAT_WORKER_PROCESS', 'NAPCAT_MAIN_PATH',
                         'NAPCAT_LOAD_PATH', 'NAPCAT_INJECT_PATH', 'NAPCAT_PATCH_PACKAGE'):
                env.pop(name, None)
            env.update({'NAPCAT_QQ_DATA_DIR': str(self.data_dir / 'qq-data'),
                        'NAPCAT_WORKDIR': str(self.data_dir / 'qq-data' / 'NapCat')})
            if not is_node:
                env.update({"NAPCAT_PATCH_PACKAGE": str(launcher.with_name("qqnt.json")),
                        "NAPCAT_LOAD_PATH": str(launcher.with_name("loadNapCat.js")),
                        "NAPCAT_INJECT_PATH": str(hook), "NAPCAT_LAUNCHER_PATH": str(launcher),
                        "NAPCAT_MAIN_PATH": str(launcher.with_name("napcat.mjs"))})
            if is_node:
                index = launcher.with_name("index.js")
                command = [str(launcher), str(index)]
            else:
                # The official user launcher passes QQ.exe and the hook DLL directly.
                command = [str(launcher), str(qq), str(hook)]
            cwd = launcher.parent
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | 0x00000004
            try:
                self._proc = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT, creationflags=flags, bufsize=0, env=env)
            except OSError as exc:
                self._error = f"NapCat 启动失败：{exc.__class__.__name__}"
                self._event("error", self._error)
                return False
            self._owned_pid = self._proc.pid
            self._selected_launcher = launcher
            if os.name == "nt" and not self._assign_job(self._proc):
                self._proc.kill()
                self._proc.wait(timeout=3)
                self._proc = None
                self._owned_pid = None
                self._error = "无法跟踪 NapCat 启动的进程树，已取消启动"
                self._event("error", self._error)
                return False
            if os.name == "nt":
                ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
                ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
                ntdll.NtResumeProcess.restype = ctypes.c_long
                if ntdll.NtResumeProcess(wintypes.HANDLE(int(self._proc._handle))) < 0:
                    self._error = "NapCat launcher 无法恢复运行"
                    self.stop()
                    return False
            self._external_running = False
            self._state = "starting"
            self._error = ''
            self._event("info", "已启动 NapCat，等待本机 WebUI 服务")
            self._reader = threading.Thread(target=self._read_output, daemon=True)
            self._reader.start()
            return True

    def _assign_job(self, proc: subprocess.Popen) -> bool:
        if os.name != "nt":
            return True
        try:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            create = kernel.CreateJobObjectW
            create.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            create.restype = wintypes.HANDLE
            job = create(None, None)
            if not job:
                raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
            class BASIC_LIMIT(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
            class EXT_LIMIT(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
            limits = EXT_LIMIT()
            limits.BasicLimitInformation.LimitFlags = 0x00002000
            kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            kernel.SetInformationJobObject.restype = wintypes.BOOL
            if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
            kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel.AssignProcessToJobObject.restype = wintypes.BOOL
            if not kernel.AssignProcessToJobObject(job, wintypes.HANDLE(int(proc._handle))):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
            self._job = job
            return True
        except Exception as exc:
            if "job" in locals() and job:
                try:
                    kernel.CloseHandle(job)
                except Exception:
                    pass
            self._event("warning", f"NapCat 进程树跟踪不可用：{exc.__class__.__name__}")
            return False

    def _read_output(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return
        try:
            for raw in iter(proc.stdout.readline, b""):
                text = raw.decode("utf-8", "replace").strip()
                if text:
                    self._event("info", text)
                if self._port_open():
                    self._state = "ready"
        finally:
            if proc.poll() is not None:
                self._state = "error" if self._state != "ready" else "ready"
                self._event("error" if self._state == "error" else "info", "NapCat 进程已退出")

    def is_ready(self) -> bool:
        if self._port_open():
            if not self._external_running:
                self._state = "ready"
            return True
        return False

    def get_webui_token(self) -> str | None:
        """Return the token for the local WebUI only; callers must never log it."""
        roots = [self.data_dir / "qq-data" / "NapCat", self.data_dir, self.install_dir / 'napcat', self.install_dir]
        if self._selected_launcher:
            roots.append(self._selected_launcher.parent)
        for path in (root / "config/webui.json" for root in roots):
            try:
                value = json.loads(path.read_text(encoding="utf-8")).get("token")
                if isinstance(value, str) and value:
                    return value
            except (OSError, ValueError, AttributeError):
                continue
        return None

    def snapshot(self) -> dict:
        running = bool(self._proc and self._proc.poll() is None)
        ready = self.is_ready()
        installed = self.install_dir.is_dir() and any(self.install_dir.rglob("napcat.mjs")) and any(self.install_dir.rglob("node.exe"))
        return {"state": self._state, "installed": bool(installed), "running": running or bool(self._job) or (self._external_running and ready),
                "owned": running or bool(self._job), "ready": ready, "launcher_path": str(self._find_launcher(None) or ""),
                "qq_path": str(self._find_qq() or ""), "webui_url": "http://127.0.0.1:6099", "error": self._error}

    def stop(self) -> bool:
        self._cancel.set()
        with self._lock:
            proc = self._proc
            if not proc or (proc.poll() is not None and not self._job):
                self._event("info", "没有由本程序运行的 NapCat 进程需要停止")
                return False
            self._state = "stopping"
            try:
                if self._job:
                    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
                    kernel.TerminateJobObject.restype = wintypes.BOOL
                    if not kernel.TerminateJobObject(self._job, 1):
                        raise OSError(ctypes.get_last_error(), "TerminateJobObject")
                    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                    kernel.CloseHandle.restype = wintypes.BOOL
                    kernel.CloseHandle(self._job)
                    self._job = None
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                else:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)
            except OSError as exc:
                self._event("error", f"停止 NapCat 失败：{exc.__class__.__name__}")
                return False
            self._state = "installed"
            self._owned_pid = None
            self._event("info", "已停止本程序启动的 NapCat 进程")
            return True
