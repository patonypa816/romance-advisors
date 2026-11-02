"""Pure protocol tests; no avatar transport, model or media evidence."""
import copy
import unittest
from uuid import uuid4
from hell_inc.contracts import ValidationError
from hell_inc.performance_contract import canonical_hash, timestamp, split_text, validate_submission, FIXED_SCRIPT


def signed_payload(session_id=None, **changes):
    body = dict(performance_id=str(uuid4()), session_id=session_id or str(uuid4()),
                source_ref={'kind': 'fixed_script', 'id': 'm4-demo-v1'},
                segments=copy.deepcopy(FIXED_SCRIPT), expires_at='2099-01-01T00:00:00.000Z')
    body.update(changes)
    body['payload_hash'] = canonical_hash({k: body[k] for k in ('session_id', 'source_ref', 'segments', 'expires_at')})
    return body


class M4ContractTests(unittest.TestCase):
    def test_160_character_comment_exact_chunks_no_unicode_or_whitespace_rewrite(self):
        comment = '  ' + '点评😀，' * 39 + '\n尾'
        self.assertEqual(len(comment), 160)
        chunks = split_text(comment)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(c) <= 120 for c in chunks))
        self.assertEqual(''.join(chunks), comment)
        self.assertEqual(split_text(comment), chunks)

    def test_total_limit_and_invalid_unicode_fail_without_truncation(self):
        self.assertEqual(''.join(split_text('文' * 960)), '文' * 960)
        for value in ('文' * 961, '', '   ', 'a\x00b', '\ud800', 123, None):
            with self.subTest(value=repr(value)), self.assertRaises(ValidationError): split_text(value)

    def test_canonical_hash_preserves_types_text_intent_and_source(self):
        body = signed_payload()
        self.assertEqual(validate_submission(body), body)
        self.assertNotEqual(canonical_hash({'a': 1}), canonical_hash({'a': True}))
        self.assertNotEqual(canonical_hash({'a': 1}), canonical_hash({'a': 1.0}))
        for key, value in [('session_id', str(uuid4())), ('expires_at', '2099-01-02T00:00:00.000Z'),
                           ('segments', [{'text': '原文 ', 'intent': 'neutral'}]),
                           ('source_ref', {'kind': 'fixed_script', 'id': 'different'})]:
            changed = copy.deepcopy(body); changed[key] = value
            with self.subTest(key=key), self.assertRaises(ValidationError): validate_submission(changed)

    def test_strict_submission_fields_and_hash_format(self):
        body = signed_payload()
        for change in ({'extra': 1}, {'payload_hash': 'A' * 64}, {'payload_hash': '0' * 64},
                       {'segments': []}, {'segments': [{'text': '文' * 121, 'intent': 'neutral'}]},
                       {'segments': [{'text': 'text', 'intent': 'invented'}]}):
            with self.subTest(change=change), self.assertRaises(ValidationError): validate_submission({**body, **change})

    def test_utc_canonicalization(self):
        self.assertEqual(timestamp('2099-01-01T00:00:00+00:00'), '2099-01-01T00:00:00.000Z')
        for bad in ('2099-01-01', '2099-01-01T08:00:00+08:00', '2099-01-01T00:00:00.000001Z', None):
            with self.subTest(bad=bad), self.assertRaises(ValidationError): timestamp(bad)
