import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from model_channels import DSH_KEY_ENV, DSH_PROVIDER_ID, ModelChannels


class Config(dict):
    fail_once = False

    def save_config(self):
        if self.fail_once:
            self.fail_once = False
            raise OSError("disk unavailable")


def make_service(monkeypatch):
    monkeypatch.setenv("DSH_OPENAI_GATEWAY_API_KEY", "private-test-key")
    monkeypatch.setenv("DSH_OPENAI_GATEWAY_PORT", "8326")
    native = {
        "id": "whale_chat",
        "enable": True,
        "model": "native",
        "key": ["native-secret"],
    }
    default = Config(
        provider=[native],
        agent_runner={
            "runner_type": "local",
            "config": {"model": {"provider_id": "whale_chat"}},
        },
    )
    owner = Config(agent_runner=copy.deepcopy(default["agent_runner"]))
    group = Config(agent_runner=copy.deepcopy(default["agent_runner"]))
    owner["agent_runner"]["config"]["persona"] = {"persona_id": "whale_owner"}
    group["agent_runner"]["config"]["persona"] = {"persona_id": "whale_group"}
    unrelated = Config(agent_runner=copy.deepcopy(default["agent_runner"]))
    configs = {
        "default": default,
        "owner": owner,
        "group": group,
        "unrelated": unrelated,
    }
    manager = SimpleNamespace(inst_map={"whale_chat": object()})

    async def create(config):
        default["provider"].append(config)
        manager.inst_map[config["id"]] = object()

    async def update(identifier, config):
        default["provider"] = [
            config if p["id"] == identifier else p for p in default["provider"]
        ]
        manager.inst_map[identifier] = object()

    manager.create_provider = AsyncMock(side_effect=create)
    manager.update_provider = AsyncMock(side_effect=update)
    context = SimpleNamespace(
        get_config=lambda: default,
        provider_manager=manager,
        astrbot_config_mgr=SimpleNamespace(confs=configs),
    )
    service = ModelChannels(
        SimpleNamespace(
            context=context,
            config={},
        )
    )
    return service, configs, manager


def choices():
    return {
        "choices": [
            {"channel": "trae", "model": "trae/test"},
            {"channel": "astrbot", "model": "whale_chat"},
        ],
        "selections": [],
    }


def test_switch_unifies_official_profiles_and_compression_without_copying_key(
    monkeypatch,
):
    service, profiles, manager = make_service(monkeypatch)
    service.catalog = AsyncMock(return_value=choices())
    asyncio.run(service.select("trae", "trae/test"))
    for name in ("default", "owner", "group"):
        settings = profiles[name]["agent_runner"]["config"]
        assert settings["model"]["provider_id"] == DSH_PROVIDER_ID
        assert settings["compression"]["provider_id"] == DSH_PROVIDER_ID
    assert (
        profiles["unrelated"]["agent_runner"]["config"]["model"]["provider_id"]
        == "whale_chat"
    )
    saved = profiles["default"]["provider"][-1]
    assert saved["key"] == ["$" + DSH_KEY_ENV]
    assert "private-test-key" not in json.dumps(saved)
    asyncio.run(service.select("astrbot", "whale_chat"))
    assert (
        profiles["owner"]["agent_runner"]["config"]["model"]["provider_id"]
        == "whale_chat"
    )
    assert profiles["default"]["provider"][0]["key"] == ["native-secret"]
    manager.create_provider.assert_awaited_once()


def test_catalog_only_reads_metadata_and_never_returns_secrets(monkeypatch):
    service, _, _ = make_service(monkeypatch)
    calls = []

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def json(self):
            return {
                "data": [
                    {
                        "id": "trae/test",
                        "name": "Test ×0.39",
                        "input": ["text", "image"],
                    },
                    {"id": "qoder/auto"},
                    {"id": 17},
                ]
            }

    class Session(Response):
        def __init__(self, **kwargs):
            assert kwargs["trust_env"] is False

        def get(self, url, **kwargs):
            calls.append(url)
            assert kwargs["allow_redirects"] is False
            return Response()

    monkeypatch.setattr("model_channels.aiohttp.ClientSession", Session)
    data = asyncio.run(service.catalog())
    assert calls == ["http://127.0.0.1:8326/v1/models"]
    assert len(data["choices"]) == 3
    assert "image" in data["choices"][1]["modalities"]
    assert data["choices"][1]["multiplier"] == 0.39
    assert data["choices"][2]["multiplier"] is None
    assert "image" not in data["choices"][2]["modalities"]
    assert not data["gateway_error"]
    assert "secret" not in json.dumps(data)
    assert "private-test-key" not in json.dumps(data)


def test_offline_gateway_keeps_native_choice_and_rejects_stale_model(monkeypatch):
    service, _, manager = make_service(monkeypatch)
    monkeypatch.delenv(DSH_KEY_ENV, raising=False)
    data = asyncio.run(service.catalog())
    assert data["gateway_error"]
    assert [p["channel"] for p in data["choices"]] == ["astrbot"]
    with pytest.raises(ValueError):
        asyncio.run(service.select("trae", "trae/stale"))
    manager.create_provider.assert_not_awaited()
    asyncio.run(service.select("astrbot", "whale_chat"))


