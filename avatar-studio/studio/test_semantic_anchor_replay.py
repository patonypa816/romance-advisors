"""既定7.36秒语义位置参数的离线验收；不调用TTS、凭据或云端。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import test_fixed_replay as fixture_tests
import test_long_action_integration as long_tests
from fixed_replay import FixedReplay
from motion_component import MotionComponent
from test_motion_component import FakeSocket, NoNetwork


class SemanticAnchorReplayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="semantic-anchor-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = fixture_tests.FixedReplayTests.source(self)
        self.fixture = self.make_fixture(second_l_at_seconds=7.36)

    def make_fixture(self, **options):
        return FixedReplay(self.source, **({"action_kind": "L", "long_action_group_count": 2,
                                            "first_q_after_seconds": 3.0} | options))

    # 复用真实startup和worker接线；helper只替换网络/后台任务，不启动服务。
    studio = long_tests.LongActionIntegrationTests.studio
    request = long_tests.LongActionIntegrationTests.request
    enqueue = long_tests.LongActionIntegrationTests.enqueue
    bind_identity = long_tests.LongActionIntegrationTests.bind_identity

    def test_only_second_group_timing_changes_while_words_audio_instructions_and_both_q_positions_stay(self):
        baseline = self.make_fixture()
        anchored = self.fixture
        self.assertEqual(anchored.pcm, baseline.pcm)
        self.assertEqual(anchored.wav, baseline.wav)
        self.assertEqual(anchored.pcm, (self.source/"tts/c0001-1/sample.pcm").read_bytes())
        self.assertEqual(anchored.wav, (self.source/"tts/c0001-1/sample.wav").read_bytes())
        self.assertEqual(anchored.audio_seconds, 17.36)
        self.assertEqual(len(anchored.pcm), 833280)
        self.assertEqual(anchored.pcm_sha256, baseline.pcm_sha256)
        self.assertEqual(anchored.wav_sha256, baseline.wav_sha256)
        self.assertNotEqual(anchored.fingerprint, baseline.fingerprint)
        expected = baseline.plan
        expected_part = expected["segments"][0]
        expected_part["actions"][1].update(delay_seconds=7.36, hold_seconds=10.0)
        timeline = expected_part["timeline"]
        timeline[3].update(at_seconds=7.36, delay_seconds=7.36-3.5, audio_at_seconds=6.86)
        timeline[4].update(at_seconds=7.86, hold_seconds=10.0)
        timeline[5]["delay_seconds"] = 10.0
        actual = anchored.plan
        # 整份计划比较，避免意外更动GE、Q、动作描述、台词或其他字段。
        self.assertEqual(actual, expected)
        part = actual["segments"][0]
        self.assertEqual([n["at_seconds"] for n in part["timeline"]], [0, .5, 3.5, 7.36, 7.86, 17.86])
        self.assertAlmostEqual(part["timeline"][3]["delay_seconds"], 3.86)
        self.assertEqual([n["instruction"] for n in part["timeline"]],
                         [n["instruction"] for n in baseline.plan["segments"][0]["timeline"]])
        self.assertEqual(part["text"], baseline.plan["segments"][0]["text"])
        public = anchored.public()
        self.assertEqual(public["second_l_audio_seconds"], 7.36)
        self.assertEqual(public["second_l_anchor"], "百废待兴")
        self.assertEqual(public["last_q_audio_seconds"], 17.36)
        self.assertEqual(public["group_holds_seconds"], [3, 10])

    async def test_real_seventeen_point_three_six_second_pcm_once_and_full_ten_second_l2_window(self):
        fixture = self.fixture
        part = fixture.plan["segments"][0]
        out = self.root/"transport"
        c = MotionComponent(NoNetwork(), out, out/"unused.png", long_speech_test=True,
                            long_speech_action_kind="L", long_action_group_count=2)
        c._ws = FakeSocket(write_delay=.005)
        c.live_id = "offline-only"; c._ready.set(); c._joined.set(); c.state = "ready"
        c.DRAIN_SECONDS = 0
        receipt = await c.send_expression(fixture.pcm, part["text"], part["actions"], 1, recovery=part["recovery"])
        rows = c._ws.motions()
        self.assertEqual(len(rows), 6)
        ge1, l1, q1, ge2, l2, q2 = rows
        texts = [r for r in c._ws.sent if r["frame"]["type"] == 10]
        self.assertEqual(len(texts), 1)
        self.assertEqual(texts[0]["frame"]["payload"]["text_msg"]["content"], part["text"])
        first_pcm = c._ws.binary[0]["time"]
        self.assertGreaterEqual(q1["started"]-l1["completed"], 3)
        self.assertLess(q1["completed"]-l1["completed"], 3.2)
        self.assertGreaterEqual(ge2["started"]-first_pcm, 6.86)
        self.assertLess(ge2["completed"]-first_pcm, 7.11)
        self.assertGreaterEqual(l2["started"]-first_pcm, 7.36)
        self.assertLess(l2["completed"]-first_pcm, 7.61)
        self.assertGreaterEqual(l1["started"]-ge1["completed"], .5)
        self.assertGreaterEqual(l2["started"]-ge2["completed"], .5)
        self.assertGreaterEqual(q2["started"]-l2["completed"], 10)
        self.assertLess(q2["completed"]-l2["completed"], 10.2)
        self.assertEqual(b"".join(r["chunk"] for r in c._ws.binary), fixture.pcm)
        self.assertGreater(c._ws.binary[-1]["time"]-first_pcm, 17.2)
        self.assertEqual(receipt["pcm_transport_sent_bytes"], len(fixture.pcm))
        self.assertEqual([r["kind"] for r in c.diagnostics.records if r["kind"].startswith("audio_")],
                         ["audio_start", "audio_end"])
        self.assertEqual([r["frame"]["payload"]["text_msg"]["content"] for r in rows],
                         [n["instruction"] for n in part["timeline"]])
        self.assertEqual(receipt["required_audio_seconds"], 17.36)
        self.assertEqual(receipt["group_hold_seconds"], [3, 10])
        self.assertTrue(receipt["actions_sent_complete"])

    async def test_startup_snapshots_and_worker_reuse_original_audio_without_tts_credentials_or_model(self):
        studio, ai = await self.studio()
        session = studio.current
        self.assertEqual(session.component.long_action_group_count, 2)
        self.assertEqual(session.component.long_speech_action_kind, "L")
        manifest = json.loads((session.out/"replay-source/manifest.json").read_text())
        self.assertEqual(manifest["second_l_audio_seconds"], 7.36)
        self.assertEqual(manifest["second_l_anchor"], "百废待兴")
        self.assertEqual(manifest["last_q_audio_seconds"], 17.36)
        self.assertEqual((session.out/"replay-source/sample.pcm").read_bytes(), self.fixture.pcm)
        self.assertEqual((session.out/"replay-source/sample.wav").read_bytes(), self.fixture.wav)
        self.assertEqual(json.loads((session.out/"replay-source/replay-plan.json").read_text()), self.fixture.plan)
        variant = json.loads((session.out/"variant.json").read_text())
        self.assertEqual(variant["second_l_audio_seconds"], 7.36)
        self.assertEqual(variant["second_l_semantic_anchor"], "百废待兴")
        self.assertEqual(variant["last_q_audio_seconds"], 17.36)
        comment = await self.enqueue(studio)
        session.work.put_nowait(None)
        receipt = {"send_complete": True, "actions_sent_complete": True, "playback_complete_confirmed": False}
        with patch("live_session.load_bailian", side_effect=AssertionError("不得读取凭据")) as credentials, \
             patch("live_session.synthesize_segment", side_effect=AssertionError("不得调用TTS")) as tts, \
             patch.object(session.component, "send_expression", AsyncMock(return_value=receipt)) as sender:
            await session.process_answers()
        credentials.assert_not_called()
        tts.assert_not_called()
        sender.assert_awaited_once()
        self.assertEqual(sender.await_args.args[0], self.fixture.pcm)
        self.assertEqual(sender.await_args.args[1], self.fixture.plan["segments"][0]["text"])
        self.assertEqual(sender.await_args.args[2], self.fixture.plan["segments"][0]["actions"])
        self.assertEqual(comment["status"], "sent")
        folder = session.out/"tts"/f'{comment["id"]}-1'
        self.assertEqual((folder/"sample.pcm").read_bytes(), self.fixture.pcm)
        self.assertEqual((folder/"sample.wav").read_bytes(), self.fixture.wav)
        ai.generate_reply.assert_not_awaited()
        ai.generate_director.assert_not_awaited()

    def test_invalid_anchor_or_modes_are_rejected(self):
        for value in (0, 6.86, 7.35, 7.37, 8.68, True, "7.36", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.make_fixture(second_l_at_seconds=value)
        for changes in ({"action_kind": "S", "long_action_group_count": 1},
                        {"action_kind": "L", "long_action_group_count": 1},
                        {"first_q_after_seconds": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.make_fixture(second_l_at_seconds=7.36, **changes)


if __name__ == "__main__":
    unittest.main()
