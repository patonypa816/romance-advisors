"""双L、中间Q与原连续口播的离线集成验收；不启动服务或收费调用。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import test_fixed_replay as fixture_tests
import test_long_action_integration as long_tests
from action_schema import validate_long_action_replay_plan, validate_long_speech_plan
from fixed_replay import FixedReplay


class TwoLongActionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="two-long-action-integration-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = fixture_tests.FixedReplayTests.source(self)
        self.fixture = FixedReplay(self.source, action_kind="L", long_action_group_count=2)

    # 使用已有离线helper，保留真实new_session与LiveSession.startup的参数接线。
    studio = long_tests.LongActionIntegrationTests.studio
    request = long_tests.LongActionIntegrationTests.request
    enqueue = long_tests.LongActionIntegrationTests.enqueue
    bind_identity = long_tests.LongActionIntegrationTests.bind_identity

    async def test_two_l_preserve_original_bytes_words_order_ge_and_both_recoveries(self):
        old = json.loads((self.source / "plans/c0001.json").read_text())["segments"][0]
        part = self.fixture.plan["segments"][0]
        self.assertEqual(self.fixture.pcm, (self.source / "tts/c0001-1/sample.pcm").read_bytes())
        self.assertEqual(self.fixture.wav, (self.source / "tts/c0001-1/sample.wav").read_bytes())
        for key in ("text", "instruction", "recovery"):
            self.assertEqual(part[key], old[key])
        originals = [action["motions"][0]["description"] for action in old["actions"]]
        self.assertEqual(len(part["actions"]), 2)
        for group, start in zip(part["actions"], (0, 2)):
            self.assertEqual(group["kind"], "L")
            self.assertEqual(group["motions"], [{"part": "whole_body", "description": "，同时".join(originals[start:start+2])}])
            for key in ("gaze", "expression"):
                self.assertEqual(group[key], old["actions"][start][key])
        timeline = part["timeline"]
        self.assertEqual([node["role"] for node in timeline], ["GE", "L", "Q"] * 2)
        for node, expected in zip(timeline, (0, .5, 8.18, 8.68, 9.18, 17.86)):
            self.assertAlmostEqual(node["at_seconds"], expected)
        self.assertAlmostEqual(timeline[3]["delay_seconds"], .5)
        self.assertAlmostEqual(timeline[3]["audio_at_seconds"], 8.18)
        self.assertAlmostEqual(timeline[4]["delay_seconds"], .5)
        for index in (2, 5):
            self.assertEqual(timeline[index]["instruction"], old["timeline"][-1]["instruction"])
        public = self.fixture.public()
        self.assertEqual(public["long_action_group_count"], 2)
        self.assertEqual(public["group_holds_seconds"], [7.68, 8.68])
        self.assertEqual(public["second_l_audio_seconds"], 8.68)
        self.assertEqual(public["q_to_next_l_seconds"], 1)
        self.assertEqual(public["type9_count"], 6)
        self.assertIsNone(public["hold_seconds"])
        self.assertEqual(validate_long_action_replay_plan(self.fixture.plan, group_count=2), self.fixture.plan)

    async def test_original_s_and_single_l_contracts_remain_distinct(self):
        single = FixedReplay(self.source, action_kind="L")
        short = FixedReplay(self.source)
        self.assertEqual(single.long_action_group_count, 1)
        self.assertEqual([node["role"] for node in single.plan["segments"][0]["timeline"]], ["GE", "L", "Q"])
        self.assertEqual(single.plan["segments"][0]["actions"][0]["hold_seconds"], 17.36)
        self.assertEqual(validate_long_action_replay_plan(single.plan), single.plan)
        self.assertEqual(validate_long_speech_plan(short.plan, group_interval=5), short.plan)
        self.assertEqual(len(short.plan["segments"][0]["timeline"]), 12)
        self.assertEqual(single.pcm, self.fixture.pcm)
        self.assertEqual(short.pcm, self.fixture.pcm)
        self.assertEqual(len({single.fingerprint, short.fingerprint, self.fixture.fingerprint}), 3)

    async def test_real_startup_snapshots_and_route_bind_two_l_without_model(self):
        studio, ai = await self.studio()
        session = studio.current
        self.assertEqual(session.long_action_group_count, 2)
        self.assertEqual(session.component.long_action_group_count, 2)
        self.assertEqual(session.component.long_speech_action_kind, "L")
        mode = json.loads((session.out / "source-used/run-mode.json").read_text())
        self.assertEqual(mode["long_action_group_count"], 2)
        self.assertEqual(mode["replay_fixture_fingerprint"], self.fixture.fingerprint)
        variant = json.loads((session.out / "variant.json").read_text())
        self.assertEqual(variant["long_action_group_holds_seconds"], [7.68, 8.68])
        self.assertIsNone(variant["long_action_hold_seconds"])
        manifest = json.loads((session.out / "replay-source/manifest.json").read_text())
        self.assertEqual(manifest["long_action_group_count"], 2)
        self.assertEqual((session.out / "replay-source/sample.pcm").read_bytes(), self.fixture.pcm)
        comment = await self.enqueue(studio)
        self.assertEqual(comment["plan"], self.fixture.plan)
        self.assertEqual(comment["action_group_count"], 2)
        response = await studio.replay(self.request(studio))
        self.assertTrue(json.loads(response.body)["duplicate"])
        self.assertEqual(session.work.qsize(), 1)
        await studio.automation.reply_once()
        ai.generate_reply.assert_not_awaited()
        ai.generate_director.assert_not_awaited()

    async def test_worker_reuses_one_pcm_without_credentials_tts_or_model_and_archives_both_l(self):
        studio, ai = await self.studio()
        comment = await self.enqueue(studio)
        receipt = {"group_id": "offline-two-L-proof", "long_action_group_count": 2,
                   "send_complete": True, "actions_sent_complete": True, "playback_complete_confirmed": False}
        studio.current.work.put_nowait(None)
        with patch("live_session.load_bailian", side_effect=AssertionError("不得读取语音凭据")) as credentials, \
             patch("live_session.synthesize_segment", side_effect=AssertionError("不得调用TTS")) as tts, \
             patch.object(studio.current.component, "send_expression", AsyncMock(return_value=receipt)) as sender:
            await studio.current.process_answers()
        credentials.assert_not_called()
        tts.assert_not_called()
        sender.assert_awaited_once()
        self.assertEqual(sender.await_args.args[0], self.fixture.pcm)
        self.assertEqual(sender.await_args.args[1], self.fixture.plan["segments"][0]["text"])
        self.assertEqual(sender.await_args.args[2], self.fixture.plan["segments"][0]["actions"])
        self.assertEqual(comment["status"], "sent")
        self.assertEqual(comment["segments_sent"], 1)
        self.assertEqual(comment["usage"], {"input_tokens": 0, "output_tokens": 0, "cost_cny": 0})
        self.assertEqual(studio.current.totals["audio_seconds"], 17.36)
        self.assertEqual(studio.current.totals["cost_cny"], 0)
        folder = studio.current.out / "tts" / f'{comment["id"]}-1'
        self.assertEqual((folder / "sample.pcm").read_bytes(), self.fixture.pcm)
        self.assertEqual(json.loads((folder / "result.json").read_text())["status"], "reused")
        self.assertEqual(json.loads((folder / "component-send.json").read_text()), receipt)
        archived = json.loads((studio.current.out / "conversation.json").read_text())
        self.assertEqual(archived["comments"][0]["motion_receipts"], [receipt])
        self.assertEqual(len(archived["comments"][0]["delivery"][0]["actions"]), 2)
        ai.generate_reply.assert_not_awaited()
        ai.generate_director.assert_not_awaited()

    async def test_shorter_than_both_l_windows_rejects_before_component_send(self):
        studio, _ = await self.studio()
        comment = await self.enqueue(studio)
        short_pcm = b"\x01\x00" * (24000 * 8)  # 足够L1的7.68秒，但放不下双L完整17.36秒。
        studio.current.work.put_nowait(None)
        with patch.object(self.fixture, "prepare", return_value=(short_pcm, studio.current.out / "mock-tts")), \
             patch("live_session.load_bailian", side_effect=AssertionError("不得读取凭据")), \
             patch("live_session.synthesize_segment", side_effect=AssertionError("不得调用TTS")), \
             patch.object(studio.current.component, "send_expression", AsyncMock()) as sender:
            await studio.current.process_answers()
        sender.assert_not_awaited()
        self.assertEqual(comment["status"], "error")
        self.assertAlmostEqual(comment["delivery_error"]["minimum_seconds"], 17.36)
        self.assertEqual(comment["delivery_error"]["audio_seconds"], 8)
        self.assertEqual(comment["delivery_error"]["action_group_count"], 2)
        self.assertFalse(comment.get("delivery_started"))
        self.assertFalse(studio.current.component._ws.attempts)

    async def test_stop_after_queue_cancels_both_l_without_preparing_or_sending(self):
        studio, _ = await self.studio()
        comment = await self.enqueue(studio)
        with patch.object(studio.current.component, "stop", AsyncMock(return_value={"state": "ended"})):
            await studio.current.request_stop("离线停止")
        studio.current.work.put_nowait(None)
        with patch.object(self.fixture, "prepare", side_effect=AssertionError("停止后不得复制")) as prepare, \
             patch.object(studio.current.component, "send_expression", AsyncMock()) as sender:
            await studio.current.process_answers()
        self.assertEqual(comment["status"], "cancelled")
        prepare.assert_not_called()
        sender.assert_not_awaited()

    async def test_changed_second_l_rejected_by_accept_and_worker(self):
        studio, _ = await self.studio()
        comment = await self.enqueue(studio)
        changed = self.fixture.plan
        changed["segments"][0]["actions"][1]["motions"][0]["description"] = "改变后的第二组动作"
        with self.assertRaises(ValueError):
            studio.accept_plan(studio.current, {**comment, "status": "processing"}, changed)
        self.assertEqual(studio.current.work.qsize(), 1)
        comment["plan"] = changed
        studio.current.work.put_nowait(None)
        with patch.object(self.fixture, "prepare", side_effect=AssertionError("偏离素材不得复制")) as prepare, \
             patch("live_session.load_bailian", side_effect=AssertionError("不得读取凭据")), \
             patch.object(studio.current.component, "send_expression", AsyncMock()) as sender:
            await studio.current.process_answers()
        self.assertEqual(comment["status"], "error")
        prepare.assert_not_called()
        sender.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
