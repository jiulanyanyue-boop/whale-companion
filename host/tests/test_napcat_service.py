from __future__ import annotations

import json
import os
import zipfile
import hashlib
import sys
import subprocess
import threading
import ctypes
import pytest

from astrbot_whale_device.napcat_service import NapCatService


def test_log_events_are_bounded_and_hide_login_secrets(tmp_path):
    service = NapCatService(tmp_path)
    service._event("info", "Authorization: Bearer super-secret-token")
    service._event("info", "https://q.qq.com/scan?ticket=private")
    events = service.poll_events()
    assert len(events) == 2
    assert "super-secret-token" not in events[0]["message"]
    assert "https://" not in events[1]["message"]
    assert all(len(event["message"]) <= 500 for event in events)


def test_safe_extract_rejects_zip_slip(tmp_path):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../outside.txt", "not allowed")
    try:
        NapCatService._safe_extract(archive, tmp_path / "out")
    except RuntimeError as exc:
        assert "非法路径" in str(exc)
    else:
        raise AssertionError("zip-slip entry should be rejected")
    assert not (tmp_path / "outside.txt").exists()


def test_existing_managed_install_is_preserved(tmp_path):
    service = NapCatService(tmp_path)
    (service.install_dir / "config").mkdir(parents=True)
    (service.install_dir / "napcat.mjs").write_text("existing", encoding="utf-8")
    (service.install_dir / "config/webui.json").write_text('{"token":"keep"}', encoding="utf-8")
    result = service.prepare_install()
    assert result["ok"] is True
    assert result["version"] == "existing"
    assert service.get_webui_token() == "keep"


def test_token_lookup_in_user_selected_launcher_folder(tmp_path):
    service = NapCatService(tmp_path)
    folder = tmp_path / "custom"
    (folder / "config").mkdir(parents=True)
    launcher = folder / "NapCatWinBootMain.exe"
    launcher.write_bytes(b"not executed")
    (folder / "config/webui.json").write_text(json.dumps({"token": "local-only"}), encoding="utf-8")
    service._selected_launcher = launcher
    assert service.get_webui_token() == "local-only"


def test_existing_service_is_not_claimed_or_stopped(tmp_path, monkeypatch):
    service = NapCatService(tmp_path)
    monkeypatch.setattr(service, "_port_open", lambda: True)
    assert service.start() is True
    snap = service.snapshot()
    assert snap["running"] is True
    assert snap["owned"] is False
    assert service.stop() is False
    assert service._proc is None


def test_cancelled_prepare_cannot_start(tmp_path):
    service = NapCatService(tmp_path)
    service._cancel.set()
    assert service.start() is False


def test_qr_links_and_ascii_payloads_are_not_logged(tmp_path):
    service = NapCatService(tmp_path)
    service._event("info", "Open https://q.qq.com/qr?a=123 to scan")
    service._event("info", "+//++/////+/++//")
    messages = [event["message"] for event in service.poll_events()]
    assert all("q.qq.com" not in message for message in messages)
    assert all("123" not in message for message in messages)
    assert all("+//++" not in message for message in messages)


def official_test_assets(tmp_path):
    archives={}
    metadata=[]
    for name,entries in {
        'NapCat.Shell.Windows.Node.zip':{'node.exe':'runtime','index.js':'entry',
            'wrapper.node':'native','package.json':'{}','config.json':'{}'},
        'NapCat.Shell.zip':{'napcat.mjs':'main','static/index.html':'login UI'},
    }.items():
        archive=tmp_path/name
        with zipfile.ZipFile(archive,'w') as output:
            for path,value in entries.items():output.writestr(path,value)
        archives[name]=archive
        metadata.append({'name':name,'size':archive.stat().st_size,
            'digest':'sha256:'+hashlib.sha256(archive.read_bytes()).hexdigest()})
    return archives,metadata


def test_preparation_combines_both_official_packages_without_overwriting_data(tmp_path,monkeypatch):
    archives,metadata=official_test_assets(tmp_path)
    service=NapCatService(tmp_path/'installed')
    monkeypatch.setattr(service,'_release_assets',lambda:('test-release',metadata))
    assert service.prepare_install(archives=archives)['ok']
    assert (service.install_dir/'node.exe').is_file()
    assert (service.install_dir/'napcat/napcat.mjs').is_file()
    assert (service.install_dir/'napcat/static/index.html').is_file()
    data=service.data_dir/'qq-data/NapCat/config';data.mkdir(parents=True)
    (data/'webui.json').write_text('{"token":"preserve-this"}')
    assert NapCatService(service.data_dir).prepare_install()['ok']
    assert service.get_webui_token()=='preserve-this'


def test_bad_archive_digest_never_installs_runtime(tmp_path,monkeypatch):
    archives,metadata=official_test_assets(tmp_path)
    archives['NapCat.Shell.zip'].write_bytes(b'modified content')
    service=NapCatService(tmp_path/'installed')
    monkeypatch.setattr(service,'_release_assets',lambda:('test-release',metadata))
    assert not service.prepare_install(archives=archives)['ok']
    assert not service.install_dir.exists()


@pytest.mark.skipif(os.name!='nt',reason='Native Windows job ownership')
def test_job_stops_owned_parent_and_child_without_touching_unrelated_process(tmp_path):
    from ctypes import wintypes
    service=NapCatService(tmp_path)
    other=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],creationflags=0x08000000)
    script='import subprocess,sys,time;child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(30)"]);print(child.pid,flush=True);time.sleep(30)'
    proc=subprocess.Popen([sys.executable,'-u','-c',script],stdout=subprocess.PIPE,
        creationflags=0x08000000|0x4)
    service._proc=proc
    child_handle=None
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    kernel.OpenProcess.restype=wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    kernel.WaitForSingleObject.restype=wintypes.DWORD
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    try:
        assert service._assign_job(proc)
        native=ctypes.WinDLL('ntdll')
        native.NtResumeProcess.argtypes=[wintypes.HANDLE];native.NtResumeProcess.restype=ctypes.c_long
        assert native.NtResumeProcess(wintypes.HANDLE(int(proc._handle)))>=0
        ready=threading.Event();result=[]
        def read_child():result.append(proc.stdout.readline());ready.set()
        threading.Thread(target=read_child,daemon=True).start()
        assert ready.wait(5)
        child_handle=kernel.OpenProcess(0x00100000,False,int(result[0]))
        assert child_handle
        assert service.stop()
        assert proc.poll() is not None
        assert kernel.WaitForSingleObject(child_handle,5000)==0
        assert other.poll() is None
    finally:
        service.stop()
        if proc.poll() is None:proc.kill()
        if child_handle:kernel.CloseHandle(child_handle)
        proc.stdout.close()
        other.kill();other.wait(5)

