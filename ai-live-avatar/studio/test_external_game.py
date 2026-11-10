"""Frozen game speech through the real session worker, with no paid services."""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

import test_studio as fixtures
import live_session
from external_game import ExternalGame, LEGACY_INSTRUCTION
from motion_component import MotionComponent
from test_motion_component import FakeSocket
from test_semantic_progress import ManualClock


class SpeechComponent(fixtures.FakeComponent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.send_aligned_expression = AsyncMock(side_effect=self._send)

    async def _send(self, pcm, text, actions, segment_index, **kwargs):
        if self.state == "stopping":
            raise RuntimeError("stopped")
        return {"group_id": "offline-group", "segment_index": segment_index,
                "send_complete": True, "handoff_wait_elapsed": True, "status": "sent"}


class ExternalWaitReceiptTests(unittest.TestCase):
    def test_short_action_progress_is_scoped_to_current_motion_job(self):
        item = {"id": "comment-one", "status": "sending", "external_job_id": "thinking",
                "external_generation": "current", "external_owner_id": "owner"}
        events = []
        external = ExternalGame(SimpleNamespace(generation="current", current=SimpleNamespace(
            component=SimpleNamespace(motion_events=events))))
        external.owner_id, external.generation = "owner", "current"
        job = {"job_id": "thinking", "mode": "motion", "item": item}
        external.jobs[job["job_id"]] = job
        event = {"role": "S", "comment_id": "comment-one", "job_id": "thinking"}
        for changed in ({"role": "GE"}, {"role": "Q"}, {"comment_id": "other"},
                        {"job_id": "other"}, {"job_id": None}):
            events[:] = [{**event, **changed}]
            self.assertFalse(external.job_public(job)["short_action_sent"])
        events[:] = [event]
        self.assertTrue(external.job_public(job)["short_action_sent"])
        for field, value in (("external_generation", "previous"), ("external_owner_id", "other"),
                             ("external_job_id", "other")):
            original = item[field]
            item[field] = value
            self.assertFalse(external.job_public(job)["short_action_sent"])
            item[field] = original
        external.studio.generation = "new-session"
        self.assertFalse(external.job_public(job)["short_action_sent"])
        external.studio.generation = "current"
        job["mode"] = "speech"
        self.assertFalse(external.job_public(job)["short_action_sent"])
        job["mode"] = "motion"
        self.assertFalse(external.job_public(dict(job))["short_action_sent"])
        # Terminal evidence remains useful after the active component is replaced.
        events.clear()
        item.update(status="cancelled", motion_receipts=[{"motion_events": [event]}])
        result = external.job_public(job)
        self.assertTrue(result["short_action_sent"])
        self.assertFalse(result["playback_confirmed"])

    def test_only_completed_final_speech_tail_is_exposed(self):
        external = ExternalGame(SimpleNamespace())
        external.generation = "offline-generation"
        receipt = {"status": "sent", "send_complete": True, "handoff_wait_elapsed": True,
                   "post_audio_wait_seconds": 4.0}
        job = {"job_id": "offline", "mode": "speech", "item": {"status": "sent",
               "motion_receipts": [dict(receipt, post_audio_wait_seconds=9), dict(receipt)]}}
        self.assertEqual(external.job_public(job)["post_audio_wait_seconds"], 4.0)
        for value in (None, -1, float("nan"), float("inf"), True, "4"):
            with self.subTest(value=value):
                job["item"]["motion_receipts"][-1]["post_audio_wait_seconds"] = value
                self.assertIsNone(external.job_public(job)["post_audio_wait_seconds"])
        job["item"]["motion_receipts"][-1] = {k: v for k, v in receipt.items()
                                             if k != "post_audio_wait_seconds"}
        self.assertIsNone(external.job_public(job)["post_audio_wait_seconds"],
                          "do not borrow the earlier segment's nine seconds")
        job["item"]["motion_receipts"][-1] = dict(receipt)
        for change in ({"status": "sending"}, {"external_worker_pending": True},
                       {"external_preparation_pending": True}):
            candidate = copy.deepcopy(job)
            candidate["item"].update(change)
            self.assertIsNone(external.job_public(candidate)["post_audio_wait_seconds"])
        for change in ({"status": "cancelled"}, {"send_complete": False}, {"handoff_wait_elapsed": False}):
            candidate = copy.deepcopy(job)
            candidate["item"]["motion_receipts"][-1].update(change)
            self.assertIsNone(external.job_public(candidate)["post_audio_wait_seconds"])
        job["mode"] = "motion"
        self.assertIsNone(external.job_public(job)["post_audio_wait_seconds"])


class ExternalGameContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        marker = self.root / "offline-config-marker"
        marker.touch()
        self.tts = AsyncMock(side_effect=self.synthesize)
        self.patches = [
            patch.object(live_session, "PersistentComponent", SpeechComponent),
            patch.object(live_session.LiveSession, "assert_previous_cloud_ended", return_value=None),
            patch.object(live_session.LiveSession, "verify_previous_cloud_ended", new=AsyncMock()),
            patch.object(live_session, "load_bailian", return_value=("offline-key", "offline-workspace")),
            patch.object(live_session, "load_vidu", side_effect=AssertionError("unexpected Vidu credentials")),
            patch.object(live_session, "synthesize_segment", self.tts),
            patch.object(fixtures.studio_server, "load_bailian", return_value=("offline-key", "offline-workspace")),
            patch.object(fixtures.studio_server, "load_vidu", return_value="offline-key"),
            patch.object(fixtures.studio_server, "LOCAL_SETTINGS", marker),
            patch.object(fixtures.studio_server.Automation, "start", return_value=None),
        ]
        for entry in self.patches:
            entry.start()
        self.ai = fixtures.FakeAI()
        self.studio = fixtures.studio_server.Studio(self.root, ai=self.ai, preview=True)
        self.studio.automation.running = Mock(return_value=True)
        self.client = TestClient(TestServer(self.studio.build(), host="127.0.0.1"))
        await self.client.start_server()
        self.studio.origin = str(self.client.make_url("/")).rstrip("/")
        self.studio.current.timed_action_planner = AsyncMock(side_effect=AssertionError("unexpected action model"))
        self.identity = {"owner_id": "romance:offline", "generation": self.studio.generation}

    async def asyncTearDown(self):
        await self.client.close()
        for entry in reversed(self.patches):
            entry.stop()
        self.temporary.cleanup()

    async def synthesize(self, http, key, workspace, out, text, instruction, **kwargs):
        out.mkdir(parents=True, exist_ok=True)
        pcm = b"\0\0" * 2400
        path = out / "audio.pcm"
        path.write_bytes(pcm)
        return {"status": "ok", "usage_verified": False, "audio_seconds": .1,
                "pcm_path": str(path), "pcm_sha256": hashlib.sha256(pcm).hexdigest()}

    async def post(self, path, body=None, *, player=False):
        prefix = self.studio.player_prefix if player else self.studio.prefix
        return await self.client.post(prefix + "/" + path, json=body or {},
            headers={"Origin": self.studio.origin})

    async def claim(self):
        response = await self.post("external/claim", {"owner_id": self.identity["owner_id"]})
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual(await response.json(), self.identity)

    def live(self):
        self.studio.current.started = True
        self.studio.current.component.state = "ready"

    async def submit(self, job_id="round:1", text="我先想一想，军师们这招有点意思。", **changed):
        return await self.post("external/jobs", {**self.identity, "job_id": job_id, "text": text, **changed})

    async def get_job(self, job_id="round:1", **changed):
        return await self.client.get(self.studio.prefix + "/external/jobs/" + job_id,
                                     params={**self.identity, **changed})

    def thinking_plan(self):
        return {"emotion": "专注思考", "actions": [{"kind": "S", "delay_seconds": 0,
            "hold_seconds": 0, "motions": [{"part": "head", "description":
                "轻轻低头看向画面下方，像在认真措辞，不拿出或生成任何物品"}],
            "gaze": "看向画面下方", "expression": "专注思考"}],
            "recovery": {"gaze": "回到镜头", "expression": "自然专注"}}

    async def submit_motion(self, job_id="thinking:1", **changed):
        return await self.post("external/jobs", {**self.identity, "job_id": job_id,
            "mode": "motion", "plan": self.thinking_plan(), **changed})

    def renderer(self, session, *, fail_motion=None):
        renderer = MotionComponent(session.http, session.out, session.image, session.diagnostics)
        renderer._ws = FakeSocket(fail_motion=fail_motion)
        renderer.live_id = "offline-only"
        renderer.state = "ready"
        renderer._ready.set()
        renderer._joined.set()
        return renderer

    async def test_motion_real_sender_writes_ge_s_q_without_tts_or_type10(self):
        await self.claim()
        self.live()
        s = self.studio.current
        original = s.component
        renderer = self.renderer(s)
        clock = ManualClock()
        renderer._wait = lambda seconds: clock.wait(renderer, seconds)
        fake_time = SimpleNamespace(monotonic=lambda: clock.now, time=time.time)
        s.component = renderer
        try:
            with patch("motion_component.time", fake_time), patch("component_live.time", fake_time):
                self.assertEqual((await self.submit_motion(expires_at=time.time() + 3)).status, 200)
                await clock.settle()
                self.assertEqual([e["role"] for e in renderer.motion_events], ["GE"])
                self.assertFalse((await (await self.get_job("thinking:1")).json())["short_action_sent"])
                lead = renderer._ws.sent[0]["frame"]["payload"]["text_msg"]["content"]
                self.assertEqual(lead.splitlines(), ["眼神：看向画面下方。", "表情：专注思考。"])
                cancelled = await (await self.post("external/jobs/thinking:1/cancel", self.identity)).json()
                self.assertEqual(cancelled["cancel_result"], "already_started")
                self.assertTrue(cancelled["worker_pending"])
                self.assertEqual((await self.submit(job_id="formal")).status, 409)
                await clock.advance(1.1)
                self.assertEqual([e["role"] for e in renderer.motion_events], ["GE", "S"])
                progress = await (await self.get_job("thinking:1")).json()
                self.assertEqual(progress["status"], "sending")
                self.assertTrue(progress["short_action_sent"])
                self.assertFalse(progress["playback_confirmed"])
                await clock.advance(1.5)
                await asyncio.wait_for(s.work.join(), 1)
                self.assertEqual([e["role"] for e in renderer.motion_events], ["GE", "S", "Q"])
                self.assertEqual(renderer.motion_events[1]["actual_interval_seconds"], 1.1)
                self.assertEqual(renderer.motion_events[-1]["actual_interval_seconds"], 1.5)
                self.assertEqual([r["frame"]["type"] for r in renderer._ws.sent], [9, 9, 9])
                self.assertEqual(renderer._ws.sent[-1]["frame"]["payload"]["text_msg"]["content"],
                                 "眼神：回到镜头。\n表情：自然专注。")
                self.assertTrue(renderer._ws.sent[1]["frame"]["payload"]["text_msg"]["content"].startswith("动作："))
                self.assertEqual(renderer._ws.binary, [])
                job = await (await self.get_job("thinking:1")).json()
                self.assertEqual(job["mode"], "motion")
                self.assertEqual(job["status"], "sent")
                self.assertTrue(job["short_action_sent"])
                self.assertTrue(job["send_complete"])
                self.assertFalse(job["handoff_wait_elapsed"])
                self.assertFalse(job["worker_pending"])
                self.assertFalse(job["playback_confirmed"])
                self.assertEqual(job["segments_sent"], 0)
                self.assertEqual(renderer.utterances_started, 0)
                self.tts.assert_not_awaited()
                self.ai.generate_reply.assert_not_awaited()
                s.timed_action_planner.assert_not_awaited()
        finally:
            s.component = original

    async def test_motion_s_attempt_and_failed_write_do_not_report_sent(self):
        await self.claim()
        self.live()
        s = self.studio.current
        original = s.component
        renderer = self.renderer(s, fail_motion=2)
        clock = ManualClock()
        renderer._wait = lambda seconds: clock.wait(renderer, seconds)
        entered, release = asyncio.Event(), asyncio.Event()
        original_send = renderer._ws.send_json

        async def blocked_s(frame):
            if renderer._ws.motion_attempts == 1:
                entered.set()
                await release.wait()
            await original_send(frame)

        renderer._ws.send_json = blocked_s
        fake_time = SimpleNamespace(monotonic=lambda: clock.now, time=time.time)
        s.component = renderer
        try:
            with patch("motion_component.time", fake_time), patch("component_live.time", fake_time):
                self.assertEqual((await self.submit_motion()).status, 200)
                await clock.advance(1.1)
                await asyncio.wait_for(entered.wait(), 1)
                progress = await (await self.get_job("thinking:1")).json()
                self.assertTrue(progress["delivery_started"])
                self.assertFalse(progress["short_action_sent"])
                self.assertEqual([e["role"] for e in renderer.motion_events], ["GE"])
                release.set()
                await asyncio.wait_for(s.work.join(), 1)
                result = await (await self.get_job("thinking:1")).json()
                self.assertEqual(result["status"], "error")
                self.assertFalse(result["short_action_sent"])
                self.assertFalse(result["playback_confirmed"])
        finally:
            release.set()
            s.component = original

    async def test_motion_duplicate_plan_mode_and_deadlines_are_frozen(self):
        await self.claim()
        self.live()
        sender = self.studio.current.component.send_motion_expression = AsyncMock(return_value={
            "status": "sent", "send_complete": True, "actions_sent": 3})
        deadline = time.time() + 30
        self.assertEqual((await self.submit_motion(expires_at=deadline)).status, 200)
        await asyncio.wait_for(self.studio.current.work.join(), 1)
        self.assertEqual((await self.submit_motion(expires_at=deadline)).status, 200)
        changed = self.thinking_plan()
        changed["recovery"]["expression"] = "微笑"
        self.assertEqual((await self.submit_motion(plan=changed, expires_at=deadline)).status, 409)
        self.assertEqual((await self.submit_motion(expires_at=deadline + 1)).status, 409)
        self.assertEqual((await self.submit(job_id="thinking:1")).status, 409)
        archive = json.loads((self.studio.current.out / "external-game.json").read_text())
        self.assertEqual(archive["jobs"][0]["mode"], "motion")
        self.assertEqual(archive["jobs"][0]["text"], "")
        self.assertEqual(archive["jobs"][0]["plan"]["actions"], self.thinking_plan()["actions"])
        self.assertEqual([n["role"] for n in archive["jobs"][0]["plan"]["timeline"]], ["GE", "S", "Q"])
        self.assertEqual(sender.await_args.kwargs["dialogue_motion"], True)
        sender.assert_awaited_once()
        self.tts.assert_not_awaited()

    async def test_motion_cancelled_before_worker_and_deadlines_never_send(self):
        await self.claim()
        self.live()
        s = self.studio.current
        sender = s.component.send_motion_expression = AsyncMock()
        s.worker.cancel()
        await asyncio.gather(s.worker, return_exceptions=True)
        self.assertEqual((await self.submit_motion()).status, 200)
        job = await (await self.post("external/jobs/thinking:1/cancel", self.identity)).json()
        self.assertEqual(job["cancel_result"], "cancelled_before_delivery")
        self.assertTrue(job["worker_pending"])
        self.assertEqual((await self.submit(job_id="formal")).status, 409)
        s.worker = asyncio.create_task(s.process_answers())
        await asyncio.wait_for(s.work.join(), 1)
        self.assertFalse((await (await self.get_job("thinking:1")).json())["worker_pending"])
        for job_id, deadlines in (("expired", {"expires_at": time.time() - 1}),
                                  ("cannot-fit", {"finish_before": time.time() + 2.5})):
            self.assertEqual((await self.submit_motion(job_id=job_id, **deadlines)).status, 200)
            await asyncio.wait_for(s.work.join(), 1)
            job = await (await self.get_job(job_id)).json()
            self.assertEqual(job["status"], "expired")
            self.assertFalse(job["delivery_started"])
        sender.assert_not_awaited()
        self.tts.assert_not_awaited()

    async def test_motion_skips_previous_q_cooldown_without_claiming_delivery(self):
        await self.claim()
        self.live()
        s = self.studio.current
        s.component._motion_not_before = time.monotonic() + 10
        sender = s.component.send_motion_expression = AsyncMock()
        self.assertEqual((await self.submit_motion()).status, 200)
        await asyncio.wait_for(s.work.join(), 1)
        job = await (await self.get_job("thinking:1")).json()
        self.assertEqual(job["status"], "expired")
        self.assertFalse(job["delivery_started"])
        self.assertFalse(job["worker_pending"])
        self.assertEqual(self.studio.automation.last_error, "")
        sender.assert_not_awaited()

    async def test_motion_contract_rejects_speech_and_long_or_delayed_plans(self):
        await self.claim()
        self.live()
        for changed in ({"text": "不能夹带口播"}, {"instruction": "保持安静"}, {"mode": "bad"},
                        {"mode": []}, {"plan": self.performance_plan()}, {"plan": None}):
            self.assertEqual((await self.submit_motion(**changed)).status, 400)
        for updates in ({"kind": "L", "hold_seconds": 2}, {"delay_seconds": 1}):
            plan = self.thinking_plan()
            plan["actions"][0].update(updates)
            self.assertEqual((await self.submit_motion(plan=plan)).status, 400)
        plan = self.thinking_plan()
        plan["actions"] *= 2
        self.assertEqual((await self.submit_motion(plan=plan)).status, 400)
        plan = self.thinking_plan()
        plan["actions"][0]["motions"][0]["part"] = "whole_body"
        self.assertEqual((await self.submit_motion(plan=plan)).status, 400)
        self.assertEqual(self.studio.external_game.jobs, {})
        self.tts.assert_not_awaited()

    async def test_motion_failure_after_s_preserves_partial_receipt_and_stop_latch(self):
        await self.claim()
        self.live()
        s = self.studio.current
        original = s.component
        renderer = self.renderer(s, fail_motion=3)
        clock = ManualClock()
        renderer._wait = lambda seconds: clock.wait(renderer, seconds)
        fake_time = SimpleNamespace(monotonic=lambda: clock.now, time=time.time)
        s.component = renderer
        try:
            with patch("motion_component.time", fake_time), patch("component_live.time", fake_time):
                self.assertEqual((await self.submit_motion()).status, 200)
                await clock.advance(3)
                await asyncio.wait_for(s.work.join(), 1)
                job = await (await self.get_job("thinking:1")).json()
                self.assertEqual(job["status"], "error")
                self.assertTrue(job["short_action_sent"])
                self.assertTrue(job["delivery_started"])
                self.assertFalse(job["send_complete"])
                self.assertFalse(job["worker_pending"])
                receipt = self.studio.external_game.jobs["thinking:1"]["item"]["motion_receipts"][0]
                self.assertEqual(receipt["actions_sent"], 2)
                self.assertEqual([e["role"] for e in receipt["motion_events"]], ["GE", "S"])
                self.assertEqual(self.studio.automation.last_error, "delivery_failed")
                self.assertEqual((await self.submit(job_id="formal")).status, 409)
                self.assertEqual(renderer._ws.binary, [])
                self.tts.assert_not_awaited()
        finally:
            s.component = original

    async def test_idle_claim_is_idempotent_and_can_start_existing_session(self):
        await self.claim()
        await self.claim()
        self.assertEqual((await self.post("external/claim", {"owner_id": "other"})).status, 409)
        player = await self.post("claim", {"client_id": "offline-player-one", "receiver_mode": "preview",
                                          "game_owner": self.identity["owner_id"]}, player=True)
        self.assertTrue((await player.json())["granted"])
        started = await self.post("start")
        self.assertEqual(started.status, 200, await started.text())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.studio.current.component.start.assert_awaited_once()
        self.assertEqual(self.studio.current.component.state, "waiting_stage")
        await self.claim()  # Same owner remains idempotent after start.

    async def test_game_claim_replaces_only_idle_player_and_blocks_old_page_reclaim(self):
        old = {"client_id": "offline-old-preview", "receiver_mode": "preview"}
        self.assertTrue((await (await self.post("claim", old, player=True)).json())["granted"])
        self.assertTrue(self.studio.player_public()["connected"])
        await self.claim()
        self.assertIsNone(self.studio.player)
        self.assertFalse(self.studio.snapshot()["studio"]["can_start"])
        self.assertEqual((await self.post("start")).status, 409)
        self.studio.current.component.start.assert_not_awaited()
        for body in (old, {**old, "game_owner": "wrong"},
                     {**old, "takeover": True, "generation": self.studio.generation}):
            self.assertFalse((await (await self.post("claim", body, player=True)).json())["granted"])
            self.assertIsNone(self.studio.player)
        stale = await self.post("heartbeat", {"client_id": old["client_id"], "generation": self.studio.generation}, player=True)
        self.assertEqual(stale.status, 409)
        new = {"client_id": "offline-game-frame", "receiver_mode": "preview", "game_owner": self.identity["owner_id"]}
        self.assertTrue((await (await self.post("claim", new, player=True)).json())["granted"])
        self.assertTrue(self.studio.snapshot()["studio"]["can_start"])
        await self.claim()  # An idempotent owner request must not drop its new iframe.
        self.assertEqual(self.studio.player["client_id"], new["client_id"])

    async def test_obs_claim_requires_explicit_matching_mode_and_never_starts_cloud(self):
        self.studio.receiver_mode = "obs"
        for body in ({"owner_id": "offline-obs"},
                     {"owner_id": "offline-obs", "receiver_mode": "preview"}):
            response = await self.post("external/claim", body)
            self.assertEqual(response.status, 409)
            self.assertIsNone(self.studio.external_game.owner_id)
        body = {"owner_id": "offline-obs", "receiver_mode": "obs"}
        response = await self.post("external/claim", body)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["owner_id"], "offline-obs")
        self.assertEqual((await self.post("external/claim", body)).status, 200)
        self.assertFalse(self.studio.current.started)
        self.studio.current.component.start.assert_not_awaited()
        self.tts.assert_not_awaited()

    async def test_preview_studio_rejects_explicit_obs_claim(self):
        response = await self.post("external/claim", {"owner_id": "offline-obs", "receiver_mode": "obs"})
        self.assertEqual(response.status, 409)
        self.assertIsNone(self.studio.external_game.owner_id)
        self.studio.current.component.start.assert_not_awaited()
        self.tts.assert_not_awaited()

    async def test_claim_refuses_nonpreview_started_busy_source_topic_and_idle_motion(self):
        cases = [(self.studio, "receiver_mode", "obs"), (self.studio.current, "started", True),
                 (self.studio.automation, "enabled", True),
                 (self.studio.director, "topic", {"state": "paused"})]
        for obj, name, value in cases:
            with self.subTest(name=name), patch.object(obj, name, value):
                self.assertEqual((await self.post("external/claim", {"owner_id": "game"})).status, 409)
        item = self.studio.enqueue_comment("旧弹幕", "观众")
        self.assertEqual((await self.post("external/claim", {"owner_id": "game"})).status, 409)
        item["status"] = "cancelled"
        self.studio.current.component._idle_motion_state["needs_recovery"] = True
        self.assertEqual((await self.post("external/claim", {"owner_id": "game"})).status, 409)

    async def test_frozen_text_once_through_tts_worker_and_receipt(self):
        await self.claim()
        self.live()
        text = "你们这句“先吃饭”，我还真有点心动。"
        submitted = await self.submit(text=text)
        self.assertEqual(submitted.status, 200, await submitted.text())
        await asyncio.wait_for(self.studio.current.work.join(), 1)
        result = await (await self.get_job()).json()
        self.assertEqual(result["status"], "sent")
        self.assertTrue(result["send_complete"])
        self.assertTrue(result["handoff_wait_elapsed"])
        self.assertIsNone(result["post_audio_wait_seconds"], "legacy receipts do not invent a completed duration")
        self.assertFalse(result["playback_confirmed"])
        self.assertEqual(result["segments_sent"], 1)
        self.assertEqual(self.tts.await_args.args[4], text)
        self.assertEqual(self.tts.await_args.args[5], LEGACY_INSTRUCTION)
        sender = self.studio.current.component.send_aligned_expression
        self.assertEqual(sender.await_args.args[1], text)
        self.assertEqual(sender.await_args.args[2], [])
        self.assertIsNone(sender.await_args.kwargs["reading"])
        self.ai.select_comments.assert_not_awaited()
        self.ai.generate_reply.assert_not_awaited()
        self.studio.current.timed_action_planner.assert_not_awaited()
        archive = json.loads((self.studio.current.out / "external-game.json").read_text())
        self.assertEqual(archive["jobs"][0]["text"], text)
        self.assertEqual(archive["jobs"][0]["status"], "sent")
        # Repeated submits after completion return the same job without work.
        for _ in range(2):
            response = await self.submit(text=text)
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["status"], "sent")
        self.tts.assert_awaited_once()
        sender.assert_awaited_once()
        self.assertEqual((await self.submit(text="换了台词")).status, 409)

    async def test_ssml_survives_tts_sender_archive_and_duplicate_identity(self):
        await self.claim()
        self.live()
        plan = self.performance_plan()
        part = plan["segments"][0]
        part.update(actions=[], recovery=None, ssml='<speak>别<break time="450ms"/>急。</speak>')
        response = await self.submit(text=part["text"], plan=plan)
        self.assertEqual(response.status, 200, await response.text())
        await asyncio.wait_for(self.studio.current.work.join(), 1)
        self.assertEqual((await (await self.get_job()).json())["status"], "sent")
        self.assertEqual(self.tts.await_args.args[4], part["text"])
        self.assertEqual(self.tts.await_args.kwargs["ssml"], part["ssml"])
        sender = self.studio.current.component.send_aligned_expression
        self.assertEqual(sender.await_args.args[1], part["text"])
        self.assertEqual(sender.await_args.kwargs["transcription_text"], part["text"])
        archived = json.loads((self.studio.current.out / "external-game.json").read_text())
        self.assertEqual(archived["jobs"][0]["plan"]["segments"][0]["ssml"], part["ssml"])
        self.assertEqual((await self.submit(text=part["text"], plan=plan)).status, 200)
        changed = copy.deepcopy(plan)
        changed["segments"][0]["ssml"] = '<speak>别<break time="900ms"/>急。</speak>'
        self.assertEqual((await self.submit(text=part["text"], plan=changed)).status, 409)
        self.tts.assert_awaited_once()

    async def test_ssml_mismatched_text_is_rejected_before_tts(self):
        await self.claim()
        self.live()
        plan = self.performance_plan()
        plan["segments"][0]["ssml"] = '<speak>台词被改掉了。</speak>'
        self.assertEqual((await self.submit(text="别急。", plan=plan)).status, 400)
        self.tts.assert_not_awaited()

    async def test_emotion_is_frozen_through_worker_archive_and_duplicate_check(self):
        await self.claim()
        self.live()
        text = "哎，你们这招，我有点不敢啊。"
        instruction = "像熟人聊天，有点心虚，带轻微自嘲，句尾收住。"
        self.assertEqual((await self.submit(text=text, instruction=instruction)).status, 200)
        await asyncio.wait_for(self.studio.current.work.join(), 1)
        self.assertEqual(self.tts.await_args.args[4:6], (text, instruction))
        external = self.studio.external_game
        item = external.jobs["round:1"]["item"]
        external.validate_plan(item, external.frozen_plan(text, instruction))
        with self.assertRaisesRegex(ValueError, "external_frozen_plan_changed"):
            external.validate_plan(item, external.frozen_plan(text, "兴奋大喊。"))
        archive = json.loads((self.studio.current.out / "external-game.json").read_text())
        self.assertEqual(archive["jobs"][0]["instruction"], instruction)
        self.assertEqual((await self.submit(text=text, instruction=instruction)).status, 200)
        self.assertEqual((await self.submit(text=text, instruction="换成高兴的语气。")).status, 409)
        self.assertEqual((await self.submit(text=text)).status, 409)
        self.tts.assert_awaited_once()
        self.studio.current.component.send_aligned_expression.assert_awaited_once()

    def performance_plan(self):
        return {"emotion": "着急又嘴硬", "reading": None, "segments": [{
            "text": "别急。", "instruction": "忍着着急，嘴角带点自嘲。",
            "actions": [{"anchor": {"quote": "别", "occurrence": 1},
                         "motions": [{"part": "head", "description": "明显前倾看向镜头"}],
                         "gaze": "直视镜头", "expression": "眉毛明显抬高，嘴角忍笑"}],
            "recovery": {"gaze": "继续看向镜头", "expression": "保持着急又好笑的神情"}}]}

    def action_tts(self):
        async def synthesize(*args, **kwargs):
            result = await self.synthesize(*args, **kwargs)
            pcm = b"\0\0" * 48000
            Path(result["pcm_path"]).write_bytes(pcm)
            result.update(audio_seconds=2, pcm_sha256=hashlib.sha256(pcm).hexdigest(), alignment={
                "status": "ok", "timebase": "audio_start", "pcm_sha256": hashlib.sha256(pcm).hexdigest(),
                "words": [{"text": "别", "begin_time": 0, "end_time": 300},
                          {"text": "急。", "begin_time": 300, "end_time": 1800}]})
            return result
        self.tts.side_effect = synthesize
        async def planner(content, *args, **kwargs):
            actions = copy.deepcopy(content["actions"])
            actions[0]["anchor"]["quote"] = "急"
            return {"actions": actions, "planning": {"phase": "tts_aligned"}}
        self.studio.current.timed_action_planner.side_effect = planner

    async def test_complete_plan_reaches_worker_and_freeze_survives_anchor_alignment(self):
        await self.claim()
        self.live()
        self.action_tts()
        plan = self.performance_plan()
        self.assertEqual((await self.submit(text="别急。", plan=plan)).status, 200)
        await asyncio.wait_for(self.studio.current.work.join(), 1)
        job = await (await self.get_job()).json()
        self.assertEqual(job["status"], "sent")
        self.assertTrue(job["delivery_started"])
        self.assertFalse(job["worker_pending"])
        self.assertEqual(self.tts.await_args.args[5], plan["segments"][0]["instruction"])
        sender = self.studio.current.component.send_aligned_expression
        sent_action = sender.await_args.args[2][0]
        self.assertEqual(sent_action["expression"], "眉毛明显抬高，嘴角忍笑")
        self.assertEqual(sent_action["anchor"]["quote"], "急")
        self.assertEqual(sender.await_args.kwargs["recovery"], plan["segments"][0]["recovery"])
        archive = json.loads((self.studio.current.out / "external-game.json").read_text())
        self.assertEqual(archive["jobs"][0]["plan"], plan)
        self.assertEqual((await self.submit(text="别急。", plan=plan)).status, 200)
        for changed in ("actions", "recovery"):
            altered = copy.deepcopy(plan)
            if changed == "actions":
                altered["segments"][0][changed][0]["expression"] = "明显皱眉"
            else:
                altered["segments"][0][changed]["expression"] = "轻轻微笑"
            self.assertEqual((await self.submit(text="别急。", plan=altered)).status, 409)
        self.tts.assert_awaited_once()

    async def test_plan_rejects_inconsistent_copy_invalid_shape_and_tagged_body(self):
        await self.claim()
        self.live()
        plan = self.performance_plan()
        self.assertEqual((await self.submit(text="另一句话", plan=plan)).status, 400)
        self.assertEqual((await self.submit(text="别急。", plan=plan, instruction="正常说话")).status, 400)
        for altered in (dict(plan, reading={}), dict(plan, segments=plan["segments"] * 2),
                        dict(plan, segments=[{**plan["segments"][0], "actions": plan["segments"][0]["actions"] * 2}])):
            self.assertEqual((await self.submit(text="别急。", plan=altered)).status, 400)
        tagged = copy.deepcopy(plan)
        tagged["segments"][0]["text"] = "[着急]别急。"
        self.assertEqual((await self.submit(text=tagged["segments"][0]["text"], plan=tagged)).status, 400)
        self.tts.assert_not_awaited()

    async def test_expired_after_slow_tts_and_finish_budget_never_start_sender(self):
        await self.claim()
        self.live()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return await self.synthesize(*args, **kwargs)
        self.tts.side_effect = slow
        self.assertEqual((await self.submit(expires_at=time.time() + .05)).status, 200)
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(.06)
        release.set()
        await asyncio.wait_for(self.studio.current.work.join(), 1)
        result = await (await self.get_job()).json()
        self.assertEqual(result["status"], "expired")
        self.assertFalse(result["delivery_started"])
        # Actual audio is short, but its drain/handoff still cannot fit here.
        self.assertEqual((await self.submit(job_id="short-window", finish_before=time.time() + 1)).status, 200)
        await asyncio.wait_for(self.studio.current.work.join(), 1)
        self.assertEqual((await (await self.get_job("short-window")).json())["status"], "expired")
        self.studio.current.component.send_aligned_expression.assert_not_awaited()
        self.assertEqual(self.studio.automation.last_error, "")

    async def test_cancel_during_tts_holds_worker_until_old_request_exits(self):
        await self.claim()
        self.live()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(*args, **kwargs):
            entered.set()
            await release.wait()
            return await self.synthesize(*args, **kwargs)
        self.tts.side_effect = slow
        self.assertEqual((await self.submit()).status, 200)
        await asyncio.wait_for(entered.wait(), 1)
        cancelled = await self.post("external/jobs/round:1/cancel", self.identity)
        result = await cancelled.json()
        self.assertEqual(result["cancel_result"], "cancelled_before_delivery")
        self.assertTrue(result["worker_pending"])
        self.assertFalse(result["delivery_started"])
        self.assertEqual((await self.submit(job_id="formal")).status, 409)
        release.set()
        await asyncio.wait_for(self.studio.current.work.join(), 1)
        self.assertFalse((await (await self.get_job()).json())["worker_pending"])
        self.studio.current.component.send_aligned_expression.assert_not_awaited()
        self.assertEqual((await self.submit(job_id="formal")).status, 200)
        await asyncio.wait_for(self.studio.current.work.join(), 1)

    async def test_cancel_before_worker_and_after_delivery_are_explicit(self):
        await self.claim()
        self.live()
        external = self.studio.external_game
        # Prevent dequeue until cancellation has reached the existing queue entry.
        worker = self.studio.current.worker
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual((await self.submit()).status, 200)
        result = await (await self.post("external/jobs/round:1/cancel", self.identity)).json()
        self.assertEqual(result["cancel_result"], "cancelled_before_delivery")
        self.assertTrue(external.jobs["round:1"]["item"]["cancel_requested"])
        self.studio.current.worker = asyncio.create_task(self.studio.current.process_answers())
        await asyncio.wait_for(self.studio.current.work.join(), 1)
        self.tts.assert_not_awaited()
        entered, release = asyncio.Event(), asyncio.Event()
        async def sending(*args, **kwargs):
            entered.set()
            await release.wait()
            return await self.studio.current.component._send(*args, **kwargs)
        sender = self.studio.current.component.send_aligned_expression
        sender.side_effect = sending
        self.assertEqual((await self.submit(job_id="started")).status, 200)
        await asyncio.wait_for(entered.wait(), 1)
        result = await (await self.post("external/jobs/started/cancel", self.identity)).json()
        self.assertEqual(result["cancel_result"], "already_started")
        self.assertTrue(result["delivery_started"])
        self.assertFalse(external.jobs["started"]["item"].get("cancel_requested", False))
        release.set()
        await asyncio.wait_for(self.studio.current.work.join(), 1)
        self.assertEqual((await (await self.get_job("started")).json())["status"], "sent")

    async def test_partial_delivery_failure_preserves_global_stop_protection(self):
        await self.claim()
        self.live()
        self.studio.current.component.send_aligned_expression.side_effect = ConnectionError("partial send")
        self.assertEqual((await self.submit()).status, 200)
        await asyncio.wait_for(self.studio.current.work.join(), 1)
        result = await (await self.get_job()).json()
        self.assertTrue(result["delivery_started"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.studio.automation.last_error, "delivery_failed")
        self.assertEqual((await self.submit(job_id="unsafe-retry")).status, 409)

    async def test_real_ge_attempt_blocks_cancel_and_retains_expression_recovery(self):
        await self.claim()
        self.live()
        self.action_tts()
        s = self.studio.current
        original = s.component
        renderer = MotionComponent(s.http, s.out, s.image, s.diagnostics)
        renderer._ws = FakeSocket()
        renderer.live_id = "offline-only"
        renderer.state = "ready"
        renderer._ready.set()
        renderer._joined.set()
        clock = ManualClock()
        renderer._wait = lambda seconds: clock.wait(renderer, seconds)
        fake_time = SimpleNamespace(monotonic=lambda: clock.now, time=time.time)
        s.component = renderer
        try:
            with patch("motion_component.time", fake_time), patch("component_live.time", fake_time):
                self.assertEqual((await self.submit(text="别急。", plan=self.performance_plan())).status, 200)
                await clock.settle()
                # GE is written before any answer PCM and is already a delivery attempt.
                self.assertEqual([e["role"] for e in renderer.motion_events], ["GE"])
                self.assertEqual(renderer._ws.binary, [])
                cancelled = await (await self.post("external/jobs/round:1/cancel", self.identity)).json()
                self.assertTrue(cancelled["delivery_started"])
                self.assertEqual(cancelled["cancel_result"], "already_started")
                await clock.advance(8)
                await asyncio.wait_for(s.work.join(), 1)
                self.assertEqual([e["role"] for e in renderer.motion_events], ["GE", "S", "Q"])
                self.assertEqual(renderer.motion_events[-1]["expression"], "保持着急又好笑的神情")
                frames = [row["frame"] for row in renderer._ws.sent if row["frame"]["type"] == 9]
                self.assertEqual(len(frames), 3)
                self.assertTrue(frames[1]["payload"]["text_msg"]["content"].startswith("动作："))
                self.assertEqual(frames[2]["payload"]["text_msg"]["content"],
                                 "眼神：继续看向镜头。\n表情：保持着急又好笑的神情。")
                self.assertEqual((await (await self.get_job()).json())["status"], "sent")
        finally:
            s.component = original

    async def test_invalid_deadlines_and_changed_deadline_are_rejected(self):
        await self.claim()
        self.live()
        for name in ("expires_at", "finish_before"):
            for value in (True, None, "soon", 0, -1, float("inf"), float("nan")):
                self.assertEqual((await self.submit(**{name: value})).status, 400)
        deadline = time.time() + 30
        self.assertEqual((await self.submit(finish_before=deadline)).status, 200)
        await asyncio.wait_for(self.studio.current.work.join(), 1)
        self.assertEqual((await self.submit(finish_before=deadline)).status, 200)
        self.assertEqual((await self.submit(finish_before=deadline + 1)).status, 409)
        self.assertEqual((await self.post("external/jobs/round:1/cancel", {**self.identity, "generation": "old"})).status, 409)

    async def test_real_sender_failure_after_ge_keeps_partial_receipt_and_error_latch(self):
        await self.claim()
        self.live()
        self.action_tts()
        s = self.studio.current
        original = s.component
        renderer = MotionComponent(s.http, s.out, s.image, s.diagnostics)
        renderer._ws = FakeSocket(fail_motion=2)
        renderer.live_id = "offline-only"
        renderer.state = "ready"
        renderer._ready.set()
        renderer._joined.set()
        clock = ManualClock()
        renderer._wait = lambda seconds: clock.wait(renderer, seconds)
        fake_time = SimpleNamespace(monotonic=lambda: clock.now, time=time.time)
        s.component = renderer
        try:
            with patch("motion_component.time", fake_time), patch("component_live.time", fake_time):
                self.assertEqual((await self.submit(text="别急。", plan=self.performance_plan())).status, 200)
                await clock.advance(8)
                await asyncio.wait_for(s.work.join(), 1)
                self.assertEqual([e["role"] for e in renderer.motion_events], ["GE"])
                job = await (await self.get_job()).json()
                self.assertEqual(job["status"], "error")
                self.assertTrue(job["delivery_started"])
                self.assertFalse(job["send_complete"])
                item = self.studio.external_game.jobs["round:1"]["item"]
                self.assertTrue(item["motion_receipts"])
                self.assertEqual(self.studio.automation.last_error, "delivery_failed")
        finally:
            s.component = original

    async def test_explicit_invalid_instruction_never_uses_default_or_calls_tts(self):
        await self.claim()
        self.live()
        for instruction in (None, 1, [], {}, "", " ", " 语气", "语气 ", "字" * 161):
            with self.subTest(instruction=instruction):
                self.assertEqual((await self.submit(instruction=instruction)).status, 400)
        self.assertEqual(self.studio.external_game.jobs, {})
        self.tts.assert_not_awaited()

    async def test_concurrent_duplicates_share_one_job_and_busy_rejects_new_job(self):
        await self.claim()
        self.live()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_tts(*args, **kwargs):
            entered.set()
            await release.wait()
            return await self.synthesize(*args, **kwargs)
        self.tts.side_effect = slow_tts
        responses = await asyncio.gather(self.submit(), self.submit())
        self.assertEqual([r.status for r in responses], [200, 200])
        await asyncio.wait_for(entered.wait(), 1)
        self.assertEqual((await self.submit(job_id="round:2")).status, 409)
        archive = json.loads((self.studio.current.out / "external-game.json").read_text())
        self.assertEqual(archive["jobs"][0]["status"], "synthesizing")
        release.set()
        await asyncio.wait_for(self.studio.current.work.join(), 1)
        self.tts.assert_awaited_once()

    async def test_real_sender_writes_exact_type10_and_pcm_without_actions(self):
        await self.claim()
        self.live()
        s = self.studio.current
        original = s.component
        renderer = MotionComponent(s.http, s.out, s.image, s.diagnostics)
        renderer._ws = FakeSocket()
        renderer.live_id = "offline-only"
        renderer.state = "ready"
        renderer._ready.set()
        renderer._joined.set()
        renderer._wait = AsyncMock()
        s.component = renderer
        text = "我先听军师们的，发这句试试。"
        try:
            self.assertEqual((await self.submit(text=text)).status, 200)
            await asyncio.wait_for(s.work.join(), 1)
            # No provider socket is used: FakeSocket captures actual transport frames.
            frames = [row["frame"] for row in renderer._ws.sent]
            self.assertEqual([frame["payload"]["text_msg"]["content"] for frame in frames if frame["type"] == 10], [text])
            self.assertFalse(any(frame["type"] in (9, 15) for frame in frames))
            self.assertEqual(b"".join(row["chunk"] for row in renderer._ws.binary), b"\0\0" * 2400)
            job = await (await self.get_job()).json()
            self.assertEqual(job["status"], "sent")
            self.assertFalse(job["playback_confirmed"])
            self.studio.current.timed_action_planner.assert_not_awaited()
        finally:
            s.component = original

    async def test_rejects_stale_identity_invalid_ids_and_changed_text_without_tts(self):
        await self.claim()
        self.live()
        self.assertEqual((await self.submit(generation="old")).status, 409)
        self.assertEqual((await self.submit(owner_id="other")).status, 409)
        for job_id in ("../escape", "含中文", "", "a" * 129):
            self.assertEqual((await self.submit(job_id=job_id)).status, 400)
        for text in ("", " 台词", "台词 ", "字" * 121, "<tag>台词"):
            self.assertEqual((await self.submit(text=text)).status, 400)
        self.assertEqual((await self.get_job()).status, 404)
        self.tts.assert_not_awaited()

    async def test_external_mode_blocks_competing_routes_and_model_loop(self):
        await self.claim()
        self.live()
        for path in ("comments", "director/submit", "intake/start", "interactions/simulate", "idle-motion-test",
                     "avatar-switch", "avatars/select", "settings", "ai/resume", "pause"):
            response = await self.post(path, {})
            self.assertEqual(response.status, 409, path)
        self.assertFalse(self.studio.automation.can_select())
        self.assertFalse(self.studio.automation.interactions_supported())
        await self.studio.automation.reply_once()
        self.ai.generate_reply.assert_not_awaited()
        self.studio.browser_seen = 0
        self.assertEqual((await self.post("heartbeat")).status, 200)
        self.assertEqual(self.studio.browser_seen, 0)
        heartbeat = await self.post("external/heartbeat", self.identity)
        self.assertEqual(heartbeat.status, 200)
        self.assertGreater(self.studio.browser_seen, time.monotonic() - 1)
        prior = self.studio.browser_seen
        self.assertEqual((await self.post("external/heartbeat", {**self.identity, "generation": "old"})).status, 409)
        self.assertEqual(prior, self.studio.browser_seen)
        state = await self.client.get(self.studio.prefix + "/external/state")
        self.assertEqual(set(await state.json()), {"owner_id", "generation", "state", "current_job", "next_job", "playback_confirmed"})

    async def test_tts_failure_is_terminal_and_repeated_job_never_retries(self):
        await self.claim()
        self.live()
        self.tts.return_value = {"status": "provider_error", "usage_verified": False}
        self.tts.side_effect = None
        self.assertEqual((await self.submit()).status, 200)
        await asyncio.wait_for(self.studio.current.work.join(), 1)
        job = await (await self.get_job()).json()
        self.assertEqual(job["status"], "error")
        self.assertFalse(job["send_complete"])
        self.assertFalse(job["playback_confirmed"])
        self.assertEqual((await self.submit()).status, 200)
        self.tts.assert_awaited_once()
        self.studio.current.component.send_aligned_expression.assert_not_awaited()

    async def test_stop_cancels_before_send_and_new_generation_never_restores_jobs(self):
        await self.claim()
        self.live()
        # Register without yielding to the real worker, then synchronously latch stop.
        external = self.studio.external_game
        item = self.studio.enqueue_comment("这句先留着。", "游戏定稿", input_kind="external_game",
            external_job_id="round:1", external_generation=self.studio.generation)
        item["status"] = "processing"
        external.jobs["round:1"] = {"job_id": "round:1", "text": item["text"],
            "text_sha256": hashlib.sha256(item["text"].encode()).hexdigest(), "item": item}
        self.studio.accept_plan(self.studio.current, item, external.frozen_plan(item["text"]))
        old_out = self.studio.current.out
        await external.stop(type("Request", (), {"json": AsyncMock(return_value=self.identity)})())
        await asyncio.wait_for(self.studio.current.work.join(), 1)
        await asyncio.sleep(0)
        self.assertEqual(item["status"], "cancelled")
        self.tts.assert_not_awaited()
        self.assertEqual(json.loads((old_out / "external-game.json").read_text())["jobs"][0]["status"], "cancelled")
        response = await self.post("new")
        self.assertEqual(response.status, 200, await response.text())
        self.assertNotEqual(self.studio.generation, self.identity["generation"])
        self.assertFalse(external.active)
        self.assertEqual(external.jobs, {})
        self.assertEqual((await self.submit()).status, 409)
        self.studio.current.component.start.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
