"""Synthetic source packets only: the probe never enables live scoring or a sender."""
import asyncio
import copy
import json
import unittest

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

from romance.appearance import AppearanceLedger
from romance.appearance_gift_probe import GiftQuantityProbe
from romance.server import create_app
from test_program import FakeAvatar
from test_server import SyntheticModel


CONFIG = {"enabled": False, "room_id": "123", "positive_gift_ids": ["463"],
          "negative_gift_ids": ["164"], "quantity_verified": False}


def event(ident="packet", gift_id="463", count=10, group="PRIVATE_COMBO", **updates):
    body = {"type": "gift", "platform": "douyin", "roomId": "123", "id": ident,
            "ts": 1000000, "receivedAt": 1000000, "user": {"id": "PRIVATE_VIEWER"},
            "data": {"giftId": gift_id, "giftName": "Untrusted name", "diamondCount": 1,
                     "repeatCount": count, "groupId": group, "sourceTimeKnown": True}}
    body.update(updates)
    return body


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.probe = GiftQuantityProbe(clock=lambda: 1000)
        self.probe.begin("123", started_at=999)

    def test_matches_game_ledger_for_two_ids_combo_replay_and_opposite_direction(self):
        ledger = AppearanceLedger({"463": 1, "164": -1}, room_id="123")
        packets = [event(str(i), count=i) for i in range(1, 11)]
        packets += [event("10", count=10), event("end", count=10), event("old", count=8)]
        packets += [event("negative", gift_id="164", group="negative"),
                    event("negative-end", gift_id="164", group="negative")]
        for packet in packets:
            raw = copy.deepcopy(packet)
            receipt = self.probe.apply(packet)
            expected = ledger.apply(dict(packet, simulated=True))
            self.assertEqual((receipt["score"], receipt["delta"], receipt["reason"]),
                             (expected["score"], expected["delta"], expected["reason"]))
            self.assertEqual(packet, raw)  # Real packets are never relabelled simulated.
        state = self.probe.snapshot()
        self.assertEqual(state["score"], 50)
        self.assertEqual(state["accepted_count"], 20)
        self.assertEqual([row["name"] for row in state["gifts"]], ["小心心", "抖音"])
        self.assertNotIn("PRIVATE", json.dumps(state))

    def test_invalid_source_time_context_price_and_group_cannot_count(self):
        packets = [event(simulated=True), event(roomId="456"), event(ts=None),
                   event(ts=900000), event(receivedAt=float("nan")), event(ts=1006000),
                   event(user={"id": ""}), event(gift_id="unknown")]
        for key, value in [("sourceTimeKnown", False), ("diamondCount", 2), ("diamondCount", True),
                           ("diamondCount", None), ("repeatCount", 0), ("repeatCount", True),
                           ("repeatCount", 1.5), ("groupId", "0"), ("groupId", None)]:
            packet = event()
            packet["data"][key] = value
            packets.append(packet)
        for packet in packets:
            self.assertFalse(self.probe.apply(packet)["accepted"], packet)
        self.assertEqual(self.probe.snapshot()["state"], "waiting")
        self.assertEqual(self.probe.snapshot()["accepted_count"], 0)
        self.assertTrue(self.probe.apply(event())["accepted"])
        self.assertEqual(self.probe.score, 60)

    def test_resetting_sample_on_same_connection_does_not_reconsume_existing_combo(self):
        self.probe.apply(event("first", count=10))
        self.probe.reset_sample()
        receipt = self.probe.apply(event("end", count=10))
        self.assertEqual((receipt["score"], receipt["delta"]), (50, 0))
        self.assertEqual(self.probe.apply(event("eleventh", count=11))["score"], 51)

    def test_per_viewer_per_combo_and_reset_have_separate_high_water_marks(self):
        self.probe.apply(event(count=10))
        self.probe.apply(event("other-combo", count=10, group="other"))
        self.probe.apply(event("other-viewer", count=10, user={"id": "other-viewer"}))
        self.assertEqual(self.probe.score, 80)
        self.probe.begin("123", started_at=1001)
        self.assertEqual(self.probe.score, 50)
        self.assertEqual(self.probe.apply(event())["reason"], "stale_gift")
        self.probe.stop()
        self.assertEqual(self.probe.apply(event())["reason"], "probe_inactive")


class HoldClient:
    def __init__(self, base_url, room_id, on_event, on_status, **kwargs):
        self.base_url, self.room_id = base_url, room_id
        self.event_types = kwargs["event_types"]

    async def run(self, stop):
        await stop.wait()

    async def close(self):
        pass


class ProbeIntakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.avatar = FakeAvatar()
        self.app = create_app(":memory:", model=SyntheticModel(), avatar_client=self.avatar)
        self.client = TestClient(TestServer(self.app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.intake = self.app["dyhub_intake"]
        self.intake.client_factory = HoldClient
        self.intake.clock = lambda: 999
        self.wb = self.app["workbench"]
        self.wb.appearance.gift_probe.clock = lambda: 1000
        reply = await self.client.post("/api/appearance/settings", json=CONFIG)
        self.assertEqual(reply.status, 200)

    async def start(self, room="123"):
        response = await self.client.post("/api/source/start", json={
            "base_url": "http://127.0.0.1:8757", "room_id": room, "scope": "broadcast"})
        self.assertEqual(response.status, 200)

    async def test_matching_subscription_real_packet_bypass_no_game_write_or_sender(self):
        before = copy.deepcopy(self.wb.game.data)
        await self.start()
        self.assertEqual(self.intake.client.event_types, ("chat", "gift"))
        status = await (await self.client.get("/api/appearance")).json()
        self.assertEqual(status["appearance"]["gift_probe"]["state"], "waiting")
        for packet in [event(), event("negative", gift_id="164", group="negative")]:
            self.assertTrue((await self.intake.on_event(packet))["accepted"])
        state = (await (await self.client.get("/api/appearance")).json())["appearance"]
        self.assertEqual(state["gift_probe"]["score"], 50)
        self.assertEqual(state["gift_probe"]["accepted_count"], 20)
        self.assertFalse(state["settings"]["quantity_verified"])
        self.assertFalse(state["settings"]["enabled"])
        self.assertEqual(self.wb.game.data, before)
        self.assertEqual(self.avatar.calls, [])
        public = await (await self.client.get("/api/public")).json()
        self.assertNotIn("gift_probe", public["appearance"])
        reset = await self.client.post("/api/appearance/gift-probe", json={})
        self.assertEqual(reset.status, 200)
        self.assertEqual((await reset.json())["appearance"]["gift_probe"]["accepted_count"], 0)
        await self.client.post("/api/source/stop", json={})
        self.assertFalse(self.wb.appearance.gift_probe.active)
        self.assertIsNone(await self.intake.on_event(event("after-stop")))
        self.assertEqual((await self.client.post("/api/appearance/gift-probe", json={})).status, 409)

    async def test_wrong_room_gets_chat_only_and_cannot_start_probe(self):
        await self.start("456")
        self.assertEqual(self.intake.client.event_types, ("chat",))
        self.assertFalse(self.wb.appearance.gift_probe.active)
        self.assertEqual((await self.client.post("/api/appearance/gift-probe", json={})).status, 409)

    async def test_verified_expensive_configuration_keeps_normal_gift_subscription(self):
        config = dict(CONFIG, enabled=True, quantity_verified=True,
                      positive_gift_ids=["99999"], negative_gift_ids=["88888"])
        response = await self.client.post("/api/appearance/settings", json=config)
        self.assertEqual(response.status, 200)
        await self.start()
        self.assertEqual(self.intake.client.event_types, ("chat", "gift"))
        self.assertFalse(self.wb.appearance.gift_probe.active)

    async def test_local_sse_uses_real_parser_filters_wrong_room_and_keeps_probe_private(self):
        self.intake.client_factory = None
        release = asyncio.Event()
        queries = []

        async def stream(request):
            queries.append(dict(request.query))
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            for packet in [event(roomId="456"), event(), event("end")]:
                await response.write(("data: " + json.dumps(packet) + "\n\n").encode())
            await release.wait()
            return response

        source_app = web.Application()
        source_app.router.add_get("/api/events", stream)
        source = TestServer(source_app)
        await source.start_server()
        try:
            response = await self.client.post("/api/source/start", json={
                "base_url": str(source.make_url("")).rstrip("/"), "room_id": "123", "scope": "broadcast"})
            self.assertEqual(response.status, 200)
            async def received():
                while self.wb.appearance.gift_probe.reason != "duplicate_progress":
                    await asyncio.sleep(.01)
            await asyncio.wait_for(received(), 2)
            self.assertEqual(queries, [{"roomId": "123", "types": "chat,gift,roomStatus"}])
            self.assertEqual(self.wb.appearance.gift_probe.score, 60)
            self.assertEqual(self.wb.appearance.gift_probe.snapshot()["accepted_count"], 10)
            self.assertEqual(self.wb.game.data["comments"], [])
            self.assertFalse(self.wb.appearance.service.settings()["quantity_verified"])
        finally:
            await self.intake.stop()
            release.set()
            await source.close()
