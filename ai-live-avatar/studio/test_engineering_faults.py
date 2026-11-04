"""Offline disk/socket fault injection; only temporary files and loopback HTTP."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_external_game as game_harness
import test_studio as studio_harness
import external_game
import live_session
from component_live import PersistentComponent
from motion_component import MotionComponent
from test_motion_component import short, GE
from test_semantic_transport import action, fixture


class AcceptanceAndCleanupFaults(unittest.IsolatedAsyncioTestCase):
    # Reuse the isolated renderer/credentials harness without inheriting its tests.
    asyncSetUp = game_harness.ExternalGameContracts.asyncSetUp
    asyncTearDown = game_harness.ExternalGameContracts.asyncTearDown
    synthesize = game_harness.ExternalGameContracts.synthesize
    post = game_harness.ExternalGameContracts.post
    claim = game_harness.ExternalGameContracts.claim
    live = game_harness.ExternalGameContracts.live
    submit = game_harness.ExternalGameContracts.submit
    get_job = game_harness.ExternalGameContracts.get_job

    async def test_repeated_get_and_duplicate_submit_are_read_only(self):
        await self.claim()
        self.live()
        await self.submit()
        s = self.studio.current
        await asyncio.wait_for(s.work.join(), 1)
        archived = json.loads((s.out / "external-game.json").read_text())["jobs"][0]
        self.assertEqual(archived["status"], "sent")
        with patch.object(external_game, "save", side_effect=AssertionError("GET wrote archive")) as save:
            for _ in range(5):
                response = await self.get_job()
                self.assertEqual((await response.json())["status"], "sent")
            self.assertEqual((await self.submit()).status, 200)
            save.assert_not_called()

    async def test_each_acceptance_archive_failure_has_terminal_idempotent_receipt(self):
        await self.claim()
        self.live()
        s = self.studio.current
        targets = ((studio_harness.studio_server, "plan"),
                   (live_session, "conversation"), (external_game, "external"))
        for module, label in targets:
            with self.subTest(stage=label):
                real_save = module.save
                failed = False
                def fail_once(path, value):
                    nonlocal failed
                    if not failed:
                        failed = True
                        raise OSError(28, "disk-full-with-sensitive-details")
                    return real_save(path, value)
                with patch.object(module, "save", side_effect=fail_once):
                    response = await self.submit(job_id=label)
                self.assertEqual(response.status, 200)
                receipt = await response.json()
                self.assertEqual(receipt["status"], "error")
                self.assertFalse(receipt["worker_pending"])
                self.assertFalse(receipt["delivery_started"])
                self.assertEqual(receipt["failure_code"], "external_accept_failed_before_queue")
                self.assertTrue(s.work.empty())
                archived = json.loads((s.out / "external-game.json").read_text())["jobs"][-1]
                self.assertEqual(archived["status"], "error")
                with patch.object(external_game, "save", side_effect=AssertionError("duplicate wrote")):
                    retry = await (await self.submit(job_id=label)).json()
                self.assertEqual(retry, receipt)
        self.tts.assert_not_awaited()
        self.assertNotIn("sensitive", json.dumps(s.snapshot()))

    async def test_enqueue_failure_is_terminal_and_never_revived(self):
        await self.claim()
        self.live()
        s = self.studio.current
        with patch.object(s.work, "put_nowait", side_effect=asyncio.QueueFull):
            receipt = await (await self.submit()).json()
        self.assertEqual(receipt["status"], "error")
        self.assertFalse(receipt["worker_pending"])
        self.assertEqual(await (await self.submit()).json(), receipt)
        self.assertTrue(s.work.empty())
        self.tts.assert_not_awaited()

    async def test_terminal_cancel_is_archived_without_get(self):
        await self.claim()
        self.live()
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await release.wait()
            return await self.synthesize(*args, **kwargs)
        self.tts.side_effect = blocked
        await self.submit()
        await asyncio.wait_for(entered.wait(), 1)
        await self.post("external/jobs/round:1/cancel", self.identity)
        release.set()
        s = self.studio.current
        await asyncio.wait_for(s.work.join(), 1)
        archived = json.loads((s.out / "external-game.json").read_text())["jobs"][0]
        self.assertEqual(archived["status"], "cancelled")
        self.assertFalse(archived["worker_pending"])

    async def test_final_checkpoint_failure_releases_join_stops_cloud_and_rejects_new_jobs(self):
        await self.claim()
        self.live()
        s = self.studio.current
        s.worker.cancel()  # Keep the real queue entry pending until fault injection.
        await asyncio.gather(s.worker, return_exceptions=True)
        await self.submit()
        item = self.studio.external_game.jobs["round:1"]["item"]
        item["status"] = "cancelled"
        with patch.object(s, "checkpoint", side_effect=OSError(28, "disk full")):
            s.worker = asyncio.create_task(s.process_answers())
            await asyncio.wait_for(s.work.join(), 1)
            await asyncio.wait_for(s.worker, 1)
        self.assertFalse(item["external_worker_pending"])
        self.assertTrue(s.stopping)
        self.assertTrue(s.component.stopped)
        self.assertEqual(s.worker_fault["code"], "answer_worker_failed")
        self.assertEqual((await self.submit(job_id="later")).status, 409)
        self.assertEqual((await self.get_job()).status, 200)
        self.tts.assert_not_awaited()

    async def test_cancelled_busy_worker_has_terminal_receipt_and_stops_without_retry(self):
        await self.claim()
        self.live()
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.tts.side_effect = blocked
        await self.submit()
        await asyncio.wait_for(entered.wait(), 1)
        s = self.studio.current
        s.worker.cancel()
        await asyncio.wait_for(asyncio.gather(s.worker, return_exceptions=True), 1)
        await asyncio.wait_for(s.work.join(), 1)
        receipt = await (await self.get_job()).json()
        self.assertEqual(receipt["status"], "cancelled")
        self.assertFalse(receipt["worker_pending"])
        self.assertFalse(receipt["delivery_started"])
        self.assertTrue(s.component.stopped)
        self.assertEqual((await self.submit(job_id="after-cancel")).status, 409)
        self.assertEqual(self.tts.await_count, 1)

    async def test_stop_checkpoint_failure_preserves_first_error_and_still_stops(self):
        s = self.studio.current
        first = OSError(28, "disk full secret")
        s.component.stop = AsyncMock(side_effect=ConnectionError("provider URL"))
        with patch.object(s, "checkpoint", side_effect=first):
            with self.assertRaises(OSError) as raised:
                await s.request_stop("offline")
        self.assertIs(raised.exception, first)
        s.component.stop.assert_awaited_once()
        self.assertEqual(s.failure, {"stage": "stop_checkpoint", "error_type": "OSError", "errno": 28})
        self.assertEqual(s.cleanup_errors[-1]["stage"], "component_stop")
        self.assertNotIn("secret", json.dumps(s.snapshot()))
        s.component.stop = AsyncMock(side_effect=s.component.__class__.stop.__get__(s.component))

    async def test_disk_and_component_close_failures_do_not_leak_http(self):
        s = self.studio.current
        s.component.close = AsyncMock(side_effect=OSError(5, "close failed"))
        with patch.object(s, "checkpoint", side_effect=OSError(28, "disk full")):
            await s.shutdown(None)
        self.assertTrue(s.component.stopped)
        self.assertTrue(s.http.closed)
        s.component.close.assert_awaited_once()
        self.assertEqual(s.failure["stage"], "stop_checkpoint")

    async def test_finished_watchdog_exception_cannot_skip_socket_or_http_cleanup(self):
        s = self.studio.current
        async def failed_watchdog():
            raise OSError(5, "watchdog failed")
        s.watchdog = asyncio.create_task(failed_watchdog())
        await asyncio.sleep(0)
        self.assertTrue(s.watchdog.done())
        s.component.close = AsyncMock()
        await s.shutdown(None)
        s.component.close.assert_awaited_once()
        self.assertTrue(s.http.closed)
        self.assertEqual(s.failure["stage"], "shutdown_watchdog")

    async def test_queue_discard_errors_do_not_skip_remaining_items_or_cloud_stop(self):
        await self.claim()
        self.live()
        s = self.studio.current
        s.worker.cancel()
        await asyncio.gather(s.worker, return_exceptions=True)
        items = []
        for index in range(2):
            item = {"id": f"queued-{index}", "status": "ready", "external_worker_pending": True}
            items.append(item)
            s.work.put_nowait(item)
        with patch.object(s, "discard_prepared", side_effect=OSError(5, "discard failed")) as discard:
            await s.fail_worker(RuntimeError("worker failed"))
        await asyncio.wait_for(s.work.join(), 1)
        self.assertEqual(discard.call_count, 2)
        self.assertTrue(all(i["status"] == "cancelled" and not i["external_worker_pending"] for i in items))
        self.assertTrue(s.component.stopped)
        self.assertTrue(s.work.empty())
        self.assertEqual([e["stage"] for e in s.cleanup_errors].count("worker_queue_cleanup"), 2)


class BlockingSocket:
    def __init__(self, *, json_kind=None, audio_frame=None, close_blocked=False, mark_closed_before_wait=False):
        self.closed = False
        self.json_kind, self.audio_frame = json_kind, audio_frame
        self.close_blocked = close_blocked
        self.mark_closed_before_wait = mark_closed_before_wait
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.json_attempts, self.audio_attempts, self.binary = [], 0, []
        self.abort = Mock(side_effect=lambda: setattr(self, "closed", True))
        self._response = SimpleNamespace(connection=SimpleNamespace(transport=SimpleNamespace(abort=self.abort)))

    async def send_json(self, frame):
        self.json_attempts.append(frame["type"])
        if frame["type"] == self.json_kind:
            self.entered.set()
            await self.release.wait()

    async def send_bytes(self, chunk):
        self.audio_attempts += 1
        if self.audio_attempts == self.audio_frame:
            self.entered.set()
            await self.release.wait()
        self.binary.append(chunk)

    async def close(self):
        if self.close_blocked:
            if self.mark_closed_before_wait:
                self.closed = True
            await asyncio.Event().wait()
        self.closed = True


class TransportFaults(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.out = Path(self.temporary.name)
        self.component = PersistentComponent(SimpleNamespace(), self.out, self.out / "unused.png")
        c = self.component
        c.WRITE_TIMEOUT = c.CLOSE_TIMEOUT = .03
        c.END_TIMEOUT = .1
        c.live_id, c.creation_attempted = "same-offline-live", True
        c.comment_id, c.job_id = "c0001", "round:offline"
        c.state = "ready"
        c._ready.set()
        c._joined.set()
        c._connect = AsyncMock()  # Never touches HTTP or credentials.
        c._collect_recording = AsyncMock()
        c._wait = AsyncMock()
        async def ended(method, path, **kwargs):
            self.assertTrue(self.socket.closed)
            self.assertEqual(path, "/live/v1/lives/same-offline-live")
            return {"live": {"status": "ended"}}
        c._json = AsyncMock(side_effect=ended)

    async def exercise_blocked_writer(self, *, audio=False, cancel=False, close_blocked=False):
        c = self.component
        self.socket = c._ws = BlockingSocket(json_kind=None if audio else 9,
            audio_frame=2 if audio else None, close_blocked=close_blocked)
        pcm = bytes(c.FRAME_BYTES * (2 if audio else 1))
        sender = asyncio.create_task(c.send_audio(pcm, "你好", "点头" if not audio else ""))
        await asyncio.wait_for(self.socket.entered.wait(), 1)
        queued = asyncio.create_task(c.send_audio(pcm, "后续"))
        if cancel:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        receipt = await asyncio.wait_for(c.stop(), .5)
        await asyncio.wait_for(asyncio.gather(sender, queued, return_exceptions=True), .5)
        self.assertTrue(receipt["cloud_end_confirmed"])
        self.assertEqual(c.transport_unknown["operation"], "audio" if audio else "json")
        self.assertEqual(c.last_utterance["delivery_outcome"], "unknown")
        self.assertFalse(c.last_utterance["send_complete"])
        self.assertEqual(self.socket.json_attempts.count(10), int(audio))
        self.assertEqual(self.socket.audio_attempts, 2 if audio else 0)
        self.assertEqual(c.sent_audio_bytes, c.FRAME_BYTES if audio else 0)
        with self.assertRaises(RuntimeError):
            await c.send_audio(pcm, "拒绝补播")
        await c._recording_task
        return receipt

    async def test_json_write_forever_stop_is_bounded_and_queue_never_sends(self):
        await self.exercise_blocked_writer()

    async def test_audio_write_forever_preserves_first_frame_and_stops(self):
        await self.exercise_blocked_writer(audio=True)
        records = list(self.component.diagnostics.records)
        first = next(r for r in records if r["kind"] == "audio_first_frame")
        self.assertEqual(first["details"]["job_id"], "round:offline")
        self.assertFalse(any(r["kind"] == "audio_last_frame" for r in records))

    async def test_cancel_during_json_write_retains_unknown_and_stops_same_live(self):
        await self.exercise_blocked_writer(cancel=True)

    async def test_socket_close_forever_aborts_before_same_live_readback(self):
        await self.exercise_blocked_writer(close_blocked=True)
        self.socket.abort.assert_called_once()

    async def test_aiohttp_closed_flag_before_drain_still_requires_abort(self):
        c = self.component
        self.socket = c._ws = BlockingSocket(close_blocked=True, mark_closed_before_wait=True)
        await asyncio.wait_for(c.stop(), .5)
        self.socket.abort.assert_called_once()
        self.assertTrue(c.cloud_end_confirmed)
        await c._recording_task

    async def test_cancelling_stop_caller_does_not_cancel_cloud_cleanup(self):
        c = self.component
        self.socket = c._ws = BlockingSocket(json_kind=5)
        stop = asyncio.create_task(c.stop())
        await asyncio.wait_for(self.socket.entered.wait(), 1)
        stop.cancel()
        result = await asyncio.wait_for(asyncio.gather(stop, return_exceptions=True), .5)
        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertTrue(self.socket.closed)
        self.assertTrue(c.cloud_end_confirmed)
        await c._recording_task

    async def test_close_receipt_disk_failure_cancels_recording_and_erases_credentials(self):
        c = self.component
        self.socket = c._ws = BlockingSocket()
        c.cloud_end_confirmed = True
        c._key, c._rtc = "offline-private-key", {"token": "offline-private-rtc"}
        exited = asyncio.Event()
        async def pending_recording():
            try:
                await asyncio.Event().wait()
            finally:
                exited.set()
        c._recording_task = asyncio.create_task(pending_recording())
        await asyncio.sleep(0)
        first = OSError(28, "receipt disk full with private details")
        with patch.object(c, "_save", side_effect=first):
            with self.assertRaises(OSError) as raised:
                await asyncio.wait_for(c.close(), .5)
        self.assertIs(raised.exception, first)
        self.assertTrue(c._recording_task.done())
        self.assertTrue(exited.is_set())
        self.assertTrue(self.socket.closed)
        self.assertEqual(c._rtc, {})
        self.assertEqual(c._key, "")
        self.assertEqual(c.close_failure, {"stage": "stop", "error_type": "OSError", "errno": 28})
        self.assertNotIn("offline-private", json.dumps(c.public()))
        c._json.assert_not_awaited()

    async def test_close_keeps_first_error_across_owner_recording_and_socket_failures(self):
        c = self.component
        c._key, c._rtc = "offline-private-key", {"token": "offline-private-rtc"}
        first = OSError(28, "first disk failure")
        c.stop = AsyncMock(side_effect=first)
        async def fail_task():
            raise OSError(5, "later cleanup failure")
        c._task = asyncio.create_task(fail_task())
        c._recording_task = asyncio.create_task(fail_task())
        await asyncio.sleep(0)
        c._close_ws = AsyncMock(side_effect=ConnectionError("socket failure"))
        with self.assertRaises(OSError) as raised:
            await c.close()
        self.assertIs(raised.exception, first)
        self.assertEqual([e["stage"] for e in c.close_cleanup_errors],
                         ["owner_task", "recording_task", "websocket_close"])
        self.assertEqual(c._rtc, {})
        self.assertEqual(c._key, "")
        c._close_ws.assert_awaited_once()

    async def test_successful_close_still_waits_for_recording(self):
        c = self.component
        self.socket = c._ws = BlockingSocket()
        c.cloud_end_confirmed = True
        entered, release = asyncio.Event(), asyncio.Event()
        async def recording():
            entered.set()
            await release.wait()
        c._recording_task = asyncio.create_task(recording())
        await entered.wait()
        closer = asyncio.create_task(c.close())
        await asyncio.sleep(0)
        self.assertFalse(closer.done())
        release.set()
        await asyncio.wait_for(closer, .5)
        self.assertFalse(c._recording_task.cancelled())
        self.assertIsNone(c.close_failure)

    async def test_lock_wait_has_deadline_and_still_closes_socket(self):
        c = self.component
        self.socket = c._ws = BlockingSocket()
        await c._send_lock.acquire()
        try:
            receipt = await asyncio.wait_for(c.stop(), .5)
        finally:
            c._send_lock.release()
        self.assertTrue(receipt["cloud_end_confirmed"])
        self.assertEqual(self.socket.json_attempts, [])
        self.assertIsNone(c.transport_unknown)  # No write attempt crossed the lock.
        await c._recording_task

    async def motion_component(self, **socket_options):
        old = self.component
        c = self.component = MotionComponent(SimpleNamespace(), self.out, self.out / "unused.png")
        for field in ("WRITE_TIMEOUT", "CLOSE_TIMEOUT", "END_TIMEOUT", "live_id", "creation_attempted",
                      "comment_id", "job_id", "_connect", "_collect_recording", "_wait", "_json"):
            setattr(c, field, getattr(old, field))
        c.state = "ready"
        c._ready.set()
        c._joined.set()
        self.socket = c._ws = BlockingSocket(**socket_options)
        return c

    async def test_real_first_ge_timeout_never_sends_s_q_or_followup(self):
        c = await self.motion_component(json_kind=9)
        sender = asyncio.create_task(c.send_motion_expression([short()], number=1, recovery=GE, dialogue_motion=True))
        await asyncio.wait_for(self.socket.entered.wait(), 1)
        queued = asyncio.create_task(c.send_motion_expression([short()], number=2, recovery=GE, dialogue_motion=True))
        results = await asyncio.wait_for(asyncio.gather(c.stop(), c.stop(), sender, queued,
                                                       return_exceptions=True), .5)
        self.assertTrue(results[0]["cloud_end_confirmed"])
        self.assertTrue(results[1]["cloud_end_confirmed"])
        self.assertEqual(self.socket.json_attempts.count(9), 1)
        self.assertEqual(c.motion_events, [])
        self.assertEqual(c.transport_unknown["operation"], "motion")
        self.assertEqual(c.last_motion_receipt["delivery_outcome"], "unknown")
        self.assertFalse(c.last_motion_receipt["send_complete"])
        await c._recording_task

    async def test_real_audio_timeout_cancels_scheduled_q_without_replay(self):
        c = await self.motion_component(audio_frame=2)
        pcm, alignment = fixture(4, (.08,))
        sender = asyncio.create_task(c.send_aligned_expression(pcm, "甲", [action()], 1,
            recovery=GE, alignment=alignment, action_kind="S", ge_lead_seconds=1.1,
            allow_tail_q_after_audio=True))
        await asyncio.wait_for(self.socket.entered.wait(), 1)
        before = self.socket.json_attempts.count(9)
        await asyncio.wait_for(c.stop(), .5)
        await asyncio.wait_for(asyncio.gather(sender, return_exceptions=True), .5)
        self.assertEqual(self.socket.json_attempts.count(9), before)
        self.assertFalse(any(e["role"] == "Q" for e in c.motion_events))
        self.assertEqual(c.last_motion_receipt["delivery_outcome"], "unknown")
        self.assertEqual(c.sent_audio_bytes, c.FRAME_BYTES)
        self.assertFalse(c._motion_task and not c._motion_task.done())
        await c._recording_task


if __name__ == "__main__":
    unittest.main()
