"""Offline local-anchor regressions: captured words, controlled PCM, no services.

The small JSON fixture preserves raw final_words from two safe stream logs.
Audio is intentionally synthetic: these tests prove timestamps/PCM provenance and
local scheduling rules, not that generated audio pronounced the captured text.
"""
import copy
import hashlib
import io
import json
from pathlib import Path
import unittest
import wave

from semantic_speech import LocalActionAlignment, SemanticSpeechError, validate_alignment
from timed_action_planner import prepare_timed_action_request, compile_timed_action_result
from tts_live import _verified_alignment


CASES = json.loads((Path(__file__).parent / 'testdata/local-anchor-cases.json').read_text())


def verified(words, text, seconds):
    # Deterministic, nonzero mono s16le frames make the supplied hash checkable;
    # WAV and PCM hashes are independently computed from actual bytes.
    pcm = b'\x01\x00\xff\xff' * (round(seconds * 24000) // 2)
    target = io.BytesIO()
    with wave.open(target, 'wb') as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(pcm)
    wav_bytes = target.getvalue()
    state = {'sentence_indices': {0}, 'reason': None, 'sentence_end': True,
             'sentences': {0: {'words': words, 'ended': True}},
             'words': words, 'audio_bytes': len(wav_bytes),
             'audio_hash': hashlib.sha256(wav_bytes)}
    alignment = _verified_alignment(state, text, len(pcm) / 48000,
                                    hashlib.sha256(wav_bytes).hexdigest(),
                                    hashlib.sha256(pcm).hexdigest())
    return alignment, pcm


def synthetic(text):
    words = [{'text': char, 'begin_index': i, 'end_index': i + 1,
              'begin_time': i * 400, 'end_time': i * 400 + 300}
             for i, char in enumerate(text)]
    return verified(words, text, 10)[0]


def action(quote):
    return {'anchor': {'quote': quote, 'occurrence': 1},
            'motions': [{'part': 'head', 'description': '轻轻点头一次'}],
            'gaze': '注视镜头', 'expression': '神情温和'}


class LocalActionAlignmentTests(unittest.TestCase):
    def assert_rejected(self, source, provider, quote, reason):
        alignment = synthetic(provider)
        with self.assertRaises(SemanticSpeechError) as caught:
            LocalActionAlignment(alignment, source, 10).verify_anchor(
                {'quote': quote, 'occurrence': 1})
        self.assertEqual(caught.exception.code, 'alignment_unavailable')
        self.assertEqual(caught.exception.details['reason'], reason)

    def run_real_case(self, case_id):
        case = next(c for c in CASES if c['case_id'] == case_id)
        part, words = copy.deepcopy(case['part']), copy.deepcopy(case['words'])
        original_part, original_words = copy.deepcopy(part), copy.deepcopy(words)
        alignment, pcm = verified(words, part['text'], case['audio_seconds'])
        self.assertEqual(alignment['status'], 'ok')
        self.assertEqual(alignment['text_matches_script'], case_id == 'c0003')
        self.assertEqual(alignment['words'], original_words)
        # The historical whole-text gate remains strict, while this path proves
        # each final anchor against the untouched original and provider words.
        if case_id == 'c0002':
            with self.assertRaises(SemanticSpeechError):
                validate_alignment(alignment, part['text'], case['audio_seconds'])
        else:
            validate_alignment(alignment, part['text'], case['audio_seconds'])
        clean, data, _ = prepare_timed_action_request(part, alignment, case['audio_seconds'], {})
        self.assertTrue(data['schedules'])
        for schedule in data['schedules']:
            planned = compile_timed_action_result(clean, alignment, case['audio_seconds'],
                {'schedule_id': schedule['schedule_id']}, schedules=data['schedules'])
            proofs = LocalActionAlignment(alignment, part['text'], len(pcm) / 48000,
                pcm_sha256=hashlib.sha256(pcm).hexdigest()).verify_actions(planned['actions'])
            self.assertEqual([p['s_at_seconds'] for p in proofs],
                             [g['s_at_seconds'] for g in schedule['groups']])
            if schedule['schedule_id'] == 's01':
                self.assertEqual([p['s_at_seconds'] for p in proofs], case['expected_s_seconds'])
            timeline = planned['timeline']
            self.assertEqual([n['role'] for n in timeline], ['GE', 'S', 'Q'] * len(part['actions']))
            for i in range(len(part['actions'])):
                ge, s, q = timeline[i * 3:i * 3 + 3]
                self.assertAlmostEqual(s['at_seconds'] - ge['at_seconds'], 1.1)
                self.assertAlmostEqual(q['at_seconds'] - s['at_seconds'], 1.5)
                self.assertLess(s['at_seconds'], case['audio_seconds'])
                if i:
                    self.assertGreaterEqual(ge['at_seconds'] - timeline[i * 3 - 1]['at_seconds'], 1 - 1e-9)
            for before, after in zip(part['actions'], planned['actions']):
                self.assertEqual({k: v for k, v in before.items() if k != 'anchor'},
                                 {k: v for k, v in after.items() if k != 'anchor'})
        self.assertEqual(part, original_part)
        self.assertEqual(words, original_words)
        self.assertEqual(alignment['words'], original_words)

    def test_c0002_raw_words_to_final_local_validation(self):
        self.run_real_case('c0002')

    def test_c0003_raw_words_to_final_local_validation(self):
        self.run_real_case('c0003')
        words = next(c['words'] for c in CASES if c['case_id'] == 'c0003')
        self.assertIn('黢黢', [w['text'] for w in words])

    def test_unrelated_substitution_deletion_and_insertion_allowed(self):
        source = '甲乙丙丁戊己庚辛'
        for provider in ('甲乙错丁戊己庚辛', '甲乙丁戊己庚辛', '甲乙新增丙丁戊己庚辛'):
            with self.subTest(provider=provider):
                a = synthetic(provider)
                proof = LocalActionAlignment(a, source, 10).verify_anchor({'quote': '戊己庚辛', 'occurrence': 1})
                self.assertEqual(proof['s_at_seconds'], a['words'][provider.index('戊')]['begin_time'] / 1000)

    def test_anchor_substitution_rejected(self):
        self.assert_rejected('甲乙丙丁', '甲错丙丁', '乙丙', 'anchor_not_covered')

    def test_duplicate_source_rejected(self):
        self.assert_rejected('甲乙丙甲乙', '甲乙丙', '甲乙', 'anchor_ambiguous')

    def test_duplicate_provider_rejected(self):
        self.assert_rejected('甲乙丙', '甲乙丙甲乙', '甲乙', 'anchor_ambiguous')

    def test_out_of_order_correspondence_rejected(self):
        self.assert_rejected('甲乙丙丁戊己庚辛', '戊己庚辛甲乙丙丁', '戊己', 'anchor_correspondence_unverified')

    def test_provider_punctuation_break_rejected(self):
        self.assert_rejected('甲乙丙丁', '甲乙，丙丁', '乙丙', 'anchor_not_contiguous')

    def test_source_punctuation_break_rejected(self):
        self.assert_rejected('甲乙，丙丁', '甲乙丙丁', '乙丙', 'anchor_not_contiguous')

    def test_word_interior_rejected_without_interpolation(self):
        a = synthetic('甲乙丙丁')
        a['words'][:2] = [{'text': '甲乙', 'begin_index': 0, 'end_index': 2,
                          'begin_time': 0, 'end_time': 700}]
        with self.assertRaises(SemanticSpeechError) as caught:
            LocalActionAlignment(a, '甲乙丙丁', 10).verify_anchor({'quote': '乙丙', 'occurrence': 1})
        self.assertEqual(caught.exception.details['reason'], 'anchor_inside_provider_word')

    def test_index_chain_break_rejected_at_stream_and_local_gates(self):
        words = copy.deepcopy(CASES[0]['words'])
        words[2]['begin_index'] += 1
        a, _ = verified(words, CASES[0]['part']['text'], CASES[0]['audio_seconds'])
        self.assertEqual((a['status'], a['reason']), ('unavailable', 'invalid_timestamps'))
        a = synthetic('甲乙丙丁')
        a['words'][2]['begin_index'] += 1
        with self.assertRaises(SemanticSpeechError) as caught:
            LocalActionAlignment(a, '甲乙丙丁', 10)
        self.assertEqual(caught.exception.details['reason'], 'invalid_timestamps')

    def test_pcm_content_hash_mismatch_rejected(self):
        case = CASES[0]
        a, pcm = verified(copy.deepcopy(case['words']), case['part']['text'], case['audio_seconds'])
        changed = bytes([pcm[0] ^ 1]) + pcm[1:]
        with self.assertRaises(SemanticSpeechError) as caught:
            LocalActionAlignment(a, case['part']['text'], case['audio_seconds'],
                                 pcm_sha256=hashlib.sha256(changed).hexdigest())
        self.assertEqual(caught.exception.details['reason'], 'pcm_sha256_mismatch')

    def test_schedule_time_tampering_rejected(self):
        case = CASES[0]
        a, _ = verified(copy.deepcopy(case['words']), case['part']['text'], case['audio_seconds'])
        part, data, _ = prepare_timed_action_request(case['part'], a, case['audio_seconds'], {})
        data['schedules'][0]['groups'][0]['s_at_seconds'] += 0.001
        with self.assertRaisesRegex(ValueError, 'invalid_timed_action_boundary'):
            compile_timed_action_result(part, a, case['audio_seconds'], {'schedule_id': 's01'}, schedules=data['schedules'])

    def test_group_order_tampering_rejected(self):
        case = CASES[0]
        a, _ = verified(copy.deepcopy(case['words']), case['part']['text'], case['audio_seconds'])
        part, data, _ = prepare_timed_action_request(case['part'], a, case['audio_seconds'], {})
        data['schedules'][0]['groups'][0]['group_index'] = 2
        with self.assertRaisesRegex(ValueError, 'invalid_timed_action_boundary'):
            compile_timed_action_result(part, a, case['audio_seconds'], {'schedule_id': 's01'}, schedules=data['schedules'])

    def test_insufficient_ge_s_q_spacing_still_rejected(self):
        text = '甲乙'
        a = synthetic(text)
        a['words'][1].update(begin_time=3599, end_time=3699)
        part = {'text': text, 'instruction': '自然清晰', 'actions': [action('甲'), action('乙')],
                'recovery': {'gaze': '注视镜头', 'expression': '神情平静'}}
        with self.assertRaises(SemanticSpeechError) as caught:
            prepare_timed_action_request(part, a, 4, {})
        self.assertEqual(caught.exception.code, 'anchor_window_conflict')
        self.assertEqual(caught.exception.details['reason'], 'no_feasible_schedule')

    def test_tail_q_rule_and_negative_preroll_preserved(self):
        text = '甲乙丙丁戊己庚辛壬癸子'
        a = synthetic(text)
        part = {'text': text, 'instruction': '自然清晰', 'actions': [action('甲'), action('癸')],
                'recovery': {'gaze': '注视镜头', 'expression': '神情平静'}}
        # Last real word ends at 4.3s; Q still belongs 1.5s after its S.
        clean, data, _ = prepare_timed_action_request(part, a, 4.3, {})
        result = compile_timed_action_result(clean, a, 4.3, {'schedule_id': 's01'}, schedules=data['schedules'])
        timeline = result['timeline']
        self.assertAlmostEqual(timeline[0]['at_seconds'], -1.1)
        self.assertGreater(timeline[-1]['at_seconds'], 4.3)
        self.assertAlmostEqual(timeline[-1]['at_seconds'] - timeline[-2]['at_seconds'], 1.5)


if __name__ == '__main__':
    unittest.main()
