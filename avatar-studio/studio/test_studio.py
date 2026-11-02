"""Offline studio contracts using loopback HTTP and a fake renderer; no credentials/cloud calls."""
import asyncio
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("studio_server_test_target", HERE / "server.py")
studio_server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(studio_server)
import live_session
import obs_setup

REAL_ASSERT_PREVIOUS = live_session.LiveSession.assert_previous_cloud_ended
REAL_VERIFY_PREVIOUS = live_session.LiveSession.verify_previous_cloud_ended


class FakeComponent:
    RATE = 24000
    def __init__(self, *args, **kwargs):
        self.state = "idle"
        self.live_id = None
        self.error = ""
        self.comment_id = None
        self.created = False
        self.stopped = False
        self.start = AsyncMock(side_effect=self._start)
        self._idle_motion_state = {"enabled": False, "status": "off", "task_running": False,
            "needs_recovery": False, "recovery_failed": False, "last_error": "",
            "current_action_id": None, "next_tick_at_monotonic": None,
            "seconds_until_next": None, "recent": [], "visual_effect_confirmed": False}
        self._idle_motion_allowed = self._idle_motion_interrupt = self._idle_motion_valid_session = None
        self.start_idle_motions = Mock(side_effect=self._start_idle_motions)
        self.stop_idle_motions = AsyncMock(side_effect=self._stop_idle_motions)
        self.test_idle_motion = Mock(side_effect=self._test_idle_motion)
        self.recover_idle_motions = AsyncMock(side_effect=self._recover_idle_motions)

    def _start_idle_motions(self, actions, settings, idle_allowed, interrupt_requested,
                            valid_session=None, *, once=None):
        # Model only the host's ownership/callback contract. Fake-clock transport
        # tests exercise actual I/Q frames and recovery ambiguity separately.
        self._idle_motion_actions = copy.deepcopy(actions)
        self._idle_motion_settings = copy.deepcopy(settings)
        self._idle_motion_allowed = idle_allowed
        self._idle_motion_interrupt = interrupt_requested
        self._idle_motion_valid_session = valid_session
        self._idle_motion_state.update(enabled=True, status="paused", task_running=True)
        return self.idle_motions_public()

    def _test_idle_motion(self, action, idle_allowed, interrupt_requested, valid_session=None):
        if not idle_allowed() or interrupt_requested():
            raise ValueError("当前忙碌，不能试播待机动作")
        return self._start_idle_motions([action], {"min_interval_seconds": 15, "max_interval_seconds": 25},
                                        idle_allowed, interrupt_requested, valid_session, once=action["id"])

    async def _stop_idle_motions(self, recover=True, *, explicit=True):
        self._idle_motion_state.update(enabled=False, task_running=False, status="off")
        if recover and not explicit and self._idle_motion_state["recovery_failed"]:
            raise studio_server.IdleRecoveryError("待机回正未确认，请先显式恢复")
        if recover:
            self._idle_motion_state.update(needs_recovery=False, recovery_failed=False, last_error="")
        return self.idle_motions_public()

    async def _recover_idle_motions(self):
        return await self._stop_idle_motions(recover=True)

    def idle_motions_public(self):
        return copy.deepcopy(self._idle_motion_state)

    async def _start(self, key, *, avatar_id=None):
        self.created = True
        self.live_id = "offline-session"
        self.state = "waiting_stage"

    def public(self):
        return {"state": self.state, "live_id": self.live_id,
                "cloud_end_confirmed": self.stopped and self.created,
                "no_cloud_session": not self.created,
                "creation_outcome_unknown": False, "recording_pending": False,
                "motion_events": [], "error": self.error,
                "idle_motions": self.idle_motions_public()}

    def rtc(self):
        return {"provider": "artc", "user_id": "offline-viewer", "token": "offline-test-grant"} if self.state in {"waiting_stage", "ready", "sending", "draining"} else {}

    def joined(self):
        self.state = "ready"

    def latch_stop(self):
        self._idle_motion_state.update(enabled=False, task_running=False, status="stopped")
        self.state = "stopping"

    async def stop(self, reason=""):
        self._idle_motion_state.update(enabled=False, task_running=False, status="stopped")
        self.stopped = True
        self.state = "ended"
        return self.public()

    async def close(self):
        self._idle_motion_state.update(enabled=False, task_running=False, status="stopped")
        return None


class FakeAI:
    """构造、就绪检查和关闭均不访问凭据或网络。"""
    def __init__(self):
        self.last_error = ""
        self.select_comments = AsyncMock(side_effect=AssertionError("unexpected selection call"))
        self.generate_reply = AsyncMock(side_effect=AssertionError("unexpected reply call"))
        self.close = AsyncMock()

    def configured(self):
        return True

    def public(self):
        return {"provider": "bailian", "model": "offline-model", "configured": True}


class StudioContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name)
        self.credentials_marker = self.data / "offline-config-marker"
        self.credentials_marker.touch()
        # Any unplanned provider preflight or credential access fails immediately.
        self.patches = [
            patch.object(live_session, "PersistentComponent", FakeComponent),
            patch.object(live_session.LiveSession, "assert_previous_cloud_ended", return_value=None),
            patch.object(live_session.LiveSession, "verify_previous_cloud_ended", new=AsyncMock()),
            patch.object(live_session, "load_bailian", side_effect=AssertionError("unexpected credential read")),
            patch.object(live_session, "load_vidu", side_effect=AssertionError("unexpected credential read")),
            patch.object(studio_server, "load_bailian", return_value=("offline-key", "offline-workspace")),
            patch.object(studio_server, "load_vidu", return_value="offline-key"),
            patch.object(studio_server, "LEGACY_SETTINGS", self.credentials_marker),
            # HTTP合同测试手动推进队列，后台模型循环由test_automation单独验证。
            patch.object(studio_server.Automation, "start", return_value=None),
        ]
        for entry in self.patches:
            entry.start()
        self.control = studio_server.Studio(self.data, ai=FakeAI())
        self.control.automation.running = Mock(return_value=True)
        self.client = TestClient(TestServer(self.control.build(), host="127.0.0.1"))
        await self.client.start_server()
        self.control.origin = str(self.client.make_url("/")).rstrip("/")

    async def asyncTearDown(self):
        await self.client.close()
        for entry in reversed(self.patches):
            entry.stop()
        self.temporary.cleanup()

    async def post(self, path, body=None, *, player=False, headers=None):
        supplied = {"Origin": self.control.origin}
        supplied.update(headers or {})
        prefix = self.control.player_prefix if player else self.control.prefix
        return await self.client.post(prefix + "/" + path, json=body or {}, headers=supplied)

    async def claim(self, client_id="offline-player-one"):
        response = await self.post("claim", {"client_id": client_id}, player=True)
        self.assertEqual(response.status, 200)
        return await response.json()

    async def pending(self):
        self.control.current.component.state = "ready"
        comment = self.control.enqueue_comment("最近粮价如何", "观众")
        comment["status"] = "processing"
        return comment

    def plan(self, pending):
        return {"emotion": "平静", "observation": "当前姿态未知", "reading": None,
                "segments": [{"text": "咱先听听你那边的情况。", "instruction": "自然清晰正常语速", "actions": [], "recovery": None}]}

    async def test_new_session_preserves_urls_and_has_no_paid_start(self):
        old, old_url, old_player = self.control.generation, self.control.prefix, self.control.player_prefix
        runtime = self.control.snapshot()["studio"]["runtime"]
        component = self.control.current.component
        component.start.assert_not_awaited()
        await self.post("stop")
        await asyncio.sleep(0)
        response = await self.post("new")
        self.assertEqual(response.status, 200)
        self.assertNotEqual(self.control.generation, old)
        self.assertEqual((self.control.prefix, self.control.player_prefix), (old_url, old_player))
        self.control.current.component.start.assert_not_awaited()
        self.assertFalse(self.control.current.started)
        self.assertEqual(self.control.snapshot()["studio"]["runtime"], runtime)

    async def test_stop_is_scheduled_before_checkpoint_failures(self):
        session = self.control.current
        component = session.component
        with patch.object(self.control.director, "stop", side_effect=OSError("offline disk failure")), \
             patch.object(session, "checkpoint", side_effect=OSError("offline disk failure")):
            response = await self.post("stop")
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.json(), {"requested": True})
            await asyncio.sleep(0)
            self.assertTrue(component.stopped)

    async def test_state_runtime_is_safe_cached_launch_identity_not_current_disk(self):
        runtime = self.control.snapshot()["studio"]["runtime"]
        self.assertEqual(set(runtime), {"pid", "started_at", "source_version"})
        self.assertEqual(runtime["pid"], studio_server.os.getpid())
        self.assertLessEqual(runtime["started_at"], time.time())
        self.assertRegex(runtime["source_version"], r"^[a-f0-9]{64}$")
        with patch.object(studio_server, "_startup_source_version", side_effect=AssertionError("must stay cached")):
            response = await self.client.get(self.control.prefix + "/state")
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["studio"]["runtime"], runtime)
        runtime["source_version"] = "caller mutation"
        self.assertNotEqual(self.control.snapshot()["studio"]["runtime"]["source_version"], runtime["source_version"])

    async def test_normal_mode_advertises_the_same_a_policy_as_archived_runtime(self):
        state = self.control.snapshot()["studio"]
        self.assertIsNone(state["fixed_replay"])
        self.assertEqual(state["dialogue_action_policy"], {
            "name": "A", "action_kind": "S", "ge_lead_seconds": 1.1, "short_to_q_seconds": 1.5})
        variant = json.loads((self.control.current.out / "variant.json").read_text())
        mode = json.loads((self.control.current.out / "source-used/run-mode.json").read_text())
        for evidence in (variant, mode):
            self.assertEqual(evidence["dialogue_action_policy"], "A")
            self.assertEqual(evidence["semantic_action_kind"], "S")
            self.assertEqual(evidence["short_to_q_seconds"], 1.5)
        self.assertEqual(variant["semantic_ge_lead_seconds"], 1.1)
        self.assertEqual(mode["ge_lead_seconds"], 1.1)
        self.control.current.component.start.assert_not_awaited()

    async def test_fixed_replay_public_exposes_complete_actual_fixture_timeline(self):
        from test_semantic_replay import make_source
        from semantic_replay import SemanticFixedReplay
        original = self.control.replay_fixture
        try:
            for index, lead in enumerate((1.1, None)):
                fixture = SemanticFixedReplay(make_source(self.data / f"timeline-source-{index}"), ge_lead_seconds=lead)
                self.control.replay_fixture = fixture
                self.assertIsNone(self.control.snapshot()["studio"]["dialogue_action_policy"])
                public = self.control.replay_public()
                expected = fixture.plan["segments"][0]["timeline"]
                self.assertEqual(public["timeline"], expected)
                self.assertEqual(len(public["timeline"]), 6 if lead is not None else 4)
                self.assertTrue(all(node["instruction"] for node in public["timeline"]))
                public["timeline"][0]["instruction"] = "caller mutation"
                self.assertEqual(self.control.replay_public()["timeline"], expected)
        finally:
            self.control.replay_fixture = original

    async def test_concurrent_start_creates_exactly_once(self):
        await self.claim()
        component = self.control.current.component
        responses = await asyncio.gather(self.post("start"), self.post("start"))
        self.assertEqual(sorted(r.status for r in responses), [200, 409])
        await asyncio.sleep(0)
        component.start.assert_awaited_once_with("offline-key")

    async def exercise_start_after_previous_readback(self, old_status):
        await self.claim()
        session = self.control.current
        component = session.component
        root = self.data / "historical-studio"
        previous = root / "data" / "runs" / "previous"
        previous.mkdir(parents=True)
        (previous / "start-attempted.json").write_text("{}")
        (previous / "component-receipt.json").write_text(json.dumps({
            "live_id": "123", "cloud_end_confirmed": False,
            "creation_outcome_unknown": False,
        }))
        entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        order = []

        class PreviousResponse:
            status = 200

            async def __aenter__(self):
                order.append("get")
                entered.set()
                await release.wait()
                return self

            async def __aexit__(self, *_):
                return False

            async def json(self):
                order.append("readback")
                return {"live": {"status": old_status}}

        async def create(*args, **kwargs):
            order.append("create")
            await component._start(*args, **kwargs)
            completed.set()

        component.start = AsyncMock(side_effect=create)
        with patch.object(live_session, "HERE", root), \
             patch.object(live_session, "ARTIFACTS", self.data / "other-artifacts"), \
             patch.object(live_session, "PREVIOUS_COMPONENT_DIRS", ()), \
             patch.object(live_session.PersistentComponent, "BASE", "https://offline.invalid", create=True), \
             patch.object(live_session.LiveSession, "assert_previous_cloud_ended", REAL_ASSERT_PREVIOUS), \
             patch.object(live_session.LiveSession, "verify_previous_cloud_ended", REAL_VERIFY_PREVIOUS), \
             patch.object(session.http, "get", return_value=PreviousResponse()) as readback:
            self.assertEqual((await self.post("start")).status, 200)
            await asyncio.wait_for(entered.wait(), 1)
            component.start.assert_not_awaited()
            release.set()
            if old_status == "ended":
                await asyncio.wait_for(completed.wait(), 1)
                self.assertEqual(order, ["get", "readback", "create"])
                component.start.assert_awaited_once()
            else:
                for _ in range(100):
                    if component.stopped:
                        break
                    await asyncio.sleep(.01)
                self.assertTrue(component.stopped)
                self.assertEqual(order, ["get", "readback"])
                component.start.assert_not_awaited()
            readback.assert_called_once()

    async def test_known_previous_end_is_read_before_new_create(self):
        await self.exercise_start_after_previous_readback("ended")

    async def test_known_previous_not_ended_prevents_new_create(self):
        await self.exercise_start_after_previous_readback("running")

    async def test_unknown_previous_identity_blocks_control_start(self):
        await self.claim()
        session = self.control.current
        root = self.data / "historical-studio"
        previous = root / "data" / "runs" / "previous"
        previous.mkdir(parents=True)
        (previous / "start-attempted.json").write_text("{}")
        receipt = previous / "component-receipt.json"
        with patch.object(live_session, "HERE", root), \
             patch.object(live_session, "ARTIFACTS", self.data / "other-artifacts"), \
             patch.object(live_session, "PREVIOUS_COMPONENT_DIRS", ()), \
             patch.object(live_session.LiveSession, "assert_previous_cloud_ended", REAL_ASSERT_PREVIOUS), \
             patch.object(live_session.LiveSession, "verify_previous_cloud_ended", REAL_VERIFY_PREVIOUS), \
             patch.object(session.http, "get", side_effect=AssertionError("no remote request")) as readback:
            for value in ({}, {"live_id": "invalid-id"},
                          {"live_id": "123", "creation_outcome_unknown": True}):
                with self.subTest(value=value):
                    receipt.write_text(json.dumps(value))
                    self.assertEqual((await self.post("start")).status, 409)
                    self.assertFalse(session.started)
                    session.component.start.assert_not_awaited()
            readback.assert_not_called()

    async def test_start_records_selected_provider_and_includes_transport_source(self):
        await self.claim()
        self.assertEqual((await self.post("start")).status, 200)
        evidence = json.loads((self.control.current.out / "brain-model-evidence.json").read_text())
        self.assertEqual((evidence["provider"], evidence["model_requested"]), ("bailian", "offline-model"))
        self.assertFalse(evidence["network_verified"])
        manifest = json.loads((self.control.current.out / "source-manifest.json").read_text())
        self.assertIn("bailian_ai.py", manifest)

    async def test_manual_resume_keeps_bailian_and_does_not_replay_failed_work(self):
        expected = {"model": "qwen3.8-flash", "region": "cn-beijing", "enable_thinking": False}
        (self.data / "bailian-brain.json").write_text(json.dumps(expected))
        # An old options file must never change the selected provider on resume.
        (self.data / "cloudbase.json").write_text(json.dumps({"model": "old-model"}))
        ai = self.control.automation.ai
        self.control.automation.last_error = ai.last_error = "timeout"
        self.assertEqual((await self.post("ai/resume")).status, 200)
        self.assertIs(self.control.automation.ai, ai)
        self.assertEqual(ai.config, expected)
        self.assertEqual(self.control.automation.last_error, "")
        ai.select_comments.assert_not_awaited()
        ai.generate_reply.assert_not_awaited()

    async def test_start_uses_same_bound_asset_as_manual_switch(self):
        await self.claim()
        s = self.control.current
        self.control.avatar_switch_config = {
            "avatar_id": "12345", "switch_videos": ["https://media.example.test/one.mp4"],
            "source_image_sha256": s.variant_info["image_sha256"], "transition_label": "离线素材"}
        response = await self.post("start")
        self.assertEqual(response.status, 200)
        await asyncio.sleep(0)
        s.component.start.assert_awaited_once_with("offline-key", avatar_id="12345")

    async def test_start_rejects_asset_bound_to_different_source(self):
        await self.claim()
        s = self.control.current
        self.control.avatar_switch_config = {
            "avatar_id": "12345", "switch_videos": ["https://media.example.test/one.mp4"],
            "source_image_sha256": "0" * 64, "transition_label": "离线素材"}
        response = await self.post("start")
        self.assertEqual(response.status, 409)
        self.assertFalse(s.started)
        s.component.start.assert_not_awaited()

    def cross_avatar_config(self):
        fixture_root = self.data / "fixture-root"
        assets = fixture_root / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        target = assets / "smile.png"
        target.write_bytes(b"offline-new-smiling-image")
        self.enterContext(patch.object(studio_server, "HERE", fixture_root))
        value = {"avatar_id": "1111111111111111111",
                 "switch_videos": ["https://media.example.test/one.mp4"],
                 "source_image_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                 "transition_label": "离线交叉对照", "target_image": target.name,
                 "initial_avatar": {"avatar_id": "2222222222222222222",
                                    "source_image_sha256": self.control.current.variant_info["image_sha256"]}}
        return value, target

    def load_switch_config(self, value):
        (self.data / "type16-test.json").write_text(json.dumps(value))
        self.control.avatar_switch_config = self.control.load_avatar_switch_config()

    async def test_cross_avatar_start_and_switch_use_different_locally_bound_assets(self):
        await self.claim()
        value, target = self.cross_avatar_config()
        self.load_switch_config(value)
        self.assertEqual(self.control.avatar_switch_binding_reason(), "")
        self.assertEqual((await self.post("start")).status, 200)
        await asyncio.sleep(0)
        s = self.control.current
        s.component.start.assert_awaited_once_with("offline-key", avatar_id=value["initial_avatar"]["avatar_id"])
        s.component.state = "ready"
        body = {"generation": self.control.generation, "request_id": "offline-cross-avatar"}
        s.component.switch_avatar = AsyncMock(return_value={"request_id": body["request_id"], "status": "sent", "sent": True})
        self.assertEqual((await self.post("avatar-switch", body)).status, 200)
        avatar_id, videos, ident, valid = s.component.switch_avatar.await_args.args
        self.assertEqual(avatar_id, value["avatar_id"])
        self.assertNotEqual(avatar_id, s.component.start.await_args.kwargs["avatar_id"])
        self.assertEqual(videos, value["switch_videos"])
        self.assertTrue(valid())
        duplicate = await self.post("avatar-switch", body)
        self.assertTrue((await duplicate.json())["duplicate"])
        s.component.switch_avatar.assert_awaited_once()

    async def test_cross_avatar_initial_or_target_hash_mismatch_blocks_start_and_switch(self):
        await self.claim()
        s = self.control.current
        s.component.switch_avatar = AsyncMock()
        for mismatch in ("initial", "target"):
            with self.subTest(mismatch=mismatch):
                value, target = self.cross_avatar_config()
                if mismatch == "initial":
                    value["initial_avatar"]["source_image_sha256"] = "0" * 64
                self.load_switch_config(value)
                if mismatch == "target":
                    target.write_bytes(b"changed-after-configuration-load")
                s.started = False
                s.component.state = "idle"
                self.assertEqual((await self.post("start")).status, 409)
                self.assertFalse(s.started)
                s.started = True
                s.component.state = "ready"
                self.assertEqual((await self.post("avatar-switch", {
                    "generation": self.control.generation, "request_id": "offline-cross-mismatch"})).status, 409)
        s.component.start.assert_not_awaited()
        s.component.switch_avatar.assert_not_awaited()

    async def test_invalid_cross_config_fails_closed_without_image_uri_fallback(self):
        await self.claim()
        original, target = self.cross_avatar_config()
        cases = []
        for missing in ("initial_avatar", "target_image"):
            value = copy.deepcopy(original)
            del value[missing]
            cases.append(value)
        cases.append({**original, "unknown": True})
        for field, bad in (("avatar_id", "１２３"), ("source_image_sha256", "g"*64)):
            value = copy.deepcopy(original)
            value["initial_avatar"][field] = bad
            cases.append(value)
            cases.append({**original, field: bad})
        cases.append({**original, "initial_avatar": {**original["initial_avatar"], "extra": True}})
        for name in ("../smile.png", str(target.resolve()), "nested/smile.png", "missing.png", "empty.png", "too-big.png"):
            cases.append({**original, "target_image": name})
        (target.parent / "empty.png").touch()
        with (target.parent / "too-big.png").open("wb") as handle:
            handle.truncate(20_000_000)
        outside = target.parent.parent / "outside.png"
        outside.write_bytes(b"outside-assets")
        (target.parent / "escape.png").symlink_to(outside)
        cases.append({**original, "target_image": "escape.png"})
        for value in cases:
            with self.subTest(value=value):
                self.load_switch_config(value)
                self.assertEqual(self.control.avatar_switch_config, {})
                self.assertTrue(self.control.avatar_switch_config_error)
                self.assertEqual((await self.post("start")).status, 409)
                self.assertFalse(self.control.current.started)
        self.control.current.component.start.assert_not_awaited()

    async def test_quit_idle_has_no_cloud_side_effect_and_closes_start_gate(self):
        component = self.control.current.component
        response = await self.post("quit")
        self.assertEqual(response.status, 200)
        self.assertTrue(self.control.closing)
        self.assertIn("退出", self.control.blocking_reason())
        component.start.assert_not_awaited()

    async def test_quit_active_session_is_rejected(self):
        self.control.current.started = True
        self.control.current.component.state = "ready"
        response = await self.post("quit")
        self.assertEqual(response.status, 409)
        self.assertFalse(self.control.closing)

    async def test_second_player_cannot_receive_rtc_or_steal_released_lease(self):
        first = await self.claim()
        second = await self.claim("offline-player-two")
        self.assertTrue(first["granted"])
        self.assertFalse(second["granted"])
        self.control.current.component.state = "waiting_stage"
        query = "?client_id=offline-player-two&generation=" + self.control.generation
        response = await self.client.get(self.control.player_prefix + "/rtc" + query)
        self.assertEqual(response.status, 409)
        query = "?client_id=offline-player-one&generation=" + self.control.generation
        response = await self.client.get(self.control.player_prefix + "/rtc" + query)
        self.assertEqual(response.status, 200)
        self.assertIn("token", await response.json())
        await self.post("release", {"client_id": "offline-player-one", "generation": self.control.generation}, player=True)
        self.assertFalse((await self.claim("offline-player-two"))["granted"])

    async def test_stop_rejects_pending_reply_before_any_tts(self):
        pending = await self.pending()
        await self.post("stop")
        with self.assertRaises(ValueError):
            self.control.accept_plan(self.control.current, pending, self.plan(pending))
        self.assertTrue(self.control.current.work.empty())
        self.assertEqual(self.control.current.comments[0]["status"], "cancelled")

    async def test_stop_rejects_replay_of_previously_accepted_reply(self):
        pending = await self.pending()
        reply = self.plan(pending)
        # Exercise the accepted fingerprint without allowing the speech worker to run.
        with patch.object(self.control.current.work, "put_nowait"):
            self.control.accept_plan(self.control.current, pending, reply)
        await self.post("stop")
        with self.assertRaises(ValueError):
            self.control.accept_plan(self.control.current, pending, reply)
        self.assertTrue(self.control.current.work.empty())

    async def test_old_generation_player_and_reply_are_rejected(self):
        await self.claim()
        previous_generation = self.control.generation
        previous = await self.pending()
        previous_session = self.control.current
        await self.post("stop")
        await asyncio.sleep(0)
        self.assertEqual((await self.post("new")).status, 200)
        await self.claim()
        current = await self.pending()
        self.assertEqual(previous["id"], current["id"])
        with self.assertRaises(ValueError):
            self.control.accept_plan(previous_session, previous, self.plan(previous))
        response = await self.post("heartbeat", {"client_id": "offline-player-one", "generation": previous_generation,
                                                "frames": 3, "audio_live": True, "video_live": True}, player=True)
        self.assertEqual(response.status, 409)
        self.assertTrue(self.control.current.work.empty())

    async def test_pause_stops_new_intake_and_skip_only_pending(self):
        self.control.current.component.state = "ready"
        self.assertEqual((await self.post("pause", {"paused": True})).status, 200)
        self.assertEqual((await self.post("comments", {"text": "暂停时不应接收"})).status, 409)
        await self.post("pause", {"paused": False})
        response = await self.post("comments", {"text": "这条可跳过"})
        comment_id = (await response.json())["id"]
        self.assertEqual((await self.post("skip", {"id": comment_id})).status, 200)
        self.assertEqual(self.control.current.comments[0]["status"], "skipped")
        processing = await self.pending()
        self.assertEqual((await self.post("skip", {"id": processing["id"]})).status, 409)

    async def test_settings_are_idle_only_and_propagate_to_brain_context(self):
        settings = {"name": "新名称", "persona": "测试人设", "reply_style": "简短回答"}
        self.assertEqual((await self.post("settings", settings)).status, 200)
        self.assertEqual(self.control.current.brain_context["operator_settings"], settings)
        self.control.current.component.state = "waiting_stage"
        self.assertEqual((await self.post("settings", settings)).status, 409)

    async def test_origin_host_and_brain_token_protection(self):
        self.assertEqual((await self.post("pause", {"paused": True}, headers={"Origin": "https://example.invalid"})).status, 403)
        response = await self.client.post(self.control.prefix + "/pause", json={"paused": True})
        self.assertEqual(response.status, 403)

    async def test_legacy_brain_interfaces_are_gone_even_with_valid_token(self):
        headers = {"X-Brain-Token": self.control.brain_token}
        response = await self.client.get(self.control.prefix + "/brain/next?wait=0", headers=headers)
        self.assertEqual(response.status, 410)
        self.assertEqual((await self.post("brain/reply", self.plan({}), headers=headers)).status, 410)
        response = await self.client.get(self.control.prefix + "/state", headers={"Host": "example.invalid"})
        self.assertEqual(response.status, 403)
        response = await self.client.get(self.control.prefix + "/brain/next?wait=0")
        self.assertEqual(response.status, 403)

    async def test_player_join_requires_real_finite_frame_evidence(self):
        await self.claim()
        self.control.current.component.state = "waiting_stage"
        proof = {"client_id": "offline-player-one", "generation": self.control.generation, "audio_live": True, "video_live": True}
        for frames in (0, -1, True, float("nan"), float("inf")):
            response = await self.post("joined", {**proof, "frames": frames}, player=True)
            self.assertEqual(response.status, 400, repr(frames))
        response = await self.post("joined", {**proof, "frames": 2}, player=True)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.control.current.component.state, "ready")

    async def test_idle_player_release_allows_replacement_before_any_paid_start(self):
        await self.claim()
        response = await self.post("release", {"client_id": "offline-player-one", "generation": self.control.generation}, player=True)
        self.assertEqual(response.status, 200)
        await asyncio.sleep(0)
        self.assertTrue((await self.claim("offline-player-two"))["granted"])
        self.control.current.component.start.assert_not_awaited()

    async def test_default_obs_mode_rejects_preview_without_claiming_owner(self):
        response = await self.post("claim", {"client_id": "preview-player-one", "receiver_mode": "preview"}, player=True)
        self.assertFalse((await response.json())["granted"])
        self.assertIsNone(self.control.player)
        self.assertTrue((await self.claim())["granted"])

    async def test_preview_mode_excludes_obs_and_second_preview(self):
        self.control.receiver_mode = "preview"
        self.assertFalse((await self.claim())["granted"])
        response = await self.post("claim", {"client_id": "preview-player-one", "receiver_mode": "preview"}, player=True)
        self.assertTrue((await response.json())["granted"])
        self.assertFalse((await self.claim())["granted"])
        response = await self.post("claim", {"client_id": "preview-player-two", "receiver_mode": "preview"}, player=True)
        self.assertFalse((await response.json())["granted"])
        self.assertEqual(self.control.snapshot()["studio"]["receiver_mode"], "preview")
        self.control.current.component.start.assert_not_awaited()

    async def test_active_preview_release_stops_and_does_not_allow_takeover(self):
        self.control.receiver_mode = "preview"
        await self.post("claim", {"client_id": "preview-player-one", "receiver_mode": "preview"}, player=True)
        await self.post("start")
        await asyncio.sleep(0)
        response = await self.post("release", {"client_id": "preview-player-one", "generation": self.control.generation}, player=True)
        self.assertEqual(response.status, 200)
        await asyncio.sleep(0)
        self.assertTrue(self.control.current.stopping)
        response = await self.post("claim", {"client_id": "preview-player-two", "receiver_mode": "preview"}, player=True)
        self.assertFalse((await response.json())["granted"])

    async def test_director_requires_ready_session_and_existing_origin_boundary(self):
        body = {"text": "郑重介绍自己", "mode": "once"}
        self.assertEqual((await self.post("director/submit", body)).status, 409)
        self.control.current.started = True
        self.control.current.component.state = "ready"
        self.assertEqual((await self.post("director/submit", body, headers={"Origin": "https://example.invalid"})).status, 403)
        response = await self.post("director/submit", body)
        self.assertEqual(response.status, 200)
        item = self.control.current.comments[0]
        self.assertEqual((item["input_kind"], item["operator_mode"]), ("operator", "once"))
        self.control.current.component.start.assert_not_awaited()

    def prepare_avatar_switch(self):
        s = self.control.current
        self.control.avatar_switch_config = {"avatar_id": "12345", "switch_videos": ["https://media.example.test/one.mp4"],
            "source_image_sha256": s.variant_info["image_sha256"], "transition_label": "单帧 0.04 秒"}
        s.started = True
        s.component.state = "ready"
        return {"generation": self.control.generation, "request_id": "offline-switch-one"}

    async def test_avatar_switch_hold_blocks_work_until_explicit_resume(self):
        body = self.prepare_avatar_switch()
        s = self.control.current
        receipt = {"request_id": body["request_id"], "status": "sent", "sent": True, "seq_id": 1,
                   "visual_effect_confirmed": False}
        s.component.switch_avatar = AsyncMock(return_value=receipt)
        self.assertEqual((await self.post("avatar-switch", body)).status, 200)
        avatar_id, videos, ident, valid = s.component.switch_avatar.await_args.args
        self.assertEqual((avatar_id, videos, ident), ("12345", ["https://media.example.test/one.mp4"], body["request_id"]))
        self.assertTrue(valid())
        self.assertFalse(self.control.automation.live_ready())
        self.assertEqual((await self.post("comments", {"text": "挥手"})).status, 409)
        self.assertEqual((await self.post("director/submit", {"text": "挥手", "mode": "motion"})).status, 409)
        duplicate = await self.post("avatar-switch", body)
        self.assertTrue((await duplicate.json())["duplicate"])
        self.assertEqual(s.component.switch_avatar.await_count, 1)
        public = self.control.snapshot()["studio"]["avatar_switch"]
        self.assertTrue(public["holding"] and public["resume_available"])
        self.assertNotIn("https://", str(public))
        response = await self.post("avatar-switch/resume", {"generation": self.control.generation})
        self.assertEqual(response.status, 200)
        self.assertFalse(self.control.avatar_switch_holding)
        self.assertTrue(self.control.automation.live_ready())
        self.assertFalse(receipt["visual_effect_confirmed"])
        s.component.start.assert_not_awaited()
        self.control.automation.ai.generate_reply.assert_not_awaited()

    async def test_avatar_switch_rejects_stale_injected_busy_and_mismatched_target(self):
        body = self.prepare_avatar_switch()
        s = self.control.current
        s.component.switch_avatar = AsyncMock()
        self.assertEqual((await self.post("avatar-switch", body, headers={"Origin": "https://example.invalid"})).status, 403)
        self.assertEqual((await self.post("avatar-switch", {**body, "generation": "old"})).status, 409)
        self.assertEqual((await self.post("avatar-switch", {**body, "avatar_id": "attacker"})).status, 400)
        for state in ("sending", "draining", "ended"):
            s.component.state = state
            self.assertEqual((await self.post("avatar-switch", body)).status, 409)
        s.component.state = "ready"
        c = self.control.enqueue_comment("再等等", "观众")
        self.assertEqual((await self.post("avatar-switch", body)).status, 409)
        c["status"] = "sent"
        self.control.director.topic = {"state": "running"}
        self.assertEqual((await self.post("avatar-switch", body)).status, 409)
        self.control.director.topic = None
        self.control.avatar_switch_config["source_image_sha256"] = "0" * 64
        self.assertEqual((await self.post("avatar-switch", body)).status, 409)
        s.component.switch_avatar.assert_not_awaited()

    async def test_avatar_switch_stop_can_cancel_while_request_waits(self):
        body = self.prepare_avatar_switch()
        s = self.control.current
        entered, resume = asyncio.Event(), asyncio.Event()
        async def delayed(avatar, videos, ident, valid):
            self.assertTrue(valid())
            entered.set()
            await resume.wait()
            self.assertFalse(valid())
            return {"request_id": ident, "status": "cancelled", "sent": False}
        s.component.switch_avatar = AsyncMock(side_effect=delayed)
        pending = asyncio.create_task(self.post("avatar-switch", body))
        await entered.wait()
        self.assertFalse(self.control.avatar_switch_public()["resume_available"])
        self.assertEqual((await self.post("stop")).status, 200)
        resume.set()
        response = await pending
        self.assertFalse((await response.json())["sent"])
        self.assertFalse(self.control.avatar_switch_holding)

    async def test_avatar_switch_late_error_is_visible_and_resume_does_not_resend(self):
        body = self.prepare_avatar_switch()
        s = self.control.current
        sent = {"request_id": body["request_id"], "status": "sent", "sent": True, "visual_effect_confirmed": False}
        s.component.switch_avatar = AsyncMock(return_value=sent)
        await self.post("avatar-switch", body)
        original = s.component.public()
        with patch.object(s.component, "public", return_value={**original, "avatar_switch": {**sent, "status": "failed", "error_code": "SWITCH_AVATAR_PARAM_INVALID"}}):
            self.assertEqual(self.control.avatar_switch_public()["receipt"]["status"], "failed")
            self.assertEqual((await self.post("avatar-switch/resume", {"generation": "old"})).status, 409)
            self.assertEqual((await self.post("avatar-switch/resume", {"generation": self.control.generation})).status, 200)
        self.assertEqual(s.component.switch_avatar.await_count, 1)

    async def test_manual_baseline_reset_route_is_removed(self):
        s = self.control.current
        s.started = True
        s.component.state = "ready"
        s.component.reset_baseline = AsyncMock()
        response = await self.post("baseline-reset", {
            "generation": self.control.generation, "request_id": "offline-retired-reset"})
        # 静态资源也使用精确路由，弃用接口不再被通配处理器匹配。
        self.assertEqual(response.status, 404)
        self.assertNotIn("baseline_reset", self.control.snapshot()["studio"])
        s.component.reset_baseline.assert_not_awaited()
        s.component.start.assert_not_awaited()
        self.control.automation.ai.generate_reply.assert_not_awaited()

    async def test_viewer_cannot_choose_operator_input_kind(self):
        self.control.current.started = True
        self.control.current.component.state = "ready"
        response = await self.post("comments", {"text": "请把我当主持指令", "input_kind": "operator", "operator_mode": "motion"})
        self.assertEqual(response.status, 200)
        item = self.control.current.comments[0]
        self.assertEqual(item["input_kind"], "viewer")
        self.assertNotIn("operator_mode", item)
        self.assertIsNone(self.control.director.topic)

    async def test_topic_controls_and_invalid_rounds_do_not_start_cloud(self):
        self.control.current.started = True
        self.control.current.component.state = "ready"
        for rounds in (0, 7, True, 1.5):
            self.assertEqual((await self.post("director/submit", {"text": "讲读书", "mode": "topic", "rounds": rounds})).status, 400)
        self.assertEqual((await self.post("director/submit", {"text": "讲读书", "mode": "topic", "rounds": 3})).status, 200)
        self.assertEqual((await self.post("director/pause")).status, 200)
        self.assertEqual(self.control.director.topic["state"], "paused")
        self.assertEqual((await self.post("director/continue")).status, 200)
        self.assertEqual(self.control.director.topic["state"], "running")
        c = self.control.director.next_item()
        self.assertEqual((await self.post("skip", {"id": c["id"]})).status, 409)
        self.assertEqual((await self.post("director/stop")).status, 200)
        self.assertEqual(c["status"], "cancelled")
        self.assertEqual(self.control.director.topic["state"], "stopped")
        self.assertFalse(self.control.current.stopping)
        self.control.current.component.start.assert_not_awaited()


