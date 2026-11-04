"""实际候选池/Studio/自动循环的离线合同；模型、TTS和渲染均用假实现。"""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import test_studio as fixtures
import live_session
from cloudbase_ai import ModelError


def plan():
    return {"emotion": "平静", "reading": None,
            "segments": [{"text": "咱听着呢。", "instruction": "自然清晰", "actions": [], "recovery": None}]}


class AutomationContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name)
        self.patches = [
            patch.object(live_session, "PersistentComponent", fixtures.FakeComponent),
            patch.object(live_session, "load_bailian", side_effect=AssertionError("unexpected TTS credentials")),
            patch.object(live_session, "load_vidu", side_effect=AssertionError("unexpected Vidu credentials")),
            patch.object(live_session, "synthesize_segment", side_effect=AssertionError("unexpected TTS request")),
        ]
        for entry in self.patches:
            entry.start()
        self.ai = fixtures.FakeAI()
        self.control = fixtures.studio_server.Studio(self.data, ai=self.ai)
        await self.control.new_session()
        self.a = self.control.automation
        self.a.enabled = True
        self.a.source_status = {"state": "connected", "room_id": "123", "error_code": None}
        self.control.current.started = True
        self.control.current.component.state = "ready"
        self.ai.select_comments.side_effect = None
        self.ai.select_comments.return_value = {"selected_id": None, "reason": "没有适合的评论"}
        self.ai.generate_reply.side_effect = None
        self.ai.generate_reply.return_value = plan()

    async def asyncTearDown(self):
        await self.a.close()
        await self.control.current.shutdown(None)
        for entry in reversed(self.patches):
            entry.stop()
        self.temporary.cleanup()

    def event(self, number=1):
        return {"id": str(number), "roomId": "123", "platform": "douyin", "type": "chat",
                "ts": time.time() * 1000, "receivedAt": time.time() * 1000,
                "user": {"id": "private-viewer", "nickname": "观众"},
                "data": {"content": f"问题{number}"}}

    def select_first(self):
        async def choose(batch, context):
            return {"selected_id": batch["candidates"][0]["id"], "reason": "明确提问"}
        self.ai.select_comments.side_effect = choose

    async def test_unstarted_or_paused_live_discards_without_buffering_or_model(self):
        self.control.current.started = False
        await self.a.on_event(self.event())
        await self.a.select_once()
        await self.a.reply_once()
        self.control.current.started = True
        self.control.paused = True
        await self.a.on_event(self.event(2))
        await self.a.select_once()
        self.assertEqual(self.a.pool.snapshot()["candidate_count"], 0)
        self.assertEqual(self.a.discarded_count, 2)
        self.ai.select_comments.assert_not_awaited()
        self.ai.generate_reply.assert_not_awaited()

    async def test_batch_selects_only_one_and_unselected_never_enter_reply_queue(self):
        self.select_first()
        for number in range(3):
            await self.a.on_event(self.event(number))
        await self.a.select_once()
        self.assertEqual(len(self.control.current.comments), 1)
        self.assertEqual(self.control.current.comments[0]["source"], "dyhub")
        self.assertEqual(self.a.pool.snapshot()["counts"]["not_selected"], 2)
        self.assertEqual(self.a.pool.snapshot()["counts"]["enqueued"], 1)
        self.ai.generate_reply.assert_not_awaited()

    async def test_null_selection_never_enqueues(self):
        await self.a.on_event(self.event())
        await self.a.select_once()
        self.assertEqual(self.control.current.comments, [])
        self.assertEqual(self.a.pool.snapshot()["counts"]["not_selected"], 1)

    async def finish_scheduled_selection(self):
        task = self.a.selection_task
        self.assertIsNotNone(task)
        await asyncio.wait_for(task, 1)
        self.a.selection_task = None

    async def test_idle_candidate_is_selected_immediately_as_one_batch(self):
        with patch("automation.time", wraps=time) as clock:
            clock.monotonic.return_value = 100
            for number in range(2):
                await self.a.on_event(self.event(number))
            self.a.schedule_selection(100)
            await self.finish_scheduled_selection()
        batch = self.ai.select_comments.await_args.args[0]
        self.assertEqual([c["text"] for c in batch["candidates"]], ["问题0", "问题1"])
        self.ai.select_comments.assert_awaited_once()

    async def test_empty_checks_do_not_delay_first_candidate_or_rate_limit(self):
        with patch("automation.time", wraps=time) as clock:
            clock.monotonic.return_value = 100
            self.a.schedule_selection(100)
            self.ai.select_comments.assert_not_awaited()
            self.assertIsNone(self.a.selection_task)
            clock.monotonic.return_value = 100.2
            await self.a.on_event(self.event())
            self.a.schedule_selection(100.2)
            await self.finish_scheduled_selection()
            await self.a.on_event(self.event(2))
            clock.monotonic.return_value = 100.9
            self.a.schedule_selection(100.9)
            self.assertIsNone(self.a.selection_task)
            clock.monotonic.return_value = 101.2
            self.a.schedule_selection(101.2)
            await self.finish_scheduled_selection()
        self.assertEqual(self.ai.select_comments.await_count, 2)

    async def test_rate_limit_starts_when_request_runs_not_when_task_is_scheduled(self):
        with patch("automation.time", wraps=time) as clock:
            clock.monotonic.return_value = 100
            await self.a.on_event(self.event())
            self.a.schedule_selection(100)
            # 模拟事件循环繁忙：任务排上了，但模型请求十秒后才实际开始。
            clock.monotonic.return_value = 110
            await self.finish_scheduled_selection()
            await self.a.on_event(self.event(2))
            clock.monotonic.return_value = 110.999
            self.a.schedule_selection(110.999)
            self.assertIsNone(self.a.selection_task)
            self.ai.select_comments.assert_awaited_once()
            clock.monotonic.return_value = 111
            self.a.schedule_selection(111)
            await self.finish_scheduled_selection()
        self.assertEqual(self.ai.select_comments.await_count, 2)

    async def test_full_queue_collects_and_selects_when_capacity_returns(self):
        first = self.control.enqueue_comment("手工问题1", "观众")
        self.control.enqueue_comment("手工问题2", "观众")
        with patch("automation.time", wraps=time) as clock:
            clock.monotonic.return_value = 100
            await self.a.on_event(self.event())
            self.a.schedule_selection(100)
            clock.monotonic.return_value = 100.5
            self.a.schedule_selection(100.5)
            self.ai.select_comments.assert_not_awaited()
            self.assertEqual(self.a.pool.snapshot()["candidate_count"], 1)
            first["status"] = "sent"
            clock.monotonic.return_value = 100.6
            self.a.schedule_selection(100.6)
            await self.finish_scheduled_selection()
        self.ai.select_comments.assert_awaited_once()

    async def test_busy_selection_collects_without_consuming_future_start_time(self):
        started, released = asyncio.Event(), asyncio.Event()

        async def delayed_selection(batch, context):
            started.set()
            await released.wait()
            return {"selected_id": None, "reason": "跳过本批"}

        self.ai.select_comments.side_effect = delayed_selection
        with patch("automation.time", wraps=time) as clock:
            clock.monotonic.return_value = 100
            await self.a.on_event(self.event())
            self.a.schedule_selection(100)
            await asyncio.wait_for(started.wait(), 1)
            first_task = self.a.selection_task
            try:
                clock.monotonic.return_value = 102.4
                await asyncio.wait_for(self.a.on_event(self.event(2)), 1)
                self.a.schedule_selection(102.4)
                self.assertIs(self.a.selection_task, first_task)
                self.assertEqual(self.a.pool.snapshot()["candidate_count"], 1)
                self.assertEqual(self.a.pool.snapshot()["selecting_count"], 1)
                self.ai.select_comments.assert_awaited_once()
            finally:
                released.set()
                await self.finish_scheduled_selection()
            # 第一请求早已超过一秒；完成后立即处理积压，不再等整段间隔。
            self.ai.select_comments.side_effect = None
            self.a.schedule_selection(102.4)
            await self.finish_scheduled_selection()
        self.assertEqual(self.ai.select_comments.await_count, 2)

    async def test_recovery_keeps_rate_limit_from_actual_previous_request(self):
        class OfflineSource:
            def __init__(self, *args):
                pass

            async def run(self, stop):
                await stop.wait()

            async def close(self):
                pass

        self.a.source_factory = OfflineSource
        for index, change in enumerate(("pause", "reconnect", "error", "unready")):
            with self.subTest(change=change), patch("automation.time", wraps=time) as clock:
                base = 100 + index * 10
                clock.monotonic.return_value = base
                await self.a.on_event(self.event(index * 10))
                self.a.schedule_selection(base)
                await self.finish_scheduled_selection()
                before = self.ai.select_comments.await_count
                clock.monotonic.return_value = base + .2
                if change == "pause":
                    self.control.paused = True
                    self.a.invalidate("intake_pause_changed")
                elif change == "reconnect":
                    await self.a.disconnect()
                    await self.a.connect()
                elif change == "error":
                    self.a.fail("offline_error", "selection")
                else:
                    self.control.current.component.state = "connecting"
                self.a.schedule_selection(base + .2)
                self.assertIsNone(self.a.selection_task)
                self.control.paused = False
                self.control.current.component.state = "ready"
                self.a.source_status = {"state": "connected", "room_id": "123"}
                if change == "error":
                    self.a.resume()
                clock.monotonic.return_value = base + .3
                await self.a.on_event(self.event(index * 10 + 1))
                self.a.schedule_selection(base + .3)
                self.assertIsNone(self.a.selection_task)
                self.assertEqual(self.ai.select_comments.await_count, before)
                clock.monotonic.return_value = base + 1
                self.a.schedule_selection(base + 1)
                await self.finish_scheduled_selection()
        self.assertEqual(self.ai.select_comments.await_count, 8)

    async def test_late_tick_starts_once_and_limits_from_that_actual_start(self):
        with patch("automation.time", wraps=time) as clock:
            clock.monotonic.return_value = 100
            await self.a.on_event(self.event())
            self.a.schedule_selection(100)
            await self.finish_scheduled_selection()
            clock.monotonic.return_value = 119
            await self.a.on_event(self.event(2))
            self.a.schedule_selection(119)
            await self.finish_scheduled_selection()
            await self.a.on_event(self.event(3))
            clock.monotonic.return_value = 119.999
            self.a.schedule_selection(119.999)
            self.assertIsNone(self.a.selection_task)
            self.assertEqual(self.ai.select_comments.await_count, 2)
            clock.monotonic.return_value = 120
            self.a.schedule_selection(120)
            await self.finish_scheduled_selection()
        self.assertEqual(self.ai.select_comments.await_count, 3)

    async def test_duplicate_and_invalid_events_do_not_request_selection(self):
        original = self.event()
        with patch("automation.time", wraps=time) as clock:
            clock.monotonic.return_value = 100
            await self.a.on_event(original)
            self.a.schedule_selection(100)
            await self.finish_scheduled_selection()
            for invalid in (original, None, {}, {**self.event(2), "type": "member"},
                            {**self.event(3), "data": {"content": ""}}):
                await self.a.on_event(invalid)
            clock.monotonic.return_value = 102
            self.a.schedule_selection(102)
            self.assertIsNone(self.a.selection_task)
        self.ai.select_comments.assert_awaited_once()
        self.assertEqual(self.a.pool.snapshot()["candidate_count"], 0)

    async def test_continuous_events_keep_one_second_between_request_starts(self):
        request_times = []
        with patch("automation.time", wraps=time) as clock:
            async def record_start(batch, context):
                request_times.append(clock.monotonic())
                return {"selected_id": None, "reason": "跳过本批"}

            self.ai.select_comments.side_effect = record_start
            for tick in range(31):
                now = 100 + tick / 10
                clock.monotonic.return_value = now
                await self.a.on_event(self.event(tick))
                self.a.schedule_selection(now)
                if self.a.selection_task:
                    await self.finish_scheduled_selection()
        self.assertEqual(request_times, [100, 101, 102, 103])
        self.assertTrue(all(b - a >= 1 for a, b in zip(request_times, request_times[1:])))

    async def test_running_loop_selects_arrival_without_old_five_second_wait(self):
        idle_checked, requested = asyncio.Event(), asyncio.Event()
        original_schedule = self.a.schedule_selection

        def observe_idle(now):
            original_schedule(now)
            idle_checked.set()

        async def observe_request(batch, context):
            requested.set()
            return {"selected_id": None, "reason": "跳过本批"}

        self.ai.select_comments.side_effect = observe_request
        with patch("automation.MAINTENANCE_INTERVAL_SECONDS", 10), \
                patch.object(self.a, "schedule_selection", side_effect=observe_idle), \
                patch.object(self.a, "dispatch_prepared"), \
                patch.object(self.control.director, "next_item", return_value=None):
            self.a.start()
            try:
                await asyncio.wait_for(idle_checked.wait(), 1)
                await self.a.on_event(self.event())
                await asyncio.wait_for(requested.wait(), 1)
                self.ai.select_comments.assert_awaited_once()
            finally:
                await self.a.close()

    async def test_running_loop_receives_while_busy_and_drains_without_new_event(self):
        first_started, second_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def controlled_selection(batch, context):
            if not first_started.is_set():
                first_started.set()
                await release.wait()
            else:
                second_started.set()
            return {"selected_id": None, "reason": "跳过本批"}

        self.ai.select_comments.side_effect = controlled_selection
        with patch("automation.MAINTENANCE_INTERVAL_SECONDS", 10), \
                patch.object(self.a, "dispatch_prepared"), \
                patch.object(self.control.director, "next_item", return_value=None):
            self.a.start()
            try:
                await self.a.on_event(self.event())
                await asyncio.wait_for(first_started.wait(), 1)
                await asyncio.wait_for(self.a.on_event(self.event(2)), 1)
                self.assertEqual(self.a.pool.snapshot()["candidate_count"], 1)
                self.assertEqual(self.a.pool.snapshot()["selecting_count"], 1)
                self.ai.select_comments.assert_awaited_once()
                # 超过限频后仍保持模型繁忙；完成唤醒须能打断十秒维护等待。
                await asyncio.sleep(1.05)
                release.set()
                await asyncio.wait_for(second_started.wait(), 1)
                self.assertEqual(self.ai.select_comments.await_count, 2)
            finally:
                release.set()
                await self.a.close()

    async def test_running_loop_wakes_at_rate_limit_after_early_completion(self):
        first_started, first_completed = asyncio.Event(), asyncio.Event()
        second_started, release = asyncio.Event(), asyncio.Event()
        request_times = []

        async def controlled_selection(batch, context):
            request_times.append(time.monotonic())
            if len(request_times) == 1:
                first_started.set()
                await release.wait()
                first_completed.set()
            else:
                second_started.set()
            return {"selected_id": None, "reason": "跳过本批"}

        self.ai.select_comments.side_effect = controlled_selection
        with patch("automation.MAINTENANCE_INTERVAL_SECONDS", 10), \
                patch.object(self.a, "dispatch_prepared"), \
                patch.object(self.control.director, "next_item", return_value=None):
            self.a.start()
            try:
                await self.a.on_event(self.event())
                await asyncio.wait_for(first_started.wait(), 1)
                await self.a.on_event(self.event(2))
                release.set()
                await asyncio.wait_for(first_completed.wait(), 1)
                self.assertLess(time.monotonic() - request_times[0], 1)
                self.assertFalse(second_started.is_set())
                # 完成事件早于限频截止；此后没有新事件，也必须在截止时自行续筛。
                await asyncio.wait_for(second_started.wait(), 2)
                self.assertEqual(self.ai.select_comments.await_count, 2)
                self.assertEqual(len(request_times), 2)
                self.assertGreaterEqual(request_times[1] - request_times[0], 1)
            finally:
                release.set()
                await self.a.close()

    async def test_running_loop_notices_queue_capacity_without_new_comment(self):
        first = self.control.enqueue_comment("手工问题1", "观众")
        self.control.enqueue_comment("手工问题2", "观众")
        checked, requested = asyncio.Event(), asyncio.Event()
        original_schedule = self.a.schedule_selection

        def observe_check(now):
            original_schedule(now)
            checked.set()

        async def observe_request(batch, context):
            requested.set()
            return {"selected_id": None, "reason": "跳过本批"}

        self.ai.select_comments.side_effect = observe_request
        with patch.object(self.a, "schedule_selection", side_effect=observe_check), \
                patch.object(self.a, "dispatch_prepared"), \
                patch.object(self.control.director, "next_item", return_value=None):
            await self.a.on_event(self.event())
            self.a.start()
            try:
                await asyncio.wait_for(checked.wait(), 1)
                self.ai.select_comments.assert_not_awaited()
                first["status"] = "sent"
                await asyncio.wait_for(requested.wait(), 1)
                self.ai.select_comments.assert_awaited_once()
            finally:
                await self.a.close()

    async def delayed(self, role):
        """模拟提供商忽略本地取消，稍后仍返回成功；不是只测试可取消的假调用。"""
        started, released = asyncio.Event(), asyncio.Event()

        async def late(*args):
            started.set()
            while not released.is_set():
                try:
                    await released.wait()
                except asyncio.CancelledError:
                    continue
            if role == "selection":
                return {"selected_id": args[0]["candidates"][0]["id"], "reason": "迟到结果"}
            return plan()

        method = self.ai.select_comments if role == "selection" else self.ai.generate_reply
        method.side_effect = late
        task = asyncio.create_task(self.a.select_once() if role == "selection" else self.a.reply_once())
        setattr(self.a, "selection_task" if role == "selection" else "reply_task", task)
        await asyncio.wait_for(started.wait(), 1)
        return task, released

    async def test_late_selection_after_pause_disconnect_stop_or_new_session_is_discarded(self):
        for index, change in enumerate(("pause", "disconnect", "stop", "new")):
            with self.subTest(change=change):
                self.control.paused = False
                self.a.enabled = True
                self.control.current.started = True
                self.control.current.component.state = "ready"
                await self.a.on_event(self.event(index))
                task, released = await self.delayed("selection")
                if change == "pause":
                    self.control.paused = True
                    self.a.invalidate("intake_pause_changed")
                elif change == "disconnect":
                    await self.a.disconnect()
                else:
                    self.a.invalidate("operator_stop", replies=True)
                    await self.control.current.request_stop("offline stop")
                    if change == "new":
                        await self.control.new_session()
                released.set()
                await asyncio.wait_for(task, 1)
                self.a.selection_task = None
                self.assertEqual(self.control.current.comments, [])
                self.assertTrue(self.control.current.work.empty())
                self.assertNotIn("enqueued", self.a.pool.snapshot()["counts"])
                # 下一子场景使用独立空闲场，避免复活已停止对象。
                if change == "stop":
                    await self.control.new_session()

    async def test_reconnect_waits_for_disconnect_and_old_callbacks_are_fenced(self):
        sources = []
        class ControlledSource:
            def __init__(self, base_url, room_id, on_event, on_status):
                self.on_event, self.on_status = on_event, on_status
                self.closing = asyncio.Event()
                self.release = asyncio.Event()
                if sources:
                    self.release.set()
                sources.append(self)

            async def run(self, stop):
                await stop.wait()

            async def close(self):
                self.closing.set()
                await self.release.wait()

        self.a.enabled = False
        self.a.source_factory = ControlledSource
        await self.a.connect()
        old = sources[0]
        disconnect = asyncio.create_task(self.a.disconnect())
        await asyncio.wait_for(old.closing.wait(), 1)
        reconnect = asyncio.create_task(self.a.connect())
        try:
            # A new subscription must wait until the old connection is closed.
            await asyncio.sleep(.01)
            self.assertEqual(len(sources), 1)
            self.assertFalse(reconnect.done())
            old.release.set()
            await asyncio.wait_for(asyncio.gather(disconnect, reconnect), 1)
            self.assertEqual(len(sources), 2)
            self.assertTrue(self.a.enabled)
            self.assertIs(self.a.source, sources[1])
            self.assertFalse(self.a.source_task.done())
            await sources[1].on_event(self.event(42))
            self.assertEqual(self.a.pool.snapshot()["candidate_count"], 1)
            version = self.a.pool.version
            # Late delivery and final stopped notification from the retired
            # subscription cannot contaminate or clear the new source's pool.
            await old.on_event(self.event(43))
            old.on_status({"state": "stopped", "room_id": "123", "error_code": None})
            self.assertEqual(self.a.pool.snapshot()["candidate_count"], 1)
            self.assertEqual(self.a.pool.version, version)
        finally:
            for source in sources:
                source.release.set()
            for task in (disconnect, reconnect):
                if not task.done():
                    task.cancel()
            await asyncio.gather(disconnect, reconnect, return_exceptions=True)

    async def test_two_in_flight_comments_apply_backpressure(self):
        self.select_first()
        first = self.control.enqueue_comment("手工问题1", "观众")
        self.control.enqueue_comment("手工问题2", "观众")
        await self.a.on_event(self.event())
        await self.a.select_once()
        self.ai.select_comments.assert_not_awaited()
        first["status"] = "sent"
        await self.a.select_once()
        self.ai.select_comments.assert_awaited_once()
        self.assertEqual(self.a.queue_count(), 2)

    async def test_late_reply_after_stop_never_reaches_tts(self):
        comment = self.control.enqueue_comment("手工问题", "观众")
        task, released = await self.delayed("reply")
        self.a.invalidate("operator_stop", replies=True)
        await self.control.current.request_stop("offline stop")
        released.set()
        await asyncio.wait_for(task, 1)
        self.assertEqual(comment["status"], "cancelled")
        self.assertTrue(self.control.current.work.empty())
        live_session.synthesize_segment.assert_not_called()

    async def test_selection_failure_consumes_only_its_batch_and_accepts_new_comments(self):
        self.ai.select_comments.side_effect = ModelError("provider_unavailable")
        await self.a.on_event(self.event())
        await self.a.select_once()
        await self.a.select_once()
        self.ai.select_comments.assert_awaited_once()  # 原批不会自动重试。
        self.assertEqual(self.a.last_error, "")
        self.assertIsNone(self.a.error_since)
        self.ai.select_comments.side_effect = None
        self.select_first()
        await self.a.on_event(self.event(2))
        await self.a.select_once()
        self.assertEqual(self.ai.select_comments.await_count, 2)
        self.assertEqual(self.control.current.comments[0]["text"], "问题2")

    async def test_reply_failure_ends_only_one_comment_and_keeps_next_pending_item(self):
        comment = self.control.enqueue_comment("手工问题", "观众")
        following = self.control.enqueue_comment("后来的问题", "另一观众")
        self.ai.generate_reply.side_effect = ModelError("reply_schema_invalid")
        await self.a.reply_once(comment)
        await self.a.reply_once(comment)
        self.ai.generate_reply.assert_awaited_once()
        self.assertEqual(comment["status"], "error")
        self.assertEqual(following["status"], "pending")
        self.assertEqual(self.a.last_error, "")
        self.assertIsNone(self.a.error_since)
        self.ai.generate_reply.side_effect = None
        with patch.object(self.control.current, "prepare_comment", new=AsyncMock(return_value=False)):
            await self.a.reply_once(following)
        self.assertEqual(self.ai.generate_reply.await_count, 2)

    async def test_preparation_failure_does_not_cancel_other_interactions(self):
        failed = self.control.enqueue_comment("谢谢", "观众", input_kind="interaction")
        following = self.control.enqueue_comment("欢迎", "观众", input_kind="interaction")
        failed["status"] = "error"
        self.a.preparation_failed(failed, "tts_failed")
        self.assertEqual(following["status"], "pending")
        self.assertEqual(self.a.last_error, "")
        self.assertIsNone(self.a.error_since)

    async def test_plan_rejection_is_recorded_per_comment_without_retry_or_sensitive_values(self):
        comment = self.control.enqueue_comment("你好", "观众")
        secret = "synthetic-diagnostic-secret-12345"
        self.control.current.diagnostics.register_secret(secret)
        error = ModelError("plan_invalid")
        error.validation = {
            "stage": "parse_plan", "reason": f"缺少instruction，{secret}",
            "response_shape": {"segments": {"type": "list", "count": 1}},
            "raw_output": secret,
        }
        self.ai.generate_reply.side_effect = error
        await self.a.reply_once()
        await self.a.reply_once()
        self.ai.generate_reply.assert_awaited_once()
        self.assertIn("缺少instruction", comment["error"])
        self.assertEqual(comment["plan_error"]["stage"], "parse_plan")
        self.assertTrue(self.control.current.work.empty())
        live_session.synthesize_segment.assert_not_called()
        out = self.control.current.out
        saved = json.loads((out / "conversation.json").read_text())["comments"][0]
        self.assertEqual(saved["plan_error"], comment["plan_error"])
        events = [json.loads(line) for line in (out / "events.jsonl").read_text().splitlines()]
        rejected = next(row for row in events if row["event"] == "llm_plan_rejected")
        self.assertEqual(rejected["comment_id"], comment["id"])
        self.assertEqual(rejected["reason"], comment["plan_error"]["reason"])
        for name in ("conversation.json", "events.jsonl", "diagnostics.jsonl"):
            self.assertNotIn(secret, (out / name).read_text())
            self.assertNotIn("raw_output", (out / name).read_text())
        # A later transport error must not inherit this comment's validation.
        self.a.resume()
        later = self.control.enqueue_comment("另一条", "观众")
        self.ai.generate_reply.side_effect = ModelError("timeout")
        await self.a.reply_once()
        self.assertNotIn("plan_error", later)
        self.assertIn("timeout", later["error"])
        self.assertEqual(comment["plan_error"], saved["plan_error"])

    async def test_accept_plan_rejection_keeps_local_validator_reason(self):
        comment = self.control.enqueue_comment("你好", "观众")
        invalid = plan()
        del invalid["segments"][0]["instruction"]
        self.ai.generate_reply.return_value = invalid
        await self.a.reply_once()
        self.assertEqual(comment["status"], "error")
        self.assertEqual(comment["plan_error"]["stage"], "accept_plan")
        self.assertIn("instruction", comment["error"])
        self.assertTrue(self.control.current.work.empty())

    async def test_reply_expired_while_model_waited_never_accepts_plan(self):
        comment = self.control.enqueue_comment("快过期的问题", "观众", expires_at=time.time() + 10)

        async def expired_reply(*args):
            comment["expires_at"] = time.time() - 1
            return plan()

        self.ai.generate_reply.side_effect = expired_reply
        with patch.object(self.control, "accept_plan", wraps=self.control.accept_plan) as accepted:
            await self.a.reply_once()
        accepted.assert_not_called()
        self.assertEqual(comment["status"], "expired")
        self.assertTrue(self.control.current.work.empty())

    def ready_comment(self, **metadata):
        comment = self.control.enqueue_comment("手工问题", "观众", **metadata)
        comment["status"] = "processing"
        with patch.object(self.control.current.work, "put_nowait"):
            self.control.accept_plan(self.control.current, comment, plan())
        return comment

    async def test_expired_speech_queue_item_does_not_start_tts(self):
        comment = self.ready_comment(expires_at=time.time() - 1)
        self.control.current.work.put_nowait(comment)
        await asyncio.wait_for(self.control.current.work.join(), 1)
        self.assertEqual(comment["status"], "expired")
        live_session.load_bailian.assert_not_called()
        live_session.synthesize_segment.assert_not_called()

    async def test_expiry_during_tts_prevents_first_audio_send(self):
        s = self.control.current
        comment = self.ready_comment(expires_at=time.time() + 10)

        async def prepared(*args):
            comment["expires_at"] = time.time() - 1
            return b"\x00\x00" * 240, self.data

        s.prepare_audio = AsyncMock(side_effect=prepared)
        s.component.send_aligned_expression = AsyncMock(return_value={})
        with patch.object(live_session, "load_bailian", return_value=("offline-key", "offline-workspace")):
            s.work.put_nowait(comment)
            await asyncio.wait_for(s.work.join(), 1)
        self.assertEqual(comment["status"], "expired")
        s.component.send_aligned_expression.assert_not_awaited()

    async def test_manual_comment_can_reply_and_reach_fake_renderer_without_selection(self):
        s = self.control.current
        comment = self.control.enqueue_comment("手工问题", "观众")
        s.prepare_audio = AsyncMock(return_value=(b"\x00\x00" * 240, self.data))
        s.component.send_aligned_expression = AsyncMock(return_value={"group_id": "offline-group"})
        with patch.object(live_session, "load_bailian", return_value=("offline-key", "offline-workspace")):
            await self.a.reply_once()
            await asyncio.wait_for(s.work.join(), 1)
        self.ai.generate_reply.assert_awaited_once()
        self.ai.select_comments.assert_not_awaited()
        self.assertEqual(comment["status"], "sent")
        s.component.send_aligned_expression.assert_awaited_once()
        live_session.synthesize_segment.assert_not_called()

    async def test_real_automation_loop_processes_manual_queue(self):
        comment = self.control.enqueue_comment("手工问题", "观众")
        accepted = asyncio.Event()

        original = self.control.accept_plan
        def intercepted(s, c, body, **options):
            # 真实循环必须走到同步计划门禁；用拦截工作队列阻止TTS。
            with patch.object(s.work, "put_nowait"):
                original(s, c, body, **options)
            accepted.set()

        with patch.object(self.control, "accept_plan", side_effect=intercepted), patch.object(self.control.current, "prepare_comment", new=AsyncMock(return_value=False)):
            self.a.start()
            await asyncio.wait_for(accepted.wait(), 1)
        self.assertEqual(comment["status"], "ready")
        self.assertTrue(self.a.running())
        self.assertTrue(self.control.current.work.empty())
        live_session.synthesize_segment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
