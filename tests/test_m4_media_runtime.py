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
