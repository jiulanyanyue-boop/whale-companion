"""AstrBot LLM tool for local memes and explicit on-demand Jimeng 4.0 generation."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from dataclasses import field
from pathlib import Path
from typing import Any

try:
    from pydantic.dataclasses import dataclass

    from astrbot.api.event import filter
    from astrbot.api.star import Context, Star
    from astrbot.core.agent.run_context import ContextWrapper
    from astrbot.core.agent.tool import FunctionTool, ToolExecResult
    from astrbot.core.astr_agent_context import AstrAgentContext
    from astrbot.core.message.components import Image
    from astrbot.core.message.message_event_result import MessageChain
except ImportError as exc:
    raise ImportError("AstrBot 4.x is required to load Whale Memes") from exc

# In this repository's runtime layout the host package is a sibling of runtime/.
_host_root = Path(__file__).resolve().parents[5] / "host"
if _host_root.is_dir() and str(_host_root) not in sys.path:
    sys.path.insert(0, str(_host_root))

from astrbot_whale_device.jimeng_client import (  # noqa: E402
    CredentialsMissing,
    JimengClient,
    image_bytes_from_result,
)
from astrbot_whale_device.memes.catalog import CATALOG_PROMPT, parse_catalog  # noqa: E402
from astrbot_whale_device.memes.library import MemeLibrary  # noqa: E402
from astrbot_whale_device.memes.references import (  # noqa: E402
    HostedReferenceUploader,
    ReferenceUploadNotConfigured,
    TOSReferenceUploader,
    fixed_reference,
)


@dataclass
class RetrieveOrGenerateMemeTool(FunctionTool[AstrAgentContext]):
    __pydantic_config__ = {"arbitrary_types_allowed": True}
    library: Any = None
    astr_context: Any = None
    generation_lock: Any = None
    generation_enabled: bool = False
    tos_endpoint: str = ""
    tos_region: str = ""
    tos_bucket: str = ""
    behavior_state: Any = field(default_factory=dict)
    spontaneous_cooldown_seconds: float = 90
    spontaneous_min_turn_gap: int = 3
    repeated_image_cooldown_seconds: float = 600
    name: str = "retrieve_or_generate_meme"
    description: str = (
        "Find and send one suitable local meme when explicitly requested, or when it naturally adds emotion "
        "to casual teasing, shared jokes, celebration, embarrassment, or comfort. Never add a meme to serious answers, "
        "device-task status, or failure explanations. Spontaneous memes require 90 seconds and three reply turns "
        "since the previous meme; do not call every turn. Explicit requests take priority over cooldowns. Search local images first. Generate only when the user "
        "needs a new meme and set generate_if_missing=true. Keep it occasional and relevant; never call every turn. "
        "Do not send personal details, private conversation quotes, or identifying information to the image generator."
    )
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Describe the emotion, scene, visual content, and any exact text desired.",
                },
                "generate_if_missing": {
                    "type": "boolean",
                    "description": "Allow generation if no suitable local image exists. Use a generic visual prompt without private information.",
                    "default": False,
                },
                "character": {
                    "type": "string",
                    "enum": [
                        "deepseek",
                        "ds_q",
                        "gpt",
                        "gpt_q",
                        "claude",
                        "claude_q",
                        "glm",
                        "qwen",
                        "gemini",
                        "grok",
                        "kimi",
                        "minimax",
                    ],
                    "description": "角色及画风。未指定时使用鲸鱼娘。",
                },
            },
            "required": ["query"],
        }
    )

    async def _send(self, event: Any, image_path: str) -> None:
        session = getattr(event, "unified_msg_origin", None)
        if not session:
            raise RuntimeError("message session is unavailable")
        state = self.behavior_state.setdefault(session, {"turn": 0})
        if state.get("sending") or getattr(event, "get_extra", lambda *args: False)(
            "_whale_meme_sent", False
        ):
            raise RuntimeError("one meme per reply")
        chain = MessageChain(chain=[Image.fromFileSystem(image_path)])
        state["sending"] = True
        try:
            sent = await asyncio.wait_for(
                self.astr_context.send_message(session, chain), timeout=30
            )
            if sent is False:
                raise RuntimeError("image send failed")
            state.update(
                last_sent=time.monotonic(),
                last_turn=state.get("turn", 0),
                last_path=image_path,
            )
            recent = state.setdefault("recent", {})
            recent[image_path] = time.monotonic()
            state["recent"] = {
                path: stamp
                for path, stamp in recent.items()
                if time.monotonic() - stamp < self.repeated_image_cooldown_seconds
            }
            getattr(event, "set_extra", lambda *args: None)("_whale_meme_sent", True)
        finally:
            state["sending"] = False

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        query: str,
        generate_if_missing: bool = False,
        character: str = "ds_q",
    ) -> ToolExecResult:
        query = (query or "").strip()
        if not query:
            return json.dumps({"sent": False, "error": "empty_query"})
        if not self.library or not self.astr_context:
            return json.dumps(
                {"sent": False, "error": "local_meme_library_unavailable"}
            )
        event = getattr(context.context, "event", None)
        if event is None:
            return json.dumps({"sent": False, "error": "message_event_unavailable"})

        session = getattr(event, "unified_msg_origin", "")
        state = self.behavior_state.setdefault(session, {"turn": 0})
        text = str(getattr(event, "get_message_str", lambda: "")() or "")
        requested = bool(
            re.search(
                r"(?:发|给|来|找|要|再|换|看|用|分享).{0,12}(?:表情包|表情|梗图|meme|sticker|斗图)|(?:表情包|梗图|meme|sticker|斗图).{0,12}(?:发|给|来|找|要|再|换|看|用)",
                text,
                re.I,
            )
        )
        prohibited = bool(
            re.search(
                r"(?:别|不要|不用|不许|停止|禁止).{0,8}(?:发|用|表情包|表情|梗图|meme|sticker)",
                text,
                re.I,
            )
        )
        if prohibited or getattr(event, "get_extra", lambda *args: False)(
            "_whale_meme_sent", False
        ):
            return json.dumps({"sent": False, "error": "meme_not_needed_this_turn"})
        if (
            not requested
            and getattr(event, "get_extra", lambda *args: False)(
                "device_tool_result", False
            )
            and not getattr(event, "get_extra", lambda *args: False)(
                "whale_casual_activity", False
            )
        ):
            return json.dumps(
                {
                    "sent": False,
                    "error": "device_task_reply",
                    "instruction": "Finish the requested phone task and reply naturally without adding an unsolicited meme.",
                }
            )
        if (
            not requested
            and "last_sent" in state
            and (
                time.monotonic() - state["last_sent"]
                < self.spontaneous_cooldown_seconds
                or state.get("turn", 0) - state.get("last_turn", 0)
                < self.spontaneous_min_turn_gap
            )
        ):
            return json.dumps(
                {
                    "sent": False,
                    "error": "spontaneous_meme_cooldown",
                    "instruction": "Continue naturally with text; do not mention internal cooldowns.",
                }
            )
        if not requested:
            generate_if_missing = False

        search_query = query
        matches = self.library.search(search_query, limit=10, minimum_score=0.12)
        if not requested:
            matches = [
                row
                for row in matches
                if time.monotonic()
                - state.get("recent", {}).get(row["path"], float("-inf"))
                >= self.repeated_image_cooldown_seconds
            ]
        else:
            matches.sort(key=lambda row: row["path"] == state.get("last_path"))
        # Existing library entries are DS unless explicitly identified otherwise.
        if character not in {"whale", "deepseek", "ds_q", "ds"}:
            token = character.removesuffix("_q")
            matches = [
                row
                for row in matches
                if token in (row["description"] + " " + row["source"]).casefold()
            ]
        if matches:
            chosen = matches[0]
            try:
                await self._send(event, chosen["path"])
            except Exception as exc:
                return json.dumps(
                    {"sent": False, "error": f"image_send_failed:{type(exc).__name__}"},
                    ensure_ascii=False,
                )
            self.library.mark_used(chosen["digest"])
            return json.dumps(
                {
                    "sent": True,
                    "generated": False,
                    "description": chosen["description"],
                    "meaning": chosen["meaning"],
                },
                ensure_ascii=False,
            )

        if not generate_if_missing:
            return json.dumps(
                {"sent": False, "generated": False, "error": "no_local_match"},
                ensure_ascii=False,
            )
        if not self.generation_enabled:
            return json.dumps(
                {
                    "sent": False,
                    "generated": False,
                    "error": "generation_disabled; configure credentials and allow_generation",
                },
                ensure_ascii=False,
            )
        try:
            client = JimengClient(timeout=45, max_polls=20, poll_interval=3)
        except CredentialsMissing:
            return json.dumps(
                {
                    "sent": False,
                    "generated": False,
                    "error": "credentials_missing; configure VOLCENGINE_ACCESS_KEY and VOLCENGINE_SECRET_KEY",
                },
                ensure_ascii=False,
            )

        lock = self.generation_lock or asyncio.Lock()
        if lock.locked():
            return json.dumps(
                {"sent": False, "generated": False, "error": "generation_in_progress"},
                ensure_ascii=False,
            )
        async with lock:
            if character == "whale":
                return json.dumps(
                    {
                        "sent": False,
                        "generated": False,
                        "error": "reference_required_for_character",
                    },
                    ensure_ascii=False,
                )
            try:
                reference = fixed_reference(self.library.root, character)
                if reference is None:
                    return json.dumps(
                        {
                            "sent": False,
                            "generated": False,
                            "error": "reference_missing_for_character",
                        },
                        ensure_ascii=False,
                    )
                uploader = TOSReferenceUploader(
                    endpoint=self.tos_endpoint,
                    region=self.tos_region,
                    bucket=self.tos_bucket,
                )
                if os.environ.get("WHALE_REFERENCE_UPLOAD_URL"):
                    uploader = HostedReferenceUploader()
                reference_url = await asyncio.to_thread(uploader.upload, reference)
            except ReferenceUploadNotConfigured:
                return json.dumps(
                    {
                        "sent": False,
                        "generated": False,
                        "error": "reference_upload_not_configured",
                    },
                    ensure_ascii=False,
                )
            except FileNotFoundError:
                return json.dumps(
                    {
                        "sent": False,
                        "generated": False,
                        "error": "reference_image_missing",
                    },
                    ensure_ascii=False,
                )
            except Exception as exc:
                return json.dumps(
                    {
                        "sent": False,
                        "generated": False,
                        "error": f"reference_upload_failed:{type(exc).__name__}",
                    },
                    ensure_ascii=False,
                )
            allowed, reason = self.library.generation_allowed(query)
            if not allowed:
                return json.dumps(
                    {"sent": False, "generated": False, "error": reason},
                    ensure_ascii=False,
                )
            try:
                prompt = f"以参考图中的{character}角色为主体，保持角色身份和外观一致，画面只出现该角色。{query}"
                submitted = await asyncio.to_thread(
                    client.submit,
                    prompt,
                    verified_fields={"size": 1048576, "image_urls": [reference_url]},
                )
                data = submitted.get("data")
                task_id = data.get("task_id") if isinstance(data, dict) else None
                if not task_id:
                    return json.dumps(
                        {
                            "sent": False,
                            "generated": False,
                            "error": "submit_response_missing_task_id",
                        },
                        ensure_ascii=False,
                    )
                result = await asyncio.to_thread(client.wait_result, str(task_id))
                image_data = image_bytes_from_result(result)
                digest = self.library.add(
                    image_data[0],
                    description="",
                    meaning="",
                    scene="",
                    source=f"jimeng_t2i_v40:{character}",
                    tags=["pending_visual_review"],
                )
                found = self.library.get(digest)
                if found is None:
                    raise RuntimeError("generated image missing from local index")
                catalog_status = "pending_visual_review"
                try:
                    provider_id = await self.astr_context.get_current_chat_provider_id(
                        event.unified_msg_origin
                    )
                    response = await self.astr_context.llm_generate(
                        chat_provider_id=provider_id,
                        prompt=CATALOG_PROMPT,
                        image_urls=[found["path"]],
                    )
                    metadata = parse_catalog(response.completion_text)
                    self.library.catalog(digest, **metadata)
                    catalog_status = "ready"
                except Exception:
                    # Keep the generated image but exclude unreviewed images from future retrieval.
                    pass
                await self._send(event, found["path"])
                self.library.mark_used(digest)
                return json.dumps(
                    {
                        "sent": True,
                        "generated": True,
                        "cached": True,
                        "digest": digest,
                        "catalog_status": catalog_status,
                    },
                    ensure_ascii=False,
                )
            except Exception as exc:
                # Do not include exception text, request fields, or credentials in tool output.
                return json.dumps(
                    {
                        "sent": False,
                        "generated": False,
                        "error": f"generation_failed:{type(exc).__name__}",
                    },
                    ensure_ascii=False,
                )


class WhaleMemesPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any]):
        super().__init__(context)
        library_root = Path(
            str(
                config.get("library_path")
                or (Path(__file__).resolve().parents[5] / "data" / "memes")
            )
        )
        self.library = MemeLibrary(
            library_root,
            daily_generation_limit=config.get("daily_generation_limit", 5),
            cooldown_seconds=config.get("duplicate_cooldown_seconds", 90),
        )
        self._generation_lock = asyncio.Lock()
        credentials_configured = bool(
            os.environ.get("VOLCENGINE_ACCESS_KEY")
            and os.environ.get("VOLCENGINE_SECRET_KEY")
        )
        tool = RetrieveOrGenerateMemeTool(
            library=self.library,
            astr_context=self.context,
            generation_lock=self._generation_lock,
        )
        tool.tos_bucket = str(config.get("tos_bucket", ""))
        tool.tos_region = str(config.get("tos_region", "cn-beijing"))
        tool.tos_endpoint = str(
            config.get("tos_endpoint", "https://tos-cn-beijing.volces.com")
        )
        tool.generation_enabled = credentials_configured and bool(
            config.get("allow_generation", True)
        )
        tool.spontaneous_cooldown_seconds = max(
            0, float(config.get("spontaneous_cooldown_seconds", 90))
        )
        tool.spontaneous_min_turn_gap = max(
            1, int(config.get("spontaneous_min_turn_gap", 3))
        )
        tool.repeated_image_cooldown_seconds = max(
            0, float(config.get("repeated_image_cooldown_seconds", 600))
        )
        self.tool = tool
        self.context.add_llm_tools(tool)

    @filter.on_llm_request(priority=-100)
    async def guide_occasional_memes(self, event, req):
        """Make semantic meme use optional and track completed conversation turns."""
        session = getattr(event, "unified_msg_origin", "")
        if not session:
            return
        state = self.tool.behavior_state.setdefault(session, {"turn": 0})
        if not event.get_extra("_whale_meme_turn_counted", False):
            state["turn"] += 1
            event.set_extra("_whale_meme_turn_counted", True)
        while len(self.tool.behavior_state) > 512:
            del self.tool.behavior_state[next(iter(self.tool.behavior_state))]
        req.system_prompt = (
            (req.system_prompt or "")
            + "\n<whale_meme_chat>像平时聊天一样，在接梗、调侃、开心、害羞或安慰时，若一张表情比文字更贴切，可调用retrieve_or_generate_meme补充情绪；不用等对方明确索要，但不必每轮都发。严肃问题、信息说明、尚未完成的操作汇报和失败说明不要主动配表情；自己逛手机后的感受和轻松闲聊可以自然配表情。明确索要表情包时先找合适的发出，再按人设说话。每轮最多一张，普通自发使用需距上次至少90秒且隔三轮回复，同图十分钟内尽量换一张；明确索要可越过冷却。优先现有图库，没匹配就自然用文字，不为凑图临时生成。工具sent=true才算发出，不复述图片路径、描述或冷却规则，不要再发同一张。</whale_meme_chat>"
        )

    async def terminate(self):
        return None
