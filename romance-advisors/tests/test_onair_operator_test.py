"""One fixed on-air +20 test, using temporary SQLite and offline Studio only."""
import asyncio
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from romance.appearance import PersistentAppearanceService
from romance.dyhub_intake import DyhubIntake
from romance.game import Game
from romance.server import create_app
from test_appearance_gift_probe import HoldClient
from test_real_appearance_program import ManualAppearanceAvatar, until
from test_server import SyntheticModel


class OnAirOperatorTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="onair-operator-offline-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.sqlite"
        self.avatar = ManualAppearanceAvatar()
        self.app = create_app(self.path, model=SyntheticModel(), avatar_client=self.avatar)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.wb = self.app["workbench"]
        self.program = self.wb.program
        self.wb.appearance.configure({"enabled": True, "quantity_verified": False, "room_id": "123",
            "positive_gift_ids": ["463"], "negative_gift_ids": ["164"]})
        self.intake = DyhubIntake(self.wb, client_factory=HoldClient)
        self.addAsyncCleanup(self.intake.stop)

    async def start_game(self):
        self.program.start("chat")
        await self.program.task
        self.program.set_mode(self.program.run_id, "game")
        await self.program._mode_task
        await self.wb.job
        self.body = {"run_id": self.program.run_id, "episode_id": self.program.game_episode_id}

    async def post(self, body=None, status=200):
        response = await self.client.post("/api/appearance/operator-test", json=self.body if body is None else body)
        result = await response.json()
        self.assertEqual(response.status, status, result)
        return result

    async def pending(self, target):
        await until(lambda: (self.program.appearance_status()["pending_confirmation"] or {}).get("target_key") == target)
        return self.program.appearance_status()["pending_confirmation"]

    async def gift(self, ident, count):
        now = time.time() * 1000
        return await self.intake.on_event({"type": "gift", "platform": "douyin", "roomId": "123", "id": ident,
            "ts": now, "receivedAt": now, "user": {"id": "offline-viewer"},
            "data": {"giftId": "463", "groupId": "group-" + ident, "diamondCount": 1,
                     "repeatCount": count, "sourceTimeKnown": True}})

    async def test_double_click_lost_response_and_real_gifts_share_score_without_paid_counts(self):
        await self.start_game()
        await self.intake.start("http://127.0.0.1:8757", "123", "broadcast")
        self.assertTrue(self.wb.appearance.snapshot(operator=True)["operator_test"]["eligible"])
        first, second = await asyncio.gather(self.post(), self.post())
        self.assertEqual(sorted(r["receipt"]["accepted"] for r in (first, second)), [False, True])
        receipt = first["receipt"]
        self.assertEqual(receipt["id"], second["receipt"]["id"])
        self.assertEqual((receipt["score_before"], receipt["score"], receipt["target"]), (50, 70, "refined"))
        pending = await self.pending("refined")
        public = (await (await self.client.get("/api/public")).json())["appearance"]
        self.assertEqual((public["score"], public["tier"], public["target"]), (70, "normal", "refined"))
        self.assertEqual(public["operator_test_contribution"]["source"], "operator_test")
        self.assertEqual(public["operator_test_contribution"]["delta"], 20)
        self.assertEqual(self.intake.counts.get("gift_accepted", 0), 0)
        self.assertEqual(self.wb.appearance.service.snapshot()["receipt_count"], 0)
        self.assertEqual(self.wb.game.storage.db.execute("SELECT COUNT(*) FROM appearance_receipts").fetchone()[0], 0)
        retry = await self.post()
        self.assertFalse(retry["receipt"]["accepted"])
        self.assertEqual(len(self.avatar.switches), 1)

        # Real packets arriving while Type16 waits keep accumulating above 70.
        self.assertEqual((await self.gift("two", 2))["score"], 72)
        self.assertEqual((await self.gift("eight", 8))["score"], 80)
        await self.program.confirm_appearance(pending["request_id"])
        next_request = await self.pending("stellar")
        await self.program.confirm_appearance(next_request["request_id"])
        await self.program.wait_for_appearance()
        self.assertEqual(self.program.confirmed_appearance_key, "stellar")
        self.assertEqual(len(self.avatar.switches), 2)
        self.assertEqual(self.intake.counts["gift_accepted"], 2)
        self.assertEqual(self.wb.appearance.service.snapshot()["receipt_count"], 2)
        self.assertEqual(self.wb.game.storage.db.execute("SELECT COUNT(*) FROM appearance_receipts").fetchone()[0], 2)
        self.assertFalse(self.wb.game.data["comments"])

        # The proof survives a fresh reader; restart cannot re-credit this episode.
        restored = Game(self.path, SyntheticModel())
        try:
            snapshot = PersistentAppearanceService(restored).snapshot()
            self.assertEqual(snapshot["operator_test"]["id"], receipt["id"])
            self.assertEqual(snapshot["score"], 80)
        finally:
            restored.storage.close()

    async def test_stale_run_episode_not_ready_paused_and_pending_are_rejected(self):
        await self.start_game()
        for body in ({**self.body, "run_id": "old-run"}, {**self.body, "episode_id": "old-episode"},
                     {**self.body, "diamonds": 20}, {}):
            await self.post(body, 409)
        for field, value in (("ready", False), ("sender_failed", True), ("broadcast_mode", False),
                             ("_appearance_active", False), ("_appearance_busy", True)):
            with self.subTest(field=field), patch.object(self.program, field, value):
                await self.post(status=409)
        with patch.object(self.program, "_appearance_context", {**self.program._appearance_context, "owner_id": "other"}):
            await self.post(status=409)
        self.program.request_appearance("refined")
        request = await self.pending("refined")
        rejected = await self.post(status=409)
        self.assertEqual(rejected["appearance"]["operator_test"]["reason"], "confirmation_pending")
        self.assertEqual(self.wb.appearance.service.snapshot()["score"], 50)
        self.assertIsNone(self.wb.appearance.service.snapshot()["operator_test"])
        await self.program.confirm_appearance(request["request_id"])
        await self.program.wait_for_appearance()

    async def test_commit_failure_does_not_schedule_or_consume_test(self):
        await self.start_game()
        with patch.object(self.wb.game.storage, "save_changes", side_effect=ValueError("offline failed commit")):
            await self.post(status=409)
        self.assertEqual(self.avatar.switches, {})
        self.assertEqual(self.wb.appearance.service.snapshot()["score"], 50)
        result = await self.post()
        self.assertTrue(result["receipt"]["accepted"])
        request = await self.pending("refined")
        await self.program.confirm_appearance(request["request_id"])
        await self.program.wait_for_appearance()


if __name__ == "__main__":
    unittest.main()
