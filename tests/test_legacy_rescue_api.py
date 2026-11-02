"""Current HTTP rescue operates only on proven historical ownership, never starts media."""
import asyncio
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select
from hell_inc.app import STATE, create_app
from hell_inc.config import Settings
from hell_inc.db import performance_sessions


class LegacyRescueAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.settings=Settings(database_url=os.environ['HELL_INC_DATABASE_URL'],world_id='rescue-'+uuid4().hex,
            origin='http://localhost',lock_dir=Path(self.tmp.name))
        self.client=TestClient(TestServer(create_app(self.settings,startup_token='test-token')),cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server();self.addAsyncCleanup(self.client.close)
        response=await self.client.post('/api/session',json={'token':'test-token'},headers={'Origin':'http://localhost'})
        self.assertEqual(response.status,200)
        self.sid=str(uuid4());now=datetime.now(timezone.utc)
        self.record={'session_id':self.sid,'mode':'real','status':'unknown',
            'media':{'creation_attempted':True,'owned_live_id':'owned-test','cloud_end_confirmed':False,'state':'unknown'}}
        async with self.client.server.app[STATE]['engine'].begin() as conn:
            await conn.execute(performance_sessions.insert().values(session_id=self.sid,world_id=self.settings.world_id,
                mode='real',status='unknown',body=self.record,created_at=now,updated_at=now))

    async def post(self,action):
        return await self.client.post('/api/performance-session/'+self.sid+'/media/'+action,
            json={'command_id':str(uuid4())},headers={'Origin':'http://localhost'})

    async def test_explicit_stop_uses_only_db_owned_id_and_persists_positive_end(self):
        runtime=SimpleNamespace(session_id=None,owned_live_id=None,component=None,latch_stop=Mock(),
            stop=AsyncMock(return_value={'state':'ended','cloud_end_confirmed':True}),shutdown=AsyncMock())
        self.client.server.app[STATE]['legacy_runtime']=runtime
        response=await self.post('stop');self.assertEqual(response.status,202);value=await response.json()
        runtime.stop.assert_awaited_once_with(self.sid,'owned-test');self.assertTrue(value['evidence_persisted'])
        async with self.client.server.app[STATE]['engine'].connect() as conn:
            saved=(await conn.execute(select(performance_sessions.c.body).where(performance_sessions.c.session_id==self.sid))).scalar_one()
        self.assertTrue(saved['media']['cloud_end_confirmed']);self.assertEqual(saved['media']['owned_live_id'],'owned-test')
        self.assertEqual(saved['status'],'paused')

    async def test_existing_in_process_scene_stops_even_when_game_lock_and_db_evidence_fail(self):
        runtime=SimpleNamespace(session_id=self.sid,owned_live_id='owned-test',component=SimpleNamespace(live_id='owned-test'),
            latch_stop=Mock(),stop=AsyncMock(return_value={'state':'ended','cloud_end_confirmed':True}),shutdown=AsyncMock())
        state=self.client.server.app[STATE];state['legacy_runtime']=runtime;state['game_gate']=True
        await state['game_lock'].acquire()
        try:
            with patch('hell_inc.app._record_legacy_rescue',AsyncMock(side_effect=OSError('database unavailable'))):
                response=await asyncio.wait_for(self.post('stop'),timeout=2)
            value=await response.json();self.assertEqual(response.status,202)
            self.assertTrue(value['media']['cloud_end_confirmed']);self.assertFalse(value['evidence_persisted'])
            runtime.latch_stop.assert_called_once_with(self.sid);runtime.stop.assert_awaited_once_with(self.sid,'owned-test')
        finally:state['game_lock'].release()

    async def test_only_stop_reconcile_routes_remain_and_origin_still_required(self):
        for action in ('start','confirm-ready'):
            response=await self.post(action);self.assertEqual(response.status,404)
        response=await self.client.post('/api/performance-session/'+self.sid+'/media/stop',
            json={'command_id':str(uuid4())},headers={'Origin':'http://wrong-origin'})
        self.assertEqual(response.status,403)
