"""AstrBot 插件入口（真实绑定，非鸭子类型壳子）。

本模块是 `host/astrbot_whale_device/` 核心包在 **AstrBot 4.x** 上的薄绑定层。
核心逻辑（认主、严格决策解析、命令白名单、任务幂等、OneBot 消息段、设备帧签名）
一行都不在这里——它全部来自同目录的核心包。

## 为什么单独成模块

`plugin.py` 里的 `register_with_astrbot` 是「鸭子类型回调注册」（`host.register_message_handler`），
那是给测试与其它宿主用的抽象。真 AstrBot 的插件模型是**继承 `Star` 的类 +
模块级 `@filter.xxx` 装饰器**，形态完全不同。把它硬塞进 `plugin.py` 会让那个文件
同时承担两种互不兼容的宿主语义。

## 真实 API（本机实读 AstrBot 4.29.4 源码确认）

```python
from astrbot.api import star                      # star.Star / star.Context
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.platform.message_type import MessageType

class Main(star.Star):
    def __init__(self, context: star.Context, config: dict | None = None) -> None: ...

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private(self, event: AstrMessageEvent) -> None: ...
```

- `EventMessageType`：`PRIVATE_MESSAGE=2`、`GROUP_MESSAGE=1`、`OTHER_MESSAGE=4`
- `AstrMessageEvent`：`get_sender_id()`、`get_message_str()`、`get_group_id()`、`plain_result(text)`、`stop_event()`
- `register_star` 装饰器**已废弃**；继承 `star.Star` 即被自动识别

## 本模块的边界

- 只做「AstrBot 事件 → 核心包 `IncomingMessage`」与「核心包结果 → AstrBot 回复」；
- **不碰**模型、不碰设备传输、不碰密钥：那些由宿主在 `on_astrbot_loaded` 时注入；
- 未装配时（缺配置）**不静默吞消息**，而是明确回一句「未配置」，让人看得见。
"""

from __future__ import annotations

from typing import Any

from .host_config import HostConfig
from .models import IncomingMessage, MessageType

# ---------------------------------------------------------------------------
# AstrBot 依赖：延迟导入
#
# 本模块**不在 AstrBot 缺席的环境里 import 失败**。
# 核心包的 800+ 单测跑在不含 AstrBot 的环境里，如果这里顶层 import astrbot，
# 整个 `astrbot_whale_device` 包都会跟着崩——那会把「框架无关核心」这个前提毁掉。
# 因此：顶层只定义普通函数，AstrBot 的实际 import 放在 `build_main_class()` 里。
# ---------------------------------------------------------------------------

_ASTRBOT_IMPORT_HINT = (
    "AstrBot is required for the plugin entry; install it with `pip install astrbot` "
    "and register this package as an AstrBot plugin directory."
)


def astrbot_available() -> bool:
    """本进程能否 import AstrBot。用于启动前的明确检查，而不是等到注册时炸。"""
    try:
        import astrbot  # noqa: F401
    except Exception:
        return False
    return True


def _require_astrbot() -> Any:
    try:
        import astrbot.api.star as star_module  # noqa: F401
        from astrbot.api import star  # noqa: F401
        from astrbot.api.event import AstrMessageEvent, filter as event_filter  # noqa: F401
        from astrbot.core.platform.message_type import MessageType as AstrMessageType  # noqa: F401
    except Exception as exc:  # pragma: no cover - 只在缺少 AstrBot 时触发
        raise ImportError(_ASTRBOT_IMPORT_HINT) from exc
    return {
        "star": star,
        "event": AstrMessageEvent,
        "filter": event_filter,
        "MessageType": AstrMessageType,
    }


# ---------------------------------------------------------------------------
# 事件转换（纯函数，可在无 AstrBot 环境里单测）
# ---------------------------------------------------------------------------


