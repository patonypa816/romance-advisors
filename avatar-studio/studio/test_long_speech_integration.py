"""长文入口、worker与归档的离线接线验收；不调用模型、凭据或云服务。

真实PCM节拍与Type9/10数量由transport测试负责；这里mock发送边界，避免重复长等待。
"""
import copy
import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web

import live_session
from action_schema import parse_long_speech_plan
from cloudbase_ai import LONG_SPEECH_EXAMPLE
from motion_component import MotionComponent
from test_motion_component import FakeSocket, NoNetwork


spec = importlib.util.spec_from_file_location("long_integration_server", Path(__file__).with_name("server.py"))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


def plan(chars=100):
    result = copy.deepcopy(LONG_SPEECH_EXAMPLE)
    text = result["segments"][0]["text"]
    result["segments"][0]["text"] = (text * 3)[:chars]
    assert len(result["segments"][0]["actions"]) == 4
    return result


class LongSpeechIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="long-integration-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)

    def studio(self, label="main", chars=100):
        ai = SimpleNamespace(configured=lambda: True, generate_reply=AsyncMock(return_value=plan(chars)),
                             generate_director=AsyncMock(return_value=plan(chars)))
        studio = server.Studio(self.path / label / "studio", ai=ai, preview=True, long_speech_test=True)
        session = live_session.LiveSession(self.path / label / "run", "A", long_speech_test=True)
        component = MotionComponent(NoNetwork(), session.out, session.image, session.diagnostics,
                                    long_speech_test=True)
        component._ws = FakeSocket()
        component.live_id = "offline-only"
        component.state = "ready"
        component._ready.set()
        component._joined.set()
        session.component, session.http, session.started = component, NoNetwork(), True
        studio.current = session
        return studio, ai

    async def test_flags_are_exclusive_process_only_and_manifest_binds_mode(self):
        ai = SimpleNamespace(configured=lambda: True)
        flags = ("short_performance_test", "continuous_speech_test", "long_speech_test")
        with self.assertRaises(ValueError):
            server.Studio(self.path / "no-preview", ai=ai, long_speech_test=True)
        for index, pair in enumerate(itertools.combinations(flags, 2)):
            values = dict.fromkeys(pair, True)
            with self.subTest(pair=pair):
                with self.assertRaises(ValueError):
                    server.Studio(self.path / f"invalid-{index}", ai=ai, preview=True, **values)
                with self.assertRaises(ValueError):
                    live_session.LiveSession(self.path / f"bad-run-{index}", "A", **values)
                with self.assertRaises(ValueError):
                    MotionComponent(NoNetwork(), self.path, self.path / "unused.png", **values)
        studio, _ = self.studio()
        saved = json.loads((self.path / "main/studio/local.json").read_text())
        for flag in flags:
            self.assertNotIn(flag, saved)
        normal = server.Studio(self.path / "main/studio", ai=ai)
        self.assertTrue(all(getattr(normal, flag) is False for flag in flags))
        out = studio.current.out
        context = json.loads((out / "brain-context.json").read_text())
        self.assertTrue(context["long_speech_test"])
        self.assertFalse(context["short_performance_test"])
        self.assertFalse(context["continuous_speech_test"])
        mode = out / "source-used/run-mode.json"
        self.assertEqual(json.loads(mode.read_text()), {"short_performance_test": False, "long_speech_test": True})
        manifest = json.loads((out / "source-manifest.json").read_text())
        self.assertEqual(manifest["run-mode.json"], hashlib.sha256(mode.read_bytes()).hexdigest())

    async def test_viewer_160_and_once_600_preserve_original_and_queue_100_char_four_groups(self):
        for mode in ("viewer", "once"):
            studio, ai = self.studio(mode)
            limit = 160 if mode == "viewer" else 600
            original = "请根据这段经历完整讲述" + "一" * (limit - len("请根据这段经历完整讲述"))
            if mode == "viewer":
                response = await studio.comments(SimpleNamespace(json=AsyncMock(return_value={"text": original})))
                comment_id = json.loads(response.body)["id"]
                with self.assertRaises(web.HTTPBadRequest):
                    await studio.comments(SimpleNamespace(json=AsyncMock(return_value={"text": original + "一"})))
            else:
                comment_id = studio.director.submit({"mode": "once", "text": original})["id"]
                with self.assertRaises(ValueError):
                    studio.director.submit({"mode": "once", "text": original + "一"})
            await studio.automation.reply_once()
            comment = next(c for c in studio.current.comments if c["id"] == comment_id)
            self.assertEqual(comment["text"], original)
            self.assertEqual(comment["status"], "ready")
            self.assertEqual(len(comment["answer"]), 100)
            self.assertEqual(comment["generated_text_chars"], 100)
            self.assertEqual(comment["action_group_count"], 4)
            self.assertEqual([n["role"] for n in comment["delivery"][0]["timeline"]], ["GE", "S", "Q"] * 4)
            self.assertIs(studio.current.work.get_nowait(), comment)
            studio.current.work.task_done()
            called, unused = ((ai.generate_reply, ai.generate_director) if mode == "viewer"
                              else (ai.generate_director, ai.generate_reply))
            called.assert_awaited_once()
            unused.assert_not_awaited()
            self.assertEqual(called.await_args.args[0]["text"], original)
            self.assertTrue(called.await_args.args[2]["long_speech_test"])
            archived = json.loads((studio.current.out / "plans" / f"{comment_id}.json").read_text())
            self.assertEqual(archived["comment"], original)
            self.assertEqual(len(archived["segments"][0]["text"]), 100)

    async def test_restricted_sources_and_modes_fail_before_model_or_transport(self):
        studio, ai = self.studio()
        for mode in ("topic", "motion"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                studio.director.submit({"mode": mode, "text": "安排动作"})
        with self.assertRaises(web.HTTPConflict):
            studio.enqueue_comment("真实评论", "观众", source="dyhub")
        with self.assertRaises(ValueError):
            await studio.automation.connect()
        comment = studio.enqueue_comment("手工输入", "观众")
        comment.update(status="processing", source="dyhub")
        with self.assertRaises(ValueError):
            studio.accept_plan(studio.current, comment, plan())
        ai.generate_reply.assert_not_awaited()
        ai.generate_director.assert_not_awaited()
        self.assertFalse(studio.current.component._ws.attempts)

    async def test_worker_revalidates_121_chars_multiple_segments_and_source_before_credentials(self):
        for case in ("121-chars", "multiple-segments", "source"):
            studio, _ = self.studio(case)
            candidate = parse_long_speech_plan(plan())
            comment = studio.enqueue_comment("离线验收", "观众")
            comment.update(status="ready", plan=candidate)
            if case == "121-chars":
                candidate["segments"][0]["text"] = "字" * 121
            elif case == "multiple-segments":
                candidate["segments"] *= 2
            else:
                comment["source"] = "dyhub"
            studio.current.work.put_nowait(comment)
            studio.current.work.put_nowait(None)
            with patch("live_session.load_bailian", side_effect=AssertionError("禁止读取凭据")) as credentials, \
                 patch("live_session.synthesize_segment", side_effect=AssertionError("禁止调用TTS")) as tts:
                await studio.current.process_answers()
            with self.subTest(case=case):
                credentials.assert_not_called()
                tts.assert_not_called()
                self.assertEqual(comment["status"], "error")
                self.assertFalse(studio.current.component._ws.attempts)
                self.assertFalse(studio.current.component._ws.binary)

    async def run_worker(self, seconds, *, chars=120):
        studio, _ = self.studio(chars=chars)
        comment = studio.enqueue_comment("根据长描述讲述经历并做四个动作", "观众")
        await studio.automation.reply_once()
        pcm = b"\x01\x00" * int(24000 * seconds)
        receipt = {"group_id": "offline-sender-proof", "mode": "long_speech_test", "long_speech_test": True,
                   "send_complete": True, "actions_sent_complete": True, "playback_complete_confirmed": False,
                   "groups": [{"group_index": n} for n in range(1, 5)]}

        async def synthesize(http, key, workspace, out, text, instruction, *, long_speech_test=False):
            self.assertIs(long_speech_test, True)
            self.assertEqual(text, comment["answer"])
            out.mkdir(parents=True)
            audio = out / "sample.pcm"
            audio.write_bytes(pcm)
            return {"status": "ok", "pcm_path": str(audio), "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
                    "audio_seconds": seconds, "usage_verified": True,
                    "input_tokens": 10, "output_tokens": 20, "cost_cny": .001,
                    "request_id": "offline-tts"}

        studio.current.work.put_nowait(None)
        with patch("live_session.load_bailian", return_value=("synthetic-only-key", "offline")) as credentials, \
             patch("live_session.synthesize_segment", side_effect=synthesize) as tts, \
             patch.object(studio.current.component, "send_expression", AsyncMock(return_value=receipt)) as sender:
            await studio.current.process_answers()
        credentials.assert_called_once()
        tts.assert_awaited_once()
        self.assertEqual(tts.await_args.kwargs, {"long_speech_test": True})
        return studio, comment, pcm, sender, receipt

    async def test_120_char_one_tts_and_pcm_delivery_archive_and_usage(self):
        studio, comment, pcm, sender, receipt = await self.run_worker(12)
        self.assertEqual(comment["status"], "sent")
        self.assertEqual(comment["segments_sent"], 1)
        sender.assert_awaited_once()
        args = sender.await_args.args
        self.assertEqual(args[0], pcm)
        self.assertEqual(args[1], comment["answer"])
        self.assertEqual(len(args[1]), 120)
        self.assertEqual(len(args[2]), 4)
        self.assertEqual(args[3], 1)
        self.assertNotIn("reading", sender.await_args.kwargs)
        self.assertEqual(comment["motion_receipts"], [receipt])
        out = studio.current.out
        saved = json.loads((out / "tts" / f'{comment["id"]}-1/component-send.json').read_text())
        self.assertEqual(saved, receipt)
        archived = json.loads((out / "conversation.json").read_text())
        self.assertEqual(archived["comments"][0]["status"], "sent")
        self.assertEqual(archived["comments"][0]["motion_receipts"], [receipt])
        self.assertEqual(archived["totals"]["audio_seconds"], 12)
        self.assertEqual(comment["usage"], {"input_tokens": 10, "output_tokens": 20, "cost_cny": .001})
        self.assertEqual(archived["totals"]["cost_cny"], .001)

    async def test_eight_seconds_rejects_four_groups_without_deletion_frames_or_retry(self):
        studio, comment, _, sender, _ = await self.run_worker(8, chars=100)
        self.assertEqual(comment["status"], "error")
        self.assertIn("8.00 秒", comment["error"])
        self.assertIn("11 秒", comment["error"])
        self.assertEqual(comment["delivery_error"], {"code": "long_audio_too_short", "audio_seconds": 8,
                                                      "minimum_seconds": 11, "action_group_count": 4})
        self.assertFalse(comment.get("delivery_started"))
        self.assertEqual(len(comment["plan"]["segments"][0]["actions"]), 4)
        sender.assert_not_awaited()
        self.assertFalse(studio.current.component._ws.attempts)
        self.assertFalse(studio.current.component._ws.binary)
        saved = json.loads((studio.current.out / "conversation.json").read_text())
        self.assertEqual(saved["comments"][0]["delivery_error"], comment["delivery_error"])
        self.assertEqual(saved["totals"]["audio_seconds"], 8)
        self.assertEqual(saved["totals"]["cost_cny"], .001)
        events = [json.loads(line) for line in (studio.current.out / "events.jsonl").read_text().splitlines()]
        self.assertTrue(any(row["event"] == "long_audio_rejected" for row in events))
        with patch("live_session.synthesize_segment", side_effect=AssertionError("不得自动重试")):
            await studio.automation.reply_once()


if __name__ == "__main__":
    unittest.main()
