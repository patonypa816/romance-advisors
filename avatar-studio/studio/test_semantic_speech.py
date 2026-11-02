"""Offline contracts and compilation, never a rendered-action acceptance test."""
import copy
import unittest

from semantic_speech import (SemanticSpeechError, compile_semantic_timeline,
                             parse_semantic_plan, validate_alignment, alignment_reason_message,
                             parse_dialogue_semantic_plan, parse_game_semantic_plan, compile_dialogue_semantic_timeline,
                             DIALOGUE_ACTION_KIND, DIALOGUE_GE_LEAD_SECONDS, DIALOGUE_S_TO_Q_SECONDS)
from action_schema import ACTION_POST_SEND_SECONDS


GE = {"gaze": "自然看向镜头", "expression": "神情平静"}


def group(quote="咱", occurrence=1):
    return {"anchor": {"quote": quote, "occurrence": occurrence},
            "motions": [{"part": "head", "description": "轻轻点头一次"}], **GE}


def plan(text="咱从草根起家，百废待兴。", actions=None, reading=None):
    actions = [group()] if actions is None else actions
    return {"emotion": "认真", "observation": "当前实际姿态未知", "reading": reading,
            "segments": [{"text": text, "instruction": "自然清晰，正常语速。", "actions": actions,
                          "recovery": dict(GE) if actions or reading else None}]}


def aligned(words):
    return {"status": "ok", "reason": None, "timebase": "audio_start", "granularity_ms": 10,
            "pcm_sha256": "fixed-pcm", "words": [
                {"text": text, "begin_index": i, "end_index": i + len(text),
                 "begin_time": begin, "end_time": end}
                for i, (text, begin, end) in enumerate(words)]}


