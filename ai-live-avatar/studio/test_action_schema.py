"""只验证本地合同和编译，不把这些测试称为 Vidu 视觉验收。"""
import copy
import json
from pathlib import Path
import unittest

from action_schema import ACTION_POST_SEND_SECONDS, compile_reading_timeline, compile_timeline as real_compile_timeline, parse_director_plan, parse_plan

GE = {"gaze": "自然看向镜头", "expression": "神情温和"}
READING = {"text": "最近粮价", "instruction": "正常清晰，轻短念出原文片段。"}

def compile_timeline(actions):
    return real_compile_timeline(actions, recovery=dict(GE) if actions else None)


def short(part="head", description="轻轻点头一次", delay=0):
    return {"kind": "S", "delay_seconds": delay, "motions": [{"part": part, "description": description}], "hold_seconds": 0, **GE}


def long(delay=0, hold=4):
    return {"kind": "L", "delay_seconds": delay, "motions": [{"part": "whole_body", "description": "从椅子上站起"}], "hold_seconds": hold, **GE}


def plan(actions=None):
    return {"emotion": "亲切", "observation": "当前姿态未知", "reading": dict(READING), "segments": [{"text": "咱听着呢。", "instruction": "自然清晰，正常语速。", "actions": [] if actions is None else actions, "recovery": dict(GE)}]}


