"""固定原音频、四动作合为单L的离线接线验收；不启动服务或云端。"""
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import live_session
import test_fixed_replay as fixture_tests
import test_replay_integration as replay_tests
from action_schema import validate_long_action_replay_plan, validate_long_speech_plan
from fixed_replay import FixedReplay
from test_motion_component import FakeSocket, NoNetwork


server = replay_tests.server


class LongActionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="long-action-integration-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = fixture_tests.FixedReplayTests.source(self)
        self.fixture = FixedReplay(self.source, action_kind="L")

    request = replay_tests.ReplayIntegrationTests.request
    enqueue = replay_tests.ReplayIntegrationTests.enqueue
    bind_identity = replay_tests.ReplayIntegrationTests.bind_identity

    async def studio(self):
        ai = SimpleNamespace(configured=lambda: False, generate_reply=AsyncMock(), generate_director=AsyncMock(),
                             select_comments=AsyncMock())
        studio = server.Studio(self.root / "studio", ai=ai, preview=True,
                               long_speech_test=True, replay_fixture=self.fixture)

        def no_worker(coroutine):
            # 验真实LiveSession.startup的模式参数，但不启动后台worker或真实HTTP会话。
            coroutine.close()
            return SimpleNamespace()

        with patch("live_session.aiohttp.ClientSession", return_value=NoNetwork()), \
             patch("live_session.asyncio.create_task", side_effect=no_worker):
            await studio.new_session()
        self.bind_identity(studio)
        component = studio.current.component
        component._ws = FakeSocket()
        component.live_id = "offline-only"
        component.state = "ready"
        component._ready.set()
        component._joined.set()
        studio.current.started = True
        return studio, ai

    async def test_single_l_keeps_original_audio_words_order_ge_and_recovery(self):
        old = json.loads((self.source / "plans/c0001.json").read_text())["segments"][0]
        part = self.fixture.plan["segments"][0]
        self.assertEqual(part["text"], old["text"])
        self.assertEqual(part["instruction"], old["instruction"])
        self.assertEqual(part["recovery"], old["recovery"])
        self.assertEqual(self.fixture.pcm, (self.source / "tts/c0001-1/sample.pcm").read_bytes())
        self.assertEqual(self.fixture.wav, (self.source / "tts/c0001-1/sample.wav").read_bytes())
        self.assertEqual(len(part["actions"]), 1)
        action = part["actions"][0]
        originals = [node["motions"][0]["description"] for node in old["actions"]]
        expected = "，".join(prefix + text for prefix, text in zip(("先", "然后", "随后", "最后"), originals))
        self.assertEqual(action["motions"], [{"part": "whole_body", "description": expected}])
        self.assertEqual(action["kind"], "L")
        self.assertEqual(action["delay_seconds"], 0)
        self.assertEqual(action["hold_seconds"], 17.36)
        for key in ("gaze", "expression"):
            self.assertEqual(action[key], old["actions"][0][key])
        timeline = part["timeline"]
        self.assertEqual([node["role"] for node in timeline], ["GE", "L", "Q"])
        self.assertEqual(timeline[0]["instruction"], old["timeline"][0]["instruction"])
        self.assertEqual(timeline[-1]["instruction"], old["timeline"][-1]["instruction"])
        self.assertEqual([node["delay_seconds"] for node in timeline], [0, .5, 17.36])
        public = self.fixture.public()
        self.assertEqual(public["original_actions"], originals)
        self.assertEqual(public["action_kind"], "L")
        self.assertIsNone(public["group_interval"])
        self.assertEqual(public["type9_count"], 3)
        self.assertEqual(validate_long_action_replay_plan(self.fixture.plan), self.fixture.plan)

    async def test_existing_s5_fixture_remains_distinct_and_valid(self):
        old_mode = FixedReplay(self.source)
        part = old_mode.plan["segments"][0]
        self.assertEqual(old_mode.action_kind, "S")
        self.assertEqual(old_mode.group_interval, 5)
        self.assertEqual([node["delay_seconds"] for node in part["actions"]], [0, 5, 5, 5])
        self.assertEqual([node["role"] for node in part["timeline"]], ["GE", "S", "Q"] * 4)
        self.assertEqual(validate_long_speech_plan(old_mode.plan, group_interval=5), old_mode.plan)
        self.assertEqual(old_mode.pcm, self.fixture.pcm)
        self.assertNotEqual(old_mode.fingerprint, self.fixture.fingerprint)
        self.assertEqual(old_mode.public()["type9_count"], 12)

    async def test_source_snapshot_and_real_startup_propagate_l_mode(self):
        studio, _ = await self.studio()
        session = studio.current
        self.assertEqual(session.long_speech_action_kind, "L")
        self.assertEqual(session.component.long_speech_action_kind, "L")
        self.assertEqual(session.component.public()["mode"], "long_action_replay")
        snapshot = session.out / "replay-source"
        manifest = json.loads((snapshot / "manifest.json").read_text())
        self.assertEqual(manifest["action_kind"], "L")
        self.assertEqual(manifest["fingerprint"], self.fixture.fingerprint)
        self.assertEqual((snapshot / "sample.pcm").read_bytes(), self.fixture.pcm)
        mode = json.loads((session.out / "source-used/run-mode.json").read_text())
        self.assertTrue(mode["fixed_replay"])
        self.assertEqual(mode["long_speech_action_kind"], "L")
        self.assertEqual(mode["replay_fixture_fingerprint"], self.fixture.fingerprint)
        variant = json.loads((session.out / "variant.json").read_text())
        self.assertIsNone(variant["planned_group_interval_seconds"])
        self.assertEqual(variant["long_action_hold_seconds"], 17.36)

    async def test_replay_route_queues_single_l_without_using_model(self):
        studio, ai = await self.studio()
        comment = await self.enqueue(studio)
        self.assertEqual(comment["status"], "ready")
        self.assertEqual(comment["plan"], self.fixture.plan)
        self.assertEqual(comment["action_group_count"], 1)
        self.assertEqual(comment["delivery"][0]["actions"][0]["kind"], "L")
        self.assertEqual(comment["replay_source_comment"], "c0001")
        response = await studio.replay(self.request(studio))
        self.assertTrue(json.loads(response.body)["duplicate"])
        self.assertEqual(studio.current.work.qsize(), 1)
        await studio.automation.reply_once()
        ai.generate_reply.assert_not_awaited()
        ai.generate_director.assert_not_awaited()

    async def test_worker_delivers_original_pcm_once_without_llm_tts_or_credentials_and_archives(self):
        studio, ai = await self.studio()
        comment = await self.enqueue(studio)
        receipt = {"group_id": "offline-long-L-send", "mode": "long_action_replay",
                   "long_speech_action_kind": "L", "send_complete": True,
                   "actions_sent_complete": True, "playback_complete_confirmed": False}
        studio.current.work.put_nowait(None)
        with patch("live_session.load_bailian", side_effect=AssertionError("不得读取凭据")) as credentials, \
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
        result = json.loads((folder / "result.json").read_text())
        self.assertEqual(result["status"], "reused")
        self.assertEqual(result["pcm_sha256"], hashlib.sha256(self.fixture.pcm).hexdigest())
        self.assertEqual(json.loads((folder / "component-send.json").read_text()), receipt)
        archived = json.loads((studio.current.out / "conversation.json").read_text())
        self.assertEqual(archived["comments"][0]["motion_receipts"], [receipt])
        self.assertEqual(archived["comments"][0]["delivery"][0]["actions"][0]["kind"], "L")
        ai.generate_reply.assert_not_awaited()
        ai.generate_director.assert_not_awaited()

    async def test_stop_after_queue_prevents_prepare_and_send(self):
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
        self.assertFalse(studio.current.component._ws.attempts)

    async def test_changed_l_content_fails_at_accept_and_worker_boundaries(self):
        studio, _ = await self.studio()
        comment = await self.enqueue(studio)
        changed = self.fixture.plan
        changed["segments"][0]["actions"][0]["motions"][0]["description"] = "不同的长动作"
        rejected = {**comment, "status": "processing"}
        with self.assertRaises(ValueError):
            studio.accept_plan(studio.current, rejected, changed)
        self.assertEqual(studio.current.work.qsize(), 1)
        comment["plan"] = changed
        studio.current.work.put_nowait(None)
        with patch("live_session.load_bailian", side_effect=AssertionError("不得读取凭据")), \
             patch("live_session.synthesize_segment", side_effect=AssertionError("不得调用TTS")), \
             patch.object(self.fixture, "prepare", side_effect=AssertionError("偏离素材不得复制")) as prepare, \
             patch.object(studio.current.component, "send_expression", AsyncMock()) as sender:
            await studio.current.process_answers()
        self.assertEqual(comment["status"], "error")
        prepare.assert_not_called()
        sender.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
