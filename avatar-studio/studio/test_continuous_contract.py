"""连续语音试验的离线合同：只验证计划，不接触凭据、云或服务。"""
import copy
import unittest

try:
    from . import action_schema as schema
    from .cloudbase_ai import CONTINUOUS_SPEECH_EXAMPLE
except ImportError:
    import action_schema as schema
    from cloudbase_ai import CONTINUOUS_SPEECH_EXAMPLE


def plan():
    return copy.deepcopy(CONTINUOUS_SPEECH_EXAMPLE)


class ContinuousContractTests(unittest.TestCase):
    def test_two_groups_reuse_single_s_and_original_q(self):
        raw = plan()
        before = copy.deepcopy(raw)
        part = raw["segments"][0]
        timeline = schema.compile_continuous_speech_timeline(part["actions"], part["recovery"])
        self.assertEqual(raw, before)
        self.assertEqual([n["role"] for n in timeline], ["GE", "S", "Q", "GE", "S", "Q"])
        self.assertEqual([n["index"] for n in timeline], list(range(1, 7)))
        self.assertEqual([n["group_index"] for n in timeline], [1, 1, 1, 2, 2, 2])
        self.assertEqual([n["at_seconds"] for n in timeline], [0, .5, 2, 3, 3.5, 5])
        self.assertEqual([n["delay_seconds"] for n in timeline], [0, .5, 1.5, 1, .5, 1.5])
        self.assertEqual(timeline[3]["audio_at_seconds"], 2.5)
        self.assertEqual([n["index"] for n in timeline if "audio_at_seconds" in n], [4])
        for group_index in (1, 2):
            action = {**part["actions"][group_index - 1], "delay_seconds": 0}
            single = schema.compile_short_performance_timeline([action], part["recovery"])
            actual = timeline[(group_index - 1) * 3:group_index * 3]
            for node, baseline in zip(actual, single):
                for key in ("instruction", "gaze", "expression", "motions", "kind", "hold_seconds"):
                    self.assertEqual(node[key], baseline[key])
            self.assertEqual(actual[2]["instruction"], "动作：正常坐姿。\n眼神：注视镜头。\n表情：神情温和。")

    def test_constants_and_valid_plan_keep_complete_schema(self):
        self.assertEqual(schema.CONTINUOUS_SECOND_S_SECONDS, 3.0)
        self.assertEqual(schema.CONTINUOUS_SECOND_GE_SECONDS, 2.5)
        self.assertEqual(schema.CONTINUOUS_MIN_AUDIO_SECONDS, 5.0)
        raw = plan()
        self.assertTrue(25 <= len(raw["segments"][0]["text"]) <= 30)
        parsed = schema.parse_plan(raw)
        self.assertIs(schema.validate_continuous_speech_plan(parsed), parsed)
        self.assertEqual(len(parsed["segments"][0]["timeline"]), 6)
        # 较短自然台词仍合法；只有实际PCM才能决定是否达到试验时长。
        raw["segments"][0]["text"] = "好。"
        schema.validate_continuous_speech_plan(schema.parse_plan(raw))

    def test_count_kind_single_motion_and_fixed_interval_are_strict(self):
        base = plan()["segments"][0]
        cases = []
        for count in (0, 1, 3):
            actions = [copy.deepcopy(base["actions"][0]) for _ in range(count)]
            for item in actions[1:]:
                item["delay_seconds"] = 3
            cases.append(actions)
        for delay in (0, .5, 2.5, 3.000001, 30, 31, -1, True, "3", float("nan"), float("inf")):
            actions = copy.deepcopy(base["actions"])
            actions[1]["delay_seconds"] = delay
            cases.append(actions)
        for position in (0, 1):
            for change in ({"kind": "L", "hold_seconds": 2, "motions": [{"part": "torso", "description": "前倾"}]},
                           {"hold_seconds": 1}, {"delay_seconds": 1} if position == 0 else {"kind": "Q"},
                           {"motions": []}, {"motions": [{"part": "head", "description": "点头"},
                                                         {"part": "right_arm", "description": "挥手"}]}):
                actions = copy.deepcopy(base["actions"])
                actions[position].update(change)
                cases.append(actions)
        for actions in cases:
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                schema.compile_continuous_speech_timeline(actions, base["recovery"])

    def test_regular_schema_validation_is_not_bypassed(self):
        for key, value in (("extra", "forbidden"), ("gaze", "字" * 41),
                           ("expression", ""), ("motions", [{"part": "whole_body", "description": "站起"}]),
                           ("motions", [{"part": "head", "description": ""}])):
            part = plan()["segments"][0]
            part["actions"][1][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                schema.compile_continuous_speech_timeline(part["actions"], part["recovery"])
        for recovery in (None, {}, {"gaze": "自然", "expression": "平静", "extra": "x"},
                         {"gaze": "", "expression": "平静"}):
            part = plan()["segments"][0]
            with self.subTest(recovery=recovery), self.assertRaises(ValueError):
                schema.compile_continuous_speech_timeline(part["actions"], recovery)

    def test_continuous_wording_keeps_fixed_trial_timing(self):
        part = plan()["segments"][0]
        baseline = schema.compile_continuous_speech_timeline(part["actions"], part["recovery"])
        action = part["actions"][1]
        action["motions"][0]["description"] = "点头然后摇头"
        action.update(gaze="看向镜头接着左看", expression="先微笑；随后平静")
        part["recovery"]["gaze"] = "先看左侧然后看回镜头"
        timeline = schema.compile_continuous_speech_timeline(part["actions"], part["recovery"])
        self.assertEqual([(n["role"], n["at_seconds"]) for n in timeline],
                         [(n["role"], n["at_seconds"]) for n in baseline])
        instructions = "\n".join(n["instruction"] for n in timeline)
        for text in (action["motions"][0]["description"], action["gaze"], action["expression"], part["recovery"]["gaze"]):
            self.assertIn(text, instructions)

    def test_reading_segments_and_text_boundaries(self):
        for mutate in (lambda p: p.update(reading={"text": "看", "instruction": "自然"}),
                       lambda p: p.update(segments=[]),
                       lambda p: p["segments"].append(copy.deepcopy(p["segments"][0]))):
            parsed = schema.parse_plan(plan())
            mutate(parsed)
            with self.assertRaisesRegex(ValueError, "reading=null且恰好一段"):
                schema.validate_continuous_speech_plan(parsed)
        for text in ("", "字" * 31, None):
            parsed = schema.parse_plan(plan())
            parsed["segments"][0]["text"] = text
            with self.assertRaises(ValueError):
                schema.validate_continuous_speech_plan(parsed)

    def test_input_boundary_matches_manual_viewer_and_once_only(self):
        for item in ({}, {"input_kind": "viewer", "source": None},
                     {"input_kind": "operator", "operator_mode": "once"},
                     {"input_kind": "operator", "operator_mode": "once", "source": "operator"}):
            self.assertEqual(schema.continuous_speech_input_mode(item), "speech")
        for item in (None, [], {"source": "manual"}, {"source": "douyin"}, {"source": "operator"},
                     {"input_kind": "operator", "operator_mode": "topic"},
                     {"input_kind": "operator", "operator_mode": "motion"},
                     {"input_kind": "operator", "operator_mode": "once", "source": "douyin"},
                     {"input_kind": "reading"}, {"input_kind": "unknown"}):
            with self.subTest(item=item), self.assertRaises(ValueError):
                schema.continuous_speech_input_mode(item)

    def test_normal_and_single_s_baselines_are_unchanged(self):
        part = plan()["segments"][0]
        regular = schema.compile_timeline(part["actions"], part["recovery"])
        self.assertEqual([n["role"] for n in regular], ["S", "S", "Q"])
        self.assertEqual([n["at_seconds"] for n in regular], [0, 3, 4.5])
        short = schema.compile_short_performance_timeline(part["actions"][:1], part["recovery"])
        self.assertEqual([n["at_seconds"] for n in short], [0, .5, 2])
        self.assertFalse(any("group_index" in n for n in short))
        with self.assertRaises(ValueError):
            schema.compile_short_performance_timeline(part["actions"], part["recovery"])


if __name__ == "__main__":
    unittest.main()
