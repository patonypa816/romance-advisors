"""OBS receiver routing with in-memory games and synthetic loopback Studio only."""
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from romance.avatar_client import AvatarClient, AvatarClientError
from romance.server import Workbench, create_app
from test_program import FakeAvatar
from test_server import SyntheticModel


class ObsRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_obs_url_is_only_in_authorized_projection_and_get_never_starts_or_heartbeats(self):
        for obs in (False, True):
            with self.subTest(obs_output=obs), patch.object(Workbench, "tick", AsyncMock()):
                avatar, model = FakeAvatar(), SyntheticModel()
                app = create_app(":memory:", model=model, avatar_client=avatar, obs_output=obs)
                async with TestClient(TestServer(app, host="127.0.0.1")) as client:
                    wb = app["workbench"]
                    for _ in range(2):
                        response = await client.get("/api/obs-output")
                        self.assertEqual(response.status, 200)
                        state = await response.json()
                        self.assertEqual(state["obs_output"], obs)
                        self.assertIsNone(state["player_url"])
                        self.assertIsNone(state["episode"])
                    self.assertEqual(avatar.calls, [])
                    self.assertEqual(model.calls, [])
                    self.assertIsNone(wb.program.task)
                    self.assertIsNone(wb.program.page_seen)
                    # Simulate an already authorized run without starting any
                    # cloud/model work, then verify each projection separately.
                    wb.program.state = "running"
                    wb.program.page_seen = 123
                    wb.program._player_url = "http://127.0.0.1:4444/player/offline/?game_owner=run"
                    try:
                        response = await client.get("/api/obs-output")
                        output = await response.json()
                        self.assertEqual(output["player_url"], wb.program.player_url if obs else None)
                        self.assertNotIn("player_url", output["program"])
                        for route in ("/api/public", "/api/state", "/api/operator-state"):
                            state = await (await client.get(route)).json()
                            self.assertNotIn("player_url", state)
                            if route == "/api/public":
                                self.assertNotIn("player_url", state["program"])
                            else:
                                self.assertEqual(state["program"]["player_url"], "" if obs else wb.program.player_url)
                        self.assertEqual(wb.program.page_seen, 123)
                        self.assertEqual(wb.program.last_heartbeat, 0)
                        self.assertEqual(avatar.calls, [])
                        self.assertEqual(model.calls, [])
                    finally:
                        wb.program.state = "idle"


class ObsAvatarClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_mode_mismatch_rejected_before_claim_new_or_paid_start(self):
        for obs, actual, code in ((True, "preview", "obs_required"), (False, "obs", "preview_required")):
            with self.subTest(obs_output=obs):
                client = AvatarClient(obs_output=obs)
                snapshot = {"session": {"state": "ended", "generation": "g", "can_new": True},
                            "studio": {"receiver_mode": actual, "can_new": True}}
                with patch.object(client, "discover"), patch.object(client, "snapshot", AsyncMock(return_value=snapshot)), \
                        patch.object(client, "request", AsyncMock()) as request:
                    with self.assertRaises(AvatarClientError) as failure:
                        await client.prepare_session("offline-owner")
                    self.assertEqual(failure.exception.code, code)
                    request.assert_not_awaited()

    async def test_exact_mode_and_owner_reach_synthetic_studio_with_preview_compatibility(self):
        for obs in (False, True):
            calls = []
            owner = "romance:offline / owner"
            app = web.Application()

            async def state(request):
                calls.append((request.method, request.path))
                return web.json_response({"session": {"state": "idle", "generation": "g"},
                                          "studio": {"receiver_mode": "obs" if obs else "preview"}})

            async def claim(request):
                calls.append((request.method, request.path, await request.json(), request.headers["Origin"]))
                return web.json_response({"owner_id": owner, "generation": "g"})

            app.router.add_get("/studio/offline/state", state)
            app.router.add_post("/studio/offline/external/claim", claim)
            async with TestServer(app, host="127.0.0.1") as server:
                origin = str(server.make_url("")).rstrip("/")
                with tempfile.TemporaryDirectory() as directory:
                    runtime = Path(directory) / "runtime.json"
                    runtime.write_text(json.dumps({"url": origin + "/studio/offline/",
                                                   "player_url": origin + "/player/offline/"}))
                    client = AvatarClient(runtime, obs_output=obs)
                    try:
                        await client.prepare_session(owner)
                        body = {"owner_id": owner, **({"receiver_mode": "obs"} if obs else {})}
                        self.assertEqual(calls, [("GET", "/studio/offline/state"),
                                                ("POST", "/studio/offline/external/claim", body, origin),
                                                ("GET", "/studio/offline/state")])
                        query = parse_qs(urlsplit(client.player_url).query)
                        self.assertEqual(query["game_owner"], [owner])
                        self.assertEqual(query.get("preview"), None if obs else ["1"])
                        self.assertEqual(client.owner(), {"owner_id": owner, "generation": "g"})
                    finally:
                        await client.close()
