"""M3 deterministic contract fixtures against PostgreSQL; NOT native-model evidence."""
import asyncio
import copy
import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import event, select, update, text, func
from hell_inc.app import STATE, create_app
from hell_inc.config import Settings
from hell_inc import db

WORK = '  噩梦打卡枕：睡着就替客户上班，醒来发现老板变成枕头。投诉后客服免费换梦并退回加班时间。\n'


def evaluation(scores=(2, 2, 2)):
    return {'criteria': [dict(id=key, score=score, evidence=[quote] if score else [], reason='离线契约测试：依据原文')
                         for key, score, quote in zip(('originality', 'coherence', 'setting'), scores,
                             ('老板变成枕头', '客服免费换梦', '噩梦打卡枕'))],
            'comment': '这款枕头代替客户上班，并由客服处理换梦。'}


class M3ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(database_url=os.environ['HELL_INC_DATABASE_URL'], world_id='m3-test-' + uuid4().hex,
                                 origin='http://localhost', lock_dir=Path(self.tmp.name))
        await self.restart()
        self.people = [await self.register(i) for i in range(3)]

    async def restart(self):
        if hasattr(self, 'client'): await self.client.close()
        token = 'isolated-' + uuid4().hex
        self.client = TestClient(TestServer(create_app(self.settings, startup_token=token)), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server(); self.addAsyncCleanup(self.client.close)
        await self.post('/api/session', {'token': token})

    async def post(self, path, payload, status=200):
        response = await self.client.post(path, json=payload, headers={'Origin': 'http://localhost'})
        body = await response.json(); self.assertEqual(response.status, status, body); return body

    async def get(self, path):
        response = await self.client.get(path); body = await response.json()
        self.assertEqual(response.status, 200, body); return body

    async def register(self, n):
        result = await self.post('/api/players', dict(command_id=str(uuid4()), provider='simulated', identity_scope='local-test',
                                                    subject_id=f'guest-{n}', display_name=f'测试作者{n}'))
        return result['player']['player_id']

    async def start(self):
        result = await self.post('/api/competitions', {'command_id': str(uuid4())})
        self.cid = result['competition']['id']; self.path = '/api/competitions/' + self.cid
        return result['competition']

    async def snapshot(self): return (await self.get(self.path))['competition']

    async def command(self, action, status=200, **extra):
        snap = await self.snapshot()
        return await self.post(self.path + '/' + action, dict(command_id=str(uuid4()), expected_version=snap['version'], **extra), status)

    async def subcommand(self, sid, action, status=200):
        return await self.command(f'submissions/{sid}/{action}', status)

    async def submit(self, person, revision=0, work=WORK, message_id=None):
        payload = dict(message_id=message_id or str(uuid4()), player_id=person, expected_submission_revision=revision, text=work)
        return await self.post(self.path + '/submissions', payload)

    async def vote(self, person, sid, revision=0):
        return await self.post(self.path + '/votes', dict(message_id=str(uuid4()), player_id=person, submission_id=sid, expected_vote_revision=revision))

    async def export(self, sid): return await self.get(self.path + f'/submissions/{sid}/evaluation-request')

    def result_payload(self, exported, scores=(2, 2, 2)):
        return {key: exported[key] for key in ('task_id', 'attempt_no', 'input_hash', 'policy_hash')} | {
            'command_id': str(uuid4()), 'evaluation': evaluation(scores)}

    async def accept(self, sid, exported=None, scores=(2, 2, 2)):
        return await self.post(self.path + f'/submissions/{sid}/evaluation-result', self.result_payload(exported or await self.export(sid), scores))

    async def reviewing(self, people=None):
        await self.start(); await self.command('open')
        self.sids = [(await self.submit(p))['submission_id'] for p in (people or self.people)]
        await self.command('close-submissions')
        return [await self.export(sid) for sid in self.sids]

    async def displaying(self, people=None):
        exports = await self.reviewing(people)
        for sid, exported in zip(self.sids, exports): await self.accept(sid, exported)
        self.assertEqual((await self.snapshot())['status'], 'DISPLAYING')

    async def voting(self):
        for sid in self.sids: await self.subcommand(sid, 'confirm-display')
        await self.command('open-voting')

    def hidden(self, snap):
        self.assertIsNone(snap['result']); self.assertIsNone(snap['votes'])
        for work in snap['submissions']: self.assertIsNone(work['evaluation'])

    async def test_three_guest_full_round_votes_changes_ties_and_single_atomic_settlement(self):
        await self.displaying(); self.hidden(await self.snapshot())
        await self.command('open-voting', 409)
        await self.voting()
        first = await self.vote(self.people[0], self.sids[0]); self.assertTrue(first['accepted'])  # self-vote
        self.assertTrue((await self.vote(self.people[0], self.sids[1], 1))['accepted'])
        self.assertEqual((await self.vote(self.people[0], self.sids[2], 1))['rejection_code'], 'stale_vote_revision')
        self.assertTrue((await self.vote(self.people[1], self.sids[0]))['accepted'])
        self.hidden(await self.snapshot())
        await self.command('close-voting')
        self.hidden(await self.snapshot())
        snap = await self.snapshot(); payload = dict(command_id=str(uuid4()), expected_version=snap['version'])
        receipts = await asyncio.gather(*(self.post(self.path + '/settle', payload) for _ in range(4)))
        self.assertEqual(sum(not r['replayed'] for r in receipts), 1)
        result = receipts[0]['competition']['result']
        self.assertEqual(len(result['awards']), 2)
        self.assertEqual({a['submission_id'] for a in result['awards']}, {self.sids[0]})
        self.assertEqual({a['performance_delta'] for a in result['awards']}, {0})
        for p in self.people:
            profile = (await self.get('/api/players/' + p))['player']
            self.assertEqual(profile['role'], 'guest'); self.assertEqual(profile['performance'], 0)
            self.assertEqual(len(profile['recent_events']), 1)
        async with self.client.server.app[STATE]['engine'].connect() as conn:
            for table, condition, expected in (
                (db.competition_settlements, db.competition_settlements.c.competition_id == self.cid, 1),
                (db.competition_events, db.competition_events.c.competition_id == self.cid, 3),
                (db.competition_reward_ledger, db.competition_reward_ledger.c.settlement_id == result['settlement_id'], 2)):
                self.assertEqual((await conn.execute(select(func.count()).select_from(table).where(condition))).scalar_one(), expected)
        await self.restart(); self.assertEqual((await self.snapshot())['result'], result)
        self.assertEqual(await self.post(self.path + '/settle', payload), {**receipts[0], 'replayed': True})

    async def test_twelve_works_capacity_all_review_all_display_votes_and_settlement(self):
        self.people += [await self.register(i) for i in range(3, 13)]
        await self.start(); await self.command('open')
        self.sids = [(await self.submit(p))['submission_id'] for p in self.people[:12]]
        self.assertEqual((await self.submit(self.people[12]))['rejection_code'], 'capacity_reached')
        self.assertTrue((await self.submit(self.people[0], 1, WORK + '修订'))['accepted'])
        await self.command('close-submissions')
        exports = [await self.export(s) for s in self.sids]
        for sid, exported in zip(self.sids[:-1], exports[:-1]): await self.accept(sid, exported)
        snap = await self.snapshot(); self.assertNotEqual(snap['status'], 'DISPLAYING')
        self.assertTrue(all(s['comment'] is None for s in snap['submissions']))
        await self.accept(self.sids[-1], exports[-1]); self.hidden(await self.snapshot())
        for sid in self.sids[:-1]: await self.subcommand(sid, 'confirm-display')
        await self.command('open-voting', 409)
        await self.subcommand(self.sids[-1], 'confirm-display'); await self.command('open-voting')
        for person, sid in zip(self.people[:12], self.sids):
            self.assertTrue((await self.vote(person, sid))['accepted'])
        await self.command('close-voting'); result = (await self.command('settle'))['competition']['result']
        self.assertEqual(len(result['awards']), 2); self.assertEqual(len(result['rankings']), 12)
        self.assertTrue(all(row['votes'] == 1 for row in result['rankings']))
        self.assertEqual(result['awards'][0]['submission_id'], self.sids[0])

    async def test_revisions_raw_text_midround_guests_replay_and_closed_boundary(self):
        await self.start(); await self.command('open')
        late = await self.register('joined-after-open')
        first = await self.submit(late); self.assertTrue(first['accepted'])
        await self.submit(self.people[0])
        revised = await self.submit(late, 1); self.assertEqual(revised['submission_revision'], 2)
        self.assertEqual(first['submission_id'], revised['submission_id'])
        payload = dict(message_id=str(uuid4()), player_id=late, expected_submission_revision=1, text=WORK)
        rejected = await self.post(self.path + '/submissions', payload)
        self.assertEqual(rejected['rejection_code'], 'stale_submission_revision')
        await self.command('close-submissions')
        self.assertEqual(await self.post(self.path + '/submissions', payload), {**rejected, 'replayed': True})
        self.assertFalse((await self.submit(late, 2))['accepted'])
        exported = await self.export(first['submission_id'])
        self.assertEqual(exported['frozen_input']['submission']['text'], WORK)
        self.assertEqual(exported['frozen_input']['submission']['revision'], 2)
        await self.post(self.path + '/submissions', {**payload, 'expected_submission_revision': 0}, 409)

    async def test_one_failure_blocks_everything_retry_not_reroll_and_hash_independence(self):
        exports = await self.reviewing()
        await self.accept(self.sids[0], exports[0])
        self.assertEqual(await self.export(self.sids[1]), exports[1])
        bad = self.result_payload(exports[1]); bad['evaluation']['criteria'][0]['evidence'] = ['不在原文']
        path = self.path + f'/submissions/{self.sids[1]}/evaluation-result'
        original_error = await self.post(path, bad, 400)
        self.assertEqual(await self.post(path, bad, 400), original_error)
        await self.accept(self.sids[2], exports[2])
        snap = await self.snapshot(); self.assertEqual(snap['status'], 'WAITING_REVIEW'); self.hidden(snap)
        self.assertTrue(all(s['comment'] is None for s in snap['submissions']))
        await self.command('open-voting', 409); await self.command('settle', 409)
        await self.subcommand(self.sids[0], 'retry', 409)
        await self.subcommand(self.sids[1], 'retry')
        fresh = await self.export(self.sids[1]); self.assertEqual(fresh['attempt_no'], exports[1]['attempt_no'] + 1)
        for key in ('task_id', 'input_hash', 'policy_hash', 'frozen_input'): self.assertEqual(fresh[key], exports[1][key])
        await self.post(path, self.result_payload(exports[1]), 409)
        await self.post(path, {**self.result_payload(fresh), 'command_id': bad['command_id']}, 409)
        await self.accept(self.sids[1], fresh)
        self.assertEqual((await self.snapshot())['status'], 'DISPLAYING')
        await self.subcommand(self.sids[0], 'retry', 409)

    async def test_restart_keeps_accepted_invalidates_running_attempt_and_cancel_retains(self):
        exports = await self.reviewing(); await self.accept(self.sids[0], exports[0])
        before = await self.snapshot(); await self.restart()
        snap = await self.snapshot(); self.assertEqual(snap['status'], 'PAUSED')
        self.assertEqual(snap['submissions'][0]['task'], before['submissions'][0]['task'])
        await self.command('resume'); self.assertEqual((await self.snapshot())['status'], 'WAITING_REVIEW')
        await self.subcommand(self.sids[0], 'retry', 409)
        await self.post(self.path + f'/submissions/{self.sids[1]}/evaluation-result', self.result_payload(exports[1]), 409)
        await self.subcommand(self.sids[1], 'retry')
        await self.command('cancel', reason='测试主动取消；不得暗中排除作品')
        snap = await self.snapshot(); self.assertEqual(snap['status'], 'CANCELLED')
        self.assertEqual(len(snap['submissions']), 3); self.assertIsNone(snap['result'])
        for p in self.people: self.assertEqual((await self.get('/api/players/' + p))['player']['recent_events'], [])

    async def test_typed_score_conflict_wrong_hash_and_malformed_attempt_receipt(self):
        exports = await self.reviewing(); path = self.path + f'/submissions/{self.sids[0]}/evaluation-result'
        good = self.result_payload(exports[0], (1, 2, 2))
        for change in ({'input_hash': '0'*64}, {'task_id': str(uuid4())}, {'attempt_no': 999}):
            await self.post(path, {**good, **change, 'command_id': str(uuid4())}, 409)
        await self.post(path, good)
        for bad in (True, 1.0):
            changed = copy.deepcopy(good); changed['evaluation']['criteria'][0]['score'] = bad
            await self.post(path, changed, 409)
        second_path = self.path + f'/submissions/{self.sids[1]}/evaluation-result'
        bad = self.result_payload(exports[1]); bad['evaluation']['total'] = 6
        await self.post(second_path, bad, 400)
        self.assertEqual((await self.snapshot())['status'], 'WAITING_REVIEW')

    async def test_settlement_mid_ledger_fault_rolls_back_all_and_same_command_retries(self):
        await self.displaying(); await self.voting(); await self.vote(self.people[0], self.sids[0]); await self.command('close-voting')
        before = await self.get(self.path); payload = dict(command_id=str(uuid4()), expected_version=before['competition']['version'])
        state = self.client.server.app[STATE]; inserts = []
        def fault(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().lower().startswith('insert into competition_reward_ledger'):
                inserts.append(statement)
                if len(inserts) == 2: raise RuntimeError('injected second award ledger failure')
        event.listen(state['engine'].sync_engine, 'before_cursor_execute', fault)
        try:
            with self.assertRaisesRegex(RuntimeError, 'second award ledger'):
                await state['competitions'].command(self.cid, 'settle', payload)
        finally: event.remove(state['engine'].sync_engine, 'before_cursor_execute', fault)
        after = await self.get(self.path); self.assertEqual(after['competition'], before['competition']); self.assertEqual(after['revision'], before['revision'])
        async with state['engine'].connect() as conn:
            self.assertEqual((await conn.execute(select(func.count()).select_from(db.competition_settlements).where(db.competition_settlements.c.competition_id == self.cid))).scalar_one(), 0)
            self.assertEqual((await conn.execute(select(func.count()).select_from(db.competition_events).where(db.competition_events.c.competition_id == self.cid))).scalar_one(), 0)
        self.assertFalse((await self.post(self.path + '/settle', payload))['replayed'])

    async def test_database_failure_gate_recovers_to_durable_pause(self):
        from sqlalchemy.exc import SQLAlchemyError
        exports = await self.reviewing(); state = self.client.server.app[STATE]
        def fail(*args): raise SQLAlchemyError('isolated injected database outage')
        event.listen(state['engine'].sync_engine, 'before_cursor_execute', fail)
        try:
            self.assertEqual((await self.client.get(self.path)).status, 503); self.assertTrue(state['game_gate'])
        finally: event.remove(state['engine'].sync_engine, 'before_cursor_execute', fail)
        await self.post(self.path + f'/submissions/{self.sids[0]}/evaluation-result', self.result_payload(exports[0]), 409)
        snap = await self.snapshot(); self.assertEqual(snap['status'], 'PAUSED'); self.assertEqual(snap['pause_reason'], 'database_recovery')
        self.hidden(snap)

    async def test_protected_routes_origin_and_single_active_round(self):
        await self.reviewing()
        await self.post('/api/competitions', {'command_id': str(uuid4())}, 409)
        for path in ('/api/competitions', self.path, self.path + f'/submissions/{self.sids[0]}/evaluation-request'):
            response = await self.client.get(path, headers={'Origin': 'https://evil.example'}); self.assertEqual(response.status, 403)
        self.client.session.cookie_jar.clear()
        for path in ('/api/competitions', self.path, self.path + '/vote-state?player_id=' + self.people[0]):
            self.assertEqual((await self.client.get(path)).status, 401)

    async def hire(self, person):
        # Real M2 path proves M3 qualification is not a prerequisite or a mutable role lookup.
        from test_m2_api import ANSWER, evaluation as interview_evaluation
        receipt = await self.post('/api/interviews', {'command_id': str(uuid4()), 'player_id': person})
        path = '/api/interviews/' + receipt['interview']['id']
        await self.post(path + '/open', dict(command_id=str(uuid4()), expected_version=receipt['interview']['version']))
        await self.post(path + '/answers', dict(message_id=str(uuid4()), player_id=person, text=ANSWER))
        exported = await self.get(path + '/evaluation-request')
        payload = {key: exported[key] for key in ('task_id', 'attempt_no', 'input_hash', 'policy_hash')}
        await self.post(path + '/evaluation-result', {**payload, 'command_id': str(uuid4()), 'evaluation': interview_evaluation()})

    async def test_first_submission_qualification_frozen_guest_hired_later_gets_no_performance(self):
        await self.start(); await self.command('open')
        first = await self.submit(self.people[0]); await self.hire(self.people[0])
        self.assertTrue((await self.submit(self.people[0], 1))['accepted'])
        self.sids = [first['submission_id']] + [(await self.submit(p))['submission_id'] for p in self.people[1:]]
        await self.command('close-submissions')
        for sid in self.sids: await self.accept(sid)
        await self.voting(); await self.vote(self.people[0], self.sids[0]); await self.command('close-voting')
        result = (await self.command('settle'))['competition']['result']
        self.assertEqual([a['performance_delta'] for a in result['awards']], [0, 0])
        profile = (await self.get('/api/players/' + self.people[0]))['player']
        self.assertEqual(profile['role'], 'employee'); self.assertEqual(profile['performance'], 0)
        self.assertEqual(len(profile['recent_events']), 2)

    async def test_employee_first_submission_two_awards_adds_exactly_two_with_actual_ledger(self):
        await self.start(); await self.command('open'); await self.hire(self.people[0])
        self.sids = [(await self.submit(p))['submission_id'] for p in self.people]
        await self.command('close-submissions')
        for sid in self.sids: await self.accept(sid)
        await self.voting(); await self.vote(self.people[0], self.sids[0]); await self.command('close-voting')
        result = (await self.command('settle'))['competition']['result']
        self.assertEqual([a['performance_delta'] for a in result['awards']], [1, 1])
        self.assertEqual((await self.get('/api/players/' + self.people[0]))['player']['performance'], 2)
        async with self.client.server.app[STATE]['engine'].connect() as conn:
            ledgers = (await conn.execute(select(db.competition_reward_ledger).where(db.competition_reward_ledger.c.settlement_id == result['settlement_id']))).mappings().all()
            self.assertEqual(sorted((r['before']['performance'], r['after']['performance']) for r in ledgers), [(0, 1), (1, 2)])

    async def rewrite_deadline(self, sid=None):
        async with self.client.server.app[STATE]['engine'].begin() as conn:
            body = (await conn.execute(select(db.competitions.c.body).where(db.competitions.c.id == self.cid))).scalar_one()
            now = (await conn.execute(text('SELECT clock_timestamp()'))).scalar_one()
            target = next(s['task'] for s in body['submissions'] if s['id'] == sid) if sid else body
            target['deadline_at'] = now.isoformat()
            await conn.execute(update(db.competitions).where(db.competitions.c.id == self.cid).values(body=body))

    async def test_server_deadline_late_submission_rejected_even_without_timer(self):
        await self.start(); await self.command('open'); await self.submit(self.people[0]); await self.rewrite_deadline()
        receipt = await self.submit(self.people[1]); self.assertFalse(receipt['accepted'])
        self.assertEqual(receipt['rejection_code'], 'deadline_passed')
        self.assertEqual(len((await self.snapshot())['submissions']), 1)

    async def test_one_expired_review_waits_never_zero_and_retry_preserves_hash(self):
        exports = await self.reviewing(); await self.rewrite_deadline(self.sids[0])
        await self.post(self.path + f'/submissions/{self.sids[0]}/evaluation-result', self.result_payload(exports[0]), 409)
        for sid, exported in zip(self.sids[1:], exports[1:]): await self.accept(sid, exported)
        snap = await self.snapshot(); self.assertEqual(snap['status'], 'WAITING_REVIEW'); self.hidden(snap)
        await self.subcommand(self.sids[0], 'retry')
        fresh = await self.export(self.sids[0]); self.assertEqual(fresh['input_hash'], exports[0]['input_hash'])
        self.assertEqual(fresh['attempt_no'], exports[0]['attempt_no'] + 1)
        await self.accept(self.sids[0], fresh); self.assertEqual((await self.snapshot())['status'], 'DISPLAYING')

    async def test_vote_state_no_tallies_late_voter_can_join_and_deadline_blocks_change(self):
        await self.displaying(); await self.voting(); late = await self.register('late-voter')
        state = await self.get(self.path + '/vote-state?player_id=' + late)
        self.assertEqual(state, {'player_id': late, 'vote_revision': 0, 'submission_id': None})
        self.assertTrue((await self.vote(late, self.sids[1]))['accepted'])
        self.assertEqual(await self.get(self.path + '/vote-state?player_id=' + late),
                         {'player_id': late, 'vote_revision': 1, 'submission_id': self.sids[1]})
        await self.rewrite_deadline(); rejected = await self.vote(late, self.sids[0], 1)
        self.assertFalse(rejected['accepted']); self.assertEqual(rejected['rejection_code'], 'deadline_passed')
        result = (await self.command('settle'))['competition']['result']
        self.assertEqual(next(a for a in result['awards'] if a['award_type'] == 'popularity')['submission_id'], self.sids[1])

    async def test_simultaneous_submission_close_has_one_serialized_final_revision(self):
        await self.start(); await self.command('open'); first = await self.submit(self.people[0])
        snap = await self.snapshot()
        async def close():
            response = await self.client.post(self.path + '/close-submissions', json=dict(command_id=str(uuid4()), expected_version=snap['version']), headers={'Origin': 'http://localhost'})
            self.assertIn(response.status, (200, 409)); return response.status
        revision, status = await asyncio.gather(self.submit(self.people[0], 1, WORK + '末次改稿'), close())
        if status == 409: await self.command('close-submissions')
        exported = await self.export(first['submission_id'])
        self.assertEqual(exported['frozen_input']['submission']['revision'], 2 if revision['accepted'] else 1)
        self.assertEqual(exported['frozen_input']['submission']['text'], WORK + '末次改稿' if revision['accepted'] else WORK)

    async def test_zero_votes_has_only_creativity_award_not_arbitrary_popularity(self):
        await self.displaying(); await self.voting(); await self.command('close-voting')
        result = (await self.command('settle'))['competition']['result']
        self.assertEqual([a['award_type'] for a in result['awards']], ['creativity'])
        self.assertTrue(all(row['votes'] == 0 for row in result['rankings']))

    async def test_comment_rank_words_warn_without_discarding_accepted_evaluation(self):
        exports = await self.reviewing()
        await self.accept(self.sids[0], exports[0]); await self.accept(self.sids[2], exports[2])
        payload = self.result_payload(exports[1]); payload['evaluation']['comment'] = '满分冠军，排名第一，领先其他作品。'
        path = self.path + f'/submissions/{self.sids[1]}/evaluation-result'
        original = await self.post(path, payload)
        self.assertEqual(await self.post(path, payload), {**original, 'replayed': True})
        snap = await self.snapshot(); self.assertEqual(snap['status'], 'DISPLAYING'); self.hidden(snap)
        work=next(s for s in snap['submissions'] if s['id']==self.sids[1])
        self.assertEqual(work['comment'],payload['evaluation']['comment']); self.assertTrue(work['comment_warning'])
        self.assertEqual(work['task']['status'],'accepted')
        await self.command('open-voting', 409); await self.command('settle', 409)
        await self.voting()
        self.assertEqual((await self.snapshot())['status'], 'VOTING')

    async def test_reserved_payload_keys_cannot_override_receipt_route_or_action(self):
        snap = await self.start(); payload = dict(command_id=str(uuid4()), expected_version=snap['version'])
        original = await self.post(self.path + '/open', payload)
        await self.post(self.path + '/cancel', {**payload, 'action': 'open'}, 409)
        other = '/api/competitions/' + str(uuid4())
        await self.post(other + '/open', {**payload, 'competition_id': self.cid}, 409)
        self.assertEqual(await self.post(self.path + '/open', payload), {**original, 'replayed': True})
        self.assertEqual((await self.snapshot())['status'], 'SUBMITTING')
        message = dict(message_id=str(uuid4()), player_id=self.people[0], expected_submission_revision=0, text=WORK)
        receipt = await self.post(self.path + '/submissions', message)
        await self.post(self.path + '/submissions', {**message, 'expected_submission_revision': False}, 409)
        await self.post(other + '/submissions', {**message, 'competition_id': self.cid}, 409)
        self.assertEqual(await self.post(self.path + '/submissions', message), {**receipt, 'replayed': True})
