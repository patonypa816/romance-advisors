import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import performance


class PerformanceCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / 'runtime.json'
        self.profile = self.root / 'profile.json'

    def invoke(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return performance.main(['--runtime', str(self.runtime), *map(str, args)])

    def test_endpoint_only_accepts_local_matching_origin(self):
        for url, origin, valid in [
            ('http://127.0.0.1:8080/studio/token/', 'http://127.0.0.1:8080', True),
            ('http://example.com:8080/studio/token/', 'http://example.com:8080', False),
            ('http://127.0.0.1:8080/studio/token/', 'https://evil.example', False),
            ('http://user:secret@127.0.0.1:8080/studio/token/', 'http://127.0.0.1:8080', False),
            ('http://127.0.0.1:8080/studio/token/?key=secret', 'http://127.0.0.1:8080', False)]:
            self.runtime.write_text(json.dumps({'url': url, 'origin': origin}))
            if valid:
                self.assertEqual(performance.endpoint(self.runtime), (url, origin))
            else:
                with self.assertRaises(ValueError): performance.endpoint(self.runtime)

    def test_export_is_allowlisted_and_does_not_overwrite(self):
        settings = {'name': '角色', 'persona': '自然', 'reply_style': '清晰', 'api_key': 'not-exported'}
        with patch.object(performance, 'call', return_value={'studio': {'settings': settings}}):
            self.assertEqual(self.invoke('export-persona', self.profile), 0)
            self.assertEqual(set(json.loads(self.profile.read_text())), performance.PERSONA_FIELDS)
            self.assertEqual(self.invoke('export-persona', self.profile), 1)
        self.assertNotIn('not-exported', self.profile.read_text())

    def test_import_rejects_extra_secret_before_network(self):
        self.profile.write_text(json.dumps({'name': '', 'persona': '', 'reply_style': '', 'api_key': 'secret'}))
        with patch.object(performance, 'call') as call:
            self.assertEqual(self.invoke('import-persona', self.profile), 1)
            call.assert_not_called()

    def test_control_only_uses_existing_session_route(self):
        with patch.object(performance, 'call', return_value={}) as call:
            self.assertEqual(self.invoke('pause', 'request-one'), 0)
            call.assert_called_once_with(self.runtime, 'performance/pause', {'request_id': 'request-one'})
