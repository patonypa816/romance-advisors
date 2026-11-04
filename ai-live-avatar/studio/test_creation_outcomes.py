"""Creation fault contracts: fake HTTP only; no credentials or cloud traffic."""
import asyncio
import errno
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import aiohttp

from component_live import PersistentComponent


class FakeResponse:
    def __init__(self, status=200, value=None, *, error=None, entered=None, release=None):
        self.status, self.value, self.error = status, value, error
        self.entered, self.release = entered, release

    async def __aenter__(self):
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self, **kwargs):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeHTTP:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("unexpected HTTP request or creation retry")
        return self.responses.pop(0)

    def __getattr__(self, name):
        raise AssertionError(f"offline test forbids unplanned HTTP operation: {name}")


def connector_failure():
    return aiohttp.ClientConnectorError(
        SimpleNamespace(host="offline.invalid", port=443, ssl=True),
        OSError(errno.ECONNREFUSED, "offline connection refused"),
    )


class CreationOutcomeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="creation-outcome-offline-")
        self.addCleanup(self.temp.cleanup)
        self.count = 0

    def component(self, response, *later_responses):
        out = Path(self.temp.name) / str(self.count)
        self.count += 1
        out.mkdir()
        image = out / "input.png"
        image.write_bytes(b"offline-image-fixture")
        http = FakeHTTP([response, *later_responses])
        return PersistentComponent(http, out, image), http

    async def run_failure(self, response):
        c, http = self.component(response)
        await c.start("offline-key", avatar_id="123")
        await asyncio.wait_for(c._task, 1)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0][0], "POST")
        self.assertIs(http.calls[0][2]["allow_redirects"], False)
        self.assertTrue(c._creation_done.is_set())
        self.assertTrue(c.public()["creation_attempted"])
        self.assertIsNone(c.live_id)
        self.assertFalse(c.cloud_end_confirmed)
        with self.assertRaises(RuntimeError):
            await c.start("offline-key", avatar_id="123")
        self.assertEqual(len(http.calls), 1)
        receipt = json.loads((c.out / "component-receipt.json").read_text())
        self.assertNotIn("offline-key", json.dumps(receipt))
        return c, receipt

    async def test_connector_failure_is_definitively_not_submitted(self):
        c, receipt = await self.run_failure(FakeResponse(error=connector_failure()))
        self.assertEqual(receipt["state"], "error")
        self.assertTrue(receipt["creation_not_submitted"])
        self.assertTrue(receipt["no_cloud_session"])
        self.assertFalse(receipt["creation_outcome_unknown"])
        event = next(row for row in c.diagnostics.records if row["kind"] == "create_not_submitted")
        self.assertEqual(event["details"]["error_type"], "ClientConnectorError")

    async def test_ambiguous_transport_and_response_failures_stay_unknown(self):
        responses = [
            FakeResponse(error=aiohttp.ServerDisconnectedError()),
            FakeResponse(error=aiohttp.ClientOSError(errno.ECONNRESET, "offline reset")),
            FakeResponse(error=asyncio.TimeoutError()),
            FakeResponse(value=ValueError("offline malformed response")),
            FakeResponse(value={"live": {}}),
            FakeResponse(status=503),
        ]
        for response in responses:
            with self.subTest(error=type(response.error or response.value).__name__, status=response.status):
                _, receipt = await self.run_failure(response)
                self.assertEqual(receipt["state"], "creation_unknown")
                self.assertTrue(receipt["creation_outcome_unknown"])
                self.assertFalse(receipt["creation_not_submitted"])
                self.assertFalse(receipt["no_cloud_session"])

    async def test_definitive_4xx_rejection_keeps_no_cloud_session(self):
        for status in (400, 401, 409, 429):
            with self.subTest(status=status):
                _, receipt = await self.run_failure(FakeResponse(status=status))
                self.assertEqual(receipt["state"], "error")
                self.assertTrue(receipt["no_cloud_session"])
                self.assertFalse(receipt["creation_outcome_unknown"])
                self.assertFalse(receipt["creation_not_submitted"])

    async def test_302_is_not_followed_or_misclassified_as_connection_failure(self):
        # A redirect response does not prove the first server created nothing.
        _, receipt = await self.run_failure(FakeResponse(status=302))
        self.assertEqual(receipt["state"], "creation_unknown")
        self.assertTrue(receipt["creation_outcome_unknown"])
        self.assertFalse(receipt["no_cloud_session"])

    async def test_stop_during_create_retains_returned_id_and_confirms_end(self):
        entered, release = asyncio.Event(), asyncio.Event()
        created = {"live": {"id": "123"}, "managed_rtc": {"token": "offline-rtc", "user_id": "viewer"}}
        c, http = self.component(
            FakeResponse(value=created, entered=entered, release=release),
            FakeResponse(value={"live": {"id": "123", "status": "ended"}}),
        )
        c._connect = AsyncMock()
        c._send = AsyncMock(return_value=True)
        c._collect_recording = AsyncMock()
        await c.start("offline-key", avatar_id="123")
        await asyncio.wait_for(entered.wait(), 1)
        stopped = asyncio.create_task(c.stop())
        # The stop latch must be visible while the create response is held.
        await asyncio.sleep(0)
        self.assertTrue(c._stop.is_set())
        self.assertFalse(stopped.done())
        self.assertFalse(c._creation_done.is_set())
        release.set()
        await asyncio.wait_for(asyncio.gather(c._task, stopped), 1)
        if c._recording_task:
            await c._recording_task
        self.assertEqual([call[0] for call in http.calls], ["POST", "GET"])
        self.assertTrue(all(call[2]["allow_redirects"] is False for call in http.calls))
        self.assertEqual(c.live_id, "123")
        self.assertEqual(c.state, "ended")
        self.assertTrue(c.cloud_end_confirmed)
        self.assertFalse(c.creation_unknown)
        self.assertFalse(c.public()["no_cloud_session"])
        c._send.assert_awaited_once_with(5, {"hangup": {"hangup_reason": "client_hangup"}}, stopping=True)

    async def test_stop_during_connector_failure_does_not_invent_cloud_session(self):
        entered, release = asyncio.Event(), asyncio.Event()
        c, http = self.component(FakeResponse(error=connector_failure(), entered=entered, release=release))
        await c.start("offline-key", avatar_id="123")
        await asyncio.wait_for(entered.wait(), 1)
        stopped = asyncio.create_task(c.stop())
        await asyncio.sleep(0)
        self.assertFalse(stopped.done())
        release.set()
        await asyncio.wait_for(asyncio.gather(c._task, stopped), 1)
        self.assertEqual(len(http.calls), 1)
        self.assertTrue(c.public()["no_cloud_session"])
        self.assertFalse(c.creation_unknown)
        self.assertIsNone(c.live_id)


if __name__ == "__main__":
    unittest.main()
