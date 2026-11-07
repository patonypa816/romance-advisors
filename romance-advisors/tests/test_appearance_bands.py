"""Configurable local test boundaries; no network or production data."""
import copy
import unittest

from romance.appearance import (APPEARANCE_BANDS, AppearanceLedger, DEFAULT_BAND_STARTS,
                                PersistentAppearanceService, appearance_bands,
                                score_tier, validate_band_starts)
from romance.game import Game

CUSTOM = {"tired": 10, "normal": 30, "refined": 55, "stellar": 90}


class AppearanceBandTests(unittest.TestCase):
    def test_default_and_custom_cover_every_integer_once(self):
        self.assertEqual(appearance_bands(), APPEARANCE_BANDS)
        expected = ((0, 0, "fish"), (1, 9, "rough"), (10, 29, "tired"),
                    (30, 54, "normal"), (55, 89, "refined"), (90, 100, "stellar"))
        self.assertEqual(appearance_bands(CUSTOM), expected)
        scores = []
        for low, high, key in expected:
            for score in range(low, high + 1):
                scores.append(score)
                self.assertEqual(score_tier(score, CUSTOM), key)
        self.assertEqual(scores, list(range(101)))
        self.assertEqual(score_tier(50, CUSTOM), "normal")
        self.assertEqual(score_tier(100, CUSTOM), "stellar")

    def test_partial_overrides_and_return_values_are_copied(self):
        self.assertEqual(validate_band_starts({"refined": 55}),
                         {**DEFAULT_BAND_STARTS, "refined": 55})
        normalized = validate_band_starts(CUSTOM)
        normalized["tired"] = 1
        self.assertEqual(CUSTOM["tired"], 10)
        defaults = validate_band_starts()
        defaults["tired"] = 1
        self.assertEqual(validate_band_starts()["tired"], 20)

    def test_bad_thresholds_rejected(self):
        invalid = ([], True, {"fish": 0}, {"rough": 1}, {"other": 10},
                   {"tired": True}, {"normal": 40.0}, {"refined": "60"},
                   {"tired": 1}, {"tired": 40}, {"normal": 51},
                   {"refined": 50}, {"refined": 80}, {"stellar": 60},
                   {"stellar": 101}, {"tired": -1})
        for config in invalid:
            with self.subTest(config=config):
                with self.assertRaisesRegex(ValueError, "invalid_appearance_band_starts"):
                    validate_band_starts(config)
                with self.assertRaises(ValueError):
                    AppearanceLedger({"plus": 1}, band_starts=config)

    def test_smallest_nonempty_and_stellar_only_at_100(self):
        values = {"tired": 2, "normal": 3, "refined": 51, "stellar": 100}
        self.assertEqual(score_tier(1, values), "rough")
        self.assertEqual(score_tier(2, values), "tired")
        self.assertEqual(score_tier(50, values), "normal")
        self.assertEqual(score_tier(99, values), "refined")
        self.assertEqual(score_tier(100, values), "stellar")

    def test_simulation_uses_custom_threshold(self):
        ledger = AppearanceLedger({"plus": 1}, band_starts=CUSTOM)
        result = ledger.apply({"type": "gift", "platform": "douyin", "simulated": True,
            "roomId": ledger.room_id, "id": "new", "user": {"id": "viewer"},
            "data": {"giftId": "plus", "groupId": "g", "diamondCount": 5, "repeatCount": 1}})
        self.assertEqual((result["score"], result["target"]), (55, "refined"))


class DurableAppearanceBandTests(unittest.TestCase):
    def setUp(self):
        self.game = Game(":memory:", None)
        self.game.data["episodes"].append({"id": "episode", "status": "ACTIVE"})
        self.game.data["episode_id"] = "episode"
        self.game._save()
        self.service = PersistentAppearanceService(self.game, clock=lambda: 1000)
        self.config = {"enabled": True, "room_id": "123", "quantity_verified": True,
                       "gift_directions": {"plus": 1}, "band_starts": copy.deepcopy(CUSTOM)}
        self.addCleanup(self.game.close)

    def test_configuration_is_frozen_and_used_by_real_gift(self):
        self.service.configure(self.config)
        self.config["band_starts"]["refined"] = 70
        self.assertEqual(self.service.settings()["band_starts"], CUSTOM)
        self.service.begin_episode("episode")
        frozen = self.game.data["appearance"]["episode"]["config"]
        self.assertEqual(frozen["band_starts"], CUSTOM)
        self.assertEqual(self.service.snapshot()["config"]["band_starts"], CUSTOM)
        receipt = self.service.apply({"type": "gift", "platform": "douyin", "roomId": "123", "id": "new",
            "ts": 1000000, "receivedAt": 1000000, "user": {"id": "viewer"},
            "data": {"giftId": "plus", "diamondCount": 5, "repeatCount": 1,
                     "groupId": "g", "sourceTimeKnown": True}})
        self.assertEqual((receipt["score"], receipt["target"]), (55, "refined"))
        self.assertEqual(self.game.storage.load()["appearance"]["episode"]["config"]["band_starts"], CUSTOM)

    def test_bad_configuration_does_not_replace_saved_settings(self):
        self.service.configure(self.config)
        before = self.service.settings()
        with self.assertRaises(ValueError):
            self.service.configure(dict(self.config, band_starts={"normal": 90}))
        self.assertEqual(self.service.settings(), before)

    def test_old_saved_config_and_frozen_episode_use_defaults(self):
        legacy = dict(self.config)
        del legacy["band_starts"]
        self.service.configure(legacy)
        del self.game.data["appearance"]["config"]["band_starts"]
        self.game._save()
        self.assertEqual(self.service.settings()["band_starts"], DEFAULT_BAND_STARTS)
        self.service.begin_episode("episode")
        self.assertEqual(self.game.data["appearance"]["episode"]["config"]["band_starts"], DEFAULT_BAND_STARTS)
        del self.game.data["appearance"]["episode"]["config"]["band_starts"]
        self.game._save()
        receipt = self.service.apply({"type": "gift", "platform": "douyin", "roomId": "123", "id": "new",
            "ts": 1000000, "receivedAt": 1000000, "user": {"id": "viewer"},
            "data": {"giftId": "plus", "diamondCount": 5, "repeatCount": 1,
                     "groupId": "g", "sourceTimeKnown": True}})
        self.assertEqual((receipt["score"], receipt["target"]), (55, "normal"))


if __name__ == "__main__":
    unittest.main()