class ActionSchemaTests(unittest.TestCase):
    def test_director_speech_has_no_reading_and_keeps_strict_speech_contract(self):
        body = plan([short()])
        for mode in ("once", "topic"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "reading"):
                parse_director_plan(body, mode=mode)
            speech = {**body, "reading": None}
            self.assertEqual([n["role"] for n in parse_director_plan(speech, mode=mode)["segments"][0]["timeline"]], ["S", "Q"])
            for extra in ({"id": "transport"}, {"actions": [short()]}, {"operator_mode": "motion"}):
                with self.subTest(extra=extra), self.assertRaises(ValueError):
                    parse_director_plan({**speech, **extra}, mode=mode)
            no_text = copy.deepcopy(speech)
            no_text["segments"][0]["text"] = ""
            with self.assertRaises(ValueError):
                parse_director_plan(no_text, mode=mode)

    def test_director_motion_compiles_l_window_ge_and_unique_tail_q(self):
        body = {"emotion": "认真", "actions": [long(hold=5), short(delay=5)], "recovery": GE}
        original = copy.deepcopy(body)
        parsed = parse_director_plan(body, mode="motion")
        self.assertEqual(body, original)
        self.assertEqual(set(parsed), {*body, "timeline"})
        self.assertEqual([n["role"] for n in parsed["timeline"]], ["L", "S", "Q"])
        self.assertEqual([n["at_seconds"] for n in parsed["timeline"]], [0, 5, 6.5])
        self.assertTrue(all(n["gaze"] in n["instruction"] and n["expression"] in n["instruction"] for n in parsed["timeline"]))
        with self.assertRaises(ValueError):
            parse_plan(body)  # 纯动作只能由可信server选主持分支，观众不接受。

    def test_director_motion_rejects_empty_or_long_tasks_and_invalid_recovery(self):
        body = {"emotion": "认真", "actions": [short()], "recovery": GE}
        for actions in ([], None, [short(delay=1)], [long(hold=5), short(delay=4.9)],
                        [short()] + [short(delay=2) for _ in range(6)]):
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                parse_director_plan({**body, "actions": actions}, mode="motion")
        for recovery in (None, {}, {**GE, "text": "别说话"}, {**GE, "gaze": ""}):
            with self.subTest(recovery=recovery), self.assertRaises(ValueError):
                parse_director_plan({**body, "recovery": recovery}, mode="motion")
        for extra in ({"reading": None}, {"segments": []}, {"text": ""}, {"timeline": []}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                parse_director_plan({**body, **extra}, mode="motion")
        speech = plan([short()] + [short(delay=2) for _ in range(6)])
        speech["reading"] = None
        with self.assertRaises(ValueError):
            parse_director_plan(speech, mode="once")
        self.assertEqual(len(parse_plan(speech)["segments"][0]["actions"]), 7)
        with self.assertRaises(ValueError):
            parse_director_plan(body, mode="viewer")

    def test_compatible_pair_compiles_one_type9(self):
        node = short("both_arms", "双手向外轻摊")
        node["motions"].append({"part": "head", "description": "头部轻轻向右倾斜"})
        timeline = compile_timeline([node])
        self.assertEqual(timeline[0]["instruction"], "动作：同时双手向外轻摊，并头部轻轻向右倾斜。\n眼神：自然看向镜头。\n表情：神情温和。")
        self.assertEqual([item["role"] for item in timeline], ["S", "Q"])

    def test_three_simultaneous_motions_rejected(self):
        node = short()
        node["motions"] += [{"part": "face", "description": "略带微笑"}, {"part": "left_arm", "description": "左手轻摊"}]
        with self.assertRaises(ValueError):
            compile_timeline([node])

    def test_conflicts_rejected_in_both_orders(self):
        for first, second in [("head", "head"), ("both_arms", "left_arm"), ("right_arm", "both_arms"), ("whole_body", "face")]:
            with self.subTest(first=first, second=second):
                node = short(first)
                node["motions"].append({"part": second, "description": "轻轻示意"})
                with self.assertRaises(ValueError):
                    compile_timeline([node])

    def test_sequential_count_is_not_capped_at_two(self):
        actions = [short(delay=0)] + [short(delay=2) for _ in range(4)]
        timeline = compile_timeline(actions)
        self.assertEqual([item["role"] for item in timeline], ["S"] * 5 + ["Q"])
        self.assertEqual([item["index"] for item in timeline], [1, 2, 3, 4, 5, 6])
        self.assertEqual([item["at_seconds"] for item in timeline], [0, 2, 4, 6, 8, 9.5])
        self.assertEqual(timeline[-1]["delay_seconds"], 1.5)
        self.assertEqual(timeline[-1]["instruction"], "动作：正常坐姿。\n眼神：自然看向镜头。\n表情：神情温和。")

    def test_empty_actions_do_not_add_q(self):
        self.assertEqual(compile_timeline([]), [])
        self.assertEqual([node["role"] for node in parse_plan(plan())["segments"][0]["timeline"]], ["R", "S0", "Q"])

    def test_long_action_has_its_own_estimated_window(self):
        timeline = compile_timeline([long(hold=5), short(delay=5)])
        self.assertEqual([item["at_seconds"] for item in timeline], [0, 5, 6.5])
        timeline = compile_timeline([long(hold=5)])
        self.assertEqual(timeline[-1]["at_seconds"], 5)
        self.assertEqual(timeline[-1]["delay_seconds"], 5)
        self.assertEqual(timeline[-1]["role"], "Q")
        with self.assertRaises(ValueError):
            compile_timeline([long(hold=5), short(delay=4.9)])

    def test_long_cannot_overlay_or_use_head(self):
        node = long()
        node["motions"].append({"part": "head", "description": "轻轻点头一次"})
        with self.assertRaises(ValueError):
            compile_timeline([node])
        node = long()
        node["motions"][0]["part"] = "head"
        with self.assertRaises(ValueError):
            compile_timeline([node])
        node["motions"][0]["part"] = "torso"
        self.assertEqual(compile_timeline([node])[0]["role"], "L")

    def test_time_values_must_be_finite_numbers_and_not_bool(self):
        for value in [True, False, float("nan"), float("inf"), -float("inf"), "2", None, 10 ** 400]:
            for field in ["delay_seconds", "hold_seconds"]:
                with self.subTest(value=str(value), field=field):
                    node = short()
                    node[field] = value
                    with self.assertRaises(ValueError):
                        compile_timeline([node])

    def test_delay_and_hold_boundaries(self):
        for delay in [-1, 0.1, 30.1]:
            with self.assertRaises(ValueError):
                compile_timeline([short(delay=delay)])
        for delay in [0, -1, .999, 30.1]:
            with self.assertRaises(ValueError):
                compile_timeline([short(), short(delay=delay)])
        self.assertEqual(compile_timeline([short(), short(delay=30)])[1]["delay_seconds"], 30)
        for hold in [0, 1.5, 30.1]:
            with self.assertRaises(ValueError):
                compile_timeline([long(hold=hold)])
        compile_timeline([long(hold=30)])
        node = short()
        node["hold_seconds"] = 1
        with self.assertRaises(ValueError):
            compile_timeline([node])

    def test_post_send_minimum_preserves_first_zero_s_tail_and_l_window(self):
        for next_node in (short(delay=ACTION_POST_SEND_SECONDS), long(delay=ACTION_POST_SEND_SECONDS)):
            timeline = compile_timeline([short(), next_node])
            self.assertEqual(timeline[0]["delay_seconds"], 0)
            self.assertEqual(timeline[1]["delay_seconds"], 1)
            self.assertEqual(timeline[-1]["delay_seconds"], 1.5 if next_node["kind"] == "S" else 4)
            invalid = {**next_node, "delay_seconds": .999}
            with self.assertRaisesRegex(ValueError, "后续节点间隔必须至少1秒"):
                compile_timeline([short(), invalid])
        with self.assertRaisesRegex(ValueError, "前一L的hold_seconds"):
            compile_timeline([long(hold=5), short(delay=1)])
        self.assertEqual(compile_timeline([long(hold=5), short(delay=5)])[1]["delay_seconds"], 5)

    def test_old_structure_model_q_and_extra_fields_rejected(self):
        for extra in [{"action": {"body": "坐着"}}, {"action_instruction": "点头"}, {"timeline": []}]:
            body = plan()
            body["segments"][0].update(extra)
            with self.assertRaises(ValueError):
                parse_plan(body)
        for kind in ["Q", "type9", None, []]:
            node = short()
            node["kind"] = kind
            with self.assertRaises(ValueError):
                compile_timeline([node])
        node = short()
        node["instruction"] = "任意指令"
        with self.assertRaises(ValueError):
            compile_timeline([node])

    def test_continuous_descriptions_preserved_without_extra_type9_nodes(self):
        for connector in ["然后", "接着", "随后", "同时", "一边", "并且", "再做", "继而", "而后", "接下来", "；", ";"]:
            with self.subTest(connector=connector):
                text = f"点头{connector}转向左侧"
                node = short(description=text)
                before = copy.deepcopy(node)
                timeline = compile_timeline([node])
                self.assertEqual(node, before)
                self.assertIn(f"动作：{text}。", timeline[0]["instruction"])
                self.assertEqual([(item["role"], item["at_seconds"]) for item in timeline], [("S", 0), ("Q", 1.5)])
        self.assertEqual(compile_timeline([short(description="轻轻并拢双手")])[0]["instruction"], "动作：轻轻并拢双手。\n眼神：自然看向镜头。\n表情：神情温和。")

    def test_contract_bounds_and_missing_fields(self):
        for key, limit in [("emotion", 24), ("observation", 300)]:
            for value in ["", " ", "字" * (limit + 1), None]:
                body = plan()
                body[key] = value
                with self.assertRaises(ValueError):
                    parse_plan(body)
        for key, limit in [("text", 30), ("instruction", 160)]:
            body = plan()
            body["segments"][0][key] = "字" * (limit + 1)
            with self.assertRaises(ValueError):
                parse_plan(body)
        for segments in [[], [plan()["segments"][0]] * 3, None]:
            body = plan()
            body["segments"] = segments
            with self.assertRaises(ValueError):
                parse_plan(body)
        for actions in [None, {}, "点头"]:
            body = plan()
            body["segments"][0]["actions"] = actions
            with self.assertRaises(ValueError):
                parse_plan(body)

    def test_transport_fields_are_ignored_and_input_is_not_mutated(self):
        body = plan([short()])
        body.update({"id": "reply-1", "lease": {"opaque": "transport"}})
        before = copy.deepcopy(body)
        result = parse_plan(body)
        self.assertEqual(body, before)
        self.assertEqual(set(result), {"emotion", "observation", "reading", "segments"})
        result["segments"][0]["timeline"][0]["motions"][0]["description"] = "改动输出"
        self.assertEqual(result["segments"][0]["actions"][0]["motions"][0]["description"], "轻轻点头一次")

    def test_brain_context_example_matches_contract(self):
        from semantic_speech import parse_dialogue_semantic_plan
        context = json.loads(Path(__file__).with_name("brain-context.json").read_text(encoding="utf-8"))
        result = parse_dialogue_semantic_plan(context["plan_contract"])
        self.assertEqual(len(result["segments"][0]["actions"][0]["motions"]), 1)
        self.assertNotIn("observation", json.dumps(context))

    def test_motion_historical_observation_is_only_accepted_explicitly(self):
        current = {"emotion": "认真", "actions": [short()], "recovery": GE}
        historical = {**current, "observation": "历史格式保留值"}
        with self.assertRaises(ValueError):
            parse_director_plan(historical, mode="motion")
        self.assertEqual(parse_director_plan(historical, mode="motion", historical_contract=True)["observation"],
                         historical["observation"])

    def test_reading_is_required_strict_and_matches_contiguous_source(self):
        body = plan()
        result = parse_plan(body, source_text="陛下最近粮价为何涨了？")
        self.assertEqual(result["reading"], READING)
        for text in ("最近粮价为何", "为何涨了", "粮价为"):
            body["reading"]["text"] = text
            self.assertEqual(parse_plan(body, source_text="最近粮价为何涨了？")["reading"]["text"], text)
        for reading in ({}, {"text": "粮价"}, {**READING, "read_seconds": 2},
                        {**READING, "instruction": ""}, {**READING, "instruction": "字" * 161}):
            body["reading"] = reading
            with self.subTest(reading=reading), self.assertRaises(ValueError):
                parse_plan(body, source_text="最近粮价为何涨了？")
        body = plan()
        del body["reading"]
        with self.assertRaises(ValueError):
            parse_plan(body)
        for text in ("粮价最近", "你问粮价", "粮价，为何"):
            body = plan()
            body["reading"]["text"] = text
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_plan(body, source_text="最近粮价为何涨了？")

    def test_reading_uses_general_speech_text_limit_not_two_to_six_rule(self):
        for text in ("啊", "你好", "A1", "你，A！", "123456", "最近粮价为何涨", "？？？", "字" * 30,
                     "你" + "！" * 11 + "好"):
            body = plan()
            body["reading"]["text"] = text
            self.assertEqual(parse_plan(body, source_text=text)["reading"]["text"], text)
        for text in ("", " ", "字" * 31, [], None):
            body = plan()
            body["reading"]["text"] = text
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_plan(body)

    def test_null_reading_skips_whole_bundle_for_any_source_length(self):
        body = plan()
        body["reading"] = None
        body["segments"][0]["recovery"] = None
        for source in (None, "", "！", "你？", "A！", "你好", "A1", "最近粮价为何涨了？", "字" * 300):
            with self.subTest(source=source):
                result = parse_plan(body, source_text=source)
                self.assertIsNone(result["reading"])
                self.assertEqual(result["segments"][0]["timeline"], [])
        # 省略阅读不限制正常回答的动作；仍可按原合同发送S及其尾Q。
        body = plan([short()])
        body["reading"] = None
        self.assertEqual([n["role"] for n in parse_plan(body, source_text="最近粮价为何涨了？")["segments"][0]["timeline"]], ["S", "Q"])

    def test_reading_first_segment_recovery_is_required_even_without_actions(self):
        body = plan()
        body["segments"][0]["recovery"] = None
        with self.assertRaises(ValueError):
            parse_plan(body)
        body = plan()
        body["segments"].append({"text": "咱细说。", "instruction": "自然清晰。", "actions": [], "recovery": None})
        result = parse_plan(body)
        self.assertEqual([n["role"] for n in result["segments"][0]["timeline"]], ["R", "S0", "Q"])
        self.assertEqual(result["segments"][1]["timeline"], [])
        body["segments"][1]["actions"] = [short()]
        body["segments"][1]["recovery"] = dict(GE)
        self.assertEqual([n["role"] for n in parse_plan(body)["segments"][1]["timeline"]], ["S", "Q"])

    def test_each_segment_resets_timing_even_after_reading_and_long_action(self):
        body = plan([long(hold=4), short(delay=4)])
        body["segments"].append({"text": "咱再说第二点。", "instruction": "自然清晰。",
                                 "actions": [short(), short(delay=2.4)], "recovery": dict(GE)})
        for reading in (None, dict(READING)):
            body["reading"] = reading
            parsed = parse_plan(body)
            first, second = parsed["segments"]
            self.assertEqual([a["delay_seconds"] for a in first["actions"]], [0, 4])
            self.assertEqual([a["delay_seconds"] for a in second["actions"]], [0, 2.4])
            self.assertEqual([n["at_seconds"] for n in second["timeline"]], [0, 2.4, 3.9])
            bad = copy.deepcopy(body)
            bad["segments"][1]["actions"][0]["delay_seconds"] = 2.4
            with self.assertRaisesRegex(ValueError, "首节点间隔必须为0"):
                parse_plan(bad)

    def test_reading_timeline_has_unique_indexes_one_tail_q_and_every_ge(self):
        timeline = compile_reading_timeline(READING, [short(), long(delay=2.5, hold=4)], GE, 0.8)
        self.assertEqual([n["role"] for n in timeline], ["R", "S0", "S", "L", "Q"])
        self.assertEqual([n["index"] for n in timeline], [1, 2, 3, 4, 5])
        self.assertEqual([n["at_seconds"] for n in timeline], [0.0, 1.8, 2.8, 5.3, 9.3])
        self.assertEqual([n["delay_seconds"] for n in timeline], [0.0, 1.8, 1.0, 2.5, 4.0])
        self.assertEqual(timeline[0]["gaze"], "目光落在镜头下方附近")
        self.assertEqual(timeline[0]["expression"], "眼睛轻微眯起，神情专注辨认")
        self.assertEqual([m["part"] for m in timeline[0]["motions"]], ["torso", "head"])
        self.assertEqual(timeline[1]["motions"], [{"part": "torso", "description": "收回前倾恢复正常坐姿"}])
        for node in timeline:
            self.assertIn(node["gaze"], node["instruction"])
            self.assertIn(node["expression"], node["instruction"])
        self.assertEqual(timeline[1]["gaze"], GE["gaze"])
        self.assertEqual(timeline[-1]["expression"], GE["expression"])

    def test_reading_preview_preserves_unknown_pcm_duration(self):
        for actions in ([], [short(), short(delay=2)]):
            timeline = compile_reading_timeline(READING, actions, GE)
            self.assertEqual(timeline[0]["at_seconds"], 0)
            self.assertTrue(all(n["at_seconds"] is None for n in timeline[1:]))
            self.assertIsNone(timeline[1]["delay_seconds"])
            self.assertEqual(timeline[2]["delay_seconds"], 1.0 if actions else 1.5)
            self.assertEqual(timeline[-1]["delay_seconds"], 1.5)
        timeline = compile_reading_timeline(READING, [], GE, 0.7)
        self.assertEqual([n["at_seconds"] for n in timeline], [0, 1.7, 3.2])
        for seconds in (True, "1", float("nan"), float("inf"), -1, 0):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                compile_reading_timeline(READING, [], GE, seconds)

    def test_each_node_ge_and_explicit_recovery_are_compiled(self):
        first, second = short(), short(delay=2)
        first.update(gaze="望向窗外", expression="神情疑惑")
        second.update(gaze="自然看向镜头", expression="神情认真")
        recovery = {"gaze": "注视镜头", "expression": "神情温和，略带好奇"}
        timeline = real_compile_timeline([first, second], recovery=recovery)
        for node, target in zip(timeline, [first, second, recovery]):
            self.assertEqual(node["gaze"], target["gaze"])
            self.assertEqual(node["expression"], target["expression"])
            self.assertIn(target["gaze"], node["instruction"])
            self.assertIn(target["expression"], node["instruction"])
        self.assertTrue(timeline[-1]["instruction"].startswith("动作：正常坐姿。\n"))
        self.assertNotIn("望向窗外", timeline[-1]["instruction"])

    def test_missing_ge_or_recovery_and_old_plan_are_rejected(self):
        for field in ("gaze", "expression"):
            node = short()
            del node[field]
            with self.assertRaises(ValueError):
                compile_timeline([node])
        for recovery in (None, {}, {"gaze": "看镜头"}, {**GE, "hold_seconds": 2}):
            with self.subTest(recovery=recovery):
                with self.assertRaises(ValueError):
                    real_compile_timeline([short()], recovery=recovery)
        body = plan([short()])
        del body["segments"][0]["recovery"]
        with self.assertRaises(ValueError):
            parse_plan(body)
        with self.assertRaises(ValueError):
            real_compile_timeline([], recovery=GE)
        self.assertEqual(real_compile_timeline([], recovery=None), [])

    def test_ge_bounds_still_rejected(self):
        for field in ("gaze", "expression"):
            for value in ("", " ", "字" * 41, None):
                node = short()
                node[field] = value
                with self.assertRaises(ValueError):
                    compile_timeline([node])
                recovery = {**GE, field: value}
                with self.assertRaises(ValueError):
                    real_compile_timeline([short()], recovery=recovery)
        node = short()
        node.update(gaze="自然看向镜头，正常眨眼", expression="神情温和、略带好奇")
        compile_timeline([node])

    def test_continuous_gaze_expression_and_recovery_preserved(self):
        node = short()
        node.update(gaze="注视镜头然后看向左侧", expression="先微笑；随后神情平静")
        recovery = {"gaze": "看向左侧接着看回镜头", "expression": "神情温和并且自然微笑"}
        body = {"emotion": "认真", "actions": [node], "recovery": recovery}
        parsed = parse_director_plan(body, mode="motion")
        for source, compiled in ((node, parsed["timeline"][0]), (recovery, parsed["timeline"][-1])):
            for field in ("gaze", "expression"):
                self.assertEqual(compiled[field], source[field])
                self.assertIn(source[field], compiled["instruction"])

    def test_full_instruction_keeps_all_individually_valid_fields_over_200(self):
        node = short(description="动" * 70)
        node["motions"].append({"part": "left_arm", "description": "作" * 70})
        node.update(gaze="看" * 40, expression="情" * 40)
        self.assertEqual(len(node["motions"]), 2)  # G/E不算额外两个主动动作。
        timeline = compile_timeline([node])
        self.assertGreater(len(timeline[0]["instruction"]), 200)
        parsed = parse_plan(plan([node]))
        self.assertEqual(parsed["segments"][0]["actions"][0]["motions"], node["motions"])
        for value in [m["description"] for m in node["motions"]] + [node["gaze"], node["expression"]]:
            self.assertIn(value, timeline[0]["instruction"])
        self.assertEqual(len(node["motions"][0]["description"]), 70)
        node["motions"][0]["description"] += "超"
        with self.assertRaises(ValueError):
            parse_plan(plan([node]))


if __name__ == "__main__":
    unittest.main()
