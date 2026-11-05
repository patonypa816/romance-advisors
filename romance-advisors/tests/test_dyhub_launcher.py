import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPO_ROOT / 'ai-live-avatar/studio/启动评论采集.command'
ZSH = shutil.which('zsh')

@unittest.skipUnless(ZSH, '需要 zsh 才能验证 macOS 评论采集启动器')
class DyHubLauncherTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dyhub-launcher-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'repo with spaces'
        self.root.mkdir()
        self.script = self.root/'ai-live-avatar/studio/启动评论采集.command'
        self.script.parent.mkdir(parents=True)
        shutil.copy2(SOURCE, self.script)
        self.bindir = self.root/'fake-bin'
        self.bindir.mkdir()
        node = self.bindir/'node'
        node.write_text('#!/bin/sh\nif [ "$1" = -e ]; then exit "$TEST_NODE_CHECK"; fi\nprintf "NODE_START|%s|%s|%s|%s|%s|%s\\n" "$PWD" "$*" "$DYHUB_HOST" "$DYHUB_PORT" "$DYHUB_COLLECTOR" "$DYHUB_TEST_SKIP_META"\n')
        node.chmod(0o755)

    def run_script(self, *, build=True, node_check=0, busy=False):
        if build:
            dist = self.root/'dyhub/dist/index.js'
            dist.parent.mkdir(parents=True)
            dist.write_text('// Synthetic fixture, never executed.\n')
        env = {'PATH': str(self.bindir) + os.pathsep + os.defpath, 'TEST_NODE_CHECK': str(node_check)}
        cmd = 'functions[/usr/sbin/lsof]="return '+('0' if busy else '1')+'"; source "$1"'
        return subprocess.run([ZSH,'-f','-c',cmd,str(self.script),str(self.script)], cwd='/',env=env,text=True,capture_output=True)

    def test_missing_build_does_not_install_or_start(self):
        r = self.run_script(build=False)
        self.assertEqual(r.returncode,1,r.stderr)
        self.assertIn('npm ci',r.stdout)
        self.assertNotIn('NODE_START',r.stdout)
        self.assertFalse((self.root/'dyhub').exists())

    def test_old_node_does_not_start(self):
        r = self.run_script(node_check=1)
        self.assertEqual(r.returncode,1,r.stderr)
        self.assertIn('需要 Node.js 20',r.stdout)
        self.assertNotIn('NODE_START',r.stdout)

    def test_busy_port_does_not_take_over(self):
        r = self.run_script(busy=True)
        self.assertEqual(r.returncode,1,r.stderr)
        self.assertIn('不会停止它',r.stdout)
        self.assertNotIn('NODE_START',r.stdout)

    def test_relative_repo_path_and_restricted_defaults(self):
        r = self.run_script()
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('NODE_START|'+str(self.root/'dyhub')+'|dist/index.js|127.0.0.1|8757|browser|1',r.stdout)

if __name__ == '__main__':
    unittest.main(verbosity=2)
