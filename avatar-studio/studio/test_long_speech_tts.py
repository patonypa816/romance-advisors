"""Offline long-text boundary checks: fake HTTP and local generated WAV only."""

import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from action_schema import LONG_SPEECH_MAX_TEXT
from tts_live import MAX_AUDIO_BYTES, MODEL, VOICE, synthesize_segment


KEY = "offline-fake-key"
INSTRUCTION = "自然清晰，像平常交谈。"


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\x00\x00" * 24000)
    return buffer.getvalue()


class FakeResponse:
    status = 200
    content_type = "audio/wav"

    def __init__(self, data=b"", status=200):
        self.data = data
        self.status = status
        self.content_length = len(data)
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self, **_):
        return {"output": {"finish_reason": "stop", "audio": {
            "url": "https://example.invalid/offline-audio",
        }}, "usage": {"input_tokens": 1, "output_tokens": 1}}

    async def iter_chunked(self, size):
        for start in range(0, len(self.data), size):
            yield self.data[start:start + size]


class FakeHTTP:
    def __init__(self, *, status=200, data=None, transport_error=False):
        self.posts = 0
        self.gets = 0
        self.request = None
        self.status = status
        self.data = wav_bytes() if data is None else data
        self.transport_error = transport_error

    def post(self, _, **kwargs):
        self.posts += 1
        self.request = kwargs["json"]
        if self.transport_error:
            raise OSError("offline simulated failure")
        return FakeResponse(status=self.status)

    def get(self, _, **kwargs):
        self.gets += 1
        assert kwargs["headers"]["Authorization"] == ""
        return FakeResponse(self.data)


class LongSpeechTTSTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="long-speech-tts-offline-")
        self.root = Path(self.temp.name)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def call(self, http, text, *, name="sample", **kwargs):
        return await synthesize_segment(
            http, KEY, "ws-offline-test", self.root / name, text, INSTRUCTION, **kwargs
        )

    async def test_explicit_mode_preserves_31_and_120_characters(self):
        self.assertEqual(LONG_SPEECH_MAX_TEXT, 120)
        for size in (31, LONG_SPEECH_MAX_TEXT):
            with self.subTest(size=size):
                text = "字" * size
                http = FakeHTTP()
                result = await self.call(http, text, name=str(size), long_speech_test=True)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["text"], text)
                self.assertEqual(result["text_codepoints"], size)
                self.assertEqual(http.posts, 1)
                self.assertEqual(http.gets, 1)
                self.assertEqual(http.request["input"]["text"], text)
                self.assertEqual(http.request["input"]["instruction"], INSTRUCTION)
                self.assertEqual(http.request["model"], MODEL)
                self.assertEqual(http.request["input"]["voice"], VOICE)
                self.assertEqual(result["audio_seconds"], 1.0)

    async def test_rejected_boundaries_make_zero_posts(self):
        for name, size, kwargs in (
            ("normal", 31, {}),
            ("false", 31, {"long_speech_test": False}),
            ("too-long", 121, {"long_speech_test": True}),
        ):
            with self.subTest(name=name):
                http = FakeHTTP()
                result = await self.call(http, "字" * size, name=name, **kwargs)
                self.assertEqual(result["status"], "invalid_input")
                self.assertEqual(http.posts, 0)
                self.assertEqual(http.gets, 0)
                self.assertFalse((self.root / name).exists())

    async def test_long_mode_keeps_30_second_audio_limit(self):
        async def overlong_probe(*_, **__):
            return {"streams": [{"codec_type": "audio", "duration": "30.01"}],
                    "format": {"duration": "30.01"}}

        http = FakeHTTP()
        with patch("tts_live._probe", side_effect=overlong_probe):
            result = await self.call(http, "字" * 120, long_speech_test=True)
        self.assertEqual(result["status"], "audio_unverified")
        self.assertIsNone(result["pcm_path"])
        self.assertEqual(http.posts, 1)

    async def test_long_mode_keeps_two_megabyte_limit(self):
        http = FakeHTTP(data=b"x" * (MAX_AUDIO_BYTES + 1))
        result = await self.call(http, "字" * 120, long_speech_test=True)
        self.assertEqual(result["status"], "audio_too_large")
        self.assertEqual(http.posts, 1)

    async def test_error_and_transport_failure_are_not_retried(self):
        for name, http, expected in (
            ("http-error", FakeHTTP(status=403), "error_response"),
            ("transport-error", FakeHTTP(transport_error=True), "transport_error"),
        ):
            with self.subTest(name=name):
                result = await self.call(http, "字" * 120, name=name, long_speech_test=True)
                self.assertEqual(result["status"], expected)
                self.assertEqual(http.posts, 1)
                self.assertEqual(http.gets, 0)


if __name__ == "__main__":
    unittest.main()