def test_profile_save_failure_restores_all_profile_selections(monkeypatch):
    service, profiles, _ = make_service(monkeypatch)
    service.catalog = AsyncMock(return_value=choices())
    profiles["group"].fail_once = True
    with pytest.raises(OSError):
        asyncio.run(service.select("trae", "trae/test"))
    for profile in profiles.values():
        assert profile["agent_runner"]["config"]["model"]["provider_id"] == "whale_chat"


def test_reload_failure_restores_existing_dsh_provider(monkeypatch):
    service, profiles, manager = make_service(monkeypatch)
    service.catalog = AsyncMock(return_value=choices())
    asyncio.run(service.select("trae", "trae/test"))
    previous = copy.deepcopy(profiles["default"]["provider"][-1])
    manager.update_provider.side_effect = [RuntimeError("cannot load adapter"), None]
    with pytest.raises(RuntimeError):
        asyncio.run(service.select("trae", "trae/test"))
    assert manager.update_provider.await_args_list[-1].args == (
        DSH_PROVIDER_ID,
        previous,
    )


def test_external_agent_rejected_before_changing_provider(monkeypatch):
    service, profiles, manager = make_service(monkeypatch)
    service.catalog = AsyncMock(return_value=choices())
    profiles["group"]["agent_runner"]["runner_type"] = "external"
    with pytest.raises(ValueError):
        asyncio.run(service.select("trae", "trae/test"))
    manager.create_provider.assert_not_awaited()


def test_local_gateway_bypasses_system_proxy_without_losing_existing_exclusions(
    monkeypatch,
):
    monkeypatch.setenv("NO_PROXY", "example.test,intranet.test")
    monkeypatch.setenv("no_proxy", "example.test,intranet.test")
    make_service(monkeypatch)
    import os

    expected = "example.test,intranet.test,127.0.0.1,localhost,::1"
    assert os.environ["NO_PROXY"] == expected
    assert os.environ["no_proxy"] == expected
    make_service(monkeypatch)
    assert os.environ["NO_PROXY"] == expected


def test_account_actions_keep_unknown_balance_and_partial_claim_results(monkeypatch):
    service, _, _ = make_service(monkeypatch)
    service.catalog = AsyncMock(return_value=choices())
    calls = []

    async def rpc(method, payload):
        calls.append((method, payload))
        if method == "account.list":
            return {
                "accounts": [
                    {
                        "id": "a",
                        "nickname": "Alice",
                        "refreshable": True,
                        "credentialRef": "must-not-leak",
                    },
                    {"id": "b", "refreshable": False},
                ]
            }
        if method == "account.refresh":
            return {"success": True}
        if method == "credits.balances":
            return {
                "accounts": [
                    {"accountId": "a", "balance": {"total": 12.5, "packages": []}},
                    {"accountId": "b", "balance": None},
                ]
            }
        if method == "credits.status":
            return {
                "accounts": [
                    {
                        "accountId": "a",
                        "status": {
                            "active": True,
                            "todayCheckedIn": False,
                            "dailyCredit": 100,
                        },
                    }
                ]
            }
        if method == "usage.autoCheckin":
            return {"autoCheckin": {"enabled": True}}
        if method == "credits.claimAll":
            return {
                "summary": {"claimed": 1, "totalCredit": 100, "failed": 1},
                "results": [
                    {
                        "accountId": "b",
                        "outcome": {"kind": "failed", "coversToday": False},
                    }
                ],
            }

    service.account_rpc = rpc
    data = asyncio.run(service.accounts("trae"))
    assert data["accounts"][0]["balance"]["total"] == 12.5
    assert data["accounts"][1]["balance"] is None
    assert "credentialRef" not in json.dumps(data)
    assert not any(method == "credits.claimAll" for method, _ in calls)
    with pytest.raises(ValueError):
        asyncio.run(service.accounts("trae", "renew", "b"))
    data = asyncio.run(service.accounts("trae", "renew", "a"))
    assert data["receipt"]["success"] is True
    data = asyncio.run(service.accounts("trae", "checkin"))
    assert data["receipt"]["summary"]["failed"] == 1
    assert data["receipt"]["results"][0]["covers_today"] is False
    with pytest.raises(ValueError):
        asyncio.run(service.accounts("trae", "autocheckin", enabled="true"))
    asyncio.run(service.accounts("trae", "autocheckin", enabled=False))
    assert ("usage.autoCheckin", {"enabled": False}) in calls


def test_failed_account_renewal_is_never_reported_as_success(monkeypatch):
    service, _, _ = make_service(monkeypatch)
    service.catalog = AsyncMock(return_value=choices())
    service.account_rpc = AsyncMock(
        side_effect=[
            {"accounts": [{"id": "a", "refreshable": True}]},
            {"success": False},
        ]
    )
    with pytest.raises(RuntimeError):
        asyncio.run(service.accounts("trae", "renew", "a"))
