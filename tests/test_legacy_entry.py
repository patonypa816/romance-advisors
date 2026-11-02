"""Current game entry keeps source freeze and explicit stop-only historical rescue."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from aiohttp.test_utils import make_mocked_request
from hell_inc.app import STATE, create_app, dispatch, legacy_media_rescue
from hell_inc.config import Settings


class EntryTests(IsolatedAsyncioTestCase):
    async def test_default_app_has_no_old_runtime_or_media_start_send_receiver_routes(self):
        settings=Settings(database_url='postgresql+asyncpg://test:test@localhost/test',frontend_dir=Path('/nonexistent'))
        with patch('hell_inc.app.RealMediaRuntime') as runtime:
            app=create_app(settings,startup_token='test')
        runtime.assert_not_called()
        self.assertIsNone(app[STATE]['legacy_runtime'])
        self.assertNotIn('performances',app[STATE])
        resources={r.canonical for r in app.router.resources()}
        self.assertIn('/api/performance-sources/freeze',resources)
        self.assertIn('/api/performance-session/{id}/media/{action}',resources)
        for path in ('/api/performance-session','/api/performances',
                     '/api/performance-session/{id}/receiver/claim',
                     '/api/performance-session/{id}/receiver/heartbeat',
                     '/vendor/aliyun-rtc-sdk-7.1.9.js'):
            self.assertNotIn(path,resources)
        for action,status in (('start',404),('confirm-ready',404),('stop',200),('reconcile',200)):
            request=make_mocked_request('POST',f'/api/performance-session/{uuid4()}/media/{action}',app=app)
            match=await app.router.resolve(request)
            self.assertEqual(match.http_exception.status if match.http_exception else 200,status)
        await app[STATE]['engine'].dispose()

    async def test_rescue_bypasses_game_db_recovery_lock(self):
        lock=asyncio.Lock(); await lock.acquire()
        try:
            state={'game_lock':lock,'game_gate':True}
            request=SimpleNamespace(path='/api/performance-session/x/media/stop',
                match_info=SimpleNamespace(route=SimpleNamespace(name='legacy_media_rescue')),
                app={STATE:state})
            handler=AsyncMock(return_value='stop-result')
            self.assertEqual(await asyncio.wait_for(dispatch(request,handler),.2),'stop-result')
            handler.assert_awaited_once()
        finally:lock.release()

    def fake_request(self,sid,action,state):
        return SimpleNamespace(app={STATE:state},match_info={'id':sid,'action':action})

    async def test_in_process_owned_scene_stops_before_database_and_returns_end_on_db_failure(self):
        sid=str(uuid4()); order=[]
        class Runtime:
            session_id=sid; owned_live_id='owned-1'; component=SimpleNamespace(live_id='owned-1')
            def latch_stop(self,ident):order.append(('latch',ident))
            async def stop(self,ident,owned):order.append(('stop',ident,owned));return {'state':'ended','cloud_end_confirmed':True}
        runtime=Runtime(); state={'legacy_runtime':runtime}
        async def failed_record(*args):order.append(('record',));raise OSError('db offline')
        with patch('hell_inc.app.body',AsyncMock(return_value={'command_id':str(uuid4())})), \
             patch('hell_inc.app._stored_legacy_session',AsyncMock(side_effect=AssertionError('must not read DB before stop'))), \
             patch('hell_inc.app._record_legacy_rescue',side_effect=failed_record):
            response=await legacy_media_rescue(self.fake_request(sid,'stop',state))
        import json
        value=json.loads(response.text)
        self.assertEqual(order,[('latch',sid),('stop',sid,'owned-1'),('record',)])
        self.assertEqual(value['media'],{'state':'ended','cloud_end_confirmed':True})
        self.assertFalse(value['evidence_persisted'])

    async def test_unknown_db_owner_does_not_adopt_or_stop_any_scene(self):
        sid=str(uuid4());runtime=SimpleNamespace(session_id=str(uuid4()),owned_live_id='other',
            component=SimpleNamespace(live_id='other'),stop=AsyncMock(),latch_stop=Mock())
        state={'legacy_runtime':runtime}
        with patch('hell_inc.app.body',AsyncMock(return_value={'command_id':str(uuid4())})), \
             patch('hell_inc.app._stored_legacy_session',AsyncMock(side_effect=OSError('db offline'))):
            response=await legacy_media_rescue(self.fake_request(sid,'stop',state))
        import json
        self.assertEqual(json.loads(response.text)['media']['state'],'unknown')
        runtime.stop.assert_not_awaited();runtime.latch_stop.assert_not_called()

    async def test_persisted_ended_scene_skips_remote_recheck(self):
        sid=str(uuid4());state={'legacy_runtime':None}
        record={'mode':'real','media':{'owned_live_id':'owned-1','cloud_end_confirmed':True}}
        with patch('hell_inc.app.body',AsyncMock(return_value={'command_id':str(uuid4())})), \
             patch('hell_inc.app._stored_legacy_session',AsyncMock(return_value=record)), \
             patch('hell_inc.app.RealMediaRuntime') as runtime:
            response=await legacy_media_rescue(self.fake_request(sid,'reconcile',state))
        import json
        self.assertTrue(json.loads(response.text)['media']['cloud_end_confirmed'])
        runtime.assert_not_called()

    async def test_persisted_owned_id_is_only_target_of_explicit_rescue(self):
        sid=str(uuid4()); runtime=SimpleNamespace(session_id=None,owned_live_id=None,component=None,
            latch_stop=Mock(),stop=AsyncMock(return_value={'state':'ended','cloud_end_confirmed':True}))
        state={'legacy_runtime':runtime}; record={'mode':'real','media':{'owned_live_id':'owned-1'}}
        with patch('hell_inc.app.body',AsyncMock(return_value={'command_id':str(uuid4())})), \
             patch('hell_inc.app._stored_legacy_session',AsyncMock(return_value=record)), \
             patch('hell_inc.app._record_legacy_rescue',AsyncMock(return_value=True)):
            response=await legacy_media_rescue(self.fake_request(sid,'stop',state))
        self.assertEqual(response.status,202)
        runtime.latch_stop.assert_called_once_with(sid)
        runtime.stop.assert_awaited_once_with(sid,'owned-1')
