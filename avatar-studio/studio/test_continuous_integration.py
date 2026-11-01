"""连续口播入口到worker的离线验收；模型、TTS、凭据均替换成合成测试数据。"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web

import live_session
from action_schema import parse_plan
from cloudbase_ai import CONTINUOUS_SPEECH_EXAMPLE
from motion_component import MotionComponent
from test_motion_component import FakeSocket, NoNetwork

spec = importlib.util.spec_from_file_location("continuous_integration_server", Path(__file__).with_name("server.py"))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def plan():
    return copy.deepcopy(CONTINUOUS_SPEECH_EXAMPLE)


class ContinuousIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="continuous-integration-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)

    def studio(self):
        ai = SimpleNamespace(configured=lambda: True, generate_reply=AsyncMock(return_value=plan()),
                             generate_director=AsyncMock(return_value=plan()))
        studio = server.Studio(self.path / "studio", ai=ai, preview=True, continuous_speech_test=True)
        session = live_session.LiveSession(self.path / "run", "A", continuous_speech_test=True)
        c = MotionComponent(NoNetwork(), session.out, session.image, session.diagnostics,
                            continuous_speech_test=True)
        c._ws = FakeSocket()
        c.live_id = "offline-only"
        c.state = "ready"
        c._ready.set()
        c._joined.set()
        c.DRAIN_SECONDS = 0
        session.component, session.http, session.started = c, NoNetwork(), True
        studio.current = session
        return studio, ai

    async def test_process_mode_is_explicit_isolated_and_saved_with_source_evidence(self):
        ai = SimpleNamespace(configured=lambda: True)
        with self.assertRaises(ValueError):
            server.Studio(self.path / "invalid", ai=ai, continuous_speech_test=True)
        with self.assertRaises(ValueError):
            server.Studio(self.path / "invalid", ai=ai, preview=True,
                          short_performance_test=True, continuous_speech_test=True)
        studio, _ = self.studio()
        saved = json.loads((self.path / "studio/local.json").read_text())
        self.assertNotIn("continuous_speech_test", saved)
        normal = server.Studio(self.path / "studio", ai=ai)
        self.assertFalse(normal.continuous_speech_test)
        self.assertFalse(normal.short_performance_test)
        out = studio.current.out
        context = json.loads((out / "brain-context.json").read_text())
        self.assertTrue(context["continuous_speech_test"])
        self.assertFalse(context["short_performance_test"])
        self.assertEqual(context["study_variant"]["action_groups"], 2)
        mode = out / "source-used/run-mode.json"
        self.assertEqual(json.loads(mode.read_text()),
                         {"short_performance_test": False, "continuous_speech_test": True})
        manifest = json.loads((out / "source-manifest.json").read_text())
        self.assertEqual(manifest["run-mode.json"], hashlib.sha256(mode.read_bytes()).hexdigest())

    async def test_only_manual_viewer_and_once_routes_are_accepted(self):
        studio, ai = self.studio()
        for mode in ("topic", "motion"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                studio.director.submit({"mode": mode, "text": "打招呼"})
        with self.assertRaises(web.HTTPConflict):
            studio.enqueue_comment("评论", "观众", source="dyhub")
        with self.assertRaises(ValueError):
            await studio.automation.connect()
        comment = studio.enqueue_comment("陛下谈谈今晚的安排", "观众")
        await studio.automation.reply_once()
        self.assertEqual(comment["status"], "ready")
        ai.generate_reply.assert_awaited_once()
        ai.generate_director.assert_not_awaited()
        self.assertEqual([n["role"] for n in comment["delivery"][0]["timeline"]], ["GE", "S", "Q"] * 2)
        # 下一条主持安排只在前条已离开发送队列后领取。
        comment["status"] = "sent"
        self.assertIs(studio.current.work.get_nowait(), comment)
        studio.current.work.task_done()
        job = studio.director.submit({"mode": "once", "text": "介绍今晚，先挥手再点头"})
        await studio.automation.reply_once()
        ai.generate_director.assert_awaited_once()
        self.assertEqual(next(c for c in studio.current.comments if c["id"] == job["id"])["status"], "ready")
        # 再次穿过accept边界时仍拒绝伪装成模拟弹幕的真实来源。
        blocked = {**comment, "id": "blocked", "status": "processing", "source": "dyhub"}
        with self.assertRaises(ValueError):
            studio.accept_plan(studio.current, blocked, plan())

    async def test_worker_rechecks_full_plan_before_credentials_and_tts(self):
        studio, _ = self.studio()
        invalid = plan()
        invalid["segments"] *= 2
        comment = {"id": "bad", "status": "ready", "input_kind": "viewer",
                   "plan": parse_plan(invalid)}
        studio.current.comments.append(comment)
        studio.current.work.put_nowait(comment)
        studio.current.work.put_nowait(None)
        with patch("live_session.load_bailian", side_effect=AssertionError("禁止读取凭据")) as credentials, \
             patch("live_session.synthesize_segment", side_effect=AssertionError("禁止调用TTS")) as tts:
            await studio.current.process_answers()
        credentials.assert_not_called()
        tts.assert_not_called()
        self.assertEqual(comment["status"], "error")
        self.assertFalse(studio.current.component._ws.attempts)

    async def run_viewer_worker(self, seconds):
        studio, _ = self.studio()
        comment = studio.enqueue_comment("陛下给大家说说今晚的安排", "观众")
        await studio.automation.reply_once()
        pcm = b"\x01\x00" * int(24000 * seconds)

        async def synthesize(http, key, workspace, out, text, instruction):
            out.mkdir(parents=True)
            audio = out / "sample.pcm"
            audio.write_bytes(pcm)
            return {"status": "ok", "pcm_path": str(audio), "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
                    "audio_seconds": seconds, "usage_verified": True,
                    "input_tokens": 10, "output_tokens": 10, "cost_cny": .001,
                    "request_id": "offline-tts"}

        studio.current.work.put_nowait(None)
        with patch("live_session.load_bailian", return_value=("synthetic-only-key", "offline")) as credentials, \
             patch("live_session.synthesize_segment", side_effect=synthesize) as tts:
            await studio.current.process_answers()
        credentials.assert_called_once()
        tts.assert_awaited_once()
        return studio, comment, pcm

    async def test_single_tts_stream_reaches_both_action_groups_and_archive(self):
        studio, comment, pcm = await self.run_viewer_worker(6)
        self.assertEqual(comment["status"], "sent")
        self.assertEqual(comment["segments_sent"], 1)
        socket = studio.current.component._ws
        self.assertEqual(sum(r["frame"]["type"] == 10 for r in socket.sent), 1)
        self.assertEqual(len(socket.motions()), 6)
        self.assertEqual(b"".join(r["chunk"] for r in socket.binary), pcm)
        receipt = json.loads((studio.current.out / "tts" / f'{comment["id"]}-1/component-send.json').read_text())
        self.assertTrue(receipt["continuous_speech_test"])
        self.assertTrue(receipt["send_complete"])
        self.assertTrue(receipt["actions_sent_complete"])
        self.assertFalse(receipt["playback_complete_confirmed"])
        self.assertEqual(len(receipt["groups"]), 2)
        self.assertEqual(studio.current.totals["audio_seconds"], 6)

    async def test_short_audio_records_exact_reason_without_any_delivery_or_retry(self):
        studio, comment, _ = await self.run_viewer_worker(2.5)
        self.assertEqual(comment["status"], "error")
        self.assertIn("2.50 秒", comment["error"])
        self.assertIn("5 秒", comment["error"])
        self.assertEqual(comment["delivery_error"]["code"], "continuous_audio_too_short")
        self.assertFalse(comment.get("delivery_started"))
        self.assertFalse(studio.current.component._ws.attempts)
        self.assertFalse(studio.current.component._ws.binary)
        self.assertEqual(studio.current.totals["audio_seconds"], 2.5)  # 已合成的用量仍保留。
        saved = json.loads((studio.current.out / "conversation.json").read_text())["comments"][0]
        self.assertEqual(saved["delivery_error"], comment["delivery_error"])
        events = [json.loads(line) for line in (studio.current.out / "events.jsonl").read_text().splitlines()]
        self.assertTrue(any(r["event"] == "continuous_audio_rejected" for r in events))
        with patch("live_session.synthesize_segment", side_effect=AssertionError("失败条目不得自动重试")):
            await studio.automation.reply_once()


if __name__ == "__main__":
    unittest.main()
