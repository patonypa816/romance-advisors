"""Selection receipts use scripted models and temporary SQLite databases only."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path

from romance.game import Game
from test_game import FakeModel


class SelectionModel(FakeModel):
    def __init__(self):
        super().__init__()
        self.selected_texts = {"你是谁啊？", "真的假的？那不行"}
        self.assessments = True
        self.assessment_override = None

    async def generate(self, task, payload):
        result = await super().generate(task, payload)
        if task != "select":
            return result
        result["options"] = [option for option in result["options"]
                             if option["summary"] in self.selected_texts]
        if self.assessments:
            result["assessments"] = [{"proposal_id": p["id"],
                "reason": "可直接作为追问或反对的回复。" if p["text"] in self.selected_texts
                          else "对直播间打招呼，没有给女生的回复内容。"}
                for p in payload["proposals"]]
        if self.assessment_override is not None:
            result["assessments"] = self.assessment_override
        return result


class ProposalSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "selection.sqlite"
        self.model = SelectionModel()
        self.game = Game(self.path, self.model)
        await self.game.new_episode()

    async def asyncTearDown(self):
        self.game.close()
        self.temp.cleanup()

    def submit(self, text, author):
        self.game.submit_proposal(author, author, text)
        return self.game.operator_state()["proposals"][-1]["id"]

    async def test_short_replies_reach_selector_and_all_receipts_persist_and_increment(self):
        first = self.submit("你是谁啊？", "a")
        self.submit("真的假的？那不行", "b")
        rejected = self.submit("主播晚上好", "c")
        duplicate = self.submit("你是谁啊？", "d")
        initial = self.game.operator_state(incremental=True)
        self.assertTrue(all(p["selection"]["status"] == "pending" for p in initial["proposals"]))
        state = await self.game.close_collection()
        self.assertEqual(state["round"]["phase"], "VOTING")
        self.assertEqual(len(state["round"]["options"]), 2)
        sent = next(payload for task, payload in self.model.calls if task == "select")
        self.assertEqual([p["text"] for p in sent["proposals"]],
                         ["你是谁啊？", "真的假的？那不行", "主播晚上好"])
        self.assertNotIn("WOMAN_PRIVATE", str(sent))
        self.assertNotIn("proposal_selection", state["round"])
        self.assertNotIn("对直播间打招呼，没有给女生的回复内容。", str(state))
        updated = self.game.operator_state(incremental=True, session_id=initial["session_id"],
            comment_cursor=initial["comment_cursor"], proposal_version=initial["proposal_version"],
            vote_version=initial["vote_version"])
        self.assertNotEqual(updated["proposal_version"], initial["proposal_version"])
        self.assertFalse(updated["proposal_reset"])
        self.assertEqual(len(updated["proposals"]), 4)
        receipts = {p["id"]: p["selection"] for p in updated["proposals"]}
        self.assertEqual(receipts[first]["status"], "selected")
        self.assertIn(receipts[first]["option_id"], [o["id"] for o in state["round"]["options"]])
        self.assertEqual(receipts[rejected], {"status": "not_selected", "reason_source": "model",
            "reason": "对直播间打招呼，没有给女生的回复内容。"})
        self.assertEqual(receipts[duplicate]["status"], "duplicate")
        self.assertEqual(receipts[duplicate]["duplicate_of"], first)
        self.game.close()
        self.game = Game(self.path, self.model)
        self.assertEqual({p["id"]: p["selection"] for p in self.game.operator_state()["proposals"]}, receipts)

    async def test_missing_reasons_never_invent_rejection_or_block_empty_selection(self):
        self.model.assessments = False
        self.submit("主播晚上好", "a")
        state = await self.game.close_collection()
        self.assertIsNone(state["error"])
        self.assertEqual(state["round"]["phase"], "DONE")
        receipt = self.game.operator_state()["proposals"][0]["selection"]
        self.assertEqual(receipt, {"status": "not_selected", "reason_source": "unrecorded",
            "reason": "模型未返回未入选原因。"})

    async def test_invalid_ambiguous_or_foreign_reasons_do_not_change_options(self):
        selected = self.submit("你是谁啊？", "a")
        rejected = self.submit("主播晚上好", "b")
        self.model.assessment_override = [None, {"proposal_id": ["bad"], "reason": "bad"},
            {"proposal_id": selected, "reason": "一" * 241},
            {"proposal_id": rejected, "reason": "一个理由"},
            {"proposal_id": rejected, "reason": "另一个理由"},
            {"proposal_id": "not-in-input", "reason": "不能挂到有效候选"}]
        state = await self.game.close_collection()
        self.assertIsNone(state["error"])
        self.assertEqual(len(state["round"]["options"]), 1)
        rows = self.game.operator_state()["proposals"]
        self.assertEqual(rows[0]["selection"], {"status": "selected", "reason_source": "system",
            "reason": "已入选投票选项。", "option_id": state["round"]["options"][0]["id"]})
        self.assertEqual(rows[1]["selection"]["reason_source"], "unrecorded")

    async def test_valid_assessment_cannot_rescue_forged_option_source(self):
        self.submit("你是谁啊？", "a")
        original_generate = self.model.generate

        async def forged_generate(task, payload):
            output = await original_generate(task, payload)
            if task == "select":
                output["options"][0]["proposal_id"] = "not-in-input"
            return output

        self.model.generate = forged_generate
        state = await self.game.close_collection()
        self.assertEqual(state["error"]["code"], "invalid_option_source")
        self.assertEqual(state["round"]["phase"], "SELECTING")
        self.assertNotIn("proposal_selection", self.game.current_round)
        self.assertEqual(self.game.operator_state()["proposals"][0]["selection"]["status"], "pending")

    async def test_suggestion_survives_same_author_later_chat_and_keeps_reward_source(self):
        def event(event_id, text, nickname):
            return {"event_id": event_id, "author_id": "a", "nickname": nickname,
                    "text": text, "expected_round_id": self.game.data["round_id"],
                    "expected_phase": "COLLECTING"}

        before = len(self.model.calls)
        first = self.game.accept_comment(event("suggestion", "你是谁啊？", "原建议作者"))
        later = self.game.accept_comment(event("chat", "主播晚上好", "后发闲聊昵称"))
        self.assertEqual(len(self.model.calls), before)
        self.assertNotEqual(first["proposal_id"], later["proposal_id"])
        state = await self.game.close_collection()
        sent = next(payload for task, payload in self.model.calls if task == "select")
        self.assertEqual([p["text"] for p in sent["proposals"]], ["你是谁啊？", "主播晚上好"])
        self.assertEqual(len(state["round"]["options"]), 1)
        chosen = self.game.current_round["options"][0]
        self.assertEqual(chosen["proposal_id"], first["proposal_id"])
        self.assertEqual(chosen["author"], {"id": "a", "nickname": "原建议作者"})
        self.game.vote("v", "观众", chosen["id"])
        state = await self.game.close_voting()
        self.assertEqual(state["round"]["selected_author"], chosen["author"])
        self.assertEqual(state["leaderboards"]["best"][0]["author_id"], "a")
        self.assertEqual(state["leaderboards"]["best"][0]["adopted"], 1)
        self.assertEqual([task for task, _ in self.model.calls].count("select"), 1)
        self.game.close()
        self.game = Game(self.path, self.model)
        self.assertEqual(self.game.current_round["options"][0]["proposal_id"], first["proposal_id"])
        self.assertEqual(len(self.game.operator_state()["proposals"]), 2)

    async def test_multiple_real_suggestions_are_seen_before_one_option_per_author(self):
        first = self.submit("你是谁啊？", "a")
        second = self.submit("真的假的？那不行", "a")
        other = self.submit("先解释昨晚紧张", "b")
        self.model.selected_texts.add("先解释昨晚紧张")
        state = await self.game.close_collection()
        sent = next(payload for task, payload in self.model.calls if task == "select")
        self.assertEqual([p["id"] for p in sent["proposals"]], [first, second, other])
        options = self.game.current_round["options"]
        self.assertEqual([option["proposal_id"] for option in options], [first, other])
        self.assertEqual([option["number"] for option in options], [1, 2])
        rows = {p["id"]: p["selection"] for p in self.game.operator_state()["proposals"]}
        self.assertEqual(rows[second]["status"], "not_selected")
        self.assertEqual(rows[second]["reason_source"], "system")
        self.assertIn("最多一个", rows[second]["reason"])
        self.assertEqual(rows[other]["status"], "selected")
        self.game.vote("v", "观众", options[1]["id"])
        state = await self.game.close_voting()
        self.assertEqual(state["round"]["selected_author"]["id"], "b")
        self.assertEqual(state["leaderboards"]["best"][0]["author_id"], "b")
        self.assertEqual([task for task, _ in self.model.calls].count("select"), 1)

    async def test_retried_comment_is_idempotent_but_new_duplicate_keeps_its_receipt(self):
        event = {"event_id": "original", "author_id": "a", "nickname": "首发",
                 "text": "你是谁啊？", "expected_round_id": self.game.data["round_id"],
                 "expected_phase": "COLLECTING"}
        first = self.game.accept_comment(event)
        self.assertEqual(self.game.accept_comment(event), first)
        self.assertEqual(len(self.game.operator_state()["proposals"]), 1)
        repeated = self.game.accept_comment(dict(event, event_id="new-event", nickname="再发"))
        self.assertNotEqual(first["proposal_id"], repeated["proposal_id"])
        await self.game.close_collection()
        rows = {p["id"]: p["selection"] for p in self.game.operator_state()["proposals"]}
        self.assertEqual(rows[repeated["proposal_id"]]["status"], "duplicate")
        self.assertEqual(rows[repeated["proposal_id"]]["duplicate_of"], first["proposal_id"])
        self.assertEqual(self.game.current_round["options"][0]["proposal_id"], first["proposal_id"])
        sent = next(payload for task, payload in self.model.calls if task == "select")
        self.assertEqual(len(sent["proposals"]), 1)

    async def test_risky_adult_invitation_and_personality_conflict_are_not_code_filters(self):
        self.model.selected_texts = {"叫她出门去酒店", "带她开房间！"}
        self.model.conflict = True
        first = self.submit("叫她出门去酒店", "a")
        second = self.submit("带她开房间！", "b")
        state = await self.game.close_collection()
        self.assertEqual(state["round"]["phase"], "VOTING")
        self.assertEqual([option["proposal_id"] for option in self.game.current_round["options"]], [first, second])
        self.assertTrue(all(option["conflict"] for option in self.game.current_round["options"]))
        self.assertEqual([task for task, _ in self.model.calls].count("select"), 1)

    async def test_budget_exclusion_distinguished_from_model_rejection(self):
        self.game.input_char_budget = len("你是谁啊？")
        self.submit("你是谁啊？", "a")
        self.submit("这条超过剩余预算", "b")
        self.submit("短", "c")
        self.submit("你是谁啊？", "d")
        await self.game.close_collection()
        sent = next(payload for task, payload in self.model.calls if task == "select")
        self.assertEqual([p["text"] for p in sent["proposals"]], ["你是谁啊？"])
        receipts = [p["selection"] for p in self.game.operator_state()["proposals"]]
        self.assertEqual([r["status"] for r in receipts],
                         ["selected", "not_submitted", "not_submitted", "duplicate"])
        self.assertEqual([r["reason_source"] for r in receipts], ["model", "system", "system", "system"])

    async def test_missing_nonstring_and_duplicate_invalid_first_reasons_degrade(self):
        variants = (None, "bad-shape", [None], "missing", "nonstring", "duplicate-invalid-first")
        for variant in variants:
            with self.subTest(variant=variant):
                if self.game.current_round["phase"] != "COLLECTING":
                    await self.game.new_episode()
                proposal_id = self.submit("主播晚上好", "a")
                if variant == "missing":
                    assessments = [{"proposal_id": proposal_id}]
                elif variant == "nonstring":
                    assessments = [{"proposal_id": proposal_id, "reason": ["invalid"]}]
                elif variant == "duplicate-invalid-first":
                    assessments = [{"proposal_id": proposal_id, "reason": None},
                                   {"proposal_id": proposal_id, "reason": "重复条目不能擅自选用"}]
                else:
                    assessments = variant
                self.model.assessments = variant is not None
                self.model.assessment_override = assessments
                state = await self.game.close_collection()
                self.assertIsNone(state["error"])
                self.assertEqual(state["round"]["phase"], "DONE")
                self.assertEqual(self.game.operator_state()["proposals"][0]["selection"]["reason_source"], "unrecorded")

    async def test_history_without_receipts_is_unrecorded_and_not_mutated(self):
        self.submit("你是谁啊？", "a")
        await self.game.close_collection()
        self.game.current_round.pop("proposal_selection")
        self.game._save()
        before = copy.deepcopy(self.game.data)
        receipt = self.game.operator_state()["proposals"][0]["selection"]
        self.assertEqual(receipt, {"status": "unrecorded", "reason_source": "unrecorded",
            "reason": "本轮筛选原因未记录。"})
        self.assertEqual(self.game.data, before)

    async def test_abort_during_collection_updates_incremental_and_full_receipts(self):
        self.submit("你是谁啊？", "a")
        initial = self.game.operator_state(incremental=True)
        self.assertEqual(initial["proposals"][0]["selection"]["status"], "pending")
        self.game.abort_episode()
        updated = self.game.operator_state(incremental=True, session_id=initial["session_id"],
            comment_cursor=initial["comment_cursor"], proposal_version=initial["proposal_version"],
            vote_version=initial["vote_version"])
        self.assertNotEqual(updated["proposal_version"], initial["proposal_version"])
        self.assertFalse(updated["proposal_reset"])
        self.assertEqual(updated["proposals"], self.game.operator_state()["proposals"])
        self.assertEqual(updated["proposals"][0]["selection"], {"status": "unrecorded",
            "reason": "本轮已终止，未完成编选。", "reason_source": "system"})
        self.game.close()
        self.game = Game(self.path, self.model)
        self.assertEqual(self.game.operator_state()["proposals"], updated["proposals"])

    async def test_abort_preserves_already_committed_selection(self):
        self.submit("你是谁啊？", "a")
        await self.game.close_collection()
        initial = self.game.operator_state(incremental=True)
        self.game.abort_episode()
        updated = self.game.operator_state(incremental=True, session_id=initial["session_id"],
            comment_cursor=initial["comment_cursor"], proposal_version=initial["proposal_version"],
            vote_version=initial["vote_version"])
        self.assertNotEqual(updated["proposal_version"], initial["proposal_version"])
        self.assertEqual(updated["proposals"], initial["proposals"])
        self.assertEqual(updated["proposals"][0]["selection"]["status"], "selected")

    async def test_abort_legacy_voting_does_not_claim_selection_was_unfinished(self):
        self.submit("你是谁啊？", "a")
        await self.game.close_collection()
        self.game.current_round.pop("proposal_selection")
        self.game._save()
        self.game.abort_episode()
        self.assertEqual(self.game.operator_state()["proposals"][0]["selection"],
            {"status": "unrecorded", "reason": "本轮筛选原因未记录。", "reason_source": "unrecorded"})

    async def test_abort_during_selection_keeps_terminal_receipt_after_late_result(self):
        self.submit("你是谁啊？", "a")
        self.model.wait_task = "select"
        operation = asyncio.create_task(self.game.close_collection())
        await self.model.started.wait()
        try:
            self.game.abort_episode()
            aborted = self.game.operator_state()["proposals"]
            self.assertEqual(aborted[0]["selection"]["reason"], "本轮已终止，未完成编选。")
        finally:
            self.model.release.set()
            await operation
        self.assertEqual(self.game.current_round["phase"], "ABORTED")
        self.assertEqual(self.game.operator_state()["proposals"], aborted)
        self.assertEqual(self.game.current_round["options"], [])

    async def test_late_comment_during_selection_remains_record_only(self):
        self.submit("你是谁啊？", "a")
        expected_round = self.game.data["round_id"]
        self.model.wait_task = "select"
        operation = asyncio.create_task(self.game.close_collection())
        await self.model.started.wait()
        try:
            receipt = self.game.accept_comment({"event_id": "late", "author_id": "b", "nickname": "迟到",
                "text": "真的假的？那不行", "expected_round_id": expected_round,
                "expected_phase": "COLLECTING"})
            self.assertEqual(receipt["result"], "stale")
        finally:
            self.model.release.set()
            await operation
        self.assertEqual(len(self.game.operator_state()["proposals"]), 1)
        self.assertEqual(len(self.game.current_round["proposal_selection"]), 1)


if __name__ == "__main__":
    unittest.main()
