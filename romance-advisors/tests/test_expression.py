import copy
import unittest

from romance.expression import ExpressionValidationError, REACTION_TTS_STYLE, build_plan, parse_ambient_result


def expressive():
    return {"emotion": "着急又忍笑", "actions": [{
        "anchor": {"quote": "认真", "occurrence": 1},
        "motions": [{"part": "face", "description": "眉毛明显抬起"},
                    {"part": "torso", "description": "上身稍向前倾"}],
        "gaze": "看向镜头", "expression": "眼睛睁大，嘴角忍笑"}],
        "recovery": {"gaze": "仍看镜头", "expression": "保留一点着急的笑意"}}


class ExpressionTests(unittest.TestCase):
    def test_joint_plan_keeps_expression_recovery_and_final_voice_cue(self):
        expression = expressive()
        instruction = REACTION_TTS_STYLE + "前半句难以置信，随后带笑求助。"
        plan = build_plan("这回我认真了，你们救救我。", instruction, expression)
        self.assertIsNone(plan["reading"])
        self.assertEqual(plan["segments"][0]["instruction"], instruction)
        self.assertEqual(plan["segments"][0]["recovery"]["expression"], "保留一点着急的笑意")
        expression["actions"][0]["motions"][0]["description"] = "改掉"
        self.assertEqual(plan["segments"][0]["actions"][0]["motions"][0]["description"], "眉毛明显抬起")

    def test_no_motion_is_valid_but_no_extra_recovery_frame(self):
        expression = {"emotion": "心虚", "actions": [], "recovery": None}
        self.assertEqual(build_plan("有点紧张。", "自然停顿。", expression)["segments"][0]["actions"], [])
        expression["recovery"] = {"gaze": "镜头", "expression": "平静"}
        with self.assertRaisesRegex(ValueError, "invalid_reaction_expression"):
            build_plan("有点紧张。", "自然停顿。", expression)

    def test_ssml_is_optional_and_preserves_original_text_and_anchor(self):
        text = "这回我认真了，你们救救我。"
        ssml = ' <speak rate="0.95">这回我认真了，<break time="200ms"/>你们救救我。</speak> '
        plan = build_plan(text, "着急又忍笑。", expressive(), ssml=ssml)
        part = plan["segments"][0]
        self.assertEqual(part["ssml"], ssml)  # Preserve the exact frozen markup.
        self.assertEqual(part["text"], text)
        self.assertEqual(part["actions"][0]["anchor"], {"quote": "认真", "occurrence": 1})
        self.assertNotIn("ssml", build_plan(text, "着急。", expressive())["segments"][0])
        self.assertNotIn("ssml", build_plan(text, "着急。", expressive(), ssml=None)["segments"][0])

    def test_ssml_envelope_uses_utf8_byte_limit(self):
        expression = {"emotion": "认真", "actions": [], "recovery": None}
        base = "<speak>认真点。</speak>"
        bounded = "<speak>" + " " * (8192 - len(base.encode("utf-8"))) + "认真点。</speak>"
        self.assertEqual(len(bounded.encode("utf-8")), 8192)
        self.assertEqual(build_plan("认真点。", "认真地说。", expression, ssml=bounded)["segments"][0]["ssml"], bounded)
        for ssml in ("", " \n ", 1, [], bounded + " ", "汉" * 2800, "\ud800"):
            with self.subTest(ssml_type=type(ssml).__name__), self.assertRaisesRegex(ValueError, "invalid_reaction_expression"):
                build_plan("认真点。", "认真地说。", expression, ssml=ssml)

    def test_bad_anchor_resource_conflict_and_invalid_shapes_rejected(self):
        cases = []
        bad = expressive(); bad["actions"][0]["anchor"]["quote"] = "不存在"; cases.append(bad)
        bad = expressive(); bad["actions"][0]["anchor"]["occurrence"] = True; cases.append(bad)
        bad = expressive(); bad["actions"][0]["anchor"]["occurrence"] = 2; cases.append(bad)
        bad = expressive(); bad["actions"][0]["motions"][0]["part"] = "whole_body"; cases.append(bad)
        bad = expressive(); bad["actions"][0]["motions"] = [{"part": "both_arms", "description": "举起双臂"}, {"part": "left_arm", "description": "放下左手"}]; cases.append(bad)
        bad = expressive(); bad["actions"] *= 2; cases.append(bad)
        bad = expressive(); bad["recovery"] = None; cases.append(bad)
        for expression in [None, {}, *cases]:
            with self.subTest(expression=expression), self.assertRaisesRegex(ValueError, "invalid_reaction_expression"):
                build_plan("认真点。", "认真地说。", expression)

    def test_literal_occurrence_is_translated_to_existing_bridge_numbering(self):
        expression = expressive()
        plan = build_plan("认，真点，认真点。", "认真地说。", expression)
        self.assertEqual(plan["segments"][0]["actions"][0]["anchor"]["occurrence"], 2)
        self.assertEqual(expression["actions"][0]["anchor"]["occurrence"], 1)
        expression["actions"][0]["anchor"]["occurrence"] = 2
        with self.assertRaisesRegex(ValueError, "invalid_reaction_expression"):
            build_plan("认，真点，认真点。", "认真地说。", expression)
        plan = build_plan("认，真点，认真点，认真些。", "认真地说。", expression)
        self.assertEqual(plan["segments"][0]["actions"][0]["anchor"]["occurrence"], 3)

    def test_ambient_silence_and_current_comment_ids_only(self):
        self.assertIsNone(parse_ambient_result({"speak": False}, {"c1"}))
        output = {"speak": True, "text": "那我认真等你们的主意。", "instruction": "带着期待自然地说。",
                  "expression": expressive(), "intent": "求助", "comment_ids": ["c1"]}
        parsed = parse_ambient_result(output, {"c1"})
        self.assertEqual(parsed["comment_ids"], ["c1"])
        self.assertTrue(parsed["plan"]["segments"][0]["instruction"].startswith(REACTION_TTS_STYLE))
        ssml = '<speak>那我认真<break time="150ms"/>等你们的主意。</speak>'
        parsed = parse_ambient_result(dict(output, ssml=ssml), {"c1"})
        self.assertEqual(parsed["plan"]["segments"][0]["ssml"], ssml)
        self.assertNotIn("ssml", parsed)
        for updates in ({"comment_ids": ["forged"]}, {"comment_ids": ["c1", "c1"]},
                        {"text": "字" * 61}, {"speak": 1}, {"ssml": " "}):
            with self.subTest(updates=updates), self.assertRaisesRegex(ValueError, "invalid_ambient_result"):
                parse_ambient_result(dict(copy.deepcopy(output), **updates), {"c1"})

    def test_safe_diagnostics_keep_field_and_reason_without_model_content(self):
        output = {"speak": True, "text": "认真点。", "instruction": "认真地说。",
                  "expression": expressive(), "intent": "求助", "comment_ids": []}
        cases = [({"text": 1}, "text", "expected_string", {"actual_type": "int"}),
                 ({"text": "隐私" * 31}, "text", "text_too_long", {"length": 62, "max_length": 60}),
                 ({"comment_ids": ["SECRET-forged-id"]}, "comment_ids[0]", "unknown_comment_id", {}),
                 ({"ssml": "\ud800"}, "ssml", "invalid_utf8", {})]
        for updates, field, code, details in cases:
            with self.subTest(field=field, code=code), self.assertRaises(ExpressionValidationError) as caught:
                parse_ambient_result(dict(output, **updates), [])
            error = caught.exception
            self.assertEqual(str(error), "invalid_ambient_result")
            self.assertEqual(error.diagnostic(), {"field": field, "code": code, "details": details})
            self.assertNotIn("SECRET", repr(error.diagnostic()))
            self.assertNotIn("隐私", repr(error.diagnostic()))
        output["expression"]["actions"][0]["motions"][1]["part"] = "face"
        with self.assertRaises(ExpressionValidationError) as caught:
            parse_ambient_result(output, [])
        self.assertEqual(caught.exception.field, "expression.actions[0].motions[1].part")
        self.assertEqual(caught.exception.code, "resource_conflict")

    def test_extra_metadata_is_ignored_and_whitespace_normalized_without_losing_actions(self):
        expression = expressive()
        canonical = build_plan("认真点。", REACTION_TTS_STYLE + "认真地说。", expression)
        expression["extra"] = "SECRET-unused"
        group = expression["actions"][0]
        group.update(extra="unused", at_seconds=99)
        group["anchor"].update(quote=" 认真 ", extra="unused")
        group["motions"][0].update(part=" face ", description=" 眉毛明显抬起 ", extra="unused")
        expression["recovery"]["extra"] = "unused"
        output = {"speak": True, "text": " 认真点。 ", "instruction": " 认真地说。 ",
                  "expression": expression, "intent": " 求助 ", "comment_ids": [" c1 "],
                  "extra": "SECRET-unused"}
        before = copy.deepcopy(output)
        parsed = parse_ambient_result(output, ["c1"])
        self.assertEqual(parsed["plan"], canonical)
        self.assertEqual(parsed["comment_ids"], ["c1"])
        self.assertEqual(output, before)
        self.assertNotIn("SECRET", repr(parsed))
        self.assertIsNone(parse_ambient_result({"speak": False, "extra": "unused"}, []))

    def test_individually_bounded_action_fields_have_no_hidden_total_cap(self):
        expression = expressive()
        group = expression["actions"][0]
        group["motions"][0]["description"] = "抬" * 70
        group["motions"][1]["description"] = "倾" * 70
        group.update(gaze="看" * 40, expression="笑" * 40)
        part = build_plan("认真点。", "认真地说。", expression)["segments"][0]
        self.assertEqual(part["actions"], expression["actions"])
        group["motions"][0]["description"] += "超"
        with self.assertRaises(ExpressionValidationError) as caught:
            build_plan("认真点。", "认真地说。", expression)
        self.assertEqual(str(caught.exception), "invalid_reaction_expression")
        self.assertEqual(caught.exception.field, "expression.actions[0].motions[0].description")
        self.assertEqual(caught.exception.code, "text_too_long")


if __name__ == "__main__":
    unittest.main()
