"""Original scheduler runtime with fake clock/socket; NOT cloud or playback proof.

Production scheduler and PCM writer execute unchanged. Only transport, wall clock
and waits are injected. Idle resource examples are synthetic, not avatar bindings.
"""
import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hell_inc.avatar_transport import motion_component, component_live, idle_motion
from hell_inc.avatar_rules.semantic_speech import compile_dialogue_semantic_timeline
from test_m4_policy import action, alignment, RECOVERY


class VirtualClock:
    def __init__(self): self.now = 100.0
    def monotonic(self): return self.now
    def time(self): return 1_800_000_000 + self.now
    async def wait(self, seconds):
        self.now += max(0, seconds)
        await asyncio.sleep(0)


class FakeSocket:
    closed = False
    def __init__(self, clock): self.clock = clock; self.frames = []; self.bytes = []; self.fail = False
    async def send_json(self, frame):
        if self.fail: raise ConnectionError('injected socket write failure')
        self.frames.append((self.clock.monotonic(), frame))
    async def send_bytes(self, chunk): self.bytes.append((self.clock.monotonic(), bytes(chunk)))


class NoNetwork:
    def __getattr__(self, name): raise AssertionError('network forbidden: ' + name)


def ready_component(out, clock):
    component = motion_component.MotionComponent(NoNetwork(), out, Path(out) / 'unused-image')
    component.live_id = 'owned-fake-live'; component.state = 'ready'
    component._ready.set(); component._joined.set(); component._ws = FakeSocket(clock)
    component._wait = clock.wait
    return component


class OriginalSchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.clock = VirtualClock()
        for module in (motion_component, component_live, idle_motion):
            patched = patch.object(module, 'time', self.clock); patched.start(); self.addCleanup(patched.stop)
        self.c = ready_component(self.tmp.name, self.clock)

    async def test_original_aligned_sender_pcm_and_ge_s_q_timing_are_runtime_executed(self):
        pcm = b'\0\0' * (24000 * 6)
        words = alignment(digest=hashlib.sha256(pcm).hexdigest())
        receipt = await self.c.send_aligned_expression(pcm, ' 你好\n', [action()], 1,
            recovery=RECOVERY, alignment=words, action_kind='S', ge_lead_seconds=1.1,
            allow_tail_q_after_audio=True, local_action_anchors=True)
        events = receipt['motion_events']
        self.assertEqual([e['role'] for e in events], ['GE', 'S', 'Q'])
        self.assertGreaterEqual(events[1]['sent_at_monotonic'] - events[0]['sent_at_monotonic'] + 1e-9, 1.1)
        self.assertGreaterEqual(events[2]['sent_at_monotonic'] - events[1]['sent_at_monotonic'] + 1e-9, 1.5)
        self.assertEqual(b''.join(chunk for _, chunk in self.c._ws.bytes), pcm)
        type10 = [f for _, f in self.c._ws.frames if f['type'] == 10]
        self.assertEqual(type10[0]['payload']['text_msg']['content'], ' 你好\n')
        self.assertTrue(receipt['send_complete']); self.assertFalse(receipt['playback_complete_confirmed'])
        self.assertFalse(receipt['visual_effect_confirmed'])
        first_pcm = self.c._ws.bytes[0][0]
        self.assertGreaterEqual(events[1]['sent_at_monotonic'], first_pcm + 2)

    async def test_type9_guard_wait_does_not_hold_socket_lock_or_block_type10_pcm(self):
        self.c._motion_epoch = 1; self.c.active_motion_plan = {'group_id': 'g'}
        node = compile_dialogue_semantic_timeline([action()], RECOVERY, alignment(), 6)[1]
        self.c._motion_not_before = self.clock.now + 1
        waiting, release = asyncio.Event(), asyncio.Event()
        motion_task = None
        async def wait(seconds):
            if asyncio.current_task() is motion_task:
                self.assertFalse(self.c._send_lock.locked())
                waiting.set(); await release.wait()
            await self.clock.wait(seconds)
        self.c._wait = wait
        motion_task = asyncio.create_task(self.c._write_motion(node, 1, 'g', 1, None, None))
        await asyncio.wait_for(waiting.wait(), 1)
        await self.c._send(10, {'text_msg': {'content': '并行文本'}})
        await self.c._audio(b'\0\0' * 240, True)
        self.assertEqual(len(self.c._ws.bytes), 1)
        self.assertEqual([f['type'] for _, f in self.c._ws.frames], [10])
        release.set(); await asyncio.wait_for(motion_task, 1)
        self.assertEqual([f['type'] for _, f in self.c._ws.frames], [10, 9])

    async def test_successful_motion_guard_survives_group_boundary_and_failed_write_not_success(self):
        nodes = compile_dialogue_semantic_timeline([action()], RECOVERY, alignment(), 6)
        self.c._motion_epoch = 1; self.c.active_motion_plan = {'group_id': 'first'}
        first = await self.c._write_motion(nodes[-1], 1, 'first', 1, None, None)
        self.c._motion_epoch = 2; self.c.active_motion_plan = {'group_id': 'next'}
        second = await self.c._write_motion(nodes[0], 2, 'next', 2, None, None)
        self.assertGreaterEqual(second - first + 1e-9, 1)
        previous_deadline = self.c._motion_not_before
        self.c._ws.fail = True
        with self.assertRaises(ConnectionError): await self.c._write_motion(nodes[1], 2, 'next', 2, None, second)
        self.assertEqual(self.c._motion_not_before, previous_deadline)
        self.assertEqual(len(self.c.motion_events), 2)

    async def test_pcm_progress_and_clock_both_gate_actions_and_stale_epoch_rejects(self):
        node = {**compile_dialogue_semantic_timeline([action()], RECOVERY, alignment(), 6)[1], 'pcm_required_bytes': 96000}
        self.c._motion_epoch = 1; self.c.active_motion_plan = {'group_id': 'g'}
        progress = {'first_sent_at_monotonic': self.clock.now, 'sent_bytes': 0}
        waiting = asyncio.Event(); release = asyncio.Event()
        async def wait(seconds):
            if not release.is_set(): waiting.set(); await release.wait()
            await self.clock.wait(seconds)
        self.c._wait = wait
        task = asyncio.create_task(self.c._run_aligned_remaining([node], 1, 'g', 1, progress))
        await asyncio.wait_for(waiting.wait(), 1)
        self.assertEqual(self.c._ws.frames, [])
        progress['sent_bytes'] = 96000; release.set(); await asyncio.wait_for(task, 1)
        self.assertGreaterEqual(self.c._ws.frames[0][0], progress['first_sent_at_monotonic'] + 2)
        with self.assertRaises(component_live._Stopped): self.c._check_aligned_plan(0, 'g')

    async def test_idle_success_only_cooldown_content_gate_and_protected_q_recovery(self):
        idle = self.c._idle_motions
        sample = {'id': 'synthetic-test-only', 'description_sha256': 'a' * 64, 'instruction': '测试动作', 'recovery_instruction': '正常坐姿',
                  'cooldown_seconds': 10, 'hold_seconds': 1}
        idle.enabled = True; idle.valid = lambda: True; idle.allowed = lambda: True; idle.interrupt = lambda: False
        self.c._ws.fail = True
        with self.assertRaises(ConnectionError): await idle.write('I', sample, lambda: True)
        self.assertEqual(idle.cooldowns, {}); self.assertTrue(idle.needs_recovery)
        self.c._ws.fail = False
        await idle.recover('test recovery', explicit=True)
        self.assertFalse(idle.needs_recovery)
        await idle.write('I', sample, lambda: True)
        self.assertIn(sample['id'], idle.cooldowns)
        self.c._pending_speech = 1
        self.assertFalse(idle.can_start(idle.epoch))
        await idle.pause_for_content()
        self.assertFalse(idle.needs_recovery)
        self.assertEqual(self.c._ws.frames[-1][1]['payload']['text_msg']['content'], '正常坐姿')
        self.assertGreaterEqual(self.clock.now, idle.not_before)
