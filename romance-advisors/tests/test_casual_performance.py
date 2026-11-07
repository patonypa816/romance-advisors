"""Offline ordinary-chat coverage with Program's real send queue and fake Studio."""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from romance.casual import CasualPerformance
from romance.model import PROMPTS, CASUAL_COMMON, AMBIENT_COMMON, COMMON, Credentials, QwenModel
from romance.program import Program


def spoken(text="喝咖啡的时候，我喜欢慢一点。", ids=()):
    return {"speak": True, "text": text, "instruction": "自然轻松地聊天。",
            "expression": {"emotion": "轻松", "actions": [], "recovery": None},
            "intent": "聊日常", "comment_ids": list(ids)}


class Model:
    def __init__(self):
        self.calls = []
        self.result = spoken()
        self.failure = False
        self.slow = False
        self.ignore_cancel = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, task, payload):
        self.calls.append((task, copy.deepcopy(payload)))
        self.entered.set()
        if self.slow:
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                if not self.ignore_cancel:
                    raise
                await self.release.wait()
        if self.failure:
            raise RuntimeError("provider-secret-not-for-public-errors")
        return copy.deepcopy(self.result)


class Studio:
    generation = 7

    def __init__(self):
        self.calls = []
        self.result = {"status": "sent", "send_complete": True}
        self.prepare_status = "prepared"
        self.slow_prepare = False
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    def receipt(self, record, **fields):
        return dict(job_id=record["job_id"], owner_id=record["owner_id"], generation=record["generation"], **fields)

    async def prepare(self, record, text):
        self.calls.append(("prepare", record, text))
        self.entered.set()
        if self.slow_prepare:
            await self.release.wait()
        return self.receipt(record, status=self.prepare_status)

    async def dispatch(self, record):
        self.calls.append(("dispatch", record))
        return self.receipt(record, **self.result)

    async def cancel_job(self, record):
        self.calls.append(("cancel", record))
        return self.receipt(record, status="cancelled", cancel_result="cancelled_before_delivery")

    async def job(self, record):
        return self.receipt(record, **self.result)


class Queue:
    # Use the production reservation, preparation, receipt and dispatch machinery.
    reserve_next = Program.reserve_next
    schedule_candidate = Program.schedule_candidate
    candidate_valid = Program.candidate_valid
    send = Program.send
    _request_same_job = Program._request_same_job
    _follow = Program._follow
    _check_receipt = Program._check_receipt
    _pending = staticmethod(Program._pending)

    def __init__(self):
        self.state = "running"
        self.ready = True
        self.content_mode = "chat"
        self.mode_epoch = 1
        self.switchable = True
        self.run_id = "existing-owner"
        self.client = Studio()
        self.workbench = SimpleNamespace(model=Model())
        self.current = self.next = None
        self.sender_failed = False
        self.formal_waiting = 0
        self.send_lock = asyncio.Lock()
        self.ambient = SimpleNamespace(next_dispatch=0, prepared=lambda *_: None)

    @property
    def enabled(self):
        return self.state == "running"

    def _metric(self, *_args, **_kwargs):
        pass

    def notify_comment(self):
        pass


class CasualTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = Queue()
        self.chat = CasualPerformance(self.p)
        self.now = 100.0
        self.clock = patch("romance.casual.time", SimpleNamespace(monotonic=lambda: self.now))
        self.clock.start()

    async def asyncTearDown(self):
        await self.chat.close()
        self.clock.stop()

    async def run_one(self):
        self.chat.tick()
        task = self.chat.task
        if task:
            await task
        return task

    def offer(self, key="c1", text="你喝咖啡加糖吗？"):
        return self.chat.offer({"id": key, "nickname": "真实昵称", "text": text})

    async def test_quiet_topic_reuses_owner_queue_and_bounded_gap(self):
        await self.run_one()
        self.assertEqual([call[0] for call in self.p.client.calls], ["prepare", "dispatch"])
        record = self.p.client.calls[0][1]
        self.assertEqual(record["owner_id"], "existing-owner")
        self.assertEqual(record["generation"], 7)
        self.assertEqual(len(self.chat.history), 1)
        task, payload = self.p.workbench.model.calls[0]
        self.assertEqual(task, "casual")
        self.assertEqual(payload["comments"], [])
        self.assertNotIn("messages", payload)
        self.assertGreaterEqual(self.chat.next_topic, 112)
        self.assertLessEqual(self.chat.next_topic, 120)
        self.chat.tick()
        self.assertEqual(len(self.p.workbench.model.calls), 1)
        self.now = 121
        await self.run_one()
        self.assertEqual(len(self.p.workbench.model.calls), 2)
        self.assertEqual(len(self.p.workbench.model.calls[-1][1]["recent_performances"]), 1)

    async def test_observed_viewer_content_prioritised_and_deduplicated(self):
        self.assertTrue(self.offer())
        self.assertFalse(self.offer())
        self.p.workbench.model.result = spoken("加一点糖也不错，你喜欢哪种？", ["c1"])
        await self.run_one()
        self.assertEqual(self.p.workbench.model.calls[0][1]["comments"][0]["text"], "你喝咖啡加糖吗？")
        self.assertEqual(self.chat.history[-1]["comment_ids"], ["c1"])
        self.assertNotIn("c1", self.chat.comments)
        self.now = 107
        self.offer("c2", "喜欢清淡的")
        self.p.workbench.model.result = spoken("清淡一点，倒也挺自在的。", ["c2"])
        await self.run_one()
        self.assertEqual(len(self.p.workbench.model.calls), 2)
        self.assertLess(self.now, self.chat.next_topic)

    async def test_non_chat_not_ready_switching_busy_and_failed_never_generate(self):
        for field, values in (("content_mode", ["game", "to_chat", "to_game"]),
                              ("state", ["starting", "stopping", "stopped"]),
                              ("ready", [False]), ("switchable", [False]), ("sender_failed", [True])):
            old = getattr(self.p, field)
            for value in values:
                setattr(self.p, field, value)
                self.chat.tick()
                self.assertIsNone(self.chat.task)
            setattr(self.p, field, old)
        for mode in ("game", "to_chat", "to_game"):
            self.p.content_mode = mode
            self.assertFalse(self.offer(mode))
        self.assertEqual(self.p.workbench.model.calls, [])

    async def test_slow_generation_cannot_cross_mode_epoch_or_run(self):
        model = self.p.workbench.model
        model.slow = model.ignore_cancel = True
        self.chat.tick()
        await model.entered.wait()
        self.p.mode_epoch += 1
        self.chat.invalidate()
        model.release.set()
        await self.chat.task
        self.assertEqual(self.p.client.calls, [])
        self.assertEqual(self.chat.history, [])
        self.assertIsNone(self.p.next)
        self.assertFalse(self.p.sender_failed)

    async def test_new_viewer_revokes_unsubmitted_topic_without_parallel_slot(self):
        model = self.p.workbench.model
        model.slow = True
        self.chat.tick()
        await model.entered.wait()
        old = self.p.next
        self.assertTrue(self.offer())
        self.assertTrue(old["invalidated"])
        self.chat.tick()
        self.assertEqual(len(model.calls), 1)
        await self.chat.task
        self.assertEqual(self.p.client.calls, [])
        model.slow = False
        model.result = spoken("我喜欢清淡一点的口味。", ["c1"])
        self.now += 7
        await self.run_one()
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(len(self.p.client.calls), 2)

    async def test_new_viewer_cancels_prepared_topic_by_same_job(self):
        studio = self.p.client
        studio.slow_prepare = True
        self.chat.tick()
        await studio.entered.wait()
        record = studio.calls[0][1]
        self.offer()
        studio.release.set()
        await self.chat.task
        self.assertEqual([c[0] for c in studio.calls], ["prepare", "cancel"])
        self.assertEqual(studio.calls[1][1]["job_id"], record["job_id"])
        self.assertFalse(self.p.sender_failed)
        self.assertEqual(self.chat.history, [])

    async def test_generation_failure_and_invalid_text_backoff_and_cap(self):
        for result in (None, spoken("长" * 61)):
            self.chat.reset()
            self.p.workbench.model.failure = result is None
            if result is not None:
                self.p.workbench.model.result = result
            for attempt in range(3):
                before = len(self.p.workbench.model.calls)
                await self.run_one()
                self.chat.tick()
                self.assertEqual(len(self.p.workbench.model.calls), before + 1)
                self.now += 21
            self.assertTrue(self.chat.error)
            self.assertFalse(self.p.sender_failed)
            before = len(self.p.workbench.model.calls)
            await self.run_one()
            self.assertEqual(len(self.p.workbench.model.calls), before)
        self.assertEqual(self.p.client.calls, [])
        self.assertNotIn("secret", self.chat.error)

    async def test_repeated_safe_silence_retires_only_assessed_comments_without_stopping(self):
        model = self.p.workbench.model
        model.result = {"speak": False}
        for index in range(5):
            key = f"unsuitable-{index}"
            self.offer(key, "不适宜回应的合成评论")
            await self.run_one()
            self.assertNotIn(key, self.chat.comments)
            self.assertNotIn(key, self.chat.attempted_at)
            self.assertEqual(self.chat.failures, 0)
            self.assertEqual(self.chat.error, "")
            self.assertFalse(self.p.sender_failed)
            self.assertEqual(self.p.state, "running")
            before = len(model.calls)
            self.chat.tick()
            self.assertEqual(len(model.calls), before)
            self.now += 7
        self.assertEqual(self.p.client.calls, [])
        self.assertEqual(self.chat.history, [])
        self.assertEqual(len(model.calls), 5)
        self.offer("fresh", "你喜欢看书吗？")
        model.result = spoken("看书很适合慢慢享受。", ["fresh"])
        await self.run_one()
        self.assertEqual(model.calls[-1][1]["comments"], [{"id": "fresh", "nickname": "真实昵称", "text": "你喜欢看书吗？"}])
        self.assertEqual([call[0] for call in self.p.client.calls], ["prepare", "dispatch"])
        self.assertEqual(self.chat.history[-1]["comment_ids"], ["fresh"])

    async def test_silent_decision_preserves_viewer_arriving_during_generation(self):
        model = self.p.workbench.model
        model.slow = True
        model.result = {"speak": False}
        self.offer("assessed-old")
        self.chat.tick()
        await model.entered.wait()
        self.offer("arrived-later", "咖啡加奶怎么样？")
        model.release.set()
        await self.chat.task
        self.assertNotIn("assessed-old", self.chat.comments)
        self.assertIn("arrived-later", self.chat.comments)
        self.assertEqual(self.chat.error, "")
        self.assertEqual(self.p.client.calls, [])
        model.slow = False
        model.result = spoken("加奶会温和一些。", ["arrived-later"])
        self.now += 7
        await self.run_one()
        self.assertEqual(self.chat.history[-1]["comment_ids"], ["arrived-later"])

    async def test_quiet_silence_waits_for_topic_gap_but_new_viewer_can_arrive_sooner(self):
        model = self.p.workbench.model
        model.result = {"speak": False}
        for _ in range(4):
            await self.run_one()
            self.assertEqual(self.chat.error, "")
            self.assertFalse(self.p.sender_failed)
            self.assertGreaterEqual(self.chat.next_topic, self.now + 12)
            self.assertLessEqual(self.chat.next_topic, self.now + 20)
            before = len(model.calls)
            self.now += 7
            self.chat.tick()
            self.assertEqual(len(model.calls), before)
            self.now += 14
        self.assertEqual(self.p.client.calls, [])
        self.chat.failures = 2
        await self.run_one()
        self.assertEqual(self.chat.failures, 0)
        self.now += 7
        self.assertLess(self.now, self.chat.next_topic)
        self.offer("new-viewer", "咖啡加奶怎么样？")
        model.result = spoken("加奶会温和一些，你喜欢吗？", ["new-viewer"])
        await self.run_one()
        self.assertEqual([call[0] for call in self.p.client.calls], ["prepare", "dispatch"])

    async def test_authoritative_preparation_failure_latches_without_retry(self):
        self.p.client.prepare_status = "failed"
        await self.run_one()
        self.assertTrue(self.p.sender_failed)
        self.assertTrue(self.chat.error)
        self.now += 1000
        await self.run_one()
        self.assertEqual(len(self.p.client.calls), 1)
        self.assertEqual(self.chat.history, [])

    async def test_incomplete_sent_and_pending_receipt_never_count_as_consumed(self):
        for receipt in ({"status": "sent"}, {"status": "unknown"}):
            self.chat.reset()
            self.p.sender_failed = False
            self.p.client.result = receipt
            # Unknown statuses are intentionally returned by a transport fake;
            # the casual layer must latch them even when Program was bypassed.
            async def send(*args, **kwargs):
                return receipt
            with patch.object(self.p, "send", send):
                await self.run_one()
            self.assertTrue(self.p.sender_failed)
            self.assertEqual(self.chat.history, [])
        self.chat.reset();self.p.sender_failed = False
        async def pending_send(*args, **kwargs):
            return {"status": "sent", "send_complete": True, "worker_pending": True}
        with patch.object(self.p, "send", pending_send):
            await self.run_one()
        self.assertTrue(self.p.sender_failed)
        self.assertEqual(self.chat.history, [])

    async def test_unknown_send_exception_latches_and_does_not_leak_provider_text(self):
        async def broken(*args, **kwargs):
            raise RuntimeError("private-credential-value")
        with patch.object(self.p, "send", broken):
            await self.run_one()
        self.assertTrue(self.p.sender_failed)
        self.assertNotIn("private", self.chat.error)
        self.assertEqual(self.chat.history, [])

    async def test_bounded_canonical_queue_and_unknown_response_id(self):
        self.assertFalse(self.chat.offer({"id": "bad", "nickname": "x", "text": "x" * 2001}))
        for i in range(100):
            self.offer(str(i), "测试内容")
        self.assertEqual(len(self.chat.comments), 32)
        self.p.workbench.model.result = spoken("这句话我想慢慢接。", ["invented"])
        await self.run_one()
        comments = self.p.workbench.model.calls[-1][1]["comments"]
        self.assertEqual([c["id"] for c in comments], ["96", "97", "98", "99"])
        self.assertEqual(self.p.client.calls, [])
        self.assertEqual(self.chat.failures, 1)

    async def test_close_cancels_generation_and_reset_drops_old_comments_history(self):
        self.p.workbench.model.slow = True
        self.offer()
        self.chat.tick()
        await self.p.workbench.model.entered.wait()
        await self.chat.close()
        self.assertIsNone(self.p.next)
        self.assertEqual(self.p.client.calls, [])
        self.chat.reset()
        self.assertEqual(self.chat.comments, {})
        self.assertEqual(self.chat.history, [])

    async def test_visual_expression_and_ssml_keep_existing_plan_contract(self):
        result = spoken("认真一点也挺好。")
        result["ssml"] = '<speak rate="0.95">认真一点也挺好。</speak>'
        result["expression"] = {"emotion": "认真", "actions": [{
            "anchor": {"quote": "认真", "occurrence": 1},
            "motions": [{"part": "face", "description": "眉眼略微舒展"}],
            "gaze": "看向镜头", "expression": "嘴角轻轻上扬"}],
            "recovery": {"gaze": "看向镜头", "expression": "自然微笑"}}
        self.p.workbench.model.result = result
        await self.run_one()
        plan = self.p.client.calls[0][1]["plan"]
        self.assertEqual(plan["segments"][0]["ssml"], result["ssml"])
        self.assertEqual(len(plan["segments"][0]["actions"]), 1)
        self.assertEqual(self.chat.history[-1]["text"], result["text"])

    async def test_mode_epoch_and_run_identity_rechecked_without_local_invalidate(self):
        for field in ("mode_epoch", "run_id"):
            self.chat.reset()
            model = self.p.workbench.model
            model.slow = True
            model.entered.clear();model.release.clear()
            self.chat.tick()
            await model.entered.wait()
            setattr(self.p, field, getattr(self.p, field) + 1 if field == "mode_epoch" else "new-run")
            model.release.set()
            await self.chat.task
            self.assertEqual(self.p.client.calls, [])

    async def test_model_adapter_routes_casual_prompt_without_formal_role_or_network(self):
        bodies = []
        class Response:
            status = 200
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            @property
            def content(self):
                return self
            async def iter_chunked(self, _size):
                yield json.dumps({"choices": [{"finish_reason": "stop", "message": {
                    "content": '{"speak":false}'}}]}).encode()
        class Session:
            def post(self, _url, **kwargs):
                bodies.append(json.loads(kwargs["data"]))
                return Response()
        model = QwenModel()
        with patch.object(model, "_http", return_value=Session()), patch(
                "romance.model.load_credentials", return_value=Credentials("synthetic-only", "https://example.invalid")):
            for task in ("casual", "ambient", "select"):
                await model.generate(task, {})
        for body, task, common in zip(bodies, ("casual", "ambient", "select"), (CASUAL_COMMON, AMBIENT_COMMON, COMMON)):
            self.assertEqual(body["messages"][0]["content"], common + PROMPTS[task])
        self.assertEqual(model.requests, 3)
        self.assertNotIn("你为本地虚构恋爱", bodies[0]["messages"][0]["content"])

    def test_prompt_separates_chat_from_formal_tasks(self):
        self.assertIn("没有游戏或恋爱情景剧", PROMPTS["casual"])
        self.assertIn("不要虚构观众", PROMPTS["casual"])
        self.assertIn("咖啡馆", CASUAL_COMMON)
        self.assertIn("本期男主", PROMPTS["ambient"])


if __name__ == "__main__":
    unittest.main()
