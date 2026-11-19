"""修复固定版 AstrBot 平铺侧栏只显示插件第一页的问题，不改用户设置。"""

from __future__ import annotations

import argparse
import hashlib
import os
import tempfile
from pathlib import Path


ASSET_PATH = Path("assets/ReadmeDialog-DszoJEdN.js")
ORIGINAL_SHA256 = "e8a75db40206e01f5c9883f6da77e6c0e42d8964034a30693a4a9295942eb3f8"
PATCHED_SHA256 = "d4f22ff6590667102ab4fac771164bc12a5b862f3a497f152675de7b037ae723"
BUNDLE_BEFORE = b".map(c=>ce(c,c.pages[0],l,!1))"
BUNDLE_AFTER = b".flatMap(c=>c.pages.map(p=>ce(c,p,l,!1)))"
SOURCE_BEFORE = """  const children: menu[] = activeWithPages.map((p) => {
    return buildPageItem(p, p.pages[0], locale, false);
  });"""
SOURCE_AFTER = """  // 平铺模式保留每个页面，新增页面不能覆盖原来的插件入口。
  const children: menu[] = activeWithPages.flatMap((p) =>
    p.pages.map((page) => buildPageItem(p, page, locale, false)),
  );"""


def patch_source_text(text: str) -> str:
    """同步对应 TypeScript 源码，保留单页面标题行为和原有分组逻辑。"""
    if text.count(SOURCE_AFTER) == 1 and SOURCE_BEFORE not in text:
        return text
    if text.count(SOURCE_BEFORE) != 1 or SOURCE_AFTER in text:
        raise ValueError("Unsupported AstrBot sidebar source; refusing to guess a patch")
    return text.replace(SOURCE_BEFORE, SOURCE_AFTER, 1)


def _atomic_write(path: Path, content: bytes) -> None:
    """在目标目录内原子替换代码文件，失败时清理临时文件。"""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".whale-sidebar-", delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def patch_dashboard(dashboard: Path, *, source: Path | None = None) -> bool:
    """仅修改已知官方包；先校验所有输入，再写入，重复安装不会重复应用补丁。"""
    asset = dashboard / ASSET_PATH
    original = asset.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    if digest == PATCHED_SHA256:
        updated = original
    elif digest == ORIGINAL_SHA256 and original.count(BUNDLE_BEFORE) == 1:
        updated = original.replace(BUNDLE_BEFORE, BUNDLE_AFTER, 1)
        if hashlib.sha256(updated).hexdigest() != PATCHED_SHA256:
            raise ValueError("Sidebar patch output checksum mismatch")
    else:
        raise ValueError("Unsupported AstrBot dashboard bundle; refusing to modify it")

    source_update = None
    if source is not None:
        source_text = source.read_text(encoding="utf-8")
        replacement = patch_source_text(source_text)
        if replacement != source_text:
            source_update = replacement.encode("utf-8")
    if source_update is not None:
        _atomic_write(source, source_update)
    if updated != original:
        _atomic_write(asset, updated)
    return updated != original or source_update is not None


def main() -> None:
    """供安装器和当前安装使用；不启动服务，不下载资产，也不读取账号资料。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard", required=True, type=Path)
    parser.add_argument("--source", type=Path)
    args = parser.parse_args()
    changed = patch_dashboard(args.dashboard, source=args.source)
    print("Plugin sidebar patched." if changed else "Plugin sidebar already patched.")


if __name__ == "__main__":
    main()
