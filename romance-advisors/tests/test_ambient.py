"""Optional performance scheduling against the real Workbench/Program pair."""
import asyncio
import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from romance.server import Workbench
from tests.test_server import SyntheticModel


REAL_SLEEP = asyncio.sleep


async def yield_only(_seconds):
    await REAL_SLEEP(0)


class AmbientModel(SyntheticModel):
    def __init__(self):
        super().__init__()
        self.slow = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.slow_draft = False
        self.draft_started = asyncio.Event()
        self.draft_release = asyncio.Event()
        self.result = {"speak": True, "text": "你们帮我想想，我有点紧张。", "instruction": "带笑求助，自然停顿。",
                       "expression": {"emotion": "紧张又期待", "actions": [], "recovery": None},
                       "intent": "求助", "comment_ids": []}

    async def generate(self, task, payload):
        if task == "draft" and self.slow_draft:
            self.draft_started.set()
            await self.draft_release.wait()
        if task != "ambient":
            return await super().generate(task, payload)
        self.calls.append((task, copy.deepcopy(payload)))
        self.started.set()
        if self.slow:
            await self.release.wait()
        return copy.deepcopy(self.result)


class ControlledAvatar:
    """A job can be terminal while its worker still owes cleanup/Q."""
    def __init__(self):
        self.origin = "http://127.0.0.1:4444"
        self.player_url = self.origin + "/player/test"
        self.generation = self.owner_id = None
        self.state = "idle"
        self.optional_status = "sent"
        self.delivery_started = False
        self.submitted = []
        self.prepared = {}
        self.timeline = []
        self.cancelled = asyncio.Event()
        self.updates = asyncio.Queue()
        self.cancel_reply_lost = False
        self.cancel_failures = 0
        self.cancel_calls = 0
        self.query_fails_after_cancel = False
        self.queried_ids = []
        self.estimated_delivery_seconds = 8.74

    async def prepare_session(self, owner_id):
        self.owner_id, self.generation = owner_id, "generation-test"

    async def snapshot(self):
        return {"session": {"state": self.state}, "studio": {"can_start": True}}

    async def start(self):
        self.state = "ready"

    async def heartbeat(self):
        pass

    async def external_state(self):
        return {"owner_id": self.owner_id, "generation": self.generation, "state": self.state}

    def receipt(self, record, status, *, started=False, pending=False):
        return {"job_id": record["job_id"], "generation": self.generation,
                "status": status, "send_complete": status == "sent",
                "delivery_started": started, "worker_pending": pending,
                "estimated_delivery_seconds": self.estimated_delivery_seconds}

    async def prepare(self, record, text):
        self.prepared[record["job_id"]] = (copy.deepcopy(record), text)
        optional = record["job_id"].startswith("ambient:")
        if optional and self.optional_status in {"preparing", "error"}:
            return self.receipt(record, self.optional_status, started=self.delivery_started,
                                pending=self.optional_status == "preparing")
        return self.receipt(record, "prepared")

    async def dispatch(self, record):
        return await self.submit(*self.prepared[record["job_id"]])

    async def submit(self, record, text):
        self.submitted.append((copy.deepcopy(record), text))
        optional = record["job_id"].startswith("ambient:")
        self.timeline.append("optional_GE" if optional and self.delivery_started else "optional" if optional else "formal")
        if not optional:
            return self.receipt(record, "sent", started=True)
        return self.receipt(record, self.optional_status, started=self.delivery_started,
                            pending=self.optional_status in {"preparing", "sending"})

    async def job(self, record):
        self.queried_ids.append(record["job_id"])
        if self.cancelled.is_set() and self.query_fails_after_cancel:
            raise OSError("synthetic query failure")
        update = await self.updates.get()
        return self.receipt(record, **update)

    async def cancel_job(self, record):
        self.cancel_calls += 1
        self.cancelled.set()
        if self.cancel_reply_lost or self.cancel_calls <= self.cancel_failures:
            raise OSError("synthetic lost cancel reply")
        if (self.optional_status != "preparing"
                and not any(r["job_id"] == record["job_id"] for r, _ in self.submitted)):
            # A prepared next has no sender worker. It must not borrow the
            # previous job's delivery_started flag or wait for its receipt.
            return self.receipt(record, "cancelled", pending=False)
        # Like the bridge, an already-started segment keeps its required tail.
        return self.receipt(record, "sending" if self.delivery_started else "cancelled",
                            started=self.delivery_started, pending=True)

    async def stop(self):
        self.state = "ended"

    async def close(self):
        pass


class AmbientSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.model = AmbientModel()
        self.avatar = ControlledAvatar()
        self.wb = Workbench(":memory:", model=self.model, avatar_client=self.avatar)
        self.program = self.wb.program
        self.background = []
        # Polling yields to our controlled receipt queue without a wall-clock wait.
        self.sleep_patch = patch("romance.program.asyncio.sleep", yield_only)
        self.sleep_patch.start()
        self.program.start()
        await self.program.task
        await self.wb.job
        self.wb.state()

    async def asyncTearDown(self):
        for task in self.background:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)
        await self.wb.close()
        self.sleep_patch.stop()

    async def spin_until(self, condition):
        for _ in range(100):
            if condition():
                return
            await REAL_SLEEP(0)
        self.fail("Controlled asynchronous operation did not reach its checkpoint")

    async def opportunity(self):
        ambient = self.program.ambient
        ambient.tick(self.wb.state())  # Observe scope before forcing its first opportunity.
        ambient.next_attempt = ambient.last_sent = 0
        ambient.tick(self.wb.state())
        await self.spin_until(lambda: self.model.started.is_set())
        return ambient.task

    def comment(self, event_id, text):
        r = self.wb.game.current_round
        return self.wb.accept_comment({"event_id": event_id, "author_id": event_id,
                                       "nickname": "测试观众", "text": text,
                                       "expected_round_id": r["id"], "expected_phase": r["phase"]})

    async def expire(self):
        self.wb.state()
        self.wb.deadline = time.time() - 1
        await self.program.step()
        if self.wb.job:
            await self.wb.job

    async def test_slow_ambient_does_not_block_comments_votes_or_phase_deadlines(self):
        self.model.slow = True
        await self.opportunity()
        self.assertFalse(self.wb.state()["busy"])
        self.comment("suggestion", "认真邀约")
        self.assertEqual(len(self.wb.game.data["proposals"]), 1)
        await self.expire()
        self.assertEqual(self.wb.game.current_round["phase"], "VOTING")
        # The same still-valid reflection may keep preparing across this phase;
        # it must not block voting or be duplicated just to mark the transition.
        self.comment("vote", "投1")
        self.assertEqual(len(self.wb.game.data["votes"]), 1)
        payload = [p for task, p in self.model.calls if task == "ambient"][-1]
        self.assertNotIn("options", payload)
        self.assertNotIn("private_preferences", str(payload))
        await self.expire()
        self.assertEqual(self.wb.game.current_round["phase"], "DONE")
        self.assertEqual(len(self.avatar.submitted), 1)
        self.assertFalse(self.avatar.submitted[0][0]["job_id"].startswith("ambient:"))

    async def test_model_result_after_phase_change_is_not_submitted(self):
        self.wb.deadline = time.time() + 20  # The time-sensitive participation task.
        self.model.slow = True
        task = await self.opportunity()
        self.comment("suggestion", "认真邀约")
        await self.wb.game.close_collection()
        self.wb.state()
        self.model.release.set()
        await task
        self.assertEqual(self.avatar.submitted, [])
        self.assertEqual(self.program.ambient.history, [])

    async def test_model_result_after_episode_change_is_not_submitted(self):
        self.model.slow = True
        task = await self.opportunity()
        await self.wb.game.new_episode()
        self.wb.state()
        self.model.release.set()
        await task
        self.assertEqual(self.avatar.submitted, [])

    async def test_conversation_survives_phase_transition_while_game_is_busy(self):
        self.model.slow = True
        task = await self.opportunity()
        candidate = self.program.next
        self.assertEqual(candidate["content_mode"], "conversation")
        self.wb.game.current_round["phase"] = "SELECTING"
        self.wb.game.busy = True
        self.program.ambient.tick(self.wb.state())
        self.assertIsNone(self.wb.deadline)
        self.assertIs(self.program.next, candidate)
        self.assertFalse(candidate["invalidated"])
        self.model.release.set()
        await task
        self.wb.game.busy = False
        self.assertEqual(len(self.avatar.submitted), 1)
        self.assertEqual(self.avatar.submitted[0][0]["job_id"], candidate["job_id"])
        self.assertNotIn("finish_before", self.avatar.submitted[0][0])

    async def test_result_aside_survives_eight_seconds_and_same_story_next_round(self):
        with patch.object(self.program, "notify_comment"):
            self.comment("result-suggestion", "认真解释一下。")
        await self.wb.game.close_collection()
        original_generate = self.model.generate

        async def continue_story(task, payload):
            if task == "reply":
                return {"message": "可以先聊聊。", "delta": 5, "ending": "continue", "reason": "愿意继续了解"}
            return await original_generate(task, payload)

        with patch.object(self.model, "generate", continue_story):
            await self.wb.game.close_voting()
        self.wb.state()
        self.assertEqual(self.wb.game.current_round["phase"], "DONE")
        self.assertAlmostEqual(self.wb.deadline - time.time(), 8, delta=.1)
        self.model.slow = True
        task = await self.opportunity()
        candidate = self.program.next
        self.assertEqual(candidate["trigger"], "new_comments")
        old_id = self.wb.game.current_round["id"]
        self.wb.deadline = time.time() - 1
        await self.program.step()
        await self.wb.job
        self.program.ambient.tick(self.wb.state())
        self.assertNotEqual(old_id, self.wb.game.current_round["id"])
        self.model.release.set()
        await task
        optional = [(r, text) for r, text in self.avatar.submitted if r["job_id"].startswith("ambient:")]
        self.assertEqual(len(optional), 1)
        self.assertNotIn("finish_before", optional[0][0])
        self.assertNotIn("expires_at", optional[0][0])
        self.assertEqual(self.program.ambient.delivery_samples[-1], 8.74)

    async def test_changed_public_message_invalidates_conversation(self):
        self.model.slow = True
        task = await self.opportunity()
        self.wb.game._message("woman", "现在有了新的剧情事实。", self.wb.game.current_round["id"])
        self.model.release.set()
        await task
        self.assertEqual(self.avatar.submitted, [])

    async def test_changed_score_invalidates_conversation(self):
        self.model.slow = True
        task = await self.opportunity()
        self.wb.game.episode["score"] += 1
        self.model.release.set()
        await task
        self.assertEqual(self.avatar.submitted, [])

    async def test_late_comment_is_answerable_without_reopening_proposals(self):
        self.wb.deadline = time.time() - 1
        with patch.object(self.program, "notify_comment"):
            receipt = self.comment("late-comment", "别紧张，观众都陪着你呢。")
        self.assertEqual(receipt["result"], "ignored")
        self.assertEqual(self.wb.game.data["proposals"], [])
        self.model.result["comment_ids"] = [receipt["id"]]
        await (await self.opportunity())
        self.assertEqual(self.model.calls[-1][1]["comments"][0]["id"], receipt["id"])
        self.assertIn(receipt["id"], self.program.ambient.responded_ids)
        self.assertEqual(self.wb.game.data["proposals"], [])
        self.assertNotIn("finish_before", self.avatar.submitted[0][0])

    async def test_previous_round_and_stale_window_comments_remain_in_reply_pool(self):
        old_round = self.wb.game.current_round["id"]
        with patch.object(self.program, "notify_comment"):
            self.comment("earlier-round", "你刚才那个表情笑死我了。")
        self.wb.game.current_round["phase"] = "DONE"
        self.wb.game.start_round()
        with patch.object(self.program, "notify_comment"):
            stale = self.wb.accept_comment({"event_id": "stale-window", "author_id": "late-viewer",
                "nickname": "晚到观众", "text": "放轻松就好。", "expected_round_id": old_round,
                "expected_phase": "COLLECTING"})
        self.assertEqual(stale["result"], "stale")
        self.assertIsNone(stale["proposal_id"])
        self.model.result["comment_ids"] = ["earlier-round", "stale-window"]
        await (await self.opportunity())
        self.assertEqual({c["id"] for c in self.model.calls[-1][1]["comments"]},
                         {"earlier-round", "stale-window"})
        ambient = self.program.ambient
        self.assertEqual(ambient.selected_comments(ambient.scope, time.monotonic() + 10), [])

    async def test_busy_generation_phases_accept_replies_without_a_deadline(self):
        for phase in ("SELECTING", "DECIDING", "DRAFTING", "WAITING_REPLY"):
            with self.subTest(phase=phase):
                self.wb.game.current_round["phase"] = phase
                self.wb.game.busy = True
                self.wb.state()
                self.assertIsNone(self.wb.deadline)
                with patch.object(self.program, "notify_comment"):
                    receipt = self.comment("busy-" + phase, "你这样挺真实的。")
                self.model.result["comment_ids"] = [receipt["id"]]
                self.program.ambient.next_dispatch = 0
                await (await self.opportunity())
                self.assertIn(receipt["id"], self.program.ambient.responded_ids)
                self.assertNotIn("finish_before", self.avatar.submitted[-1][0])
                self.assertNotEqual(self.avatar.submitted[-1][0].get("mode"), "motion")
        self.assertEqual(len(self.avatar.submitted), 4)
        self.wb.game.busy = False

    async def test_second_result_comment_is_not_blocked_by_first_result_attempt(self):
        self.wb.game.current_round["phase"] = "DONE"
        for index in (1, 2):
            with patch.object(self.program, "notify_comment"):
                receipt = self.comment("result-comment-" + str(index), "这个故事还挺有意思。")
            self.model.result["comment_ids"] = [receipt["id"]]
            self.program.ambient.next_dispatch = 0
            await (await self.opportunity())
            self.assertIn(receipt["id"], self.program.ambient.responded_ids)
        self.assertEqual(len(self.avatar.submitted), 2)

    async def test_busy_context_never_reveals_saved_unreleased_formal_message(self):
        r = self.wb.game.current_round
        r["phase"] = "WAITING_REPLY"
        r["performance"] = {"status": "pending", "man_released": False}
        self.wb.game._message("man", "尚未向观众公开的正式选择", r["id"])
        self.wb.game.busy = True
        await (await self.opportunity())
        payload = self.model.calls[-1][1]
        self.assertNotIn("尚未向观众公开的正式选择", str(payload))
        self.assertEqual(payload["messages"], [{"role": m["role"], "text": m["text"]}
            for m in self.wb.game.public_state()["messages"][-8:]])
        self.wb.game.busy = False

    async def test_stale_answer_can_be_regenerated_from_same_unanswered_comment(self):
        self.model.slow = True
        with patch.object(self.program, "notify_comment"):
            self.comment("reconsider", "你怎么看刚才那句？")
        self.model.result["comment_ids"] = ["reconsider"]
        task = await self.opportunity()
        self.wb.game._message("woman", "剧情已经有了新进展。", self.wb.game.current_round["id"])
        self.model.release.set()
        await task
        self.assertEqual(self.avatar.submitted, [])
        self.assertNotIn("reconsider", self.program.ambient.responded_ids)
        self.program.ambient.attempted_at["reconsider"] -= 10
        await (await self.opportunity())
        self.assertIn("reconsider", self.program.ambient.responded_ids)
        self.assertEqual(len(self.avatar.submitted), 1)
        self.assertIn("剧情已经有了新进展。", str(self.model.calls[-1][1]["messages"]))

    async def test_new_cast_does_not_inherit_old_story_comments(self):
        old_round = self.wb.game.current_round["id"]
        with patch.object(self.program, "notify_comment"):
            self.comment("old-story", "你和她刚才怎么回事？")
        await self.wb.game.new_episode()
        # A network-delayed old page request is recorded under the receiving
        # episode, but must still keep its original character context.
        with patch.object(self.program, "notify_comment"):
            receipt = self.wb.accept_comment({"event_id": "old-story-arrived-late",
                "author_id": "delayed-viewer", "nickname": "晚到观众", "text": "你刚才还说要请她喝酒。",
                "expected_round_id": old_round, "expected_phase": "COLLECTING"})
        self.assertEqual(receipt["result"], "stale")
        self.assertEqual(receipt["episode_id"], self.wb.game.episode["id"])
        await (await self.opportunity())
        self.assertEqual(self.model.calls[-1][1]["comments"], [])

    async def test_unprepared_cast_and_formal_waiting_do_not_start_ambient(self):
        self.program.formal_waiting = 1
        self.program.ambient.tick(self.wb.state())
        self.program.formal_waiting = 0
        self.wb.game.episode["status"] = "PREPARING"
        self.program.ambient.tick(self.wb.state())
        await REAL_SLEEP(0)
        self.assertFalse(self.model.started.is_set())
        self.assertEqual(self.avatar.prepared, {})

    async def test_comment_ownership_survives_phase_change_and_is_answered_once(self):
        self.model.slow = True
        with patch.object(self.program, "notify_comment"):
            self.comment("cross-phase", "你怎么看刚才那句话？")
        comment_id = self.wb.game.data["comments"][-1]["id"]
        self.model.result["comment_ids"] = [comment_id]
        task = await self.opportunity()
        candidate = self.program.next
        await self.wb.game.close_collection()
        self.program.ambient.tick(self.wb.state())
        self.assertIs(self.program.ambient._inflight_owners[comment_id], candidate)
        self.model.release.set()
        await task
        self.assertIn(comment_id, self.program.ambient.responded_ids)
        self.assertNotIn(comment_id, self.program.ambient.inflight_ids)
        self.assertEqual(len(self.avatar.submitted), 1)

    async def test_validation_error_keeps_field_reason_without_model_text(self):
        self.model.result["instruction"] = "PRIVATE_MODEL_CONTENT" * 10
        with self.assertLogs("romance.performance", level="WARNING") as logs:
            await (await self.opportunity())
        event = self.program.ambient.events[-1]
        self.assertEqual(event["reason"], "invalid_ambient_result")
        self.assertEqual(event["validation"]["field"], "instruction")
        self.assertNotIn("PRIVATE_MODEL_CONTENT", str(event) + str(logs.output))
        self.assertEqual(self.avatar.submitted, [])

    async def test_participation_budget_uses_actual_preparation_and_delivery(self):
        ambient = self.program.ambient
        self.assertEqual(ambient.participation_budget(), 14.5)
        with patch("romance.performance.time.monotonic", return_value=10):
            ambient.prepared({"planning_started": 6}, {"estimated_delivery_seconds": 8.42})
        self.assertAlmostEqual(ambient.participation_budget(), 12.42)

    async def test_stop_cancels_pending_conversation_without_submission(self):
        self.model.slow = True
        task = await self.opportunity()
        await self.program.stop()
        self.model.release.set()
        await task
        self.assertEqual(self.avatar.submitted, [])
        self.assertEqual(self.program.state, "stopped")

    async def test_cast_change_waits_for_started_line_to_finish(self):
        self.avatar.optional_status = "sending"
        self.avatar.delivery_started = True
        task = await self.opportunity()
        await self.spin_until(lambda: len(self.avatar.submitted) == 1)
        old_episode = self.wb.game.episode["id"]
        self.wb.game.current_round["phase"] = "DONE"
        self.wb.game.episode["status"] = "SUCCEEDED"
        self.wb.state()
        self.wb.deadline = time.time() - 1
        await self.program.step()
        self.assertEqual(self.wb.game.episode["id"], old_episode)
        self.avatar.updates.put_nowait({"status": "sent", "started": True, "pending": False})
        await task
        await self.program.step()
        await self.wb.job
        self.assertNotEqual(self.wb.game.episode["id"], old_episode)

    async def test_ssml_flows_through_ambient_prepare_and_dispatch_without_extra_generation(self):
        ssml = '<speak rate="0.95">你们帮我想想，<break time="180ms"/>我有点紧张。</speak>'
        self.model.result["ssml"] = ssml
        task = await self.opportunity()
        await task
        self.assertEqual([name for name, _ in self.model.calls].count("ambient"), 1)
        self.assertEqual(len(self.avatar.submitted), 1)
        record, text = self.avatar.submitted[0]
        self.assertEqual(text, self.model.result["text"])
        self.assertEqual(record["plan"]["segments"][0]["ssml"], ssml)
        self.assertEqual(self.avatar.prepared[record["job_id"]][0]["plan"], record["plan"])
        self.assertNotIn("ssml", record)
        self.assertEqual(self.program.ambient.history[-1]["text"], text)

    async def start_contended_formal(self, *, started, expect_failure=False):
        self.avatar.optional_status = "sending" if started else "preparing"
        self.avatar.delivery_started = started
        optional = await self.opportunity()
        await self.spin_until(lambda: len(self.avatar.prepared) == 1)
        if started:
            await self.spin_until(lambda: len(self.avatar.submitted) == 1)
        formal = asyncio.create_task(self.program.perform(self.program.prepare("formal-round", "正式决定。"), "正式决定。"))
        self.background.append(formal)
        # The optional poll may already await a provider receipt when invalidated.
        await self.spin_until(lambda: self.program.formal_waiting == 1)
        self.avatar.updates.put_nowait({"status": self.avatar.optional_status, "started": started, "pending": True})
        await self.spin_until(lambda: self.avatar.cancelled.is_set())
        self.assertEqual(len(self.avatar.submitted), int(started))
        if not expect_failure:
            self.assertEqual(self.program.send_lock.locked(), started)
            self.assertFalse(formal.done())
        return optional, formal

    async def test_formal_cancels_unstarted_optional_but_waits_for_worker_exit(self):
        self.avatar.cancel_reply_lost = True
        optional, formal = await self.start_contended_formal(started=False)
        self.avatar.updates.put_nowait({"status": "cancelled", "pending": True})
        await REAL_SLEEP(0)
        await REAL_SLEEP(0)
        self.assertFalse(formal.done())
        self.assertEqual(len(self.avatar.submitted), 0)
        self.avatar.updates.put_nowait({"status": "cancelled", "pending": False})
        await optional
        await formal
        self.assertEqual(self.avatar.timeline, ["formal"])
        self.assertEqual(set(self.avatar.queried_ids), {next(iter(self.avatar.prepared))})
        self.assertEqual([row["text"] for row in self.program.ambient.history], ["正式决定。"])

    async def test_started_optional_keeps_tail_and_formal_awaits_worker_completion(self):
        optional, formal = await self.start_contended_formal(started=True)
        self.avatar.updates.put_nowait({"status": "sent", "started": True, "pending": True})
        await REAL_SLEEP(0)
        await REAL_SLEEP(0)
        self.assertFalse(formal.done())
        self.avatar.timeline.append("optional_Q")
        self.avatar.updates.put_nowait({"status": "sent", "started": True, "pending": False})
        await optional
        await formal
        self.assertEqual(self.avatar.timeline, ["optional_GE", "optional_Q", "formal"])
        self.assertEqual(len(self.program.ambient.history), 2)

    async def test_lost_cancel_reply_and_failed_query_block_the_formal_sender(self):
        self.avatar.cancel_reply_lost = True
        self.avatar.query_fails_after_cancel = True
        optional, formal = await self.start_contended_formal(started=False, expect_failure=True)
        await optional
        with self.assertRaisesRegex(ValueError, "invalid_performance_send"):
            await formal
        self.assertTrue(self.program.sender_failed)
        self.assertEqual(len(self.avatar.submitted), 0)
        await self.program.step()
        self.assertEqual(self.program.state, "error")

    async def test_cancel_not_received_is_repeated_once_and_waits_for_worker_exit(self):
        self.avatar.cancel_failures = 1
        optional, formal = await self.start_contended_formal(started=False)
        # Query proves the first cancellation did not reach the preparing job.
        self.avatar.updates.put_nowait({"status": "synthesizing", "pending": True})
        await self.spin_until(lambda: self.avatar.cancel_calls == 2)
        self.assertEqual(len(self.avatar.submitted), 0)
        self.assertFalse(formal.done())
        self.avatar.updates.put_nowait({"status": "cancelled", "pending": False})
        await optional
        await formal
        self.assertEqual(self.avatar.cancel_calls, 2)
        self.assertEqual(self.avatar.timeline, ["formal"])

    async def test_optional_preparation_error_without_delivery_is_skipped(self):
        self.avatar.optional_status = "error"
        await (await self.opportunity())
        self.assertEqual(self.program.ambient.history, [])
        await self.program.step()
        self.assertEqual(self.program.state, "running")
        self.assertFalse(self.program.ambient.error)

    async def test_optional_partial_delivery_error_stops_program(self):
        self.avatar.optional_status = "error"
        self.avatar.delivery_started = True
        await (await self.opportunity())
        self.assertEqual(self.program.ambient.history, [])
        await self.program.step()
        self.assertEqual(self.program.state, "error")
        self.assertEqual(self.avatar.state, "ended")

    async def test_sent_only_history_silence_cooldown_and_new_episode_reset(self):
        before = copy.deepcopy(self.wb.game.data["messages"])
        await (await self.opportunity())
        ambient = self.program.ambient
        self.assertEqual(len(ambient.history), 1)
        self.assertEqual(self.wb.game.data["messages"], before)
        self.assertEqual(self.wb.game.episode["score"], 50)
        calls = len([t for t, _ in self.model.calls if t == "ambient"])
        self.model.result = {"speak": False}
        for _ in range(20):
            ambient.tick(self.wb.state())
        await ambient.task
        # Preparation is allowed during breathing; repeated ticks still reserve
        # only one candidate, and model silence cannot enter sent history.
        self.assertEqual(len([t for t, _ in self.model.calls if t == "ambient"]), calls + 1)
        self.assertEqual(len(ambient.history), 1)
        self.assertEqual(len(self.avatar.submitted), 1)
        await self.wb.game.new_episode()
        ambient.tick(self.wb.state())
        self.assertEqual(ambient.history, [])

    async def test_short_breath_allows_another_quiet_line_without_new_comments(self):
        await (await self.opportunity())
        ambient = self.program.ambient
        previous = ambient.task
        # 声音收尾已由发送器完成，短呼吸后可继续；无需再等旧的8/22秒门槛。
        with patch("romance.performance.time", SimpleNamespace(
                time=time.time, monotonic=lambda: ambient.last_sent + 3.1)):
            ambient.tick(self.wb.state())
            self.assertIsNot(ambient.task, previous)
            await ambient.task
        self.assertEqual(len(self.avatar.submitted), 2)
        self.assertEqual(self.model.calls[-1][1]["scene"]["content_mode"], "conversation")
        self.assertNotIn("trigger", self.model.calls[-1][1]["scene"])
        self.assertEqual(self.model.calls[-1][1]["comments"], [])

    async def test_slow_silence_waits_after_completion_before_trying_again(self):
        self.model.result = {"speak": False}
        self.model.slow = True
        clock = {"now": time.monotonic()}
        with patch("romance.performance.time", SimpleNamespace(
                time=time.time, monotonic=lambda: clock["now"])):
            task = await self.opportunity()
            # 模拟请求已消耗8秒，但不等待真实网络，也不改asyncio自己的时钟。
            clock["now"] += 8
            self.model.release.set()
            await task
            self.assertGreaterEqual(self.program.ambient.next_attempt, clock["now"] + 4)
            for _ in range(20):
                self.program.ambient.tick(self.wb.state())
            self.assertEqual(sum(name == "ambient" for name, _ in self.model.calls), 1)
            self.assertEqual(self.avatar.submitted, [])

    async def test_real_comment_reply_takes_priority_over_closing_prompt(self):
        self.comment("helpful", "别紧张，先说说你在想什么。")
        self.wb.deadline = time.time() + 25
        await (await self.opportunity())
        payload = self.model.calls[-1][1]
        self.assertEqual(payload["scene"]["content_mode"], "conversation")
        self.assertEqual([c["text"] for c in payload["comments"]], ["别紧张，先说说你在想什么。"])
        self.assertNotIn("expires_at", self.avatar.submitted[0][0])
        self.assertNotIn("finish_before", self.avatar.submitted[0][0])

    async def test_no_comment_closing_prompt_still_obeys_participation_deadline(self):
        self.wb.deadline = time.time() + 25
        await (await self.opportunity())
        self.assertEqual(self.model.calls[-1][1]["scene"]["trigger"], "closing_soon")
        record, _ = self.avatar.submitted[0]
        self.assertEqual(record["expires_at"], self.wb.deadline)
        self.assertEqual(record["finish_before"], self.wb.deadline)

    async def test_short_window_chooses_conversation_without_phase_expiry(self):
        ambient = self.program.ambient
        self.wb.deadline = time.time() + 10
        ambient.tick(self.wb.state())
        ambient.next_attempt = ambient.last_sent = 0
        ambient.tick(self.wb.state())
        await ambient.task
        self.assertEqual(self.model.calls[-1][1]["scene"]["content_mode"], "conversation")
        self.assertNotIn("remaining_seconds", self.model.calls[-1][1]["scene"])
        record, _ = self.avatar.submitted[0]
        self.assertNotIn("expires_at", record)
        self.assertNotIn("finish_before", record)

    async def start_slow_draft(self):
        # This helper isolates the drafting gesture; cross-phase speech is tested
        # separately and can now legitimately survive the collection transition.
        with patch.object(self.program, "notify_comment"):
            self.comment("thinking-suggestion", "认真解释一下刚才为什么紧张。")
        # This helper tests a glance without an unanswered comment taking its
        # place. Dedicated tests below cover the higher-priority reply path.
        with patch.object(self.program, "reserve_next", return_value=None):
            self.program.ambient.tick(self.wb.state())
        self.program.ambient.responded_ids.add("thinking-suggestion")
        await self.wb.game.close_collection()
        self.model.slow_draft = True
        task = asyncio.create_task(self.wb.game.close_voting())
        self.background.append(task)
        await self.model.draft_started.wait()
        self.assertEqual(self.wb.game.current_round["phase"], "DRAFTING")
        self.assertTrue(self.wb.game.busy)
        return task

    async def test_drafting_glance_runs_once_without_model_or_spoken_history(self):
        draft = await self.start_slow_draft()
        calls = copy.deepcopy(self.model.calls)
        ambient = self.program.ambient
        ambient.tick(self.wb.state())
        await ambient.task
        self.assertEqual(len(self.avatar.submitted), 1)
        record, text = self.avatar.submitted[0]
        self.assertEqual(record["mode"], "motion")
        self.assertEqual(text, "")
        self.assertEqual(len(record["plan"]["actions"]), 1)
        self.assertEqual(record["plan"]["actions"][0]["motions"][0]["part"], "head")
        self.assertEqual(self.model.calls, calls)
        self.assertEqual(ambient.history, [])
        self.model.result = {"speak": False}
        for _ in range(5):
            ambient.tick(self.wb.state())
        await REAL_SLEEP(0)
        self.assertEqual(len(self.avatar.submitted), 1)
        self.model.draft_release.set()
        await draft
        self.assertEqual(self.wb.game.current_round["phase"], "DONE")
        self.assertEqual(self.avatar.timeline, ["optional", "formal"])

    async def test_drafting_glance_does_not_queue_behind_busy_sender(self):
        draft = await self.start_slow_draft()
        self.model.result = {"speak": False}
        async with self.program.send_lock:
            self.program.ambient.tick(self.wb.state())
        self.program.ambient.tick(self.wb.state())
        await REAL_SLEEP(0)
        self.assertEqual(self.avatar.submitted, [])
        self.model.draft_release.set()
        await draft
        self.assertEqual(self.avatar.timeline, ["formal"])

    async def test_drafting_does_not_make_up_glance_after_old_speech_finishes(self):
        self.avatar.optional_status = "sending"
        self.avatar.delivery_started = True
        old_speech = await self.opportunity()
        await self.spin_until(lambda: len(self.avatar.submitted) == 1)
        draft = await self.start_slow_draft()
        self.model.result = {"speak": False}
        self.program.ambient.tick(self.wb.state())
        self.assertTrue(self.program.ambient.thinking_attempted)
        self.avatar.updates.put_nowait({"status": "sent", "started": True, "pending": False})
        await old_speech
        self.program.ambient.tick(self.wb.state())
        await REAL_SLEEP(0)
        self.assertEqual(len(self.avatar.submitted), 1)
        self.model.draft_release.set()
        await draft
        self.assertEqual(self.avatar.timeline, ["optional_GE", "formal"])

    async def test_fast_formal_invalidates_not_yet_started_drafting_glance(self):
        draft = await self.start_slow_draft()
        self.program.ambient.tick(self.wb.state())
        motion = self.program.ambient.task
        # The formal coroutine completes the draft before the scheduled motion
        # gets its first event-loop turn; its old phase must not leak a Type9.
        self.wb.game.current_round["phase"] = "WAITING_REPLY"
        await motion
        self.assertEqual(self.avatar.submitted, [])
        # This controlled draft is cancelled in tearDown, without making a
        # fabricated formal result part of the test's game history.


if __name__ == "__main__":
    unittest.main()
