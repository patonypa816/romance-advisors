"""Offline HTTP contracts: synthetic credentials only, no cloud or avatar calls."""
import asyncio
import copy
import json
import unittest
from unittest import mock

import aiohttp
from aiohttp import web

try:
    from . import bailian_ai as module
    from . import cloudbase_ai as shared
    from .test_cloudbase_ai import COMMENT, BATCH, CONTEXT, PLAN, OPERATOR, MOTION, TOPIC
    from .test_timed_action_planner import PART, ALIGNMENT, RESULT
except ImportError:
    import bailian_ai as module
    import cloudbase_ai as shared
    from test_cloudbase_ai import COMMENT, BATCH, CONTEXT, PLAN, OPERATOR, MOTION, TOPIC
    from test_timed_action_planner import PART, ALIGNMENT, RESULT

KEY = "sk-synthetic-offline-not-a-real-key-12345"
CONFIG = {"model": module.MODEL, "region": module.REGION, "enable_thinking": False}
SELECTION = {"selected_id": "c1", "reason": "正常质疑可回应"}


def completion(value=SELECTION):
    return {"id": "offline-request-1", "model": module.MODEL,
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(value)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}


class Content:
    def __init__(self, raw, *, block=False):
        self.raw = raw
        self.offset = 0
        self.block = block
        self.entered = asyncio.Event()
        self.read_sizes = []

    async def read(self, size):
        self.entered.set()
        self.read_sizes.append(size)
        if self.block:
            await asyncio.Event().wait()
        result = self.raw[self.offset:self.offset + size]
        self.offset += len(result)
        return result


class Response:
    def __init__(self, body=None, *, raw=None, status=200, block=False, declared=None):
        self.status = status
        self.content = Content(raw if raw is not None else json.dumps(body or completion()).encode(), block=block)
        self.content_length = declared
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class Session:
    def __init__(self, factory, kwargs):
        self.factory = factory
        self.kwargs = kwargs
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def post(self, endpoint, **kwargs):
        self.factory.calls.append((endpoint, kwargs))
        result = self.factory.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class Factory:
    def __init__(self, *responses):
        self.responses = list(responses or [Response()])
        self.calls = []
        self.sessions = []

    def __call__(self, **kwargs):
        session = Session(self, kwargs)
        self.sessions.append(session)
        return session


class BailianTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()

    def client(self, *responses, loader=None, config=None):
        factory = Factory(*responses)
        loader = loader or mock.Mock(return_value=(KEY, "offline-workspace"))
        client = module.BailianAI(CONFIG if config is None else config,
                                  credentials_loader=loader, session_factory=factory)
        self.clients.append(client)
        return client, factory, loader

    async def assert_error(self, response, code):
        client, factory, _ = self.client(response)
        with self.assertRaises(module.ModelError) as caught:
            await client.select_comments(BATCH, CONTEXT)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(client.last_error, code)
        self.assertEqual(len(factory.calls), 1)
        self.assertTrue(factory.sessions[0].closed)
        self.assertNotIn(KEY, repr(caught.exception))
        self.assertNotIn(KEY, json.dumps(client.public()))
        return client

    async def test_lazy_constructor_fixed_http_parameters_and_local_public_status(self):
        client, factory, loader = self.client()
        loader.assert_not_called()
        self.assertEqual(client.public()["provider"], "bailian")
        self.assertTrue(client.configured())
        self.assertFalse(factory.calls)
        self.assertEqual(await client.select_comments(BATCH, CONTEXT), SELECTION)
        endpoint, kwargs = factory.calls[0]
        self.assertEqual(endpoint, "https://offline-workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/chat/completions")
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer " + KEY, "Content-Type": "application/json"})
        payload = json.loads(kwargs["data"])
        self.assertEqual(payload["model"], module.MODEL)
        self.assertIs(payload["enable_thinking"], False)
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["max_tokens"], 512)
        self.assertNotIn("extra_body", payload)
        self.assertNotIn("tools", payload)
        self.assertEqual([m["role"] for m in payload["messages"]], ["system", "user"])
        self.assertIs(factory.sessions[0].kwargs["trust_env"], False)
        self.assertEqual(factory.sessions[0].kwargs["timeout"].total, 45)
        self.assertEqual(client.usage["selection"]["total_tokens"], 15)
        self.assertFalse(client._active)

    async def test_configuration_and_workspace_injection_fail_before_http(self):
        for config in ({}, {**CONFIG, "model": "qwen3-flash"}, {**CONFIG, "region": "ap-singapore"},
                       {**CONFIG, "enable_thinking": True}, {**CONFIG, "enable_thinking": 0}):
            client, factory, loader = self.client(config=config)
            self.assertFalse(client.configured())
            with self.assertRaises(module.ModelError) as caught:
                await client.select_comments(BATCH, CONTEXT)
            self.assertEqual(caught.exception.code, "configuration_invalid")
            self.assertFalse(factory.calls)
            loader.assert_not_called()
        for workspace in ("user@host", "foo.bar", "foo/evil", "foo:80", "a?x", "-foo", "foo-", "a" * 64, ""):
            client, factory, _ = self.client(loader=lambda: (KEY, workspace))
            self.assertFalse(client.configured())
            with self.assertRaises(module.ModelError):
                await client.select_comments(BATCH, CONTEXT)
            self.assertFalse(factory.calls)

    async def test_credentials_hot_reload_and_loader_errors_are_sanitized(self):
        rotated = KEY + "-rotated"
        loader = mock.Mock(side_effect=[(KEY, "offline"), (rotated, "offline")])
        client, factory, _ = self.client(Response(), Response(), loader=loader)
        await client.select_comments(BATCH, CONTEXT)
        await client.select_comments(BATCH, CONTEXT)
        self.assertEqual([call[1]["headers"]["Authorization"] for call in factory.calls],
                         ["Bearer " + KEY, "Bearer " + rotated])
        for value in (("bad", "offline"), (KEY + "\n", "offline"), None):
            client, factory, _ = self.client(loader=lambda: value)
            with self.assertRaises(module.ModelError) as caught:
                await client.select_comments(BATCH, CONTEXT)
            self.assertEqual(str(caught.exception), "credential_invalid")
            self.assertFalse(factory.calls)
        client, factory, _ = self.client(loader=mock.Mock(side_effect=RuntimeError(KEY)))
        with self.assertRaises(module.ModelError) as caught:
            await client.select_comments(BATCH, CONTEXT)
        self.assertNotIn(KEY, repr(caught.exception))
        self.assertFalse(factory.calls)

    async def test_http_and_network_errors_are_single_attempt_and_safe(self):
        for status, code in ((301, "provider_error"), (401, "unauthorized"), (403, "forbidden"),
                             (404, "model_not_found"), (408, "timeout"), (429, "rate_limited"), (500, "provider_error")):
            client = await self.assert_error(Response(raw=KEY.encode(), status=status), code)
            self.assertEqual(client.usage["selection"]["unverified_requests"], 1)
        await self.assert_error(aiohttp.ClientConnectionError(KEY), "transport_error")

    async def test_envelope_model_choices_content_finish_and_json_validation(self):
        await self.assert_error(Response(raw=b"{not json"), "response_invalid_json")
        variants = []
        for change, code in (({"model": "qwen3-flash"}, "model_mismatch"), ({"model": None}, "model_mismatch"),
                             ({"choices": []}, "response_invalid"), ({"choices": [None]}, "response_invalid"),
                             ({"error": {"message": KEY}}, "provider_error")):
            variants.append(({**completion(), **change}, code))
        for reason, code in (("length", "output_truncated"), ("tool_calls", "output_unfinished"), (None, "output_unfinished")):
            body = completion()
            body["choices"][0]["finish_reason"] = reason
            variants.append((body, code))
        for content, code in ((None, "content_missing"), ("", "content_missing"), ("[]", "output_invalid_json"),
                              ('{"x":NaN}', "output_invalid_json"), ('{"x":1,"x":2}', "output_invalid_json"),
                              ('```json\n{}\n```', "output_invalid_json"),
                              (json.dumps({"selected_id": None, "reason": KEY}), "sensitive_output"),
                              (json.dumps({"x": KEY}).replace("s", "\\u0073"), "sensitive_output")):
            body = completion()
            body["choices"][0]["message"]["content"] = content
            variants.append((body, code))
        for body, code in variants:
            await self.assert_error(Response(body), code)

    async def test_usage_missing_invalid_and_truncated_are_metered(self):
        for usage in ([], {"total_tokens": True}, {"prompt_tokens": -1}, {"completion_tokens": 1.2}):
            client = await self.assert_error(Response({**completion(), "usage": usage}), "usage_invalid")
            self.assertEqual(client.usage["selection"]["unverified_requests"], 1)
        client, _, _ = self.client(Response({**completion(), "usage": None}))
        await client.select_comments(BATCH, CONTEXT)
        self.assertEqual(client.usage["selection"]["unverified_requests"], 1)
        body = completion()
        body["choices"][0]["finish_reason"] = "length"
        client = await self.assert_error(Response(body), "output_truncated")
        self.assertEqual(client.usage["selection"]["total_tokens"], 15)

    async def test_input_and_streamed_response_byte_limits(self):
        client, factory, _ = self.client()
        with self.assertRaises(module.ModelError) as caught:
            await client._call("reply", [{"role": "user", "content": "字" * module.MAX_INPUT_BYTES}])
        self.assertEqual(caught.exception.code, "input_too_large")
        self.assertFalse(factory.calls)
        for response in (Response(raw=b"x", declared=module.MAX_RESPONSE_BYTES + 1),
                         Response(raw=b"x" * (module.MAX_RESPONSE_BYTES + 1))):
            await self.assert_error(response, "response_too_large")
            self.assertTrue(all(0 < size <= 16384 for size in response.content.read_sizes))

    async def test_timeout_cancel_and_close_release_inflight_requests(self):
        for mode in ("timeout", "cancel", "close"):
            response = Response(block=True)
            client, factory, _ = self.client(response)
            with mock.patch.object(module, "REQUEST_TIMEOUT_SECONDS", .02 if mode == "timeout" else 45):
                task = asyncio.create_task(client.select_comments(BATCH, CONTEXT))
                await response.content.entered.wait()
                if mode == "cancel":
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                    self.assertEqual(client.last_error, "cancelled")
                else:
                    if mode == "close":
                        await client.close()
                    with self.assertRaises(module.ModelError) as caught:
                        await task
                    self.assertEqual(caught.exception.code, "timeout" if mode == "timeout" else "client_closed")
            self.assertTrue(response.closed)
            self.assertTrue(factory.sessions[0].closed)
            self.assertFalse(client._active)
            self.assertEqual(len(factory.calls), 1)
            self.assertEqual(client.usage["selection"]["unverified_requests"], 1)
            if mode == "close":
                self.assertFalse(client.configured())
                with self.assertRaises(module.ModelError):
                    await client.select_comments(BATCH, CONTEXT)
                self.assertEqual(len(factory.calls), 1)

    async def test_close_before_inner_task_starts_does_not_leave_active_task(self):
        client, _, _ = self.client()
        task = asyncio.create_task(client.select_comments(BATCH, CONTEXT))
        await asyncio.sleep(0)
        await client.close()
        with self.assertRaises(module.ModelError) as caught:
            await task
        self.assertEqual(caught.exception.code, "client_closed")
        self.assertFalse(client._active)

    async def test_all_business_routes_use_shared_validation_over_http(self):
        client, factory, _ = self.client(*(Response(completion(value)) for value in
                                          (PLAN, PLAN, PLAN, MOTION, RESULT)))
        self.assertEqual(await client.generate_reply(COMMENT, [], {}), PLAN)
        self.assertEqual(await client.generate_director(OPERATOR, [], {}), PLAN)
        self.assertEqual(await client.generate_director({**OPERATOR, "operator_mode": "topic"}, [], {}, topic=TOPIC), PLAN)
        self.assertEqual(await client.generate_director({**OPERATOR, "operator_mode": "motion"}, [], {}), MOTION)
        result = await client.plan_timed_actions(PART, ALIGNMENT, 13, {})
        self.assertEqual(result["planning"]["schedule_id"], "s01")
        self.assertEqual(client.requests, {"selection": 0, "reply": 5})
        for _, kwargs in factory.calls:
            self.assertEqual(json.loads(kwargs["data"])["max_tokens"], 4096)
        invalid = copy.deepcopy(shared.SHORT_PERFORMANCE_EXAMPLE)
        invalid["segments"][0]["actions"][0]["delay_seconds"] = 2
        client, _, _ = self.client(Response(completion(invalid)))
        with self.assertRaises(module.ModelError) as caught:
            await client.generate_reply(COMMENT, [], {"short_performance_test": True})
        self.assertEqual(caught.exception.code, "plan_invalid")
        self.assertIs(module.BailianAI.__bases__[0], shared.StructuredBrain)
        self.assertNotIsInstance(client, shared.CloudBaseAI)

    async def test_real_aiohttp_local_server_reads_parameters_and_closes_sockets(self):
        # Only this test adapter rewrites the verified HTTPS endpoint to an
        # ephemeral loopback server. Production has no endpoint override.
        seen = []
        async def handler(request):
            seen.append((request.headers["Authorization"], await request.json()))
            return web.json_response(completion())
        app = web.Application()
        app.router.add_post("/compatible-mode/v1/chat/completions", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        sessions = []
        class LocalSession:
            def __init__(self, **kwargs):
                self.session = aiohttp.ClientSession(**kwargs)
                sessions.append(self.session)
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                await self.session.close()
            def post(self, endpoint, **kwargs):
                return self.session.post(f"http://127.0.0.1:{port}/compatible-mode/v1/chat/completions", **kwargs)
        client = module.BailianAI(CONFIG, credentials_loader=lambda: (KEY, "offline"), session_factory=LocalSession)
        self.clients.append(client)
        try:
            self.assertEqual(await client.select_comments(BATCH, CONTEXT), SELECTION)
            self.assertEqual(seen[0][0], "Bearer " + KEY)
            self.assertIs(seen[0][1]["enable_thinking"], False)
            self.assertTrue(all(s.closed for s in sessions))
        finally:
            await runner.cleanup()


if __name__ == "__main__":
    unittest.main()
