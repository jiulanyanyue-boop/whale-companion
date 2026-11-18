"""独立宿主：不装 AstrBot 也能把整条链路跑起来。

## 它是什么

`host/astrbot_whale_device/` 是框架无关核心；AstrBot 与「独立宿主」是它的两个外层。
本模块就是那个独立宿主：**一个可执行的入口**，把这些零件真的装配起来：

```text
OneBot HTTP (NapCat)  ──拉取/推送消息──>  独立宿主  ──WSS──>  手机
                              │
                              ├─ 认主（PrincipalGate，精确 user_id）
                              ├─ 模型决策（ModelGateway → OpenAI 兼容 HTTP）
                              ├─ 严格解析（parse_model_decision + validate_device_plan）
                              └─ 设备执行（WebSocketDeviceTransport → DeviceServer）
```

## 与 AstrBot 版的关系

- **共用同一份核心**：`plugin.py` 的 `WhaleDevicePlugin` / `build_plugin_with_gateway`；
- **只换外壳**：AstrBot 版用 `star.Star` + `@filter` 装饰器（见 `astrbot_star.py`），
  本模块用一个 `while` 循环拉 OneBot 事件；
- **两者都可运行**，核心逻辑零重复。

## 为什么需要它

本机（以及任何没装 AstrBot 的机器）没法验证 AstrBot 插件是否真的能加载。
独立宿主让「主机 → 模型 → 设备」这条链**在没有框架的机器上也能端到端跑通**，
从而把「能不能用」与「有没有装框架」解耦。

## 拉取模式 vs 推送模式

OneBot v11 有两种收消息方式：
1. **HTTP POST 上报**（NapCat 主动推给宿主）——需要宿主开 HTTP 服务；
2. **`get_latest_events` 轮询**（宿主主动拉）——不需要开服务，适合本机试验。

本模块用 **轮询**（第 2 种）：它不需要额外端口、不需要配置 NapCat 的上报地址，
一条命令就能跑，最适合「先试用」。生产可以用第 1 种替换，核心逻辑不变。

## 安全边界（与 AstrBot 版完全一致）

- 主人只按**精确 `user_id`** 识别，昵称一律不算（M04）；
- 群聊永不触发设备任务（M03）；
- 模型 API Key 与 OneBot token 只从环境变量读，**绝不进日志**；
- 设备帧签名密钥缺失时**启动失败**，不静默降级。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from typing import Any, Callable

from .host_config import HostConfig
from .models import IncomingMessage, MessageType
from .onebot import OneBotSender
from .onebot_client import OneBotClient
from .plugin import PluginConfig, build_plugin_with_gateway
from .model_client import make_model_transport
from .astrbot_adapter import ModelGateway, make_model_decider
from .device_server import WhaleDeviceServer, WebSocketDeviceTransport

_desktop_counter_lock = threading.Lock()
_desktop_counter = 0


def next_desktop_counter() -> int:
    global _desktop_counter
    with _desktop_counter_lock:
        _desktop_counter = max(time.time_ns(), _desktop_counter + 1)
        return _desktop_counter

# ---------------------------------------------------------------------------
# 事件 → IncomingMessage（与 astrbot_star 同一套语义，但吃 OneBot 原始字典）
# ---------------------------------------------------------------------------


def incoming_from_onebot_event(event: dict[str, Any], *, self_id: str = "") -> IncomingMessage | None:
    """把 `get_latest_events` 拿到的事件转成核心包的 `IncomingMessage`。

    返回 `None` 表示「这条不该处理」（非 message 事件、缺关键字段）。
    与 `onebot.parse_event` 的区别：那是**严格解析器**（非法就抛错，用于生产链路）；
    这里是**宽松预筛**（用于轮询循环，遇到脏数据跳过而不是让整轮崩掉）。

    注意：宽松只体现在「跳过脏数据」，**身份判定依然严格**——
    `user_id` 缺失就返回 `None`，绝不用昵称兜底。
    """
    if not isinstance(event, dict):
        return None
    if str(event.get("post_type", "")) != "message":
        return None

    raw_type = str(event.get("message_type", ""))
    if raw_type not in ("private", "group"):
        return None

    user_id = event.get("user_id")
    if user_id is None or str(user_id).strip() == "":
        # 缺 user_id 一律丢弃：昵称不能顶替 user_id（M04/M03 硬要求）。
        return None

    message_id = event.get("message_id")
    if message_id is None or str(message_id).strip() == "":
        message_id = f"onebot:{user_id}:{event.get('time', 0)}"

    text = _extract_text(event.get("message"))
    if text is None:
        return None

    is_group = raw_type == "group"
    group_id = event.get("group_id")
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    nickname = str(sender.get("nickname", "")) if isinstance(sender, dict) else ""

    return IncomingMessage(
        message_id=str(message_id),
        user_id=str(user_id),
        text=text,
        message_type=MessageType.GROUP if is_group else MessageType.PRIVATE,
        channel="onebot",
        group_id=str(group_id) if is_group and group_id is not None else None,
        sender_name=nickname,
        is_bot=bool(self_id) and str(self_id) == str(user_id),
    )


def _extract_text(message: Any) -> str | None:
    """从 OneBot `message` 字段抽纯文本。

    字符串直接返回；segment 数组只拼 `text` 段，其余段（image/record/face）
    用占位符表示——**不把媒体 URL 当正文**（与 M03「不降级为文本 URL」同一条原则）。
    """
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        parts: list[str] = []
        for segment in message:
            if not isinstance(segment, dict):
                return None
            kind = segment.get("type")
            data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            if kind == "text":
                parts.append(str(data.get("text", "")))
            elif kind in {"image", "record", "video", "face"}:
                parts.append(f"[{kind}]")
            else:
                # 未知段：不猜，直接判定这条消息无法处理。
                return None
        return "".join(parts)
    return None


# ---------------------------------------------------------------------------
# 宿主装配
# ---------------------------------------------------------------------------


class StandaloneHost:
    """把核心包 + 三个新客户端装成一台能跑的机器。

    @param config [HostConfig]（密钥全部从环境变量读）
    """

    def __init__(self, config: HostConfig, *, environ: dict[str, str] | None = None, store=None) -> None:
        self.config = config
        self._environ = dict(os.environ if environ is None else environ)

        # 1) 设备服务端：手机主动连过来。
        server_cfg = config.device_server
        ssl_context = self._build_ssl_context(server_cfg)
        self.device_server = WhaleDeviceServer(
            host=server_cfg.host,
            port=server_cfg.port,
            ssl_context=ssl_context,
        )
        self.transport = WebSocketDeviceTransport(self.device_server)

        # 2) 模型网关：密钥只活在 transport 里，payload 里没有它。
        api_key = config.model_api_key(self._environ)
        model_transport = make_model_transport(
            api_key=api_key,
            base_url=config.model.base_url or None,
        )
        self.gateway = ModelGateway(config.model, transport=model_transport)

        # 3) OneBot 发送端：真实 HTTP API。
        onebot_cfg = config.onebot
        self.onebot_client: OneBotClient | None = None
        self.onebot_sender: OneBotSender | None = None
        if onebot_cfg is not None:
            self.onebot_client = OneBotClient(
                base_url=onebot_cfg.http_url,
                access_token=onebot_cfg.access_token(self._environ) or None,
            )
            self.onebot_sender = OneBotSender(self.onebot_client.send_private_message)

        # 4) 核心插件：认主、严格解析、设备执行全在这一层。
        plugin_config = PluginConfig(
            owner_user_id=config.owner_user_id,
            device_id=config.device_id,
            allowed_packages=set(config.allowed_packages),
            blocked_packages=set(config.blocked_packages),
            signing_secret=config.signing_secret(self._environ),
        )
        self.plugin = build_plugin_with_gateway(
            plugin_config,
            self.gateway,
            self.transport,
            store=store,
            sender=self.onebot_sender,
        )

    def _build_ssl_context(self, server_cfg: Any) -> Any:
        """构建 WSS 的 TLS 上下文。

        给了证书就用 `PROTOCOL_TLS_SERVER` 加载；没给则返回 `None`（明文回环，
        仅限本机试验）。生产必须给证书——M05 禁止明文 ws。
        """
        import ssl

        if not server_cfg.uses_tls:
            return None
        missing = server_cfg.missing_tls_files()
        if missing:
            raise FileNotFoundError(f"TLS files declared but missing: {missing}")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(server_cfg.certfile, server_cfg.keyfile)
        return context

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> int:
        """启动设备服务端；返回实际监听端口。"""
        port = self.device_server.start()
        from .public_tunnel import PublicTunnel
        tunnel_path = os.environ.get("WHALE_PUBLIC_TUNNEL_CONFIG", "")
        if tunnel_path and not getattr(self, "_public_tunnel", None):
            self._public_tunnel = PublicTunnel(tunnel_path)
            self._public_tunnel.start()
        return port

    def stop(self) -> None:
        if getattr(self, "_public_tunnel", None):
            self._public_tunnel.stop()
            self._public_tunnel = None
        self.device_server.stop()

    @property
    def device_url(self) -> str:
        return self.device_server.url

    def request_device(self, command: str, payload: dict[str, Any], *, device_id: str):
        """Sign desktop requests with the same certificate authenticated by WSS."""
        import base64
        import pathlib
        import uuid
        from dataclasses import replace
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from .models import DeviceEnvelope
        from .pairing import canonical_bytes

        request_id = str(uuid.uuid4())
        stamp = time.time_ns() // 1_000_000
        envelope = DeviceEnvelope(
            protocol_version=1, request_id=request_id, device_id=device_id,
            command=command, payload=payload, deadline_ms=8000,
            idempotency_key=f"desktop:{request_id}", counter=next_desktop_counter(), issued_at_ms=stamp,
        )
        key = serialization.load_pem_private_key(
            pathlib.Path(self.config.device_server.keyfile).read_bytes(), password=None,
        )
        signature = key.sign(canonical_bytes(envelope.as_dict()), ec.ECDSA(hashes.SHA256()))
        envelope = replace(envelope, signature=base64.b64encode(signature).decode("ascii"))
        return self.transport.request(envelope)

    # -- 处理一条消息 -----------------------------------------------------

    def handle_message(self, message: IncomingMessage) -> dict[str, Any]:
        """交给核心插件；异常转成 `failed` 而不外泄细节。"""
        try:
            return self.plugin.on_onebot_message(message)
        except Exception as exc:
            return {"kind": "failed", "reason": f"host_error:{exc.__class__.__name__}"}

    def reply_text(self, result: dict[str, Any]) -> str:
        """把结果转成要发回 QQ 的文本（与 AstrBot 版同一套固定映射）。"""
        from .astrbot_star import reply_text_for

        return reply_text_for(result)


# ---------------------------------------------------------------------------
# 给手机端看的地址（把「绑定地址」翻译成「可连接地址」）
# ---------------------------------------------------------------------------


def _listen_path(device_url: str) -> str:
    """从 `ws://host:port/path` 里取出 `/path` 部分。

    路径是协议约定的一部分（设备端固定连这个路径），所以拼给用户的地址
    也必须带上它——只给 `ws://ip:port` 会连到错的端点。
    """
    marker = "://"
    if marker not in device_url:
        return ""
    after = device_url.split(marker, 1)[1]
    if "/" not in after:
        return ""
    return "/" + after.split("/", 1)[1]


def phone_facing_urls(
    *,
    bind_host: str,
    port: int,
    path: str = "",
    scheme: str = "ws",
) -> list[str]:
    """算出手机端「主机连接地址」应填什么。

    为什么需要它：主机监听 `0.0.0.0`（所有网卡）时，打印出来的
    `ws://0.0.0.0:8765/device` **不是一个可连接的目标地址**，手机照着填必然失败；
    而 `127.0.0.1` 表示「本机自己」，手机填它也连不到主机。

    所以这里把绑定地址翻译成**可连接地址**：
    - 绑定到具体非回环 IP → 就用它；
    - 绑定到 `0.0.0.0` / `::` / 回环 → 枚举本机的局域网 IPv4，逐个给出候选；
    - 一个都拿不到 → 返回空列表，由调用方提示用户自己用 `ipconfig` 查。

    @param scheme **必须与实际监听协议一致**：服务端启用了 TLS 时这里是 `wss`，
    否则给用户 `ws://` 会让客户端连不上（协议不匹配）。默认 `ws` 仅为兼容调用方。
    """
    host = (bind_host or "").strip()
    actual_scheme = (scheme or "ws").strip().lower()
    if actual_scheme not in ("ws", "wss"):
        actual_scheme = "ws"
    tail = f":{port}{path}"

    # 绑定到具体地址（且不是通配/回环）→ 直接就是可连接地址。
    if host and host not in ("0.0.0.0", "::", "[::]", "127.0.0.1", "localhost"):
        return [f"{actual_scheme}://{host}{tail}"]

    urls: list[str] = []
    for ip in local_ipv4_addresses():
        urls.append(f"{actual_scheme}://{ip}{tail}")
    return urls


def local_ipv4_addresses() -> list[str]:
    """枚举本机可用的局域网 IPv4 地址（供手机连接）。

    实现方式：不依赖 psutil，用标准库 `socket` 做「连一个外部地址但 UDP 不真正发包」
    的技巧来获取默认路由网卡地址；再补充解析主机名。失败时返回空列表，
    绝不抛异常打断启动。
    """
    found: list[str] = []

    def _add(value: str) -> None:
        value = (value or "").strip()
        if not value or value.startswith("127."):
            return
        # 只保留「像局域网地址」的：私有段与链路本地段。
        if not (
            value.startswith("10.")
            or value.startswith("192.168.")
            or value.startswith("172.")
            or value.startswith("169.254.")
        ):
            return
        if value not in found:
            found.append(value)

    try:
        import socket

        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # 不需要真的连通：UDP connect 只用于让系统选出默认网卡。
            probe.connect(("8.8.8.8", 80))
            _add(probe.getsockname()[0])
        finally:
            probe.close()
    except Exception:
        pass

    try:
        import socket

        for info in socket.gethostbyname_ex(socket.gethostname())[2]:
            _add(info)
    except Exception:
        pass

    return found


# ---------------------------------------------------------------------------
# 轮询循环
# ---------------------------------------------------------------------------


def _extract_events(raw: Any) -> list[Any] | None:
    """从 `get_latest_events` 的响应里取出事件数组。

    三种形态都要接（见 [poll_once] 的说明）。取不到就返回 `None`，
    由调用方保持游标不变——**不能把「取不到」当成「没有新消息」以外的语义**。
    """
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, dict):
        return None
    # 形态 1：{"data": {"events": [...]}}
    data = raw.get("data")
    if isinstance(data, dict):
        nested = data.get("events")
        if isinstance(nested, list):
            return nested
        # 有些实现把 data 直接放成数组
        if isinstance(data, list):
            return data
    # 形态 2：{"events": [...]}
    flat = raw.get("events")
    if isinstance(flat, list):
        return flat
    return None


def poll_once(
    *,
    client: Any,
    host: StandaloneHost,
    last_seen: str | None,
) -> tuple[str | None, int]:
    """拉一轮 `get_latest_events` 并处理。

    返回 `(新游标, 处理条数)`。游标用最后一条的 `message_id`；
    拿不到就保持不变，避免重复消费。

    `OneBotClient.call_api` 返回的是**完整响应体**
    `{"status": "ok", "retcode": 0, "data": {...}}`，所以事件在 `data` 层里。
    但不同 OneBot 实现对 `get_latest_events` 的包装不一致，这里三种形态都接：
    1. `{"data": {"events": [...]}}`（NapCat 常见）
    2. `{"events": [...]}`（部分实现直接平铺）
    3. `[...]`（极端简化实现直接返回数组）
    """
    raw: Any = {}
    if hasattr(client, "call_api"):
        call = getattr(client, "call_api")
        # `OneBotClient._url` 要求端点以 `/` 开头（它是标准 OneBot 的 API 路径规则），
        # 传裸名 `get_latest_events` 会被判非法。这里按它的契约写全路径。
        raw = call("/get_latest_events", {"limit": 20, "timeout": 0})

    events = _extract_events(raw)
    if events is None:
        return last_seen, 0

    processed = 0
    cursor = last_seen
    for event in events:
        if not isinstance(event, dict):
            continue
        message_id = event.get("message_id")
        if message_id is not None and str(message_id) == str(last_seen):
            continue
        message = incoming_from_onebot_event(event)
        if message is None:
            continue
        result = host.handle_message(message)
        text = host.reply_text(result)
        if text and host.onebot_sender is not None:
            try:
                host.onebot_sender.send_private(message.user_id, text)
            except Exception:
                # 发送失败不影响游标推进：M03 的重试归 lifecycle，这里只保证不崩。
                pass
        processed += 1
        if message_id is not None:
            cursor = str(message_id)
    return cursor, processed


def main(argv: list[str] | None = None) -> int:
    """命令行入口：`python -m astrbot_whale_device.standalone --config host.json`"""
    parser = argparse.ArgumentParser(
        prog="whale-host",
        description="Whale Agent v2 standalone host (no AstrBot required)",
    )
    parser.add_argument("--config", required=True, help="host config JSON path")
    parser.add_argument("--once", action="store_true", help="poll one round then exit")
    parser.add_argument("--interval", type=float, default=2.0, help="poll interval seconds")
    args = parser.parse_args(argv)

    try:
        config = HostConfig.load(args.config)
    except Exception as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    print("=== Whale Agent standalone host ===")
    print(json.dumps(config.loggable(), ensure_ascii=False, indent=2))

    try:
        host = StandaloneHost(config)
    except Exception as exc:
        print(f"assembly error: {exc}", file=sys.stderr)
        return 3

    port = host.start()
    print(f"device server listening: {host.device_url} (port {port})")

    # ---- 给手机端看的「该填什么」----
    # 只打印监听地址是不够的：`0.0.0.0` 是绑定地址，手机照着填会连不上。
    # 这里把每个可用的局域网 IPv4 都算一遍，直接给出「手机端应填的完整地址」，
    # 用户在 App 里抄这一行就行，不必自己拼 IP 与端口。
    # scheme 必须跟着实际监听协议走：启用了 TLS 就是 wss，否则客户端协议不匹配。
    listen_scheme = "wss" if config.device_server.uses_tls else "ws"
    print()
    if config.device_server.uses_tls:
        print("手机端「主机连接地址」请从这里选一行（wss://，不要填 0.0.0.0 或 127.0.0.1）：")
    else:
        print("手机端「主机连接地址」请从这里选一行（ws://，不要填 0.0.0.0 或 127.0.0.1）：")
    phone_urls = phone_facing_urls(
        bind_host=config.device_server.host,
        port=port,
        path=_listen_path(host.device_url),
        scheme=listen_scheme,
    )
    if phone_urls:
        for url in phone_urls:
            print(f"    {url}")
    else:
        print("    （没有找到局域网 IPv4 地址）请确认主机已连上 WiFi/以太网，")
        print(f"    再用 `ipconfig` 查本机 IP，然后手动拼成 {listen_scheme}://<那个IP>:{port}<路径>")

    print()
    print("设备 ID 请照抄配置里的 device_id：")
    print(f"    {config.device_id}")

    if config.device_server.uses_tls:
        print()
        print("注意：当前用的是自签名证书。手机端需要手动信任该证书才能连上；")
        print("      生产环境请换成受信任 CA 签发的证书。")
    else:
        print()
        print("注意：当前是明文 ws://（无 TLS）。仅可对可信局域网试验，生产环境必须启用 TLS。")

    if host.onebot_client is None:
        print("WARNING: onebot is not configured; messages will not be received")
        host.stop()
        return 0

    last_seen: str | None = None
    try:
        while True:
            try:
                last_seen, count = poll_once(client=host.onebot_client, host=host, last_seen=last_seen)
            except Exception as exc:
                # OneBot 不可用（NapCat 没开 / 地址错 / token 错）不应该让**主机整体挂掉**：
                # 设备服务端是独立能力，手机照样能连上来。这里只报告问题并继续。
                #
                # 为什么不直接退出：主人第一次试用往往还没装好 NapCat，
                # 而「确认设备服务端已在监听」本身就是有价值的进度。
                print()
                print(f"OneBot 暂时不可用：{exc}")
                print("  设备服务端仍在运行，手机可以连接；QQ 消息暂时收不到。")
                print("  修好 NapCat 的 HTTP 地址/token 后重跑即可。")
                if args.once:
                    print("（--once 模式：本轮结束）")
                    break
                time.sleep(max(1.0, args.interval))
                continue

            if args.once:
                print(f"processed {count} message(s)")
                break
            time.sleep(max(0.1, args.interval))
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        host.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
