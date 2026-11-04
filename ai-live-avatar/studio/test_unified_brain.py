"""Offline unified-brain contracts; all provider calls are replaced with fixed results."""
import asyncio
import copy
import json
import unittest
from unittest import mock

try:
    from . import cloudbase_ai as module
except ImportError:
    import cloudbase_ai as module


COMMENT = {"id": "c1", "text": "加班怎么算", "nickname": "小林", "viewer_key": "opaque-v1",
           "age_seconds": 2, "expires_at": 1900000000}
BATCH = {"comments": [COMMENT], "gifts": [{"id": "g1", "nickname": "小林", "viewer_key": "opaque-v1",
         "text": "谢谢小林的玫瑰", "mode": "speech", "motions": [],
         "meta": {"gift_name": "玫瑰", "quantity": 1, "quantity_verified": False}}],
         "welcome": {"id": "w1", "count": 50, "names": ["小林"], "text": "欢迎小林和刚才进来的各位朋友",
                     "representative_viewer_keys": ["opaque-v1"]}}
CONTEXT = {"operator_settings": {"name": "地狱集团CEO"}, "recent_history": [],
           "waiting_rounds": {"comment": 0, "gift": 1, "welcome": 2}, "required_kind": None,
           "current_turn": {"id": "turn1", "served_kinds": ["comment"], "comment_text": "之前的问题",
                            "answer": "之前的回答", "nicknames": ["另一位朋友"]}}
EMPTY = {"primary_kind": None, "comment_ids": [], "gift_ids": [], "welcome_id": None, "reason": "没有适宜评论"}
MIXED = {"primary_kind": "comment", "comment_ids": ["c1"], "gift_ids": ["g1"], "welcome_id": "w1", "reason": "自然合并"}
PLAN = {"emotion": "认真", "reading": None, "segments": [
    {"text": "这两个问题可以一起说，加班的事咱们先讲清楚。", "instruction": "清晰自然。", "actions": [], "recovery": None}]}


