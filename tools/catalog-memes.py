"""Catalog meme pixels with the configured AstrBot OpenAI-compatible vision provider.

Run from the repository root. Completed sidecars are checkpoints, so reruns only
retry missing/failed entries when --retry-failed is supplied.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ASTRBOT_ROOT = ROOT / "runtime" / "astrbot"
os.environ.setdefault("ASTRBOT_ROOT", str(ASTRBOT_ROOT))
sys.path.insert(0, str(ASTRBOT_ROOT))
sys.path.insert(0, str(ROOT / "host"))

from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot_whale_device.gui_secrets import StoredSecrets
from astrbot_whale_device.host_config import HostConfig
from astrbot_whale_device.memes.catalog import CATALOG_PROMPT, parse_catalog
from astrbot_whale_device.memes.library import MemeLibrary

IMAGE_SUFFIXES = {".webp", ".jpg", ".jpeg", ".png"}


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def is_reference(path: Path) -> bool:
    return "母图" in path.name


def error_text(exc: BaseException) -> str:
    # Keep reports useful without exposing provider credentials or response bodies.
    text = str(exc).replace("\n", " ")
    for marker in ("sk-", "Bearer ", "api_key", "Authorization"):
        if marker in text:
            return f"{type(exc).__name__}: provider error (details redacted)"
    return f"{type(exc).__name__}: {text[:300]}"


def provider_from_config() -> ProviderOpenAIOfficial:
    config_path = ROOT / "dist-exe" / "host.json"
    config = HostConfig.load(config_path)
    secrets = StoredSecrets.load(config_path)
    key = secrets.model_api_key
    api_base = config.model.base_url.rstrip("/")
    if not api_base.endswith("/v1"):
        api_base += "/v1"
    provider_config = {
        "id": "whale_meme_catalog",
        "type": "openai_chat_completion",
        "provider": "openai",
        "key": [key] if key else [],
        "api_base": api_base,
        "model": config.model.model_id,
        "timeout": 90,
        "modalities": ["text", "image", "tool_use"],
    }
    return ProviderOpenAIOfficial(provider_config, {})


async def catalog_one(provider: ProviderOpenAIOfficial, path: Path, *, semaphore: asyncio.Semaphore) -> dict[str, Any]:
    async with semaphore:
        started = time.time()
        try:
            response = await asyncio.wait_for(provider.text_chat(prompt=CATALOG_PROMPT, image_urls=[str(path.resolve())], request_max_retries=1), timeout=110)
            metadata = parse_catalog(response.completion_text)
            return {"status": "ok", "metadata": metadata, "seconds": round(time.time() - started, 2)}
        except Exception as exc:
            return {"status": "failed", "error": error_text(exc), "seconds": round(time.time() - started, 2)}


async def run(args: argparse.Namespace) -> int:
    image_dir = Path(args.image_dir).resolve()
    sidecar_dir = image_dir.parent / "catalog"
    library = MemeLibrary(image_dir.parent)
    files = sorted((p for p in image_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES), key=lambda p: p.name.casefold())
    provider = provider_from_config()
    semaphore = asyncio.Semaphore(3)
    summary: list[dict[str, Any]] = []
    try:
        async def process(path: Path) -> dict[str, Any]:
            sidecar = sidecar_dir / f"{path.name}.json"
            existing = None
            if sidecar.is_file():
                try:
                    existing = json.loads(sidecar.read_text(encoding="utf-8"))
                except Exception:
                    existing = None
            if existing and existing.get("status") == "ok":
                return existing
            if existing and existing.get("status") == "failed" and not args.retry_failed:
                return existing
            result = await catalog_one(provider, path, semaphore=semaphore)
            record = {"file": path.name, "role": "reference_only" if is_reference(path) else "searchable", **result}
            if result["status"] == "ok":
                metadata = result["metadata"]
                digest = library.add(path, source="catalog_import", role=record["role"], **metadata)
                record["digest"] = digest
            atomic_json(sidecar, record)
            return record
        summary = await asyncio.gather(*(process(path) for path in files))
    finally:
        await provider.client.close()
    ok = sum(x.get("status") == "ok" for x in summary)
    failed = len(summary) - ok
    markdown = ["# Meme Catalog", "", f"成功：{ok}；失败：{failed}；总数：{len(summary)}", "", "| 文件 | 状态 | 角色 | OCR文字 | 含义 |", "|---|---|---|---|---|"]
    for item in summary:
        metadata = item.get("metadata", {})
        markdown.append("| " + " | ".join(str(item.get(k, "")).replace("|", "\\|").replace("\n", " ") for k in ("file", "status", "role")) + " | " + str(metadata.get("image_text", "")).replace("|", "\\|").replace("\n", "<br>") + " | " + str(metadata.get("meaning", "")).replace("|", "\\|").replace("\n", "<br>") + " |")
    atomic_json(sidecar_dir / "index.json", {"total": len(summary), "ok": ok, "failed": failed, "items": summary})
    (sidecar_dir / "README.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    print(json.dumps({"total": len(summary), "success": ok, "failed": failed, "catalog": str(sidecar_dir)}, ensure_ascii=False))
    return 0 if failed == 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image-dir", default=str(ROOT / "data" / "memes" / "images"))
    parser.add_argument("--retry-failed", action="store_true")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
