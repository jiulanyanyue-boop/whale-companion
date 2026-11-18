import html
import threading
import time
import queue
from types import SimpleNamespace

from astrbot_whale_device import embedded_window as ew


class FakeEvents:
    def __init__(self):
        self.loaded = []
        self.closing = []


class FakeWindow:
    def __init__(self, url="about:blank"):
        self.url = url
        self.hidden = False
        self.loaded_urls = []
        self.scripts = []
        self.events = FakeEvents()

    def get_current_url(self): return self.url
    def load_url(self, url): self.url = url; self.loaded_urls.append(url)
    def evaluate_js(self, script): self.scripts.append(script)
    def run_js(self, script): self.scripts.append(script)
    def hide(self): self.hidden = True
    def show(self): self.hidden = False
    def restore(self): self.hidden = False
    def destroy(self): pass


class Runtime:
    def __init__(self, url=ew.DASHBOARD_URL, token="secret-token"):
        self.url = url; self.token = token; self.starts = 0; self.stops = 0
    def start(self): self.starts += 1; return self.url
    def stop(self): self.stops += 1
    def dashboard_token(self): return self.token


class Tray:
    def __init__(self): self.events = queue.Queue(); self.stopped = False
    def start(self): pass
    def stop(self): self.stopped = True


def test_dashboard_url_rejects_external_and_non_dashboard_origins():
    assert ew.is_dashboard_url(ew.DASHBOARD_URL)
    assert not ew.is_dashboard_url("https://127.0.0.1:6185/")
    assert not ew.is_dashboard_url("http://localhost:6185/")
    assert not ew.is_dashboard_url("http://127.0.0.1:6186/")
    assert not ew.is_dashboard_url("http://127.0.0.1:6185@evil.example/")


def test_startup_html_escapes_error_and_offers_retry():
    message = '<script>alert("x")</script>'
    rendered = ew.startup_html(message, error=True)
    assert html.escape(message) in rendered
    assert message not in rendered
    assert 'pywebview.api.retry()' in rendered


def test_closing_hides_to_tray_but_explicit_exit_allows_close():
    controller = ew.DesktopWindow(Runtime(), "profile", SimpleNamespace(), tray_factory=None)
    controller.window = FakeWindow(); controller.tray = Tray()
    assert controller._closing() is False
    assert controller.window.hidden
    controller._exit.set()
    assert controller._closing() is True


def test_loaded_injects_token_once_only_for_trusted_origin():
    window = FakeWindow(ew.DASHBOARD_URL)
    controller = ew.DesktopWindow(Runtime(token="top-secret"), "profile", SimpleNamespace(), tray_factory=None)
    controller.window = window
    controller._loaded(); controller._loaded()
    assert len(window.scripts) == 1
    assert 'top-secret' in window.scripts[0]
    window.url = "https://evil.example/"
    controller._loaded()
    assert len(window.scripts) == 1


def test_retry_does_not_start_duplicate_runtime_workers():
    entered = threading.Event(); release = threading.Event()
    class SlowRuntime(Runtime):
        def start(self):
            self.starts += 1; entered.set(); release.wait(2); return ew.DASHBOARD_URL
    runtime = SlowRuntime(); window = FakeWindow()
    controller = ew.DesktopWindow(runtime, "profile", SimpleNamespace(), tray_factory=None)
    controller.window = window
    controller._launch(); assert entered.wait(1)
    controller._launch(); assert runtime.starts == 1
    release.set()
    for _ in range(20):
        if window.loaded_urls: break
        time.sleep(.01)
    assert window.loaded_urls == [ew.DASHBOARD_URL]
