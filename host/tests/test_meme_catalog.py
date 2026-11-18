import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astrbot_whale_device.memes.catalog import parse_catalog
from astrbot_whale_device.memes.library import MemeLibrary


def test_reference_role_excluded_unless_requested(tmp_path):
    library = MemeLibrary(tmp_path)
    digest = library.add(b"reference", description="多个模型角色", meaning="设定图", role="reference_only")
    assert library.search("模型", minimum_score=0) == []
    assert library.search("模型", minimum_score=0, include_reference=True)[0]["digest"] == digest


def test_import_from_images_preserves_filename(tmp_path):
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    image = image_dir / "name.webp"
    image.write_bytes(b"RIFF0000WEBP test")
    library = MemeLibrary(tmp_path)
    digest = library.add(image, description="猫", meaning="回应")
    assert Path(library.get(digest)["path"]).name == image.name


def test_parse_catalog_json_and_reject_empty_meaning():
    value = {"description": "画面", "image_text": "小字", "meaning": "惊讶", "emotion": "惊讶", "scene": "聊天"}
    assert parse_catalog(json.dumps(value, ensure_ascii=False)) == value
    value["meaning"] = ""
    try:
        parse_catalog(json.dumps(value, ensure_ascii=False))
    except ValueError:
        pass
    else:
        raise AssertionError("empty meaning must be rejected")

