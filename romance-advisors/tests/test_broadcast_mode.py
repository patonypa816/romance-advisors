"""Synthetic mode changes exercise the existing single Studio sender."""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

from romance.server import Workbench, create_app
from test_program import FakeAvatar
from test_server import SyntheticModel


def quiet_casual(program):
    program.casual = Mock(error="")
    program.casual.close = AsyncMock()


class HeldAvatar(FakeAvatar):
    def __init__(self, *, hold_prepare=False, unknown=False):
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.hold_prepare = hold_prepare
        self.unknown = unknown
        self.cancelled = False
        self.record = None

    def receipt(self, status, **values):
        return {"job_id": self.record["job_id"], "generation": self.generation,
                "status": status, **values}

    async def prepare(self, record, text):
        self.record = record
        if self.hold_prepare:
            self.entered.set()
            await self.release.wait()
        return self.receipt("prepared")

    async def dispatch(self, record):
        self.sent.append((dict(record), "speech"))
        self.entered.set()
        if self.unknown:
            await asyncio.sleep(0)
            raise OSError("synthetic transport loss")
        return self.receipt("sending", delivery_started=True, worker_pending=True)

    async def cancel_job(self, record):
        if self.hold_prepare:
            self.cancelled = True
            return self.receipt("cancelled", cancel_result="cancelled_before_delivery")
        return self.receipt("sending", delivery_started=True, worker_pending=True,
                            cancel_result="already_started")

    async def job(self, record):
        if self.unknown:
            raise OSError("synthetic unknown receipt")
        if self.cancelled:
            return self.receipt("cancelled")
        if not self.release.is_set():
            return self.receipt("sending", delivery_started=True, worker_pending=True)
        return self.receipt("sent", delivery_started=True, send_complete=True,
                            handoff_wait_elapsed=True, post_audio_wait_seconds=4)


class BroadcastModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.avatar = FakeAvatar()
        self.model = SyntheticModel()
        self.wb = Workbench(":memory:", model=self.model, avatar_client=self.avatar)
        quiet_casual(self.wb.program)
        self.wb.program.ambient.tick = Mock()

    async def asyncTearDown(self):
        await self.wb.close()

    async def start(self, mode="game"):
        self.wb.program.start(mode)
        await self.wb.program.task
        if self.wb.job:
            await self.wb.job

    async def test_chat_start_does_not_create_story_and_retires_restored_episode(self):
        await self.wb.game.new_episode()
        old = self.wb.game.episode["id"]
        calls = len(self.model.calls)
        await self.start("chat")
        p = self.wb.program
        self.assertEqual(len(self.model.calls), calls)
        self.assertEqual(self.wb.game.episode["id"], old)
        self.assertEqual(self.wb.game.episode["status"], "TECH_ENDED")
        self.assertIsNone(p.game_episode_id)
        self.assertTrue(p.public()["switchable"])
        self.assertEqual(p.public()["content_mode"], "chat")
        with self.assertRaisesRegex(ValueError, "game_mode_required"):
            self.wb.accept_comment({})

    async def test_toggle_keeps_live_identity_and_new_episode_is_explicit(self):
        await self.start("chat")
        p = self.wb.program
        identity = (p.run_id, p.client.generation, p.player_url, p.started_at)
        p.set_mode(p.run_id, "game")
        worker = p._mode_task
        self.assertEqual(p.content_mode, "to_game")
        self.assertFalse(p.public()["switchable"])
        p.set_mode(p.run_id, "game")
        self.assertIs(worker, p._mode_task)
        await worker
        await self.wb.job
        episode = p.game_episode_id
        self.assertEqual(episode, self.wb.game.episode["id"])
        p.set_mode(p.run_id, "chat")
        self.assertIsNone(p.game_episode_id)
        await p._mode_task
        self.assertEqual(p.content_mode, "chat")
        p.set_mode(p.run_id, "game")
        await p._mode_task
        await self.wb.job
        self.assertNotEqual(p.game_episode_id, episode)
        self.assertEqual((p.run_id, p.client.generation, p.player_url, p.started_at), identity)
        self.assertEqual(self.avatar.calls.count("start"), 1)
        self.assertNotIn("stop", self.avatar.calls)

    async def test_stale_run_and_opposite_transition_are_rejected(self):
        await self.start("chat")
        p = self.wb.program
        with self.assertRaisesRegex(ValueError, "invalid_program_run"):
            p.set_mode("old", "game")
        p.set_mode(p.run_id, "game")
        with self.assertRaisesRegex(ValueError, "operation_in_progress"):
            p.set_mode(p.run_id, "chat")
        await p._mode_task

    async def test_game_off_waits_started_sentence_and_never_starts_typing(self):
        await self.start()
        p = self.wb.program
        avatar = HeldAvatar()
        avatar.generation, avatar.owner_id, avatar.state = p.client.generation, p.run_id, "ready"
        p.client = avatar
        record = p.prepare("held", "先把这句话讲完。")
        self.wb.job = asyncio.create_task(p.perform(record, "先把这句话讲完。"))
        await avatar.entered.wait()
        p.set_mode(p.run_id, "chat")
        self.assertEqual(self.wb.game.episode["status"], "TECH_ENDED")
        self.assertEqual(self.wb.game.current_round["phase"], "ABORTED")
        await asyncio.sleep(.01)
        self.assertEqual(p.content_mode, "to_chat")
        self.assertFalse(p._mode_task.done())
        avatar.release.set()
        await asyncio.wait_for(p._mode_task, 2)
        self.assertEqual(p.content_mode, "chat")
        self.assertFalse(p.sender_failed)
        self.assertEqual(len(avatar.sent), 1)
        self.assertNotIn("stop", avatar.calls)

    async def test_preparation_is_cancelled_by_job_id_before_mode_slot_reuse(self):
        await self.start()
        p = self.wb.program
        avatar = HeldAvatar(hold_prepare=True)
        avatar.generation, avatar.owner_id, avatar.state = p.client.generation, p.run_id, "ready"
        p.client = avatar
        record = p.prepare("held", "未开口的这句话。")
        self.wb.job = asyncio.create_task(p.perform(record, "未开口的这句话。"))
        await avatar.entered.wait()
        p.set_mode(p.run_id, "chat")
        await asyncio.sleep(.01)
        self.assertFalse(p._mode_task.done())
        avatar.release.set()
        await asyncio.wait_for(p._mode_task, 2)
        self.assertTrue(avatar.cancelled)
        self.assertFalse(avatar.sent)
        self.assertIsNone(p.next)
        self.assertFalse(p.sender_failed)

    async def test_unknown_dispatch_stops_broadcast_instead_of_unlocking_mode(self):
        await self.start()
        p = self.wb.program
        avatar = HeldAvatar(unknown=True)
        avatar.generation, avatar.owner_id, avatar.state = p.client.generation, p.run_id, "ready"
        p.client = avatar
        record = p.prepare("unknown", "结果未知。")
        self.wb.job = asyncio.create_task(p.perform(record, "结果未知。"))
        await avatar.entered.wait()
        p.set_mode(p.run_id, "chat")
        await asyncio.wait_for(p._mode_task, 2)
        self.assertEqual(p.state, "error")
        self.assertEqual(p.content_mode, "to_chat")
        self.assertIn("stop", avatar.calls)

    async def test_global_stop_interrupts_transition_without_self_join(self):
        await self.start()
        p = self.wb.program
        avatar = HeldAvatar()
        avatar.generation, avatar.owner_id, avatar.state = p.client.generation, p.run_id, "ready"
        p.client = avatar
        record = p.prepare("stop", "正常结束优先。")
        self.wb.job = asyncio.create_task(p.perform(record, "正常结束优先。"))
        await avatar.entered.wait()
        p.set_mode(p.run_id, "chat")
        await asyncio.sleep(0)
        await asyncio.wait_for(p.stop(), 2)
        self.assertEqual(p.state, "stopped")
        self.assertTrue(p._mode_task.done())
        self.assertIn("stop", avatar.calls)

    async def test_settled_finale_survives_game_off(self):
        await self.start()
        episode = self.wb.game.episode
        self.wb.game.submit_proposal("author", "军师", "认真回答")
        await self.wb.game.close_collection()
        self.wb.game.vote("viewer", "观众", "1")
        with patch("romance.game.WOMAN_TYPING_SECONDS", 0):
            await self.wb.game.close_voting()
        finale = dict(episode["finale"])
        self.wb.program.set_mode(self.wb.program.run_id, "chat")
        await self.wb.program._mode_task
        self.assertEqual(episode["status"], "SUCCEEDED")
        self.assertEqual(episode["finale"], finale)

    async def test_late_reply_cannot_score_after_synchronous_game_gate_closes(self):
        await self.start()
        game = self.wb.game
        game.submit_proposal("author", "军师", "认真回答")
        await game.close_collection()
        game.vote("viewer", "观众", "1")
        original_generate = self.model.generate
        entered = asyncio.Event()

        async def ignores_cancel(task, payload):
            if task == "reply":
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    pass  # A transport may already have a provider result.
            return await original_generate(task, payload)

        self.model.generate = ignores_cancel
        self.wb.launch(game.close_voting)
        await entered.wait()
        self.wb.program.set_mode(self.wb.program.run_id, "chat")
        self.assertFalse(game.public_state()["round"].get("woman_typing", False))
        await asyncio.wait_for(self.wb.program._mode_task, 2)
        self.assertEqual(game.episode["score"], 50)
        self.assertIsNone(game.current_round["delta"])
        self.assertEqual(game.current_round["phase"], "ABORTED")
        self.assertEqual(game.episode["status"], "TECH_ENDED")

    async def test_cancelled_network_transaction_latches_sender_until_full_stop(self):
        await self.start()
        p = self.wb.program
        avatar = HeldAvatar(hold_prepare=True)
        avatar.generation, avatar.owner_id, avatar.state = p.client.generation, p.run_id, "ready"
        p.client = avatar
        record = p.prepare("cancelled", "结果不能静默复用。")
        self.wb.job = asyncio.create_task(p.perform(record, "结果不能静默复用。"))
        await avatar.entered.wait()
        self.wb.job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.wb.job
        self.assertTrue(p.sender_failed)
        self.assertFalse(p.switchable)
        self.assertIsNotNone(p.next)
        with self.assertRaisesRegex(ValueError, "operation_in_progress"):
            p.set_mode(p.run_id, "chat")
        await p.step()
        self.assertEqual(p.state, "error")
        self.assertIn("stop", avatar.calls)

    async def test_formal_record_from_previous_game_epoch_cannot_replay(self):
        await self.start()
        p = self.wb.program
        record = p.prepare("previous", "这是上一期的话。")
        p.set_mode(p.run_id, "chat")
        await p._mode_task
        p.set_mode(p.run_id, "game")
        await p._mode_task
        await self.wb.job
        with self.assertRaisesRegex(ValueError, "invalid_performance_unavailable"):
            await p.perform(record, "这是上一期的话。")
        self.assertFalse(self.avatar.sent)

    async def finish_broadcast_episode(self, ending):
        await self.start("chat")
        p = self.wb.program
        p.set_mode(p.run_id, "game")
        await p._mode_task
        await self.wb.job
        episode_id = self.wb.game.episode["id"]
        original_generate = self.model.generate

        async def final_reply(task, payload):
            result = await original_generate(task, payload)
            if task == "reply" and ending == "failure":
                return {"message": "这次不太合适。", "delta": -payload["score"],
                        "ending": "failure", "reason": "合成终局"}
            return result

        self.model.generate = final_reply
        self.wb.game.submit_proposal("author", "军师", "认真回答")
        await self.wb.game.close_collection()
        self.wb.game.vote("viewer", "观众", "1")
        with patch("romance.game.WOMAN_TYPING_SECONDS", 0):
            await self.wb.game.close_voting()
        self.wb.state()
        self.assertGreater(self.wb.deadline, time.time())
        await p.step()
        self.assertEqual(p.content_mode, "game")
        self.wb.deadline = time.time() - 1
        await p.step()
        self.assertEqual(p.content_mode, "to_chat")
        await p._mode_task
        self.assertEqual(p.content_mode, "chat")
        self.assertEqual(self.wb.game.episode["id"], episode_id)
        self.assertEqual(self.wb.game.episode["status"], "SUCCEEDED" if ending == "success" else "FAILED")
        self.assertEqual(len(self.wb.game.data["episodes"]), 1)
        self.assertNotIn("stop", self.avatar.calls)

    async def test_broadcast_win_finishes_finale_then_returns_to_chat(self):
        await self.finish_broadcast_episode("success")

    async def test_broadcast_loss_finishes_finale_then_returns_to_chat(self):
        await self.finish_broadcast_episode("failure")


class BroadcastModeHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_formal_chat_start_mode_endpoint_and_legacy_start_contract(self):
        app = create_app(":memory:", model=SyntheticModel(), avatar_client=FakeAvatar())
        wb = app["workbench"]
        quiet_casual(wb.program)
        async with TestClient(TestServer(app)) as client:
            response = await client.post("/api/program/start", json={"mode": "chat"})
            self.assertEqual(response.status, 200)
            await wb.program.task
            self.assertIsNone(wb.game.episode)
            state = await (await client.get("/api/public")).json()
            self.assertEqual(state["program"]["content_mode"], "chat")
            response = await client.post("/api/program/mode", json={"run_id": "old", "mode": "game"})
            self.assertEqual(response.status, 409)
            response = await client.post("/api/comments", json={"text": "不能计入旧游戏"})
            self.assertEqual(response.status, 409)
            response = await client.post("/api/program/mode", json={"run_id": wb.program.run_id, "mode": "game"})
            self.assertEqual(response.status, 200)
            await wb.program._mode_task
            await wb.job
            self.assertIsNotNone(wb.program.game_episode_id)
            await wb.program.stop()
            response = await client.post("/api/program/start", json={})
            self.assertEqual(response.status, 200)
            await wb.program.task
            await wb.job
            self.assertEqual(wb.program.content_mode, "game")
