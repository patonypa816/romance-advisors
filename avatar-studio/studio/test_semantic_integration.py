"""Formal Studio queue uses the same semantic path for viewer and director inputs."""
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
import test_studio as fixtures
from cloudbase_ai import ModelError
from semantic_speech import compile_dialogue_semantic_timeline

GE = {"gaze": "注视镜头", "expression": "神情温和"}
TEXT = "咱从田里走来，也知道百姓的苦。"


def plan(*, reading=None, actions=True, quote="咱"):
    return {"emotion": "平静", "observation": "当前画面姿态未知", "reading": reading,
            "segments": [{"text": TEXT, "instruction": "自然清晰", "actions": [
                {"anchor": {"quote": quote, "occurrence": 1}, "motions": [
                    {"part": "head", "description": "轻轻点头一次"}], **GE}
            ] if actions else [], "recovery": dict(GE) if actions or reading else None}]}


class SemanticIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ai = fixtures.FakeAI()
        self.ai.plan_timed_actions = AsyncMock(side_effect=self.plan_actions)
        self.patches = [patch.object(live_session, "PersistentComponent", fixtures.FakeComponent),
                        patch.object(live_session, "load_bailian", return_value=("offline", "offline")),
                        patch.object(live_session, "load_vidu", side_effect=AssertionError("no cloud")),
                        patch.object(live_session, "synthesize_segment", side_effect=self.synthesize)]
        for p in self.patches:
            p.start()
        self.studio = fixtures.studio_server.Studio(self.root, ai=self.ai)
        await self.studio.new_session()
        self.s = self.studio.current
        self.s.started = True
        self.s.component.state = "ready"
        self.sender = self.s.component.send_aligned_expression = AsyncMock(return_value={"group_id": "offline", "reading_sent_complete": True})
        self.s.component.send_expression = AsyncMock(side_effect=AssertionError("legacy speech called"))
        self.tts_calls = []
        self.bad_alignment = False

    async def plan_actions(self, part, alignment, seconds, context, *, source_text=""):
        # 模拟模型选择原锚点，仍运行生产编译器；特定用例再模拟其他模型输出。
        self.assertEqual(set(part), {"text", "instruction", "actions", "recovery"})
        self.assertTrue(self.tts_calls)
        return {"actions": copy.deepcopy(part["actions"]),
                "timeline": compile_dialogue_semantic_timeline(part["actions"], part["recovery"], alignment, seconds),
                "planning": {"phase": "tts_aligned", "model_called": True,
                             "group_count": len(part["actions"])}}

    async def asyncTearDown(self):
        await self.studio.automation.close()
        await self.s.shutdown(None)
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    async def synthesize(self, http, key, workspace, out, text, instruction, **options):
        self.tts_calls.append((text, options))
        out.mkdir(parents=True)
        pcm = b"\0\0" * 24000 * 8
        path = out / "sample.pcm"
        path.write_bytes(pcm)
        words = [{"text": text, "begin_time": 0, "end_time": 7600}]
        # The first and last spoken character have measured boundaries; no guessed interpolation.
        if text == TEXT:
            words = [{"text": "咱", "begin_time": 0, "end_time": 400},
                     {"text": TEXT[1:-2], "begin_time": 400, "end_time": 7000},
                     {"text": TEXT[-2:], "begin_time": 7000, "end_time": 7600}]
        result = {"status": "ok", "pcm_path": str(path), "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
                  "usage_verified": True, "input_tokens": 2, "output_tokens": 3, "cost_cny": .001, "audio_seconds": 8}
        if options.get("word_timestamps"):
            result["alignment"] = {"status": "unavailable" if self.bad_alignment else "ok", "timebase": "audio_start",
                                   "words": words, "pcm_sha256": result["pcm_sha256"]}
        return result

    async def submit(self, body, *, source="manual", mode=None):
        if mode == "topic":
            self.studio.director.submit({"text": "百姓怎么样", "mode": "topic", "rounds": 1})
            c = self.studio.director.next_item()
        else:
            c = self.studio.enqueue_comment("百姓怎么样", "观众", source=source)
            if mode:
                c.update(input_kind="operator", operator_mode=mode)
        c["status"] = "processing"
        self.studio.accept_plan(self.s, c, copy.deepcopy(body))
        await asyncio.wait_for(self.s.work.join(), 2)
        return c

    async def test_all_speech_entry_routes_compile_from_same_tts(self):
        for source, mode in (("manual", None), ("dyhub", None), ("manual", "once"), ("manual", "topic")):
            c = await self.submit(plan(), source=source, mode=mode)
            self.assertEqual(c["status"], "sent", c.get("error"))
            self.assertEqual(c["delivery"][0]["timeline"][1]["at_seconds"], 0)
            self.assertTrue(self.tts_calls[-1][1]["word_timestamps"])
            self.assertEqual(self.sender.await_args.kwargs["alignment"]["status"], "ok")
            self.assertEqual(self.sender.await_args.kwargs["action_kind"], "S")
            self.assertEqual(self.sender.await_args.kwargs["ge_lead_seconds"], 1.1)
            self.assertTrue(self.sender.await_args.kwargs["allow_tail_q_after_audio"])
            self.assertTrue(self.sender.await_args.kwargs["local_action_anchors"])
            self.assertEqual([node["role"] for node in c["delivery"][0]["timeline"]], ["GE", "S", "Q"])
        self.assertEqual(self.sender.await_count, 4)
        manifest = json.loads((self.s.out / "source-manifest.json").read_text())
        self.assertIn("semantic_speech.py", manifest)
        self.assertTrue(self.studio.snapshot()["studio"]["semantic_speech"])

    async def test_unrelated_provider_character_difference_reaches_formal_sender(self):
        async def changed(*args, **kwargs):
            result = await self.synthesize(*args, **kwargs)
            result["alignment"]["words"][1]["text"] = result["alignment"]["words"][1]["text"].replace("田", "填")
            return result
        with patch.object(live_session, "synthesize_segment", side_effect=changed):
            comment = await self.submit(plan())
        self.assertEqual(comment["status"], "sent", comment.get("error"))
        self.sender.assert_awaited_once()
        self.assertIn("填", self.sender.await_args.kwargs["alignment"]["words"][1]["text"])

    async def test_unsafe_final_model_anchor_is_rejected_before_sender(self):
        async def changed(*args, **kwargs):
            result = await self.synthesize(*args, **kwargs)
            result["alignment"]["words"][0]["text"] = "咱咱"
            return result
        # The mock planner can compile occurrence=1, but the formal acceptance
        # gate must reject the now-ambiguous local correspondence before sending.
        with patch.object(live_session, "synthesize_segment", side_effect=changed):
            comment = await self.submit(plan())
        self.assertEqual(comment["status"], "error")
        self.ai.plan_timed_actions.assert_awaited_once()
        self.sender.assert_not_awaited()

    async def test_observed_flat_anchor_shape_reaches_only_canonical_sender(self):
        body = plan()
        action = body["segments"][0]["actions"][0]
        action["occurrence"] = action["anchor"]["occurrence"]
        action["anchor"] = action["anchor"]["quote"]
        before = copy.deepcopy(body)
        c = await self.submit(body)
        self.assertEqual(c["status"], "sent", c.get("error"))
        self.assertEqual(self.sender.await_args.args[2], plan()["segments"][0]["actions"])
        self.assertEqual(body, before)

    async def test_viewer_and_topic_continuous_actions_reach_sender_with_a_policy(self):
        for mode in (None, "topic"):
            body = plan()
            part = body["segments"][0]
            action = part["actions"][0]
            action["motions"][0]["description"] = "轻轻点头然后向左转头"
            action.update(gaze="注视镜头接着看向左侧", expression="先微笑；随后神情平静")
            part["recovery"]["gaze"] = "看向左侧然后看回镜头"
            c = await self.submit(body, mode=mode)
            self.assertEqual(c["status"], "sent", c.get("error"))
            self.assertEqual(self.sender.await_args.args[2], part["actions"])
            self.assertEqual(self.sender.await_args.kwargs["recovery"], part["recovery"])
            timeline = c["delivery"][0]["timeline"]
            self.assertEqual([(n["role"], n["at_seconds"]) for n in timeline],
                             [("GE", -1.1), ("S", 0), ("Q", 1.5)])
            self.assertIn(action["motions"][0]["description"], timeline[1]["instruction"])

    async def test_unavailable_alignment_or_dense_groups_never_start_reading(self):
        for bad, code in ((True, "alignment_unavailable"), (False, "anchor_window_conflict")):
            self.bad_alignment = bad
            body = plan(reading={"text": "百姓", "instruction": "轻声短读"})
            if not bad:
                second = copy.deepcopy(body["segments"][0]["actions"][0])
                second["anchor"]["quote"] = "从田里走来"
                body["segments"][0]["actions"].append(second)
            c = await self.submit(body)
            self.assertEqual(c["status"], "error")
            self.assertEqual(c["delivery_error"]["code"], code)
            self.assertFalse(c.get("delivery_started"))
            self.assertEqual(self.tts_calls[-2][1], {})
        self.sender.assert_not_awaited()
        self.assertEqual(self.s.totals["cost_cny"], .004)

    async def test_last_word_s_can_finish_q_after_audio_and_still_pass_reading_to_sender(self):
        c = await self.submit(plan(reading={"text": "百姓", "instruction": "轻声短读"}, quote="苦"))
        self.assertEqual(c["status"], "sent", c.get("error"))
        timeline = c["delivery"][0]["timeline"]
        self.assertEqual([(n["role"], n["at_seconds"]) for n in timeline],
                         [("GE", 5.9), ("S", 7), ("Q", 8.5)])
        self.assertTrue(self.sender.await_args.kwargs["allow_tail_q_after_audio"])
        self.assertIsNotNone(self.sender.await_args.kwargs["reading"])

    async def test_plain_speech_and_reading_without_actions_do_not_require_alignment(self):
        self.bad_alignment = True
        for reading in (None, {"text": "百姓", "instruction": "轻声短读"}):
            c = await self.submit(plan(actions=False, reading=reading))
            self.assertEqual(c["status"], "sent", c.get("error"))
            self.assertEqual(c["delivery"][0]["timeline"], [])
        self.assertIsNotNone(self.sender.await_args.kwargs["reading"])

    async def test_specific_safe_timestamp_reason_is_visible(self):
        original = self.s.prepare_audio
        async def prepare(*args):
            result = await original(*args)
            args[3]["alignment"].update(status="unavailable", reason="text_mismatch")
            return result
        self.s.prepare_audio = prepare
        c = await self.submit(plan())
        self.assertEqual(c["status"], "error")
        self.assertEqual(c["delivery_error"]["reason"], "text_mismatch")
        self.assertIn("时间戳文字与台词内容不一致", c["error"])
        self.sender.assert_not_awaited()

    async def test_partial_send_failure_is_archived_without_success(self):
        async def fail_after_first_frame(*args, **kwargs):
            self.s.component.last_motion_receipt = {
                "comment_id": self.s.component.comment_id, "segment_index": 1,
                "mode": "semantic_speech", "status": "error", "group_id": "partial",
                "send_complete": False, "actions_sent_complete": False,
                "motion_events": [{"role": "GE"}]}
            raise RuntimeError("offline simulated write failure")
        self.sender.side_effect = fail_after_first_frame
        c = await self.submit(plan())
        self.assertEqual(c["status"], "error")
        self.assertEqual(c["segments_sent"], 0)
        receipt = json.loads((self.s.out / "tts" / (c["id"] + "-1") / "component-send.json").read_text())
        self.assertFalse(receipt["send_complete"])
        self.assertEqual(receipt["motion_events"], [{"role": "GE"}])
        self.assertEqual(c["motion_receipts"][0], receipt)

    async def test_stale_or_cancelled_tts_cannot_send(self):
        original = self.s.prepare_audio
        for reason in ("expired", "cancelled"):
            async def prepare(*args):
                result = await original(*args)
                comment = args[2]
                if reason == "expired": comment["expires_at"] = time.time() - 1
                else: comment["cancel_requested"] = True
                return result
            self.s.prepare_audio = prepare
            c = await self.submit(plan())
            self.assertEqual(c["status"], reason)
        self.sender.assert_not_awaited()

    async def test_final_anchor_uses_same_tts_and_preserves_saved_draft(self):
        async def choose_measured_word(part, alignment, seconds, context, **options):
            self.assertEqual(set(part), {"text", "instruction", "actions", "recovery"})
            self.assertEqual(seconds, 8)
            self.assertEqual(alignment["words"][1]["begin_time"], 400)
            part["actions"][0]["anchor"] = {"quote": "从田里走来", "occurrence": 1}
            return await self.plan_actions(part, alignment, seconds, context, **options)
        self.ai.plan_timed_actions.side_effect = choose_measured_word
        c = await self.submit(plan())
        self.assertEqual(c["status"], "sent", c.get("error"))
        part = c["delivery"][0]
        self.assertEqual(part["actions"][0]["anchor"]["quote"], "从田里走来")
        self.assertEqual(part["timeline"][1]["at_seconds"], .4)
        self.assertEqual(self.sender.await_args.args[2], part["actions"])
        archived = json.loads((self.s.out / "plans" / (c["id"] + ".json")).read_text())
        self.assertEqual(archived["segments"][0]["actions"][0]["anchor"]["quote"], "咱")
        evidence = json.loads((self.s.out / "tts" / (c["id"] + "-1") / "timed-action-plan.json").read_text())
        pcm = self.sender.await_args.args[0]
        self.assertEqual(evidence["planning"]["pcm_sha256"], hashlib.sha256(pcm).hexdigest())
        self.ai.plan_timed_actions.assert_awaited_once()

    async def test_model_error_before_any_reading_or_audio_pauses_future_model_work(self):
        self.ai.plan_timed_actions.side_effect = ModelError("timed_action_plan_invalid")
        c = await self.submit(plan(reading={"text": "百姓", "instruction": "轻声短读"}))
        self.assertEqual(c["status"], "error")
        self.assertEqual(c["model_error_code"], "timed_action_plan_invalid")
        self.assertIn("时间戳动作计划", c["error"])
        self.assertFalse(c.get("delivery_started"))
        self.assertEqual(self.studio.automation.last_error, "timed_action_plan_invalid")
        self.sender.assert_not_awaited()

    async def check_cancel_during_planning(self, reason):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def hold(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise
        self.ai.plan_timed_actions.side_effect = hold
        pending = asyncio.create_task(self.submit(plan()))
        await asyncio.wait_for(entered.wait(), 1)
        c = self.s.comments[-1]
        self.assertEqual(c["status"], "planning_actions")
        self.assertTrue(self.studio.automation.brain_public()["busy"])
        self.assertTrue(self.studio.director.delivery_busy())
        self.assertEqual(self.studio.automation.queue_count(), 1)
        if reason == "stop":
            self.s.latch_stop()
        elif reason == "expired":
            c["expires_at"] = time.time() - 1
        else:
            c["cancel_requested"] = True
        await pending
        self.assertTrue(cancelled.is_set())
        self.assertEqual(c["status"], "expired" if reason == "expired" else "cancelled")
        self.assertIsNone(self.s.timed_action_task)
        self.assertFalse(self.s.worker.done())
        self.sender.assert_not_awaited()
        self.ai.plan_timed_actions.assert_awaited_once()

    async def test_stop_during_timed_planning_cancels_model_and_keeps_worker(self):
        await self.check_cancel_during_planning("stop")

    async def test_expiry_during_timed_planning_blocks_late_result(self):
        await self.check_cancel_during_planning("expired")

    async def test_cancel_during_timed_planning_blocks_late_result(self):
        await self.check_cancel_during_planning("cancel")

    async def test_second_segment_planning_error_keeps_first_send_receipt(self):
        calls = 0
        async def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ModelError("timed_action_plan_invalid")
            return await self.plan_actions(*args, **kwargs)
        self.ai.plan_timed_actions.side_effect = fail_second
        async def remember_first(*args, **kwargs):
            receipt = {"group_id": "first", "comment_id": self.s.component.comment_id,
                       "segment_index": args[3], "mode": "semantic_speech", "status": "sent",
                       "send_complete": True, "actions_sent_complete": True,
                       "reading_sent_complete": True, "motion_events": [{"role": "Q"}]}
            self.s.component.last_motion_receipt = receipt
            return receipt
        self.sender.side_effect = remember_first
        body = plan()
        body["segments"].append(copy.deepcopy(body["segments"][0]))
        c = await self.submit(body)
        self.assertEqual(c["status"], "error")
        self.assertEqual(c["segments_sent"], 1)
        self.assertTrue(c["delivery_started"])
        self.assertEqual(len(c["motion_receipts"]), 1)
        self.assertEqual(c["motion_receipts"][0]["group_id"], "first")
        self.assertTrue((self.s.out / "tts" / (c["id"] + "-1") / "component-send.json").is_file())
        self.sender.assert_awaited_once()

    async def test_cancel_after_plan_validation_before_worker_resumes_cannot_send(self):
        original_prepare = self.s.prepare_timed_actions
        accepted, resume = asyncio.Event(), asyncio.Event()
        async def pause_before_return(*args):
            result = await original_prepare(*args)
            # 明确把撤销放在规划已成功、执行队列尚未恢复的边界，
            # 不依赖不同Python版本对已完成gather是否让出的细节。
            accepted.set()
            await resume.wait()
            return result
        self.s.prepare_timed_actions = pause_before_return
        pending = asyncio.create_task(self.submit(plan()))
        await asyncio.wait_for(accepted.wait(), 1)
        c = self.s.comments[-1]
        c.update(cancel_requested=True, status="cancelled")
        resume.set()
        await pending
        self.assertEqual(c["status"], "cancelled")
        self.assertFalse(c.get("delivery_started"))
        self.sender.assert_not_awaited()


class PreparedSemanticIntegration(unittest.IsolatedAsyncioTestCase):
    """Exercise the real queue and preparation path with a fake final sender."""

    async def asyncSetUp(self):
        from test_semantic_replay import make_preparation
        self.temp = tempfile.TemporaryDirectory(prefix="prepared-studio-integration-")
        self.root = Path(self.temp.name)
        self.source = make_preparation(self.root / "prepared")
        self.studios = []
        self.patches = [
            patch.object(live_session, "load_bailian", side_effect=AssertionError("no TTS credentials")),
            patch.object(live_session, "synthesize_segment", side_effect=AssertionError("no repeated TTS")),
            patch.object(fixtures.studio_server, "load_bailian", side_effect=AssertionError("no startup TTS credentials")),
            patch.object(fixtures.studio_server, "load_vidu", side_effect=AssertionError("no cloud session")),
        ]
        self.guards = [entry.start() for entry in self.patches]

    async def asyncTearDown(self):
        for studio in self.studios:
            await studio.automation.close()
        for entry in reversed(self.patches):
            entry.stop()
        self.temp.cleanup()

    async def make_studio(self, lead, label, *, fixture=None):
        from semantic_replay import PreparedSemanticReplay
        fixture = fixture or PreparedSemanticReplay(self.source, ge_lead_seconds=lead)
        ai = fixtures.FakeAI()
        ai.configured = Mock(side_effect=AssertionError("no model credentials"))
        ai.public = Mock(side_effect=AssertionError("no model credential preflight"))
        ai.generate_director = AsyncMock()
        ai.plan_timed_actions = AsyncMock(side_effect=AssertionError("no new action planning"))
        studio = fixtures.studio_server.Studio(self.root / label, ai=ai, preview=True, replay_fixture=fixture)
        self.studios.append(studio)

        async def offline_startup(session, app):
            session.component = fixtures.FakeComponent()
            session.component.send_aligned_expression = AsyncMock(return_value={
                "group_id": "offline-prepared", "send_complete": True,
                "actions_sent_complete": True, "reading_sent_complete": True})
            session.component.send_expression = AsyncMock(side_effect=AssertionError("no legacy sender"))

        with patch.object(live_session.LiveSession, "startup", offline_startup):
            await studio.new_session()
        studio.current.image.write_bytes(fixture.image_path.read_bytes())
        studio.current.avatar_id = fixture.avatar_id
        return studio, ai, fixture

    async def enqueue(self, studio, fixture):
        studio.current.started = True
        studio.current.component.state = "ready"
        request = SimpleNamespace(json=AsyncMock(return_value={"generation": studio.generation,
            "fingerprint": fixture.fingerprint, "request_id": "offline-prepared-request-0001"}))
        response = await studio.replay(request)
        ident = json.loads(response.body)["id"]
        return next(c for c in studio.current.comments if c["id"] == ident)

    async def test_prepared_new_session_snapshot_and_queue_keep_three_group_a_b_plans(self):
        for label, lead, count in (("A", 1.1, 9), ("B", None, 6)):
            with self.subTest(mode=label):
                studio, ai, fixture = await self.make_studio(lead, label)
                session = studio.current
                snapshot = studio.snapshot()
                replay = snapshot["studio"]["fixed_replay"]
                self.assertEqual(replay["source_kind"], "prepared")
                self.assertEqual(replay["action_group_count"], 3)
                self.assertEqual(replay["type9_count"], count)
                self.assertEqual(replay["timeline"], fixture.plan["segments"][0]["timeline"])
                self.assertEqual(session.variant_info["timed_action_planning"], "prepared_verified_anchors")
                archive = session.out / "replay-source"
                manifest = json.loads((archive / "manifest.json").read_text())
                self.assertEqual(manifest["source_kind"], "prepared")
                self.assertEqual((archive / "sample.pcm").read_bytes(), fixture.pcm)
                self.assertEqual(json.loads((archive / "replay-plan.json").read_text()), fixture.plan)
                source_manifest = json.loads((session.out / "source-manifest.json").read_text())
                for name in ("semantic_replay.py", "semantic_speech.py", "live_session.py", "server.py"):
                    self.assertIn(name, source_manifest)
                session.component.start.assert_not_awaited()
                comment = await self.enqueue(studio, fixture)
                self.assertEqual(comment["plan"], fixture.plan)
                self.assertEqual(comment["replay_fixture_fingerprint"], fixture.fingerprint)
                self.assertEqual(comment["action_group_count"], 3)
                self.assertEqual(len(comment["delivery"][0]["timeline"]), count)
                self.assertEqual(session.work.qsize(), 1)
                ai.public.assert_not_called()
                ai.configured.assert_not_called()
        for guard in self.guards:
            guard.assert_not_called()

    async def test_prepared_worker_reuses_audio_and_local_schedule_without_credentials_tts_or_llm(self):
        for label, lead, count in (("A", 1.1, 9), ("B", None, 6)):
            with self.subTest(mode=label):
                studio, ai, fixture = await self.make_studio(lead, label)
                comment = await self.enqueue(studio, fixture)
                session = studio.current
                session.work.put_nowait(None)
                with patch.object(session, "prepare_audio", wraps=session.prepare_audio) as audio, \
                     patch.object(session, "prepare_timed_actions", wraps=session.prepare_timed_actions) as schedule:
                    await asyncio.wait_for(session.process_answers(), 2)
                audio.assert_awaited_once()
                schedule.assert_awaited_once()
                sender = session.component.send_aligned_expression
                sender.assert_awaited_once()
                self.assertEqual(sender.await_args.args[0], fixture.pcm)
                self.assertEqual(sender.await_args.args[2], fixture.plan["segments"][0]["actions"])
                self.assertEqual(sender.await_args.kwargs["alignment"], fixture.alignment)
                self.assertEqual(sender.await_args.kwargs["ge_lead_seconds"], lead)
                self.assertEqual(sender.await_args.kwargs["action_kind"], "S")
                self.assertNotIn("max_audio_seconds", sender.await_args.kwargs)
                self.assertNotIn("max_text_codepoints", sender.await_args.kwargs)
                self.assertEqual(comment["status"], "sent", comment.get("error"))
                self.assertEqual(comment["plan"], fixture.plan)
                self.assertEqual(len(comment["delivery"][0]["timeline"]), count)
                self.assertEqual(comment["usage"], {"input_tokens": 0, "output_tokens": 0, "cost_cny": 0})
                self.assertEqual(session.totals["audio_seconds"], 29.2)
                self.assertEqual(session.totals["cost_cny"], 0)
                session.component.send_expression.assert_not_awaited()
                ai.plan_timed_actions.assert_not_awaited()
                ai.generate_reply.assert_not_awaited()
                ai.generate_director.assert_not_awaited()
                folder = session.out / "tts" / (comment["id"] + "-1")
                self.assertEqual((folder / "sample.pcm").read_bytes(), fixture.pcm)
                self.assertEqual(json.loads((folder / "alignment.json").read_text()), fixture.alignment)
                planning = json.loads((folder / "timed-action-plan.json").read_text())["planning"]
                self.assertEqual(planning["source_kind"], "prepared")
                self.assertFalse(planning["model_called"])
        for guard in self.guards:
            guard.assert_not_called()

    async def test_minute_new_session_queue_and_worker_use_only_explicit_long_playback_limits(self):
        from semantic_replay import PreparedMinuteSemanticReplay
        from test_semantic_replay import make_minute_preparation
        fixture = PreparedMinuteSemanticReplay(make_minute_preparation(self.root / "minute-source"))
        studio, ai, fixture = await self.make_studio(1.1, "minute-run", fixture=fixture)
        session = studio.current
        self.assertFalse(session.started)
        session.component.start.assert_not_awaited()
        replay = studio.snapshot()["studio"]["fixed_replay"]
        self.assertEqual(replay["source_kind"], "prepared_minute")
        self.assertEqual(replay["comparison_label"], "A · 1分钟六动作")
        self.assertEqual(replay["source_tts_segments"], 2)
        self.assertEqual(replay["type9_count"], 18)
        self.assertEqual(session.variant_info["speech_text_limit"], 240)
        self.assertEqual(session.variant_info["timed_action_planning"], "concatenated_prepared_verified_anchors")
        mode = json.loads((session.out / "source-used/run-mode.json").read_text())
        self.assertEqual((mode["max_audio_seconds"], mode["max_text_codepoints"]), (60, 240))
        self.assertEqual(mode["source_tts_segments"], 2)
        archive = session.out / "replay-source"
        self.assertEqual(json.loads((archive / "concatenation.json").read_text())["tts_calls"], 2)
        comment = await self.enqueue(studio, fixture)
        self.assertEqual(len(comment["plan"]["segments"]), 1)
        self.assertEqual(len(comment["plan"]["segments"][0]["text"]), 180)
        self.assertEqual(comment["action_group_count"], 6)
        session.work.put_nowait(None)
        await asyncio.wait_for(session.process_answers(), 2)
        sender = session.component.send_aligned_expression
        sender.assert_awaited_once()
        self.assertEqual(sender.await_args.args[0], fixture.pcm)
        self.assertEqual(sender.await_args.args[1], fixture.plan["segments"][0]["text"])
        self.assertEqual(sender.await_args.kwargs["max_audio_seconds"], 60)
        self.assertEqual(sender.await_args.kwargs["max_text_codepoints"], 240)
        self.assertEqual(sender.await_args.kwargs["ge_lead_seconds"], 1.1)
        self.assertEqual(sender.await_args.kwargs["action_kind"], "S")
        self.assertIsNone(sender.await_args.kwargs["reading"])
        self.assertEqual(comment["status"], "sent", comment.get("error"))
        self.assertEqual(comment["plan"], fixture.plan)
        self.assertEqual(session.totals["audio_seconds"], 57)
        self.assertEqual(session.totals["cost_cny"], 0)
        ai.plan_timed_actions.assert_not_awaited()
        session.component.send_expression.assert_not_awaited()
        for guard in self.guards:
            guard.assert_not_called()


if __name__ == "__main__":
    unittest.main()
