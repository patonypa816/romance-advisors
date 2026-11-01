"""Read-only projection of already accepted/settled game data. No scoring/asset API."""
from copy import deepcopy
from sqlalchemy import select
from .db import interviews, competitions
from .performance_contract import (source_ref,canonical_hash,split_text,FIXED_SCRIPT,PerformanceError)

async def freeze_source(conn,world_id,source):
    source_ref(source,False)
    kind=source['kind']
    if kind=='fixed_script':
        segments=deepcopy(FIXED_SCRIPT); snapshot={'script_id':'m4-demo-v1','segments':segments}
        return {'source_ref':deepcopy(source),'segments':segments,'content_hash':canonical_hash(segments),'source_snapshot':snapshot}
    table=interviews if kind=='interview_result' else competitions
    row=(await conn.execute(select(table).where(table.c.world_id==world_id,table.c.id==source['id']))).mappings().first()
    if not row: raise PerformanceError('source_not_found',404)
    b=row['body']
    if kind=='interview_result':
        if b['status']!='COMPLETED' or not b.get('result'): raise PerformanceError('source_not_ready')
        snapshot={k:deepcopy(b.get(k)) for k in ('id','version','player','evaluation','result','task','policy_hash','rule_hash')}
        decision=b['result']['decision']
        speech='面试结果：'+('录用。' if decision=='hire' else '暂不录用。')
        segments=[{'text':s,'intent':'result'} for s in split_text(speech)]
    elif kind=='competition_comment':
        if b['status'] not in ('DISPLAYING','VOTING','READY_TO_SETTLE','COMPLETED') or not b['submissions'] or not all(s.get('task',{}).get('status')=='accepted' for s in b['submissions']): raise PerformanceError('source_not_ready')
        s=next((s for s in b['submissions'] if s['id']==source['submission_id']),None)
        if not s: raise PerformanceError('source_not_found',404)
        # No scores, winners, or other entrants in comment snapshot.
        snapshot={'id':b['id'],'version':b['version'],'submission_id':s['id'],'submission_revision':s['revision'],'comment':s['evaluation']['comment'],'task_id':s['task']['id'],'input_hash':s['task']['input_hash'],'policy_hash':s['task']['policy_hash']}
        segments=[{'text':t,'intent':'comment'} for t in split_text(s['evaluation']['comment'])]
    else:
        if b['status']!='COMPLETED' or not b.get('result'): raise PerformanceError('source_not_ready')
        snapshot={'id':b['id'],'version':b['version'],'result':deepcopy(b['result'])}
        names={s['id']:s['player']['display_name'] for s in b['submissions']}
        speech='比赛结果：'+''.join(('创意奖' if a['award_type']=='creativity' else '人气奖')+'，'+names[a['submission_id']]+'。' for a in b['result']['awards'])
        segments=[{'text':s,'intent':'result'} for s in split_text(speech)]
    ref={**source,'source_version':row['version'],'source_hash':canonical_hash(snapshot)}
    return {'source_ref':ref,'segments':segments,'content_hash':canonical_hash(segments),'source_snapshot':snapshot}
