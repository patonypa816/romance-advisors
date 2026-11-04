"""Offline regressions for integer PCM gates; writes never prove cloud playback."""
import asyncio
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from motion_component import MotionComponent
from test_motion_component import FakeSocket, NoNetwork, GE
from test_semantic_transport import action, fixture


class ManualClock:
    """Advance only explicit local deadlines, without changing asyncio's clock."""

    def __init__(self):
        self.now = 100.0
        self.waiters = []

    async def settle(self):
        for _ in range(12):
            await asyncio.sleep(0)

    async def wait(self, component, seconds):
        component._check()
        if seconds > 0:
            future = asyncio.get_running_loop().create_future()
            self.waiters.append((self.now + seconds, future))
            try:
                await future
            finally:
                self.waiters = [(at, waiter) for at, waiter in self.waiters
                                if waiter is not future]
        else:
            await asyncio.sleep(0)
        component._check()

    async def advance(self, seconds):
        target = self.now + seconds
        await self.settle()
        while self.now < target:
            deadlines = [at for at, future in self.waiters if not future.done()]
            self.now = min(target, min(deadlines)) if deadlines else target
            for at, future in list(self.waiters):
                if at <= self.now and not future.done():
                    future.set_result(None)
            await self.settle()


class SemanticProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="semantic-progress-offline-")
        self.addCleanup(self.temp.cleanup)
        self.clock = ManualClock()
        # Patch the module's reference, not the shared time module: asyncio must
        # retain its real clock so a broken regression cannot hang the test loop.
        self.time_patch = patch("motion_component.time", SimpleNamespace(
            monotonic=lambda: self.clock.now, time=time.time))
        self.time_patch.start()
        self.addCleanup(self.time_patch.stop)
        self.tasks = []

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def start(self, *, seconds=10.8, start=1.92, initial_bytes=None, starts=None, action_kind="L",
                    ge_lead_seconds=.5, real_audio=False, drain_seconds=0,
                    max_audio_seconds=30, max_text_codepoints=120, text_repeat=1,
                    allow_tail_q_after_audio=False, delayed_motion=None):
        out = Path(self.temp.name)
        c = MotionComponent(NoNetwork(), out, out / "unused.png")
        c._ws = FakeSocket()
        c.live_id = "offline-only"
        c._ready.set()
        c._joined.set()
        c.state = "ready"
        c.DRAIN_SECONDS = drain_seconds
        c._wait = lambda seconds: self.clock.wait(c, seconds)
        if delayed_motion:
            original_send = c._ws.send_json
            async def delayed_send(frame):
                if frame["type"] == 9:
                    delay = delayed_motion.get(c._ws.motion_attempts + 1, 0)
                    if delay:
                        await self.clock.wait(c, delay)
                await original_send(frame)
            c._ws.send_json = delayed_send
        starts = (start,) if starts is None else starts
        anchors = "甲乙丙丁戊己"[:len(starts)]
        text = "".join(char * text_repeat for char in anchors)
        pcm, alignment = fixture(seconds, starts, word_repeat=text_repeat)
        initial_bytes = len(pcm) if initial_bytes is None else initial_bytes
        release = asyncio.Event()
        began = asyncio.Event()

        async def controlled_audio(audio, count_audio, *, on_frame_sent):
            # Keep the real callback and scheduler path, holding only the final
            # bytes when the test needs to distinguish elapsed time from progress.
            await c._ws.send_bytes(audio[:initial_bytes])
            on_frame_sent(initial_bytes, self.clock.now)
            began.set()
            if initial_bytes < len(audio):
                await release.wait()
                c._check()
                await c._ws.send_bytes(audio[initial_bytes:])
                on_frame_sent(len(audio), self.clock.now)

        if not real_audio:
            c._audio = controlled_audio
        task = asyncio.create_task(c.send_aligned_expression(
            pcm, text, [action(char) for char in anchors], 1, recovery=GE, alignment=alignment,
            action_kind=action_kind, ge_lead_seconds=ge_lead_seconds,
            max_audio_seconds=max_audio_seconds, max_text_codepoints=max_text_codepoints,
            allow_tail_q_after_audio=allow_tail_q_after_audio))
        self.tasks.append(task)
        await self.clock.settle()
        lead = ge_lead_seconds or 0.0
        if starts[0] < lead:
            await self.clock.advance(lead - starts[0])
        self.assertTrue(c._ws.binary if real_audio else began.is_set(), "the real send entry must reach PCM")
        return c, task, release, pcm

    def roles(self, component):
        return [event["role"] for event in component.motion_events]

    async def test_a_final_q_after_audio_keeps_real_pcm_gate_and_original_clock(self):
        with patch("component_live.time", SimpleNamespace(monotonic=lambda: self.clock.now, time=time.time)):
            c, task, _, pcm = await self.start(seconds=8, starts=(0, 3.6, 7.2), action_kind="S",
                ge_lead_seconds=1.1, allow_tail_q_after_audio=True, real_audio=True)
            await self.clock.advance(8.01)
            self.assertTrue(c.audio_sent_complete)
            self.assertEqual(self.roles(c), ["GE", "S", "Q"] * 2 + ["GE", "S"])
            self.assertFalse(task.done(), "full PCM does not bypass the final Q clock")
            self.assertEqual(c.last_motion_receipt["timeline"][-1]["pcm_required_bytes"], len(pcm))
            self.assertEqual(c.last_motion_receipt["timeline"][-1]["audio_at_seconds"], 8.7)
            await self.clock.advance(.71)
            self.assertTrue(task.done(), "Q must never wait for nonexistent after-audio PCM")
            receipt = await task
            self.assertEqual(self.roles(c), ["GE", "S", "Q"] * 3)
            self.assertGreaterEqual(receipt["motion_events"][-1]["actual_at_seconds"], 8.7)
            self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), pcm)
            self.assertTrue(receipt["actions_sent_complete"])

    async def test_a_after_audio_q_still_requires_the_last_real_pcm_sample(self):
        c, task, release, pcm = await self.start(seconds=8, starts=(0, 3.6, 7.2), action_kind="S",
            ge_lead_seconds=1.1, allow_tail_q_after_audio=True, initial_bytes=8 * 48000 - 2)
        await self.clock.advance(9)
        self.assertEqual(self.roles(c), ["GE", "S", "Q"] * 2 + ["GE", "S"])
        self.assertFalse(task.done())
        release.set()
        await self.clock.settle()
        await self.clock.advance(.03)
        self.assertTrue(task.done())
        receipt = await task
        self.assertEqual(receipt["pcm_transport_sent_bytes"], len(pcm))
        self.assertEqual(self.roles(c), ["GE", "S", "Q"] * 3)

    async def test_stop_while_waiting_after_audio_q_never_sends_it_late(self):
        c, task, _, _ = await self.start(seconds=8, starts=(0, 3.6, 7.2), action_kind="S",
            ge_lead_seconds=1.1, allow_tail_q_after_audio=True)
        await self.clock.advance(8.1)
        expected = ["GE", "S", "Q"] * 2 + ["GE", "S"]
        self.assertEqual(self.roles(c), expected)
        self.assertTrue(c.audio_sent_complete)
        c.latch_stop()
        await self.clock.advance(10)
        with self.assertRaises(RuntimeError):
            await task
        self.assertEqual(self.roles(c), expected)
        self.assertFalse(c.active_motion_plan["is_active"])

    async def test_delayed_a_writes_do_not_compress_any_interval_after_audio(self):
        c, task, _, _ = await self.start(seconds=8, starts=(0, 3.6, 7.2), action_kind="S",
            ge_lead_seconds=1.1, allow_tail_q_after_audio=True,
            delayed_motion={3: .7, 4: .3, 5: .4, 8: .5})
        await self.clock.advance(15)
        self.assertTrue(task.done())
        receipt = await task
        events = receipt["motion_events"]
        self.assertEqual([e["role"] for e in events], ["GE", "S", "Q"] * 3)
        for index, event in enumerate(events[1:], 1):
            minimum = {"GE": 1, "S": 1.1, "Q": 1.5}[event["role"]]
            self.assertGreaterEqual(event["sent_at_monotonic"] - events[index-1]["sent_at_monotonic"],
                                    minimum - 1e-6)
        self.assertGreater(events[-1]["actual_at_seconds"], 8.7)

    async def test_10_8_second_q_sends_after_audio_clock_reaches_end(self):
        c, task, _, pcm = await self.start()
        self.assertEqual(len(pcm), 518400)
        await self.clock.advance(10.799)
        self.assertEqual(self.roles(c), ["GE", "L"])
        await self.clock.advance(.002)
        self.assertTrue(task.done(), "full PCM must not wait for a fractional byte")
        receipt = await task
        self.assertEqual(self.roles(c), ["GE", "L", "Q"])
        self.assertGreaterEqual(receipt["motion_events"][-1]["actual_at_seconds"], 10.8)
        self.assertTrue(receipt["actions_sent_complete"])
        self.assertNotIn("visual_effect_confirmed", receipt)
        self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), pcm)

    async def test_last_missing_sample_blocks_q_even_after_audio_clock(self):
        c, task, _, _ = await self.start(initial_bytes=518398)
        await self.clock.advance(11)
        self.assertEqual(self.roles(c), ["GE", "L"])
        self.assertEqual(c.last_motion_receipt["pcm_transport_sent_bytes"], 518398)
        self.assertFalse(task.done())

    async def test_late_last_sample_releases_exactly_one_q(self):
        c, task, release, pcm = await self.start(initial_bytes=518398)
        await self.clock.advance(11)
        self.assertEqual(self.roles(c), ["GE", "L"])
        release.set()
        await self.clock.settle()
        await self.clock.advance(.03)
        self.assertTrue(task.done())
        receipt = await task
        await self.clock.advance(1)
        self.assertEqual(self.roles(c), ["GE", "L", "Q"])
        self.assertEqual(len(c._ws.motions()), 3)
        self.assertEqual(receipt["pcm_transport_sent_bytes"], len(pcm))
        self.assertGreaterEqual(receipt["motion_events"][-1]["actual_at_seconds"], 11)

    async def test_two_s_qs_keep_word_anchors_and_finish_before_full_10_4_second_pcm(self):
        c, task, release, pcm = await self.start(
            seconds=10.4, starts=(.08, 6.64), initial_bytes=round(8.14 * 24000) * 2,
            action_kind="S")
        await self.clock.advance(8.141)
        self.assertEqual(self.roles(c), ["GE", "S", "Q"] * 2)
        receipt = c.last_motion_receipt
        self.assertEqual(receipt["action_kind"], "S")
        self.assertEqual([round(n["audio_at_seconds"], 2) for n in receipt["timeline"]],
                         [-.42, .08, 1.58, 6.14, 6.64, 8.14])
        self.assertEqual(receipt["timeline"][-1]["pcm_required_bytes"], 390720)
        self.assertLess(receipt["pcm_transport_sent_bytes"], len(pcm))
        self.assertFalse(receipt["send_complete"])
        self.assertFalse(c.audio_sent_complete)
        self.assertFalse(task.done(), "last Q must not finish or truncate the audio send")
        self.assertGreaterEqual(c.motion_events[3]["actual_interval_seconds"], 1)
        for index in (2, 5):
            self.assertGreaterEqual(c.motion_events[index]["actual_interval_seconds"], 1.5)
        # Keep audio in flight to its original 10.4 second tail, then release
        # all remaining bytes. Earlier Q completion never releases this gate.
        await self.clock.advance(10.4 - 8.141)
        release.set()
        await self.clock.settle()
        result = await task
        self.assertEqual(result["pcm_transport_sent_bytes"], len(pcm))
        self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), pcm)
        self.assertTrue(result["send_complete"])
        self.assertTrue(result["actions_sent_complete"])
        self.assertGreaterEqual(result["last_pcm_sent_at_monotonic"] -
                                result["first_pcm_sent_at_monotonic"], 10.4 - 1e-9)

    async def test_zero_anchor_s_prerolls_before_text_and_first_pcm(self):
        c, task, _, pcm = await self.start(seconds=2, start=0, action_kind="S")
        self.assertEqual(self.roles(c), ["GE", "S"])
        text = next(row for row in c._ws.sent if row["frame"]["type"] == 10)
        self.assertLessEqual(c._ws.motions()[1]["completed"], text["started"])
        self.assertLessEqual(text["completed"], c._ws.binary[0]["time"])
        self.assertGreaterEqual(c.motion_events[1]["actual_interval_seconds"], .5)
        self.assertEqual(c.last_motion_receipt["timeline"][1]["pcm_required_bytes"], 0)
        await self.clock.advance(1.499)
        self.assertEqual(self.roles(c), ["GE", "S"])
        await self.clock.advance(.002)
        result = await task
        self.assertEqual(self.roles(c), ["GE", "S", "Q"])
        self.assertEqual(result["timeline"][-1]["pcm_required_bytes"], 72000)
        self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), pcm)

    async def test_ab_real_pcm_scheduler_keeps_10_4_seconds_and_original_s_q_anchors(self):
        for lead in (1.1, None):
            with self.subTest(lead=lead), patch("component_live.time", SimpleNamespace(
                    monotonic=lambda: self.clock.now, time=time.time)):
                c, task, _, pcm = await self.start(seconds=10.4, starts=(.08, 6.64),
                                                    action_kind="S", ge_lead_seconds=lead, real_audio=True)
                await self.clock.advance(8.15)
                roles = ["GE", "S", "Q"] * 2 if lead is not None else ["S", "Q"] * 2
                self.assertEqual(self.roles(c), roles)
                self.assertFalse(task.done())
                self.assertLess(c.last_motion_receipt["pcm_transport_sent_bytes"], len(pcm))
                self.assertFalse(c.audio_sent_complete)
                events = c.motion_events
                self.assertEqual([round(e["planned_at_seconds"], 2) for e in events if e["role"] in {"S", "Q"}],
                                 [.08, 1.58, 6.64, 8.14])
                for event in events:
                    if event["role"] == "S" and lead is not None:
                        self.assertGreaterEqual(event["actual_interval_seconds"], 1.1 - 1e-6)
                    elif event["role"] == "Q":
                        self.assertGreaterEqual(event["actual_interval_seconds"], 1.5 - 1e-6)
                await self.clock.advance(2.3)
                self.assertTrue(task.done())
                result = await task
                self.assertEqual(result["ge_lead_seconds"], lead)
                self.assertEqual(result["independent_ge"], lead is not None)
                self.assertEqual(result["audio_seconds"], 10.4)
                self.assertTrue(result["send_complete"])
                self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), pcm)

    async def test_ab_zero_anchor_and_cancel_never_emit_pending_q(self):
        for lead in (1.1, None):
            with self.subTest(lead=lead):
                c, task, _, _ = await self.start(seconds=2, start=0, action_kind="S", ge_lead_seconds=lead)
                expected = ["GE", "S"] if lead is not None else ["S"]
                self.assertEqual(self.roles(c), expected)
                self.assertEqual(c.motion_events[-1]["actual_at_seconds"], 0)
                self.assertEqual(c.last_motion_receipt["timeline"][len(expected)-1]["pcm_required_bytes"], 0)
                if lead is not None:
                    self.assertGreaterEqual(c.motion_events[-1]["actual_interval_seconds"], 1.1 - 1e-6)
                task.cancel()
                await self.clock.settle()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                await self.clock.advance(3)
                self.assertEqual(self.roles(c), expected)
                self.assertFalse(c.active_motion_plan["is_active"])

    async def test_three_group_ab_28_and_30_second_real_pcm_with_full_drain(self):
        starts = (.08, 11.337, 26.5)
        for seconds in (28, 30):
            for lead in (1.1, None):
                with self.subTest(seconds=seconds, lead=lead), patch("component_live.time", SimpleNamespace(
                        monotonic=lambda: self.clock.now, time=time.time)):
                    c, task, _, pcm = await self.start(seconds=seconds, starts=starts, action_kind="S",
                        ge_lead_seconds=lead, real_audio=True, drain_seconds=4)
                    if lead is not None:
                        self.assertEqual(self.roles(c), ["GE"])
                        self.assertAlmostEqual(c.last_motion_receipt["timeline"][0]["at_seconds"], -1.02)
                    else:
                        self.assertEqual(self.roles(c), [])
                    await self.clock.advance(seconds + .01)
                    self.assertFalse(task.done(), "full PCM still requires the unchanged four-second safety buffer")
                    self.assertEqual(c.state, "draining")
                    self.assertTrue(c.last_motion_receipt["send_complete"])
                    self.assertFalse(c.last_motion_receipt["handoff_wait_elapsed"])
                    self.assertNotIn("post_audio_wait_seconds", c.last_motion_receipt)
                    self.assertEqual(len(c.motion_events), 9 if lead is not None else 6)
                    self.assertEqual(self.roles(c), (["GE", "S", "Q"] if lead is not None else ["S", "Q"]) * 3)
                    self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), pcm)
                    self.assertEqual(c.last_motion_receipt["pcm_transport_sent_bytes"], round(seconds * 24000) * 2)
                    for index in range(1, 4):
                        events = [event for event in c.motion_events if event["group_index"] == index]
                        active = next(event for event in events if event["role"] == "S")
                        quiet = next(event for event in events if event["role"] == "Q")
                        self.assertAlmostEqual(active["planned_at_seconds"], starts[index-1])
                        self.assertAlmostEqual(quiet["planned_at_seconds"], starts[index-1] + 1.5)
                        self.assertGreaterEqual(quiet["actual_interval_seconds"], 1.5 - 1e-6)
                        if lead is not None:
                            self.assertGreaterEqual(active["actual_interval_seconds"], 1.1 - 1e-6)
                    await self.clock.advance(4)
                    self.assertTrue(task.done())
                    result = await task
                    self.assertEqual(result["status"], "sent")
                    self.assertTrue(result["handoff_wait_elapsed"])
                    self.assertAlmostEqual(result["post_audio_wait_seconds"], 4.0)
                    self.assertEqual(c.state, "ready")
                    if seconds == 28:
                        self.assertEqual(result["timeline"][-1]["pcm_required_bytes"], len(pcm))
                    else:
                        self.assertLess(result["timeline"][-1]["pcm_required_bytes"], len(pcm))

    async def test_completed_audio_tail_duration_includes_later_q_without_adding_drain_twice(self):
        with patch("component_live.time", SimpleNamespace(monotonic=lambda: self.clock.now, time=time.time)):
            c, task, _, _ = await self.start(seconds=.1, start=0, action_kind="S", ge_lead_seconds=1.1,
                allow_tail_q_after_audio=True, real_audio=True, drain_seconds=.2)
            await self.clock.advance(.31)
            self.assertTrue(c.last_motion_receipt["handoff_wait_elapsed"])
            self.assertFalse(task.done(), "the final Q still owns the sender after the short test drain")
            self.assertNotIn("post_audio_wait_seconds", c.last_motion_receipt)
            await self.clock.advance(1.2)
            result = await task
            self.assertEqual(result["status"], "sent")
            self.assertAlmostEqual(result["post_audio_wait_seconds"], 1.4)
            self.assertEqual(self.roles(c), ["GE", "S", "Q"])

    async def test_three_group_ab_stop_blocks_pending_q_later_actions_and_pcm(self):
        for lead in (1.1, None):
            with self.subTest(lead=lead), patch("component_live.time", SimpleNamespace(
                    monotonic=lambda: self.clock.now, time=time.time)):
                c, task, _, pcm = await self.start(seconds=30, starts=(.08, 11.337, 26.5),
                                                    action_kind="S", ge_lead_seconds=lead, real_audio=True)
                await self.clock.advance(11.5)
                expected = ["GE", "S", "Q", "GE", "S"] if lead is not None else ["S", "Q", "S"]
                self.assertEqual(self.roles(c), expected)
                self.assertLess(c.last_motion_receipt["pcm_transport_sent_bytes"], len(pcm))
                c.latch_stop()
                await self.clock.advance(25)
                self.assertTrue(task.done())
                with self.assertRaises(RuntimeError):
                    await task
                counts = (len(c._ws.binary), len(c.motion_events))
                await self.clock.advance(10)
                self.assertEqual((len(c._ws.binary), len(c.motion_events)), counts)
                self.assertEqual(self.roles(c), expected)
                self.assertFalse(c.last_motion_receipt["send_complete"])
                self.assertFalse(c.active_motion_plan["is_active"])

    async def test_minute_57_second_pcm_180_chars_six_groups_and_four_second_drain(self):
        # GE2 follows Q1 by >35s, exercising the per-request 65s PCM-progress
        # deadline rather than silently retaining the normal 35s timeout.
        starts = (.08, 40, 43.6, 47.2, 50.8, 54.4)
        with patch("component_live.time", SimpleNamespace(monotonic=lambda: self.clock.now, time=time.time)):
            c, task, _, pcm = await self.start(seconds=57, starts=starts, action_kind="S", ge_lead_seconds=1.1,
                real_audio=True, drain_seconds=4, max_audio_seconds=60, max_text_codepoints=240, text_repeat=30)
            await self.clock.advance(57.01)
            self.assertEqual(self.roles(c), ["GE", "S", "Q"] * 6)
            self.assertEqual(len(c.motion_events), 18)
            self.assertEqual(c.state, "draining")
            self.assertFalse(task.done())
            self.assertEqual(len([row for row in c._ws.sent if row["frame"]["type"] == 10]), 1)
            self.assertEqual(b"".join(row["chunk"] for row in c._ws.binary), pcm)
            self.assertEqual(c.last_motion_receipt["pcm_transport_sent_bytes"], 57 * 24000 * 2)
            self.assertEqual([e["planned_at_seconds"] for e in c.motion_events if e["role"] == "S"], list(starts))
            self.assertEqual([e["planned_at_seconds"] for e in c.motion_events if e["role"] == "Q"],
                             [start + 1.5 for start in starts])
            for event in c.motion_events:
                if event["role"] == "S":
                    self.assertGreaterEqual(event["actual_interval_seconds"], 1.1 - 1e-6)
                elif event["role"] == "Q":
                    self.assertGreaterEqual(event["actual_interval_seconds"], 1.5 - 1e-6)
            await self.clock.advance(4)
            result = await task
            self.assertTrue(result["send_complete"])
            self.assertTrue(result["handoff_wait_elapsed"])
            self.assertEqual((result["max_audio_seconds"], result["max_text_codepoints"]), (60, 240))
            self.assertEqual(c.MAX_AUDIO_SECONDS, 30)

    async def test_minute_stop_prevents_all_later_pcm_and_motion(self):
        with patch("component_live.time", SimpleNamespace(monotonic=lambda: self.clock.now, time=time.time)):
            c, task, _, pcm = await self.start(seconds=57, starts=(.08, 9, 18, 28, 40, 53),
                action_kind="S", ge_lead_seconds=1.1, real_audio=True,
                max_audio_seconds=60, max_text_codepoints=240, text_repeat=30)
            await self.clock.advance(28.2)
            self.assertEqual(self.roles(c), ["GE", "S", "Q"] * 3 + ["GE", "S"])
            c.latch_stop()
            await self.clock.advance(40)
            with self.assertRaises(RuntimeError):
                await task
            counts = (len(c._ws.binary), len(c.motion_events))
            await self.clock.advance(30)
            self.assertEqual((len(c._ws.binary), len(c.motion_events)), counts)
            self.assertLess(c.last_motion_receipt["pcm_transport_sent_bytes"], len(pcm))
            self.assertFalse(c.last_motion_receipt["send_complete"])
            self.assertFalse(c.active_motion_plan["is_active"])

    async def check_invalidated_plan(self, reason):
        c, task, _, _ = await self.start()
        await self.clock.advance(2)
        self.assertEqual(self.roles(c), ["GE", "L"])
        if reason == "stop":
            c.latch_stop()
        elif reason == "cancel":
            task.cancel()
        else:
            c._motion_epoch += 1
        await self.clock.advance(10)
        self.assertTrue(task.done())
        error = asyncio.CancelledError if reason == "cancel" else RuntimeError
        with self.assertRaises(error):
            await task
        await self.clock.advance(10)
        self.assertEqual(self.roles(c), ["GE", "L"])
        self.assertEqual(len(c._ws.motions()), 2)

    async def test_stop_never_sends_pending_q(self):
        await self.check_invalidated_plan("stop")

    async def test_cancel_never_sends_pending_q(self):
        await self.check_invalidated_plan("cancel")

    async def test_expired_epoch_never_sends_pending_q(self):
        await self.check_invalidated_plan("expired")

    async def test_nonfinal_millisecond_anchor_accepts_exact_sample_progress(self):
        # 543 ms * 48,000 is 26064.000000000004 as a float. L must
        # accept exactly 26064 bytes without needing the next PCM sample.
        c, task, _, _ = await self.start(seconds=4, start=.543, initial_bytes=26064)
        await self.clock.advance(.542)
        self.assertEqual(self.roles(c), ["GE"])
        await self.clock.advance(.002)
        self.assertEqual(self.roles(c), ["GE", "L"])
        self.assertEqual(c.last_motion_receipt["pcm_transport_sent_bytes"], 26064)
        self.assertGreaterEqual(c.motion_events[1]["actual_at_seconds"], .543)
        self.assertGreaterEqual(c.motion_events[1]["actual_interval_seconds"], .5)
        self.assertFalse(task.done())


if __name__ == "__main__":
    unittest.main()
