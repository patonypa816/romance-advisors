"""统一编排的人工插队、来源中断和无发送失败边界。"""
import asyncio
import copy
import unittest
from unittest.mock import patch

from cloudbase_ai import ModelError
import test_unified_orchestration as fixtures


class UnifiedEdges(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.UnifiedOrchestration.asyncSetUp
    asyncTearDown = fixtures.UnifiedOrchestration.asyncTearDown
    select = fixtures.UnifiedOrchestration.select
    comment = fixtures.UnifiedOrchestration.comment
    member = fixtures.UnifiedOrchestration.member
    gift = fixtures.UnifiedOrchestration.gift
    choose = fixtures.UnifiedOrchestration.choose
    deliver = fixtures.UnifiedOrchestration.deliver

    async def test_explicit_manual_can_prepare_when_it_blocks_committed_turn(self):
        self.comment(1)
        item = await self.choose()
        with patch.object(self.a, "dispatch_prepared"):
            await self.a.reply_once(item)
        self.d.submit({"text": "先说一句重要通知", "mode": "once"})
        manual = self.d.next_item()
        self.assertEqual(manual["operator_mode"], "once")
        self.assertFalse(self.d.delivery_gate(item))
        await self.deliver(manual)
        self.a.dispatch_prepared()
        await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(item["status"], "sent")

    async def test_source_disconnect_preserves_started_welcome_completion(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def send(*args, **kwargs):
            entered.set()
            await finish.wait()
            return {}
        self.s.component.send_aligned_expression.side_effect = send
        self.member(1)
        item = await self.choose()
        await self.a.reply_once(item)
        await asyncio.wait_for(entered.wait(), 1)
        self.a.invalidate("source_unavailable")
        finish.set()
        await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(item["status"], "sent")
        self.assertEqual(self.a.interactions.public()["welcome_seen_count"], 1)

    async def test_failed_selector_does_not_retry_same_welcome_forever(self):
        self.member(1)
        self.ai.select_turn.side_effect = ModelError("selection_invalid")
        await self.a.select_once()
        self.assertFalse(self.a.interactions.has_pending())
        self.assertFalse(self.s.comments)
        await self.a.select_once()
        self.assertEqual(self.ai.select_turn.await_count, 1)
        self.assertEqual(self.a.interactions.public()["welcome_seen_count"], 0)

    async def test_motion_only_gift_splits_from_comment_instead_of_overwriting_action(self):
        settings = copy.deepcopy(self.a.interactions.settings)
        settings["gift_rules"][0]["mode"] = "motion"
        self.a.interactions.configure(settings)
        self.comment(1)
        self.gift(1)
        async def mixed(snapshot, context):
            result = await self.select(snapshot, context)
            result["gift_ids"] = [snapshot["gifts"][0]["id"]]
            return result
        self.ai.select_turn.side_effect = mixed
        item = await self.choose()
        self.assertEqual(item["served_kinds"], ["comment"])
        self.assertTrue(self.a.interactions.has_pending("gift"))
        self.assertEqual(item["interaction_jobs"], [])

    async def test_simulated_welcome_can_schedule_without_source_connection(self):
        self.a.enabled = False
        self.a.source_status["state"] = "stopped"
        self.member(1)
        item = await self.choose()
        await self.deliver(item)
        self.assertEqual(self.a.interactions.public()["welcome_seen_count"], 1)

    async def test_comment_and_gift_share_anonymous_identity(self):
        from test_interaction_integration import event
        self.comment(1)
        self.a.interactions.ingest(event("gift", ident="same-person", uid="c-user1"))
        comment = self.a.pool.snapshot()["candidates"][0]
        gift = self.a.interactions.snapshot_candidates()["gift"][0]
        self.assertEqual(comment["viewer_key"], gift["viewer_key"])

    async def test_expired_reservation_stays_cancelled_at_sender_gate(self):
        self.member(1)
        item = await self.choose()
        with patch.object(self.a, "dispatch_prepared"):
            await self.a.reply_once(item)
        self.a.interactions.clear("new_session", reset_seen=True)
        self.assertFalse(self.s._begin_delivery(item))
        self.assertEqual(item["status"], "cancelled")
        self.assertFalse(item.get("delivery_started"))
        self.assertTrue(self.a.future_slot_free())


if __name__ == "__main__":
    unittest.main()
