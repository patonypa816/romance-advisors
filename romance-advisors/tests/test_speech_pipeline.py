"""Controlled barriers exercise both speaker slots without paid services."""
import asyncio
import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from romance.server import Workbench
from test_ambient import AmbientModel, ControlledAvatar, REAL_SLEEP, yield_only


class BarrierAvatar(ControlledAvatar):
    def __init__(self):
        super().__init__()
        self.jobs, self.blocked = {}, set()
        self.prepares, self.dispatches, self.trace = [], [], []
        self.active_sender = None
        self.lose_prepare, self.lose_before, self.lose_after, self.query_fail = set(), set(), set(), set()
        self.auto_finish = False
        self.stopped = asyncio.Event()

    def response(self, name):
        return copy.deepcopy(self.jobs[name])

    async def prepare(self, record, text):
        name = record['job_id']
        self.prepares.append(name)
        self.jobs[name] = dict(self.receipt(record, 'preparing' if name in self.blocked else 'prepared'),
                               preparation_pending=name in self.blocked)
        self.trace.append(('prepare', name))
        if name in self.lose_prepare:
            self.lose_prepare.remove(name)
            raise OSError('lost prepare response')
        return self.response(name)

    async def dispatch(self, record):
        name = record['job_id']
        self.dispatches.append(name)
        if name in self.lose_before:
            self.lose_before.remove(name)
            raise OSError('request not received')
        if self.active_sender:
            raise AssertionError('two simultaneous senders')
        self.active_sender = name
        self.jobs[name].update(status='sending', worker_pending=True)
        self.trace.append(('dispatch', name))
        if self.auto_finish:
            self.finish(name)
        if name in self.lose_after:
            self.lose_after.remove(name)
            raise OSError('lost dispatch response')
        return self.response(name)

    async def submit(self, record, text):
        self.jobs[record['job_id']] = self.receipt(record, 'prepared')
        return await self.dispatch(record)

    async def job(self, record):
        name = record['job_id']
        self.queried_ids.append(name)
        if name in self.query_fail:
            raise OSError('unconfirmed query')
        return self.response(name)

    async def cancel_job(self, record):
        name = record['job_id']
        job = self.jobs[name]
        self.trace.append(('cancel', name))
        if job.get('delivery_started'):
            return dict(self.response(name), cancel_result='already_started')
        job.update(status='cancelled', cancel_result='cancelled_before_delivery')
        if not job.get('preparation_pending'):
            job['worker_pending'] = False
            if self.active_sender == name:
                self.active_sender = None
        return self.response(name)

    def finish_prepare(self, name):
        self.jobs[name]['preparation_pending'] = False
        if self.jobs[name]['status'] == 'preparing':
            self.jobs[name]['status'] = 'prepared'

    def started(self, name):
        self.jobs[name]['delivery_started'] = True

    def finish(self, name):
        self.jobs[name].update(status='sent', send_complete=True, delivery_started=True,
                               short_action_sent=self.jobs[name].get('mode') == 'motion',
                               preparation_pending=False, worker_pending=False)
        if self.active_sender == name:
            self.active_sender = None

    async def stop(self):
        self.stopped.set()
        await super().stop()


class SpeechPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.avatar, self.model = BarrierAvatar(), AmbientModel()
        self.wb = Workbench(':memory:', model=self.model, avatar_client=self.avatar)
        self.p, self.tasks = self.wb.program, []
        self.patcher = patch('romance.program.asyncio.sleep', yield_only)
        self.patcher.start()
        self.p.start()
        await self.p.task
        await self.wb.job
        self.wb.state()

    async def asyncTearDown(self):
        for t in self.tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.wb.close()
        self.patcher.stop()

    async def until(self, check):
        for _ in range(200):
            if check():
                return
            await REAL_SLEEP(0)
        self.fail('event barrier not reached')

    def line(self, name, kind='proactive', valid=lambda: True, motion=False):
        c = self.p.reserve_next(kind=kind, valid=valid)
        self.assertIsNotNone(c)
        record = {'job_id': name, 'owner_id': self.p.run_id, 'generation': self.avatar.generation,
                  'plan': {'emotion': '紧张', 'segments': [{'text': name, 'actions': [], 'recovery': None}]}}
        if motion:
            record['mode'] = 'motion'
        task = self.p.schedule_candidate(c, self.p.send(record, name, optional=kind != 'formal', candidate=c))
        self.tasks.append(task)
        return c, task

    def formal(self):
        record = self.p.prepare('formal', '正式决定。')
        # These barriers isolate the pre-existing speech priority contract.
        record.pop('message_motion_plan')
        t = asyncio.create_task(self.p.perform(record, '正式决定。'))
        self.tasks.append(t)
        return record['job_id'], t

    def comment(self, name):
        r = self.wb.game.current_round
        return self.wb.accept_comment({'event_id': name, 'author_id': name, 'nickname': '观众',
            'text': '别紧张，慢慢说。', 'expected_round_id': r['id'], 'expected_phase': r['phase']})

    def message_performance(self, callback):
        record = self.p.prepare('message-round', '我想认真回答。')
        task = asyncio.create_task(self.p.perform(record, '我想认真回答。', on_message_action=callback))
        self.tasks.append(task)
        return record, task

    async def start_message_motion(self, callback):
        record, task = self.message_performance(callback)
        await self.until(lambda: record['job_id'] in self.avatar.dispatches)
        self.avatar.finish(record['job_id'])
        typing = record['job_id'] + ':typing'
        await self.until(lambda: typing in self.avatar.dispatches)
        return record, typing, task

    async def test_message_action_follows_complete_speech_notifies_once_at_s_and_waits_for_q(self):
        notified = []
        record, task = self.message_performance(lambda: notified.append(self.avatar.active_sender))
        speech, typing = record['job_id'], record['job_id'] + ':typing'
        self.avatar.lose_after.add(typing)
        await self.until(lambda: speech in self.avatar.dispatches)
        self.avatar.started(speech)
        self.avatar.jobs[speech].update(status='sent', send_complete=True)
        for _ in range(5):
            await REAL_SLEEP(0)
        self.assertNotIn(typing, self.avatar.dispatches)  # Speech worker still owes its tail.
        self.avatar.finish(speech)
        await self.until(lambda: typing in self.avatar.dispatches)
        self.avatar.started(typing)  # GE/start does not expose a message.
        await self.until(lambda: self.p.current['receipt'].get('delivery_started'))
        self.assertEqual(notified, [])
        self.assertEqual(self.p.formal_waiting, 1)
        self.comment('during-typing')
        for _ in range(5):
            await REAL_SLEEP(0)
        self.assertFalse(any(name == 'ambient' for name, _ in self.model.calls))
        self.avatar.jobs[typing]['short_action_sent'] = True
        await self.until(lambda: bool(notified))
        self.assertEqual(notified, [typing])
        self.avatar.jobs[typing].update(status='sent', send_complete=True)
        for _ in range(8):
            await REAL_SLEEP(0)
        self.assertEqual(notified, [typing])  # Repeated S receipts never repeat the callback.
        self.assertFalse(task.done())  # S and terminal text do not stand in for Q/worker cleanup.
        self.assertEqual(self.p.formal_waiting, 1)
        self.avatar.finish(typing)
        receipt = await task
        self.assertEqual(receipt['job_id'], speech)
        self.assertEqual(receipt['message_motion_receipt']['job_id'], typing)
        self.assertTrue(receipt['message_motion_receipt']['short_action_sent'])
        self.assertEqual(self.avatar.prepares, [speech])  # Silent motion never synthesizes audio.
        self.assertEqual(self.avatar.dispatches, [speech, typing])
        self.assertEqual(self.avatar.trace.count(('dispatch', typing)), 1)
        self.assertFalse(self.p.sender_failed)
        self.assertEqual(self.p.formal_waiting, 0)

    async def test_message_motion_missing_s_or_error_before_s_never_notifies(self):
        # Exercise the terminal guard separately from the event notification.
        for status, evidence in (('sent', False), ('error', False), ('sent', 1)):
            with self.subTest(status=status, evidence=evidence):
                notified = []
                _, typing, task = await self.start_message_motion(lambda: notified.append(True))
                self.avatar.jobs[typing].update(status=status, send_complete=True,
                                                worker_pending=False, short_action_sent=evidence)
                self.avatar.active_sender = None
                with self.assertRaisesRegex(ValueError, 'invalid_performance_send'):
                    await task
                self.assertEqual(notified, [])
                self.assertTrue(self.p.sender_failed)
                self.assertEqual(self.p.formal_waiting, 0)
                self.p.sender_failed, self.p.current = False, None
                self.avatar.jobs.clear()
                self.avatar.dispatches.clear()

    async def test_message_motion_q_failure_preserves_already_notified_s(self):
        notified = []
        _, typing, task = await self.start_message_motion(lambda: notified.append(True))
        self.avatar.jobs[typing]['short_action_sent'] = True
        await self.until(lambda: bool(notified))
        self.avatar.jobs[typing].update(status='error', send_complete=False, worker_pending=False)
        with self.assertRaisesRegex(ValueError, 'invalid_performance_send'):
            await task
        self.assertEqual(notified, [True])
        self.assertTrue(self.p.sender_failed)

    async def test_message_motion_s_requires_current_owner_generation_and_job(self):
        notified = []
        record = {'job_id': 'identity:typing', 'owner_id': self.p.run_id,
                  'generation': self.avatar.generation, 'mode': 'motion'}
        candidate = {'record': record, 'job_id': record['job_id'], 'optional': False,
                     'require_short_action': True, 'on_message_action': lambda: notified.append(True)}
        receipt = dict(self.avatar.receipt(record, 'sent'), short_action_sent=True)
        original_run, original_generation = self.p.run_id, self.avatar.generation
        for mismatch in ('stopped', 'owner', 'generation', 'current', 'job', 'receipt_generation',
                         'candidate_job', 'receipt_owner'):
            with self.subTest(mismatch=mismatch):
                self.p.current = candidate
                value = dict(receipt)
                if mismatch == 'stopped':
                    self.p.state = 'stopping'
                elif mismatch == 'owner':
                    self.p.run_id = 'another-owner'
                elif mismatch == 'generation':
                    self.avatar.generation = 'new-generation'
                elif mismatch == 'current':
                    self.p.current = dict(candidate)
                elif mismatch == 'job':
                    value['job_id'] = 'another:typing'
                elif mismatch == 'candidate_job':
                    candidate['job_id'] = 'another:typing'
                elif mismatch == 'receipt_owner':
                    value['owner_id'] = 'another-owner'
                else:
                    value['generation'] = 'old-generation'
                with self.assertRaises(ValueError):
                    await self.p._follow(candidate, value)
                self.assertEqual(notified, [])
                self.p.state, self.p.run_id = 'running', original_run
                self.avatar.generation = original_generation
                candidate['job_id'] = record['job_id']
        self.p.current = None

    async def test_stop_during_message_motion_blocks_late_s_callback(self):
        notified = []
        _, typing, task = await self.start_message_motion(lambda: notified.append(True))
        await self.p.stop()
        self.avatar.jobs[typing]['short_action_sent'] = True
        with self.assertRaises(ValueError):
            await task
        self.assertEqual(notified, [])
        self.assertEqual(self.p.formal_waiting, 0)

    async def test_message_speech_failure_never_submits_motion(self):
        notified = []
        record, task = self.message_performance(lambda: notified.append(True))
        await self.until(lambda: record['job_id'] in self.avatar.dispatches)
        self.avatar.jobs[record['job_id']].update(status='error', worker_pending=False)
        with self.assertRaisesRegex(ValueError, 'invalid_performance_send'):
            await task
        self.assertEqual(self.avatar.dispatches, [record['job_id']])
        self.assertEqual(notified, [])

    async def test_a_sends_while_b_prepares_and_only_one_sender(self):
        a, ta = self.line('a')
        await self.until(lambda: self.p.current is a)
        self.avatar.started('a')
        b, tb = self.line('b', 'comment')
        await self.until(lambda: b['state'] == 'prepared')
        self.assertEqual(self.avatar.dispatches, ['a'])
        self.assertIs(self.p.next, b)
        self.avatar.finish('a')
        await ta
        await self.until(lambda: self.p.current is b)
        self.avatar.finish('b')
        await tb
        self.assertEqual(self.avatar.dispatches, ['a', 'b'])

    async def test_prepared_next_waits_for_tail_then_dispatches_without_another_breath(self):
        a, ta = self.line('a')
        await self.until(lambda: self.p.current is a)
        self.avatar.started('a')
        b, tb = self.line('b', 'comment')
        await self.until(lambda: b['state'] == 'prepared')
        # Even a premature terminal receipt cannot bypass worker/tail cleanup.
        self.avatar.jobs['a'].update(status='sent', send_complete=True, worker_pending=True,
                                     handoff_wait_elapsed=True, post_audio_wait_seconds=4)
        await REAL_SLEEP(0)
        self.assertFalse(ta.done())
        self.assertEqual(self.avatar.dispatches, ['a'])
        self.avatar.finish('a')
        receipt = await ta
        with patch.object(self.p.ambient.rng, 'uniform', return_value=3):
            self.p.remember_sent(a['record'], 'a', receipt)
        self.assertLessEqual(self.p.ambient.next_dispatch, time.monotonic())
        await self.until(lambda: self.p.current is b)
        self.avatar.finish('b')
        await tb
        self.assertEqual(self.avatar.dispatches, ['a', 'b'])

    async def test_breath_tail_credit_has_old_receipt_fallback_and_deduplicates(self):
        now = [100.0]
        base = {'status': 'sent', 'send_complete': True, 'handoff_wait_elapsed': True}
        cases = [(4, 0), (1, 1.5), (0, 2.5), (None, 2.5), (-1, 2.5),
                 (float('nan'), 2.5), (float('inf'), 2.5), (True, 2.5), ('4', 2.5)]
        with patch('romance.performance.time', SimpleNamespace(monotonic=lambda: now[0])), \
             patch.object(self.p.ambient.rng, 'uniform', return_value=2.5) as rng:
            for n, (elapsed, remaining) in enumerate(cases):
                self.p.remember_sent({'job_id': f'wait-{n}'}, '已说',
                                     dict(base, post_audio_wait_seconds=elapsed))
                self.assertAlmostEqual(self.p.ambient.next_dispatch, now[0] + remaining)
            for n, change in enumerate(({}, {'handoff_wait_elapsed': False})):
                receipt = dict(base, **change)
                if change:
                    receipt['post_audio_wait_seconds'] = 4
                self.p.remember_sent({'job_id': f'legacy-{n}'}, '旧回执', receipt)
                self.assertEqual(self.p.ambient.next_dispatch, 102.5)
            self.p.remember_sent({'job_id': 'motion', 'mode': 'motion'}, '',
                                 dict(base, post_audio_wait_seconds=4))
            self.assertEqual(self.p.ambient.next_dispatch, 102.5)
            record, receipt = {'job_id': 'once'}, dict(base, post_audio_wait_seconds=4)
            self.p.remember_sent(record, '一次', receipt)
            draws = rng.call_count
            now[0] = 110.0
            self.p.remember_sent(record, '一次', receipt)
            self.assertEqual(rng.call_count, draws)
            self.assertEqual(self.p.ambient.next_dispatch, 100.0)
            for n, change in enumerate(({'status': 'cancelled'}, {'send_complete': False},
                                        {'worker_pending': True}, {'preparation_pending': True})):
                self.p.remember_sent({'job_id': f'incomplete-{n}'}, '未完成', {**receipt, **change})
            self.assertEqual(rng.call_count, draws)
            self.assertEqual(self.p.ambient.next_dispatch, 100.0)

    async def test_formal_cancels_next_and_prepares_while_started_current_finishes(self):
        a, ta = self.line('a')
        await self.until(lambda: self.p.current is a)
        self.avatar.started('a')
        self.avatar.blocked.add('b')
        b, tb = self.line('b', 'comment')
        await self.until(lambda: 'b' in self.avatar.jobs)
        name, formal = self.formal()
        await self.until(lambda: self.avatar.jobs['b']['status'] == 'cancelled')
        self.assertIs(self.p.next, b)
        self.assertNotIn(name, self.avatar.prepares)
        self.avatar.finish_prepare('b')
        await tb
        await self.until(lambda: name in self.avatar.prepares)
        self.assertFalse(ta.done())
        self.assertEqual(self.avatar.dispatches, ['a'])
        self.avatar.finish('a')
        await ta
        await self.until(lambda: name in self.avatar.dispatches)
        self.avatar.finish(name)
        await formal
        self.assertNotIn('b', self.avatar.dispatches)

    async def test_formal_waits_legacy_thinking_tail_before_prepare(self):
        a, ta = self.line('motion', 'motion', motion=True)
        await self.until(lambda: self.p.current is a)
        self.avatar.started('motion')
        name, formal = self.formal()
        await self.until(lambda: self.p.next is not None)
        self.assertNotIn(name, self.avatar.prepares)
        self.avatar.finish('motion')
        await ta
        await self.until(lambda: name in self.avatar.dispatches)
        self.avatar.finish(name)
        await formal
        self.assertFalse(self.p.sender_failed)

    async def test_stop_calls_cloud_before_slow_cleanup(self):
        self.avatar.blocked.add('slow')
        _, task = self.line('slow')
        await self.until(lambda: 'slow' in self.avatar.jobs)
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.p.ambient.close
        async def close():
            entered.set()
            await release.wait()
            await original()
        with patch.object(self.p.ambient, 'close', close):
            stopping = asyncio.create_task(self.p.stop())
            await self.until(lambda: entered.is_set() and self.avatar.stopped.is_set())
            release.set()
            await stopping
        self.assertTrue(task.done())
        self.assertFalse(self.p.enabled)

    async def test_late_cancelled_prepare_holds_slot_until_resource_cleanup(self):
        valid = {'yes': True}
        self.avatar.blocked.add('late')
        c, task = self.line('late', valid=lambda: valid['yes'])
        await self.until(lambda: 'late' in self.avatar.jobs)
        valid['yes'] = False
        await self.until(lambda: self.avatar.jobs['late']['status'] == 'cancelled')
        self.assertIs(self.p.next, c)
        self.assertIsNone(self.p.reserve_next(kind='comment', valid=lambda: True))
        self.avatar.finish_prepare('late')
        await task
        self.assertIsNone(self.p.next)
        self.assertEqual(self.avatar.dispatches, [])

    async def test_lost_prepare_dispatch_responses_query_same_id_once(self):
        self.avatar.lose_prepare.add('lost')
        self.avatar.lose_after.add('lost')
        c, task = self.line('lost')
        await self.until(lambda: self.p.current is c)
        self.assertEqual(self.avatar.prepares, ['lost'])
        self.assertEqual(self.avatar.dispatches, ['lost'])
        self.assertEqual(set(self.avatar.queried_ids), {'lost'})
        self.avatar.finish('lost')
        await task

    async def test_unreceived_dispatch_retries_same_id_without_duplicate_delivery(self):
        self.avatar.lose_before.add('lost')
        _, task = self.line('lost')
        await self.until(lambda: self.avatar.active_sender == 'lost')
        self.assertEqual(self.avatar.prepares, ['lost'])
        self.assertEqual(self.avatar.dispatches, ['lost', 'lost'])
        self.assertEqual(self.avatar.trace.count(('dispatch', 'lost')), 1)
        self.avatar.finish('lost')
        await task

    async def test_unknown_dispatch_keeps_current_and_blocks_admission(self):
        self.avatar.lose_after.add('unknown')
        self.avatar.query_fail.add('unknown')
        c, task = self.line('unknown')
        with self.assertRaises(OSError):
            await task
        self.assertIs(self.p.current, c)
        self.assertTrue(self.p.sender_failed)
        self.assertIsNone(self.p.reserve_next(kind='comment', valid=lambda: True))

    async def test_expression_context_only_started_current_and_deduplicated_sent(self):
        self.avatar.blocked.add('a')
        a, ta = self.line('a')
        self.assertEqual(self.p.expression_context(), {'recent_performances': [], 'current_performance': None})
        await self.until(lambda: a['state'] == 'preparing')
        self.assertIsNone(self.p.expression_context()['current_performance'])
        self.avatar.finish_prepare('a')
        await self.until(lambda: self.p.current is a)
        self.assertIsNone(self.p.expression_context()['current_performance'])
        self.avatar.started('a')
        await self.until(lambda: (a['receipt'] or {}).get('delivery_started'))
        self.assertEqual(self.p.expression_context()['current_performance']['text'], 'a')
        b, tb = self.line('b', 'comment')
        await self.until(lambda: b['state'] == 'prepared')
        self.assertEqual(self.p.expression_context()['current_performance']['text'], 'a')
        self.assertEqual(self.p.expression_context()['recent_performances'], [])
        self.avatar.finish('a')
        receipt = await ta
        for _ in range(2):
            self.p.remember_sent(a['record'], 'a', receipt, intent='回应')
        history = self.p.expression_context()['recent_performances']
        self.assertEqual([v['text'] for v in history], ['a'])
        self.assertIsNone(history[0]['recovery'])
        self.p.ambient.next_dispatch = 0
        await self.until(lambda: self.p.current is b)
        self.avatar.finish('b')
        await tb

    async def test_comments_merge_wakeup_and_first_line_does_not_wait_random_delay(self):
        self.model.slow = True
        self.comment('one')
        self.comment('two')
        handle = self.p._comment_wakeup
        self.comment('three')
        self.assertIs(handle, self.p._comment_wakeup)
        await self.until(lambda: self.model.started.is_set())
        self.assertEqual(sum(t == 'ambient' for t, _ in self.model.calls), 1)
        self.assertFalse(self.wb.game.busy)
        self.assertEqual(len(self.wb.game.data['proposals']), 3)

    async def test_attempted_only_selected_and_responded_only_sent_ids(self):
        self.model.slow = True
        for i in range(12):
            self.comment(str(i))
        await self.until(lambda: self.model.started.is_set())
        ambient = self.p.ambient
        payload = [p for t, p in self.model.calls if t == 'ambient'][-1]
        selected = {c['id'] for c in payload['comments']}
        all_ids = {c['id'] for c in self.wb.game.data['comments']}
        self.assertEqual(set(ambient.attempted_at), selected)
        self.assertEqual(ambient.inflight_ids, selected)
        self.assertEqual(len(all_ids - selected), 4)
        self.assertEqual(ambient.responded_ids, set())
        self.model.result['comment_ids'] = [next(iter(selected))]
        self.avatar.auto_finish = True
        self.model.release.set()
        await ambient.task
        self.assertEqual(ambient.responded_ids, set(self.model.result['comment_ids']))
        self.assertEqual(ambient.inflight_ids, set())
        self.assertTrue(all_ids - selected <= {c['id'] for c in ambient.selected_comments(ambient.scope, time.monotonic())})

    async def test_proactive_yields_once_but_comment_flood_keeps_reply(self):
        self.model.slow = True
        self.p.ambient.tick(self.wb.state())
        await self.until(lambda: self.model.started.is_set())
        old = self.p.next
        self.comment('reply-now')
        await self.until(lambda: self.p.next is not None and self.p.next is not old)
        replacement = self.p.next
        self.assertEqual(replacement['kind'], 'comment')
        for i in range(8):
            self.comment('flood-' + str(i))
        await REAL_SLEEP(0)
        self.assertIs(self.p.next, replacement)
        self.assertFalse(replacement['invalidated'])
        self.assertEqual(sum(t == 'ambient' for t, _ in self.model.calls), 2)

    async def test_prestart_cancel_releases_only_same_planning_slot(self):
        self.p.ambient.tick(self.wb.state())
        self.assertIsNotNone(self.p.next)
        self.p.ambient.invalidate()
        await self.until(lambda: self.p.next is None)
        newer = self.p.reserve_next(kind='comment', valid=lambda: True)
        await REAL_SLEEP(0)
        self.assertIs(self.p.next, newer)
        self.assertEqual(self.avatar.prepares, [])
        self.p.next = None

    async def test_unconfirmed_dispatch_does_not_generate_optional_next(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.avatar.dispatch
        async def delayed(record):
            result = await original(record)
            entered.set()
            await release.wait()
            return result
        with patch.object(self.avatar, 'dispatch', delayed):
            a, ta = self.line('a')
            await entered.wait()
            self.p.ambient.tick(self.wb.state())
            self.assertIs(self.p.current, a)
            self.assertIsNone(self.p.next)
            self.assertFalse(any(t == 'ambient' for t, _ in self.model.calls))
            release.set()
            await self.until(lambda: a['receipt'] is not None)
            self.p.ambient.tick(self.wb.state())
            self.assertIsNone(self.p.next)
            self.avatar.started('a')
            await self.until(lambda: (a['receipt'] or {}).get('delivery_started'))
            self.model.slow = True
            self.p.ambient.tick(self.wb.state())
            await self.until(lambda: self.model.started.is_set())
            self.assertIsNotNone(self.p.next)
            self.avatar.finish('a')
            await ta

    async def test_phase_changed_during_prepare_discards_late_tts(self):
        self.avatar.blocked.add('old-phase')
        self.p.ambient.tick(self.wb.state())
        self.p.ambient.invalidate()
        await self.until(lambda: self.p.next is None)
        scope, epoch = self.p.ambient.scope, self.p.ambient.epoch
        c, task = self.line('old-phase', valid=lambda: self.p.ambient.current(epoch, scope))
        await self.until(lambda: 'old-phase' in self.avatar.jobs)
        self.wb.game.submit_proposal('author', '军师', '认真回答。')
        await self.wb.game.close_collection()
        self.p.ambient.tick(self.wb.state())
        await self.until(lambda: self.avatar.jobs['old-phase']['status'] == 'cancelled')
        self.avatar.finish_prepare('old-phase')
        await task
        self.assertNotIn('old-phase', self.avatar.dispatches)
        self.assertEqual(self.p.ambient.history, [])

    async def test_stop_closes_admission_before_cleanup_task_first_turn(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.p._finish_stop
        async def delayed(initiator):
            entered.set()
            await release.wait()
            await original(initiator)
        with patch.object(self.p, '_finish_stop', delayed):
            stopping = asyncio.create_task(self.p.stop())
            await entered.wait()
            self.assertFalse(self.p.enabled)
            self.assertIsNone(self.p.reserve_next(kind='comment', valid=lambda: True))
            self.p.ambient.tick(self.wb.state())
            self.assertEqual(self.avatar.prepares, [])
            self.assertEqual(self.avatar.dispatches, [])
            release.set()
            await stopping

    async def test_generation_failures_have_safe_distinct_reasons(self):
        async def timeout(task, payload):
            raise TimeoutError('PRIVATE provider payload')
        ambient = self.p.ambient
        with patch.object(self.model, 'generate', timeout):
            ambient.tick(self.wb.state())
            await ambient.task
        self.assertEqual(ambient.events[-1]['reason'], 'generation_timeout')
        self.model.result = {'speak': True, 'private': 'bad shape'}
        ambient.next_attempt = 0
        ambient.tick(self.wb.state())
        await ambient.task
        self.assertEqual(ambient.events[-1]['reason'], 'invalid_ambient_result')
        self.assertNotIn('PRIVATE', str(ambient.events))


if __name__ == '__main__':
    unittest.main()
