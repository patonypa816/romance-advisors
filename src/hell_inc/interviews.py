"""One fixed interview, synchronous admission and native manual evaluation import."""
from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from uuid import UUID, uuid4
from sqlalchemy import select, update, text
from .db import (worlds, players, memberships, interviews, interview_commands,
                 input_records, model_tasks)
from .contracts import ValidationError
from .records.service import RecordService, PlayerNotFound, CommandConflict
from .evaluation import RULE, POLICY, content_hash, validate_evaluation

ACTIVE = ('READY','ANSWERING','EVALUATING','REVIEW_FAILED','PAUSED')


class InterviewError(ValueError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
        return value
    except (ValueError, AttributeError):
        raise ValidationError('ID 必须为规范 UUID。') from None


def fields(data, names):
    if not isinstance(data,dict) or set(data) != set(names.split()):
        raise ValidationError('请求字段不完整或包含未知字段。')
    for key in ('command_id','message_id','player_id','task_id'):
        if key in data:
            uuid(data[key])
    if 'expected_version' in data and (type(data['expected_version']) is not int or data['expected_version']<1):
        raise ValidationError('expected_version 必须为正整数。')
    return data


def stamp(value):
    return datetime.fromisoformat(value)


def actions(b):
    return {'READY':['open','pause','cancel'], 'ANSWERING':['answer','pause','cancel'],
        'EVALUATING':['pause','cancel'], 'REVIEW_FAILED':['retry','pause','cancel'],
        'PAUSED':['resume','cancel']}.get(b['status'],[])


def snapshot(b):
    return {k:deepcopy(v) for k,v in b.items() if k not in ('policy','rule_hash','policy_hash')} | {'allowed_actions':actions(b)}


class InterviewService:
    def __init__(self, engine, world_id):
        self.engine, self.world_id = engine, world_id
        self.records = RecordService(engine, world_id)

    @asynccontextmanager
    async def _command_transaction(self, command_id, payload):
        """Business rejection reserves the command too; transient DB failure does not."""
        failure = None
        async with self.engine.begin() as conn:
            try:
                yield conn
            except InterviewError as exc:
                if exc.status != 409:
                    raise
                old = (await conn.execute(select(interview_commands.c.command_id).where(
                    interview_commands.c.world_id == self.world_id,
                    interview_commands.c.command_id == command_id))).first()
                if not old:
                    now = (await conn.execute(text('SELECT clock_timestamp()'))).scalar_one()
                    await conn.execute(interview_commands.insert().values(world_id=self.world_id,
                        command_id=command_id, payload=payload,
                        response={'error':{'code':exc.code,'message':str(exc),'status':exc.status}},
                        created_at=now))
                failure = exc
        if failure:
            raise failure

    async def _lock(self, conn):
        revision = (await conn.execute(select(worlds.c.revision).where(
            worlds.c.world_id == self.world_id).with_for_update())).scalar_one()
        now = (await conn.execute(text('SELECT clock_timestamp()'))).scalar_one()
        return revision, now

    async def _load(self, conn, id):
        uuid(id)
        row = (await conn.execute(select(interviews).where(interviews.c.id == id,
            interviews.c.world_id == self.world_id))).mappings().first()
        if not row:
            raise InterviewError('interview_not_found',404)
        return deepcopy(row['body'])

    async def _replay(self, conn, table, key, id, payload):
        old = (await conn.execute(select(table).where(table.c.world_id == self.world_id,
            table.c[key] == id))).mappings().first()
        if old:
            if content_hash(old['payload']) != content_hash(payload):
                raise CommandConflict('同一 ID 不可用于不同目标、动作或原始载荷。')
            if 'error' in old['response']:
                problem=old['response']['error']
                if problem['code']=='validation_error':raise ValidationError(problem['message'])
                raise InterviewError(problem['code'],problem['status'])
            return {**old['response'],'replayed':True}

    async def _save(self, conn, b, revision):
        b['version'] += 1
        await conn.execute(update(interviews).where(interviews.c.id == b['id']).values(
            body=b, status=b['status'],version=b['version']))
        await conn.execute(update(worlds).where(worlds.c.world_id == self.world_id).values(revision=revision+1))
        return revision+1

    async def _receipt(self, conn, command_id, payload, b, revision, now):
        response = {'command_id':command_id,'replayed':False,'revision':revision,'interview':snapshot(b)}
        await conn.execute(interview_commands.insert().values(world_id=self.world_id,
            command_id=command_id,payload=payload,response=response,created_at=now))
        return response

    async def start(self,data):
        fields(data,'command_id player_id')
        payload = {'action':'start',**data}
        async with self._command_transaction(data['command_id'],payload) as conn:
            revision, now = await self._lock(conn)
            replay = await self._replay(conn,interview_commands,'command_id',data['command_id'],payload)
            if replay: return replay
            if (await conn.execute(select(interviews.c.id).where(interviews.c.world_id==self.world_id,
                interviews.c.status.in_(ACTIVE)))).first():
                raise InterviewError('active_interview_exists')
            p = (await conn.execute(select(players).where(players.c.world_id==self.world_id,
                players.c.player_id==data['player_id']))).mappings().first()
            if not p: raise PlayerNotFound(data['player_id'])
            if (await conn.execute(select(memberships).where(memberships.c.player_id==p['player_id'],
                memberships.c.role!='guest'))).first(): raise InterviewError('not_eligible')
            b = {'id':str(uuid4()),'version':0,'status':'READY','paused_from':None,'pause_reason':None,
                'player':{'player_id':p['player_id'],'display_name':p['display_name']},
                'rule':deepcopy(RULE),'policy':deepcopy(POLICY),'rule_hash':content_hash(RULE),
                'policy_hash':content_hash(POLICY),'opened_at':None,'deadline_at':None,
                'answer':None,'task':None,'evaluation':None,'result':None}
            await conn.execute(interviews.insert().values(id=b['id'],world_id=self.world_id,
                player_id=p['player_id'],status=b['status'],version=0,body=b,created_at=now))
            revision = await self._save(conn,b,revision)
            return await self._receipt(conn,data['command_id'],payload,b,revision,now)

    async def _task_outcome(self,conn,b,outcome,now):
        if not b['task']: return
        task = (await conn.execute(select(model_tasks).where(model_tasks.c.id==b['task']['id']))).mappings().one()
        attempts = deepcopy(task['attempts'])
        attempts.append({'attempt_no':b['task']['attempt_no'],'outcome':outcome,'at':now.isoformat()})
        await conn.execute(update(model_tasks).where(model_tasks.c.id==task['id']).values(attempts=attempts))
        b['task']['status']=outcome
        b['task']['error_code']=outcome if outcome!='accepted' else None

    async def command(self,id,action,data):
        fields(data,'command_id expected_version reason' if action=='cancel' else 'command_id expected_version')
        uuid(id)
        if action=='cancel' and (not isinstance(data['reason'],str) or not 1<=len(data['reason'])<=200
            or not data['reason'].strip() or '\x00' in data['reason']): raise ValidationError('取消原因长度 1–200。')
        payload={'action':action,'id':id,**data}
        async with self._command_transaction(data['command_id'],payload) as conn:
            revision,now=await self._lock(conn)
            replay=await self._replay(conn,interview_commands,'command_id',data['command_id'],payload)
            if replay:return replay
            b=await self._load(conn,id)
            if b['version']!=data['expected_version']:raise InterviewError('version_conflict')
            if action not in actions(b):raise InterviewError('state_conflict')
            if action=='open':
                b['status']='ANSWERING';b['opened_at']=now.isoformat()
                b['deadline_at']=(now+timedelta(seconds=b['rule']['answer_seconds'])).isoformat()
            elif action=='pause':
                b['paused_from']=b['status'];b['pause_reason']='operator';b['status']='PAUSED'
                if b['paused_from']=='EVALUATING':await self._task_outcome(conn,b,'interrupted',now)
            elif action=='resume':
                b['status']='REVIEW_FAILED' if b['paused_from']=='EVALUATING' else b['paused_from']
                b['paused_from']=None;b['pause_reason']=None
                if b['status']=='ANSWERING':
                    b['opened_at']=now.isoformat();b['deadline_at']=(now+timedelta(seconds=b['rule']['answer_seconds'])).isoformat()
            elif action=='retry':
                b['status']='EVALUATING';b['task'].update(status='awaiting_result',
                    attempt_no=b['task']['attempt_no']+1,deadline_at=(now+timedelta(minutes=15)).isoformat(),error_code=None)
            elif action=='cancel':
                if b['status']=='EVALUATING':await self._task_outcome(conn,b,'interrupted',now)
                b['status']='CANCELLED';b['pause_reason']=data['reason']
            revision=await self._save(conn,b,revision)
            return await self._receipt(conn,data['command_id'],payload,b,revision,now)

    async def answer(self,id,data):
        fields(data,'message_id player_id text');uuid(id)
        answer=data['text']
        if not isinstance(answer,str) or not 1<=len(answer)<=1000 or not answer.strip() or '\x00' in answer:
            raise ValidationError('答案必须为 1–1000 字符非空文本。')
        payload={'id':id,'provider':'simulated','identity_scope':'local-test',**data}
        async with self.engine.begin() as conn:
            revision,now=await self._lock(conn)
            replay=await self._replay(conn,input_records,'message_id',data['message_id'],payload)
            if replay:return replay
            b=await self._load(conn,id)
            rejection=None
            if data['player_id']!=b['player']['player_id']:rejection='wrong_player'
            elif b['answer']:rejection='answer_already_submitted'
            elif b['status']=='PAUSED':rejection='paused'
            elif b['status']=='EXPIRED':rejection='deadline_passed'
            elif b['status']!='ANSWERING':rejection='not_open'
            elif now>=stamp(b['deadline_at']):
                rejection='deadline_passed';b['status']='EXPIRED';revision=await self._save(conn,b,revision)
            if rejection is None:
                b['answer']={'text':answer,'admitted_at':now.isoformat()};b['status']='EVALUATING'
                frozen={'rule':b['rule'],'policy':b['policy'],'raw_answer':answer}
                task_id=str(uuid4())
                b['task']={'id':task_id,'status':'awaiting_result','attempt_no':1,
                    'deadline_at':(now+timedelta(minutes=15)).isoformat(),'error_code':None}
                await conn.execute(model_tasks.insert().values(id=task_id,interview_id=id,
                    frozen_input=frozen,input_hash=content_hash(frozen),policy_hash=content_hash(b['policy']),attempts=[]))
                revision=await self._save(conn,b,revision)
            response={'input_id':data['message_id'],'accepted':rejection is None,'rejection_code':rejection,
                'replayed':False,'interview_id':id,'revision':revision}
            await conn.execute(input_records.insert().values(world_id=self.world_id,message_id=data['message_id'],
                payload=payload,response=response,created_at=now))
            return response

    async def evaluation_request(self,id):
        async with self.engine.connect() as conn:
            conn=await conn.execution_options(isolation_level='REPEATABLE READ')
            async with conn.begin():
                b=await self._load(conn,id)
                now=(await conn.execute(text('SELECT clock_timestamp()'))).scalar_one()
                if b['status']!='EVALUATING' or b['task']['status']!='awaiting_result' or now>=stamp(b['task']['deadline_at']):
                    raise InterviewError('state_conflict')
                task=(await conn.execute(select(model_tasks).where(model_tasks.c.id==b['task']['id']))).mappings().one()
                return {'task_id':task['id'],'attempt_no':b['task']['attempt_no'],
                    'input_hash':task['input_hash'],'policy_hash':task['policy_hash'],'frozen_input':task['frozen_input']}

    async def evaluation_result(self,id,data):
        fields(data,'command_id task_id attempt_no input_hash policy_hash evaluation');uuid(id)
        if type(data['attempt_no']) is not int or data['attempt_no']<1:
            raise ValidationError('attempt_no 必须为正整数。')
        for key in ('input_hash','policy_hash'):
            value=data[key]
            if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
                raise ValidationError('hash 必须为 SHA256。')
        payload={'action':'evaluation-result','id':id,**data}
        failure=None
        async with self._command_transaction(data['command_id'],payload) as conn:
            revision,now=await self._lock(conn)
            replay=await self._replay(conn,interview_commands,'command_id',data['command_id'],payload)
            if replay:return replay
            b=await self._load(conn,id)
            if b['status']!='EVALUATING' or b['task']['status']!='awaiting_result':raise InterviewError('state_conflict')
            task=(await conn.execute(select(model_tasks).where(model_tasks.c.id==b['task']['id']))).mappings().one()
            current={'rule':b['rule'],'policy':b['policy'],'raw_answer':b['answer']['text']}
            if (data['task_id']!=task['id'] or data['attempt_no']!=b['task']['attempt_no']
                or data['input_hash']!=task['input_hash'] or data['policy_hash']!=task['policy_hash']
                or content_hash(b['rule'])!=b['rule_hash'] or content_hash(b['policy'])!=b['policy_hash']
                or content_hash(current)!=task['input_hash'] or content_hash(b['policy'])!=task['policy_hash']):
                raise InterviewError('state_conflict')
            if now>=stamp(b['task']['deadline_at']):
                failure=InterviewError('evaluation_expired')
                await self._task_outcome(conn,b,'timeout',now)
            else:
                try:evaluation=validate_evaluation(data['evaluation'],b['answer']['text'])
                except ValidationError as exc:
                    failure=exc
                    await self._task_outcome(conn,b,'invalid_evidence' if str(exc).startswith('invalid_evidence') else 'invalid_schema',now)
            if failure:
                b['status']='REVIEW_FAILED'
                await self._save(conn,b,revision)
                problem={'code':'validation_error' if isinstance(failure,ValidationError) else failure.code,
                    'message':str(failure),'status':400 if isinstance(failure,ValidationError) else failure.status}
                await conn.execute(interview_commands.insert().values(world_id=self.world_id,
                    command_id=data['command_id'],payload=payload,response={'error':problem},created_at=now))
            else:
                scores={r['id']:r['score'] for r in evaluation['criteria']}
                decision='hire' if evaluation['total']>=4 and scores['empathy']>=1 and scores['solution']>=1 else 'reject'
                b['evaluation']=evaluation
                plan={'decision':decision,'department':b['rule']['department'],'position':b['rule']['position'],
                    'evaluation':evaluation,'input_hash':task['input_hash'],'policy_hash':task['policy_hash']}
                b['result']=await self.records.apply_interview_result(conn,self.world_id,id,b['player']['player_id'],plan)
                await self._task_outcome(conn,b,'accepted',now)
                b['status']='COMPLETED'
                revision=await self._save(conn,b,revision)
                response=await self._receipt(conn,data['command_id'],payload,b,revision,now)
        if failure:raise failure
        return response

    async def _read(self,id=None):
        async with self.engine.connect() as conn:
            conn=await conn.execution_options(isolation_level='REPEATABLE READ')
            async with conn.begin():
                revision=(await conn.execute(select(worlds.c.revision).where(worlds.c.world_id==self.world_id))).scalar_one()
                now=(await conn.execute(text('SELECT clock_timestamp()'))).scalar_one()
                result={'revision':revision,'server_time':now.isoformat()}
                if id is not None:result['interview']=snapshot(await self._load(conn,id))
                else:
                    rows=(await conn.execute(select(interviews.c.body).where(interviews.c.world_id==self.world_id)
                        .order_by(interviews.c.created_at.desc()).limit(20))).scalars().all()
                    result['interviews']=[snapshot(b) for b in rows]
                return result

    async def get(self,id):return await self._read(id)
    async def list_interviews(self):return await self._read()

    async def recover(self,reason='restart'):
        async with self.engine.begin() as conn:
            revision,now=await self._lock(conn)
            rows=(await conn.execute(select(interviews.c.body).where(interviews.c.world_id==self.world_id,
                interviews.c.status.in_(ACTIVE)))).scalars().all()
            for raw in rows:
                b=deepcopy(raw)
                if b['status']=='PAUSED':continue
                b['paused_from']=b['status'];b['pause_reason']=reason;b['status']='PAUSED'
                if b['paused_from']=='EVALUATING':await self._task_outcome(conn,b,'interrupted',now)
                revision=await self._save(conn,b,revision)

    async def expire_due(self):
        async with self.engine.begin() as conn:
            revision,now=await self._lock(conn)
            rows=(await conn.execute(select(interviews.c.body).where(interviews.c.world_id==self.world_id,
                interviews.c.status.in_(['ANSWERING','EVALUATING'])))).scalars().all()
            for raw in rows:
                b=deepcopy(raw)
                if b['status']=='ANSWERING' and now>=stamp(b['deadline_at']):
                    b['status']='EXPIRED';revision=await self._save(conn,b,revision)
                elif b['status']=='EVALUATING' and now>=stamp(b['task']['deadline_at']):
                    await self._task_outcome(conn,b,'timeout',now)
                    b['status']='REVIEW_FAILED';revision=await self._save(conn,b,revision)
