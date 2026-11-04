"""Two-stage external jobs: real local worker, event barriers, no paid calls."""
import asyncio
import copy
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import test_external_game as game_fixtures
from motion_component import MotionComponent
from test_motion_component import FakeSocket


class ExternalPreparationContracts(unittest.IsolatedAsyncioTestCase):
    # Reuse only fixture helpers, so discovery does not rerun the legacy suite.
    asyncSetUp = game_fixtures.ExternalGameContracts.asyncSetUp
    asyncTearDown = game_fixtures.ExternalGameContracts.asyncTearDown
    synthesize = game_fixtures.ExternalGameContracts.synthesize
    post = game_fixtures.ExternalGameContracts.post
    claim = game_fixtures.ExternalGameContracts.claim
    live = game_fixtures.ExternalGameContracts.live
    submit = game_fixtures.ExternalGameContracts.submit
    get_job = game_fixtures.ExternalGameContracts.get_job
    performance_plan = game_fixtures.ExternalGameContracts.performance_plan
    action_tts = game_fixtures.ExternalGameContracts.action_tts

    async def prepare(self, job_id="round:1", text="我先想一想，军师们这招有点意思。", **changed):
        return await self.post("external/jobs/prepare", {**self.identity, "job_id": job_id, "text": text, **changed})

    async def dispatch(self, job_id="round:1", **changed):
        return await self.post("external/jobs/" + job_id + "/dispatch", {**self.identity, **changed})

    async def settled(self, job_id="round:1"):
        task = self.studio.external_game.jobs[job_id]["preparation_task"]
        await asyncio.wait_for(asyncio.shield(task), 1)
        await asyncio.sleep(0)  # The task's completion callback releases the next slot.
        return await (await self.get_job(job_id)).json()

    async def begin(self):
        await self.claim()
        self.live()

    async def test_prepare_has_zero_transport_and_duplicates_reuse_aligned_audio(self):
        await self.begin()
        self.action_tts()
        session = self.studio.current
        original = session.component
        renderer = MotionComponent(session.http, session.out, session.image, session.diagnostics)
        renderer._ws = FakeSocket()
        renderer.state = "ready"
        renderer.live_id = "offline-only"
        renderer._ready.set()
        renderer._joined.set()
        session.component = renderer
        plan = self.performance_plan()
        try:
            self.assertEqual((await self.prepare(text="别急。", plan=plan)).status, 200)
            result = await self.settled()
            self.assertEqual(result["status"], "prepared")
            self.assertFalse(result["preparation_pending"])
            self.assertFalse(result["worker_pending"])
            self.assertGreater(result["estimated_delivery_seconds"], 4)
            self.assertIsNone(renderer.comment_id)
            self.assertEqual(renderer._ws.sent, [])
            self.assertEqual(renderer._ws.binary, [])
            self.assertTrue(session.work.empty())
            self.assertFalse(result["delivery_started"])
            self.assertEqual((await self.prepare(text="别急。", plan=plan)).status, 200)
            self.tts.assert_awaited_once()
            session.timed_action_planner.assert_awaited_once()
            # Runtime alignment changed the anchor, but original request identity stays frozen.
            archive = json.loads((session.out / "external-game.json").read_text())
            self.assertEqual(archive["jobs"][0]["plan"], plan)
            for changed in (dict(plan, emotion="开心"),):
                self.assertEqual((await self.prepare(text="别急。", plan=changed)).status, 409)
        finally:
            session.component = original

    async def test_current_sends_while_next_prepares_but_dispatch_never_reserves_future_send(self):
        await self.begin()
        session = self.studio.current
        entered, release = asyncio.Event(), asyncio.Event()
        sender = session.component.send_aligned_expression
        async def sending(*args, **kwargs):
            entered.set()
            await release.wait()
            return await session.component._send(*args, **kwargs)
        sender.side_effect = sending
        await self.prepare("A")
        await self.settled("A")
        await self.dispatch("A")
        await asyncio.wait_for(entered.wait(), 1)
        current_identity = (session.component.comment_id, session.component.job_id)
        current_item = self.studio.external_game.jobs["A"]["item"]
        await self.prepare("B")
        candidate = await self.settled("B")
        self.assertEqual(candidate["status"], "prepared")
        self.assertEqual((session.component.comment_id, session.component.job_id), current_identity)
        self.assertEqual(current_item["status"], "sending")
        self.assertEqual(current_item["motion_receipts"], [])
        state = await (await self.client.get(self.studio.prefix + "/external/state")).json()
        self.assertEqual(state["current_job"]["job_id"], "A")
        self.assertEqual(state["next_job"]["job_id"], "B")
        self.assertEqual((await self.dispatch("B")).status, 409)
        self.assertEqual((await self.prepare("C")).status, 409)
        self.assertTrue(session.work.empty())
        sender.assert_awaited_once()
        release.set()
        await asyncio.wait_for(session.work.join(), 1)
        responses = await asyncio.gather(self.dispatch("B"), self.dispatch("B"))
        self.assertEqual([r.status for r in responses], [200, 200])
        await asyncio.wait_for(session.work.join(), 1)
        self.assertEqual(sender.await_count, 2)
        self.assertEqual(self.tts.await_count, 2)
        self.assertEqual((await (await self.get_job("B")).json())["status"], "sent")

    async def test_concurrent_prepare_reservation_and_same_id_response_loss_never_resynthesize(self):
        await self.begin()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return await self.synthesize(*args, **kwargs)
        self.tts.side_effect = slow
        first, second = await asyncio.gather(self.prepare("one"), self.prepare("two"))
        self.assertEqual(sorted([first.status, second.status]), [200, 409])
        winning = "one" if first.status == 200 else "two"
        await asyncio.wait_for(entered.wait(), 1)
        # A lost HTTP response is repaired by querying/repeating the same ID.
        self.assertEqual((await self.prepare(winning)).status, 200)
        pending = await (await self.get_job(winning)).json()
        self.assertTrue(pending["preparation_pending"])
        self.assertFalse(pending["worker_pending"])
        release.set()
        await self.settled(winning)
        await self.dispatch(winning)
        await self.studio.current.work.join()
        self.assertEqual((await self.dispatch(winning)).status, 200)
        self.tts.assert_awaited_once()
        self.studio.current.component.send_aligned_expression.assert_awaited_once()

    async def test_mode_and_deadlines_are_frozen_and_motion_cannot_use_prepare(self):
        await self.begin()
        deadline = time.time() + 60
        await self.prepare(finish_before=deadline)
        await self.settled()
        self.assertEqual((await self.prepare(finish_before=deadline + 1)).status, 409)
        self.assertEqual((await self.prepare()).status, 409)
        self.assertEqual((await self.submit(finish_before=deadline)).status, 409)
        self.assertEqual((await self.prepare("motion", mode="motion", plan={})).status, 400)
        await self.post("external/jobs/round:1/cancel", self.identity)
        await self.submit("legacy")
        await self.studio.current.work.join()
        self.assertEqual((await self.prepare("legacy")).status, 409)
        self.assertEqual((await self.dispatch("legacy")).status, 409)

    async def test_legacy_tts_still_excludes_next_and_prepared_next_excludes_legacy(self):
        await self.begin()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return await self.synthesize(*args, **kwargs)
        self.tts.side_effect = slow
        await self.submit("legacy")
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual((await self.prepare("next")).status, 409)
        release.set()
        await self.studio.current.work.join()
        await self.prepare("next")
        await self.settled("next")
        self.assertEqual((await self.submit("other-legacy")).status, 409)
        self.assertEqual(self.tts.await_count, 2)

    async def test_cancelled_paid_tts_keeps_slot_and_accounts_late_result_exactly_once(self):
        await self.begin()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            result = await self.synthesize(*args, **kwargs)
            result.update(usage_verified=True, input_tokens=7, output_tokens=3, cost_cny=.002)
            return result
        self.tts.side_effect = slow
        await self.prepare()
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(2):
            result = await (await self.post("external/jobs/round:1/cancel", self.identity)).json()
            self.assertEqual(result["status"], "cancelled")
            self.assertTrue(result["preparation_pending"])
            self.assertFalse(result["worker_pending"])
        self.assertEqual((await self.prepare("replacement")).status, 409)
        self.assertEqual((await self.dispatch()).status, 200)  # Terminal query, not a second attempt.
        release.set()
        final = await self.settled()
        self.assertFalse(final["preparation_pending"])
        self.assertEqual(self.studio.current.totals["input_tokens"], 7)
        self.assertEqual(self.studio.current.totals["cost_cny"], .002)
        self.tts.assert_awaited_once()
        self.studio.current.component.send_aligned_expression.assert_not_awaited()
        self.studio.current.timed_action_planner.assert_not_awaited()
        self.assertEqual(self.studio.current._prepared_comments, {})
        self.assertEqual((await self.prepare("replacement")).status, 200)
        await self.settled("replacement")

    async def test_stop_requests_cloud_before_waiting_for_tts_and_reclaims_all_tasks(self):
        await self.begin()
        entered, release, stop_called = asyncio.Event(), asyncio.Event(), asyncio.Event()
        session = self.studio.current
        original_stop = session.component.stop
        async def cloud_stop(reason):
            stop_called.set()
            return await original_stop(reason)
        session.component.stop = AsyncMock(side_effect=cloud_stop)
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return await self.synthesize(*args, **kwargs)
        self.tts.side_effect = slow
        await self.prepare()
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual((await self.post("external/stop", self.identity)).status, 200)
        await asyncio.wait_for(stop_called.wait(), 1)
        self.assertTrue(session.stopping)
        self.assertFalse(release.is_set())
        self.assertTrue(self.studio.external_game.jobs["round:1"]["item"]["external_preparation_pending"])
        self.assertEqual((await self.prepare("after-stop")).status, 409)
        release.set()
        await self.settled()
        if session.background:
            await asyncio.gather(*tuple(session.background))
        self.assertEqual(session._external_preparation_tasks, set())
        self.assertEqual(session._preparing_originals, {})
        self.assertEqual(session._timed_action_tasks, set())
        session.component.send_aligned_expression.assert_not_awaited()
        session.component.stop.assert_awaited_once()
        self.assertEqual(session.totals["audio_seconds"], .1)

    async def test_failed_next_never_changes_current_identity_receipt_or_error_latch(self):
        await self.begin()
        session = self.studio.current
        entered, release = asyncio.Event(), asyncio.Event()
        async def sending(*args, **kwargs):
            entered.set()
            await release.wait()
            return await session.component._send(*args, **kwargs)
        session.component.send_aligned_expression.side_effect = sending
        await self.prepare("A")
        await self.settled("A")
        await self.dispatch("A")
        await asyncio.wait_for(entered.wait(), 1)
        item = self.studio.external_game.jobs["A"]["item"]
        before = (session.component.comment_id, session.component.job_id, item["status"], copy.deepcopy(item["motion_receipts"]))
        self.tts.side_effect = RuntimeError("fake synthesis failure")
        await self.prepare("B")
        failed = await self.settled("B")
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["failure_code"], "preparation_failed")
        self.assertEqual((session.component.comment_id, session.component.job_id, item["status"], item["motion_receipts"]), before)
        self.assertEqual(self.studio.automation.last_error, "")
        self.assertFalse(session.stopping)
        release.set()
        await session.work.join()
        self.assertEqual(item["status"], "sent")

    async def test_cache_identity_pcm_alignment_and_plan_tampering_fail_closed_without_new_tts(self):
        await self.begin()
        session = self.studio.current
        mutations = {
            "voice": lambda item, cache: setattr(session, "tts_voice", "different_voice"),
            "avatar": lambda item, cache: setattr(session, "avatar_id", "different_avatar"),
            "persona": lambda item, cache: setattr(session, "persona_epoch", session.persona_epoch + 1),
            "pcm": lambda item, cache: cache["segments"].__setitem__(0, (b"different_pcm", cache["segments"][0][1])),
            "alignment": lambda item, cache: item["plan"]["segments"][0].__setitem__("alignment", {"status": "tampered"}),
            "plan": lambda item, cache: item["plan"]["segments"][0].__setitem__("instruction", "换语气"),
            "cache_shape": lambda item, cache: cache.pop("fingerprint"),
            "generation": lambda item, cache: item.__setitem__("external_generation", "old"),
            "owner": lambda item, cache: item.__setitem__("external_owner_id", "other"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                await self.prepare(name)
                self.assertEqual((await self.settled(name))["status"], "prepared")
                item = self.studio.external_game.jobs[name]["item"]
                cache = session._prepared_comments[item["id"]]
                mutate(item, cache)
                result = await (await self.dispatch(name)).json()
                self.assertIn(result["status"], {"error", "cancelled"})
                self.assertFalse(result["delivery_started"])
                self.assertFalse(result["worker_pending"])
        self.assertEqual(self.tts.await_count, len(mutations))
        session.component.send_aligned_expression.assert_not_awaited()
        self.assertEqual(self.studio.automation.last_error, "")

    async def test_cache_missing_after_dispatch_never_falls_back_to_paid_tts(self):
        await self.begin()
        session = self.studio.current
        await self.prepare()
        await self.settled()
        session.worker.cancel()
        await asyncio.gather(session.worker, return_exceptions=True)
        await self.dispatch()
        item = self.studio.external_game.jobs["round:1"]["item"]
        # Even a changed public status must not weaken requires_prepared.
        item["status"] = "ready"
        session._prepared_comments.clear()
        session.worker = asyncio.create_task(session.process_answers())
        await asyncio.wait_for(session.work.join(), 1)
        result = await (await self.get_job()).json()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failure_code"], "prepared_cache_invalid")
        self.assertFalse(result["worker_pending"])
        self.tts.assert_awaited_once()
        session.component.send_aligned_expression.assert_not_awaited()

    async def test_prepared_status_alone_is_insufficient_until_preparation_task_ends(self):
        await self.begin()
        session = self.studio.current
        published, release = asyncio.Event(), asyncio.Event()
        original_prepare = session.prepare_comment
        async def delay_cleanup(item):
            result = await original_prepare(item)
            published.set()
            await release.wait()
            return result
        session.prepare_comment = delay_cleanup
        await self.prepare()
        await asyncio.wait_for(published.wait(), 1)
        self.assertEqual((await self.dispatch()).status, 409)
        session.component.send_aligned_expression.assert_not_awaited()
        release.set()
        await self.settled()
        self.assertEqual((await self.dispatch()).status, 200)
        await session.work.join()
        self.tts.assert_awaited_once()

    async def test_expiry_and_actual_pcm_finish_budget_are_rechecked_at_dispatch(self):
        await self.begin()
        now = time.time()
        for name, deadlines in (("expires", {"expires_at": now + 30}),
                                ("finishes", {"finish_before": now + 10})):
            await self.prepare(name, **deadlines)
            await self.settled(name)
            with patch("external_game.time.time", return_value=now + 31):
                result = await (await self.dispatch(name)).json()
            self.assertEqual(result["status"], "expired")
            self.assertFalse(result["delivery_started"])
        await self.prepare("too-short", finish_before=time.time() + 1)
        self.assertEqual((await self.settled("too-short"))["status"], "expired")
        self.assertEqual(self.tts.await_count, 3)
        self.studio.current.component.send_aligned_expression.assert_not_awaited()

    async def test_identity_change_during_late_tts_never_publishes_or_plans_actions(self):
        await self.begin()
        self.action_tts()
        synthesize = self.tts.side_effect
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return await synthesize(*args, **kwargs)
        self.tts.side_effect = slow
        await self.prepare(text="别急。", plan=self.performance_plan())
        await asyncio.wait_for(entered.wait(), 1)
        self.studio.current.tts_voice = "changed_while_synthesizing"
        release.set()
        result = await self.settled()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.studio.current._prepared_comments, {})
        self.studio.current.timed_action_planner.assert_not_awaited()
        self.studio.current.component.send_aligned_expression.assert_not_awaited()

    async def test_cancel_dispatch_race_and_lost_cancel_response_do_not_duplicate_delivery(self):
        await self.begin()
        await self.prepare()
        await self.settled()
        responses = await asyncio.gather(self.dispatch(), self.post("external/jobs/round:1/cancel", self.identity))
        self.assertEqual([r.status for r in responses], [200, 200])
        await self.studio.current.work.join()
        response = await (await self.post("external/jobs/round:1/cancel", self.identity)).json()
        self.assertIn(response["status"], {"sent", "cancelled"})
        self.assertFalse(response["worker_pending"])
        self.assertFalse(response["preparation_pending"])
        await self.dispatch()
        await self.studio.current.work.join()
        self.assertLessEqual(self.studio.current.component.send_aligned_expression.await_count, 1)
        self.tts.assert_awaited_once()

    async def test_final_gate_rejects_voice_change_after_dispatch_without_sending_or_resynthesizing(self):
        await self.begin()
        session = self.studio.current
        await self.prepare()
        await self.settled()
        original_begin = session._begin_delivery
        def change_at_final_gate(item):
            session.tts_voice = "changed_at_final_gate"
            return original_begin(item)
        session._begin_delivery = change_at_final_gate
        await self.dispatch()
        await session.work.join()
        result = await (await self.get_job()).json()
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["failure_code"], "prepared_cache_invalid")
        self.assertFalse(result["delivery_started"])
        self.tts.assert_awaited_once()
        session.component.send_aligned_expression.assert_not_awaited()

    async def test_shutdown_also_drains_old_automation_preparation_before_closing_http(self):
        # 旧主讲准备不在 external tasks 中；同样必须在共享 AI/场次 HTTP 关闭前收账。
        self.live()
        session = self.studio.current
        entered, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_stop = session.component.stop
        async def stop(reason):
            stopped.set()
            return await original_stop(reason)
        session.component.stop = AsyncMock(side_effect=stop)
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            self.assertFalse(session.http.closed)
            self.ai.close.assert_not_awaited()
            return await self.synthesize(*args, **kwargs)
        self.tts.side_effect = slow
        item = self.studio.enqueue_comment("旧主讲尚在合成。", "主持")
        item["status"] = "processing"
        self.studio.accept_plan(session, item, self.studio.external_game.frozen_plan(item["text"]), enqueue=False)
        self.studio.automation.reply_task = asyncio.create_task(session.prepare_comment(item))
        await asyncio.wait_for(entered.wait(), 1)
        closing = asyncio.create_task(self.client.close())
        await asyncio.wait_for(stopped.wait(), 1)
        self.assertFalse(session.http.closed)
        self.assertFalse(closing.done())
        release.set()
        await asyncio.wait_for(closing, 1)
        self.assertEqual(item["status"], "cancelled")
        self.assertEqual(session.totals["audio_seconds"], .1)
        self.assertTrue(session.http.closed)
        self.ai.close.assert_awaited_once()

    async def test_shutdown_keeps_shared_planner_open_until_paid_preparation_finishes(self):
        await self.begin()
        entered, release, cloud_stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
        session = self.studio.current
        original_stop = session.component.stop
        async def stop(reason):
            cloud_stopped.set()
            return await original_stop(reason)
        session.component.stop = AsyncMock(side_effect=stop)
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            self.ai.close.assert_not_awaited()
            return await self.synthesize(*args, **kwargs)
        self.tts.side_effect = slow
        await self.prepare()
        await asyncio.wait_for(entered.wait(), 1)
        closing = asyncio.create_task(self.client.close())
        await asyncio.wait_for(cloud_stopped.wait(), 1)
        self.ai.close.assert_not_awaited()
        self.assertFalse(closing.done())
        release.set()
        await asyncio.wait_for(closing, 1)
        self.ai.close.assert_awaited_once()
        self.assertEqual(session._external_preparation_tasks, set())
        self.assertTrue(session.http.closed)


if __name__ == "__main__":
    unittest.main()
