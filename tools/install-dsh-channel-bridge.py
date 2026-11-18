"""Install the local DSH companion without editing upstream plugin code."""

import argparse
import shutil
from pathlib import Path

import yaml

parser = argparse.ArgumentParser()
parser.add_argument(
    "--profile", type=Path, default=Path.home() / ".dsh/profiles/desktop"
)
args = parser.parse_args()
profile = args.profile.resolve()
patch = profile / "cordis.patch.yml"
entries = yaml.safe_load(patch.read_text(encoding="utf-8")) or []
if not isinstance(entries, list):
    raise SystemExit("Unsupported DSH profile format")
target = profile / "plugins/whale-channel-bridge/index.mjs"
target.parent.mkdir(parents=True, exist_ok=True)
shutil.copy2(Path(__file__).with_name("dsh-channel-bridge.mjs"), target)
identifier = "whale-channel-bridge"
# DSH patches modify existing IDs; new plugins must be added through insert.
entries = [
    e for e in entries if not (isinstance(e, dict) and e.get("id") == identifier)
]
entry = None
for patch_entry in entries:
    if not isinstance(patch_entry, dict):
        continue
    for inserted in patch_entry.get("insert", []):
        if isinstance(inserted, dict) and inserted.get("id") == identifier:
            entry = inserted
            break
if entry is None:
    entry = {"id": identifier}
    entries.append({"insert": [entry]})
entry["name"] = target.as_uri()
backup = patch.with_name("cordis.patch.yml.before-whale-channel-bridge")
if not backup.exists():
    shutil.copy2(patch, backup)
temp = patch.with_suffix(".whale.tmp")
temp.write_text(
    yaml.safe_dump(entries, allow_unicode=True, sort_keys=False), encoding="utf-8"
)
temp.replace(patch)
print(
    "Whale channel management companion installed using a DSH profile insert. Running DSH can load it automatically; restart only if profile watching is disabled."
)
