"""Offline second-stage contracts; all model replies are synthetic mocks."""
import asyncio
import copy
import json
from pathlib import Path
import unittest
from unittest import mock

try:
    from .cloudbase_ai import CloudBaseAI, ModelError, SEMANTIC_SPEECH_CONTRACT
    from .semantic_speech import SemanticSpeechError, compile_dialogue_semantic_timeline
    from .timed_action_planner import build_timed_action_schedules, prepare_timed_action_request
except ImportError:
    from cloudbase_ai import CloudBaseAI, ModelError, SEMANTIC_SPEECH_CONTRACT
    from semantic_speech import SemanticSpeechError, compile_dialogue_semantic_timeline
    from timed_action_planner import build_timed_action_schedules, prepare_timed_action_request


TEXT = "甲乙丙丁戊己庚辛壬癸子"
ALIGNMENT = {"status": "ok", "timebase": "audio_start", "words": [
    {"text": char, "begin_time": index * 1000, "end_time": index * 1000 + 250}
    for index, char in enumerate(TEXT)]}
PART = {"text": TEXT, "instruction": "自然清晰。", "actions": [
    {"anchor": {"quote": char, "occurrence": 1},
     "motions": [{"part": part, "description": description}],
     "gaze": "注视镜头", "expression": "神情温和"}
    for char, part, description in (("乙", "head", "轻轻点头一次"),
                                    ("丙", "right_arm", "抬起右手"),
                                    ("丁", "both_arms", "双手向两侧展开"))],
    "recovery": {"gaze": "注视镜头", "expression": "神情平静"}}
RESULT = {"schedule_id": "s01"}


