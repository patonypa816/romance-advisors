"""Pure synthetic gifts; no database, model, collector, or cloud session."""
import asyncio
from types import SimpleNamespace
import unittest

from romance.appearance import AppearanceLedger, appearance_snapshot
from romance.dyhub_intake import DyhubIntake


def gift(message="one", *, gift_id="plus", price=6, count=1, group="group-a", **changes):
    return {"simulated": True, "type": "gift", "platform": "douyin",
            "roomId": "offline-room", "id": message,
            "user": {"id": "synthetic-viewer"},
            "data": {"giftId": gift_id, "diamondCount": price,
                     "repeatCount": count, "groupId": group}, **changes}


class AppearanceGiftTests(unittest.TestCase):
    def ledger(self, **settings):
        return AppearanceLedger({"plus": 1, "minus": -1}, **settings)

    def test_cumulative_combo_and_distinct_snapshot(self):
        ledger = self.ledger()
        self.assertEqual((ledger.score, ledger.target), (50, "normal"))
        self.assertEqual(ledger.apply(gift())["score"], 56)
        event = gift("two", count=2)
        event["data"].update(comboCount=88, groupCount=99)
        self.assertEqual(ledger.apply(event)["score"], 62)
        frozen = appearance_snapshot(ledger.target)
        self.assertFalse(ledger.apply(event)["accepted"])
        self.assertEqual(ledger.apply(gift("end", count=2))["reason"], "duplicate_progress")
        self.assertEqual(ledger.apply(gift("back", gift_id="minus", price=12,
                                           group="negative"))["score"], 50)
        self.assertEqual(frozen["key"], "refined")
        self.assertNotEqual(frozen["avatar_key"], appearance_snapshot("normal")["avatar_key"])
        self.assertNotIn("score", frozen)

    def test_overflow_is_consumed_before_interleaved_opposite_gift(self):
        ledger = self.ledger(score=98)
        first = ledger.apply(gift(price=1, count=10))
        self.assertEqual((first["score"], first["delta"], first["overflow"]), (100, 2, 8))
        self.assertEqual(ledger.apply(gift("negative", gift_id="minus", group="b"))["score"], 94)
        self.assertEqual(ledger.apply(gift("end", price=1, count=10))["score"], 94)
        self.assertEqual(ledger.apply(gift("late", price=1, count=12))["score"], 96)
        self.assertEqual(ledger.sequence, 3)

    def test_zero_endpoint_and_top_zero_gain(self):
        ledger = self.ledger(score=2)
        self.assertEqual(ledger.apply(gift(gift_id="minus", price=10))["score"], 0)
        self.assertEqual(ledger.target, "fish")
        self.assertEqual(ledger.apply(gift("back", price=1, group="b"))["score"], 1)
        ledger = self.ledger(score=100)
        top = ledger.apply(gift(price=20))
        self.assertTrue(top["accepted"])
        self.assertEqual(ledger.target, "stellar")
        self.assertEqual((top["delta"], top["overflow"]), (0, 20))
        self.assertEqual(ledger.apply(gift("negative", gift_id="minus", price=1, group="b"))["score"], 99)

    def test_invalid_or_untrusted_fields_do_not_consume_progress(self):
        for change in ({"simulated": False}, {"roomId": "wrong"},
                       {"episodeId": "old"}, {"user": {"id": ""}},
                       {"data": {"giftId": "plus", "diamondCount": 6, "repeatCount": 1, "groupId": "0"}}):
            ledger = self.ledger()
            self.assertFalse(ledger.apply(gift(**change))["accepted"])
            self.assertEqual((ledger.score, ledger.sequence), (50, 0))
        for field, value in (("diamondCount", None), ("diamondCount", True),
                             ("diamondCount", 0), ("repeatCount", 1.5),
                             ("giftId", "unknown"), ("groupId", "")):
            ledger = self.ledger()
            event = gift()
            event["data"][field] = value
            self.assertFalse(ledger.apply(event)["accepted"])
            self.assertTrue(ledger.apply(gift())["accepted"])

    def test_group_price_conflict_and_out_of_order_count(self):
        ledger = self.ledger()
        ledger.apply(gift(count=2))
        self.assertEqual(ledger.apply(gift("conflict", price=7, count=3))["reason"], "group_price_conflict")
        self.assertEqual(ledger.apply(gift("old", count=1))["score"], 62)
        self.assertEqual(ledger.apply(gift("three", count=3))["score"], 68)

    def test_same_price_id_controls_opposite_direction(self):
        ledger = self.ledger()
        ledger.apply(gift())
        self.assertEqual(ledger.apply(gift("negative", gift_id="minus"))["score"], 50)


class SimulatedGiftRoutingTests(unittest.IsolatedAsyncioTestCase):
    def intake(self, callback=None):
        # A deliberately tiny harness: any comment ingestion is a test failure.
        game = SimpleNamespace(episode={"id": "episode", "status": "ACTIVE"})
        intake = DyhubIntake(SimpleNamespace(game=game), on_simulated_gift=callback)
        intake.episode_id = "episode"
        intake.room_id = "offline-room"
        intake.stop_event = asyncio.Event()
        return intake

    async def test_gifts_only_reach_explicit_simulated_callback(self):
        ledger = AppearanceLedger({"plus": 1})
        intake = self.intake(ledger.apply)
        self.assertEqual((await intake.on_event(gift()))["score"], 56)
        await intake.on_event(gift("real", simulated=False))
        await intake.on_event(gift("wrong", roomId="wrong"))
        self.assertEqual(ledger.score, 56)
        intake.stop_event.set()
        await intake.on_event(gift("stopped", count=2))
        self.assertEqual(ledger.score, 56)

    async def test_default_intake_ignores_even_marked_gifts(self):
        intake = self.intake()
        await intake.on_event(gift())
        self.assertEqual(intake.counts["ignored"], 1)

    async def test_ended_episode_does_not_route_simulated_gifts(self):
        received = []
        intake = self.intake(received.append)
        intake.workbench.game.episode["status"] = "ENDED"
        await intake.on_event(gift())
        self.assertEqual(received, [])


if __name__ == "__main__":
    unittest.main()
