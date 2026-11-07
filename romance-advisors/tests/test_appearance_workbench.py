import unittest

from aiohttp.test_utils import TestClient, TestServer

from romance.server import create_app
from test_program import FakeAvatar
from test_server import SyntheticModel


class AppearanceEndpoints(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(create_app(":memory:", model=SyntheticModel(), avatar_client=FakeAvatar())))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def post(self, path, payload, code):
        reply = await self.client.post("/api/" + path, json=payload)
        body = await reply.json()
        self.assertEqual(reply.status, code, body)
        return body

    async def test_saved_bands_drive_free_rehearsal_without_enabling_paid_feature(self):
        body = {"enabled": False, "room_id": "", "positive_gift_ids": [], "negative_gift_ids": [],
                "quantity_verified": False, "band_starts": {"tired": 15, "normal": 33, "refined": 53, "stellar": 90}}
        saved = await self.post("appearance/settings", body, 200)
        self.assertEqual(saved["appearance"]["settings"]["band_starts"], body["band_starts"])
        await self.post("appearance/rehearsal", {"action": "start"}, 200)
        changed = await self.post("appearance/rehearsal", {"action": "up", "diamonds": 3}, 200)
        state = changed["appearance"]
        self.assertEqual((state["tier"], state["target"], state["score"]), ("normal", "refined", 53))
        self.assertEqual(state["transition"]["direction"], "up")
        public = await (await self.client.get("/api/public")).json()
        self.assertEqual(public["appearance"]["transition"]["id"], state["transition"]["id"])
        self.assertNotIn("settings", public["appearance"])
        await self.post("program/start", {}, 409)
        await self.post("appearance/rehearsal", {"action": "stop"}, 200)
        self.assertFalse(self.client.app["workbench"].program.client.calls)

    async def test_unverified_or_conflicting_gift_mapping_cannot_enable_live_mode(self):
        body = {"enabled": True, "room_id": "123", "positive_gift_ids": ["123"], "negative_gift_ids": ["124"],
                "quantity_verified": False}
        await self.post("appearance/settings", body, 409)
        await self.post("appearance/settings", dict(body, quantity_verified=True, negative_gift_ids=["123"]), 409)
        status = await (await self.client.get("/api/appearance")).json()
        self.assertFalse(status["appearance"]["enabled"])

    async def test_public_gift_guide_follows_saved_directions(self):
        settings = {"enabled": False, "room_id": "123", "quantity_verified": False}
        for positive, negative, visible in [
            (["463", "999"], ["164"], True), (["164"], ["463"], False), (["463"], [], False)
        ]:
            await self.post("appearance/settings", dict(settings, positive_gift_ids=positive,
                                                        negative_gift_ids=negative), 200)
            for route in ("/api/public", "/api/obs-output"):
                appearance = (await (await self.client.get(route)).json())["appearance"]
                self.assertIs(appearance["gift_guide_configured"], visible)
                self.assertNotIn("settings", appearance)

    async def test_exact_one_diamond_pair_enables_unverified_live_test(self):
        body = {"enabled": False, "room_id": "123", "positive_gift_ids": ["463"],
                "negative_gift_ids": ["164"], "quantity_verified": False}
        saved = await self.post("appearance/settings", body, 200)
        self.assertTrue(saved["appearance"]["readiness"]["can_enable_live_gifts"])
        self.assertEqual(saved["appearance"]["readiness"]["live_gift_mode"], "disabled")
        saved = await self.post("appearance/settings", dict(body, enabled=True), 200)
        state = saved["appearance"]
        self.assertFalse(state["settings"]["quantity_verified"])
        self.assertEqual(state["readiness"]["live_gift_mode"], "one_diamond_test")
        self.assertFalse(state["readiness"]["ready"])
        self.assertIn("待现场验收", state["readiness"]["message"])
        wb = self.client.app["workbench"]
        self.assertTrue(wb.appearance_requested())
        self.assertTrue(wb.appearance_gifts_enabled("123"))
        self.assertFalse(wb.appearance_gifts_enabled("456"))
        self.assertFalse(state["gift_probe"]["available"])
        self.assertFalse(wb.program.client.calls)
        public = (await (await self.client.get("/api/public")).json())["appearance"]
        self.assertNotIn("readiness", public)

    async def test_unverified_live_test_rejects_other_mappings_and_bad_rooms(self):
        body = {"enabled": True, "room_id": "123", "positive_gift_ids": ["463"],
                "negative_gift_ids": ["164"], "quantity_verified": False}
        for change in ({"positive_gift_ids": ["463", "999"]},
                       {"positive_gift_ids": ["164"], "negative_gift_ids": ["463"]},
                       {"negative_gift_ids": []}, {"room_id": ""},
                       {"room_id": "offline-room"}, {"room_id": "１２３"},
                       {"room_id": "1" * 33}):
            with self.subTest(change=change):
                await self.post("appearance/settings", dict(body, **change), 409)
        verified = await self.post("appearance/settings", dict(body, quantity_verified=True,
                                   positive_gift_ids=["999"], negative_gift_ids=["888"]), 200)
        self.assertEqual(verified["appearance"]["readiness"]["live_gift_mode"], "verified")
