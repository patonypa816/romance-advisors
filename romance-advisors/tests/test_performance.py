import asyncio
import copy
import json
import unittest
from unittest.mock import patch

from romance.game import Game
from test_game import FakeModel


class FakePerformer:
    def __init__(self):
        self.enabled = True
        self.prepared = []
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.fail = False
        self.receipt = {"status": "sent", "send_complete": True,
                        "handoff_wait_elapsed": True, "playback_confirmed": False}

    def prepare(self, round_id, text):
        self.prepared.append((round_id, text))
        return {"job_id": round_id + ":reaction", "owner_id": "private-owner",
                "generation": 7, "status": "pending", "man_released": False}

    async def perform(self, record, text):
        self.calls.append((copy.deepcopy(record), text))
        self.started.set()
        await self.release.wait()
        if self.fail or not self.enabled:
            raise RuntimeError("private-transport-failure")
        return copy.deepcopy(self.receipt)


class PerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.model = FakeModel()
        self.performer = FakePerformer()
        self.game = Game(":memory:", self.model, performer=self.performer)
        await self.game.new_episode()

    async def asyncTearDown(self):
        self.game.close()

    async def finish(self):
        self.game.submit_proposal("advisor", "军师", "解释紧张")
        state = await self.game.close_collection()
        self.game.vote("voter", "观众", state["round"]["options"][0]["id"])
        return await self.game.close_voting()

    def assert_unsettled(self, state):
        self.assertEqual(state["episode"]["score"], 50)
        self.assertEqual(state["leaderboards"]["best"], [])
        self.assertEqual([m["role"] for m in state["messages"]], ["woman"])
        self.assertFalse(any(task == "reply" for task, _ in self.model.calls))

    async def test_wait_hides_committed_message_and_source_then_releases(self):
        self.performer.release.clear()
        task = asyncio.create_task(self.finish())
        await self.performer.started.wait()
        state = self.game.public_state()
        self.assert_unsettled(state)
        self.assertEqual(state["round"]["phase"], "WAITING_REPLY")
        for key in ("reaction", "selected_option_id", "selected_author", "selection_source"):
            self.assertIsNone(state["round"][key])
        self.assertEqual(len(self.game.data["messages"]), 2)
        self.assertEqual(self.game.operator_state()["diagnostics"]["round"]["reaction"], "那我认真解释。")
        self.assertEqual(self.game.operator_state()["diagnostics"]["messages"][-1]["text"], "昨晚有点紧张。")
        self.assertEqual(state["round"]["performance"], {
            "status": "pending", "man_released": False, "playback_confirmed": False})
        self.assertNotIn("private-owner", json.dumps(state))
        self.performer.release.set()
        result = await task
        self.assertEqual(result["round"]["phase"], "DONE")
        self.assertEqual(result["round"]["reaction"], "那我认真解释。")
        self.assertEqual([m["role"] for m in result["messages"]], ["woman", "man", "woman"])
        self.assertTrue(result["round"]["performance"]["man_released"])
        self.assertFalse(result["round"]["performance"]["playback_confirmed"])
        self.assertEqual(len(self.performer.prepared), 1)
        self.assertEqual(self.performer.calls[0][1], "那我认真解释。")
        self.assertNotIn("昨晚有点紧张。", json.dumps(self.performer.calls))

    async def test_woman_retry_keeps_message_and_never_replays_sent_reaction(self):
        self.model.fail_task = "reply"
        state = await self.finish()
        original_id = state["messages"][-1]["id"]
        self.assertEqual(state["episode"]["score"], 50)
        self.assertEqual(state["round"]["phase"], "WAITING_REPLY")
        self.assertTrue(state["round"]["performance"]["man_released"])
        self.model.fail_task = None
        self.performer.enabled = False
        state = await self.game.retry()
        self.assertEqual(state["round"]["phase"], "DONE")
        self.assertEqual(state["messages"][1]["id"], original_id)
        self.assertEqual(state["episode"]["score"], 54)
        self.assertEqual(len(self.performer.calls), 1)
        self.assertEqual(len(self.performer.prepared), 1)

    def use_typing_performer(self, *, fail_after_s=False):
        prepare = self.performer.prepare
        self.performer.prepare = lambda round_id, text: {
            **prepare(round_id, text), "message_motion_plan": {"emotion": "专注"}}
        before_s, allow_s = asyncio.Event(), asyncio.Event()
        after_s, allow_tail = asyncio.Event(), asyncio.Event()

        async def perform(record, text, *, on_message_action):
            before_s.set()
            await allow_s.wait()
            on_message_action()
            # Repeated polls for the same S must leave one message in storage.
            on_message_action()
            after_s.set()
            await allow_tail.wait()
            if fail_after_s:
                raise RuntimeError("typing tail lost")
            motion = {**self.performer.receipt, "job_id": record["job_id"] + ":typing",
                      "generation": record["generation"], "short_action_sent": True}
            return {**self.performer.receipt, "message_motion_receipt": motion}

        self.performer.perform = perform
        return before_s, allow_s, after_s, allow_tail

    async def test_message_released_on_s_while_reply_waits_for_typing_tail(self):
        before_s, allow_s, after_s, allow_tail = self.use_typing_performer()
        task = asyncio.create_task(self.finish())
        await asyncio.wait_for(before_s.wait(), 1)
        self.assert_unsettled(self.game.public_state())
        allow_s.set()
        await asyncio.wait_for(after_s.wait(), 1)
        self.assertEqual([m["role"] for m in self.game.public_state()["messages"]], ["woman", "man"])
        self.assertTrue(self.game.storage.load()["rounds"][-1]["performance"]["man_released"])
        self.assertFalse(any(name == "reply" for name, _ in self.model.calls))
        allow_tail.set()
        final = await task
        self.assertEqual([m["role"] for m in final["messages"]], ["woman", "man", "woman"])
        self.assertEqual(final["round"]["phase"], "DONE")

    async def test_failed_typing_tail_preserves_public_message_without_settling(self):
        before_s, allow_s, _, allow_tail = self.use_typing_performer(fail_after_s=True)
        task = asyncio.create_task(self.finish())
        await asyncio.wait_for(before_s.wait(), 1)
        allow_s.set()
        allow_tail.set()
        state = await task
        self.assertIsNotNone(state["error"])
        self.assertEqual([m["role"] for m in state["messages"]], ["woman", "man"])
        self.assertEqual(state["episode"]["score"], 50)
        self.assertFalse(any(name == "reply" for name, _ in self.model.calls))

    async def test_late_typing_s_after_abort_cannot_publish_the_hidden_message(self):
        before_s, allow_s, _, allow_tail = self.use_typing_performer()
        task = asyncio.create_task(self.finish())
        await asyncio.wait_for(before_s.wait(), 1)
        self.game.abort_episode()
        allow_s.set()
        allow_tail.set()
        state = await task
        self.assertEqual(state["round"]["phase"], "ABORTED")
        self.assert_unsettled(state)

    async def test_abort_during_minimum_woman_typing_cannot_commit_finale(self):
        self.model.delta = 50
        generated = asyncio.Event()
        original = self.model.generate

        async def generate(task, payload):
            result = await original(task, payload)
            if task == "reply":
                generated.set()
            return result

        self.model.generate = generate
        with patch("romance.game.WOMAN_TYPING_SECONDS", .1):
            task = asyncio.create_task(self.finish())
            try:
                await asyncio.wait_for(generated.wait(), 1)
                pending = self.game.public_state()
                self.assertTrue(pending["round"]["woman_typing"])
                self.assertEqual(pending["episode"]["score"], 50)
                self.assertIsNone(pending["finale"])
                self.game.abort_episode()
                state = await asyncio.wait_for(task, 1)
                self.assertEqual(state["episode"]["status"], "TECH_ENDED")
                self.assertEqual(state["episode"]["score"], 50)
                self.assertIsNone(state["finale"])
                self.assertFalse(state["round"]["woman_typing"])
                self.assertEqual(len(state["messages"]), 2)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_stop_rejects_late_receipt_without_releasing_or_replying(self):
        self.performer.release.clear()
        task = asyncio.create_task(self.finish())
        await self.performer.started.wait()
        self.game.abort_episode()
        self.performer.release.set()
        await task
        state = self.game.public_state()
        self.assertEqual(state["round"]["phase"], "ABORTED")
        self.assertEqual(state["episode"]["status"], "TECH_ENDED")
        self.assert_unsettled(state)
        self.assertFalse(self.game.current_round["performance"]["man_released"])
        saved = self.game.storage.load()["rounds"][-1]
        self.assertEqual(saved["performance"]["status"], "pending")

    async def test_new_episode_cannot_receive_old_receipt(self):
        self.performer.release.clear()
        task = asyncio.create_task(self.finish())
        await self.performer.started.wait()
        old_round = self.game.current_round
        self.game.abort_episode()
        await self.game.new_episode()
        new_round_id = self.game.current_round["id"]
        self.performer.release.set()
        await task
        state = self.game.public_state()
        self.assertEqual(state["round"]["id"], new_round_id)
        self.assertEqual(state["round"]["phase"], "COLLECTING")
        self.assertEqual(len(state["messages"]), 1)
        self.assertFalse(old_round["performance"]["man_released"])
        self.assertFalse(any(name == "reply" for name, _ in self.model.calls))

    async def test_transport_failure_does_not_settle_and_retry_uses_same_record(self):
        ssml = '<speak>那我<break time="180ms"/>认真解释。</speak>'
        generate = self.model.generate

        async def with_ssml(task, payload):
            result = await generate(task, payload)
            return dict(result, ssml=ssml) if task == "draft" else result

        self.model.generate = with_ssml
        self.performer.fail = True
        state = await self.finish()
        self.assert_unsettled(state)
        self.assertIsNotNone(state["error"])
        self.assertNotIn("private-transport-failure", json.dumps(state))
        original = copy.deepcopy(self.game.current_round["performance"])
        self.assertEqual(original["plan"]["segments"][0]["ssml"], ssml)
        self.performer.fail = False
        state = await self.game.retry()
        self.assertEqual(state["round"]["phase"], "DONE")
        self.assertEqual(self.performer.calls[1][0], original)
        self.assertEqual(len(self.performer.prepared), 1)
        self.assertEqual([task for task, _ in self.model.calls].count("draft"), 1)

    async def test_disabling_does_not_bypass_pending_performance(self):
        self.performer.fail = True
        await self.finish()
        self.performer.fail = False
        self.performer.enabled = False
        state = await self.game.retry()
        self.assert_unsettled(state)
        self.assertEqual(len(self.performer.calls), 2)
        self.assertEqual(len(self.performer.prepared), 1)

    async def test_missing_performer_does_not_bypass_pending_performance(self):
        self.performer.fail = True
        await self.finish()
        self.game.performer = None
        state = await self.game.retry()
        self.assert_unsettled(state)
        self.assertEqual(state["error"]["code"], "invalid_performance_unavailable")

    async def test_incomplete_or_wrong_job_receipt_does_not_release(self):
        self.performer.receipt["send_complete"] = False
        state = await self.finish()
        self.assert_unsettled(state)
        self.assertEqual(state["error"]["code"], "invalid_performance_receipt")
        self.performer.receipt.update(send_complete=True, job_id="another-round")
        state = await self.game.retry()
        self.assert_unsettled(state)
        self.assertFalse(self.game.current_round["performance"]["man_released"])

    async def test_text_only_mode_has_no_performance_or_changed_flow(self):
        self.performer.enabled = False
        state = await self.finish()
        self.assertEqual(state["round"]["phase"], "DONE")
        self.assertEqual(len(state["messages"]), 3)
        self.assertNotIn("performance", state["round"])
        self.assertEqual(self.performer.prepared, [])
        self.assertEqual(self.performer.calls, [])

    async def test_reaction_over_120_characters_is_rejected_before_preparing(self):
        original_generate = self.model.generate

        async def generate(task, payload):
            result = await original_generate(task, payload)
            if task == "draft":
                result["reaction"] = "长" * 121
            return result

        self.model.generate = generate
        state = await self.finish()
        self.assert_unsettled(state)
        self.assertEqual(state["error"]["code"], "invalid_reaction")
        self.assertEqual(state["round"]["phase"], "DRAFTING")
        self.assertEqual(self.performer.prepared, [])
        self.assertEqual(len(self.game.data["messages"]), 1)


if __name__ == "__main__":
    unittest.main()
