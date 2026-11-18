"""Native WebView2 shell for AstrBot's management interface."""
from __future__ import annotations
import html
import json
import queue
import threading
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
    def __init__(self,runtime,storage_path,webview_module,*,tray_factory=None):
        self.runtime=runtime;self.storage_path=Path(storage_path).resolve();self.webview=webview_module
        self.tray_factory=tray_factory;self.window=None;self.tray=None
        self._exit=threading.Event();self._busy=threading.Lock();self._authenticated=False

    def retry(self):
        if self.window and self.window.get_current_url() not in (None,'','about:blank'):return False
        self._launch();return True

    def _launch(self):
        if self._exit.is_set() or not self._busy.acquire(blocking=False):return
        def worker():
            try:
                url=self.runtime.start()
                if not self._exit.is_set():self.window.load_url(url)
            except Exception:
                if not self._exit.is_set():self.window.load_html(startup_html('请确认安装目录完整，并检查 runtime/logs 中的运行日志。',error=True))
            finally:self._busy.release()
        threading.Thread(target=worker,daemon=True,name='whale-desktop-start').start()

    def _loaded(self):
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
