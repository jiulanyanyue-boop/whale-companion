from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from astrbot_whale_device import integrated_runtime as module


class FakeCredentials:
    def __init__(self):
        self.saved = False
        self.applied = False

    def ensure_signing_secret(self):
        return False

    def save(self, path):
        self.saved = True

    def apply_to_environ(self):
        self.applied = True


class FakeConfig:
    pass


class FakeStore:
    instances = []

    def __init__(self, path):
        self.path = path
        self.closed = False
        self.__class__.instances.append(self)

    def close(self):
        self.closed = True


class FakeService:
    instances = []
    fail = False

    def __init__(self):
        self.started = False
        self.stopped = False
        self.host = type("Host", (), {"plugin": type("Plugin", (), {})()})()
        self.__class__.instances.append(self)

    def start(self):
        if self.fail:
            raise RuntimeError("device startup failed")
        self.started = True

    def stop(self):
        self.stopped = True


class FakeProcess:
    next_pid = 4001

    def __init__(self):
        self.pid = self.__class__.next_pid
        self.__class__.next_pid += 1
        self.terminated = False
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


class FakeQQController:
    instances = []

    def __init__(self, path):
        self.path = path
        self.logins = 0
        self.clears = 0
        self.__class__.instances.append(self)

    def status(self):
        return {"auto_login": False}

    def clear_logout_suppression(self):
        self.clears += 1

    def login(self):
        self.logins += 1


@pytest.fixture
def runtime_env(tmp_path, monkeypatch):
    data_dir = tmp_path / "dist-exe"
    data_dir.mkdir()
    config_path = data_dir / "host.json"
    config_path.write_text("{}", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "runtime" / "astrbot-venv" / "Scripts").mkdir(parents=True)
    (tmp_path / "tools" / "run-astrbot.py").write_text("", encoding="utf-8")
    (tmp_path / "runtime" / "astrbot-venv" / "Scripts" / "python.exe").write_bytes(b"")

    FakeStore.instances.clear()
    FakeService.instances.clear()
    FakeQQController.instances.clear()
    FakeService.fail = False
    monkeypatch.setattr(module, "HostConfig", FakeConfig, raising=False)
    monkeypatch.setattr(module, "StoredSecrets", FakeCredentials, raising=False)
    return config_path


def patch_dependencies(monkeypatch, *, auto_login=False):
    class ConfigLoader:
        @classmethod
        def load(cls, path):
            return FakeConfig()

    class CredentialsLoader:
        @classmethod
        def load(cls, path):
            credentials = FakeCredentials()
            return credentials

    monkeypatch.setattr("astrbot_whale_device.host_config.HostConfig.load", ConfigLoader.load)
    monkeypatch.setattr("astrbot_whale_device.gui_secrets.StoredSecrets.load", CredentialsLoader.load)
    monkeypatch.setattr("astrbot_whale_device.astrbot_native_bridge.ensure_rpc_token", lambda path: "r" * 48)
    monkeypatch.setattr("astrbot_whale_device.storage.TaskStore", FakeStore)
    monkeypatch.setattr("astrbot_whale_device.headless_service.HeadlessDeviceService.from_config", lambda *a, **k: FakeService())
    monkeypatch.setattr("astrbot_whale_device.desktop_device_transport.DesktopDeviceTransport", lambda *a: object())

    class QQ(FakeQQController):
        def status(self):
            return {"auto_login": auto_login}

    monkeypatch.setattr("astrbot_whale_device.qq_account.QQAccountController", QQ)


def test_reuses_authenticated_device_and_dashboard_without_spawning(monkeypatch, runtime_env):
    patch_dependencies(monkeypatch)
    runtime = module.IntegratedRuntime(runtime_env, startup_timeout=1)
    requests = []

    monkeypatch.setattr(runtime, "_port_open", staticmethod(lambda port: True))

    def request(url, **kwargs):
        requests.append((url, kwargs))
        if url.endswith("/v1/device/status"):
            assert kwargs["token"] == "r" * 48
            return {"started": True}
        return {"status": "ok", "data": {"setup_required": False}}

    monkeypatch.setattr(runtime, "_request", staticmethod(request))
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: pytest.fail("must reuse"))

    assert runtime.start() == module.DASHBOARD_URL
    assert runtime._service is None
    assert runtime._process is None
    assert runtime._started
    runtime.stop()
    assert not runtime._started
    assert any(url.endswith("/v1/device/status") for url, _ in requests)