def to_incoming_message(
    *,
    sender_id: str,
    text: str,
    message_id: str,
    group_id: str | None,
    self_id: str,
    sender_name: str = "",
) -> IncomingMessage:
    """把 AstrBot 事件字段转成核心包的 `IncomingMessage`。

    只搬运核心包认可的字段（`contracts/` 与 M03 的 8 个入站字段口径一致）：
    `user_id`、`message_id`、`text`、`message_type`、`group_id`、`self_id`。

    **昵称不参与身份判定**（M04 硬要求）：`sender_name` 只作为展示信息带入，
    身份只看 `user_id` 的精确匹配。
    """
    is_group = group_id is not None and group_id != ""
    return IncomingMessage(
        message_id=message_id,
        user_id=str(sender_id),
        text=text,
        message_type=MessageType.GROUP if is_group else MessageType.PRIVATE,
        channel="astrbot",
        group_id=str(group_id) if is_group else None,
        sender_name=sender_name,
        # `self_id == user_id` 表示机器人自发消息；这里用调用方传入的 self_id 判定。
        is_bot=bool(self_id) and str(self_id) == str(sender_id),
    )


def reply_text_for(result: dict[str, Any]) -> str:
    """把核心包的 `route()` 结果转成一段要给 QQ 发的纯文本。

    固定映射（不复用模型原始输出，避免把内部 reason 漏给客人）：

    - `reply` → 模型文本；
    - `silent` → 空串（不发送，M03「不回复不等于执行设备动作」）；
    - `device_task` → 固定摘要（**不回显 payload 细节**）；
    - `failed` → 固定失败文案（**不回显 `reason`**，那是内部诊断信息）。
    """
    kind = str(result.get("kind", ""))
    if kind == "reply":
        return str(result.get("text", ""))
    if kind == "silent":
        return ""
    if kind == "device_task":
        count = 0
        plan = result.get("plan")
        if isinstance(plan, dict):
            commands = plan.get("commands")
            if isinstance(commands, list):
                count = len(commands)
        return f"已下发给设备（{count} 步）。"
    if kind == "failed":
        return "任务未完成：请求未被接受。"
    return ""


# ---------------------------------------------------------------------------
# 插件类工厂
# ---------------------------------------------------------------------------


