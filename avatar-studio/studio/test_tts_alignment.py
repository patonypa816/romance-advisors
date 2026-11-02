"""Offline SSE, timestamp, privacy and cancellation checks; no service calls."""

import asyncio
import base64
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from tts_live import MAX_AUDIO_BYTES, MAX_EVENT_LOG_BYTES, synthesize_segment
from semantic_speech import compile_semantic_timeline


KEY = "offline-alignment-key"
SIGNED_URL = "https://example.invalid/sample.wav?signature=private-value"
TEXT = "你好，再见。"
WORDS = [
    {"text": text, "begin_index": i, "end_index": i + 1,
     "begin_time": i * 80, "end_time": (i + 1) * 80}
    for i, text in enumerate(("你", "好", "，再", "见。"))
]


def wav_bytes():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(b"\0\0" * 24000)
    return buf.getvalue()


def sentence(words, *, index=0, kind="sentence-begin"):
    return {"output": {"type": kind, "sentence": {"index": index, "words": words}}}


def events(data=None):
    data = wav_bytes() if data is None else data
    return [sentence(WORDS[:2]), sentence([]),
            {"output": {"audio": {"data": base64.b64encode(data).decode("ascii")}}},
            sentence(WORDS, kind="sentence-end"),
            {"request_id": "offline-request", "output": {
                "finish_reason": "stop", "audio": {"url": SIGNED_URL}},
             "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}}]


def encode(items):
    return b"".join(b"event: result\r\ndata: " +
                    json.dumps(item, ensure_ascii=False).encode() + b"\r\n\r\n"
                    for item in items)


class Response:
    status = 200

    def __init__(self, raw, *, sse=False, cancel=False):
        self.raw = raw
        self.content = self
        self.content_length = len(raw)
        self.content_type = "text/event-stream" if sse else "audio/wav"
        self.cancel = cancel

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def iter_chunked(self, size):
        # Split inside UTF-8 and SSE separators, as a real TCP stream may do.
        if self.cancel:
            raise asyncio.CancelledError()
        chunk_size = 97 if len(self.raw) < 100_000 else size
        for start in range(0, len(self.raw), chunk_size):
            yield self.raw[start:start + chunk_size]


class HTTP:
    def __init__(self, items=None, *, raw=None, data=None, cancel=False):
        self.data = wav_bytes() if data is None else data
        self.raw = encode(events() if items is None else items) if raw is None else raw
        self.posts = self.gets = 0
        self.cancel = cancel
        self.headers = None

    def post(self, _, **kwargs):
        self.posts += 1
        self.request = kwargs["json"]
        self.headers = kwargs["headers"]
        assert kwargs["allow_redirects"] is False
        return Response(self.raw, sse=True, cancel=self.cancel)

    def get(self, _, **kwargs):
        self.gets += 1
        assert kwargs["headers"]["Authorization"] == ""
        assert kwargs["allow_redirects"] is False
        return Response(self.data)


class AlignmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="tts-alignment-offline-")
        self.root = Path(self.temp.name)

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def call(self, http, text=TEXT, name="attempt"):
        return await synthesize_segment(http, KEY, "ws-offline", self.root / name,
                                        text, "自然清晰。", word_timestamps=True)

    async def test_cumulative_snapshots_empty_words_and_pcm_binding(self):
        http = HTTP()
        result = await self.call(http)
        self.assertEqual(result["status"], "ok")
        alignment = result["alignment"]
        self.assertEqual(alignment["status"], "ok")
        self.assertEqual(alignment["words"], WORDS)
        self.assertEqual(alignment["timebase"], "audio_start")
        self.assertEqual(alignment["granularity_ms"], 80)
        self.assertEqual(alignment["pcm_sha256"], hashlib.sha256(
            Path(result["pcm_path"]).read_bytes()).hexdigest())
        self.assertTrue(http.request["input"]["word_timestamp_enabled"])
        self.assertEqual(http.headers["X-DashScope-SSE"], "enable")
        self.assertEqual((http.posts, http.gets), (1, 1))
        evidence = "".join(p.read_text() for p in (self.root / "attempt").glob("*.json*"))
        self.assertNotIn(KEY, evidence)
        self.assertNotIn(SIGNED_URL, evidence)
        self.assertNotIn("private-value", evidence)
        self.assertNotIn(base64.b64encode(wav_bytes()).decode(), evidence)
        log = self.root / "attempt" / "events-safe.jsonl"
        self.assertLessEqual(log.stat().st_size, MAX_EVENT_LOG_BYTES)
        self.assertEqual(json.loads(log.read_text().splitlines()[-1])["final_words"], WORDS)

    async def test_verified_pcm_survives_missing_usage_without_retry(self):
        items = events()
        items[-1].pop("usage")
        http = HTTP(items)
        result = await self.call(http, name="unverified-usage")
        self.assertEqual(result["status"], "usage_unverified")
        self.assertFalse(result["usage_verified"])
        self.assertIsNone(result["cost_cny"])
        self.assertEqual(result["pcm_sha256"], hashlib.sha256(Path(result["pcm_path"]).read_bytes()).hexdigest())
        self.assertEqual(result["alignment"]["pcm_sha256"], result["pcm_sha256"])
        self.assertEqual((http.posts, http.gets), (1, 1))

    async def punctuation_result(self, punctuation, *, width=None, changes=None, name="punctuation"):
        width = len(punctuation) if width is None else width
        words = [
            {"text": "你", "begin_index": 0, "end_index": 1, "begin_time": 0, "end_time": 80},
            {"text": punctuation, "begin_index": 1, "end_index": 1 + width, "begin_time": 80, "end_time": 240},
            {"text": "再见。", "begin_index": 1 + width, "end_index": 3 + width, "begin_time": 320, "end_time": 480},
        ]
        if changes:
            words[1].update(changes)
        items = events()
        items[0] = sentence(words[:1])
        items[3] = sentence(words, kind="sentence-end")
        return await self.call(HTTP(items), text="你" + punctuation + "再见。", name=name)

    async def test_standalone_punctuation_indices_preserve_real_word_anchor(self):
        for index, punctuation in enumerate(("。", "？！")):
            result = await self.punctuation_result(punctuation, name=str(index))
            self.assertEqual(result["alignment"]["status"], "ok")
            self.assertEqual(result["alignment"]["words"][1]["text"], punctuation)
            action = {"anchor": {"quote": "再见", "occurrence": 1},
                      "motions": [{"part": "head", "description": "轻轻点头"}],
                      "gaze": "注视镜头", "expression": "神情温和"}
            nodes = compile_semantic_timeline([action], {"gaze": "注视镜头", "expression": "神情平静"},
                                               result["alignment"], 4)
            self.assertEqual(nodes[1]["at_seconds"], .32)
            self.assertNotEqual(nodes[1]["at_seconds"], .08)

    async def test_standalone_punctuation_does_not_allow_blank_or_nonpositive_width(self):
        for index, (text, width) in enumerate((("", 1), (" ", 1), ("\n", 1), ("。", 0), ("。", -1))):
            result = await self.punctuation_result(text, width=width, name=str(index))
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["alignment"]["reason"], "invalid_timestamps")

    async def test_provider_index_width_is_not_a_character_count(self):
        for index, (text, width) in enumerate((("？！", 1), ("。 ", 2), ("黢黢", 1))):
            result = await self.punctuation_result(text, width=width, name=str(index))
            self.assertEqual(result["alignment"]["status"], "ok")
            self.assertEqual(result["alignment"]["words"][1]["text"], text)
            self.assertEqual(result["alignment"]["words"][1]["end_index"], 1 + width)

    async def test_standalone_punctuation_still_requires_valid_time_window(self):
        for index, change in enumerate(({"begin_time": 79}, {"end_time": 80},
                                       {"end_time": 1001}, {"begin_time": -1})):
            result = await self.punctuation_result("。", changes=change, name=str(index))
            self.assertEqual(result["alignment"]["reason"], "invalid_timestamps")

    async def test_acronym_whitespace_is_equivalent_without_changing_original_words(self):
        for index, (source, space) in enumerate((("AI。", " "), ("A I。", ""), ("AI。", "\t\u3000"))):
            words = [{"text": "A", "begin_index": 0, "end_index": 1, "begin_time": 0, "end_time": 80},
                     {"text": space + "I。", "begin_index": 1, "end_index": 2, "begin_time": 80, "end_time": 160}]
            items = events(); items[0] = sentence(words[:1]); items[3] = sentence(words, kind="sentence-end")
            result = await self.call(HTTP(items), text=source, name=str(index))
            self.assertEqual(result["alignment"]["status"], "ok")
            self.assertEqual(result["alignment"]["words"], words)

    async def test_text_differences_are_retained_for_later_local_anchor_verification(self):
        words = [{"text": "A", "begin_index": 0, "end_index": 1, "begin_time": 0, "end_time": 80},
                 {"text": " I。", "begin_index": 1, "end_index": 2, "begin_time": 80, "end_time": 160}]
        for index, source in enumerate(("ai。", "AI？", "AI1。", "A。", "A你I。", "A\u200bI。", "ＡI。")):
            items = events(); items[0] = sentence(words[:1]); items[3] = sentence(words, kind="sentence-end")
            result = await self.call(HTTP(items), text=source, name=str(index))
            self.assertEqual(result["alignment"]["status"], "ok")
            self.assertFalse(result["alignment"]["text_matches_script"])
            self.assertEqual(result["alignment"]["words"], words)

    async def test_120_characters_enabled_and_121_rejected(self):
        items = events()
        long_words = [{"text": "字", "begin_index": i, "end_index": i + 1,
                       "begin_time": i, "end_time": i + 1} for i in range(120)]
        items[0] = sentence(long_words)
        items[3] = sentence(long_words, kind="sentence-end")
        result = await self.call(HTTP(items), text="字" * 120)
        self.assertEqual(result["alignment"]["status"], "ok")
        http = HTTP()
        result = await self.call(http, text="字" * 121, name="too-long")
        self.assertEqual(result["status"], "invalid_input")
        self.assertEqual(http.posts, 0)

    async def test_alignment_failures_keep_audio_without_retry(self):
        cases = {}
        items = events()
        items.insert(3, sentence([], index=1))
        cases["multiple_sentences_unverified"] = items
        items = events()
        items[0] = sentence([])
        items[3] = sentence([], kind="sentence-end")
        cases["timestamps_missing"] = items
        items = events(data=b"different stream bytes")
        cases["stream_download_mismatch"] = items
        items = events()
        items[2]["output"]["audio"]["data"] = "invalid base64!"
        cases["stream_audio_invalid"] = items
        items = events()
        items[3]["output"]["sentence"]["words"] = copy.deepcopy(WORDS)
        items[3]["output"]["sentence"]["words"][-1]["begin_index"] = 99
        cases["invalid_timestamps"] = items
        items = events()
        items[3]["output"]["sentence"]["words"] = copy.deepcopy(WORDS)
        items[3]["output"]["sentence"]["words"][0]["end_time"] = 81
        cases["inconsistent_snapshots"] = items
        for reason, items in cases.items():
            with self.subTest(reason=reason):
                http = HTTP(items)
                result = await self.call(http, name=reason)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["alignment"]["reason"], reason)
                self.assertEqual(result["alignment"]["words"], [])
                self.assertEqual(result["alignment"]["pcm_sha256"], result["pcm_sha256"])
                self.assertEqual((http.posts, http.gets), (1, 1))

    async def test_nonmonotonic_out_of_audio_missing_and_bool_timestamps(self):
        for name, change in (
            ("backwards", lambda w: w[-1].update(begin_time=1)),
            ("beyond_audio", lambda w: w[-1].update(end_time=1001)),
            ("missing", lambda w: w[-1].pop("begin_time")),
            ("bool", lambda w: w[-1].update(end_time=True)),
            ("negative", lambda w: w[0].update(begin_time=-1)),
        ):
            with self.subTest(name=name):
                items = events()
                words = copy.deepcopy(WORDS)
                change(words)
                items[0] = sentence([])
                items[3] = sentence(words, kind="sentence-end")
                result = await self.call(HTTP(items), name=name)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["alignment"]["reason"], "invalid_timestamps")

    async def test_truncated_or_malformed_sse_is_not_retried(self):
        for name, raw, expected in (
            ("missing-stop", encode(events()[:-1]), "tts_not_finished"),
            ("partial-final", encode(events())[:-2], "tts_not_finished"),
            ("invalid-json", b"data: {broken}\n\n", "invalid_response"),
        ):
            with self.subTest(name=name):
                http = HTTP(raw=raw)
                result = await self.call(http, name=name)
                self.assertEqual(result["status"], expected)
                self.assertEqual((http.posts, http.gets), (1, 0))

    async def test_stream_and_download_size_limits_and_duration_limit(self):
        huge = b"x" * (MAX_AUDIO_BYTES + 1)
        for name, http in (("stream", HTTP(events(data=huge))),
                           ("download", HTTP(data=huge))):
            result = await self.call(http, name=name)
            self.assertEqual(result["status"], "audio_too_large")
            self.assertEqual(http.posts, 1)
        with patch("tts_live._probe", return_value={
            "streams": [{"codec_type": "audio", "duration": "30.01"}],
            "format": {"duration": "30.01"}}):
            result = await self.call(HTTP(), name="duration")
        self.assertEqual(result["status"], "audio_unverified")

    async def test_cancel_propagates_and_records_without_retry(self):
        http = HTTP(cancel=True)
        with self.assertRaises(asyncio.CancelledError):
            await self.call(http)
        self.assertEqual((http.posts, http.gets), (1, 0))
        self.assertEqual(json.loads((self.root / "attempt" / "result.json").read_text())[
            "status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
