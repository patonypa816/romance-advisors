"""Owned runtime/receiver tests with inert component and HTTP doubles, never cloud calls."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from hell_inc.media_runtime import MediaConfig, RealMediaRuntime
from hell_inc.performance_contract import PerformanceError


class FakeComponent:
    def __init__(self, http, out, image, *, on_created, recording=False):
        self.on_created=on_created; self.state='idle'; self.live_id=None; self._task=None
        self.calls=[]; self.end_confirmed=True; self.cloud_end_confirmed=False; self.stop_latched=False
        self.after_created=None; self.fail_creation=False
    async def start(self, key, *, avatar_id):
        self.calls.append(('create', avatar_id)); self.state='creating'
        if self.fail_creation: raise RuntimeError('ambiguous create')
        self.live_id='owned-live-test'
        await self.on_created(self.live_id, {'safe': True})
        if self.after_created: await self.after_created()
        if self.stop_latched:return await self.stop()
        self.calls.append(('connect_after_durable_callback',)); self.state='waiting_stage'
        return self.public()
    def public(self): return {'state':self.state,'ready':self.state=='ready' and not self.stop_latched,'cloud_end_confirmed':self.cloud_end_confirmed}
    def rtc(self): return {'token':'ephemeral-rtc-test-only','user_id':'receiver-test','provider':'artc'}
    def joined(self): self.state='ready'
    def latch_stop(self): self.stop_latched=True
    async def stop(self):
        self.calls.append(('stop',)); self.cloud_end_confirmed=self.end_confirmed
        self.state='ended' if self.end_confirmed else 'stop_unconfirmed'
        return self.public()


class FakeReadback:
    def __init__(self, ended=True): self.ended=ended; self.calls=[]; self.status=200
    def get(self,url,**kwargs): self.calls.append(('GET',url,kwargs)); return self
    async def __aenter__(self): return self
    async def __aexit__(self,*args): return False
    async def json(self): return {'live':{'status':'ended' if self.ended else 'running'}}
    async def close(self): pass


class MediaRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.config=MediaConfig('opaque-vidu-test','opaque-bailian-test','approved-workspace','123456','configured-model','configured-voice','批准的测试人设',Path(self.tmp.name))
        self.settings=SimpleNamespace(real_media_enabled=True,allow_paid_media=True)
        self.clock=[100.0]; self.http=FakeReadback(); self.components=[]; self.events=[]
        def factory(*args,**kwargs):
            c=FakeComponent(*args,**kwargs); self.components.append(c); return c
        self.runtime=RealMediaRuntime(self.settings,component_factory=factory,http=self.http,config=self.config,clock=lambda:self.clock[0])
        self.addAsyncCleanup(self.runtime.shutdown)
        self.sid=str(uuid4()); self.rid=str(uuid4())
    async def start(self):
        async def created(live_id,meta): self.events.append(('durable',live_id,meta))
        async def persist(meta): self.events.append(('persist',meta))
        await self.runtime.start(self.sid,created,persist)
    def heartbeat(self,claim,**changes):
        data=dict(receiver_id=self.rid,receiver_token=claim['receiver_token'],joined=True,remote_video=True,audio_playback=True,autoplay_blocked=False)
        data.update(changes); return self.runtime.heartbeat(self.sid,data)
    async def ready(self):
        await self.start(); claim=self.runtime.claim(self.sid,self.rid)
        self.heartbeat(claim); self.runtime.confirm(self.sid,self.rid); self.runtime.resume(self.sid)
        return claim

    async def test_runtime_construction_zero_provider_setup_and_disabled_gate(self):
        with patch('aiohttp.ClientSession',side_effect=AssertionError('unexpected network construction')):
            disabled=RealMediaRuntime(SimpleNamespace(real_media_enabled=False,allow_paid_media=False))
            self.assertIsNone(disabled.component); self.assertIsNone(disabled.config); self.assertIsNone(disabled.http)
            with self.assertRaises(PerformanceError): disabled.ensure_configured()
            with self.assertRaises(PerformanceError): disabled.assert_ready(self.sid)
            await disabled.shutdown()
        self.assertEqual(self.http.calls,[])

    async def test_owned_id_committed_before_connect_and_no_duplicate_start(self):
        await self.start()
        self.assertEqual(self.events[0][0],'durable')
        self.assertEqual(self.runtime.owned_live_id,'owned-live-test')
        self.assertEqual(self.components[0].calls,[('create','123456'),('connect_after_durable_callback',)])
        with self.assertRaises(PerformanceError): await self.start()
        self.assertEqual(len(self.components),1)
        self.assertEqual(Path(self.tmp.name,self.sid).stat().st_mode & 0o777,0o700)

    async def test_receiver_unique_claim_heartbeat_manual_confirmation_and_resume_all_required(self):
        await self.start()
        with self.assertRaises(PerformanceError): self.runtime.resume(self.sid)
        with self.assertRaises(PerformanceError): self.runtime.confirm(self.sid,self.rid)
        claim=self.runtime.claim(self.sid,self.rid)
        with self.assertRaises(PerformanceError): self.runtime.claim(self.sid,str(uuid4()))
        with self.assertRaises(PerformanceError): self.runtime.confirm(self.sid,self.rid)
        self.heartbeat(claim)
        self.assertFalse(self.runtime.ready(self.sid))
        self.runtime.confirm(self.sid,self.rid); self.assertFalse(self.runtime.ready(self.sid))
        self.runtime.resume(self.sid); self.assertTrue(self.runtime.ready(self.sid))
        self.assertEqual(self.runtime.view()['receiver']['evidence_source'],'client_reported')

    async def test_heartbeat_loss_autoplay_block_and_expiry_close_gate_without_transfer(self):
        claim=await self.ready(); self.heartbeat(claim,autoplay_blocked=True,audio_playback=False)
        self.assertFalse(self.runtime.ready(self.sid)); self.assertFalse(self.runtime.operator_confirmed)
        with self.assertRaises(PerformanceError): self.runtime.resume(self.sid)
        self.heartbeat(claim); self.runtime.confirm(self.sid,self.rid); self.runtime.resume(self.sid)
        self.clock[0]+=self.runtime.RECEIVER_TTL+1
        self.assertFalse(self.runtime.ready(self.sid)); self.assertFalse(self.runtime.view()['receiver']['healthy'])
        with self.assertRaises(PerformanceError): self.runtime.claim(self.sid,str(uuid4()))

    async def test_receiver_token_epoch_session_isolation_and_no_snapshot_secret(self):
        claim=await self.ready()
        with self.assertRaises(PerformanceError): self.heartbeat(claim,receiver_token='wrong')
        with self.assertRaises(PerformanceError): self.runtime.validate({'session_id':self.sid,'epoch':'old-epoch'})
        with self.assertRaises(PerformanceError): self.runtime.validate({'session_id':str(uuid4()),'epoch':self.runtime.epoch})
        view=json.dumps(self.runtime.view())
        for secret in (claim['receiver_token'],claim['rtc']['token'],'opaque-vidu-test','opaque-bailian-test'):
            self.assertNotIn(secret,view)
        self.assertNotIn('opaque-vidu-test',repr(self.config))
        refreshed=self.runtime.claim(self.sid,self.rid)
        self.assertNotEqual(refreshed['receiver_token'],claim['receiver_token'])
        with self.assertRaises(PerformanceError): self.heartbeat(claim)
        self.assertFalse(self.runtime.operator_confirmed)

    async def test_stop_unconfirmed_is_unknown_and_reconcile_only_get_owned_id(self):
        await self.start(); self.components[0].end_confirmed=False
        result=await self.runtime.stop(self.sid,self.runtime.owned_live_id)
        self.assertEqual(result,{'state':'unknown','cloud_end_confirmed':False})
        self.assertFalse(self.runtime.ready(self.sid))
        result=await self.runtime.reconcile(self.sid,self.runtime.owned_live_id)
        self.assertEqual(result,{'state':'ended','cloud_end_confirmed':True})
        self.assertEqual([c[0] for c in self.http.calls],['GET'])
        self.assertTrue(self.http.calls[0][1].endswith('/owned-live-test'))
        self.assertEqual(self.http.calls[0][2]['headers'],{'Authorization':'Token opaque-vidu-test'})
        with self.assertRaises(PerformanceError): await self.start()

    async def test_ambiguous_creation_without_id_requires_manual_console_no_read_or_create(self):
        result=await self.runtime.reconcile(self.sid,None)
        self.assertEqual(result['state'],'unknown'); self.assertEqual(result['reconciliation_required'],'provider_console')
        self.assertEqual(self.http.calls,[]); self.assertEqual(self.components,[])

    async def test_old_receiver_cannot_reuse_after_ended_release_epoch_changes(self):
        claim=await self.ready(); epoch=self.runtime.epoch
        await self.runtime.stop(self.sid,self.runtime.owned_live_id); await self.runtime.release_ended()
        self.assertNotEqual(self.runtime.epoch,epoch); self.assertIsNone(self.runtime.receiver_token)
        with self.assertRaises(PerformanceError): self.heartbeat(claim)
        self.assertFalse(self.runtime.ready(self.sid))

    async def test_late_healthy_heartbeat_does_not_silently_reopen_expired_gate_without_tick(self):
        claim=await self.ready()
        self.clock[0]+=self.runtime.RECEIVER_TTL+0.01
        # No media_tick / ready() query before this late packet: heartbeat itself
        # must notice the expired lease and invalidate old manual confirmation.
        self.heartbeat(claim)
        self.assertTrue(self.runtime.paused);self.assertFalse(self.runtime.operator_confirmed)
        self.assertFalse(self.runtime.ready(self.sid))
        with self.assertRaises(PerformanceError):self.runtime.resume(self.sid)
        self.runtime.confirm(self.sid,self.rid);self.runtime.resume(self.sid)
        self.assertTrue(self.runtime.ready(self.sid))

    async def test_production_start_requires_pinned_local_sdk_and_explicit_rtc_origins_before_provider(self):
        settings=SimpleNamespace(real_media_enabled=True,allow_paid_media=True,frontend_dir=Path(self.tmp.name)/'frontend',rtc_connect_origins=())
        runtime=RealMediaRuntime(settings,config=self.config,http=self.http)
        with self.assertRaises(PerformanceError): runtime.ensure_configured()
        settings.rtc_connect_origins=('https://approved-rtc.example',)
        sdk=settings.frontend_dir/'vendor'/'aliyun-rtc-sdk-7.1.9.js';sdk.parent.mkdir(parents=True);sdk.write_text('wrong-sdk-not-executable')
        with self.assertRaises(PerformanceError): runtime.ensure_configured()
        self.assertIsNone(runtime.component);self.assertEqual(self.http.calls,[])
        await runtime.shutdown()

    async def test_explicit_env_override_never_falls_back_to_other_key_or_old_avatar(self):
        import os
        env={'VIDU_API_KEY':'legacy-opaque-test','DASHSCOPE_API_KEY':'legacy-bailian-test','BAILIAN_WORKSPACE_ID':'workspace',
             'HELL_INC_AVATAR_ID':'123456','HELL_INC_PLANNER_MODEL':'model','HELL_INC_TTS_VOICE':'voice','HELL_INC_VOICE_PERSONA':'approved'}
        with patch.dict(os.environ,env,clear=True):
            config=MediaConfig.from_env();self.assertEqual(config.api_key,'legacy-opaque-test');self.assertEqual(config.avatar_id,'123456')
            os.environ['HELL_INC_VIDU_KEY']=''
            with self.assertRaises(PerformanceError):MediaConfig.from_env()
            os.environ['HELL_INC_VIDU_KEY']='explicit-opaque-test'
            self.assertEqual(MediaConfig.from_env().api_key,'explicit-opaque-test')
            del os.environ['HELL_INC_AVATAR_ID']
            with self.assertRaises(PerformanceError):MediaConfig.from_env()
        with self.assertRaises(ValueError):
            MediaConfig('opaque','opaque','workspace','123','model','voice','persona',Path('relative-path'))

    async def test_cancel_waiting_for_sender_worker_before_record_load_is_clean_cancellation(self):
        from hell_inc.performances import PerformanceService
        service=PerformanceService(None,'pure-worker-test',runtime=self.runtime)
        await service.worker_lock.acquire()
        pid=str(uuid4());service.running.add(pid)
        task=asyncio.create_task(service._run(pid))
        await asyncio.sleep(0)  # Yield to lock acquisition; not media-time evidence.
        task.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):await task
        finally:service.worker_lock.release()
        self.assertNotIn(pid,service.running)

    async def test_old_reconcile_failure_cannot_pause_new_runtime_owner(self):
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock
        from hell_inc.performances import PerformanceService
        service=PerformanceService(None,'pure-old-task-test',runtime=self.runtime)
        old_sid,new_sid=str(uuid4()),str(uuid4())
        old={'session_id':old_sid,'mode':'real','status':'paused','media':{'creation_attempted':True,'owned_live_id':'old-owned'}}
        service._session=AsyncMock(return_value=old)
        service._pause_rows=AsyncMock();service._save_session=AsyncMock()
        service._revision=AsyncMock(return_value=2);service._update_media=AsyncMock()
        async def command(_id,_payload,operation):
            return await operation(None,1,datetime.now(timezone.utc)),False
        service._command=command
        started,released=asyncio.Event(),asyncio.Event()
        async def delayed_reconcile(session_id,owned_id):
            self.assertEqual((session_id,owned_id),(old_sid,'old-owned'))
            started.set();await released.wait();raise RuntimeError('old readback failed')
        self.runtime.reconcile=delayed_reconcile;self.runtime.session_id=old_sid
        await service.media_command(old_sid,'reconcile',{'command_id':str(uuid4())})
        await asyncio.wait_for(started.wait(),2)
        # A has since closed and B is the current owner; do not stop B when
        # the stale, read-only A request eventually reports a failure.
        self.runtime.session_id=new_sid;self.runtime.owned_live_id='new-owned';self.runtime.paused=False
        released.set();await asyncio.gather(*tuple(service.media_tasks))
        self.assertFalse(self.runtime.paused)
        self.assertEqual(self.runtime.session_id,new_sid);self.assertEqual(self.runtime.owned_live_id,'new-owned')
        service._update_media.assert_awaited_once_with(old_sid,{'state':'unknown','cloud_end_confirmed':False})


class StopOnlyHTTP:
    """Original component I/O shape; local barriers and frames, zero sockets."""
    def __init__(self,*,allow_create=False,confirm_end=True):
        self.allow_create=allow_create;self.confirm_end=confirm_end;self.ended=False
        self.calls=[];self.frames=[];self.audio=[];self.started=asyncio.Event();self.release=asyncio.Event()
        if not allow_create:self.release.set()
    def request(self,method,url,**kwargs):
        self.calls.append((method,url))
        if method=='POST':
            if not self.allow_create:raise AssertionError('stop-only must never POST')
            owner=self
            class Created:
                status=200
                async def __aenter__(inner):owner.started.set();await owner.release.wait();return inner
                async def __aexit__(inner,*args):pass
                async def json(inner,**kwargs):return {'live':{'id':'durable-owned-test'},'managed_rtc':{'token':'private-rtc','user_id':'test'}}
            return Created()
        owner=self
        class Readback:
            status=200
            async def __aenter__(inner):return inner
            async def __aexit__(inner,*args):pass
            async def json(inner,**kwargs):return {'live':{'id':'durable-owned-test','status':'ended' if owner.ended else 'running'}}
        return Readback()
    def get(self,url,**kwargs):return self.request('GET',url,**kwargs)
    async def ws_connect(self,url,**kwargs):
        self.calls.append(('WSS',url));owner=self
        if hasattr(self,'events'):self.events.append(('wss',url))
        class Socket:
            closed=False
            def __aiter__(inner):return inner
            async def __anext__(inner):await asyncio.Future()
            async def send_json(inner,frame):
                owner.frames.append(frame)
                if frame['type']==5 and owner.confirm_end:owner.ended=True
            async def send_bytes(inner,data):owner.audio.append(data);raise AssertionError('cleanup cannot send audio')
            async def close(inner):inner.closed=True
        return Socket()
    async def close(self):pass


class StopLatchRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_before_runtime_start_is_sticky_and_never_constructs_provider(self):
        runtime=RealMediaRuntime(SimpleNamespace(real_media_enabled=True,allow_paid_media=True))
        sid=str(uuid4());runtime.latch_stop(sid)
        with self.assertRaisesRegex(PerformanceError,'media_stop_latched'):
            await runtime.start(sid,None,None)
        self.assertIsNone(runtime.component);self.assertIsNone(runtime.http)

    async def test_inflight_create_retains_id_then_original_finish_without_receiver_or_audio(self):
        from hell_inc.avatar_transport.motion_component import MotionComponent
        with tempfile.TemporaryDirectory() as tmp:
            http=StopOnlyHTTP(allow_create=True);events=[];http.events=events
            config=MediaConfig('opaque-test','tts-test','workspace','123','model','voice','persona',Path(tmp))
            runtime=RealMediaRuntime(SimpleNamespace(real_media_enabled=True,allow_paid_media=True),component_factory=MotionComponent,http=http,config=config)
            sid=str(uuid4())
            async def created(live_id,meta):events.append(('durable',live_id));return True
            async def persist(meta):events.append(('state',meta['state']))
            await runtime.start(sid,created,persist)
            await asyncio.wait_for(http.started.wait(),2)
            runtime.latch_stop(sid)
            stopping=asyncio.create_task(runtime.stop(sid,None))
            with self.assertRaises(PerformanceError):runtime.claim(sid,str(uuid4()))
            http.release.set()
            result=await asyncio.wait_for(stopping,3)
            await asyncio.wait_for(runtime.component._task,3)
            self.assertIn(('durable','durable-owned-test'),events)
            self.assertLess(events.index(('durable','durable-owned-test')),next(i for i,e in enumerate(events) if e[0]=='wss'))
            self.assertEqual(runtime.owned_live_id,'durable-owned-test')
            self.assertEqual([f['type'] for f in http.frames],[1,5])
            self.assertEqual(sum(c[0]=='POST' for c in http.calls),1)
            self.assertEqual(http.audio,[]);self.assertFalse(runtime.ready(sid));self.assertEqual(runtime.component.rtc(),{})
            self.assertTrue(result['cloud_end_confirmed'])
            await runtime.shutdown()

    async def test_restart_owned_stop_uses_original_hangup_no_post_tts_sdk_or_paid_gate(self):
        import os
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'VIDU_API_KEY':'opaque-test','HELL_INC_MEDIA_DIR':tmp},clear=True):
            http=StopOnlyHTTP();runtime=RealMediaRuntime(SimpleNamespace(real_media_enabled=False,allow_paid_media=False),http=http)
            sid=str(uuid4());result=await runtime.stop(sid,'durable-owned-test')
            self.assertTrue(result['cloud_end_confirmed'])
            self.assertEqual([f['type'] for f in http.frames],[1,5]);self.assertEqual(http.audio,[])
            self.assertTrue(all(method in ('GET','WSS') for method,_ in http.calls))
            self.assertTrue(all('durable-owned-test' in url for _,url in http.calls))
            self.assertIsNone(runtime.component);self.assertIsNone(runtime.session_id)
            with self.assertRaises(PerformanceError):runtime.claim(sid,str(uuid4()))
            await runtime.shutdown()

    async def test_stop_without_positive_end_stays_unknown_and_old_cleanup_does_not_pause_new_owner(self):
        from hell_inc.avatar_transport.component_live import PersistentComponent
        with tempfile.TemporaryDirectory() as tmp:
            config=MediaConfig('opaque-test','tts-test','workspace','123','model','voice','persona',Path(tmp))
            http=StopOnlyHTTP(confirm_end=False);runtime=RealMediaRuntime(SimpleNamespace(real_media_enabled=False,allow_paid_media=False),http=http,config=config)
            old,new=str(uuid4()),str(uuid4());runtime.session_id=new;runtime.paused=False;runtime.operator_confirmed=True
            with patch.object(PersistentComponent,'END_TIMEOUT',.01),patch.object(PersistentComponent,'POLL_INTERVAL',.001):
                result=await runtime.stop(old,'durable-owned-test')
            self.assertEqual(result,{'state':'unknown','cloud_end_confirmed':False})
            self.assertFalse(runtime.paused);self.assertTrue(runtime.operator_confirmed)
            self.assertEqual(runtime.session_id,new);self.assertIn(old,runtime.stopped_sessions)
            with self.assertRaisesRegex(PerformanceError,'media_stop_latched'):await runtime.start(old,None,None)
            before=list(http.calls);unknown=await runtime.stop(str(uuid4()),None)
            self.assertEqual(http.calls,before);self.assertEqual(unknown['reconciliation_required'],'provider_console')
            await runtime.shutdown()


    async def test_restart_slow_get_precheck_cannot_delay_active_hangup_indefinitely(self):
        with tempfile.TemporaryDirectory() as tmp:
            http=StopOnlyHTTP()
            class HungReadback:
                async def __aenter__(self):await asyncio.Future()
                async def __aexit__(self,*args):pass
            http.get=lambda *args,**kwargs:HungReadback()
            config=MediaConfig('opaque-test','tts-test','workspace','123','model','voice','persona',Path(tmp))
            runtime=RealMediaRuntime(SimpleNamespace(real_media_enabled=False,allow_paid_media=False),http=http,config=config)
            with patch.object(runtime,'STOP_PREFLIGHT_TIMEOUT',.01):
                result=await asyncio.wait_for(runtime.stop(str(uuid4()),'durable-owned-test'),1)
            self.assertTrue(result['cloud_end_confirmed'])
            self.assertEqual([f['type'] for f in http.frames],[1,5]);self.assertEqual(http.audio,[])
            await runtime.shutdown()


    async def test_cleanup_only_http_never_poison_next_paid_runtime_workspace(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            env={'VIDU_API_KEY':'opaque-vidu-test','DASHSCOPE_API_KEY':'opaque-bailian-test','BAILIAN_WORKSPACE_ID':'approved-workspace',
                 'HELL_INC_AVATAR_ID':'123','HELL_INC_PLANNER_MODEL':'model','HELL_INC_TTS_VOICE':'voice','HELL_INC_VOICE_PERSONA':'persona','HELL_INC_MEDIA_DIR':tmp}
            clients=[]
            def transport(*,workspace,proxy=None,audio_hosts=()):
                client=FakeReadback();client.workspace=workspace;client.closed=False
                async def close():client.closed=True
                client.close=close;clients.append(client);return client
            settings=SimpleNamespace(real_media_enabled=False,allow_paid_media=False)
            runtime=RealMediaRuntime(settings,component_factory=FakeComponent)
            with patch.dict(os.environ,env,clear=True),patch('hell_inc.media_runtime.MediaHTTP',side_effect=transport):
                old,new=str(uuid4()),str(uuid4())
                ended=await runtime.stop(old,'durable-owned-test')
                self.assertTrue(ended['cloud_end_confirmed'])
                self.assertIsNone(runtime.http)
                self.assertEqual(clients[0].workspace,'cleanup-only')
                # Restart cleanup has no runtime.session_id, so service close need
                # not call release_ended. Isolation must work even without it.
                self.assertIsNone(runtime.session_id)
                self.assertIs(runtime._cleanup_http,clients[0])
                settings.real_media_enabled=True;settings.allow_paid_media=True
                async def created(live_id,meta):pass
                async def persist(meta):pass
                await runtime.start(new,created,persist)
                self.assertEqual(runtime.http.workspace,'approved-workspace')
                self.assertIs(runtime.http,clients[1]);self.assertIsNot(runtime.http,clients[0])
                self.assertEqual(len(clients),2)
                await runtime.shutdown()
                self.assertTrue(clients[1].closed);self.assertTrue(clients[0].closed)
