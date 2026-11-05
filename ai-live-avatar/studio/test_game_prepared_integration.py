"""Real game/client/Studio HTTP handshake, with only TTS and rendering faked."""
import asyncio
import copy
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_external_game as fixtures
from test_semantic_progress import ManualClock

# Keep the application boundary real: these tests catch protocol mismatches
# which separate fake-client and fake-server unit tests cannot see.
GAME_ROOT = Path(__file__).resolve().parents[2] / "romance-advisors"
for directory in (GAME_ROOT, GAME_ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

from romance.avatar_client import AvatarClient, AvatarClientError
from romance.expression import build_plan
from romance.performance import MESSAGE_MOTION_PLAN
from romance.server import Workbench
from test_game import FakeModel


class ObservedClient(AvatarClient):
    def __init__(self, runtime_path):
        super().__init__(runtime_path)
        self.paths = []
        self.lose_responses = set()

    async def request(self, path, *, body=None, query=None):
        self.paths.append(path)
        result = await super().request(path, body=body, query=query)
        kind = "prepare" if path == "external/jobs/prepare" else "dispatch" if path.endswith("/dispatch") else None
        if kind in self.lose_responses:
            self.lose_responses.remove(kind)
            # The server already accepted it. The client must query this ID,
            # never synthesize again or dispatch a fresh job to recover.
            raise AvatarClientError("connection_unconfirmed")
        return result


class GamePreparedIntegration(unittest.IsolatedAsyncioTestCase):
    synthesize = fixtures.ExternalGameContracts.synthesize
    post = fixtures.ExternalGameContracts.post
    claim = fixtures.ExternalGameContracts.claim
    live = fixtures.ExternalGameContracts.live

    async def asyncSetUp(self):
        await fixtures.ExternalGameContracts.asyncSetUp(self)
        await self.claim()
        self.live()
        self.avatar = ObservedClient(self.root / "absent-runtime.json")
        self.model = FakeModel()
        self.wb = Workbench(":memory:", model=self.model, avatar_client=self.avatar)
        self.avatar.origin = self.studio.origin
        self.avatar.control_url = self.studio.origin + self.studio.prefix + "/"
        self.avatar.owner_id = self.identity["owner_id"]
        self.avatar.generation = self.identity["generation"]
        program = self.wb.program
        program.state, program.ready, program.run_id = "running", True, self.identity["owner_id"]
        program.started_at = program.page_seen = time.monotonic()
        await self.wb.game.new_episode()

    async def asyncTearDown(self):
        await self.wb.close()
        await fixtures.ExternalGameContracts.asyncTearDown(self)

    async def eventually(self, predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(.01)

    async def test_success_finale_recovers_lost_prepare_and_dispatch_without_repeating(self):
        await self.formal_finale_round(50)

    async def test_failure_finale_recovers_lost_prepare_and_dispatch_without_repeating(self):
        await self.formal_finale_round(-50)

    async def formal_finale_round(self, delta):
        self.model.delta = delta
        avatar_url = "https://p3.douyinpic.com/aweme/synthetic-avatar.jpeg"
        self.avatar.lose_responses = {"prepare", "dispatch"}
        entered, release, reply_generated = asyncio.Event(), asyncio.Event(), asyncio.Event()
        session = self.studio.current
        original = session.component
        renderer = fixtures.ExternalGameContracts.renderer(self, session)
        socket = renderer._ws
        clock = ManualClock()
        renderer._wait = lambda seconds: clock.wait(renderer, seconds)
        fake_time = SimpleNamespace(monotonic=lambda: clock.now, time=time.time)

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"status": "sent", "send_complete": True, "handoff_wait_elapsed": True,
                    "post_audio_wait_seconds": 0.0}

        renderer.send_aligned_expression = sender = AsyncMock(side_effect=send)
        session.component = renderer
        model_generate = self.model.generate
        self.model.wait_task = "reply"

        async def generate(kind, payload):
            result = await model_generate(kind, payload)
            if kind == "reply":
                reply_generated.set()
            return result

        self.model.generate = generate
        game, task = self.wb.game, None

        def roles():
            return [message["role"] for message in game.public_state()["messages"]]

        def assert_no_reply():
            self.assertFalse(any(kind == "reply" for kind, _ in self.model.calls))

        try:
            # Only motion scheduling uses the manual clock. The game typing
            # window runs on real time, alongside the fake model request.
            with (patch("motion_component.time", fake_time), patch("component_live.time", fake_time),
                  patch("romance.game.WOMAN_TYPING_SECONDS", .4),
                  patch.object(self.studio, "player_public", return_value={"connected": True})):
                # Enter through the same comment boundary used by DyHub, keeping
                # the original author and avatar through the prepared show.
                game.accept_comment({"event_id": "dyhub:synthetic", "author_id": "advisor",
                    "nickname": "军师", "text": "解释紧张", "avatar_url": avatar_url,
                    "expected_round_id": game.current_round["id"], "expected_phase": "COLLECTING"})
                selected = await game.close_collection()
                score_before = selected["episode"]["score"]
                game.vote("viewer", "观众", selected["round"]["options"][0]["id"])
                task = asyncio.create_task(game.close_voting())
                await asyncio.wait_for(entered.wait(), 3)
                self.assertEqual(roles(), ["woman"])
                self.assertEqual(renderer.motion_events, [])
                assert_no_reply()
                self.assertEqual(self.tts.await_count, 1)
                release.set()

                await self.eventually(lambda: len(renderer.motion_events) == 1)
                self.assertEqual([event["role"] for event in renderer.motion_events], ["GE"])
                self.assertEqual(roles(), ["woman"])
                motion = next(job for job in self.studio.external_game.jobs.values() if job["mode"] == "motion")
                self.assertEqual(motion["submitted_plan"]["actions"], MESSAGE_MOTION_PLAN["actions"])
                self.assertEqual(motion["submitted_plan"]["recovery"], MESSAGE_MOTION_PLAN["recovery"])
                self.assertFalse(self.studio.external_game.job_public(motion)["short_action_sent"])
                self.assertFalse(game.public_state()["round"]["woman_typing"])
                assert_no_reply()

                await clock.advance(1.1)
                await self.eventually(lambda: game.public_state()["round"]["performance"]["man_released"])
                self.assertEqual([event["role"] for event in renderer.motion_events], ["GE", "S"])
                self.assertEqual(roles(), ["woman", "man"])
                self.assertEqual(game.public_state()["round"]["phase"], "WAITING_REPLY")
                self.assertTrue(self.studio.external_game.job_public(motion)["short_action_sent"])
                self.assertFalse(game.public_state()["round"]["woman_typing"])
                self.assertFalse(task.done())
                assert_no_reply()

                await clock.advance(1.5)
                await asyncio.wait_for(self.model.started.wait(), 3)
                typing = game.public_state()
                self.assertEqual([event["role"] for event in renderer.motion_events], ["GE", "S", "Q"])
                self.assertTrue(typing["round"]["woman_typing"])
                self.assertEqual(roles(), ["woman", "man"])
                self.assertEqual(typing["episode"]["score"], score_before)
                self.assertIsNone(typing["round"]["delta"])
                self.assertIsNone(typing["finale"])
                # A fast generated reply still waits for the visible typing
                # window; neither its body nor its score leaks early.
                self.model.release.set()
                await asyncio.wait_for(reply_generated.wait(), 1)
                typing = game.public_state()
                self.assertTrue(typing["round"]["woman_typing"])
                self.assertEqual(roles(), ["woman", "man"])
                self.assertEqual(typing["episode"]["score"], score_before)
                self.assertIsNone(typing["round"]["delta"])
                self.assertIsNone(typing["finale"])
                self.assertFalse(task.done())

                # Finale time must start at the joint woman/score commit, not
                # at S, Q, model completion, or the first public-state poll.
                commit_not_before = time.time()
                result = await asyncio.wait_for(task, 3)
                self.assertEqual(result["round"]["phase"], "DONE", result.get("error"))
                self.assertFalse(result["round"]["woman_typing"])
                self.assertTrue(result["round"]["performance"]["man_released"])
                self.assertFalse(result["round"]["performance"]["playback_confirmed"])
                self.assertEqual(roles(), ["woman", "man", "woman"])
                self.assertEqual(result["episode"]["score"], score_before + self.model.delta)
                finale = result["finale"]
                self.assertEqual(finale["status"], "SUCCEEDED" if delta > 0 else "FAILED")
                self.assertGreaterEqual(finale["started_at"], commit_not_before)
                self.assertEqual(finale["duration_ms"], 6800)
                award = finale["awards"]["best" if delta > 0 else "worst"]
                self.assertEqual(award["author_id"], "advisor")
                self.assertEqual(award["avatar_url"], avatar_url)
                self.assertNotIn(avatar_url, json.dumps(self.model.calls))
                frozen = copy.deepcopy(game.episode["finale"])
                await game.retry()
                for _ in range(3):
                    self.assertEqual(game.public_state()["finale"]["started_at"], frozen["started_at"])
                self.assertEqual(game.episode["finale"], frozen)
                self.assertEqual(game.storage.load()["episodes"][-1]["finale"], frozen)
                self.assertEqual(result["round"]["delta"], self.model.delta)
                self.assertEqual(self.tts.await_count, 1)
                self.assertEqual(sender.await_count, 1)
                self.assertEqual(self.avatar.lose_responses, set())
                self.assertEqual(self.avatar.paths.count("external/jobs/prepare"), 1)
                # A prepared response may require one same-ID redispatch; only
                # the real worker send count proves no duplicate performance.
                self.assertIn(sum(path.endswith("/dispatch") for path in self.avatar.paths), (1, 2))
                self.assertEqual(self.avatar.paths.count("external/jobs"), 1)
                self.assertEqual(len(self.studio.external_game.jobs), 2)
                self.assertEqual(motion["job_id"], game.current_round["performance"]["job_id"] + ":typing")
                self.assertEqual([row["frame"]["type"] for row in socket.sent], [9, 9, 9])
                action_text = socket.sent[1]["frame"]["payload"]["text_msg"]["content"]
                for movement in MESSAGE_MOTION_PLAN["actions"][0]["motions"]:
                    self.assertIn(movement["description"], action_text)
                self.assertEqual(socket.binary, [])
                self.assertIs(renderer._ws, socket)
                self.assertFalse(session.stopping)
                self.assertEqual(len(self.wb.program.expression_context()["recent_performances"]), 1)
                self.ai.generate_reply.assert_not_awaited()
                session.timed_action_planner.assert_not_awaited()
        finally:
            release.set()
            self.model.release.set()
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            session.component = original

    async def test_next_is_prepared_over_http_while_current_is_sending(self):
        entered, release, next_tts = asyncio.Event(), asyncio.Event(), asyncio.Event()
        active, maximum, spoken = 0, 0, []
        sender = self.studio.current.component.send_aligned_expression
        original_tts = self.synthesize

        async def tts(*args, **kwargs):
            result = await original_tts(*args, **kwargs)
            if self.tts.await_count == 2:
                next_tts.set()
            return result

        async def send(pcm, text, *args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(active, maximum)
            spoken.append(text)
            try:
                if len(spoken) == 1:
                    entered.set()
                    await release.wait()
                return {"status": "sent", "send_complete": True, "handoff_wait_elapsed": True}
            finally:
                active -= 1

        self.tts.side_effect, sender.side_effect = tts, send
        program = self.wb.program
        first = "我先听听大家的想法。"
        second = "好，我认真解释一下。"
        expression = {"emotion": "认真", "actions": [], "recovery": None}
        a = {"job_id": "overlap:a", **self.avatar.owner(), "plan": build_plan(first, "自然说。", expression)}
        b = {"job_id": "overlap:b", **self.avatar.owner(), "plan": build_plan(second, "认真说。", expression)}
        task_a = asyncio.create_task(program.send(a, first))
        task_b = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task_b = asyncio.create_task(program.perform(copy.deepcopy(b), second))
            await asyncio.wait_for(next_tts.wait(), 3)
            await self.eventually(lambda: self.studio.external_game.jobs["overlap:b"]["item"]["status"] == "prepared")
            self.assertEqual(spoken, [first])
            self.assertTrue(self.studio.external_game.jobs["overlap:a"]["item"]["external_worker_pending"])
            self.assertFalse(self.studio.external_game.jobs["overlap:b"]["item"]["external_worker_pending"])
            self.assertNotIn(second, str(program.expression_context()))
        finally:
            release.set()
        self.assertEqual((await asyncio.wait_for(task_a, 3))["status"], "sent")
        self.assertIsNotNone(task_b)
        self.assertEqual((await asyncio.wait_for(task_b, 3))["status"], "sent")
        self.assertEqual(spoken, [first, second])
        self.assertEqual(maximum, 1)
        self.assertEqual(self.tts.await_count, 2)

    async def test_stop_reaches_cloud_boundary_before_slow_preparation_returns(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original_tts = self.synthesize

        async def slow_tts(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_tts(*args, **kwargs)

        self.tts.side_effect = slow_tts
        program = self.wb.program
        text = "我先听听大家的想法。"
        record = {"job_id": "stop:slow", **self.avatar.owner(),
                  "plan": build_plan(text, "自然说。", {"emotion": "期待", "actions": [], "recovery": None})}
        delivery = asyncio.create_task(program.send(record, text, optional=True))
        stop = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            stop = asyncio.create_task(program.stop())
            await self.eventually(lambda: self.studio.current.component.stopped)
            self.assertFalse(release.is_set())
            self.studio.current.component.send_aligned_expression.assert_not_awaited()
        finally:
            release.set()
        self.assertIsNotNone(stop)
        await asyncio.wait_for(stop, 3)
        await asyncio.gather(delivery, return_exceptions=True)
        await self.eventually(lambda: not self.studio.external_game.jobs["stop:slow"]["item"]["external_preparation_pending"])
        self.assertEqual(program.state, "stopped")
        self.assertEqual(self.tts.await_count, 1)
        self.studio.current.component.send_aligned_expression.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
