from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import secrets
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_ASTRBOT = REPO / "runtime" / "astrbot"
DEFAULT_HOST_CONFIG = REPO / "dist-exe" / "host.json"
PLUGIN = "astrbot_plugin_whale_device"


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if path.exists():
        backup = path.with_name(f"{path.name}.backup-{stamp}")
        shutil.copy2(path, backup)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def build_profile_routing(
    existing: dict[str, str], owner_id: str, owner_profile: str, group_profile: str
) -> dict[str, str]:
    owner_route = f"whale_qq:FriendMessage:{owner_id}"
    routing = dict(existing)
    routing["whale_qq:FriendMessage:*"] = group_profile
    routing["whale_qq:GroupMessage:*"] = group_profile
    routing[owner_route] = owner_profile
    return {owner_route: routing.pop(owner_route), **routing}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Configure the AstrBot-native Whale phone plugin."
    )
    parser.add_argument("--astrbot-root", type=Path, default=DEFAULT_ASTRBOT)
    parser.add_argument("--host-config", type=Path, default=DEFAULT_HOST_CONFIG)
    parser.add_argument(
        "--activate-desktop-mode",
        action="store_true",
        help="write the marker that disables the desktop app's legacy QQ consumer on its next start",
    )
    parser.add_argument(
        "--deactivate-desktop-mode",
        action="store_true",
        help="remove the desktop AstrBot-mode marker",
    )
    args = parser.parse_args()
    root = args.astrbot_root.resolve()
    host_path = args.host_config.resolve()
    if args.activate_desktop_mode and args.deactivate_desktop_mode:
        parser.error("activation and deactivation cannot be requested together")
    host = json.loads(host_path.read_text(encoding="utf-8-sig"))
    owner = str(host.get("owner_user_id", "")).strip()
    device_id = str(host.get("device_id", "")).strip()
    if not owner or not device_id:
        raise SystemExit("host config must contain owner_user_id and device_id")
    if (
        not (root / "main.py").is_file()
        or not (root / "data" / "cmd_config.json").is_file()
    ):
        raise SystemExit("AstrBot root is missing main.py or data/cmd_config.json")

    plugin_config_path = root / "data" / "config" / f"{PLUGIN}_config.json"
    plugin_config = (
        json.loads(plugin_config_path.read_text(encoding="utf-8-sig"))
        if plugin_config_path.is_file()
        else {}
    )
    plugin_config.update(
        {
            "owner_user_id": owner,
            "device_id": device_id,
            "platform_id": "whale_qq",
            "rpc_url": "http://127.0.0.1:8767/v1/device/execute-plan",
            "token_env": "WHALE_DEVICE_RPC_TOKEN",
        }
    )
    plugin_config.setdefault("owner_persona_id", "whale_owner")
    plugin_config.setdefault("group_persona_id", "whale_group")
    if args.activate_desktop_mode:
        plugin_config.update(
            deployment_mode="desktop", desktop_config_path=str(host_path)
        )
    elif args.deactivate_desktop_mode:
        plugin_config.update(deployment_mode="plugin", desktop_config_path="")
    atomic_json(plugin_config_path, plugin_config)

    cmd_path = root / "data" / "cmd_config.json"
    cmd = json.loads(cmd_path.read_text(encoding="utf-8-sig"))
    cmd.setdefault("platform_settings", {}).setdefault("segmented_reply", {}).update(
        enable=True, only_llm_result=True, split_mode="regex", regex=r".+"
    )
    cmd.setdefault("agent_runner", {}).setdefault("config", {}).setdefault("misc", {})[
        "max_steps"
    ] = 32
    provider_settings = cmd.setdefault("provider_settings", {})
    provider_settings["computer_use_runtime"] = "none"
    proactive = provider_settings.setdefault("proactive_capability", {})
    proactive["add_cron_tools"] = False

    providers = cmd.setdefault("provider", [])
    model_config = host.get("model", {})
    model_id = model_config.get("model_id", "deepseek-flash")
    api_base = model_config.get("base_url", "https://api.deepseek.com").rstrip("/")
    if not api_base.endswith("/v1"):
        api_base += "/v1"
    provider = next(
        (item for item in providers if item.get("id") == "whale_chat"), None
    )
    if provider is None:
        provider = {
            "id": "whale_chat",
            "type": "openai_chat_completion",
            "provider_type": "chat_completion",
            "provider": "openai",
            "enable": True,
            "key": ["$WHALE_MODEL_KEY"],
            "api_base": api_base,
            "model": model_id,
            "timeout": 120,
            "proxy": "",
            "custom_headers": {},
            "modalities": ["text", "image", "tool_use"],
        }
        providers.append(provider)
    else:
        provider.update(
            {
                "type": "openai_chat_completion",
                "provider_type": "chat_completion",
                "provider": "openai",
                "enable": True,
                "key": ["$WHALE_MODEL_KEY"],
                "api_base": api_base,
                "model": model_id,
                "modalities": ["text", "image", "tool_use"],
            }
        )
    runner = cmd.setdefault("agent_runner", {})
    runner["runner_type"] = "local"
    runner_config = runner.setdefault("config", {})
    runner_config.setdefault("model", {}).setdefault("provider_id", "whale_chat")
    persona_config = runner_config.setdefault("persona", {})
    persona_config["persona_id"] = "whale_girl"

    platforms = cmd.setdefault("platform", [])
    platform = next((item for item in platforms if item.get("id") == "whale_qq"), None)
    if platform is None:
        platform = {"id": "whale_qq", "type": "aiocqhttp", "enable": True}
        platforms.append(platform)
    platform.update(
        {
            "type": "aiocqhttp",
            "enable": True,
            "ws_reverse_host": "127.0.0.1",
            "ws_reverse_port": 6199,
            "ws_reverse_token": platform.get("ws_reverse_token")
            or secrets.token_urlsafe(36),
        }
    )
    runner = cmd.setdefault("agent_runner", {}).setdefault("config", {})
    runner.setdefault("misc", {})["tool_schema_mode"] = (
        "deferred"
        if (root / "astrbot/core/agent/tool_discovery.py").is_file()
        else "full"
    )
    runner.setdefault("compression", {}).update(max_turns=-1, keep_recent_ratio=0.3)
    runner["compression"].setdefault(
        "provider_id", runner_config["model"]["provider_id"]
    )
    provider["max_context_tokens"] = 32768
    atomic_json(cmd_path, cmd)

    os.environ["ASTRBOT_ROOT"] = str(root)
    os.chdir(root)
    sys.path.insert(0, str(root))
    from astrbot.core import astrbot_config, db_helper, sp
    from astrbot.core.astrbot_config_mgr import AstrBotConfigManager
    from astrbot.core.umop_config_router import UmopConfigRouter

    async def configure_profiles() -> None:
        await db_helper.initialize()
        await sp.initialize()
        router = UmopConfigRouter(sp)
        await router.initialize()
        manager = AstrBotConfigManager(astrbot_config, router, sp)
        await manager.initialize()

        profile_ids = plugin_config.setdefault("routing_profile_ids", {})
        defaults = manager.default_conf
        for persona_id in ("whale_owner", "whale_group"):
            profile_id = profile_ids.get(persona_id)
            if profile_id not in manager.confs:
                profile_id = next(
                    (
                        item["id"]
                        for item in manager.get_conf_list()
                        if item["id"] != "default"
                        and item["name"]
                        in {
                            persona_id,
                            "小鲸鱼 · 主人对话"
                            if persona_id == "whale_owner"
                            else "小鲸鱼 · 群友对话",
                        }
                    ),
                    None,
                )
            if not profile_id:
                profile_id = await manager.create_conf(
                    config=copy.deepcopy(dict(defaults)),
                    name="小鲸鱼 · 主人对话"
                    if persona_id == "whale_owner"
                    else "小鲸鱼 · 群友对话",
                )
            config = manager.confs[profile_id]
            config["agent_runner"] = copy.deepcopy(defaults.get("agent_runner", {}))
            config["agent_runner"].setdefault("config", {}).setdefault("model", {})[
                "provider_id"
            ] = defaults["agent_runner"]["config"]["model"]["provider_id"]
            persona = config["agent_runner"]["config"].setdefault("persona", {})
            persona["persona_id"] = persona_id
            config.save_config()
            profile_ids[persona_id] = profile_id

        routing = build_profile_routing(
            router.umop_to_conf_id,
            owner,
            profile_ids["whale_owner"],
            profile_ids["whale_group"],
        )
        await router.update_routing_data(routing)
        atomic_json(plugin_config_path, plugin_config)

    async def configure_astrbot() -> None:
        try:
            await configure_profiles()
            for persona_id in ("whale_owner", "whale_group"):
                persona = await db_helper.get_persona_by_id(persona_id)
                if persona is None:
                    raise RuntimeError(f"Whale persona {persona_id} was not found")
                await db_helper.update_persona(persona_id, tools=None)
        finally:
            await db_helper.engine.dispose()

    asyncio.run(configure_astrbot())

    marker = host_path.parent / "astrbot-device-mode.json"
    if args.activate_desktop_mode:
        atomic_json(marker, {"enabled": True, "mode": "astrbot"})
    elif args.deactivate_desktop_mode:
        marker.unlink(missing_ok=True)
    print("AstrBot plugin configuration and official tool policy are ready.")
    print(f"Plugin config: {plugin_config_path}")
    print(
        "Official provider: whale_chat (DeepSeek Flash); QQ reverse WebSocket: 127.0.0.1:6199/ws"
    )
    if args.activate_desktop_mode:
        print(
            "Desktop legacy QQ connection will be suppressed after restarting the desktop app."
        )
    elif args.deactivate_desktop_mode:
        print("Desktop AstrBot-mode marker removed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
