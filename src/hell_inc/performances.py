"""Durable, explicitly executed offline performance protocol. Never owns game assets.

All send decisions share a local mutex; no network/adapter await holds a DB transaction.
The existing world instance lock remains the single-process ownership boundary.
"""
import asyncio
from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import datetime
from uuid import uuid5, NAMESPACE_URL
from types import SimpleNamespace
from .media_runtime import RealMediaRuntime
from sqlalchemy import select, update, text
from .db import worlds, performance_sessions as sessions, performances, performance_commands as commands, interviews, competitions
from .performance_contract import (PerformanceError,TERMINAL,canonical_hash,validate_submission,validate_envelope,iso)
from .interviews import fields, uuid
from .contracts import ValidationError
from .records.service import CommandConflict
from .performance_sources import freeze_source
from .performance_adapter import FixtureAdapter

class PerformanceService:
    def __init__(self,engine,world_id,adapter=None,runtime=None):
        self.engine,self.world_id=engine,world_id
        self.adapter=adapter or FixtureAdapter()
        self.runtime=runtime or RealMediaRuntime(SimpleNamespace(real_media_enabled=False,allow_paid_media=False))
        self.media_tasks=set()
        self.sender_lock=asyncio.Lock()
        self.worker_lock=asyncio.Lock()
        self.tasks=set()
        self.running=set()
        self.cancelled=set()
        self.gate=False
        self.stop_requested=False

    async def close_gate(self):
        # Gate closure and durable sending commit share the same boundary mutex.
        # The mutex is never held over prepare/send, so media cannot block pause.
        self.stop_requested=True
        async with self.sender_lock:
            self.gate=True

    @asynccontextmanager
    async def _tx(self):
        try:
            async with self.engine.begin() as conn:
                revision=(await conn.execute(select(worlds.c.revision).where(worlds.c.world_id==self.world_id).with_for_update())).scalar_one()
                now=(await conn.execute(text('SELECT clock_timestamp()'))).scalar_one()
                yield conn,revision,now
        except (PerformanceError,ValidationError,CommandConflict):
            raise
        except BaseException:
            self.stop_requested=True
            self.gate=True
            raise

    async def _revision(self,conn):
        return (await conn.execute(update(worlds).where(worlds.c.world_id==self.world_id).values(revision=worlds.c.revision+1).returning(worlds.c.revision))).scalar_one()

    def _snapshot(self,revision,now,**values):
        if 'session' in values:
            session=deepcopy(values['session'])
            media=self.runtime.view(session.get('media') if session else None)
            if session and session['mode']=='real':session['media']=media
            values.update(session=session,media=media)
        return {'revision':revision,'server_time':iso(now),**deepcopy(values)}

    async def _load(self,conn,id):
        uuid(id)
        b=(await conn.execute(select(performances.c.body).where(performances.c.world_id==self.world_id,performances.c.performance_id==id))).scalar_one_or_none()
        if b is None: raise PerformanceError('performance_not_found',404)
        return deepcopy(b)

    async def _session(self,conn,id=None):
        query=select(sessions.c.body).where(sessions.c.world_id==self.world_id)
        if id is not None: query=query.where(sessions.c.session_id==id)
        else: query=query.where(sessions.c.status!='closed')
        b=(await conn.execute(query)).scalar_one_or_none()
        if id is not None and b is None: raise PerformanceError('session_not_found',404)
        return deepcopy(b)

    async def _save(self,conn,b,now):
        b['updated_at']=iso(now)
        await conn.execute(update(performances).where(performances.c.performance_id==b['performance_id'],performances.c.world_id==self.world_id).values(state=b['state'],body=b,updated_at=now))

    async def _save_session(self,conn,s,now):
        await conn.execute(update(sessions).where(sessions.c.session_id==s['session_id'],sessions.c.world_id==self.world_id).values(status=s['status'],body=s,updated_at=now))

    async def _receipt(self,conn,id,payload,response,now):
        await conn.execute(commands.insert().values(world_id=self.world_id,command_id=id,payload=payload,response=response,created_at=now))

    async def _replay(self,conn,id,payload):
        row=(await conn.execute(select(commands).where(commands.c.world_id==self.world_id,commands.c.command_id==id))).mappings().first()
        if row:
            if canonical_hash(row['payload'])!=canonical_hash(payload): raise CommandConflict('同一 command_id 不可改变载荷。')
            if 'error' in row['response']:
                e=row['response']['error'];raise PerformanceError(e['code'],e['status'])
            return deepcopy(row['response'])

    async def _command(self,id,payload,operation):
        failure=None; replayed=False
        async with self.sender_lock:
            async with self._tx() as (conn,revision,now):
                old=await self._replay(conn,id,payload)
                if old is not None: return old,True
                try: response=await operation(conn,revision,now)
                except PerformanceError as exc:
                    failure=exc
                    response={'error':{'code':exc.code,'status':exc.status}}
                await self._receipt(conn,id,payload,response,now)
        if failure: raise failure
        return response,replayed

    async def freeze(self,data):
        fields(data,'source')
        async with self.sender_lock:
            async with self._tx() as (conn,rev,now):
                frozen=await freeze_source(conn,self.world_id,data['source'])
                frozen.pop('source_snapshot')
                return self._snapshot(rev,now,**frozen)

    async def session_get(self):
        async with self.sender_lock:
            async with self._tx() as (conn,rev,now): return self._snapshot(rev,now,session=await self._session(conn))

    async def session_create(self,data):
        fields(data,'command_id session_id mode avatar_aspect');uuid(data['session_id'])
        async def op(conn,rev,now):
            if data['mode'] not in ('offline_fixture','real'):raise ValidationError('mode 无效。')
            if data['mode']=='real' and not self.runtime.real_enabled:raise PerformanceError('real_media_disabled')
            if data['avatar_aspect'] not in (None,'16:9','4:3'): raise ValidationError('avatar_aspect 无效。')
            if self.gate: raise PerformanceError('performance_gate_closed')
            if await self._session(conn): raise PerformanceError('session_exists')
            if (await conn.execute(select(sessions.c.session_id).where(sessions.c.session_id==data['session_id']))).first(): raise PerformanceError('session_id_conflict')
            s={k:data[k] for k in ('session_id','mode','avatar_aspect')}|{'status':'paused' if data['mode']=='real' else 'active','media_verified':False}
            if data['mode']=='real':s['media']=self.runtime.default_media()
            await conn.execute(sessions.insert().values(session_id=s['session_id'],world_id=self.world_id,mode=s['mode'],status=s['status'],body=s,created_at=now,updated_at=now))
            return self._snapshot(await self._revision(conn),now,session=s)
        result,replayed=await self._command(data['command_id'],{'action':'create_session',**data},op)
        return result,not replayed

    def _cancel_unstarted(self,b,reason='cancelled'):
        b['cancel_requested']=True;self.cancelled.add(b['performance_id'])
        for s in b['segments']:
            if s['state'] not in TERMINAL and s['state']!='sending': s['state']=reason
        if b['state'] not in TERMINAL and not any(s['state']=='sending' for s in b['segments']):
            b['state']=reason
            b['result']={'partial':any(s['state']=='send_completed' for s in b['segments'])}

    async def _pause_rows(self,conn,sid,now):
        rows=(await conn.execute(select(performances.c.body).where(performances.c.world_id==self.world_id,performances.c.session_id==sid,performances.c.state.not_in(TERMINAL)))).scalars().all()
        outcome='unstarted_cancelled'
        for raw in rows:
            b=deepcopy(raw)
            if b['state']=='sending': outcome='in_flight'
            self._cancel_unstarted(b);await self._save(conn,b,now)
        unknown=(await conn.execute(select(performances.c.performance_id).where(performances.c.world_id==self.world_id,performances.c.session_id==sid,performances.c.state=='unknown'))).first()
        return 'unknown' if unknown else outcome

    async def session_command(self,id,action,data):
        uuid(id);fields(data,'command_id')
        if action not in ('pause','resume','close'): raise ValidationError('session action 无效。')
        async def op(conn,rev,now):
            s=await self._session(conn,id)
            if s['status']=='closed': raise PerformanceError('session_closed')
            if action=='resume':
                if self.gate or s['status']=='unknown': raise PerformanceError('performance_gate_closed')
                if s['mode']=='real':self.runtime.resume(id)
                unresolved=(await conn.execute(select(performances.c.performance_id).where(performances.c.world_id==self.world_id,performances.c.session_id==id,performances.c.state.in_(('sending','unknown'))))).first()
                if unresolved: raise PerformanceError('unresolved_send')
                s['status']='active';outcome='unstarted_cancelled'
            else:
                # Close must not alter state when uncertainty forbids it.
                unresolved=(await conn.execute(select(performances.c.performance_id).where(performances.c.world_id==self.world_id,performances.c.session_id==id,performances.c.state.in_(('sending','unknown'))))).first()
                if action=='close':
                    if s['mode']=='real':
                        if s.get('media',{}).get('creation_attempted') and not s['media'].get('cloud_end_confirmed'):raise PerformanceError('cloud_end_not_confirmed')
                        active=(await conn.execute(select(performances.c.performance_id).where(performances.c.world_id==self.world_id,performances.c.session_id==id,performances.c.state=='sending'))).first()
                        if active or any(pid in self.running for pid in (await conn.execute(select(performances.c.performance_id).where(performances.c.session_id==id))).scalars()):raise PerformanceError('unresolved_send')
                    elif unresolved or s['status']=='unknown':raise PerformanceError('unresolved_send')
                if s['mode']=='real':self.runtime.pause()
                if s['status']!='unknown' or action=='close':
                    s['status']='paused' if action=='pause' else 'closed'
                outcome=await self._pause_rows(conn,id,now)
            await self._save_session(conn,s,now)
            return self._snapshot(await self._revision(conn),now,session=s,stop_status=outcome)
        result,replayed=await self._command(data['command_id'],{'action':'session_'+action,'session_id':id,**data},op)
        if not replayed and action=='close' and result['session']['mode']=='real' and self.runtime.session_id==id:await self.runtime.release_ended()
        return result

    async def _game_source_available(self,conn,ref):
        if ref['kind']=='fixed_script': return
        table=interviews if ref['kind']=='interview_result' else competitions
        status=(await conn.execute(select(table.c.status).where(table.c.world_id==self.world_id,table.c.id==ref['id']))).scalar_one_or_none()
        if status=='PAUSED': raise PerformanceError('game_paused')

    async def submit(self,data):
        data=validate_envelope(data)
        async with self.sender_lock:
            async with self._tx() as (conn,rev,now):
                old=(await conn.execute(select(performances).where(performances.c.performance_id==data['performance_id']))).mappings().first()
                if old:
                    if old['world_id']!=self.world_id or old['payload_hash']!=data['payload_hash']: raise PerformanceError('performance_id_conflict')
                    return self._snapshot(rev,now,performance=old['body']),False
                data=validate_submission(data)
                if self.gate: raise PerformanceError('performance_gate_closed')
                s=await self._session(conn,data['session_id'])
                if s['status']!='active': raise PerformanceError('session_not_active')
                if datetime.fromisoformat(data['expires_at'].replace('Z','+00:00'))<=now: raise PerformanceError('performance_expired')
                pending=(await conn.execute(select(performances.c.state).where(performances.c.world_id==self.world_id,performances.c.state.not_in(TERMINAL)))).scalars().all()
                if len(pending)>=2: raise PerformanceError('queue_full')
                ref=data['source_ref'];source={k:v for k,v in ref.items() if k not in ('source_version','source_hash')}
                await self._game_source_available(conn,ref)
                frozen=await freeze_source(conn,self.world_id,source)
                if frozen['source_ref']!=ref or frozen['segments']!=data['segments']: raise PerformanceError('source_changed')
                b={k:deepcopy(data[k]) for k in ('performance_id','session_id','source_ref','payload_hash','expires_at')}
                b.update(source_snapshot=frozen['source_snapshot'],state='accepted',segments=[{**v,'index':i,'state':'accepted','result':None} for i,v in enumerate(data['segments'])],cancel_requested=False,mode=s['mode'],evidence={'execution_kind':s['mode'],'media_verified':False,'audience_heard':False},result=None,error_code=None,created_at=iso(now),updated_at=iso(now))
                await conn.execute(performances.insert().values(performance_id=b['performance_id'],world_id=self.world_id,session_id=s['session_id'],payload_hash=b['payload_hash'],immutable_payload={k:deepcopy(data[k]) for k in ('session_id','source_ref','segments','expires_at')},state='accepted',body=b,created_at=now,updated_at=now))
                return self._snapshot(await self._revision(conn),now,performance=b),True

    async def get(self,id):
        async with self.sender_lock:
            async with self._tx() as (conn,rev,now): return self._snapshot(rev,now,performance=await self._load(conn,id))

    async def list(self,session_id=None):
        if session_id is not None: uuid(session_id)
        async with self.sender_lock:
            async with self._tx() as (conn,rev,now):
                q=select(performances.c.body).where(performances.c.world_id==self.world_id)
                if session_id: q=q.where(performances.c.session_id==session_id)
                rows=(await conn.execute(q.order_by(performances.c.created_at.desc(),performances.c.performance_id).limit(50))).scalars().all()
                return self._snapshot(rev,now,performances=rows)

    async def command(self,id,action,data):
        uuid(id);fields(data,'command_id')
        if action not in ('execute','cancel'): raise ValidationError('performance action 无效。')
        launch=False
        async def op(conn,rev,now):
            nonlocal launch
            b=await self._load(conn,id)
            if action=='cancel':
                outcome='already_terminal' if b['state'] in TERMINAL else ('in_flight' if b['state']=='sending' else 'unstarted_cancelled')
                if b['state']=='unknown': outcome='unknown'
                if b['state'] not in TERMINAL:
                    self._cancel_unstarted(b);await self._save(conn,b,now);rev=await self._revision(conn)
                return self._snapshot(rev,now,performance=b,cancel_outcome=outcome)
            if b['state'] not in TERMINAL and id not in self.running:
                s=await self._session(conn,b['session_id'])
                if self.gate or s['status']!='active': raise PerformanceError('session_not_active')
                if b['mode']=='real':self.runtime.assert_ready(b['session_id'])
                if datetime.fromisoformat(b['expires_at'].replace('Z','+00:00'))<=now:
                    self._cancel_unstarted(b,'expired');await self._save(conn,b,now);rev=await self._revision(conn)
                else: launch=True
            return self._snapshot(rev,now,performance=b)
        result,replayed=await self._command(data['command_id'],{'action':action,'performance_id':id,**data},op)
        # There is no await between durable receipt and task reservation.
        if launch and not replayed and id not in self.running:
            self.running.add(id)
            task=asyncio.create_task(self._run(id));self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return result

    async def _guard_segment(self,conn,b,now):
        s=await self._session(conn,b['session_id'])
        if self.gate or self.stop_requested or b['cancel_requested'] or s['status']!='active' or (b['mode']=='real' and not self.runtime.ready(b['session_id'])):
            self._cancel_unstarted(b);return False
        if datetime.fromisoformat(b['expires_at'].replace('Z','+00:00'))<=now:
            self._cancel_unstarted(b,'expired');return False
        try: await self._game_source_available(conn,b['source_ref'])
        except PerformanceError:
            self._cancel_unstarted(b);return False
        return True

    async def _run(self,id):
        attempted=False
        b={}
        try:
            async with self.worker_lock:
                index=0
                while True:
                    async with self.sender_lock:
                        async with self._tx() as (conn,rev,now):
                            b=await self._load(conn,id)
                            if b['state'] in TERMINAL: return
                            if not await self._guard_segment(conn,b,now):
                                await self._save(conn,b,now);await self._revision(conn);return
                            if index>=len(b['segments']): return
                            segment=b['segments'][index]
                            if segment['state']=='send_completed': index+=1;continue
                            segment['state']='preparing'
                            if index==0: b['state']='preparing'
                            await self._save(conn,b,now);await self._revision(conn)
                    adapter=self.runtime.adapter(self.persist_preparation) if b['mode']=='real' else self.adapter
                    frozen_segment={'text':segment['text'],'intent':segment['intent'],'index':index}
                    cancellation=lambda:self.gate or self.stop_requested or id in self.cancelled or (b['mode']=='real' and not self.runtime.ready(b['session_id']))
                    if b['mode']=='real':
                        context={'performance_id':id,'session_id':b['session_id'],'segment_index':index,'payload_hash':b['payload_hash']}
                        prepared=await adapter.prepare(frozen_segment,cancellation,context=context)
                    else:prepared=await adapter.prepare(frozen_segment,cancellation)
                    async with self.sender_lock:
                        async with self._tx() as (conn,rev,now):
                            b=await self._load(conn,id)
                            if b['state'] in TERMINAL: return
                            if not await self._guard_segment(conn,b,now):
                                await self._save(conn,b,now);await self._revision(conn);return
                            b['segments'][index]['state']='ready'
                            if index==0: b['state']='ready'
                            await self._save(conn,b,now);await self._revision(conn)
                    # Separate durable sending commit is the dispatch gate.
                    async with self.sender_lock:
                        async with self._tx() as (conn,rev,now):
                            b=await self._load(conn,id)
                            if b['state'] in TERMINAL: return
                            if not await self._guard_segment(conn,b,now):
                                await self._save(conn,b,now);await self._revision(conn);return
                            b['state']='sending';b['segments'][index]['state']='sending'
                            s=await self._session(conn,b['session_id'])
                            await self._save(conn,b,now);await self._revision(conn)
                        attempted=True
                    # Cancellation after this boundary cannot promise remote abort.
                    receipt=await adapter.send(prepared,s,lambda:not self.gate and not self.stop_requested and id not in self.cancelled)
                    async with self.sender_lock:
                        async with self._tx() as (conn,rev,now):
                            b=await self._load(conn,id)
                            # Recovery may have already fenced an interrupted attempt.
                            if b['state']=='unknown': return
                            b['segments'][index].update(state='send_completed',result=receipt)
                            index+=1
                            if b['cancel_requested'] or any(x['state'] in ('cancelled','expired') for x in b['segments']):
                                b['state']='cancelled';b['result']={'partial':True,('fixture_receipt' if b['mode']=='offline_fixture' else 'transport_receipt'):receipt}
                            elif index==len(b['segments']):
                                b['state']='send_completed';b['result']={'partial':False,('fixture_receipt' if b['mode']=='offline_fixture' else 'transport_receipt'):receipt}
                            await self._save(conn,b,now);await self._revision(conn)
                        attempted=False
                        if b['state'] in TERMINAL:return
        except BaseException as exc:
            # Shutdown before any possible transport write retains safe pending work.
            # Next startup pauses its session; only a NEW manual execute can resume it.
            if isinstance(exc,asyncio.CancelledError) and not attempted and b.get('mode')!='real':
                raise
            # A possibly attempted write or failed receipt commit is never retryable.
            if attempted: await self.close_gate()
            try:
                async with self.sender_lock:
                    async with self._tx() as (conn,rev,now):
                        b=await self._load(conn,id)
                        if b['state'] not in TERMINAL:
                            uncertain=attempted or any(s['state']=='sending' for s in b['segments']) or (b['mode']=='real' and any(x.get('preparation',{}).get('attempted') and x['preparation'].get('phase') not in ('failed','ready') for x in b['segments']))
                            b['state']='unknown' if uncertain else ('cancelled' if id in self.cancelled else 'failed')
                            b['error_code']=('preparation_outcome_unknown' if b['mode']=='real' and not attempted and not any(x['state']=='sending' for x in b['segments']) else 'send_outcome_unknown') if uncertain else 'preparation_failed'
                            b['result']={'partial':any(x['state']=='send_completed' for x in b['segments'])}
                            for seg in b['segments']:
                                if seg['state']=='sending':seg['state']='unknown'
                                elif seg['state'] not in TERMINAL:seg['state']='cancelled' if uncertain else b['state']
                            await self._save(conn,b,now)
                            if uncertain:
                                s=await self._session(conn,b['session_id']);s['status']='unknown';await self._save_session(conn,s,now)
                            await self._revision(conn)
            except BaseException:
                self.gate=True
            if isinstance(exc,asyncio.CancelledError): raise
        finally:
            self.running.discard(id)

    async def pause_for_game(self,command_id=None,source=None):
        """After the game commit, persist linkage receipt; retries cannot pause a new session."""
        async def op(conn,rev,now):
            s=await self._session(conn)
            if not s: return self._snapshot(rev,now,session=None,stop_status='unstarted_cancelled')
            if s['status']!='unknown':s['status']='paused'
            if s['mode']=='real':self.runtime.pause()
            outcome=await self._pause_rows(conn,s['session_id'],now)
            await self._save_session(conn,s,now)
            return self._snapshot(await self._revision(conn),now,session=s,stop_status=outcome)
        if command_id is not None:
            uuid(command_id)
            # Separate deterministic command namespace, not a new external action.
            linked_id=str(uuid5(NAMESPACE_URL,'hell-inc/performance-pause/'+str(source)+'/'+command_id))
            return (await self._command(linked_id,{'action':'game_pause','source':source,'game_command_id':command_id},op))[0]
        async with self.sender_lock:
            async with self._tx() as (conn,rev,now):return await op(conn,rev,now)

    async def recover(self,reason='restart'):
        self.stop_requested=True
        async with self.sender_lock:
            self.gate=True
            async with self._tx() as (conn,rev,now):
                rows=(await conn.execute(select(performances.c.body).where(performances.c.world_id==self.world_id,performances.c.state.not_in(TERMINAL)))).scalars().all()
                changed=False
                for raw in rows:
                    b=deepcopy(raw)
                    if b['state']=='sending' or any(x['state']=='sending' for x in b['segments']):
                        b['state']='unknown';b['error_code']='restart_send_unknown';self.cancelled.add(b['performance_id'])
                        for seg in b['segments']:
                            if seg['state']=='sending':seg['state']='unknown'
                            elif seg['state'] not in TERMINAL:seg['state']='cancelled'
                        await self._save(conn,b,now);changed=True
                    elif b['mode']=='real' and b['state'] in ('preparing','ready'):
                        b['state']='unknown';b['error_code']='preparation_outcome_unknown';await self._save(conn,b,now);changed=True
                s=await self._session(conn)
                if s:
                    unresolved=(await conn.execute(select(performances.c.performance_id).where(performances.c.world_id==self.world_id,performances.c.session_id==s['session_id'],performances.c.state=='unknown'))).first()
                    s['status']='unknown' if unresolved else 'paused'
                    if s['mode']=='real':
                        s.setdefault('media',self.runtime.default_media())
                        self.runtime.pause()
                        if s.get('media',{}).get('creation_attempted') and not s['media'].get('cloud_end_confirmed'):
                            s['media']['state']='unknown';s['status']='unknown'
                        s['media']['receiver']=self.runtime.default_media()['receiver'];s['media']['operator_confirmed']=False
                    await self._save_session(conn,s,now);changed=True
                if changed:await self._revision(conn)
        self.gate=False
        self.stop_requested=False

    async def expire_due(self):
        async with self.sender_lock:
            async with self._tx() as (conn,rev,now):
                rows=(await conn.execute(select(performances.c.body).where(performances.c.world_id==self.world_id,performances.c.state.in_(('accepted','preparing','ready'))))).scalars().all()
                changed=False
                for raw in rows:
                    b=deepcopy(raw)
                    if datetime.fromisoformat(b['expires_at'].replace('Z','+00:00'))<=now:
                        self._cancel_unstarted(b,'expired')
                        await self._save(conn,b,now);changed=True
                if changed: await self._revision(conn)

    async def shutdown(self):
        await self.close_gate()
        for task in list(self.tasks):task.cancel()
        if self.tasks:await asyncio.gather(*list(self.tasks),return_exceptions=True)
        if self.media_tasks:await asyncio.gather(*list(self.media_tasks),return_exceptions=True)
        await self.runtime.shutdown()

    async def _update_media(self,id,patch):
        # Callbacks carry only whitelisted lifecycle facts, never RTC grants.
        allowed={'state','owned_live_id','creation_attempted','cloud_end_confirmed','reconciliation_required'}
        if set(patch)-allowed:raise ValueError('unsafe_media_evidence')
        async with self.sender_lock:
            async with self._tx() as (conn,rev,now):
                s=await self._session(conn,id)
                if s['status']=='closed':return
                if s['media'].get('cloud_end_confirmed') and not patch.get('cloud_end_confirmed'):
                    return  # Positive end readback is monotonic; stale failures cannot erase it.
                if patch.get('owned_live_id') and s['media'].get('owned_live_id') not in (None,patch['owned_live_id']):raise PerformanceError('owned_live_id_conflict')
                s['media'].update(deepcopy(patch))
                if patch.get('state')=='unknown':s['status']='unknown'
                elif patch.get('state')=='ended':s['status']='paused'
                await self._save_session(conn,s,now);await self._revision(conn)

    async def persist_preparation(self,context,evidence):
        async with self.sender_lock:
            async with self._tx() as (conn,rev,now):
                b=await self._load(conn,context['performance_id'])
                if b['session_id']!=context['session_id'] or b['payload_hash']!=context['payload_hash']:raise PerformanceError('preparation_context_changed')
                i=context['segment_index'];segment=b['segments'][i]
                # Do not authorize a subsequent charge after cancellation. Return
                # evidence for a request already attempted is still retained.
                if evidence.get('phase') in ('stage1','tts','stage2') and (b['state'] in TERMINAL or b['cancel_requested'] or not self.runtime.ready(b['session_id'])):
                    raise PerformanceError('preparation_gate_closed')
                segment['preparation']=deepcopy(evidence)
                segment.setdefault('preparation_history',[]).append(deepcopy(evidence))
                await self._save(conn,b,now);await self._revision(conn)

    def _launch_media(self,awaitable):
        task=asyncio.create_task(awaitable);self.media_tasks.add(task);task.add_done_callback(self.media_tasks.discard)

    async def media_command(self,id,action,data):
        uuid(id)
        if action=='start':fields(data,'command_id confirm_paid')
        elif action=='confirm-ready':fields(data,'command_id receiver_id confirm_audio_video');uuid(data['receiver_id'])
        elif action in ('stop','reconcile'):fields(data,'command_id')
        else:raise ValidationError('media action 无效。')
        launch=False;owned=None
        async def op(conn,rev,now):
            nonlocal launch,owned
            s=await self._session(conn,id)
            if s['mode']!='real' or s['status']=='closed':raise PerformanceError('real_session_required')
            media=s['media'];owned=media.get('owned_live_id')
            if action=='start':
                if data['confirm_paid'] is not True:raise ValidationError('需要明确收费确认。')
                self.runtime.ensure_configured()
                if media.get('creation_attempted'):raise PerformanceError('media_already_attempted')
                media.update(state='creating',creation_attempted=True);s['status']='paused';launch=True
            elif action in ('stop','reconcile'):
                self.runtime.pause()
                if s['status']!='unknown':s['status']='paused'
                if action=='stop' and self.runtime.component is not None:self.runtime.component.latch_stop()
                await self._pause_rows(conn,id,now)
                if not media.get('creation_attempted'):
                    media.update(state='not_started',cloud_end_confirmed=False);s['status']='paused'
                else:
                    media['state']='stopping' if action=='stop' else 'unknown';launch=True
            else:
                if data['confirm_audio_video'] is not True:raise ValidationError('需要实际音画确认。')
                self.runtime.confirm(id,data['receiver_id'])
                media['operator_confirmed']=True
            await self._save_session(conn,s,now)
            return self._snapshot(await self._revision(conn),now,session=s)
        result,replayed=await self._command(data['command_id'],{'action':'media_'+action,'session_id':id,**data},op)
        if launch and not replayed:
            async def execute():
                try:
                    if action=='start':
                        async def created(live_id,metadata):await self._update_media(id,{'owned_live_id':live_id,'state':'connecting'})
                        await self.runtime.start(id,created,lambda patch:self._update_media(id,patch))
                    else:
                        patch=await (self.runtime.stop(id,owned) if action=='stop' else self.runtime.reconcile(id,owned))
                        await self._update_media(id,patch)
                except BaseException:
                    if self.runtime.session_id==id:self.runtime.pause()
                    try:await self._update_media(id,{'state':'unknown','cloud_end_confirmed':False})
                    except BaseException:await self.close_gate()
            self._launch_media(execute())
        return result

    async def claim_receiver(self,id,data):
        uuid(id);fields(data,'receiver_id');uuid(data['receiver_id'])
        async with self.sender_lock:
            async with self._tx() as (conn,rev,now):
                s=await self._session(conn,id)
                if s['mode']!='real' or s['status']=='closed' or not s['media'].get('owned_live_id'):raise PerformanceError('rtc_not_ready')
                claim=self.runtime.claim(id,data['receiver_id'])
                s['status']='paused';await self._pause_rows(conn,id,now)
                await self._save_session(conn,s,now);rev=await self._revision(conn)
                # Ephemeral token deliberately never written to DB or command receipt.
                return self._snapshot(rev,now,**claim)

    async def receiver_heartbeat(self,id,data):
        uuid(id);fields(data,'receiver_id receiver_token joined remote_video audio_playback autoplay_blocked');uuid(data['receiver_id'])
        if not isinstance(data['receiver_token'],str) or len(data['receiver_token'])>256:raise ValidationError('receiver token 无效。')
        for key in ('joined','remote_video','audio_playback','autoplay_blocked'):
            if type(data[key]) is not bool:raise ValidationError('receiver 状态须为布尔值。')
        async with self.sender_lock:
            async with self._tx() as (conn,rev,now):
                s=await self._session(conn,id)
                self.runtime.heartbeat(id,data)
                if not self.runtime.receiver_healthy() or self.runtime.paused:
                    if s['status']!='unknown':s['status']='paused'
                    await self._pause_rows(conn,id,now)
                await self._save_session(conn,s,now)
                return self._snapshot(await self._revision(conn),now,session=s)

    async def media_tick(self):
        self.runtime.receiver_healthy()  # May latch expiry even after an earlier view.
        if self.runtime.needs_pause_sync:
            await self.pause_for_game()
            self.runtime.needs_pause_sync=False
