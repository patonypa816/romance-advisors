"""Offline shell contracts: dummy executables, no downloads or package installs."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parent


class CloudPreparationContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "tools").mkdir()
        (self.root / "bin").mkdir()
        for name in ("fetch_vendor.sh", "prepare_cloud.sh"):
            shutil.copyfile(TOOLS / name, self.root / "tools" / name)
        self.env = {"PATH": str(self.root / "bin") + os.pathsep + os.defpath,
                    "TRACE": str(self.root / "trace")}
        self.target = self.root / "static/vendor/aliyun-rtc-sdk-7.1.9.js"
        self.target.parent.mkdir(parents=True)
        self.fake("sha256sum", """IFS= read -r line
path=${line#*  }
[ "$(cat "$path")" = dummy-verified-sdk ]
""")
        self.fake("curl", """echo curl >> "$TRACE"
while [ "$#" -gt 0 ]; do
  if [ "$1" = -o ]; then shift; target=$1; fi
  shift
done
printf '%s' "${SDK_BYTES:-dummy-verified-sdk}" > "$target"
""")

    def fake(self, name, body):
        target = self.root / "bin" / name
        target.write_text("#!/bin/sh\nset -eu\n" + body)
        target.chmod(0o755)

    def run_script(self, name, *args):
        return subprocess.run(["sh", str(self.root / "tools" / name), *args],
                              cwd="/", env=self.env, capture_output=True, text=True)

    def test_verified_existing_sdk_never_downloads(self):
        self.target.write_text("dummy-verified-sdk")
        self.assertEqual(self.run_script("fetch_vendor.sh").returncode, 0)
        self.assertFalse((self.root / "trace").exists())

    def test_download_and_repeat_are_idempotent(self):
        self.assertEqual(self.run_script("fetch_vendor.sh").returncode, 0)
        self.assertEqual(self.target.read_text(), "dummy-verified-sdk")
        self.assertEqual(self.run_script("fetch_vendor.sh").returncode, 0)
        self.assertEqual((self.root / "trace").read_text(), "curl\n")
        self.assertEqual(list(self.target.parent.glob("*.tmp.*")), [])

    def test_checksum_failure_preserves_target_and_cleans_temp(self):
        self.target.write_text("old-unverified-file")
        self.env["SDK_BYTES"] = "dummy-corrupt"
        self.assertNotEqual(self.run_script("fetch_vendor.sh").returncode, 0)
        self.assertEqual(self.target.read_text(), "old-unverified-file")
        self.assertEqual(list(self.target.parent.glob("*.tmp.*")), [])

    def test_prepare_reuses_venv_and_does_not_source_env_or_start_server(self):
        self.fake("python3", '[ "$1" = -c ] || exit 99\n')
        venv = self.root / ".venv/bin"
        venv.mkdir(parents=True)
        (venv / "python").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TRACE"\n')
        (venv / "python").chmod(0o755)
        (self.root / ".env").write_text("exit 99\n")
        self.target.write_text("dummy-verified-sdk")
        for _ in range(2):
            self.assertEqual(self.run_script("prepare_cloud.sh").returncode, 0)
        self.assertEqual((self.root / "trace").read_text().splitlines(),
                         ["-c import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)",
                          "-m pip install -r requirements.txt"] * 2)
        self.assertFalse((self.root / "studio/data").exists())

    def test_unknown_argument_does_not_install(self):
        self.assertNotEqual(self.run_script("prepare_cloud.sh", "--start").returncode, 0)
        self.assertFalse((self.root / "trace").exists())


if __name__ == "__main__":
    unittest.main()
