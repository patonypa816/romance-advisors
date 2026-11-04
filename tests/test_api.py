import os
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer
from hell_inc.app import create_app
from hell_inc.config import Settings


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(database_url=os.environ["HELL_INC_DATABASE_URL"],
                                 world_id="test-" + uuid4().hex, origin="http://localhost",
                                 lock_dir=Path(self.tmp.name))
        self.token = "isolated-test-token-" + uuid4().hex
        self.client = await self.make_client(self.token)
        self.addAsyncCleanup(self.client.close)

    async def make_client(self, token):
        client = TestClient(TestServer(create_app(self.settings, startup_token=token)),
                            cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        return client

    async def login(self):
        response = await self.client.post("/api/session", json={"token": self.token},
                                          headers={"Origin": "http://localhost"})
        self.assertEqual(response.status, 200)
        return response

    def payload(self, **changes):
        return {"command_id": str(uuid4()), "provider": "simulated", "identity_scope": "local-test",
                "subject_id": "visitor-001", "display_name": "小林", **changes}

    async def test_auth_cookie_one_use_and_refresh(self):
        self.assertEqual(await (await self.client.get("/api/session")).json(), {"authenticated": False})
        self.assertEqual((await self.client.get("/api/players")).status, 401)
        response = await self.login()
        cookie = response.cookies["hell_inc_session"]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"].lower(), "strict")
        self.assertEqual(await (await self.client.get("/api/session")).json(), {"authenticated": True})
        again = await self.client.post("/api/session", json={"token": self.token}, headers={"Origin": "http://localhost"})
        self.assertIn(again.status, (401, 403))
        self.assertEqual((await self.client.get("/api/players")).status, 200)

    async def test_csrf_denied_even_with_authenticated_cookie(self):
        await self.login()
        for headers in ({}, {"Origin": "https://evil.example"}, {"Origin": "null"},
                        {"Origin": "http://localhost.evil.example"}):
            response = await self.client.post("/api/players", json=self.payload(), headers=headers)
            self.assertEqual(response.status, 403)
        self.assertEqual((await (await self.client.get("/api/players")).json())["players"], [])

    async def test_register_replay_conflict_validation_and_no_asset_endpoint(self):
        await self.login()
        data = self.payload()
        async def post(body):
            return await self.client.post("/api/players", json=body, headers={"Origin": "http://localhost"})
        first = await post(data)
        self.assertIn(first.status, (200, 201))
        body = await first.json()
        repeat = await post(data)
        self.assertEqual(await repeat.json(), {**body, "replayed": True})
        self.assertEqual((await post({**data, "display_name": "冲突"})).status, 409)
        for change in ({"performance": 999}, {"role": "employee"}, {"subject_id": 12}, {"subject_id": " padded"}):
            bad = await post(self.payload(**change))
            self.assertEqual(bad.status, 400)
            self.assertEqual(set((await bad.json())["error"]), {"code", "message"})
        mutation = await self.client.patch(f'/api/players/{body["player"]["player_id"]}',
                                          json={"performance": 999}, headers={"Origin": "http://localhost"})
        self.assertIn(mutation.status, (404, 405))
        missing = await self.client.get("/api/players/" + str(uuid4()))
        self.assertEqual(missing.status, 404)

    async def test_app_restart_keeps_data_but_not_session(self):
        await self.login()
        response = await self.client.post("/api/players", json=self.payload(), headers={"Origin": "http://localhost"})
        original = await response.json()
        cookies = self.client.session.cookie_jar.filter_cookies(self.client.make_url("/"))
        await self.client.close()
        self.token = "new-test-token-" + uuid4().hex
        self.client = await self.make_client(self.token)
        self.addAsyncCleanup(self.client.close)
        self.client.session.cookie_jar.update_cookies(cookies)
        self.assertEqual((await self.client.get("/api/players")).status, 401)
        await self.login()
        current = await (await self.client.get("/api/players/" + original["player"]["player_id"])).json()
        self.assertEqual(current["player"], original["player"])
        self.assertEqual(current["revision"], original["revision"])

    async def test_health_reports_actual_world_and_html_is_not_reflected(self):
        health = await (await self.client.get("/api/health")).json()
        self.assertEqual(health, {"status": "ok", "mode": "local-test", "world_id": self.settings.world_id})
        response = await self.client.get('/does-not-exist?name=%3Cscript%3Ealert(1)%3C/script%3E')
        self.assertNotIn("<script>alert(1)</script>", await response.text())
