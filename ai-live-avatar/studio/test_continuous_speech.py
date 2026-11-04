"""连续口播传输离线验收：真实六秒PCM帧节拍，禁止连接真实网络。"""
import asyncio
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock

from motion_component import MotionComponent
from test_motion_component import FakeSocket, NoNetwork, GE, short, long, eventually


class DelayedSocket(FakeSocket):
    def __init__(self, *, delayed_node=None, node_delay=0, pcm_delay=0, **kwargs):
        super().__init__(**kwargs)
        self.delayed_node, self.node_delay, self.pcm_delay = delayed_node, node_delay, pcm_delay

    async def send_json(self, frame):
        if frame["type"] == 9 and self.motion_attempts + 1 == self.delayed_node:
            await asyncio.sleep(self.node_delay)
        await super().send_json(frame)

    async def send_bytes(self, chunk):
        if self.pcm_delay:
            await asyncio.sleep(self.pcm_delay)
        await super().send_bytes(chunk)


def actions():
    return [short(), short(3.0)]


class ContinuousSpeechTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="continuous-speech-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.count = 0

    def component(self, *, continuous=True, short_mode=False, socket=None):
        self.count += 1
        out = Path(self.tmp.name) / str(self.count)
        c = MotionComponent(NoNetwork(), out, out / "unused.png",
                            continuous_speech_test=continuous, short_performance_test=short_mode)
        c._ws = socket or FakeSocket()
        c.live_id = "offline-only"
        c._ready.set(); c._joined.set(); c.state = "ready"
        c.DRAIN_SECONDS = 0
        return c

    async def send(self, c, *, pcm=None, action_nodes=None, **kwargs):
        # 六秒原始PCM，经过真实_audio逐100ms发送，不模拟成一次空调用。
        return await c.send_expression(pcm if pcm is not None else b"\x01\x00" * (c.RATE * 6),
                                       "咱接着说两句话。", actions() if action_nodes is None else action_nodes,
                                       1, recovery=GE, **kwargs)

    def assert_gap(self, next_row, previous_row, seconds):
        self.assertGreaterEqual(next_row["started"] - previous_row["completed"], seconds - 0.015)

    async def test_one_text_one_pcm_stream_two_complete_groups(self):
        c = self.component(socket=FakeSocket(write_delay=0.025))
        drains = []
        real_wait = c._wait
        c.DRAIN_SECONDS = 4.0

        async def capture_drain(seconds):
            if seconds == 4.0:
                drains.append(time.monotonic())
                return
            await real_wait(seconds)

        c._wait = capture_drain
        pcm = b"\x01\x00" * (c.RATE * 6)
        receipt = await self.send(c, pcm=pcm)
        rows = c._ws.motions()
        self.assertEqual(len(rows), 6)
        self.assertEqual(sum(row["frame"]["type"] == 10 for row in c._ws.sent), 1)
        self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), pcm)
        self.assertEqual(len(c._ws.binary), 60)
        self.assertEqual(len(drains), 1)
        self.assertGreater(drains[0], c._ws.binary[-1]["time"])
        self.assertGreaterEqual(c._ws.binary[-1]["time"] - c._ws.binary[0]["time"], 5.8)
        for start in (0, 3):
            self.assert_gap(rows[start+1], rows[start], 0.5)
            self.assert_gap(rows[start+2], rows[start+1], 1.5)
        first = c._ws.binary[0]["time"]
        self.assertGreaterEqual(rows[3]["started"] - first, 2.49)
        before_ge2 = [row for row in c._ws.binary if row["time"] <= rows[3]["started"]]
        self.assertGreaterEqual(sum(len(row["chunk"]) for row in before_ge2), c.RATE * 2 * 2.5)
        self.assertGreaterEqual(rows[3]["started"], rows[2]["completed"])
        for left, right in ((rows[2]["completed"], rows[3]["started"]),
                            (rows[3]["completed"], rows[4]["started"]),
                            (rows[4]["completed"], rows[5]["started"])):
            self.assertTrue(any(left < row["time"] < right for row in c._ws.binary))
        self.assertLess(max(b["time"]-a["time"] for a, b in zip(c._ws.binary, c._ws.binary[1:])), 0.25)
        self.assertEqual([n["role"] for n in receipt["timeline"]], ["GE", "S", "Q"] * 2)
        self.assertEqual([n["at_seconds"] for n in receipt["timeline"]], [0, .5, 2, 3, 3.5, 5])
        self.assertEqual([n["group_index"] for n in receipt["motion_events"]], [1, 1, 1, 2, 2, 2])
        self.assertEqual(c._motion_epoch, 1)
        self.assertEqual(receipt["mode"], "continuous_speech_test")
        self.assertEqual(receipt["pcm_transport_sent_bytes"], len(pcm))
        self.assertTrue(all(group["actions_sent_complete"] for group in receipt["groups"]))
        self.assertNotIn("playback_complete_confirmed", receipt)
        self.assertEqual(c.public()["mode"], "continuous_speech_test")

    async def test_delayed_q1_holds_ge2_then_preserves_second_group_gaps(self):
        c = self.component(socket=DelayedSocket(delayed_node=3, node_delay=1.35))
        await self.send(c)
        rows = c._ws.motions()
        # Q1阻塞使GE2晚于计划；仍须从Q1实际成功写入后留足一秒。
        self.assertGreater(rows[3]["started"] - rows[0]["completed"], 3.3)
        self.assertGreaterEqual(rows[3]["started"] - rows[2]["completed"], 1.0)
        self.assert_gap(rows[4], rows[3], .5)
        self.assert_gap(rows[5], rows[4], 1.5)

    async def test_pcm_progress_gate_waits_for_successful_bytes(self):
        c = self.component(socket=DelayedSocket(pcm_delay=.12))
        await self.send(c)
        ge2 = c._ws.motions()[3]
        binary = [row for row in c._ws.binary if row["time"] <= ge2["started"]]
        self.assertGreaterEqual(len(binary), 25)
        self.assertGreater(ge2["started"] - c._ws.binary[0]["time"], 2.7)
        self.assertAlmostEqual(c.last_utterance["first_pcm_sent_at_monotonic"],
                               c._ws.binary[0]["time"], delta=.002)

    async def test_invalid_and_short_audio_have_zero_writes(self):
        invalids = [[], [short()], [short(), short(2.9)], [short(), long()],
                    [short(pair=True), short(3)], [short(), short(3), short(3)]]
        invalids.append([{**short(), "delay_seconds": .1}, short(3)])
        for nodes in invalids:
            c = self.component()
            with self.subTest(nodes=nodes), self.assertRaises(ValueError):
                await self.send(c, action_nodes=nodes)
            self.assertFalse(c._ws.attempts); self.assertFalse(c._ws.binary)
        for pcm in (b"\0\0", b"\0\0" * (24000*5-1), b"x"):
            c = self.component()
            with self.subTest(length=len(pcm)), self.assertRaises(ValueError):
                await self.send(c, pcm=pcm)
            self.assertFalse(c._ws.attempts); self.assertFalse(c._ws.binary)
        c = self.component()
        with self.assertRaises(ValueError):
            await self.send(c, reading={"text": "短读", "pcm": b"\0\0"})
        with self.assertRaises(ValueError):
            await c.send_motion_expression(actions(), recovery=GE)
        self.assertFalse(c._ws.attempts)
        with self.assertRaises(ValueError):
            self.component(short_mode=True)

    async def test_stop_in_each_unsent_node_window_cancels_remainder(self):
        for sent_count in range(1, 6):
            c = self.component()
            task = asyncio.create_task(self.send(c))
            await eventually(lambda: len(c._ws.motions()) == sent_count, timeout=8)
            c.latch_stop()
            with self.subTest(sent_count=sent_count), self.assertRaises(RuntimeError):
                await task
            count = len(c._ws.sent); binary_count = len(c._ws.binary)
            await asyncio.sleep(.03)
            self.assertEqual(len(c._ws.motions()), sent_count)
            self.assertEqual(len(c._ws.sent), count)
            self.assertEqual(len(c._ws.binary), binary_count)
            self.assertFalse(c.last_utterance["actions_sent_complete"])

    async def test_failed_s_or_q_never_sends_later_nodes(self):
        for failed_node in (2, 3, 5, 6):
            c = self.component(socket=FakeSocket(fail_motion=failed_node))
            with self.subTest(failed_node=failed_node), self.assertRaises((ConnectionError, RuntimeError)):
                await self.send(c)
            self.assertEqual(c._ws.motion_attempts, failed_node)
            self.assertEqual(len(c._ws.motions()), failed_node-1)
            self.assertTrue(c._stop.is_set())
            if failed_node == 2:
                self.assertFalse(c._ws.binary)
                self.assertFalse(any(row["frame"]["type"] == 10 for row in c._ws.sent))

    async def test_short_and_normal_keep_existing_single_group_transport(self):
        for short_mode in (False, True):
            c = self.component(continuous=False, short_mode=short_mode)
            c._audio = AsyncMock()
            receipt = await c.send_expression(b"\0\0", "一句话", [short()], 1, recovery=GE)
            self.assertEqual([node["role"] for node in receipt["timeline"]],
                             ["GE", "S", "Q"] if short_mode else ["S", "Q"])
            c._audio.assert_awaited_once_with(b"\0\0", count_audio=True)
            self.assertEqual(sum(row["frame"]["type"] == 10 for row in c._ws.sent), 1)
            self.assertFalse(receipt["continuous_speech_test"])

    async def test_legacy_audio_route_cannot_bypass_the_continuous_contract(self):
        c = self.component()
        for entry in (c.send_audio, c._send_audio):
            with self.assertRaisesRegex(ValueError, "完整表演入口"):
                await entry(b"\0\0", "短句", "")
        self.assertFalse(c._ws.attempts)
        self.assertFalse(c._ws.binary)
        self.assertEqual(c._pending_speech, 0)


if __name__ == "__main__":
    unittest.main()