class TimedActionPlannerTests(unittest.IsolatedAsyncioTestCase):
    def client(self, result=RESULT):
        client = CloudBaseAI({})
        client._call = mock.AsyncMock(return_value=copy.deepcopy(result))
        return client

    async def test_local_spelling_difference_keeps_shorter_exact_prefix_and_full_schedule(self):
        part = copy.deepcopy(PART)
        part["actions"] = part["actions"][:2]
        # Only starts at 0 and 4 seconds fit two groups. The eight-character
        # phrase at 0 crosses a misspelling; its exact two-character prefix is valid.
        alignment = {"status": "ok", "timebase": "audio_start", "words": [
            {"text": TEXT[:2], "begin_time": 0, "end_time": 100},
            {"text": "错" + TEXT[3:-1], "begin_time": 1000, "end_time": 2000},
            {"text": TEXT[-1], "begin_time": 4000, "end_time": 4100}]}
        before = copy.deepcopy((part, alignment))
        schedules = build_timed_action_schedules(part, alignment, 5)
        self.assertEqual(len(schedules), 1)
        self.assertEqual([g["anchor"]["quote"] for g in schedules[0]["groups"]], [TEXT[:2], TEXT[-1]])
        self.assertEqual([g["s_at_seconds"] for g in schedules[0]["groups"]], [0, 4])
        client = self.client()
        result = await client.plan_timed_actions(part, alignment, 5, {})
        self.assertFalse(result["planning"]["model_called"])
        self.assertEqual(len(result["actions"]), 2)
        self.assertEqual([n["at_seconds"] for n in result["timeline"] if n["role"] == "S"], [0, 4])
        self.assertEqual((part, alignment), before)
        client._call.assert_not_awaited()

    async def test_single_schedule_skips_model_but_still_runs_final_validation(self):
        part = copy.deepcopy(PART)
        part["actions"] = part["actions"][:1]
        alignment = {"status": "ok", "timebase": "audio_start", "words": [
            {"text": TEXT, "begin_time": 0, "end_time": 1000}]}
        client = self.client()
        result = await client.plan_timed_actions(part, alignment, 2, {})
        self.assertEqual(result["planning"]["candidate_count"], 1)
        self.assertFalse(result["planning"]["model_called"])
        self.assertEqual([n["role"] for n in result["timeline"]], ["GE", "S", "Q"])
        client._call.assert_not_awaited()
        module = __import__(CloudBaseAI.__module__, fromlist=["compile_timed_action_result"])
        with mock.patch.object(module, "compile_timed_action_result", side_effect=ValueError("invalid")) as compile_result:
            with self.assertRaises(ModelError) as caught:
                await client.plan_timed_actions(part, alignment, 2, {})
        self.assertEqual(caught.exception.code, "timed_action_plan_invalid")
        compile_result.assert_called_once()
        client._call.assert_not_awaited()

    async def test_actual_timestamps_reach_reply_call_and_only_anchors_change(self):
        client = self.client()
        original = copy.deepcopy(PART)
        alignment = copy.deepcopy(ALIGNMENT)
        alignment.update({"pcm_sha256": "LOCAL_HASH", "pcm_path": "/private/audio.pcm"})
        alignment["words"][0]["private_token"] = "PRIVATE_WORD"
        context = {"character": {"identity": "原创角色", "temperament": "沉稳", "speech": "自然",
                                 "path": "/private/avatar.png"},
                   "credentials": "PRIVATE_CREDENTIAL", "planning": "OLD_SCHEDULE"}
        planned = await client.plan_timed_actions(PART, alignment, 13.0, context, source_text="请依次演示三个动作")
        client._call.assert_awaited_once()
        role, messages = client._call.call_args.args
        self.assertEqual(role, "reply")
        remote = json.loads(messages[1]["content"])
        self.assertEqual(remote["alignment"], ALIGNMENT)
        self.assertEqual(remote["audio_seconds"], 13.0)
        self.assertEqual(remote["actions"], PART["actions"])
        for forbidden in ("LOCAL_HASH", "/private", "PRIVATE_WORD", "PRIVATE_CREDENTIAL", "OLD_SCHEDULE"):
            self.assertNotIn(forbidden, json.dumps(messages))
        self.assertIn("原创角色", messages[0]["content"])
        self.assertEqual(PART, original)
        self.assertEqual(len(planned["actions"]), 3)
        for before, after in zip(original["actions"], planned["actions"]):
            self.assertEqual({k: v for k, v in before.items() if k != "anchor"},
                             {k: v for k, v in after.items() if k != "anchor"})
        selected = next(schedule for schedule in remote["schedules"] if schedule["schedule_id"] == "s01")
        self.assertEqual([node["at_seconds"] for node in planned["timeline"] if node["role"] == "S"],
                         [group["s_at_seconds"] for group in selected["groups"]])
        self.assertEqual(planned["timeline"], compile_dialogue_semantic_timeline(
            planned["actions"], PART["recovery"], ALIGNMENT, 13.0))
        self.assertEqual(planned["planning"], {"phase": "tts_aligned", "model_called": True, "group_count": 3,
                                               "schedule_id": "s01", "candidate_count": len(remote["schedules"])})
        planned["actions"][0]["motions"][0]["description"] = "修改返回副本"
        self.assertEqual(PART, original)

    async def test_empty_actions_do_not_require_alignment_or_call_model(self):
        for recovery in (None, PART["recovery"]):
            part = {**PART, "actions": [], "recovery": recovery}
            client = self.client()
            result = await client.plan_timed_actions(part, None, 1, {})
            self.assertEqual(result["actions"], [])
            self.assertEqual(result["timeline"], [])
            self.assertFalse(result["planning"]["model_called"])
            client._call.assert_not_awaited()

    async def test_legal_long_character_library_context_is_accepted(self):
        character = {"identity": "名" * 80 + "的AI虚构演绎；不是本人或真实历史记录",
                     "temperament": "稳" * 12000, "speech": "自然" * 2000}
        client = self.client()
        result = await client.plan_timed_actions(PART, ALIGNMENT, 13.0, {"character": character})
        self.assertEqual(len(result["actions"]), 3)
        system = client._call.call_args.args[1][0]["content"]
        trusted = json.loads(system.split("\n可信人物语气：\n", 1)[1])
        self.assertEqual(trusted, character)
        client._call.assert_awaited_once()

    async def test_missing_or_unverified_alignment_rejected_before_model(self):
        variants = [None, {**ALIGNMENT, "status": "unavailable"},
                    {**ALIGNMENT, "words": []},
                    {**ALIGNMENT, "timebase": "sentence_start"}]
        for alignment in variants:
            with self.subTest(alignment=alignment):
                client = self.client()
                with self.assertRaises(SemanticSpeechError) as caught:
                    await client.plan_timed_actions(PART, alignment, 13.0, {})
                self.assertEqual(caught.exception.code, "alignment_unavailable")
                self.assertEqual(client.last_error, "alignment_unavailable")
                client._call.assert_not_awaited()

    async def test_invalid_audio_duration_rejected_before_model(self):
        for seconds in (True, 0, -1, float("nan"), float("inf"), 31, 9):
            with self.subTest(seconds=seconds):
                client = self.client()
                with self.assertRaises(SemanticSpeechError):
                    await client.plan_timed_actions(PART, ALIGNMENT, seconds, {})
                client._call.assert_not_awaited()

    async def test_model_cannot_change_content_count_or_add_fields(self):
        invalid = [None, [], {}, {"anchors": []},
                   {"anchors": [action["anchor"] for action in PART["actions"]]},
                   {**RESULT, "text": "改写台词"}, {**RESULT, "actions": PART["actions"]},
                   {**RESULT, "recovery": PART["recovery"]},
                   {**RESULT, "groups": []}, {**RESULT, "anchors": []}]
        for response in invalid:
            with self.subTest(response=response):
                client = self.client(response)
                with self.assertRaises(ModelError) as caught:
                    await client.plan_timed_actions(PART, ALIGNMENT, 13.0, {})
                self.assertEqual(caught.exception.code, "timed_action_plan_invalid")
                self.assertEqual(client.last_error, "timed_action_plan_invalid")
                client._call.assert_awaited_once()

    async def test_unknown_or_invalid_schedule_id_rejected_without_retry(self):
        for schedule_id in ("s999", "", "previous_request_s01", True, None, [], {}):
            with self.subTest(schedule_id=schedule_id):
                client = self.client({"schedule_id": schedule_id})
                with self.assertRaises(ModelError):
                    await client.plan_timed_actions(PART, ALIGNMENT, 13.0, {})
                client._call.assert_awaited_once()

    async def test_impossible_group_count_spacing_or_tail_is_rejected_before_model(self):
        variants = [({**PART, "actions": PART["actions"] * 2}, ALIGNMENT, 13.0),
                    (PART, {**ALIGNMENT, "words": [{**word, "begin_time": word["begin_time"] // 10,
                               "end_time": word["end_time"] // 10} for word in ALIGNMENT["words"]]}, 4.0)]
        for part, alignment, seconds in variants:
            with self.subTest(seconds=seconds):
                client = self.client()
                with self.assertRaises(SemanticSpeechError) as caught:
                    await client.plan_timed_actions(part, alignment, seconds, {})
                self.assertEqual(caught.exception.code, "anchor_window_conflict")
                self.assertEqual(caught.exception.details, {"reason": "no_feasible_schedule", "audio_seconds": seconds,
                    "group_count": len(part["actions"]), "s_to_q_seconds": 1.5, "ge_lead_seconds": 1.1, "minimum_post_send_seconds": 1.0})
                self.assertEqual(client.last_error, "anchor_window_conflict")
                client._call.assert_not_awaited()

    async def test_provider_word_interior_is_not_an_independent_anchor(self):
        alignment = copy.deepcopy(ALIGNMENT)
        alignment["words"][:2] = [{"text": "甲乙", "begin_time": 0, "end_time": 1250}]
        _, data, _ = prepare_timed_action_request(PART, alignment, 13.0, {})
        for schedule in data["schedules"]:
            self.assertNotIn(1.0, [group["s_at_seconds"] for group in schedule["groups"]])
            self.assertTrue(all(not group["anchor"]["quote"].startswith("乙") for group in schedule["groups"]))

    async def test_exact_spacing_allows_final_q_after_actual_audio(self):
        alignment = copy.deepcopy(ALIGNMENT)
        for index, word in enumerate(alignment["words"]):
            word["begin_time"] = index * 720
            word["end_time"] = index * 720 + 250
        client = self.client()
        result = await client.plan_timed_actions(PART, alignment, 8, {})
        self.assertEqual([n["at_seconds"] for n in result["timeline"] if n["role"] == "S"], [0, 3.6, 7.2])
        self.assertEqual([n["at_seconds"] for n in result["timeline"] if n["role"] == "Q"], [1.5, 5.1, 8.7])
        alignment["words"][5]["begin_time"] -= 1
        client = self.client()
        with self.assertRaises(SemanticSpeechError) as caught:
            await client.plan_timed_actions(PART, alignment, 8, {})
        self.assertEqual(caught.exception.code, "anchor_window_conflict")
        client._call.assert_not_awaited()

    def test_end_of_audio_zero_duration_word_is_never_an_active_anchor(self):
        part = copy.deepcopy(PART)
        part["text"] = "甲乙丙"
        for action, quote in zip(part["actions"], part["text"]):
            action["anchor"] = {"quote": quote, "occurrence": 1}
        alignment = {"status": "ok", "timebase": "audio_start", "words": [
            {"text": char, "begin_time": begin, "end_time": end}
            for char, begin, end in (("甲", 0, 100), ("乙", 3600, 3700), ("丙", 7200, 7200))]}
        with self.assertRaises(SemanticSpeechError) as caught:
            build_timed_action_schedules(part, alignment, 7.2)
        self.assertEqual(caught.exception.code, "alignment_unavailable")
        self.assertEqual(caught.exception.details["reason"], "invalid_timestamps")

    async def test_provider_error_propagates_without_retry_and_sets_last_error(self):
        client = self.client()
        client._call.side_effect = ModelError("timeout")
        with self.assertRaises(ModelError) as caught:
            await client.plan_timed_actions(PART, ALIGNMENT, 13, {})
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(client.last_error, "timeout")
        client._call.assert_awaited_once()

    async def test_cancellation_propagates_without_retry(self):
        client = self.client()
        client._call.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await client.plan_timed_actions(PART, ALIGNMENT, 13, {})
        self.assertEqual(client.last_error, "cancelled")
        client._call.assert_awaited_once()

    async def test_alignment_snapshot_cannot_change_during_model_await(self):
        alignment = copy.deepcopy(ALIGNMENT)
        client = self.client()
        async def mutate_and_reply(*_):
            alignment["words"][0]["begin_time"] = 2000
            PART_COPY["actions"][0]["motions"][0]["description"] = "外部修改"
            return copy.deepcopy(RESULT)
        PART_COPY = copy.deepcopy(PART)
        client._call.side_effect = mutate_and_reply
        result = await client.plan_timed_actions(PART_COPY, alignment, 13.0, {})
        self.assertEqual(result["timeline"][1]["at_seconds"], 0)
        self.assertEqual(result["actions"][0]["motions"], PART["actions"][0]["motions"])

    async def test_original_draft_may_be_infeasible_but_valid_final_anchors_succeed(self):
        with self.assertRaises(SemanticSpeechError):
            compile_dialogue_semantic_timeline(PART["actions"], PART["recovery"], ALIGNMENT, 13.0)
        result = await self.client().plan_timed_actions(PART, ALIGNMENT, 13.0, {})
        self.assertEqual(len(result["timeline"]), 9)

    def test_every_candidate_schedule_is_complete_valid_and_bounded(self):
        schedules = build_timed_action_schedules(PART, ALIGNMENT, 13.0)
        self.assertTrue(1 <= len(schedules) <= 12)
        signatures = set()
        for schedule in schedules:
            self.assertEqual([group["group_index"] for group in schedule["groups"]], [1, 2, 3])
            actions = copy.deepcopy(PART["actions"])
            for action, group in zip(actions, schedule["groups"]):
                action["anchor"] = group["anchor"]
            timeline = compile_dialogue_semantic_timeline(actions, PART["recovery"], ALIGNMENT, 13.0)
            self.assertEqual([n["at_seconds"] for n in timeline if n["role"] == "S"],
                             [group["s_at_seconds"] for group in schedule["groups"]])
            signature = tuple((group["anchor"]["quote"], group["anchor"]["occurrence"]) for group in schedule["groups"])
            self.assertNotIn(signature, signatures)
            signatures.add(signature)

    def test_repeated_text_with_no_unique_local_anchor_is_rejected(self):
        text = "甲乙，甲乙，甲乙。"
        alignment = {"status": "ok", "timebase": "audio_start", "words": [
            {"text": token, "begin_time": index * 5000, "end_time": index * 5000 + 1000}
            for index, token in enumerate(("甲乙，", "甲乙，", "甲乙。"))]}
        part = {**PART, "text": text, "actions": [{**PART["actions"][0], "anchor": {"quote": "甲乙", "occurrence": 1}}]}
        with self.assertRaises(SemanticSpeechError) as caught:
            build_timed_action_schedules(part, alignment, 13)
        self.assertEqual(caught.exception.details["reason"], "no_verified_anchor")

    def test_synthetic_infeasible_draft_has_generic_full_schedule(self):
        # Nearby draft anchors cannot fit the required windows. The same frozen
        # actions must still get complete legal schedules from synthetic words.
        with self.assertRaises(SemanticSpeechError):
            compile_dialogue_semantic_timeline(PART["actions"], PART["recovery"], ALIGNMENT, 13.0)
        schedules = build_timed_action_schedules(PART, ALIGNMENT, 13.0)
        self.assertTrue(schedules)
        for schedule in schedules:
            self.assertEqual(len(schedule["groups"]), len(PART["actions"]))
            self.assertTrue(all("s_at_seconds" in group and "l_at_seconds" not in group
                                for group in schedule["groups"]))
        self.assertLessEqual(len(schedules), 12)

    def test_first_stage_explains_draft_and_final_planning_authority(self):
        self.assertIn("草案", SEMANTIC_SPEECH_CONTRACT)
        self.assertIn("仅可改anchor", SEMANTIC_SPEECH_CONTRACT)
        self.assertIn("保持冻结", SEMANTIC_SPEECH_CONTRACT)
        self.assertIn("不能为了时间窗删改用户明确要求", SEMANTIC_SPEECH_CONTRACT)

    async def test_whole_body_cannot_reach_second_model_and_prompt_has_only_dialogue_s_policy(self):
        part = copy.deepcopy(PART)
        part["actions"][0]["motions"] = [{"part": "whole_body", "description": "站起来"}]
        client = self.client()
        with self.assertRaises(ModelError) as caught:
            await client.plan_timed_actions(part, ALIGNMENT, 13.0, {})
        self.assertEqual(caught.exception.code, "timed_action_input_invalid")
        client._call.assert_not_awaited()
        client = self.client()
        await client.plan_timed_actions(PART, ALIGNMENT, 13.0, {})
        prompt = client._call.call_args.args[1][0]["content"]
        for required in ("s_at_seconds", "GE→1.1秒→S→1.5秒→Q", "至少相隔3.6秒"):
            self.assertIn(required, prompt)
        for removed in ("l_at_seconds", "GE→0.5秒→L", "至少3秒", "严格在audio_seconds结束", "减1.5秒"):
            self.assertNotIn(removed, prompt)

    async def test_final_q_may_precede_audio_end_without_moving_anchors_or_padding(self):
        result = await self.client().plan_timed_actions(PART, ALIGNMENT, 13, {})
        timeline = result["timeline"]
        s_times = [node["at_seconds"] for node in timeline if node["role"] == "S"]
        q_times = [node["at_seconds"] for node in timeline if node["role"] == "Q"]
        self.assertEqual(len(s_times), len(PART["actions"]))
        self.assertTrue(all(abs(q - s - 1.5) < 1e-9 for s, q in zip(s_times, q_times)))
        self.assertLess(q_times[-1], 13)


if __name__ == "__main__":
    unittest.main()
