import ctypes
import importlib.util
from pathlib import Path
from types import SimpleNamespace

LAUNCHER = Path(__file__).resolve().parents[1] / 'launcher.py'
spec = importlib.util.spec_from_file_location('whale_launcher', LAUNCHER)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class FakeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.argtypes = None
        self.restype = None
    def __call__(self, *args):
        return self.callback(*args)


def test_single_instance_primary_and_duplicate_close_duplicate_handle(monkeypatch):
    errors = iter((0, 183))
    monkeypatch.setattr(ctypes, 'set_last_error', lambda value: None)
    monkeypatch.setattr(ctypes, 'get_last_error', lambda: next(errors))
    closed = []
    api = SimpleNamespace(
        CreateMutexW=FakeFunction(lambda *args: 101),
        CloseHandle=FakeFunction(lambda handle: closed.append(handle) or 1),
    )
    primary = launcher.SingleInstance(api)
    assert primary.acquire() is True
    assert api.CreateMutexW.restype is ctypes.c_void_p
    assert api.CloseHandle.restype is ctypes.c_int
    primary.close()
    duplicate = launcher.SingleInstance(api)
    assert duplicate.acquire() is False
    assert duplicate.handle is None
    assert closed == [101, 101]


def test_duplicate_launch_restores_and_foregrounds_existing_window():
    calls = []
    api = SimpleNamespace(
        FindWindowW=FakeFunction(lambda klass, title: calls.append(('find', title)) or 42),
        ShowWindow=FakeFunction(lambda hwnd, command: calls.append(('show', hwnd, command)) or 1),
        SetForegroundWindow=FakeFunction(lambda hwnd: calls.append(('foreground', hwnd)) or 1),
    )
    assert launcher._wake_existing_window(api) is True
    assert calls == [('find', launcher.WINDOW_TITLE), ('show', 42, 9), ('foreground', 42)]


def test_source_config_path_is_dist_exe_host_json(monkeypatch):
    monkeypatch.setattr(launcher.sys, 'frozen', False, raising=False)
    assert launcher._base_dir().endswith('dist-exe')


def test_default_run_uses_embedded_window():
    seen = []
    assert launcher._run('config.json', False, seen.append) == 0
    assert seen == ['config.json']
