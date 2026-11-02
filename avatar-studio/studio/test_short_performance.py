"""显式短表演模式的纯离线测试：假socket，无服务、真实凭据或云调用。"""
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web

import cloudbase_ai
from action_schema import parse_director_plan
from live_session import LiveSession
from motion_component import MotionComponent
from test_motion_component import FakeSocket, NoNetwork, short, long, GE, eventually


def plan():
    return {"emotion": "温和", "observation": "当前姿态未知", "reading": None,
            "segments": [{"text": "咱先说一句。", "instruction": "自然清楚", "actions": [short()], "recovery": GE}]}


def motion_plan():
    return {"emotion": "温和", "observation": "当前姿态未知", "actions": [short()], "recovery": GE}


class ShortPerformanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="short-performance-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.count = 0

    def component(self, *, enabled=True, write_delay=0, fail_motion=None):
        self.count += 1
        out = Path(self.tmp.name) / str(self.count)
        c = MotionComponent(NoNetwork(), out, out / "unused.png", short_performance_test=enabled)
        c._ws = FakeSocket(write_delay=write_delay, fail_motion=fail_motion)
        c.live_id = "offline-only"
        c._ready.set()
        c._joined.set()
        c.state = "ready"
        c.DRAIN_SECONDS = 0
        return c

    async def test_successful_writes_anchor_both_gaps_and_audio_runs_through_q(self):
        c = self.component(write_delay=0.06)
        self.assertTrue(c.public()["short_performance_test"])
        self.assertEqual(c.public()["motion_rules"]["max_simultaneous_short_actions"], 1)
        self.assertEqual(c.public()["motion_rules"]["short_performance_lead_seconds"], 0.5)
        # 真实PCM节拍超过Q窗口，可证明Q发送时音频仍在流动。
        pcm = b"\0\0" * (c.RATE * 2)
        receipt = await c.send_expression(pcm, "咱先说一句。", [short()], 1, recovery=GE)
        ge, s, q = c._ws.motions()
        self.assertGreaterEqual(s["started"] - ge["completed"], 0.49)
        self.assertLess(s["started"] - ge["completed"], 0.65)
        self.assertGreaterEqual(q["started"] - s["completed"], 1.49)
        self.assertLess(q["started"] - s["completed"], 1.65)
        text = [row for row in c._ws.sent if row["frame"]["type"] == 10]
        self.assertEqual(len(text), 1)
        self.assertGreaterEqual(text[0]["started"], s["completed"])
        self.assertLess(text[0]["started"] - s["completed"], 0.08)
        self.assertTrue(c._ws.binary)
        self.assertGreaterEqual(c._ws.binary[0]["time"], s["completed"])
        self.assertLess(c._ws.binary[0]["time"], q["started"])
        self.assertGreater(c._ws.binary[-1]["time"], q["completed"])
        self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), pcm)
        self.assertEqual([row["role"] for row in receipt["timeline"]], ["GE", "S", "Q"])
        self.assertEqual([row["at_seconds"] for row in receipt["timeline"]], [0, 0.5, 2.0])
        content = ge["frame"]["payload"]["text_msg"]["content"]
        self.assertEqual(content.splitlines(), ["眼神：自然看向镜头。", "表情：神情温和。"])
        self.assertTrue(receipt["actions_sent_complete"])

    async def test_stop_in_lead_window_cancels_s_text_pcm_and_q(self):
        c = self.component()
        task = asyncio.create_task(c.send_expression(b"\0\0" * c.RATE, "一句话", [short()], 1, recovery=GE))
        await eventually(lambda: len(c._ws.motions()) == 1)
        await asyncio.sleep(0.1)
        self.assertFalse(c._ws.binary)
        self.assertEqual([row["frame"]["type"] for row in c._ws.sent], [9])
        c.latch_stop()
        with self.assertRaises(RuntimeError):
            await task
        await asyncio.sleep(0.55)
        self.assertEqual([row["frame"]["type"] for row in c._ws.sent], [9])
        self.assertFalse(c._ws.binary)

    async def test_failed_s_never_releases_speech_or_q(self):
        c = self.component(fail_motion=2)
        with self.assertRaises(ConnectionError):
            await c.send_expression(b"\0\0", "一句话", [short()], 1, recovery=GE)
        self.assertEqual([row["frame"]["type"] for row in c._ws.sent], [9])
        self.assertFalse(c._ws.binary)

    async def test_transport_rejects_out_of_scope_before_any_write(self):
        for actions in ([long()], [short(), short(1)], [short(pair=True)], []):
            with self.subTest(actions=actions):
                c = self.component()
                with self.assertRaises(ValueError):
                    await c.send_expression(b"\0\0", "一句话", actions, 1, recovery=GE)
                self.assertFalse(c._ws.attempts)
                self.assertFalse(c._ws.binary)

    async def test_normal_mode_still_sends_s_and_speech_immediately(self):
        c = self.component(enabled=False)
        c._audio = AsyncMock()
        task = asyncio.create_task(c.send_expression(b"\0\0", "一句话", [short(pair=True)], 1, recovery=GE))
        await eventually(lambda: any(row["frame"]["type"] == 10 for row in c._ws.sent))
        s = c._ws.motions()[0]
        text = next(row for row in c._ws.sent if row["frame"]["type"] == 10)
        self.assertLess(text["started"] - s["completed"], 0.08)
        self.assertIn("动作：", s["frame"]["payload"]["text_msg"]["content"])
        receipt = await task
        self.assertEqual([row["role"] for row in receipt["timeline"]], ["S", "Q"])
        self.assertFalse(receipt["short_performance_test"])

    async def test_model_prompt_and_full_plan_validation(self):
        ai = cloudbase_ai.CloudBaseAI({})
        ai._call = AsyncMock(return_value=plan())
        job = {"input_kind": "operator", "operator_mode": "once", "id": "test", "nickname": "主持", "text": "说一句并摊手"}
        result = await ai.generate_director(job, [], {"short_performance_test": True})
        self.assertEqual(result, plan())
        self.assertIn(cloudbase_ai.SHORT_PERFORMANCE_PROMPT, ai._call.call_args.args[1][0]["content"])
        invalids = []
        value = plan(); value["segments"] *= 2; invalids.append(value)
        value = plan(); value["segments"][0]["actions"] = [long()]; invalids.append(value)
        value = plan(); value["segments"][0]["actions"] += [short(1)]; invalids.append(value)
        value = plan(); value["segments"][0]["actions"] = [short(pair=True)]; invalids.append(value)
        value = plan(); value["reading"] = {"text": "说一句", "instruction": "自然"}; invalids.append(value)
        for value in invalids:
            ai._call = AsyncMock(return_value=value)
            with self.assertRaises(cloudbase_ai.ModelError):
                await ai.generate_director(job, [], {"short_performance_test": True})
            ai._call.assert_awaited_once()  # 无自动重试
        ai._call.reset_mock()
        with self.assertRaises(cloudbase_ai.ModelError):
            await ai.generate_director({**job, "operator_mode": "topic"}, [], {"short_performance_test": True})
        ai._call.assert_not_awaited()
        ai._call = AsyncMock(return_value=motion_plan())
        self.assertEqual(await ai.generate_director({**job, "operator_mode": "motion"}, [],
                                                    {"short_performance_test": True}), motion_plan())
        self.assertIn(cloudbase_ai.SHORT_PERFORMANCE_MOTION_PROMPT, ai._call.call_args.args[1][0]["content"])

    async def test_viewer_model_keeps_comment_input_and_omits_conflicting_example(self):
        ai = cloudbase_ai.CloudBaseAI({})
        ai._call = AsyncMock(return_value=plan())
        comment = {"id": "viewer", "text": "你好", "nickname": "观众"}
        context = {"short_performance_test": True, "plan_contract": {"example": "CONFLICTING_MULTI_SEGMENT_EXAMPLE"}}
        self.assertEqual(await ai.generate_reply(comment, [], context), plan())
        messages = ai._call.call_args.args[1]
        self.assertIn(cloudbase_ai.SHORT_PERFORMANCE_PROMPT, messages[0]["content"])
        self.assertNotIn("CONFLICTING_MULTI_SEGMENT_EXAMPLE", messages[0]["content"])
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["comment"]["text"], comment["text"])
        self.assertNotIn("operator_job", payload)
        bad = plan(); bad["segments"] *= 2
        ai._call = AsyncMock(return_value=bad)
        with self.assertRaises(cloudbase_ai.ModelError):
            await ai.generate_reply(comment, [], context)
        ai._call.assert_awaited_once()

    async def test_process_flag_is_explicit_not_persisted_and_source_manifest_captures_it(self):
        spec = importlib.util.spec_from_file_location("short_test_server", Path(__file__).with_name("server.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        path = Path(self.tmp.name) / "studio"
        with self.assertRaises(ValueError):
            module.Studio(path, short_performance_test=True)
        fake_ai = cloudbase_ai.CloudBaseAI({})
        first = module.Studio(path, ai=fake_ai, preview=True, short_performance_test=True)
        self.assertNotIn("short_performance_test", json.loads((path / "local.json").read_text()))
        second = module.Studio(path, ai=fake_ai)
        self.assertFalse(second.short_performance_test)
        for body in ({"mode": "topic", "text": "讲故事"},):
            with self.assertRaises(ValueError):
                first.director.submit(body)
        with self.assertRaises(web.HTTPConflict):
            first.enqueue_comment("评论", "观众", source="dyhub")
        with self.assertRaises(ValueError):
            await first.automation.connect()
        session = LiveSession(Path(self.tmp.name) / "run", "A", short_performance_test=True)
        context = json.loads((session.out / "brain-context.json").read_text())
        self.assertTrue(context["short_performance_test"])
        self.assertEqual(context["study_variant"]["max_simultaneous_short_actions"], 1)
        mode = session.out / "source-used" / "run-mode.json"
        self.assertEqual(json.loads(mode.read_text()), {"short_performance_test": True})
        manifest = json.loads((session.out / "source-manifest.json").read_text())
        self.assertEqual(manifest["run-mode.json"], hashlib.sha256(mode.read_bytes()).hexdigest())

    async def test_worker_rejects_full_plan_before_credentials_or_tts(self):
        session = LiveSession(Path(self.tmp.name) / "run", "A", short_performance_test=True)
        session.component = self.component()
        bad = plan()
        bad["segments"] *= 2
        comment = {"id": "offline-job", "input_kind": "operator", "operator_mode": "once",
                   "status": "ready", "plan": parse_director_plan(bad, mode="once")}
        session.comments.append(comment)
        session.work.put_nowait(comment)
        session.work.put_nowait(None)
        with patch("live_session.load_bailian", side_effect=AssertionError("禁止读取真实凭据")) as credentials, \
             patch("live_session.synthesize_segment", side_effect=AssertionError("禁止调用TTS")) as tts:
            await session.process_answers()
        self.assertEqual(comment["status"], "error")
        self.assertFalse(session.component._ws.attempts)
        credentials.assert_not_called()
        tts.assert_not_called()

    def studio_without_services(self):
        spec = importlib.util.spec_from_file_location("short_test_server", Path(__file__).with_name("server.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ai = SimpleNamespace(configured=lambda: True, generate_reply=AsyncMock(return_value=plan()),
                             generate_director=AsyncMock(return_value=motion_plan()))
        studio = module.Studio(Path(self.tmp.name) / "studio", ai=ai, preview=True, short_performance_test=True)
        studio.current = LiveSession(Path(self.tmp.name) / "run", "A", short_performance_test=True)
        studio.current.component = self.component(write_delay=0.04)
        studio.current.started = True
        return studio, ai

    async def test_simulated_comment_routes_through_reply_to_test_timeline(self):
        studio, ai = self.studio_without_services()
        comment = studio.enqueue_comment("你好", "观众")
        await studio.automation.reply_once()
        ai.generate_reply.assert_awaited_once()
        ai.generate_director.assert_not_awaited()
        self.assertEqual(comment["input_kind"], "viewer")
        self.assertEqual(comment["status"], "ready")
        self.assertEqual([node["role"] for node in comment["delivery"][0]["timeline"]], ["GE", "S", "Q"])
        # 即使绕过入队直接到达accept边界，真实来源也不能被当作模拟弹幕。
        blocked = {**comment, "id": "blocked", "status": "processing", "source": "dyhub"}
        with self.assertRaises(ValueError):
            studio.accept_plan(studio.current, blocked, plan())

    async def test_motion_queue_sends_only_three_type9_frames_without_credentials_or_tts(self):
        studio, ai = self.studio_without_services()
        result = studio.director.submit({"mode": "motion", "text": "摊手"})
        comment = next(c for c in studio.current.comments if c["id"] == result["id"])
        await studio.automation.reply_once()
        ai.generate_director.assert_awaited_once()
        ai.generate_reply.assert_not_awaited()
        self.assertEqual([node["role"] for node in comment["plan"]["timeline"]], ["GE", "S", "Q"])
        studio.current.work.put_nowait(None)
        with patch("live_session.load_bailian", side_effect=AssertionError("禁止读取凭据")) as credentials, \
             patch("live_session.synthesize_segment", side_effect=AssertionError("禁止调用TTS")) as tts:
            await studio.current.process_answers()
        self.assertEqual(comment["status"], "sent")
        c = studio.current.component
        self.assertEqual([row["frame"]["type"] for row in c._ws.sent], [9, 9, 9])
        ge, s, q = c._ws.motions()
        self.assertGreaterEqual(s["started"] - ge["completed"], 0.49)
        self.assertLess(s["started"] - ge["completed"], 0.65)
        self.assertGreaterEqual(q["started"] - s["completed"], 1.49)
        self.assertLess(q["started"] - s["completed"], 1.65)
        self.assertIn("动作：正常坐姿。", q["frame"]["payload"]["text_msg"]["content"])
        self.assertFalse(c._ws.binary)
        self.assertEqual(c.utterances_started, 0)
        credentials.assert_not_called()
        tts.assert_not_called()

    async def test_motion_stop_and_failed_s_cancel_remaining_frames(self):
        c = self.component()
        task = asyncio.create_task(c.send_motion_expression([short()], recovery=GE))
        await eventually(lambda: len(c._ws.motions()) == 1)
        c.latch_stop()
        with self.assertRaises(RuntimeError):
            await task
        await asyncio.sleep(0.55)
        self.assertEqual([row["frame"]["type"] for row in c._ws.sent], [9])
        self.assertFalse(c._ws.binary)
        c = self.component(fail_motion=2)
        with self.assertRaises(ConnectionError):
            await c.send_motion_expression([short()], recovery=GE)
        self.assertEqual([row["frame"]["type"] for row in c._ws.sent], [9])
        self.assertFalse(c._ws.binary)


if __name__ == "__main__":
    unittest.main()