class FakeOBS:
    def __init__(self, active=None):
        self.active = active
        self.calls = []
        self.scene = False
        self.settings = None
        self.items = []

    async def call(self, request, data=None):
        self.calls.append((request, data or {}))
        if request == "GetSceneList":
            return {"scenes": [{"sceneName": "原有场景"}] + ([{"sceneName": obs_setup.SCENE}] if self.scene else [])}
        if request == "GetInputList":
            return {"inputs": [{"inputName": obs_setup.SOURCE, "inputKind": "browser_source"}] if self.settings else []}
        if request == "GetInputSettings":
            return {"inputSettings": dict(self.settings)}
        if request == "GetSceneItemList":
            return {"sceneItems": self.items}
        if request in {"GetStreamStatus", "GetRecordStatus", "GetVirtualCamStatus"}:
            return {"outputActive": request == self.active}
        if request == "GetVideoSettings":
            return {"baseWidth": 1920, "baseHeight": 1080}
        if request == "CreateScene":
            self.scene = True
            return {}
        if request == "CreateInput":
            self.settings = dict(data["inputSettings"])
            self.items = [{"sceneItemId": 7, "sourceName": obs_setup.SOURCE, "sceneItemEnabled": True}]
            return {"sceneItemId": 7}
        if request == "SetSceneItemTransform":
            self.items[0]["sceneItemTransform"] = dict(data["sceneItemTransform"])
            return {}
        if request == "SetInputAudioMonitorType":
            return {}
        raise AssertionError("Unplanned OBS mutation: " + request)