def build_main_class(
    *,
    host_config_loader: Any = None,
    plugin_factory: Any = None,
) -> type:
    """构造继承 `astrbot.api.star.Star` 的插件类。

    为什么用工厂而不是在模块顶层直接写 `class Main(star.Star)`：
    因为顶层继承要求模块导入时就存在 AstrBot，会让「框架无关核心 + 800 单测」
    在不含 AstrBot 的环境里整体不可用。工厂把这份依赖推迟到真正装配的那一刻，
    同时保持 AstrBot 认得的「继承 Star 的类」这一形态。

    @param host_config_loader 返回 [HostConfig] 的 callable；`None` 时读环境变量约定路径
    @param plugin_factory 接收 [HostConfig] 返回 `WhaleDevicePlugin` 的 callable
    """

    api = _require_astrbot()
    star = api["star"]
    event_type = api["event"]
    event_filter = api["filter"]
    AstrMessageType = api["MessageType"]

    private_filter = event_filter.EventMessageType.PRIVATE_MESSAGE
    group_filter = event_filter.EventMessageType.GROUP_MESSAGE

    class WhaleAstrBotPlugin(star.Star):  # type: ignore[misc, name-defined]
        """Whale Agent v2 的 AstrBot 插件（设备控制 + 主人认主 + 严格决策）。

        AstrBot 会自动识别继承 `star.Star` 的类，无需 `@register_star`（已废弃）。
        """

        def __init__(self, context: Any, config: dict | None = None) -> None:
            super().__init__(context, config)
            self._config: HostConfig | None = None
            self._plugin: Any = None
            self._ready_error: str = ""
            self._load_config(host_config_loader, plugin_factory)

        # -- 装配 ---------------------------------------------------------

        def _load_config(self, loader: Any, factory: Any) -> None:
            """装配核心插件；失败时**记录原因**，不静默吞消息。"""
            try:
                config = loader() if callable(loader) else None
            except Exception as exc:
                self._ready_error = f"config load failed: {exc.__class__.__name__}"
                return
            if config is None:
                self._ready_error = "host config is not provided"
                return
            if not isinstance(config, HostConfig):
                self._ready_error = "host config has unexpected type"
                return
            self._config = config
            if not callable(factory):
                self._ready_error = "plugin factory is not provided"
                return
            try:
                self._plugin = factory(config)
            except Exception as exc:
                self._ready_error = f"plugin init failed: {exc.__class__.__name__}"

        @property
        def is_ready(self) -> bool:
            return self._plugin is not None and not self._ready_error

        # -- 事件入口 -----------------------------------------------------

        async def _handle(self, event: Any, *, is_group: bool) -> None:
            """统一处理：转 `IncomingMessage` → 核心 route → 回复。

            两条硬性行为：
            1. **群聊永不触发设备任务**（M03 硬要求）——交给核心包的 `PrincipalGate`
               判定，这里只负责如实上报 `group_id`，不做任何「猜群主」的花样；
            2. 未装配时明确回复「未配置」，**不静默丢弃**（主人的确认/失败/安全警告不能吞）。
            """
            if self._ready_error:
                await self._reply(event, f"Whale Agent 未装配：{self._ready_error}")
                return
            if self._plugin is None or self._config is None:
                await self._reply(event, "Whale Agent 未装配。")
                return

            text = event.get_message_str()
            sender_id = event.get_sender_id()
            group_id = event.get_group_id() if is_group else None
            message_id = self._message_id(event)
            self_id = self._self_id(event)

            message = to_incoming_message(
                sender_id=sender_id,
                text=text,
                message_id=message_id,
                group_id=group_id,
                self_id=self_id,
            )
            try:
                result = self._plugin.on_onebot_message(message)
            except Exception as exc:
                # 内部异常不回显 class 名以外的细节，避免泄漏实现。
                await self._reply(event, f"处理失败：{exc.__class__.__name__}")
                return

            reply = reply_text_for(result)
            if reply:
                await self._reply(event, reply)
                event.stop_event()

        @event_filter.event_message_type(private_filter)
        async def on_private_message(self, event: Any) -> None:
            """私聊：唯一允许触发设备任务的入口（核心包再按 owner 精确匹配）。"""
            await self._handle(event, is_group=False)

        @event_filter.event_message_type(group_filter)
        async def on_group_message(self, event: Any) -> None:
            """群聊：只聊不控。核心包对群消息一律 GUEST，永远拿不到设备工具。"""
            await self._handle(event, is_group=True)

        # -- AstrBot 生命周期 ---------------------------------------------

        @event_filter.on_astrbot_loaded()
        async def on_loaded(self, *args: Any, **kwargs: Any) -> None:
            """AstrBot 加载完成：这里适合启动设备 WSS 服务端（后续接线点）。"""
            if self.is_ready:
                self.logger.info("Whale Agent 已装配完成。")

        # -- 辅助 ---------------------------------------------------------

        async def _reply(self, event: Any, text: str) -> None:
            """发一条纯文本回复。空文本不发（`silent` 路径不会走到这里）。"""
            if not text:
                return
            result = event.plain_result(text)
            await event.send(result)

        @staticmethod
        def _message_id(event: Any) -> str:
            """取消息 ID。

            AstrBot 的 `message_obj` 上通常带 `message_id`；取不到时退回一个
            由发送者+时间组成的稳定值，**绝不返回空串**——核心包用 message_id
            做幂等去重，空串会让所有消息互相覆盖。
            """
            obj = getattr(event, "message_obj", None)
            raw = getattr(obj, "message_id", None) if obj is not None else None
            if raw:
                return str(raw)
            sender = event.get_sender_id()
            stamp = getattr(obj, "timestamp", None) if obj is not None else None
            return f"astrbot:{sender}:{stamp or '0'}"

        @staticmethod
        def _self_id(event: Any) -> str:
            getter = getattr(event, "get_self_id", None)
            if callable(getter):
                try:
                    return str(getter())
                except Exception:
                    return ""
            return ""

        # -- 显式暴露 AstrBot 用到的枚举，便于测试核对 --------------------

        __astrbot_message_type__ = AstrMessageType
        __astrbot_event_type__ = event_type
        __astrbot_filter__ = event_filter

    WhaleAstrBotPlugin.__name__ = "Main"
    WhaleAstrBotPlugin.__qualname__ = "Main"
    return WhaleAstrBotPlugin