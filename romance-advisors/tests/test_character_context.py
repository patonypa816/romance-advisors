"""The man remembers his spoken asides without leaking them into story facts."""
import asyncio
import copy
import json
import unittest

from romance.game import Game
from test_game import FakeModel
from test_performance import FakePerformer


class ContextPerformer(FakePerformer):
    def __init__(self):
        super().__init__()
        self.context = {
            "recent_performances": [{"text": "我有点紧张，先听听你们。", "intent": "求助",
                                     "emotion": "紧张但愿意试试",
                                     "recovery": {"gaze": "镜头", "expression": "局促的笑意"}}],
            "current_performance": {"text": "这回我得认真想想。", "emotion": "犹豫"},
            # Even an expanded scheduler API must not feed an unsent next into
            # story generation by accidentally copying its whole return value.
            "next": {"text": "UNSENT_DRAFT"},
        }

    def expression_context(self):
        return self.context


class CharacterContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.model = FakeModel()
        self.performer = ContextPerformer()
        self.game = Game(":memory:", self.model, performer=self.performer)
        await self.game.new_episode()
        self.game.submit_proposal("advisor", "军师", "解释紧张")
        state = await self.game.close_collection()
        self.option = state["round"]["options"][0]["id"]
        self.game.vote("voter", "观众", self.option)

    async def asyncTearDown(self):
        self.game.close()

    async def test_actual_expression_context_reaches_only_man_draft(self):
        result = await self.game.close_voting()
        self.assertEqual(result["round"]["phase"], "DONE")
        for task, payload in self.model.calls:
            if task == "draft":
                self.assertEqual(payload["recent_performances"], self.performer.context["recent_performances"])
                self.assertEqual(payload["current_performance"], self.performer.context["current_performance"])
                self.assertEqual(payload["selected_option_id"], self.option)
                self.assertNotIn("WOMAN_PRIVATE", json.dumps(payload))
            else:
                self.assertNotIn("recent_performances", payload)
                self.assertNotIn("current_performance", payload)
            self.assertNotIn("UNSENT_DRAFT", json.dumps(payload))
        # Asides remain performance context; they never become phone messages.
        messages = [m["text"] for m in result["messages"]]
        self.assertNotIn(self.performer.context["recent_performances"][0]["text"], messages)
        self.assertEqual([m["role"] for m in result["messages"]], ["woman", "man", "woman"])

    async def test_text_only_run_does_not_reuse_old_avatar_context(self):
        self.performer.enabled = False
        result = await self.game.close_voting()
        self.assertEqual(result["round"]["phase"], "DONE")
        draft = next(p for task, p in self.model.calls if task == "draft")
        self.assertNotIn("recent_performances", draft)
        self.assertNotIn("current_performance", draft)
        self.assertEqual(self.performer.calls, [])

    async def test_context_is_snapshot_and_retry_keeps_locked_choice(self):
        self.model.wait_task = "draft"
        self.model.empty_draft = True
        original = copy.deepcopy(self.performer.context["recent_performances"])
        task = asyncio.create_task(self.game.close_voting())
        try:
            await self.model.started.wait()
            self.performer.context["recent_performances"][0]["emotion"] = "松了一口气"
        finally:
            self.model.release.set()
        failed = await task
        self.assertEqual(failed["round"]["phase"], "DRAFTING")
        first = [p for kind, p in self.model.calls if kind == "draft"][-1]
        self.assertEqual(first["recent_performances"], original)
        self.model.empty_draft = False
        self.model.wait_task = None
        result = await self.game.retry()
        retry = [p for kind, p in self.model.calls if kind == "draft"][-1]
        self.assertEqual(retry["selected_option_id"], first["selected_option_id"])
        self.assertEqual(retry["recent_performances"][0]["emotion"], "松了一口气")
        self.assertEqual(result["round"]["phase"], "DONE")


if __name__ == "__main__":
    unittest.main()
