"""Freeze actual accepted M2/M3 sources without re-evaluation or asset mutation."""
import copy
import unittest
from uuid import uuid4
from sqlalchemy import select, update
from hell_inc import db
from hell_inc.app import STATE
from hell_inc.performance_contract import canonical_hash
from test_m4_api import M4Fixture
from test_m4_contract import signed_payload
import test_m3_api as m3
from test_m2_api import ANSWER, evaluation as m2_evaluation


class M4SourceTests(M4Fixture):
    # Reuse only fixture helpers, not old test cases: all 65 existing tests stay separate.
    register = m3.M3ApiTests.register
    start = m3.M3ApiTests.start
    snapshot = m3.M3ApiTests.snapshot
    command = m3.M3ApiTests.command
    subcommand = m3.M3ApiTests.subcommand
    submit_work = m3.M3ApiTests.submit
    export = m3.M3ApiTests.export
    result_payload = m3.M3ApiTests.result_payload
    accept = m3.M3ApiTests.accept
    voting = m3.M3ApiTests.voting

    async def game_rows(self):
        # Full old tables, excluding world revision and login/session-only state.
        async with self.client.server.app[STATE]['engine'].connect() as conn:
            return {t.name: [dict(r) for r in (await conn.execute(select(t))).mappings()]
                    for t in db.metadata.sorted_tables if t.name not in ('schema_versions', 'worlds') and not t.name.startswith('performance')}

    async def freeze(self, source, status=200):
        return await self.post('/api/performance-sources/freeze', {'source': source}, status)

    async def perform_frozen(self, frozen):
        body = signed_payload(self.sid, source_ref=frozen['source_ref'], segments=frozen['segments'])
        before = await self.game_rows()
        received = await self.post('/api/performances', body, 201)
        self.assertEqual(canonical_hash(received['performance']['source_snapshot']), frozen['source_ref']['source_hash'])
        await self.action(body['performance_id'], 'execute'); await self.drained()
        self.assertEqual((await self.record(body['performance_id']))['state'], 'send_completed')
        self.assertEqual(await self.game_rows(), before)
        return body

    async def test_m2_authoritative_result_freeze_immutable_assets_and_spoof_rejection(self):
        player = await self.register('m2-source')
        response = await self.post('/api/interviews', {'command_id': str(uuid4()), 'player_id': player})
        interview = response['interview']; path = '/api/interviews/' + interview['id']
        source = {'kind': 'interview_result', 'id': interview['id']}
        await self.freeze(source, 409)
        await self.post(path + '/open', {'command_id': str(uuid4()), 'expected_version': interview['version']})
        await self.post(path + '/answers', {'message_id': str(uuid4()), 'player_id': player, 'text': ANSWER})
        export = await self.get(path + '/evaluation-request')
        result = {k: export[k] for k in ('task_id', 'attempt_no', 'input_hash', 'policy_hash')}
        await self.post(path + '/evaluation-result', {**result, 'command_id': str(uuid4()), 'evaluation': m2_evaluation()})
        frozen = await self.freeze(source); repeated = await self.freeze(source)
        for key in ('source_ref', 'segments', 'content_hash'): self.assertEqual(frozen[key], repeated[key])
        self.assertIn('录用', ''.join(s['text'] for s in frozen['segments']))
        body = await self.perform_frozen(frozen)
        spoof = signed_payload(self.sid, source_ref=frozen['source_ref'], segments=[{'text': '任意伪造结果', 'intent': 'result'}])
        await self.post('/api/performances', spoof, 409)
        await self.post('/api/performance-sources/freeze', {'source': {**source, 'result': {'decision': 'hire'}}}, 400)
        # Stored payload snapshot remains the exact accepted copy even if source version later changes.
        async with self.client.server.app[STATE]['engine'].begin() as conn:
            await conn.execute(update(db.interviews).where(db.interviews.c.id == interview['id']).values(version=db.interviews.c.version + 1))
        stale = signed_payload(self.sid, source_ref=frozen['source_ref'], segments=frozen['segments'])
        await self.post('/api/performances', stale, 409)
        replay = await self.post('/api/performances', body)
        self.assertEqual(canonical_hash(replay['performance']['source_snapshot']), frozen['source_ref']['source_hash'])

    async def test_m3_comment_160_chunks_gate_and_settled_result_do_not_change_game(self):
        people = [await self.register('source-' + str(n)) for n in range(3)]
        await self.start(); await self.command('open')
        self.sids = [(await self.submit_work(p))['submission_id'] for p in people]
        source = {'kind': 'competition_comment', 'id': self.cid, 'submission_id': self.sids[0]}
        await self.freeze(source, 409)
        await self.command('close-submissions')
        comment = '这款枕头替客户上班。' * 16
        self.assertEqual(len(comment), 160)
        exported = await self.export(self.sids[0]); payload = self.result_payload(exported)
        payload['evaluation']['comment'] = comment
        await self.post(self.path + f'/submissions/{self.sids[0]}/evaluation-result', payload)
        await self.freeze(source, 409)  # one accepted review cannot leak early comment
        for sid in self.sids[1:]: await self.accept(sid)
        frozen = await self.freeze(source)
        self.assertEqual(''.join(s['text'] for s in frozen['segments']), comment)
        self.assertTrue(all(len(s['text']) <= 120 for s in frozen['segments']))
        self.assertTrue(all(s['intent'] == 'comment' for s in frozen['segments']))
        self.assertNotIn('source_snapshot', frozen)  # freeze projection carries no hidden scores
        await self.perform_frozen(frozen)
        self.assertEqual((await self.snapshot())['status'], 'DISPLAYING')
        await self.voting(); await self.command('close-voting'); await self.command('settle')
        result = await self.freeze({'kind': 'competition_result', 'id': self.cid})
        self.assertIn('创意奖', ''.join(s['text'] for s in result['segments']))
        await self.perform_frozen(result)


class FixedScriptSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_smoke_is_single_exact_bounded_segment_and_valid_submission_source(self):
        import re
        from hell_inc.performance_sources import freeze_source
        from hell_inc.performance_contract import validate_submission
        # Fixed source must not need any DB/provider/model input.
        frozen=await freeze_source(None,'offline-test',{'kind':'fixed_script','id':'m4-live-smoke-v1'})
        expected='各位新同事，欢迎来到地狱集团。我是你们的首席执行官。在这里，能力决定岗位，创意决定晋升。今天先做个小测试：放轻松，认真回答，别急着把灵魂交给人事部。'
        self.assertEqual(frozen['segments'],[{'text':expected,'intent':'neutral'}])
        self.assertEqual(len(expected),74)
        self.assertEqual(len(re.findall(r'[\u4e00-\u9fff]',expected)),64)
        self.assertEqual(frozen['source_snapshot']['script_id'],'m4-live-smoke-v1')
        self.assertEqual(frozen['content_hash'],canonical_hash(frozen['segments']))
        payload=signed_payload(source_ref=frozen['source_ref'],segments=frozen['segments'])
        self.assertEqual(validate_submission(payload),payload)
        # No claimed duration, audio transformation or provider output in the source.
        self.assertEqual(set(frozen['segments'][0]),{'text','intent'})

    async def test_live_smoke_does_not_replace_demo_or_allow_caller_text(self):
        from hell_inc.performance_sources import freeze_source
        from hell_inc.performance_contract import FIXED_SCRIPT
        from hell_inc.contracts import ValidationError
        original=await freeze_source(None,'offline-test',{'kind':'fixed_script','id':'m4-demo-v1'})
        self.assertEqual(original['segments'],FIXED_SCRIPT)
        self.assertEqual(original['source_snapshot']['script_id'],'m4-demo-v1')
        frozen=await freeze_source(None,'offline-test',{'kind':'fixed_script','id':'m4-live-smoke-v1'})
        frozen['segments'][0]['text']='caller mutation'
        again=await freeze_source(None,'offline-test',{'kind':'fixed_script','id':'m4-live-smoke-v1'})
        self.assertNotEqual(again['segments'][0]['text'],'caller mutation')
        for source in ({'kind':'fixed_script','id':'m4-live-smoke-v2'},
                       {'kind':'fixed_script','id':'m4-live-smoke-v1','text':'caller supplied'}):
            with self.assertRaises(ValidationError):await freeze_source(None,'offline-test',source)
