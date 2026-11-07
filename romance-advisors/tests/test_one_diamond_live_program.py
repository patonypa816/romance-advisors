"""Production Program/intake/ledger with offline Studio and temporary SQLite only."""
import asyncio
from pathlib import Path
import tempfile
import time
import unittest

from romance.dyhub_intake import DyhubIntake
from romance.server import Workbench
from test_appearance_gift_probe import HoldClient
from test_real_appearance_program import ManualAppearanceAvatar, until
from test_server import SyntheticModel


class OneDiamondLiveProgramTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="one-diamond-live-offline-")
        self.addCleanup(self.temp.cleanup)
        self.avatar = ManualAppearanceAvatar(initial="stellar")
        self.wb = Workbench(Path(self.temp.name) / "test.sqlite", model=SyntheticModel(), avatar_client=self.avatar)
        self.addAsyncCleanup(self.wb.close)
        self.wb.appearance.configure({"enabled": True, "quantity_verified": False, "room_id": "123",
                                     "positive_gift_ids": ["463"], "negative_gift_ids": ["164"],
                                     "band_starts": {"refined": 53}})
        self.program = self.wb.program
        self.intake = DyhubIntake(self.wb, client_factory=HoldClient)
        self.addAsyncCleanup(self.intake.stop)

    async def pending(self, target):
        await until(lambda: (self.program.appearance_status()["pending_confirmation"] or {}).get("target_key") == target)
        return self.program.appearance_status()["pending_confirmation"]

    async def gift(self, ident, gift_id="463", count=1, **fields):
        now = time.time() * 1000
        return await self.intake.on_event({"type": "gift", "platform": "douyin", "roomId": "123", "id": ident,
            "ts": now, "receivedAt": now, "user": {"id": "offline-real-contract-viewer"},
            "data": {"giftId": gift_id, "diamondCount": 1, "repeatCount": count,
                     "groupId": "combo-" + gift_id, "sourceTimeKnown": True, **fields}})

    async def test_unverified_single_and_combo_use_normal_score_and_confirmed_switch_chain(self):
        # A non-normal opening must still receive the usual dynamic confirmation.
        self.program.start()
        await self.program.task
        opening = await self.pending("normal")
        self.assertFalse(self.wb.appearance.service.snapshot()["active"])
        self.assertEqual(self.program.confirmed_appearance_key, "stellar")
        self.assertFalse(self.wb.job.done())
        await self.program.confirm_appearance(opening["request_id"])
        await self.wb.job
        self.assertTrue(self.wb.appearance.service.snapshot()["active"])
        self.assertEqual(self.wb.appearance.service.snapshot()["score"], 50)
        await self.intake.start("http://127.0.0.1:8757", "123", "broadcast")
        self.assertEqual(self.intake.client.event_types, ("chat", "gift"))
        self.assertFalse(self.wb.appearance.gift_probe.active)
        readiness = self.wb.appearance.snapshot(operator=True)["readiness"]
        self.assertTrue(readiness["ready"])
        self.assertEqual(readiness["live_gift_mode"], "one_diamond_test")
        self.assertFalse(self.wb.appearance.service.settings()["quantity_verified"])

        # Explicit single packets use their stable event ID; combos use cumulative counts.
        self.assertEqual((await self.gift("single", groupId=None, combo=False))["score"], 51)
        self.assertEqual((await self.gift("single", groupId=None, combo=False))["reason"], "duplicate_message")
        self.assertEqual((await self.gift("combo-1"))["score"], 52)
        self.assertEqual((await self.gift("combo-2", count=2))["score"], 53)
        request = await self.pending("refined")
        self.assertEqual(self.program.confirmed_appearance_key, "normal")
        self.assertEqual((await self.gift("combo-end", count=2))["reason"], "duplicate_progress")
        self.assertEqual((await self.gift("bad-price", diamondCount=2))["reason"], "one_diamond_required")
        await self.program.confirm_appearance(request["request_id"])
        await self.program.wait_for_appearance()
        self.assertEqual(self.program.confirmed_appearance_key, "refined")

        self.assertEqual((await self.gift("down", "164", count=2))["score"], 51)
        request = await self.pending("normal")
        self.assertEqual(self.program.confirmed_appearance_key, "refined")
        await self.program.confirm_appearance(request["request_id"])
        await self.program.wait_for_appearance()
        self.assertEqual(self.program.confirmed_appearance_key, "normal")
        self.assertEqual(self.wb.appearance.service.snapshot()["sequence"], 4)
        self.assertEqual(len(self.avatar.switches), 3)
        self.assertEqual(self.intake.counts["gift_accepted"], 4)
        self.assertFalse(self.wb.appearance.service.settings()["quantity_verified"])
        self.assertFalse(self.wb.game.data["comments"])


if __name__ == "__main__":
    unittest.main()
