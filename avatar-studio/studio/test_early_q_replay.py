"""只提前第一Q的固定素材对照；临时合成fixture、真实PCM节拍，无云调用。"""
import copy
from pathlib import Path
import tempfile
import unittest

import test_fixed_replay as fixture_tests
from fixed_replay import FixedReplay
from motion_component import MotionComponent
from test_motion_component import FakeSocket, NoNetwork


class EarlyQReplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="early-q-replay-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = fixture_tests.FixedReplayTests.source(self)

    def fixture(self, **options):
        return FixedReplay(self.source, **({"action_kind": "L", "long_action_group_count": 2} | options))

    def test_only_first_hold_and_derived_q_timing_change_with_all_source_material_preserved(self):
        before = {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        baseline = self.fixture()
        early = self.fixture(first_q_after_seconds=3.0)
        original, changed = baseline.plan, early.plan
        original_part, part = original["segments"][0], changed["segments"][0]
        self.assertEqual(early.pcm, baseline.pcm)
        self.assertEqual(early.wav, baseline.wav)
        self.assertEqual(early.pcm, before[Path("tts/c0001-1/sample.pcm")])
        self.assertEqual(early.wav, before[Path("tts/c0001-1/sample.wav")])
        self.assertEqual(early.audio_seconds, 17.36)
        self.assertEqual(len(early.pcm), 833280)
        self.assertEqual(early.pcm_sha256, baseline.pcm_sha256)
        self.assertEqual(early.wav_sha256, baseline.wav_sha256)
        self.assertNotEqual(early.fingerprint, baseline.fingerprint)
        self.assertEqual(original_part["actions"][0]["hold_seconds"], 7.68)
        self.assertEqual(part["actions"][0]["hold_seconds"], 3.0)
        expected = copy.deepcopy(original)
        expected["segments"][0]["actions"][0]["hold_seconds"] = 3.0
        expected_timeline = expected["segments"][0]["timeline"]
        expected_timeline[1]["hold_seconds"] = 3.0
        expected_timeline[2].update(at_seconds=3.5, delay_seconds=3.0)
        expected_timeline[3]["delay_seconds"] = 5.18
        self.assertEqual(changed, expected)
        self.assertEqual([n["at_seconds"] for n in part["timeline"]], [0, .5, 3.5, 8.68, 9.18, 17.86])
        self.assertEqual([n["instruction"] for n in part["timeline"]],
                         [n["instruction"] for n in original_part["timeline"]])
        self.assertEqual(part["actions"][1], original_part["actions"][1])
        self.assertEqual(part["text"], original_part["text"])
        self.assertEqual(early.public()["first_q_after_seconds"], 3.0)
        self.assertAlmostEqual(early.public()["q_to_next_l_seconds"], 5.68)
        self.assertAlmostEqual(baseline.public()["q_to_next_l_seconds"], 1.0)
        self.assertEqual(early.public()["second_l_audio_seconds"], 8.68)
        self.assertEqual(before, {p.relative_to(self.source): p.read_bytes() for p in self.source.rglob("*") if p.is_file()})

    async def test_real_original_pcm_once_q1_after_three_seconds_l2_keeps_audio_position(self):
        early = self.fixture(first_q_after_seconds=3.0)
        part = early.plan["segments"][0]
        out = self.root/"transport"
        c = MotionComponent(NoNetwork(), out, out/"unused.png", long_speech_test=True,
                            long_speech_action_kind="L", long_action_group_count=2)
        c._ws = FakeSocket(write_delay=.005)
        c.live_id = "offline-only"; c._ready.set(); c._joined.set(); c.state = "ready"
        c.DRAIN_SECONDS = 0
        receipt = await c.send_expression(early.pcm, part["text"], part["actions"], 1, recovery=part["recovery"])
        motions = c._ws.motions()
        self.assertEqual(len(motions), 6)
        ge1, l1, q1, ge2, l2, q2 = motions
        texts = [row for row in c._ws.sent if row["frame"]["type"] == 10]
        self.assertEqual(len(texts), 1)
        self.assertEqual(texts[0]["frame"]["payload"]["text_msg"]["content"], part["text"])
        self.assertGreaterEqual(q1["completed"]-l1["completed"], 3.0)
        self.assertLess(q1["completed"]-l1["completed"], 3.2)
        self.assertGreaterEqual(l2["started"]-c._ws.binary[0]["time"], 8.68)
        self.assertLess(l2["completed"]-c._ws.binary[0]["time"], 8.95)
        self.assertGreaterEqual(ge2["started"]-q1["completed"], 5.17)
        self.assertGreaterEqual(l1["started"]-ge1["completed"], .499)
        self.assertGreaterEqual(l2["started"]-ge2["completed"], .499)
        self.assertGreaterEqual(q2["started"]-l2["completed"], 8.67)
        self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), early.pcm)
        self.assertEqual(receipt["pcm_transport_sent_bytes"], len(early.pcm))
        self.assertEqual([row["kind"] for row in c.diagnostics.records if row["kind"].startswith("audio_")],
                         ["audio_start", "audio_end"])
        self.assertEqual([row["frame"]["payload"]["text_msg"]["content"] for row in motions],
                         [node["instruction"] for node in part["timeline"]])
        self.assertEqual(receipt["required_audio_seconds"], 17.36)
        self.assertEqual(receipt["group_hold_seconds"], [3.0, 8.68])
        self.assertTrue(receipt["send_complete"])
        self.assertTrue(receipt["actions_sent_complete"])

    def test_early_q_rejects_invalid_values_and_any_mode_except_two_l(self):
        for value in (0, 1.5, 2.9, 3.1, 4, True, "3", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.fixture(first_q_after_seconds=value)
        for mode in ({"action_kind": "S", "long_action_group_count": 1},
                     {"action_kind": "L", "long_action_group_count": 1}):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.fixture(first_q_after_seconds=3.0, **mode)


if __name__ == "__main__":
    unittest.main()
