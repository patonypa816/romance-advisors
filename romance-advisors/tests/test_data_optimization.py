"""Transaction and recovery checks use only in-memory/temporary SQLite files."""
import copy
import asyncio
import json
import sqlite3
import unittest

from romance.game import Game
from romance.model import ModelInputTooLarge
from romance.storage import Storage
import test_comments as comment_fixture


class StorageDeltaTests(unittest.TestCase):
    def setUp(self):
        self.storage = Storage(":memory:")
        self.state = self.storage.load()
        self.storage.save(self.state)

    def tearDown(self):
        self.storage.close()

    def test_identical_save_issues_no_sql_and_one_changed_record_only_writes_once(self):
        self.state["votes"] = [{"id": str(i), "option_id": "one"} for i in range(100)]
        self.storage.save(self.state)
        statements = []
        self.storage.db.set_trace_callback(statements.append)
        self.storage.save(copy.deepcopy(self.state))
        self.assertEqual(statements, [])
        self.state["votes"][42]["option_id"] = "two"
        self.storage.save(self.state)
        writes = [s for s in statements if s.startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(len(writes), 1)
        self.assertIn("votes", writes[0])

    def test_unique_messages_and_immutable_records_survive_failed_batch(self):
        def message(key, round_id, parent=None):
            return {"id": key, "round_id": round_id, "role": "man", "parent_id": parent, "text": "合成"}
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.save_changes({"messages": [message("m1", "r"), message("m2", "r")]})
        self.assertEqual(self.storage.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)
        # Retry without a load: a rolled-back batch must not poison the cache.
        self.storage.save_changes({"messages": [message("m1", "r")]})
        self.assertEqual(len(self.storage.load()["messages"]), 1)
        with self.assertRaisesRegex(ValueError, "committed_message_is_immutable"):
            self.storage.save_changes({"messages": [dict(message("m1", "r"), text="变化")]})
        self.storage.save_changes({"messages": [message("reply1", "r2", "m1")]})
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.save_changes({"messages": [message("reply2", "r3", "m1")]})
        self.assertEqual(len(self.storage.load()["messages"]), 2)


class DataOptimizationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = comment_fixture.CommentTests.asyncSetUp
    asyncTearDown = comment_fixture.CommentTests.asyncTearDown
    event = comment_fixture.CommentTests.event
    options = comment_fixture.CommentTests.options

    async def test_failed_changed_vote_rolls_back_inserted_receipt_and_cache(self):
        options = await self.options()
        self.game.accept_comment(self.event("投1", "v"))
        before = copy.deepcopy(self.game.data)
        event = self.event("投2", "v")
        self.game.storage.db.execute("CREATE TRIGGER fail_vote BEFORE UPDATE ON votes BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.game.accept_comment(event)
        self.assertEqual(self.game.data, before)
        self.assertEqual(self.game.storage.load(), before)
        self.game.storage.db.execute("DROP TRIGGER fail_vote")
        receipt = self.game.accept_comment(event)
        self.assertEqual(receipt["option_id"], options[1]["id"])
        self.assertEqual(self.game.accept_comment(event), receipt)
        self.game.close()
        self.game = Game(self.path, self.model)
        self.assertEqual(self.game.accept_comment(event), receipt)
        self.assertEqual(self.game.operator_state()["votes"][0]["option_id"], options[1]["id"])

    async def test_incremental_page_recovery_and_input_versions(self):
        for i in range(5):
            self.game.accept_comment(self.event(f"合成{i}", str(i)))
        page = self.game.operator_state(incremental=True, comment_limit=2)
        self.assertTrue(page["reset"])
        self.assertTrue(page["has_more"])
        self.assertEqual(len(page["comments"]), 2)
        ids = [c["id"] for c in page["comments"]]
        while page["has_more"]:
            page = self.game.operator_state(incremental=True, session_id=page["session_id"],
                                            comment_cursor=page["comment_cursor"], comment_limit=2,
                                            proposal_version=page["proposal_version"], vote_version=page["vote_version"])
            self.assertNotIn("proposals", page)
            self.assertNotIn("votes", page)
            ids.extend(c["id"] for c in page["comments"])
        self.assertEqual(ids, [c["id"] for c in self.game.operator_state()["comments"]])
        version = page["proposal_version"]
        self.game.accept_comment(self.event("最后一条", "0"))
        updated = self.game.operator_state(incremental=True, session_id=page["session_id"], comment_cursor=5,
                                           proposal_version=version, vote_version=page["vote_version"])
        self.assertNotEqual(updated["proposal_version"], version)
        self.assertFalse(updated["proposal_reset"])
        self.assertEqual(len(updated["proposals"]), 1)
        self.assertEqual(len(updated["comments"]), 1)
        self.assertEqual(updated["proposals"][0]["text"], "最后一条")
        self.game.close()
        self.game = Game(self.path, self.model)
        recovered = self.game.operator_state(incremental=True, session_id=page["session_id"], comment_cursor=0,
                                             proposal_version=updated["proposal_version"], vote_version=page["vote_version"])
        self.assertIn("proposals", recovered)
        self.assertTrue(recovered["proposal_reset"])
        self.assertEqual(len(recovered["comments"]), 6)
        await self.game.new_session()
        reset = self.game.operator_state(incremental=True, session_id=page["session_id"], comment_cursor=6)
        self.assertTrue(reset["reset"])
        self.assertEqual(reset["comments"], [])

    async def test_changed_vote_and_new_author_return_only_changed_ids(self):
        options = await self.options()
        for i in range(12):
            self.game.accept_comment(self.event("投1", f"v{i}"))
        initial = self.game.operator_state(incremental=True)
        self.assertTrue(initial["vote_reset"])
        self.game.accept_comment(self.event("投2", "v3"))
        self.game.accept_comment(self.event("投2", "new"))
        changed = self.game.operator_state(incremental=True, session_id=initial["session_id"],
                                          comment_cursor=initial["comment_cursor"], proposal_version=initial["proposal_version"],
                                          vote_version=initial["vote_version"])
        self.assertFalse(changed["vote_reset"])
        self.assertNotIn("proposals", changed)
        self.assertEqual([v["author_id"] for v in changed["votes"]], ["v3", "new"])
        merged = {v["id"]: v for v in initial["votes"]}
        merged.update({v["id"]: v for v in changed["votes"]})
        self.assertEqual(list(merged.values()), self.game.operator_state()["votes"])
        full = self.game.operator_state(incremental=True, vote_version="unknown")
        self.assertTrue(full["vote_reset"])
        self.assertEqual(len(full["votes"]), 13)
        self.assertEqual(self.game._counts()[options[1]["id"]], 2)

    async def test_new_identical_comment_keeps_new_source_and_retry_writes_nothing(self):
        self.game.accept_comment(self.event("解释紧张"))
        sql = []
        self.game.storage.db.set_trace_callback(sql.append)
        event = self.event("解释紧张")
        receipt = self.game.accept_comment(event)
        self.game.storage.db.set_trace_callback(None)
        writes = [s for s in sql if s.startswith(("INSERT", "UPDATE", "DELETE"))]
        self.assertEqual(len(writes), 2)
        self.assertEqual(sum(s.startswith("INSERT INTO comments") for s in writes), 1)
        self.assertEqual(sum(s.startswith("INSERT INTO proposals") for s in writes), 1)
        sql.clear()
        self.game.storage.db.set_trace_callback(sql.append)
        self.assertEqual(self.game.accept_comment(event), receipt)
        self.game.storage.db.set_trace_callback(None)
        self.assertEqual(sql, [])

    async def test_live_counts_cache_invalidates_only_after_vote_commit(self):
        options = await self.options()
        self.game.accept_comment(self.event("投1", "v"))
        page = self.game.operator_state(incremental=True)
        class ObservedVotes(list):
            scans = 0
            def __iter__(self):
                self.scans += 1
                return super().__iter__()
        votes = ObservedVotes(self.game._round_inputs["votes"][self.game.data["round_id"]])
        self.game._round_inputs["votes"][self.game.data["round_id"]] = votes
        self.game._counts_cache = None
        self.assertEqual(self.game._counts()[options[0]["id"]], 1)
        self.game.public_state()
        self.game.operator_state(incremental=True, session_id=page["session_id"], comment_cursor=page["comment_cursor"],
                                 proposal_version=page["proposal_version"], vote_version=page["vote_version"])
        self.assertEqual(votes.scans, 1)
        self.game.accept_comment(self.event("投2", "v"))
        self.assertEqual(self.game._counts()[options[1]["id"]], 1)
        self.assertEqual(votes.scans, 2)

    async def test_input_too_large_persists_nonretryable_error_and_metrics(self):
        self.game.submit_proposal("a", "军师", "合成建议")
        generate = self.model.generate
        async def oversized(task, payload):
            if task == "select":
                raise ModelInputTooLarge(request_bytes=200001, task=task, proposal_count=1)
            return await generate(task, payload)
        self.model.generate = oversized
        state = await self.game.close_collection()
        self.assertEqual(state["error"]["code"], "model_input_too_large")
        self.assertFalse(state["error"]["retryable"])
        self.assertEqual(state["error"]["metadata"]["request_bytes"], 200001)
        metrics = self.game.metrics.snapshot()
        self.assertTrue(any(e["stage"] == "model_select_end" and e["code"] == "model_input_too_large" for e in metrics))
        calls = len(self.model.calls)
        self.game.model.generate = lambda *args: self.fail("must not retry deterministic oversized input")
        self.assertEqual((await self.game.retry())["error"], state["error"])
        self.assertEqual(len(self.model.calls), calls)
        self.game.close()
        self.game = Game(self.path, self.model)
        self.assertEqual((await self.game.retry())["error"], state["error"])
        self.assertNotIn("合成建议", json.dumps(metrics, ensure_ascii=False))

    async def test_cancelled_model_has_matching_end_metric(self):
        self.game.accept_comment(self.event("解释紧张"))
        self.model.wait_task = "select"
        operation = asyncio.create_task(self.game.close_collection())
        await self.model.started.wait()
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        events = [e for e in self.game.metrics.snapshot() if e["stage"].startswith("model_select_")]
        self.assertEqual([e["stage"] for e in events], ["model_select_start", "model_select_end"])
        self.assertEqual(events[1]["outcome"], "cancelled")
        for key in ("run_id", "session_id", "episode_id", "round_id"):
            self.assertEqual(events[0][key], events[1][key])

    async def test_late_model_metric_retains_original_scope_after_new_session(self):
        self.game.accept_comment(self.event("解释紧张"))
        self.model.wait_task = "select"
        operation = asyncio.create_task(self.game.close_collection())
        await self.model.started.wait()
        await self.game.new_session()
        self.model.release.set()
        await operation
        events = [e for e in self.game.metrics.snapshot() if e["stage"].startswith("model_select_")]
        self.assertEqual(len(events), 2)
        self.assertNotEqual(events[0]["session_id"], self.game.data["session_id"])
        self.assertEqual(events[0]["session_id"], events[1]["session_id"])
        self.assertEqual(events[0]["round_id"], events[1]["round_id"])


if __name__ == "__main__":
    unittest.main()