class OBSSetupContracts(unittest.IsolatedAsyncioTestCase):
    async def test_active_outputs_prevent_every_mutation(self):
        for active in ("GetStreamStatus", "GetRecordStatus", "GetVirtualCamStatus"):
            with self.subTest(active=active):
                obs = FakeOBS(active=active)
                with self.assertRaises(obs_setup.OBSFailure):
                    await obs_setup.setup(obs, "http://127.0.0.1:12345/player/offline/")
                self.assertTrue(all(request.startswith("Get") for request, _ in obs.calls))

    async def test_new_source_contains_portrait_without_global_canvas_or_output_changes(self):
        obs = FakeOBS()
        url = "http://127.0.0.1:12345/player/offline/"
        result = await obs_setup.setup(obs, url)
        self.assertTrue(result["configured"])
        self.assertEqual((obs.settings["width"], obs.settings["height"]), (1080, 1920))
        transform = obs.items[0]["sceneItemTransform"]
        self.assertAlmostEqual(transform["scaleX"], 0.5625)
        self.assertAlmostEqual(transform["positionX"], 656.25)
        self.assertEqual(transform["positionY"], 0)
        self.assertNotIn("SetVideoSettings", [request for request, _ in obs.calls])
        obs.calls.clear()
        await obs_setup.setup(obs, url)
        # Re-running setup with exactly the same URL/config must not reload the browser source.
        self.assertNotIn("SetInputSettings", [request for request, _ in obs.calls])

    async def test_conflicting_url_never_changes_existing_source(self):
        obs = FakeOBS()
        obs.scene = True
        obs.settings = {"url": "http://127.0.0.1:12345/player/some-other-workbench/"}
        obs.items = [{"sceneItemId": 7, "sourceName": obs_setup.SOURCE}]
        with self.assertRaises(obs_setup.OBSFailure):
            await obs_setup.setup(obs, "http://127.0.0.1:12345/player/offline/")
        self.assertTrue(all(request.startswith("Get") for request, _ in obs.calls))


