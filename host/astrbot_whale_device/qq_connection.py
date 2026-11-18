"""Persistent OneBot v11 forward-WebSocket connection for the desktop host.

Only the reader receives frames. Model processing and correlated API calls never
block event reception. Credentials remain in memory and never enter snapshots.
"""
from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from concurrent.futures import Future
from typing import Any, Callable
from urllib.parse import urlsplit

from .onebot import OneBotEventError, OneBotSender, parse_event
from .onebot_lifecycle import OneBotLifecycle, OutboundAction


class QQConnectionError(RuntimeError):
    """Sanitized connection or protocol error suitable for the UI."""


def validate_ws_url(value: str) -> str:
    value = str(value).strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("QQ 接入地址格式不正确") from None
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise ValueError("QQ 接入地址必须以 ws:// 或 wss:// 开头")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("接入地址中不要填写账号、令牌或查询参数，请使用独立的 Token 输入框")
    if port is not None and not 0 < port < 65536:
        raise ValueError("QQ 接入端口不正确")
    return value


class QQConnection:
    """Connect a prepared StandaloneHost to NapCat's OneBot WebSocket server.

    ``on_change`` is invoked on background threads; GUI callers must marshal to
    their main thread. ``stop`` closes the transport and prevents in-flight
    model results from sending replies. No historical queue executes on resume.
    """

    def __init__(
        self, host: Any, ws_url: str, access_token: str = "", *,
        auto_reply: bool = True,
        on_change: Callable[[dict[str, Any]], None] | None = None,
        request_timeout: float = 8,
        reconnect_delays: tuple[float, ...] = (1, 2, 4, 8, 15, 30),
        status_interval: float = 15,
    ) -> None:
        self.host = host
        self.ws_url = validate_ws_url(ws_url)
        self._token = str(access_token).strip()
        if any(c in self._token for c in "\r\n"):
            raise ValueError("OneBot Token 不能包含换行")
        self._callback = on_change
        self._request_timeout = max(0.05, float(request_timeout))
        self._delays = reconnect_delays or (1,)
        self._interval = max(0.05, float(status_interval))
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._socket: Any = None
        self._pending: dict[str, Future] = {}
        self._events: queue.Queue = queue.Queue(maxsize=256)
        self._manager: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._route = threading.local()
        self._auto_generation = 0
        self._connection_generation = 0
        self._state = dict(state="stopped", account_id="", nickname="", auto_reply=bool(auto_reply),
                           received=0, replied=0, last_error="", last_activity_ms=0)
        self.sender = OneBotSender(self._send_message)
        # OneBot does not promise send idempotency. Do not blindly retry after
        # an acknowledgement is lost; the operation may already have succeeded.
        self.lifecycle = OneBotLifecycle(host.plugin.store, host.plugin.config.device_id,
                                        self.sender, max_attempts=1)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def _update(self, **values: Any) -> None:
        with self._lock:
            if values.get("state") == "offline" and self._state["state"] != "offline":
                self._connection_generation += 1
            self._state.update(values)
            snapshot = dict(self._state)
        if self._callback:
            try:
                self._callback(snapshot)
            except Exception:
                pass

    def start(self) -> None:
        with self._lock:
            if self._manager and self._manager.is_alive():
                return
            if self._worker and self._worker.is_alive():
                raise QQConnectionError("上一条消息仍在结束处理中，请稍后重连")
            self._stop.clear()
            self._events = queue.Queue(maxsize=256)
            self.host.plugin.deliver_replies = False
            self.host.plugin.device_task_guard = self._allow_event_action
            self._worker = threading.Thread(target=self._process_events, name="whale-qq-events", daemon=True)
            self._manager = threading.Thread(target=self._run, name="whale-qq-connection", daemon=True)
            self._worker.start()
            self._manager.start()

    def _allow_event_action(self) -> bool:
        with self._lock:
            return (not self._stop.is_set() and self._state["auto_reply"]
                and self._state["state"] == "connected"
                and getattr(self._route, "generation", None) == self._auto_generation
                and getattr(self._route, "connection_generation", None) == self._connection_generation)

    def stop(self) -> None:
        self._stop.set()
        self._disconnect()
        current = threading.current_thread()
        for thread in (self._manager, self._worker):
            if thread and thread is not current:
                thread.join(timeout=2)
        self._update(state="stopped", account_id="", nickname="", last_error="")

    def set_auto_reply(self, enabled: bool) -> None:
        with self._lock:
            self._auto_generation += 1
        self._update(auto_reply=bool(enabled))

    def call_api(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        echo = uuid.uuid4().hex
        future: Future = Future()
        with self._lock:
            connection = self._socket
            if self._stop.is_set() or connection is None:
                raise ConnectionError("QQ 连接已断开")
            self._pending[echo] = future
        try:
            with self._send_lock:
                connection.send(json.dumps({"action": action.lstrip("/"), "params": params or {}, "echo": echo},
                                           ensure_ascii=False, separators=(",", ":")))
            result = future.result(timeout=self._request_timeout)
            if result.get("status") != "ok" or result.get("retcode", 0) != 0:
                raise QQConnectionError("OneBot 请求被拒绝，请检查 QQ 登录状态和接口权限")
            return result
        except QQConnectionError:
            raise
        except TimeoutError:
            raise TimeoutError("QQ 接口响应超时") from None
        except (ConnectionError, OSError):
            raise ConnectionError("QQ 连接中断") from None
        except Exception:
            raise QQConnectionError("QQ 接口通信失败") from None
        finally:
            with self._lock:
                self._pending.pop(echo, None)

    def _disconnect(self) -> None:
        with self._lock:
            connection, self._socket = self._socket, None
            self._connection_generation += 1
            pending = list(self._pending.values())
            self._pending.clear()
        for future in pending:
            if not future.done():
                future.set_exception(ConnectionError("QQ 连接中断"))
        if connection:
            try:
                connection.close()
            except Exception:
                pass

    def _reader(self, connection: Any, finished: threading.Event) -> None:
        try:
            for raw in connection:
                if self._stop.is_set():
                    break
                try:
                    value = json.loads(raw)
                except (TypeError, ValueError, UnicodeError):
                    self._update(last_error="收到无法识别的 QQ 接口数据")
                    continue
                if not isinstance(value, dict):
                    continue
                echo = value.get("echo")
                if isinstance(echo, str):
                    with self._lock:
                        pending = self._pending.get(echo)
                    if pending and not pending.done():
                        pending.set_result(value)
                    continue
                if value.get("post_type") == "meta_event":
                    status = value.get("status")
                    if isinstance(status, dict) and status.get("online") is False:
                        self._update(state="offline", last_error="QQ 已离线，请在 NapCat 中重新登录")
                    continue
                if value.get("post_type") != "message":
                    continue
                with self._lock:
                    enabled = self._state["auto_reply"] and self._state["state"] == "connected"
                    generation = self._auto_generation
                    connection_generation = self._connection_generation
                if enabled:
                    try:
                        self._events.put_nowait((value, generation, connection_generation))
                    except queue.Full:
                        self._update(last_error="消息处理队列已满，本条未执行，请稍后重发")
        except Exception:
            pass
        finally:
            finished.set()
            self._disconnect()

    @staticmethod
    def _connection_error(exc: Exception) -> str:
        # Never display exceptions: websocket errors may contain headers or URL.
        response = getattr(exc, "response", None)
        if getattr(response, "status_code", None) in (401, 403):
            return "OneBot Token 不匹配或连接被拒绝，请核对接入配置"
        if isinstance(exc, QQConnectionError):
            return "QQ 接口未就绪，请确认 NapCat 已登录并启用 OneBot WebSocket 服务"
        if isinstance(exc, TimeoutError):
            return "QQ 接入超时，正在重试"
        return "QQ 连接中断，请确认 NapCat 正在运行；正在自动重连"

    def _run(self) -> None:
        from websockets.sync.client import connect
        failures = 0
        while not self._stop.is_set():
            self._update(state="connecting" if failures == 0 else "reconnecting", last_error="", account_id="", nickname="")
            finished = threading.Event()
            reader = None
            try:
                headers = {"Authorization": f"Bearer {self._token}"} if self._token else None
                connection = connect(self.ws_url, additional_headers=headers,
                                     open_timeout=self._request_timeout, close_timeout=1,
                                     ping_interval=20, ping_timeout=20, max_size=2 * 1024 * 1024,
                                     proxy=None)
                with self._lock:
                    if self._stop.is_set():
                        connection.close()
                        break
                    self._socket = connection
                reader = threading.Thread(target=self._reader, args=(connection, finished),
                                          name="whale-qq-reader", daemon=True)
                reader.start()
                login = self.call_api("get_login_info").get("data")
                if not isinstance(login, dict) or not str(login.get("user_id", "")).isdigit():
                    raise QQConnectionError("登录信息不可用")
                self._update(account_id=str(login["user_id"]), nickname=str(login.get("nickname", ""))[:100])
                while not self._stop.is_set() and not finished.is_set():
                    status = self.call_api("get_status").get("data")
                    online = isinstance(status, dict) and status.get("online") is True
                    if online:
                        if self.snapshot()["state"] != "connected":
                            login = self.call_api("get_login_info").get("data")
                            if not isinstance(login, dict) or not str(login.get("user_id", "")).isdigit():
                                raise QQConnectionError("登录信息不可用")
                            self._update(account_id=str(login["user_id"]), nickname=str(login.get("nickname", ""))[:100])
                        self._update(state="connected", last_error="")
                        failures = 0
                    else:
                        self._update(state="offline", last_error="QQ 尚未登录或已离线，请在 NapCat 中登录")
                    if finished.wait(self._interval):
                        break
            except Exception as exc:
                if not self._stop.is_set():
                    self._update(state="reconnecting", last_error=self._connection_error(exc))
            finally:
                self._disconnect()
                if reader:
                    reader.join(timeout=2)
            if not self._stop.is_set():
                delay = self._delays[min(failures, len(self._delays) - 1)]
                failures += 1
                self._stop.wait(max(0.01, delay))
        self._update(state="stopped", account_id="", nickname="")

    def _send_message(self, *, user_id: str, message: list[dict[str, Any]],
                      idempotency_key: str | None = None) -> dict[str, Any]:
        with self._lock:
            if self._stop.is_set() or not self._state["auto_reply"] or self._state["state"] != "connected":
                raise ConnectionError("自动回复已暂停或 QQ 未连接")
            if (getattr(self._route, "generation", None) != self._auto_generation or
                    getattr(self._route, "connection_generation", None) != self._connection_generation):
                raise ConnectionError("本条回复已因暂停或断线而取消")
        group = getattr(self._route, "group_id", "")
        action = "send_group_msg" if group else "send_private_msg"
        params = {"group_id" if group else "user_id": group or user_id, "message": message}
        if idempotency_key:
            # Reserve before a send. An interrupted acknowledgement is ambiguous;
            # duplicate events must not send the same operation a second time.
            marker = f"onebot:send_attempt:{idempotency_key}"
            if not self.host.plugin.store.add_effect(marker, "attempted", int(time.time() * 1000)):
                raise QQConnectionError("该回复已有发送尝试，未重复发送")
        result = self.call_api(action, params)
        with self._lock:
            self._state["replied"] += 1
            self._state["last_activity_ms"] = int(time.time() * 1000)
        self._update()
        return result

    def _process_events(self) -> None:
        while not self._stop.is_set():
            try:
                event, generation, connection_generation = self._events.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                with self._lock:
                    enabled = (self._state["auto_reply"] and generation == self._auto_generation
                               and connection_generation == self._connection_generation
                               and self._state["state"] == "connected" and not self._stop.is_set())
                    account = self._state["account_id"]
                if not enabled:
                    continue
                message = parse_event(event)
                if message.is_bot or message.user_id == account:
                    continue
                if event.get("message_type") == "group" and not message.group_id:
                    continue
                self._route.connection_generation = connection_generation
                self._route.group_id = message.group_id or ""
                self._route.generation = generation
                with self._lock:
                    self._state["received"] += 1
                    self._state["last_activity_ms"] = int(time.time() * 1000)
                self._update()
                result = self.host.plugin.on_onebot_event(event)
                if self._stop.is_set():
                    continue
                text = self.host.reply_text(result)
                media = result.get("media", []) if result.get("kind") == "reply" else []
                if not text and not media:
                    continue
                action = OutboundAction(idempotency_key=f"reply:{message.message_id}",
                                        user_id=message.user_id, text=text, media=media)
                outcome = self.lifecycle.dispatch(action)
                if outcome.get("status") not in {"sent", "duplicate"}:
                    self._update(last_error="QQ 回复未确认发送成功，请检查运行日志与 QQ 连接")
            except OneBotEventError:
                continue
            except Exception:
                self._update(last_error="消息处理失败，请检查模型配置与连接状态")
            finally:
                self._events.task_done()
