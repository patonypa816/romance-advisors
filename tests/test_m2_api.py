"""M2 offline evaluation FIXTURES against real PostgreSQL, not model-quality evidence."""
import asyncio
import copy
import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import event, text
from hell_inc.app import STATE, create_app
from hell_inc.config import Settings

ANSWER = ' 很抱歉让您失望。我们为您免费重做噩梦，并请地狱质检员确认恐怖等级。 '


def evaluation(scores=(2, 2, 2)):
    quotes = ('很抱歉让您失望', '免费重做噩梦', '地狱质检员')
    return {'criteria': [{'id': key, 'score': score,
                          'evidence': [quote] if score else [], 'reason': '离线契约测试依据'}
                         for key, score, quote in zip(('empathy', 'solution', 'setting'), scores, quotes)]}


class M2ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(database_url=os.environ['HELL_INC_DATABASE_URL'],
                                 world_id='m2-test-' + uuid4().hex, origin='http://localhost',
                                 lock_dir=Path(self.tmp.name))
        await self.restart()
        self.registration = dict(command_id=str(uuid4()), provider='simulated', identity_scope='local-test',
                                 subject_id='m2-guest', display_name='测试游客')
        self.original = await self.post('/api/players', self.registration)
        self.player_id = self.original['player']['player_id']

    async def restart(self):
        if hasattr(self, 'client'):
            await self.client.close()
        token = 'isolated-' + uuid4().hex
        self.client = TestClient(TestServer(create_app(self.settings, startup_token=token)),
                                 cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        await self.post('/api/session', {'token': token})

    async def post(self, path, payload, status=200):
        response = await self.client.post(path, json=payload, headers={'Origin': 'http://localhost'})
        body = await response.json()
        self.assertEqual(response.status, status, body)
        return body

    async def get(self, path):
        response = await self.client.get(path)
        body = await response.json()
        self.assertEqual(response.status, 200, body)
        return body

    async def start(self):
        receipt = await self.post('/api/interviews', {'command_id': str(uuid4()), 'player_id': self.player_id})
        self.interview_id = receipt['interview']['id']
        self.path = '/api/interviews/' + self.interview_id
        return receipt['interview']

    async def snapshot(self):
        return (await self.get(self.path))['interview']

    async def command(self, action, **extra):
        snap = await self.snapshot()
        return await self.post(self.path + '/' + action,
                               dict(command_id=str(uuid4()), expected_version=snap['version'], **extra))

    async def answered(self):
        await self.start()
        await self.command('open')
        self.answer_payload = dict(message_id=str(uuid4()), player_id=self.player_id, text=ANSWER)
        self.answer_receipt = await self.post(self.path + '/answers', self.answer_payload)
        self.assertTrue(self.answer_receipt['accepted'])
        return await self.export()

    async def export(self):
        export = await self.get(self.path + '/evaluation-request')
        self.assertEqual(set(export), {'task_id', 'attempt_no', 'input_hash', 'policy_hash', 'frozen_input'})
        return export

    def result_payload(self, export, scores=(2, 2, 2)):
        return {key: export[key] for key in ('task_id', 'attempt_no', 'input_hash', 'policy_hash')} | {
            'command_id': str(uuid4()), 'evaluation': evaluation(scores)}

    async def test_fixture_hire_atomic_profile_events_replay_and_restart(self):
        export = await self.answered()
        before = await self.snapshot()
        self.assertEqual(before['status'], 'EVALUATING')
        self.assertEqual(before['task']['status'], 'awaiting_result')
        self.assertEqual(before['answer']['text'], ANSWER)
        payload = self.result_payload(export)
        first = await self.post(self.path + '/evaluation-result', payload)
        result = first['interview']
        self.assertEqual(result['status'], 'COMPLETED')
        self.assertEqual(result['evaluation']['total'], 6)
        self.assertEqual(result['result']['decision'], 'hire')
        self.assertEqual(await self.post(self.path + '/evaluation-result', payload), {**first, 'replayed': True})
        await self.post(self.path + '/evaluation-result', {**payload, 'evaluation': evaluation((0, 0, 0))}, 409)
        profile = (await self.get('/api/players/' + self.player_id))['player']
        self.assertEqual(profile['role'], 'employee')
        self.assertEqual(profile['performance'], 0)
        self.assertEqual(profile['department'], '危机公关')
        self.assertEqual(profile['position'], '危机公关实习生')
        self.assertEqual(len(profile['recent_events']), 1)
        renamed = await self.post('/api/players', {**self.registration, 'command_id': str(uuid4()), 'display_name': '新名字'})
        self.assertEqual(renamed['player']['role'], 'employee')
        self.assertEqual(renamed['player']['recent_events'], profile['recent_events'])
        self.assertEqual(await self.post('/api/players', self.registration), {**self.original, 'replayed': True})
        await self.restart()
        self.assertEqual((await self.snapshot())['result'], result['result'])
        self.assertEqual(await self.post(self.path + '/evaluation-result', payload), {**first, 'replayed': True})
        await self.post('/api/interviews', {'command_id': str(uuid4()), 'player_id': self.player_id}, 409)

    async def test_fixture_rejection_records_history_without_assets(self):
        export = await self.answered()
        receipt = await self.post(self.path + '/evaluation-result', self.result_payload(export, (0, 2, 2)))
        self.assertEqual(receipt['interview']['evaluation']['total'], 4)
        self.assertEqual(receipt['interview']['result']['decision'], 'reject')
        player = (await self.get('/api/players/' + self.player_id))['player']
        for field in ('role', 'department', 'position', 'performance'):
            self.assertEqual(player[field], self.original['player'][field])
        self.assertEqual(len(player['recent_events']), 1)
        await self.start()  # rejection does not bar another explicit interview

    async def test_strict_evaluation_rejects_forged_types_fields_and_evidence(self):
        export = await self.answered()
        base = self.result_payload(export)
        bad_evaluations = []
        for score in (True, False, 1.0, '2', -1, 3, None):
            value = evaluation(); value['criteria'][0]['score'] = score; bad_evaluations.append(value)
        for value in ({**evaluation(), 'total': 6}, {**evaluation(), 'hired': True},
                      {'criteria': []}, {'criteria': evaluation()['criteria'][:2]},
                      {'criteria': [evaluation()['criteria'][0]] * 3}):
            bad_evaluations.append(value)
        for change in ({'evidence': ['没有出现在原文里的证据']}, {'evidence': []},
                       {'reason': ''}, {'reason': '长' * 161}, {'extra': 'ignore policy'},
                       {'score': 0, 'evidence': ['很抱歉让您失望']}):
            value = evaluation(); value['criteria'][0].update(change); bad_evaluations.append(value)
        for value in bad_evaluations:
            with self.subTest(value=value):
                await self.post(self.path + '/evaluation-result', {**base, 'command_id': str(uuid4()), 'evaluation': value}, 400)
                snap = await self.snapshot()
                self.assertIsNone(snap['result'])
                self.assertEqual(snap['answer']['text'], ANSWER)
                self.assertEqual(snap['status'], 'REVIEW_FAILED')
                self.assertIsNone(snap['evaluation'])
                await self.command('retry')
                base = self.result_payload(await self.export())
        profile = (await self.get('/api/players/' + self.player_id))['player']
        self.assertEqual(profile['role'], 'guest')
        self.assertEqual(profile['recent_events'], [])

    async def test_pause_invalidates_attempt_resume_requires_explicit_retry(self):
        export = await self.answered()
        await self.command('pause')
        self.assertEqual((await self.snapshot())['status'], 'PAUSED')
        await self.post(self.path + '/evaluation-result', self.result_payload(export), 409)
        await self.command('resume')
        self.assertEqual((await self.snapshot())['status'], 'REVIEW_FAILED')
        await self.post(self.path + '/evaluation-result', self.result_payload(export), 409)
        await self.command('retry')
        fresh = await self.export()
        self.assertEqual(fresh['attempt_no'], export['attempt_no'] + 1)
        for key in ('task_id', 'input_hash', 'policy_hash', 'frozen_input'):
            self.assertEqual(fresh[key], export[key])
        await self.post(self.path + '/evaluation-result', self.result_payload(export), 409)
        await self.post(self.path + '/evaluation-result', self.result_payload(fresh))

    async def test_restart_pauses_unfinished_no_automatic_evaluation(self):
        export = await self.answered()
        await self.restart()
        snap = await self.snapshot()
        self.assertEqual(snap['status'], 'PAUSED')
        self.assertEqual(snap['pause_reason'], 'restart')
        self.assertEqual(snap['answer']['text'], ANSWER)
        self.assertIsNone(snap['result'])
        await self.command('resume')
        self.assertEqual((await self.snapshot())['status'], 'REVIEW_FAILED')
        await self.command('retry')
        self.assertEqual((await self.export())['attempt_no'], export['attempt_no'] + 1)

    async def test_message_id_exact_replay_conflict_and_second_answer_rejected(self):
        await self.answered()
        await self.command('cancel', reason='测试取消')
        replay = await self.post(self.path + '/answers', self.answer_payload)
        self.assertEqual(replay, {**self.answer_receipt, 'replayed': True})
        await self.post(self.path + '/answers', {**self.answer_payload, 'text': ANSWER + '!'}, 409)
        await self.post(self.path + '/answers', {**self.answer_payload, 'player_id': str(uuid4())}, 409)
        self.assertIsNone((await self.snapshot())['result'])

    async def test_command_replay_precedes_version_guard_and_conflict(self):
        snap = await self.start()
        payload = dict(command_id=str(uuid4()), expected_version=snap['version'])
        first = await self.post(self.path + '/open', payload)
        await self.command('pause')
        self.assertEqual(await self.post(self.path + '/open', payload), {**first, 'replayed': True})
        await self.post(self.path + '/resume', payload, 409)
        await self.post(self.path + '/open', {**payload, 'expected_version': 999}, 409)
        await self.post(self.path + '/resume', {**payload, 'command_id': str(uuid4())}, 409)

    async def test_concurrent_start_and_answer_only_one_wins(self):
        async def start_one():
            return await self.client.post('/api/interviews', json={'command_id': str(uuid4()), 'player_id': self.player_id}, headers={'Origin': 'http://localhost'})
        responses = await asyncio.gather(*(start_one() for _ in range(6)))
        self.assertEqual(sorted(r.status for r in responses), [200, 409, 409, 409, 409, 409])
        winner = await next(r for r in responses if r.status == 200).json()
        self.interview_id = winner['interview']['id']; self.path = '/api/interviews/' + self.interview_id
        await self.command('open')
        async def answer_one():
            return await self.post(self.path + '/answers', dict(message_id=str(uuid4()), player_id=self.player_id, text=ANSWER))
        answers = await asyncio.gather(*(answer_one() for _ in range(8)))
        self.assertEqual(sum(a['accepted'] for a in answers), 1)
        self.assertEqual((await self.snapshot())['task']['attempt_no'], 1)

    async def test_m2_routes_protected_and_cross_origin_denied(self):
        await self.answered()
        for suffix in ('', '/evaluation-request'):
            response = await self.client.get(self.path + suffix, headers={'Origin': 'https://evil.example'})
            self.assertEqual(response.status, 403)
        self.client.session.cookie_jar.clear()
        for path in ('/api/interviews', self.path, self.path + '/evaluation-request'):
            self.assertEqual((await self.client.get(path)).status, 401)
        self.assertEqual((await self.client.post(self.path + '/evaluation-result', json={}, headers={'Origin': 'http://localhost'})).status, 401)

    async def test_settlement_fault_rolls_back_every_write_and_same_import_retries(self):
        export = await self.answered()
        payload = self.result_payload(export)
        state = self.client.server.app[STATE]
        before = await self.get(self.path)
        def fault(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith('INSERT INTO interview_commands'.upper()):
                raise RuntimeError('injected final receipt failure')
        event.listen(state['engine'].sync_engine, 'before_cursor_execute', fault)
        try:
            with self.assertRaisesRegex(RuntimeError, 'injected final receipt failure'):
                await state['interviews'].evaluation_result(self.interview_id, payload)
        finally:
            event.remove(state['engine'].sync_engine, 'before_cursor_execute', fault)
        after = await self.get(self.path)
        self.assertEqual(after['interview'], before['interview'])
        self.assertEqual(after['revision'], before['revision'])
        player = (await self.get('/api/players/' + self.player_id))['player']
        self.assertEqual(player['role'], 'guest'); self.assertEqual(player['recent_events'], [])
        receipt = await self.post(self.path + '/evaluation-result', payload)
        self.assertFalse(receipt['replayed'])
        self.assertEqual(receipt['interview']['status'], 'COMPLETED')

    async def rewrite_deadline(self, *, task=False):
        """Test-only DB clock deadline manipulation; no production mutation endpoint."""
        from hell_inc.db import interviews
        from sqlalchemy import select, update
        engine = self.client.server.app[STATE]['engine']
        async with engine.begin() as conn:
            row = (await conn.execute(select(interviews.c.body).where(interviews.c.id == self.interview_id))).scalar_one()
            now = (await conn.execute(text('SELECT clock_timestamp()'))).scalar_one()
            target = row['task'] if task else row
            target['deadline_at'] = now.isoformat()
            await conn.execute(update(interviews).where(interviews.c.id == self.interview_id).values(body=row))

    async def test_answer_deadline_rejection_persists_and_replays(self):
        await self.start(); await self.command('open')
        await self.rewrite_deadline()
        payload = dict(message_id=str(uuid4()), player_id=self.player_id, text=ANSWER)
        rejected = await self.post(self.path + '/answers', payload)
        self.assertFalse(rejected['accepted'])
        self.assertEqual(rejected['rejection_code'], 'deadline_passed')
        self.assertEqual(await self.post(self.path + '/answers', payload), {**rejected, 'replayed': True})
        snap = await self.snapshot()
        self.assertEqual(snap['status'], 'EXPIRED')
        self.assertIsNone(snap['task']); self.assertIsNone(snap['evaluation']); self.assertIsNone(snap['result'])
        self.assertEqual((await self.get('/api/players/' + self.player_id))['player'], self.original['player'])

    async def test_manual_evaluation_deadline_fails_not_zero_and_retry_is_explicit(self):
        export = await self.answered()
        from datetime import datetime
        snap = await self.snapshot()
        duration = datetime.fromisoformat(snap['task']['deadline_at']) - datetime.fromisoformat(snap['answer']['admitted_at'])
        self.assertEqual(duration.total_seconds(), 900)
        await self.rewrite_deadline(task=True)
        await self.post(self.path + '/evaluation-result', self.result_payload(export), 409)
        # Timer and import are both allowed to observe expiry, neither may settle.
        await self.client.server.app[STATE]['interviews'].expire_due()
        snap = await self.snapshot()
        self.assertEqual(snap['status'], 'REVIEW_FAILED')
        self.assertIsNone(snap['evaluation']); self.assertIsNone(snap['result'])
        self.assertEqual(snap['answer']['text'], ANSWER)
        await self.command('retry')
        fresh = await self.export()
        self.assertEqual(fresh['attempt_no'], export['attempt_no'] + 1)
        await self.post(self.path + '/evaluation-result', self.result_payload(export), 409)

    async def test_answer_business_rejections_and_malformed_boundaries(self):
        await self.start()
        payload = dict(message_id=str(uuid4()), player_id=self.player_id, text=ANSWER)
        rejected = await self.post(self.path + '/answers', payload)
        self.assertEqual(rejected['rejection_code'], 'not_open')
        await self.command('open')
        self.assertEqual(await self.post(self.path + '/answers', payload), {**rejected, 'replayed': True})
        wrong = await self.post(self.path + '/answers', {**payload, 'message_id': str(uuid4()), 'player_id': str(uuid4())})
        self.assertEqual(wrong['rejection_code'], 'wrong_player')
        for answer in ('', ' ', '\x00', 'a' * 1001, True, 1, None):
            await self.post(self.path + '/answers', {**payload, 'message_id': str(uuid4()), 'text': answer}, 400)
        await self.command('pause')
        paused = await self.post(self.path + '/answers', {**payload, 'message_id': str(uuid4())})
        self.assertEqual(paused['rejection_code'], 'paused')
        await self.command('resume')
        accepted = await self.post(self.path + '/answers', {**payload, 'message_id': str(uuid4())})
        self.assertTrue(accepted['accepted'])
        second = await self.post(self.path + '/answers', {**payload, 'message_id': str(uuid4())})
        self.assertEqual(second['rejection_code'], 'answer_already_submitted')

    async def test_stale_hashes_and_cancelled_import_never_mutate_profile(self):
        export = await self.answered()
        for change in ({'input_hash': '0' * 64}, {'policy_hash': '0' * 64},
                       {'task_id': str(uuid4())}, {'attempt_no': 99}):
            await self.post(self.path + '/evaluation-result', {**self.result_payload(export), **change}, 409)
        self.assertEqual((await self.snapshot())['status'], 'EVALUATING')
        await self.command('cancel', reason='取消后禁止采纳')
        await self.post(self.path + '/evaluation-result', self.result_payload(export), 409)
        self.assertIsNone((await self.snapshot())['result'])
        self.assertEqual((await self.get('/api/players/' + self.player_id))['player'], self.original['player'])

    async def test_snapshot_revision_and_interview_version_are_consistent_during_writes(self):
        await self.start()
        baseline = await self.get(self.path)
        async def writer():
            for _ in range(10):
                await self.command('pause'); await self.command('resume')
        task = asyncio.create_task(writer())
        try:
            for _ in range(30):
                current = await self.get(self.path)
                self.assertEqual(current['revision'] - baseline['revision'],
                                 current['interview']['version'] - baseline['interview']['version'])
                self.assertIn('server_time', current)
        finally:
            await task

    async def test_failed_import_receipt_replays_and_reserves_command_id(self):
        export = await self.answered()
        payload = self.result_payload(export)
        payload['evaluation']['criteria'][0]['evidence'] = ['伪造证据']
        first = await self.post(self.path + '/evaluation-result', payload, 400)
        replay = await self.post(self.path + '/evaluation-result', payload, 400)
        self.assertEqual(replay, first)
        await self.command('retry')
        await self.post(self.path + '/evaluation-result', {
            **self.result_payload(await self.export()), 'command_id': payload['command_id']}, 409)

    async def test_successful_import_payload_conflict_is_type_sensitive(self):
        export = await self.answered()
        payload = self.result_payload(export, (1, 2, 2))
        await self.post(self.path + '/evaluation-result', payload)
        for value in (True, 1.0):
            conflicting = copy.deepcopy(payload)
            conflicting['evaluation']['criteria'][0]['score'] = value
            await self.post(self.path + '/evaluation-result', conflicting, 409)

    async def test_database_failure_closes_gate_and_recovery_durably_pauses(self):
        from sqlalchemy.exc import SQLAlchemyError
        export = await self.answered()
        state = self.client.server.app[STATE]
        def fail_database(conn, cursor, statement, parameters, context, executemany):
            raise SQLAlchemyError('injected database outage; no credentials')
        event.listen(state['engine'].sync_engine, 'before_cursor_execute', fail_database)
        try:
            response = await self.client.get(self.path)
            self.assertEqual(response.status, 503)
            self.assertTrue(state['game_gate'])
            await self.post(self.path + '/evaluation-result', self.result_payload(export), 503)
        finally:
            event.remove(state['engine'].sync_engine, 'before_cursor_execute', fail_database)
        await self.post(self.path + '/evaluation-result', self.result_payload(export), 409)
        snap = await self.snapshot()
        self.assertEqual(snap['status'], 'PAUSED')
        self.assertEqual(snap['pause_reason'], 'database_recovery')
        self.assertIsNone(snap['result']); self.assertIsNone(snap['evaluation'])
        self.assertEqual(snap['answer']['text'], ANSWER)
        await self.command('resume')
        self.assertEqual((await self.snapshot())['status'], 'REVIEW_FAILED')

    async def test_concurrent_imports_settle_once_despite_unrelated_world_revision(self):
        from sqlalchemy import select, func
        from hell_inc.db import settlements, reward_ledger, game_events, model_tasks
        export = await self.answered()
        await self.post('/api/players', {**self.registration, 'command_id': str(uuid4()), 'display_name': '评估期间改名'})
        self.assertEqual(await self.export(), export)
        payload = self.result_payload(export)
        results = await asyncio.gather(*(self.post(self.path + '/evaluation-result', payload) for _ in range(6)))
        self.assertEqual(sum(not r['replayed'] for r in results), 1)
        self.assertEqual(len({r['interview']['result']['settlement_id'] for r in results}), 1)
        settlement_id = results[0]['interview']['result']['settlement_id']
        async with self.client.server.app[STATE]['engine'].connect() as conn:
            for table, condition in (
                (settlements, settlements.c.interview_id == self.interview_id),
                (reward_ledger, reward_ledger.c.settlement_id == settlement_id),
                (game_events, game_events.c.interview_id == self.interview_id),
                (model_tasks, model_tasks.c.interview_id == self.interview_id),
            ):
                count = (await conn.execute(select(func.count()).select_from(table).where(condition))).scalar_one()
                self.assertEqual(count, 1, table.name)

    async def test_restart_ready_answering_failed_and_paused_requires_manual_resume(self):
        for stage in ('READY', 'ANSWERING', 'REVIEW_FAILED', 'PAUSED'):
            with self.subTest(stage=stage):
                await self.start()
                if stage in ('ANSWERING', 'REVIEW_FAILED'):
                    await self.command('open')
                if stage == 'REVIEW_FAILED':
                    await self.post(self.path + '/answers', dict(message_id=str(uuid4()), player_id=self.player_id, text=ANSWER))
                    invalid = self.result_payload(await self.export())
                    invalid['evaluation'] = {'criteria': []}
                    await self.post(self.path + '/evaluation-result', invalid, 400)
                if stage == 'PAUSED':
                    await self.command('pause')
                before = await self.snapshot()
                await self.restart()
                after = await self.snapshot()
                self.assertEqual(after['status'], 'PAUSED')
                self.assertIsNone(after['result'])
                self.assertEqual(after['answer'], before['answer'])
                await self.command('resume')
                self.assertEqual((await self.snapshot())['status'], 'READY' if stage == 'PAUSED' else stage)
                await self.command('cancel', reason='独立状态重启验证结束')

    async def test_import_queued_before_observed_outage_rechecks_gate_after_mutex(self):
        export = await self.answered()
        state = self.client.server.app[STATE]
        # Simulate a preceding serialized handler that observed database failure.
        # The queued import must not rely on its pre-mutex view of game_gate.
        async with state['game_lock']:
            pending = asyncio.create_task(self.post(self.path + '/evaluation-result', self.result_payload(export), 409))
            await asyncio.sleep(0)
            state['game_gate'] = True
        await pending
        snap = await self.snapshot()
        self.assertEqual(snap['status'], 'PAUSED')
        self.assertEqual(snap['pause_reason'], 'database_recovery')
        self.assertIsNone(snap['result'])
        player = (await self.get('/api/players/' + self.player_id))['player']
        self.assertEqual(player['role'], 'guest'); self.assertEqual(player['recent_events'], [])

    async def test_nul_in_evaluation_is_malformed_not_database_outage(self):
        export = await self.answered()
        payload = self.result_payload(export)
        payload['evaluation']['criteria'][0]['reason'] = 'bad\x00text'
        await self.post(self.path + '/evaluation-result', payload, 400)
        self.assertFalse(self.client.server.app[STATE]['game_gate'])
        self.assertIsNone((await self.snapshot())['result'])
        import json
        raw = json.dumps(self.result_payload(export)).replace('"score": 2', '"score": 1e9999', 1)
        response = await self.client.post(self.path + '/evaluation-result', data=raw,
                                         headers={'Origin': 'http://localhost', 'Content-Type': 'application/json'})
        self.assertEqual(response.status, 400)
        self.assertFalse(self.client.server.app[STATE]['game_gate'])
        self.assertEqual((await self.snapshot())['status'], 'EVALUATING')
