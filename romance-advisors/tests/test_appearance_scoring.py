"""Confirm that a visible portrait can affect the story without buying an author's rank."""
import unittest
from unittest.mock import patch

from romance.appearance import appearance_snapshot
from romance.game import Game
from test_game import FakeModel
from test_performance import FakePerformer


class ScoringModel(FakeModel):
    impression = 2

    async def generate(self, task, payload):
        result = await super().generate(task, payload)
        if task == "reply":
            result["impression_delta"] = self.impression
        return result


class PublishedPortrait(FakePerformer):
    key = "refined"

    def message_appearance(self):
        return appearance_snapshot(self.key, simulated=False)

    def prepare(self, round_id, text):
        return dict(super().prepare(round_id, text), message_motion_plan={})

    async def perform(self, record, text, *, on_message_action):
        on_message_action()
        return dict(self.receipt, message_motion_receipt={
            "job_id": record["job_id"] + ":typing", "generation": record["generation"],
            "status": "sent", "send_complete": True, "short_action_sent": True})


class AppearanceScoringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch("romance.game.WOMAN_TYPING_SECONDS", 0))
        self.model, self.performer = ScoringModel(), PublishedPortrait()
        self.game = Game(":memory:", self.model, performer=self.performer)
        self.addCleanup(self.game.close)
        await self.game.new_episode()

    async def round(self):
        self.game.submit_proposal("author", "军师", "约周六见面")
        await self.game.close_collection()
        self.game.vote("fan", "观众", "1")
        return await self.game.close_voting()

    async def test_changed_appearance_counts_once_and_does_not_credit_advisor(self):
        self.model.delta = 0
        first = await self.round()
        self.assertEqual(first["episode"]["score"], 52)
        self.assertEqual(first["leaderboards"]["best"][0]["positive"], 0)
        self.game.start_round()
        again = await self.round()
        self.assertEqual(again["episode"]["score"], 52)
        self.assertEqual(self.game.current_round["impression_delta"], 0)
        self.game.start_round()
        self.performer.key, self.model.delta, self.model.impression = "fish", 1, -2
        fish = await self.round()
        self.assertEqual(fish["episode"]["score"], 51)
        self.assertEqual(fish["leaderboards"]["best"][0]["positive"], 1)
        self.assertEqual(fish["leaderboards"]["worst"][0]["negative"], 0)
        self.assertEqual([m["portrait"]["key"] for m in fish["messages"] if m["role"]=="man"],
                         ["refined", "refined", "fish"])

    async def test_appearance_cannot_decide_finale(self):
        self.model.delta = 48
        first = await self.round()
        self.assertEqual((first["episode"]["score"], first["episode"]["status"]), (99, "ACTIVE"))
        self.game.start_round()
        self.performer.key, self.model.delta, self.model.impression = "fish", 1, -2
        final = await self.round()
        self.assertEqual((final["episode"]["score"], final["episode"]["status"]), (100, "SUCCEEDED"))
        self.assertEqual(final["finale"]["awards"]["best"]["score_delta"], 49)
