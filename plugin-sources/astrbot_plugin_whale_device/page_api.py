from __future__ import annotations

import asyncio
import inspect
from typing import Any

from quart import request

try:
    from .model_channels import ModelChannels
except ImportError:
    from model_channels import ModelChannels

PAGE_PREFIX = "/astrbot_plugin_whale_device/page"


async def _call(target: Any, name: str, *args, **kwargs):
    fn = getattr(target, name, None) if target is not None else None
    if not callable(fn):
        return None
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)


def _dict(v):
    return dict(v) if isinstance(v, dict) else {}


def _safe(v, allow_qr=False):
    s = _dict(v)
    blocked = {
        "token",
        "access_token",
        "refresh_token",
        "secret",
        "password",
        "authorization",
    }
    if not allow_qr:
        blocked.update({"qr", "qr_url", "qrcode", "qrcodeurl", "login_url", "qr_image"})
    return {str(k): x for k, x in s.items() if str(k).lower() not in blocked}


async def _body():
    try:
        return _dict(await request.get_json(silent=True))
    except Exception:
        return {}


class WhaleDevicePageApi:
    def __init__(self, plugin):
        self.plugin = plugin
        self.model_channels = ModelChannels(plugin)

    def register_routes(self):
        register = self.plugin.context.register_web_api
        register(
            f"{PAGE_PREFIX}/activities",
            self.activities,
            ["GET"],
            "Whale personal activities",
        )
        register(
            f"{PAGE_PREFIX}/activities",
            self.update_activities,
            ["POST"],
            "Whale personal activity settings",
        )
        for name, fn, methods in (
            ("model-accounts", self.model_accounts, ["POST"]),
            ("models", self.models, ["GET"]),
            ("models", self.update_models, ["POST"]),
            ("status", self.status, ["GET"]),
            ("settings", self.settings, ["GET"]),
            ("settings", self.update_settings, ["POST"]),
            ("pairing", self.pairing, ["GET"]),
            ("apps", self.apps, ["GET"]),
            ("apps", self.update_apps, ["POST"]),
            ("qq", self.qq, ["GET"]),
            ("qq/login-qr", self.qq_login_qr, ["GET"]),
            ("qq/login", self.qq_login, ["POST"]),
            ("qq/logout", self.qq_logout, ["POST"]),
            ("qq/auto-login", self.qq_auto_login, ["POST"]),
            ("personas", self.personas, ["GET"]),
        ):
            register(f"{PAGE_PREFIX}/{name}", fn, methods, f"Whale device page {name}")

    async def model_accounts(self):
        body = await _body()
        if not isinstance(body.get("channel"), str) or set(body) - {
            "channel",
            "action",
            "account_id",
            "enabled",
        }:
            return {"status": "error", "message": "账号操作格式无效"}, 400
        try:
            return {"status": "ok", "data": await self.model_channels.accounts(**body)}
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}, 400
        except RuntimeError as exc:
            return {"status": "error", "message": str(exc)}, 503
        except Exception:
            return {"status": "error", "message": "账号操作未完成，请稍后重试。"}, 503

    async def models(self):
        return {"status": "ok", "data": await self.model_channels.catalog()}

    async def update_models(self):
        body = await _body()
        if set(body) != {"channel", "model"} or any(
            not isinstance(body[k], str) or not body[k] for k in ("channel", "model")
        ):
            return {"status": "error", "message": "请选择有效的渠道和模型"}, 400
        try:
            data = await self.model_channels.select(body["channel"], body["model"])
            return {"status": "ok", "data": data}
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}, 400
        except Exception:
            return {
                "status": "error",
                "message": "模型切换失败，请检查 AstrBot 日志后重试。",
            }, 503

    async def settings(self):
        connection = (
            await _call(
                getattr(self.plugin, "device_provider", None), "connection_settings"
            )
            or {}
        )
        return {"status": "ok", "data": connection}

    async def update_settings(self):
        body = await _body()
        if set(body) != {"public_url"}:
            return {"status": "error", "message": "设置格式无效"}, 400
        try:
            result = await _call(
                getattr(self.plugin, "device_provider", None),
                "set_connection_settings",
                body["public_url"],
            )
            if result is None:
                return {"status": "error", "message": "手机服务尚未就绪"}, 409
            self.plugin.config["device_public_url"] = result["public_url"]
            save = getattr(self.plugin.config, "save_config", None)
            if callable(save):
                await asyncio.to_thread(save)
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}, 400
        except Exception:
            return {"status": "error", "message": "设置保存失败，请重试"}, 503
        return await self.settings()

    async def status(self):
        try:
            d = (
                await _call(getattr(self.plugin, "device_provider", None), "status")
                or {}
            )
        except Exception:
            d = {
                "connected": False,
                "error": "手机服务暂不可用，请检查桌面主机是否已启动。",
            }
        q = (
            await _call(getattr(self.plugin, "qq_account_controller", None), "status")
            or {}
        )
        return {
            "status": "ok",
            "data": {
                "mode": self.plugin.config.get("deployment_mode", "plugin"),
                "device": _safe(d)
                or {"connected": False, "paired": bool(self.plugin.device_id)},
                "qq": _safe(q),
            },
        }

    async def pairing(self):
        d = getattr(self.plugin, "device_provider", None)
        v = await _call(d, "pairing_info")
        return {
            "status": "ok",
            "data": _safe(v, True)
            if v
            else {"paired": bool(self.plugin.device_id), "available": False},
        }

    async def apps(self):
        v = (
            await _call(getattr(self.plugin, "device_provider", None), "allowed_apps")
            or []
        )
        return {
            "status": "ok",
            "data": {"apps": v if isinstance(v, list) else _dict(v).get("apps", [])},
        }

    async def update_apps(self):
        b = await _body()
        apps = b.get("apps")
        if not isinstance(apps, list) or any(
            not isinstance(x, (dict, str)) for x in apps
        ):
            return {"status": "error", "message": "应用列表格式无效"}, 400
        apps = [x.get("package_name", "") if isinstance(x, dict) else x for x in apps]
        v = await _call(
            getattr(self.plugin, "device_provider", None), "set_allowed_apps", apps
        )
        if v is None:
            return {"status": "error", "message": "当前部署未提供应用权限管理"}, 409
        return {"status": "ok", "data": _safe(v) or {"apps": apps}}

    async def qq(self):
        return {
            "status": "ok",
            "data": _safe(
                await _call(
                    getattr(self.plugin, "qq_account_controller", None), "status"
                )
                or {}
            ),
        }

    async def qq_login_qr(self):
        try:
            return {
                "status": "ok",
                "data": _safe(
                    await _call(
                        getattr(self.plugin, "qq_account_controller", None), "login_qr"
                    )
                    or {"available": False},
                    True,
                ),
            }
        except Exception:
            return {
                "status": "ok",
                "data": {"available": False, "message": "正在等待登录二维码。"},
            }

    async def qq_login(self):
        c = getattr(self.plugin, "qq_account_controller", None)
        if c is None:
            return {
                "status": "error",
                "message": "纯插件模式请在官方平台配置中登录 QQ",
            }, 409
        try:
            return {"status": "ok", "data": _safe(await _call(c, "login") or {})}
        except Exception:
            return {"status": "error", "message": "QQ登录请求未能提交"}, 503

    async def qq_logout(self):
        c = getattr(self.plugin, "qq_account_controller", None)
        if c is None:
            return {
                "status": "error",
                "message": "纯插件模式请在官方平台配置中管理 QQ 登录",
            }, 409
        try:
            return {"status": "ok", "data": _safe(await _call(c, "cancel_login") or {})}
        except Exception:
            return {"status": "error", "message": "QQ下线请求未能提交"}, 503

    async def qq_auto_login(self):
        c = getattr(self.plugin, "qq_account_controller", None)
        if c is None:
            return {
                "status": "error",
                "message": "纯插件模式不提供 EXE 启动自动登录",
            }, 409
        b = await _body()
        if type(b.get("enabled")) is not bool:
            return {"status": "error", "message": "自动登录开关格式无效"}, 400
        try:
            return {
                "status": "ok",
                "data": _safe(await _call(c, "set_auto_login", b["enabled"]) or {}),
            }
        except Exception:
            return {"status": "error", "message": "自动登录设置未能保存"}, 503

    async def personas(self):
        return {
            "status": "ok",
            "data": {
                "owner": {"id": self.plugin.owner_persona_id, "url": "/#/persona"},
                "group": {"id": self.plugin.group_persona_id, "url": "/#/persona"},
            },
        }

    async def activities(self):
        activities = getattr(self.plugin, "activities", None)
        return {
            "status": "ok",
            "data": activities.status()
            if activities
            else {"enabled": False, "last_status": "starting"},
        }

    async def update_activities(self):
        body = await _body()
        activities = getattr(self.plugin, "activities", None)
        if activities is None:
            return {"status": "error", "message": "自主活动正在启动"}, 409
        if set(body) == {"pause"} and body["pause"] is True:
            activities.pause()
        elif set(body) == {"run_now"} and body["run_now"] is True:
            if not activities.request_now():
                return {
                    "status": "error",
                    "message": "请先开启自主活动，或等待本次活动结束",
                }, 409
        elif set(body) == {"enabled"} and type(body["enabled"]) is bool:
            self.plugin.config["autonomy_enabled"] = body["enabled"]
            self.plugin.config.save_config()
            if body["enabled"]:
                activities.closed = False
                activities.pause(0)
                activities.start()
            else:
                await activities.stop()
        elif body and set(body) <= {"start_hour", "end_hour", "browse_during_quiet"}:
            start = body.get(
                "start_hour", self.plugin.config.get("autonomy_start_hour", 8)
            )
            end = body.get("end_hour", self.plugin.config.get("autonomy_end_hour", 23))
            browse = body.get(
                "browse_during_quiet",
                self.plugin.config.get("autonomy_browse_during_quiet", True),
            )
            if (
                type(start) is not int
                or type(end) is not int
                or not 0 <= start < end <= 24
                or type(browse) is not bool
            ):
                return {
                    "status": "error",
                    "message": "请填写有效的北京时间段（开始早于结束，0至24小时）",
                }, 400
            self.plugin.config.update(
                autonomy_start_hour=start,
                autonomy_end_hour=end,
                autonomy_browse_during_quiet=browse,
                autonomy_timezone="Asia/Shanghai",
            )
            self.plugin.config.save_config()
        else:
            return {"status": "error", "message": "自主活动设置格式无效"}, 400
        return await self.activities()
