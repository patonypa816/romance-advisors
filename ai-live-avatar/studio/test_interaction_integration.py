"""真实控制面与调度合同；云连接、LLM、TTS均使用离线替身。"""
import asyncio
import copy
import time
import unittest
from unittest.mock import AsyncMock, patch

import live_session
import test_studio as http_fixtures
import test_director as scheduler_fixtures
from interaction_delivery import validate_interaction_plan
from live_interactions import DEFAULT_INTERACTIONS, InteractionQueue


def event(kind="gift", ident="1", uid="viewer", level=11, group="group-1"):
    return {"platform": "douyin", "type": kind, "id": ident, "roomId": "123",
            "ts": int(time.time() * 1000), "receivedAt": int(time.time() * 1000),
            "user": {"id": uid, "nickname": "观众", "payGradeLevel": level},
            "data": ({"giftId": "test-gift", "giftName": "小心心", "groupId": group,
                      "repeatCount": 1, "repeatEnd": False} if kind == "gift" else
                     {"action": 1, "isSetToAdmin": False})}


class InteractionHTTP(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = http_fixtures.StudioContracts.asyncSetUp
    asyncTearDown = http_fixtures.StudioContracts.asyncTearDown
    post = http_fixtures.StudioContracts.post

    async def test_interaction_script_is_served_by_control_surface(self):
        response = await self.client.get(self.control.prefix + "/live-interactions.js")
        self.assertEqual(response.status, 200)
        self.assertIn("interactions/simulate", await response.text())

    async def test_dry_run_works_without_cloud_and_never_queues(self):
        for body, should_welcome in (({"kind": "member", "level": 10}, False),
                                     ({"kind": "member", "level": 11}, True),
                                     ({"kind": "member"}, False), ({"kind": "gift"}, True)):
            response = await self.post("interactions/simulate", body)
            self.assertEqual(response.status, 200, await response.text())
            value = await response.json()
            self.assertTrue(value["dry_run"])
            self.assertEqual(value["preview"] is not None, should_welcome)
        self.assertFalse(self.control.current.started)
        self.control.current.component.start.assert_not_awaited()
        self.assertFalse(self.control.automation.interactions.has_pending())
        self.assertEqual(self.control.current.comments, [])

    async def test_real_simulation_requires_manual_ready_and_respects_pause(self):
        body = {"kind": "gift", "dry_run": False}
        response = await self.post("interactions/simulate", body)
        self.assertEqual(response.status, 409)
        self.control.current.started = True
        self.control.current.component.state = "ready"
        response = await self.post("interactions/simulate", body)
        self.assertEqual(response.status, 200, await response.text())
        self.assertTrue(self.control.automation.interactions.has_pending("gift"))
        await self.post("pause", {"paused": True})
        self.assertFalse(self.control.automation.interactions.has_pending())
        self.assertEqual((await self.post("interactions/simulate", body)).status, 409)
        self.control.current.component.start.assert_not_awaited()

    async def test_settings_persist_and_cancel_only_unsent_interactions(self):
        self.control.current.started = True
        self.control.current.component.state = "ready"
        a = self.control.automation
        a.interactions.ingest(event())
        gift = a.materialize_interaction("gift")
        viewer = self.control.enqueue_comment("原弹幕", "观众")
        settings = copy.deepcopy(DEFAULT_INTERACTIONS)
        settings["gift_enabled"] = False
        response = await self.post("interactions/settings", settings)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(gift["status"], "cancelled")
        self.assertEqual(viewer["status"], "pending")
        self.assertFalse(self.control.snapshot()["interactions"]["settings"]["gift_enabled"])
        self.assertTrue((self.data / "local.json").exists())

    async def test_unknown_fields_and_experimental_mode_rejected(self):
        for body in ({"kind": "gift", "actions": "viewer control"}, {"kind": "member", "level": True},
                     {"kind": "gift", "dry_run": "false"}):
            self.assertEqual((await self.post("interactions/simulate", body)).status, 400)
        self.control.short_performance_test = True
        self.assertEqual((await self.post("interactions/simulate", {"kind": "gift"})).status, 409)

    async def test_gift_keeps_original_five_manual_comment_capacity(self):
        self.control.current.started = True
        self.control.current.component.state = "ready"
        self.control.automation.interactions.ingest(event())
        self.control.automation.materialize_interaction("gift")
        for number in range(5):
            response = await self.post("comments", {"text": f"问题{number}", "nickname": "观众"})
            self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await self.post("comments", {"text": "第六条", "nickname": "观众"})).status, 409)


