"""离线验证动作调度；假连接不产生云会话，发送完成不代表视觉完成。"""
import asyncio
import base64
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

from action_schema import compile_timeline as real_compile_timeline, parse_plan
from motion_component import MotionComponent

GE = {"gaze": "自然看向镜头", "expression": "神情温和"}
SWITCH_VIDEOS = ["https://tax.shumaitech.cn/offline-only-a.mp4?credential=offline-private-value",
                 "https://tax.shumaitech.cn/offline-only-b.mp4"]


def short(delay=0, pair=False):
    motions = [{"part": "both_arms", "description": "双手向外轻摊"}]
    if pair:
        motions.append({"part": "head", "description": "头部轻轻右倾"})
    return {"kind": "S", "delay_seconds": delay, "motions": motions, "hold_seconds": 0, **GE}


def long(hold=2):
    return {"kind": "L", "delay_seconds": 0, "motions": [{"part": "whole_body", "description": "从椅子上站起"}], "hold_seconds": hold, **GE}


def compressed_timeline(actions, recovery=None):
    """先走真实校验，再仅压缩墙钟等待；不改变节点数与叠加关系。"""
    timeline = real_compile_timeline(actions, recovery=recovery)
    for node in timeline:
        for field in ("delay_seconds", "at_seconds", "hold_seconds"):
            node[field] *= 0.02
    return timeline


class NoNetwork:
    def __getattr__(self, name):
        raise AssertionError(f"离线测试禁止访问HTTP客户端：{name}")


class FakeSocket:
    closed = False

    def __init__(self, write_delay=0, fail_motion=None):
        self.write_delay = write_delay
        self.fail_motion = fail_motion
        self.motion_attempts = 0
        self.attempts = []
        self.sent = []
        self.binary = []

    async def send_json(self, frame):
        row = {"frame": copy.deepcopy(frame), "started": time.monotonic()}
        self.attempts.append(row)
        if frame["type"] == 9:
            self.motion_attempts += 1
            if self.motion_attempts == self.fail_motion:
                raise ConnectionError("injected local socket failure")
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        row["completed"] = time.monotonic()
        self.sent.append(row)

    async def send_bytes(self, chunk):
        self.binary.append({"time": time.monotonic(), "chunk": bytes(chunk)})

    def motions(self):
        return [row for row in self.sent if row["frame"]["type"] == 9]


class FakeIncomingSocket:
    def __init__(self):
        self.queue = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()

    def receive(self, frame):
        self.queue.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(frame)))


async def eventually(predicate, timeout=1):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(wait(), timeout)


class MotionComponentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="terra-motion-offline-")
        self.addCleanup(self.temp.cleanup)
        self.components = []

    def component(self, *, mock_audio=True, write_delay=0, fail_motion=None):
        out = Path(self.temp.name) / str(len(self.components))
        component = MotionComponent(NoNetwork(), out, out / "unused-image.png")
        component._ws = FakeSocket(write_delay=write_delay, fail_motion=fail_motion)
        component.live_id = "offline-only"
        component._ready.set()
        component._joined.set()
        component.state = "ready"
        component.DRAIN_SECONDS = 0
        if mock_audio:
            component._audio = AsyncMock(return_value=None)
        self.components.append(component)
        return component

    async def test_start_asset_and_image_modes_are_exclusive_with_same_session_options(self):
        bodies = {}
        for avatar_id in (None, "12345678901234567890"):
            with self.subTest(avatar_id=avatar_id):
                component = self.component()
                component.state = "idle"
                image_bytes = b"\x89PNG\r\n\x1a\noffline-fixture"
                component.image.parent.mkdir(parents=True, exist_ok=True)
                component.image.write_bytes(image_bytes)
                component._run = AsyncMock(return_value=None)
                # asset分支若偷偷编码原图，本测试应立即失败而非仅检查最终body。
                encoding_guard = (patch("component_live.base64.b64encode", side_effect=AssertionError("asset must not encode image"))
                                  if avatar_id is not None else patch("component_live.base64.b64encode", wraps=base64.b64encode))
                with encoding_guard:
                    receipt = await component.start("offline-api-value", avatar_id=avatar_id)
                    await component._task
                component._run.assert_awaited_once()
                body = component._run.await_args.args[0]
                mode = "avatar_id" if avatar_id is not None else "image_uri"
                bodies[mode] = body
                self.assertEqual(receipt["creation_input_mode"], mode)
                self.assertEqual(receipt["creation_avatar_id"], avatar_id)
                self.assertEqual(component.public()["creation_input_mode"], mode)
                if avatar_id is not None:
                    self.assertEqual(body["avatar_id"], avatar_id)
                    self.assertNotIn("image_uri", body)
                else:
                    self.assertNotIn("avatar_id", body)
                    prefix, encoded = body["image_uri"].split(",", 1)
                    self.assertEqual(prefix, "data:image/png;base64")
                    self.assertEqual(base64.b64decode(encoded), image_bytes)
                event = next(row for row in component.diagnostics.records if row["kind"] == "start_requested")
                self.assertEqual(event["details"]["creation_input_mode"], mode)
                self.assertEqual(event["details"]["creation_avatar_id"], avatar_id)
                safe = json.dumps({"receipt": receipt, "diagnostics": component.diagnostics.export()})
                self.assertNotIn("offline-api-value", safe)
                self.assertNotIn("data:image", safe)
                self.assertNotIn("image_uri\": \"data:", safe)
        asset_common = {k: v for k, v in bodies["avatar_id"].items() if k != "avatar_id"}
        image_common = {k: v for k, v in bodies["image_uri"].items() if k != "image_uri"}
        self.assertEqual(asset_common, image_common)
        self.assertEqual(image_common, {"model": "vidu-s2",
                                      "managed_rtc": {"provider": "artc", "enable_recording": True},
                                      "action": {"enabled": True}})

    async def test_start_invalid_asset_id_refuses_before_read_attempt_or_cloud_task(self):
        for avatar_id in ("", "123x", "１２３", "١٢٣", " 123", "123\n", "1"*65, 123, True, [], {}):
            with self.subTest(avatar_id=avatar_id):
                component = self.component()
                component.state = "idle"
                component._run = AsyncMock(side_effect=AssertionError("must not start cloud task"))
                # 不提供底图文件；非法ID必须优先拒绝，不能先读取本地文件。
                with self.assertRaises(ValueError):
                    await component.start("offline-api-value", avatar_id=avatar_id)
                self.assertFalse(component._attempted)
                self.assertFalse(component.creation_attempted)
                self.assertIsNone(component._task)
                self.assertIsNone(component.creation_input_mode)
                self.assertIsNone(component.creation_avatar_id)
                component._run.assert_not_awaited()

    async def test_pair_is_one_s_frame_followed_by_one_q(self):
        component = self.component()
        with patch("motion_component.compile_timeline", side_effect=compressed_timeline), \
                patch("motion_component.ACTION_POST_SEND_SECONDS", .02):
            receipt = await component.send_expression(b"\0\0", "咱听着。", [short(pair=True)], 1, recovery=dict(GE))
        motions = component._ws.motions()
        self.assertEqual(len(motions), 2)
        self.assertEqual(motions[0]["frame"]["payload"]["text_msg"]["content"], "动作：同时双手向外轻摊，并头部轻轻右倾。\n眼神：自然看向镜头。\n表情：神情温和。")
        self.assertEqual(motions[1]["frame"]["payload"]["text_msg"]["content"], "动作：正常坐姿。\n眼神：自然看向镜头。\n表情：神情温和。")
        self.assertEqual([event["role"] for event in component.motion_events], ["S", "Q"])
        self.assertEqual(component.motion_events[0]["simultaneous_count"], 2)
        self.assertTrue(receipt["actions_sent_complete"])
        self.assertFalse(receipt["visual_effect_confirmed"])

    async def test_real_q_waits_1_point_5_seconds_after_last_success(self):
        # 保留真实墙钟验收，避免压时测试把1.5秒常量错误也一起隐藏。
        component = self.component(write_delay=0.015)
        receipt = await asyncio.wait_for(component.send_expression(b"\0\0", "咱听着。", [short()], 1, recovery=dict(GE)), 3)
        first, quiet = component._ws.motions()
        self.assertGreaterEqual(quiet["started"] - first["completed"], 1.49)
        self.assertLess(quiet["started"] - first["completed"], 2.5)
        self.assertEqual(receipt["timeline"][-1]["delay_seconds"], 1.5)
        self.assertEqual(len([e for e in component.motion_events if e["role"] == "Q"]), 1)

    async def test_four_sequential_nodes_anchor_each_gap_to_success(self):
        component = self.component(write_delay=0.02)
        actions = [short()] + [short(delay=2) for _ in range(3)]
        with patch("motion_component.compile_timeline", side_effect=compressed_timeline), \
                patch("motion_component.ACTION_POST_SEND_SECONDS", .02):
            await component.send_expression(b"\0\0", "咱一件件说明白。", actions, 1, recovery=dict(GE))
        sent = component._ws.motions()
        self.assertEqual([e["role"] for e in component.motion_events], ["S"] * 4 + ["Q"])
        for previous, current, delay in zip(sent, sent[1:], [0.04, 0.04, 0.04, 0.03]):
            self.assertGreaterEqual(current["started"] - previous["completed"], delay - 0.002)
        self.assertEqual(len(sent), 5)

    async def test_no_actions_sends_speech_without_q(self):
        component = self.component()
        receipt = await component.send_expression(b"\0\0", "咱听着。", [], 1, recovery=None)
        self.assertEqual(component._ws.motions(), [])
        self.assertEqual(receipt["timeline"], [])
        self.assertTrue(receipt["actions_sent_complete"])
        self.assertEqual([row["frame"]["type"] for row in component._ws.sent], [10])

    async def test_long_window_prevents_overlap_and_tail_q_uses_hold(self):
        component = self.component()
        with patch("motion_component.compile_timeline", side_effect=compressed_timeline), \
                patch("motion_component.ACTION_POST_SEND_SECONDS", .02):
            receipt = await component.send_expression(b"\0\0", "咱起身看看。", [long(), short(delay=2)], 1, recovery=dict(GE))
        sent = component._ws.motions()
        self.assertGreaterEqual(sent[1]["started"] - sent[0]["completed"], 0.038)
        self.assertEqual([e["role"] for e in component.motion_events], ["L", "S", "Q"])
        standalone = self.component()
        with patch("motion_component.compile_timeline", side_effect=compressed_timeline), \
                patch("motion_component.ACTION_POST_SEND_SECONDS", .02):
            receipt = await standalone.send_expression(b"\0\0", "咱起身看看。", [long(hold=3)], 1, recovery=dict(GE))
        first, quiet = standalone._ws.motions()
        self.assertGreaterEqual(quiet["started"] - first["completed"], 0.058)
        self.assertEqual(receipt["timeline"][-1]["delay_seconds"], 0.06)

    async def test_invalid_long_overlap_is_rejected_before_any_frame(self):
        for actions in ([long(hold=3), short(delay=2)], [long()]):
            with self.subTest(actions=actions):
                component = self.component()
                candidate = copy.deepcopy(actions)
                if len(candidate) == 1:
                    candidate[0]["motions"].append({"part": "head", "description": "轻轻点头"})
                with self.assertRaises(ValueError):
                    await component.send_expression(b"\0\0", "咱起身看看。", candidate, 1, recovery=dict(GE))
                self.assertEqual(component._ws.attempts, [])

    async def test_pcm_progresses_while_motion_group_is_waiting(self):
        component = self.component(mock_audio=False)
        pcm = b"\x01\x00" * int(component.RATE * 0.4)
        with patch("motion_component.compile_timeline", side_effect=compressed_timeline), \
                patch("motion_component.ACTION_POST_SEND_SECONDS", .02):
            receipt = await component.send_expression(pcm, "咱边说边比划。", [short(), short(delay=3)], 1, recovery=dict(GE))
        motions = component._ws.motions()
        self.assertEqual(b"".join(row["chunk"] for row in component._ws.binary), pcm)
        self.assertTrue(any(motions[0]["completed"] <= row["time"] < motions[1]["started"] for row in component._ws.binary))
        self.assertTrue(any(row["time"] > motions[-1]["completed"] for row in component._ws.binary))
        self.assertTrue(receipt["send_complete"])
        self.assertTrue(receipt["actions_sent_complete"])

    async def test_stop_before_q_cancels_remaining_frames(self):
        component = self.component()
        task = asyncio.create_task(component.send_expression(b"\0\0", "咱听着。", [short()], 1, recovery=dict(GE)))
        await eventually(lambda: len(component._ws.motions()) == 1)
        component.latch_stop()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(task, 0.5)
        self.assertEqual(len(component._ws.motions()), 1)
        self.assertFalse(component.last_utterance["actions_sent_complete"])
        self.assertFalse(component.active_motion_plan["is_active"])
        self.assertIsNone(component._motion_task)

    async def test_stop_while_first_motion_waits_for_send_lock(self):
        component = self.component()
        await component._send_lock.acquire()
        task = asyncio.create_task(component.send_expression(b"\0\0", "咱听着。", [short()], 1, recovery=dict(GE)))
        try:
            await eventually(lambda: component.state == "sending")
            component.latch_stop()
        finally:
            component._send_lock.release()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(task, 0.5)
        self.assertEqual(component._ws.attempts, [])

    async def test_stop_while_q_waits_for_send_lock(self):
        component = self.component()
        with patch("motion_component.compile_timeline", side_effect=compressed_timeline), \
                patch("motion_component.ACTION_POST_SEND_SECONDS", .02):
            task = asyncio.create_task(component.send_expression(b"\0\0", "咱听着。", [short()], 1, recovery=dict(GE)))
            await eventually(lambda: len(component._ws.motions()) == 1 and component.state == "draining")
            await component._send_lock.acquire()
            try:
                await asyncio.sleep(0.06)  # 压时后的Q应已到期，正在等这个锁。
                self.assertFalse(task.done())
                component.latch_stop()
            finally:
                component._send_lock.release()
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(task, 0.5)
        self.assertEqual(len(component._ws.motions()), 1)

    async def test_queued_next_segment_cannot_receive_previous_q(self):
        component = self.component()
        with patch("motion_component.compile_timeline", side_effect=compressed_timeline), \
                patch("motion_component.ACTION_POST_SEND_SECONDS", .02):
            first_task = asyncio.create_task(component.send_expression(b"\0\0", "第一句。", [short()], 1, recovery=dict(GE)))
            await eventually(lambda: len(component._ws.motions()) == 1)
            second_task = asyncio.create_task(component.send_expression(b"\0\0", "第二句。", [short(pair=True)], 2, recovery=dict(GE)))
            first, second = await asyncio.wait_for(asyncio.gather(first_task, second_task), 1)
        events = component.motion_events
        self.assertEqual([(e["segment_index"], e["role"]) for e in events], [(1, "S"), (1, "Q"), (2, "S"), (2, "Q")])
        self.assertNotEqual(first["group_id"], second["group_id"])
        self.assertEqual([e["group_id"] for e in events[:2]], [first["group_id"]] * 2)
        self.assertEqual([e["group_id"] for e in events[2:]], [second["group_id"]] * 2)

    async def test_motion_send_failure_stops_without_false_completion(self):
        for failed_index in (1, 2, 3):
            with self.subTest(failed_index=failed_index):
                component = self.component(fail_motion=failed_index)
                with patch("motion_component.compile_timeline", side_effect=compressed_timeline), \
                patch("motion_component.ACTION_POST_SEND_SECONDS", .02):
                    with self.assertRaises((RuntimeError, ConnectionError)):
                        await component.send_expression(b"\0\0", "咱听着。", [short(), short(delay=2)], 1, recovery=dict(GE))
                self.assertTrue(component._stop.is_set())
                self.assertFalse(component.last_utterance["actions_sent_complete"])
                self.assertEqual(len(component._ws.motions()), failed_index - 1)
                self.assertEqual(len(component.motion_events), failed_index - 1)
                self.assertFalse(any(row["kind"] == "motion_group_sent" for row in component.diagnostics.records))
                self.assertFalse(component.active_motion_plan["is_active"])
                self.assertIsNone(component._motion_task)
                attempted = len(component._ws.attempts)
                await asyncio.sleep(0.02)
                self.assertEqual(len(component._ws.attempts), attempted)

    async def test_type15_type16_type97_and_bare_type9_are_blocked(self):
        for kind in (9, 15, 16, 97):
            component = self.component()
            with self.assertRaises(ValueError):
                await component._send(kind, {"text_msg": {"content": "旁路指令"}})
            self.assertEqual(component._ws.attempts, [])
        for entry in ("send_idle_action", "reset_baseline", "send_audio"):
            component = self.component()
            with self.subTest(entry=entry):
                with self.assertRaises(RuntimeError):
                    if entry == "send_idle_action":
                        await component.send_idle_action("任意动作", "offline-idle", lambda: True)
                    elif entry == "reset_baseline":
                        await component.reset_baseline("offline-reset", lambda: True)
                    else:
                        await component.send_audio(b"\0\0", "咱听着。", "任意旧动作")
                self.assertEqual(component._ws.attempts, [])

    async def test_manual_avatar_switch_sends_exact_single_type16_without_url_leak(self):
        component = self.component()
        receipt = await component.switch_avatar("123456", SWITCH_VIDEOS, "offline-switch", lambda: True)
        self.assertEqual(len(component._ws.sent), 1)
        frame = component._ws.sent[0]["frame"]
        self.assertEqual(frame, {"type": 16, "live_id": component.live_id,
                                "conn_id": component._conn_id, "seq_id": 1,
                                "payload": {"switch_avatar": {"switch_videos": SWITCH_VIDEOS,
                                                               "avatar_id": "123456"}}})
        self.assertEqual(receipt["seq_id"], frame["seq_id"])
        self.assertEqual(receipt["status"], "sent")
        self.assertTrue(receipt["sent"])
        self.assertIsInstance(receipt["sent_at"], float)
        self.assertEqual(receipt["video_count"], 2)
        self.assertFalse(receipt["visual_effect_confirmed"])
        self.assertFalse(receipt["success_acknowledged"])
        self.assertFalse(receipt["success_ack_available"])
        self.assertEqual(component.public()["avatar_switch"], receipt)
        self.assertEqual(component._ws.binary, [])
        self.assertEqual(component.motion_events, [])
        self.assertEqual(component.utterances_started, 0)
        self.assertEqual(component.motion_groups_started, 0)
        component._audio.assert_not_awaited()
        safe = json.dumps({"public": component.public(), "receipt": receipt,
                           "diagnostics": component.diagnostics.export(),
                           "requests": component._action_requests,
                           "fingerprints": component._avatar_switch_fingerprints,
                           "attempts": component._avatar_switch_attempts})
        for url in SWITCH_VIDEOS:
            self.assertNotIn(url, safe)
        for path in component.out.glob("*"):
            if path.is_file():
                self.assertNotIn("tax.shumaitech.cn", path.read_text())

    async def test_manual_avatar_switch_duplicate_and_parameter_conflict(self):
        component = self.component(write_delay=0.02)
        first = asyncio.create_task(component.switch_avatar("123456", SWITCH_VIDEOS, "same", lambda: True))
        await eventually(lambda: len(component._ws.attempts) == 1)
        pending = await component.switch_avatar("123456", SWITCH_VIDEOS, "same", lambda: True)
        self.assertTrue(pending["duplicate"])
        self.assertEqual(pending["status"], "pending")
        for avatar_id, urls in [("654321", SWITCH_VIDEOS), ("123456", list(reversed(SWITCH_VIDEOS)))]:
            with self.assertRaises(ValueError):
                await component.switch_avatar(avatar_id, urls, "same", lambda: True)
        receipt = await first
        self.assertEqual(await component.switch_avatar("123456", SWITCH_VIDEOS, "same", lambda: True),
                         {**receipt, "duplicate": True})
        self.assertEqual([row["frame"]["type"] for row in component._ws.sent], [16])

    async def test_manual_avatar_switch_rejects_invalid_shape_and_unsafe_urls_before_attempt(self):
        invalid_urls = ["file:///tmp/test.mp4", "ftp://example.com/a.mp4", "https://localhost/a",
                        "http://foo.localhost/a", "http://localhost./a", "http://127.0.0.1/a",
                        "http://127.1/a", "http://2130706433/a", "http://0x7f000001/a",
                        "http://10.0.0.1/a", "http://192.168.1.1/a", "http://172.16.0.1/a",
                        "http://169.254.169.254/a", "http://[::1]/a", "http://[fc00::1]/a",
                        "http://[::ffff:127.0.0.1]/a", "http://224.0.0.1/a",
                        "https://u:p@example.com/a", "https://@example.com/a", "https:///a",
                        "https://example.com:99999/a", "https://example.com/a\nb", None,
                        "https://example.com/" + "a"*4096]
        cases = [("123456", [url], "valid") for url in invalid_urls]
        cases += [(avatar_id, SWITCH_VIDEOS, "valid") for avatar_id in ("", "123x", "１２３", 123, "1"*65)]
        cases += [("123456", urls, "valid") for urls in ([], SWITCH_VIDEOS*6, "https://example.com/a")]
        cases += [("123456", SWITCH_VIDEOS, request_id) for request_id in ("", "https://private.example/a", "x"*129, None)]
        for avatar_id, urls, request_id in cases:
            with self.subTest(avatar_id=avatar_id, urls=urls, request_id=request_id):
                component = self.component()
                with self.assertRaises(ValueError) as error:
                    await component.switch_avatar(avatar_id, urls, request_id, lambda: True)
                self.assertNotIn("https://", str(error.exception))
                self.assertEqual(component._ws.attempts, [])
                self.assertEqual(component._action_requests, {})

    async def test_manual_avatar_switch_rejects_busy_unready_stopped_or_stale(self):
        for reason in ("sending", "draining", "pending", "speech_lock", "not_ready",
                       "not_joined", "stopped", "stale"):
            with self.subTest(reason=reason):
                component = self.component()
                if reason in {"sending", "draining"}:
                    component.state = reason
                elif reason == "pending":
                    component._pending_speech = 1
                elif reason == "speech_lock":
                    await component._speech_lock.acquire()
                elif reason == "not_ready":
                    component._ready.clear()
                elif reason == "not_joined":
                    component._joined.clear()
                elif reason == "stopped":
                    component.latch_stop()
                try:
                    receipt = await component.switch_avatar("123456", SWITCH_VIDEOS, "same", lambda: reason != "stale")
                    self.assertEqual(receipt["status"], "cancelled")
                    self.assertFalse(receipt["sent"])
                    duplicate = await component.switch_avatar("123456", SWITCH_VIDEOS, "same", lambda: True)
                    self.assertTrue(duplicate["duplicate"])
                    self.assertEqual(component._ws.attempts, [])
                finally:
                    if reason == "speech_lock":
                        component._speech_lock.release()

    async def test_manual_avatar_switch_rechecks_after_waiting_for_send_lock(self):
        for reason in ("stopped", "stale", "pending"):
            with self.subTest(reason=reason):
                component = self.component()
                valid = True
                await component._send_lock.acquire()
                task = asyncio.create_task(component.switch_avatar("123456", SWITCH_VIDEOS, "first", lambda: valid))
                try:
                    await eventually(lambda: component._speech_lock.locked())
                    concurrent = await component.switch_avatar("123456", SWITCH_VIDEOS, "second", lambda: True)
                    self.assertEqual(concurrent["status"], "cancelled")
                    if reason == "stopped":
                        component.latch_stop()
                    elif reason == "stale":
                        valid = False
                    else:
                        component._pending_speech = 1
                finally:
                    component._send_lock.release()
                receipt = await asyncio.wait_for(task, 0.5)
                self.assertEqual(receipt["status"], "cancelled")
                self.assertIsNone(receipt["seq_id"])
                self.assertEqual(component._avatar_switch_attempts, {})
                self.assertEqual(component._ws.attempts, [])

    async def test_manual_avatar_switch_refuses_active_speech_and_motion_until_group_done(self):
        for input_kind in ("speech", "motion"):
            with self.subTest(input_kind=input_kind):
                component = self.component()
                with patch("motion_component.compile_timeline", side_effect=compressed_timeline), \
                patch("motion_component.ACTION_POST_SEND_SECONDS", .02):
                    send = (component.send_expression(b"\0\0", "咱听着。", [short()], 1, recovery=dict(GE))
                            if input_kind == "speech" else
                            component.send_motion_expression([short()], recovery=dict(GE)))
                    task = asyncio.create_task(send)
                    await eventually(lambda: len(component._ws.motions()) == 1)
                    rejected = await component.switch_avatar("123456", SWITCH_VIDEOS, "during", lambda: True)
                    self.assertEqual(rejected["status"], "cancelled")
                    self.assertFalse(any(row["frame"]["type"] == 16 for row in component._ws.sent))
                    await asyncio.wait_for(task, 0.5)
                receipt = await component.switch_avatar("123456", SWITCH_VIDEOS, "after", lambda: True)
                self.assertTrue(receipt["sent"])
                self.assertEqual(component._ws.sent[-1]["frame"]["type"], 16)

    async def test_manual_avatar_switch_correlated_error_during_write_is_not_overwritten(self):
        component = self.component()
        original_send = component._ws.send_json

        async def send_with_immediate_error(frame):
            handled = component._handle_control_error({"type": 103, "seq_id": frame["seq_id"],
                        "payload": {"error": {"source": "switch_avatar", "code": "SWITCH_AVATAR_FAILED"}}})
            self.assertTrue(handled)
            await original_send(frame)
        component._ws.send_json = send_with_immediate_error
        receipt = await component.switch_avatar("123456", SWITCH_VIDEOS, "same", lambda: True)
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["error_code"], "SWITCH_AVATAR_FAILED")
        self.assertTrue(receipt["sent"])
        self.assertFalse(component._stop.is_set())
        self.assertEqual(component.state, "ready")
        duplicate = await component.switch_avatar("123456", SWITCH_VIDEOS, "same", lambda: True)
        self.assertEqual(duplicate["status"], "failed")
        self.assertEqual(len(component._ws.sent), 1)

    async def test_avatar_switch_reader_handles_only_attempted_seq_and_exact_error_source(self):
        cases = [(103, "switch_avatar", "known", False), (103, "switch_avatar", "unknown", True),
                 (103, "switch_avatar", "string", True), (103, "switch_avatar", "bool", True),
                 (103, "baseline_reset", "known", True), (103, None, "known", True),
                 (6, "switch_avatar", "known", True)]
        for kind, source, seq_kind, should_stop in cases:
            with self.subTest(kind=kind, source=source, seq_kind=seq_kind):
                component = self.component()
                receipt = await component.switch_avatar("123456", SWITCH_VIDEOS, "same", lambda: True)
                seq_id = {"known": receipt["seq_id"], "unknown": receipt["seq_id"]+999,
                          "string": str(receipt["seq_id"]), "bool": True}[seq_kind]
                incoming = FakeIncomingSocket()
                task = asyncio.create_task(component._read(incoming))
                try:
                    incoming.receive({"type": kind, "seq_id": seq_id,
                                      "payload": {"error": {"source": source, "code": "SWITCH_AVATAR_FAILED"}}})
                    await eventually(lambda: component.diagnostics.counts["in_total"] == 1)
                    self.assertEqual(component._stop.is_set(), should_stop)
                    if not should_stop:
                        self.assertEqual(component.public()["avatar_switch"]["status"], "failed")
                        self.assertEqual(component.error, "")
                    else:
                        self.assertEqual(component.public()["avatar_switch"]["status"], "sent")
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

    async def test_avatar_switch_error_code_and_message_cannot_leak_urls(self):
        component = self.component()
        receipt = await component.switch_avatar("123456", SWITCH_VIDEOS, "same", lambda: True)
        self.assertTrue(component._handle_control_error({"type": 103, "seq_id": receipt["seq_id"],
                       "payload": {"error": {"source": "switch_avatar", "code": SWITCH_VIDEOS[0],
                                             "message": SWITCH_VIDEOS[1]}}}))
        safe = json.dumps({"public": component.public(), "diagnostics": component.diagnostics.export()})
        self.assertNotIn("tax.shumaitech.cn", safe)
        self.assertEqual(component.avatar_switch["error_code"], "SWITCH_AVATAR_ERROR")

    async def test_avatar_switch_transport_failure_is_consumed_and_never_retried(self):
        component = self.component()
        component._ws.send_json = AsyncMock(side_effect=ConnectionError("local failure"))
        with self.assertRaises(RuntimeError):
            await component.switch_avatar("123456", SWITCH_VIDEOS, "same", lambda: True)
        self.assertTrue(component._stop.is_set())
        self.assertEqual(component.avatar_switch["status"], "send_unconfirmed")
        self.assertFalse(component.avatar_switch["sent"])
        self.assertIsNotNone(component.avatar_switch["seq_id"])
        duplicate = await component.switch_avatar("123456", SWITCH_VIDEOS, "same", lambda: True)
        self.assertEqual(duplicate["status"], "send_unconfirmed")
        component._ws.send_json.assert_awaited_once()

    async def test_each_sent_instruction_contains_its_own_ge_and_q_recovery(self):
        component = self.component()
        first, second = short(pair=True), short(delay=2)
        first.update(gaze="自然看向镜头", expression="神情略带好奇")
        second.update(gaze="望向桌面", expression="神情认真")
        recovery = {"gaze": "目光回到镜头", "expression": "神情温和，保留说话时的好奇"}
        with patch("motion_component.compile_timeline", side_effect=compressed_timeline), \
                patch("motion_component.ACTION_POST_SEND_SECONDS", .02):
            await component.send_expression(b"\0\0", "咱仔细看看。", [first, second], 1, recovery=recovery)
        self.assertEqual(len(component._ws.motions()), 3)
        for sent, event, target in zip(component._ws.motions(), component.motion_events, [first, second, recovery]):
            text = sent["frame"]["payload"]["text_msg"]["content"]
            self.assertIn(target["gaze"], text)
            self.assertIn(target["expression"], text)
            self.assertEqual(event["gaze"], target["gaze"])
            self.assertEqual(event["expression"], target["expression"])
        self.assertEqual(component.motion_events[0]["simultaneous_count"], 2)
        self.assertNotIn("望向桌面", component._ws.motions()[-1]["frame"]["payload"]["text_msg"]["content"])

    async def test_missing_ge_recovery_and_long_instruction_fail_before_send(self):
        missing = short()
        del missing["gaze"]
        huge = short(pair=True)
        for motion in huge["motions"]:
            motion["description"] = "动" * 70
        huge.update(gaze="看" * 40, expression="情" * 40)
        for actions, recovery in [([missing], GE), ([short()], None), ([huge], GE), ([], GE)]:
            with self.subTest(actions=actions, recovery=recovery):
                component = self.component()
                with self.assertRaises(ValueError):
                    await component.send_expression(b"\0\0", "咱听着。", actions, 1, recovery=recovery)
                self.assertEqual(component._ws.attempts, [])
                component._audio.assert_not_awaited()

    async def test_reading_real_pcm_then_s0_and_answer_are_one_continuous_group(self):
        # 保留真实paced _audio和真实1秒/1.5秒等待；立即返回的mock会伪造R→S0时序。
        component = self.component(mock_audio=False, write_delay=0.02)
        read_pcm = b"\x01\x00" * int(component.RATE * 0.2)
        answer_pcm = b"\x02\x00" * int(component.RATE * 1.2)
        drain_states = []
        original_wait = component._wait

        async def observed_wait(seconds):
            if component.state == "draining" and seconds == component.DRAIN_SECONDS:
                drain_states.append(seconds)
            await original_wait(seconds)

        component._wait = observed_wait
        receipt = await asyncio.wait_for(component.send_expression(
            answer_pcm, "粮价这事，咱细说。", [short()], 1, recovery=GE,
            reading={"text": "最近粮价为何涨", "pcm": read_pcm}), 5)
        frames = component._ws.sent
        self.assertEqual([row["frame"]["type"] for row in frames], [9, 10, 9, 10, 9, 9])
        self.assertEqual([e["role"] for e in component.motion_events], ["R", "S0", "S", "Q"])
        self.assertEqual([n["index"] for n in receipt["timeline"]], [1, 2, 3, 4])
        self.assertEqual([n["at_seconds"] for n in receipt["timeline"]], [0, 1.2, 2.2, 3.7])
        r, s0, s, q = component._ws.motions()
        read_text, answer_text = [row for row in frames if row["frame"]["type"] == 10]
        self.assertEqual(read_text["frame"]["payload"]["text_msg"]["content"], "最近粮价为何涨")
        self.assertEqual(answer_text["frame"]["payload"]["text_msg"]["content"], "粮价这事，咱细说。")
        self.assertGreaterEqual(read_text["started"] - r["completed"], 0.995)
        self.assertLess(read_text["started"] - r["completed"], 1.3)
        self.assertGreaterEqual(s0["started"] - read_text["completed"], 0.195)
        self.assertGreaterEqual(s0["started"] - r["completed"], 1.215)
        self.assertGreaterEqual(s["started"] - s0["completed"], 0.995)
        self.assertLess(s["started"] - s0["completed"], 1.3)
        self.assertGreaterEqual(q["started"] - s["completed"], 1.495)
        self.assertGreaterEqual(answer_text["started"], s0["completed"])
        read_binary = [row for row in component._ws.binary if row["chunk"].startswith(b"\x01\x00")]
        answer_binary = [row for row in component._ws.binary if row["chunk"].startswith(b"\x02\x00")]
        self.assertTrue(all(r["completed"] + 0.995 <= row["time"] < s0["started"] for row in read_binary))
        self.assertTrue(all(row["time"] >= s0["completed"] for row in answer_binary))
        self.assertTrue(any(row["time"] > s["completed"] for row in answer_binary))
        self.assertEqual(b"".join(row["chunk"] for row in component._ws.binary), read_pcm + answer_pcm)
        self.assertEqual(receipt["reading_text"], "最近粮价为何涨")
        self.assertEqual(receipt["reading_audio_seconds"], 0.2)
        self.assertAlmostEqual(receipt["audio_seconds"], 1.4)
        self.assertAlmostEqual(component.audio_seconds, 1.4)
        self.assertEqual(component.sent_audio_bytes, len(read_pcm) + len(answer_pcm))
        self.assertTrue(receipt["reading_sent_complete"])
        self.assertTrue(receipt["send_complete"])
        self.assertTrue(receipt["actions_sent_complete"])
        self.assertEqual(len(drain_states), 1)
        self.assertEqual(component.utterances_started, 1)
        self.assertEqual(component.utterances_sent, 1)
        self.assertEqual(len({row["frame"]["conn_id"] for row in frames}), 1)
        origin = r["completed"]
        for index, (event, row) in enumerate(zip(component.motion_events, component._ws.motions())):
            self.assertAlmostEqual(event["actual_at_seconds"], row["completed"] - origin, delta=0.003)
            previous = component._ws.motions()[index-1]["completed"] if index else row["completed"]
            self.assertAlmostEqual(event["actual_interval_seconds"], row["completed"] - previous, delta=0.003)
            self.assertFalse(event["visual_effect_confirmed"])
            for field in ("gaze", "expression"):
                self.assertIn(event[field], row["frame"]["payload"]["text_msg"]["content"])

    @patch("motion_component.ACTION_POST_SEND_SECONDS", .02)
    async def test_same_component_reading_direct_reading_never_leaks_bundle(self):
        component = self.component(mock_audio=False)
        waits = []
        original_wait = component._wait

        async def compressed_wait(seconds):
            # 只缩短等待，仍经真实校验、调度、Type10和PCM发送路径。
            waits.append(seconds)
            await original_wait(min(seconds, 0.003))

        component._wait = compressed_wait
        rounds = [
            ("最近粮价为何涨了？", "最近粮价", "粮价这事，咱细说。", b"\x01\x00"),
            ("为什么百姓辛苦一年仍然吃不饱？" * 4, None, "咱听见百姓的难处了。", b"\x02\x00"),
            ("近来天气如何？", "近来天气", "天时也得仔细看看。", b"\x03\x00"),
        ]
        receipts = []
        for source, read_text, answer, sample in rounds:
            body = {"emotion": "平静", "observation": "自然承接当前弹幕",
                    "reading": None if read_text is None else {
                        "text": read_text, "instruction": "自然清晰地短读原文。"},
                    "segments": [{"text": answer, "instruction": "自然清晰。", "actions": [],
                                  "recovery": None if read_text is None else dict(GE)}]}
            plan = parse_plan(body, source_text=source)
            frame_start, pcm_start, event_start = (len(component._ws.sent),
                                                   len(component._ws.binary), len(component.motion_events))
            waits.clear()
            # 不同轮与阅读/回答使用不同PCM，能识别前轮音频是否误入后轮。
            read_pcm, answer_pcm = sample * 24, (sample + sample) * 24
            segment = plan["segments"][0]
            reading = None if plan["reading"] is None else {
                "text": plan["reading"]["text"], "pcm": read_pcm}
            receipt = await asyncio.wait_for(component.send_expression(
                answer_pcm, segment["text"], segment["actions"], 1,
                recovery=segment["recovery"], reading=reading), 0.5)
            receipts.append(receipt)
            frames = [row["frame"] for row in component._ws.sent[frame_start:]]
            sent_pcm = b"".join(row["chunk"] for row in component._ws.binary[pcm_start:])
            roles = [event["role"] for event in component.motion_events[event_start:]]
            spoken = [frame["payload"]["text_msg"]["content"] for frame in frames if frame["type"] == 10]
            if read_text is None:
                self.assertEqual([frame["type"] for frame in frames], [10])
                self.assertEqual(spoken, [answer])
                self.assertEqual(sent_pcm, answer_pcm)
                self.assertEqual(roles, [])
                self.assertEqual(receipt["timeline"], [])
                self.assertIsNone(receipt["reading_text"])
                self.assertIsNone(component.active_motion_plan["reading_text"])
                self.assertEqual(receipt["reading_lead_seconds"], 0)
                self.assertEqual(receipt["reading_audio_seconds"], 0)
                # 只允许短PCM自身的节拍等待和drain；不能有阅读提前量或尾Q等待。
                self.assertTrue(all(seconds <= 0.01 for seconds in waits), waits)
            else:
                self.assertEqual([frame["type"] for frame in frames], [9, 10, 9, 10, 9])
                self.assertEqual(roles, ["R", "S0", "Q"])
                self.assertEqual(spoken, [read_text, answer])
                self.assertEqual(sent_pcm, read_pcm + answer_pcm)
                self.assertEqual(receipt["reading_text"], read_text)
                self.assertTrue(any(seconds > 0.9 for seconds in waits))
            self.assertTrue(receipt["send_complete"])
            self.assertTrue(receipt["actions_sent_complete"])
            self.assertEqual(component.state, "ready")
        self.assertEqual(component.utterances_sent, 3)
        self.assertEqual(len({receipt["group_id"] for receipt in receipts}), 3)
        self.assertEqual(len({row["frame"]["conn_id"] for row in component._ws.sent}), 1)

    async def test_reading_empty_answer_actions_still_send_s0_and_one_tail_q(self):
        component = self.component(mock_audio=False)
        read_pcm = b"\x01\x00" * int(component.RATE * 0.06)
        receipt = await asyncio.wait_for(component.send_expression(
            b"\0\0", "咱听着。", [], 1, recovery=GE,
            reading={"text": "啊", "pcm": read_pcm}), 3.5)
        self.assertEqual([e["role"] for e in component.motion_events], ["R", "S0", "Q"])
        _, s0, q = component._ws.motions()
        self.assertGreaterEqual(q["started"] - s0["completed"], 1.495)
        self.assertEqual(receipt["timeline"][-1]["delay_seconds"], 1.5)
        self.assertEqual(receipt["reading_text"], "啊")
        self.assertTrue(receipt["actions_sent_complete"])

    async def test_stop_during_reading_lead_never_sends_text_pcm_or_recovery(self):
        component = self.component(mock_audio=False)
        task = asyncio.create_task(component.send_expression(
            b"\x02\x00" * 100, "咱听着。", [short()], 1, recovery=GE,
            reading={"text": "最近粮价", "pcm": b"\x01\x00" * 100}))
        await eventually(lambda: len(component.motion_events) == 1)
        component.latch_stop()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(task, 0.5)
        self.assertEqual([row["frame"]["type"] for row in component._ws.sent], [9])
        self.assertEqual(component._ws.binary, [])
        self.assertFalse(component.last_utterance["reading_sent_complete"])
        self.assertFalse(component.active_motion_plan["is_active"])

    async def test_stop_during_paced_reading_never_sends_s0_answer_or_q(self):
        component = self.component(mock_audio=False)
        task = asyncio.create_task(component.send_expression(
            b"\x02\x00" * 100, "咱听着。", [short()], 1, recovery=GE,
            reading={"text": "最近粮价", "pcm": b"\x01\x00" * int(component.RATE * 0.4)}))
        await eventually(lambda: len(component._ws.binary) >= 2, timeout=2)
        component.latch_stop()
        with self.assertRaises(RuntimeError):
            await asyncio.wait_for(task, 0.5)
        self.assertEqual([e["role"] for e in component.motion_events], ["R"])
        self.assertEqual([row["frame"]["type"] for row in component._ws.sent], [9, 10])
        self.assertFalse(component.last_utterance["reading_sent_complete"])
        self.assertFalse(component.last_utterance["send_complete"])
        self.assertFalse(component.last_utterance["actions_sent_complete"])
        self.assertFalse(component.active_motion_plan["is_active"])
        count = len(component._ws.attempts)
        await asyncio.sleep(0.02)
        self.assertEqual(len(component._ws.attempts), count)

    async def test_failed_r_or_s0_never_continues_to_answer_or_q(self):
        for failed_index in (1, 2):
            with self.subTest(failed_index=failed_index):
                component = self.component(mock_audio=False, fail_motion=failed_index)
                read_pcm = b"\x01\x00" * int(component.RATE * 0.06)
                with self.assertRaises(ConnectionError):
                    await component.send_expression(b"\x02\x00" * 100, "咱听着。", [], 1,
                                                    recovery=GE, reading={"text": "你好", "pcm": read_pcm})
                self.assertEqual([e["role"] for e in component.motion_events], ["R"] if failed_index == 2 else [])
                self.assertEqual([row["frame"]["type"] for row in component._ws.sent], [9, 10] if failed_index == 2 else [])
                self.assertEqual(b"".join(row["chunk"] for row in component._ws.binary), read_pcm if failed_index == 2 else b"")
                self.assertEqual(component.last_utterance["reading_sent_complete"], failed_index == 2)
                self.assertFalse(component.last_utterance["send_complete"])
                self.assertFalse(component.last_utterance["actions_sent_complete"])
                self.assertTrue(component._stop.is_set())
                self.assertFalse(component.active_motion_plan["is_active"])

    async def test_both_pcm_and_reading_contract_validate_before_any_frame(self):
        invalid_audio = (b"", b"\0", bytearray(b"\0\0"), None)
        cases = [(audio, {"text": "你好", "pcm": b"\0\0"}, GE) for audio in invalid_audio]
        cases += [(b"\0\0", {"text": "你好", "pcm": audio}, GE) for audio in invalid_audio]
        cases += [(b"\0\0", reading, GE) for reading in (
            {}, [], {"text": "你好"}, {"text": "你好", "pcm": b"\0\0", "instruction": "额外"},
            {"text": "", "pcm": b"\0\0"}, {"text": "字" * 31, "pcm": b"\0\0"})]
        cases.append((b"\0\0", {"text": "你好", "pcm": b"\0\0"}, None))
        for pcm, reading, recovery in cases:
            with self.subTest(pcm=pcm, reading=reading, recovery=recovery):
                component = self.component()
                with self.assertRaises(ValueError):
                    await component.send_expression(pcm, "咱听着。", [], 1, recovery=recovery, reading=reading)
                self.assertEqual(component._ws.attempts, [])
                component._audio.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
