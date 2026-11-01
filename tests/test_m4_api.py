"""M4 PostgreSQL/HTTP offline fixtures. No real TTS, RTC, audio or audience proof."""
import asyncio
import copy
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select, update
from hell_inc.app import STATE, create_app
from hell_inc.config import Settings
from hell_inc import db
from hell_inc.performance_contract import canonical_hash, iso
from test_m4_contract import signed_payload


class BarrierAdapter:
    """Controlled test-only transport: barriers, never wall-clock media simulation."""
    def __init__(self, block_prepare=False, block_send=False, fail_prepare=False):
        self.preparing = asyncio.Event(); self.sending = asyncio.Event()
        self.prepare_release = asyncio.Event(); self.send_release = asyncio.Event()
        if not block_prepare: self.prepare_release.set()
        if not block_send: self.send_release.set()
        self.fail_prepare = fail_prepare; self.calls = []; self.active = 0; self.max_active = 0

    async def prepare(self, segment, cancellation):
        self.preparing.set(); await self.prepare_release.wait()
        if self.fail_prepare: raise RuntimeError('test preparation failed')
        from hell_inc.performance_adapter import FixtureAdapter
        return await FixtureAdapter().prepare(segment, cancellation)

    async def send(self, prepared, session, still_valid):
        from hell_inc.performance_adapter import FixtureAdapter
        self.calls.append(prepared); self.active += 1; self.max_active = max(self.max_active, self.active)
        self.sending.set()
        try:
            async def hold(_prepared): await self.send_release.wait()
            return await FixtureAdapter(send_hook=hold).send(prepared, session, still_valid)
        finally: self.active -= 1


