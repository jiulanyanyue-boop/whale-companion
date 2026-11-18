"""Shared voice assets, temporary synthesis, and speech recognition."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import threading
from pathlib import Path

MAX_AUDIO_BYTES = 20 * 1024 * 1024


class VoiceService:
    """Keep voice-message and call audio on the same recognition interface."""

    def __init__(self, config, data_dir):
        self.config = config
        self.data_dir = Path(data_dir)
        self.assets = Path(
            config.get("voice_assets_dir") or Path(__file__).parent / "voice_assets"
        )
        self._model = None
        self._recognition_lock = threading.Lock()

    def clips(self):
        """Read replaceable, explicitly described recordings.

        Returns:
            Valid clip metadata, without exposing filesystem paths to the model.
        """
        path = self.assets / "catalog.json"
        if not path.is_file():
            return []
        rows = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list) or len(rows) > 500:
            raise ValueError("invalid_voice_catalog")
        result = []
        for row in rows:
            clip_id = row.get("id", "")
            if not isinstance(clip_id, str) or not clip_id or len(clip_id) > 80:
                raise ValueError("invalid_voice_clip_id")
            path = (self.assets / row["file"]).resolve()
            if not path.is_relative_to(
                self.assets.resolve()
            ) or path.suffix.lower() not in {
                ".wav",
                ".mp3",
                ".ogg",
                ".silk",
                ".amr",
                ".flac",
                ".m4a",
            }:
                raise ValueError("invalid_voice_clip_path")
            if path.is_file() and 0 < path.stat().st_size <= MAX_AUDIO_BYTES:
                result.append(
                    {
                        "id": clip_id,
                        "text": str(row.get("text", ""))[:1000],
                        "test_voice": bool(row.get("test_voice", False)),
                    }
                )
        if len({row["id"] for row in result}) != len(result):
            raise ValueError("duplicate_voice_clip_id")
        return result

    def clip_bytes(self, clip_id):
        """Resolve a catalog entry instead of accepting arbitrary model paths.

        Args:
            clip_id: Catalog identifier selected by the model.

        Returns:
            Recording bytes and the recording's actual transcript.
        """
        valid = {row["id"]: row for row in self.clips()}
        if clip_id not in valid:
            raise ValueError("voice_clip_not_found")
        rows = json.loads((self.assets / "catalog.json").read_text(encoding="utf-8"))
        row = next(row for row in rows if row["id"] == clip_id)
        path = (self.assets / row["file"]).resolve()
        if not path.is_relative_to(self.assets.resolve()):
            raise ValueError("invalid_voice_clip_path")
        with path.open("rb") as stream:
            data = stream.read(MAX_AUDIO_BYTES + 1)
        if not data or len(data) > MAX_AUDIO_BYTES:
            raise ValueError("voice_file_too_large")
        return data, valid[clip_id]["text"]

    async def transcribe_path(self, path, context=None, session=None):
        """Recognize a voice recording or a completed call utterance.

        Args:
            path: Resolved audio file supplied by the trusted media adapter.
            context: AstrBot context for a user-configured STT provider.
            session: Real originating session; never derived from recognized text.

        Returns:
            Recognized text and the actual recognition backend.
        """
        path = Path(path)
        if not path.is_file() or not 0 < path.stat().st_size <= MAX_AUDIO_BYTES:
            raise ValueError("invalid_voice_file")
        if context is not None:
            provider = await context.get_using_stt_provider_async(session)
            if provider is not None:
                try:
                    text = await asyncio.wait_for(
                        provider.get_text(audio_url=str(path)), 90
                    )
                    if isinstance(text, str) and text.strip():
                        return text.strip()[:8000], "configured_stt"
                except Exception:
                    # A provider outage must not disable an already prepared local model.
                    pass
        return await asyncio.to_thread(self._local_transcribe, path)

    def _local_transcribe(self, path):
        """Serialize CPU recognition so concurrent sessions cannot exhaust memory.

        Args:
            path: Local audio path, including WAV call utterances.

        Returns:
            Transcript and local backend identifier.
        """
        import av
        from faster_whisper import WhisperModel

        with av.open(str(path)) as recording:
            if (
                recording.duration is not None
                and recording.duration / av.time_base > 180
            ):
                raise ValueError("voice_too_long_max_180_seconds")
        if not self._recognition_lock.acquire(blocking=False):
            raise RuntimeError("voice_recognizer_busy")
        try:
            if self._model is None:
                self._model = WhisperModel(
                    str(self.config.get("voice_local_model", "base")),
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=4,
                    download_root=str(self.data_dir / "models"),
                    local_files_only=True,
                )
            segments, _ = self._model.transcribe(
                str(path), language="zh", beam_size=5, vad_filter=True
            )
            text = "".join(segment.text for segment in segments).strip()
            if not text:
                raise ValueError("no_speech_recognized")
            return text[:8000], "local_whisper"
        finally:
            self._recognition_lock.release()

    @staticmethod
    def synthesize_test_voice(text, path):
        """Use the installed Windows voice until a real TTS provider is supplied.

        Args:
            text: Exact words to speak, passed as data through standard input.
            path: Owned temporary output file.
        """
        if os.name != "nt":
            raise RuntimeError("configure_tts_provider_or_select_clip")
        script = r"""Add-Type -AssemblyName System.Speech
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
  $s.SelectVoice('Microsoft Huihui Desktop')
  $s.SetOutputToWaveFile($request.path)
  $s.Speak($request.text)
} finally { $s.Dispose() }
"""
        # EncodedCommand fixes console encoding; user text is never executable code.
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            input=json.dumps({"text": text, "path": str(path)}, ensure_ascii=True),
            text=True,
            capture_output=True,
            timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode or not Path(path).is_file():
            raise RuntimeError("test_voice_synthesis_failed")
