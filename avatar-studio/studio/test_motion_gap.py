"""Motion-only cooldown checks; real writes with an isolated manual clock."""
import asyncio
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from component_live import _Stopped
from motion_component import MotionComponent
from test_motion_component import FakeSocket, NoNetwork, GE, short
from test_semantic_progress import ManualClock


def node(kind, index):
    return {"kind": kind, "role": kind, "index": index,
            "at_seconds": 0.0, "delay_seconds": 0.0,
            "instruction": "动作：正常坐姿。", **GE,
            "motions": [] if kind in {"GE", "Q"} else [{"part": "head", "description": "点头"}]}


class MotionGapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="motion-gap-offline-")
        self.addCleanup(self.temp.cleanup)
        self.clock = ManualClock()
        self.patch = patch("motion_component.time", SimpleNamespace(
            monotonic=lambda: self.clock.now, time=time.time))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.tasks = []
        self.count = 0

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def component(self):
        self.count += 1
        out = Path(self.temp.name) / str(self.count)
        c = MotionComponent(NoNetwork(), out, out / "unused.png")
        c._ws = FakeSocket()
        c.live_id = "offline-only"
        c.state = "ready"
        c._ready.set(); c._joined.set()
        c._motion_epoch = 1
        c.active_motion_plan = {"group_id": "test"}
        c._wait = lambda seconds: self.clock.wait(c, seconds)
        return c

    def spawn(self, awaitable):
        task = asyncio.create_task(awaitable)
        self.tasks.append(task)
        return task

    def write(self, c, kind, index, *, previous=None):
        return c._write_motion(node(kind, index), c._motion_epoch, "test", 1,
                               None, previous)

    async def test_each_s_l_q_holds_next_ge_for_one_second(self):
        for kind in ("S", "L", "Q"):
            with self.subTest(kind=kind):
                c = self.component()
                sent = await self.write(c, kind, 1)
                task = self.spawn(self.write(c, "GE", 2, previous=sent))
                await self.clock.advance(.999)
                self.assertEqual(len(c._ws.motions()), 1)
                await self.clock.advance(.002)
                self.assertTrue(task.done())
                self.assertGreaterEqual(await task - sent, 1.0)
                # GE has its own scheduler lead; it does not create an extra
                # one-second floor. The next motion may use its existing lead.
                await self.clock.advance(.5)
                third = await self.write(c, "L", 3)
                self.assertLess(third - c.motion_events[1]["sent_at_monotonic"], 1.0)

    async def test_deadline_starts_after_successful_socket_write(self):
        c = self.component()
        original_send = c._ws.send_json
        async def slow_first(frame):
            if not c._ws.sent:
                await c._wait(.4)
            await original_send(frame)
        c._ws.send_json = slow_first
        first = self.spawn(self.write(c, "S", 1))
        await self.clock.advance(.4)
        sent = await first
        task = self.spawn(self.write(c, "GE", 2, previous=sent))
        await self.clock.advance(.999)
        self.assertFalse(task.done())
        self.assertEqual(len(c._ws.sent), 1)
        await self.clock.advance(.002)
        self.assertGreaterEqual(await task - sent, 1.0)

    async def test_motion_wait_does_not_hold_text_or_pcm_socket_lock(self):
        c = self.component()
        await self.write(c, "S", 1)
        motion = self.spawn(self.write(c, "GE", 2))
        await self.clock.settle()
        text = self.spawn(c._send(10, {"text_msg": {"content": "离线音频"}}))
        pcm = b"\0\0" * 2400
        audio = self.spawn(c._audio(pcm, count_audio=True))
        await self.clock.settle()
        self.assertTrue(text.done())
        await text
        self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), pcm)
        self.assertFalse(motion.done())
        await self.clock.advance(.2)
        self.assertTrue(audio.done())
        await audio
        self.assertFalse(motion.done())

    async def test_tail_q_blocks_first_action_of_next_expression(self):
        c = self.component()
        first = self.spawn(c.send_motion_expression([short()], recovery=GE, number=1))
        await self.clock.advance(1.5)
        self.assertTrue(first.done())
        self.assertEqual([e["role"] for e in (await first)["motion_events"]], ["S", "Q"])
        q_at = c.motion_events[-1]["sent_at_monotonic"]
        second = self.spawn(c.send_motion_expression([short()], recovery=GE, number=2))
        await self.clock.advance(.999)
        self.assertEqual(len(c._ws.motions()), 2)
        await self.clock.advance(.002)
        self.assertEqual(len(c._ws.motions()), 3)
        self.assertGreaterEqual(c.motion_events[-1]["sent_at_monotonic"] - q_at, 1.0)
        await self.clock.advance(1.5)
        self.assertTrue((await second)["actions_sent_complete"])

    async def test_stopped_cancelled_or_expired_wait_cannot_send(self):
        for reason in ("stop", "cancel", "expired"):
            with self.subTest(reason=reason):
                c = self.component()
                await self.write(c, "Q", 1)
                task = self.spawn(self.write(c, "S", 2))
                await self.clock.advance(.4)
                if reason == "stop":
                    c.latch_stop()
                elif reason == "cancel":
                    task.cancel()
                else:
                    c._motion_epoch += 1
                await self.clock.advance(1)
                expected = asyncio.CancelledError if reason == "cancel" else _Stopped
                with self.assertRaises(expected):
                    await task
                self.assertEqual(len(c._ws.motions()), 1)
                self.assertNotIn(("test", 2), c._motion_claims)

    async def test_competing_motion_rechecks_deadline_after_lock(self):
        c = self.component()
        await self.write(c, "Q", 1)
        next_s = self.spawn(self.write(c, "S", 2))
        next_ge = self.spawn(self.write(c, "GE", 3))
        await self.clock.advance(1.001)
        self.assertTrue(next_s.done())
        self.assertFalse(next_ge.done())
        self.assertEqual(len(c._ws.motions()), 2)
        await self.clock.advance(1)
        self.assertGreaterEqual(await next_ge - await next_s, 1.0)


if __name__ == "__main__":
    unittest.main()