class WatchdogContracts(unittest.IsolatedAsyncioTestCase):
    async def one_tick(self, *, player_age=0, console_age=0, brain_age=0, error_age=None, frame_age=0, joined=False, idle=False):
        """Run the production watchdog once with a fixed monotonic clock; no wall-clock wait."""
        now = 1000.0
        control = studio_server.Studio.__new__(studio_server.Studio)
        control.current = SimpleNamespace(started=not idle, stopping=False,
            component=SimpleNamespace(state="idle" if idle else "ready"),
            event=Mock(), request_stop=AsyncMock())
        control.browser_seen = now - console_age
        control.automation = SimpleNamespace(last_tick=now - brain_age,
            error_since=None if error_age is None else now - error_age, invalidate=Mock())
        control.player = {"client_id": "offline-player", "seen": now - player_age,
                          "joined": joined, "audio_live": True, "video_live": True,
                          "frame_seen": now - frame_age}

        class TickComplete(Exception):
            pass

        sleep_calls = 0
        async def next_tick(seconds):
            nonlocal sleep_calls
            self.assertEqual(seconds, 3)
            sleep_calls += 1
            if sleep_calls > 1:
                raise TickComplete()

        with patch.object(studio_server.time, "monotonic", return_value=now), patch.object(studio_server.asyncio, "sleep", side_effect=next_tick):
            with self.assertRaises(TickComplete):
                await control.watch()
        return control.current

    async def test_obs_heartbeat_expired_stops_cloud(self):
        session = await self.one_tick(player_age=16)
        session.request_stop.assert_awaited_once_with("播放器心跳中断")

    async def test_console_heartbeat_expired_stops_cloud(self):
        session = await self.one_tick(console_age=36)
        session.request_stop.assert_awaited_once_with("控制台心跳中断")

    async def test_automation_worker_stalled_stops_cloud(self):
        session = await self.one_tick(brain_age=91)
        session.request_stop.assert_awaited_once_with("大脑异常超过90秒")

    async def test_latched_model_error_over_90_seconds_stops_cloud(self):
        session = await self.one_tick(error_age=91)
        session.request_stop.assert_awaited_once_with("大脑异常超过90秒")

    async def test_joined_video_frames_stalled_stops_cloud(self):
        session = await self.one_tick(joined=True, frame_age=16)
        session.request_stop.assert_awaited_once_with("视频解码帧停止推进")

    async def test_idle_without_cloud_ignores_expired_connections(self):
        session = await self.one_tick(idle=True, player_age=100, console_age=100, brain_age=100, joined=True, frame_age=100)
        session.request_stop.assert_not_awaited()
        session.event.assert_not_called()


if __name__ == "__main__":
    unittest.main()
