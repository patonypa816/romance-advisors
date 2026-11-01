"""Bounded, whole-round competition. Every mutation holds the existing world lock."""
from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4
from sqlalchemy import select, update, text
from .db import worlds, players, memberships, competitions, competition_commands, input_records
from .contracts import ValidationError
from .interviews import uuid, fields, stamp, InterviewError
from .evaluation import content_hash
from .competition_evaluation import RULE, POLICY, validate_evaluation
from .records.service import RecordService, PlayerNotFound, CommandConflict

ACTIVE=('READY','SUBMITTING','REVIEWING','WAITING_REVIEW','DISPLAYING','VOTING','READY_TO_SETTLE','PAUSED')
class CompetitionError(InterviewError): pass

def envelope(data,key):
    if not isinstance(data,dict) or key not in data:raise ValidationError('缺少请求 ID。')
    uuid(data[key])


def actions(b):
    return {'READY':['open','pause','cancel'],'SUBMITTING':['submit','close-submissions','pause','cancel'],
        'REVIEWING':['pause','cancel'],'WAITING_REVIEW':['retry','pause','cancel'],
        'DISPLAYING':['confirm-display','open-voting','pause','cancel'],
        'VOTING':['vote','close-voting','pause','cancel'],'READY_TO_SETTLE':['settle','pause','cancel'],
        'PAUSED':['resume','cancel']}.get(b['status'],[])

def snapshot(b):
    result={k:deepcopy(b[k]) for k in ('id','version','status','paused_from','pause_reason','rule','opened_at','deadline_at','result')}
    visible=b['status'] in ('DISPLAYING','VOTING','READY_TO_SETTLE','COMPLETED') or (b['status']=='PAUSED' and b['paused_from'] in ('DISPLAYING','VOTING','READY_TO_SETTLE'))
    result['submissions']=[]
    for s in b['submissions']:
        v={k:deepcopy(s[k]) for k in ('id','player','revision','text','submitted_at','display_confirmed_at')}
        v['task']={k:s['task'][k] for k in ('id','status','attempt_no','deadline_at','error_code')} if s['task'] else None
        v['comment']=s['evaluation']['comment'] if visible and s['evaluation'] else None
        v['evaluation']={k:deepcopy(s['evaluation'][k]) for k in ('criteria','total')} if b['status']=='COMPLETED' else None
        result['submissions'].append(v)
    result['votes']=[{k:v[k] for k in ('player_id','submission_id','revision')} for v in b['votes']] if b['status']=='COMPLETED' else None
    result['allowed_actions']=actions(b)
    return result

