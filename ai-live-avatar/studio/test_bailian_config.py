"""Model routing must not fall back on damaged options or copy credentials."""
import json
from pathlib import Path
import tempfile
import unittest

from credentials import bailian_config, cloudbase_config
from cloudbase_ai import CloudBaseAI, ModelError


class BailianConfigContracts(unittest.TestCase):
    def test_absent_file_defaults_to_selected_model_without_writing_files(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.assertEqual(bailian_config(root), {
                "model": "qwen3.8-flash", "region": "cn-beijing", "enable_thinking": False})
            self.assertEqual(list(root.iterdir()), [])

    def test_damaged_unknown_or_oversized_file_is_not_silently_replaced(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            target = root / "bailian-brain.json"
            for text in ('{', '[]', ' ' * 16385, json.dumps({"api_key": "offline-placeholder"}),
                         json.dumps({"endpoint": "https://untrusted.invalid"})):
                with self.subTest(text=text[:80]):
                    target.write_text(text)
                    self.assertEqual(bailian_config(root), {})
                    self.assertEqual(target.read_text(), text)

    def test_explicit_wrong_model_is_preserved_for_client_rejection(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "bailian-brain.json").write_text(json.dumps({"model": "old-provider-model"}))
            self.assertEqual(bailian_config(root)["model"], "old-provider-model")


class CloudBaseConfigContracts(unittest.TestCase):
    def test_missing_environment_blocks_only_retained_cloudbase_adapter(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            options = cloudbase_config(root)
            self.assertEqual(options["env_id"], "")
            client = CloudBaseAI(options)
            with self.assertRaisesRegex(ModelError, "configuration_invalid"):
                client._validate_config()
            self.assertFalse(client.configured())
            self.assertEqual(bailian_config(root)["model"], "qwen3.8-flash")
            self.assertEqual(list(root.iterdir()), [])

    def test_operator_environment_and_external_reference_are_preserved(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "cloudbase.json").write_text(json.dumps({
                "env_id": "synthetic-cloudbase-offline", "credentials_file": "/not-read/offline.csv"}))
            options = cloudbase_config(root)
            self.assertEqual(options["env_id"], "synthetic-cloudbase-offline")
            self.assertEqual(options["credentials_file"], "/not-read/offline.csv")
            CloudBaseAI(options)._validate_config()

if __name__ == "__main__":
    unittest.main()
