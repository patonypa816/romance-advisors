import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from romance.model import COMMON, PROMPTS, Credentials, ModelError, ModelInputTooLarge, QwenModel, load_credentials, strict_json


class ModelBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.route_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.route_temp.cleanup)
        patcher = patch("romance.model.QWEN_ROUTE_FILE", Path(self.route_temp.name) / "absent.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_json_rejects_nonfinite_duplicates_and_nonobject(self):
        for value in ('{"delta":NaN}', '{"x":1,"x":2}', '[]', 'null'):
            with self.assertRaises(ValueError):
                strict_json(value)

    def test_credentials_use_csv_without_copying_or_repr(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            source = Path(directory) / "credential.csv"
            source.write_text("apiKey,sk-synthetic-only\nworkspaceId,synthetic\napiHost,synthetic.cn-beijing.maas.aliyuncs.com\n")
            value = load_credentials(source)
            self.assertTrue(value.endpoint.endswith("/compatible-mode/v1/chat/completions"))
            self.assertNotIn("sk-synthetic", repr(value))
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_credentials_reject_exfiltration_endpoint_and_newlines(self):
        for endpoint in ("https://attacker.invalid/compatible-mode/v1", "https://ok.maas.aliyuncs.com@attacker.invalid/compatible-mode/v1",
                         "https://ok.maas.aliyuncs.com/compatible-\nmode/v1"):
            with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "sk-synthetic-only", "DASHSCOPE_BASE_URL": endpoint}, clear=True):
                with self.assertRaises(ModelError) as error:
                    load_credentials()
                self.assertNotIn("sk-synthetic", str(error.exception))
                self.assertNotIn("attacker.invalid", str(error.exception))


def valid_response(**overrides):
    return {"usage": {"prompt_tokens": 3, "completion_tokens": 2},
            "choices": [{"finish_reason": "stop", "message": {"content": '{"options":[]}'}}], **overrides}


class ModelTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_uses_current_persona_guidance_and_fresh_variation_each_time(self):
        seen = []

        async def handler(request):
            seen.append(await request.json())
            return web.json_response(valid_response())

        app = web.Application()
        app.router.add_post("/model", handler)
        model = QwenModel()
        # 固定随机源而不调用真实模型，验证新建每一期都组装指引和独立灵感。
        first = Mock(choice=lambda options: options[0], randrange=lambda limit: 101)
        second = Mock(choice=lambda options: options[-1], randrange=lambda limit: 202)
        previous = {"previous_titles": ["书店角落的误会"]}
        async with TestServer(app) as server:
            try:
                with patch("romance.model.load_credentials", return_value=Credentials("synthetic-key", str(server.make_url("/model")))), \
                     patch("romance.model.random.SystemRandom", side_effect=[first, second]):
                    await model.generate("prepare", previous)
                    await model.generate("prepare", previous)
                contexts = [json.loads(request["messages"][1]["content"]) for request in seen]
                for request, context in zip(seen, contexts):
                    self.assertEqual(request["messages"][0], {"role": "system", "content": COMMON + PROMPTS["prepare"]})
                    self.assertEqual(context["previous_titles"], previous["previous_titles"])
                    self.assertEqual(set(context["variation"]), {"place", "man_style", "woman_style", "occasion", "seed"})
                    self.assertTrue(all(context["variation"][key] for key in ("place", "man_style", "woman_style", "occasion")))
                self.assertEqual([context["variation"]["seed"] for context in contexts], [101, 202])
                self.assertNotEqual(contexts[0]["variation"]["man_style"], contexts[1]["variation"]["man_style"])
                self.assertNotIn("variation", previous)
            finally:
                await model.aclose()

    async def test_many_candidates_keep_selection_pool_but_bound_explanations(self):
        seen = []

        async def handler(request):
            seen.append(await request.json())
            return web.json_response(valid_response())

        app = web.Application()
        app.router.add_post("/model", handler)
        model = QwenModel()
        proposals = [{"id": f"proposal-{n:032d}", "text": f"第{n}种回复方向"} for n in range(100)]
        async with TestServer(app) as server:
            try:
                with patch("romance.model.load_credentials", return_value=Credentials("synthetic-key", str(server.make_url("/model")))):
                    await model.generate("select", {"proposals": proposals, "assessment_limit": 99999})
                payload = json.loads(seen[0]["messages"][-1]["content"])
                self.assertEqual(payload["proposals"], proposals)
                self.assertEqual(payload["assessment_limit"], 20)
                self.assertGreaterEqual(seen[0]["max_tokens"], 4096)
                self.assertIn("最多返回输入assessment_limit条", seen[0]["messages"][0]["content"])
            finally:
                await model.aclose()

    async def test_reuses_connection_rotates_credentials_and_ignores_cookies(self):
        seen, sessions = [], []

        async def handler(request):
            seen.append((request.transport, request.headers.get("Authorization"), request.headers.get("Cookie")))
            response = web.json_response(valid_response())
            response.set_cookie("provider_cookie", "must-not-be-reused")
            return response

        def factory(**kwargs):
            session = aiohttp.ClientSession(**kwargs)
            sessions.append(session)
            return session

        app = web.Application()
        app.router.add_post("/model", handler)
        model = QwenModel(session_factory=factory)
        async with TestServer(app) as server:
            endpoint = str(server.make_url("/model"))
            try:
                with patch("romance.model.load_credentials", side_effect=[
                    Credentials("synthetic-key-one", endpoint), Credentials("synthetic-key-two", endpoint)]):
                    await model.generate("select", {"proposals": []})
                    await model.generate("select", {"proposals": []})
                self.assertEqual(len(sessions), 1)
                self.assertIs(seen[0][0], seen[1][0])
                self.assertNotEqual(seen[0][1], seen[1][1])
                self.assertEqual([row[2] for row in seen], [None, None])
                self.assertTrue(model.history[0]["connection_created"])
                self.assertTrue(model.history[1]["connection_reused"])
                self.assertNotIn("synthetic-key", json.dumps(model.history))
                self.assertNotIn(endpoint, json.dumps(model.history))
            finally:
                await model.aclose()
        self.assertTrue(sessions[0].closed)
        await model.aclose()  # Workbench shutdown can safely repeat closure.

    async def test_disconnect_does_not_retry_post_and_next_explicit_call_works(self):
        calls = 0

        async def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                request.transport.close()
                return web.Response()
            return web.json_response(valid_response())

        app = web.Application()
        app.router.add_post("/model", handler)
        model = QwenModel()
        async with TestServer(app) as server:
            with patch("romance.model.load_credentials", return_value=Credentials("synthetic-key", str(server.make_url("/model")))):
                try:
                    with self.assertRaises(ModelError):
                        await model.generate("select", {})
                    self.assertEqual(calls, 1)
                    self.assertEqual(await model.generate("select", {}), {"options": []})
                    self.assertEqual(calls, 2)
                finally:
                    await model.aclose()

    async def test_malformed_envelopes_are_format_failures_and_release_connection(self):
        invalid = [valid_response(usage=[]), valid_response(choices=[None]),
                   valid_response(choices=[{"finish_reason": "stop", "message": None}])]

        async def handler(_request):
            return web.json_response(invalid.pop(0) if invalid else valid_response())

        app = web.Application()
        app.router.add_post("/model", handler)
        model = QwenModel()
        async with TestServer(app) as server:
            with patch("romance.model.load_credentials", return_value=Credentials("synthetic-key", str(server.make_url("/model")))):
                try:
                    for _ in range(3):
                        with self.assertRaisesRegex(ModelError, "格式不正确"):
                            await model.generate("select", {})
                    self.assertEqual(await model.generate("select", {}), {"options": []})
                finally:
                    await model.aclose()

    async def test_cancelled_call_does_not_disable_session_and_close_cancels_pending(self):
        entered = asyncio.Queue()
        release = asyncio.Event()

        async def handler(request):
            data = await request.json()
            if "wait_for_test" in data["messages"][-1]["content"]:
                entered.put_nowait(True)
                await release.wait()
            return web.json_response(valid_response())

        app = web.Application()
        app.router.add_post("/model", handler)
        model = QwenModel()
        async with TestServer(app) as server:
            with patch("romance.model.load_credentials", return_value=Credentials("synthetic-key", str(server.make_url("/model")))):
                try:
                    pending = asyncio.create_task(model.generate("select", {"wait_for_test": True}))
                    await asyncio.wait_for(entered.get(), 2)
                    session = model._session
                    pending.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await pending
                    self.assertEqual(await model.generate("select", {}), {"options": []})
                    self.assertIs(model._session, session)
                    pending = asyncio.create_task(model.generate("select", {"wait_for_test": True}))
                    await asyncio.wait_for(entered.get(), 2)
                    await asyncio.wait_for(model.aclose(), 2)
                    self.assertTrue(pending.cancelled())
                    self.assertTrue(session.closed)
                finally:
                    release.set()
                    await model.aclose()

    async def test_complete_request_limit_fails_before_creating_http_session(self):
        def no_network(**_kwargs):
            self.fail("oversized input must never open a model connection")

        model = QwenModel(session_factory=no_network)
        with patch("romance.model.load_credentials", return_value=Credentials("synthetic-key", "http://127.0.0.1/unused")):
            with self.assertRaises(ModelInputTooLarge) as caught:
                await model.generate("select", {"proposals": [{"text": "汉" * 70000}]})
        self.assertGreater(caught.exception.safe_metadata["request_bytes"], 200000)
        self.assertEqual(caught.exception.safe_metadata["proposal_count"], 1)
        self.assertEqual(model.requests, 0)
        self.assertFalse(model.history[-1]["network_started"])
        self.assertNotIn("汉", json.dumps(model.history, ensure_ascii=False))
        await model.aclose()


if __name__ == "__main__":
    unittest.main()
