"""Offline scheduler, timing and ownership regressions; HTTP access is forbidden."""
import asyncio
import copy
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from motion_component import MotionComponent
from idle_motion import IdleRecoveryError
from test_motion_component import FakeSocket, NoNetwork, GE, short, SWITCH_VIDEOS
from test_semantic_progress import ManualClock
from test_semantic_transport import fixture


def idle_action(identity="a", *, cooldown=60, hold=3):
    return {"id": identity, "name": identity, "instruction": "动作：离线测试" + identity,
            "hold_seconds": hold, "cooldown_seconds": cooldown, "weight": 1,
            "recovery_instruction": "动作：正常坐姿。\n眼神：注视镜头。\n表情：神情自然。",
            "description_sha256": "a" * 64}


class HeldInitial(FakeSocket):
    def __init__(self):
        super().__init__()
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def send_json(self, frame):
        if frame.get("payload", {}).get("text_msg", {}).get("content", "").startswith("动作：离线测试"):
            self.entered.set()
            await self.release.wait()
        await super().send_json(frame)


class IdleMotionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="idle-motion-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.clock = ManualClock()
        clock = SimpleNamespace(monotonic=lambda: self.clock.now, time=time.time)
        for module in ("idle_motion.time", "motion_component.time", "component_live.time"):
            p = patch(module, clock)
            p.start()
            self.addCleanup(p.stop)
        choice = patch("idle_motion.random.choices", side_effect=lambda rows, **kw: [rows[0]])
        choice.start()
        self.addCleanup(choice.stop)
        self.allowed, self.interrupt, self.valid = True, False, True
        self.components, self.tasks = [], []

    async def asyncTearDown(self):
        for c in self.components:
            c.latch_stop()
            if isinstance(c._ws, HeldInitial):
                c._ws.release.set()
            for task in (c._idle_motions.task,):
                if task and not task.done():
                    task.cancel()
                if task:
                    self.tasks.append(task)
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def component(self, socket=None):
        out = Path(self.tmp.name) / str(len(self.components))
        c = MotionComponent(NoNetwork(), out, out / "unused.png")
        c._ws = socket or FakeSocket()
        c.live_id, c.state = "offline-only", "ready"
        c._ready.set(); c._joined.set()
        c.DRAIN_SECONDS = 0
        c._wait = lambda seconds: self.clock.wait(c, seconds)
        self.components.append(c)
        return c

    async def start(self, c, actions=None, *, once=False):
        actions = actions or [idle_action()]
        c.start_idle_motions(actions, {"min_interval_seconds": 15, "max_interval_seconds": 15},
            lambda: self.allowed, lambda: self.interrupt, lambda: self.valid,
            once=actions[0]["id"] if once else None)
        await self.clock.settle()

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task

    @staticmethod
    def roles(c):
        return [e["role"] for e in c.idle_motions_public()["recent"] if e["status"] == "sent"]

    async def test_continuous_idle_timer_is_reset_by_busy_without_catchup(self):
        c = self.component()
        self.assertFalse(c.idle_motions_public()["enabled"])
        await self.start(c)
        await self.clock.advance(14)
        self.assertEqual(c._ws.sent, [])
        self.allowed = False
        await self.clock.advance(20)
        self.allowed = True
        await self.clock.advance(14)
        self.assertEqual(c._ws.sent, [])
        await self.clock.advance(1.2)
        self.assertEqual(self.roles(c), ["I"])
        self.assertEqual(c.motion_groups_started, 0)
        self.assertEqual(c.utterances_started, 0)
        self.assertFalse(c.idle_motions_public()["visual_effect_confirmed"])

    async def test_hold_releases_speech_and_selection_only_does_not_recover_early(self):
        c = self.component()
        await self.start(c, once=True)
        self.assertEqual(self.roles(c), ["I"])
        self.assertFalse(c._speech_lock.locked())
        self.allowed = False
        await self.clock.advance(2.8)
        self.assertEqual(self.roles(c), ["I"])
        await self.clock.advance(.3)
        self.assertEqual(self.roles(c), ["I", "Q"])
        await self.clock.advance(1.1)
        self.assertFalse(c.idle_motions_public()["enabled"])

    async def test_interrupt_recovers_at_1_1_and_waits_one_second_before_content(self):
        c = self.component()
        await self.start(c, once=True)
        self.interrupt = True
        self.allowed = False
        await self.clock.advance(1.09)
        self.assertEqual(self.roles(c), ["I"])
        await self.clock.advance(.11)
        self.assertEqual(self.roles(c), ["I", "Q"])
        pcm, _ = fixture(.1)
        speech = self.spawn(c.send_expression(pcm, "甲", [], 1))
        await self.clock.advance(.8)
        self.assertEqual(len(c._ws.sent), 2)
        await self.clock.advance(.4)
        await speech
        self.assertEqual(self.roles(c), ["I", "Q"])
        self.assertEqual(len(c._ws.binary), 1)

    async def test_all_content_entries_share_exactly_one_q_and_guard(self):
        for kind in ("semantic", "expression", "motion", "legacy"):
            c = self.component()
            await self.start(c, once=True)
            pcm, _ = fixture(.1)
            if kind == "semantic":
                coro = c.send_aligned_expression(pcm, "甲", [], 1, action_kind="S", ge_lead_seconds=1.1)
            elif kind == "expression":
                coro = c.send_expression(pcm, "甲", [], 1)
            elif kind == "motion":
                coro = c.send_motion_expression([short()], recovery=GE)
            else:
                coro = c.send_audio(pcm, "甲")
            task = self.spawn(coro)
            await self.clock.advance(2.05)
            if task.done():
                await task
            self.assertEqual(len(c._ws.sent), 2, kind)
            await self.clock.advance(6)
            await task
            self.assertEqual(self.roles(c), ["I", "Q"], kind)
            self.assertFalse(c.idle_motions_public()["enabled"])

    async def test_inflight_i_finishes_before_same_q_then_content(self):
        socket = HeldInitial()
        c = self.component(socket)
        await self.start(c, once=True)
        self.assertTrue(socket.entered.is_set())
        task = self.spawn(c.send_expression(b"\0\0"*2400, "甲", [], 1))
        await self.clock.advance(1)
        self.assertEqual(socket.sent, [])
        socket.release.set()
        await self.clock.advance(4)
        await task
        self.assertEqual(self.roles(c), ["I", "Q"])
        events = [e for e in c.idle_motions_public()["recent"] if e["status"] == "sent"]
        self.assertGreaterEqual(events[1]["sent_at_monotonic"]-events[0]["sent_at_monotonic"], 1.1-1e-6)

    async def test_cancel_before_initial_write_sends_zero_frames(self):
        c = self.component()
        await c._send_lock.acquire()
        await self.start(c, once=True)
        off = self.spawn(c.stop_idle_motions())
        await self.clock.settle()
        c._send_lock.release()
        await self.clock.advance(.2)
        await off
        self.assertEqual(c._ws.sent, [])
        self.assertFalse(c.idle_motions_public()["needs_recovery"])

    async def test_single_test_becoming_busy_before_write_is_cancelled_without_late_replay(self):
        c = self.component()
        await c._send_lock.acquire()
        await self.start(c, once=True)
        self.allowed = False
        c._send_lock.release()
        await self.clock.advance(.2)
        self.allowed = True
        await self.clock.advance(30)
        self.assertEqual(c._ws.sent, [])
        self.assertFalse(c.idle_motions_public()["enabled"])

    async def test_cooldowns_no_consecutive_repeat_and_wait_without_candidate(self):
        c = self.component()
        await self.start(c, [idle_action("a", cooldown=100), idle_action("b", cooldown=100)])
        await self.clock.advance(45)
        ids = [e["action_id"] for e in c.idle_motions_public()["recent"]
               if e["role"] == "I" and e["status"] == "sent"]
        self.assertEqual(ids, ["a", "b"])
        self.assertEqual(c.idle_motions_public()["status"], "waiting_candidates")
        await self.clock.advance(50)
        self.assertEqual(c.idle_motions_public()["i_sent"], 2)

    async def test_stop_recovers_once_and_running_module_rejects_another_start(self):
        c = self.component()
        await self.start(c, once=True)
        with self.assertRaises(ValueError):
            await self.start(c)
        off = self.spawn(c.stop_idle_motions())
        await self.clock.advance(3)
        await off
        await c.stop_idle_motions()
        self.assertEqual(self.roles(c), ["I", "Q"])

    async def test_avatar_handoff_after_nonrecovering_disable_still_waits_for_q_and_guard(self):
        c = self.component()
        await self.start(c, once=True)
        off = self.spawn(c.stop_idle_motions(recover=False))
        await self.clock.advance(.05)
        await off
        self.assertTrue(c.idle_motions_public()["needs_recovery"])
        switched = self.spawn(c.switch_avatar("123", SWITCH_VIDEOS, "offline-switch", lambda: True))
        await self.clock.advance(2)
        self.assertEqual([r["frame"]["type"] for r in c._ws.sent], [9, 9])
        await self.clock.advance(.2)
        self.assertTrue((await switched)["sent"])
        self.assertEqual([r["frame"]["type"] for r in c._ws.sent], [9, 9, 16])
        self.assertEqual(self.roles(c), ["I", "Q"])
        self.assertEqual(c._motion_control_pending, 0)

    async def test_q_failure_latches_blocks_every_content_entry_without_retry_until_explicit(self):
        c = self.component(FakeSocket(fail_motion=2))
        await self.start(c, once=True)
        await self.clock.advance(5)
        self.assertTrue(c.idle_motions_public()["recovery_failed"])
        count = len(c._ws.attempts)
        pcm, _ = fixture(.1)
        calls = (c.send_expression(pcm, "甲", [], 1),
                 c.send_aligned_expression(pcm, "甲", [], 1, action_kind="S", ge_lead_seconds=1.1),
                 c.send_motion_expression([short()], recovery=GE), c.send_audio(pcm, "甲"))
        for coro in calls:
            with self.assertRaises(IdleRecoveryError):
                await coro
        self.assertEqual(len(c._ws.attempts), count)
        self.assertFalse(c._stop.is_set())
        restore = self.spawn(c.recover_idle_motions())
        await self.clock.advance(3)
        await restore
        self.assertFalse(c.idle_motions_public()["needs_recovery"])
        self.assertFalse(c.idle_motions_public()["recovery_failed"])
        speech = self.spawn(c.send_expression(pcm, "甲", [], 1))
        await self.clock.advance(1)
        await speech
        self.assertEqual(self.roles(c), ["I", "Q"])

    async def test_unconfirmed_i_is_not_retried_but_retains_recovery(self):
        c = self.component(FakeSocket(fail_motion=1))
        await self.start(c, once=True)
        await self.clock.advance(30)
        self.assertEqual(len(c._ws.attempts), 1)
        self.assertTrue(c.idle_motions_public()["needs_recovery"])
        off = self.spawn(c.stop_idle_motions())
        await self.clock.advance(2)
        await off
        self.assertEqual(self.roles(c), ["Q"])

    async def test_q_failure_during_content_does_not_stop_connection_or_repeat_q(self):
        c = self.component(FakeSocket(fail_motion=2))
        await self.start(c, once=True)
        speech = self.spawn(c.send_expression(b"\0\0"*2400, "甲", [], 1))
        await self.clock.advance(2)
        with self.assertRaises(IdleRecoveryError):
            await speech
        self.assertFalse(c._stop.is_set())
        self.assertEqual(len(c._ws.attempts), 2)
        self.assertEqual(c._ws.binary, [])
        with self.assertRaises(IdleRecoveryError):
            await c.send_expression(b"\0\0"*2400, "甲", [], 1)
        self.assertEqual(len(c._ws.attempts), 2)

    async def test_ordinary_control_disable_cannot_retry_failed_q_but_explicit_restore_can(self):
        c = self.component(FakeSocket(fail_motion=2))
        await self.start(c, once=True)
        await self.clock.advance(5)
        self.assertTrue(c.idle_motions_public()["recovery_failed"])
        attempts = len(c._ws.attempts)
        with self.assertRaises(IdleRecoveryError):
            await c.stop_idle_motions(recover=True, explicit=False)
        self.assertEqual(len(c._ws.attempts), attempts)
        self.assertTrue(c.idle_motions_public()["needs_recovery"])
        restore = self.spawn(c.recover_idle_motions())
        await self.clock.advance(3)
        await restore
        self.assertEqual(self.roles(c), ["I", "Q"])
        self.assertFalse(c.idle_motions_public()["recovery_failed"])

    async def test_cancelled_q_is_ambiguous_and_requires_explicit_recovery(self):
        c = self.component()
        entered, release = asyncio.Event(), asyncio.Event()
        original = c._ws.send_json
        async def held_q(frame):
            if frame["payload"]["text_msg"]["content"] == idle_action()["recovery_instruction"]:
                entered.set()
                await release.wait()
            await original(frame)
        c._ws.send_json = held_q
        await self.start(c, once=True)
        await self.clock.advance(3.2)
        self.assertTrue(entered.is_set())
        c._idle_motions.task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await c._idle_motions.task
        with self.assertRaises(IdleRecoveryError):
            await c.send_expression(b"\0\0"*2400, "甲", [], 1)
        release.set()
        restore = self.spawn(c.recover_idle_motions())
        await self.clock.advance(3)
        await restore
        self.assertEqual(self.roles(c), ["I", "Q"])

    async def test_concurrent_content_and_manual_stop_share_one_q(self):
        c = self.component()
        await self.start(c, once=True)
        first = self.spawn(c.send_expression(b"\0\0"*2400, "甲", [], 1))
        second = self.spawn(c.send_expression(b"\0\0"*2400, "乙", [], 2))
        off = self.spawn(c.stop_idle_motions())
        await self.clock.advance(5)
        await first; await second; await off
        self.assertEqual(self.roles(c), ["I", "Q"])
        self.assertEqual(c.utterances_sent, 2)

    async def test_invalid_session_and_latch_stop_never_send_q(self):
        for invalid in ("session", "stop"):
            self.valid = True
            c = self.component()
            await self.start(c, once=True)
            if invalid == "session":
                self.valid = False
            else:
                c.latch_stop()
            await self.clock.advance(8)
            self.assertEqual(self.roles(c), ["I"])
            self.assertFalse(c.idle_motions_public()["enabled"])

    async def test_single_test_refuses_busy_and_snapshots_are_immutable(self):
        c = self.component()
        self.allowed = False
        with self.assertRaises(ValueError):
            c.test_idle_motion(idle_action(), lambda: self.allowed, lambda: False)
        self.assertEqual(c._ws.sent, [])
        self.allowed = True
        actions = [idle_action()]
        original = copy.deepcopy(actions)
        await self.start(c, actions)
        actions[0]["instruction"] = "mutated"
        await self.clock.advance(15.1)
        self.assertEqual(c._ws.sent[0]["frame"]["payload"]["text_msg"]["content"], original[0]["instruction"])


if __name__ == "__main__":
    unittest.main()
