"""Live rankings use provenance; all fixtures are temporary synthetic records."""
import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp.test_utils import TestClient, TestServer

from romance.dyhub_intake import source_identity
from romance.game import Game
from romance.server import create_app
from test_game import FakeModel
from test_finale import SuggestionModel


class LiveBoardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "game.sqlite"
        self.model = FakeModel()
        self.typing = patch("romance.game.WOMAN_TYPING_SECONDS", 0)
        self.typing.start()
        self.game = Game(self.path, self.model)
        self.broadcast = {"room_id": "123", "live_id": "stream-a"}
        self.game.bind_live_broadcast(**self.broadcast)
        await self.game.new_episode()

    async def asyncTearDown(self):
        self.game.close()
        self.typing.stop()
        self.temp.cleanup()

    async def settle(self, viewer, *, live=False, delta=1, nickname="模拟小王", legacy=False):
        if self.game.current_round["phase"] == "DONE":
            self.game.start_round()
        author = source_identity("viewer", "123", viewer) if live else viewer
        event = {"event_id": source_identity("message", "123", self.game.data["round_id"] + viewer),
                 "author_id": author, "nickname": nickname, "text": "建议认真解释",
                 "expected_round_id": self.game.data["round_id"], "expected_phase": "COLLECTING"}
        receipt = self.game.accept_comment(event, source="dyhub" if live else "local",
                                           live_broadcast=self.broadcast if live else None)
        if legacy:
            self.game._get("comments", receipt["id"]).pop("source")
            self.game._get("proposals", receipt["proposal_id"]).pop("source")
            self.game._save()
        self.model.delta = delta
        await self.game.close_collection()
        self.game.vote("voter", "投票", "1")
        return await self.game.close_voting()

    async def test_mixed_history_same_nickname_real_viewers_survive_episode_and_restart(self):
        await self.settle("local", delta=-3)
        await self.settle("old-real", live=True, legacy=True, delta=-2)
        await self.settle("new-real", live=True, delta=3)
        live = self.game.public_state(live_only=True)["leaderboards"]
        self.assertEqual({row["author_id"] for row in live["best"]},
                         {source_identity("viewer", "123", v) for v in ("old-real", "new-real")})
        self.assertTrue(all(row["nickname"] == "模拟小王" for row in live["best"]))
        self.assertEqual(len(self.game.public_state()["leaderboards"]["best"]), 3)
        await self.game.new_episode()
        self.assertEqual(self.game.public_state(live_only=True)["leaderboards"], live)
        self.game.close()
        self.game = Game(self.path, self.model)
        self.assertEqual(self.game.public_state(live_only=True)["leaderboards"], live)
        await self.game.new_session()
        self.assertEqual(self.game.public_state(live_only=True)["leaderboards"], live)
        self.assertEqual(self.game.public_state()["leaderboards"], {"best": [], "worst": []})

    async def test_unbound_legacy_history_stays_debug_only_and_is_not_backfilled(self):
        self.game.data.pop("live_broadcast_id")
        self.game.data.pop("live_broadcasts")
        await self.settle("old-real", live=True, legacy=True)
        self.assertEqual(len(self.game.public_state()["leaderboards"]["best"]), 1)
        self.assertEqual(self.game.public_state(live_only=True)["leaderboards"]["best"], [])
        self.game.bind_live_broadcast(**self.broadcast)
        self.assertEqual(self.game.public_state(live_only=True)["leaderboards"]["best"], [])
        await self.settle("new-real", live=True)
        self.assertEqual(len(self.game.public_state(live_only=True)["leaderboards"]["best"]), 1)

    async def test_same_broadcast_is_idempotent_and_new_broadcast_preserves_story(self):
        await self.settle("first", live=True)
        old_binding = self.game.bind_live_broadcast(**self.broadcast)
        old_boards = self.game.public_state(live_only=True)["leaderboards"]
        statements = []
        self.game.storage.db.set_trace_callback(statements.append)
        self.assertEqual(self.game.bind_live_broadcast(**self.broadcast), old_binding)
        self.assertFalse(any(s.startswith(("INSERT", "UPDATE", "DELETE")) for s in statements))
        episode, round_, token = self.game.episode, self.game.current_round, self.game._token
        self.broadcast = dict(self.broadcast, live_id="stream-b")
        new_binding = self.game.bind_live_broadcast(**self.broadcast)
        self.assertNotEqual(new_binding["id"], old_binding["id"])
        self.assertIs(self.game.episode, episode)
        self.assertIs(self.game.current_round, round_)
        self.assertEqual(self.game._token, token)
        self.assertEqual(self.game.public_state(live_only=True)["leaderboards"]["best"], [])
        await self.settle("second", live=True, delta=-1)
        self.assertEqual(len(self.game.public_state()["leaderboards"]["best"]), 2)
        self.assertEqual(len(self.game.public_state(live_only=True)["leaderboards"]["best"]), 1)
        self.assertEqual(len(self.game.data["live_broadcasts"]), 2)
        # Returning to a known identity recovers its retained history.
        self.game.bind_live_broadcast("123", "stream-a")
        self.assertEqual(self.game.public_state(live_only=True)["leaderboards"], old_boards)

    async def test_new_broadcast_during_reply_does_not_cancel_or_reattribute_old_input(self):
        self.game.accept_comment({"event_id": "in-flight", "author_id": "viewer",
            "nickname": "观众", "text": "建议解释", "expected_round_id": self.game.data["round_id"],
            "expected_phase": "COLLECTING"}, source="dyhub", live_broadcast=self.broadcast)
        await self.game.close_collection()
        self.game.vote("voter", "投票", "1")
        self.model.wait_task = "reply"
        task = asyncio.create_task(self.game.close_voting())
        await self.model.started.wait()
        try:
            self.game.bind_live_broadcast("123", "stream-b")
            self.assertTrue(self.game.busy)
            self.model.release.set()
            await task
        finally:
            self.model.release.set()
            await task
        self.assertEqual(self.game.current_round["phase"], "DONE")
        self.assertEqual(self.game.public_state(live_only=True)["leaderboards"]["best"], [])
        self.game.bind_live_broadcast(**self.broadcast)
        self.assertEqual(self.game.public_state(live_only=True)["leaderboards"]["best"][0]["adopted"], 1)

    async def test_unknown_restart_input_and_forged_body_do_not_attach_to_retained_board(self):
        await self.settle("known", live=True)
        self.game.close()
        self.game = Game(self.path, self.model)
        self.game.start_round()
        event = {"event_id": "unknown", "author_id": "unknown", "nickname": "观众", "text": "解释",
                 "expected_round_id": self.game.data["round_id"], "expected_phase": "COLLECTING",
                 "live_broadcast_id": self.game.data["live_broadcast_id"], "live_broadcast": self.broadcast}
        receipt = self.game.accept_comment(event, source="dyhub")
        self.assertNotIn("live_broadcast_id", receipt)
        await self.game.close_collection()
        await self.game.close_voting()
        self.assertEqual(len(self.game.public_state()["leaderboards"]["best"]), 2)
        self.assertEqual(len(self.game.public_state(live_only=True)["leaderboards"]["best"]), 1)

    async def test_wrong_room_and_local_trusted_context_cannot_enter_official_board(self):
        for context, source in ((dict(self.broadcast, room_id="456"), "dyhub"),
                                (dict(self.broadcast, live_id="wrong"), "dyhub"),
                                (self.broadcast, "local")):
            with self.subTest(context=context, source=source):
                if self.game.current_round["phase"] == "DONE":
                    self.game.start_round()
                receipt = self.game.accept_comment({"event_id": self.game.data["round_id"],
                    "author_id": "fake", "nickname": "观众", "text": "解释",
                    "expected_round_id": self.game.data["round_id"], "expected_phase": "COLLECTING"},
                    source=source, live_broadcast=context)
                self.assertNotIn("live_broadcast_id", receipt)
                await self.game.close_collection()
                await self.game.close_voting()
        self.assertEqual(self.game.public_state(live_only=True)["leaderboards"]["best"], [])

    async def test_binding_validation_and_storage_failure_leave_active_board_unchanged(self):
        before = copy.deepcopy(self.game.data)
        for room, live in (("not-a-room", "x"), ("１２３", "x"), ("123", ""), ("123", "x" * 257)):
            with self.assertRaises(ValueError):
                self.game.bind_live_broadcast(room, live)
        self.assertEqual(self.game.data, before)
        with patch.object(self.game.storage, "save", side_effect=RuntimeError("disk failure")):
            with self.assertRaises(RuntimeError):
                self.game.bind_live_broadcast("123", "stream-b")
        self.assertEqual(self.game.data, before)

    async def test_room_number_is_part_of_broadcast_identity(self):
        first = self.game.bind_live_broadcast("123", "same-platform-id")
        second = self.game.bind_live_broadcast("456", "same-platform-id")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.game.bind_live_broadcast("123", "same-platform-id"), first)

    async def test_workbench_forwards_only_adapter_broadcast_context(self):
        from romance.server import Workbench
        wb = Workbench(":memory:", model=self.model)
        try:
            await wb.game.new_episode()
            bound = wb.game.bind_live_broadcast(**self.broadcast)
            event = {"event_id": "forwarded", "author_id": "viewer", "nickname": "观众", "text": "解释",
                     "expected_round_id": wb.game.data["round_id"], "expected_phase": "COLLECTING"}
            receipt = wb.accept_comment(event, source="dyhub", live_broadcast=self.broadcast)
            self.assertEqual(receipt["live_broadcast_id"], bound["id"])
            self.assertEqual(wb.game._get("proposals", receipt["proposal_id"])["live_broadcast_id"], bound["id"])
        finally:
            await wb.close()

    async def test_explicit_local_cannot_forge_origin_using_ids_or_body_source(self):
        author = source_identity("viewer", "123", "fake")
        event = {"event_id": source_identity("message", "123", "fake"), "author_id": author,
                 "nickname": "真实的昵称", "text": "建议认真解释", "source": "dyhub",
                 "expected_round_id": self.game.data["round_id"], "expected_phase": "COLLECTING"}
        receipt = self.game.accept_comment(event)
        self.assertEqual(receipt["source"], "local")
        self.model.delta = 50
        await self.game.close_collection()
        await self.game.close_voting()
        self.assertEqual(self.game.public_state(live_only=True)["leaderboards"], {"best": [], "worst": []})
        self.assertEqual(self.game.public_state(live_only=True)["finale"]["awards"], {"best": None, "worst": None})
        self.assertEqual(self.game.public_state()["finale"]["awards"]["best"]["author_id"], author)

    async def test_legacy_author_prefix_alone_does_not_prove_live_origin(self):
        await self.settle(source_identity("viewer", "123", "fake"))
        proposal = self.game.data["proposals"][-1]
        proposal.pop("source")
        receipt = self.game._proposal_comments[proposal["id"]]
        receipt.pop("source")
        receipt["id"] = "local-comment"
        self.assertEqual(self.game.public_state(live_only=True)["leaderboards"]["best"], [])

    async def test_official_awards_count_live_scores_and_unselected_live_suggestions_only(self):
        await self.settle("local", delta=-10)
        await self.settle("real", live=True, delta=1)
        state = await self.settle("local", delta=100)
        frozen = copy.deepcopy(self.game.episode["finale"])
        self.assertEqual(state["finale"]["awards"]["worst"]["author_id"], "local")
        statements = []
        self.game.storage.db.set_trace_callback(statements.append)
        with patch.object(self.game.rng, "choice", side_effect=AssertionError("read must not redraw")):
            for _ in range(3):
                awards = self.game.public_state(live_only=True)["finale"]["awards"]
                self.assertEqual(awards["best"]["author_id"], source_identity("viewer", "123", "real"))
                self.assertEqual(awards["worst"]["author_id"], source_identity("viewer", "123", "real"))
                self.assertEqual(awards["best"]["adopted"], 1)
                self.assertEqual(awards["worst"]["negative_delta"], 0)
        self.assertEqual(self.game.episode["finale"], frozen)
        self.assertFalse(any(s.startswith(("INSERT", "UPDATE", "DELETE")) for s in statements))

    async def test_official_and_debug_share_one_fallback_draw_when_all_authors_are_live(self):
        await self.settle("first", live=True, delta=1)
        with patch.object(self.game.rng, "choice", wraps=self.game.rng.choice) as choose:
            await self.settle("second", live=True, delta=100)
        fallback_draws = [call for call in choose.call_args_list
                          if len(call.args[0]) == 2 and all(isinstance(item, str) and
                              item.startswith("dyhub:viewer:") for item in call.args[0])]
        self.assertEqual(len(fallback_draws), 1)
        frozen = self.game.episode["finale"]
        self.assertEqual(frozen["awards"], frozen["live_awards"])

    async def test_legacy_frozen_awards_are_filtered_without_redraw_or_storage_write(self):
        await self.settle("local", delta=-10)
        await self.settle("real", live=True, legacy=True, delta=100)
        self.game.episode["finale"].pop("live_awards")
        self.game._save()
        frozen = copy.deepcopy(self.game.episode["finale"])
        with patch.object(self.game.rng, "choice", side_effect=AssertionError("legacy read must not redraw")):
            public = self.game.public_state(live_only=True)["finale"]
        self.assertIsNone(public["awards"]["worst"])
        self.assertEqual(public["awards"]["best"]["author_id"], source_identity("viewer", "123", "real"))
        self.assertEqual(self.game.episode["finale"], frozen)

    async def test_random_official_award_can_choose_unselected_real_suggestion(self):
        self.model = SuggestionModel()
        self.game.model = self.model
        for viewer, live in (("local", False), ("real", True)):
            self.game.accept_comment({
                "event_id": source_identity("message", "123", viewer),
                "author_id": source_identity("viewer", "123", viewer) if live else viewer,
                "nickname": "同名", "text": "建议" + viewer,
                "expected_round_id": self.game.data["round_id"], "expected_phase": "COLLECTING"},
                source="dyhub" if live else "local")
        self.model.delta = 50
        await self.game.close_collection()
        self.game.vote("voter", "投票", "1")
        await self.game.close_voting()
        official = self.game.public_state(live_only=True)
        self.assertEqual(official["leaderboards"], {"best": [], "worst": []})
        self.assertIsNone(official["finale"]["awards"]["best"])
        worst = official["finale"]["awards"]["worst"]
        self.assertEqual(worst["author_id"], source_identity("viewer", "123", "real"))
        self.assertEqual(worst["adopted"], 0)
        self.assertEqual(worst["selection_reason"], "random_suggestion")

    async def test_http_normal_and_obs_are_live_debug_contract_stays_available(self):
        await self.settle("local", delta=-1)
        await self.settle("real", live=True, delta=100)
        self.game.close()
        self.game = Game(":memory:", self.model)
        app = create_app(self.path, model=self.model)
        async with TestClient(TestServer(app)) as client:
            for endpoint in ("/api/public", "/api/operator-state", "/api/obs-output", "/api/obs-output?debug=1"):
                state = await (await client.get(endpoint)).json()
                self.assertEqual(len(state["leaderboards"]["best"]), 1, endpoint)
                self.assertEqual(state["finale"]["awards"]["worst"]["author_id"], source_identity("viewer", "123", "real"))
            for endpoint in ("/api/state", "/api/public?debug=1", "/api/operator-state?debug=1"):
                state = await (await client.get(endpoint)).json()
                self.assertEqual(len(state["leaderboards"]["best"]), 2, endpoint)
            self.assertEqual(len(app["workbench"].state()["leaderboards"]["best"]), 1)
            self.assertEqual(len(app["workbench"].state(debug=True)["leaderboards"]["best"]), 2)


if __name__ == "__main__":
    unittest.main()
