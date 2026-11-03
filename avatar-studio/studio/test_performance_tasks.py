"""Offline full-task orchestration; existing sender, fake PCM/provider only."""
import asyncio
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

import test_director as fixtures
from performance_tasks import chunk_plan, split_text
from bailian_ai import BailianAI


def plan(text, actions=None):
    actions = actions or []
    return {"emotion": "自然", "segments": [{"text": text, "instruction": "自然清晰",
        "actions": actions, "recovery": {"gaze": "注视镜头", "expression": "平静"} if actions else None}]}


def action(quote, occurrence=1):
    return {"anchor": {"quote": quote, "occurrence": occurrence},
            "motions": [{"part": "head", "description": "轻轻点头"}], "gaze": "注视镜头", "expression": "平静"}


class PartitionTests(unittest.TestCase):
    def test_long_fixed_script_lossless_with_spaces_punctuation_and_long_sentence(self):
        for text in ["  开始了。\n" + "一个没有逗号的长句" * 42 + "。最后一句！ \n", " A long English sentence, with words. " * 20,
                     "你好。" * 154]:
            chunks = split_text(text)
            self.assertEqual("".join(c[2] for c in chunks), text)
            self.assertTrue(all(len(c[2].strip()) <= 80 for c in chunks))
            parts = chunk_plan(plan(text))
            self.assertEqual("".join(p["segments"][0]["text"] for p in parts), text)

    def test_natural_boundary_before_capacity(self):
        text = "甲" * 40 + "。" + "乙" * 50 + "。"
        self.assertEqual(split_text(text)[0][2], "甲" * 40 + "。")

    def test_repeated_anchor_remapped_to_local_occurrence(self):
        text = "重点" + "甲" * 72 + "。再说重点，然后说重点。"
        parts = chunk_plan(plan(text, [action("重点", 2), action("重点", 3)]))
        self.assertEqual("".join(p["segments"][0]["text"] for p in parts), text)
        actions = [a for p in parts for a in p["segments"][0]["actions"]]
        self.assertEqual([a["anchor"]["occurrence"] for a in actions], [1, 2])

    def test_anchor_is_never_cut_or_silently_dropped(self):
        text = "甲" * 77 + "连续短语" + "乙" * 70
        parts = chunk_plan(plan(text, [action("连续短语")]))
        self.assertEqual(sum(len(p["segments"][0]["actions"]) for p in parts), 1)
        containing = next(p["segments"][0] for p in parts if p["segments"][0]["actions"])
        self.assertIn("连续短语", containing["text"])
        with self.assertRaisesRegex(ValueError, "锚点"):
            chunk_plan(plan("甲" * 100, [action("甲" * 81)]))

    def test_unknown_fields_and_invalid_limits_are_rejected(self):
        for limit in [True, 19, 121]:
            with self.assertRaises(ValueError):split_text("你好", limit)
        bad = plan("你好")
        bad["observation"] = "猜测姿态"
        with self.assertRaises(ValueError):chunk_plan(bad)


