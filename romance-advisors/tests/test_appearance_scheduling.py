"""Offline barriers prove appearance writes cannot split a formal pair."""
import asyncio
import copy
import unittest
from unittest.mock import patch

from romance.server import Workbench
from test_ambient import AmbientModel, REAL_SLEEP, yield_only
from test_speech_pipeline import BarrierAvatar


class AppearanceAvatar(BarrierAvatar):
    supports_simulated_appearance = True

    def __init__(self):
        super().__init__()
        self.switches, self.switch_queries = [], []
        self.appearance_status = "confirmed"
        self.lose_switch_reply = False
        self.switch_receipts = {}
        self.program = None

    async def switch_appearance(self, record):
        p = self.program
        assert not self.active_sender and p.current is None and p.next is None and not p.formal_waiting
        self.switches.append(copy.deepcopy(record))
        self.trace.append(("switch", record["target_key"]))
        self.switch_receipts[record["request_id"]] = dict(record, simulated=True)
        if self.lose_switch_reply:
            self.lose_switch_reply = False
            raise OSError("lost simulated reply")
        return await self.appearance(record)

    async def appearance(self, record):
        self.switch_queries.append(record["request_id"])
        return dict(self.switch_receipts[record["request_id"]], status=self.appearance_status)


class AppearanceSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.avatar = AppearanceAvatar()
        self.wb = Workbench(":memory:", model=AmbientModel(), avatar_client=self.avatar)
        self.p = self.avatar.program = self.wb.program
        self.tasks = []
        self.patcher = patch("romance.program.asyncio.sleep", yield_only)
        self.patcher.start()
        self.p.start()
        await self.p.task
        await self.wb.job
        self.wb.state()

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.wb.close()
        self.patcher.stop()

    async def until(self, condition):
        for _ in range(300):
            if condition():
                return
            await REAL_SLEEP(0)
        self.fail("offline barrier not reached")

    async def test_latest_target_is_merged_before_any_write(self):
        self.p.request_appearance("refined")
        self.p.request_appearance("normal")
        await self.p.wait_for_appearance()
        self.assertEqual(self.avatar.switches, [])
        self.assertEqual(self.p.confirmed_appearance_key, "normal")

    async def test_formal_speech_gap_and_typing_tail_are_one_protected_pair(self):
        self.p.request_appearance("refined")
        await self.p.wait_for_appearance()
        seen = []
        record = self.p.prepare("appearance-round", "军师的正式回复。")
        task = asyncio.create_task(self.p.perform(record, "军师的正式回复。",
            on_message_action=lambda: seen.append(self.p.message_appearance())))
        self.tasks.append(task)
        speech, typing = record["job_id"], record["job_id"] + ":typing"
        await self.until(lambda: speech in self.avatar.dispatches)
        self.p.request_appearance("normal")
        self.avatar.jobs[speech].update(status="sent", send_complete=True)
        for _ in range(10):
            await REAL_SLEEP(0)
        self.assertEqual(len(self.avatar.switches), 1)  # Worker still owes speech tail.
        self.avatar.finish(speech)
        await self.until(lambda: typing in self.avatar.dispatches)
        self.assertEqual(len(self.avatar.switches), 1)  # Includes the inter-job gap.
        self.avatar.jobs[typing].update(short_action_sent=True, delivery_started=True)
        await self.until(lambda: bool(seen))
        self.assertEqual(seen[0]["key"], "refined")
        for _ in range(10):
            await REAL_SLEEP(0)
        self.assertEqual(len(self.avatar.switches), 1)  # S is not Q/worker completion.
        self.avatar.finish(typing)
        await task
        self.assertEqual(self.p.confirmed_appearance_key, "normal")
        self.assertEqual([r["target_key"] for r in self.avatar.switches], ["refined", "normal"])
        self.assertEqual(seen[0]["key"], "refined")  # The published snapshot stays frozen.

    async def test_current_optional_finishes_and_unstarted_optional_is_revoked(self):
        current = self.p.reserve_next(kind="proactive", valid=lambda: True)
        record = {"job_id": "current-optional", "owner_id": self.p.run_id,
                  "generation": self.avatar.generation}
        task = self.p.schedule_candidate(current, self.p.send(record, "闲聊", optional=True, candidate=current))
        self.tasks.append(task)
        await self.until(lambda: record["job_id"] in self.avatar.dispatches)
        self.p.request_appearance("refined")
        self.assertIsNone(self.p.reserve_next(kind="proactive", valid=lambda: True))
        for _ in range(10):
            await REAL_SLEEP(0)
        self.assertEqual(self.avatar.switches, [])
        self.avatar.finish(record["job_id"])
        await task
        await self.p.wait_for_appearance()
        self.assertEqual(len(self.avatar.switches), 1)

        # A prepared optional next must cancel by the same job ID, never dispatch.
        self.avatar.blocked.add("prepared-optional")
        candidate = self.p.reserve_next(kind="proactive", valid=lambda: True)
        second = dict(record, job_id="prepared-optional")
        optional = self.p.schedule_candidate(candidate, self.p.send(second, "未发送闲聊", optional=True,
                                                                    candidate=candidate))
        self.tasks.append(optional)
        await self.until(lambda: "prepared-optional" in self.avatar.jobs)
        self.p.request_appearance("normal")
        await self.until(lambda: self.avatar.jobs["prepared-optional"]["status"] == "cancelled")
        self.assertEqual(len(self.avatar.switches), 1)
        self.avatar.finish_prepare("prepared-optional")
        await optional
        await self.p.wait_for_appearance()
        self.assertNotIn("prepared-optional", self.avatar.dispatches)
        self.assertEqual(len(self.avatar.switches), 2)

    async def test_lost_reply_only_queries_the_same_request(self):
        self.avatar.lose_switch_reply = True
        self.p.request_appearance("refined")
        await self.p.wait_for_appearance()
        self.assertEqual(len(self.avatar.switches), 1)
        self.assertEqual(set(self.avatar.switch_queries), {self.avatar.switches[0]["request_id"]})
        self.assertEqual(self.p.confirmed_appearance_key, "refined")

    async def test_written_is_not_confirmation_and_latest_target_waits(self):
        self.avatar.appearance_status = "written"
        self.p.request_appearance("refined")
        await self.until(lambda: bool(self.avatar.switches))
        self.p.request_appearance("normal")
        for _ in range(10):
            await REAL_SLEEP(0)
        self.assertEqual(self.p.confirmed_appearance_key, "normal")
        self.assertEqual(len(self.avatar.switches), 1)
        self.assertIsNone(self.p.reserve_next(kind="proactive", valid=lambda: True))
        self.avatar.appearance_status = "confirmed"
        await self.p.wait_for_appearance()
        self.assertEqual([r["target_key"] for r in self.avatar.switches], ["refined", "normal"])

    async def test_failure_latches_sender_without_false_confirmation_or_retry(self):
        self.avatar.appearance_status = "failed"
        self.p.request_appearance("refined")
        await self.p.wait_for_appearance()
        self.assertEqual(self.p.confirmed_appearance_key, "normal")
        self.assertTrue(self.p.sender_failed)
        self.assertIsNone(self.p.message_appearance())
        await self.p.wait_for_appearance()
        with self.assertRaises(ValueError):
            self.p.request_appearance("refined")
        self.assertEqual(len(self.avatar.switches), 1)

    async def test_real_client_capability_is_required(self):
        self.avatar.supports_simulated_appearance = False
        with self.assertRaises(ValueError):
            self.p.request_appearance("refined")
        self.assertIsNone(self.p.message_appearance())
        self.assertEqual(self.avatar.switches, [])

    async def test_formal_reserved_before_worker_gets_first_turn_runs_first(self):
        self.p.request_appearance("refined")
        record = self.p.prepare("reserved-formal", "先把这句话说完。")
        task = asyncio.create_task(self.p.perform(record, "先把这句话说完。"))
        self.tasks.append(task)
        # Reservation is synchronous within perform, before worker admission.
        # Start perform before giving the already queued worker its next turn.
        self.p.formal_waiting += 1
        await self.until(lambda: record["job_id"] in self.avatar.dispatches)
        self.p.formal_waiting -= 1
        self.assertEqual(self.avatar.switches, [])
        self.avatar.finish(record["job_id"])
        typing = record["job_id"] + ":typing"
        await self.until(lambda: typing in self.avatar.dispatches)
        self.assertEqual(self.avatar.switches, [])
        self.avatar.finish(typing)
        await task
        self.assertEqual(len(self.avatar.switches), 1)

    async def test_mismatched_confirmation_never_updates_visible_face(self):
        original = self.avatar.appearance

        async def wrong_owner(record):
            return dict(await original(record), owner_id="obsolete-owner")

        self.avatar.appearance = wrong_owner
        self.p.request_appearance("refined")
        await self.p.wait_for_appearance()
        self.assertTrue(self.p.sender_failed)
        self.assertEqual(self.p.confirmed_appearance_key, "normal")
        self.assertEqual(len(self.avatar.switches), 1)

    async def test_stop_while_waiting_formal_never_sends_a_switch(self):
        self.p.formal_waiting = 1
        self.p.request_appearance("refined")
        for _ in range(5):
            await REAL_SLEEP(0)
        await self.p.stop()
        self.assertEqual(self.avatar.switches, [])
        self.assertTrue(self.p._appearance_task.done())