class SemanticSpeechTests(unittest.TestCase):
    def assert_code(self, code, call):
        with self.assertRaises(SemanticSpeechError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        self.assertIsInstance(caught.exception.details, dict)
        return caught.exception

    def test_contract_120_chars_preserves_inputs_and_no_timeline(self):
        body = plan("咱" * 120)
        before = copy.deepcopy(body)
        parsed = parse_semantic_plan(body)
        self.assertEqual(parsed, body)
        self.assertEqual(body, before)
        self.assertNotIn("timeline", parsed["segments"][0])
        self.assertNotIn("mode", parsed)
        with self.assertRaises(ValueError):
            parse_semantic_plan(plan("咱" * 121))

    def test_dialogue_policy_rejects_whole_body_but_preserves_historical_parser_and_compiler(self):
        self.assertEqual((DIALOGUE_ACTION_KIND, DIALOGUE_GE_LEAD_SECONDS, DIALOGUE_S_TO_Q_SECONDS), ("S", 1.1, 1.5))
        body = plan()
        body["segments"][0]["actions"][0]["motions"] = [{"part": "whole_body", "description": "站起来"}]
        self.assertEqual(parse_semantic_plan(body), body)
        with self.assertRaisesRegex(ValueError, "A方案S短动作不支持whole_body"):
            parse_dialogue_semantic_plan(body)
        body = plan()
        parsed = parse_dialogue_semantic_plan(body)
        self.assertEqual(parsed, body)
        alignment = aligned([(body["segments"][0]["text"], 0, 7000)])
        historical = compile_semantic_timeline(parsed["segments"][0]["actions"], GE, alignment, 8)
        dialogue = compile_dialogue_semantic_timeline(parsed["segments"][0]["actions"], GE, alignment, 8)
        self.assertEqual([(n["role"], n["at_seconds"]) for n in historical], [("GE", -.5), ("L", 0), ("Q", 8)])
        self.assertEqual([(n["role"], n["at_seconds"]) for n in dialogue], [("GE", -1.1), ("S", 0), ("Q", 1.5)])
        for length in (121, 240):
            with self.subTest(length=length), self.assertRaises(ValueError):
                parse_dialogue_semantic_plan(plan("咱" * length))
        with self.assertRaises(SemanticSpeechError):
            compile_dialogue_semantic_timeline(parsed["segments"][0]["actions"], GE, alignment, 31)

    def test_observed_flat_anchor_layout_is_lossless_and_input_only(self):
        expected = plan()
        body = copy.deepcopy(expected)
        node = body["segments"][0]["actions"][0]
        node["occurrence"] = node["anchor"]["occurrence"]
        node["anchor"] = node["anchor"]["quote"]
        before = copy.deepcopy(body)
        for mode in (None, "once", "topic"):
            self.assertEqual(parse_semantic_plan(body, mode=mode), expected)
        self.assertEqual(body, before)
        with self.assertRaises(ValueError):
            compile_semantic_timeline(body["segments"][0]["actions"], GE, None, 10)

    def test_continuous_description_and_ge_keep_a_spacing_and_text(self):
        body = plan("咱讲开场，百姓接着听。", [group(), group("百姓")])
        first = body["segments"][0]["actions"][0]
        first["motions"][0]["description"] = "轻轻点头然后转向左侧"
        first.update(gaze="看向镜头接着看向左侧", expression="先微笑；随后神情平静")
        body["segments"][0]["recovery"] = {"gaze": "看向左侧然后看回镜头", "expression": "平静并且略带微笑"}
        before = copy.deepcopy(body)
        parsed = parse_dialogue_semantic_plan(body, mode="topic")["segments"][0]
        alignment = aligned([("咱讲开场，", 0, 3000), ("百姓接着听。", 3600, 7000)])
        timeline = compile_dialogue_semantic_timeline(parsed["actions"], parsed["recovery"], alignment, 8)
        self.assertEqual(body, before)
        self.assertEqual([n["role"] for n in timeline], ["GE", "S", "Q", "GE", "S", "Q"])
        for node, at in zip(timeline, [-1.1, 0, 1.5, 2.5, 3.6, 5.1]):
            self.assertAlmostEqual(node["at_seconds"], at)
        for field in ("gaze", "expression"):
            self.assertIn(first[field], timeline[0]["instruction"])
            self.assertIn(parsed["recovery"][field], timeline[2]["instruction"])
        self.assertIn(first["motions"][0]["description"], timeline[1]["instruction"])
        # 放宽描述不放宽Type9时序：同样文本把下一组提前1毫秒仍应拒绝。
        too_close = aligned([("咱讲开场，", 0, 3000), ("百姓接着听。", 3599, 7000)])
        self.assert_code("anchor_window_conflict", lambda: compile_dialogue_semantic_timeline(
            parsed["actions"], parsed["recovery"], too_close, 8))

    def test_dialogue_tail_q_after_speech_keeps_all_three_groups_and_historical_defaults(self):
        actions = [group("甲"), group("乙"), group("丙")]
        alignment = aligned([("甲", 0, 100), ("乙", 3600, 3700), ("丙", 7200, 7300)])
        before = copy.deepcopy((actions, alignment))
        nodes = compile_dialogue_semantic_timeline(actions, GE, alignment, 8)
        self.assertEqual([n["at_seconds"] for n in nodes if n["role"] == "S"], [0, 3.6, 7.2])
        self.assertEqual([n["at_seconds"] for n in nodes if n["role"] == "Q"], [1.5, 5.1, 8.7])
        self.assertEqual((actions, alignment), before)
        # Historical fixed replays keep the original within-audio tail window.
        for kind, lead in (("L", .5), ("S", .5), ("S", None), ("S", 1.1)):
            with self.subTest(kind=kind, lead=lead):
                self.assert_code("anchor_window_conflict", lambda: compile_semantic_timeline(
                    actions, GE, alignment, 8, action_kind=kind, ge_lead_seconds=lead))
        short_gap = copy.deepcopy(alignment)
        short_gap["words"][1]["begin_time"] = 3599
        self.assert_code("anchor_window_conflict", lambda: compile_dialogue_semantic_timeline(
            actions, GE, short_gap, 8))

    def test_dialogue_active_s_cannot_use_audio_end_zero_duration_word(self):
        alignment = aligned([("甲", 8000, 8000)])
        self.assert_code("anchor_window_conflict", lambda: compile_dialogue_semantic_timeline(
            [group("甲")], GE, alignment, 8))

    def test_after_audio_q_opt_in_cannot_change_l_or_fixed_minute_policy(self):
        for kind, lead, maximum, text_limit in (("L", .5, 30, 120), ("S", .5, 30, 120),
                                               ("S", None, 30, 120), ("S", 1.1, 60, 240)):
            with self.subTest(kind=kind, lead=lead, maximum=maximum), self.assertRaises(ValueError):
                compile_semantic_timeline([], None, None, 1, action_kind=kind,
                    ge_lead_seconds=lead, max_audio_seconds=maximum, max_text_codepoints=text_limit,
                    allow_tail_q_after_audio=True)

    def test_flat_anchor_never_drops_unknown_fields_defaults_or_value_checks(self):
        node = {**group(), "anchor": "咱", "occurrence": 1}
        bad = [{**node, **extra} for extra in ({"kind": "L"}, {"delay_seconds": 0},
                {"hold_seconds": 3}, {"extra": 1}, {"anchor": {"quote": "咱", "occurrence": 1}})]
        bad.append({key: value for key, value in node.items() if key != "occurrence"})
        bad += [{**node, "occurrence": value} for value in (True, 1.0, "1", 0, 2)]
        bad.append({**node, "anchor": "不存在"})
        bad.append({**node, "motions": [{"part": "head", "description": "点头"}] * 2})
        bad.append({**node, "motions": [{"part": "unknown", "description": "点头"}]})
        for item in bad:
            with self.subTest(item=item), self.assertRaises(ValueError):
                parse_semantic_plan(plan(actions=[item]))
        with self.assertRaises(ValueError):
            parse_semantic_plan(plan("百废，待兴。百废待兴。", [{**node, "anchor": "百废待兴"}]))

    def test_provider_spaces_preserve_letter_times_and_punctuation_boundary(self):
        alignment = aligned([("A", 400, 500), (" I。", 800, 1000)])
        original = copy.deepcopy(alignment)
        for quote, expected in (("AI", .4), ("A", .4), ("I", .8)):
            body = parse_semantic_plan(plan("AI。", [group(quote)]))
            nodes = compile_semantic_timeline(body["segments"][0]["actions"], GE, alignment, 5)
            self.assertEqual(nodes[1]["at_seconds"], expected)
        self.assertEqual(alignment, original)
        with self.assertRaises(ValueError):
            parse_semantic_plan(plan("A I。", [group("AI")]))
        self.assert_code("alignment_unavailable", lambda: compile_semantic_timeline(
            [group("AI")], GE, aligned([("A，", 0, 300), (" I。", 800, 1000)]), 5))
        error = self.assert_code("alignment_unavailable", lambda: compile_semantic_timeline(
            [group("I")], GE, aligned([("A I。", 400, 1000)]), 5))
        self.assertEqual(error.details["reason"], "anchor_inside_provider_word")

    def test_only_fixed_unavailable_reasons_reach_diagnostics(self):
        error = self.assert_code("alignment_unavailable", lambda: validate_alignment(
            {"status": "unavailable", "reason": "text_mismatch", "details": "private"}, "AI", 5))
        self.assertEqual(error.details, {"reason": "text_mismatch"})
        self.assertIn("文字", alignment_reason_message("text_mismatch"))
        for reason in ("https://invalid/secret-value", {"secret": "private"}):
            error = self.assert_code("alignment_unavailable", lambda: validate_alignment(
                {"status": "unavailable", "reason": reason}, "AI", 5))
            self.assertEqual(error.details, {"reason": "verified_audio_start_alignment_required"})
            self.assertNotIn("secret", alignment_reason_message(reason))

    def test_reading_remains_original_comment_substring_and_requires_recovery(self):
        reading = {"text": "粮价", "instruction": "自然轻短念出。"}
        body = plan(actions=[], reading=reading)
        self.assertEqual(parse_semantic_plan(body, source_text="最近粮价如何")["reading"], reading)
        self.assertEqual(compile_semantic_timeline([], GE, None, 1), [])
        for source in ("粮食如何",):
            with self.assertRaises(ValueError):
                parse_semantic_plan(body, source_text=source)
        for mode in ("once", "topic"):
            with self.assertRaises(ValueError):
                parse_semantic_plan(body, mode=mode)
        body["segments"][0]["recovery"] = None
        with self.assertRaises(ValueError):
            parse_semantic_plan(body)

    def test_pure_speech_needs_no_alignment_or_actions(self):
        body = plan(actions=[])
        for mode in (None, "once", "topic"):
            self.assertEqual(parse_semantic_plan(body, mode=mode), body)
        self.assertEqual(compile_semantic_timeline([], None, None, 2), [])
        with self.assertRaises(ValueError):
            parse_semantic_plan(body, mode="motion")

    def test_no_model_seconds_unknown_fields_or_punctuation_anchors(self):
        for extra in ({"kind": "L"}, {"delay_seconds": 0}, {"hold_seconds": 3}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                parse_semantic_plan(plan(actions=[{**group(), **extra}]))
        for quote in ("百废待兴。", "百 废", "不存在"):
            with self.subTest(quote=quote), self.assertRaises(ValueError):
                parse_semantic_plan(plan(actions=[group(quote)]))
        for occurrence in (0, True, 1.5, 2):
            with self.subTest(occurrence=occurrence), self.assertRaises(ValueError):
                parse_semantic_plan(plan(actions=[group("咱", occurrence)]))

    def test_one_or_two_compatible_motions(self):
        first = group()
        first["motions"].append({"part": "left_arm", "description": "左手轻轻向前摊开"})
        self.assertEqual(len(parse_semantic_plan(plan(actions=[first]))["segments"][0]["actions"][0]["motions"]), 2)
        for parts in (("head", "head"), ("both_arms", "left_arm"), ("whole_body", "head")):
            bad = group()
            bad["motions"] = [{"part": part, "description": "轻轻活动一次"} for part in parts]
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                parse_semantic_plan(plan(actions=[bad]))

    def test_accepted_sample_has_complete_two_group_timeline(self):
        actions = [group(), group("百废待兴")]
        alignment = aligned([("咱从草根起家，", 0, 6000), ("百废待兴。", 7360, 17360)])
        before = copy.deepcopy((actions, alignment))
        nodes = compile_semantic_timeline(actions, GE, alignment, 17.36)
        self.assertEqual([n["role"] for n in nodes], ["GE", "L", "Q", "GE", "L", "Q"])
        for actual, expected in zip([n["at_seconds"] for n in nodes], [-.5, 0, 3, 6.86, 7.36, 17.36]):
            self.assertAlmostEqual(actual, expected)
        self.assertEqual([n["index"] for n in nodes], list(range(1, 7)))
        self.assertEqual([n["hold_seconds"] for n in nodes if n["role"] == "L"], [3, 10])
        self.assertEqual((actions, alignment), before)
        self.assertTrue(all(n["gaze"] in n["instruction"] and n["expression"] in n["instruction"] for n in nodes))

    def test_repeated_anchor_occurrence_uses_normalized_full_script(self):
        body = plan("山河，山河。", [group("山河", 2)])
        parse_semantic_plan(body)
        alignment = aligned([("山河，", 0, 1000), ("山河。", 4000, 5000)])
        nodes = compile_semantic_timeline(body["segments"][0]["actions"], GE, alignment, 8)
        self.assertEqual(nodes[1]["at_seconds"], 4)

    def test_selected_occurrence_cannot_cross_punctuation(self):
        text = "百废，待兴。百废待兴。"
        with self.assertRaisesRegex(ValueError, "跨标点"):
            parse_semantic_plan(plan(text, [group("百废待兴", 1)]))
        parse_semantic_plan(plan(text, [group("百废待兴", 2)]))
        alignment = aligned([("百废，", 0, 1000), ("待兴。", 1500, 2500), ("百废待兴。", 4000, 5000)])
        error = self.assert_code("alignment_unavailable", lambda: compile_semantic_timeline(
                                [group("百废待兴", 1)], GE, alignment, 8))
        self.assertEqual(error.details["reason"], "anchor_not_contiguous")
        nodes = compile_semantic_timeline([group("百废待兴", 2)], GE, alignment, 8)
        self.assertEqual(nodes[1]["at_seconds"], 4)

    def test_unavailable_missing_and_word_interior_anchors_do_not_guess(self):
        self.assert_code("alignment_unavailable", lambda: compile_semantic_timeline([group()], GE, {"status": "unavailable"}, 4))
        error = self.assert_code("alignment_unavailable", lambda: compile_semantic_timeline([group("百废待兴")], GE,
                                aligned([("咱", 0, 1000)]), 5))
        self.assertEqual(error.details["reason"], "anchor_not_covered")
        error = self.assert_code("alignment_unavailable", lambda: compile_semantic_timeline([group("草根")], GE,
                                aligned([("咱从草根起家", 0, 3000)]), 5))
        self.assertEqual(error.details["reason"], "anchor_inside_provider_word")

    def test_alignment_binds_text_pcm_and_finite_times(self):
        alignment = aligned([("咱，", 0, 1000)])
        self.assertIs(validate_alignment(alignment, "咱。", 4, pcm_sha256="fixed-pcm"), alignment)
        for text, digest in (("他", "fixed-pcm"), ("咱", "different-pcm")):
            self.assert_code("alignment_unavailable", lambda: validate_alignment(alignment, text, 4, pcm_sha256=digest))
        for begin, end in ((True, 100), (-1, 100), (100, 99), (0, 5000)):
            bad = copy.deepcopy(alignment)
            bad["words"][0].update(begin_time=begin, end_time=end)
            self.assert_code("alignment_unavailable", lambda: validate_alignment(bad, "咱", 4))

    def test_nearby_reversed_and_short_tail_groups_rejected_without_mutation(self):
        for starts in ((0, 3000), (4000, 0), (0, 7360)):
            actions = [group(), group("百废待兴")]
            alignment = aligned([("咱", starts[0], starts[0] + 100), ("百废待兴", starts[1], starts[1] + 100)])
            seconds = 9 if starts == (0, 7360) else 10
            before = copy.deepcopy(actions)
            # Out-of-order provider timestamps are an alignment failure first.
            code = "alignment_unavailable" if starts == (4000, 0) else "anchor_window_conflict"
            self.assert_code(code, lambda: compile_semantic_timeline(actions, GE, alignment, seconds))
            self.assertEqual(actions, before)

    def test_post_q_exact_boundary_preserves_full_windows_and_anchors(self):
        nodes = compile_semantic_timeline([group(), group("百废待兴")], GE,
                  aligned([("咱", 0, 100), ("百废待兴", 4500, 5000)]), 7.5)
        self.assertEqual([node["at_seconds"] for node in nodes], [-.5, 0, 3, 4, 4.5, 7.5])
        self.assertEqual(nodes[3]["at_seconds"] - nodes[2]["at_seconds"], ACTION_POST_SEND_SECONDS)
        self.assertEqual(nodes[4]["at_seconds"] - nodes[3]["at_seconds"], .5)
        self.assertEqual(nodes[-1]["at_seconds"] - nodes[-2]["at_seconds"], 3)

    def test_post_q_point_999_and_old_zero_gap_are_rejected_without_repair(self):
        for second_l_ms in (3500, 4499):
            with self.subTest(second_l_ms=second_l_ms):
                actions = [group(), group("百废待兴")]
                alignment = aligned([("咱", 0, 100), ("百废待兴", second_l_ms, 5000)])
                before = copy.deepcopy((actions, alignment))
                error = self.assert_code("anchor_window_conflict", lambda: compile_semantic_timeline(
                    actions, GE, alignment, 8))
                self.assertEqual(error.details["q_at_seconds"], 3)
                self.assertEqual(error.details["next_ge_at_seconds"], second_l_ms / 1000 - .5)
                self.assertEqual(error.details["minimum_post_send_seconds"], 1)
                self.assertEqual((actions, alignment), before)

    def test_explicit_s_replay_keeps_anchors_but_returns_q_after_1_5_seconds(self):
        actions = [group(), group("百废待兴")]
        alignment = aligned([("咱", 80, 100), ("百废待兴", 6640, 7000)])
        before = copy.deepcopy((actions, alignment))
        nodes = compile_semantic_timeline(actions, GE, alignment, 10.4, action_kind="S")
        self.assertEqual([node["role"] for node in nodes], ["GE", "S", "Q"] * 2)
        self.assertEqual([round(node["at_seconds"], 2) for node in nodes],
                         [-.42, .08, 1.58, 6.14, 6.64, 8.14])
        self.assertTrue(all(node["hold_seconds"] == 0 for node in nodes))
        self.assertEqual((actions, alignment), before)
        default = compile_semantic_timeline(actions, GE, alignment, 10.4)
        explicit_l = compile_semantic_timeline(actions, GE, alignment, 10.4, action_kind="L")
        self.assertEqual(default, explicit_l)
        self.assertEqual(default[-1]["at_seconds"], 10.4)
        self.assertEqual([node["role"] for node in default], ["GE", "L", "Q"] * 2)

    def test_s_replay_short_tail_and_sub_one_second_post_q_gap_reject_without_repair(self):
        cases = [([group()], aligned([("咱", 3000, 3100)]), 4.499),
                 ([group(), group("百废待兴")],
                  aligned([("咱", 0, 100), ("百废待兴", 2999, 3100)]), 5)]
        for actions, alignment, seconds in cases:
            before = copy.deepcopy((actions, alignment))
            self.assert_code("anchor_window_conflict", lambda: compile_semantic_timeline(
                actions, GE, alignment, seconds, action_kind="S"))
            self.assertEqual((actions, alignment), before)
        nodes = compile_semantic_timeline([group(), group("百废待兴")], GE,
                    aligned([("咱", 0, 100), ("百废待兴", 3000, 3100)]), 4.5, action_kind="S")
        self.assertEqual([node["at_seconds"] for node in nodes], [-.5, 0, 1.5, 2.5, 3, 4.5])

    def test_action_kind_rejects_unknown_values_even_without_actions(self):
        for kind in ("s", "Q", None, True, [], {}):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                compile_semantic_timeline([], None, None, 1, action_kind=kind)
        whole = group()
        whole["motions"][0]["part"] = "whole_body"
        with self.assertRaisesRegex(ValueError, "whole_body"):
            compile_semantic_timeline([whole], GE, aligned([("咱", 0, 100)]), 2, action_kind="S")

    def test_ge_ab_variants_keep_s_q_positions_and_embedded_gaze_expression(self):
        actions = [group(), group("百废待兴")]
        alignment = aligned([("咱", 80, 100), ("百废待兴", 6640, 7000)])
        expected = {1.1: (["GE", "S", "Q"] * 2, [-1.02, .08, 1.58, 5.54, 6.64, 8.14]),
                    None: (["S", "Q"] * 2, [.08, 1.58, 6.64, 8.14])}
        default = compile_semantic_timeline(actions, GE, alignment, 10.4, action_kind="S")
        for lead, (roles, times) in expected.items():
            nodes = compile_semantic_timeline(actions, GE, alignment, 10.4,
                                               action_kind="S", ge_lead_seconds=lead)
            self.assertEqual([n["role"] for n in nodes], roles)
            self.assertEqual([round(n["at_seconds"], 2) for n in nodes], times)
            active = [n for n in nodes if n["role"] == "S"]
            self.assertEqual([n["instruction"] for n in active],
                             [n["instruction"] for n in default if n["role"] == "S"])
            for node in active:
                self.assertIn("眼神：自然看向镜头", node["instruction"])
                self.assertIn("表情：神情平静", node["instruction"])
            for before, after in zip(nodes, nodes[1:]):
                self.assertAlmostEqual(after["delay_seconds"], after["at_seconds"]-before["at_seconds"])

    def test_ge_ab_variants_reject_short_gap_instead_of_moving_anchors(self):
        actions = [group(), group("百废待兴")]
        for lead, minimum_ms in ((1.1, 3600), (None, 2500)):
            for delta in (-1, 0):
                alignment = aligned([("咱", 0, 100), ("百废待兴", minimum_ms+delta, minimum_ms+100)])
                before = copy.deepcopy((actions, alignment))
                if delta:
                    self.assert_code("anchor_window_conflict", lambda: compile_semantic_timeline(
                        actions, GE, alignment, 6, action_kind="S", ge_lead_seconds=lead))
                else:
                    nodes = compile_semantic_timeline(actions, GE, alignment, 6,
                                                       action_kind="S", ge_lead_seconds=lead)
                    q = next(n for n in nodes if n["role"] == "Q")
                    entry = next(n for n in nodes if n["group_index"] == 2)
                    self.assertAlmostEqual(entry["at_seconds"] - q["at_seconds"], 1)
                self.assertEqual((actions, alignment), before)

    def test_ge_variant_validation_and_default_l_s_regression(self):
        for lead in (True, False, 0, 1, .6, "1.1", float("nan"), [], {}):
            with self.subTest(lead=lead), self.assertRaises(ValueError):
                compile_semantic_timeline([], None, None, 1, action_kind="S", ge_lead_seconds=lead)
        for lead in (None, 1.1):
            with self.subTest(lead=lead), self.assertRaises(ValueError):
                compile_semantic_timeline([], None, None, 1, ge_lead_seconds=lead)
        alignment = aligned([("咱", 0, 100)])
        for kind in ("L", "S"):
            self.assertEqual(compile_semantic_timeline([group()], GE, alignment, 4, action_kind=kind),
                             compile_semantic_timeline([group()], GE, alignment, 4,
                                                       action_kind=kind, ge_lead_seconds=.5))

    def test_six_group_ab_accepts_arbitrary_measured_anchors_through_exact_30_seconds(self):
        starts_ms = (80, 4271, 8666, 13137, 18531, 25777)
        actions = [group(char) for char in "甲乙丙丁戊己"]
        alignment = aligned([(char, begin, begin+50) for char, begin in zip("甲乙丙丁戊己", starts_ms)])
        for lead in (1.1, None):
            with self.subTest(lead=lead):
                nodes = compile_semantic_timeline(actions, GE, alignment, 30,
                                                   action_kind="S", ge_lead_seconds=lead)
                self.assertEqual(len(nodes), 18 if lead is not None else 12)
                self.assertEqual([n["at_seconds"] for n in nodes if n["role"] == "S"],
                                 [at / 1000 for at in starts_ms])
                self.assertEqual([n["at_seconds"] for n in nodes if n["role"] == "Q"],
                                 [at / 1000 + 1.5 for at in starts_ms])
                self.assert_code("alignment_unavailable", lambda: compile_semantic_timeline(
                    actions, GE, alignment, 30 + 1/24000, action_kind="S", ge_lead_seconds=lead))
        with self.assertRaises(ValueError):
            compile_semantic_timeline(actions + [group("庚")], GE, alignment, 30, action_kind="S")

    def test_minute_validation_is_explicit_and_normal_scope_stays_30_seconds_120_chars(self):
        starts_ms = (80, 9000, 18000, 28000, 40000, 53000)
        text = "".join(char * 30 for char in "甲乙丙丁戊己")
        actions = [group(char) for char in "甲乙丙丁戊己"]
        alignment = aligned([(char * 30, begin, begin+50) for char, begin in zip("甲乙丙丁戊己", starts_ms)])
        caps = {"max_audio_seconds": 60, "max_text_codepoints": 240}
        self.assertIs(validate_alignment(alignment, text, 57, pcm_sha256="fixed-pcm", **caps), alignment)
        nodes = compile_semantic_timeline(actions, GE, alignment, 57, action_kind="S", ge_lead_seconds=1.1, **caps)
        self.assertEqual([n["role"] for n in nodes], ["GE", "S", "Q"] * 6)
        self.assert_code("alignment_unavailable", lambda: validate_alignment(alignment, text, 57))
        self.assert_code("alignment_unavailable", lambda: validate_alignment(
            aligned([("字" * 121, 0, 100)]), "字" * 121, 1))
        self.assert_code("alignment_unavailable", lambda: validate_alignment(alignment, text, 60 + 1/24000, **caps))
        for change in ({"max_audio_seconds": 60}, {"max_text_codepoints": 240},
                       {"max_audio_seconds": True, "max_text_codepoints": 240},
                       {"max_audio_seconds": 61, "max_text_codepoints": 240}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_alignment(alignment, text, 57, **change)
        for mode in ({"action_kind": "L", "ge_lead_seconds": 1.1},
                     {"action_kind": "S", "ge_lead_seconds": None}, {"action_kind": "S"}):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                compile_semantic_timeline(actions, GE, alignment, 57, **mode, **caps)



class GameSemanticContract(unittest.TestCase):
    def test_game_shape_keeps_legacy_parser_separate(self):
        legacy = plan()
        game = {key: copy.deepcopy(legacy[key]) for key in ("emotion", "segments")}
        for mode in (None, "once", "topic"):
            self.assertEqual(parse_game_semantic_plan(game, mode=mode), game)
            with self.assertRaises(ValueError):
                parse_game_semantic_plan(legacy, mode=mode)
        self.assertEqual(parse_dialogue_semantic_plan(legacy), legacy)

    def test_game_length_recovery_and_actions_still_enforced(self):
        game = {key: copy.deepcopy(plan()[key]) for key in ("emotion", "segments")}
        game["segments"][0]["text"] = "咱" * 121
        with self.assertRaisesRegex(ValueError, "120"):
            parse_game_semantic_plan(game)
        game = {key: copy.deepcopy(plan()[key]) for key in ("emotion", "segments")}
        game["segments"][0]["recovery"] = None
        with self.assertRaises(ValueError):
            parse_game_semantic_plan(game)
        game["segments"][0].update(actions=[], recovery=None)
        self.assertEqual(parse_game_semantic_plan(game), game)

if __name__ == "__main__":
    unittest.main()
