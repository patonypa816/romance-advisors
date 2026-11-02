"""导演执行与发送前撤销合同；真实本地调度器+假连接，不读取云凭据。"""
import asyncio
import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import live_session
from action_schema import ACTION_POST_SEND_SECONDS, compile_timeline
from diagnostics import Diagnostics
from motion_component import MotionComponent

GE = {"gaze": "自然看向镜头", "expression": "神情温和"}
TIMING_SCALE = 0.02


def short(delay=0):
    return {"kind": "S", "delay_seconds": delay,
            "motions": [{"part": "head", "description": "头部轻轻右倾"}],
            "hold_seconds": 0, **GE}


def long():
    return {"kind": "L", "delay_seconds": 0,
            "motions": [{"part": "whole_body", "description": "从椅子上站起"}],
            "hold_seconds": 2, **GE}


def compressed_timeline(actions, recovery=None):
    timeline = compile_timeline(actions, recovery)
    for node in timeline:
        for field in ("delay_seconds", "at_seconds", "hold_seconds"):
            node[field] *= TIMING_SCALE
    return timeline


class NoNetwork:
    def __getattr__(self, name):
        raise AssertionError("离线合同禁止访问网络：" + name)


class FakeSocket:
    closed = False

    def __init__(self):
        self.frames = []
        self.binary = []
        self.write_delay = 0
        self.first_motion = asyncio.Event()

    async def send_json(self, frame):
        started = time.monotonic()
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        self.frames.append({"frame": copy.deepcopy(frame), "started": started,
                            "completed": time.monotonic()})
        if frame["type"] == 9:
            self.first_motion.set()

    async def send_bytes(self, chunk):
        self.binary.append(bytes(chunk))


class DirectorExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="director-execution-offline-")
        self.out = Path(self.temp.name)
        # 只组装worker需要的对象，避免启动HTTP服务或调用云端生命周期。
        self.s = object.__new__(live_session.LiveSession)
        self.s.out = self.out
        self.s.http = NoNetwork()
        self.s.diagnostics = Diagnostics(self.out)
        self.s.comments = []
        self.s.stopping = False
        self.s.work = asyncio.Queue()
        self.s.totals = {"input_tokens": 0, "output_tokens": 0, "cost_cny": 0.0,
                         "audio_seconds": 0.0, "usage_unverified_segments": 0}
        self.events = []
        self.s.event = lambda event, **fields: self.events.append({"event": event, **fields})
        self.s.component = MotionComponent(self.s.http, self.out, self.out / "unused.png")
        self.s.component._ws = FakeSocket()
        self.s.component.live_id = "offline-only"
        self.s.component.state = "ready"
        self.s.component._ready.set()
        self.s.component._joined.set()
        self.s.component.DRAIN_SECONDS = 0
        self.s.component._audio = AsyncMock()
        self.patches = [
            patch.object(live_session, "load_bailian", side_effect=AssertionError("禁止读取TTS凭据")),
            patch.object(live_session, "load_vidu", side_effect=AssertionError("禁止读取Vidu凭据")),
            patch.object(live_session, "synthesize_segment", side_effect=AssertionError("禁止调用TTS")),
            patch("motion_component.compile_timeline", side_effect=compressed_timeline),
            # 本套件验证worker与撤销合同，计划窗口和发送闸门须使用同一缩放。
            # 真实一秒与PCM并发由test_motion_gap单独验收，不能在这里绕过闸门。
            patch("motion_component.ACTION_POST_SEND_SECONDS", ACTION_POST_SEND_SECONDS * TIMING_SCALE),
        ]
        for entry in self.patches:
            entry.start()
        self.worker = asyncio.create_task(self.s.process_answers())

    async def asyncTearDown(self):
        self.s.latch_stop()
        self.s.work.put_nowait(None)
        await asyncio.wait_for(self.worker, 2)
        for entry in reversed(self.patches):
            entry.stop()
        self.temp.cleanup()

    def comment(self, *, motion=True, actions=None, segments=1):
        actions = [short()] if actions is None else actions
        if motion:
            plan = {"emotion": "平静",
                    "actions": actions, "recovery": dict(GE),
                    "timeline": compile_timeline(actions, GE)}
        else:
            recovery = dict(GE) if actions else None
            plan = {"emotion": "平静", "segments": [
                {"text": "咱听着呢。", "instruction": "自然清晰", "actions": actions,
                 "recovery": recovery, "timeline": compile_timeline(actions, recovery)}
                for _ in range(segments)]}
        comment = {"id": "comment-" + str(len(self.s.comments)), "status": "ready", "plan": plan,
                   "segments_sent": 0,
                   "usage": {"input_tokens": 0, "output_tokens": 0, "cost_cny": 0.0}}
        if motion:
            comment.update(input_kind="operator", operator_mode="motion")
        self.s.comments.append(comment)
        return comment

    async def run_comment(self, comment):
        self.s.work.put_nowait(comment)
        await asyncio.wait_for(self.s.work.join(), 2)

    def prepared_result(self):
        pcm = b"\x02\x00" * 100
        path = self.out / "offline.pcm"
        path.write_bytes(pcm)
        return {"status": "ok", "usage_verified": True, "input_tokens": 5,
                "output_tokens": 9, "cost_cny": 0.001, "audio_seconds": 0.01,
                "pcm_path": str(path), "pcm_sha256": hashlib.sha256(pcm).hexdigest()}

    async def test_motion_uses_only_type9_and_tail_q_without_tts_or_speech_counters(self):
        comment = self.comment()
        await self.run_comment(comment)
        self.assertEqual(comment["status"], "sent")
        self.assertTrue(comment["delivery_started"])
        self.assertEqual(comment["segments_sent"], 0)
        self.assertEqual(comment["actions_sent"], 2)
        self.assertEqual(comment["motion_group_status"], "sent")
        receipt, = comment["motion_receipts"]
        self.assertEqual([e["role"] for e in receipt["motion_events"]], ["S", "Q"])
        self.assertTrue(receipt["actions_sent_complete"])
        self.assertFalse(receipt["visual_effect_confirmed"])
        self.assertFalse(receipt["playback_complete_confirmed"])
        component = self.s.component
        self.assertEqual([r["frame"]["type"] for r in component._ws.frames], [9, 9])
        self.assertFalse(component._ws.binary)
        component._audio.assert_not_awaited()
        self.assertEqual(component.audio_seconds, 0)
        self.assertEqual(component.utterances_started, 0)
        self.assertEqual(component.utterances_sent, 0)
        live_session.load_bailian.assert_not_called()
        live_session.synthesize_segment.assert_not_called()

    async def test_motion_l_window_and_successful_write_anchor_reuse_scheduler(self):
        comment = self.comment(actions=[long(), short(delay=2)])
        self.s.component._ws.write_delay = 0.015
        await self.run_comment(comment)
        first, second, quiet = self.s.component._ws.frames
        self.assertGreaterEqual(second["started"] - first["completed"], 0.039)
        self.assertGreaterEqual(quiet["started"] - second["completed"], 0.029)
        self.assertEqual([e["role"] for e in self.s.component.motion_events], ["L", "S", "Q"])
        self.assertIn(GE["gaze"], quiet["frame"]["payload"]["text_msg"]["content"])

    async def test_ready_cancelled_queue_item_never_executes(self):
        for motion in (True, False):
            comment = self.comment(motion=motion)
            comment["status"] = "cancelled"
            await self.run_comment(comment)
            self.assertEqual(comment["status"], "cancelled")
            self.assertFalse(comment.get("delivery_started"))
        self.assertEqual(self.s.component._ws.frames, [])
        live_session.load_bailian.assert_not_called()

    async def test_cancel_requested_before_claim_skips_both_channels(self):
        for motion in (True, False):
            comment = self.comment(motion=motion)
            comment["cancel_requested"] = True
            await self.run_comment(comment)
            self.assertEqual(comment["status"], "cancelled")
            self.assertFalse(comment.get("delivery_started"))
        live_session.load_bailian.assert_not_called()
        live_session.synthesize_segment.assert_not_called()
        self.assertEqual(self.s.component._ws.frames, [])

    async def test_expired_motion_and_speech_queue_items_never_execute(self):
        for motion in (True, False):
            comment = self.comment(motion=motion)
            comment["expires_at"] = time.time() - 1
            await self.run_comment(comment)
            self.assertEqual(comment["status"], "expired")
        live_session.load_bailian.assert_not_called()
        self.assertEqual(self.s.component._ws.frames, [])

    async def test_cancel_before_prepare_audio_never_calls_tts(self):
        comment = self.comment(motion=False)
        comment["cancel_requested"] = True
        result = await self.s.prepare_audio("offline", "offline", comment,
                                            comment["plan"]["segments"][0], 1)
        self.assertIsNone(result)
        self.assertEqual(comment["status"], "cancelled")
        live_session.synthesize_segment.assert_not_called()

    async def test_cancel_during_tts_blocks_send_and_preserves_usage(self):
        comment = self.comment(motion=False)
        started, released = asyncio.Event(), asyncio.Event()

        async def synthesize(*args):
            started.set()
            await released.wait()
            return self.prepared_result()

        with patch.object(live_session, "load_bailian", return_value=("offline", "offline")), \
                patch.object(live_session, "synthesize_segment", side_effect=synthesize):
            self.s.work.put_nowait(comment)
            await asyncio.wait_for(started.wait(), 1)
            comment["cancel_requested"] = True
            released.set()
            await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(comment["status"], "cancelled")
        self.assertEqual(comment["usage"]["input_tokens"], 5)
        self.assertEqual(self.s.totals["output_tokens"], 9)
        self.assertEqual(self.s.totals["cost_cny"], 0.001)
        self.assertEqual(comment["segments_sent"], 0)
        self.assertFalse(comment.get("delivery_started"))
        self.assertFalse(self.s.component._ws.frames)

    async def test_cancel_after_audio_preparation_still_prevents_first_send(self):
        comment = self.comment(motion=False)

        async def prepared(*args):
            comment["cancel_requested"] = True
            return b"\0\0", self.out

        self.s.prepare_audio = AsyncMock(side_effect=prepared)
        with patch.object(live_session, "load_bailian", return_value=("offline", "offline")):
            await self.run_comment(comment)
        self.assertEqual(comment["status"], "cancelled")
        self.assertFalse(self.s.component._ws.frames)

    async def test_late_tts_error_cannot_overwrite_cancelled_status(self):
        comment = self.comment(motion=False)

        async def failed(*args):
            comment.update(cancel_requested=True, status="cancelled")
            raise ConnectionError("离线模拟迟到失败")

        with patch.object(live_session, "load_bailian", return_value=("offline", "offline")), \
                patch.object(live_session, "synthesize_segment", side_effect=failed):
            await self.run_comment(comment)
        self.assertEqual(comment["status"], "cancelled")
        self.assertFalse(comment.get("delivery_started"))
        self.assertFalse(self.s.component._ws.frames)

    async def test_cancel_during_answer_tts_never_starts_delivery(self):
        comment = self.comment(motion=False)
        calls = 0

        async def prepared(*args):
            nonlocal calls
            calls += 1
            comment["cancel_requested"] = True
            return b"\0\0", self.out

        self.s.prepare_audio = AsyncMock(side_effect=prepared)
        with patch.object(live_session, "load_bailian", return_value=("offline", "offline")):
            await self.run_comment(comment)
        self.assertEqual(calls, 1)
        self.assertEqual(comment["status"], "cancelled")
        self.assertFalse(self.s.component._ws.frames)

    async def test_cancel_after_motion_started_preserves_remaining_nodes_and_q(self):
        comment = self.comment(actions=[short(), short(delay=2)])
        self.s.work.put_nowait(comment)
        await asyncio.wait_for(self.s.component._ws.first_motion.wait(), 1)
        self.assertTrue(comment["delivery_started"])
        comment["cancel_requested"] = True
        await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(comment["status"], "sent")
        self.assertEqual([e["role"] for e in self.s.component.motion_events], ["S", "S", "Q"])

    async def test_cancel_after_speech_started_finishes_accepted_two_segment_round(self):
        comment = self.comment(motion=False, segments=2, actions=[])
        self.s.prepare_audio = AsyncMock(return_value=(b"\0\0", self.out))
        calls = []

        async def sent(*args, **kwargs):
            self.assertTrue(comment["delivery_started"])
            calls.append(args[3])
            comment["cancel_requested"] = True
            return {"group_id": "offline-" + str(args[3])}

        self.s.component.send_expression = AsyncMock(side_effect=sent)
        with patch.object(live_session, "load_bailian", return_value=("offline", "offline")):
            await self.run_comment(comment)
        self.assertEqual(comment["status"], "sent")
        self.assertEqual(comment["segments_sent"], 2)
        self.assertEqual(calls, [1, 2])

    async def test_global_stop_interrupts_motion_and_preserves_partial_receipt(self):
        comment = self.comment(actions=[short(), short(delay=10)])
        self.s.work.put_nowait(comment)
        await asyncio.wait_for(self.s.component._ws.first_motion.wait(), 1)
        self.s.latch_stop()
        await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(comment["status"], "cancelled")
        self.assertEqual(comment["actions_sent"], 1)
        self.assertEqual(comment["motion_group_status"], "cancelled")
        self.assertFalse(comment["motion_receipts"][0]["actions_sent_complete"])
        self.assertEqual([e["role"] for e in self.s.component.motion_events], ["S"])

    async def test_global_stop_interrupts_started_speech_round_before_second_segment(self):
        comment = self.comment(motion=False, segments=2, actions=[])
        self.s.prepare_audio = AsyncMock(return_value=(b"\0\0", self.out))

        async def sent(*args, **kwargs):
            self.s.latch_stop()
            return {"group_id": "offline-stop"}

        self.s.component.send_expression = AsyncMock(side_effect=sent)
        with patch.object(live_session, "load_bailian", return_value=("offline", "offline")):
            await self.run_comment(comment)
        self.assertEqual(comment["status"], "cancelled")
        self.assertEqual(comment["segments_sent"], 1)
        self.s.component.send_expression.assert_awaited_once()

    async def test_queue_retired_while_previous_motion_sends_cannot_reenter(self):
        first = self.comment(actions=[short(), short(delay=5)])
        retired = self.comment()
        self.s.work.put_nowait(first)
        self.s.work.put_nowait(retired)
        await asyncio.wait_for(self.s.component._ws.first_motion.wait(), 1)
        retired.update(status="cancelled", cancel_requested=True)
        await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(first["status"], "sent")
        self.assertEqual(retired["status"], "cancelled")
        self.assertFalse(retired.get("delivery_started"))
        self.assertEqual({e["comment_id"] for e in self.s.component.motion_events}, {first["id"]})

    async def test_prepare_overlaps_active_send_and_reuses_all_audio_once(self):
        current = self.comment(motion=False, actions=[])
        future = self.comment(motion=False, segments=2)
        started, release = asyncio.Event(), asyncio.Event()
        active, maximum = 0, 0
        order = []

        async def send(pcm, text, actions, number, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            owner = self.s.component.comment_id
            order.append(owner)
            if owner == current["id"]:
                started.set()
                await release.wait()
            active -= 1
            return {"group_id": owner + str(number)}

        self.s.prepare_audio = AsyncMock(return_value=(b"\0\0", self.out))
        self.s.component.send_expression = AsyncMock(side_effect=send)
        hooks = []
        self.s.on_delivery_started = lambda c: hooks.append(("start", c["id"]))
        self.s.on_delivery_finished = lambda c: hooks.append(("finish", c["id"]))
        with patch.object(live_session, "load_bailian", return_value=("offline", "offline")):
            self.s.work.put_nowait(current)
            await asyncio.wait_for(started.wait(), 1)
            self.assertTrue(await self.s.prepare_comment(future))
            self.assertEqual(current["status"], "sending")
            self.assertEqual(future["status"], "prepared")
            self.assertEqual(self.s.component.comment_id, current["id"])
            self.assertFalse(future.get("delivery_started"))
            self.assertEqual(self.s.prepare_audio.await_count, 3)
            # 再请求同一份准备不会重复收费，发送复用两段回答。
            self.assertTrue(await self.s.prepare_comment(future))
            self.s.work.put_nowait(future)
            release.set()
            await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(self.s.prepare_audio.await_count, 3)
        self.assertEqual(maximum, 1)
        self.assertEqual(order, [current["id"], future["id"], future["id"]])
        self.assertEqual(future["segments_sent"], 2)
        self.assertEqual(hooks, [("start", current["id"]), ("finish", current["id"]),
                                 ("start", future["id"]), ("finish", future["id"])])
        self.assertNotIn(future["id"], self.s._prepared_comments)

    async def test_final_gate_yields_prepared_story_to_viewer_without_resynthesis(self):
        story = self.comment(motion=False, actions=[])
        viewer = self.comment(motion=False, actions=[])
        self.s.prepare_audio = AsyncMock(return_value=(b"\0\0", self.out))
        self.s.component.send_expression = AsyncMock(return_value={"group_id": "offline"})
        has_viewer = True
        self.s.delivery_gate = lambda c: c is viewer or not has_viewer
        with patch.object(live_session, "load_bailian", return_value=("offline", "offline")):
            self.assertTrue(await self.s.prepare_comment(story))
            self.assertTrue(await self.s.prepare_comment(viewer))
            await self.run_comment(story)
            self.assertEqual(story["status"], "prepared")
            self.assertFalse(story.get("delivery_started"))
            self.assertIsNone(self.s.component.comment_id)
            self.s.component.send_expression.assert_not_awaited()
            await self.run_comment(viewer)
            self.assertEqual(viewer["status"], "sent")
            has_viewer = False
            await self.run_comment(story)
        self.assertEqual(story["status"], "sent")
        self.assertEqual(self.s.prepare_audio.await_count, 2)

    async def test_prepared_plan_context_change_fails_closed_without_paid_retry(self):
        c = self.comment(motion=False, actions=[])
        c.update(topic_id="story", topic_version=1, return_context_version=0)
        self.s.prepare_audio = AsyncMock(return_value=(b"\0\0", self.out))
        with patch.object(live_session, "load_bailian", return_value=("offline", "offline")):
            self.assertTrue(await self.s.prepare_comment(c))
            c["return_context_version"] += 1
            await self.run_comment(c)
        self.assertEqual(c["status"], "error")
        self.assertEqual(self.s.prepare_audio.await_count, 1)
        self.assertFalse(c.get("delivery_started"))
        self.assertFalse(self.s.component._ws.frames)

    async def test_preparation_late_result_after_pause_stop_or_discard_never_publishes(self):
        for reason in ("pause", "stop", "discard", "version"):
            c = self.comment(motion=False, actions=[])
            entered, release = asyncio.Event(), asyncio.Event()
            async def prepare(*args):
                entered.set()
                await release.wait()
                return b"\0\0", self.out
            self.s.prepare_audio = AsyncMock(side_effect=prepare)
            with patch.object(live_session, "load_bailian", return_value=("offline", "offline")):
                task = asyncio.create_task(self.s.prepare_comment(c))
                await asyncio.wait_for(entered.wait(), 1)
                if reason in {"pause", "stop"}:
                    c["cancel_requested"] = True
                elif reason == "discard":
                    self.s.discard_prepared(c["id"])
                else:
                    c["topic_version"] = 2
                release.set()
                self.assertFalse(await asyncio.wait_for(task, 1))
            self.assertNotIn(c["id"], getattr(self.s, "_prepared_comments", {}))
            self.assertFalse(c.get("delivery_started"))
            self.assertEqual(c["status"], "cancelled")
        self.assertFalse(self.s.component._ws.frames)

    async def test_cancel_preparation_during_paid_tts_still_accounts_late_result(self):
        c = self.comment(motion=False, actions=[])
        entered, release = asyncio.Event(), asyncio.Event()
        async def synthesize(*args, **kwargs):
            entered.set()
            await release.wait()
            return self.prepared_result()
        with patch.object(live_session, "load_bailian", return_value=("offline", "offline")), \
                patch.object(live_session, "synthesize_segment", side_effect=synthesize):
            task = asyncio.create_task(self.s.prepare_comment(c))
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done(), "重复撤销仍须等已发出的收费TTS结果入账")
            release.set()
            self.assertFalse(await asyncio.wait_for(task, 1))
        self.assertEqual(c["status"], "cancelled")
        self.assertEqual(c["usage"]["input_tokens"], 5)
        self.assertEqual(self.s.totals["cost_cny"], .001)
        self.assertFalse(c.get("delivery_started"))
        self.assertNotIn(c["id"], getattr(self.s, "_prepared_comments", {}))

    async def test_expiry_after_answer_tts_stops_before_planner(self):
        c = self.comment(motion=False)
        self.s.semantic_speech = True
        async def answer_audio(*args):
            c["expires_at"] = time.time() - 1
            return b"\0\0", self.out
        self.s.prepare_audio = AsyncMock(side_effect=answer_audio)
        self.s.prepare_timed_actions = AsyncMock()
        with patch.object(live_session, "load_bailian", return_value=("offline", "offline")):
            self.assertFalse(await self.s.prepare_comment(c))
        self.s.prepare_audio.assert_awaited_once()
        self.s.prepare_timed_actions.assert_not_awaited()
        self.assertEqual(c["status"], "expired")
        self.assertFalse(c.get("delivery_started"))

    async def test_original_invalidation_after_answer_tts_blocks_paid_action_planning(self):
        self.s.semantic_speech = True
        self.s.prepare_timed_actions = AsyncMock()
        for reason in ("expired", "cancelled", "discard"):
            c = self.comment(motion=False)
            async def answer_audio(*args):
                if reason == "expired":
                    c["expires_at"] = time.time() - 1
                elif reason == "cancelled":
                    c["cancel_requested"] = True
                else:
                    self.s.discard_prepared(c)
                return b"\0\0", self.out
            self.s.prepare_audio = AsyncMock(side_effect=answer_audio)
            with patch.object(live_session, "load_bailian", return_value=("offline", "offline")):
                self.assertFalse(await self.s.prepare_comment(c))
            self.assertEqual(c["status"], "expired" if reason == "expired" else "cancelled")
            self.assertFalse(c.get("delivery_started"))
        self.s.prepare_timed_actions.assert_not_awaited()

    async def test_preparation_failure_does_not_fail_current_sender_or_global_callback(self):
        current = self.comment(motion=False, actions=[])
        future = self.comment(motion=False, actions=[])
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"group_id": "current"}
        self.s.component.send_expression = AsyncMock(side_effect=send)
        self.s.prepare_audio = AsyncMock(return_value=(b"\0\0", self.out))
        failure = self.s.timed_action_error = AsyncMock()
        with patch.object(live_session, "load_bailian", return_value=("offline", "offline")):
            self.s.work.put_nowait(current)
            await asyncio.wait_for(entered.wait(), 1)
            self.s.prepare_audio.side_effect = live_session.ModelError("timed_action_plan_invalid")
            self.assertFalse(await self.s.prepare_comment(future))
            self.assertEqual(current["status"], "sending")
            release.set()
            await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(current["status"], "sent")
        self.assertEqual(future["status"], "error")
        self.assertEqual(future["preparation_error"]["code"], "timed_action_plan_invalid")
        failure.assert_not_called()

    async def test_concurrent_action_preparation_keeps_legacy_task_and_cached_timeline(self):
        self.s.semantic_speech = True
        self.s.brain_context = {}
        current = self.comment(motion=False, actions=[])
        future = self.comment(motion=False, actions=[])
        pcm = b"\0\0" * 24000 * 2
        for c in (current, future):
            c["text"] = c["id"]
            c["plan"]["segments"][0].update(
                actions=[{"anchor": {"quote": "咱", "occurrence": 1},
                          "motions": [{"part": "head", "description": "轻轻点头"}], **GE}],
                recovery=dict(GE))
        entered = {c["id"]: asyncio.Event() for c in (current, future)}
        release = {c["id"]: asyncio.Event() for c in (current, future)}
        async def audio(key, workspace, comment, part, number):
            part["alignment"] = {"status": "ok", "timebase": "audio_start",
                                 "pcm_sha256": hashlib.sha256(pcm).hexdigest(), "words": [
                {"text": "咱", "begin_time": 0, "end_time": 200},
                {"text": "听着呢。", "begin_time": 200, "end_time": 1800}]}
            return pcm, self.out
        async def planner(content, alignment, seconds, context, *, source_text):
            entered[source_text].set()
            await release[source_text].wait()
            return {"actions": copy.deepcopy(content["actions"]), "planning": {"phase": "tts_aligned"}}
        self.s.prepare_audio = AsyncMock(side_effect=audio)
        self.s.timed_action_planner = AsyncMock(side_effect=planner)
        self.s.component.send_aligned_expression = AsyncMock(return_value={"group_id": "semantic"})
        with patch.object(live_session, "load_bailian", return_value=("offline", "offline")):
            self.s.work.put_nowait(current)
            await asyncio.wait_for(entered[current["id"]].wait(), 1)
            active_planner = self.s.timed_action_task
            pending = asyncio.create_task(self.s.prepare_comment(future))
            await asyncio.wait_for(entered[future["id"]].wait(), 1)
            self.assertIs(self.s.timed_action_task, active_planner)
            self.assertEqual(len(self.s._timed_action_tasks), 2)
            release[future["id"]].set()
            self.assertTrue(await asyncio.wait_for(pending, 1))
            self.assertIs(self.s.timed_action_task, active_planner)
            release[current["id"]].set()
            await asyncio.wait_for(self.s.work.join(), 1)
            await self.run_comment(future)
        self.assertEqual(self.s.prepare_audio.await_count, 2)
        self.assertEqual(self.s.timed_action_planner.await_count, 2)
        self.assertEqual(self.s.component.send_aligned_expression.await_count, 2)
        self.assertEqual(future["status"], "sent")
        self.assertFalse(self.s._timed_action_tasks)
        self.assertIsNone(self.s.timed_action_task)
        self.assertEqual([n["role"] for n in future["plan"]["segments"][0]["timeline"]], ["GE", "S", "Q"])
        self.assertTrue(self.s.component.send_aligned_expression.await_args.kwargs["allow_tail_q_after_audio"])


if __name__ == "__main__":
    unittest.main()
