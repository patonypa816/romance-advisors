"""Real game/client/Studio HTTP handshake, with only TTS and rendering faked."""
import asyncio
import copy
from pathlib import Path
import sys
import time
import unittest

import test_external_game as fixtures

# Keep the application boundary real: these tests catch protocol mismatches
# which separate fake-client and fake-server unit tests cannot see.
GAME_ROOT = Path(__file__).resolve().parents[2] / "romance-advisors"
for directory in (GAME_ROOT, GAME_ROOT / "tests"):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

from romance.avatar_client import AvatarClient, AvatarClientError
from romance.expression import build_plan
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

    async def test_formal_round_recovers_lost_prepare_and_dispatch_without_repeating(self):
        self.avatar.lose_responses = {"prepare", "dispatch"}
        entered, release = asyncio.Event(), asyncio.Event()
        sender = self.studio.current.component.send_aligned_expression

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            return {"status": "sent", "send_complete": True, "handoff_wait_elapsed": True}

        sender.side_effect = send
        game = self.wb.game
        game.submit_proposal("advisor", "军师", "解释紧张")
        selected = await game.close_collection()
        game.vote("viewer", "观众", selected["round"]["options"][0]["id"])
        task = asyncio.create_task(game.close_voting())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            state = game.public_state()
            self.assertEqual([m["role"] for m in state["messages"]], ["woman"])
            self.assertFalse(any(kind == "reply" for kind, _ in self.model.calls))
            self.assertEqual(self.tts.await_count, 1)
        finally:
            release.set()
        result = await asyncio.wait_for(task, 3)
        self.assertEqual(result["round"]["phase"], "DONE", result.get("error"))
        self.assertTrue(result["round"]["performance"]["man_released"])
        self.assertFalse(result["round"]["performance"]["playback_confirmed"])
        self.assertEqual([m["role"] for m in result["messages"]], ["woman", "man", "woman"])
        self.assertEqual(self.tts.await_count, 1)
        self.assertEqual(sender.await_count, 1)
        self.assertEqual(self.avatar.paths.count("external/jobs/prepare"), 1)
        self.assertEqual(sum(p.endswith("/dispatch") for p in self.avatar.paths), 1)
        self.assertEqual(len(self.studio.external_game.jobs), 1)
        self.assertEqual(len(self.wb.program.expression_context()["recent_performances"]), 1)

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
