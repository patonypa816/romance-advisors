"""Offline-only synthetic connection tests: no sockets, services or real secrets."""
import ast
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import aiohttp
import bailian_ai
import avatar_library
import idle_action_library
import connection
import credentials

CONFIG = {"model": bailian_ai.MODEL, "region": bailian_ai.REGION, "enable_thinking": False}


class CredentialConnectionTest(unittest.TestCase):
    def test_environment_keys_and_brain(self):
        for key in ("sk-synthetic-12345", "opaque-placeholder_123", "x"):
            with self.subTest(key=key), patch.dict(os.environ, {
                    "BAILIAN_API_KEY": key, "BAILIAN_WORKSPACE_ID": "test-workspace",
                    "VIDU_API_KEY": key}, clear=True), \
                    patch.object(credentials, "config", side_effect=AssertionError("legacy read")):
                self.assertEqual(credentials.load_bailian(), (key, "test-workspace"))
                self.assertEqual(credentials.load_vidu(), key)
                self.assertEqual(bailian_ai.BailianAI(CONFIG)._credentials(), (key, "test-workspace"))

    def test_bad_environment_never_falls_back(self):
        for key in ("", " ", "opaque\r", "opaque\n", "a\tb", "a b", "é", "x" * 4097):
            with self.subTest(length=len(key)), patch.dict(os.environ, {
                    "BAILIAN_API_KEY": key, "DASHSCOPE_API_KEY": "sk-good-alias",
                    "BAILIAN_WORKSPACE_ID": "test-workspace", "VIDU_API_KEY": key}, clear=True), \
                    patch.object(credentials, "config", side_effect=AssertionError("legacy read")):
                with self.assertRaises(ValueError):
                    credentials.load_bailian()
                with self.assertRaises(ValueError):
                    credentials.load_vidu()
                with self.assertRaises(bailian_ai.ModelError):
                    bailian_ai.BailianAI(CONFIG)._credentials()

    def test_nul_is_not_header_safe(self):
        self.assertFalse(credentials.header_safe_key("a\x00b"))

    def test_opaque_custom_loader_without_environment_still_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            brain = bailian_ai.BailianAI(CONFIG, credentials_loader=lambda: ("opaque", "workspace"))
            with self.assertRaises(bailian_ai.ModelError):
                brain._credentials()
            brain = bailian_ai.BailianAI(CONFIG, credentials_loader=lambda: ("sk-synthetic-12345", "workspace"))
            self.assertEqual(brain._credentials()[0], "sk-synthetic-12345")

    def test_no_proxy_retains_constructor_arguments(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(connection.aiohttp, "ClientSession") as factory:
            connection.client_session(trust_env=False)
            factory.assert_called_once_with(trust_env=False)

    def test_old_aiohttp_explicit_failure_only_with_proxy(self):
        with patch.object(connection, "_SUPPORTS_PROXY", False), \
                patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.invalid:8080"}, clear=True), \
                patch.object(connection.aiohttp, "ClientSession") as factory:
            with self.assertRaisesRegex(RuntimeError, "requires aiohttp"):
                connection.client_session()
            factory.assert_not_called()

    def test_original_audio_download_explicitly_clears_auth(self):
        tree = ast.parse(Path(__file__).with_name("tts_live.py").read_text())
        downloads = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                     and node.args and isinstance(node.args[0], ast.Name)
                     and node.args[0].id == "audio_url"]
        self.assertEqual(len(downloads), 1)
        options = {kw.arg: ast.literal_eval(kw.value) for kw in downloads[0].keywords
                   if kw.arg in ("headers", "allow_redirects")}
        self.assertEqual(options, {"headers": {"Authorization": ""}, "allow_redirects": False})


class SessionConnectionTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_api_factories_preserve_auth_and_proxy(self):
        seen = []

        async def intercept(request, *args, **kwargs):
            seen.append(request)
            raise OSError("offline socket blocked")

        with patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.invalid:8080",
                                    "VIDU_API_KEY": "opaque-test-only"}, clear=True), \
                patch.object(aiohttp.TCPConnector, "connect", side_effect=intercept):
            brain = bailian_ai.BailianAI(CONFIG)
            with self.assertRaises(aiohttp.ClientError):
                await brain._request("workspace", "opaque-test-only", b"{}")
            avatar = avatar_library.ViduAvatarClient()
            idle = idle_action_library.ViduIdleActionClient()
            try:
                with self.assertRaises(avatar_library.AvatarError):
                    await avatar.list_page(1, 10)
                with self.assertRaises(idle_action_library.IdleActionError):
                    await idle._get("/live/v1/offline-test")
                self.assertIsNone(avatar._images)  # Restricted image path untouched.
            finally:
                await avatar.close()
                await idle.close()
        self.assertEqual(len(seen), 3)
        self.assertEqual([r.headers["Authorization"] for r in seen],
                         ["Bearer opaque-test-only", "Token opaque-test-only", "Token opaque-test-only"])
        for request in seen:
            self.assertEqual(str(request.proxy), "http://proxy.invalid:8080")
            self.assertIs(request.ssl, True)

    async def test_http_and_wss_proxy_and_tls_without_opening_socket(self):
        class NoSocket(Exception):
            pass

        for environment in ({}, {"HTTPS_PROXY": "http://proxy.invalid:8080"}):
            for websocket in (False, True):
                seen = []

                async def intercept(request, *args, **kwargs):
                    seen.append(request)
                    raise NoSocket()

                with self.subTest(environment=bool(environment), websocket=websocket), \
                        patch.dict(os.environ, environment, clear=True):
                    async with connection.client_session() as session:
                        self.assertFalse(session.trust_env)
                        self.assertNotIn("Authorization", session.headers)
                        with patch.object(session.connector, "connect", side_effect=intercept):
                            with self.assertRaises(NoSocket):
                                if websocket:
                                    await session.ws_connect("wss://api.vidu.cn/offline-test")
                                else:
                                    await session.get("https://api.vidu.cn/offline-test")
                        self.assertEqual(len(seen), 1)
                        self.assertEqual(str(seen[0].proxy) if seen[0].proxy else None,
                                         environment.get("HTTPS_PROXY"))
                        self.assertIs(seen[0].ssl, True)
                        self.assertIsNone(seen[0].proxy_auth)


if __name__ == "__main__":
    unittest.main()
