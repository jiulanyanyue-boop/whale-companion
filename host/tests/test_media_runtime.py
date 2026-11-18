import os
import sys
from pathlib import Path
from types import SimpleNamespace

from astrbot_whale_device.media_runtime import prepare_media_environment


def test_existing_converter_is_reused(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda *a, **k: "existing")
    env = {"PATH": "existing-path"}
    assert prepare_media_environment(env, tmp_path / "bin")
    assert env == {"PATH": "existing-path"}
    assert not (tmp_path / "bin").exists()


def test_bundled_converter_is_shared_via_child_environment(tmp_path, monkeypatch):
    source = tmp_path / "bundled.exe"
    source.write_bytes(b"test converter")
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    monkeypatch.setitem(
        sys.modules,
        "imageio_ffmpeg",
        SimpleNamespace(get_ffmpeg_exe=lambda: str(source)),
    )
    env = {"PATH": "original"}
    assert prepare_media_environment(env, tmp_path / "bin")
    binary = Path(env["PATH"].split(os.pathsep)[0]) / (
        "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    )
    assert binary.read_bytes() == b"test converter"
    assert env["PATH"].endswith(os.pathsep + "original")
