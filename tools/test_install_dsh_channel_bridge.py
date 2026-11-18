import subprocess
import sys
from pathlib import Path

import yaml


def test_install_uses_insert_and_is_idempotent(tmp_path):
    patch = tmp_path / "cordis.patch.yml"
    unrelated = {"id": "ui-chat", "config": {"enabled": True}}
    patch.write_text(
        yaml.safe_dump([unrelated, {"id": "whale-channel-bridge", "name": "old"}]),
        encoding="utf-8",
    )
    script = Path(__file__).with_name("install-dsh-channel-bridge.py")
    for _ in range(2):
        subprocess.run(
            [sys.executable, str(script), "--profile", str(tmp_path)],
            check=True,
            capture_output=True,
        )
    entries = yaml.safe_load(patch.read_text(encoding="utf-8"))
    assert entries[0] == unrelated
    assert not any(e.get("id") == "whale-channel-bridge" for e in entries)
    inserted = [x for e in entries for x in e.get("insert", [])]
    assert len(inserted) == 1
    target = tmp_path / "plugins/whale-channel-bridge/index.mjs"
    assert inserted[0] == {"id": "whale-channel-bridge", "name": target.as_uri()}
    assert (
        target.read_bytes() == script.with_name("dsh-channel-bridge.mjs").read_bytes()
    )
    assert (tmp_path / "cordis.patch.yml.before-whale-channel-bridge").exists()
