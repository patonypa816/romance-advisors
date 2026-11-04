"""HTTP and scheduler integration checks with explicit synthetic model outputs."""
import asyncio
import copy
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from romance.server import DEFAULT_LAYOUT, Workbench, create_app, normalize_layout


class SyntheticModel:
    def __init__(self):
        self.calls = []

    async def generate(self, task, payload):
        self.calls.append((task, copy.deepcopy(payload)))
        if task == "prepare":
            character = {"name": "示例人物", "age": 28, "personality": "真诚", "background": "虚构测试", "speech_style": "简洁"}
            return {"title": "合成测试", "man": character, "woman": dict(character, private_preferences="仅女生知道的测试秘密"),
                    "shared_background": "共同参加读书会", "public_hints": ["测试人物"], "opening": "周六有空吗？"}
        if task == "select":
            return {"options": [{"proposal_id": payload["proposals"][0]["id"], "summary": "认真邀约", "conflict": False, "reason": "符合人设"}]} if payload["proposals"] else {"options": []}
        if task == "draft":
            selected = payload["selected_option_id"] if payload["selection_locked"] else next(iter(payload["allowed_option_ids"]), None)
            return {"selected_option_id": selected, "reaction": "我想认真回答。", "reaction_instruction": "稍有忐忑，语气认真，句尾自然收住。", "reaction_expression": {"emotion": "忐忑而认真", "actions": [], "recovery": None}, "message": "愿意周六一起去吗？"}
        if task == "reply":
            return {"message": "愿意和你交往。", "delta": 100 - payload["score"], "ending": "success", "reason": "合成终局"}


class LayoutGeometryTests(unittest.TestCase):
    def test_default_and_extreme_layouts_keep_avatar_and_phone_clear_of_dense_comments(self):
        self.assertEqual(normalize_layout({}), DEFAULT_LAYOUT)
        self.assertEqual(DEFAULT_LAYOUT, {"top": 336, "right": 48, "bottom": 312, "left": 48,
                                          "avatarX": 552, "avatarY": 360, "avatarWidth": 480})
        self.assertEqual(normalize_layout({"avatarWidth": 1080})["avatarWidth"], 480)
        cases = [{}, {key: -1000 for key in DEFAULT_LAYOUT}, {key: 10000 for key in DEFAULT_LAYOUT},
                 {"top": 420, "bottom": 660, "left": 120, "right": 120, "avatarWidth": 1080},
                 {"top": 260, "bottom": 480, "avatarX": 900, "avatarY": 1000, "avatarWidth": 200}]
        for raw in cases:
            with self.subTest(raw=raw):
                layout = normalize_layout(raw)
                right_edge, bottom_edge = 1080 - layout["right"], 1920 - layout["bottom"]
                phone_width = 480
                phone_x = layout["left"]
                avatar_right = layout["avatarX"] + layout["avatarWidth"]
                avatar_bottom = layout["avatarY"] + layout["avatarWidth"] * 4 / 3
                phone_bottom = bottom_edge - 328
                boards_top = phone_bottom - 256
                poll_top = phone_bottom + 24
                self.assertGreaterEqual(layout["avatarX"], phone_x + phone_width + 24)
                self.assertGreaterEqual(layout["avatarY"], layout["top"])
                self.assertLessEqual(avatar_right, right_edge)
                self.assertLessEqual(avatar_bottom + 24, boards_top + 1e-9)
                self.assertGreater(phone_bottom, layout["top"])
                self.assertEqual(bottom_edge - poll_top, 304)
                self.assertGreaterEqual(right_edge - phone_x - phone_width - 24, 336)
                self.assertLessEqual(bottom_edge, 1640)
                self.assertEqual(normalize_layout(layout), layout)


class ServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "test.sqlite"
        self.model = SyntheticModel()
        self.app = create_app(self.db, model=self.model)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def action(self, path, payload=None):
        response = await self.client.post("/api/" + path, json=payload or {})
        result = await response.json()
        self.assertLess(response.status, 300, result)
        job = self.app["workbench"].job
        if job:
            await job
        return result

    async def state(self, public=False):
        response = await self.client.get("/api/public" if public else "/api/state")
        return await response.json()

    async def comment(self, text, author="a", *, context=None, event_id=None):
        round_ = context if context is not None else (await self.state())["round"]
        body = {"event_id": event_id or uuid.uuid4().hex, "author_id": author, "nickname": "模拟" + author,
                "text": text, "expected_round_id": round_["id"] if round_ else None,
                "expected_phase": round_["phase"] if round_ else None}
        response = await self.client.post("/api/comments", json=body)
        receipt = await response.json()
        self.assertEqual(response.status, 200, receipt)
        return receipt["comment"], body

    async def incremental(self, previous=None, **overrides):
        params = {key: str(previous[key]) for key in ("session_id", "comment_cursor", "proposal_version", "vote_version")
                  if previous and previous.get(key) is not None}
        params.update({key: str(value) for key, value in overrides.items()})
        response = await self.client.get("/api/operator-state", params=params)
        self.assertEqual(response.status, 200)
        return await response.json()

    async def test_incremental_polling_recovers_gaps_pages_and_session_changes(self):
        await self.action("episode/new")
        comments = [(await self.comment(f"建议{i}", author=str(i)))[0] for i in range(5)]
        first = await self.incremental(comment_limit=2)
        self.assertTrue(first["reset"])
        self.assertTrue(first["proposal_reset"])
        self.assertTrue(first["vote_reset"])
        self.assertTrue(first["has_more"])
        self.assertEqual([c["id"] for c in first["comments"]], [c["id"] for c in comments[:2]])
        second = await self.incremental(first, comment_limit=2)
        self.assertFalse(second["reset"])
        self.assertNotIn("votes", second)
        self.assertNotIn("proposals", second)
        self.assertEqual(second["comment_cursor"], 4)
        # Simulate a lost response: repeating the last committed cursor yields
        # the same page, rather than skipping records or advancing server state.
        repeated = await self.incremental(first, comment_limit=2)
        self.assertEqual(repeated["comments"], second["comments"])
        last = await self.incremental(second, comment_limit=2)
        self.assertFalse(last["has_more"])
        self.assertEqual(last["comments"], comments[4:])
        unchanged = await self.incremental(last)
        self.assertEqual(unchanged["comments"], [])
        await self.comment("掉过几次轮询之后的新建议", author="late")
        gap = await self.incremental(last)
        self.assertEqual(len(gap["comments"]), 1)
        self.assertEqual(len((await self.state())["comments"]), 6)  # Legacy remains full.
        await self.action("session/new")
        reset = await self.incremental(gap)
        self.assertTrue(reset["reset"])
        self.assertEqual(reset["comments"], [])
        self.assertNotEqual(reset["session_id"], gap["session_id"])

    async def test_incremental_versions_include_same_author_updates_and_restart(self):
        await self.action("episode/new")
        await self.comment("原建议")
        first = await self.incremental()
        await self.comment("更新建议")
        changed = await self.incremental(first)
        self.assertNotEqual(changed["proposal_version"], first["proposal_version"])
        self.assertEqual(len(changed["proposals"]), 1)
        self.assertEqual(changed["proposals"][0]["text"], "更新建议")
        await self.comment("另一条建议", author="b")
        generate = self.model.generate
        async def two_options(task, payload):
            if task == "select":
                return {"options": [{"proposal_id": proposal["id"], "summary": f"建议{i}", "conflict": False, "reason": "合成测试"}
                                    for i, proposal in enumerate(payload["proposals"], 1)]}
            return await generate(task, payload)
        with patch.object(self.model, "generate", side_effect=two_options):
            await self.action("collection/close")
        await self.comment("投1")
        await self.comment("投1", author="b")
        voted = await self.incremental(changed)
        self.assertEqual(len(voted["votes"]), 2)
        await self.comment("投2")
        revoted = await self.incremental(voted)
        self.assertNotEqual(revoted["vote_version"], voted["vote_version"])
        self.assertFalse(revoted["vote_reset"])
        self.assertEqual(len(revoted["votes"]), 1)
        self.assertEqual(revoted["votes"][0]["author_id"], "a")
        self.assertEqual(len((await self.state())["votes"]), 2)
        await self.client.close()
        self.app = create_app(self.db, model=self.model)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        resumed = await self.incremental(revoted)
        self.assertEqual(resumed["comments"], [])
        self.assertIn("votes", resumed)
        self.assertIn("proposals", resumed)
        self.assertTrue(resumed["vote_reset"])
        self.assertEqual(len(resumed["votes"]), 2)
        invalid = await self.incremental(resumed, comment_cursor="not-an-integer", comment_limit=1)
        self.assertTrue(invalid["reset"])
        self.assertEqual(len(invalid["comments"]), 1)
        for cursor in (-1, 100000):
            self.assertTrue((await self.incremental(resumed, comment_cursor=cursor))["reset"])

    async def test_operator_metrics_do_not_enter_public_overlay(self):
        wb = self.app["workbench"]
        wb.game.metrics.record("poll_test", outcome="ok", text="私密测试正文")
        operator = await self.incremental()
        self.assertTrue(operator["performance_events"])
        self.assertNotIn("私密测试正文", str(operator["performance_events"]))
        self.assertNotIn("performance_events", await self.state(public=True))

    async def test_comments_reach_model_votes_and_saved_result_through_automation(self):
        await self.action("episode/new")
        first, first_event = await self.comment("先认真解释昨晚为何紧张")
        await self.comment("解释清楚后再问她周六是否有空")
        await self.comment("开个有点笨拙的玩笑", author="b")
        await self.comment("投1", author="c")
        self.assertEqual(first["result"], "candidate")
        self.assertEqual(len(self.model.calls), 1)  # Receiving comments calls no model.
        self.assertEqual(len((await self.state())["proposals"]), 3)
        old_round = (await self.state())["round"]
        await self.action("settings", {"auto_advance": True, "collect_seconds": 5, "vote_seconds": 5})
        wb = self.app["workbench"]
        wb.state()  # Establish the collection clock before advancing test time.
        wb.deadline = time.time() - 1
        await asyncio.sleep(.6)
        if wb.job:
            await wb.job
        self.assertEqual((await self.state())["round"]["phase"], "VOTING")
        select_payload = next(payload for task, payload in self.model.calls if task == "select")
        self.assertEqual([p["text"] for p in select_payload["proposals"]],
                         ["先认真解释昨晚为何紧张", "解释清楚后再问她周六是否有空", "开个有点笨拙的玩笑"])
        stale, _ = await self.comment("投1", context=old_round)
        self.assertEqual(stale["result"], "stale")
        vote, vote_event = await self.comment("投1", author="a")
        repeated = await self.client.post("/api/comments", json=vote_event)
        self.assertEqual((await repeated.json())["comment"], vote)
        invalid, _ = await self.comment("投4", author="a")
        chat, _ = await self.comment("我觉得投1", author="b")
        self.assertEqual(invalid["result"], "ignored")
        self.assertEqual(chat["result"], "ignored")
        await self.comment("投1", author="c")
        s = await self.state()
        self.assertEqual(s["round"]["options"][0]["votes"], 2)
        self.assertEqual(len(s["comments"]), 9)
        self.assertNotIn("comments", await self.state(True))
        wb.deadline = time.time() - 1
        await asyncio.sleep(.6)
        if wb.job:
            await wb.job
        await asyncio.sleep(.6)
        s = await self.state()
        self.assertEqual(s["episode"]["status"], "SUCCEEDED")
        self.assertFalse(s["settings"]["auto_advance"])
        self.assertEqual(len(s["messages"]), 3)
        self.assertEqual(s["leaderboards"]["best"][0]["author_id"], "a")
        self.assertEqual(len(self.model.calls), 4)
        await self.client.close()
        self.app = create_app(self.db, model=self.model)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        restored = await self.state()
        self.assertEqual(restored["comments"], s["comments"])
        self.assertEqual(restored["votes"], s["votes"])
        old_receipt = await self.client.post("/api/comments", json=first_event)
        self.assertEqual((await old_receipt.json())["comment"], first)
        self.assertEqual(len((await self.state())["comments"]), 9)

    async def test_comment_at_expired_deadline_is_logged_without_counting(self):
        await self.action("episode/new")
        await self.action("settings", {"auto_advance": True})
        wb = self.app["workbench"]
        state = wb.state()
        event = {"event_id": "late", "author_id": "a", "nickname": "模拟a", "text": "迟到建议",
                 "expected_round_id": state["round"]["id"], "expected_phase": "COLLECTING"}
        # A fixed clock tests the boundary without racing the background tick.
        with patch("romance.server.time.time", return_value=wb.deadline + .01):
            response = await self.client.post("/api/comments", json=event)
            legacy = await self.client.post("/api/proposals", json={"author_id": "legacy", "nickname": "旧接口", "text": "迟到建议"})
            self.assertEqual(legacy.status, 409)
        self.assertEqual(response.status, 200)
        receipt = (await response.json())["comment"]
        self.assertEqual(receipt["result"], "ignored")
        self.assertIn("截止", receipt["reason"])
        self.assertEqual((await self.state())["proposals"], [])
        await self.action("settings", {"auto_advance": False})

    async def test_comments_are_recorded_during_generation_and_invalid_envelopes_fail(self):
        await self.action("episode/new")
        await self.comment("认真回答")
        started, release = asyncio.Event(), asyncio.Event()
        generate = self.model.generate

        async def delayed(task, payload):
            if task == "select":
                started.set()
                await release.wait()
            return await generate(task, payload)

        self.model.generate = delayed
        response = await self.client.post("/api/collection/close", json={})
        self.assertEqual(response.status, 202)
        await asyncio.wait_for(started.wait(), 1)
        receipt, body = await self.comment("模型生成时也能发评论", author="b")
        self.assertEqual(receipt["result"], "ignored")
        self.assertIn("生成", receipt["reason"])
        body["text"] = "不允许改重试正文"
        response = await self.client.post("/api/comments", json=body)
        self.assertEqual(response.status, 409)
        response = await self.client.post("/api/comments", json={"text": "缺少上下文"})
        self.assertEqual(response.status, 409)
        release.set()
        await self.app["workbench"].job
        self.assertEqual(len((await self.state())["comments"]), 2)

    async def test_manual_episode_through_http_and_public_projection(self):
        await self.action("episode/new")
        await self.action("proposals", {"author_id": "a", "nickname": "测试军师", "text": "认真邀约"})
        await self.action("collection/close")
        options = (await self.state())["round"]["options"]
        await self.action("votes", {"author_id": "v", "nickname": "模拟投票", "option_id": options[0]["id"]})
        await self.action("voting/close")
        state = await self.state(True)
        self.assertEqual(state["episode"]["status"], "SUCCEEDED")
        self.assertEqual(state["episode"]["score"], 100)
        self.assertEqual(state["leaderboards"]["best"][0]["adopted"], 1)
        self.assertEqual(len(state["messages"]), 3)
        self.assertNotIn("diagnostics", state)
        self.assertNotIn("model", state)
        self.assertNotIn("仅女生知道", str(state))
        self.assertNotIn("dice", str(state))
        await self.action("episode/new")
        self.assertEqual((await self.state())["leaderboards"]["best"][0]["adopted"], 1)
        await self.action("session/new")
        self.assertEqual((await self.state())["leaderboards"], {"best": [], "worst": []})

    async def test_cross_site_and_foreign_host_cannot_mutate(self):
        response = await self.client.post("/api/episode/new", json={}, headers={"Origin": "https://attacker.invalid"})
        self.assertEqual(response.status, 403)
        response = await self.client.get("/api/state", headers={"Host": "attacker.invalid"})
        self.assertEqual(response.status, 403)
        response = await self.client.post("/api/episode/new", data="{}", headers={"Content-Type": "text/plain"})
        self.assertEqual(response.status, 415)
        self.assertEqual(self.model.calls, [])

    async def test_layout_persists_for_separate_browser_and_database_reload(self):
        layout = {"top": 200, "right": 170, "bottom": 350, "left": 70, "avatarX": 80, "avatarY": 340, "avatarWidth": 600}
        await self.action("layout", layout)
        expected = normalize_layout(layout)
        self.assertEqual((await self.state(True))["layout"], expected)
        await self.client.close()
        self.app = create_app(self.db, model=self.model)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.assertEqual((await self.state())["layout"], expected)

    async def test_automation_stops_at_episode_end(self):
        await self.action("episode/new")
        await self.action("collection/close")  # no candidates, synthetic self-completion
        await self.action("settings", {"auto_advance": True})
        await asyncio.sleep(.6)
        state = await self.state()
        self.assertFalse(state["settings"]["auto_advance"])
        self.assertEqual(sum(task == "prepare" for task, _ in self.model.calls), 1)

    async def test_interrupted_phase_offers_retry_after_restart(self):
        await self.action("episode/new")
        game = self.app["workbench"].game
        game.current_round["phase"] = "SELECTING"
        game._save()
        await self.client.close()
        self.app = create_app(self.db, model=self.model)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        state = await self.state()
        self.assertIsNotNone(state["error"])
        self.assertFalse(state["busy"])
        await self.action("retry")
        self.assertEqual((await self.state())["episode"]["status"], "SUCCEEDED")

    async def test_automation_budget_pauses_without_changing_story(self):
        await self.action("episode/new")
        await self.action("settings", {"auto_advance": True})
        wb = self.app["workbench"]
        wb.auto_started = time.monotonic() - 1201
        await asyncio.sleep(.6)
        state = await self.state()
        self.assertFalse(state["settings"]["auto_advance"])
        self.assertEqual(state["episode"]["score"], 50)
        self.assertEqual(state["episode"]["status"], "ACTIVE")
        self.assertIsNotNone(state["automation"]["note"])
        self.assertEqual(len(self.model.calls), 1)

    async def test_only_static_directory_is_served(self):
        response = await self.client.get("/data/test.sqlite")
        self.assertEqual(response.status, 404)
        response = await self.client.get("/static/../romance/model.py")
        self.assertEqual(response.status, 404)


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_attempts_every_resource_after_independent_failures(self):
        model = SyntheticModel()
        model.aclose = AsyncMock(side_effect=RuntimeError("secret-model-url"))
        wb = Workbench(":memory:", model=model)
        async def failed():
            raise RuntimeError("secret-task-details")
        wb.scheduler = asyncio.create_task(failed())
        wb.job = asyncio.create_task(failed())
        await asyncio.sleep(0)
        wb.program.close = AsyncMock(side_effect=RuntimeError("secret-program-details"))
        close_storage = wb.game.close
        wb.game.close = Mock(wraps=close_storage)
        with self.assertRaisesRegex(ValueError, "^cleanup_failed$"):
            await wb.close()
        wb.program.close.assert_awaited_once()
        model.aclose.assert_awaited_once()
        wb.game.close.assert_called_once()
        self.assertEqual(wb.cleanup["first_error"], "scheduler")
        self.assertEqual(wb.cleanup["steps"]["storage"], "closed")
        self.assertNotIn("secret", str(wb.cleanup))

    async def test_abort_stops_job_and_clock_even_when_database_and_settings_fail(self):
        wb = Workbench(":memory:", model=SyntheticModel())
        cancelled = asyncio.Event()
        async def running():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        wb.job = asyncio.create_task(running())
        await asyncio.sleep(0)
        wb.settings["auto_advance"] = True
        wb.deadline = time.time() + 100
        with patch.object(wb.game, "abort_episode", side_effect=OSError("private-path")), patch.object(wb, "save_settings", side_effect=OSError("private-path")):
            with self.assertRaisesRegex(ValueError, "^cleanup_failed$"):
                await wb.abort()
        self.assertTrue(cancelled.is_set())
        self.assertFalse(wb.settings["auto_advance"])
        self.assertIsNone(wb.job)
        self.assertIsNone(wb.deadline)
        self.assertEqual(wb.cleanup["errors"], ["episode", "settings"])
        await wb.close()


if __name__ == "__main__":
    unittest.main()
