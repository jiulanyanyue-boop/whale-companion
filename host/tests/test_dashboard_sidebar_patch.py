"""锁定多页面菜单修复的版本边界、幂等性和未识别资产保护。"""

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "tools/patch_dashboard_sidebar.py"
spec = importlib.util.spec_from_file_location("whale_sidebar_patch", SCRIPT)
sidebar = importlib.util.module_from_spec(spec)


def load_patch():
    """延迟导入维护脚本，使测试能独立于 AstrBot 与前端依赖运行。"""
    spec.loader.exec_module(sidebar)
    return sidebar


def test_sidebar_source_keeps_all_pages_and_single_page_title_behavior():
    """仅从 map 首页面改为 flatMap 全页面，不改变既有标题或分组处理。"""
    patch = load_patch()
    updated = patch.patch_source_text(patch.SOURCE_BEFORE)
    assert updated == patch.SOURCE_AFTER
    assert "flatMap" in updated
    assert "p.pages[0]" not in updated
    assert "buildPageItem(p, page, locale, false)" in updated
    assert patch.patch_source_text(updated) == updated


def test_sidebar_source_rejects_unknown_or_duplicate_code():
    """上游变化或重复片段必须明确失败，不能猜测替换。"""
    patch = load_patch()
    for value in ("unsupported source", patch.SOURCE_BEFORE * 2):
        with pytest.raises(ValueError):
            patch.patch_source_text(value)


def test_sidebar_asset_rejects_unrecognized_build_without_modifying_file(tmp_path):
    """不能将固定版本补丁盲目应用到其他构建或用户手动修改的包。"""
    patch = load_patch()
    dashboard = tmp_path / "dist"
    asset = dashboard / patch.ASSET_PATH
    asset.parent.mkdir(parents=True)
    original = b"unknown bundle " + patch.BUNDLE_BEFORE
    asset.write_bytes(original)
    with pytest.raises(ValueError):
        patch.patch_dashboard(dashboard)
    assert asset.read_bytes() == original


def test_whale_page_titles_cover_main_and_sts2():
    """新安装时同时保留原主机页面与新增联机页面的中文菜单名。"""
    import json

    root = SCRIPT.parents[1] / "plugin-sources/astrbot_plugin_whale_device"
    labels = json.loads((root / ".astrbot-plugin/i18n/zh-CN.json").read_text("utf-8"))
    assert labels["views"]["whale"]["title"] == "Whale 主机"
    assert labels["views"]["sts2"]["title"] == "STS2 联机"
