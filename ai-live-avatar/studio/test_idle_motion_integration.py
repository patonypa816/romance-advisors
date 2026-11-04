"""Loopback HTTP contracts for idle ownership; fake catalogue GETs, no cloud."""
import asyncio
import copy
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import test_studio as fixtures
from idle_action_library import IdleActionLibrary
from idle_motion import IdleRecoveryError
from test_idle_action_library import config, DESCRIPTION


class IdleMotionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    post = fixtures.StudioContracts.post

    async def asyncSetUp(self):
        await fixtures.StudioContracts.asyncSetUp(self)
        self.config_path = self.data / "idle-action-library.json"
        self.config_path.write_text(json.dumps(config()))
        self.catalog_client = type("OfflineCatalogue", (), {})()
        self.catalog_client.get_library = AsyncMock(return_value={"id": "lib-1", "avatar_id": "avatar-1"})
        self.catalog_client.list_actions = AsyncMock(return_value={"total": 1, "items": [
            {"id": "action-1", "name": "轻轻点头", "description": DESCRIPTION}]})
        self.catalog_client.close = AsyncMock()
        await self.control.idle_library.close()
        self.control.idle_library = IdleActionLibrary(self.data, client=self.catalog_client)
        self.tasks = []
        self.extra_patches = [
            patch("idle_action_library.credentials.load_vidu", side_effect=AssertionError("unexpected idle credentials")),
            patch("live_session.synthesize_segment", side_effect=AssertionError("unexpected idle TTS")),
        ]
        for item in self.extra_patches:
            item.start()

    async def asyncTearDown(self):
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for item in reversed(self.extra_patches):
            item.stop()
        await fixtures.StudioContracts.asyncTearDown(self)

    def ready(self):
        s = self.control.current
        s.started, s.avatar_id, s.component.state = True, "avatar-1", "ready"
        return s

    def body(self, **values):
        return {"generation": self.control.generation, **values}

    async def enable(self):
        return await self.post("idle-motions", self.body(enabled=True))

    def configure_body(self, **values):
        return self.body(min_interval_seconds=15, max_interval_seconds=25,
                         enabled_ids=["action-1"], **values)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def hold_sync(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def held_get(library_id):
            entered.set()
            await release.wait()
            return {"id": "lib-1", "avatar_id": "avatar-1"}
        self.catalog_client.get_library.side_effect = held_get
        return entered, release

    async def test_default_off_requires_ready_and_enable_only_reads_catalogue(self):
        self.assertFalse(self.control.idle_motions_public()["scheduler"]["enabled"])
        self.catalog_client.get_library.assert_not_awaited()
        self.assertEqual((await self.enable()).status, 409)
        s = self.ready()
        response = await self.enable()
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["scheduler"]["enabled"])
        self.catalog_client.get_library.assert_awaited_once_with("lib-1")
        self.catalog_client.list_actions.assert_awaited_once_with("lib-1", 0, 50)
        s.component.start_idle_motions.assert_called_once()
        s.component.start.assert_not_awaited()
        self.control.automation.ai.select_comments.assert_not_awaited()
        self.control.automation.ai.generate_reply.assert_not_awaited()
        self.assertEqual(s.comments, [])
        self.assertEqual(s.totals["cost_cny"], 0)
        self.assertTrue(s.component._idle_motion_allowed())
        self.assertFalse(s.component._idle_motion_interrupt())
        self.assertTrue(s.component._idle_motion_valid_session())

    async def test_each_enable_and_single_test_rechecks_remote_then_uses_snapshot(self):
        s = self.ready()
        self.assertEqual((await self.enable()).status, 200)
        self.assertEqual((await self.post("idle-motions", self.body(enabled=False))).status, 200)
        self.assertEqual((await self.enable()).status, 200)
        self.assertEqual(self.catalog_client.get_library.await_count, 2)
        await self.post("idle-motions", self.body(enabled=False))
        self.assertEqual((await self.post("idle-motion-test", self.body(action_id="action-1"))).status, 200)
        self.assertEqual(self.catalog_client.get_library.await_count, 3)
        s.component.test_idle_motion.assert_called_once()
        action = s.component.test_idle_motion.call_args.args[0]
        self.assertEqual(action["description_sha256"], config()["actions"][0]["description_sha256"])
        self.assertNotIn("visual_verified", action)
        s.component.start.assert_not_awaited()

    async def test_schema_generation_and_origin_are_checked_for_all_routes(self):
        s = self.ready()
        routes = {"idle-motions": self.body(enabled=True), "idle-motions-sync": self.body(),
            "idle-motions-config": self.configure_body(), "idle-motion-test": self.body(action_id="action-1"),
            "idle-motions-recover": self.body()}
        for route, valid in routes.items():
            with self.subTest(route=route):
                response = await self.post(route, {**valid, "unknown": "forbidden"})
                self.assertEqual(response.status, 400)
                self.assertEqual((await self.post(route, {**valid, "generation": "old"})).status, 409)
                response = await self.post(route, valid, headers={"Origin": "http://untrusted.invalid"})
                self.assertEqual(response.status, 403)
        for value in (1, 0, "true", None):
            self.assertEqual((await self.post("idle-motions", self.body(enabled=value))).status, 400)
        self.assertEqual((await self.post("idle-motion-test", self.body(action_id=1))).status, 400)
        self.catalog_client.get_library.assert_not_awaited()
        s.component.start_idle_motions.assert_not_called()

    async def test_remote_changed_description_is_rejected_without_overwriting_reviewed_file(self):
        s = self.ready()
        before = self.config_path.read_bytes()
        self.catalog_client.list_actions.return_value["items"][0]["description"] = "changed remote description"
        self.assertEqual((await self.enable()).status, 409)
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(self.control.idle_library.sync_status, "review_required")
        s.component.start_idle_motions.assert_not_called()
        self.assertEqual((await self.post("idle-motion-test", self.body(action_id="action-1"))).status, 409)
        s.component.test_idle_motion.assert_not_called()

    async def test_cross_avatar_rejected_before_any_catalogue_get(self):
        s = self.ready()
        s.avatar_id = "another-avatar"
        for route, body in (("idle-motions", self.body(enabled=True)),
                ("idle-motion-test", self.body(action_id="action-1")),
                ("idle-motions-sync", self.body()), ("idle-motions-config", self.configure_body())):
            self.assertEqual((await self.post(route, body)).status, 409, route)
        self.catalog_client.get_library.assert_not_awaited()
        s.component.start_idle_motions.assert_not_called()

    async def test_disable_is_not_blocked_by_pending_network_and_cancels_late_enable(self):
        s = self.ready()
        entered, release = await self.hold_sync()
        pending = self.spawn(self.enable())
        await asyncio.wait_for(entered.wait(), 1)
        self.assertFalse(self.control.lock.locked())
        response = await asyncio.wait_for(self.post("idle-motions", self.body(enabled=False)), 1)
        self.assertEqual(response.status, 200)
        release.set()
        self.assertEqual((await pending).status, 409)
        s.component.start_idle_motions.assert_not_called()
        s.component.stop_idle_motions.assert_awaited_once_with(recover=True)

    async def test_avatar_or_session_change_during_sync_prevents_late_enable(self):
        for change in ("avatar", "session"):
            s = self.ready()
            entered, release = await self.hold_sync()
            pending = self.spawn(self.enable())
            await asyncio.wait_for(entered.wait(), 1)
            if change == "avatar":
                s.avatar_id = "other-avatar"
            else:
                self.assertEqual((await self.post("stop")).status, 200)
            release.set()
            self.assertEqual((await pending).status, 409)
            s.component.start_idle_motions.assert_not_called()
            s.avatar_id = "avatar-1"

    async def test_disable_also_cancels_enable_queued_behind_another_metadata_request(self):
        s = self.ready()
        entered, release = await self.hold_sync()
        syncing = self.spawn(self.post("idle-motions-sync", self.body()))
        await asyncio.wait_for(entered.wait(), 1)
        enabling = self.spawn(self.enable())
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertFalse(enabling.done())
        self.assertEqual((await self.post("idle-motions", self.body(enabled=False))).status, 200)
        release.set()
        self.assertEqual((await syncing).status, 200)
        self.assertEqual((await enabling).status, 409)
        s.component.start_idle_motions.assert_not_called()
        self.catalog_client.get_library.assert_awaited_once()

    async def test_metadata_changes_are_serialized_while_network_does_not_hold_control_lock(self):
        self.ready()
        entered, release = await self.hold_sync()
        syncing = self.spawn(self.post("idle-motions-sync", self.body()))
        await asyncio.wait_for(entered.wait(), 1)
        changing = self.spawn(self.post("idle-motions-config", self.configure_body()))
        for _ in range(12):
            await asyncio.sleep(0)
        self.assertFalse(self.control.lock.locked())
        self.assertFalse(changing.done())
        release.set()
        self.assertEqual((await syncing).status, 200)
        self.assertEqual((await changing).status, 200)
        self.catalog_client.get_library.assert_awaited_once()

    async def test_running_or_unrecovered_module_rejects_sync_and_settings(self):
        s = self.ready()
        for state in ({"enabled": True}, {"task_running": True}, {"needs_recovery": True}):
            base = copy.deepcopy(s.component._idle_motion_state)
            s.component._idle_motion_state.update(state)
            for route, body in (("idle-motions-sync", self.body()),
                                 ("idle-motions-config", self.configure_body())):
                self.assertEqual((await self.post(route, body)).status, 409)
            s.component._idle_motion_state = base
        self.catalog_client.get_library.assert_not_awaited()

    async def test_fixed_replay_and_explicit_trial_modes_do_not_enable_idle(self):
        s = self.ready()
        self.control.replay_fixture = object()
        try:
            self.assertFalse(self.control.idle_motion_allowed(s))
            self.assertEqual((await self.enable()).status, 409)
        finally:
            self.control.replay_fixture = None
        self.control.semantic_speech = False
        try:
            self.assertFalse(self.control.idle_motion_allowed(s))
            self.assertEqual((await self.enable()).status, 409)
        finally:
            self.control.semantic_speech = True
        self.catalog_client.get_library.assert_not_awaited()

    async def test_configuration_validates_and_persists_only_interval_and_enabled_ids(self):
        s = self.ready()
        body = self.body(min_interval_seconds=20, max_interval_seconds=30, enabled_ids=[])
        self.assertEqual((await self.post("idle-motions-config", body)).status, 200)
        saved = json.loads(self.config_path.read_text())
        self.assertEqual(saved["settings"], {"min_interval_seconds": 20, "max_interval_seconds": 30})
        self.assertFalse(saved["actions"][0]["enabled"])
        for changes in ({"min_interval_seconds": 1}, {"max_interval_seconds": 10},
                        {"enabled_ids": ["unknown"]}, {"enabled_ids": ["action-1", "action-1"]}):
            before = self.config_path.read_bytes()
            self.assertEqual((await self.post("idle-motions-config", {**body, **changes})).status, 400)
            self.assertEqual(self.config_path.read_bytes(), before)
        self.catalog_client.get_library.assert_not_awaited()
        s.component.start_idle_motions.assert_not_called()

    async def test_pool_and_selection_only_block_new_i_without_interrupting_hold(self):
        s = self.ready()
        a = self.control.automation
        self.assertTrue(self.control.idle_motion_allowed(s))
        a.enabled = True
        a.pool.ingest({"id": "1", "roomId": "123", "platform": "douyin", "type": "chat",
            "ts": time.time()*1000, "receivedAt": time.time()*1000,
            "user": {"id": "offline-viewer", "nickname": "观众"}, "data": {"content": "问题"}})
        self.assertEqual(a.pool.snapshot()["candidate_count"], 1)
        self.assertFalse(self.control.idle_motion_allowed(s))
        self.assertFalse(self.control.idle_motion_interrupt(s))
        a.pool.clear("test")
        selection = asyncio.create_task(asyncio.Event().wait())
        self.tasks.append(selection)
        a.selection_task = selection
        try:
            self.assertFalse(self.control.idle_motion_allowed(s))
            self.assertFalse(self.control.idle_motion_interrupt(s))
        finally:
            a.selection_task = None

    async def test_selected_viewer_operator_running_topic_and_errors_interrupt(self):
        s = self.ready()
        for mode in ("viewer", "operator"):
            c = self.control.enqueue_comment("待处理内容", "观众", input_kind=mode)
            for status in ("pending", "processing", "prepared", "preparing", "ready", "synthesizing", "planning_actions", "sending"):
                c["status"] = status
                self.assertFalse(self.control.idle_motion_allowed(s), (mode, status))
                self.assertTrue(self.control.idle_motion_interrupt(s), (mode, status))
            c["status"] = "sent"
        for state in ("running", "error"):
            self.control.director.topic = {"state": state}
            self.assertFalse(self.control.idle_motion_allowed(s))
            self.assertTrue(self.control.idle_motion_interrupt(s))
        self.control.director.topic = None
        self.control.automation.last_error = "offline-error"
        self.assertFalse(self.control.idle_motion_allowed(s))
        self.assertTrue(self.control.idle_motion_interrupt(s))

    async def test_paused_prepared_topic_is_ignored_but_delivery_or_queue_still_interrupts(self):
        s = self.ready()
        item = self.control.enqueue_comment("未来主讲段", "主持", input_kind="operator", operator_mode="topic")
        item.update(status="prepared", delivery_started=False)
        self.control.director.topic = {"state": "paused"}
        self.assertTrue(self.control.idle_motion_allowed(s))
        self.assertFalse(self.control.idle_motion_interrupt(s))
        item["delivery_started"] = True
        self.assertFalse(self.control.idle_motion_allowed(s))
        self.assertTrue(self.control.idle_motion_interrupt(s))
        item["delivery_started"] = False
        s.work.put_nowait(object())
        self.assertFalse(self.control.idle_motion_allowed(s))
        self.assertTrue(self.control.idle_motion_interrupt(s))
        s.work.get_nowait(); s.work.task_done()
        self.control.director.topic = None

    async def test_single_test_refuses_busy_and_unknown_action(self):
        s = self.ready()
        comment = self.control.enqueue_comment("观众问题", "观众")
        self.assertEqual((await self.post("idle-motion-test", self.body(action_id="action-1"))).status, 409)
        comment["status"] = "sent"
        s.component.test_idle_motion.reset_mock()
        self.assertEqual((await self.post("idle-motion-test", self.body(action_id="unknown"))).status, 409)
        s.component.test_idle_motion.assert_not_called()
        self.assertFalse(s.component.idle_motions_public()["enabled"])

    async def test_q_failure_remains_visible_until_explicit_recovery(self):
        s = self.ready()
        s.component._idle_motion_state.update(needs_recovery=True, recovery_failed=True,
            status="error", last_error="idle_motion_recovery_failed")
        self.assertFalse(self.control.idle_motion_allowed(s))
        self.assertEqual((await self.enable()).status, 409)
        response = await self.post("idle-motions-recover", self.body())
        self.assertEqual(response.status, 200)
        s.component.recover_idle_motions.assert_awaited_once_with()
        self.assertFalse((await response.json())["scheduler"]["needs_recovery"])
        self.assertEqual((await self.post("idle-motions-recover", self.body())).status, 409)

    async def test_disable_recovery_failure_returns_conflict_and_retains_recovery_obligation(self):
        s = self.ready()
        s.component._idle_motion_state.update(needs_recovery=True, recovery_failed=True,
            status="error", last_error="idle_motion_recovery_failed")
        s.component.stop_idle_motions.side_effect = IdleRecoveryError("offline Q unconfirmed")
        self.assertEqual((await self.post("idle-motions", self.body(enabled=False))).status, 409)
        self.assertTrue(self.control.idle_motions_public()["scheduler"]["needs_recovery"])
        s.component.start.assert_not_awaited()

    async def test_recovery_rejects_physical_sending_and_observation_ownership(self):
        s = self.ready()
        # Keep the real queue populated across the HTTP await. The regular
        # worker would consume a sentinel before the route can inspect it.
        if s.worker:
            s.worker.cancel()
            await asyncio.gather(s.worker, return_exceptions=True)
            s.worker = None
        s.component._idle_motion_state.update(needs_recovery=True, recovery_failed=True,
            status="error", last_error="idle_motion_recovery_failed")
        for busy in ("sending", "draining", "comment_id", "work", "avatar_hold"):
            with self.subTest(busy=busy):
                if busy in {"sending", "draining"}:
                    s.component.state = busy
                elif busy == "comment_id":
                    s.component.comment_id = "current-content-owner"
                elif busy == "work":
                    s.work.put_nowait(object())
                elif busy == "avatar_hold":
                    self.control.avatar_switch_holding = True
                self.assertFalse(self.control.idle_motions_public()["recover_available"])
                self.assertEqual((await self.post("idle-motions-recover", self.body())).status, 409)
                s.component.recover_idle_motions.assert_not_awaited()
                self.assertTrue(s.component.idle_motions_public()["needs_recovery"])
                s.component.state = "ready"
                s.component.comment_id = None
                if not s.work.empty():
                    s.work.get_nowait(); s.work.task_done()
                self.control.avatar_switch_holding = False
        self.assertTrue(self.control.idle_motions_public()["recover_available"])
        self.assertEqual((await self.post("idle-motions-recover", self.body())).status, 200)
        s.component.recover_idle_motions.assert_awaited_once()

    async def test_removed_d_route_is_404_and_state_has_no_legacy_idle_d(self):
        s = self.ready()
        self.assertEqual((await self.post("idle-d", self.body(enabled=True))).status, 404)
        self.assertNotIn("idle_d", s.component.public())
        self.assertNotIn("idle_d", self.control.snapshot()["studio"])
        self.assertFalse(hasattr(s.component, "start_idle_d"))
        self.assertFalse(hasattr(s.component, "stop_idle_d"))
        self.assertEqual((await self.enable()).status, 200)
        self.assertEqual((await self.post("idle-d", self.body(enabled=False))).status, 404)
        self.assertNotIn("idle_d", self.control.snapshot()["studio"])

    async def test_stop_and_new_session_do_not_keep_enabled_intent_or_old_callbacks(self):
        old = self.ready()
        self.assertEqual((await self.enable()).status, 200)
        valid = old.component._idle_motion_valid_session
        allowed = old.component._idle_motion_allowed
        self.assertTrue(valid())
        self.assertEqual((await self.post("stop")).status, 200)
        self.assertFalse(valid())
        self.assertFalse(allowed())
        await asyncio.sleep(0)
        self.assertEqual((await self.post("new")).status, 200)
        self.assertFalse(self.control.current.component.idle_motions_public()["enabled"])
        self.assertFalse(valid())
        self.control.current.component.start.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
