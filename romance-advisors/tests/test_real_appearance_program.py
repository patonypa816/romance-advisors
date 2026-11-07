"""Program lifecycle uses the real opt-in contract; component writes are offline."""
import asyncio
import copy
import unittest

from appearance_studio_double import OfflineStudioAvatar
from romance.server import Workbench
from test_server import SyntheticModel

KEYS = ("fish", "rough", "tired", "normal", "refined", "stellar")


class ManualAppearanceAvatar(OfflineStudioAvatar):
    supports_simulated_appearance = False
    supports_real_appearance = True

    def __init__(self, initial="normal"):
        super().__init__(drain_seconds=0)
        self.confirmed_appearance_key = initial
        self.broadcast_key = initial
        self.available = True
        self.lifecycle = []
        self.status_calls = 0

    async def appearance_status(self):
        self.status_calls += 1
        key = self.confirmed_appearance_key
        return {"ready": self.available, "enabled": self.available,
                "message": "" if self.available else "appearance_assets_not_ready",
                "confirmed_key": key, "broadcast_key": self.broadcast_key,
                "portrait": {"key": key, "description": key + "的样子", "simulated": False},
                "version": len(self.switches)}

    async def appearance_lifecycle(self, record, action):
        self.lifecycle.append({**copy.deepcopy(record), "action": action})
        return await self.appearance_status()

    async def switch_appearance(self, record):
        old = self.switches.get(record["request_id"])
        if old:
            if old["record"] != record:
                raise ValueError("changed request")
            return copy.deepcopy(old["receipt"])
        target = record["target_key"]
        asset = str(2000 + KEYS.index(target))
        direction = "up" if KEYS.index(target) > KEYS.index(self.confirmed_appearance_key) else "down"
        raw = await self.component.switch_avatar(asset, ["https://example.com/offline-" + direction + ".mp4"],
                                                  record["request_id"], lambda: self.state != "ended")
        receipt = {**copy.deepcopy(record), "status": "written", "sent": raw["sent"],
                   "simulated": False, "seq_id": raw["seq_id"], "direction": direction,
                   "from_key": self.confirmed_appearance_key}
        self.switches[record["request_id"]] = {"record": copy.deepcopy(record), "receipt": receipt}
        return copy.deepcopy(receipt)

    async def confirm_appearance(self, record, *, outcome="target_dynamic"):
        receipt = self.switches[record["request_id"]]["receipt"]
        if outcome == "target_dynamic":
            self.confirmed_appearance_key = record["target_key"]
            receipt.update(status="confirmed", confirmed_key=self.confirmed_appearance_key)
        else:
            receipt.update(status="retained", confirmed_key=receipt["from_key"])
        return copy.deepcopy(receipt)


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(.005)


class ProgramAppearanceContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.avatar = ManualAppearanceAvatar()
        self.wb = Workbench(":memory:", model=SyntheticModel(), avatar_client=self.avatar)
        self.wb.appearance_requested = lambda: True
        self.started, self.ended, self.operations = [], [], []
        self.wb.appearance_episode_started = self.started.append
        self.wb.appearance_episode_ended = self.ended.append
        self.wb.appearance_operation = lambda **fields: self.operations.append(copy.deepcopy(fields))
        self.program = self.wb.program

    async def asyncTearDown(self):
        await self.wb.close()

    async def start(self):
        self.program.start()
        await self.program.task
        if self.wb.job:
            await self.wb.job

    async def pending(self, *, after=None):
        await until(lambda: self.program.appearance_status()["pending_confirmation"] is not None
                    and self.program.appearance_status()["pending_confirmation"]["request_id"] != after)
        return self.program.appearance_status()["pending_confirmation"]

    async def test_opt_in_missing_configuration_never_starts_paid_session(self):
        self.avatar.available = False
        await self.start()
        self.assertNotIn("start", self.avatar.calls)
        self.assertEqual(self.program.state, "error")
        self.assertEqual(self.program.appearance_status()["message"], "appearance_assets_not_ready")
        self.assertIn("关闭造型效果", self.program.error)
        self.assertEqual(self.avatar.switches, {})

    async def test_disabled_keeps_original_start_and_does_not_read_appearance(self):
        self.wb.appearance_requested = lambda: False
        await self.start()
        self.assertIn("start", self.avatar.calls)
        self.assertEqual(self.avatar.status_calls, 0)
        self.assertEqual(self.avatar.switches, {})

    async def test_stellar_initial_requires_normal_dynamic_confirmation_before_50(self):
        self.avatar.confirmed_appearance_key = self.avatar.broadcast_key = "stellar"
        self.program.start()
        await self.program.task
        record = await self.pending()
        self.assertEqual(record["target_key"], "normal")
        self.assertEqual(record["direction"], "down")
        self.assertEqual(self.program.confirmed_appearance_key, "stellar")
        self.assertEqual(self.started, [])
        self.assertFalse(self.program.appearance_status()["active"])
        self.assertFalse(self.wb.job.done())
        with self.assertRaises(ValueError):
            await self.program.confirm_appearance("wrong_request")
        await self.program.confirm_appearance(record["request_id"])
        await self.wb.job
        self.assertEqual(self.started, [self.wb.game.episode["id"]])
        self.assertTrue(self.program.appearance_status()["active"])
        self.assertEqual(self.program.message_appearance()["key"], "normal")
        self.assertEqual(len(self.avatar.switches), 1)

    async def test_bind_await_holds_optional_admission(self):
        original = self.avatar.appearance_lifecycle
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed_bind(record, action):
            if action == "bind":
                entered.set()
                await release.wait()
            return await original(record, action)
        self.avatar.appearance_lifecycle = delayed_bind
        self.program.start()
        await self.program.task
        await asyncio.wait_for(entered.wait(), 2)
        self.assertTrue(self.program._appearance_busy)
        self.assertIsNone(self.program.reserve_next(kind="proactive", valid=lambda: True))
        self.assertIsNone(self.program.current)
        self.assertIsNone(self.program.next)
        release.set()
        await self.wb.job
        self.assertTrue(self.program.appearance_status()["active"])

    async def test_six_targets_merge_and_receipt_polling_never_persists_duplicates(self):
        await self.start()
        self.program.request_appearance("refined")
        first = await self.pending()
        self.program.request_appearance("tired")
        self.program.request_appearance("fish")
        await asyncio.sleep(.25)
        self.assertEqual(len(self.avatar.switches), 1)
        written = [r for r in self.operations if r.get("status") == "written"]
        self.assertEqual(len(written), 1)
        await self.program.confirm_appearance(first["request_id"])
        second = await self.pending(after=first["request_id"])
        self.assertEqual(second["target_key"], "fish")
        self.assertEqual(second["direction"], "down")
        await self.program.confirm_appearance(second["request_id"])
        await self.program.wait_for_appearance()
        self.assertEqual(self.program.confirmed_appearance_key, "fish")
        self.program.request_appearance("fish")
        await self.program.wait_for_appearance()
        self.assertEqual(len(self.avatar.switches), 2)

    async def test_mode_handoff_waits_current_confirmation_then_restores_broadcast(self):
        self.avatar.broadcast_key = "stellar"
        await self.start()
        episode = self.wb.game.episode["id"]
        self.program.request_appearance("refined")
        first = await self.pending()
        self.program.set_mode(self.program.run_id, "chat")
        await until(lambda: any(r["action"] == "release" for r in self.avatar.lifecycle))
        self.assertIn(episode, self.ended)
        self.assertEqual(self.program.content_mode, "to_chat")
        await self.program.confirm_appearance(first["request_id"])
        restore = await self.pending(after=first["request_id"])
        self.assertEqual((restore["target_key"], restore["purpose"]), ("stellar", "restore"))
        self.assertEqual(self.program.content_mode, "to_chat")
        await self.program.confirm_appearance(restore["request_id"])
        await self.program._mode_task
        self.assertEqual(self.program.content_mode, "chat")
        self.assertEqual(self.program.confirmed_appearance_key, "stellar")
        self.assertFalse(self.program.appearance_status()["active"])
        with self.assertRaises(ValueError):
            self.program.request_appearance("normal")

    async def test_explicit_failure_pauses_immediately_without_waiting_for_a_click(self):
        await self.start()
        self.program.request_appearance("refined")
        request = await self.pending()
        self.avatar.switches[request["request_id"]]["receipt"]["status"] = "failed"
        await self.program.wait_for_appearance()
        self.assertTrue(self.program.sender_failed)
        self.assertEqual(self.program.confirmed_appearance_key, "normal")
        self.assertEqual(len(self.avatar.switches), 1)
        with self.assertRaises(ValueError):
            self.program.request_appearance("refined")

    async def test_failed_restore_never_releases_chat_or_retries(self):
        self.avatar.broadcast_key = "stellar"
        await self.start()
        self.program.request_appearance("refined")
        first = await self.pending()
        await self.program.confirm_appearance(first["request_id"])
        await self.program.wait_for_appearance()
        self.program.set_mode(self.program.run_id, "chat")
        restore = await self.pending(after=first["request_id"])
        self.avatar.switches[restore["request_id"]]["receipt"]["status"] = "failed"
        await self.program._mode_task
        self.assertEqual(self.program.state, "error")
        self.assertNotEqual(self.program.content_mode, "chat")
        self.assertEqual(self.program.confirmed_appearance_key, "refined")
        self.assertIsNotNone(self.program._appearance_context)
        self.assertTrue(self.program.sender_failed)
        self.assertEqual(len(self.avatar.switches), 2)
        self.assertIn("stop", self.avatar.calls)

    async def test_formal_speech_and_typing_pair_never_admits_type16(self):
        await self.start()
        game = self.wb.game
        game.submit_proposal("advisor", "军师", "约她周六见面")
        await game.close_collection()
        game.vote("viewer", "观众", "1")
        speech = game.current_round["id"] + ":reaction"
        typing = speech + ":typing"
        self.avatar.hold(speech, "pcm")
        self.avatar.hold(typing, "Q")
        task = asyncio.create_task(game.close_voting())
        async def cleanup():
            self.avatar.release(speech, "pcm")
            self.avatar.release(typing, "Q")
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.addAsyncCleanup(cleanup)
        await self.avatar.wait_node(speech, "pcm")
        self.program.request_appearance("refined")
        self.avatar.release(speech, "pcm")
        await self.avatar.wait_node(typing, "before_Q")
        self.assertEqual(self.avatar.switches, {})
        self.avatar.release(typing, "Q")
        pending = await self.pending()
        await self.program.confirm_appearance(pending["request_id"])
        await asyncio.wait_for(task, 5)
        wire = self.avatar.wire_events
        order = [r["order"] for r in wire if r.get("type") == 16]
        self.assertEqual(len(order), 1)
        bounds = self.avatar.boundaries
        self.assertEqual([r["job_id"] for r in bounds], [speech, typing])
        self.assertGreater(order[0], bounds[-1]["end_order"])

    async def test_stop_never_sends_restore_or_new_target(self):
        await self.start()
        self.program.request_appearance("refined")
        await self.pending()
        self.program.request_appearance("fish")
        await self.program.stop()
        self.assertEqual(len(self.avatar.switches), 1)
        self.assertIn("stop", self.avatar.calls)
        self.assertFalse(self.program.appearance_status()["active"])
        self.assertTrue(self.program.cleanup["cloud_stop_confirmed"])
