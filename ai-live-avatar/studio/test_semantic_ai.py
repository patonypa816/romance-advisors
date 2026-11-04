"""Normal speech generation routes; mock the call boundary, never credentials/SDK."""
import copy
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

try:
    from . import cloudbase_ai as module
except ImportError:
    import cloudbase_ai as module


COMMENT = {"id": "offline", "text": "这主意先点头，再抬手聊聊。", "nickname": "观众"}
JOB = {"input_kind": "operator", "operator_mode": "once", "id": "offline-host",
       "nickname": "主持", "text": "先点头再抬手，聊聊这主意。"}
TOPIC = {"instruction": JOB["text"], "total_rounds": 3, "completed_rounds": 1,
         "next_round": 2, "sent_history": ["刚才说过前因。"]}
PLAN = {"emotion": "温和", "reading": None,
        "segments": [{"text": "咱先聊聊这主意，把来龙去脉说清楚，再聊聊该如何往前走。",
                      "instruction": "自然清楚，正常语速。", "actions": [
                          {"anchor": {"quote": "聊聊", "occurrence": 1},
                           "motions": [{"part": "head", "description": "轻轻点头一次"}],
                           "gaze": "注视镜头", "expression": "神情温和"},
                          {"anchor": {"quote": "聊聊", "occurrence": 2},
                           "motions": [{"part": "right_arm", "description": "右手轻轻抬起"}],
                           "gaze": "注视镜头", "expression": "神情认真"}],
                      "recovery": {"gaze": "注视镜头", "expression": "神情温和"}}]}


