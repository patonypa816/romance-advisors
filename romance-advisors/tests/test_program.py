import asyncio
import contextlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from romance.avatar_client import AvatarClient, AvatarClientError, check_startup_blocker
from romance.game import REACTION_TTS_STYLE
from romance.server import Workbench, create_app
from test_server import SyntheticModel


class FakeAvatar:
    def __init__(self):
        self.origin = "http://127.0.0.1:4444"
        self.player_url = self.origin + "/player/PRIVATE/?preview=1"
        self.generation = None
        self.owner_id = None
        self.state = "idle"
        self.calls = []
        self.sent = []
        self.can_start = True
        self.blocking_code = ""
        self.blocking_reason = ""
        self.closed = False

    async def prepare_session(self, owner_id):
        self.owner_id, self.generation = owner_id, "generation-1"
        self.calls.append("claim")

    async def snapshot(self):
        return {"session": {"state": self.state}, "studio": {"can_start": self.can_start,
                "blocking_code": self.blocking_code, "blocking_reason": self.blocking_reason}}

    async def start(self):
        self.calls.append("start")
        self.state = "ready"

    async def heartbeat(self):
        self.calls.append("heartbeat")

    async def external_state(self):
        return {"owner_id": self.owner_id, "generation": self.generation, "state": self.state}

    async def prepare(self, record, text):
        self.prepared = (record, text)
        return {"job_id": record["job_id"], "generation": self.generation, "status": "prepared"}

    async def dispatch(self, record):
        return await self.submit(*self.prepared)

    async def submit(self, record, text):
        self.sent.append((dict(record), text))
        # No cloud playback or handoff receipt is needed to advance the game.
        return {"job_id": record["job_id"], "generation": self.generation,
                "status": "sent", "send_complete": True, "playback_confirmed": False,
                "short_action_sent": record.get("mode") == "motion"}

    async def stop(self):
        self.calls.append("stop")
        self.state = "ended"

    async def close(self):
        self.closed = True


class ProgramTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.avatar = FakeAvatar()
        self.model = SyntheticModel()
        self.wb = Workbench(":memory:", model=self.model, avatar_client=self.avatar)

    async def asyncTearDown(self):
        await self.wb.close()

    async def start(self):
        self.wb.program.start()
        await self.wb.program.task
        if self.wb.job:
            await self.wb.job

    async def expire_phase(self):
        self.wb.state()
        self.wb.deadline = time.time() - 1
        await self.wb.program.step()
        if self.wb.job:
            await self.wb.job

    async def test_prepare_freezes_independent_message_motion_plan(self):
        await self.start()
        first = self.wb.program.prepare("first-round", "我想认真回答。")
        second = self.wb.program.prepare("second-round", "我想认真回答。")
        plan = second["message_motion_plan"]
        self.assertEqual(set(plan), {"emotion", "actions", "recovery"})
        self.assertEqual(len(plan["actions"]), 1)
        action = plan["actions"][0]
        self.assertEqual((action["kind"], action["delay_seconds"], action["hold_seconds"]), ("S", 0, 0))
        self.assertEqual([m["part"] for m in action["motions"]], ["head", "right_arm"])
        self.assertIn("实体手机不出镜", action["motions"][0]["description"])
        self.assertEqual(set(plan["recovery"]), {"gaze", "expression"})
        self.assertEqual(plan["recovery"]["gaze"], "视线短暂停在下方")
        first["message_motion_plan"]["actions"].clear()
        self.assertEqual(len(second["message_motion_plan"]["actions"]), 1)
        third = self.wb.program.prepare("third-round", "我想认真回答。")
        self.assertEqual(third["message_motion_plan"], second["message_motion_plan"])

    async def test_complete_round_and_automatic_next_episode_keep_run_budget(self):
        await self.start()
        program = self.wb.program
        started_at = program.started_at
        first_episode = self.wb.game.episode["id"]
        self.wb.game.submit_proposal("a", "军师", "认真回答")
        await self.expire_phase()
        self.wb.game.vote("v", "观众", "1")
        await self.expire_phase()
        state = self.wb.state()
        self.assertEqual(state["episode"]["status"], "SUCCEEDED")
        self.assertEqual(len(self.avatar.sent), 2)
        self.assertEqual(self.avatar.sent[0][1], "我想认真回答。")
        instruction = REACTION_TTS_STYLE + "稍有忐忑，语气认真，句尾自然收住。"
        self.assertEqual(self.avatar.sent[0][0]["instruction"], instruction)
        self.assertEqual(self.wb.game.current_round["reaction_instruction"], instruction)
        self.assertEqual(self.wb.game.storage.load()["rounds"][-1]["reaction_instruction"], instruction)
        self.assertTrue(state["round"]["performance"]["man_released"])
        self.assertFalse(state["round"]["performance"]["playback_confirmed"])
        await self.expire_phase()
        self.assertNotEqual(self.wb.game.episode["id"], first_episode)
        self.assertEqual(self.wb.game.episode["score"], 50)
        self.assertEqual(self.wb.state()["leaderboards"]["best"][0]["adopted"], 1)
        self.assertEqual(program.started_at, started_at)
        self.assertEqual(program.state, "running")

    async def test_only_operator_receives_private_player_url(self):
        await self.start()
        self.assertIn("PRIVATE", self.wb.state(operator=True)["program"]["player_url"])
        self.assertNotIn("PRIVATE", json.dumps(self.wb.state()))
        self.assertNotIn("player_url", self.wb.state()["program"])

    async def test_page_heartbeat_timeout_stops_cloud_even_with_running_player(self):
        await self.start()
        self.wb.program.page_seen = time.monotonic() - 21
        await self.wb.program.step()
        self.assertEqual(self.wb.program.state, "error")
        self.assertIn("stop", self.avatar.calls)
        self.assertEqual(self.avatar.state, "ended")
        self.assertFalse(self.wb.settings["auto_advance"])

    async def test_budget_is_cumulative_and_stops_before_next_paid_generation(self):
        await self.start()
        count = len(self.model.calls)
        self.wb.program.started_at = time.monotonic() - 1201
        await self.wb.program.step()
        self.assertEqual(len(self.model.calls), count)
        self.assertEqual(self.wb.program.state, "stopped")
        self.assertEqual(self.avatar.state, "ended")
        self.assertEqual(self.wb.game.episode["score"], 50)

    async def test_stale_heartbeat_and_overlapping_start_are_rejected(self):
        await self.start()
        with self.assertRaises(ValueError):
            self.wb.program.heartbeat("previous-run")
        with self.assertRaises(ValueError):
            self.wb.program.start()
        self.wb.program.heartbeat(self.wb.program.run_id)

    async def test_unsupported_reaction_is_not_committed(self):
        original = self.model.generate

        async def generate(task, payload):
            result = await original(task, payload)
            if task == "draft":
                result["reaction"] = "[微笑]好的"
            return result

        self.model.generate = generate
        await self.start()
        await self.expire_phase()  # No suggestions: draft directly.
        self.assertEqual(self.wb.game.current_round["phase"], "DRAFTING")
        self.assertEqual(self.wb.game.data["error"]["code"], "invalid_reaction")
        self.assertEqual(len(self.wb.game.data["messages"]), 1)
        self.assertEqual(self.avatar.sent, [])

    async def test_existing_text_episode_is_not_replayed(self):
        await self.wb.game.new_episode()
        await self.wb.game.close_collection()
        self.assertEqual(self.avatar.sent, [])
        previous = self.wb.game.episode["id"]
        await self.start()
        self.assertNotEqual(self.wb.game.episode["id"], previous)
        self.assertEqual(self.avatar.sent, [])

    async def test_invalid_instruction_repairs_before_committing_or_speaking(self):
        original = self.model.generate
        attempts = 0

        async def generate(task, payload):
            nonlocal attempts
            result = await original(task, payload)
            if task == "draft":
                attempts += 1
                if attempts == 1:
                    result.pop("reaction_instruction")
            return result

        self.model.generate = generate
        await self.start()
        await self.expire_phase()
        r = self.wb.game.current_round
        self.assertEqual(self.wb.game.data["error"]["code"], "invalid_reaction_instruction")
        self.assertTrue(r["selection_locked"])
        self.assertIsNone(r["reaction"])
        self.assertEqual(len(self.wb.game.data["messages"]), 1)
        self.assertEqual(self.avatar.sent, [])
        await self.wb.program.step()
        await self.wb.job
        self.assertEqual(attempts, 2)
        self.assertEqual(r["phase"], "DONE")
        self.assertEqual(len(self.avatar.sent), 2)
        draft_payload = next(p for task, p in reversed(self.model.calls) if task == "draft")
        self.assertEqual(draft_payload["validation_error"], "invalid_reaction_instruction")
        self.assertTrue(draft_payload["selection_locked"])

    async def test_formal_ssml_stays_in_frozen_plan_through_prepare_and_send(self):
        ssml = '<speak rate="0.95">我想<break time="160ms"/>认真回答。</speak>'
        generate = self.model.generate

        async def with_ssml(task, payload):
            result = await generate(task, payload)
            return dict(result, ssml=ssml) if task == "draft" else result

        self.model.generate = with_ssml
        await self.start()
        await self.expire_phase()
        self.assertEqual(len(self.avatar.sent), 2)
        record, text = self.avatar.sent[0]
        self.assertEqual(text, "我想认真回答。")
        self.assertEqual(record["plan"]["segments"][0]["ssml"], ssml)
        self.assertEqual(record["plan"], self.wb.game.current_round["reaction_plan"])
        self.assertEqual(self.avatar.prepared[0]["plan"], record["plan"])
        self.assertNotIn("ssml", record)
        self.assertEqual([task for task, _ in self.model.calls].count("draft"), 1)

    async def test_old_waiting_round_without_instruction_keeps_legacy_delivery(self):
        await self.start()
        game = self.wb.game
        # An old saved pending round must not gain a new mood on resumption.
        game.performer = None
        game.current_round["vote_snapshot"] = {}
        game._decide()
        await game._draft(game._token)
        r = game.current_round
        r.pop("reaction_instruction")
        r.pop("reaction_plan")
        self.assertIsNone(r.get("performance"))
        # Freeze an actual pre-migration pending record. Preparing it today
        # would intentionally opt into the new message gesture.
        r["performance"] = self.wb.program.prepare(r["id"], r["reaction"])
        r["performance"].pop("message_motion_plan")
        game.performer = self.wb.program
        await game._perform_reaction(game._token)
        self.assertEqual(len(self.avatar.sent), 1)
        self.assertNotIn("instruction", self.avatar.sent[0][0])
        self.assertTrue(r["performance"]["man_released"])

    async def test_stop_during_player_preparation_never_starts_cloud(self):
        self.avatar.can_start = False
        self.wb.program.start()
        await asyncio.sleep(0)
        await self.wb.program.stop()
        self.assertEqual(self.wb.program.state, "stopped")
        self.assertNotIn("start", self.avatar.calls)
        self.assertEqual(self.model.calls, [])

    async def test_previous_session_blocker_fails_without_waiting_or_cloud_start(self):
        self.avatar.can_start = False
        self.avatar.blocking_code = "previous_session_unconfirmed"
        self.avatar.blocking_reason = "PRIVATE https://provider.invalid/?token=SECRET"
        with patch("romance.program.asyncio.sleep", AsyncMock()) as sleep:
            await self.start()
        program = self.wb.program
        self.assertEqual(program.state, "error")
        self.assertIn("上一场云端结束尚未确认", program.error)
        self.assertEqual(program.last_failure["code"], "previous_session_unconfirmed")
        sleep.assert_not_awaited()
        self.assertNotIn("start", self.avatar.calls)
        self.assertEqual(self.model.calls, [])
        self.assertNotIn("PRIVATE", json.dumps(program.public(operator=True)))
        self.assertNotIn("SECRET", json.dumps(program.public(operator=True)))

    async def test_player_connection_waits_then_starts_once(self):
        self.avatar.can_start = False
        self.avatar.blocking_code = "player_not_connected"
        observed = []

        async def player_connects(_delay):
            observed.append(self.wb.program.public()["startup_stage"])
            self.avatar.can_start = True
            self.avatar.blocking_code = ""

        with patch("romance.program.asyncio.sleep", player_connects):
            await self.start()
        self.assertEqual(observed, ["waiting_player"])
        self.assertEqual(self.avatar.calls.count("start"), 1)
        self.assertEqual(self.wb.program.state, "running")

    async def test_player_wait_is_bounded_and_keeps_specific_error(self):
        self.avatar.can_start = False
        self.avatar.blocking_code = "player_not_connected"
        now = time.monotonic()

        # Advance only the program's clock after its first actual wait.
        clock = [now]

        async def advance(_delay):
            clock[0] += 91

        with patch("romance.program.time") as program_time, \
                patch("romance.program.asyncio.sleep", advance):
            program_time.monotonic.side_effect = lambda: clock[0]
            await self.start()
        self.assertEqual(self.wb.program.last_failure["code"], "player_timeout")
        self.assertIn("播放器连接等待超时", self.wb.program.error)
        self.assertNotIn("start", self.avatar.calls)

    async def test_startup_reports_actual_creation_connection_and_stage(self):
        stages = iter(["idle", "creating", "connecting", "waiting_stage", "ready"])
        observed = []

        async def snapshot():
            state = next(stages)
            return {"session": {"state": state}, "studio": {"can_start": state == "idle"}}

        async def record_stage(_delay):
            observed.append((self.wb.program.public()["startup_stage"], self.wb.program.avatar_state))

        with patch.object(self.avatar, "snapshot", snapshot), \
                patch("romance.program.asyncio.sleep", record_stage):
            await self.start()
        self.assertEqual(observed, [("creating", "creating"), ("connecting", "connecting"),
                                    ("waiting_stage", "waiting_stage")])
        self.assertEqual(self.wb.program.public()["startup_stage"], "ready")
        self.assertEqual(self.wb.program.state, "running")

    async def test_safe_client_failure_is_preserved_without_raw_exception_text(self):
        with patch.object(self.avatar, "prepare_session", AsyncMock(
                side_effect=AvatarClientError("model_not_configured"))):
            await self.start()
        self.assertIn("百炼大脑模型配置", self.wb.program.error)
        self.assertEqual(self.wb.program.last_failure["code"], "model_not_configured")
        self.assertNotIn("start", self.avatar.calls)

    async def test_preclaim_failure_does_not_claim_cloud_stop_confirmation(self):
        with patch.object(self.avatar, "prepare_session", AsyncMock(
                side_effect=AvatarClientError("previous_session_unconfirmed"))):
            await self.start()
        program = self.wb.program
        self.assertEqual(program.state, "error")
        self.assertIn("上一场云端结束尚未确认", program.error)
        self.assertNotIn("stop", self.avatar.calls)
        for cleanup in [program.cleanup, program.public(operator=True)["cleanup"]]:
            self.assertIsNone(cleanup["cloud_stop_confirmed"])
            self.assertEqual(cleanup["steps"]["cloud_stop"], "not_applicable")
            self.assertEqual(cleanup["errors"], [])

    async def test_stop_before_claim_needs_no_cloud_confirmation(self):
        program = self.wb.program
        entered = asyncio.Event()

        async def prepare(_owner_id):
            entered.set()
            await asyncio.Event().wait()

        with patch.object(self.avatar, "prepare_session", prepare):
            program.start()
            await asyncio.wait_for(entered.wait(), .5)
            await asyncio.wait_for(program.stop(), .5)
        self.assertEqual(program.state, "stopped")
        self.assertEqual(program.error, "")
        self.assertIsNone(program.cleanup["cloud_stop_confirmed"])
        self.assertEqual(program.cleanup["steps"]["cloud_stop"], "not_applicable")
        self.assertNotIn("stop", self.avatar.calls)

    async def test_stop_requested_before_ready_snapshot_returns_wins(self):
        program = self.wb.program
        read_entered, read_release, cleanup_entered, cleanup_release = (
            asyncio.Event() for _ in range(4))

        async def snapshot():
            read_entered.set()
            await read_release.wait()
            return {"session": {"state": "idle"}, "studio": {"can_start": True}}

        async def close_ambient():
            cleanup_entered.set()
            await cleanup_release.wait()

        with patch.object(self.avatar, "snapshot", snapshot), \
                patch.object(program.ambient, "close", close_ambient):
            program.start()
            await asyncio.wait_for(read_entered.wait(), .5)
            stop = asyncio.create_task(program.stop())
            await asyncio.wait_for(cleanup_entered.wait(), .5)
            read_release.set()
            await asyncio.wait_for(program.task, .5)
            self.assertNotIn("start", self.avatar.calls)
            cleanup_release.set()
            await asyncio.wait_for(stop, .5)
        self.assertEqual(program.state, "stopped")
        self.assertEqual(self.model.calls, [])

    async def test_avatar_error_is_an_immediate_failure(self):
        await self.start()
        program = self.wb.program
        record = program.prepare("test-round", "确定的台词")

        async def failed(record, text):
            return {"job_id": record["job_id"], "generation": self.avatar.generation,
                    "status": "error", "send_complete": False}

        self.avatar.submit = failed
        with self.assertRaises(ValueError):
            await asyncio.wait_for(program.perform(record, "确定的台词"), .1)

    async def test_first_invalid_prepare_is_corrected_in_same_episode(self):
        original = self.model.generate
        attempts = 0

        async def generate(task, payload):
            nonlocal attempts
            result = await original(task, payload)
            if task == "prepare":
                attempts += 1
                if attempts == 1:
                    result["woman"].pop("private_preferences")
            return result

        self.model.generate = generate
        await self.start()
        episode_id = self.wb.game.episode["id"]
        self.assertEqual(self.wb.game.data["error"]["code"], "invalid_private_preferences")
        await self.wb.program.step()
        await self.wb.job
        self.assertEqual(self.wb.program.state, "running")
        self.assertEqual(self.wb.game.episode["id"], episode_id)
        self.assertEqual(self.wb.game.current_round["phase"], "COLLECTING")
        self.assertEqual(attempts, 2)
        self.assertEqual(self.model.calls[-1][1]["validation_error"], "invalid_private_preferences")
        self.assertNotIn("stop", self.avatar.calls)

    async def test_second_invalid_prepare_stops_without_further_model_calls(self):
        original = self.model.generate

        async def generate(task, payload):
            result = await original(task, payload)
            if task == "prepare":
                result["woman"]["private_preferences"] = ""
            return result

        self.model.generate = generate
        await self.start()
        await self.wb.program.step()
        await self.wb.job
        self.assertEqual(sum(task == "prepare" for task, _ in self.model.calls), 2)
        await self.wb.program.step()
        await self.wb.program.step()
        self.assertEqual(self.wb.program.state, "error")
        self.assertEqual(sum(task == "prepare" for task, _ in self.model.calls), 2)
        self.assertEqual(self.avatar.calls.count("stop"), 1)
        self.assertEqual(self.avatar.sent, [])
        self.assertIsNone(self.wb.game.data["error"])
        self.assertEqual(self.wb.state(operator=True)["program"]["last_failure"], {
            "code": "invalid_private_preferences", "stage": "PREPARING"})
        self.assertNotIn("last_failure", self.wb.state()["program"])
        self.wb.program.start()
        self.assertIsNone(self.wb.state(operator=True)["program"]["last_failure"])
        await self.wb.program.task
        await self.wb.job

    async def test_sender_error_never_enters_content_correction_or_resends(self):
        async def failed(record, text):
            self.avatar.sent.append((dict(record), text))
            return {"job_id": record["job_id"], "generation": self.avatar.generation,
                    "status": "error", "send_complete": False}

        self.avatar.submit = failed
        await self.start()
        await self.expire_phase()
        self.assertEqual(self.wb.game.data["error"]["code"], "invalid_performance_send")
        await self.wb.program.step()
        await self.wb.program.step()
        self.assertEqual(self.wb.program.state, "error")
        self.assertEqual(len(self.avatar.sent), 1)
        self.assertEqual(self.wb.program.validation_repairs, {})
        self.assertFalse(any(task == "reply" for task, _ in self.model.calls))

    async def test_invalid_woman_result_repairs_without_replaying_frozen_reaction(self):
        original = self.model.generate
        attempts = 0

        async def generate(task, payload):
            nonlocal attempts
            result = await original(task, payload)
            if task == "reply":
                attempts += 1
                if attempts == 1:
                    result["delta"] = None
            return result

        self.model.generate = generate
        await self.start()
        await self.expire_phase()
        man_id = self.wb.game.data["messages"][-1]["id"]
        self.assertEqual(self.wb.game.data["error"]["code"], "invalid_delta")
        self.assertTrue(self.wb.game.current_round["performance"]["man_released"])
        frozen_instruction = self.wb.game.current_round["reaction_instruction"]
        await self.wb.program.step()
        await self.wb.job
        self.assertEqual(self.wb.program.state, "running")
        self.assertEqual(self.wb.game.current_round["phase"], "DONE")
        self.assertEqual(self.wb.game.data["messages"][1]["id"], man_id)
        self.assertEqual(len(self.avatar.sent), 2)
        self.assertEqual(self.avatar.sent[0][0]["instruction"], frozen_instruction)
        self.assertEqual(attempts, 2)
        self.assertEqual(self.model.calls[-1][1]["validation_error"], "invalid_delta")

    async def test_cleanup_failures_do_not_skip_cloud_stop_or_erase_initial_error(self):
        await self.start()
        with patch.object(self.wb.program.ambient, "close", AsyncMock(side_effect=OSError("PRIVATE ambient detail"))), \
                patch.object(self.wb, "abort", AsyncMock(side_effect=OSError("PRIVATE disk detail"))):
            await self.wb.program.stop(error="最初的准备失败。")
        program = self.wb.program
        self.assertEqual(self.avatar.calls.count("stop"), 1)
        self.assertEqual(self.avatar.state, "ended")
        self.assertEqual(program.error, "最初的准备失败。")
        self.assertTrue(program.cleanup["cloud_stop_confirmed"])
        self.assertEqual([row["stage"] for row in program.cleanup["errors"]], ["ambient", "game_abort"])
        self.assertEqual(program.state, "error")
        self.assertFalse(program.stopping)
        self.assertNotIn("PRIVATE", json.dumps(program.public(operator=True)))
        await program.close()
        self.assertTrue(self.avatar.closed)

    async def test_startup_and_cloud_stop_failures_have_separate_safe_causes(self):
        # Simulate a transport failure after the run has obtained its owner.
        self.avatar.owner_id, self.avatar.generation = "claimed-run", "generation-1"
        with patch.object(self.avatar, "prepare_session", AsyncMock(side_effect=OSError("PRIVATE credential URL"))), \
                patch.object(self.avatar, "stop", AsyncMock(side_effect=RuntimeError("PRIVATE cloud error"))):
            self.wb.program.start()
            await self.wb.program.task
        program = self.wb.program
        self.assertIn("准备未完成", program.error)
        self.assertEqual(program.last_failure, {"stage": "STARTING", "code": "avatar_prepare_failed", "error_type": "OSError"})
        self.assertFalse(program.cleanup["cloud_stop_confirmed"])
        self.assertEqual(program.cleanup["errors"][-1], {"stage": "cloud_stop", "error_type": "RuntimeError"})
        self.assertNotIn("PRIVATE", json.dumps(program.public(operator=True)))
        await program.close()
        self.assertTrue(self.avatar.closed)

    async def test_concurrent_stop_and_cancelled_caller_share_completed_cleanup(self):
        await self.start()
        entered, release = asyncio.Event(), asyncio.Event()
        actual_stop = self.avatar.stop

        async def slow_stop():
            entered.set()
            await release.wait()
            await actual_stop()

        with patch.object(self.avatar, "stop", slow_stop):
            first = asyncio.create_task(self.wb.program.stop())
            await asyncio.wait_for(entered.wait(), .5)
            second = asyncio.create_task(self.wb.program.stop())
            first.cancel()
            await asyncio.sleep(0)
            self.assertFalse(self.wb.program.ready)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(first, .5)
            await asyncio.wait_for(second, .5)
        self.assertEqual(self.avatar.calls.count("stop"), 1)
        self.assertEqual(self.wb.program.state, "stopped")
        self.assertTrue(self.wb.program.cleanup["cloud_stop_confirmed"])

    async def test_close_attempts_http_close_even_if_stop_unexpectedly_raises(self):
        await self.start()
        with patch.object(self.wb.program, "stop", AsyncMock(side_effect=OSError("PRIVATE stop"))):
            with self.assertRaises(OSError):
                await self.wb.program.close()
        self.assertTrue(self.avatar.closed)

    async def test_startup_failure_racing_manual_stop_cannot_join_its_own_cleanup(self):
        program = self.wb.program
        startup_entered, fail_startup, reported = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def prepare(_run_id):
            startup_entered.set()
            await fail_startup.wait()
            raise OSError("PRIVATE startup connection detail")

        original_stop = program.stop

        async def observed_stop(*, error=""):
            if error:
                reported.set()
            await original_stop(error=error)

        async def close_ambient():
            # Force the startup error to join a manual stop already in flight.
            fail_startup.set()
            await reported.wait()
            await asyncio.sleep(0)

        with patch.object(self.avatar, "prepare_session", prepare), \
                patch.object(program, "stop", observed_stop), \
                patch.object(program.ambient, "close", close_ambient):
            program.start()
            await asyncio.wait_for(startup_entered.wait(), .5)
            await asyncio.wait_for(program.stop(), .5)
            await asyncio.wait_for(program.task, .5)

        self.assertEqual(self.avatar.calls.count("stop"), 0)
        self.assertIsNone(program.cleanup["cloud_stop_confirmed"])
        self.assertEqual(program.cleanup["steps"]["cloud_stop"], "not_applicable")
        self.assertTrue(program._stop_task.done())
        self.assertFalse(program.stopping)
        self.assertEqual(program.state, "error")
        self.assertIn("准备未完成", program.error)
        self.assertEqual(program.last_failure["code"], "avatar_prepare_failed")
        self.assertNotIn("PRIVATE", json.dumps(program.public(operator=True)))

    async def test_sender_metrics_identify_round_and_never_record_dialogue(self):
        await self.start()
        await self.expire_phase()
        events = self.wb.game.metrics.snapshot()
        stages = [event["stage"] for event in events]
        self.assertLess(stages.index("sender_wait_start"), stages.index("sender_wait_end"))
        self.assertLess(stages.index("avatar_job_start"), stages.index("avatar_job_end"))
        end = next(event for event in events if event["stage"] == "avatar_job_end")
        self.assertEqual(end["run_id"], self.wb.program.run_id)
        self.assertEqual(end["round_id"], self.wb.game.current_round["id"])
        self.assertEqual(end["outcome"], "sent")
        self.assertGreaterEqual(end["elapsed_seconds"], 0)
        self.assertNotIn("认真", json.dumps(events, ensure_ascii=False))


class ProgramHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_exclusivity_heartbeat_and_exact_frame_origin(self):
        avatar = FakeAvatar()
        app = create_app(":memory:", model=SyntheticModel(), avatar_client=avatar)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/")
            self.assertIn("frame-src 'self' http://127.0.0.1:4444;", response.headers["Content-Security-Policy"])
            response = await client.post("/api/program/start", json={})
            self.assertEqual(response.status, 200)
            await app["workbench"].program.task
            await app["workbench"].job
            response = await client.get("/api/state")
            state = await response.json()
            run_id = state["program"]["run_id"]
            self.assertIn("PRIVATE", state["program"]["player_url"])
            response = await client.get("/api/public")
            self.assertNotIn("PRIVATE", await response.text())
            response = await client.post("/api/collection/close", json={})
            self.assertEqual(response.status, 409)
            response = await client.post("/api/program/heartbeat", json={"run_id": "old"})
            self.assertEqual(response.status, 409)
            response = await client.post("/api/program/heartbeat", json={"run_id": run_id})
            self.assertEqual(response.status, 200)
            response = await client.post("/api/program/stop", json={})
            self.assertEqual(response.status, 200)
            self.assertEqual(app["workbench"].program.state, "stopped")


class AvatarDiscoveryTests(unittest.TestCase):
    def test_legacy_and_unknown_blockers_are_safe_and_only_player_waits(self):
        for reason in ["请等待工作台预览连接", "请先把播放地址加入 OBS 浏览器源"]:
            check_startup_blocker({"studio": {"can_start": False, "blocking_reason": reason}})
        with self.assertRaises(AvatarClientError) as failure:
            check_startup_blocker({"studio": {"can_start": False,
                "blocking_reason": "上一场云端结束尚未确认，禁止创建下一场"}})
        self.assertEqual(failure.exception.code, "previous_session_unconfirmed")
        with self.assertRaises(AvatarClientError) as failure:
            check_startup_blocker({"studio": {"can_start": False,
                "blocking_code": "PRIVATE", "blocking_reason": "https://provider.invalid/?token=SECRET"}})
        self.assertEqual(failure.exception.code, "startup_blocked")
        self.assertNotIn("PRIVATE", str(failure.exception))
        self.assertNotIn("SECRET", str(failure.exception))

    def test_runtime_requires_matching_loopback_origins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            for control, player in [
                ("https://evil.invalid/studio/token/", "https://evil.invalid/player/token/"),
                ("http://127.0.0.1:1234/studio/token/", "http://127.0.0.1:4321/player/token/"),
                ("http://user@127.0.0.1:1234/studio/token/", "http://127.0.0.1:1234/player/token/"),
            ]:
                path.write_text(json.dumps({"url": control, "player_url": player}))
                with self.assertRaises(ValueError):
                    AvatarClient(path).discover()
            path.write_text(json.dumps({"url": "http://127.0.0.1:1234/studio/token/",
                                        "player_url": "http://127.0.0.1:1234/player/token/"}))
            client = AvatarClient(path)
            client.discover()
            self.assertEqual(client.origin, "http://127.0.0.1:1234")
            self.assertTrue(client.player_url.endswith("?preview=1"))


class AvatarHTTPClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_preflight_blocker_rejects_before_claim_or_start(self):
        client = AvatarClient()
        snapshot = {"session": {"state": "idle", "generation": "g-1"}, "studio": {
            "receiver_mode": "preview", "can_start": False,
            "blocking_code": "previous_session_unconfirmed", "blocking_reason": "PRIVATE"}}
        with patch.object(client, "discover"), \
                patch.object(client, "snapshot", AsyncMock(return_value=snapshot)), \
                patch.object(client, "request", AsyncMock()) as request:
            with self.assertRaises(AvatarClientError) as failure:
                await client.prepare_session("game-run")
        self.assertEqual(failure.exception.code, "previous_session_unconfirmed")
        request.assert_not_awaited()
        self.assertIsNone(client.owner_id)
        self.assertIsNone(client.generation)

    async def test_submit_sends_frozen_instruction_and_omits_for_legacy_records(self):
        calls = []
        app = web.Application()

        async def submit(request):
            calls.append(await request.json())
            return web.json_response({"status": "pending"})

        app.router.add_post("/external/jobs", submit)
        async with TestServer(app) as server:
            client = AvatarClient()
            client.control_url = str(server.make_url("/"))
            client.origin = client.control_url.rstrip("/")
            client.owner_id, client.generation = "game-run", "g-1"
            try:
                record = {"job_id": "round:reaction", "instruction": "嘴上逞强，带轻微自嘲。"}
                await client.submit(record, "哎，这招真行吗。")
                await client.submit({"job_id": "old:reaction"}, "旧台词。")
                motion = {"job_id": "round:thinking", "mode": "motion", "plan": {"actions": []},
                          "expires_at": 12345, "instruction": "这一字段不能混入无声动作"}
                await client.submit(motion, "这一台词也不能混入无声动作")
                self.assertEqual(calls[0], {**client.owner(), **record, "text": "哎，这招真行吗。"})
                self.assertNotIn("instruction", calls[1])
                self.assertEqual(calls[2], {**client.owner(), "job_id": motion["job_id"],
                                           "mode": "motion", "plan": motion["plan"], "expires_at": 12345})
            finally:
                await client.close()

    async def test_two_stage_endpoints_keep_speech_and_dispatch_identity_separate(self):
        client = AvatarClient()
        client.owner_id, client.generation = "owner", "generation"
        record = {"job_id": "round/1:reaction", "instruction": "轻声说。", "plan": {"segments": []},
                  "expires_at": 123, "finish_before": 456}
        with patch.object(client, "request", AsyncMock(return_value={})) as request:
            await client.prepare(record, "我想好了。")
            await client.dispatch(record)
        self.assertEqual(request.await_args_list[0].args, ("external/jobs/prepare",))
        self.assertEqual(request.await_args_list[0].kwargs["body"],
                         {**client.owner(), **record, "text": "我想好了。"})
        self.assertEqual(request.await_args_list[1].args,
                         ("external/jobs/round%2F1%3Areaction/dispatch",))
        self.assertEqual(request.await_args_list[1].kwargs["body"], client.owner())

    async def test_claim_uses_exact_origin_and_binds_private_player_owner(self):
        calls = []
        app = web.Application()

        async def state(request):
            return web.json_response({"session": {"state": "idle", "generation": "g-1"},
                                      "studio": {"receiver_mode": "preview"}})

        async def claim(request):
            calls.append((request.headers.get("Origin"), await request.json()))
            return web.json_response({"owner_id": "game-run", "generation": "g-1"})

        app.router.add_get("/studio/private/state", state)
        app.router.add_post("/studio/private/external/claim", claim)
        async with TestServer(app) as server:
            origin = str(server.make_url("")).rstrip("/")
            with tempfile.TemporaryDirectory() as directory:
                runtime = Path(directory) / "runtime.json"
                runtime.write_text(json.dumps({"url": origin + "/studio/private/",
                                               "player_url": origin + "/player/private/"}))
                client = AvatarClient(runtime)
                try:
                    await client.prepare_session("game-run")
                    self.assertEqual(calls, [(origin, {"owner_id": "game-run"})])
                    self.assertTrue(client.player_url.endswith("?preview=1&game_owner=game-run"))
                    self.assertEqual(client.owner(), {"owner_id": "game-run", "generation": "g-1"})
                finally:
                    await client.close()


if __name__ == "__main__":
    unittest.main()
