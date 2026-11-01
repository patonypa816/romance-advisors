"""Finite story planning and conditional drafts; no credentials or model calls."""
import copy
import json
import unittest
from unittest.mock import AsyncMock

try:
    from . import cloudbase_ai as module
    from .bailian_ai import BailianAI
except ImportError:
    import cloudbase_ai as module
    from bailian_ai import BailianAI


JOB = {"input_kind": "operator", "operator_mode": "topic", "id": "topic-a",
       "text": "讲讲粮食为什么值得珍惜", "rounds": 3}
OUTLINE = [{"id": "cause", "title": "缘由", "goal": "从一次挨饿经历说明粮食来之不易。"},
           {"id": "choice", "title": "做法", "goal": "说明如何减少浪费，承接经历，不再重复开场。"},
           {"id": "ending", "title": "收束", "goal": "用珍惜眼前一餐自然收束故事，不扩展话题。"}]
PLAN = {"emotion": "温和", "observation": "当前实际姿态未知", "reading": None,
        "segments": [{"text": "吃多少盛多少，就是珍惜粮食最实在的办法。",
                      "instruction": "自然清楚，正常语速。", "actions": [], "recovery": None}]}
BRIDGE_PLAN = copy.deepcopy(PLAN)
BRIDGE_PLAN["segments"][0]["text"] = "好，咱们接着说。" + PLAN["segments"][0]["text"]


def topic(**updates):
    return {"id": JOB["id"], "instruction": JOB["text"], "total_rounds": 3, "completed_rounds": 1,
            "next_round": 2, "sent_history": ["咱也曾为一口饭发愁。"], "outline": copy.deepcopy(OUTLINE),
            "resume_required": False, "interaction_history": [], **updates}


