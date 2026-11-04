"""Token Plan is isolated from ordinary TTS credentials; synthetic files only."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import credentials
from bailian_ai import BailianAI
from test_bailian_ai import CONFIG, Factory, KEY, SELECTION, BATCH, CONTEXT


class TokenPlanCredentialsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.key = self.root / "key.txt"
        self.key.write_text("sk-sp-synthetic-only-plan-key\n")
        self.route = self.root / "route.json"
        self.route.write_text(json.dumps({"billing": "token_plan", "api_key_file": str(self.key)}))
        patcher = patch.object(credentials, "QWEN_ROUTE_FILE", self.route)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_qwen_uses_plan_while_tts_uses_original_csv(self):
        csv = self.root / "tts.csv"
        csv.write_text("apiKey,sk-synthetic-ordinary-tts\nworkspaceId,offline\napiHost,offline.cn-beijing.maas.aliyuncs.com\n")
        with patch.object(credentials, "config", return_value={"bailian_credentials_file": str(csv)}):
            self.assertEqual(credentials.load_bailian(), ("sk-synthetic-ordinary-tts", "offline"))
            self.assertEqual(credentials.load_bailian_qwen()[1], "token-plan")
            self.assertTrue(credentials.load_bailian_qwen()[0].startswith("sk-sp-"))
            self.assertNotIn("sk-sp-", self.route.read_text())

    def test_invalid_plan_route_fails_without_loading_tts_key(self):
        with patch.object(credentials, "load_bailian", side_effect=AssertionError("ordinary fallback")):
            for raw in ("broken", json.dumps({"billing": "token_plan", "api_key_file": "relative.txt"}),
                        json.dumps({"billing": "token_plan", "api_key_file": str(self.root / "missing.txt")})):
                self.route.write_text(raw)
                with self.assertRaises((ValueError, OSError)):
                    credentials.load_bailian_qwen()

    def test_absent_route_preserves_legacy_independent_configuration(self):
        with patch.object(credentials, "QWEN_ROUTE_FILE", self.root / "absent.json"), \
             patch.object(credentials, "load_bailian", return_value=("sk-synthetic", "offline")) as legacy:
            self.assertEqual(credentials.load_bailian_qwen(), ("sk-synthetic", "offline"))
            legacy.assert_called_once()


class TokenPlanTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_endpoint_is_used_and_route_is_visible_without_key(self):
        factory = Factory()
        client = BailianAI(CONFIG, credentials_loader=lambda: (KEY, "token-plan"), session_factory=factory)
        try:
            self.assertEqual(await client.select_comments(BATCH, CONTEXT), SELECTION)
            self.assertEqual(factory.calls[0][0], "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions")
            self.assertEqual(client.public()["billing_route"], "token_plan")
            self.assertEqual(client.public()["last_billing_route"], "token_plan")
            self.assertNotIn(KEY, json.dumps(client.public()))
        finally:
            await client.close()