class M4Fixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(database_url=os.environ['HELL_INC_DATABASE_URL'], world_id='m4-test-' + uuid4().hex,
                                 origin='http://localhost', lock_dir=Path(self.tmp.name))
        await self.restart()
        self.sid = str(uuid4())
        self.session_payload = dict(command_id=str(uuid4()), session_id=self.sid, mode='offline_fixture', avatar_aspect=None)
        await self.post('/api/performance-session', self.session_payload, 201)

    async def restart(self):
        if hasattr(self, 'client'): await self.client.close()
        token = 'isolated-' + uuid4().hex
        self.client = TestClient(TestServer(create_app(self.settings, startup_token=token)), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server(); self.addAsyncCleanup(self.client.close)
        await self.post('/api/session', {'token': token})

    @property
    def service(self): return self.client.server.app[STATE]['performances']

    async def post(self, path, payload, status=200):
        response = await self.client.post(path, json=payload, headers={'Origin': 'http://localhost'})
        body = await response.json(); self.assertEqual(response.status, status, body); return body

    async def get(self, path):
        response = await self.client.get(path); body = await response.json()
        self.assertEqual(response.status, 200, body); return body

    async def submit(self, **changes):
        body = signed_payload(self.sid, **changes)
        receipt = await self.post('/api/performances', body, 201)
        return body, receipt['performance']

    async def action(self, pid, action, status=200, command_id=None):
        return await self.post('/api/performances/' + pid + '/' + action, {'command_id': command_id or str(uuid4())}, status)

    async def record(self, pid): return (await self.get('/api/performances/' + pid))['performance']

    async def drained(self):
        # Bounded test timeout detects a hung worker, not elapsed-media evidence.
        async def drain():
            while self.service.tasks: await asyncio.gather(*tuple(self.service.tasks))
        await asyncio.wait_for(drain(), 5)


class M4ApiTests(M4Fixture):
    async def test_fixture_execution_concurrent_execute_once_no_outbound_http_explicit_media_false(self):
        adapter = BarrierAdapter(block_send=True); self.service.adapter = adapter
        body, _ = await self.submit(); pid = body['performance_id']
        # HTTP test client is used before/after this narrow worker-only patch.
        await asyncio.gather(*(self.action(pid, 'execute') for _ in range(5)))
        await asyncio.wait_for(adapter.sending.wait(), 5)
        with patch('aiohttp.ClientSession._request', side_effect=AssertionError('outbound HTTP forbidden')) as outbound:
            adapter.send_release.set(); await self.drained(); outbound.assert_not_called()
        done = await self.record(pid)
        self.assertEqual(done['state'], 'send_completed')
        self.assertEqual(len(adapter.calls), len(body['segments'])); self.assertEqual(adapter.max_active, 1)
        self.assertEqual(done['evidence'], {'execution_kind': 'offline_fixture', 'media_verified': False, 'audience_heard': False})
        self.assertIn('fixture_receipt', done['result']); self.assertNotIn('provider_receipt', done['result'])
        await self.action(pid, 'execute'); self.assertEqual(len(adapter.calls), len(body['segments']))

    async def test_same_id_conflict_terminal_replay_and_typed_payload(self):
        body, _ = await self.submit(); pid = body['performance_id']
        await self.action(pid, 'cancel')
        replay = await self.post('/api/performances', body)
        self.assertEqual(replay['performance']['state'], 'cancelled')
        for change in ({'session_id': str(uuid4())}, {'expires_at': '2099-01-02T00:00:00.000Z'},
                       {'segments': [{'text': '替换文本', 'intent': 'result'}]}):
            await self.post('/api/performances', signed_payload(**{**body, **change}), 409)
        await self.post('/api/performances', {**body, 'payload_hash': '0' * 64}, 400)
        for typed in (True, 1, 1.0):
            changed = signed_payload(**{**body, 'source_ref': {**body['source_ref'], 'source_version': typed}})
            await self.post('/api/performances', changed, 409)
            await self.post('/api/performances', {**changed, 'performance_id': str(uuid4())}, 400)
        for value in (True, 1, 1.0):
            await self.post('/api/performance-session', {**self.session_payload, 'avatar_aspect': value}, 409)

    async def test_command_id_dedup_cross_action_and_session(self):
        body, _ = await self.submit(); pid = body['performance_id']; command = str(uuid4())
        original = await self.action(pid, 'cancel', command_id=command)
        repeat = await self.action(pid, 'cancel', command_id=command)
        self.assertEqual(repeat['performance'], original['performance'])
        await self.action(pid, 'execute', 409, command_id=command)
        await self.post('/api/performance-session/' + self.sid + '/pause', {'command_id': command}, 409)

    async def test_cancel_before_start_sends_nothing(self):
        adapter = BarrierAdapter(); self.service.adapter = adapter
        body, _ = await self.submit(); pid = body['performance_id']
        result = await self.action(pid, 'cancel')
        self.assertEqual(result['cancel_outcome'], 'unstarted_cancelled')
        await self.action(pid, 'execute'); await self.drained()
        self.assertEqual(adapter.calls, [])
        self.assertEqual((await self.record(pid))['state'], 'cancelled')

    async def test_cancel_during_send_keeps_prefix_receipt_cancels_suffix(self):
        adapter = BarrierAdapter(block_send=True); self.service.adapter = adapter
        body, _ = await self.submit(); pid = body['performance_id']
        await self.action(pid, 'execute'); await asyncio.wait_for(adapter.sending.wait(), 5)
        cancelled = await asyncio.wait_for(self.action(pid, 'cancel'), 2)
        self.assertEqual(cancelled['cancel_outcome'], 'in_flight')
        adapter.send_release.set(); await self.drained()
        record = await self.record(pid)
        self.assertEqual(record['state'], 'cancelled'); self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(record['segments'][0]['state'], 'send_completed')
        self.assertTrue(all(s['state'] == 'cancelled' for s in record['segments'][1:]))
        self.assertTrue(record['result']['partial'])

    async def test_prepare_failure_never_sends(self):
        adapter = BarrierAdapter(fail_prepare=True); self.service.adapter = adapter
        body, _ = await self.submit(); await self.action(body['performance_id'], 'execute'); await self.drained()
        self.assertEqual((await self.record(body['performance_id']))['state'], 'failed'); self.assertEqual(adapter.calls, [])

    async def test_pause_closes_gate_cancel_suffix_and_close_rejects_inflight(self):
        adapter = BarrierAdapter(block_send=True); self.service.adapter = adapter
        body, _ = await self.submit(); pid = body['performance_id']
        await self.action(pid, 'execute'); await asyncio.wait_for(adapter.sending.wait(), 5)
        base = '/api/performance-session/' + self.sid
        result = await self.post(base + '/pause', {'command_id': str(uuid4())})
        self.assertEqual(result['session']['status'], 'paused'); self.assertEqual(result['stop_status'], 'in_flight')
        await self.post(base + '/close', {'command_id': str(uuid4())}, 409)
        await self.post('/api/performances', signed_payload(self.sid), 409)
        adapter.send_release.set(); await self.drained()
        await self.post(base + '/resume', {'command_id': str(uuid4())})
        await self.action(pid, 'execute'); self.assertEqual(len(adapter.calls), 1)

    async def test_queue_bounded_and_single_sender(self):
        adapter = BarrierAdapter(block_send=True); self.service.adapter = adapter
        first, _ = await self.submit(); await self.action(first['performance_id'], 'execute')
        await asyncio.wait_for(adapter.sending.wait(), 5)
        second, _ = await self.submit(); await self.action(second['performance_id'], 'execute')
        rejected = await self.post('/api/performances', signed_payload(self.sid), 409)
        self.assertEqual(rejected['error']['code'], 'queue_full')
        adapter.send_release.set(); await self.drained(); self.assertEqual(adapter.max_active, 1)
        self.assertEqual((await self.record(second['performance_id']))['state'], 'send_completed')

    async def test_stale_session_invalid_source_and_auth_origin_guards(self):
        await self.post('/api/performance-session', {**self.session_payload, 'command_id': str(uuid4()), 'session_id': str(uuid4()), 'mode': 'real'}, 400)
        await self.post('/api/performances', signed_payload(str(uuid4())), 404)
        await self.post('/api/performance-sources/freeze', {'source': {'kind': 'interview_result', 'id': str(uuid4())}}, 404)
        await self.post('/api/performance-sources/freeze', {'source': {'kind': 'fixed_script', 'id': 'm4-demo-v1'}, 'scores': 9}, 400)
        await self.post('/api/performance-session', {**self.session_payload, 'command_id': str(uuid4()), 'session_id': str(uuid4())}, 409)
        for path in ('/api/performance-session', '/api/performances'):
            response = await self.client.get(path, headers={'Origin': 'https://evil.example'}); self.assertEqual(response.status, 403)
        self.client.session.cookie_jar.clear()
        for path in ('/api/performance-session', '/api/performances'):
            self.assertEqual((await self.client.get(path)).status, 401)

    async def test_expiry_between_segments_preserves_prefix_and_same_id_replay(self):
        adapter = BarrierAdapter(block_send=True); self.service.adapter = adapter
        body, _ = await self.submit(expires_at=iso(datetime.now(timezone.utc) + timedelta(seconds=2)))
        pid = body['performance_id']; await self.action(pid, 'execute')
        await asyncio.wait_for(adapter.sending.wait(), 5)
        # Actual expiry boundary, explicitly NOT elapsed-time media evidence.
        until = datetime.fromisoformat(body['expires_at'].replace('Z', '+00:00'))
        await asyncio.sleep(max(0, (until - datetime.now(timezone.utc)).total_seconds()) + 0.05)
        adapter.send_release.set(); await self.drained()
        record = await self.record(pid)
        self.assertEqual(record['state'], 'expired'); self.assertEqual(len(adapter.calls), 1)
        self.assertEqual(record['segments'][0]['state'], 'send_completed')
        self.assertTrue(all(s['state'] == 'expired' for s in record['segments'][1:]))
        self.assertTrue(record['result']['partial'])
        replay = await self.post('/api/performances', body)
        self.assertEqual(replay['performance'], record)
        await self.post('/api/performances', signed_payload(**{**body, 'expires_at': '2099-01-01T00:00:00.000Z'}), 409)

    async def test_restart_durable_sending_unknown_no_autoresend_or_new_session(self):
        body, _ = await self.submit(); pid = body['performance_id']
        # Seed the exact durable pre-send boundary to emulate abrupt process death,
        # rather than graceful shutdown (which has its own cancellation handler).
        async with self.client.server.app[STATE]['engine'].begin() as conn:
            record = (await conn.execute(select(db.performances.c.body).where(db.performances.c.performance_id == pid))).scalar_one()
            record['state'] = 'sending'; record['segments'][0]['state'] = 'sending'
            await conn.execute(update(db.performances).where(db.performances.c.performance_id == pid).values(state='sending', body=record))
        await self.restart()
        adapter = BarrierAdapter(); self.service.adapter = adapter
        record = await self.record(pid); self.assertEqual(record['state'], 'unknown')
        self.assertEqual(record['segments'][0]['state'], 'unknown')
        self.assertEqual((await self.get('/api/performance-session'))['session']['status'], 'unknown')
        await self.action(pid, 'execute'); await self.drained(); self.assertEqual(adapter.calls, [])
        base = '/api/performance-session/' + self.sid
        await self.post(base + '/resume', {'command_id': str(uuid4())}, 409)
        await self.post(base + '/close', {'command_id': str(uuid4())}, 409)
        await self.post('/api/performance-session', {**self.session_payload, 'command_id': str(uuid4()), 'session_id': str(uuid4())}, 409)
        self.assertEqual((await self.post('/api/performances', body))['performance']['state'], 'unknown')

    async def test_restart_accepted_retained_paused_and_only_manual_resume_can_execute(self):
        body, _ = await self.submit(); pid = body['performance_id']; await self.restart()
        adapter = BarrierAdapter(); self.service.adapter = adapter
        self.assertEqual((await self.record(pid))['state'], 'accepted')
        self.assertEqual((await self.get('/api/performance-session'))['session']['status'], 'paused')
        await self.action(pid, 'execute', 409); self.assertEqual(adapter.calls, [])
        await self.post('/api/performance-session/' + self.sid + '/resume', {'command_id': str(uuid4())})
        await self.action(pid, 'execute'); await self.drained()
        self.assertEqual((await self.record(pid))['state'], 'send_completed')

    async def test_final_receipt_commit_failure_after_side_effect_is_unknown_not_retryable(self):
        from sqlalchemy import event
        from sqlalchemy.exc import SQLAlchemyError
        adapter = BarrierAdapter(block_send=True); self.service.adapter = adapter
        body, _ = await self.submit(); pid = body['performance_id']
        await self.action(pid, 'execute'); await asyncio.wait_for(adapter.sending.wait(), 5)
        engine = self.client.server.app[STATE]['engine']; original = self.service._save
        armed = False; failures = []
        async def save(conn, record, now):
            nonlocal armed
            await original(conn, record, now)
            if not failures and any(s['state'] == 'send_completed' for s in record['segments']): armed = True
        def fail_commit(conn):
            nonlocal armed
            if armed:
                armed = False; failures.append('commit')
                raise SQLAlchemyError('injected receipt commit failure after side effect')
        self.service._save = save
        event.listen(engine.sync_engine, 'commit', fail_commit)
        try:
            adapter.send_release.set(); await self.drained()
        finally:
            event.remove(engine.sync_engine, 'commit', fail_commit); self.service._save = original
        self.assertEqual(failures, ['commit']); self.assertEqual(len(adapter.calls), 1)
        self.assertTrue(self.service.gate)
        record = await self.record(pid); self.assertEqual(record['state'], 'unknown')
        self.assertEqual(record['segments'][0]['state'], 'unknown')
        await self.action(pid, 'execute'); self.assertEqual(len(adapter.calls), 1)

    async def test_cancel_during_preparation_before_send_cancels_all(self):
        adapter = BarrierAdapter(block_prepare=True); self.service.adapter = adapter
        body, _ = await self.submit(); pid = body['performance_id']
        await self.action(pid, 'execute'); await asyncio.wait_for(adapter.preparing.wait(), 5)
        result = await self.action(pid, 'cancel'); self.assertEqual(result['cancel_outcome'], 'unstarted_cancelled')
        adapter.prepare_release.set(); await self.drained()
        self.assertEqual((await self.record(pid))['state'], 'cancelled'); self.assertEqual(adapter.calls, [])
