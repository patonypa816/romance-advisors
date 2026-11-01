"""Record-only diagnostic using real planner/SSE/converter/component/scheduler.

Providers, renderer ACK and sockets are offline fixtures. No fake browser joined,
no paid/live calls, no recorded-media or audience acceptance is inferred.
"""
import asyncio
import base64
import copy
import hashlib
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
from hell_inc.avatar_transport import component_live, motion_component, idle_motion
from hell_inc.media_http import MediaHTTP, BailianPlanner
from hell_inc.media_runtime import MediaConfig
from test_m4_real_adapter import Response, wave_fixture
from test_m4_policy import action, RECOVERY
from test_m4_scheduler import VirtualClock, FakeSocket


class JsonResponse(Response):
    async def json(self, **kwargs): return json.loads(self.raw)


class RecordOnlyProvider:
    headers = {}
    def __init__(self, clock, seconds=15):
        self.clock=clock;self.wav,self.pcm=wave_fixture(seconds)
        self.calls=[];self.sockets=[];self.trace=[];self.component=None
        self.actions=True;self.result_no_actions=False;self.bad_stage2=False;self.bad_audio=False
        self.creation_lost=False;self.ended=True;self.fail_pcm=False
        self.recording_status='pending';self.recording_url=None;self.before_create=None
    def request(self, method, url, **kwargs):
        self.calls.append((method,url,copy.deepcopy(kwargs)))
        if url.endswith('/chat/completions'):
            data=json.loads(kwargs['json']['messages'][1]['content'])
            if 'input' in data:
                self.trace.append('stage2')
                output={'schedule_id':'not-a-schedule' if self.bad_stage2 else data['input']['schedules'][0]['schedule_id']}
            else:
                self.trace.append('stage1')
                clause=re.search(r'[^\s，。！？,.!?；;：:]+',data['text']).group()
                a=action();a['anchor']={'quote':clause,'occurrence':1}
                has_actions=self.actions and not (self.result_no_actions and data.get('intent')=='result')
                output={'instruction':'自然清晰、松弛、正常语速','actions':[a] if has_actions else [],'recovery':RECOVERY if has_actions else None}
            value={'id':'offline-model-'+str(len(self.calls)),'usage':{'prompt_tokens':4,'completion_tokens':2,'total_tokens':6},
                   'choices':[{'message':{'content':json.dumps(output,ensure_ascii=False)}}]}
            return JsonResponse(json.dumps(value).encode(),'application/json')
        if 'maas.aliyuncs.com' in url:
            self.trace.append('tts');text=kwargs['json']['input']['text']
            words=[{'text':text.strip(),'begin_index':0,'end_index':len(text.strip()),'begin_time':2000,'end_time':2500}]
            event={'request_id':'offline-record-tts','usage':{'input_tokens':5,'output_tokens':8,'total_tokens':13},
                   'output':{'type':'sentence-end','sentence':{'index':0,'words':words},
                             'audio':{'data':base64.b64encode(b'bad-wav' if self.bad_audio else self.wav).decode()},'finish_reason':'stop'}}
            return Response(('data: '+json.dumps(event,ensure_ascii=False)+'\n\n').encode(),'text/event-stream')
        if method=='POST':
            if not url.endswith('/live/s_avatar/component'):raise AssertionError('unapproved create path')
            self.trace.append('create')
            if self.before_create:self.before_create()
            if self.creation_lost:raise ConnectionError('offline ambiguous creation; signed-url/secret must not be persisted')
            value={'live':{'id':'record-owned-test','status':'creating'},'managed_rtc':{'token':'never-public-rtc-test','user_id':'unused-browser','channel_id':'unused-channel'}}
        elif url.endswith('/recording'):
            self.trace.append('recording_get');value={'recording':{'status':self.recording_status,'video_url':self.recording_url,'enabled':True}}
        else:
            self.trace.append('end_get')
            if not self.ended:self.clock.now+=1
            value={'live':{'id':'record-owned-test','status':'ended' if self.ended else 'active'}}
        return JsonResponse(json.dumps(value).encode(),'application/json')
    async def ws_connect(self,url,**kwargs):
        self.trace.append('connect');owner=self
        class Socket(FakeSocket):
            def __init__(self):super().__init__(owner.clock);self.closed=False;self.messages=asyncio.Queue()
            def __aiter__(self):return self
            async def __anext__(self):
                value=await self.messages.get()
                if value is None:raise StopAsyncIteration
                return value
            async def send_json(self,frame):
                await super().send_json(frame)
                if frame['type']==1:
                    self.messages.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.TEXT,data=json.dumps({'type':2,'payload':{'conn_init_ack':{'success':True}}})))
                if frame['type']==5:owner.trace.append('hangup')
            async def send_bytes(self,chunk):
                if owner.fail_pcm:raise ConnectionError('offline injected PCM send failure')
                await super().send_bytes(chunk)
            async def close(self):self.closed=True;self.messages.put_nowait(None)
            async def ping(self):pass
        socket=Socket();self.sockets.append(socket);return socket


class SharedRecordOnlyPreparationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.clock=VirtualClock()
        for module in (component_live,motion_component,idle_motion):
            p=patch.object(module,'time',self.clock);p.start();self.addCleanup(p.stop)
        self.provider=RecordOnlyProvider(self.clock)
        self.http=MediaHTTP(self.provider,workspace='offline-workspace',proxy='http://offline-proxy.example:8080')
        self.planner=BailianPlanner(self.http,'opaque-model-test','offline-workspace','approved-model')
        self.evidence=[];self.guards=[]
    async def prepare(self,*,require_actions=True,bounds=(13,17),text=' 你好\n'):
        from uuid import uuid4
        from hell_inc.real_performance import prepare_frozen
        context={'performance_id':str(uuid4()),'session_id':str(uuid4()),'segment_index':0,'payload_hash':'a'*64}
        async def persist(ctx,value):self.evidence.append(copy.deepcopy(value))
        return await prepare_frozen({'text':text,'intent':'comment'},lambda:False,context=context,
            authorize=lambda ctx:self.guards.append(ctx),current_epoch=lambda:'exclusive-attempt',model=self.planner,http=self.http,
            api_key='opaque-tts-test',workspace='offline-workspace',artifact_root=self.root/'prep',preparation_evidence=persist,
            voice='approved-voice',persona='approved-neutral-persona',require_actions=require_actions,duration_bounds=bounds)

    async def test_shared_real_planner_sse_pcm_preparation_needs_no_browser_or_cloud(self):
        with patch('aiohttp.ClientSession',side_effect=AssertionError('external network forbidden')):
            semantic,request_id=await self.prepare()
        self.assertEqual(self.provider.trace,['stage1','tts','stage2'])
        self.assertEqual(semantic.content['text'],' 你好\n');self.assertEqual(semantic.pcm,self.provider.pcm)
        self.assertEqual(len(semantic.pcm)/48000,15);self.assertEqual(semantic.pcm_sha256,hashlib.sha256(self.provider.pcm).hexdigest())
        self.assertTrue(semantic.content['actions']);self.assertEqual(request_id,'offline-record-tts')
        self.assertEqual(self.evidence[-1]['phase'],'ready');self.assertGreaterEqual(len(self.guards),3)
        for method,url,kwargs in self.provider.calls:
            self.assertNotIn('vidu',url)
            if url.endswith('/chat/completions'):
                request=kwargs['json'];self.assertIs(request['enable_thinking'],False)
                self.assertGreater(request['max_tokens'],0);self.assertLessEqual(request['max_tokens'],4096)
        self.assertFalse(hasattr(semantic,'runtime_epoch'))

    async def test_actionless_stage1_fails_before_tts_or_create(self):
        self.provider.actions=False
        with self.assertRaises(ValueError):await self.prepare()
        self.assertEqual(self.provider.trace,['stage1'])
        self.assertEqual(self.evidence[-1]['phase'],'failed')

    async def test_bad_audio_or_duration_or_schedule_never_creates_or_retries(self):
        for failure in ('audio','duration','stage2'):
            self.provider.calls=[];self.provider.trace=[];self.provider.bad_audio=failure=='audio';self.provider.bad_stage2=failure=='stage2'
            with self.subTest(failure=failure),self.assertRaises(ValueError):
                await self.prepare(bounds=(1,5) if failure=='duration' else (13,17))
            self.assertNotIn('create',self.provider.trace)
            self.assertEqual(self.provider.trace.count('stage1'),1);self.assertEqual(self.provider.trace.count('tts'),1)
            self.assertLessEqual(self.provider.trace.count('stage2'),1)

    async def test_explicit_duration_contract_not_hardcoded_single_fifteen_second_case(self):
        self.provider.wav,self.provider.pcm=wave_fixture(6)
        semantic,_=await self.prepare(bounds=(5,7))
        self.assertEqual(len(semantic.pcm)/48000,6)
        self.assertEqual(self.provider.trace,['stage1','tts','stage2'])

    async def test_diagnostic_gate_requires_recording_and_never_sets_browser_joined(self):
        from hell_inc.avatar_compat import send_prepared
        with self.assertRaisesRegex(ValueError,'diagnostic_recording_required'):
            motion_component.MotionComponent(self.http,self.root/'invalid',self.root/'missing-image',recording=False,record_only_diagnostic=True)
        semantic,_=await self.prepare()
        production=motion_component.MotionComponent(self.http,self.root/'production',self.root/'missing-image')
        production.state='ready';production._ready.set();production._ws=FakeSocket(self.clock);production._wait=self.clock.wait
        with self.assertRaises(RuntimeError):await send_prepared(semantic,production.send_aligned_expression,lambda:True)
        self.assertFalse(production._joined.is_set());self.assertEqual(production._ws.bytes,[])
        diagnostic=motion_component.MotionComponent(self.http,self.root/'diagnostic',self.root/'missing-image',recording=True,record_only_diagnostic=True)
        diagnostic.state='ready';diagnostic._ready.set();diagnostic._ws=FakeSocket(self.clock);diagnostic._wait=self.clock.wait
        with self.assertRaisesRegex(RuntimeError,'record_only_has_no_receiver'):diagnostic.joined()
        receipt=await send_prepared(semantic,diagnostic.send_aligned_expression,lambda:True)
        self.assertTrue(receipt['send_complete']);self.assertTrue(receipt['actions_sent_complete'])
        self.assertEqual([e['role'] for e in diagnostic.motion_events],['GE','S','Q'])
        self.assertEqual(b''.join(b for _,b in diagnostic._ws.bytes),self.provider.pcm)
        self.assertFalse(diagnostic._joined.is_set());self.assertFalse(diagnostic.public()['stage_joined'])
        self.assertFalse(diagnostic.public()['connected']);self.assertFalse(diagnostic.public()['playback_complete_confirmed'])
        events=''.join(p.read_text() for p in diagnostic.out.glob('*.jsonl'))
        self.assertNotIn('stage_audio_video_decoded_ready',events)
        # The diagnostic exception is narrow: legacy unconstrained motion still requires joined.
        legacy={k:v for k,v in action().items() if k!='anchor'}|{'kind':'S','delay_seconds':0,'hold_seconds':0}
        with self.assertRaises(RuntimeError):await diagnostic.send_motion_expression([legacy],recovery=RECOVERY)

    async def test_changed_prepared_pcm_text_or_motion_evidence_rejected_before_dispatch(self):
        from dataclasses import replace
        from unittest.mock import AsyncMock
        from hell_inc.avatar_compat import send_prepared
        semantic,_=await self.prepare();sender=AsyncMock()
        for corrupt in (replace(semantic,pcm=semantic.pcm[:-2]),replace(semantic,pcm_sha256='0'*64),replace(semantic,timeline_json='[]')):
            with self.subTest(corrupt=corrupt.pcm_sha256),self.assertRaises(ValueError):await send_prepared(corrupt,sender,lambda:True)
        sender.assert_not_awaited()


class RecordOnlyRunnerTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=SharedRecordOnlyPreparationTests.asyncSetUp
    async def configure(self):
        self.config=MediaConfig('opaque-vidu-test','opaque-tts-test','offline-workspace','123456','approved-model','approved-voice','approved-neutral-persona',self.root/'artifacts')
        self.segments=[{'text':' 你好\n','intent':'comment'},{'text':'谢谢','intent':'comment'},{'text':'再见','intent':'result'}]
        self.sources=[{'kind':'competition_comment','id':'11111111-1111-4111-8111-111111111111','submission_id':'22222222-2222-4222-8222-222222222222'}, {'kind':'competition_comment','id':'11111111-1111-4111-8111-111111111111','submission_id':'33333333-3333-4333-8333-333333333333'}, {'kind':'competition_result','id':'11111111-1111-4111-8111-111111111111'}]
        async def freeze(sources):
            from hell_inc.performance_contract import canonical_hash
            return [{'source_ref':{**source,'source_version':7,'source_hash':canonical_hash({'source':source})},'source_snapshot':{'source':source},'segments':[copy.deepcopy(segment)],'content_hash':canonical_hash([segment])} for source,segment in zip(sources,self.segments)]
        self.sources=await freeze(self.sources)
        self.out=self.root/'attempt';self.components=[];self.send_receipts=[];owner=self
        def factory(*args,**kwargs):
            component=motion_component.MotionComponent(*args,**kwargs)
            component._wait=owner.clock.wait;component.END_TIMEOUT=.02;component.POLL_INTERVAL=.001
            original=component.send_aligned_expression
            async def send(*args,**kwargs):
                owner.provider.trace.append('send_enter')
                result=await original(*args,**kwargs)
                owner.provider.trace.append('send_returned')
                owner.send_receipts.append(copy.deepcopy(result));return result
            component.send_aligned_expression=send
            owner.components.append(component);owner.provider.component=component
            return component
        self.factory=factory
    async def run_attempt(self,**changes):
        from hell_inc.record_only import run_record_only
        return await run_record_only(self.config,self.out,sources=self.sources,confirm_paid=True,
            http=self.http,model=self.planner,component_factory=self.factory,max_total_seconds=50,ready_timeout=2,**changes)
    def manifest(self):return json.loads((self.out/'attempt.json').read_text())

    async def test_all_three_prepare_before_one_create_ordered_original_send_drain_q_then_stop(self):
        await self.configure()
        def check_create():
            self.assertEqual(self.provider.trace,['stage1','tts','stage2']*3+['create'])
            self.assertEqual(self.manifest()['state'],'creating')
            self.assertEqual(len(self.manifest()['segments']),3)
            planned=json.loads((self.out/'prepared-manifest.json').read_text())
            self.assertEqual(planned['evidence'],'planned_not_sent')
            self.assertEqual([s['content']['text'] for s in planned['segments']],[s['text'] for s in self.segments])
            self.assertTrue(all(s['pcm_sha256'] and s['timeline'] and s['alignment'] for s in planned['segments']))
        self.provider.before_create=check_create
        with patch('socket.create_connection',side_effect=AssertionError('external network forbidden')):
            result=await self.run_attempt()
        self.assertEqual(len(self.components),1);component=self.components[0]
        self.assertEqual(self.provider.trace.count('create'),1);self.assertEqual(self.provider.trace.count('send_enter'),3)
        self.assertEqual(self.provider.trace.count('send_returned'),3)
        self.assertLess(max(i for i,x in enumerate(self.provider.trace) if x=='send_returned'),self.provider.trace.index('hangup'))
        self.assertEqual(self.provider.trace.count('recording_get'),1)
        self.assertTrue(result['cloud_end_confirmed']);self.assertTrue(result['transport_complete'])
        self.assertEqual(result['mode'],'record_only_diagnostic')
        for key in ('receiver_verified','audience_heard','visible_actions_verified','recording_available'):self.assertIs(result[key],False)
        self.assertFalse(component._joined.is_set());self.assertFalse(component.public()['connected'])
        self.assertEqual(component.utterances_sent,3)
        self.assertEqual(b''.join(b for socket in self.provider.sockets for _,b in socket.bytes),self.provider.pcm*3)
        self.assertEqual([e['role'] for e in component.motion_events],['GE','S','Q']*3)
        times=[e['sent_at_monotonic'] for e in component.motion_events]
        self.assertTrue(all(b-a>=1-1e-8 for a,b in zip(times,times[1:])))
        for receipt in self.send_receipts:
            self.assertTrue(receipt['buffer_complete']);self.assertTrue(receipt['actions_sent_complete'])
            self.assertEqual(receipt['pcm_transport_sent_bytes'],len(self.provider.pcm))
        for path in self.out.rglob('*'):
            if path.is_file():
                self.assertEqual(path.stat().st_mode&0o777,0o600)
                if path.suffix in ('.json','.jsonl'):
                    raw=path.read_text()
                    for secret in ('opaque-vidu-test','opaque-tts-test','opaque-model-test','never-public-rtc-test','stage_audio_video_decoded_ready'):
                        self.assertNotIn(secret,raw,path.name)
        frozen=json.loads((self.out/'frozen-segments.json').read_text());self.assertEqual(frozen,self.segments)
        count=len(self.provider.calls)
        with self.assertRaises(FileExistsError):await self.run_attempt()
        self.assertEqual(len(self.provider.calls),count)

    async def test_default_paid_denial_and_recording_false_make_zero_provider_requests(self):
        from dataclasses import replace
        from hell_inc.record_only import run_record_only
        await self.configure()
        with self.assertRaisesRegex(ValueError,'explicit_paid_confirmation_required'):
            await run_record_only(self.config,self.out,sources=self.sources,http=self.http,model=self.planner)
        with self.assertRaisesRegex(ValueError,'diagnostic_recording_required'):
            await run_record_only(replace(self.config,recording_enabled=False),self.out,sources=self.sources,confirm_paid=True,http=self.http,model=self.planner)
        self.assertEqual(self.provider.calls,[]);self.assertFalse(self.out.exists())

    async def test_any_preparation_failure_empty_actions_or_total_duration_mismatch_creates_zero(self):
        await self.configure();self.provider.actions=False
        with self.assertRaises(ValueError):await self.run_attempt()
        self.assertEqual(self.provider.trace,['stage1','tts']*3);self.assertEqual(self.components,[])
        self.assertEqual(self.manifest()['state'],'preparation_failed')
        self.assertIsNone(self.manifest()['owned_live_id'])
        count=len(self.provider.calls)
        with self.assertRaises(FileExistsError):await self.run_attempt()
        self.assertEqual(len(self.provider.calls),count)

    async def test_last_segment_failure_keeps_prior_paid_evidence_but_still_zero_cloud(self):
        await self.configure();original=self.provider.request
        def request(method,url,**kwargs):
            if 'maas.aliyuncs.com' in url and not url.endswith('/chat/completions') and self.provider.trace.count('tts')==2:self.provider.bad_audio=True
            return original(method,url,**kwargs)
        self.provider.request=request
        with self.assertRaises(ValueError):await self.run_attempt()
        self.assertEqual(self.provider.trace.count('stage1'),3);self.assertEqual(self.provider.trace.count('tts'),3)
        self.assertEqual(self.provider.trace.count('stage2'),2);self.assertNotIn('create',self.provider.trace)
        self.assertEqual(len(self.manifest()['segments']),2)
        evidence=(self.out/'preparation.jsonl').read_text();self.assertIn('offline-record-tts',evidence)

    async def test_partial_send_failure_stops_owned_scene_without_replay_or_later_segment(self):
        await self.configure()
        original_factory=self.factory
        def factory(*args,**kwargs):
            c=original_factory(*args,**kwargs);original=c.send_aligned_expression
            async def send(*args,**kwargs):
                if self.provider.trace.count('send_returned')==1:self.provider.fail_pcm=True
                return await original(*args,**kwargs)
            c.send_aligned_expression=send;return c
        self.factory=factory
        with self.assertRaises(ConnectionError):await self.run_attempt()
        self.assertEqual(self.provider.trace.count('create'),1)
        self.assertEqual(self.provider.trace.count('send_enter'),2);self.assertEqual(self.provider.trace.count('send_returned'),1)
        self.assertIn('hangup',self.provider.trace);self.assertTrue(self.manifest()['cloud_end_confirmed'])
        self.assertNotIn('transport_complete',self.manifest());self.assertEqual(self.manifest()['error_code'],'diagnostic_failed')

    async def test_lost_create_response_is_unknown_no_second_post_or_fake_end(self):
        await self.configure();self.provider.creation_lost=True
        with self.assertRaises(RuntimeError):await self.run_attempt()
        self.assertEqual(self.provider.trace.count('create'),1);self.assertNotIn('send_enter',self.provider.trace)
        self.assertEqual(self.manifest()['state'],'unknown');self.assertFalse(self.manifest()['cloud_end_confirmed'])
        self.assertIsNone(self.manifest()['owned_live_id'])
        with self.assertRaises(FileExistsError):await self.run_attempt()
        self.assertEqual(self.provider.trace.count('create'),1)

    async def test_stop_unconfirmed_never_reports_ended_or_reopens_scene(self):
        await self.configure();self.provider.ended=False
        result=await self.run_attempt()
        self.assertEqual(result['state'],'unknown');self.assertFalse(result['cloud_end_confirmed'])
        self.assertEqual(self.provider.trace.count('create'),1);self.assertNotIn('recording_get',self.provider.trace)
        with self.assertRaises(FileExistsError):await self.run_attempt()
        self.assertEqual(self.provider.trace.count('create'),1)

    async def test_short_result_may_have_no_action_without_forcing_text_or_minimum_duration(self):
        await self.configure();self.provider.result_no_actions=True
        self.provider.recording_url='https://unapproved-download.example/movie.mp4?signature=private-recording-token'
        self.provider.wav,self.provider.pcm=wave_fixture(6)
        result=await self.run_attempt()
        self.assertEqual(result['pcm_seconds'],18)
        self.assertTrue(result['recording_available']);self.assertEqual(result['recording_host'],'unapproved-download.example')
        self.assertFalse(any('unapproved-download.example' in url for _,url,_ in self.provider.calls))
        self.assertNotIn('private-recording-token',''.join(p.read_text() for p in self.out.rglob('*.json'))) 
        self.assertEqual([s['actions'] for s in result['segments']],[1,1,0])
        self.assertEqual(self.provider.trace.count('stage2'),2);self.assertEqual(self.provider.trace.count('create'),1)
        self.assertEqual(json.loads((self.out/'frozen-segments.json').read_text()),self.segments)

    async def test_source_hash_round_submission_and_chunk_bounds_fail_before_any_charge(self):
        from hell_inc.record_only import run_record_only
        from hell_inc.performance_contract import canonical_hash
        await self.configure()
        variants=[]
        bad=copy.deepcopy(self.sources);bad[0]['segments'][0]['text']='替换原文';variants.append(bad)
        bad=copy.deepcopy(self.sources);bad[1]['source_ref']['id']='44444444-4444-4444-8444-444444444444';variants.append(bad)
        bad=copy.deepcopy(self.sources);bad[1]['source_ref']['submission_id']=bad[0]['source_ref']['submission_id'];variants.append(bad)
        bad=copy.deepcopy(self.sources);bad[2]['source_snapshot']={'fake':'changed'};variants.append(bad)
        bad=copy.deepcopy(self.sources);bad[0]['segments']*=7;bad[0]['content_hash']=canonical_hash(bad[0]['segments']);variants.append(bad)
        for sources in variants:
            with self.subTest(sources=sources),self.assertRaises(ValueError):
                await run_record_only(self.config,self.out,sources=sources,confirm_paid=True,http=self.http,model=self.planner)
        self.assertEqual(self.provider.calls,[]);self.assertFalse(self.out.exists())

    async def test_precreate_revalidation_rejects_changed_prepared_pcm_before_scene_cost(self):
        from dataclasses import replace
        from hell_inc import record_only
        await self.configure();original=record_only.prepare_frozen
        async def corrupt(*args,**kwargs):
            semantic,request_id=await original(*args,**kwargs)
            return replace(semantic,pcm_sha256='0'*64),request_id
        with patch.object(record_only,'prepare_frozen',corrupt),self.assertRaises(ValueError):await self.run_attempt()
        self.assertNotIn('create',self.provider.trace);self.assertEqual(self.components,[])


