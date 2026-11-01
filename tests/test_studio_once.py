"""Offline input contract only. No Studio startup, model, TTS or Vidu calls."""
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from hell_inc.studio_once import InputError, build_once, control_endpoint, digest, submit_once


def sources():
    result = []
    for index, text in enumerate(('点评原文甲。', '点评原文乙。', '比赛结果：创意奖，乙。人气奖，甲。')):
        ref = {'kind': 'competition_comment' if index < 2 else 'competition_result',
               'id': 'round-1', 'source_version': 9, 'source_hash': 'a' * 64}
        if index < 2:
            ref['submission_id'] = 'work-' + str(index)
        segments = [{'text': text, 'intent': 'comment' if index < 2 else 'result'}]
        result.append({'source_ref': ref, 'segments': segments, 'content_hash': digest(segments)})
    return result


class ProjectionTests(unittest.TestCase):
    def test_exact_facts_and_only_original_once_keys(self):
        data = sources()
        original = copy.deepcopy(data)
        prepared = build_once(data)
        self.assertEqual(data, original)
        self.assertEqual(set(prepared['body']), {'text', 'mode'})
        self.assertEqual(prepared['body']['mode'], 'once')
        self.assertLessEqual(len(prepared['body']['text']), 600)
        for item in data:
            self.assertIn(''.join(s['text'] for s in item['segments']), prepared['body']['text'])
        self.assertEqual(prepared['source_refs'], [s['source_ref'] for s in data])
        self.assertIn('不要求逐字照读', prepared['body']['text'])

    def test_overlong_never_truncated(self):
        data = sources()
        data[0]['segments'][0]['text'] = '原文' * 300
        data[0]['content_hash'] = digest(data[0]['segments'])
        with self.assertRaisesRegex(InputError, '600 字'):
            build_once(data)

    def test_changed_facts_rejected(self):
        data = sources()
        data[2]['segments'][0]['text'] = '比赛结果：创意奖，甲。'
        with self.assertRaisesRegex(InputError, '哈希不符'):
            build_once(data)
        data = sources()
        data[2]['source_ref']['id'] = 'other-round'
        with self.assertRaisesRegex(InputError, '同一比赛'):
            build_once(data)

    def test_original_control_boundary(self):
        url, origin = control_endpoint('http://127.0.0.1:8765/studio/private-token/')
        self.assertEqual(url, 'http://127.0.0.1:8765/studio/private-token/director/submit')
        self.assertEqual(origin, 'http://127.0.0.1:8765')
        for value in ('https://elsewhere/studio/token', 'http://127.0.0.1:8765/player/token',
                      'http://127.0.0.1:8765/studio/token?key=secret'):
            with self.assertRaises(InputError) as caught:
                control_endpoint(value)
            self.assertNotIn(value, str(caught.exception))


class Response:
    status = 200
    def __init__(self, payload): self.payload = payload
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def json(self): return self.payload


class Session:
    def __init__(self, payload=None, error=None): self.payload = payload; self.error = error; self.calls = []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error: raise self.error
        return Response(self.payload)


class SubmitTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_old_director_response_shape_not_playback(self):
        # Fixed 2e076b6 director.py submit: return {"id": c["id"]};
        # server.py enqueue_comment: c0001 format. No invented ok/status envelope.
        session = Session({'id': 'c0017'})
        prepared = build_once(sources())
        with patch('hell_inc.studio_once.aiohttp.ClientSession', return_value=session):
            result = await submit_once(prepared, 'http://127.0.0.1:8765/studio/private-token')
        self.assertEqual(result, {'state': 'accepted', 'http_status': 200, 'studio_id': 'c0017', 'playback': 'not_verified'})
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.calls[0][1]['json'], prepared['body'])
        self.assertEqual(session.calls[0][1]['headers'], {'Origin': 'http://127.0.0.1:8765'})
        self.assertFalse(session.calls[0][1]['allow_redirects'])

    async def test_unknown_never_retried_or_secret_logged(self):
        session = Session(error=TimeoutError('private-token signed-secret-url'))
        with patch('hell_inc.studio_once.aiohttp.ClientSession', return_value=session):
            result = await submit_once(build_once(sources()), 'http://127.0.0.1:8765/studio/private-token')
        self.assertEqual(result, {'state': 'unknown', 'error_type': 'timeout'})
        self.assertEqual(len(session.calls), 1)
        session = Session(error=ConnectionError('private-token signed-secret-url'))
        with patch('hell_inc.studio_once.aiohttp.ClientSession', return_value=session):
            result = await submit_once(build_once(sources()), 'http://127.0.0.1:8765/studio/private-token')
        self.assertEqual(result, {'state': 'unknown', 'error_type': 'connect'})
        self.assertEqual(len(session.calls), 1)
        session = Session({'id': 'private-token'})
        with patch('hell_inc.studio_once.aiohttp.ClientSession', return_value=session):
            result = await submit_once(build_once(sources()), 'http://127.0.0.1:8765/studio/private-token')
        self.assertEqual(result['state'], 'unknown')
        self.assertNotIn('private-token', json.dumps(result))


class CommandTests(unittest.TestCase):
    def test_receipt_before_post_and_reuse_refuses(self):
        spec = importlib.util.spec_from_file_location('studio_once_cli', Path(__file__).parents[1] / 'tools/studio_once.py')
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.json'; source.write_text(json.dumps(sources()))
            receipt = Path(directory) / 'receipt.json'
            async def submit(prepared, control_url):
                recorded = json.loads(receipt.read_text())
                self.assertEqual(recorded['state'], 'unknown')
                self.assertEqual(recorded['body'], prepared['body'])
                self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
                return {'state': 'accepted', 'studio_id': 'c0001', 'playback': 'not_verified'}
            with patch.object(sys, 'argv', ['studio_once', '--sources', str(source), '--submit', '--receipt', str(receipt)]), patch.dict(os.environ, {'STUDIO_CONTROL_URL': 'http://127.0.0.1:8765/studio/private-token'}), patch.object(cli, 'submit_once', side_effect=submit) as call, patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(cli.main(), 0)
                self.assertEqual(cli.main(), 2)
                self.assertEqual(call.call_count, 1)
                self.assertNotIn('private-token', output.getvalue() + receipt.read_text())

    def test_preview_does_not_load_media_modules(self):
        code = "import sys; import hell_inc.studio_once; assert not any(n in sys.modules for n in ('hell_inc.real_performance','hell_inc.avatar_compat','hell_inc.avatar_transport','hell_inc.record_only'))"
        subprocess.run([sys.executable, '-c', code], check=True)
