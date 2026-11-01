"""Normal A dialogue with the real sender and local clock; no credentials/cloud."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import live_session
from bailian_ai import BailianAI, MODEL, REGION
from motion_component import MotionComponent
from test_motion_component import FakeSocket, NoNetwork, GE
from test_semantic_progress import ManualClock
from test_semantic_transport import action, fixture
from test_studio import studio_server


class ATransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dialogue-a-send-")
        self.addCleanup(self.temp.cleanup)
        self.clock = ManualClock()
        fake_time = SimpleNamespace(monotonic=lambda: self.clock.now, time=time.time)
        self.patches = [patch("motion_component.time", fake_time), patch("component_live.time", fake_time)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.tasks = []
        self.components = []

    async def asyncTearDown(self):
        for component in self.components:
            component.latch_stop()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def component(self):
        out = Path(self.temp.name) / str(len(self.components))
        c = MotionComponent(NoNetwork(), out, out / "unused.png")
        c._ws = FakeSocket()
        c.live_id = "offline-only"
        c.state = "ready"
        c._ready.set()
        c._joined.set()
        c.DRAIN_SECONDS = 0
        c._wait = lambda seconds: self.clock.wait(c, seconds)
        self.components.append(c)
        return c

    def send(self, c, *, seconds=4, start=.08, actions=True, reading=None, number=1):
        pcm, alignment = fixture(seconds, (start,))
        task = asyncio.create_task(c.send_aligned_expression(pcm, "甲", [action()] if actions else [], number,
            recovery=GE if actions or reading else None, alignment=alignment if actions else {"status": "unavailable"},
            reading=reading, action_kind="S", ge_lead_seconds=1.1, allow_tail_q_after_audio=True))
        self.tasks.append(task)
        return pcm, task

    async def test_reply_s_has_ge_1_1_and_early_q_while_audio_finishes_whole(self):
        c = self.component()
        pcm, task = self.send(c)
        await self.clock.advance(3)
        self.assertEqual([e["role"] for e in c.motion_events], ["GE", "S", "Q"])
        self.assertFalse(task.done())
        self.assertFalse(c.audio_sent_complete)
        self.assertLess(c.last_motion_receipt["pcm_transport_sent_bytes"], len(pcm))
        await self.clock.advance(3)
        receipt = await task
        self.assertGreaterEqual(receipt["motion_events"][1]["actual_interval_seconds"], 1.1-1e-6)
        self.assertGreaterEqual(receipt["motion_events"][2]["actual_interval_seconds"], 1.5-1e-6)
        self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), pcm)

    async def test_reading_r_pcm_s0_then_answer_ge_s_q_keeps_both_pcm_streams(self):
        c = self.component()
        reading = {"text": "读这句", "pcm": b"\1\0" * 7200}
        pcm, task = self.send(c, reading=reading)
        await self.clock.advance(8)
        receipt = await task
        self.assertEqual([e["role"] for e in receipt["motion_events"]], ["R", "S0", "GE", "S", "Q"])
        # The answer group resets its local previous-event field; measure
        # the reading S0→answer GE boundary from their actual write clocks.
        self.assertGreaterEqual(receipt["motion_events"][2]["sent_at_monotonic"]-
                                receipt["motion_events"][1]["sent_at_monotonic"], 1-1e-6)
        self.assertGreaterEqual(receipt["motion_events"][3]["actual_interval_seconds"], 1.1-1e-6)
        self.assertGreaterEqual(receipt["motion_events"][4]["actual_interval_seconds"], 1.5-1e-6)
        self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), reading["pcm"]+pcm)
        self.assertEqual([row["frame"]["payload"]["text_msg"]["content"] for row in c._ws.sent if row["frame"]["type"] == 10],
                         ["读这句", "甲"])
        self.assertTrue(receipt["reading_sent_complete"])

    async def test_two_segments_preserve_each_audio_origin_and_post_q_gate(self):
        c = self.component()
        first, task = self.send(c, seconds=2, start=.4)
        await self.clock.advance(3)
        await task
        second, task = self.send(c, seconds=2, start=.4, number=2)
        await self.clock.advance(4)
        receipt = await task
        first_group = c.motion_events[:3]
        second_group = c.motion_events[3:]
        self.assertEqual([e["role"] for e in c.motion_events], ["GE", "S", "Q"] * 2)
        self.assertGreaterEqual(second_group[0]["sent_at_monotonic"]-first_group[-1]["sent_at_monotonic"], 1-1e-6)
        self.assertAlmostEqual(receipt["timeline"][1]["at_seconds"], .4)
        self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), first+second)
        self.assertEqual(len([row for row in c._ws.sent if row["frame"]["type"] == 10]), 2)

    async def test_voice_only_and_reading_only_need_no_alignment_or_extra_ge(self):
        for reading in (None, {"text": "读这句", "pcm": b"\1\0" * 2400}):
            c = self.component()
            pcm, task = self.send(c, seconds=.1, actions=False, reading=reading)
            await self.clock.advance(5)
            receipt = await task
            self.assertEqual([e["role"] for e in receipt["motion_events"]], [] if reading is None else ["R", "S0", "Q"])
            self.assertEqual(receipt["timeline"], [])
            self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary),
                             pcm if reading is None else reading["pcm"]+pcm)

    async def test_stop_during_reading_prevents_s0_answer_and_pending_actions(self):
        c = self.component()
        _, task = self.send(c, reading={"text": "读这句", "pcm": b"\1\0" * 24000})
        await self.clock.advance(1.1)
        self.assertEqual([e["role"] for e in c.motion_events], ["R"])
        c.latch_stop()
        counts = (len(c._ws.sent), len(c._ws.binary))
        await self.clock.advance(10)
        with self.assertRaises(RuntimeError):
            await task
        self.assertEqual((len(c._ws.sent), len(c._ws.binary)), counts)
        self.assertEqual([e["role"] for e in c.motion_events], ["R"])


class ADynamicEndToEndTests(unittest.IsolatedAsyncioTestCase):
    """Real AI plan validation, Studio worker, candidate compiler and sender.

    Only the paid model boundary, TTS provider boundary and renderer socket are
    synthetic; no test calls a credential loader or an external service.
    """
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dialogue-a-dynamic-")
        self.root = Path(self.temp.name)
        self.clock = ManualClock()
        self.model_calls, self.tts_calls, self.candidate_requests = [], [], []
        self.tts_effect = None
        self.body = self.plan()
        self.ai = BailianAI({"model": MODEL, "region": REGION, "enable_thinking": False},
            credentials_loader=Mock(side_effect=AssertionError("no model credential loading")))
        self.ai.configured = Mock(return_value=True)
        self.ai._call = AsyncMock(side_effect=self.model)
        fake_time = SimpleNamespace(monotonic=lambda: self.clock.now, time=time.time)

        async def offline_startup(session, app):
            session.http = NoNetwork()
            component = MotionComponent(NoNetwork(), session.out, session.image, session.diagnostics)
            component._ws = FakeSocket()
            component.live_id = "offline-only"
            component.state = "ready"
            component._ready.set()
            component._joined.set()
            component.DRAIN_SECONDS = 0
            component._wait = lambda seconds: self.clock.wait(component, seconds)
            session.component = component
            session.worker = asyncio.create_task(session.process_answers())

        self.patches = [patch("motion_component.time", fake_time), patch("component_live.time", fake_time),
            patch.object(live_session.LiveSession, "startup", offline_startup),
            patch.object(live_session, "load_bailian", return_value=("synthetic-key", "synthetic-workspace")),
            patch.object(live_session, "load_vidu", side_effect=AssertionError("no Vidu credentials")),
            patch.object(live_session, "synthesize_segment", side_effect=self.tts)]
        for p in self.patches:
            p.start()
        self.studio = studio_server.Studio(self.root, ai=self.ai)
        await self.studio.new_session()
        self.s = self.studio.current
        self.s.started = True
        self.c = self.s.component

    async def asyncTearDown(self):
        self.s.latch_stop()
        self.s.work.put_nowait(None)
        await self.clock.advance(100)
        await asyncio.wait_for(self.s.worker, 2)
        await self.studio.automation.close()
        await self.studio.avatar_library.close()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    @staticmethod
    def plan(*, groups=1, reading=False, segments=1):
        part = {"text": "甲乙丙丁戊己", "instruction": "自然清楚。",
            "actions": [action(char) for char in "甲乙丙丁戊己"[:groups]],
            "recovery": dict(GE) if groups or reading else None}
        return {"emotion": "温和", "observation": "姿态未知",
                "reading": {"text": "弹幕", "instruction": "轻声自然短读。"} if reading else None,
                "segments": [copy.deepcopy(part) for _ in range(segments)]}

    async def model(self, role, messages):
        data = json.loads(messages[1]["content"])
        if role == "selection":
            self.model_calls.append("selection")
            return {"selected_id": data["candidates"][0]["id"], "reason": "正常互动"}
        if "schedules" in data:
            self.model_calls.append("timed_actions")
            self.candidate_requests.append(copy.deepcopy(data))
            self.assertTrue(self.tts_calls)
            self.assertTrue(data["schedules"])
            return {"schedule_id": data["schedules"][0]["schedule_id"]}
        self.model_calls.append("reply")
        return copy.deepcopy(self.body)

    async def tts(self, http, key, workspace, out, text, instruction, **options):
        self.assertEqual((key, workspace), ("synthetic-key", "synthetic-workspace"))
        self.assertIs(http, self.s.http)
        out.mkdir(parents=True)
        seconds = .3 if text == "弹幕" else getattr(self, "answer_seconds", 24)
        pcm = b"\2\0" * round(seconds * 24000)
        path = out / "sample.pcm"
        path.write_bytes(pcm)
        starts = getattr(self, "answer_starts_ms", None) if text != "弹幕" else None
        alignment = {"status": "ok", "timebase": "audio_start",
            "pcm_sha256": hashlib.sha256(pcm).hexdigest(), "words": [
                {"text": char, "begin_time": starts[index] if starts else 80+index*4000,
                 "end_time": starts[index]+100 if starts else 180+index*4000}
                for index, char in enumerate(text)]}
        self.tts_calls.append({"text": text, "pcm": pcm, "alignment": copy.deepcopy(alignment), "options": options})
        if self.tts_effect == "expired":
            self.s.comments[-1]["expires_at"] = time.time()-1
        elif self.tts_effect == "stopped":
            self.s.latch_stop()
        return {"status": "ok", "pcm_path": str(path), "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
                "audio_seconds": seconds, "usage_verified": True, "input_tokens": 1, "output_tokens": 1,
                "cost_cny": .001, **({"alignment": alignment} if options.get("word_timestamps") else {})}

    async def submit(self, *, automatic=False):
        if automatic:
            a = self.studio.automation
            a.enabled = True
            a.source_status = {"state": "connected", "room_id": "123", "error_code": None}
            await a.on_event({"id": "offline-1", "roomId": "123", "platform": "douyin", "type": "chat",
                "ts": time.time()*1000, "receivedAt": time.time()*1000,
                "user": {"id": "offline-viewer", "nickname": "观众"}, "data": {"content": "弹幕"}})
            await a.select_once()
            comment = self.s.comments[-1]
        else:
            comment = self.studio.enqueue_comment("弹幕", "观众", source="manual")
        await self.studio.automation.reply_once(comment)
        # Let the real worker finish model/TTS orchestration before advancing
        # only transport deadlines. asyncio retains its real clock throughout.
        for _ in range(1000):
            if comment.get("delivery_started") or comment["status"] in {"sent", "error", "expired", "cancelled"}:
                break
            await asyncio.sleep(.001)
        else:
            self.fail("dynamic worker never reached delivery or a terminal decision")
        await self.clock.advance(100)
        await asyncio.wait_for(self.s.work.join(), 2)
        return comment

    def assert_a(self, comment, groups):
        self.assertEqual(comment["status"], "sent", comment.get("error"))
        for receipt in comment["motion_receipts"]:
            self.assertEqual((receipt["action_kind"], receipt["ge_lead_seconds"]), ("S", 1.1))
            self.assertEqual([event["role"] for event in receipt["motion_events"] if event["role"] not in {"R", "S0"}],
                             ["GE", "S", "Q"]*groups)
            for event in receipt["motion_events"]:
                if event["role"] == "S":
                    self.assertGreaterEqual(event["actual_interval_seconds"], 1.1-1e-6)
                elif event["role"] == "Q":
                    self.assertGreaterEqual(event["actual_interval_seconds"], 1.5-1e-6)

    async def test_manual_and_selected_viewer_flow_use_same_tts_candidate_and_real_a_sender(self):
        for automatic in (False, True):
            with self.subTest(automatic=automatic):
                before = len(self.c._ws.binary)
                comment = await self.submit(automatic=automatic)
                self.assert_a(comment, 1)
                candidate = self.candidate_requests[-1]
                self.assertEqual(candidate["alignment"]["words"], self.tts_calls[-1]["alignment"]["words"])
                self.assertEqual(b"".join(row["chunk"] for row in self.c._ws.binary[before:]), self.tts_calls[-1]["pcm"])
        self.assertIn("selection", self.model_calls)
        self.ai._credentials_loader.assert_not_called()

    async def test_six_dynamic_groups_keep_every_motion_q_time_and_complete_sentence(self):
        self.body = self.plan(groups=6)
        comment = await self.submit()
        self.assert_a(comment, 6)
        self.assertEqual(len(self.candidate_requests), 1)
        candidates = self.candidate_requests[0]["schedules"]
        self.assertTrue(all(len(schedule["groups"]) == 6 for schedule in candidates))
        part = comment["delivery"][0]
        self.assertEqual(len(part["actions"]), 6)
        self.assertEqual([a["motions"] for a in part["actions"]], [a["motions"] for a in self.body["segments"][0]["actions"]])
        receipt = comment["motion_receipts"][0]
        for index in range(6):
            self.assertAlmostEqual(receipt["timeline"][index*3+2]["at_seconds"]-
                                   receipt["timeline"][index*3+1]["at_seconds"], 1.5)
        self.assertLess(receipt["timeline"][-1]["pcm_required_bytes"], len(self.tts_calls[0]["pcm"]))
        self.assertEqual(b"".join(row["chunk"] for row in self.c._ws.binary), self.tts_calls[0]["pcm"])
        self.assertEqual(len([row for row in self.c._ws.sent if row["frame"]["type"] == 10]), 1)

    async def test_dynamic_three_groups_on_eight_second_pcm_finish_tail_q_after_audio(self):
        self.body = self.plan(groups=3)
        self.answer_seconds = 8
        self.answer_starts_ms = (0, 1000, 3600, 4500, 7200, 7600)
        comment = await self.submit()
        self.assert_a(comment, 3)
        receipt = comment["motion_receipts"][0]
        self.assertEqual([n["at_seconds"] for n in receipt["timeline"] if n["role"] == "S"], [0, 3.6, 7.2])
        self.assertEqual(receipt["timeline"][-1]["at_seconds"], 8.7)
        self.assertEqual(receipt["timeline"][-1]["pcm_required_bytes"], len(self.tts_calls[0]["pcm"]))
        self.assertGreaterEqual(receipt["motion_events"][-1]["actual_at_seconds"], 8.7)
        self.assertEqual(len(self.tts_calls), 1)
        self.assertEqual(b"".join(row["chunk"] for row in self.c._ws.binary), self.tts_calls[0]["pcm"])

    async def test_read_comment_then_two_segments_reach_a_with_full_audio(self):
        self.body = self.plan(reading=True, segments=2)
        comment = await self.submit()
        self.assertEqual(comment["status"], "sent", comment.get("error"))
        self.assertEqual(comment["segments_sent"], 2)
        self.assertEqual([event["role"] for event in comment["motion_receipts"][0]["motion_events"]],
                         ["R", "S0", "GE", "S", "Q"])
        self.assertEqual([event["role"] for event in comment["motion_receipts"][1]["motion_events"]], ["GE", "S", "Q"])
        self.assertEqual([call["text"] for call in self.tts_calls], ["弹幕", "甲乙丙丁戊己", "甲乙丙丁戊己"])
        self.assertEqual(b"".join(row["chunk"] for row in self.c._ws.binary), b"".join(call["pcm"] for call in self.tts_calls))
        self.assertEqual(len(self.candidate_requests), 2)

    async def test_dynamic_voice_only_and_reading_only_skip_candidate_model(self):
        for reading in (False, True):
            with self.subTest(reading=reading):
                self.body = self.plan(groups=0, reading=reading)
                before = len(self.c._ws.binary)
                tts_before = len(self.tts_calls)
                comment = await self.submit()
                self.assertEqual(comment["status"], "sent", comment.get("error"))
                self.assertEqual(self.candidate_requests, [])
                receipt = comment["motion_receipts"][0]
                self.assertEqual(receipt["timeline"], [])
                self.assertEqual([event["role"] for event in receipt["motion_events"]],
                                 ["R", "S0", "Q"] if reading else [])
                self.assertEqual(b"".join(row["chunk"] for row in self.c._ws.binary[before:]),
                                 b"".join(call["pcm"] for call in self.tts_calls[tts_before:]))

    async def test_expiry_or_stop_after_same_tts_never_starts_reading_or_answer(self):
        for reason in ("expired", "stopped"):
            with self.subTest(reason=reason):
                self.tts_effect = reason
                comment = await self.submit()
                self.assertEqual(comment["status"], "expired" if reason == "expired" else "cancelled")
                self.assertFalse(comment.get("delivery_started"))
                self.assertFalse(self.c._ws.sent)
                self.assertFalse(self.c._ws.binary)
                self.assertEqual(len(self.candidate_requests), 0)


if __name__ == "__main__":
    unittest.main()
