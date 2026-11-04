"""Instance identity must survive stale or copied runtime files safely."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from romance import launcher


class LauncherTests(unittest.TestCase):
    def check_instance(self, foreign=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = str((root / 'data/game.sqlite').resolve())
            claimed = '/another-copy/data/game.sqlite' if foreign else database
            runtime = root / 'runtime.json'
            record = {'app': 'romance-advisors', 'pid': 1234,
                      'url': 'http://127.0.0.1:54321', 'database': claimed}
            runtime.write_text(json.dumps(record))
            with patch.object(launcher, 'ROOT', root), patch.object(launcher, 'RUNTIME', runtime), \
                 patch.object(launcher.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(record).encode())):
                return launcher.running()

    def test_same_project_instance_is_reused(self):
        self.assertIsNotNone(self.check_instance())

    def test_copied_runtime_cannot_select_foreign_instance(self):
        self.assertIsNone(self.check_instance(foreign=True))
