from __future__ import annotations

import asyncio
import inspect
import ntpath
from functools import wraps
from typing import Any

from quart import request

try:
    from .model_channels import ModelChannels
    from .sts2_lan import validate_game_port, validate_public_address, validate_share_address
    from .sts2_relay import relay_settings
except ImportError:
    from model_channels import ModelChannels
    from sts2_lan import validate_game_port, validate_public_address, validate_share_address
    from sts2_relay import relay_settings

PAGE_PREFIX = "/astrbot_plugin_whale_device/page"


def _serialize_sts2(handler):
    """串行化页面的 STS2 写操作，拒绝重复点击或多页面并发修改。"""
    @wraps(handler)
    async def locked(self, *args, **kwargs):
        """持有插件级锁直到安装、配置或进程操作结束。"""
        if self.sts2_lock.locked():
            return {"status": "error", "message": "STS2 操作正在进行，请稍后再试"}, 409
        async with self.sts2_lock:
            return await handler(self, *args, **kwargs)
    return locked


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
        self.sts2_lock = asyncio.Lock()

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
            ("sts2", self.sts2, ["GET"]),
            ("sts2/settings", self.update_sts2_settings, ["POST"]),
            ("sts2/enabled", self.update_sts2_enabled, ["POST"]),
            ("sts2/setup", self.setup_sts2, ["POST"]),
            ("sts2/install", self.install_sts2, ["POST"]),
            ("sts2/uninstall", self.uninstall_sts2, ["POST"]),
            ("sts2/start", self.start_sts2, ["POST"]),
            ("sts2/join", self.join_sts2, ["POST"]),
            ("sts2/stop", self.stop_sts2, ["POST"]),
            ("sts2/lan/install", self.install_sts2_lan, ["POST"]),
            ("sts2/lan/uninstall", self.uninstall_sts2_lan, ["POST"]),
            ("sts2/relay/install", self.install_sts2_relay, ["POST"]),
            ("sts2/relay/prepare", self.prepare_sts2_relay, ["POST"]),
            ("sts2/relay/download", self.download_sts2_relay, ["POST"]),
            ("sts2/relay/start", self.start_sts2_relay, ["POST"]),
            ("sts2/relay/stop", self.stop_sts2_relay, ["POST"]),
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

    async def sts2(self):
        """返回 STS2 安装、游戏进程和本机 API 状态。"""
        try:
            data = await asyncio.to_thread(self.plugin.sts2.status)
            return {"status": "ok", "data": data}
        except Exception:
            return {"status": "error", "message": "STS2 状态读取失败"}, 503

    @_serialize_sts2
    async def update_sts2_settings(self):
        """保存游戏及公网入口；外部端口不改变本机监听，不操作系统网络。"""
        body = await _body()
        game_dir, port = body.get("game_dir"), body.get("api_port")
        if (
            not {"game_dir", "api_port"} <= set(body)
            or not set(body) <= {"game_dir", "api_port", "transport", "share_address",
                                 "connection", "public_port", "relay_host", "relay_control_port"}
            or not isinstance(game_dir, str)
            or len(game_dir) > 512
            or type(port) is not int
            or not 1024 <= port <= 65535
        ):
            return {"status": "error", "message": "游戏目录或端口格式无效"}, 400
        game_dir = game_dir.strip()
        if game_dir and (not ntpath.isabs(game_dir) or not ntpath.splitdrive(game_dir)[0]):
            return {"status": "error", "message": "请输入 Windows 绝对路径"}, 400
        transport = body.get("transport", self.plugin.sts2.transport())
        if transport not in ("local", "lan"):
            return {"status": "error", "message": "联机模式无效"}, 400
        try:
            connection = body.get("connection", self.plugin.config.get("sts2_connection", "lan"))
            if connection not in ("lan", "direct", "relay"):
                raise ValueError("好友入口类型无效")
            validator = validate_public_address if connection == "direct" else validate_share_address
            address = validator(body.get("share_address", self.plugin.config.get("sts2_share_address", "")))
            public_port = validate_game_port(body.get("public_port", self.plugin.config.get("sts2_public_port", 33771)))
            relay_config = dict(self.plugin.config)
            for key in ("relay_host", "relay_control_port"):
                relay_config["sts2_" + key] = body.get(key, self.plugin.config.get("sts2_" + key, "" if key == "relay_host" else 7000))
            relay_config["sts2_public_port"] = public_port
            relay_config["sts2_api_port"] = port
            relay = relay_settings(relay_config)
            if public_port == port or relay["control_port"] == port:
                raise ValueError("公网游戏/中转控制端口不能与当前 MCP API 端口相同")
        except ValueError as exc:
            return {"status": "error", "message": str(exc)}, 400
        previous = {key: self.plugin.config.get(key, default) for key, default in (
            ("sts2_game_dir", ""), ("sts2_api_port", 15526),
            ("sts2_transport", "local"), ("sts2_share_address", ""),
            ("sts2_connection", "lan"), ("sts2_public_port", 33771),
            ("sts2_relay_host", ""), ("sts2_relay_control_port", 7000),
        )}
        relay_changed = (
            relay["host"] != previous["sts2_relay_host"]
            or relay["control_port"] != previous["sts2_relay_control_port"]
            or public_port != previous["sts2_public_port"]
            or connection != previous["sts2_connection"] or transport != previous["sts2_transport"]
        )
        if relay_changed:
            try:
                await asyncio.to_thread(self.plugin.sts2.relay._assert_stopped)
            except Exception:
                return {"status": "error", "message": "请先停止中转客户端，再修改中转或联机入口设置"}, 409
        game_settings_changed = (
            game_dir != previous["sts2_game_dir"] or port != previous["sts2_api_port"]
            or transport != previous["sts2_transport"]
        )
        if game_settings_changed and self.plugin.sts2._alive_managed_processes():
            return {"status": "error", "message": "请先停止 Whale 启动的游戏实例，再修改设置"}, 409
        current_game = self.plugin.sts2.game_dir()
        if game_settings_changed and current_game is not None:
            try:
                await asyncio.to_thread(self.plugin.sts2._assert_game_stopped, current_game)
            except Exception:
                return {"status": "error", "message": "请先关闭当前目录的游戏窗口，再修改设置"}, 409
        self.plugin.config["sts2_game_dir"] = game_dir
        self.plugin.config["sts2_api_port"] = port
        self.plugin.config["sts2_transport"] = transport
        self.plugin.config["sts2_share_address"] = address
        self.plugin.config["sts2_connection"] = connection
        self.plugin.config["sts2_public_port"] = public_port
        self.plugin.config["sts2_relay_host"] = relay["host"]
        self.plugin.config["sts2_relay_control_port"] = relay["control_port"]
        try:
            self.plugin.config.save_config()
        except Exception:
            for key, value in previous.items():
                self.plugin.config[key] = value
            return {"status": "error", "message": "STS2 设置保存失败"}, 503
        return await self.sts2()

    @_serialize_sts2
    async def update_sts2_enabled(self):
        """保存主人私聊中是否开放 STS2 AI 工具。"""
        body = await _body()
        if set(body) != {"enabled"} or type(body["enabled"]) is not bool:
            return {"status": "error", "message": "STS2 开关格式无效"}, 400
        previous = self.plugin.config.get("sts2_enabled", False)
        self.plugin.config["sts2_enabled"] = body["enabled"]
        try:
            self.plugin.config.save_config()
        except Exception:
            self.plugin.config["sts2_enabled"] = previous
            return {"status": "error", "message": "STS2 开关保存失败"}, 503
        return await self.sts2()

    @_serialize_sts2
    async def install_sts2(self):
        """下载校验并安装官方 STS2MCP 发布文件。"""
        if await _body():
            return {"status": "error", "message": "安装请求不接受参数"}, 400
        status = await asyncio.to_thread(self.plugin.sts2.status)
        if status.get("processes"):
            return {"status": "error", "message": "请先停止 Whale 启动的游戏实例"}, 409
        try:
            data = await asyncio.to_thread(self.plugin.sts2.install)
            return {"status": "ok", "data": data}
        except Exception as exc:
            return {"status": "error", "message": str(exc)[:240]}, 409

    @_serialize_sts2
    async def setup_sts2(self):
        """自动定位 Steam 游戏，按已保存模式安装 Mod 并启用 AI 游戏工具。"""
        if await _body():
            return {"status": "error", "message": "一键配置请求不接受参数"}, 400
        before = {
            "sts2_game_dir": self.plugin.config.get("sts2_game_dir", ""),
            "sts2_api_port": self.plugin.config.get("sts2_api_port", 15526),
            "sts2_enabled": self.plugin.config.get("sts2_enabled", False),
        }
        status = await asyncio.to_thread(self.plugin.sts2.status)
        if status.get("processes"):
            return {"status": "error", "message": "请先停止 Whale 启动的游戏实例"}, 409
        game_dir = await asyncio.to_thread(self.plugin.sts2.game_dir)
        if game_dir is None:
            game_dir = await asyncio.to_thread(self.plugin.sts2.discover_game_dir)
        if game_dir is None:
            return {
                "status": "error",
                "message": "未能从 Steam 库自动定位游戏，请先在下方填写游戏目录并保存。",
            }, 404
        self.plugin.config["sts2_game_dir"] = str(game_dir)
        self.plugin.config["sts2_api_port"] = self.plugin.sts2.port()
        try:
            await asyncio.to_thread(self.plugin.sts2.install)
            if self.plugin.sts2.transport() == "lan":
                await asyncio.to_thread(self.plugin.sts2.install_lan)
            self.plugin.config["sts2_enabled"] = True
            self.plugin.config.save_config()
            data = await asyncio.to_thread(self.plugin.sts2.status)
            return {"status": "ok", "data": data}
        except Exception as exc:
            for key, value in before.items():
                self.plugin.config[key] = value
            try:
                self.plugin.config.save_config()
            except Exception:
                pass
            return {"status": "error", "message": str(exc)[:240]}, 409

    @_serialize_sts2
    async def uninstall_sts2(self):
        """只移除 Whale Companion 安装记录拥有的 STS2MCP 文件。"""
        if await _body():
            return {"status": "error", "message": "卸载请求不接受参数"}, 400
        status = await asyncio.to_thread(self.plugin.sts2.status)
        if status.get("processes"):
            return {"status": "error", "message": "请先停止 Whale 启动的游戏实例"}, 409
        try:
            data = await asyncio.to_thread(self.plugin.sts2.uninstall)
            return {"status": "ok", "data": data}
        except Exception as exc:
            return {"status": "error", "message": str(exc)[:240]}, 409

    @_serialize_sts2
    async def start_sts2(self):
        """按配置的 FastMP 或 LAN 模式启动 AI 房主窗口。"""
        if await _body():
            return {"status": "error", "message": "启动请求不接受参数"}, 400
        try:
            data = await asyncio.to_thread(self.plugin.sts2.start_coop)
            return {"status": "ok", "data": data}
        except Exception as exc:
            return {"status": "error", "message": str(exc)[:240]}, 409

    @_serialize_sts2
    async def stop_sts2(self):
        """停止本插件启动并记录的游戏实例，不处理用户手动启动的进程。"""
        if await _body():
            return {"status": "error", "message": "停止请求不接受参数"}, 400
        try:
            data = await asyncio.to_thread(self.plugin.sts2.stop_coop)
            return {"status": "ok", "data": data}
        except Exception as exc:
            return {"status": "error", "message": str(exc)[:240]}, 409

    @_serialize_sts2
    async def join_sts2(self):
        """按当前模式启动本机加入窗口，LAN 需在游戏内选择 IP 加入。"""
        if await _body():
            return {"status": "error", "message": "加入请求不接受参数"}, 400
        try:
            data = await asyncio.to_thread(self.plugin.sts2.join_local)
            return {"status": "ok", "data": data}
        except Exception as exc:
            return {"status": "error", "message": str(exc)[:240]}, 409

    @_serialize_sts2
    async def install_sts2_lan(self):
        """安装校验后的固定 LanSpire 发布包并配置四人容量。"""
        if await _body():
            return {"status": "error", "message": "LAN 安装请求不接受参数"}, 400
        try:
            return {"status": "ok", "data": await asyncio.to_thread(self.plugin.sts2.install_lan)}
        except Exception as exc:
            return {"status": "error", "message": str(exc)[:240]}, 409

    async def _sts2_relay_operation(self, operation: str):
        """只执行路由内固定的中转操作，不接受客户端提供路径、命令或凭据。"""
        if await _body():
            return {"status": "error", "message": "中转操作不接受参数，请先保存页面设置"}, 400
        try:
            result = await asyncio.to_thread(getattr(self.plugin.sts2.relay, operation))
            return {"status": "ok", "data": result}
        except Exception as exc:
            # 避免异常回显证书、令牌、配置原文或底层进程参数。
            known = str(exc)
            message = known if known.startswith("relay_") and len(known) < 100 else "中转操作失败，请检查依赖、目录权限与已保存设置"
            return {"status": "error", "message": message}, 409

    @_serialize_sts2
    async def install_sts2_relay(self):
        """显式安装固定摘要的 Windows x64 frpc，不自动启动。"""
        return await self._sts2_relay_operation("install")

    @_serialize_sts2
    async def prepare_sts2_relay(self):
        """生成全新 token/证书与配套服务器包，不自动连接远端。"""
        return await self._sts2_relay_operation("prepare")

    @_serialize_sts2
    async def download_sts2_relay(self):
        """重新下载当前私有包，不改变已有凭据。"""
        return await self._sts2_relay_operation("download")

    @_serialize_sts2
    async def start_sts2_relay(self):
        """仅在显式操作后启动受管中转进程。"""
        return await self._sts2_relay_operation("start")

    @_serialize_sts2
    async def stop_sts2_relay(self):
        """仅停止创建时间、路径与参数仍匹配的受管 frpc。"""
        return await self._sts2_relay_operation("stop")

    @_serialize_sts2
    async def uninstall_sts2_lan(self):
        """卸载受管 LAN 文件；保留共享配置和原始备份，不删除档案。"""
        if await _body():
            return {"status": "error", "message": "LAN 卸载请求不接受参数"}, 400
        try:
            return {"status": "ok", "data": await asyncio.to_thread(self.plugin.sts2.uninstall_lan)}
        except Exception as exc:
            return {"status": "error", "message": str(exc)[:240]}, 409