# This one test uses actual persisted M3/game APIs and the same offline media chain.
# It is PostgreSQL integration evidence, not evaluator quality or live Vidu evidence.
import test_m4_api as m4api
import test_m3_api as m3api
from hell_inc import db
from hell_inc.app import STATE
from sqlalchemy import select


class RecordOnlyM3IntegrationTests(m4api.M4Fixture):
    configure=RecordOnlyRunnerTests.configure
    run_attempt=RecordOnlyRunnerTests.run_attempt
    manifest=RecordOnlyRunnerTests.manifest
    register=m3api.M3ApiTests.register
    start=m3api.M3ApiTests.start
    snapshot=m3api.M3ApiTests.snapshot
    command=m3api.M3ApiTests.command
    subcommand=m3api.M3ApiTests.subcommand
    submit_work=m3api.M3ApiTests.submit
    export=m3api.M3ApiTests.export
    result_payload=m3api.M3ApiTests.result_payload
    accept=m3api.M3ApiTests.accept
    voting=m3api.M3ApiTests.voting
    vote=m3api.M3ApiTests.vote
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await SharedRecordOnlyPreparationTests.asyncSetUp(self)
    async def test_actual_settled_m3_exports_two_comments_result_to_record_only_without_asset_mutation(self):
        await self.configure()
        self.provider.wav,self.provider.pcm=wave_fixture(6)
        await self.post('/api/performance-session',self.session_payload|{'command_id':str(__import__('uuid').uuid4()),'mode':'record_only_diagnostic'},400)
        people=[await self.register('record-only-'+str(i)) for i in range(2)]
        await self.start();await self.command('open')
        self.sids=[(await self.submit_work(p))['submission_id'] for p in people]
        await self.command('close-submissions')
        for sid in self.sids:await self.accept(sid)
        await self.voting()
        for person,sid in zip(people,self.sids):await self.vote(person,sid)
        await self.command('close-voting');await self.command('settle')
        refs=[{'kind':'competition_comment','id':self.cid,'submission_id':sid} for sid in self.sids]
        refs.append({'kind':'competition_result','id':self.cid})
        self.sources=[await self.post('/api/performance-sources/freeze',{'source':ref}) for ref in refs]
        async def game_rows():
            async with self.client.server.app[STATE]['engine'].connect() as conn:
                return {t.name:[dict(r) for r in (await conn.execute(select(t))).mappings()]
                        for t in db.metadata.sorted_tables if not t.name.startswith('performance')}
        before=await game_rows();result=await self.run_attempt()
        self.assertTrue(result['transport_complete']);self.assertFalse(result['receiver_verified'])
        self.assertEqual(result['source_verification'],'export_consistency_only')
        self.assertEqual(await game_rows(),before)
        self.assertEqual(json.loads((self.out/'frozen-sources.json').read_text()),self.sources)
        spoken=[frame['payload']['text_msg']['content'] for socket in self.provider.sockets for _,frame in socket.frames if frame['type']==10]
        self.assertEqual(spoken,[s['text'] for source in self.sources for s in source['segments']])
