"""Normal real-source contract; temp SQLite, production writer, memory transport only."""
import asyncio
import copy
from pathlib import Path
import tempfile
import time
import unittest

from appearance_studio_double import OfflineStudioAvatar, ASSET_IDS, SWITCH_VIDEOS
from romance.appearance import appearance_snapshot
from romance.dyhub_intake import DyhubIntake
from romance.server import Workbench
from test_appearance_round import PortraitReplyModel


class ManualConfirmStudio(OfflineStudioAvatar):
    supports_simulated_appearance = False
    supports_real_appearance = True

    def __init__(self):
        super().__init__()
        self.version = 0
        self.appearance_context = None

    async def appearance_status(self):
        return {"ready": True, "enabled": True, "confirmed_key": self.confirmed_appearance_key,
                "broadcast_key": "normal", "version": self.version,
                "portrait": dict(appearance_snapshot(self.confirmed_appearance_key, simulated=False), version=self.version)}

    async def appearance_lifecycle(self, context, action):
        if action == "bind":
            self.appearance_context = copy.deepcopy(context)
        return await self.appearance_status()

    async def switch_appearance(self, record):
        ident = record["request_id"]
        if ident in self.switches:
            assert self.switches[ident]["record"] == record
            return copy.deepcopy(self.switches[ident]["receipt"])
        assert {k: record[k] for k in self.appearance_context} == self.appearance_context
        await self.component._wait(max(0, self.component._motion_not_before - time.monotonic()))
        sent = await self.component.switch_avatar(ASSET_IDS[record["target_key"]], SWITCH_VIDEOS, ident, lambda: True)
        receipt = {**copy.deepcopy(record), "status": "written", "simulated": False,
                   "sent": sent["sent"], "seq_id": sent["seq_id"], "confirmed_key": self.confirmed_appearance_key,
                   "evidence": "memory_transport_only", "playback_confirmed": False}
        self.switches[ident] = {"record": copy.deepcopy(record), "receipt": receipt}
        return copy.deepcopy(receipt)

    async def confirm_appearance(self, record, *, outcome="target_dynamic"):
        receipt = self.switches[record["request_id"]]["receipt"]
        assert receipt["status"] == "written" and outcome == "target_dynamic"
        asset = self.socket.received_assets[receipt["seq_id"]]
        self.confirmed_appearance_key = next(k for k, v in ASSET_IDS.items() if v == asset)
        self.version += 1
        receipt.update(status="confirmed", confirmed_key=self.confirmed_appearance_key,
                       playback_confirmed=True, evidence="synthetic_operator_confirmation")
        return copy.deepcopy(receipt)


class LiveIntakeRoundTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="appearance-real-contract-")
        self.addCleanup(self.temp.cleanup)
        self.studio, self.model = ManualConfirmStudio(), PortraitReplyModel()
        self.model.inspect_at_reply = lambda: None
        self.wb = Workbench(Path(self.temp.name) / "game.sqlite", model=self.model, avatar_client=self.studio)
        self.addAsyncCleanup(self.wb.close)
        self.wb.appearance.configure({"enabled": True, "room_id": "123", "positive_gift_ids": ["101"],
                                     "negative_gift_ids": ["102"], "quantity_verified": True})
        self.program, self.game = self.wb.program, self.wb.game
        self.program.start()
        await self.program.task
        await self.wb.job
        self.assertIsNone(self.game.data["error"])
        self.assertTrue(self.wb.appearance.service.snapshot()["active"])
        self.intake = DyhubIntake(self.wb)
        self.intake.scope, self.intake.room_id, self.intake.started_at = "broadcast", "123", time.time()
        self.intake.stop_event = asyncio.Event()

    async def gift(self, message, gift_id, price, count=1):
        now = time.time() * 1000
        return await self.intake.on_event({"type": "gift", "platform": "douyin", "roomId": "123", "id": message,
            "ts": now, "receivedAt": now, "user": {"id": "real-source-viewer"},
            "data": {"giftId": gift_id, "diamondCount": price, "repeatCount": count,
                     "groupId": "group-" + gift_id, "sourceTimeKnown": True}})

    async def pending(self, key):
        async with asyncio.timeout(20):
            while True:
                receipt = self.program.appearance_status()["pending_confirmation"]
                if receipt and receipt["target_key"] == key:
                    return receipt
                if self.program.sender_failed:
                    self.fail(str(self.program.last_failure))
                await asyncio.sleep(.01)

    async def test_normal_intake_frozen_portrait_and_manual_confirmation(self):
        self.assertEqual((await self.gift("up-1", "101", 6))["score"], 56)
        self.assertEqual((await self.gift("up-2", "101", 6, 2))["score"], 62)
        request = await self.pending("refined")
        self.assertEqual(request["status"], "written")
        await asyncio.sleep(.2)
        self.assertEqual(self.program.confirmed_appearance_key, "normal")
        await self.program.confirm_appearance(request["request_id"])
        await self.program.wait_for_appearance()
        self.assertEqual(self.program.confirmed_appearance_key, "refined")
        self.game.submit_proposal("advisor", "军师", "约她周六见面")
        await self.game.close_collection()
        self.game.vote("viewer", "观众", "1")
        speech = self.game.current_round["id"] + ":reaction"
        typing = speech + ":typing"
        self.studio.hold(typing, "Q")
        round_task = asyncio.create_task(self.game.close_voting())

        async def cleanup_round():
            self.studio.release(typing, "Q")
            if not round_task.done():
                round_task.cancel()
            await asyncio.gather(round_task, return_exceptions=True)
        self.addAsyncCleanup(cleanup_round)
        async with asyncio.timeout(20):
            while True:
                bubbles = [m for m in self.game.public_state()["messages"] if m["role"] == "man"]
                if bubbles:
                    break
                if round_task.done():
                    self.fail(str(self.game.data["error"]))
                await asyncio.sleep(.01)
        bubble = bubbles[0]
        self.assertEqual(bubble["portrait"]["key"], "refined")
        self.assertFalse(bubble["portrait"]["simulated"])
        self.assertEqual((await self.gift("down-1", "102", 12))["score"], 50)
        self.assertFalse((await self.gift("down-1", "102", 12))["accepted"])
        self.assertFalse((await self.gift("down-end", "102", 12))["accepted"])
        self.assertEqual(self.program.confirmed_appearance_key, "refined")
        self.assertFalse(self.studio.node_event(typing, "Q").is_set())
        self.studio.release(typing, "Q")
        request = await self.pending("normal")
        self.assertEqual(self.program.confirmed_appearance_key, "refined")
        self.assertFalse(any(task == "reply" for task, _ in self.model.calls))
        self.assertFalse(round_task.done())
        await self.program.confirm_appearance(request["request_id"])
        state = await asyncio.wait_for(round_task, 20)
        self.assertIsNone(state["error"], self.program.last_failure)
        self.assertEqual(self.program.confirmed_appearance_key, "normal")
        reply = next(payload for task, payload in self.model.calls if task == "reply")
        self.assertEqual(reply["reply_to_message"]["portrait"], bubble["portrait"])
        self.assertIn("梳起来", state["messages"][-1]["text"])
        saved = self.game.storage.load()["rounds"][-1]["man_appearance"]
        self.assertEqual(saved["portrait"], bubble["portrait"])
        self.assertEqual(self.wb.appearance.service.snapshot()["score"], 50)
        self.assertEqual(len(self.studio.switches), 2)
        switches = [row["order"] for row in self.studio.wire_events if row.get("type") == 16]
        first, second = self.studio.boundaries
        self.assertFalse(any(first["start_order"] <= order <= second["end_order"] for order in switches))
        self.assertGreater(switches[-1], second["end_order"])
        self.assertEqual(self.game.storage.db.execute("SELECT COUNT(*) FROM appearance_receipts").fetchone()[0], 4)


if __name__ == "__main__":
    unittest.main()