def test_owned_service_failure_rolls_back_service_and_store(monkeypatch, runtime_env):
    patch_dependencies(monkeypatch)
    FakeService.fail = True
    runtime = module.IntegratedRuntime(runtime_env, startup_timeout=1)
    monkeypatch.setattr(runtime, "_port_open", staticmethod(lambda port: False))

    with pytest.raises(RuntimeError, match="device startup failed"):
        runtime.start()

    service = FakeService.instances[-1]
    assert service.stopped
    assert FakeStore.instances[-1].closed
    assert runtime._process is None
    assert runtime._started is False


@pytest.mark.parametrize(("auto_login", "expected_logins"), [(False, 0), (True, 1)])
def test_auto_login_flag_controls_login(monkeypatch, runtime_env, auto_login, expected_logins):
    patch_dependencies(monkeypatch, auto_login=auto_login)
    runtime = module.IntegratedRuntime(runtime_env, startup_timeout=1)
    process = FakeProcess()
    monkeypatch.setattr(runtime, "_port_open", staticmethod(lambda port: port == 6185))
    monkeypatch.setattr(runtime, "_dashboard_ready", lambda: True)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: pytest.fail("dashboard is external"))
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "_request", staticmethod(lambda *a, **k: {"status": "ok", "data": {"setup_required": False}}))

    runtime.start()
    controller = FakeQQController.instances[-1]
    assert controller.logins == expected_logins
    assert controller.clears == expected_logins
    runtime.stop()


def test_owned_dashboard_token_uses_header_and_never_persists_secret(monkeypatch, runtime_env):
    patch_dependencies(monkeypatch)
    runtime = module.IntegratedRuntime(runtime_env, startup_timeout=1)
    process = FakeProcess()
    monkeypatch.setattr(runtime, "_port_open", staticmethod(lambda port: False))
    monkeypatch.setattr(runtime, "_dashboard_ready", lambda: True)
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(module.secrets, "token_urlsafe", lambda n: "s" * 48)
    captured = {}

    def request(url, **kwargs):
        captured.update(kwargs)
        if url.endswith("/api/v1/auth/setup-status"):
            return {"status": "ok", "data": {"setup_required": False}}
        return {"status": "ok", "data": {"token": "jwt-token"}}

    monkeypatch.setattr(runtime, "_request", staticmethod(request))
    runtime.start()
    assert runtime.dashboard_token() == "jwt-token"
    assert captured["headers"]["X-AstrBot-Desktop-Session"] == "s" * 48
    persisted = "".join(path.read_text(encoding="utf-8", errors="ignore") for path in runtime_env.parent.rglob("*") if path.is_file())
    assert "s" * 48 not in persisted
    runtime.stop()

def test_stop_cancels_dashboard_wait(monkeypatch, runtime_env):
    patch_dependencies(monkeypatch)
    runtime = module.IntegratedRuntime(runtime_env, startup_timeout=30)
    process = FakeProcess()
    monkeypatch.setattr(runtime, "_port_open", staticmethod(lambda port: False))
    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(runtime, "_dashboard_ready", lambda: False)
    killed = []
    monkeypatch.setattr(module.subprocess, "run", lambda args, **kwargs: killed.append(args))

    outcome = []

    def worker():
        try:
            runtime.start()
        except Exception as exc:
            outcome.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    deadline = time.monotonic() + 2
    while runtime._process is None and time.monotonic() < deadline:
        time.sleep(0.01)
    runtime.stop()
    thread.join(2)

    assert not thread.is_alive()
    assert outcome and "启动已取消" in str(outcome[0])
    assert killed and str(process.pid) in killed[0]