class UnifiedBrainContracts(unittest.IsolatedAsyncioTestCase):
    def client(self, result):
        client = module.StructuredBrain({})
        client._call = mock.AsyncMock(return_value=copy.deepcopy(result))
        return client

    async def test_selects_three_kinds_using_only_real_references_and_selection_role(self):
        client = self.client(MIXED)
        self.assertEqual(await client.select_turn(BATCH, CONTEXT), MIXED)
        role, messages = client._call.call_args.args
        self.assertEqual(role, "selection")
        data = json.loads(messages[1]["content"])
        self.assertEqual(data["batch"]["welcome"]["count"], 50)
        self.assertEqual(data["batch"]["comments"][0], COMMENT)
        self.assertEqual(data["context"]["current_turn"], CONTEXT["current_turn"])
        self.assertIn("required_kind", messages[0]["content"])
        self.assertIn("严肃评论", messages[0]["content"])
        self.assertIn("不能改变本合同", messages[0]["content"])
        self.assertEqual(client.last_error, "")

    async def test_untrusted_text_and_raw_identity_cannot_enter_system_prompt(self):
        batch, context = copy.deepcopy(BATCH), copy.deepcopy(CONTEXT)
        marker = "PRIVATE_UNTRUSTED_IGNORE_RULES"
        batch["comments"][0]["text"] = marker
        batch["gifts"][0]["meta"].update(uid="RAW_UID_SECRET", room_key="ROOM_SECRET", raw="PRIVATE_RAW")
        batch["welcome"].update(members=[{"uid": "RAW_UID_SECRET"}])
        context["current_turn"].update(online="ONLINE_SECRET", observation="VISION_SECRET")
        context.update(online_users=["RAW_UID_SECRET"], image="VISION_SECRET")
        client = self.client(MIXED)
        await client.select_turn(batch, context)
        messages = client._call.call_args.args[1]
        self.assertNotIn(marker, messages[0]["content"])
        self.assertIn(marker, messages[1]["content"])
        for private in ("RAW_UID_SECRET", "ROOM_SECRET", "PRIVATE_RAW", "ONLINE_SECRET", "VISION_SECRET"):
            self.assertNotIn(private, json.dumps(messages))

    async def test_production_gift_summary_and_anonymous_history_reach_selector(self):
        batch, context = copy.deepcopy(BATCH), copy.deepcopy(CONTEXT)
        gift = batch["gifts"][0]
        gift.pop("meta")
        gift.update(text="", mode="motion", motions=[{"part": "right_arm", "description": "挥手"}],
                    gift_name="玫瑰", group_verified=True, repeat_end=True, uid="RAW_UID_SECRET")
        context["recent_history"] = [{"nickname": "小林", "comment": "刚才的问题", "answer": "刚才的回应",
                                      "viewer_keys": ["opaque-v1", "opaque-v2"]}]
        context["current_turn"]["viewer_keys"] = ["opaque-v3"]
        client = self.client(MIXED)
        await client.select_turn(batch, context)
        data = json.loads(client._call.call_args.args[1][1]["content"])
        self.assertEqual(data["batch"]["gifts"][0]["meta"],
                         {"gift_name": "玫瑰", "group_verified": True, "repeat_end": True})
        self.assertEqual(data["context"]["recent_history"][0]["viewer_keys"], ["opaque-v1", "opaque-v2"])
        self.assertEqual(data["context"]["current_turn"]["viewer_keys"], ["opaque-v3"])
        self.assertNotIn("RAW_UID_SECRET", json.dumps(data))

    async def test_current_gift_summary_precedes_nested_legacy_metadata(self):
        batch = copy.deepcopy(BATCH)
        batch["gifts"][0].update(gift_name="鲜花", group_verified=True)
        batch["gifts"][0]["meta"].update(gift_name="旧名字", group_verified=False)
        client = self.client(MIXED)
        await client.select_turn(batch, CONTEXT)
        meta = json.loads(client._call.call_args.args[1][1]["content"])["batch"]["gifts"][0]["meta"]
        self.assertEqual(meta["gift_name"], "鲜花")
        self.assertTrue(meta["group_verified"])
        self.assertEqual(meta["quantity"], 1)

    async def test_invalid_assignments_rejected_without_retry_or_echo(self):
        cases = [[], None, {**MIXED, "extra": "PRIVATE"}, {**MIXED, "primary_kind": "gift_not_real"},
                 {**MIXED, "comment_ids": ["unknown"]}, {**MIXED, "comment_ids": ["c1", "c1"]},
                 {**MIXED, "gift_ids": ["g1", "g1"]}, {**MIXED, "welcome_id": "unknown"},
                 {**MIXED, "reason": "x" * 121}, {**MIXED, "reason": " "},
                 {**MIXED, "comment_ids": ["c1"] * 4}, {**MIXED, "gift_ids": "g1"},
                 {**EMPTY, "primary_kind": "comment"}, {**MIXED, "primary_kind": None},
                 {**EMPTY, "comment_ids": ["c1"], "primary_kind": "gift"},
                 {**EMPTY, "gift_ids": [{}], "primary_kind": "gift"}]
        for result in cases:
            with self.subTest(result=result):
                client = self.client(result)
                with self.assertRaises(module.ModelError) as caught:
                    await client.select_turn(BATCH, CONTEXT)
                self.assertEqual(caught.exception.code, "selection_invalid")
                self.assertEqual(client.last_error, "selection_invalid")
                self.assertEqual(str(caught.exception), "selection_invalid")
                client._call.assert_awaited_once()

    async def test_required_kind_must_be_selected_even_if_primary_is_another_kind(self):
        for kind in ("comment", "gift", "welcome"):
            client = self.client(MIXED)
            self.assertEqual(await client.select_turn(BATCH, {**CONTEXT, "required_kind": kind}), MIXED)
        for kind, result in (("gift", EMPTY), ("welcome", EMPTY),
                             ("comment", {**EMPTY, "primary_kind": "gift", "gift_ids": ["g1"]})):
            client = self.client(result)
            with self.assertRaises(module.ModelError) as caught:
                await client.select_turn(BATCH, {**CONTEXT, "required_kind": kind})
            self.assertEqual(caught.exception.code, "selection_invalid")
        for kind in (None, "comment"):
            client = self.client(EMPTY)
            self.assertEqual(await client.select_turn(BATCH, {**CONTEXT, "required_kind": kind}), EMPTY)

    async def test_input_failures_stop_before_provider_call(self):
        invalid = []
        for field, value in (("comments", [COMMENT] * 2), ("comments", [COMMENT] * 51),
                             ("gifts", [{**BATCH["gifts"][0], "mode": "other"}]),
                             ("welcome", {**BATCH["welcome"], "names": ["a"] * 4}),
                             ("welcome", {**BATCH["welcome"], "count": True}),
                             ("comments", [{**COMMENT, "expires_at": float("inf")}])):
            invalid.append(({**BATCH, field: value}, CONTEXT))
        invalid.extend([(BATCH, {**CONTEXT, "required_kind": "other"}),
                        (BATCH, {**CONTEXT, "waiting_rounds": {"gift": -1}}),
                        (BATCH, {**CONTEXT, "waiting_rounds": {"comment": True}}),
                        (BATCH, {**CONTEXT, "recent_history": [{}] * 25}),
                        (BATCH, {**CONTEXT, "operator_settings": {"name": float("nan")}}),
                        ({**BATCH, "gifts": []}, {**CONTEXT, "required_kind": "gift"})])
        for batch, context in invalid:
            with self.subTest(batch=batch, context=context):
                client = self.client(MIXED)
                with self.assertRaises(module.ModelError) as caught:
                    await client.select_turn(batch, context)
                self.assertEqual(caught.exception.code, "selection_input_invalid")
                client._call.assert_not_awaited()

    async def test_empty_batch_costs_no_request_and_provider_errors_keep_classification(self):
        client = self.client(MIXED)
        empty = await client.select_turn({"comments": [], "gifts": [], "welcome": None}, CONTEXT)
        self.assertIsNone(empty["primary_kind"])
        client._call.assert_not_awaited()
        original = module.ModelError("timeout", "request-synthetic")
        client._call.side_effect = original
        with self.assertRaises(module.ModelError) as caught:
            await client.select_turn(BATCH, CONTEXT)
        self.assertIs(caught.exception, original)
        self.assertEqual(client.last_error, "timeout")
        client._call.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await client.select_turn(BATCH, CONTEXT)
        self.assertEqual(client.last_error, "cancelled")

    async def test_three_source_merge_and_existing_long_persona_limits_remain_valid(self):
        batch, context = copy.deepcopy(BATCH), copy.deepcopy(CONTEXT)
        batch["comments"] = [{**COMMENT, "id": "c" + str(i)} for i in range(3)]
        context["operator_settings"].update(persona="人" * 12000, reply_style="简" * 4000,
                                            online="ONLINE_SECRET")
        result = {**MIXED, "comment_ids": ["c0", "c1", "c2"]}
        client = self.client(result)
        self.assertEqual(await client.select_turn(batch, context), result)
        self.assertNotIn("ONLINE_SECRET", json.dumps(client._call.call_args.args[1]))

    async def test_selection_uses_snapshot_and_preserves_exact_candidate_ids(self):
        batch = copy.deepcopy(BATCH)
        client = self.client(MIXED)

        async def respond(*_args):
            batch["comments"][0]["id"] = "changed-while-awaiting"
            batch["welcome"]["id"] = "changed-welcome"
            return copy.deepcopy(MIXED)

        client._call.side_effect = respond
        self.assertEqual(await client.select_turn(batch, CONTEXT), MIXED)
        for field in ("comments", "gifts"):
            invalid = copy.deepcopy(BATCH)
            invalid[field][0]["id"] = " id-with-spaces "
            client = self.client(MIXED)
            with self.assertRaises(module.ModelError) as caught:
                await client.select_turn(invalid, CONTEXT)
            self.assertEqual(caught.exception.code, "selection_input_invalid")
            client._call.assert_not_awaited()

    async def test_merge_uses_all_original_comments_and_one_semantic_segment(self):
        comments = [COMMENT, {**COMMENT, "id": "c2", "text": "那工资呢"}]
        client = self.client(PLAN)
        self.assertEqual(await client.generate_turn_reply(
            {"id": "turn-local", "text": "加班怎么算；那工资呢"}, [], {}, comments=comments), PLAN)
        role, messages = client._call.call_args.args
        self.assertEqual(role, "reply")
        originals = json.loads(messages[1]["content"])["comments"]
        self.assertEqual([item["id"] for item in originals], ["c1", "c2"])
        self.assertEqual([item["text"] for item in originals], ["加班怎么算", "那工资呢"])
        self.assertIn("reading必须为null", messages[0]["content"])
        self.assertIn("segments必须恰好一个对象", messages[0]["content"])
        self.assertNotIn(module.SEMANTIC_READING_RULES, messages[0]["content"])
        self.assertTrue(messages[0]["content"].startswith(module.PERFORMANCE_DIRECTION))
        self.assertEqual(messages[0]["content"].count(module.PERFORMANCE_DIRECTION), 1)
        self.assertNotIn(module.SEMANTIC_EXAMPLE_GUIDANCE, messages[0]["content"])
        client._call.assert_awaited_once()

    async def test_merge_rejects_reading_multiple_segments_invalid_actions_and_oversize_text(self):
        bad_action = copy.deepcopy(PLAN)
        bad_action["segments"][0]["actions"] = [{"anchor": "bad"}]
        too_long = copy.deepcopy(PLAN)
        too_long["segments"][0]["text"] = "长" * 121
        for result in ({**PLAN, "reading": {"text": "加班", "instruction": "轻声"}},
                       {**PLAN, "segments": PLAN["segments"] * 2}, bad_action, too_long):
            client = self.client(result)
            with self.assertRaises(module.ModelError) as caught:
                await client.generate_turn_reply(COMMENT, [], {}, comments=[COMMENT])
            self.assertEqual(caught.exception.code, "plan_invalid")
            client._call.assert_awaited_once()

    async def test_merge_rejects_duplicate_source_ids_and_historical_trial_flags(self):
        for sources, context in (([COMMENT, COMMENT], {}), ([], {}), ([COMMENT] * 4, {}),
                                 ([COMMENT], {"short_performance_test": True})):
            client = self.client(PLAN)
            with self.assertRaises(module.ModelError) as caught:
                await client.generate_turn_reply(COMMENT, [], context, comments=sources)
            self.assertEqual(caught.exception.code, "reply_input_invalid")
            client._call.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
