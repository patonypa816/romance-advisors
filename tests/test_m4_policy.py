"""Pure offline policy tests, NOT actual MotionComponent/PCM/RTC runtime evidence.

These validate copied dialogue rules and M4 preparation contracts. No successful
Type9 scheduling, audible playback, paid TTS, or idle-resource binding is claimed.
"""
import copy
import unittest

from hell_inc.avatar_rules import action_schema as schema
from hell_inc.avatar_rules.semantic_speech import (
    LocalActionAlignment, SemanticSpeechError, compile_dialogue_semantic_timeline,
    compile_semantic_timeline, parse_dialogue_semantic_plan, validate_alignment,
)


def action(quote='你好', occurrence=1):
    return {'anchor': {'quote': quote, 'occurrence': occurrence},
            'motions': [{'part': 'head', 'description': '轻轻点头'}],
            'gaze': '看向镜头', 'expression': '平静'}


def alignment(words=None, digest='a' * 64):
    return {'status': 'ok', 'timebase': 'audio_start', 'pcm_sha256': digest,
            'words': words or [{'text': '你好', 'begin_time': 2000, 'end_time': 2500}]}


RECOVERY = {'gaze': '看向镜头', 'expression': '自然'}


class DialoguePolicyTests(unittest.TestCase):
    def test_current_dialogue_s_ge_lead_and_recovery_not_historical_l(self):
        current = compile_dialogue_semantic_timeline([action()], RECOVERY, alignment(), 6)
        legacy = compile_semantic_timeline([action()], RECOVERY, alignment(), 6)
        self.assertEqual([n['role'] for n in current], ['GE', 'S', 'Q'])
        self.assertAlmostEqual(current[1]['at_seconds'] - current[0]['at_seconds'], 1.1)
        self.assertAlmostEqual(current[2]['at_seconds'] - current[1]['at_seconds'], 1.5)
        self.assertEqual(current[1]['hold_seconds'], 0)
        self.assertEqual([n['role'] for n in legacy], ['GE', 'L', 'Q'])
        self.assertAlmostEqual(legacy[1]['at_seconds'] - legacy[0]['at_seconds'], .5)
        self.assertEqual(schema.ACTION_POST_SEND_SECONDS, 1.0)
        self.assertEqual(schema.QUIET_INSTRUCTION, '正常坐姿')
        self.assertIn('动作：正常坐姿。', current[2]['instruction'])
        self.assertIn('眼神：', current[1]['instruction'])
        self.assertIn('表情：', current[1]['instruction'])

    def test_policy_rejects_short_q_to_next_ge_gap_without_moving_anchors(self):
        words = [{'text': '你好', 'begin_time': 2000, 'end_time': 2400},
                 {'text': '再见', 'begin_time': 5599, 'end_time': 6000}]
        actions = [action(), action('再见')]
        before = copy.deepcopy(actions)
        with self.assertRaises(SemanticSpeechError) as caught:
            compile_dialogue_semantic_timeline(actions, RECOVERY, alignment(words), 9)
        self.assertEqual(caught.exception.code, 'anchor_window_conflict')
        self.assertEqual(actions, before)
        words[1]['begin_time'] = 5600
        timeline = compile_dialogue_semantic_timeline(actions, RECOVERY, alignment(words), 9)
        self.assertAlmostEqual(timeline[3]['at_seconds'] - timeline[2]['at_seconds'], 1)
        self.assertEqual(timeline[4]['at_seconds'], 5.6)

    def test_final_q_tail_allowed_only_by_existing_dialogue_policy(self):
        current = compile_dialogue_semantic_timeline([action()], RECOVERY, alignment(), 2.5)
        self.assertEqual(current[-1]['at_seconds'], 3.5)
        with self.assertRaises(SemanticSpeechError):
            compile_semantic_timeline([action()], RECOVERY, alignment(), 2.5,
                                      action_kind='S', ge_lead_seconds=1.1)
        with self.assertRaises(ValueError):
            compile_semantic_timeline([action()], RECOVERY, alignment(), 6,
                                      action_kind='S', ge_lead_seconds=.7)

    def test_no_action_has_no_implicit_q_or_state_frame(self):
        self.assertEqual(compile_dialogue_semantic_timeline([], None, None, 0), [])
        plan = {'emotion': '平静', 'observation': '固定台词', 'reading': None,
                'segments': [{'text': '你好', 'instruction': '自然说话', 'actions': [], 'recovery': None}]}
        self.assertEqual(parse_dialogue_semantic_plan(plan)['segments'][0]['actions'], [])
        plan['segments'][0]['recovery'] = RECOVERY
        with self.assertRaises(ValueError): parse_dialogue_semantic_plan(plan)

    def test_alignment_is_bound_to_exact_pcm_hash(self):
        self.assertEqual(validate_alignment(alignment(), '你好', 6, pcm_sha256='a' * 64), alignment())
        with self.assertRaises(SemanticSpeechError) as caught:
            LocalActionAlignment(alignment(), '你好', 6, pcm_sha256='b' * 64)
        self.assertEqual(caught.exception.details['reason'], 'pcm_sha256_mismatch')

    def test_anchor_must_be_unique_contiguous_and_provider_word_start(self):
        cases = [
            ('你好你好', [{'text': '你好', 'begin_time': 100, 'end_time': 200},
                        {'text': '你好', 'begin_time': 300, 'end_time': 400}], '你好', 'anchor_ambiguous'),
            ('你好', [{'text': '你好', 'begin_time': 100, 'end_time': 200}], '好', 'anchor_inside_provider_word'),
            ('你，好', [{'text': '你好', 'begin_time': 100, 'end_time': 200}], '你好', 'anchor_not_contiguous'),
            ('你好', [{'text': '你好', 'begin_time': 100, 'end_time': 200}], '再见', 'anchor_not_covered'),
        ]
        for text, words, quote, reason in cases:
            with self.subTest(reason=reason):
                local = LocalActionAlignment(alignment(words), text, 6, pcm_sha256='a' * 64)
                with self.assertRaises(SemanticSpeechError) as caught:
                    local.verify_anchor({'quote': quote, 'occurrence': 1})
                self.assertEqual(caught.exception.details['reason'], reason)
        verified = LocalActionAlignment(alignment(), '你好', 6, pcm_sha256='a' * 64)
        self.assertEqual(verified.verify_anchor(action()['anchor'])['s_at_seconds'], 2)

    def test_dialogue_rejects_whole_body_arbitrary_timing_and_typed_occurrence(self):
        for change in ('whole_body', 'kind', 'seconds', 'bool_occurrence'):
            node = action()
            if change == 'whole_body': node['motions'][0]['part'] = 'whole_body'
            elif change == 'kind': node['kind'] = 'L'
            elif change == 'seconds': node['delay_seconds'] = 1
            else: node['anchor']['occurrence'] = True
            with self.subTest(change=change), self.assertRaises(ValueError):
                compile_dialogue_semantic_timeline([node], RECOVERY, alignment(), 6)


