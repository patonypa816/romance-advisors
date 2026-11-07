"""Real HTTP owner routes and Type16 sender, all transport stays in memory."""
import asyncio
import copy
import hashlib
import json
import time
import unittest
from unittest.mock import AsyncMock, Mock

from aiohttp import web

from external_appearance import ExternalAppearance, KEYS
from test_avatar_library import png
import test_external_game as external_fixtures


class AppearanceContracts(unittest.IsolatedAsyncioTestCase):
    # Reuse only the setup helpers; inherited speech tests remain in their suite.
    asyncSetUp = external_fixtures.ExternalGameContracts.asyncSetUp
    async def asyncTearDown(self):
        if hasattr(self, "original_component"):
            await self.restore_component()
        await external_fixtures.ExternalGameContracts.asyncTearDown(self)
    synthesize = external_fixtures.ExternalGameContracts.synthesize
    post = external_fixtures.ExternalGameContracts.post
    claim = external_fixtures.ExternalGameContracts.claim
    live = external_fixtures.ExternalGameContracts.live
    renderer = external_fixtures.ExternalGameContracts.renderer

    async def configured(self, initial="normal"):
        await self.claim()
        s = self.studio.current
        self.original_component = s.component
        self.renderer_component = self.renderer(s)
        s.component = self.renderer_component
        self.addAsyncCleanup(self.restore_component)
        s.started = True
        self.studio.player_public = lambda: {"connected": True}
        self.studio.player = {"client_id": "offline-receiver", "joined": True, "audio_live": True,
                              "video_live": True, "seen": time.monotonic(), "frame_seen": time.monotonic(), "frames": 1}
        images, assets = {}, {}
        for index, key in enumerate(KEYS):
            ident = str(1000 + index)
            image = self.root / (key + ".png")
            image.write_bytes(png((index * 35, 20, 80)))
            digest = hashlib.sha256(image.read_bytes()).hexdigest()
            images[ident] = image
            assets[key] = {"avatar_id": ident, "image_sha256": digest, "description": key + "外貌"}
        self.config = {"enabled": True, "character_id": "same-person", "voice_id": "offline_voice",
                       "broadcast_key": initial, "assets": assets,
                       "transitions": {"up": "https://assets.example.com/offline-up.mp4",
                                       "down": "https://assets.example.com/offline-down.mp4"}}
        (self.root / "external-appearance.json").write_text(json.dumps(self.config))
        self.studio.library_asset = lambda ident: {"status": "success", "sha256": hashlib.sha256(images[ident].read_bytes()).hexdigest()}
        self.studio.avatar_library.image_path = lambda ident: images[ident]
        self.studio.asset_tts_voice = lambda ident: "offline_voice"
        s.tts_voice = "offline_voice"
        s.apply_avatar(images[assets[initial]["avatar_id"]], assets[initial]["avatar_id"], self.studio.config["settings"])
        self.control = self.studio.external_game.appearance = ExternalAppearance(self.studio.external_game)
        # Router handlers were bound to the original controller; retain that
        # object and replace its in-memory configuration/state for this fixture.
        # Actual runs load once before route construction.
        for route in self.client.server.app.router.routes():
            handler = getattr(route, "handler", None)
            obj = getattr(handler, "__self__", None)
            if isinstance(obj, ExternalAppearance) and obj is not self.control:
                obj.__dict__.update(self.control.__dict__)
                self.control = self.studio.external_game.appearance = obj
        self.control.TRANSITION_GUARD_SECONDS = .02
        self.control.RECEIVER_TIMEOUT_SECONDS = .1
        self.control.POLL_SECONDS = .005
        async def pulse():
            while True:
                await asyncio.sleep(.005)
                now = time.monotonic()
                self.studio.player.update(frames=self.studio.player["frames"] + 1, seen=now, frame_seen=now)
        self.pulse_task = asyncio.create_task(pulse())
        async def stop_pulse():
            self.pulse_task.cancel()
            await asyncio.gather(self.pulse_task, return_exceptions=True)
        self.addAsyncCleanup(stop_pulse)
        self.context = {**self.identity, "mode_epoch": 1, "episode_id": "episode-1"}
        response = await self.post("external/appearance/lifecycle", {**self.context, "action": "bind"})
        self.assertEqual(response.status, 200, await response.text())
        return s

    async def restore_component(self):
        self.studio.current.component = self.original_component

    def body(self, key="refined", ident="appearance_request_1", purpose="game"):
        return {**self.context, "request_id": ident, "target_key": key, "purpose": purpose}

    async def switch(self, **kwargs):
        body = self.body(**kwargs)
        response = await self.post("external/appearance", body)
        self.assertEqual(response.status, 200, await response.text())
        record = self.control.requests[body["request_id"]]
        if record.get("task"):
            await record["task"]
        return self.control.receipt(record)

    async def confirm(self, ident="appearance_request_1", outcome="target_dynamic"):
        return await self.post("external/appearance/" + ident + "/confirm", {**self.context, "outcome": outcome})

    def frames(self):
        return [r["frame"] for r in self.renderer_component._ws.sent if r["frame"]["type"] == 16]

    async def test_automatic_adoption_preserves_person_voice_history_without_visual_claim(self):
        s = await self.configured()
        epoch, history = s.persona_epoch, copy.deepcopy(s.brain_context)
        s._prepared_comments["old"] = {"offline": True}
        response = await self.post("external/appearance", self.body())
        self.assertEqual(response.status, 200)
        await asyncio.sleep(.01)
        self.assertEqual(self.control.confirmed_key, "normal")
        self.assertTrue(self.studio.performance_holding)
        await self.control.requests["appearance_request_1"]["task"]
        receipt = self.control.receipt(self.control.requests["appearance_request_1"])
        self.assertEqual(receipt["status"], "applied")
        self.assertTrue(receipt["program_adopted"])
        self.assertEqual(receipt["evidence"], "transport_guard_healthy_receiver")
        for field in ("playback_confirmed", "operator_confirmed", "visual_confirmed", "success_acknowledged"):
            self.assertFalse(receipt[field])
        self.assertEqual(self.control.confirmed_key, "refined")
        self.assertFalse(self.studio.performance_holding)
        self.assertIsNone(self.control.public()["pending_confirmation"])
        self.assertEqual(s.persona_epoch, epoch)
        self.assertEqual(s.tts_voice, "offline_voice")
        self.assertEqual(s._prepared_comments, {})
        self.assertEqual(s.brain_context["character"], history["character"])
        self.assertEqual(self.frames()[0]["payload"]["switch_avatar"]["switch_videos"], [self.config["transitions"]["up"]])
        self.assertEqual((await self.confirm()).status, 409)
        self.assertEqual((await self.post("external/appearance", self.body())).status, 200)
        self.assertEqual((await self.post("external/appearance", self.body(key="fish"))).status, 409)
        self.assertEqual(len(self.frames()), 1)
        self.assertNotIn("https", json.dumps(self.control.public()))

    async def test_stellar_initial_is_not_normal_and_direction_is_server_selected(self):
        await self.configured("stellar")
        self.assertEqual(self.control.confirmed_key, "stellar")
        self.assertEqual((await self.post("external/appearance", self.body(key="refined"))).status, 409)
        receipt = await self.switch(key="normal")
        self.assertEqual(receipt["from_key"], "stellar")
        self.assertEqual(receipt["direction"], "down")
        self.assertEqual(self.frames()[0]["payload"]["switch_avatar"]["avatar_id"], self.config["assets"]["normal"]["avatar_id"])
        self.assertEqual(receipt["status"], "applied")
        self.assertTrue(self.control.initialized)
        self.assertEqual((await self.post("external/appearance", {**self.body(ident="appearance_bad_url"), "url": "https://example.com"})).status, 400)

    async def test_release_cancels_not_yet_written_and_old_epoch_cannot_rebind(self):
        await self.configured()
        recovery_gate = asyncio.Event()
        self.renderer_component.stop_idle_motions = AsyncMock(side_effect=lambda **kwargs: recovery_gate.wait())
        async def wait_recovery(**kwargs):
            await recovery_gate.wait()
        self.renderer_component.stop_idle_motions.side_effect = wait_recovery
        response = await self.post("external/appearance", self.body())
        self.assertEqual(response.status, 200)
        released = await self.post("external/appearance/lifecycle", {**self.context, "action": "release"})
        self.assertEqual(released.status, 200)
        recovery_gate.set()
        await self.control.requests["appearance_request_1"]["task"]
        self.assertEqual(self.frames(), [])
        self.assertEqual(self.control.requests["appearance_request_1"]["status"], "cancelled")
        self.assertEqual((await self.post("external/appearance/lifecycle", {**self.context, "action": "bind"})).status, 409)
        self.assertEqual((await self.post("external/appearance", self.body(ident="old_target_again"))).status, 409)
        self.context = dict(self.context, mode_epoch=2, episode_id="episode-2")
        self.assertEqual((await self.post("external/appearance/lifecycle", {**self.context, "action": "bind"})).status, 200)
        old = dict(self.body(ident="old_late_target"), mode_epoch=1, episode_id="episode-1")
        self.assertEqual((await self.post("external/appearance", old)).status, 409)

    async def test_failure_during_guard_blocks_and_late_failure_after_adoption_stops(self):
        s = await self.configured()
        response = await self.post("external/appearance", self.body())
        self.assertEqual(response.status, 200)
        await asyncio.sleep(.005)
        record = self.control.requests["appearance_request_1"]
        self.renderer_component._handle_control_error({"type": 103, "seq_id": record["seq_id"],
            "payload": {"error": {"source": "switch_avatar", "code": "OFFLINE_FAILURE"}}})
        await record["task"]
        self.assertEqual(self.control.public()["receipt"]["status"], "failed")
        self.assertTrue(self.studio.performance_holding)
        self.assertEqual(self.control.confirmed_key, "normal")
        self.assertEqual((await self.confirm()).status, 409)
        original = await self.confirm(outcome="original_dynamic")
        self.assertEqual(original.status, 200, await original.text())
        receipt = await self.switch(ident="appearance_request_2")
        self.assertEqual(receipt["status"], "applied")
        s.latch_stop = Mock()
        s.request_stop = AsyncMock()
        self.renderer_component._handle_control_error({"type": 103, "seq_id": receipt["seq_id"],
            "payload": {"error": {"source": "switch_avatar", "code": "LATE_FAILURE"}}})
        self.control.public()
        self.assertEqual(self.control.fault, "appearance_late_failure")
        self.assertTrue(self.studio.performance_holding)
        await asyncio.sleep(0)
        s.request_stop.assert_awaited_once()

    async def test_guard_without_new_receiver_frames_times_out_without_retry(self):
        await self.configured()
        self.pulse_task.cancel()
        await asyncio.gather(self.pulse_task, return_exceptions=True)
        receipt = await self.switch()
        self.assertEqual(receipt["status"], "unconfirmed")
        self.assertEqual(receipt["error_code"], "APPEARANCE_RECEIVER_TIMEOUT")
        self.assertFalse(receipt["program_adopted"])
        self.assertEqual(self.control.confirmed_key, "normal")
        self.assertTrue(self.studio.performance_holding)
        again = await self.post("external/appearance", self.body())
        self.assertEqual((await again.json())["status"], "unconfirmed")
        self.assertEqual(len(self.frames()), 1)

    async def test_frames_before_guard_do_not_replace_post_guard_progress(self):
        await self.configured()
        response = await self.post("external/appearance", self.body())
        self.assertEqual(response.status, 200)
        await asyncio.sleep(.01)
        self.assertGreater(self.studio.player["frames"], 1)
        self.pulse_task.cancel()
        await asyncio.gather(self.pulse_task, return_exceptions=True)
        record = self.control.requests["appearance_request_1"]
        await record["task"]
        self.assertEqual(record["status"], "unconfirmed")
        self.assertFalse(record["program_adopted"])
        self.assertEqual(self.control.confirmed_key, "normal")

    async def test_receiver_change_and_track_loss_never_adopt_target(self):
        for field in ("replacement", "audio_live"):
            # Restore a clean controller between two independently consumed IDs.
            if field == "audio_live":
                self.control.reset()
                self.control.readiness()
                self.context["mode_epoch"] += 1
                await self.post("external/appearance/lifecycle", {**self.context, "action": "bind"})
            else:
                await self.configured()
            body = self.body(ident="appearance_" + field)
            response = await self.post("external/appearance", body)
            self.assertEqual(response.status, 200)
            await asyncio.sleep(.005)
            if field == "replacement":
                self.studio.player = dict(self.studio.player)
            else:
                self.studio.player["audio_live"] = False
            record = self.control.requests[body["request_id"]]
            await record["task"]
            self.assertEqual(record["status"], "unconfirmed")
            self.assertFalse(record["program_adopted"])
            self.assertTrue(self.studio.performance_holding)
            self.studio.avatar_switch_holding = False

    async def test_missing_assets_voice_hash_and_https_are_real_blockers(self):
        await self.configured()
        self.studio.asset_tts_voice = lambda ident: "other_voice"
        self.assertEqual(self.control.readiness(), "appearance_asset_binding_invalid")
        self.studio.asset_tts_voice = lambda ident: "offline_voice"
        self.studio.current.avatar_id = "unknown"
        self.assertEqual(self.control.readiness(), "appearance_identity_unknown")
        self.config["transitions"]["up"] = "http://assets.example.com/up.mp4"
        (self.root / "external-appearance.json").write_text(json.dumps(self.config))
        invalid = ExternalAppearance(self.studio.external_game)
        self.assertEqual(invalid.readiness(), "appearance_config_invalid")

    async def test_last_q_guard_precedes_type16_and_release_can_revoke_during_wait(self):
        await self.configured()
        self.renderer_component._motion_not_before = time.monotonic() + .1
        response = await self.post("external/appearance", self.body())
        self.assertEqual(response.status, 200)
        await asyncio.sleep(.02)
        self.assertEqual(self.frames(), [])
        release = await self.post("external/appearance/lifecycle", {**self.context, "action": "release"})
        self.assertEqual(release.status, 200)
        await self.control.requests["appearance_request_1"]["task"]
        self.assertEqual(self.frames(), [])
        self.assertEqual(self.control.requests["appearance_request_1"]["status"], "cancelled")

    async def test_formal_jobs_and_unknown_sender_block_type16(self):
        await self.configured()
        item = {"status": "sending", "external_worker_pending": True}
        self.studio.external_game.jobs["formal"] = {"item": item}
        self.assertEqual((await self.post("external/appearance", self.body())).status, 409)
        self.assertEqual(self.frames(), [])
        self.studio.external_game.jobs.clear()
        self.renderer_component._ws.send_json = AsyncMock(side_effect=ConnectionError("offline loss"))
        receipt = await self.switch()
        self.assertEqual(receipt["status"], "unconfirmed")
        self.assertTrue(self.studio.performance_holding)
        self.assertEqual((await self.confirm()).status, 409)
