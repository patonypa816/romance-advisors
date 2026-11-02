"""No provider sockets: local lock/process liveness plus original stop-only fake I/O."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from hell_inc.record_only import _write
from hell_inc.record_only_guard import DiagnosticGuard, watch, stop_owned, _prepare_stop
from test_m4_media_runtime import StopOnlyHTTP


class RecordGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.out=self.root/'attempt';self.out.mkdir()
        self.calls=[]
    async def stopper(self,out,owned):self.calls.append((out,owned));return True
    def manifest(self,**values):_write(self.out/'attempt.json',values)

    async def test_dead_parent_stops_only_its_persisted_id(self):
        self.manifest(create_attempted=True,owned_live_id='only-this-owned-id',create_started_monotonic=100)
        result=await watch(self.out,lambda:False,stop=self.stopper,clock=lambda:101)
        self.assertEqual(self.calls,[(self.out,'only-this-owned-id')]);self.assertTrue(result['cloud_end_confirmed'])
        self.assertTrue((self.out/'watchdog-stop.json').exists())

    async def test_evidence_disk_failure_cannot_prevent_owned_hangup(self):
        self.manifest(create_attempted=True,owned_live_id='disk-failure-owned',create_started_monotonic=100)
        with patch('hell_inc.record_only_guard._write',side_effect=OSError('disk full')):
            result=await watch(self.out,lambda:False,stop=self.stopper)
        self.assertTrue(result['cloud_end_confirmed'])
        self.assertEqual(self.calls,[(self.out,'disk-failure-owned')])

    async def test_deadline_waits_for_inflight_create_id_and_latches_stop(self):
        self.manifest(create_attempted=True,owned_live_id=None,create_started_monotonic=0)
        async def sleep(_):
            self.assertTrue((self.out/'watchdog-stop.json').exists())
            self.manifest(create_attempted=True,owned_live_id='late-owned',create_started_monotonic=0)
        result=await watch(self.out,lambda:True,stop=self.stopper,clock=lambda:181,sleep=sleep)
        self.assertTrue(result['stop_attempted']);self.assertEqual(self.calls[0][1],'late-owned')

    async def test_dead_parent_unknown_without_id_never_guesses(self):
        self.manifest(create_attempted=True,owned_live_id=None,create_started_monotonic=100)
        result=await watch(self.out,lambda:False,stop=self.stopper)
        self.assertEqual(result['state'],'unknown');self.assertEqual(self.calls,[])

    async def test_confirmed_ended_and_precreate_exit_do_not_stop(self):
        self.manifest(create_attempted=True,owned_live_id='owned',cloud_end_confirmed=True)
        self.assertEqual((await watch(self.out,lambda:False,stop=self.stopper))['state'],'ended')
        self.manifest(state='preparing')
        self.assertEqual((await watch(self.out,lambda:False,stop=self.stopper))['state'],'no_cloud_attempt')
        self.assertEqual(self.calls,[])

    async def test_ownership_mismatch_fails_closed(self):
        self.manifest(create_attempted=True,owned_live_id='id-a')
        (self.out/'component').mkdir();_write(self.out/'component'/'component-owned-id.json',{'live_id':'id-b'})
        self.assertEqual((await watch(self.out,lambda:False,stop=self.stopper))['state'],'unknown')
        self.assertEqual(self.calls,[])

    async def test_actual_original_stop_only_sends_type1_type5_never_create_or_audio(self):
        http=StopOnlyHTTP()
        with patch.dict(os.environ,{'VIDU_API_KEY':'test-opaque-key'},clear=True),patch('hell_inc.media_http.MediaHTTP',return_value=http):
            ended=await stop_owned(self.out,'durable-owned-test')
        self.assertTrue(ended);self.assertEqual([f['type'] for f in http.frames],[1,5])
        self.assertFalse(http.audio);self.assertTrue(all(method in ('GET','WSS') for method,_ in http.calls))
        self.assertTrue(all('durable-owned-test' in url for _,url in http.calls))


    async def test_post_readiness_disk_failure_still_reaches_original_hangup(self):
        http=StopOnlyHTTP()
        with patch.dict(os.environ,{'VIDU_API_KEY':'test-opaque-key'},clear=True),patch('hell_inc.media_http.MediaHTTP',return_value=http):
            prepared=_prepare_stop(self.out)
        prepared[0].END_TIMEOUT=.01;prepared[0].POLL_INTERVAL=.001
        with patch.object(Path,'mkdir',side_effect=OSError('disk unavailable')):
            self.assertTrue(await stop_owned(self.out,'durable-owned-test',prepared=prepared))
        self.assertEqual([f['type'] for f in http.frames],[1,5])
        self.assertFalse(http.audio)

    async def test_stop_owned_setup_does_not_require_writable_evidence_directory(self):
        http=StopOnlyHTTP()
        with patch.dict(os.environ,{'VIDU_API_KEY':'test-opaque-key'},clear=True),patch('hell_inc.media_http.MediaHTTP',return_value=http),patch.object(Path,'mkdir',side_effect=OSError('disk unavailable')):
            ended=await stop_owned(self.out,'durable-owned-test')
        self.assertTrue(ended);self.assertEqual([f['type'] for f in http.frames],[1,5])
        self.assertFalse(http.audio)


class RecordGuardProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
    def test_distinct_attempt_dirs_still_conflict_and_unknown_blocks_next(self):
        lock=self.root/'lock';first=DiagnosticGuard(self.root/'one',lock_path=lock).acquire()
        try:
            with self.assertRaises(BlockingIOError):DiagnosticGuard(self.root/'two',lock_path=lock).acquire()
            first.out.mkdir();_write(first.out/'attempt.json',{'create_attempted':True,'owned_live_id':None})
        finally:first.close()
        with self.assertRaisesRegex(RuntimeError,'previous_diagnostic_unknown'):
            DiagnosticGuard(self.root/'three',lock_path=lock).acquire()
        _write(first.out/'watchdog.json',{'cloud_end_confirmed':True})
        guard=DiagnosticGuard(self.root/'four',lock_path=lock).acquire();guard.close()

    def test_independent_child_detects_killed_parent_pipe_without_network(self):
        out=self.root/'killed';lock=self.root/'lock';ready=self.root/'ready'
        code='''import time,sys
from pathlib import Path
from hell_inc.record_only_guard import DiagnosticGuard
from hell_inc.record_only import _write
out,lock,ready=map(Path,sys.argv[1:])
g=DiagnosticGuard(out,lock_path=lock).acquire();g.start()
out.mkdir();_write(out/'attempt.json',{'create_attempted':True,'owned_live_id':None,'create_started_monotonic':time.monotonic()})
ready.write_text(str(g.child.pid))
time.sleep(30)
'''
        env={**os.environ,'VIDU_API_KEY':'no-real-requests-test'};env.pop('HELL_INC_VIDU_KEY',None)
        parent=subprocess.Popen([sys.executable,'-c',code,str(out),str(lock),str(ready)],env=env)
        try:
            deadline=time.monotonic()+8
            while not ready.exists() and time.monotonic()<deadline:time.sleep(.05)
            self.assertTrue(ready.exists());parent.kill();parent.wait(timeout=3)
            receipt=out/'watchdog.json';deadline=time.monotonic()+8
            while not receipt.exists() and time.monotonic()<deadline:time.sleep(.05)
            self.assertTrue(receipt.exists());result=json.loads(receipt.read_text())
            self.assertEqual(result['state'],'unknown');self.assertFalse(result['stop_attempted'])
            self.assertEqual(result['reason'],'created_id_unavailable')
            deadline=time.monotonic()+3
            while True:
                try:
                    with self.assertRaisesRegex(RuntimeError,'previous_diagnostic_unknown'):
                        DiagnosticGuard(self.root/'next',lock_path=lock).acquire()
                    break
                except BlockingIOError:
                    if time.monotonic()>deadline:raise
                    time.sleep(.05)
        finally:
            if parent.poll() is None:parent.kill();parent.wait(timeout=3)
