"""Controlled evaluation provenance only; no model requests or quality claims."""
import copy
import unittest
from unittest.mock import patch
from uuid import uuid4
from sqlalchemy import select
from hell_inc.app import STATE
from hell_inc import db
from hell_inc.competition_evaluation import POLICY, RULE, policy_for
from hell_inc.competitions import CompetitionService
from hell_inc.evaluation import content_hash
import test_m3_api as m3


class EvaluationProviderPolicyTests(unittest.TestCase):
    def test_default_exact_historical_policy_and_independent_copy(self):
        original=copy.deepcopy(POLICY)
        self.assertEqual(policy_for(),original)
        with patch.dict('os.environ',{},clear=True):
            self.assertEqual(CompetitionService(None,'offline').evaluation_policy,original)
        policy=policy_for();policy['instructions']='changed'
        self.assertEqual(POLICY,original)

    def test_bailian_changes_only_provider_model_and_hash_not_rubric(self):
        policy=policy_for('bailian_manual_api','qwen3-max')
        self.assertEqual(policy,{**POLICY,'provider':'bailian_manual_api','model':'qwen3-max'})
        self.assertNotEqual(content_hash(policy),content_hash(POLICY))
        with patch.dict('os.environ',{'HELL_INC_M3_EVALUATION_PROVIDER':'bailian_manual_api','HELL_INC_M3_EVALUATION_MODEL':'qwen3-max'}):
            self.assertEqual(CompetitionService(None,'offline').evaluation_policy,policy)
        self.assertEqual(policy['instructions'],POLICY['instructions'])

    def test_unknown_provider_missing_or_unsafe_model_rejected(self):
        for provider,model in [('other','qwen3-max'),('bailian_manual_api',None),('bailian_manual_api',''),('bailian_manual_api','a\nb'),('bailian_manual_api','x'*129),('chatgpt_native_manual','qwen3-max')]:
            with self.subTest(provider=provider,model=model),self.assertRaises(ValueError):policy_for(provider,model)


class EvaluationProviderPersistenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=m3.M3ApiTests.asyncSetUp
    restart=m3.M3ApiTests.restart
    post=m3.M3ApiTests.post
    get=m3.M3ApiTests.get
    register=m3.M3ApiTests.register
    start=m3.M3ApiTests.start
    snapshot=m3.M3ApiTests.snapshot
    command=m3.M3ApiTests.command
    submit_work=m3.M3ApiTests.submit
    export=m3.M3ApiTests.export

    async def test_new_round_freezes_bailian_metadata_old_tasks_remain_immutable(self):
        state=self.client.server.app[STATE]
        state['competitions']=CompetitionService(state['engine'],self.settings.world_id,evaluation_provider='chatgpt_native_manual',evaluation_model='')
        player=await self.register('provider-fixture')
        async def task():
            await self.start();await self.command('open')
            sid=(await self.submit_work(player))['submission_id']
            await self.command('close-submissions')
            return sid,await self.export(sid)
        sid,old=await task();old_id=self.cid
        self.assertEqual(old['frozen_input']['policy'],POLICY)
        async def body(cid):
            async with state['engine'].connect() as conn:
                return (await conn.execute(select(db.competitions.c.body).where(db.competitions.c.id==cid))).scalar_one()
        before=await body(old_id)
        state['competitions']=CompetitionService(state['engine'],self.settings.world_id,evaluation_provider='bailian_manual_api',evaluation_model='qwen3-max')
        self.assertEqual(await self.export(sid),old)
        self.assertEqual(await body(old_id),before)
        await self.command('cancel',reason='离线来源配置迁移，保留原任务')
        new_sid,new=await task()
        self.assertEqual(new['frozen_input']['policy'],policy_for('bailian_manual_api','qwen3-max'))
        self.assertEqual(new['frozen_input']['rule'],RULE)
        self.assertEqual(new['policy_hash'],content_hash(new['frozen_input']['policy']))
        self.assertEqual(new['input_hash'],content_hash(new['frozen_input']))
        self.assertNotEqual(new['policy_hash'],old['policy_hash'])
        state['competitions']=CompetitionService(state['engine'],self.settings.world_id,evaluation_provider='chatgpt_native_manual',evaluation_model='')
        self.assertEqual(await self.export(new_sid),new)
        await self.post('/api/competitions',{'command_id':str(uuid4()),'provider':'bailian_manual_api','model':'qwen3-max'},400)
        persisted=await body(old_id)
        self.assertEqual(persisted['policy'],before['policy'])
        self.assertEqual(persisted['policy_hash'],before['policy_hash'])
        self.assertEqual(persisted['submissions'][0]['task']['frozen_input'],old['frozen_input'])
        self.assertEqual(persisted['submissions'][0]['task']['input_hash'],old['input_hash'])
