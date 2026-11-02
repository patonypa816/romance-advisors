"""Operator/topic scheduling through real Studio boundaries, using no cloud."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import test_studio as fixtures
import live_session


def speech(text="咱先说这一句。"):
    return {"emotion": "平静",
            "segments": [{"text": text, "instruction": "自然清晰", "actions": [], "recovery": None}]}


class DirectorScheduling(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.patches = [
            patch.object(live_session, "PersistentComponent", fixtures.FakeComponent),
            patch.object(live_session, "load_bailian", return_value=("offline-key", "offline-workspace")),
            patch.object(live_session, "synthesize_segment", side_effect=AssertionError("real TTS forbidden")),
        ]
        for entry in self.patches:
            entry.start()
        self.ai = fixtures.FakeAI()
        self.ai.generate_reply.side_effect = None
        self.ai.generate_reply.return_value = speech("咱听见你的问题了。")

        async def directed(job, history, context, topic):
            return speech(f"咱讲主线第{topic['next_round']}段。" if topic else "咱按这个意思说一句。")

        self.ai.generate_director = AsyncMock(side_effect=directed)
        self.control = fixtures.studio_server.Studio(self.data, ai=self.ai)
        await self.control.new_session()
        self.a, self.d, self.s = self.control.automation, self.control.director, self.control.current
        self.s.started = True
        self.s.component.state = "ready"
        self.s.prepare_audio = AsyncMock(return_value=(b"\0\0" * 240, self.data))
        self.s.component.send_aligned_expression = AsyncMock(return_value={"group_id": "offline-group"})

    async def asyncTearDown(self):
        await self.a.close()
        await self.control.current.shutdown(None)
        for entry in reversed(self.patches):
            entry.stop()
        self.temp.cleanup()

    def topic(self, rounds=3, text="讲三段当年要饭的经历"):
        return self.d.submit({"text": text, "mode": "topic", "rounds": rounds})

    async def deliver_one(self):
        await self.a.reply_once()
        await asyncio.wait_for(self.s.work.join(), 1)
        self.d.sync()

    async def test_no_topic_or_input_never_generates_content(self):
        self.assertIsNone(self.d.next_item())
        await self.a.reply_once()
        self.ai.generate_director.assert_not_awaited()
        self.ai.generate_reply.assert_not_awaited()

    async def test_three_bounded_rounds_prioritize_all_viewers_then_story(self):
        self.topic()
        one = self.control.enqueue_comment("粮食怎么珍惜", "观众一")
        two = self.control.enqueue_comment("当年最难的是什么", "观众二")
        expected = [("viewer", one["id"]), ("viewer", two["id"]), ("operator", 1),
                    ("operator", 2), ("operator", 3)]
        actual = []
        for _ in expected:
            c = self.d.next_item()
            actual.append((c["input_kind"], c.get("topic_round", c["id"])))
            await self.deliver_one()
        self.assertEqual(actual, expected)
        self.assertEqual(self.d.topic["state"], "completed")
        self.assertEqual(self.d.topic["completed_rounds"], 3)
        self.assertIsNone(self.d.next_item())
        self.assertEqual(self.ai.generate_director.await_count, 3)
        last = self.ai.generate_director.await_args.args[3]
        self.assertEqual(last["sent_history"], ["咱讲主线第1段。", "咱讲主线第2段。"])
        self.assertEqual(last["next_round"], 3)

    async def test_operator_once_precedes_waiting_viewer_and_topic(self):
        self.topic(1)
        viewer = self.control.enqueue_comment("有个问题", "观众")
        result = self.d.submit({"text": "先郑重介绍自己", "mode": "once"})
        self.assertEqual(self.d.next_item()["id"], result["id"])
        await self.deliver_one()
        call = self.ai.generate_director.await_args.args
        self.assertIsNone(call[3])
        self.assertEqual(call[0]["input_kind"], "operator")
        self.ai.generate_reply.assert_not_awaited()
        self.assertEqual(self.d.next_item()["id"], viewer["id"])
        self.assertEqual(viewer["status"], "pending")

    async def test_progress_waits_for_successful_delivery_not_generation(self):
        self.topic(2)
        entered, finish = asyncio.Event(), asyncio.Event()

        async def slow_send(*args, **kwargs):
            entered.set()
            await finish.wait()
            return {}

        self.s.component.send_aligned_expression.side_effect = slow_send
        await self.a.reply_once()
        await asyncio.wait_for(entered.wait(), 1)
        self.d.sync()
        self.assertEqual(self.d.topic["completed_rounds"], 0)
        self.assertEqual(self.d.topic["sent_history"], [])
        future = self.d.next_item()
        self.assertEqual(future["topic_round"], 2)
        self.assertEqual(self.d.model_context(future)["predecessor"]["status"], "conditional_draft")
        finish.set()
        await asyncio.wait_for(self.s.work.join(), 1)
        self.d.sync()
        self.assertEqual(self.d.topic["completed_rounds"], 1)

    async def late_model(self):
        started, finish = asyncio.Event(), asyncio.Event()

        async def late(*args):
            started.set()
            while not finish.is_set():
                try:
                    await finish.wait()
                except asyncio.CancelledError:
                    pass
            return speech("旧主题的迟到内容。")

        self.ai.generate_director.side_effect = late
        task = asyncio.create_task(self.a.reply_once())
        self.a.reply_task = task
        await asyncio.wait_for(started.wait(), 1)
        return task, finish

    async def test_replacement_discards_uncancellable_late_old_topic(self):
        old = self.topic(2)
        task, finish = await self.late_model()
        old_item = self.a.active_item
        new = self.topic(1, "现在聊读书")
        self.assertNotEqual(old, new)
        finish.set()
        await asyncio.wait_for(task, 1)
        self.a.reply_task = None
        self.assertEqual(old_item["status"], "cancelled")
        self.assertTrue(self.s.work.empty())
        self.s.component.send_aligned_expression.assert_not_awaited()
        self.assertEqual(self.a.last_error, "")
        self.assertEqual(self.d.next_item()["text"], "现在聊读书")

    async def test_pause_resume_does_not_accept_old_generation_for_same_round(self):
        self.topic(2)
        task, finish = await self.late_model()
        old_item = self.a.active_item
        self.d.pause()
        self.d.resume()
        finish.set()
        await asyncio.wait_for(task, 1)
        self.a.reply_task = None
        self.assertEqual(old_item["status"], "cancelled")
        self.assertTrue(self.s.work.empty())
        new_item = self.d.next_item()
        self.assertNotEqual(new_item["id"], old_item["id"])
        self.assertEqual(new_item["topic_round"], 1)

    async def test_pause_or_replace_before_scheduled_coroutine_starts_cannot_wedge_queue(self):
        for change in ("pause_resume", "replace"):
            with self.subTest(change=change):
                self.topic(2)
                old_item = self.d.next_item()
                task = asyncio.create_task(self.a.reply_once(old_item))
                self.a.reply_task = task
                # Deliberately do not yield: the scheduled coroutine has not
                # set active_item yet, exactly when the operator can intervene.
                if change == "pause_resume":
                    self.d.pause()
                    self.d.resume()
                else:
                    self.topic(1, "换成读书话题")
                await asyncio.wait_for(task, 1)
                self.a.reply_task = None
                self.assertEqual(old_item["status"], "cancelled")
                self.assertFalse(self.d.delivery_busy())
                self.assertIsNotNone(self.d.next_item())
                self.ai.generate_director.assert_not_awaited()

    async def test_skipped_viewer_is_not_revived_by_scheduled_coroutine(self):
        c = self.control.enqueue_comment("这条不必说", "观众")
        task = asyncio.create_task(self.a.reply_once(c))
        c["status"] = "skipped"
        await asyncio.wait_for(task, 1)
        self.assertEqual(c["status"], "skipped")
        self.ai.generate_reply.assert_not_awaited()

    async def test_pause_during_send_finishes_current_round_without_next(self):
        self.topic(2)
        entered, finish = asyncio.Event(), asyncio.Event()

        async def slow_send(*args, **kwargs):
            entered.set()
            await finish.wait()
            return {}

        self.s.component.send_aligned_expression.side_effect = slow_send
        await self.a.reply_once()
        await asyncio.wait_for(entered.wait(), 1)
        self.d.pause()
        finish.set()
        await asyncio.wait_for(self.s.work.join(), 1)
        self.d.sync()
        self.assertEqual(self.d.topic["state"], "paused")
        self.assertEqual(self.d.topic["completed_rounds"], 1)
        self.assertIsNone(self.d.next_item())
        self.d.resume()
        self.assertEqual(self.d.next_item()["topic_round"], 2)

    async def test_send_failure_pauses_without_counting_or_automatic_replay(self):
        self.topic(2)
        self.s.component.send_aligned_expression.side_effect = RuntimeError("offline partial write")
        await self.deliver_one()
        self.assertEqual(self.d.topic["state"], "error")
        self.assertEqual(self.d.topic["completed_rounds"], 0)
        self.assertIsNone(self.d.next_item())
        with self.assertRaises(ValueError):
            self.d.resume()
        self.assertEqual(self.ai.generate_director.await_count, 1)

    async def test_continue_after_complete_cannot_extend_finite_outline(self):
        self.topic(1)
        await self.deliver_one()
        self.assertIsNone(self.d.next_item())
        with self.assertRaises(ValueError):
            self.d.resume()
        self.assertEqual((self.d.topic["completed_rounds"], self.d.topic["total_rounds"]), (1, 1))
        self.assertEqual(self.d.topic["state"], "completed")

    async def test_topic_pause_is_independent_of_viewer_intake_pause(self):
        self.topic(1)
        self.control.paused = True
        await self.deliver_one()
        self.assertEqual(self.d.topic["state"], "completed")

    async def test_new_session_cannot_replay_old_director_topic(self):
        self.topic(2)
        self.d.submit({"text": "摆一下手", "mode": "motion"})
        await self.s.request_stop("offline stop")
        await self.control.new_session()
        self.assertIsNone(self.d.topic)
        self.assertIsNone(self.d.next_item())
        self.assertEqual(self.control.current.comments, [])

    async def test_selector_receives_topic_and_off_topic_selection_survives_topic_change(self):
        self.topic(1)
        self.a.enabled = True
        import time
        event = {"id": "offline-event", "roomId": "123", "platform": "douyin", "type": "chat",
                 "ts": time.time() * 1000, "receivedAt": time.time() * 1000,
                 "user": {"id": "offline-viewer", "nickname": "观众"}, "data": {"content": "当时吃什么"}}
        await self.a.on_event(event)
        entered, finish = asyncio.Event(), asyncio.Event()
        contexts = []

        async def late(batch, context):
            contexts.append(context)
            entered.set()
            while not finish.is_set():
                try:
                    await finish.wait()
                except asyncio.CancelledError:
                    pass
            return {"selected_id": batch["candidates"][0]["id"], "reason": "旧话题相关"}

        self.ai.select_comments.side_effect = late
        self.a.selection_task = asyncio.create_task(self.a.select_once())
        await asyncio.wait_for(entered.wait(), 1)
        self.topic(1, "现在聊书法")
        finish.set()
        await asyncio.wait_for(self.a.selection_task, 1)
        self.a.selection_task = None
        self.assertEqual(contexts[0]["current_topic"]["instruction"], "讲三段当年要饭的经历")
        self.assertTrue(any(c.get("source") == "dyhub" for c in self.s.comments))

    async def test_pure_motion_uses_director_contract_and_never_tts(self):
        self.ai.generate_director.side_effect = None
        self.ai.generate_director.return_value = {
            "emotion": "平静",
            "actions": [{"kind": "S", "delay_seconds": 0,
                         "motions": [{"part": "head", "description": "轻轻点头一次"}],
                         "hold_seconds": 0, "gaze": "看向镜头", "expression": "神情平静"}],
            "recovery": {"gaze": "看向镜头", "expression": "神情平静"}}
        self.s.component.send_motion_expression = AsyncMock(return_value={"actions_sent": 2, "status": "sent"})
        self.d.submit({"text": "点头，不说话", "mode": "motion"})
        await self.deliver_one()
        c = self.s.comments[0]
        self.assertEqual((c["status"], c["segments_sent"], c["answer"]), ("sent", 0, ""))
        self.s.prepare_audio.assert_not_awaited()
        live_session.load_bailian.assert_not_called()
        self.ai.generate_reply.assert_not_awaited()

    async def test_future_preparation_overlaps_send_and_keeps_one_slot(self):
        self.topic(3)
        entered, finish = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await finish.wait()
            return {}
        self.s.component.send_aligned_expression.side_effect = send
        await self.a.reply_once()
        await asyncio.wait_for(entered.wait(), 1)
        try:
            await self.a.reply_once()
            future = self.s.comments[-1]
            self.assertEqual((future["topic_round"], future["status"]), (2, "prepared"))
            self.assertEqual(self.d.topic["completed_rounds"], 0)
            context = self.ai.generate_director.await_args.args[3]
            self.assertEqual(context["completed_rounds"], 0)
            self.assertEqual(context["sent_history"], [])
            self.assertEqual(context["predecessor"]["status"], "conditional_draft")
            self.assertIsNone(self.d.next_item())
            self.assertEqual(self.s.prepare_audio.await_count, 2)
        finally:
            finish.set()
            await asyncio.wait_for(self.s.work.join(), 1)
        self.a.dispatch_prepared()
        await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(self.d.topic["completed_rounds"], 2)
        self.assertEqual(self.s.prepare_audio.await_count, 2)

    async def test_selected_reply_preparing_blocks_prepared_story_until_interaction_finishes(self):
        self.topic(3)
        await self.deliver_one()
        # Prepare standby content without sending it, then let a viewer arrive.
        with patch.object(self.a, "dispatch_prepared"):
            await self.a.reply_once()
        old_story = self.s.comments[-1]
        viewer = self.control.enqueue_comment("今天想聊别的", "观众")
        started, finish = asyncio.Event(), asyncio.Event()
        async def reply(*args):
            started.set()
            await finish.wait()
            return speech("那咱就顺着这个问题聊。")
        self.ai.generate_reply.side_effect = reply
        task = asyncio.create_task(self.a.reply_once(viewer))
        await asyncio.wait_for(started.wait(), 1)
        try:
            self.a.dispatch_prepared()
            self.assertTrue(self.s.work.empty())
            self.assertEqual(old_story["status"], "prepared")
            self.assertEqual(self.d.topic["completed_rounds"], 1)
        finally:
            finish.set()
            await asyncio.wait_for(task, 1)
            await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(old_story["status"], "cancelled")
        self.assertTrue(self.d.topic["resume_required"])
        self.assertEqual(self.d.topic["completed_rounds"], 1)
        await self.deliver_one()
        context = self.ai.generate_director.await_args.args[3]
        self.assertTrue(context["resume_required"])
        self.assertEqual(context["interaction_history"][-1]["answer"], "那咱就顺着这个问题聊。")
        self.assertFalse(self.d.topic["resume_required"])
        await self.deliver_one()
        self.assertFalse(self.ai.generate_director.await_args.args[3]["resume_required"])

    async def test_screening_candidate_blocks_story_and_topic_stock_does_not_use_intake_quota(self):
        self.topic(2)
        self.a.enabled = True
        self.a.source_status = {"state": "connected", "room_id": "123"}
        import time
        await self.a.on_event({"id": "pending-screen", "roomId": "123", "platform": "douyin", "type": "chat",
            "ts": time.time() * 1000, "receivedAt": time.time() * 1000,
            "user": {"id": "viewer", "nickname": "观众"}, "data": {"content": "问个完全不同的问题"}})
        self.assertIsNone(self.d.next_item())
        self.a.pool.clear("screened_out")
        story = self.d.next_item()
        self.assertEqual(story["topic_round"], 1)
        self.assertEqual(self.a.queue_count(), 0)
        self.assertTrue(self.a.can_select())

    async def test_expired_selected_reply_does_not_create_return_bridge(self):
        import time
        self.topic(2)
        await self.deliver_one()
        c = self.control.enqueue_comment("过时问题", "观众", expires_at=time.time() - 1)
        next_ = self.d.next_item()
        self.assertEqual(c["status"], "expired")
        await self.a.reply_once(next_)
        await asyncio.wait_for(self.s.work.join(), 1)
        self.assertFalse(self.ai.generate_director.await_args.args[3]["resume_required"])

    async def test_future_preparation_error_does_not_interrupt_current_send_or_retry(self):
        self.topic(3)
        entered, finish = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await finish.wait()
            return {}
        self.s.component.send_aligned_expression.side_effect = send
        await self.a.reply_once()
        await asyncio.wait_for(entered.wait(), 1)
        try:
            self.s.prepare_audio.side_effect = RuntimeError("offline next segment TTS failure")
            await self.a.reply_once()
            current, future = self.s.comments
            self.assertEqual(current["status"], "sending")
            self.assertEqual(future["status"], "error")
            self.assertEqual(self.d.topic["error_phase"], "preparation")
            self.assertEqual(self.a.last_error, "")
            await self.a.reply_once()
            self.assertEqual(self.ai.generate_director.await_count, 2)
        finally:
            finish.set()
            await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(self.d.topic["completed_rounds"], 1)
        self.assertEqual(self.d.topic["state"], "error")

    async def test_first_ever_manual_pause_has_no_return_bridge(self):
        self.topic(1)
        self.d.pause()
        self.d.resume()
        await self.deliver_one()
        self.assertFalse(self.ai.generate_director.await_args.args[3]["resume_required"])

    async def test_reply_history_excludes_prepared_drafts(self):
        self.topic(2)
        with patch.object(self.a, "dispatch_prepared"):
            await self.a.reply_once()
        self.assertEqual(self.s.comments[0]["status"], "prepared")
        self.assertEqual(self.control.reply_history(self.s), [])


    async def test_manual_arriving_after_prepared_viewer_does_not_deadlock(self):
        self.topic(1)
        viewer = self.control.enqueue_comment("观众问题", "观众")
        with patch.object(self.a, "dispatch_prepared"):
            await self.a.reply_once()
        self.assertEqual(viewer["status"], "prepared")
        result = self.d.submit({"text": "先说这句主持安排", "mode": "once"})
        manual = self.d.next_item()
        self.assertEqual(manual["id"], result["id"])
        await self.a.reply_once(manual)
        await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(manual["status"], "sent")
        self.assertEqual(viewer["status"], "prepared")
        self.a.dispatch_prepared()
        await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(viewer["status"], "sent")
        self.assertEqual(self.d.topic["completed_rounds"], 0)

    async def test_pause_during_bridge_send_keeps_new_manual_return_intent(self):
        self.topic(3)
        await self.deliver_one()
        self.control.enqueue_comment("插个话题", "观众")
        await self.deliver_one()
        self.assertTrue(self.d.topic["resume_required"])
        entered, finish = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await finish.wait()
            return {}
        self.s.component.send_aligned_expression.side_effect = send
        await self.a.reply_once()
        await asyncio.wait_for(entered.wait(), 1)
        try:
            self.assertTrue(self.s.comments[-1]["resume_required"])
            self.d.pause()
        finally:
            finish.set()
            await asyncio.wait_for(self.s.work.join(), 1)
        self.assertTrue(self.d.topic["resume_required"])
        self.d.resume()
        self.assertTrue(self.d.next_item()["resume_required"])

    async def test_real_model_wrapper_prefetch_during_bridge_uses_item_resume_flag_once(self):
        from bailian_ai import BailianAI
        brain = BailianAI({})
        requests, spoken = [], []
        bridge_text = "好，咱们接着说。接下来看他怎样应对眼前的难题。"
        final_text = "他把眼前的事情一步步办好，这段经历就讲到这里。"
        entered, finish = asyncio.Event(), asyncio.Event()

        async def model_call(role, messages):
            data = json.loads(messages[1]["content"])
            state = data["topic"]
            requests.append({"messages": copy.deepcopy(messages), "topic": state,
                             "global_resume_required": self.d.topic["resume_required"]})
            self.assertEqual(role, "reply")
            text = bridge_text if state["resume_required"] else final_text if state["next_round"] == 3 else "先把这段经历的来龙去脉说清楚。"
            return speech(text)

        async def send(pcm, text, *args, **kwargs):
            spoken.append(text)
            if text == bridge_text:
                entered.set()
                await finish.wait()
            return {}

        # Keep real request construction and strict parsing; only the external
        # model call, TTS and component wire are faked by this offline fixture.
        brain._call = AsyncMock(side_effect=model_call)
        self.ai.generate_director = brain.generate_director
        self.s.component.send_aligned_expression.side_effect = send
        self.topic(3)
        await self.deliver_one()
        viewer = self.control.enqueue_comment("插一句，今天想聊别的", "观众")
        await self.deliver_one()
        self.assertEqual(viewer["status"], "sent")
        self.assertEqual(self.d.topic["completed_rounds"], 1)
        self.assertTrue(self.d.topic["resume_required"])
        await self.a.reply_once()
        await asyncio.wait_for(entered.wait(), 1)
        bridge = self.s.comments[-1]
        try:
            self.assertEqual(bridge["status"], "sending")
            self.assertTrue(bridge["resume_required"])
            self.assertTrue(self.d.topic["resume_required"])
            self.assertEqual(self.d.topic["completed_rounds"], 1)
            await self.a.reply_once()
            future = self.s.comments[-1]
            self.assertEqual((future["topic_round"], future["status"]), (3, "prepared"))
            captured = requests[-1]
            self.assertTrue(captured["global_resume_required"])
            self.assertFalse(captured["topic"]["resume_required"])
            self.assertEqual(captured["topic"]["completed_rounds"], 1)
            self.assertEqual(captured["topic"]["sent_history"], [spoken[0]])
            self.assertEqual(captured["topic"]["predecessor"], {
                "id": bridge["id"], "round": 2, "text": bridge_text, "status": "conditional_draft"})
            trusted = json.loads(captured["messages"][0]["content"].split("\n可信角色与参考规划：\n", 1)[1])
            # This exact request assertion catches the original double-context
            # bug even though the deterministic model stub returns good prose.
            self.assertNotIn("current_topic", trusted)
            self.assertNotIn('"resume_required":true', json.dumps(trusted, ensure_ascii=False, separators=(",", ":")))
            self.assertEqual(self.d.topic["completed_rounds"], 1)
            self.assertTrue(self.d.topic["resume_required"])
            self.assertEqual(len(spoken), 3)
        finally:
            finish.set()
            await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(self.d.topic["completed_rounds"], 2)
        self.assertFalse(self.d.topic["resume_required"])
        self.a.dispatch_prepared()
        await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(spoken, ["先把这段经历的来龙去脉说清楚。", "咱听见你的问题了。", bridge_text, final_text])
        self.assertEqual(self.d.topic["completed_rounds"], 3)
        self.assertEqual(self.d.topic["state"], "completed")
        self.assertEqual(brain._call.await_count, 3)

    async def test_unprepared_topic_and_motion_history_are_valid_model_context(self):
        from cloudbase_ai import _topic_context
        self.topic(2)
        self.assertNotIn("outline", self.d.context())
        _topic_context(self.d.context())
        self.s.comments.extend([
            {"id": "motion-history", "text": "点头", "answer": "", "status": "sent", "operator_mode": "motion"},
            {"id": "manual-history", "text": "主持输入" * 120, "answer": "实际已经说出的台词", "status": "sent", "operator_mode": "once"}])
        item = self.d.next_item()
        self.d.topic["outline"] = [{"id": "one", "title": "起", "goal": "开端"}, {"id": "two", "title": "终", "goal": "结尾"}]
        context = self.d.model_context(item)
        self.assertEqual(len(context["interaction_history"]), 1)
        _topic_context(context)

    async def test_real_loop_overlaps_future_and_drains_all_viewers_before_one_return(self):
        self.topic(2)
        entered, finish = asyncio.Event(), asyncio.Event()
        spoken = []
        async def send(pcm, text, *args, **kwargs):
            spoken.append(text)
            if len(spoken) == 1:
                entered.set()
                await finish.wait()
            return {}
        async def until(predicate):
            async with asyncio.timeout(3):
                while not predicate():
                    await asyncio.sleep(.01)
        self.s.component.send_aligned_expression.side_effect = send
        self.a.start()
        await asyncio.wait_for(entered.wait(), 1)
        try:
            await until(lambda: len(self.s.comments) == 2 and self.s.comments[1]["status"] == "prepared")
            self.assertEqual(self.d.topic["completed_rounds"], 0)
            one = self.control.enqueue_comment("第一个观众问题", "观众一")
            two = self.control.enqueue_comment("第二个观众问题", "观众二")
            await until(lambda: one["status"] == "prepared")
            self.assertEqual(two["status"], "pending")
        finally:
            finish.set()
        await until(lambda: self.d.topic["state"] == "completed")
        self.assertEqual(spoken, ["咱讲主线第1段。", "咱听见你的问题了。", "咱听见你的问题了。", "咱讲主线第2段。"])
        self.assertEqual((one["status"], two["status"]), ("sent", "sent"))
        contexts = [call.args[3] for call in self.ai.generate_director.await_args_list]
        self.assertEqual([context["resume_required"] for context in contexts], [False, False, True])
        self.assertEqual(self.d.topic["completed_rounds"], 2)

    async def test_error_latch_blocks_late_prepared_content_and_final_send_gate(self):
        self.topic(1)
        with patch.object(self.a, "dispatch_prepared"):
            await self.a.reply_once()
        prepared = self.s.comments[0]
        self.assertEqual(prepared["status"], "prepared")
        self.a.fail("delivery_failed", "sending")
        self.a.dispatch_prepared()
        self.assertTrue(self.s.work.empty())
        # Even an item queued just before the latch must pass the final gate.
        self.s.work.put_nowait(prepared)
        await asyncio.wait_for(self.s.work.join(), 1)
        self.s.component.send_aligned_expression.assert_not_awaited()
        self.assertEqual(self.d.topic["completed_rounds"], 0)
        self.assertEqual(prepared["status"], "prepared")

    async def test_stop_card_distinguishes_sending_segment_from_finished_stop(self):
        self.topic(2)
        entered, finish = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await finish.wait()
            return {}
        self.s.component.send_aligned_expression.side_effect = send
        await self.a.reply_once()
        await asyncio.wait_for(entered.wait(), 1)
        try:
            self.d.stop()
            self.assertEqual(self.d.public()["topic"]["view_state"], "本段结束后停止")
        finally:
            finish.set()
            await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(self.d.public()["topic"]["view_state"], "已停止")
        self.assertEqual(self.d.topic["completed_rounds"], 1)
        self.assertIsNone(self.d.next_item())

if __name__ == "__main__":
    unittest.main()
