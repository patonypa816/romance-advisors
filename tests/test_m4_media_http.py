"""Network-security contracts with fake aiohttp transport; zero external requests."""
import unittest
from unittest.mock import patch

from hell_inc.media_http import validate_secret, validate_workspace, validate_url, scrub_url, MediaHTTP


class MediaSecurityTests(unittest.TestCase):
    def test_opaque_secrets_not_vendor_prefixes_preserved_and_header_injection_rejected(self):
        for secret in ('opaque-token_123', 'vendor.new-format:abc==', 'not-an-sk-key'):
            self.assertEqual(validate_secret(secret), secret)
        for bad in ('', '\rkey', 'key\nInjected: secret', 'key\x00', None, 1, True, 'x' * 10000):
            with self.subTest(bad=repr(bad)), self.assertRaises(ValueError): validate_secret(bad)

    def test_workspace_one_dns_label_not_arbitrary_origin(self):
        self.assertEqual(validate_workspace('approved-workspace'), 'approved-workspace')
        for bad in ('', '../other', 'user@evil', 'a.b', 'https://evil', 'a/b', '-prefix', 'suffix-', 'x\n'):
            with self.subTest(bad=bad), self.assertRaises(ValueError): validate_workspace(bad)

    def test_download_https_allowlist_no_credentials_loopback_private_or_scheme_escape(self):
        hosts = ('approved-media.example', 'localhost', '127.0.0.1', '10.1.2.3', '169.254.169.254', '::1')
        good = 'https://approved-media.example/audio.wav?signature=private-value'
        self.assertEqual(validate_url(good, hosts), good)
        for bad in ('http://approved-media.example/a', 'https://evil.example/a',
                    'https://approved-media.example.evil.example/a', 'https://user:pass@approved-media.example/a',
                    'https://localhost/a', 'https://127.0.0.1/a', 'https://10.1.2.3/a',
                    'https://169.254.169.254/a', 'https://[::1]/a', 'file:///tmp/audio',
                    'https://approved-media.example:444/a'):
            with self.subTest(bad=bad), self.assertRaises(ValueError): validate_url(bad, hosts)

    def test_signed_urls_scrubbed_not_retained_in_diagnostics(self):
        redacted = scrub_url('https://approved-media.example/audio.wav?token=secret-signature#secret-fragment')
        self.assertNotIn('secret', redacted); self.assertNotIn('?', redacted); self.assertNotIn('#', redacted)
        self.assertIn('approved-media.example', redacted)


class RecordingSession:
    def __init__(self, headers=None): self.headers = headers or {}; self.calls = []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs)); return object()
    async def ws_connect(self, url, **kwargs):
        self.calls.append(('WSS', url, kwargs)); return object()


class MediaTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = RecordingSession()
        self.http = MediaHTTP(self.session, workspace='approved-workspace', proxy='http://proxy.example:3128',
                              audio_hosts=('approved-media.example',))
        self.api = 'https://approved-workspace.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation'

    async def test_http_wss_proxy_tls_and_exact_opaque_authorization_schemes(self):
        self.http.post(self.api, headers={'Authorization': 'Bearer opaque-bailian'})
        self.http.post('https://api.vidu.cn/live/s_avatar/component', headers={'Authorization': 'Token opaque-vidu'})
        await self.http.ws_connect('wss://api.vidu.cn/live/v1/external-lives/owned/stream', headers={'Authorization': 'Token opaque-vidu'})
        self.assertEqual(len(self.session.calls), 3)
        for method, _, kwargs in self.session.calls:
            self.assertEqual(kwargs['proxy'], 'http://proxy.example:3128'); self.assertIs(kwargs['ssl'], True)
            if method != 'WSS': self.assertIs(kwargs['allow_redirects'], False)
        self.assertEqual(self.session.calls[0][2]['headers']['Authorization'], 'Bearer opaque-bailian')
        self.assertEqual(self.session.calls[1][2]['headers']['Authorization'], 'Token opaque-vidu')

    async def test_download_strips_authorization_and_never_follows_redirects(self):
        self.http.get('https://approved-media.example/audio?signature=opaque', headers={'Authorization': 'Bearer never-forward'})
        kwargs = self.session.calls[-1][2]
        self.assertFalse(any(k.lower() == 'authorization' for k in kwargs['headers']))
        self.assertIs(kwargs['allow_redirects'], False)
        for url, headers in ((self.api, {'Authorization': 'Token wrong'}),
                             ('https://api.vidu.cn/live', {'Authorization': 'Bearer wrong'}),
                             (self.api, {'Authorization': 'Bearer ok', 'authorization': 'Bearer other'}),
                             (self.api, {'Cookie': 'forbidden'})):
            with self.subTest(url=url, headers=headers), self.assertRaises(ValueError): self.http.get(url, headers=headers)
        with self.assertRaises(ValueError): self.http.get(self.api, ssl=False)
        with self.assertRaises(ValueError): self.http.get('http://approved-media.example/audio')
        self.assertEqual(len(self.session.calls), 1)

    async def test_ambient_auth_is_rejected_for_http_and_wss_case_insensitively(self):
        for name in ('Authorization', 'authorization'):
            session = RecordingSession({name: 'Bearer ambient-forbidden'})
            http = MediaHTTP(session, workspace='approved-workspace')
            with self.subTest(name=name):
                with self.assertRaises(ValueError): http.get('https://api.vidu.cn/live')
                with self.assertRaises(ValueError): await http.ws_connect('wss://api.vidu.cn/live')
                self.assertEqual(session.calls, [])

    async def test_constructing_transport_does_not_create_network_client_or_read_credentials(self):
        with patch('aiohttp.ClientSession', side_effect=AssertionError('lazy construction required')) as client:
            http = MediaHTTP(workspace='approved-workspace')
            self.assertIsNone(http.session)
            await http.close(); client.assert_not_called()

    async def test_original_aiohttp_websocket_upgrade_rejects_redirect_before_second_request(self):
        from unittest.mock import AsyncMock, MagicMock
        import aiohttp
        response=MagicMock(); response.status=302; response.reason='Found'
        response.headers={'Location':'https://unapproved.example/steal'}; response.history=()
        response.request_info=MagicMock()
        http=MediaHTTP(workspace='approved-workspace',proxy='http://proxy.example:3128')
        try:
            with patch.object(aiohttp.ClientSession,'_request',new_callable=AsyncMock,return_value=response) as request:
                with self.assertRaises(aiohttp.WSServerHandshakeError):
                    await http.ws_connect('wss://api.vidu.cn/live/v1/external-lives/owned/stream',headers={'Authorization':'Token opaque-test'})
                self.assertEqual(request.await_count,1)
                self.assertIs(request.await_args.kwargs['allow_redirects'],False)
                self.assertEqual(request.await_args.kwargs['proxy'],'http://proxy.example:3128')
        finally: await http.close()
