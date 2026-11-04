"""SSML stays out of subtitles and transport text; timing remains PCM-bound."""
import copy
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from live_session import LiveSession
from motion_component import MotionComponent
from semantic_speech import (parse_dialogue_semantic_plan, speech_transcription,
                             validate_ssml_action_anchors)
from test_motion_component import FakeSocket, NoNetwork
from timed_action_planner import prepare_timed_action_request


GE = {"gaze": "看向镜头", "expression": "自然微笑"}


def action(quote):
    return {"anchor": {"quote": quote, "occurrence": 1}, "motions": [
        {"part": "head", "description": "轻轻点头"}], **GE}


class SSMLContracts(unittest.TestCase):
    def test_long_alias_cannot_bypass_transport_speech_budget(self):
        with self.assertRaisesRegex(ValueError, "实际朗读"):
            speech_transcription("甲", '<speak><sub alias="' + "甲" * 121 + '">甲</sub></speak>',
                                 None, 1, "same")

    def test_phoneme_tokens_never_leak_into_unknown_say_as_transcription(self):
        xml = '<speak><phoneme alphabet="py" ph="hang2">行</phoneme><say-as interpret-as="digits">1</say-as></speak>'
        alignment = {"status": "ok", "timebase": "audio_start", "pcm_sha256": "same",
            "words": [{"text": "[h][ang]一", "begin_time": 0, "end_time": 200}]}
        with self.assertRaisesRegex(ValueError, "实际朗读"):
            speech_transcription("行1", xml, alignment, .2, "same")

    def test_plan_keeps_xml_and_plain_text_separate(self):
        part = {"text": "你好。", "instruction": "自然", "actions": [], "recovery": None,
                "ssml": '<speak>你<break time="450ms"/>好。</speak>'}
        body = {"emotion": "自然", "reading": None, "segments": [part]}
        self.assertEqual(parse_dialogue_semantic_plan(body, mode="once"), body)
        wrong = copy.deepcopy(body)
        wrong["segments"][0]["ssml"] = '<speak>换了正文。</speak>'
        with self.assertRaises(ValueError):
            parse_dialogue_semantic_plan(wrong, mode="once")

    def test_rewritten_span_cannot_become_an_action_anchor(self):
        text = "WHO告诉我"; xml = '<speak><sub alias="世界卫生组织">WHO</sub>告诉我</speak>'
        with self.assertRaisesRegex(ValueError, "未改写"):
            validate_ssml_action_anchors(text, xml, [action("WHO")])
        validate_ssml_action_anchors(text, xml, [action("告诉我")])

    def test_planner_excludes_rewritten_words_even_when_provider_keeps_literal_text(self):
        text = "WHO告诉我"
        part = {"text": text, "instruction": "自然", "actions": [action("告诉我")], "recovery": GE,
                "ssml": '<speak><sub alias="世界卫生组织">WHO</sub>告诉我</speak>'}
        alignment = {"status": "ok", "timebase": "audio_start", "words": [
            {"text": "WHO", "begin_time": 0, "end_time": 300},
            {"text": "告诉我", "begin_time": 600, "end_time": 900}]}
        clean, data, _ = prepare_timed_action_request(part, alignment, 1, {})
        self.assertEqual(clean["ssml"], part["ssml"])
        quotes = [g["anchor"]["quote"] for s in data["schedules"] for g in s["groups"]]
        self.assertTrue(quotes)
        self.assertTrue(all("WHO" not in quote for quote in quotes))

    def test_cache_identity_changes_with_voice_and_ssml(self):
        session = SimpleNamespace(tts_voice="one", brain_context={})
        comment = {"plan": {"segments": [{"text": "你好", "ssml": '<speak>你好</speak>'}]}}
        before = LiveSession._preparation_fingerprint(session, comment)
        session.tts_voice = "two"
        self.assertNotEqual(before, LiveSession._preparation_fingerprint(session, comment))
        session.tts_voice = "one"
        comment["plan"]["segments"][0]["ssml"] = '<speak>你<break/>好</speak>'
        self.assertNotEqual(before, LiveSession._preparation_fingerprint(session, comment))


class SSMLTransport(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def component(self):
        out = Path(self.temp.name)
        component = MotionComponent(NoNetwork(), out, out / "unused.png")
        component._ws = FakeSocket()
        component.live_id = "offline-only"
        component._ready.set(); component._joined.set(); component.state = "ready"
        component.DRAIN_SECONDS = 0
        return component

    async def test_say_as_uses_pcm_bound_spoken_words_for_type10(self):
        pcm = b'\0\0' * 4800
        digest = hashlib.sha256(pcm).hexdigest()
        alignment = {"status": "ok", "timebase": "audio_start", "pcm_sha256": digest,
                     "words": [{"text": "一二三", "begin_time": 0, "end_time": 200}]}
        xml = '<speak><say-as interpret-as="digits">123</say-as></speak>'
        spoken = speech_transcription("123", xml, alignment, .2, digest)
        self.assertEqual(spoken, "一二三")
        component = self.component()
        await component.send_aligned_expression(pcm, "123", [], 1, alignment=alignment,
                                                ssml=xml, transcription_text=spoken)
        texts = [x["frame"]["payload"]["text_msg"]["content"] for x in component._ws.sent
                 if x["frame"]["type"] == 10]
        self.assertEqual(texts, ["一二三"])
        self.assertEqual(b''.join(row["chunk"] for row in component._ws.binary), pcm)

    async def test_unknown_reading_or_changed_transcription_sends_nothing(self):
        pcm = b'\0\0' * 4800
        xml = '<speak><say-as interpret-as="digits">123</say-as></speak>'
        for alignment, spoken in ((None, "123"), ({"status": "ok", "timebase": "audio_start",
                "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
                "words": [{"text": "一二三", "begin_time": 0, "end_time": 200}]}, "改了")):
            component = self.component()
            with self.assertRaises(ValueError):
                await component.send_aligned_expression(pcm, "123", [], 1, alignment=alignment,
                                                       ssml=xml, transcription_text=spoken)
            self.assertEqual(component._ws.sent, [])
            self.assertEqual(component._ws.binary, [])


if __name__ == '__main__':
    unittest.main()
