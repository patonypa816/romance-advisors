import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from romance.game import Game


class FakeModel:
    def __init__(self):
        self.calls = []
        self.fail_task = None
        self.conflict = False
        self.delta = 4
        self.pick = None
        self.force_pick = False
        self.empty_draft = False
        self.zero_options = False
        self.invalid_ending = False
        self.summary_override = None
        self.wait_task = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.expression = {"emotion": "犹豫后认真", "actions": [], "recovery": None}

    async def generate(self, task, payload):
        self.calls.append((task, copy.deepcopy(payload)))
        if task == self.wait_task:
            self.started.set()
            await self.release.wait()
        if task == self.fail_task:
            raise RuntimeError("SECRET-provider-error")
        if task == "prepare":
            base = {"name": "阿澈", "age": 25, "personality": "害羞", "background": "朋友", "speech_style": "简短"}
            return {"title": "草稿箱", "man": base, "woman": dict(base, name="小雨", private_preferences="WOMAN_PRIVATE"), "shared_background": "聚会认识", "public_hints": ["都是成年人"], "opening": "昨晚怎么没说完？"}
        if task == "select":
            return {"options": [] if self.zero_options else [{"proposal_id": p["id"], "summary": self.summary_override or p["text"], "conflict": self.conflict, "reason": "与内向习惯冲突" if self.conflict else "适合"} for p in payload["proposals"][:4]]}
        if task == "draft":
            selected = payload["selected_option_id"] if payload["selection_locked"] else (self.pick if self.pick is not None else (payload["allowed_option_ids"][0] if payload["allowed_option_ids"] else None))
            if self.force_pick:
                selected = self.pick
            return {"selected_option_id": selected, "reaction": "那我认真解释。", "reaction_instruction": "有点犹豫，随后诚恳地说。", "reaction_expression": copy.deepcopy(self.expression), "message": "" if self.empty_draft else "昨晚有点紧张。"}
        if task == "reply":
            after = max(0, min(100, payload["score"] + self.delta))
            return {"message": "明白啦。", "delta": self.delta, "ending": "bad" if self.invalid_ending else "success" if after == 100 else "failure" if after == 0 else "continue", "reason": "合心意"}
        raise AssertionError(task)


class FixedRandom:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def random(self):
        self.calls += 1
        return self.value

    def choice(self, items):
        return items[0]


class GameTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "game.sqlite3"
        self.model = FakeModel()
        self.game = Game(self.path, self.model)
        await self.game.new_episode()

    async def asyncTearDown(self):
        self.game.close()
        self.tmp.cleanup()

    async def options(self):
        self.game.submit_proposal("a", "同名", "解释紧张")
        self.game.submit_proposal("b", "同名", "开个玩笑")
        state = await self.game.close_collection()
        return state["round"]["options"]

    async def finish(self, delta=4):
        self.model.delta = delta
        options = await self.options()
        self.game.vote("v", "投票者", options[0]["id"])
        return await self.game.close_voting()

    async def test_prepare_preserves_structured_private_preferences_and_rejects_blank_text(self):
        original = self.model.generate
        for preferences in (["喜欢认真兑现约定"], {"likes": "认真兑现约定"}, "  喜欢认真兑现约定  "):
            with self.subTest(preferences=preferences):
                async def generate(task, payload):
                    result = await original(task, payload)
                    result["woman"]["private_preferences"] = preferences
                    return result

                before = len(self.model.calls)
                with patch.object(self.model, "generate", generate):
                    state = await self.game.new_episode()
                self.assertEqual(len(self.model.calls), before + 1)
                self.assertIsNone(state["error"])
                self.assertEqual(state["round"]["phase"], "COLLECTING")
                expected = preferences.strip() if isinstance(preferences, str) else preferences
                self.assertEqual(self.game.episode["woman"]["private_preferences"], expected)
                self.assertNotIn("private_preferences", json.dumps(state))

        async def blank(task, payload):
            result = await original(task, payload)
            result["woman"]["private_preferences"] = " \n\t "
            return result

        with patch.object(self.model, "generate", blank):
            state = await self.game.new_episode()
        self.assertEqual(state["error"]["code"], "invalid_private_preferences")
        self.assertEqual(state["messages"], [])
        self.assertIsNotNone(self.game._prepare_candidate)

    async def test_fast_reply_stays_private_while_typing_then_commits_with_score(self):
        # A short real display interval lets us observe the normal public API
        # after the synthetic model returned but before either result is public.
        with patch("romance.game.WOMAN_TYPING_SECONDS", .12):
            started = time.monotonic()
            task = asyncio.create_task(self.finish())
            await asyncio.sleep(.025)
            self.assertTrue(any(name == "reply" for name, _ in self.model.calls))
            state = self.game.public_state()
            self.assertTrue(state["round"]["woman_typing"])
            self.assertEqual([m["role"] for m in state["messages"]], ["woman", "man"])
            self.assertEqual(state["episode"]["score"], 50)
            final = await task
            self.assertGreaterEqual(time.monotonic() - started, .12)
            self.assertFalse(final["round"]["woman_typing"])
            self.assertEqual([m["role"] for m in final["messages"]], ["woman", "man", "woman"])
            self.assertEqual(final["episode"]["score"], 54)

    async def test_abort_during_typing_discards_prepared_reply(self):
        with patch("romance.game.WOMAN_TYPING_SECONDS", .12):
            task = asyncio.create_task(self.finish())
            await asyncio.sleep(.025)
            self.assertTrue(self.game.public_state()["round"]["woman_typing"])
            self.game.abort_episode()
            final = await task
        self.assertFalse(final["round"]["woman_typing"])
        self.assertEqual([m["role"] for m in final["messages"]], ["woman", "man"])
        self.assertEqual(final["episode"]["score"], 50)

    async def test_count_change_invalid_vote_and_after_close(self):
        options = await self.options()
        for _ in range(60):
            self.game.vote("v", "投票者", options[0]["id"])
        self.game.vote("v", "新昵称", options[1]["id"])
        with self.assertRaises(ValueError):
            self.game.vote("v", "投票者", "bad")
        self.assertEqual([o["votes"] for o in self.game.public_state()["round"]["options"]], [0, 1])
        state = await self.game.close_voting()
        self.assertEqual(state["round"]["selected_author"]["id"], "b")
        with self.assertRaises(ValueError):
            self.game.vote("late", "迟到", options[0]["id"])

    async def test_highest_no_conflict_fixed_and_hidden(self):
        state = await self.finish()
        self.assertEqual(state["round"]["phase"], "DONE")
        self.assertEqual(len(state["messages"]), 3)
        self.assertEqual(state["episode"]["score"], 54)
        self.assertNotIn("WOMAN_PRIVATE", json.dumps(state))
        self.assertNotIn("dice", json.dumps(state))
        for task, payload in self.model.calls:
            if task in ("select", "draft"):
                self.assertNotIn("WOMAN_PRIVATE", json.dumps(payload))
                self.assertEqual(payload["partner"], {"name": "小雨", "age": 25})
            if task == "reply":
                self.assertNotIn("options", payload)
                self.assertNotIn("man", payload)
                self.assertEqual(payload["partner"], {"name": "阿澈", "age": 25})
                self.assertEqual(set(payload["partner"]), {"name", "age"})
        calls = len(self.model.calls)
        await self.game.close_voting()
        await self.game.retry()
        self.assertEqual(len(self.model.calls), calls)

    async def test_conflict_alternative_b_attribution(self):
        self.model.conflict = True
        rng = FixedRandom(.1)
        self.game.rng = rng
        options = await self.options()
        self.game.vote("v", "投票者", options[0]["id"])
        self.model.pick = options[1]["id"]
        state = await self.game.close_voting()
        self.assertEqual(state["round"]["selected_author"]["id"], "b")
        self.assertEqual(state["leaderboards"]["best"][0]["author_id"], "b")
        self.assertEqual(rng.calls, 1)
        payload = next(p for t, p in self.model.calls if t == "draft")
        self.assertNotIn(options[0]["id"], payload["allowed_option_ids"])

    async def test_seventy_five_percent_must_execute_highest(self):
        self.model.conflict = True
        self.game.rng = FixedRandom(.25)
        options = await self.options()
        self.game.vote("v", "投票者", options[0]["id"])
        self.model.pick = options[1]["id"]
        state = await self.game.close_voting()
        self.assertEqual(state["round"]["selected_option_id"], options[0]["id"])
        self.assertEqual(state["round"]["selection_source"], "audience")

    async def test_legal_selection_survives_empty_draft_retry(self):
        self.model.conflict = True
        rng = FixedRandom(.1)
        self.game.rng = rng
        options = await self.options()
        self.game.vote("v", "投票者", options[0]["id"])
        self.model.pick = options[1]["id"]
        self.model.empty_draft = True
        failed = await self.game.close_voting()
        self.assertEqual(failed["round"]["phase"], "DRAFTING")
        self.assertIsNone(failed["round"]["reaction"])
        self.assertEqual(len(failed["messages"]), 1)
        self.model.empty_draft = False
        state = await self.game.retry()
        self.assertEqual(state["round"]["selected_author"]["id"], "b")
        self.assertEqual(rng.calls, 1)
        payload = [p for t, p in self.model.calls if t == "draft"][-1]
        self.assertEqual(payload["validation_error"], "invalid_man_message")
        self.assertEqual(payload["allowed_option_ids"], [options[1]["id"]])

    async def test_woman_failure_recovery_does_not_duplicate_man_or_score(self):
        self.model.fail_task = "reply"
        state = await self.finish()
        self.assertEqual(state["episode"]["score"], 50)
        self.assertEqual(len(state["messages"]), 2)
        self.assertEqual(state["leaderboards"]["best"], [])
        self.assertNotIn("SECRET", json.dumps(state))
        original_id = state["messages"][-1]["id"]
        self.game.close()
        self.game = Game(self.path, self.model)
        self.model.fail_task = None
        state = await self.game.retry()
        self.assertEqual(state["messages"][1]["id"], original_id)
        self.assertEqual(state["episode"]["score"], 54)
        self.assertEqual(len(state["messages"]), 3)
        self.assertEqual(state["leaderboards"]["best"][0]["adopted"], 1)

    async def test_missing_draft_message_keeps_safe_diagnostic_and_locked_vote(self):
        options = await self.options()
        selected = options[0]["id"]
        self.game.vote("v", "投票者", selected)
        generate = self.model.generate

        async def missing_message(task, payload):
            result = await generate(task, payload)
            if task == "draft":
                result.pop("message")
                result["UNTRUSTED_SECRET_FIELD"] = "SECRET_MODEL_RESPONSE"
            return result

        self.model.generate = missing_message
        state = await self.game.close_voting()
        self.assertEqual(state["error"]["code"], "invalid_man_message")
        self.assertEqual(state["round"]["phase"], "DRAFTING")
        self.assertEqual(len(state["messages"]), 1)
        self.assertEqual(state["episode"]["score"], 50)
        diagnostic = self.game.operator_state()["diagnostics"]["round"]["draft_failure"]
        self.assertEqual(diagnostic["fields"]["message"], {"present": False, "type": None})
        self.assertEqual(diagnostic["fields"]["reaction_expression"], {"present": True, "type": "dict"})
        self.assertNotIn("draft_failure", json.dumps(state))
        self.assertNotIn("SECRET", json.dumps(self.game.storage.load()))

        # A restart preserves the frozen choice and diagnosis; a valid retry
        # releases exactly one message and clears only the obsolete diagnosis.
        self.game.close()
        self.game = Game(self.path, self.model)
        self.assertEqual(self.game.current_round["draft_failure"], diagnostic)
        self.model.generate = generate
        state = await self.game.retry()
        self.assertEqual(state["round"]["phase"], "DONE")
        self.assertEqual(state["round"]["selected_option_id"], selected)
        self.assertEqual(state["episode"]["score"], 54)
        self.assertEqual(len(state["messages"]), 3)
        self.assertNotIn("draft_failure", self.game.current_round)
        payload = [p for t, p in self.model.calls if t == "draft"][-1]
        self.assertEqual(payload["allowed_option_ids"], [selected])
        self.assertEqual(payload["validation_error"], "invalid_man_message")

    async def test_zero_options_self_no_credit(self):
        self.model.zero_options = True
        state = await self.game.close_collection()
        self.assertEqual(state["round"]["phase"], "DONE")
        self.assertEqual(state["round"]["selection_source"], "self")
        self.assertEqual(len(state["messages"]), 3)
        self.assertEqual(state["leaderboards"]["best"], [])
        self.assertNotIn("select", [task for task, _ in self.model.calls])

    async def test_nonzero_candidates_still_select_when_budget_excludes_everything(self):
        self.game.input_char_budget = 1
        self.game.submit_proposal("a", "军师", "超过预算的建议")
        await self.game.close_collection()
        calls = [payload for task, payload in self.model.calls if task == "select"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["proposals"], [])
        self.assertEqual(self.game.current_round["input_count"], 1)

    async def test_unrelated_candidate_still_requires_selector(self):
        self.game.submit_proposal("a", "观众", "今天吃什么")
        self.model.zero_options = True
        state = await self.game.close_collection()
        self.assertEqual([task for task, _ in self.model.calls].count("select"), 1)
        self.assertEqual(state["round"]["phase"], "DONE")

    async def test_frozen_snapshot_never_recounts(self):
        await self.finish()
        def forbidden():
            raise AssertionError("frozen votes must not be recounted")
        self.game._counts = forbidden
        self.assertEqual(self.game.public_state()["round"]["phase"], "DONE")

    async def test_bad_expression_keeps_locked_source_without_committing_message(self):
        self.model.expression = None
        state = await self.finish()
        self.assertEqual(state["error"]["code"], "invalid_reaction_expression")
        self.assertEqual(state["round"]["phase"], "DRAFTING")
        self.assertEqual(len(state["messages"]), 1)
        self.assertIsNone(self.game.current_round.get("reaction_plan"))
        selected = self.game.current_round["selected_option_id"]
        self.model.expression = {"emotion": "认真", "actions": [], "recovery": None}
        await self.game.retry()
        self.assertEqual(self.game.current_round["selected_option_id"], selected)
        payload = [p for t, p in self.model.calls if t == "draft"][-1]
        self.assertEqual(payload["validation_error"], "invalid_reaction_expression")
        plan = self.game.current_round["reaction_plan"]
        self.assertEqual(plan["segments"][0]["text"], self.game.current_round["reaction"])
        self.assertEqual(plan["segments"][0]["instruction"], self.game.current_round["reaction_instruction"])
        self.assertEqual(self.game.storage.load()["rounds"][-1]["reaction_plan"], plan)

    async def test_plan_is_bound_to_performance_with_legacy_record_compatibility(self):
        class Performer:
            def prepare(self, round_id, reaction):
                return {"job_id": round_id, "owner_id": "test", "generation": 1,
                        "status": "pending", "man_released": False}
        await self.finish()
        self.game.performer = Performer()
        r = self.game.current_round
        record = self.game._prepare_performance(r["id"], r["reaction"], r["reaction_instruction"], r["reaction_plan"])
        self.assertEqual(record["plan"], r["reaction_plan"])
        record["plan"]["emotion"] = "更改副本"
        self.assertNotEqual(record["plan"], r["reaction_plan"])
        legacy = self.game._prepare_performance(r["id"], r["reaction"])
        self.assertNotIn("plan", legacy)
        self.assertNotIn("instruction", legacy)

    async def test_ssml_freezes_with_reaction_and_survives_storage_restore(self):
        ssml = '<speak rate="0.95">那我<break time="180ms"/>认真解释。</speak>'
        generate = self.model.generate

        async def with_ssml(task, payload):
            result = await generate(task, payload)
            return dict(result, ssml=ssml) if task == "draft" else result

        self.model.generate = with_ssml
        state = await self.finish()
        self.assertEqual(state["round"]["reaction"], "那我认真解释。")
        self.assertEqual(state["messages"][1]["text"], "昨晚有点紧张。")
        frozen = copy.deepcopy(self.game.current_round["reaction_plan"])
        self.assertEqual(frozen["segments"][0]["ssml"], ssml)
        self.assertEqual(frozen["segments"][0]["text"], state["round"]["reaction"])
        self.assertNotIn("ssml", self.game.current_round)
        self.assertEqual([task for task, _ in self.model.calls].count("draft"), 1)
        self.game.close()
        self.game = Game(self.path, self.model)
        self.assertEqual(self.game.current_round["reaction_plan"], frozen)
        self.assertEqual(self.game.public_state()["round"]["reaction"], "那我认真解释。")

    async def test_invalid_ssml_envelope_keeps_source_locked_before_commit(self):
        generate = self.model.generate

        async def with_bad_ssml(task, payload):
            result = await generate(task, payload)
            return dict(result, ssml=["not a string"]) if task == "draft" else result

        self.model.generate = with_bad_ssml
        state = await self.finish()
        self.assertEqual(state["error"]["code"], "invalid_reaction_expression")
        self.assertEqual(len(state["messages"]), 1)
        self.assertTrue(self.game.current_round["selection_locked"])
        self.assertNotIn("reaction_plan", self.game.current_round)
        self.assertEqual(self.game.current_round["draft_failure"]["fields"]["ssml"],
                         {"present": True, "type": "list"})

    async def test_scores_boards_sessions_and_episode_reset(self):
        for delta in (4, 0, -5):
            if self.game.current_round["phase"] == "DONE":
                self.game.start_round()
            state = await self.finish(delta)
        row = state["leaderboards"]["best"][0]
        self.assertEqual((row["adopted"], row["positive"], row["negative"]), (3, 1, 1))
        self.assertAlmostEqual(row["rate"], 1 / 3)
        self.assertAlmostEqual(state["leaderboards"]["worst"][0]["rate"], 1 / 3)
        state = await self.game.new_episode()
        self.assertEqual(state["episode"]["score"], 50)
        self.assertEqual(state["leaderboards"]["best"][0]["adopted"], 3)
        await self.game.new_session()
        self.assertEqual(self.game.public_state()["leaderboards"]["best"], [])

    async def test_endings_clamp_and_invalid_delta(self):
        state = await self.finish(90)
        self.assertEqual(state["episode"]["status"], "SUCCEEDED")
        self.assertEqual(state["round"]["delta"], 50)
        with self.assertRaises(ValueError):
            self.game.start_round()
        await self.game.new_episode()
        state = await self.finish(-70)
        self.assertEqual(state["episode"]["status"], "FAILED")
        await self.game.new_episode()
        state = await self.finish(float("nan"))
        self.assertEqual(state["error"]["code"], "invalid_delta")
        self.assertEqual(state["episode"]["score"], 50)

    async def test_invalid_ending_not_committed(self):
        self.model.invalid_ending = True
        state = await self.finish()
        self.assertEqual(len(state["messages"]), 2)
        self.assertEqual(state["error"]["code"], "invalid_ending_score_consistency")
        self.model.invalid_ending = False
        state = await self.game.retry()
        self.assertEqual(state["episode"]["score"], 54)

    async def test_aborted_inflight_result_cannot_write(self):
        options = await self.options()
        self.game.vote("v", "投票者", options[0]["id"])
        self.model.wait_task = "reply"
        task = asyncio.create_task(self.game.close_voting())
        await self.model.started.wait()
        self.game.abort_episode()
        self.model.release.set()
        await task
        state = self.game.public_state()
        self.assertEqual(state["episode"]["status"], "TECH_ENDED")
        self.assertEqual(state["episode"]["score"], 50)
        self.assertEqual(len(state["messages"]), 2)

    async def test_zero_votes_no_fictional_winner(self):
        await self.options()
        state = await self.game.close_voting()
        self.assertEqual(self.game.current_round["decision"]["branch"], "no_votes")
        self.assertIsNone(self.game.current_round["decision"]["dice"])
        self.assertEqual(state["round"]["phase"], "DONE")

    async def test_model_cannot_override_highest_or_reselect_rejected_highest(self):
        self.model.conflict = True
        rng = FixedRandom(.5)
        self.game.rng = rng
        options = await self.options()
        self.game.vote("v", "投票者", options[0]["id"])
        self.model.force_pick = True
        self.model.pick = options[1]["id"]
        state = await self.game.close_voting()
        self.assertEqual(state["error"]["code"], "invalid_selected_source")
        self.assertEqual(len(state["messages"]), 1)
        self.model.force_pick = False
        await self.game.retry()
        self.assertEqual(rng.calls, 1)
        self.assertEqual(self.game.public_state()["round"]["selected_option_id"], options[0]["id"])
        self.game.start_round()
        self.game.rng = FixedRandom(.1)
        options = await self.options()
        self.game.vote("v", "投票者", options[0]["id"])
        self.model.force_pick = True
        self.model.pick = options[0]["id"]
        state = await self.game.close_voting()
        self.assertEqual(state["error"]["code"], "invalid_selected_source")

    async def test_sqlite_failure_rolls_back_reply_score_and_credit(self):
        self.game.storage.db.execute("""CREATE TRIGGER fail_reply BEFORE INSERT ON messages
            WHEN json_extract(NEW.body, '$.parent_id') IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'test_failure'); END""")
        state = await self.finish()
        self.assertEqual(state["round"]["phase"], "WAITING_REPLY")
        self.assertEqual(state["episode"]["score"], 50)
        self.assertEqual(len(state["messages"]), 2)
        self.assertEqual(state["leaderboards"]["best"], [])
        self.game.storage.db.execute("DROP TRIGGER fail_reply")
        state = await self.game.retry()
        self.assertEqual(state["episode"]["score"], 54)
        self.assertEqual(state["leaderboards"]["best"][0]["adopted"], 1)

    async def test_duplicate_suggestion_representative_and_keyword_text(self):
        self.game.submit_proposal(author_id="a", nickname="旧昵称", text="相同建议")
        self.game.submit_proposal(author_id="b", nickname="同名", text="相同建议")
        self.game.submit_proposal(author_id="a", nickname="新昵称", text="相同建议")
        state = await self.game.close_collection()
        self.assertEqual(len(state["round"]["options"]), 1)
        self.assertEqual(state["round"]["options"][0]["author"], {"id": "a", "nickname": "旧昵称"})
        self.assertEqual(len(self.game.operator_state()["proposals"]), 3)

    async def test_draft_keeps_original_when_summary_reverses_negation(self):
        original = "先别邀约，等她有空再问；这次只解释紧张。"
        self.model.summary_override = "立刻邀约"
        self.game.submit_proposal("a", "军师", original)
        proposal_id = self.game.operator_state()["proposals"][0]["id"]
        state = await self.game.close_collection()
        self.game.vote("v", "投票者", state["round"]["options"][0]["id"])
        await self.game.close_voting()
        payload = next(p for task, p in self.model.calls if task == "draft")
        option = payload["options"][0]
        self.assertEqual(option["original_text"], original)
        self.assertEqual(option["proposal_id"], proposal_id)
        self.assertEqual(option["summary"], "立刻邀约")
        self.assertEqual(self.game.operator_state()["proposals"][0]["text"], original)


if __name__ == "__main__":
    unittest.main()
