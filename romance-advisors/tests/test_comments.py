"""Local comment routing uses synthetic models and temporary databases only."""
import asyncio
from contextlib import closing
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from romance.game import Game
from test_game import FakeModel


class CommentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "comments.sqlite"
        self.model = FakeModel()
        self.game = Game(self.path, self.model)
        await self.game.new_episode()
        self.sequence = 0

    async def asyncTearDown(self):
        self.game.close()
        self.temp.cleanup()

    def event(self, text, author="a", nickname="同名"):
        self.sequence += 1
        return {"event_id": f"event-{self.sequence}", "author_id": author, "nickname": nickname,
                "text": text, "expected_round_id": self.game.data["round_id"],
                "expected_phase": self.game.current_round["phase"] if self.game.current_round else None}

    async def options(self):
        self.game.accept_comment(self.event("解释紧张"))
        self.game.accept_comment(self.event("开个玩笑", "b"))
        await self.game.close_collection()
        return self.game.current_round["options"]

    async def test_comments_keep_each_candidate_with_its_original_source(self):
        calls = len(self.model.calls)
        first = self.game.accept_comment(self.event("解释紧张"))
        second = self.game.accept_comment(self.event("认真解释", nickname="新昵称"))
        third = self.game.accept_comment(self.event("开个玩笑", "b"))
        self.assertEqual(len(self.model.calls), calls)
        self.assertNotEqual(first["proposal_id"], second["proposal_id"])
        self.assertNotEqual(second["proposal_id"], third["proposal_id"])
        state = self.game.operator_state()
        self.assertEqual(len(state["comments"]), 3)
        self.assertEqual([p["text"] for p in state["proposals"]], ["解释紧张", "认真解释", "开个玩笑"])
        self.assertEqual([p["nickname"] for p in state["proposals"]], ["同名", "新昵称", "同名"])
        self.assertEqual(first["result"], "candidate")
        self.assertNotIn("comments", self.game.public_state())
        self.assertNotIn("votes", self.game.public_state())

    async def test_vote_commands_change_vote_and_invalid_option_keeps_old_vote(self):
        early = self.game.accept_comment(self.event("投1", "v"))
        self.assertEqual((early["kind"], early["result"]), ("vote", "ignored"))
        options = await self.options()
        self.game.accept_comment(self.event("投1", "v"))
        self.game.accept_comment(self.event("投1", "v"))
        changed = self.game.accept_comment(self.event("投2", "v", "新昵称"))
        invalid = self.game.accept_comment(self.event("投4", "v"))
        chat = self.game.accept_comment(self.event("我觉得投1", "v"))
        self.assertEqual(changed["result"], "voted")
        self.assertEqual((invalid["kind"], invalid["result"]), ("vote", "ignored"))
        self.assertEqual((chat["kind"], chat["result"]), ("chat", "ignored"))
        self.assertEqual(self.game.operator_state()["votes"][0]["option_id"], options[1]["id"])
        self.assertEqual([o["votes"] for o in self.game.public_state()["round"]["options"]], [0, 1])
        self.game.accept_comment(self.event("投1", "different-id", "新昵称"))
        self.assertEqual([o["votes"] for o in self.game.public_state()["round"]["options"]], [1, 1])

    async def test_raw_comment_whitespace_is_preserved_but_commands_are_trimmed(self):
        original = "  先解释紧张。\n再认真邀约。 \n"
        event = self.event(original)
        receipt = self.game.accept_comment(event)
        self.assertEqual(receipt["text"], original)
        self.assertEqual(self.game.operator_state()["comments"][0]["text"], original)
        self.assertEqual(self.game.operator_state()["proposals"][0]["text"], original.strip())
        self.assertEqual(self.game.accept_comment(event), receipt)
        with self.assertRaisesRegex(ValueError, "invalid_comment_conflict"):
            self.game.accept_comment(dict(event, text=original.strip()))
        await self.game.close_collection()
        vote = self.game.accept_comment(self.event(" \n投1\t ", "v"))
        self.assertEqual(vote["text"], " \n投1\t ")
        self.assertEqual(vote["result"], "voted")
        self.game.close()
        self.game = Game(self.path, self.model)
        self.assertEqual(self.game.operator_state()["comments"][0]["text"], original)
        self.assertEqual(self.game.accept_comment(event), receipt)

    async def test_idempotency_survives_window_round_and_restart(self):
        event = self.event("解释紧张")
        receipt = self.game.accept_comment(event)
        await self.game.close_collection()
        self.assertEqual(self.game.accept_comment(event, blocked_reason="已截止"), receipt)
        await self.game.close_voting()
        self.game.start_round()
        self.assertEqual(self.game.accept_comment(event), receipt)
        self.game.close()
        self.game = Game(self.path, self.model)
        self.assertEqual(self.game.accept_comment(event), receipt)
        self.assertEqual(len(self.game.operator_state()["comments"]), 1)
        self.assertEqual(self.game.operator_state()["proposals"], [])
        with self.assertRaisesRegex(ValueError, "invalid_comment_conflict"):
            self.game.accept_comment(dict(event, text="不同正文"))

    async def test_stale_context_never_enters_next_window_or_round(self):
        delayed = self.event("投1")
        await self.options()
        stale_phase = self.game.accept_comment(delayed, blocked_reason="已截止")
        self.assertEqual(stale_phase["result"], "stale")
        delayed_round = self.event("投1")
        await self.game.close_voting()
        self.game.start_round()
        self.assertEqual(self.game.accept_comment(delayed_round)["result"], "stale")
        self.assertEqual(self.game.operator_state()["votes"], [])
        self.assertEqual(self.game.operator_state()["proposals"], [])
        event = self.event("解释紧张")
        del event["expected_phase"]
        with self.assertRaisesRegex(ValueError, "invalid_comment_context"):
            self.game.accept_comment(event)

    async def test_blocked_or_busy_comments_are_durable_without_participating(self):
        blocked = self.game.accept_comment(self.event("截止后的建议"), blocked_reason="本轮征集已截止。")
        self.assertEqual((blocked["result"], blocked["reason"]), ("ignored", "本轮征集已截止。"))
        self.model.wait_task = "draft"
        operation = asyncio.create_task(self.game.close_collection())
        await self.model.started.wait()
        current_round = self.game.current_round
        busy = self.game.accept_comment(self.event("生成期间的评论"))
        self.assertEqual(busy["result"], "ignored")
        self.assertIs(current_round, self.game.current_round)
        self.assertEqual(self.game.operator_state()["proposals"], [])
        self.model.release.set()
        await operation
        self.assertEqual(self.game.current_round["phase"], "DONE")
        self.assertEqual(len(self.game.operator_state()["comments"]), 2)

    async def test_save_failure_rolls_back_receipt_and_effective_input_together(self):
        event = self.event("解释紧张")
        before = copy.deepcopy(self.game.data)
        self.game.storage.db.execute("CREATE TRIGGER fail_comment BEFORE INSERT ON comments BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.game.accept_comment(event)
        self.assertEqual(self.game.data, before)
        self.assertEqual(self.game.storage.load(), before)
        self.game.storage.db.execute("DROP TRIGGER fail_comment")
        self.assertEqual(self.game.accept_comment(event)["result"], "candidate")
        await self.game.close_collection()
        before = copy.deepcopy(self.game.data)
        self.game.storage.db.execute("CREATE TRIGGER fail_comment BEFORE INSERT ON comments BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.game.accept_comment(self.event("投1", "v"))
        self.assertEqual(self.game.data, before)
        self.assertEqual(self.game.storage.load(), before)

    async def test_failed_comment_write_does_not_detach_inflight_model_round(self):
        self.game.accept_comment(self.event("解释紧张"))
        self.model.wait_task = "select"
        operation = asyncio.create_task(self.game.close_collection())
        await self.model.started.wait()
        current_round = self.game.current_round
        before = copy.deepcopy(self.game.data)
        self.game.storage.db.execute("CREATE TRIGGER fail_comment BEFORE INSERT ON comments BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                self.game.accept_comment(self.event("生成时保存失败的评论"))
            self.assertEqual(self.game.data, before)
            self.assertIs(self.game.current_round, current_round)
        finally:
            self.game.storage.db.execute("DROP TRIGGER fail_comment")
            self.model.release.set()
            await operation
        self.assertEqual(self.game.current_round["phase"], "VOTING")
        self.assertEqual(len(self.game.current_round["options"]), 1)
        self.assertEqual(len(self.game.operator_state()["comments"]), 1)

    async def test_old_database_loads_and_comment_history_is_scoped_to_session(self):
        before = self.game.public_state()
        self.game.close()
        # Reproduce a previous-release database: there is no comments table yet.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE comments")
        self.game = Game(self.path, self.model)
        self.assertEqual(self.game.public_state(), before)
        receipt = self.game.accept_comment(self.event("旧库也能评论"))
        rounds = self.game.operator_state()["comment_rounds"]
        self.assertEqual(rounds[0]["id"], receipt["round_id"])
        await self.game.new_session()
        self.assertEqual(self.game.operator_state()["comments"], [])
        self.assertEqual(self.game.operator_state()["comment_rounds"], [])
        self.game.close()
        self.game = Game(":memory:", self.model)
        event = self.event("还没开始场次")
        self.assertIsNone(event["expected_round_id"])
        receipt = self.game.accept_comment(event)
        self.assertEqual(receipt["result"], "ignored")
        self.assertIsNone(receipt["session_id"])
        self.assertEqual(self.game.operator_state()["comments"], [receipt])


if __name__ == "__main__":
    unittest.main()
