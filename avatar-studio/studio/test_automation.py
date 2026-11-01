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
    return {"emotion": "平静", "observation": "当前姿态未知", "reading": None,
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

    async def test_time_window_accumulates_comments_before_first_call(self):
        self.a.schedule_selection(100)
        await self.a.on_event(self.event(1))
        self.a.schedule_selection(101)
        await self.a.on_event(self.event(2))
        self.a.schedule_selection(104.999)
        self.ai.select_comments.assert_not_awaited()
        self.assertIsNone(self.a.selection_task)
        self.a.schedule_selection(105)
        await self.finish_scheduled_selection()
        batch = self.ai.select_comments.await_args.args[0]
        self.assertEqual([c["text"] for c in batch["candidates"]], ["问题1", "问题2"])

    async def test_empty_window_does_not_call_when_comment_later_arrives(self):
        self.a.schedule_selection(100)
        self.a.schedule_selection(105)
        await self.a.on_event(self.event())
        self.a.schedule_selection(106)
        self.assertIsNone(self.a.selection_task)
        self.ai.select_comments.assert_not_awaited()
        self.a.schedule_selection(110)
        await self.finish_scheduled_selection()
        self.ai.select_comments.assert_awaited_once()

    async def test_full_reply_queue_skips_window_without_catching_up(self):
        self.a.schedule_selection(100)
        await self.a.on_event(self.event())
        first = self.control.enqueue_comment("手工问题1", "观众")
        self.control.enqueue_comment("手工问题2", "观众")
        self.a.schedule_selection(105)
        first["status"] = "sent"
        self.a.schedule_selection(106)
        self.assertIsNone(self.a.selection_task)
        self.a.schedule_selection(110)
        await self.finish_scheduled_selection()
        self.ai.select_comments.assert_awaited_once()

    async def test_busy_model_skips_window_and_completion_does_not_trigger_call(self):
        self.a.schedule_selection(100)
        await self.a.on_event(self.event())
        task, released = await self.delayed("selection")
        await self.a.on_event(self.event(2))
        self.a.schedule_selection(105)
        released.set()
        await task
        self.a.selection_task = None
        self.a.schedule_selection(106)
        self.assertIsNone(self.a.selection_task)
        self.ai.select_comments.assert_awaited_once()
        self.a.schedule_selection(110)
        await self.finish_scheduled_selection()
        self.assertEqual(self.ai.select_comments.await_count, 2)

    async def test_pause_disconnect_error_and_unready_restart_full_window(self):
        for index, change in enumerate(("pause", "disconnect", "error", "unready")):
            with self.subTest(change=change):
                self.a.invalidate("new_test_case")
                self.a.schedule_selection(100)
                await self.a.on_event(self.event(index))
                if change == "pause":
                    self.control.paused = True
                    self.a.invalidate("intake_pause_changed")
                elif change == "disconnect":
                    self.a.on_status({"state": "retrying", "room_id": "123"})
                elif change == "error":
                    self.a.fail("offline_error", "selection")
                else:
                    self.control.current.component.state = "connecting"
                self.a.schedule_selection(105)
                self.control.paused = False
                self.control.current.component.state = "ready"
                self.a.source_status = {"state": "connected", "room_id": "123"}
                if change == "error":
                    self.a.resume()
                self.a.schedule_selection(106)
                await self.a.on_event(self.event(index + 10))
                self.a.schedule_selection(110.999)
                self.assertIsNone(self.a.selection_task)
                self.a.schedule_selection(111)
                await self.finish_scheduled_selection()
        self.assertEqual(self.ai.select_comments.await_count, 4)

    async def test_late_tick_never_bursts_or_compresses_next_interval(self):
        self.a.schedule_selection(100)
        await self.a.on_event(self.event())
        self.a.schedule_selection(119)
        await self.finish_scheduled_selection()
        await self.a.on_event(self.event(2))
        self.a.schedule_selection(120)
        self.a.schedule_selection(123.999)
        self.assertIsNone(self.a.selection_task)
        self.ai.select_comments.assert_awaited_once()
        self.a.schedule_selection(124)
        await self.finish_scheduled_selection()
        self.assertEqual(self.ai.select_comments.await_count, 2)

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

    async def test_selection_error_latches_without_automatic_retry(self):
        self.ai.select_comments.side_effect = ModelError("provider_unavailable")
        await self.a.on_event(self.event())
        await self.a.select_once()
        await self.a.on_event(self.event(2))
        await self.a.select_once()
        self.ai.select_comments.assert_awaited_once()
        self.assertEqual(self.a.last_error, "provider_unavailable")
        self.assertIsNotNone(self.a.error_since)
        self.assertEqual(self.control.current.comments, [])

    async def test_reply_error_latches_and_comment_is_not_retried(self):
        comment = self.control.enqueue_comment("手工问题", "观众")
        self.ai.generate_reply.side_effect = ModelError("reply_schema_invalid")
        await self.a.reply_once()
        await self.a.reply_once()
        self.ai.generate_reply.assert_awaited_once()
        self.assertEqual(comment["status"], "error")
        self.assertEqual(self.a.last_error, "reply_schema_invalid")

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
