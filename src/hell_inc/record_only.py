"""Explicit, one-attempt diagnostic; not imported by the production HTTP service.

Prepare a bounded three-source game scenario before creating one recorded renderer.
No receiver claim, heartbeat, joined flag or audience acceptance is manufactured.
"""
import asyncio
import json
import os
import shutil
from pathlib import Path
from uuid import uuid4
from .avatar_compat import send_prepared, validate_prepared
from .avatar_transport.motion_component import MotionComponent
from .avatar_transport.diagnostics import private_open, scrub
from .media_http import MediaHTTP, BailianPlanner
from .performance_contract import canonical_hash, source_ref
from .real_performance import prepare_frozen


def _write(path, value):
    tmp=path.with_suffix('.tmp')
    with private_open(tmp,'w',encoding='utf-8') as f:
        json.dump(value,f,ensure_ascii=False,allow_nan=False);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)


async def run_record_only(config,out,*,sources,confirm_paid=False,http=None,model=None,
                          component_factory=None,max_total_seconds=90,
                          ready_timeout=90):
    if confirm_paid is not True:raise ValueError('explicit_paid_confirmation_required')
    if config.recording_enabled is not True:raise ValueError('diagnostic_recording_required')
    if type(max_total_seconds) not in (int,float) or not 0<max_total_seconds<=90:raise ValueError('invalid_duration_bounds')
    if type(ready_timeout) not in (int,float) or not 0<ready_timeout<=180:raise ValueError('invalid_ready_timeout')
    if not isinstance(sources,list) or len(sources)!=3:raise ValueError('three_game_sources_required')
    refs=[]
    for item,kind in zip(sources,('competition_comment','competition_comment','competition_result')):
        ref=source_ref(item['source_ref'])
        if ref['kind']!=kind:raise ValueError('m3_scenario_required')
        if not isinstance(item.get('segments'),list) or not item['segments']:raise ValueError('empty_game_source')
        if canonical_hash(item['segments'])!=item['content_hash']:raise ValueError('source_hash_mismatch')
        if 'source_snapshot' in item and canonical_hash(item['source_snapshot'])!=ref['source_hash']:
            raise ValueError('source_snapshot_mismatch')
        refs.append(ref)
    if len({r['id'] for r in refs})!=1 or refs[0]['submission_id']==refs[1]['submission_id']:
        raise ValueError('same_round_two_submissions_required')
    frozen=json.loads(json.dumps(sources,ensure_ascii=False,allow_nan=False))
    segments=[segment for item in frozen for segment in item['segments']]
    if not 1<=len(segments)<=8:raise ValueError('segment_cap_exceeded')
    for segment in segments:
        if (not isinstance(segment,dict) or set(segment)!={'text','intent'} or
            not isinstance(segment['text'],str) or not segment['text'].strip() or len(segment['text'])>120 or
            segment['intent'] not in ('question','listen','comment','result','neutral')):
            raise ValueError('invalid_frozen_segment')
    if not all(shutil.which(name) for name in ('ffmpeg','ffprobe')):raise ValueError('audio_tools_required')
    out=Path(out)
    if not out.is_absolute():raise ValueError('absolute_attempt_directory_required')
    out.mkdir(mode=0o700,parents=False,exist_ok=False)
    out.chmod(0o700)
    attempt=str(uuid4());performance=str(uuid4());digest=canonical_hash(segments)
    summary={'mode':'record_only_diagnostic','attempt_id':attempt,'source_hash':digest,
             'source_verification':'export_consistency_only','state':'preparing','recording_requested':True,'receiver_verified':False,
             'audience_heard':False,'visible_actions_verified':False,'owned_live_id':None,
             'cloud_end_confirmed':False,'recording_available':False,'segments':[],
             'budgets':{'stage1':len(segments),'tts':len(segments),'stage2':len(segments),'create':1,'max_pcm_seconds':max_total_seconds,'local_scene_stop_deadline_seconds':180}}
    _write(out/'attempt.json',summary)
    _write(out/'frozen-sources.json',frozen)
    _write(out/'frozen-segments.json',segments)
    owns_http=http is None
    http=http or MediaHTTP(workspace=config.workspace,proxy=config.proxy,audio_hosts=config.audio_hosts)
    model=model or BailianPlanner(http,config.tts_key,config.workspace,config.model)
    component=None;prepared=[];cancelled=False;total=0.0

    def authorize(ctx):
        if cancelled or summary['state']!='preparing' or ctx['session_id']!=attempt or ctx['payload_hash']!=digest:
            raise ValueError('diagnostic_preparation_not_authorized')

    def safe(value):
        if isinstance(value,dict):return {k:safe(v) for k,v in value.items()}
        if isinstance(value,list):return [safe(v) for v in value]
        if isinstance(value,str):
            for key in (config.api_key,config.tts_key):value=value.replace(key,'[credential removed]')
            return scrub(value)
        return value

    async def evidence(ctx,event):
        # Never log provider messages or tokens. Existing preparation emits bounded
        # validated plans, counts, IDs and PCM hashes only.
        item={'segment_index':ctx['segment_index'],**safe(event)}
        with private_open(out/'preparation.jsonl','a',encoding='utf-8') as f:
            f.write(json.dumps(item,ensure_ascii=False,allow_nan=False)+'\n');f.flush();os.fsync(f.fileno())

    async def owned(live_id,cloud):
        summary['owned_live_id']=live_id
        _write(out/'attempt.json',summary)  # durable before original component WSS

    async def stop_and_collect():
        if component is None:return
        component.latch_stop()
        summary['state']='stopping'
        try:_write(out/'attempt.json',summary)
        except OSError:pass  # A failed evidence disk must not prevent hangup.
        try:
            result=await component.stop('record-only diagnostic finished')
        except BaseException:
            summary.update(state='unknown',stop_error='stop_unconfirmed')
            try:_write(out/'attempt.json',summary)
            except OSError:pass
            raise
        summary['cloud_end_confirmed']=result.get('cloud_end_confirmed') is True
        summary['state']='ended' if summary['cloud_end_confirmed'] else 'unknown'
        _write(out/'attempt.json',summary)
        if summary['cloud_end_confirmed'] and summary['owned_live_id']:
            try:
                result=await component._json('GET',f"/live/v1/lives/{summary['owned_live_id']}/recording",timeout=10)
                recording=result.get('recording') or {}
                # Signed URL remains memory-only; downloading requires an independently
                # reviewed exact host, not an implicit extension of the allowlist.
                from urllib.parse import urlsplit
                summary['recording_available']=bool(recording.get('video_url'))
                summary['recording_host']=urlsplit(recording.get('video_url') or '').hostname
                summary['provider_recording_enabled']=recording.get('enabled') is True
            except Exception:
                summary['recording_query_error']='recording_unavailable'
        _write(out/'attempt.json',summary)

    try:
        for index,segment in enumerate(segments):
            remaining=max_total_seconds-total
            if remaining<=0:raise ValueError('diagnostic_duration_out_of_range')
            semantic,request_id=await prepare_frozen(segment,lambda:cancelled,
                context={'session_id':attempt,'performance_id':performance,'segment_index':index,'payload_hash':digest},
                authorize=authorize,current_epoch=lambda:attempt,model=model,http=http,
                api_key=config.tts_key,workspace=config.workspace,artifact_root=out/'preparation',
                preparation_evidence=evidence,voice=config.voice,persona=config.persona,
                require_actions=False,duration_bounds=(0.001,min(30,remaining)))
            prepared.append(semantic);total+=semantic.audio_seconds
            summary['segments'].append({'index':index,'pcm_sha256':semantic.pcm_sha256,
                'seconds':semantic.audio_seconds,'actions':len(semantic.content['actions']),
                'tts_request_id':request_id})
            _write(out/'attempt.json',summary)
        if total>max_total_seconds:raise ValueError('diagnostic_duration_out_of_range')
        if not any(item.content['actions'] for item in prepared):raise ValueError('diagnostic_actions_required')
        for segment,semantic in zip(segments,prepared):
            validate_prepared(semantic)
            if semantic.content['text']!=segment['text']:raise ValueError('frozen_text_changed')
        segment_sources=[item['source_ref'] for item in frozen for _ in item['segments']]
        _write(out/'prepared-manifest.json',{'evidence':'planned_not_sent','segments':[
            {'index':index,'source_ref':segment_sources[index],'content':safe(semantic.content),
             'alignment':safe(semantic.alignment),'timeline':safe(semantic.timeline),
             'pcm_sha256':semantic.pcm_sha256,'audio_seconds':semantic.audio_seconds,
             'text_hash':canonical_hash(segments[index]['text'])}
            for index,semantic in enumerate(prepared)]})
        summary.update(state='prepared',pcm_seconds=total);_write(out/'attempt.json',summary)
        component=(component_factory or MotionComponent)(http,out/'component',out/'unused-image',
            on_created=owned,recording=True,collect_recording=False,record_only_diagnostic=True)
        summary.update(state='creating',create_attempted=True);_write(out/'attempt.json',summary)
        # Local 180s stop deadline, not a guarantee of provider billing/end time.
        async with asyncio.timeout(180):
            await component.start(config.api_key,avatar_id=config.avatar_id)
            async with asyncio.timeout(ready_timeout):
                while component.state!='ready':
                    if component.state in ('ended','error','stop_unconfirmed','creation_unknown'):
                        raise RuntimeError('diagnostic_renderer_not_ready')
                    await asyncio.sleep(.05)
            def deliverable():
                return (bool(summary['owned_live_id']) and component.live_id==summary['owned_live_id']
                        and component.recording_enabled and component._ready.is_set()
                        and not component._stop.is_set() and not cancelled)
            summary.update(state='sending',send_attempted=True);_write(out/'attempt.json',summary)
            for index,semantic in enumerate(prepared):
                receipt=await send_prepared(semantic,component.send_aligned_expression,deliverable,number=index+1)
                _write(out/f'send-{index}.json',safe(receipt))
                if (not all(receipt.get(k) is True for k in ('send_complete','buffer_complete','actions_sent_complete'))
                        or receipt.get('pcm_transport_sent_bytes')!=len(semantic.pcm)):
                    raise RuntimeError('diagnostic_send_unconfirmed')
            summary['transport_complete']=True
    except BaseException as exc:
        cancelled=True
        summary['error_code']='diagnostic_cancelled' if isinstance(exc,asyncio.CancelledError) else 'diagnostic_failed'
        if component is None:summary['state']='preparation_failed'
        _write(out/'attempt.json',summary)
        raise
    finally:
        # Never cancel an in-flight create or discard its ID. Stop waits for that
        # exact create, hangs up, and requires positive ended readback.
        cleanup=asyncio.create_task(stop_and_collect())
        try:
            try:await asyncio.shield(cleanup)
            except asyncio.CancelledError:await cleanup;raise
        finally:
            if owns_http:await http.close()
    return summary
