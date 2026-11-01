"""Offline credential-source tests; no network requests or real credentials."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import credentials


class EnvironmentCredentialsTest(unittest.TestCase):
    def test_bailian_environment_takes_precedence(self):
        with patch.dict(os.environ, {"BAILIAN_API_KEY": "sk-test-only", "BAILIAN_WORKSPACE_ID": "test-workspace"}, clear=True), \
             patch.object(credentials, "config", side_effect=AssertionError("legacy read")):
            self.assertEqual(credentials.load_bailian(), ("sk-test-only", "test-workspace"))

    def test_partial_environment_fails_closed(self):
        with patch.dict(os.environ, {"BAILIAN_API_KEY": "sk-test-only"}, clear=True), \
             patch.object(credentials, "config", side_effect=AssertionError("legacy read")):
            self.assertFalse(credentials.source_available(("BAILIAN_API_KEY", "BAILIAN_WORKSPACE_ID")))
            with self.assertRaises(ValueError):
                credentials.load_bailian()

    def test_vidu_environment_takes_precedence(self):
        with patch.dict(os.environ, {"VIDU_API_KEY": "vda_TEST_ONLY_0123456789"}, clear=True), \
             patch.object(credentials, "config", side_effect=AssertionError("legacy read")):
            self.assertEqual(credentials.load_vidu(), "vda_TEST_ONLY_0123456789")

    def test_legacy_fallback_and_presence(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {}, clear=True):
            root = Path(temp)
            csv = root / "bailian.csv"
            csv.write_text("apiKey,sk-test-only\nworkspaceId,test-workspace\napiHost,test-workspace.cn-beijing.maas.aliyuncs.com\n")
            vidu = root / "vidu.txt"
            vidu.write_text("vda_TEST_ONLY_0123456789")
            marker = root / "settings.json"
            marker.touch()
            with patch.object(credentials, "config", return_value={"bailian_credentials_file": str(csv), "vidu_credentials_file": str(vidu)}):
                self.assertTrue(credentials.source_available(("VIDU_API_KEY",), marker))
                self.assertEqual(credentials.load_bailian(), ("sk-test-only", "test-workspace"))
                self.assertEqual(credentials.load_vidu(), "vda_TEST_ONLY_0123456789")


if __name__ == "__main__":
    unittest.main()