class TaskExecution(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.DirectorScheduling.asyncSetUp
    asyncTearDown = fixtures.DirectorScheduling.asyncTearDown

    def submit(self, text="这是一个完整表演任务。" * 20, kind="script", **kw):
        self.ai.generate_performance = getattr(self.ai, "generate_performance", AsyncMock(return_value=plan(text)))
        return self.control.performance.submit({"request_id": "task-one", "kind": kind, "text": text, **kw})

    async def deliver(self):
        c = self.d.next_item()
        await self.a.reply_once(c)
        await asyncio.wait_for(self.s.work.join(), 2)
        self.control.performance.sync()
        return c

    async def finish(self):
        for _ in range(300):
            if not self.control.performance.first():return
            await self.deliver()
        self.fail("finite task did not finish")

    async def test_long_script_uses_original_sender_and_never_expression_brain(self):
        text = "  这次先把第一件事情讲清楚。\n" * 14 + "最后自然结束。 "
        self.submit(text)
        await self.finish()
        task = self.control.performance.public()[0]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["planned_text"], text)
        self.assertEqual(task["sent_text"], text)
        self.assertGreater(task["sent_blocks"], 2)
        sent = "".join(c.args[1] for c in self.s.component.send_aligned_expression.await_args_list)
        self.assertEqual(sent, text)
        self.ai.generate_performance.assert_not_awaited()
        self.ai.generate_reply.assert_not_awaited()
        self.ai.generate_director.assert_not_awaited()
        self.assertFalse(self.s.stopping)  # completed is not session ended

    async def test_intent_154_chars_is_split_before_tts_and_finishes_once(self):
        text = "甲" * 154
        self.submit("把完整故事讲给观众听", "intent")
        self.ai.generate_performance.return_value = plan(text)
        await self.finish()
        task = self.control.performance.public()[0]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["sent_text"], text)
        self.assertEqual(task["sent_blocks"], 2)
        self.ai.generate_performance.assert_awaited_once()
        self.assertIsNone(self.d.next_item())

    async def test_pause_at_boundary_then_resume_never_replays_sent_block(self):
        self.submit()
        await self.deliver()
        p = self.control.performance
        before = p.public()[0]["sent_text"]
        p.control("task-one", "pause")
        self.assertEqual(p.public()[0]["status"], "paused")
        self.assertIsNone(self.d.next_item())
        p.control("task-one", "resume")
        await self.finish()
        self.assertTrue(p.public()[0]["sent_text"].startswith(before))
        self.assertEqual(self.s.component.send_aligned_expression.await_count, p.public()[0]["total_blocks"])

    async def test_pause_during_send_finishes_current_block_only(self):
        self.submit()
        entered, finish = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set(); await finish.wait(); return {}
        self.s.component.send_aligned_expression.side_effect = send
        await self.a.reply_once(self.d.next_item())
        await asyncio.wait_for(entered.wait(), 1)
        p = self.control.performance
        p.control("task-one", "pause")
        self.assertEqual(p.public()[0]["sent_blocks"], 0)
        finish.set(); await asyncio.wait_for(self.s.work.join(), 2); p.sync()
        self.assertEqual(p.public()[0]["sent_blocks"], 1)
        self.assertEqual(p.public()[0]["status"], "paused")
        self.assertIsNone(self.d.next_item())

    async def test_cancel_during_send_keeps_receipt_without_next_block(self):
        self.submit()
        entered, finish = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set(); await finish.wait(); return {}
        self.s.component.send_aligned_expression.side_effect = send
        await self.a.reply_once(self.d.next_item()); await asyncio.wait_for(entered.wait(), 1)
        p = self.control.performance
        p.control("task-one", "cancel")
        finish.set(); await asyncio.wait_for(self.s.work.join(), 2); p.sync()
        self.assertEqual(p.public()[0]["status"], "cancelled")
        self.assertEqual(p.public()[0]["sent_blocks"], 1)
        self.assertTrue(p.public()[0]["partial_delivery"])
        self.assertIsNone(self.d.next_item())

    async def test_late_model_after_cancel_cannot_reappear(self):
        self.submit("说明这个故事", "intent")
        entered, finish = asyncio.Event(), asyncio.Event()
        async def generate(*args):
            entered.set(); await finish.wait(); return plan("迟到内容")
        self.ai.generate_performance.side_effect = generate
        task = asyncio.create_task(self.a.reply_once(self.d.next_item()))
        await asyncio.wait_for(entered.wait(), 1)
        self.control.performance.control("task-one", "cancel")
        finish.set(); await task
        self.assertEqual(self.control.performance.public()[0]["status"], "cancelled")
        self.s.prepare_audio.assert_not_awaited()
        self.s.component.send_aligned_expression.assert_not_awaited()

    async def test_prepared_pause_resumes_same_audio_without_regeneration(self):
        self.submit("完整的一句话。")
        self.d.delivery_gate = lambda c: False
        await self.a.reply_once(self.d.next_item())
        p = self.control.performance
        p.control("task-one", "pause")
        p.control("task-one", "resume")
        self.d.delivery_gate = lambda c: p.delivery_gate(c)
        self.a.dispatch_prepared()
        await asyncio.wait_for(self.s.work.join(), 2); p.sync()
        self.assertEqual(p.public()[0]["status"], "completed")
        self.s.prepare_audio.assert_awaited_once()

    async def test_prefetch_one_block_then_pause_insert_resume_without_retts(self):
        self.submit("甲" * 150)
        entered, finish = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set(); await finish.wait(); return {}
        self.s.component.send_aligned_expression.side_effect = send
        first = self.d.next_item()
        await self.a.reply_once(first); await asyncio.wait_for(entered.wait(), 1)
        future = self.d.next_item()
        self.assertEqual(future["performance_block"], 1)
        await self.a.reply_once(future)
        self.assertEqual(future["status"], "prepared")
        self.assertIsNone(self.d.next_item())
        p = self.control.performance
        self.assertEqual(p.public()[0]["sent_blocks"], 0)
        self.assertFalse(p.delivery_gate(future))
        p.control("task-one", "pause")
        self.assertTrue(p.unfinished())
        finish.set(); await asyncio.wait_for(self.s.work.join(), 2); p.sync()
        self.assertEqual(p.public()[0]["sent_blocks"], 1)
        self.assertIsNone(self.d.next_item())
        p.submit({"request_id": "two", "kind": "script", "text": "乙"})
        await self.deliver()
        self.assertEqual(p.public()[1]["status"], "completed")
        p.control("task-one", "resume")
        self.a.dispatch_prepared()
        await asyncio.wait_for(self.s.work.join(), 2); p.sync()
        self.assertEqual(p.public()[0]["status"], "completed")
        self.assertEqual(self.s.prepare_audio.await_count, 3)
        self.assertEqual([call.args[1] for call in self.s.component.send_aligned_expression.await_args_list],
                         ["甲" * 80, "乙", "甲" * 70])

    async def test_failed_predecessor_cancels_prepared_future(self):
        self.submit("甲" * 150)
        entered, finish = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set(); await finish.wait(); raise OSError("disconnect")
        self.s.component.send_aligned_expression.side_effect = send
        await self.a.reply_once(self.d.next_item()); await asyncio.wait_for(entered.wait(), 1)
        future = self.d.next_item(); await self.a.reply_once(future)
        finish.set(); await asyncio.wait_for(self.s.work.join(), 2); self.control.performance.sync()
        self.assertEqual(self.control.performance.public()[0]["status"], "unknown")
        self.assertEqual(future["status"], "cancelled")
        self.a.dispatch_prepared()
        self.s.component.send_aligned_expression.assert_awaited_once()

    async def test_fixed_auto_actions_cannot_rewrite_script(self):
        self.submit("原稿不可改。", auto_actions=True)
        self.ai.generate_performance.return_value = plan("我改写了。")
        await self.deliver()
        self.assertEqual(self.control.performance.public()[0]["status"], "failed")
        self.assertIn("固定稿编排必须保留", self.control.performance.public()[0]["error"])
        self.s.prepare_audio.assert_not_awaited()
        self.s.component.send_aligned_expression.assert_not_awaited()

    async def test_pure_motion_uses_original_motion_sender_zero_tts(self):
        self.submit("轻轻点头", "motion")
        self.ai.generate_performance.return_value = {"emotion": "自然", "actions": [{"kind": "S",
            "delay_seconds": 0, "hold_seconds": 0, "motions": [{"part": "head", "description": "轻轻点头"}],
            "gaze": "注视镜头", "expression": "平静"}], "recovery": {"gaze": "注视镜头", "expression": "平静"}}
        self.s.component.send_motion_expression = AsyncMock(return_value={"actions_sent": 1})
        await self.finish()
        self.s.prepare_audio.assert_not_awaited()
        self.s.component.send_aligned_expression.assert_not_awaited()
        self.s.component.send_motion_expression.assert_awaited_once()
        self.assertEqual(self.control.performance.public()[0]["status"], "completed")

    async def test_failure_after_sending_is_unknown_not_auto_retry(self):
        self.submit()
        self.s.component.send_aligned_expression.side_effect = OSError("disconnect")
        await self.deliver()
        self.assertEqual(self.control.performance.public()[0]["status"], "unknown")
        self.assertIsNone(self.d.next_item())
        self.s.component.send_aligned_expression.assert_awaited_once()

    async def test_same_id_deduplicates_and_different_payload_rejected(self):
        request = {"request_id": "same", "kind": "script", "text": "你好。"}
        p = self.control.performance
        one = p.submit(request)
        self.assertEqual(p.submit(request)["task_id"], one["task_id"])
        with self.assertRaises(ValueError):p.submit({**request, "text": "换词。"})
        await self.finish()
        p.submit(request)
        self.assertIsNone(self.d.next_item())

    async def test_fifo_no_task_interleaving(self):
        self.submit("甲" * 170)
        self.control.performance.submit({"request_id": "two", "kind": "script", "text": "乙" * 130})
        await self.finish()
        sent = "".join(c.args[1] for c in self.s.component.send_aligned_expression.await_args_list)
        self.assertEqual(sent, "甲" * 170 + "乙" * 130)

    async def test_checkpoint_failure_does_not_block_cancel(self):
        self.submit()
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            result = self.control.performance.control("task-one", "cancel")
        self.assertEqual(result["status"], "cancelled")


class PromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_brain_has_no_topic_resume_or_old_field_contract(self):
        brain = BailianAI({})
        brain._call = AsyncMock(return_value=plan("说明这件事。"))
        await brain.generate_performance("intent", "解释一下", {"character": {"identity": "主持人"},
            "planning": ["旧的30字规则"], "current_topic": {"resume_required": True}}, {}, "自然")
        prompt = brain._call.call_args.args[1][0]["content"]
        for old in ["reading", "observation", "resume_required", "旧的30字规则", "segments为1至2", "好，咱们接着说"]:
            self.assertNotIn(old, prompt)
        self.assertIn("主持人", prompt)
        self.assertIn("1.1秒", prompt)


class HttpContracts(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.fixtures.StudioContracts.asyncSetUp
    asyncTearDown = fixtures.fixtures.StudioContracts.asyncTearDown
    post = fixtures.fixtures.StudioContracts.post

    async def test_same_control_origin_required_and_no_implicit_cloud_start(self):
        body = {"request_id": "http-one", "kind": "script", "text": "你好。"}
        response = await self.post("performance/submit", body)
        self.assertEqual(response.status, 409)
        self.assertFalse(self.control.current.started)
        self.control.current.started = True
        self.control.current.component.state = "ready"
        response = await self.post("performance/submit", body, headers={"Origin": "https://untrusted.example"})
        self.assertEqual(response.status, 403)
        response = await self.post("performance/submit", body)
        self.assertEqual(response.status, 200)
        result = await response.json()
        response = await self.post("performance/submit", body)
        self.assertEqual((await response.json())["task_id"], result["task_id"])
        response = await self.post("performance/cancel", {"request_id": "http-one"})
        self.assertEqual((await response.json())["status"], "cancelled")
        response = await self.client.get(self.control.prefix + "/performance")
        self.assertEqual(len((await response.json())["tasks"]), 1)