class PreparationSeamTests(unittest.IsolatedAsyncioTestCase):
    """Injected providers only; no actual TTS or MotionComponent is instantiated."""
    def setUp(self):
        import hashlib
        self.pcm = b'\x01\x00' * (24000 * 6)
        self.digest = hashlib.sha256(self.pcm).hexdigest()
        self.words = alignment(digest=self.digest)
        self.plan = {'text': ' 你好 ', 'instruction': ' 自然说话 ',
                     'actions': [action()], 'recovery': copy.deepcopy(RECOVERY)}

    def stage1(self):
        from hell_inc.avatar_compat import freeze_stage1
        return freeze_stage1(self.plan['text'], self.plan)

    def audio(self):
        return {'pcm': self.pcm, 'alignment': self.words, 'sample_rate': 24000,
                'channels': 1, 'sample_format': 's16le'}

    def test_frozen_stage1_exact_whitespace_and_nested_value_ownership(self):
        from hell_inc.avatar_compat import freeze_stage1
        frozen = self.stage1()
        self.plan['actions'][0]['gaze'] = '修改'
        thawed = frozen.content
        thawed['text'] = '修改'
        self.assertEqual(frozen.content['text'], ' 你好 ')
        self.assertEqual(frozen.content['instruction'], ' 自然说话 ')
        self.assertEqual(frozen.content['actions'][0]['gaze'], '看向镜头')
        with self.assertRaisesRegex(ValueError, 'frozen_text_changed'):
            freeze_stage1('另一个结果', self.plan)

    def test_stage2_can_change_only_anchors_not_text_instruction_action_or_recovery(self):
        from hell_inc.avatar_compat import bind_audio
        frozen = self.stage1()
        changes = {
            'text': lambda p: p.update(text='你好'),
            'instruction': lambda p: p.update(instruction='快说'),
            'recovery': lambda p: p['recovery'].update(gaze='左看'),
            'count': lambda p: p.update(actions=[]),
            'motion': lambda p: p['actions'][0]['motions'][0].update(description='摇头'),
            'gaze': lambda p: p['actions'][0].update(gaze='左看'),
            'expression': lambda p: p['actions'][0].update(expression='生气'),
            'extra_field': lambda p: p['actions'][0].update(delay_seconds=3),
        }
        for name, mutate in changes.items():
            changed = frozen.content
            mutate(changed)
            with self.subTest(name=name), self.assertRaises(ValueError):
                bind_audio(frozen, self.pcm, self.words, changed)
        prepared = bind_audio(frozen, self.pcm, self.words, frozen.content)
        self.assertIs(prepared.pcm, self.pcm)
        self.assertEqual(prepared.pcm_sha256, self.digest)
        self.assertEqual(prepared.audio_seconds, 6)
        self.assertEqual(prepared.content['text'], ' 你好 ')
        prepared.content['actions'].clear()
        self.assertEqual(len(prepared.content['actions']), 1)

    def test_pcm_format_duration_and_hash_must_match_without_crop_padding_or_retime(self):
        from hell_inc.avatar_compat import bind_audio
        for change in ({'sample_rate': 48000}, {'sample_rate': True}, {'channels': 2},
                       {'channels': True}, {'sample_format': 'f32le'}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'invalid_pcm_format'):
                bind_audio(self.stage1(), self.pcm, self.words, self.plan, **change)
        for pcm in (b'', b'\x00', bytearray(self.pcm), b'\x00\x00' * (24000 * 31)):
            with self.subTest(length=len(pcm)), self.assertRaises(ValueError):
                bind_audio(self.stage1(), pcm, self.words, self.plan)
        with self.assertRaises(SemanticSpeechError):
            bind_audio(self.stage1(), self.pcm + b'\x01\x00', self.words, self.plan)

    async def test_two_stage_calls_synthesis_once_and_rejects_bad_hash_before_stage2(self):
        from hell_inc.avatar_compat import prepare_semantic
        from unittest.mock import AsyncMock
        synthesize = AsyncMock(return_value=self.audio())
        anchors = AsyncMock(return_value=self.plan)
        prepared = await prepare_semantic(self.stage1(), synthesize, anchors)
        synthesize.assert_awaited_once()
        anchors.assert_awaited_once()
        self.assertEqual(prepared.pcm, self.pcm)
        self.words['pcm_sha256'] = '0' * 64
        synthesize = AsyncMock(return_value=self.audio())
        anchors = AsyncMock(return_value=self.plan)
        with self.assertRaises(SemanticSpeechError):
            await prepare_semantic(self.stage1(), synthesize, anchors)
        synthesize.assert_awaited_once()
        anchors.assert_not_awaited()

    async def test_no_actions_skip_stage2_and_cancellation_prevents_extra_preparation(self):
        from hell_inc.avatar_compat import freeze_stage1, prepare_semantic
        from unittest.mock import AsyncMock
        plan = {**self.plan, 'actions': [], 'recovery': None}
        frozen = freeze_stage1(plan['text'], plan)
        synthesize = AsyncMock(return_value={**self.audio(), 'alignment': None})
        anchors = AsyncMock(side_effect=AssertionError('must not plan actions'))
        prepared = await prepare_semantic(frozen, synthesize, anchors)
        self.assertEqual(prepared.timeline, [])
        anchors.assert_not_awaited()
        synthesize.reset_mock()
        with self.assertRaisesRegex(ValueError, 'preparation_cancelled'):
            await prepare_semantic(frozen, synthesize, anchors, cancelled=lambda: True)
        synthesize.assert_not_awaited()

    async def test_owned_sender_seam_delegates_policy_and_rejects_altered_evidence(self):
        from dataclasses import replace
        from unittest.mock import AsyncMock
        from hell_inc.avatar_compat import bind_audio, send_prepared
        prepared = bind_audio(self.stage1(), self.pcm, self.words, self.plan)
        sender = AsyncMock(return_value={'transport_only': True})
        self.assertEqual(await send_prepared(prepared, sender, lambda: True), {'transport_only': True})
        args, kwargs = sender.await_args
        self.assertIs(args[0], self.pcm)
        self.assertEqual(args[1], self.plan['text'])
        self.assertEqual(kwargs['action_kind'], 'S')
        self.assertEqual(kwargs['ge_lead_seconds'], 1.1)
        self.assertTrue(kwargs['local_action_anchors'])
        sender.reset_mock()
        for altered in (replace(prepared, pcm_sha256='0' * 64), replace(prepared, timeline_json='[]')):
            with self.assertRaisesRegex(ValueError, 'prepared_evidence_changed'):
                await send_prepared(altered, sender, lambda: True)
        with self.assertRaises(ValueError): await send_prepared(prepared, sender, lambda: False)
        sender.assert_not_awaited()

    def test_stage2_anchor_change_uses_measured_word_start_without_rewriting_audio(self):
        from hell_inc.avatar_compat import freeze_stage1, bind_audio
        plan = {**self.plan, 'text': '你好再见'}
        words = alignment([{'text': '你好', 'begin_time': 2000, 'end_time': 2500},
                           {'text': '再见', 'begin_time': 5000, 'end_time': 5500}], self.digest)
        stage2 = copy.deepcopy(plan)
        stage2['actions'][0]['anchor'] = {'quote': '再见', 'occurrence': 1}
        prepared = bind_audio(freeze_stage1(plan['text'], plan), self.pcm, words, stage2)
        self.assertEqual(prepared.timeline[1]['at_seconds'], 5)
        self.assertEqual(prepared.timeline[2]['at_seconds'], 6.5)
        self.assertIs(prepared.pcm, self.pcm)
        self.assertEqual(prepared.content['text'], plan['text'])


class OfflineFixtureAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_fixture_zero_http_calls_no_pcm_and_explicit_false_media_evidence(self):
        from unittest.mock import patch
        from hell_inc.performance_adapter import FixtureAdapter
        adapter = FixtureAdapter()
        text = ' 原文不得悄悄去空白。\n'
        with patch('aiohttp.ClientSession', side_effect=AssertionError('unexpected HTTP')) as http, \
             patch('urllib.request.urlopen', side_effect=AssertionError('unexpected HTTP')) as urlopen, \
             patch('socket.create_connection', side_effect=AssertionError('unexpected socket')) as socket:
            prepared = await adapter.prepare({'text': text, 'intent': 'result'}, lambda: False)
            receipt = await adapter.send(prepared, {'mode': 'offline_fixture'}, lambda: True)
        http.assert_not_called(); urlopen.assert_not_called(); socket.assert_not_called()
        self.assertEqual(prepared.text, text)
        self.assertEqual(receipt['execution_kind'], 'offline_fixture')
        for key in ('media_verified', 'audience_heard', 'audio_generated', 'provider_called'):
            self.assertIs(receipt[key], False)
        self.assertEqual(receipt['fixture_receipt'], prepared.content_hash)
        self.assertFalse(hasattr(prepared, 'pcm'))

    async def test_fixture_gate_cancellation_and_owned_content_not_hook_mutation(self):
        from hell_inc.performance_adapter import FixtureAdapter
        original = {'text': '冻结原文', 'intent': 'comment'}
        async def hook(prepared):
            original['text'] = '恶意更改'
        adapter = FixtureAdapter(prepare_hook=hook)
        prepared = await adapter.prepare(original, lambda: False)
        self.assertEqual(prepared.text, '冻结原文')
        for session, gate in (({'mode': 'real'}, True), ({'mode': 'offline_fixture'}, False)):
            with self.assertRaisesRegex(ValueError, 'fixture_send_gate_closed'):
                await adapter.send(prepared, session, lambda: gate)
        with self.assertRaisesRegex(ValueError, 'preparation_cancelled'):
            await adapter.prepare(original, lambda: True)
