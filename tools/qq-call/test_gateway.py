import array
import base64
import importlib.util
import io
import threading
import unittest
import urllib.error
import urllib.request
import wave
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "qq_call_gateway", Path(__file__).with_name("gateway.py")
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def wav_data():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 320)
    return base64.b64encode(stream.getvalue()).decode()


class GatewayTests(unittest.TestCase):
    def test_speech_segmentation_has_silence_and_minimum_speech(self):
        buffer = module.SpeechBuffer()
        silence = b"\0\0" * 320
        speech = array.array("h", [1000] * 320).tobytes()
        self.assertTrue(all(buffer.feed(silence) is None for _ in range(50)))
        for _ in range(25):
            self.assertIsNone(buffer.feed(speech))
        results = [buffer.feed(silence) for _ in range(31)]
        self.assertEqual(sum(item is not None for item in results), 1)

    def test_stale_call_and_foreign_caller_never_play_audio(self):
        with TemporaryDirectory() as tmp:
            gate = module.AudioGateway(
                "x" * 40,
                "123",
                "http://127.0.0.1:6110",
                Path(tmp) / "lease",
                "unix:/unused",
            )
            gate.call = {"phase": "connected", "callerUin": "999", "inviteAt": "new"}
            gate.pending["1"] = "old"
            with patch.object(module.subprocess, "Popen") as spawn:
                receipt = gate.play(
                    {"call_id": "old", "utterance_id": "1", "audio": wav_data()}
                )
                self.assertFalse(receipt["played"])
                spawn.assert_not_called()
            gate.call["callerUin"] = "123"
            self.assertFalse(
                gate.play({"call_id": "old", "utterance_id": "1", "audio": wav_data()})[
                    "played"
                ]
            )

    def test_audio_and_state_require_bearer_token(self):
        with TemporaryDirectory() as tmp:
            gate = module.AudioGateway(
                "x" * 40,
                "123",
                "http://127.0.0.1:6110",
                Path(tmp) / "lease",
                "unix:/unused",
            )
            server = ThreadingHTTPServer(("127.0.0.1", 0), module.make_handler(gate))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                url = f"http://127.0.0.1:{server.server_port}/v1/status"
                with self.assertRaises(urllib.error.HTTPError) as error:
                    opener.open(url)
                self.assertEqual(error.exception.code, 401)
                req = urllib.request.Request(
                    url, headers={"Authorization": "Bearer " + "x" * 40}
                )
                with opener.open(req) as response:
                    self.assertEqual(response.status, 200)
                    self.assertNotIn(b"x" * 40, response.read())
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
