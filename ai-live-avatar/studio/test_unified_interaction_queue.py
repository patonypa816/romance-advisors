"""欢迎冻结、整轮结算与礼物合并的离线行为合同。"""
import copy
import json
import unittest
from unittest.mock import patch

from live_interactions import InteractionQueue, PREPARATION_TTL_SECONDS
import test_live_interactions as fixtures


class UnifiedQueueTests(unittest.TestCase):
    def setUp(self):
        self.clock = fixtures.Clock()
        self.queue = InteractionQueue(clock=self.clock)

    def event(self, kind="member", index=0, uid=None, nickname=None, **data):
        event = fixtures.InteractionRulesTests.event(self, kind, message=f"{kind}-{index}",
                                            uid=uid if uid is not None else f"private-{index}", **data)
        event["user"]["nickname"] = nickname if nickname is not None else f"朋友{index}"
        return event

    def welcome(self):
        summary = self.queue.snapshot_candidates()["welcome"]
        return self.queue.reserve_selection(welcome_id=summary["id"])[0]

    def test_big_batch_short_text_full_members_and_no_model_member_list(self):
        for i in range(120):
            self.assertEqual(self.queue.ingest(self.event(index=i))["status"], "queued")
        preview = self.queue.snapshot_candidates()["welcome"]
        self.assertEqual(preview["count"], 120)
        self.assertEqual(len(preview["names"]), 3)
        self.assertTrue(preview["collective"])
        self.assertIn("各位朋友", preview["text"])
        self.assertLessEqual(len(preview["text"]), 120)
        self.assertNotIn("private-", json.dumps(preview))
        self.assertNotIn("members", preview)
        job = self.queue.reserve_selection(welcome_id=preview["id"])[0]
        self.assertEqual(len(job["meta"]["members"]), 120)
        self.assertEqual(self.queue.public()["welcome_seen_count"], 0)
        self.queue.finish(job, success=True, sent_any=True)
        self.assertEqual(self.queue.public()["welcome_seen_count"], 120)

    def test_snapshot_stable_and_new_members_never_expand_selected_batch(self):
        self.queue.ingest(self.event(index=0))
        old = self.queue.snapshot_candidates()["welcome"]
        self.assertEqual(old, self.queue.snapshot_candidates()["welcome"])
        self.queue.ingest(self.event(index=1))
        new = self.queue.snapshot_candidates()["welcome"]
        self.assertNotEqual(old["id"], new["id"])
        job = self.queue.reserve_selection(welcome_id=old["id"])[0]
        self.assertEqual(job["meta"]["batch_size"], 1)
        self.assertEqual(self.queue.snapshot_candidates()["welcome"]["count"], 1)
        self.assertEqual(self.queue.reserve_selection(welcome_id=new["id"]), [])
        self.assertEqual(self.queue.reserve_selection(welcome_id=old["id"]), [])

    def test_reserved_member_reentry_is_occupied_and_success_is_idempotent(self):
        self.queue.ingest(self.event(index=0))
        job = self.welcome()
        self.assertEqual(self.queue.ingest(self.event(index=1, uid="private-0"))["reason"], "welcome_reserved")
        first = self.queue.finish(job, success=True, sent_any=True)
        self.assertEqual(first, self.queue.finish(job["id"], success=False, sent_any=False))
        self.assertFalse(self.queue.reservation_active(job))
        self.assertEqual(self.queue.ingest(self.event(index=2, uid="private-0"))["reason"], "welcome_already_seen")

    def test_freeze_crosses_candidate_ttl_and_completes_once(self):
        self.queue.ingest(self.event())
        self.clock.now += 59
        job = self.welcome()
        self.assertEqual(job["expires_at"], self.clock.now + PREPARATION_TTL_SECONDS)
        self.clock.now += 70
        self.assertTrue(self.queue.reservation_active(job))
        self.assertEqual(self.queue.finish(job, success=True, sent_any=True)["status"], "completed")

    def test_no_send_failure_releases_only_fresh_and_retry_is_bounded(self):
        self.queue.ingest(self.event())
        job = self.welcome()
        result = self.queue.finish(job, success=False, sent_any=False, reason="tts_failed")
        self.assertEqual(result["released"], 1)
        second = self.welcome()
        result = self.queue.finish(second, success=False, sent_any=False, reason="tts_failed")
        self.assertEqual(result["released"], 0)
        self.assertFalse(self.queue.has_pending())
        self.assertEqual(self.queue.public()["welcome_seen_count"], 0)

    def test_selection_failures_and_explicit_skips_never_return_to_pool(self):
        for index, reason in enumerate(("selection_skipped", "selection_failed", "snapshot_expired")):
            with self.subTest(reason=reason):
                self.queue.ingest(self.event(index=index))
                job = self.welcome()
                result = self.queue.finish(job, success=False, sent_any=False, reason=reason)
                self.assertEqual(result["released"], 0)
                self.assertFalse(self.queue.has_pending())
        self.assertEqual(self.queue.public()["welcome_seen_count"], 0)

    def test_gift_preview_name_is_bounded_to_brain_input_contract(self):
        self.queue.ingest(self.event("gift", nickname="长" * 60))
        preview = self.queue.snapshot_candidates()["gift"]
        self.assertEqual(len(preview), 1)
        self.assertEqual(preview[0]["nickname"], "长" * 32)

    def test_failed_expired_original_candidate_does_not_return_but_real_reentry_can(self):
        self.queue.ingest(self.event())
        job = self.welcome()
        self.clock.now += 60
        self.assertEqual(self.queue.finish(job, success=False, sent_any=False)["released"], 0)
        self.assertFalse(self.queue.has_pending())
        self.assertEqual(self.queue.ingest(self.event(index=1, uid="private-0"))["status"], "queued")

    def test_partial_releases_occupancy_no_replay_and_short_reentry_suppression(self):
        self.queue.ingest(self.event())
        job = self.welcome()
        result = self.queue.finish(job, success=False, sent_any=True, reason="audio_failed")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.queue.public()["welcome_occupied_count"], 0)
        self.assertEqual(self.queue.public()["welcome_seen_count"], 0)
        self.assertFalse(self.queue.has_pending())
        self.assertEqual(self.queue.ingest(self.event(index=1, uid="private-0"))["reason"], "welcome_recently_sent")
        self.clock.now += 15
        self.assertEqual(self.queue.ingest(self.event(index=2, uid="private-0"))["status"], "queued")

    def test_timeout_terminates_without_replay_and_rejects_late_success(self):
        self.queue.ingest(self.event())
        job = self.welcome()
        self.clock.now += PREPARATION_TTL_SECONDS
        self.assertFalse(self.queue.reservation_active(job))
        self.assertEqual(self.queue.finish(job, success=True, sent_any=True)["status"], "partial")
        self.assertEqual(self.queue.public()["welcome_seen_count"], 0)
        self.assertFalse(self.queue.has_pending())

    def test_started_before_preparation_deadline_finishes_after_deadline(self):
        self.queue.ingest(self.event())
        job = self.welcome()
        self.clock.now += PREPARATION_TTL_SECONDS - 1
        self.assertTrue(self.queue.mark_started(job))
        self.assertTrue(self.queue.mark_started(job["id"]))
        self.clock.now += 10
        self.assertTrue(self.queue.reservation_active(job))
        result = self.queue.finish(job, success=True, sent_any=True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.queue.public()["welcome_seen_count"], 1)

    def test_not_started_cannot_start_at_or_after_preparation_deadline(self):
        self.queue.ingest(self.event())
        job = self.welcome()
        self.clock.now += PREPARATION_TTL_SECONDS
        self.assertFalse(self.queue.mark_started(job))
        self.assertFalse(self.queue.reservation_active(job))
        self.assertNotEqual(self.queue.finish(job, success=True, sent_any=True)["status"], "completed")
        self.assertEqual(self.queue.public()["welcome_seen_count"], 0)

    def test_preserved_started_round_survives_deadline_but_not_new_scene(self):
        self.queue.ingest(self.event())
        job = self.welcome()
        self.assertTrue(self.queue.mark_started(job))
        self.queue.clear("source_disconnect", preserve_reservation_ids=[job["id"]])
        self.clock.now += PREPARATION_TTL_SECONDS + 1
        self.assertTrue(self.queue.reservation_active(job))
        self.queue.clear("new_session", reset_seen=True, preserve_reservation_ids=[job["id"]])
        self.assertFalse(self.queue.mark_started(job))
        self.assertEqual(self.queue.finish(job, success=True, sent_any=True)["status"], "cancelled")

    def test_clear_and_new_scene_reject_late_finish_and_old_token(self):
        self.queue.ingest(self.event())
        old = self.queue.snapshot_candidates()["welcome"]
        job = self.welcome()
        self.queue.clear("new_session", reset_seen=True)
        self.assertFalse(self.queue.reservation_active(job))
        self.assertEqual(self.queue.finish(job, success=True, sent_any=True)["status"], "cancelled")
        self.assertEqual(self.queue.reserve_selection(welcome_id=old["id"]), [])
        self.assertEqual(self.queue.ingest(self.event())["status"], "queued")
        self.assertEqual(self.queue.public()["welcome_seen_count"], 0)

    def test_disconnect_preserves_only_explicit_current_reservation(self):
        self.queue.ingest(self.event(index=0))
        current = self.welcome()
        self.queue.ingest(self.event(index=1))
        prepared = self.welcome()
        self.queue.ingest(self.event(index=2))
        result = self.queue.clear("source_disconnect", preserve_reservation_ids=[current["reservation_id"]])
        self.assertEqual(result["preserved_reservations"], 1)
        self.assertTrue(self.queue.reservation_active(current))
        self.assertFalse(self.queue.reservation_active(prepared))
        self.assertFalse(self.queue.has_pending())
        self.assertEqual(self.queue.public()["welcome_occupied_count"], 1)
        self.assertEqual(self.queue.finish(current, success=True, sent_any=True)["status"], "completed")
        self.assertEqual(self.queue.public()["welcome_seen_count"], 1)

    def test_configure_can_preserve_started_round_with_original_frozen_content(self):
        self.queue.ingest(self.event())
        current = self.welcome()
        before = copy.deepcopy(current)
        self.queue.configure({"welcome_enabled": False}, preserve_reservation_ids=[current["id"]])
        self.assertEqual(current, before)
        self.assertTrue(self.queue.reservation_active(current))
        self.assertEqual(self.queue.finish(current, success=True, sent_any=True)["status"], "completed")
        self.assertEqual(self.queue.public()["welcome_seen_count"], 1)

    def test_reset_or_stop_always_overrides_reservation_preservation(self):
        for reason, reset in (("new_session", True), ("source_disconnect", True),
                              ("session_stop", False), ("service_closed", False)):
            with self.subTest(reason=reason, reset=reset):
                self.queue.clear("new_session", reset_seen=True)
                self.queue.ingest(self.event())
                current = self.welcome()
                self.queue.clear(reason, reset_seen=reset, preserve_reservation_ids=[current["id"]])
                self.assertFalse(self.queue.reservation_active(current))
                self.assertEqual(self.queue.finish(current, success=True, sent_any=True)["status"], "cancelled")
                self.assertEqual(self.queue.public()["welcome_seen_count"], 0)

    def test_pending_expiry_never_counts_as_seen_and_does_not_delay_entry(self):
        self.queue.ingest(self.event())
        self.assertIsNotNone(self.queue.snapshot_candidates()["welcome"])
        self.clock.now += 60
        self.assertIsNone(self.queue.snapshot_candidates()["welcome"])
        self.assertEqual(self.queue.public()["welcome_seen_count"], 0)
        self.assertEqual(self.queue.ingest(self.event(index=1, uid="private-0"))["status"], "queued")

    def test_multiple_gifts_merge_text_and_only_original_configured_action(self):
        for i, name in enumerate(("小心心", "玫瑰", "棒棒糖")):
            self.queue.ingest(self.event("gift", index=i, giftId=f"g{i}", giftName=name))
        preview = self.queue.snapshot_candidates()["gift"]
        jobs = self.queue.reserve_selection(gift_ids=[item["id"] for item in preview])
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(len(job["meta"]["sources"]), 3)
        self.assertEqual(job["motions"], preview[0]["motions"])
        self.assertEqual(len(job["meta"]["merged_motion_source_ids"]), 2)
        for item in preview:
            self.assertIn(item["nickname"], job["text"])
        self.assertLessEqual(len(job["text"]), 120)
        self.assertFalse(self.queue.has_pending())
        self.assertEqual(self.queue.reserve_selection(gift_ids=[preview[0]["id"]]), [])
        self.queue.finish(job, success=True, sent_any=True)

    def test_gift_preview_mutation_does_not_modify_reserved_actions(self):
        self.queue.ingest(self.event("gift"))
        preview = self.queue.snapshot_candidates()["gift"][0]
        motions = copy.deepcopy(preview["motions"])
        preview["motions"][0]["description"] = "换衣服"
        job = self.queue.reserve_selection(gift_ids=[preview["id"]])[0]
        self.assertEqual(job["motions"], motions)

    def test_invalid_mixed_selection_is_atomic_and_gift_limit_enforced(self):
        for i in range(4):
            self.queue.ingest(self.event("gift", index=i))
        self.queue.ingest(self.event())
        preview = self.queue.snapshot_candidates()
        ids = [item["id"] for item in preview["gift"]]
        self.assertEqual(self.queue.reserve_selection(gift_ids=ids), [])
        self.assertEqual(self.queue.reserve_selection(gift_ids=ids[:1], welcome_id="invented"), [])
        self.assertEqual(self.queue.public()["pending_count"], 5)
        self.assertEqual(self.queue.reserve_selection(gift_ids=[ids[0], ids[0]]), [])

    def test_injected_identity_matches_across_categories_without_raw_ids(self):
        self.queue = InteractionQueue(clock=self.clock, viewer_key=lambda room, uid: "anonymous-" + uid[-1])
        self.queue.ingest(self.event())
        self.queue.ingest(self.event("gift"))
        preview = self.queue.snapshot_candidates()
        self.assertEqual(preview["gift"][0]["viewer_key"], preview["welcome"]["representative_viewer_keys"][0])
        self.assertNotIn("private-", json.dumps(preview))

    def test_reservation_protects_gift_group_past_replay_window(self):
        with patch("live_interactions.GROUP_REPLAY_SECONDS", 5):
            self.queue.ingest(self.event("gift"))
            job = self.queue.pop("gift")
            before = copy.deepcopy(job)
            self.clock.now += 6
            self.assertEqual(self.queue.ingest(self.event("gift", index=1, uid="private-0", repeatCount=7))["status"], "duplicate")
            self.assertEqual(job, before)
            self.queue.finish(job, success=True, sent_any=True)


if __name__ == "__main__":
    unittest.main()