class TopicGeneration(unittest.IsolatedAsyncioTestCase):
    def client(self, result=PLAN, provider=module.CloudBaseAI):
        client = provider({})
        client._call = AsyncMock(return_value=copy.deepcopy(result))
        return client

    async def generate(self, state, result=PLAN):
        client = self.client(result)
        answer = await client.generate_director({**JOB, "topic_round": state["next_round"]}, [], {}, state)
        return client, answer

    async def test_outline_uses_shared_metered_reply_contract_for_both_providers(self):
        for provider in (module.CloudBaseAI, BailianAI):
            client = self.client({"outline": OUTLINE}, provider)
            self.assertEqual(await client.generate_topic_outline(JOB, {"character": {"identity": "原创角色"}}), OUTLINE)
            role, messages = client._call.call_args.args
            self.assertEqual(role, "reply")
            self.assertIn(module.TOPIC_OUTLINE_PROMPT, messages[0]["content"])
            self.assertIn("没有工具权限", messages[0]["content"])
            self.assertEqual(json.loads(messages[1]["content"])["operator_job"]["rounds"], 3)
            self.assertEqual(client.last_error, "")
            client._call.assert_awaited_once()

    async def test_outline_rejects_wrong_count_duplicate_ids_extra_control_and_unbounded_text(self):
        malformed = [{"outline": OUTLINE[:-1]}, {"outline": OUTLINE, "completed_rounds": 3}]
        for change in ({"id": "cause"}, {"goal": "字" * 241}, {"title": ""}, {"send": True}):
            bad = copy.deepcopy(OUTLINE)
            bad[1].update(change)
            malformed.append({"outline": bad})
        for result in malformed:
            with self.subTest(result=result):
                client = self.client(result)
                with self.assertRaises(module.ModelError) as caught:
                    await client.generate_topic_outline(JOB, {})
                self.assertEqual(caught.exception.code, "topic_outline_invalid")
                self.assertNotIn(OUTLINE[0]["goal"], json.dumps(caught.exception.validation, ensure_ascii=False))
                client._call.assert_awaited_once()

    async def test_outline_invalid_request_never_calls_model(self):
        for changed in ({"rounds": True}, {"rounds": 0}, {"rounds": 61},
                        {"rounds": 3, "total_rounds": 2}, {"rounds": 1, "total_rounds": True},
                        {"operator_mode": "once"}):
            client = self.client({"outline": OUTLINE})
            with self.assertRaises(module.ModelError) as caught:
                await client.generate_topic_outline({**JOB, **changed}, {})
            self.assertEqual(caught.exception.code, "topic_outline_input_invalid")
            client._call.assert_not_awaited()

    async def test_pending_empty_outline_allows_real_viewer_and_selection_wrappers(self):
        pending = topic(completed_rounds=0, next_round=1, sent_history=[], outline=[])
        original = copy.deepcopy(pending)
        for provider in (module.CloudBaseAI, BailianAI):
            viewer = self.client(provider=provider)
            self.assertEqual(await viewer.generate_reply({"id": "viewer-1", "text": "今天天气怎样"}, [],
                                                        {"current_topic": pending}), PLAN)
            settings = viewer._call.call_args.args[1][0]["content"]
            self.assertIn(JOB["text"], settings)
            self.assertNotIn('"outline":[]', settings)
            viewer._call.assert_awaited_once()
            selection = self.client({"selected_id": "viewer-1", "reason": "回应观众的新问题"}, provider)
            self.assertEqual(await selection.select_comments(
                {"candidates": [{"id": "viewer-1", "text": "今天天气怎样"}]},
                {"current_topic": pending}), {"selected_id": "viewer-1", "reason": "回应观众的新问题"})
            data = json.loads(selection._call.call_args.args[1][1]["content"])
            self.assertNotIn("outline", data["context"]["current_topic"])
            selection._call.assert_awaited_once()
            # Lazy standby planning must also accept the same honest snapshot.
            outline = self.client({"outline": OUTLINE}, provider)
            self.assertEqual(await outline.generate_topic_outline(JOB, {"current_topic": pending}), OUTLINE)
        self.assertEqual(pending, original)

    async def test_active_empty_or_incomplete_outline_rejected_before_model_call(self):
        for outline in ([], OUTLINE[:1]):
            state = topic(completed_rounds=0, next_round=1, sent_history=[], outline=outline)
            client = self.client()
            with self.assertRaises(module.ModelError) as caught:
                await client.generate_director({**JOB, "topic_round": 1}, [], {}, state)
            self.assertEqual(caught.exception.code, "director_input_invalid")
            client._call.assert_not_awaited()

    async def test_invalid_partial_passive_outline_still_rejected(self):
        client = self.client()
        with self.assertRaises(module.ModelError) as caught:
            await client.generate_reply({"id": "viewer-1", "text": "你好"}, [],
                                        {"current_topic": topic(outline=OUTLINE[:1])})
        self.assertEqual(caught.exception.code, "reply_input_invalid")
        client._call.assert_not_awaited()

    async def test_conditional_draft_retains_true_bookmark_and_exact_predecessor(self):
        predecessor = {"id": "topic-a-round-2", "round": 2,
                       "text": "吃多少盛多少，才不糟蹋粮食。", "status": "conditional_draft"}
        state = topic(next_round=3, predecessor=predecessor)
        client, _ = await self.generate(state)
        data = json.loads(client._call.call_args.args[1][1]["content"])["topic"]
        self.assertEqual(data["completed_rounds"], 1)
        self.assertEqual(data["sent_history"], state["sent_history"])
        self.assertEqual(data["predecessor"], predecessor)
        self.assertEqual(data["next_unit"], OUTLINE[2])
        self.assertIn("绝非已经讲过", client._call.call_args.args[1][0]["content"])

    async def test_missing_malformed_or_late_predecessor_cannot_advance_context(self):
        predecessor = {"id": "draft-2", "round": 2, "text": "准备好的完整正文。", "status": "conditional_draft"}
        cases = [topic(next_round=3), topic(next_round=3, predecessor={**predecessor, "text": ""}),
                 topic(next_round=3, predecessor={**predecessor, "id": ""}),
                 topic(next_round=3, predecessor={**predecessor, "round": 1}),
                 topic(next_round=3, predecessor={**predecessor, "status": "sent"}),
                 topic(next_round=4, predecessor=predecessor),
                 topic(completed_rounds=2, next_round=3, predecessor=predecessor),
                 topic(next_round=2, predecessor={"id": "old", "round": 1, "text": "并未实际发送的旧稿", "status": "sent"})]
        no_outline = topic(next_round=3, predecessor=predecessor)
        no_outline.pop("outline")
        cases.append(no_outline)
        for state in cases:
            with self.subTest(state=state):
                client = self.client()
                with self.assertRaises(module.ModelError) as caught:
                    await client.generate_director({**JOB, "topic_round": state["next_round"]}, [], {}, state)
                self.assertEqual(caught.exception.code, "director_input_invalid")
                client._call.assert_not_awaited()

    async def test_resume_keeps_actual_interactions_separate_and_rewrites_complete_segment(self):
        interactions = [{"id": "comment-a", "text": "夏天吃什么", "answer": "夏天吃得清淡些，按自己胃口来。"}]
        state = topic(resume_required=True, interaction_history=interactions)
        client, result = await self.generate(state, BRIDGE_PLAN)
        messages = client._call.call_args.args[1]
        data = json.loads(messages[1]["content"])["topic"]
        self.assertEqual(data["interaction_history"], interactions)
        self.assertEqual(data["completed_rounds"], 1)
        self.assertEqual(len(data["sent_history"]), 1)
        for required in ("同一个segment", "不超过120字", "重新合成语音", "真实时间戳", "避免机械重讲", "不虚增书签"):
            self.assertIn(required, messages[0]["content"])
        self.assertEqual(result, BRIDGE_PLAN)

    async def test_topic_request_has_one_authoritative_context_despite_global_resume_or_stale_topic(self):
        predecessor = {"id": "actual-bridge", "round": 2, "text": BRIDGE_PLAN["segments"][0]["text"],
                       "status": "conditional_draft"}
        item = topic(next_round=3, predecessor=predecessor, resume_required=False)
        # One case is valid but contradictory; another cannot even be parsed as
        # a passive topic. Neither may override this item's trusted context.
        stale_contexts = [topic(id="GLOBAL-TOPIC-ONLY", instruction="GLOBAL-INSTRUCTION-ONLY", resume_required=True),
                          {"id": "STALE-MALFORMED-TOPIC", "resume_required": True}]
        history = [{"text": "最近实际发送的观众问题", "answer": "最近实际发送的回答"}]
        for provider in (module.CloudBaseAI, BailianAI):
            for stale in stale_contexts:
                with self.subTest(provider=provider.__name__, stale=stale["id"]):
                    context = {"character": {"identity": "原创角色"}, "current_topic": stale}
                    original = copy.deepcopy(context)
                    client = self.client(provider=provider)
                    job = {**JOB, "topic_round": 3}
                    self.assertEqual(await client.generate_director(job, history, context, item), PLAN)
                    role, messages = client._call.call_args.args
                    self.assertEqual(role, "reply")
                    trusted = json.loads(messages[0]["content"].split("\n可信角色与参考规划：\n", 1)[1])
                    self.assertEqual(trusted, {"character": {"identity": "原创角色"}})
                    self.assertNotIn("current_topic", json.dumps(messages, ensure_ascii=False))
                    self.assertNotIn(stale["id"], json.dumps(messages, ensure_ascii=False))
                    self.assertEqual(json.loads(messages[1]["content"]), {
                        "operator_job": module._director_job(job), "history": history,
                        "topic": module._topic_context(item, active=True)})
                    self.assertFalse(json.loads(messages[1]["content"])["topic"]["resume_required"])
                    self.assertEqual(context, original)
                    client._call.assert_awaited_once()

    async def test_viewer_selection_and_once_keep_passive_topic_context(self):
        passive = topic(resume_required=True)
        for provider in (module.CloudBaseAI, BailianAI):
            for route in ("viewer", "selection", "once"):
                with self.subTest(provider=provider.__name__, route=route):
                    context = {"character": {"identity": "原创角色"}, "current_topic": passive}
                    result = {"selected_id": "viewer-a", "reason": "回应观众"} if route == "selection" else PLAN
                    client = self.client(result, provider)
                    if route == "selection":
                        await client.select_comments({"candidates": [{"id": "viewer-a", "text": "你好"}]}, context)
                        data = json.loads(client._call.call_args.args[1][1]["content"])
                        transmitted = data["context"]["current_topic"]
                    else:
                        if route == "viewer":
                            await client.generate_reply({"id": "viewer-a", "text": "你好"}, [], context)
                            separator = "\n可信设定与合同：\n"
                        else:
                            await client.generate_director({**JOB, "operator_mode": "once"}, [], context)
                            separator = "\n可信角色与参考规划：\n"
                        system = client._call.call_args.args[1][0]["content"]
                        transmitted = json.loads(system.split(separator, 1)[1])["current_topic"]
                    self.assertEqual(transmitted, module._topic_context(passive))

    async def test_finite_last_unit_then_completed_context_rejected_without_extra_call(self):
        client, _ = await self.generate(topic(completed_rounds=2, next_round=3,
                                             sent_history=["先说前因。", "再说办法。 "]))
        data = json.loads(client._call.call_args.args[1][1]["content"])["topic"]
        self.assertEqual(data["next_unit"]["id"], "ending")
        self.assertIn("末轮自然收束", client._call.call_args.args[1][0]["content"])
        client = self.client()
        with self.assertRaises(module.ModelError):
            await client.generate_director({**JOB, "topic_round": 4}, [], {},
                                           topic(completed_rounds=3, next_round=4))
        client._call.assert_not_awaited()

    async def test_topic_rejects_two_segments_but_existing_viewer_and_once_keep_contract(self):
        result = copy.deepcopy(PLAN)
        result["segments"].append(copy.deepcopy(result["segments"][0]))
        with self.assertRaises(module.ModelError) as caught:
            await self.generate(topic(), result)
        self.assertEqual(caught.exception.code, "plan_invalid")
        once = self.client(result)
        self.assertEqual(await once.generate_director({**JOB, "operator_mode": "once"}, [], {}), result)
        viewer = self.client(result)
        self.assertEqual(await viewer.generate_reply({"id": "c", "text": "你好"}, [], {}), result)

    async def test_topic_segment_including_bridge_cannot_exceed_120_characters(self):
        result = copy.deepcopy(PLAN)
        result["segments"][0]["text"] = "好，咱们接着说。" + "字" * 120
        with self.assertRaises(module.ModelError):
            await self.generate(topic(resume_required=True), result)

    def test_full_story_history_retained_and_interaction_relevance_is_not_a_priority(self):
        state = topic(total_rounds=8, completed_rounds=7, next_round=8,
                      sent_history=[f"已经发出的第{i}段。" for i in range(7)],
                      outline=[{"id": str(i), "title": "小段", "goal": "讲清一个完整意思。"} for i in range(8)])
        self.assertEqual(module._topic_context(state)["sent_history"], state["sent_history"])
        self.assertNotIn("相关性用于同批优先排序", module.SELECTION_PROMPT)
        self.assertIn("不按相关性给评论排序", module.SELECTION_PROMPT)
        self.assertIn("不强行把回答拉回故事", module.SEMANTIC_REPLY_PROMPT)

    def test_unsent_story_cannot_be_added_to_confirmed_history(self):
        with self.assertRaises(ValueError):
            module._topic_context(topic(sent_history=["已发出。", "尚未发出。"], completed_rounds=1))


if __name__ == "__main__":
    unittest.main()
