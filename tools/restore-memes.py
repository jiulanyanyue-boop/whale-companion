"""Restore private images and rebuild their local search index."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "host"))

from astrbot_whale_device.memes.library import MemeLibrary

FIELDS = ("description", "image_text", "meaning", "emotion", "scene")
FORMATS = {".jpg", ".jpeg", ".webp", ".png", ".gif"}


def restore(snapshot: Path, destination: Path) -> int:
    records = json.loads((snapshot / "catalog.json").read_text(encoding="utf-8"))
    rows = []
    for record in records:
        name = record["file"]
        if Path(name).name != name or any(c in name for c in "/\\:") or Path(name).suffix.lower() not in FORMATS:
            raise ValueError("Invalid asset filename")
        content = (snapshot / "images" / name).read_bytes()
        if hashlib.sha256(content).hexdigest() != record["digest"]:
            raise ValueError(f"Asset checksum mismatch: {name}")
        target = destination / "images" / name
        if target.exists() and target.read_bytes() != content:
            raise FileExistsError(f"Local image differs; resolve before restoring: {name}")
        rows.append((record, target, content))

    library = MemeLibrary(destination)
    for record, target, content in rows:
        if not target.exists():
            target.write_bytes(content)
        metadata = {key: record["metadata"].get(key, "") for key in FIELDS}
        library.add(target, source="private_snapshot", role=record["role"], **metadata)
    return len(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--destination", type=Path, default=ROOT / "data" / "memes")
    args = parser.parse_args()
    print(f"Restored {restore(args.snapshot, args.destination)} images.")
