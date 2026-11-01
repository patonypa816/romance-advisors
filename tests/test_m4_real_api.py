"""PostgreSQL durable media lifecycle with inert provider component. Never live cloud."""
import asyncio
import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4
from sqlalchemy import select, update

from hell_inc import db
from hell_inc.app import STATE
from hell_inc.config import Settings
from hell_inc.media_runtime import MediaConfig, RealMediaRuntime
from test_m4_api import M4Fixture
from test_m4_media_runtime import FakeComponent, FakeReadback
from test_m4_contract import signed_payload


class M4RealLifecycleTests(M4Fixture):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.settings=Settings(database_url=os.environ['HELL_INC_DATABASE_URL'],world_id='m4-real-test-'+uuid4().hex,
                               origin='http://localhost',lock_dir=Path(self.tmp.name),real_media_enabled=True,allow_paid_media=True)
        self.config=MediaConfig('opaque-vidu-test','opaque-bailian-test','approved-workspace','123456','test-model','test-voice','批准的测试人设',Path(self.tmp.name)/'artifacts')
        self.components=[];self.create_observations=[];self.fail_create=False;self.http=FakeReadback();self.clock=[100.]
        await self.restart()
        self.sid=str(uuid4());self.session_payload=dict(command_id=str(uuid4()),session_id=self.sid,mode='real',avatar_aspect=None)
        await self.post('/api/performance-session',self.session_payload,201)

    async def restart(self):
        await super().restart()
        def factory(*args,**kwargs):
            component=FakeComponent(*args,**kwargs);component.fail_creation=self.fail_create
            original_start=component.start
            async def start(key,*,avatar_id):
                async with self.client.server.app[STATE]['engine'].connect() as conn:
                    body=(await conn.execute(select(db.performance_sessions.c.body).where(db.performance_sessions.c.session_id==self.sid))).scalar_one()
                self.create_observations.append(body['media'].copy())
                return await original_start(key,avatar_id=avatar_id)
            component.start=start
            async def after_created():
                async with self.client.server.app[STATE]['engine'].connect() as conn:
                    body=(await conn.execute(select(db.performance_sessions.c.body).where(db.performance_sessions.c.session_id==self.sid))).scalar_one()
                self.assertEqual(body['media']['owned_live_id'],'owned-live-test')
            component.after_created=after_created
            self.components.append(component);return component
        self.service.runtime=RealMediaRuntime(self.settings,component_factory=factory,http=self.http,config=self.config,clock=lambda:self.clock[0])

    async def media(self,action,*,command=None,status=None,**extra):
        return await self.post('/api/performance-session/'+self.sid+'/media/'+action,
                               {'command_id':command or str(uuid4()),**extra}, status or (200 if action=='confirm-ready' else 202))
    async def media_drained(self):
        async def wait():
            while self.service.media_tasks: await asyncio.gather(*tuple(self.service.media_tasks))
        await asyncio.wait_for(wait(),5)
    async def session(self): return await self.get('/api/performance-session')
    async def make_ready(self):
        await self.media('start',confirm_paid=True);await self.media_drained()
        rid=str(uuid4());base='/api/performance-session/'+self.sid
        claim=await self.post(base+'/receiver/claim',{'receiver_id':rid})
        heartbeat=dict(receiver_id=rid,receiver_token=claim['receiver_token'],joined=True,remote_video=True,audio_playback=True,autoplay_blocked=False)
        await self.post(base+'/receiver/heartbeat',heartbeat)
        await self.media('confirm-ready',receiver_id=rid,confirm_audio_video=True)
        await self.post(base+'/resume',{'command_id':str(uuid4())})
        return claim,heartbeat

    async def test_local_real_session_start_durable_before_post_id_before_connect_dedupe(self):
        snap=await self.session();self.assertEqual(snap['session']['status'],'paused')
        self.assertFalse(snap['media']['creation_attempted']);self.assertEqual(self.components,[]);self.assertEqual(self.http.calls,[])
        command=str(uuid4())
        initial=await self.media('start',command=command,confirm_paid=True)
        await self.media_drained()
        self.assertEqual(self.create_observations[0]['state'],'creating');self.assertIs(self.create_observations[0]['creation_attempted'],True)
        self.assertEqual(len(self.components),1);self.assertIn(('connect_after_durable_callback',),self.components[0].calls)
        repeat=await self.media('start',command=command,confirm_paid=True)
        self.assertEqual(repeat['session'],initial['session'])
        await self.media('start',command=command,confirm_paid=1,status=409)
        await self.media('start',confirm_paid=1,status=400)
        self.assertIs((await self.session())['session']['media_verified'],False)
        await self.media('start',confirm_paid=True,status=409)
        await self.media('stop',command=command,status=409)
        await self.media_drained();self.assertEqual(sum(c[0]=='create' for c in self.components[0].calls),1)

    async def test_ambiguous_create_never_retries_after_lost_reply_or_new_command(self):
        self.fail_create=True;command=str(uuid4())
        await self.media('start',command=command,confirm_paid=True);await self.media_drained()
        snap=await self.session();self.assertEqual(snap['media']['state'],'unknown');self.assertIsNone(snap['media']['owned_live_id'])
        await self.media('start',command=command,confirm_paid=True);await self.media('start',confirm_paid=True,status=409)
        await self.media('reconcile');await self.media_drained()
        self.assertEqual((await self.session())['media']['reconciliation_required'],'provider_console')
        self.assertEqual(self.http.calls,[]);self.assertEqual(len(self.components),1)
        await self.post('/api/performance-session/'+self.sid+'/close',{'command_id':str(uuid4())},409)

    async def test_restart_creating_unknown_no_auto_post_and_new_start_blocked(self):
        async with self.client.server.app[STATE]['engine'].begin() as conn:
            body=(await conn.execute(select(db.performance_sessions.c.body).where(db.performance_sessions.c.session_id==self.sid))).scalar_one()
            body['media'].update(state='creating',creation_attempted=True)
            await conn.execute(update(db.performance_sessions).where(db.performance_sessions.c.session_id==self.sid).values(body=body))
        await self.restart()
        snap=await self.session();self.assertEqual(snap['media']['state'],'unknown');self.assertEqual(self.components,[])
        await self.media('start',confirm_paid=True,status=409)
        await self.media('reconcile');await self.media_drained();self.assertEqual(self.http.calls,[])

    async def test_receiver_token_never_persisted_single_claim_typed_heartbeat_and_epoch_gate(self):
        claim,heartbeat=await self.make_ready();base='/api/performance-session/'+self.sid
        self.assertEqual((await self.session())['session']['status'],'active')
        await self.post(base+'/receiver/claim',{'receiver_id':str(uuid4())},409)
        await self.post(base+'/receiver/heartbeat',{**heartbeat,'joined':1},400)
        await self.post(base+'/receiver/heartbeat',{**heartbeat,'receiver_token':'invalid'},409)
        async with self.client.server.app[STATE]['engine'].connect() as conn:
            rows=[]
            for table in (db.performance_sessions,db.performance_commands):
                rows.extend([dict(r) for r in (await conn.execute(select(table).where(table.c.world_id==self.settings.world_id))).mappings()])
        serialized=json.dumps(rows,default=str);snapshot=json.dumps(await self.session())
        for token in (claim['receiver_token'],claim['rtc']['token'],'opaque-vidu-test','opaque-bailian-test'):
            self.assertNotIn(token,serialized);self.assertNotIn(token,snapshot)
        await self.post(base+'/receiver/heartbeat',{**heartbeat,'autoplay_blocked':True,'audio_playback':False})
        snap=await self.session();self.assertEqual(snap['session']['status'],'paused');self.assertFalse(snap['media']['operator_confirmed'])
        await self.post(base+'/resume',{'command_id':str(uuid4())},409)
        self.assertEqual(snap['media']['receiver']['evidence_source'],'client_reported')

    async def test_receiver_ttl_pauses_future_work_no_lease_transfer(self):
        claim,heartbeat=await self.make_ready()
        body,_=await self.submit();self.clock[0]+=self.service.runtime.RECEIVER_TTL+1
        await self.service.media_tick()
        self.assertEqual((await self.session())['session']['status'],'paused')
        self.assertEqual((await self.record(body['performance_id']))['state'],'cancelled')
        await self.post('/api/performance-session/'+self.sid+'/receiver/claim',{'receiver_id':str(uuid4())},409)

    async def test_pause_not_cloud_stop_unconfirmed_stop_blocks_close_until_get_reconcile(self):
        await self.make_ready();base='/api/performance-session/'+self.sid
        await self.post(base+'/pause',{'command_id':str(uuid4())})
        self.assertNotIn(('stop',),self.components[0].calls)
        self.assertFalse((await self.session())['media']['cloud_end_confirmed'])
        self.components[0].end_confirmed=False
        await self.media('stop');await self.media_drained()
        self.assertEqual((await self.session())['media']['state'],'unknown')
        await self.post(base+'/close',{'command_id':str(uuid4())},409)
        await self.media('start',confirm_paid=True,status=409)
        await self.media('reconcile');await self.media_drained()
        self.assertTrue((await self.session())['media']['cloud_end_confirmed'])
        self.assertEqual([c[0] for c in self.http.calls],['GET'])
        await self.post(base+'/close',{'command_id':str(uuid4())})

    async def test_real_preparation_restart_unknown_never_new_charge(self):
        await self.make_ready();body,_=await self.submit();pid=body['performance_id']
        async with self.client.server.app[STATE]['engine'].begin() as conn:
            row=(await conn.execute(select(db.performances.c.body).where(db.performances.c.performance_id==pid))).scalar_one()
            row['state']='preparing';row['segments'][0]['state']='preparing'
            row['segments'][0]['preparation']={'attempted':True,'phase':'tts','request_id':'retained-request'}
            await conn.execute(update(db.performances).where(db.performances.c.performance_id==pid).values(state='preparing',body=row))
        await self.restart()
        record=await self.record(pid);self.assertEqual(record['state'],'unknown')
        self.assertEqual(record['segments'][0]['preparation']['request_id'],'retained-request')
        count=len(self.components);await self.action(pid,'execute');await self.drained();self.assertEqual(len(self.components),count)
        self.assertEqual((await self.post('/api/performances',body))['performance']['state'],'unknown')

    async def test_old_close_receipt_replay_cannot_release_new_owned_runtime(self):
        await self.make_ready();old_sid=self.sid
        await self.media('stop');await self.media_drained()
        close_command={'command_id':str(uuid4())}
        original=await self.post('/api/performance-session/'+old_sid+'/close',close_command)
        self.sid=str(uuid4())
        await self.post('/api/performance-session',dict(command_id=str(uuid4()),session_id=self.sid,mode='real',avatar_aspect=None),201)
        await self.media('start',confirm_paid=True);await self.media_drained()
        runtime=self.service.runtime;component=runtime.component;owned=runtime.owned_live_id;epoch=runtime.epoch
        replay=await self.post('/api/performance-session/'+old_sid+'/close',close_command)
        self.assertEqual(replay['session'],original['session'])
        self.assertIs(runtime.component,component);self.assertEqual(runtime.session_id,self.sid)
        self.assertEqual(runtime.owned_live_id,owned);self.assertEqual(runtime.epoch,epoch)

    async def test_positive_end_and_closed_session_ignore_delayed_old_monitor_updates(self):
        await self.make_ready();await self.media('stop');await self.media_drained()
        self.assertTrue((await self.session())['media']['cloud_end_confirmed'])
        await self.service._update_media(self.sid,{'state':'unknown','cloud_end_confirmed':False})
        snap=await self.session();self.assertEqual(snap['media']['state'],'ended');self.assertTrue(snap['media']['cloud_end_confirmed'])
        await self.post('/api/performance-session/'+self.sid+'/close',{'command_id':str(uuid4())})
        async with self.client.server.app[STATE]['engine'].connect() as conn:
            before=(await conn.execute(select(db.performance_sessions.c.body).where(db.performance_sessions.c.session_id==self.sid))).scalar_one()
        await self.service._update_media(self.sid,{'state':'unknown','cloud_end_confirmed':False})
        async with self.client.server.app[STATE]['engine'].connect() as conn:
            after=(await conn.execute(select(db.performance_sessions.c.body).where(db.performance_sessions.c.session_id==self.sid))).scalar_one()
        self.assertEqual(after,before);self.assertEqual(after['status'],'closed')

    async def test_get_snapshot_expiry_then_late_heartbeat_durably_pauses_session(self):
        claim,heartbeat=await self.make_ready();self.clock[0]+=self.service.runtime.RECEIVER_TTL+0.01
        observed=await self.session();self.assertFalse(observed['media']['receiver']['healthy'])
        await self.post('/api/performance-session/'+self.sid+'/receiver/heartbeat',heartbeat)
        await self.service.media_tick()
        snap=await self.session();self.assertEqual(snap['session']['status'],'paused');self.assertFalse(snap['media']['operator_confirmed'])
        await self.post('/api/performance-session/'+self.sid+'/resume',{'command_id':str(uuid4())},409)
        await self.media('confirm-ready',receiver_id=claim['receiver_id'],confirm_audio_video=True)
        await self.post('/api/performance-session/'+self.sid+'/resume',{'command_id':str(uuid4())})
        self.assertEqual((await self.session())['session']['status'],'active')
