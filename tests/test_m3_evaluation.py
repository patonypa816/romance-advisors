"""Pure rubric fixtures: validate shape/evidence, not model semantic quality."""
import copy
import unittest
from itertools import product
from hell_inc.contracts import ValidationError
from hell_inc.competition_evaluation import validate_evaluation, comment_needs_review


def fixture(scores=(2, 2, 2)):
    return {'criteria': [dict(id=key, score=score, evidence=['引用'] if score else [], reason='测试依据')
                         for key, score in zip(('originality', 'coherence', 'setting'), scores)], 'comment': '描述作品的机制。'}


class M3EvaluationTests(unittest.TestCase):
    def test_all_27_score_combinations_code_total(self):
        for scores in product(range(3), repeat=3): self.assertEqual(validate_evaluation(fixture(scores), '原文引用')['total'], sum(scores))

    def test_reject_forged_fields_scores_evidence_and_comments(self):
        bad = []
        for field in ('total', 'winner', 'award', 'performance', 'tool_calls'): bad.append({**fixture(), field: 999})
        for score in (True, False, 1.0, '2', -1, 3, None):
            value = fixture(); value['criteria'][0]['score'] = score; bad.append(value)
        for comment in ('', ' ', '\x00', '长' * 161, None, True): bad.append({**fixture(), 'comment': comment})
        for change in ({'id': 'empathy'}, {'evidence': ['伪造证据']}, {'evidence': []}, {'score': 0, 'evidence': ['引用']}, {'reason': ''}, {'reason': '长' * 161}, {'extra': 1}):
            value = fixture(); value['criteria'][0].update(change); bad.append(value)
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValidationError): validate_evaluation(value, '原文引用')

    def test_unicode_exact_original_quote(self):
        value = fixture()
        for row in value['criteria']: row['evidence'] = ['é']
        self.assertEqual(validate_evaluation(value, ' é ')['total'], 6)
        with self.assertRaises(ValidationError): validate_evaluation(value, 'e\u0301')

    def test_comment_words_trigger_review_without_discarding_evaluation(self):
        value = fixture(); value['comment'] = '产品302号提供3种可选的梦境。'
        self.assertEqual(validate_evaluation(value, '原文引用')['comment'], value['comment'])
        self.assertFalse(comment_needs_review(value['comment']))
        for comment in ('作品名为冠军噩梦。', '满分冠军，排名第一，领先其他作品。', '6/6冠军第一名'):
            value['comment'] = comment
            with self.subTest(comment=comment):
                self.assertEqual(validate_evaluation(value, '原文引用')['comment'], comment)
                self.assertTrue(comment_needs_review(comment))
