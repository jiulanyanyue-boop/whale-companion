"""Native WebView2 shell for AstrBot's management interface."""
from __future__ import annotations
import html
import json
import queue
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from .integrated_runtime import DASHBOARD_URL, IntegratedRuntime

def is_dashboard_url(url):
    try:
        parsed=urlsplit(url or '')
        return parsed.scheme=='http' and parsed.hostname=='127.0.0.1' and parsed.port==6185 and not parsed.username and not parsed.password
    except ValueError:return False

def startup_html(message='正在启动 AstrBot 和手机服务…', *, error=False):
    heading='暂时无法启动' if error else 'Whale · AstrBot'
    retry='<button onclick="window.pywebview.api.retry()">重新尝试</button>' if error else ''
    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><style>
    body{{background:#f7fbf8;color:#17352b;font:16px/1.8 "Microsoft YaHei",sans-serif;margin:0;display:grid;place-items:center;height:100vh}}
    main{{max-width:600px;padding:32px}}h1{{font-size:28px}}p{{color:#6f8179}}button{{background:#19724d;color:white;border:0;border-radius:6px;padding:10px 20px;cursor:pointer}}
    </style><main><h1>{heading}</h1><p>{html.escape(message)}</p>{retry}</main></html>'''

class DesktopWindow:
    """管理桌面窗口、服务启动与托盘；启动失败时保留不含凭据的诊断信息。"""
    def __init__(self,runtime,storage_path,webview_module,*,tray_factory=None):
        self.runtime=runtime;self.storage_path=Path(storage_path).resolve();self.webview=webview_module
        self.tray_factory=tray_factory;self.window=None;self.tray=None
        self._exit=threading.Event();self._busy=threading.Lock();self._authenticated=False
        self._page_ready=threading.Event()
        self._log_path=self.storage_path.parent.parent/'runtime/logs/desktop-shell.log'

    def _record_failure(self, stage, exc):
        """只记录异常类型和栈位置，不记录消息、源码行或局部变量中的密钥。"""
        try:
            self._log_path.parent.mkdir(parents=True,exist_ok=True)
            with self._log_path.open('a',encoding='utf-8') as output:
                output.write(f'{datetime.now(timezone.utc).isoformat()} {stage}\n')
                for _ in range(8):
                    output.write(type(exc).__name__+'\n')
                    for frame in traceback.extract_tb(exc.__traceback__):
                        output.write(f'  {frame.filename}:{frame.lineno} in {frame.name}\n')
                    exc=exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
                    if exc is None:break
        except OSError:
            pass

    def _show_failure(self, exc):
        """展示安全的故障提示；即使页面渲染失败，也保留其独立诊断栈。"""
        self._record_failure('desktop startup failed',exc)
        if self._exit.is_set():return
        try:
            self.window.load_html(startup_html(
                '请检查 host.json 配置及安装目录是否完整。诊断日志：runtime/logs/desktop-shell.log；后端日志：runtime/logs/desktop-launcher.log。',error=True))
        except Exception as render_error:
            self._record_failure('startup error page failed',render_error)

    def retry(self):
        if self.window and self.window.get_current_url() not in (None,'','about:blank'):return False
        self._launch();return True

    def _launch(self):
        """串行启动服务，捕获快速失败，避免工作线程静默退出。"""
        if self._exit.is_set() or not self._busy.acquire(blocking=False):return
        def worker():
            try:
                url=self.runtime.start()
                if not self._exit.is_set():self.window.load_url(url)
            except Exception as exc:
                self._show_failure(exc)
            finally:self._busy.release()
        threading.Thread(target=worker,daemon=True,name='whale-desktop-start').start()

    def _loaded(self):
        """标记首屏已完成初始化，仅向受信任的本机页面注入登录令牌。"""
        self._page_ready.set()
        if self._authenticated or self._exit.is_set() or not is_dashboard_url(self.window.get_current_url()):return
        try:
            token=self.runtime.dashboard_token()
            if token:
                self._authenticated=True
                self.window.evaluate_js('localStorage.setItem("token",'+json.dumps(token)+');location.replace('+json.dumps(DASHBOARD_URL)+');')
        except Exception:pass

    def _closing(self):
        if self._exit.is_set():return True
        if self.tray is not None:self.window.hide();return False
        self._exit.set();return True

    def _background(self):
        """等待 WebView2 首屏就绪再启动，防止初始化过程覆盖成功导航或报错页。"""
        deadline=time.monotonic()+30
        while not self._page_ready.wait(.1):
            if self._exit.is_set():return
            if time.monotonic()>=deadline:
                self._show_failure(TimeoutError('WebView2 initial page timeout'))
                return
        if self._exit.is_set():return
        if self.tray_factory:
            try:self.tray=self.tray_factory();self.tray.start()
            except Exception:
                self.tray=None
                self.window.load_html(startup_html('系统托盘暂不可用，关闭窗口将退出应用。正在准备管理页面…'))
        self._launch()
        while not self._exit.wait(.2):
            if not self.tray:continue
            try:action=self.tray.events.get_nowait()
            except queue.Empty:continue
            if action=='show':self.window.show();self.window.restore()
            elif action=='exit':self._exit.set();self.window.destroy()

    def run(self):
        self.storage_path.mkdir(parents=True,exist_ok=True)
        self.webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER']=False
        self.webview.settings['ALLOW_DOWNLOADS']=True
        class RetryApi:
            def retry(api_self):return self.retry()
        self.window=self.webview.create_window('Whale · AstrBot',html=startup_html(),width=1200,height=820,
            min_size=(900,650),js_api=RetryApi(),background_color='#f7fbf8')
        self.window.events.loaded+=self._loaded;self.window.events.closing+=self._closing
        try:self.webview.start(self._background,gui='edgechromium',private_mode=False,storage_path=str(self.storage_path))
        finally:
            self._exit.set()
            if self.tray is not None:self.tray.stop()
            self.runtime.stop()
        return 0

def run_window(runtime,storage_path):
    import webview
    from .desktop_tray import DesktopTray
    return DesktopWindow(runtime,storage_path,webview,tray_factory=DesktopTray).run()

def run_embedded(config_path,runtime_factory=None):
    path=Path(config_path).resolve()
    return run_window((runtime_factory or IntegratedRuntime)(path),path.parent/'webview-profile')
