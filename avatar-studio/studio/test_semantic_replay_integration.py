"""Verified semantic replay reaches S transport without a new model/TTS request."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import live_session
from motion_component import MotionComponent
from semantic_replay import SemanticFixedReplay
from test_motion_component import FakeSocket, NoNetwork
import test_replay_integration as replay_integration
from test_semantic_replay import make_source


server = replay_integration.server


class SemanticReplayIntegrationTests(replay_integration.ReplayIntegrationTests):
    ge_lead_seconds = .5

    def setUp(self):
        # Reuse the controller's duplicate-click, input isolation, cancellation,
        # and queued-plan tampering tests against the new semantic fixture.
        import tempfile
        from pathlib import Path
        self.tmp = tempfile.TemporaryDirectory(prefix="semantic-replay-integration-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.fixture = SemanticFixedReplay(make_source(self.root / "source"),
                                           ge_lead_seconds=self.ge_lead_seconds)

    async def studio(self, *, bind=True, ready=True):
        ai = SimpleNamespace(configured=Mock(side_effect=AssertionError("no model credentials")),
                             public=Mock(side_effect=AssertionError("no model credential status")),
                             generate_reply=AsyncMock(), generate_director=AsyncMock(),
                             select_comments=AsyncMock(), plan_timed_actions=AsyncMock())
        studio = server.Studio(self.root / "studio", ai=ai, preview=True, replay_fixture=self.fixture)

        async def offline_startup(session, app):
            session.http = NoNetwork()
            component = MotionComponent(NoNetwork(), session.out, session.image, session.diagnostics)
            component._ws = FakeSocket()
            component.live_id = "offline-only"
            component.state = "idle"
            session.component = component

        with patch.object(live_session.LiveSession, "startup", offline_startup):
            await studio.new_session()
        if bind:
            self.bind_identity(studio)
        if ready:
            studio.current.started = True
            studio.current.component.state = "ready"
            studio.current.component._ready.set()
            studio.current.component._joined.set()
        return studio, ai

    async def test_mode_snapshot_and_avatar_image_identity_bind_before_replay(self):
        with self.assertRaises(ValueError):
            server.Studio(self.root / "not-preview", replay_fixture=self.fixture)
        with self.assertRaises(ValueError):
            server.Studio(self.root / "mixed", preview=True, long_speech_test=True,
                          replay_fixture=self.fixture)
        with self.assertRaises(ValueError):
            live_session.LiveSession(self.root / "mixed-run", "A", long_speech_test=True,
                                     replay_fixture=self.fixture)
        studio, ai = await self.studio(bind=False)
        self.assertTrue(studio.replay_identity_reason())
        self.assertFalse(studio.replay_public()["available"])
        self.bind_identity(studio)
        snapshot = studio.snapshot()
        self.assertTrue(snapshot["studio"]["semantic_replay"])
        self.assertTrue(snapshot["studio"]["semantic_speech"])
        self.assertFalse(snapshot["studio"]["long_speech_test"])
        self.assertEqual(snapshot["brain"]["provider"], "fixed_replay")
        self.assertEqual(snapshot["studio"]["fixed_replay"]["s_seconds"], [.08, 6.64])
        self.assertEqual(snapshot["studio"]["fixed_replay"]["q_seconds"], [1.58, 8.14])
        self.assertEqual(snapshot["studio"]["fixed_replay"]["ge_lead_seconds"], self.ge_lead_seconds)
        self.assertEqual(snapshot["studio"]["fixed_replay"]["independent_ge"], self.ge_lead_seconds is not None)
        ai.public.assert_not_called()
        ai.configured.assert_not_called()
        mode = json.loads((studio.current.out / "source-used/run-mode.json").read_text())
        self.assertTrue(mode["semantic_replay"])
        self.assertEqual(mode["semantic_action_kind"], "S")
        self.assertEqual(mode["ge_lead_seconds"], self.ge_lead_seconds)
        self.assertEqual((studio.current.out / "replay-source/sample.pcm").read_bytes(), self.fixture.pcm)
        studio.current.avatar_id = "999"
        self.assertFalse(studio.replay_public()["available"])

    async def test_worker_uses_original_pcm_once_without_tts_credentials_or_new_usage(self):
        studio, ai = await self.studio()
        comment = await self.enqueue(studio)
        receipt = {"group_id": "offline-send-proof", "send_complete": True,
                   "actions_sent_complete": True, "playback_complete_confirmed": False}
        studio.current.work.put_nowait(None)
        with patch("live_session.load_bailian", side_effect=AssertionError("no credentials")) as credentials, \
             patch("live_session.synthesize_segment", side_effect=AssertionError("no TTS")) as tts, \
             patch.object(studio.current.component, "send_expression", AsyncMock()) as legacy, \
             patch.object(studio.current.component, "send_aligned_expression", AsyncMock(return_value=receipt)) as sender:
            await studio.current.process_answers()
        credentials.assert_not_called()
        tts.assert_not_called()
        legacy.assert_not_awaited()
        ai.plan_timed_actions.assert_not_awaited()
        ai.generate_reply.assert_not_awaited()
        ai.generate_director.assert_not_awaited()
        sender.assert_awaited_once()
        self.assertEqual(sender.await_args.args[0], self.fixture.pcm)
        self.assertEqual(sender.await_args.args[1], self.fixture.plan["segments"][0]["text"])
        self.assertEqual(sender.await_args.args[2], self.fixture.plan["segments"][0]["actions"])
        self.assertEqual(sender.await_args.kwargs["alignment"], self.fixture.alignment)
        self.assertEqual(sender.await_args.kwargs["action_kind"], "S")
        self.assertEqual(sender.await_args.kwargs["ge_lead_seconds"], self.ge_lead_seconds)
        self.assertFalse(sender.await_args.kwargs["allow_tail_q_after_audio"])
        self.assertEqual(comment["status"], "sent", comment.get("error"))
        self.assertEqual(comment["segments_sent"], 1)
        self.assertEqual(comment["usage"], {"input_tokens": 0, "output_tokens": 0, "cost_cny": 0})
        self.assertEqual(studio.current.totals["audio_seconds"], 10.4)
        for field in ("input_tokens", "output_tokens", "cost_cny", "usage_unverified_segments"):
            self.assertEqual(studio.current.totals[field], 0)
        folder = studio.current.out / "tts" / f'{comment["id"]}-1'
        self.assertEqual((folder / "sample.pcm").read_bytes(), self.fixture.pcm)
        self.assertEqual(json.loads((folder / "component-send.json").read_text()), receipt)
        self.assertEqual([n["role"] for n in comment["delivery"][0]["timeline"]],
                         (["GE", "S", "Q"] if self.ge_lead_seconds is not None else ["S", "Q"]) * 2)


class SemanticReplayGE11IntegrationTests(SemanticReplayIntegrationTests):
    ge_lead_seconds = 1.1


class SemanticReplayEmbeddedGEIntegrationTests(SemanticReplayIntegrationTests):
    ge_lead_seconds = None

