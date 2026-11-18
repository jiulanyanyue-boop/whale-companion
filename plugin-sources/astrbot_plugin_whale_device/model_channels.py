"""Use AstrBot providers as the single source of model channel selection."""

from __future__ import annotations

import asyncio
import copy
import os
import re
import time
from pathlib import Path

import aiohttp

DSH_PROVIDER_ID = "whale_dsh"
DSH_KEY_ENV = "WHALE_DSH_MODEL_KEY"
CHANNEL_NAMES = {
    "trae": "Trae 积分",
    "qoder": "Qoder",
    "deepseek-account": "DeepSeek 账号",
    "openai-codex": "Codex 账号",
    "our-free-model": "免费模型",
}


class ModelChannels:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = asyncio.Lock()
        # Plugin-only deployments can use the gateway on the same host as well.
        dsh_home = Path(
            os.environ.get("DSH_CHANNEL_PACK_STATE_DIR")
            or os.environ.get("DSH_HOME")
            or str(Path.home() / ".dsh")
        )
        key = os.environ.get("DSH_OPENAI_GATEWAY_API_KEY", "")
        if not key:
            try:
                key = (
                    (dsh_home / "openai-gateway/api-key")
                    .read_text(encoding="utf-8")
                    .strip()
                )
            except OSError:
                pass
        if key:
            os.environ[DSH_KEY_ENV] = key
        # httpx also reads the Windows system proxy. Local gateways must bypass it.
        bypass = ",".join(
            dict.fromkeys(
                item.strip()
                for item in ",".join(
                    [
                        os.environ.get("NO_PROXY", ""),
                        os.environ.get("no_proxy", ""),
                        "127.0.0.1,localhost,::1",
                    ]
                ).split(",")
                if item.strip()
            )
        )
        os.environ["NO_PROXY"] = os.environ["no_proxy"] = bypass

    async def catalog(self):
        """Read model metadata without making a paid inference request.

        Returns:
            Public channel choices and official profile selections, without keys.
        """
        context = self.plugin.context
        config = context.get_config()
        providers = config.get("provider", [])
        choices = [
            {
                "channel": "astrbot",
                "channel_name": "已配置的 API",
                "model": p["id"],
                "name": str(p.get("model") or p["id"]),
            }
            for p in providers
            if p.get("id") != DSH_PROVIDER_ID
            and p.get("enable")
            and p.get("provider_type", "chat_completion") == "chat_completion"
            and p.get("id") in context.provider_manager.inst_map
        ]
        error = ""
        key = os.environ.get(DSH_KEY_ENV, "")
        if not key:
            error = "未读取到 DSH 网关密钥，请先启动 DSH 并启用账号渠道网关。"
        else:
            try:
                port = int(os.environ.get("DSH_OPENAI_GATEWAY_PORT", "8326"))
                if not 1 <= port <= 65535:
                    raise ValueError("Invalid gateway port")
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=5), trust_env=False
                ) as session:
                    async with session.get(
                        f"http://127.0.0.1:{port}/v1/models",
                        headers={"Authorization": f"Bearer {key}"},
                        allow_redirects=False,
                    ) as response:
                        if response.status != 200:
                            raise ValueError("Gateway rejected catalog request")
                        data = await response.json()
                for item in data.get("data", []):
                    model = item.get("id", "")
                    if not isinstance(model, str) or "/" not in model:
                        continue
                    channel, name = model.split("/", 1)
                    choices.append(
                        {
                            "channel": channel,
                            "channel_name": CHANNEL_NAMES.get(channel, channel),
                            "model": model,
                            "name": name,
                            "multiplier": float(match.group(1))
                            if (
                                match := re.search(
                                    r"(?:×|[xX])\s*(\d+(?:\.\d+)?)",
                                    str(item.get("name", "")),
                                )
                            )
                            else None,
                            "modalities": [
                                "text",
                                "tool_use",
                                *(
                                    ["image"]
                                    if "image" in item.get("input", [])
                                    else []
                                ),
                            ],
                        }
                    )
            except (
                aiohttp.ClientError,
                TimeoutError,
                ValueError,
                TypeError,
                AttributeError,
            ):
                error = "DSH 模型网关暂不可用，请确认 DSH 正在运行、渠道账号已登录。"
        profiles = context.astrbot_config_mgr.confs
        profile_ids = [
            "default",
            *(
                pid
                for pid, profile in profiles.items()
                if profile.get("agent_runner", {})
                .get("config", {})
                .get("persona", {})
                .get("persona_id")
                in {
                    getattr(self.plugin, "owner_persona_id", "whale_owner"),
                    getattr(self.plugin, "group_persona_id", "whale_group"),
                }
            ),
        ]
        selections = []
        for profile_id in dict.fromkeys(profile_ids):
            profile = profiles.get(profile_id)
            if profile is None:
                continue
            provider_id = (
                profile.get("agent_runner", {})
                .get("config", {})
                .get("model", {})
                .get("provider_id", "")
            )
            provider = next((p for p in providers if p.get("id") == provider_id), {})
            model = provider.get("model", "")
            selections.append(
                {
                    "profile": profile_id,
                    "provider_id": provider_id,
                    "channel": model.split("/", 1)[0]
                    if provider_id == DSH_PROVIDER_ID
                    else "astrbot",
                    "model": model if provider_id == DSH_PROVIDER_ID else provider_id,
                }
            )
        return {"choices": choices, "selections": selections, "gateway_error": error}

    async def select(self, channel, model):
        """Save the default and Whale profiles through official provider objects.

        Args:
            channel: Channel identifier from the live catalog.
            model: Model identifier from the same catalog.

        Returns:
            Updated public channel metadata.

        Raises:
            ValueError: If the requested choice is no longer available.
            RuntimeError: If applying or persisting the selection fails.
        """
        async with self.lock:
            catalog = await self.catalog()
            choice = next(
                (
                    p
                    for p in catalog["choices"]
                    if p["channel"] == channel and p["model"] == model
                ),
                None,
            )
            if choice is None:
                raise ValueError("该渠道或模型当前不可用，请刷新列表后重试。")
            context = self.plugin.context
            manager = context.provider_manager
            provider_id = model if channel == "astrbot" else DSH_PROVIDER_ID
            profiles = context.astrbot_config_mgr.confs
            ids = dict.fromkeys(
                [
                    "default",
                    *(
                        pid
                        for pid, profile in profiles.items()
                        if profile.get("agent_runner", {})
                        .get("config", {})
                        .get("persona", {})
                        .get("persona_id")
                        in {
                            getattr(self.plugin, "owner_persona_id", "whale_owner"),
                            getattr(self.plugin, "group_persona_id", "whale_group"),
                        }
                    ),
                ]
            )
            targets = [profiles[i] for i in ids if i in profiles]
            if any(
                p.get("agent_runner", {}).get("runner_type", "local") != "local"
                for p in targets
            ):
                raise ValueError("当前使用外部 Agent，请在 AstrBot 官方配置中切换。")
            previous_provider = next(
                (
                    copy.deepcopy(p)
                    for p in context.get_config().get("provider", [])
                    if p.get("id") == DSH_PROVIDER_ID
                ),
                None,
            )
            if channel != "astrbot":
                config = {
                    "id": DSH_PROVIDER_ID,
                    "type": "openai_chat_completion",
                    "provider_type": "chat_completion",
                    "provider": "openai",
                    "enable": True,
                    "key": ["$" + DSH_KEY_ENV],
                    "api_base": f"http://127.0.0.1:{int(os.environ.get('DSH_OPENAI_GATEWAY_PORT', '8326'))}/v1",
                    "model": model,
                    "timeout": 120,
                    "proxy": "",
                    "custom_headers": {},
                    "modalities": choice.get("modalities", ["text", "tool_use"]),
                    "max_context_tokens": 32768,
                }
                try:
                    if previous_provider is not None:
                        await manager.update_provider(DSH_PROVIDER_ID, config)
                    else:
                        await manager.create_provider(config)
                except Exception:
                    if previous_provider is not None:
                        await manager.update_provider(
                            DSH_PROVIDER_ID, previous_provider
                        )
                    raise
            if provider_id not in manager.inst_map:
                raise RuntimeError("模型未能加载，原有选择仍然保留。")
            backups = [copy.deepcopy(p.get("agent_runner", {})) for p in targets]
            try:
                for profile in targets:
                    runner = profile.setdefault("agent_runner", {})
                    settings = runner.setdefault("config", {})
                    settings.setdefault("model", {})["provider_id"] = provider_id
                    settings.setdefault("compression", {})["provider_id"] = provider_id
                    profile.save_config()
            except Exception:
                if channel != "astrbot" and previous_provider is not None:
                    await manager.update_provider(DSH_PROVIDER_ID, previous_provider)
                for profile, backup in zip(targets, backups):
                    profile["agent_runner"] = backup
                    profile.save_config()
                raise
            return await self.catalog()

    async def account_rpc(self, method, payload):
        """Call the authenticated local companion without exposing credentials.

        Args:
            method: Allowlisted DSH management method.
            payload: Validated method arguments.

        Returns:
            The method result after DSH confirms success.

        Raises:
            RuntimeError: If the companion or the upstream operation is unavailable.
        """
        key = os.environ.get(DSH_KEY_ENV, "")
        if not key:
            raise RuntimeError("未读取到 DSH 网关密钥。")
        try:
            port = int(os.environ.get("WHALE_DSH_MANAGEMENT_PORT", "8327"))
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=65), trust_env=False
            ) as session:
                async with session.post(
                    f"http://127.0.0.1:{port}/rpc",
                    json={"method": method, "payload": payload},
                    headers={"Authorization": "Bearer " + key},
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        raise RuntimeError(
                            "DSH 管理服务未能完成请求，请在 DSH 检查账号状态。"
                        )
                    result = await response.json()
            if result.get("ok") is not True:
                raise RuntimeError(
                    "该操作未成功或渠道暂不支持，请在 DSH 检查账号登录和活动状态。"
                )
            return result.get("value") or {}
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise RuntimeError(
                "DSH 账号管理未连接，请重启 DSH 加载 Whale 渠道管理插件后重试。"
            ) from None

    async def accounts(self, channel, action="read", account_id=None, enabled=None):
        """Query balances or execute explicit account actions in DSH.

        Args:
            channel: Provider ID from the live model catalog.
            action: Read, renew, checkin, or autocheckin.
            account_id: Specific account selected for credential renewal.
            enabled: Shared DSH daily checkin preference.

        Returns:
            Public account balances and operation receipts.

        Raises:
            ValueError: If arguments do not match an available account or channel.
        """
        if channel == "astrbot" or channel not in {
            p["channel"] for p in (await self.catalog())["choices"]
        }:
            raise ValueError("当前渠道未提供 DSH 账号积分管理。")
        if action not in {"read", "renew", "checkin", "autocheckin"}:
            raise ValueError("不支持的账号操作。")
        listing = await self.account_rpc("account.list", {"provider": channel})
        entries = listing.get("accounts", [])
        receipt = None
        if action == "renew":
            account = next((a for a in entries if a.get("id") == account_id), None)
            if not account or not account.get("refreshable"):
                raise ValueError("该账号不支持静默续期，请在 DSH 重新登录。")
            result = await self.account_rpc(
                "account.refresh", {"accountId": account_id}
            )
            if result.get("success") is not True:
                raise RuntimeError("凭据续期未完成，请在 DSH 重新登录。")
            receipt = {"action": action, "success": True}
            entries = (
                await self.account_rpc("account.list", {"provider": channel})
            ).get("accounts", [])
        elif action == "checkin":
            result = await self.account_rpc("credits.claimAll", {"provider": channel})
            receipt = {
                "action": action,
                "summary": {
                    k: result.get("summary", {}).get(k, 0)
                    for k in (
                        "claimed",
                        "totalCredit",
                        "alreadyClaimed",
                        "inactive",
                        "failed",
                    )
                },
                "results": [
                    {
                        "account_id": x.get("accountId"),
                        "kind": x.get("outcome", {}).get("kind"),
                        "covers_today": x.get("outcome", {}).get("coversToday", True),
                    }
                    for x in result.get("results", [])
                ],
            }
        elif action == "autocheckin":
            if type(enabled) is not bool:
                raise ValueError("自动签到设置必须为开或关。")
            await self.account_rpc("usage.autoCheckin", {"enabled": enabled})
            receipt = {"action": action, "success": True}
        # Optional capabilities fail independently; unknown balance is never zero.
        balances, status, auto = await asyncio.gather(
            self.account_rpc("credits.balances", {"provider": channel}),
            self.account_rpc("credits.status", {"provider": channel}),
            self.account_rpc("usage.autoCheckin", {}),
            return_exceptions=True,
        )
        balance_map = (
            {a.get("accountId"): a for a in balances.get("accounts", [])}
            if isinstance(balances, dict)
            else {}
        )
        status_map = (
            {a.get("accountId"): a.get("status") for a in status.get("accounts", [])}
            if isinstance(status, dict)
            else {}
        )
        accounts = []
        for a in entries:
            balance = balance_map.get(a.get("id"), {}).get("balance")
            checkin = status_map.get(a.get("id"))
            accounts.append(
                {
                    "id": a.get("id"),
                    "name": a.get("nickname") or a.get("id"),
                    "enabled": a.get("enabled", True),
                    "expires_at": a.get("expiresAt"),
                    "refreshable": a.get("refreshable") is True,
                    "balance": {k: balance.get(k) for k in ("total", "packages")}
                    if isinstance(balance, dict)
                    else None,
                    "checkin": {
                        k: checkin.get(k)
                        for k in (
                            "active",
                            "todayCheckedIn",
                            "dailyCredit",
                            "todayCredit",
                            "actionRequired",
                        )
                    }
                    if isinstance(checkin, dict)
                    else None,
                }
            )
        return {
            "channel": channel,
            "accounts": accounts,
            "queried_at": time.time(),
            "receipt": receipt,
            "balance_error": str(balances) if isinstance(balances, Exception) else "",
            "checkin_supported": isinstance(status, dict),
            "auto_checkin": {
                "enabled": auto.get("autoCheckin", {}).get("enabled") is True
            }
            if isinstance(auto, dict)
            else None,
        }