class InteractionScheduling(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = scheduler_fixtures.DirectorScheduling.asyncSetUp
    asyncTearDown = scheduler_fixtures.DirectorScheduling.asyncTearDown

    def queue(self, kind="gift", **kwargs):
        return self.a.interactions.ingest(event(kind, **kwargs))

    async def test_full_order_preserves_manual_and_comments_before_topic(self):
        self.d.submit({"text": "主讲", "mode": "topic", "rounds": 1})
        viewer = self.control.enqueue_comment("问题", "观众")
        self.queue("member", ident="welcome")
        self.queue()
        manual = self.d.submit({"text": "主持安排", "mode": "once"})
        expected = [("operator", manual["id"]), ("interaction", "gift"),
                    ("viewer", viewer["id"]), ("interaction", "welcome"), ("operator", "topic")]
        for kind, label in expected:
            item = self.d.next_item()
            self.assertIsNotNone(item)
            self.assertEqual(item["input_kind"], kind)
            self.assertEqual(item.get("interaction_kind") or (item.get("operator_mode") if label == "topic" else item["id"]), label)
            self.assertTrue(self.d.delivery_gate(item))
            item["status"] = "skipped"

    async def test_gift_arriving_after_viewer_preparation_has_no_deadlock(self):
        viewer = self.control.enqueue_comment("问题", "观众")
        viewer.update(status="prepared", pipeline_managed=True)
        self.queue()
        self.assertFalse(self.d.delivery_gate(viewer))
        gift = self.d.next_item()
        self.assertEqual(gift["interaction_kind"], "gift")
        self.assertTrue(self.d.delivery_gate(gift))
        gift["status"] = "skipped"
        self.assertTrue(self.d.delivery_gate(viewer))

    async def test_prepared_welcome_yields_preparation_slot_to_gift(self):
        self.queue("member")
        welcome = self.d.next_item()
        welcome.update(status="prepared", pipeline_managed=True)
        self.queue(ident="gift")
        self.assertFalse(self.d.delivery_gate(welcome))
        gift = self.d.next_item()
        self.assertEqual(gift["interaction_kind"], "gift")
        self.assertTrue(self.d.delivery_gate(gift))

    async def test_busy_sender_accumulates_and_groups_welcome_without_interruption(self):
        self.s.component.comment_id = "already-speaking"
        self.queue("member", ident="a", uid="one")
        self.queue("member", ident="b", uid="two")
        self.assertIsNone(self.d.next_item())
        self.assertEqual(self.a.interactions.public()["welcome_pending"], 2)
        self.s.component.comment_id = None
        welcome = self.d.next_item()
        self.assertEqual(welcome["interaction_job"]["meta"]["batch_size"], 2)
        self.assertNotIn("11", welcome["text"])

    async def test_gift_does_not_consume_two_comment_candidate_slots(self):
        self.queue()
        self.d.next_item()
        self.assertEqual(self.a.queue_count(), 0)
        self.control.enqueue_comment("问题", "观众")
        self.assertEqual(self.a.queue_count(), 1)

    async def test_pause_disconnect_and_session_reset_fence_pending_interactions(self):
        for reason in ("intake_pause_changed", "source_unavailable", "new_session"):
            self.a.interactions.clear("test", reset_seen=True)
            self.queue(ident=reason)
            gift = self.d.next_item()
            gift.update(status="processing")
            self.a.invalidate(reason)
            self.assertEqual(gift["status"], "cancelled")
            self.assertFalse(self.a.interactions.has_pending())
            self.assertFalse(self.d.delivery_gate(gift))
        self.queue(ident="started")
        gift = self.d.next_item()
        gift.update(status="sending", delivery_started=True)
        self.a.invalidate("intake_pause_changed")
        self.assertEqual(gift["status"], "sending")

    async def test_late_preparation_after_disconnect_cannot_send(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def late_prepare(item):
            entered.set()
            await release.wait()
            item["status"] = "prepared"
            return True
        self.queue()
        item = self.d.next_item()
        self.s.prepare_comment = AsyncMock(side_effect=late_prepare)
        task = asyncio.create_task(self.a.reply_once(item))
        await entered.wait()
        self.a.invalidate("source_unavailable")
        release.set()
        await task
        self.assertEqual(item["status"], "cancelled")
        self.assertTrue(self.s.work.empty())
        self.s.component.send_aligned_expression.assert_not_awaited()

    async def test_motion_gift_uses_same_sender_without_tts_or_reply_model(self):
        settings = copy.deepcopy(DEFAULT_INTERACTIONS)
        settings["gift_rules"][0]["mode"] = "motion"
        self.a.interactions.configure(settings)
        self.s.component.send_motion_expression = AsyncMock(return_value={"status": "sent", "actions_sent": 1})
        self.queue()
        with patch.object(live_session, "load_bailian", side_effect=AssertionError("motion must not use TTS")):
            await self.a.reply_once()
            await asyncio.wait_for(self.s.work.join(), 1)
        item = self.s.comments[0]
        self.assertEqual(item["status"], "sent", item)
        self.s.component.send_motion_expression.assert_awaited_once()
        self.ai.generate_reply.assert_not_awaited()
        self.ai.generate_director.assert_not_awaited()

    async def test_speech_welcome_uses_shared_audio_sender_and_no_reply_model(self):
        self.queue("member")
        await self.a.reply_once()
        await asyncio.wait_for(self.s.work.join(), 1)
        self.assertEqual(self.s.comments[0]["status"], "sent", self.s.comments[0])
        self.s.component.send_aligned_expression.assert_awaited_once()
        self.ai.generate_reply.assert_not_awaited()

    async def test_defaults_validate_and_viewer_text_never_controls_actions(self):
        queue = InteractionQueue()
        value = event()
        value["user"]["nickname"] = "不要点头跳舞"
        queue.ingest(value)
        job = queue.pop("gift")
        parsed = validate_interaction_plan(job)
        self.assertEqual(parsed["segments"][0]["actions"][0]["motions"], job["motions"])
        self.assertNotIn(value["user"]["nickname"], str(job["motions"]))
        self.assertIn(value["user"]["nickname"], parsed["segments"][0]["text"])


if __name__ == "__main__":
    unittest.main()