class CompetitionService:
    def __init__(self,engine,world_id):
        self.engine,self.world_id=engine,world_id
        self.records=RecordService(engine,world_id)

    @asynccontextmanager
    async def _command_transaction(self,command_id,payload,table=competition_commands,key='command_id'):
        failure=None
        async with self.engine.begin() as conn:
            try: yield conn
            except CompetitionError as exc:
                if exc.status not in (404,409): raise
                old=(await conn.execute(select(table.c[key]).where(table.c.world_id==self.world_id,table.c[key]==command_id))).first()
                if not old:
                    now=(await conn.execute(text('SELECT clock_timestamp()'))).scalar_one()
                    await self._error_receipt(conn,command_id,payload,exc,now,table,key)
                failure=exc
        if failure: raise failure

    async def _lock(self,conn):
        revision=(await conn.execute(select(worlds.c.revision).where(worlds.c.world_id==self.world_id).with_for_update())).scalar_one()
        return revision,(await conn.execute(text('SELECT clock_timestamp()'))).scalar_one()

    async def _load(self,conn,id):
        uuid(id)
        row=(await conn.execute(select(competitions.c.body).where(competitions.c.world_id==self.world_id,competitions.c.id==id))).scalar_one_or_none()
        if row is None: raise CompetitionError('competition_not_found',404)
        return deepcopy(row)

    async def _player(self,conn,id):
        p=(await conn.execute(select(players).where(players.c.world_id==self.world_id,players.c.player_id==id))).mappings().first()
        if not p: raise PlayerNotFound(id)
        m=(await conn.execute(select(memberships).where(memberships.c.world_id==self.world_id,memberships.c.player_id==id))).mappings().first()
        return {'player_id':id,'display_name':p['display_name']}, ({k:m[k] for k in ('role','department','position','performance')} if m else {'role':'guest','department':None,'position':None,'performance':0})

    async def _replay(self,conn,table,key,id,payload):
        old=(await conn.execute(select(table).where(table.c.world_id==self.world_id,table.c[key]==id))).mappings().first()
        if old:
            if content_hash(old['payload'])!=content_hash(payload): raise CommandConflict('同一 ID 不可改变载荷。')
            if 'error' in old['response']:
                e=old['response']['error']
                if e['code']=='validation_error': raise ValidationError(e['message'])
                raise CompetitionError(e['code'],e['status'])
            return {**old['response'],'replayed':True}

    async def _save(self,conn,b,revision):
        b['version']+=1
        await conn.execute(update(competitions).where(competitions.c.id==b['id']).values(body=b,status=b['status'],version=b['version']))
        await conn.execute(update(worlds).where(worlds.c.world_id==self.world_id).values(revision=revision+1))
        return revision+1

    async def _receipt(self,conn,id,payload,b,revision,now):
        response={'command_id':id,'replayed':False,'revision':revision,'competition':snapshot(b)}
        await conn.execute(competition_commands.insert().values(world_id=self.world_id,command_id=id,payload=payload,response=response,created_at=now))
        return response

    async def _error_receipt(self,conn,id,payload,exc,now,table=competition_commands,key='command_id'):
        problem={'code':'validation_error' if isinstance(exc,ValidationError) else exc.code,'message':str(exc),'status':400 if isinstance(exc,ValidationError) else exc.status}
        await conn.execute(table.insert().values(world_id=self.world_id,**{key:id},payload=payload,response={'error':problem},created_at=now))

    async def start(self,data):
        envelope(data,'command_id');payload={'action':'competition-start','data':data}
        async with self._command_transaction(data['command_id'],payload) as conn:
            revision,now=await self._lock(conn)
            replay=await self._replay(conn,competition_commands,'command_id',data['command_id'],payload)
            if replay:return replay
            fields(data,'command_id')
            if (await conn.execute(select(competitions.c.id).where(competitions.c.world_id==self.world_id,competitions.c.status.in_(ACTIVE)))).first(): raise CompetitionError('active_competition_exists')
            b={'id':str(uuid4()),'version':0,'status':'READY','paused_from':None,'pause_reason':None,'rule':deepcopy(RULE),'policy':deepcopy(POLICY),'rule_hash':content_hash(RULE),'policy_hash':content_hash(POLICY),'opened_at':None,'deadline_at':None,'submissions':[],'votes':[],'result':None}
            await conn.execute(competitions.insert().values(id=b['id'],world_id=self.world_id,status='READY',version=0,body=b,created_at=now))
            revision=await self._save(conn,b,revision)
            return await self._receipt(conn,data['command_id'],payload,b,revision,now)

    def _submission(self,b,id):
        uuid(id)
        s=next((s for s in b['submissions'] if s['id']==id),None)
        if not s:raise CompetitionError('submission_not_found',404)
        return s

    def _outcome(self,s,outcome,now):
        t=s['task'];t['attempts'].append({'attempt_no':t['attempt_no'],'outcome':outcome,'at':now.isoformat()})
        t['status']=outcome;t['error_code']=None if outcome=='accepted' else outcome

    def _close_submissions(self,b,now):
        b['deadline_at']=None
        if not b['submissions']:b['status']='EXPIRED';return
        b['status']='REVIEWING'
        for s in b['submissions']:
            frozen={'rule':deepcopy(b['rule']),'policy':deepcopy(b['policy']),'submission':{k:s[k] for k in ('id','revision','text')}}
            s['task']={'id':str(uuid4()),'status':'awaiting_result','attempt_no':1,'deadline_at':(now+timedelta(seconds=b['rule']['evaluation_seconds'])).isoformat(),'error_code':None,'frozen_input':frozen,'input_hash':content_hash(frozen),'policy_hash':content_hash(b['policy']),'attempts':[]}

    def _pause(self,b,reason,now):
        b['paused_from']=b['status'];b['status']='PAUSED';b['pause_reason']=reason
        for s in b['submissions']:
            if s['task'] and s['task']['status']=='awaiting_result':self._outcome(s,'interrupted',now)

    def _expire(self,b,now):
        changed=False
        if b['status'] in ('SUBMITTING','VOTING') and now>=stamp(b['deadline_at']):
            if b['status']=='SUBMITTING':self._close_submissions(b,now)
            else:b['status']='READY_TO_SETTLE';b['deadline_at']=None
            changed=True
        if b['status'] in ('REVIEWING','WAITING_REVIEW'):
            for s in b['submissions']:
                if s['task']['status']=='awaiting_result' and now>=stamp(s['task']['deadline_at']):
                    self._outcome(s,'timeout',now);b['status']='WAITING_REVIEW';changed=True
        return changed

    async def command(self,id,action,data):
        uuid(id);envelope(data,'command_id')
        payload={'action':action,'competition_id':id,'data':data}
        async with self._command_transaction(data['command_id'],payload) as conn:
            revision,now=await self._lock(conn)
            replay=await self._replay(conn,competition_commands,'command_id',data['command_id'],payload)
            if replay:return replay
            fields(data,'command_id expected_version reason' if action=='cancel' else 'command_id expected_version')
            if action=='cancel' and (not isinstance(data['reason'],str) or not 1<=len(data['reason'])<=200 or not data['reason'].strip() or '\x00' in data['reason']):raise ValidationError('取消原因必须为 1–200 字符。')
            b=await self._load(conn,id)
            if b['version']!=data['expected_version']:raise CompetitionError('version_conflict')
            if action not in ('open','close-submissions','open-voting','close-voting','settle','pause','resume','cancel') or action not in actions(b):raise CompetitionError('state_conflict')
            if action=='open':
                b['status']='SUBMITTING';b['opened_at']=now.isoformat();b['deadline_at']=(now+timedelta(seconds=b['rule']['submission_seconds'])).isoformat()
            elif action=='close-submissions':self._close_submissions(b,now)
            elif action=='open-voting':
                if not all(s['display_confirmed_at'] and s['task']['status']=='accepted' for s in b['submissions']):raise CompetitionError('not_all_displayed')
                b['status']='VOTING';b['deadline_at']=(now+timedelta(seconds=b['rule']['vote_seconds'])).isoformat()
            elif action=='close-voting':b['status']='READY_TO_SETTLE';b['deadline_at']=None
            elif action=='pause':self._pause(b,'operator',now)
            elif action=='resume':
                previous=b['paused_from'];b['status']='WAITING_REVIEW' if previous in ('REVIEWING','WAITING_REVIEW') else previous
                b['paused_from']=None;b['pause_reason']=None
                if b['status'] in ('SUBMITTING','VOTING'):
                    seconds=b['rule']['submission_seconds' if b['status']=='SUBMITTING' else 'vote_seconds']
                    b['deadline_at']=(now+timedelta(seconds=seconds)).isoformat()
            elif action=='cancel':
                for s in b['submissions']:
                    if s['task'] and s['task']['status']=='awaiting_result':self._outcome(s,'interrupted',now)
                b['status']='CANCELLED';b['pause_reason']=data['reason'];b['deadline_at']=None
            elif action=='settle':
                if not b['submissions'] or not all(s['evaluation'] and s['task']['status']=='accepted' and s['display_confirmed_at'] for s in b['submissions']):raise CompetitionError('state_conflict')
                counts={s['id']:0 for s in b['submissions']}
                for v in b['votes']:counts[v['submission_id']]+=1
                rankings=[{'submission_id':s['id'],'total':s['evaluation']['total'],'votes':counts[s['id']]} for s in b['submissions']]
                winners=[('creativity',max(b['submissions'],key=lambda s:s['evaluation']['total']))]
                if b['votes']:winners.append(('popularity',max(b['submissions'],key=lambda s:counts[s['id']])))
                awards=[{'player_id':s['player']['player_id'],'submission_id':s['id'],'award_type':kind,'performance_delta':1 if s['eligibility']['role']=='employee' else 0} for kind,s in winners]
                plan={'rule':b['rule'],'submissions':b['submissions'],'votes':b['votes'],'awards':awards,'rankings':rankings}
                b['result']=await self.records.apply_competition_result(conn,self.world_id,id,plan);b['status']='COMPLETED'
            revision=await self._save(conn,b,revision)
            return await self._receipt(conn,data['command_id'],payload,b,revision,now)

    async def submit(self,id,data):return await self._input(id,'submit',data)
    async def vote(self,id,data):return await self._input(id,'vote',data)

    async def _input(self,id,action,data):
        uuid(id)
        envelope(data,'message_id')
        payload={'action':action,'competition_id':id,'data':data}
        async with self._command_transaction(data['message_id'],payload,input_records,'message_id') as conn:
            revision,now=await self._lock(conn)
            replay=await self._replay(conn,input_records,'message_id',data['message_id'],payload)
            if replay:return replay
            fields(data,'message_id player_id expected_submission_revision text' if action=='submit' else 'message_id player_id submission_id expected_vote_revision')
            key='expected_submission_revision' if action=='submit' else 'expected_vote_revision'
            if type(data[key]) is not int or data[key]<0:raise ValidationError('revision 必须为非负整数。')
            if action=='submit':
                if not isinstance(data['text'],str) or not 1<=len(data['text'])<=1000 or not data['text'].strip() or '\x00' in data['text']:raise ValidationError('作品必须为 1–1000 字符。')
            else:uuid(data['submission_id'])
            b=await self._load(conn,id);rejection=None
            try:player,eligibility=await self._player(conn,data['player_id'])
            except PlayerNotFound:rejection='not_eligible'
            before=b['status'];expired=self._expire(b,now)
            if expired:revision=await self._save(conn,b,revision)
            wanted='SUBMITTING' if action=='submit' else 'VOTING'
            if rejection is None:
                if b['status']=='PAUSED':rejection='paused'
                elif expired and before==wanted:rejection='deadline_passed'
                elif b['status']!=wanted:rejection='deadline_passed' if b['status']=='EXPIRED' else 'not_open'
            item=None
            if rejection is None and action=='submit':
                item=next((s for s in b['submissions'] if s['player']['player_id']==data['player_id']),None)
                if data[key]!=(item['revision'] if item else 0):rejection='stale_submission_revision'
                elif not item and len(b['submissions'])>=b['rule']['max_submissions']:rejection='capacity_reached'
                else:
                    if not item:
                        item={'id':str(uuid4()),'player':player,'eligibility':eligibility,'revision':0,'text':'','submitted_at':now.isoformat(),'revisions':[],'task':None,'evaluation':None,'display_confirmed_at':None}
                        b['submissions'].append(item)
                    item['revision']+=1;item['text']=data['text']
                    item['revisions'].append({'revision':item['revision'],'message_id':data['message_id'],'text':data['text'],'admitted_at':now.isoformat()})
            elif rejection is None:
                item=next((v for v in b['votes'] if v['player_id']==data['player_id']),None)
                if data[key]!=(item['revision'] if item else 0):rejection='stale_vote_revision'
                elif data['submission_id'] not in {s['id'] for s in b['submissions']}:rejection='invalid_candidate'
                else:
                    if not item:
                        item={'player_id':data['player_id'],'player':player,'eligibility':eligibility,'revision':0,'revisions':[]}
                        b['votes'].append(item)
                    item['revision']+=1;item['submission_id']=data['submission_id']
                    item['revisions'].append({'revision':item['revision'],'message_id':data['message_id'],'submission_id':data['submission_id'],'admitted_at':now.isoformat()})
            if rejection is None:revision=await self._save(conn,b,revision)
            response={'input_id':data['message_id'],'accepted':rejection is None,'rejection_code':rejection,'replayed':False,'competition_id':id,'revision':revision,'submission_id':item['id'] if item and action=='submit' else (item['submission_id'] if item else None),'submission_revision':item['revision'] if item and action=='submit' else None,'vote_revision':item['revision'] if item and action=='vote' else None}
            await conn.execute(input_records.insert().values(world_id=self.world_id,message_id=data['message_id'],payload=payload,response=response,created_at=now))
            return response

    async def submission_command(self,id,submission_id,action,data):
        uuid(id);uuid(submission_id);envelope(data,'command_id')
        payload={'action':action,'competition_id':id,'submission_id':submission_id,'data':data}
        async with self._command_transaction(data['command_id'],payload) as conn:
            revision,now=await self._lock(conn)
            replay=await self._replay(conn,competition_commands,'command_id',data['command_id'],payload)
            if replay:return replay
            fields(data,'command_id expected_version')
            b=await self._load(conn,id);s=self._submission(b,submission_id)
            if b['version']!=data['expected_version']:raise CompetitionError('version_conflict')
            if action=='retry':
                if b['status'] not in ('REVIEWING','WAITING_REVIEW') or not s['task'] or s['task']['status'] not in ('timeout','invalid_schema','invalid_evidence','interrupted'):raise CompetitionError('state_conflict')
                s['task'].update(status='awaiting_result',attempt_no=s['task']['attempt_no']+1,deadline_at=(now+timedelta(seconds=b['rule']['evaluation_seconds'])).isoformat(),error_code=None)
                b['status']='WAITING_REVIEW' if any(x['task']['status'] not in ('accepted','awaiting_result') for x in b['submissions']) else 'REVIEWING'
            elif action=='confirm-display':
                if b['status']!='DISPLAYING' or not all(x['task']['status']=='accepted' for x in b['submissions']):raise CompetitionError('state_conflict')
                if not s['display_confirmed_at']:s['display_confirmed_at']=now.isoformat()
            else:raise CompetitionError('state_conflict')
            revision=await self._save(conn,b,revision)
            return await self._receipt(conn,data['command_id'],payload,b,revision,now)

    async def evaluation_request(self,id,submission_id):
        async with self.engine.connect() as conn:
            conn=await conn.execution_options(isolation_level='REPEATABLE READ')
            async with conn.begin():
                b=await self._load(conn,id);s=self._submission(b,submission_id);t=s['task']
                now=(await conn.execute(text('SELECT clock_timestamp()'))).scalar_one()
                if b['status'] not in ('REVIEWING','WAITING_REVIEW') or not t or t['status']!='awaiting_result' or now>=stamp(t['deadline_at']):raise CompetitionError('state_conflict')
                return {'task_id':t['id'],'attempt_no':t['attempt_no'],'input_hash':t['input_hash'],'policy_hash':t['policy_hash'],'frozen_input':t['frozen_input']}

    async def evaluation_result(self,id,submission_id,data):
        uuid(id);uuid(submission_id);envelope(data,'command_id')
        payload={'action':'evaluation-result','competition_id':id,'submission_id':submission_id,'data':data};failure=None
        async with self._command_transaction(data['command_id'],payload) as conn:
            revision,now=await self._lock(conn)
            replay=await self._replay(conn,competition_commands,'command_id',data['command_id'],payload)
            if replay:return replay
            fields(data,'command_id task_id attempt_no input_hash policy_hash evaluation')
            if type(data['attempt_no']) is not int or data['attempt_no']<1:raise ValidationError('attempt_no 必须为正整数。')
            for key in ('input_hash','policy_hash'):
                if not isinstance(data[key],str) or len(data[key])!=64 or any(c not in '0123456789abcdef' for c in data[key]):raise ValidationError('hash 必须为 SHA256。')
            b=await self._load(conn,id);s=self._submission(b,submission_id);t=s['task']
            if b['status'] not in ('REVIEWING','WAITING_REVIEW') or not t or t['status']!='awaiting_result':raise CompetitionError('state_conflict')
            current={'rule':b['rule'],'policy':b['policy'],'submission':{k:s[k] for k in ('id','revision','text')}}
            if (data['task_id']!=t['id'] or data['attempt_no']!=t['attempt_no'] or data['input_hash']!=t['input_hash'] or data['policy_hash']!=t['policy_hash'] or content_hash(current)!=t['input_hash'] or content_hash(b['rule'])!=b['rule_hash'] or content_hash(b['policy'])!=b['policy_hash'] or content_hash(b['policy'])!=t['policy_hash'] or content_hash(t['frozen_input'])!=t['input_hash']):raise CompetitionError('state_conflict')
            if now>=stamp(t['deadline_at']):failure=CompetitionError('evaluation_expired');self._outcome(s,'timeout',now)
            else:
                try:evaluation=validate_evaluation(data['evaluation'],s['text'])
                except ValidationError as exc:
                    failure=exc;self._outcome(s,'invalid_evidence' if str(exc).startswith('invalid_evidence') else 'invalid_schema',now)
            if failure:
                b['status']='WAITING_REVIEW';await self._save(conn,b,revision)
                await self._error_receipt(conn,data['command_id'],payload,failure,now)
            else:
                s['evaluation']=evaluation;self._outcome(s,'accepted',now)
                if all(x['task']['status']=='accepted' for x in b['submissions']):b['status']='DISPLAYING'
                else:self._expire(b,now)
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
                if id is not None:result['competition']=snapshot(await self._load(conn,id))
                else:
                    rows=(await conn.execute(select(competitions.c.body).where(competitions.c.world_id==self.world_id).order_by(competitions.c.created_at.desc()).limit(20))).scalars().all()
                    result['competitions']=[snapshot(b) for b in rows]
                return result

    async def get(self,id):return await self._read(id)
    async def list_competitions(self):return await self._read()

    async def vote_state(self,id,player_id):
        uuid(player_id)
        async with self.engine.connect() as conn:
            conn=await conn.execution_options(isolation_level='REPEATABLE READ')
            async with conn.begin():
                b=await self._load(conn,id);await self._player(conn,player_id)
                v=next((v for v in b['votes'] if v['player_id']==player_id),None)
                return {'player_id':player_id,'vote_revision':v['revision'] if v else 0,'submission_id':v['submission_id'] if v else None}

    async def recover(self,reason='restart'):
        async with self.engine.begin() as conn:
            revision,now=await self._lock(conn)
            rows=(await conn.execute(select(competitions.c.body).where(competitions.c.world_id==self.world_id,competitions.c.status.in_(ACTIVE)))).scalars().all()
            for raw in rows:
                b=deepcopy(raw)
                if b['status']=='PAUSED':continue
                self._pause(b,reason,now);revision=await self._save(conn,b,revision)

    async def expire_due(self):
        async with self.engine.begin() as conn:
            revision,now=await self._lock(conn)
            rows=(await conn.execute(select(competitions.c.body).where(competitions.c.world_id==self.world_id,competitions.c.status.in_(ACTIVE)))).scalars().all()
            for raw in rows:
                b=deepcopy(raw)
                if self._expire(b,now):revision=await self._save(conn,b,revision)
