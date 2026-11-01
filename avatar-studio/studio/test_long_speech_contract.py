"""长文语音试验纯离线合同：不启动服务，不接触凭据与云调用。"""
import copy
import unittest

try:
    from . import action_schema as schema
    from .cloudbase_ai import CONTINUOUS_SPEECH_EXAMPLE, LONG_SPEECH_EXAMPLE, SHORT_PERFORMANCE_EXAMPLE
except ImportError:
    import action_schema as schema
    from cloudbase_ai import CONTINUOUS_SPEECH_EXAMPLE, LONG_SPEECH_EXAMPLE, SHORT_PERFORMANCE_EXAMPLE


def plan(count=4, text=None):
    result = copy.deepcopy(LONG_SPEECH_EXAMPLE)
    segment = result["segments"][0]
    template = segment["actions"]
    segment["actions"] = [copy.deepcopy(template[i % len(template)]) for i in range(count)]
    for i, action in enumerate(segment["actions"]):
        action["delay_seconds"] = 0 if i == 0 else 3.0
    if text is not None:
        segment["text"] = text
    return result


class LongSpeechContractTests(unittest.TestCase):
    def test_constants_and_complete_example(self):
        self.assertEqual(schema.LONG_SPEECH_MAX_TEXT, 120)
        self.assertEqual(schema.LONG_SPEECH_MAX_GROUPS, 6)
        self.assertEqual(schema.LONG_SPEECH_GROUP_INTERVAL, 3.0)
        self.assertEqual(schema.LONG_SPEECH_TAIL_SECONDS, 2.0)
        self.assertTrue(80 <= len(LONG_SPEECH_EXAMPLE["segments"][0]["text"]) <= 100)
        self.assertEqual(len(LONG_SPEECH_EXAMPLE["segments"][0]["actions"]), 4)
        parsed = schema.parse_long_speech_plan(plan())
        self.assertEqual(set(parsed), {"emotion", "observation", "reading", "segments"})
        self.assertEqual(set(parsed["segments"][0]), {"text", "instruction", "actions", "recovery", "timeline"})
        self.assertEqual(len(parsed["segments"][0]["timeline"]), 12)

    def test_every_group_reuses_single_s_and_same_original_q(self):
        for count in range(1, 7):
            raw = plan(count)
            before = copy.deepcopy(raw)
            segment = raw["segments"][0]
            actual = schema.compile_long_speech_timeline(segment["actions"], segment["recovery"])
            self.assertEqual(raw, before)
            self.assertEqual(len(actual), count * 3)
            self.assertEqual([n["index"] for n in actual], list(range(1, count * 3 + 1)))
            self.assertEqual([n["role"] for n in actual], ["GE", "S", "Q"] * count)
            expected_times = [offset + local for offset in range(0, count * 3, 3) for local in (0, .5, 2)]
            self.assertEqual([n["at_seconds"] for n in actual], expected_times)
            self.assertEqual([n["delay_seconds"] for n in actual], [0, .5, 1.5] + [1, .5, 1.5] * (count - 1))
            for i in range(count):
                group = actual[i * 3:(i + 1) * 3]
                single = schema.compile_short_performance_timeline(
                    [{**segment["actions"][i], "delay_seconds": 0}], segment["recovery"])
                self.assertEqual([n["group_index"] for n in group], [i + 1] * 3)
                if i == 0:
                    self.assertNotIn("audio_at_seconds", group[0])
                else:
                    self.assertEqual(group[0]["audio_at_seconds"], i * 3 - .5)
                for node, baseline in zip(group, single):
                    for field in ("instruction", "gaze", "expression", "motions", "kind", "hold_seconds"):
                        self.assertEqual(node[field], baseline[field])
                self.assertEqual(group[2]["instruction"], "动作：正常坐姿。\n眼神：注视镜头。\n表情：神情温和。")
                self.assertFalse(any("audio_at_seconds" in n for n in group[1:]))
            self.assertEqual(schema.long_speech_required_audio_seconds(segment["actions"]), (count - 1) * 3 + 2)

    def test_long_text_boundaries_and_original_plan_are_preserved(self):
        for length in (1, 30, 60, 100, 120):
            raw = plan(text="字" * length)
            before = copy.deepcopy(raw)
            parsed = schema.parse_long_speech_plan(raw)
            self.assertEqual(raw, before)
            self.assertEqual(parsed["segments"][0]["text"], "字" * length)
            self.assertIs(schema.validate_long_speech_plan(parsed), parsed)
        for invalid in ("", " ", "字" * 121, None, 1, [], {}):
            raw = plan()
            raw["segments"][0]["text"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                schema.parse_long_speech_plan(raw)

    def test_external_schema_requires_exact_fields_without_transport_or_timeline(self):
        invalids = [None, [], {}]
        for field in ("emotion", "observation", "reading", "segments"):
            raw = plan()
            raw.pop(field)
            invalids.append(raw)
        for field in ("id", "lease", "timeline", "unknown"):
            raw = plan()
            raw[field] = "untrusted"
            invalids.append(raw)
        for field in ("text", "instruction", "actions", "recovery"):
            raw = plan()
            raw["segments"][0].pop(field)
            invalids.append(raw)
        for field in ("timeline", "unknown", "action"):
            raw = plan()
            raw["segments"][0][field] = "untrusted"
            invalids.append(raw)
        for raw in invalids:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                schema.parse_long_speech_plan(raw)

    def test_validator_rebuilds_internal_timelines_and_still_checks_every_field(self):
        internal = schema.parse_long_speech_plan(plan())
        expected = copy.deepcopy(internal)
        internal["timeline"] = [{"instruction": "forged top timeline"}]
        internal["segments"][0]["timeline"] = [{"instruction": "forged segment timeline"}]
        self.assertIs(schema.validate_long_speech_plan(internal), internal)
        self.assertEqual(internal, expected)
        for mutate in (lambda p: p.update(unknown="x"),
                       lambda p: p["segments"][0].update(unknown="x"),
                       lambda p: p.pop("emotion"),
                       lambda p: p["segments"][0].pop("instruction"),
                       lambda p: p["segments"][0].update(text="字" * 121),
                       lambda p: p.update(reading={"text": "字", "instruction": "自然"})):
            internal = schema.parse_long_speech_plan(plan())
            mutate(internal)
            before = copy.deepcopy(internal)
            with self.assertRaises(ValueError):
                schema.validate_long_speech_plan(internal)
            # 失败计划不能被校验器局部改成看似已通过的内容。
            self.assertEqual(internal, before)

    def test_no_reading_or_extra_segments_and_other_text_limits_remain(self):
        invalids = []
        for value in ({"text": "字", "instruction": "自然"}, False, "", []):
            raw = plan()
            raw["reading"] = value
            invalids.append(raw)
        for segments in ([], None, {}, [None], [plan()["segments"][0]] * 2):
            raw = plan()
            raw["segments"] = segments
            invalids.append(raw)
        for owner, key, value in (("top", "emotion", "字" * 25), ("top", "observation", "字" * 301),
                                  ("segment", "instruction", "字" * 161), ("segment", "instruction", "")):
            raw = plan()
            (raw if owner == "top" else raw["segments"][0])[key] = value
            invalids.append(raw)
        for raw in invalids:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                schema.parse_long_speech_plan(raw)

    def test_action_count_single_s_hold_and_fixed_intervals_are_strict(self):
        invalids = [plan(0)["segments"][0]["actions"], plan(7)["segments"][0]["actions"], None, {}, "S"]
        for position in (0, 3):
            for change in ({"kind": "L", "hold_seconds": 2, "motions": [{"part": "torso", "description": "前倾"}]},
                           {"kind": "Q"}, {"hold_seconds": 1}, {"hold_seconds": False},
                           {"motions": []}, {"motions": [{"part": "head", "description": "点头"},
                                                        {"part": "right_arm", "description": "挥手"}]}):
                actions = plan()["segments"][0]["actions"]
                actions[position].update(change)
                invalids.append(actions)
        for position in (0, 1, 3):
            values = (1, False, "0", float("nan")) if position == 0 else (
                0, .5, 2.5, 3.00001, 30, 31, -1, True, "3", float("nan"), float("inf"))
            for delay in values:
                actions = plan()["segments"][0]["actions"]
                actions[position]["delay_seconds"] = delay
                invalids.append(actions)
        for actions in invalids:
            with self.subTest(actions=actions):
                with self.assertRaises(ValueError):
                    schema.compile_long_speech_timeline(actions, plan()["segments"][0]["recovery"])
                with self.assertRaises(ValueError):
                    schema.long_speech_required_audio_seconds(actions)

    def test_regular_motion_gaze_recovery_validation_is_not_bypassed(self):
        for key, value in (("extra", "forbidden"), ("gaze", "字" * 41), ("expression", ""),
                           ("motions", [{"part": "whole_body", "description": "站起"}]),
                           ("motions", [{"part": "head", "description": ""}]),
                           ("motions", [{"part": "head", "description": "点头", "extra": "x"}]),
                           ("motions", [{"part": "invalid", "description": "点头"}])):
            raw = plan()
            raw["segments"][0]["actions"][2][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                schema.parse_long_speech_plan(raw)
        for recovery in (None, {}, {"gaze": "自然", "expression": "平静", "extra": "x"},
                         {"gaze": "", "expression": "平静"}):
            raw = plan()
            raw["segments"][0]["recovery"] = recovery
            with self.subTest(recovery=recovery), self.assertRaises(ValueError):
                schema.parse_long_speech_plan(raw)

    def test_continuous_wording_keeps_fixed_long_trial_timing(self):
        raw = plan()
        part = raw["segments"][0]
        baseline = schema.compile_long_speech_timeline(part["actions"], part["recovery"])
        action = part["actions"][2]
        action["motions"][0]["description"] = "点头然后摇头"
        action.update(gaze="看向镜头接着左看", expression="先微笑；随后平静")
        part["recovery"]["gaze"] = "先看左侧然后看回镜头"
        parsed = schema.parse_long_speech_plan(raw)["segments"][0]
        timeline = schema.compile_long_speech_timeline(parsed["actions"], parsed["recovery"])
        self.assertEqual([(n["role"], n["at_seconds"]) for n in timeline],
                         [(n["role"], n["at_seconds"]) for n in baseline])
        instructions = "\n".join(n["instruction"] for n in timeline)
        for text in (action["motions"][0]["description"], action["gaze"], action["expression"], part["recovery"]["gaze"]):
            self.assertIn(text, instructions)

    def test_input_mode_only_manual_viewer_and_once_with_long_error(self):
        for item in ({}, {"input_kind": "viewer", "source": None},
                     {"input_kind": "operator", "operator_mode": "once"},
                     {"input_kind": "operator", "operator_mode": "once", "source": "operator"}):
            self.assertEqual(schema.long_speech_input_mode(item), "speech")
        for item in (None, [], {"source": "manual"}, {"source": "douyin"}, {"source": "operator"},
                     {"input_kind": "operator", "operator_mode": "topic"},
                     {"input_kind": "operator", "operator_mode": "motion"},
                     {"input_kind": "operator", "operator_mode": "once", "source": "douyin"},
                     {"input_kind": "reading"}, {"input_kind": "unknown"}):
            with self.subTest(item=item), self.assertRaisesRegex(ValueError, "长文语音试验仅开放"):
                schema.long_speech_input_mode(item)

    def test_normal_short_continuous_contracts_keep_their_original_limits(self):
        with self.assertRaisesRegex(ValueError, "1至30字"):
            schema.parse_plan(plan(text="字" * 60))
        short = schema.parse_plan(copy.deepcopy(SHORT_PERFORMANCE_EXAMPLE))
        schema.validate_short_performance_plan(short)
        self.assertEqual([n["role"] for n in short["segments"][0]["timeline"]], ["GE", "S", "Q"])
        continuous = schema.parse_plan(copy.deepcopy(CONTINUOUS_SPEECH_EXAMPLE))
        schema.validate_continuous_speech_plan(continuous)
        self.assertEqual([n["at_seconds"] for n in continuous["segments"][0]["timeline"]], [0, .5, 2, 3, 3.5, 5])
        with self.assertRaises(ValueError):
            schema.validate_continuous_speech_plan(schema.parse_plan(plan(text="短句。")))


if __name__ == "__main__":
    unittest.main()
