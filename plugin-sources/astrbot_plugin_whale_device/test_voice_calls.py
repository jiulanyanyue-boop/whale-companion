import asyncio
import base64
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from voice_calls import VoiceCallRuntime


def runtime():
    plugin = SimpleNamespace(
        config={},
        owner_user_id="123",
        platform_id="whale_qq",
        owner_persona_id="owner",
        voice=SimpleNamespace(transcribe_path=AsyncMock(return_value=("你好", "test"))),
        context=SimpleNamespace(
            get_current_chat_provider_id=AsyncMock(return_value="chat"),
            llm_generate=AsyncMock(
                return_value=SimpleNamespace(completion_text="你好呀")
            ),
        ),
        _persona_prompt=AsyncMock(return_value="人设"),
    )
    return VoiceCallRuntime(plugin)


def test_call_gateway_rejects_other_hosts_and_embedded_credentials():
    for url in [
        "http://example.com:6112",
        "http://user@127.0.0.1:6112",
        "http://127.0.0.1:6113",
        "http://127.0.0.1:6112/extra",
    ]:
        plugin = SimpleNamespace(config={"voice_call_gateway": url})
        with pytest.raises(ValueError):
            VoiceCallRuntime(plugin)


def test_untrusted_caller_never_reaches_asr_or_model():
    worker = runtime()
    asyncio.run(
        worker.handle_utterance(
            None,
            {"caller_id": "999", "call_id": "call", "utterance_id": "1", "audio": ""},
        )
    )
    worker.plugin.voice.transcribe_path.assert_not_awaited()
    worker.plugin.context.llm_generate.assert_not_awaited()


def test_hangup_discards_transcript_before_model_call():
    worker = runtime()
    worker.still_current = AsyncMock(return_value=False)
    item = {
        "caller_id": "123",
        "call_id": "call",
        "utterance_id": "1",
        "audio": base64.b64encode(b"audio").decode(),
    }
    asyncio.run(worker.handle_utterance(None, item))
    worker.plugin.context.llm_generate.assert_not_awaited()
    assert worker.history == []


def test_hangup_after_model_reply_never_synthesizes_or_plays():
    worker = runtime()
    worker.still_current = AsyncMock(side_effect=[True, False])
    worker.plugin.context.get_using_tts_provider_async = AsyncMock()
    item = {
        "caller_id": "123",
        "call_id": "call",
        "utterance_id": "1",
        "audio": base64.b64encode(b"audio").decode(),
    }
    asyncio.run(worker.handle_utterance(None, item))
    worker.plugin.context.llm_generate.assert_awaited_once()
    worker.plugin.context.get_using_tts_provider_async.assert_not_awaited()
    assert worker.history == []


def test_duplicate_utterance_is_never_reprocessed():
    worker = runtime()
    worker.still_current = AsyncMock(return_value=False)
    item = {
        "caller_id": "123",
        "call_id": "call",
        "utterance_id": "1",
        "audio": base64.b64encode(b"audio").decode(),
    }

    async def run():
        await worker.handle_utterance(None, item)
        await worker.handle_utterance(None, item)

    asyncio.run(run())
    worker.plugin.voice.transcribe_path.assert_awaited_once()


@pytest.mark.parametrize("played", [True, False])
def test_complete_turn_uses_persona_and_records_only_finished_playback(
    tmp_path, played
):
    worker = runtime()
    worker.still_current = AsyncMock(return_value=True)
    path = tmp_path / "reply.wav"
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 1600)
    tts = SimpleNamespace(get_audio=AsyncMock(return_value=str(path)))
    worker.plugin.context.get_using_tts_provider_async = AsyncMock(return_value=tts)

    class Result:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def raise_for_status(self):
            pass

        async def json(self):
            return {"played": played}

    sent = []

    def post(url, **kwargs):
        sent.append(kwargs["json"])
        return Result()

    item = {
        "caller_id": "123",
        "call_id": "call",
        "utterance_id": "1",
        "audio": base64.b64encode(b"audio").decode(),
    }
    asyncio.run(worker.handle_utterance(SimpleNamespace(post=post), item))
    assert sent[0]["call_id"] == "call" and sent[0]["utterance_id"] == "1"
    assert base64.b64decode(sent[0]["audio"]).startswith(b"RIFF")
    assert (
        "人设" in worker.plugin.context.llm_generate.call_args.kwargs["system_prompt"]
    )
    assert bool(worker.history) is played
    if played:
        assert worker.history[-1]["content"] == "你好呀"
