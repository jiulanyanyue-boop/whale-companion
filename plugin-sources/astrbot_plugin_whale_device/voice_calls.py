"""Run call inference in AstrBot while a Linux sidecar owns QQ audio."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp


class VoiceCallRuntime:
    """Consume authenticated owner call audio without granting new device rights."""

    def __init__(self, plugin):
        self.plugin = plugin
        self.task = None
        self.status = {"ready": False, "phase": "disabled", "outgoing": False}
        self.history = []
        self.call_id = ""
        self.seen = set()
        self.base_url = str(
            plugin.config.get("voice_call_gateway", "http://127.0.0.1:6112")
        ).rstrip("/")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port != 6112
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("call_gateway_must_use_fixed_loopback_endpoint")

    def start(self):
        if self.plugin.config.get("voice_calls_enabled", False):
            self.task = asyncio.create_task(self.run(), name="whale-qq-calls")

    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self.status = {"ready": False, "phase": "stopped", "outgoing": False}

    async def run(self):
        token_path = Path(self.plugin.config.get("voice_call_token_file", ""))
        try:
            token = token_path.read_text(encoding="utf-8").strip()
            if len(token) < 32 or not self.plugin.owner_user_id.isdigit():
                raise ValueError("invalid_call_credentials")
        except (OSError, ValueError):
            self.status = {
                "ready": False,
                "phase": "configuration_required",
                "outgoing": False,
            }
            return
        async with aiohttp.ClientSession(
            headers={"Authorization": "Bearer " + token},
            timeout=aiohttp.ClientTimeout(total=10),
            trust_env=False,
        ) as client:
            heartbeat = asyncio.create_task(self.heartbeat(client))
            try:
                await self.consume(client)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat

    async def heartbeat(self, client):
        while True:
            try:
                async with client.get(self.base_url + "/v1/heartbeat") as response:
                    response.raise_for_status()
                    await response.read()
            except (aiohttp.ClientError, TimeoutError):
                pass
            await asyncio.sleep(3)

    async def consume(self, client):
        while True:
            try:
                async with client.get(self.base_url + "/v1/audio/next") as response:
                    response.raise_for_status()
                    state = await response.json()
                self.status = {
                    key: state.get(key)
                    for key in ("ready", "phase", "error", "capabilities")
                }
                if state.get("phase") != "connected":
                    self.history.clear()
                    self.call_id = ""
                    self.seen.clear()
                item = state.get("utterance")
                if isinstance(item, dict) and state.get("phase") == "connected":
                    await self.handle_utterance(client, item)
                else:
                    await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.status = {
                    "ready": False,
                    "phase": "unavailable",
                    "error": type(exc).__name__,
                    "outgoing": False,
                }
                await asyncio.sleep(2)

    async def still_current(self, client, call_id):
        async with client.get(self.base_url + "/v1/status") as response:
            response.raise_for_status()
            state = await response.json()
        return (
            state.get("phase") == "connected"
            and state.get("call_id") == call_id
            and str(state.get("caller_id")) == self.plugin.owner_user_id
        )

    async def handle_utterance(self, client, item):
        """Recognize, answer, and speak only within the original call generation.

        Args:
            client: Authenticated loopback client.
            item: Bounded audio and caller identity from the trusted QQ bridge.
        """
        call_id, utterance_id = item.get("call_id"), item.get("utterance_id")
        if (
            str(item.get("caller_id")) != self.plugin.owner_user_id
            or not call_id
            or not utterance_id
        ):
            return
        key = (call_id, utterance_id)
        if key in self.seen:
            return
        if call_id != self.call_id:
            self.history.clear()
            self.seen.clear()
            self.call_id = call_id
        self.seen.add(key)
        if len(self.seen) > 512:
            raise RuntimeError("call_turn_limit")
        encoded = item.get("audio", "")
        if not isinstance(encoded, str) or len(encoded) > 1024 * 1024:
            return
        raw = base64.b64decode(encoded, validate=True)
        session = f"{self.plugin.platform_id}:FriendMessage:{self.plugin.owner_user_id}"
        with tempfile.TemporaryDirectory(prefix="whale-call-") as tmp:
            path = Path(tmp) / "incoming.wav"
            path.write_bytes(raw)
            text, backend = await self.plugin.voice.transcribe_path(
                path, self.plugin.context, session
            )
            if not text.strip() or not await self.still_current(client, call_id):
                return
            provider_id = await self.plugin.context.get_current_chat_provider_id(
                session
            )
            persona = (
                await self.plugin._persona_prompt(self.plugin.owner_persona_id)
                or "你是小鲸鱼。"
            )
            response = await asyncio.wait_for(
                self.plugin.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=text,
                    system_prompt=persona
                    + "\n现在正在和主人进行QQ语音通话。自然说一两句简短中文，最多120字。转写可能有错，没听清就简短确认。不要使用Markdown，不要编造已执行的设备操作；本轮没有设备工具。",
                    contexts=list(self.history[-12:]),
                ),
                30,
            )
            reply = str(getattr(response, "completion_text", "") or "").strip()[:180]
            if not reply or not await self.still_current(client, call_id):
                return
            tts = await self.plugin.context.get_using_tts_provider_async(session)
            if tts:
                output = Path(await asyncio.wait_for(tts.get_audio(reply), 45))
            else:
                output = Path(tmp) / "reply.wav"
                await asyncio.to_thread(
                    self.plugin.voice.synthesize_test_voice, reply, output
                )
            # Providers may produce compressed audio; use AstrBot's shared decoder.
            from astrbot.core.message.components import Record

            output = Path(await Record(file=str(output)).convert_to_file_path())
            if not await self.still_current(client, call_id):
                return
            if output.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("call_reply_too_large")
            audio = output.read_bytes()
            async with client.post(
                self.base_url + "/v1/audio/play",
                json={
                    "call_id": call_id,
                    "utterance_id": utterance_id,
                    "audio": base64.b64encode(audio).decode("ascii"),
                },
                timeout=aiohttp.ClientTimeout(total=55),
            ) as result:
                result.raise_for_status()
                receipt = await result.json()
            if receipt.get("played") is True:
                self.history.extend(
                    [
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": reply},
                    ]
                )
                self.history = self.history[-12:]
                self.status["last_turn"] = {
                    "recognized": True,
                    "backend": backend,
                    "played": True,
                }
