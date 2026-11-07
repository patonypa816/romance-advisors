"""Temporary SQLite only; durable real-source replay without network/credentials."""
import asyncio
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from romance.appearance import (APPEARANCE_BANDS, PersistentAppearanceService,
                                appearance_snapshot, score_tier)
from romance.dyhub_intake import DyhubIntake
from romance.game import Game


def event(message="one", *, gift_id="plus", price=6, count=1, group="a", **changes):
    return {"type": "gift", "platform": "douyin", "roomId": "123", "id": message,
            "ts": 1000000, "receivedAt": 1000000, "user": {"id": "viewer"},
            "data": {"giftId": gift_id, "diamondCount": price, "repeatCount": count,
                     "groupId": group, "sourceTimeKnown": True}, **changes}


class DurableAppearanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.sqlite"
        self.game = Game(self.path, None)
        self.start_episode("first")
        self.service = PersistentAppearanceService(self.game, clock=lambda: 1000)
        self.service.configure({"enabled": True, "room_id": "123", "quantity_verified": True,
                                "gift_directions": {"plus": 1, "minus": -1}})
        self.service.begin_episode("first")

    def tearDown(self):
        self.game.storage.close()
        self.tmp.cleanup()

    def start_episode(self, ident):
        self.game.data["episodes"].append({"id": ident, "status": "ACTIVE"})
        self.game.data["episode_id"] = ident
        self.game._save()

    def test_all_integer_bands_and_public_snapshots(self):
        for low, high, key in APPEARANCE_BANDS:
            for score in range(low, high + 1):
                self.assertEqual(score_tier(score), key)
            portrait = appearance_snapshot(key, simulated=False)
            self.assertEqual(portrait["portrait_url"], f"/static/assets/appearance/{key}.jpg")
            self.assertNotIn("score", portrait)
            self.assertFalse(portrait["simulated"])

    def test_cumulative_end_overflow_and_restart(self):
        self.assertEqual(self.service.apply(event(count=10))["score"], 100)
        self.assertEqual(self.service.apply(event("minus", gift_id="minus", group="b"))["score"], 94)
        self.game.storage.close()
        self.game = Game(self.path, None)
        self.service = PersistentAppearanceService(self.game, clock=lambda: 1000)
        self.assertEqual(self.service.apply(event("end", count=10))["score"], 94)
        self.assertEqual(self.service.apply(event("twelve", count=12))["score"], 100)
        self.assertEqual(self.service.apply(event("twelve", count=12))["reason"], "duplicate_message")
        self.assertNotIn("groups", self.game.data["appearance"])
        self.assertNotIn("receipts", self.game.data["appearance"])

    def test_old_group_not_reassigned_after_new_episode(self):
        self.service.apply(event())
        self.service.close_episode()
        self.start_episode("second")
        self.service.begin_episode("second")
        self.assertEqual(self.service.apply(event("old-end", count=2))["reason"], "old_episode_group")
        self.assertEqual(self.service.apply(event("new", group="new"))["score"], 56)

    def test_atomic_receipt_score_and_group_rollback(self):
        self.game.storage.db.execute("CREATE TRIGGER reject_group BEFORE INSERT ON appearance_groups BEGIN SELECT RAISE(FAIL, 'fixture'); END")
        with self.assertRaises(sqlite3.DatabaseError):
            self.service.apply(event())
        self.assertEqual(self.service.snapshot()["score"], 50)
        self.assertEqual(self.game.storage.db.execute("SELECT COUNT(*) FROM appearance_receipts").fetchone()[0], 0)
        self.assertEqual(json.loads(self.game.storage.db.execute("SELECT body FROM meta").fetchone()[0])["appearance"]["episode"]["score"], 50)

    def test_wrong_stale_unreliable_simulated_rejected(self):
        for payload in (event(roomId="456"), event(episodeId="old"), event(ts=900000),
                        event(simulated=True), event(price=0), event(count=True),
                        event(group="0"), event(data={"giftId": "plus", "groupId": "a", "repeatCount": 1}),
                        event(data={"giftId": "plus", "groupId": "a", "diamondCount": 6, "repeatCount": 1, "sourceTimeKnown": False})):
            self.assertFalse(self.service.apply(payload)["accepted"])
        self.assertEqual(self.service.snapshot()["score"], 50)
        self.assertTrue(self.service.apply(event())["accepted"])

    def test_settings_copy_and_disable_after_close(self):
        config = self.service.settings()
        config["gift_directions"]["plus"] = -1
        self.assertEqual(self.service.settings()["gift_directions"]["plus"], 1)
        self.service.close_episode()
        self.service.configure({"enabled": False})
        self.assertFalse(self.service.settings()["enabled"])

    def test_unverified_quantity_blocks_real_start(self):
        self.service.close_episode()
        self.service.configure({"room_id": "123", "gift_directions": {"plus": 1}})
        with self.assertRaisesRegex(ValueError, "quantity_unverified"):
            self.service.begin_episode("first")

    def test_explicit_noncombo_single_only(self):
        payload = event(group=None)
        self.assertFalse(self.service.apply(payload)["accepted"])
        payload["data"]["combo"] = False
        self.assertTrue(self.service.apply(payload)["accepted"])
        self.assertFalse(self.service.apply(payload)["accepted"])

    def test_unverified_exact_pair_uses_one_diamond_price_and_durable_progress(self):
        self.service.close_episode()
        self.service.configure({"enabled": True, "room_id": "123", "quantity_verified": False,
                                "gift_directions": {"463": 1, "164": -1}})
        self.start_episode("second")
        self.service.begin_episode("second")
        self.assertFalse(self.service.settings()["quantity_verified"])
        for price in (None, True, 0, 2, 1.0, "1"):
            with self.subTest(price=price):
                self.assertEqual(self.service.apply(event("bad-price", gift_id="463", price=price))["reason"],
                                 "one_diamond_required")
        self.assertEqual(self.service.apply(event("one", gift_id="463", price=1))["score"], 51)
        self.assertEqual(self.service.apply(event("combo", gift_id="463", price=1, count=3))["score"], 53)
        self.assertEqual(self.service.apply(event("end", gift_id="463", price=1, count=3))["reason"], "duplicate_progress")
        self.assertEqual(self.service.apply(event("old-count", gift_id="463", price=1, count=2))["score"], 53)
        self.assertEqual(self.service.apply(event("down", gift_id="164", price=1, count=2))["score"], 51)
        self.game.storage.close()
        self.game = Game(self.path, None)
        self.service = PersistentAppearanceService(self.game, clock=lambda: 1000)
        self.assertEqual(self.service.apply(event("combo", gift_id="463", price=1, count=3))["reason"], "duplicate_message")
        self.assertEqual(self.service.apply(event("four", gift_id="463", price=1, count=4))["score"], 52)

    def test_unverified_one_diamond_test_keeps_unreliable_event_guards(self):
        self.service.close_episode()
        self.service.configure({"enabled": True, "room_id": "123", "quantity_verified": False,
                                "gift_directions": {"463": 1, "164": -1}})
        self.start_episode("second")
        self.service.begin_episode("second")
        good = event("safe", gift_id="463", price=1)
        invalid = [{"roomId": "456"}, {"id": ""}, {"user": {"id": ""}},
                   {"ts": None}, {"ts": 900000}, {"receivedAt": float("inf")},
                   {"simulated": True}, {"episodeId": "old"}]
        for change in invalid:
            with self.subTest(change=change):
                self.assertFalse(self.service.apply(dict(good, **change))["accepted"])
        for field, value in (("repeatCount", None), ("repeatCount", True), ("repeatCount", 0),
                             ("repeatCount", 1.5), ("groupId", None), ("groupId", "0"),
                             ("sourceTimeKnown", False), ("giftId", "999")):
            with self.subTest(field=field, value=value):
                payload = copy.deepcopy(good)
                payload["data"][field] = value
                self.assertFalse(self.service.apply(payload)["accepted"])
        self.assertEqual(self.service.snapshot()["receipt_count"], 0)
        self.assertTrue(self.service.apply(good)["accepted"])


class RealGiftIntakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_broadcast_gift_goes_to_workbench_separately(self):
        received = []
        wb = SimpleNamespace(game=SimpleNamespace(episode={"id": "first", "status": "ACTIVE"}),
                             program=SimpleNamespace(state="running", ready=True, switchable=True,
                                                     content_mode="game", game_episode_id="first"),
                             accept_appearance_gift=lambda e: (received.append(e) or {"accepted": True}))
        intake = DyhubIntake(wb, clock=lambda: 1000)
        intake.scope, intake.room_id, intake.started_at = "broadcast", "123", 1000
        intake.stop_event = asyncio.Event()
        self.assertTrue((await intake.on_event(event()))["accepted"])
        wb.program.content_mode = "chat"
        await intake.on_event(event("chat-time"))
        wb.program.content_mode = "game"
        await intake.on_event(event("fake", simulated=True))
        await intake.on_event(event("old", ts=900000))
        self.assertEqual(len(received), 1)
        self.assertEqual(intake.counts["gift_accepted"], 1)
