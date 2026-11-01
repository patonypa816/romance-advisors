"""离线验证候选池，不能视为真实 dyhub 或模型筛选验收。"""
import copy
import json
import unittest

from comment_intake import CandidatePool


class FakeClock:
    def __init__(self):
        self.now = 1_800_000_000.0

    def __call__(self):
        return self.now


class CandidatePoolTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.pool = CandidatePool(clock=self.clock)

    def event(self, message="m1", user="private-viewer-id", room="r1", text="最近粮价如何？", **extra):
        return {"id": message, "roomId": room, "platform": "douyin", "type": "chat",
                "ts": self.clock.now * 1000, "receivedAt": self.clock.now * 1000,
                "user": {"id": user, "nickname": "观众昵称"}, "data": {"content": text}, **extra}

    def test_message_and_normalized_author_duplicates_are_separate(self):
        first = self.pool.ingest(self.event(text="Ａ  b"))
        self.assertEqual(first["status"], "candidate")
        self.assertEqual(self.pool.ingest(self.event(text="不同内容"))["reason"], "duplicate_message")
        self.assertEqual(self.pool.ingest(self.event(message="m2", text="a b"))["reason"], "duplicate_author_text")
        self.assertEqual(self.pool.ingest(self.event(message="m3", user="other", text="a b"))["status"], "candidate")
        self.assertEqual(self.pool.ingest(self.event(message="m4", text="另一个问题"))["status"], "candidate")

    def test_room_isolation_and_process_salt_and_no_raw_identity(self):
        event = self.event()
        before = copy.deepcopy(event)
        self.pool.ingest(event)
        self.pool.ingest(self.event(room="r2"))
        batch = self.pool.lease_batch()
        keys = [item["viewer_key"] for item in batch["candidates"]]
        self.assertNotEqual(keys[0], keys[1])
        other = CandidatePool(clock=self.clock)
        other.ingest(event)
        self.assertNotEqual(keys[0], other.lease_batch()["candidates"][0]["viewer_key"])
        self.assertEqual(event, before)
        for value in (batch, self.pool.snapshot(), self.pool.__dict__):
            self.assertNotIn("private-viewer-id", str(value))
        self.assertNotEqual(batch["candidates"][0]["id"], event["id"])

    def test_author_window_ends_but_recent_message_identity_stays_deduplicated(self):
        self.pool.ingest(self.event())
        self.clock.now += 45
        self.assertEqual(self.pool.ingest(self.event(message="m2"))["status"], "candidate")
        self.assertEqual(self.pool.ingest(self.event())["reason"], "duplicate_message")

    def test_text_bounds_and_no_semantic_filter(self):
        for value in ("", " ", "字" * 161, None, 1):
            self.assertEqual(self.pool.ingest(self.event(text=value))["reason"], "invalid_text")
        for index, value in enumerate(("字" * 160, "😂😂", "加我买货", "你说得不对")):
            self.assertEqual(self.pool.ingest(self.event(message=str(index), text=value))["status"], "candidate")
        event = self.event(message="nickname", text="你好")
        event["user"]["nickname"] = "长" * 1000
        self.pool.ingest(event)
        self.assertEqual(len(self.pool.lease_batch()["candidates"][-1]["nickname"]), 16)

    def test_only_valid_standard_chat_events(self):
        invalid = [None, {}, self.event(type="gift"), self.event(platform="other"),
                   self.event(user=None), self.event(id=""), self.event(roomId=True)]
        for value in invalid:
            self.assertEqual(self.pool.ingest(value)["status"], "error")

    def test_timestamp_requires_both_finite_epoch_milliseconds(self):
        for key in ("ts", "receivedAt"):
            for value in (None, True, "1800000000000", float("nan"), float("inf"), 0, 10 ** 400):
                with self.subTest(key=key, value=str(value)):
                    self.assertEqual(self.pool.ingest(self.event(**{key: value}))["reason"], "invalid_timestamp")
            self.assertEqual(self.pool.ingest(self.event(**{key: (self.clock.now - 45) * 1000}))["status"], "expired")
            self.assertEqual(self.pool.ingest(self.event(**{key: (self.clock.now + 5.1) * 1000}))["reason"], "future_timestamp")
        self.assertEqual(self.pool.ingest(self.event(ts=self.clock.now))["reason"], "old_timestamp")

    def test_ttl_uses_oldest_time_and_local_arrival_boundary(self):
        self.pool.ingest(self.event(ts=(self.clock.now - 40) * 1000))
        self.clock.now += 4
        self.assertEqual(self.pool.snapshot()["candidate_count"], 1)
        self.clock.now += 1
        self.assertEqual(self.pool.snapshot()["candidate_count"], 0)
        self.pool.ingest(self.event(message="future", ts=(self.clock.now + 4) * 1000,
                                    receivedAt=(self.clock.now + 4) * 1000))
        self.clock.now += 45
        self.assertEqual(self.pool.snapshot()["candidate_count"], 0)

    def test_clock_rollback_does_not_extend_lifetime(self):
        self.pool.ingest(self.event())
        self.clock.now += 40
        age = self.pool.snapshot()["candidates"][0]["age_seconds"]
        self.clock.now -= 30
        self.assertEqual(self.pool.snapshot()["candidates"][0]["age_seconds"], age)
        self.clock.now += 35
        self.assertEqual(self.pool.snapshot()["candidate_count"], 0)

    def test_capacity_history_and_dedup_are_bounded(self):
        pool = CandidatePool(clock=self.clock, max_candidates=3, max_history=5, dedup_limit=4)
        for index in range(100):
            pool.ingest(self.event(message=str(index), text=f"问题{index}"))
        snapshot = pool.snapshot()
        self.assertEqual(snapshot["candidate_count"], 3)
        self.assertEqual(snapshot["history_count"], 5)
        self.assertLessEqual(snapshot["dedup_count"], 4)
        self.assertEqual(snapshot["counts"]["evicted"], 97)
        for index in range(50):
            self.pool.ingest(self.event(message=str(index), text=f"问题{index}"))
        self.assertEqual(len(self.pool.snapshot()["history"]), 30)

    def test_only_one_batch_and_arrival_does_not_mutate_it(self):
        pool = CandidatePool(clock=self.clock, max_candidates=2)
        first = pool.ingest(self.event())
        batch = pool.lease_batch()
        self.assertIsNone(pool.lease_batch())
        pool.ingest(self.event(message="m2", text="第二个"))
        pool.ingest(self.event(message="m3", text="第三个"))
        result = pool.resolve(batch["batch_id"], first["id"], "合适")
        self.assertEqual(result["selected"]["id"], first["id"])
        self.assertEqual(pool.snapshot()["candidate_count"], 1)
        pool.lease_batch()
        pool.ingest(self.event(message="m4", text="第四个"))
        pool.lease_batch()

    def test_full_selecting_pool_rejects_new_arrival_without_evicting_batch(self):
        pool = CandidatePool(clock=self.clock, max_candidates=1)
        first = pool.ingest(self.event())
        batch = pool.lease_batch()
        self.assertEqual(pool.ingest(self.event(message="m2", text="第二个"))["status"], "evicted")
        self.assertEqual(pool.resolve(batch["batch_id"], first["id"], "合适")["status"], "selected")

    def test_resolve_null_or_one_and_delivery_is_not_reply(self):
        ids = [self.pool.ingest(self.event(message=str(i), text=f"问题{i}"))["id"] for i in range(3)]
        batch = self.pool.lease_batch()
        result = self.pool.resolve(batch["batch_id"], ids[1], "合适")
        self.assertEqual(result["selected"]["text"], "问题1")
        counts = self.pool.snapshot()["counts"]
        self.assertEqual(counts["selected"], 1)
        self.assertEqual(counts["not_selected"], 2)
        self.assertNotIn("enqueued", counts)
        self.assertEqual(self.pool.mark_delivery(ids[1], "not_enqueued", "暂停了")["status"], "not_enqueued")
        self.assertEqual(self.pool.mark_delivery(ids[1], "enqueued", "重复交付")["status"], "error")
        self.pool.ingest(self.event(message="new", text="新问题"))
        batch = self.pool.lease_batch()
        self.assertIsNone(self.pool.resolve(batch["batch_id"], None, "都不适合")["selected"])

    def test_invalid_unknown_and_replayed_results_cannot_select(self):
        self.pool.ingest(self.event())
        batch = self.pool.lease_batch()
        self.assertEqual(self.pool.resolve("unknown", batch["candidates"][0]["id"], "x")["reason"], "stale_batch")
        self.assertTrue(self.pool.snapshot()["active_batch"])
        result = self.pool.resolve(batch["batch_id"], "unknown", "x")
        self.assertEqual(result["reason"], "invalid_selected_id")
        self.assertIsNone(self.pool.resolve(batch["batch_id"], batch["candidates"][0]["id"], "x")["selected"])
        self.assertEqual(self.pool.snapshot()["candidate_count"], 0)

    def test_successful_result_replay_and_large_batch_request(self):
        for index in range(20):
            self.pool.ingest(self.event(message=str(index), text=f"问题{index}"))
        batch = self.pool.lease_batch(max_items=100)
        self.assertEqual(len(batch["candidates"]), 12)
        selected_id = batch["candidates"][0]["id"]
        self.assertEqual(self.pool.resolve(batch["batch_id"], selected_id, "合适")["status"], "selected")
        self.assertEqual(self.pool.resolve(batch["batch_id"], selected_id, "合适")["reason"], "stale_batch")
        self.assertEqual(self.pool.snapshot()["candidate_count"], 8)

    def test_batch_expiry_rejects_model_result_and_finalizes_rest(self):
        self.pool.ingest(self.event(ts=(self.clock.now - 40) * 1000))
        self.pool.ingest(self.event(message="new", text="新问题"))
        batch = self.pool.lease_batch()
        self.clock.now += 5
        result = self.pool.resolve(batch["batch_id"], batch["candidates"][1]["id"], "合适")
        self.assertIsNone(result["selected"])
        self.assertEqual(self.pool.snapshot()["counts"]["expired"], 2)

    def test_clear_revokes_batch_and_delivery_but_preserves_dedup(self):
        event = self.event()
        self.pool.ingest(event)
        batch = self.pool.lease_batch()
        self.pool.clear("stop")
        self.assertEqual(self.pool.snapshot()["version"], 1)
        self.assertIsNone(self.pool.resolve(batch["batch_id"], batch["candidates"][0]["id"], "x")["selected"])
        self.assertEqual(self.pool.ingest(event)["status"], "duplicate")
        self.pool.ingest(self.event(message="new", text="新问题"))
        batch = self.pool.lease_batch()
        selected = self.pool.resolve(batch["batch_id"], batch["candidates"][0]["id"], "x")["selected"]
        self.pool.clear("stop")
        self.assertEqual(self.pool.mark_delivery(selected["id"], "enqueued", "x")["reason"], "unknown_delivery")

    def test_failure_does_not_retry_or_select_and_reasons_are_bounded(self):
        self.pool.ingest(self.event())
        batch = self.pool.lease_batch()
        result = self.pool.failed(batch["batch_id"], "sensitive" * 1000)
        self.assertIsNone(result["selected"])
        self.assertEqual(result["reason"], "invalid_reason")
        self.assertIsNone(self.pool.lease_batch())
        self.assertNotIn("sensitive", json.dumps(self.pool.snapshot()))

    def test_snapshot_and_batch_are_copies(self):
        self.pool.ingest(self.event())
        snapshot = self.pool.snapshot()
        snapshot["candidates"][0]["text"] = "篡改"
        snapshot["history"][0]["text"] = "篡改"
        batch = self.pool.lease_batch()
        original = batch["candidates"][0]["text"]
        batch["candidates"][0]["text"] = "篡改"
        result = self.pool.resolve(batch["batch_id"], batch["candidates"][0]["id"], "合适")
        self.assertEqual(result["selected"]["text"], original)


if __name__ == "__main__":
    unittest.main()
