"""Prepare explicit test recordings and optionally download a local ASR model."""

import argparse
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "runtime/astrbot/data/plugins/astrbot_plugin_whale_device"


def main():
    """Prepare voice assets without sending a message or changing QQ sessions."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-model", action="store_true")
    parser.add_argument("--model", default="base", choices=["tiny", "base", "small"])
    parser.add_argument(
        "--model-cache",
        type=Path,
        help="Override the AstrBot plugin voice model cache.",
    )
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location(
        "whale_voice_setup", PLUGIN / "voice.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assets = PLUGIN / "voice_assets"
    assets.mkdir(exist_ok=True)
    catalog = []
    for clip_id, text in [
        ("test_greeting", "你好，我是小鲸鱼。这是一条测试语音，能听清楚吗？"),
        ("test_acknowledge", "好的，我听到了。我现在使用的是临时测试音色。"),
        ("test_goodbye", "那我们晚点再聊，记得好好休息。"),
    ]:
        path = assets / f"{clip_id}.wav"
        if not path.exists():
            module.VoiceService.synthesize_test_voice(text, path)
        catalog.append(
            {"id": clip_id, "file": path.name, "text": text, "test_voice": True}
        )
    catalog_path = assets / "catalog.json"
    if not catalog_path.exists():
        catalog_path.write_text(
            json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    print("Prepared three Chinese test clips; no QQ messages sent.")
    if args.download_model:
        from faster_whisper import download_model

        cache = args.model_cache or (
            ROOT
            / "runtime/astrbot/data/plugin_data/astrbot_plugin_whale_device/voice/models"
        )
        path = download_model(args.model, cache_dir=str(cache))
        print(f"Local ASR model ready: {path}")


if __name__ == "__main__":
    main()