class SemanticAIContracts(unittest.IsolatedAsyncioTestCase):
    def client(self, result=PLAN):
        client = module.CloudBaseAI({})
        client._call = AsyncMock(return_value=copy.deepcopy(result))
        client._credentials = Mock(side_effect=AssertionError("禁止读取凭据"))
        client._run_sdk = AsyncMock(side_effect=AssertionError("禁止真实SDK调用"))
        return client

    async def generate(self, client, route, context=None):
        if route == "viewer":
            return await client.generate_reply(COMMENT, [{"reading_status": "sent"}], context or {})
        return await client.generate_director({**JOB, "operator_mode": route}, [], context or {},
                                              topic=TOPIC if route == "topic" else None)

    async def test_current_routes_reject_observation_and_motion_excludes_legacy_context(self):
        for route in ("viewer", "once", "topic"):
            client = self.client({**copy.deepcopy(PLAN), "observation": "旧结构字段"})
            with self.subTest(route=route), self.assertRaises(module.ModelError) as caught:
                await self.generate(client, route)
            self.assertEqual(caught.exception.validation["stage"], "top_level")
            client._call.assert_awaited_once()
        motion = {"emotion": "温和", "actions": copy.deepcopy(module.SHORT_PERFORMANCE_EXAMPLE["segments"][0]["actions"]),
                  "recovery": copy.deepcopy(module.SHORT_PERFORMANCE_EXAMPLE["segments"][0]["recovery"])}
        client = self.client(motion)
        context = {"character": {"identity": "原创角色"},
                   "planning": ["LEGACY 最近观察画面"], "plan_contract": {"observation": "LEGACY"}}
        await client.generate_director({**JOB, "operator_mode": "motion"}, [], context)
        messages = client._call.call_args.args[1]
        self.assertNotIn("observation", messages[0]["content"])
        self.assertNotIn("LEGACY", messages[0]["content"])
        self.assertIn("原创角色", messages[0]["content"])
        client = self.client({**motion, "observation": "旧结构字段"})
        with self.assertRaises(module.ModelError) as caught:
            await client.generate_director({**JOB, "operator_mode": "motion"}, [], {})
        self.assertEqual(caught.exception.validation["stage"], "top_level")

    async def test_viewer_once_topic_share_semantic_parser_and_preserve_requested_actions(self):
        for route in ("viewer", "once", "topic"):
            with self.subTest(route=route):
                client = self.client()
                with patch.object(module, "parse_plan", side_effect=AssertionError("旧parser")), \
                     patch.object(module, "parse_director_plan", side_effect=AssertionError("旧主持parser")):
                    result = await self.generate(client, route)
                self.assertEqual(result, PLAN)
                self.assertEqual(len(result["segments"][0]["actions"]), 2)
                self.assertNotIn("timeline", result["segments"][0])
                role, messages = client._call.call_args.args
                self.assertEqual(role, "reply")
                prompt = messages[0]["content"]
                if route == "topic":
                    self.assertIn("segments必须恰好一个对象", prompt)
                    self.assertNotIn("segments为1至2个对象", prompt)
                else:
                    self.assertIn(module.SEMANTIC_SPEECH_CONTRACT, prompt)
                self.assertNotIn("observation", prompt)
                self.assertIn(module.SEMANTIC_ACTION_WRITING_RULES, prompt)
                for required in ("1至120字", "不含标点", "从1开始", "3.6秒"):
                    self.assertIn(required, prompt)
                if route == "viewer":
                    self.assertIn(module.SEMANTIC_READING_RULES, prompt)
                else:
                    self.assertIn("reading必须为null", prompt)
                client._call.assert_awaited_once()
                client._credentials.assert_not_called()
                client._run_sdk.assert_not_awaited()

    async def test_old_context_contracts_removed_but_role_settings_topic_reading_retained(self):
        context = {"character": {"identity": "原创角色"},
                   "operator_settings": {"reply_style": "自然松弛"}, "current_topic": TOPIC,
                   "planning": ["CONFLICT old schedule"], "plan_contract": {"text": "CONFLICT old example"},
                   "routing": {"vidu": "CONFLICT old Type9 timing", "tts": "CONFLICT other rule"},
                   "study_variant": {"goal": "CONFLICT trial", "source_image_path": "/private/image.png",
                                     "reading_selection": "自然选择", "reading_history_window": 6}}
        original = copy.deepcopy(context)
        for route in ("viewer", "once", "topic"):
            client = self.client()
            await self.generate(client, route, context)
            prompt = client._call.call_args.args[1][0]["content"]
            self.assertNotIn("CONFLICT", prompt)
            self.assertNotIn("/private/image.png", prompt)
            for required in ("原创角色", "自然松弛"):
                self.assertIn(required, prompt)
            if route == "viewer":
                self.assertIn("自然选择", prompt)
                self.assertIn("reading_history_window", prompt)
            else:
                self.assertNotIn("study_variant", prompt)
            if route == "topic":
                # 主讲内容保留在唯一的单段输入中，不复制进全局背景。
                data = json.loads(client._call.call_args.args[1][1]["content"])
                self.assertEqual(data["topic"]["instruction"], TOPIC["instruction"])
                self.assertNotIn('"current_topic"', prompt)
            else:
                self.assertIn(TOPIC["instruction"], prompt)
        self.assertEqual(context, original)

    async def test_fixture_and_observed_layout_use_one_validated_call_without_production_examples(self):
        for route in ("viewer", "once", "topic"):
            example = copy.deepcopy(module.SEMANTIC_SPEECH_EXAMPLE)
            client = self.client(example)
            self.assertEqual(await self.generate(client, route), example)
            prompt = client._call.call_args.args[1][0]["content"]
            self.assertNotIn(module.SEMANTIC_EXAMPLE_GUIDANCE, prompt)
            self.assertNotIn(json.dumps(example, ensure_ascii=False, separators=(",", ":")), prompt)
            flat = copy.deepcopy(example)
            action = flat["segments"][0]["actions"][0]
            action["occurrence"] = action["anchor"]["occurrence"]
            action["anchor"] = action["anchor"]["quote"]
            client = self.client(flat)
            self.assertEqual(await self.generate(client, route), example)
            client._call.assert_awaited_once()
            client._credentials.assert_not_called()

    async def test_actual_routes_use_task_first_prompt_and_only_applicable_topic_contract(self):
        for route in ("viewer", "once", "topic"):
            with self.subTest(route=route):
                client = self.client()
                await self.generate(client, route)
                prompt = client._call.call_args.args[1][0]["content"]
                self.assertTrue(prompt.startswith(module.PERFORMANCE_DIRECTION))
                self.assertEqual(prompt.count(module.PERFORMANCE_DIRECTION), 1)
                self.assertNotIn(module.SEMANTIC_EXAMPLE_GUIDANCE, prompt)
                for historical_example in ("轻轻点头一次", "右手抠鼻子", "合法结构示例"):
                    self.assertNotIn(historical_example, prompt)
                if route == "topic":
                    self.assertIn(module.SEMANTIC_TOPIC_PROMPT, prompt)
                    self.assertIn("topic.resume_required", prompt)
                    self.assertIn("conditional_draft", prompt)
                elif route == "once":
                    self.assertIn(module.SEMANTIC_DIRECTOR_PROMPT, prompt)
                    self.assertNotIn("topic.resume_required", prompt)
                    self.assertNotIn("conditional_draft", prompt)
                client._call.assert_awaited_once()
                client._credentials.assert_not_called()
                client._run_sdk.assert_not_awaited()

    async def test_role_deduplication_retains_name_and_distinct_values_without_mutating_context(self):
        for persona_distinct, style_distinct in ((False, False), (True, False), (False, True), (True, True)):
            context = {"character": {"name": "角色甲", "temperament": "角色背景", "speech": "自然口语"},
                       "operator_settings": {"name": "角色甲", "persona": "另一个背景" if persona_distinct else "角色背景",
                                             "reply_style": "简短清楚" if style_distinct else "自然口语"}}
            original = copy.deepcopy(context)
            for route in ("viewer", "once", "topic", "motion"):
                with self.subTest(route=route, persona_distinct=persona_distinct, style_distinct=style_distinct):
                    motion = {"emotion": "温和", "actions": copy.deepcopy(module.SHORT_PERFORMANCE_EXAMPLE["segments"][0]["actions"]),
                              "recovery": copy.deepcopy(module.SHORT_PERFORMANCE_EXAMPLE["segments"][0]["recovery"])}
                    client = self.client(motion if route == "motion" else PLAN)
                    await self.generate(client, route, context)
                    system = client._call.call_args.args[1][0]["content"]
                    marker = "\n可信设定与合同：\n" if route == "viewer" else "\n可信角色与参考规划：\n"
                    trusted = json.loads(system.split(marker, 1)[1])
                    self.assertEqual(trusted["character"], context["character"])
                    expected = {"name": "角色甲"}
                    if persona_distinct:
                        expected["persona"] = context["operator_settings"]["persona"]
                    if style_distinct:
                        expected["reply_style"] = context["operator_settings"]["reply_style"]
                    self.assertEqual(trusted["operator_settings"], expected)
                    self.assertEqual(context, original)
                    client._call.assert_awaited_once()
                    client._credentials.assert_not_called()

    async def test_voice_only_120_chars_and_reading_source_rules(self):
        plan = copy.deepcopy(PLAN)
        plan["segments"][0].update(text="字" * 120, actions=[], recovery=None)
        for route in ("viewer", "once", "topic"):
            self.assertEqual(await self.generate(self.client(plan), route), plan)
        plan["reading"] = {"text": "这主意", "instruction": "轻声自然短读。"}
        plan["segments"][0]["recovery"] = {"gaze": "注视镜头", "expression": "神情温和"}
        self.assertEqual(await self.generate(self.client(plan), "viewer"), plan)
        for route in ("once", "topic"):
            with self.assertRaises(module.ModelError):
                await self.generate(self.client(plan), route)
        plan["reading"]["text"] = "不在评论里"
        with self.assertRaises(module.ModelError):
            await self.generate(self.client(plan), "viewer")

    async def test_legacy_model_timing_rejected_not_repaired_or_retried(self):
        plan = copy.deepcopy(PLAN)
        plan["segments"][0]["actions"][0]["delay_seconds"] = 0
        client = self.client(plan)
        with self.assertRaises(module.ModelError) as caught:
            await self.generate(client, "viewer")
        self.assertEqual(caught.exception.validation["stage"], "parse_semantic_plan")
        self.assertEqual(caught.exception.code, "plan_invalid")
        self.assertNotIn(PLAN["segments"][0]["text"], json.dumps(caught.exception.validation))
        self.assertEqual(client._call.await_count, 1)

    async def test_natural_action_chains_preserved_in_all_normal_speech_routes(self):
        plan = copy.deepcopy(PLAN)
        action = plan["segments"][0]["actions"][0]
        action["motions"][0]["description"] = "轻轻点头然后转向左侧；接着转回正面"
        action["gaze"] = "先注视镜头然后看向左侧"
        action["expression"] = "微笑并且神情放松"
        plan["segments"][0]["recovery"]["gaze"] = "目光平稳并且注视镜头"
        for route in ("viewer", "once", "topic"):
            with self.subTest(route=route):
                client = self.client(plan)
                self.assertEqual(await self.generate(client, route), plan)
                prompt = client._call.call_args.args[1][0]["content"]
                self.assertIn(module.ACTION_WRITING_RULES, prompt)
                for restriction in ("单一短动作", "不能用然后", "不得以然后", "不能藏进一项description"):
                    self.assertNotIn(restriction, prompt)
                self.assertIn("3.6秒", prompt)
                client._call.assert_awaited_once()
                client._credentials.assert_not_called()
                client._run_sdk.assert_not_awaited()

    async def test_anchor_and_motion_errors_have_specific_safe_reasons(self):
        cases = []
        plan = copy.deepcopy(PLAN)
        plan["segments"][0]["text"] = "聊聊之后，聊，聊也是一样。"
        plan["segments"][0]["actions"] = [plan["segments"][0]["actions"][1]]
        cases.append((plan, "anchor.occurrence选中的短语必须在原文连续存在，不得跨标点拼接"))
        plan = copy.deepcopy(PLAN)
        first = plan["segments"][0]["actions"][0]
        first["motions"].append({"part": "head", "description": "头部转向左侧"})
        cases.append((plan, "同一语义S组的同时动作存在身体部位冲突"))
        for plan, reason in cases:
            with self.subTest(reason=reason):
                client = self.client(plan)
                with self.assertRaises(module.ModelError) as caught:
                    await self.generate(client, "viewer")
                self.assertEqual(caught.exception.code, "plan_invalid")
                self.assertEqual(caught.exception.validation["stage"], "parse_semantic_plan")
                self.assertEqual(caught.exception.validation["reason"], reason)
                self.assertNotIn(plan["segments"][0]["text"], json.dumps(caught.exception.validation, ensure_ascii=False))
                client._call.assert_awaited_once()
                client._credentials.assert_not_called()

    async def test_all_trial_flags_and_pure_motion_keep_old_prompt_parser(self):
        cases = [("short_performance_test", module.SHORT_PERFORMANCE_EXAMPLE),
                 ("continuous_speech_test", module.CONTINUOUS_SPEECH_EXAMPLE),
                 ("long_speech_test", module.LONG_SPEECH_EXAMPLE)]
        for flag, expected in cases:
            for route in ("viewer", "once"):
                with self.subTest(flag=flag, route=route):
                    client = self.client(expected)
                    with patch.object(module, "parse_dialogue_semantic_plan", side_effect=AssertionError("新parser不可侵入试验")):
                        self.assertEqual(await self.generate(client, route, {flag: True}), expected)
                    self.assertNotIn(module.SEMANTIC_SPEECH_CONTRACT, client._call.call_args.args[1][0]["content"])
        motion = {key: copy.deepcopy(module.SHORT_PERFORMANCE_EXAMPLE[key]) for key in ("emotion",)}
        motion.update(copy.deepcopy(module.SHORT_PERFORMANCE_EXAMPLE["segments"][0]))
        motion.pop("text")
        motion.pop("instruction")
        client = self.client(motion)
        with patch.object(module, "parse_dialogue_semantic_plan", side_effect=AssertionError("新parser不可侵入纯动作")):
            result = await client.generate_director({**JOB, "operator_mode": "motion"}, [], {})
        self.assertEqual(result, motion)
        self.assertIn(module.DIRECTOR_PROMPT["motion"], client._call.call_args.args[1][0]["content"])

    async def test_normal_routes_reject_unsupported_whole_body_before_tts_with_safe_reason(self):
        body = copy.deepcopy(PLAN)
        body["segments"][0]["actions"][0]["motions"] = [{"part": "whole_body", "description": "站起来"}]
        for route in ("viewer", "once", "topic"):
            client = self.client(body)
            with self.subTest(route=route), self.assertRaises(module.ModelError) as caught:
                await self.generate(client, route)
            self.assertEqual(caught.exception.code, "plan_invalid")
            self.assertEqual(caught.exception.validation["reason"],
                             "A方案S短动作不支持whole_body；不得把全身动作伪装为局部动作")
            self.assertNotIn(body["segments"][0]["text"], json.dumps(caught.exception.validation, ensure_ascii=False))
            client._call.assert_awaited_once()
            client._credentials.assert_not_called()


if __name__ == "__main__":
    unittest.main()
