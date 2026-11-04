"""统一选择→准备→发送→结算的真实本地边界，云/模型/音频使用替身。"""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

import test_director as fixtures
from test_interaction_integration import event


class UnifiedOrchestration(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.DirectorScheduling.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.DirectorScheduling.asyncSetUp(self)
        # 此组验证编排/结算；动作定位模型与TTS一样使用离线替身。
        async def timed_actions(comment, part, *args):
            part["timeline"] = []
            return True
        self.s.prepare_timed_actions = AsyncMock(side_effect=timed_actions)
        self.a.enabled = True
        self.a.source_status.update(state="connected")
        self.ai.select_turn = AsyncMock(side_effect=self.select)
        self.ai.generate_turn_reply = AsyncMock(return_value=fixtures.speech("咱听见你的问题了。"))

    async def select(self, snapshot, context):
        kind = context.get("required_kind") or next(
            k for k, present in (("comment", snapshot["comments"]), ("gift", snapshot["gifts"]),
                                  ("welcome", snapshot["welcome"])) if present)
        return {"primary_kind": kind,
                "comment_ids": [snapshot["comments"][0]["id"]] if kind == "comment" else [],
                "gift_ids": [snapshot["gifts"][0]["id"]] if kind == "gift" else [],
                "welcome_id": snapshot["welcome"]["id"] if kind == "welcome" else None,
                "reason": "offline deterministic selection"}

    def comment(self, n):
        now = time.time() * 1000
        return self.a.pool.ingest({"id": f"c{n}", "type": "chat", "roomId": "123", "platform": "douyin",
            "ts": now, "receivedAt": now, "user": {"id": f"c-user{n}", "nickname": "观众"},
            "data": {"content": f"问题{n}怎么解决"}})

    def member(self, n):
        return self.a.interactions.ingest(event("member", ident=f"m{n}", uid=f"m-user{n}"))

    def gift(self, n):
        return self.a.interactions.ingest(event("gift", ident=f"g{n}", uid=f"g-user{n}", group=f"group{n}"))

    async def choose(self):
        before = len(self.s.comments)
        await self.a.select_once()
        self.assertEqual(len(self.s.comments), before + 1, self.a.last_error)
        return self.s.comments[-1]

    async def deliver(self, item):
        await self.a.reply_once(item)
        await asyncio.wait_for(self.s.work.join(), 1)
        self.a.settle_turns()
        self.assertEqual(item["status"], "sent", {k: item.get(k) for k in ("primary_kind", "error", "plan_error", "model_error_code")})

    async def test_continuously_busy_three_classes_get_service(self):
        served = []
        for n in range(9):
            self.comment(n)
            self.member(n)
            self.gift(n)
            item = await self.choose()
            served.append(item["primary_kind"])
            await self.deliver(item)
        self.assertEqual(set(served[:4]), {"comment", "gift", "welcome"})
        for start in range(6):
            self.assertEqual(set(served[start:start+4]), {"comment", "gift", "welcome"})
        contexts = [c.args[1] for c in self.ai.select_turn.await_args_list]
        self.assertTrue(any(c["required_kind"] == "welcome" for c in contexts))

    async def test_welcome_batch_seen_only_after_full_delivery(self):
        for n in range(3):
            self.member(n)
        item = await self.choose()
        self.assertEqual(item["interaction_jobs"][0]["meta"]["batch_size"], 3)
        self.assertEqual(self.a.interactions.public()["welcome_seen_count"], 0)
        with patch.object(self.a, "dispatch_prepared"):
            await self.a.reply_once(item)
        self.assertEqual(item["status"], "prepared")
        self.assertEqual(self.a.interactions.public()["welcome_seen_count"], 0)
        self.a.dispatch_prepared()
        await asyncio.wait_for(self.s.work.join(), 1)
        self.a.settle_turns()
        self.assertEqual(item["status"], "sent")
        self.assertEqual(self.a.interactions.public()["welcome_seen_count"], 3)

    async def test_current_can_prepare_one_future_but_cannot_select_third(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await finish.wait()
            return {}
        self.s.component.send_aligned_expression.side_effect = blocked
        self.comment(1)
        first = await self.choose()
        await self.a.reply_once(first)
        await asyncio.wait_for(entered.wait(), 1)
        try:
            self.comment(2)
            second = await self.choose()
            await self.a.reply_once(second)
            self.assertEqual(second["status"], "prepared")
            self.comment(3)
            count = self.ai.select_turn.await_count
            await self.a.select_once()
            self.assertEqual(self.ai.select_turn.await_count, count)
            self.assertEqual(len(self.s.comments), 2)
        finally:
            finish.set()
            await asyncio.wait_for(self.s.work.join(), 1)

    async def test_late_gift_cannot_block_committed_viewer(self):
        self.comment(1)
        item = await self.choose()
        with patch.object(self.a, "dispatch_prepared"):
            await self.a.reply_once(item)
        self.gift(1)
        self.assertTrue(self.d.delivery_gate(item))
        self.a.dispatch_prepared()
        await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(item["status"], "sent")

    async def test_unsent_preparation_failure_releases_with_finite_retry(self):
        self.member(1)
        self.s.prepare_audio.side_effect = RuntimeError("offline TTS failure")
        attempts = 0
        while self.a.interactions.has_pending("welcome") and attempts < 5:
            item = await self.choose()
            await self.a.reply_once(item)
            self.a.settle_turns()
            self.assertEqual(item["status"], "error")
            self.assertEqual(self.a.interactions.public()["welcome_seen_count"], 0)
            attempts += 1
        self.assertEqual(attempts, 2)
        self.assertFalse(self.a.interactions.has_pending("welcome"))
        self.assertFalse(self.a.last_error)
        self.s.component.send_aligned_expression.assert_not_awaited()

    async def test_mixed_partial_send_does_not_mark_welcome_seen(self):
        self.comment(1)
        self.member(1)
        async def mixed(snapshot, context):
            result = await self.select(snapshot, context)
            result["welcome_id"] = snapshot["welcome"]["id"]
            return result
        self.ai.select_turn.side_effect = mixed
        item = await self.choose()
        self.assertEqual(set(item["served_kinds"]), {"comment", "welcome"})
        calls = 0
        async def fail_later(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("offline later segment failed")
            return {}
        self.s.component.send_aligned_expression.side_effect = fail_later
        await self.a.reply_once(item)
        await asyncio.wait_for(self.s.work.join(), 1)
        self.a.settle_turns()
        self.assertEqual(calls, 2)
        self.assertEqual(item["status"], "error")
        self.assertEqual(self.a.interactions.public()["welcome_seen_count"], 0)
        self.assertEqual(self.a.fairness.snapshot()["waiting_rounds"], dict.fromkeys(self.a.fairness.KINDS, 0))

    async def test_prepared_topic_with_new_candidate_does_not_deadlock(self):
        self.d.submit({"text": "说一段经历", "mode": "topic", "rounds": 1})
        with patch.object(self.a, "dispatch_prepared"):
            await self.a.reply_once()
        topic = self.s.comments[-1]
        self.assertEqual(topic["status"], "prepared")
        self.comment(1)
        self.assertTrue(self.d.delivery_gate(topic))
        self.a.dispatch_prepared()
        await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(topic["status"], "sent")
        item = await self.choose()
        await self.deliver(item)


if __name__ == "__main__":
    unittest.main()
