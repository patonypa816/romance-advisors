"""Operator-owned references in synthetic temporary files; no providers contacted."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import credentials


class OperatorCredentialReferences(unittest.TestCase):
    def test_default_location_is_neutral_and_shared_with_game(self):
        expected = Path(__file__).resolve().parents[2] / ".local/local-settings.json"
        self.assertEqual(credentials.LOCAL_SETTINGS, expected)

    def test_only_explicit_files_are_read_without_copying_keys_into_settings(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            ordinary = root / "operator-bailian.csv"
            ordinary.write_text("apiKey,sk-synthetic-ordinary\nworkspaceId,synthetic\napiHost,synthetic.cn-beijing.maas.aliyuncs.com\n")
            vidu = root / "operator-vidu.txt"
            vidu.write_text("vda_synthetic_operator_key_only")
            settings = root / ".local/local-settings.json"
            settings.parent.mkdir()
            original = json.dumps({"bailian_credentials_file": str(ordinary), "vidu_credentials_file": str(vidu)})
            settings.write_text(original)
            with patch.object(credentials, "LOCAL_SETTINGS", settings):
                self.assertEqual(credentials.load_bailian(), ("sk-synthetic-ordinary", "synthetic"))
                self.assertEqual(credentials.load_vidu(), "vda_synthetic_operator_key_only")
            self.assertEqual(settings.read_text(), original)
            self.assertNotIn("sk-synthetic", settings.read_text())
            self.assertNotIn("vda_", settings.read_text())

    def test_missing_or_invalid_settings_do_not_search_other_accounts(self):
        with tempfile.TemporaryDirectory() as name:
            settings = Path(name) / "local-settings.json"
            with patch.object(credentials, "LOCAL_SETTINGS", settings):
                for loader in (credentials.load_bailian, credentials.load_vidu):
                    with self.assertRaises(FileNotFoundError):
                        loader()
                settings.write_text("{}")
                for loader in (credentials.load_bailian, credentials.load_vidu):
                    with self.assertRaises(KeyError):
                        loader()


if __name__ == "__main__":
    unittest.main()
