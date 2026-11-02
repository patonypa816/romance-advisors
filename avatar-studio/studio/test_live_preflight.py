"""Offline checks for unresolved historical sessions; no cloud connections."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from aiohttp import web
import live_session


class FakeResponse:
    def __init__(self, status=200, live_status="ended"):
        self.status, self.live_status = status, live_status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def json(self):
        return {"live": {"status": self.live_status}}


class PreviousCloudTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="studio-preflight-offline-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.here = root / "studio"
        self.artifacts = root / "artifacts"
        self.runs = self.here / "data" / "runs"
        self.current = root / "current"
        self.current.mkdir()
        self.session = object.__new__(live_session.LiveSession)
        self.session.out = self.current
        self.session.http = Mock()
        for target, value in (("HERE", self.here), ("ARTIFACTS", self.artifacts),
                              ("PREVIOUS_COMPONENT_DIRS", ())):
            patcher = patch.object(live_session, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def receipt(self, name, value, *, marker=False):
        run = self.runs / name
        run.mkdir(parents=True)
        (run / "component-receipt.json").write_text(json.dumps(value))
        if marker:
            (run / "start-attempted.json").write_text("{}")
        return run

    async def test_confirmed_ended_and_no_session_do_not_request_history(self):
        self.receipt("ended", {"cloud_end_confirmed": True, "live_id": "retired-id"}, marker=True)
        self.receipt("rejected", {"no_cloud_session": True}, marker=True)
        self.session.assert_previous_cloud_ended()
        await self.session.verify_previous_cloud_ended("offline-key")
        self.session.http.get.assert_not_called()
        self.assertEqual(json.loads((self.current / "preflight-previous-ended.json").read_text()), [])

    async def test_unconfirmed_id_is_checked_and_only_ended_passes(self):
        self.receipt("unresolved", {"cloud_end_confirmed": False, "live_id": "123"})
        self.session.http.get.return_value = FakeResponse(live_status="ended")
        await self.session.verify_previous_cloud_ended("offline-key")
        self.session.http.get.assert_called_once()
        self.assertEqual(json.loads((self.current / "preflight-previous-ended.json").read_text()),
                         [{"live_id": "123", "status": "ended"}])
        self.session.http.get.return_value = FakeResponse(live_status="running")
        with self.assertRaises(web.HTTPConflict):
            await self.session.verify_previous_cloud_ended("offline-key")

    async def test_unknown_or_missing_id_never_silently_passes(self):
        self.receipt("unknown", {"creation_outcome_unknown": True, "cloud_end_confirmed": True})
        with self.assertRaises(web.HTTPConflict):
            await self.session.verify_previous_cloud_ended("offline-key")
        self.session.http.get.assert_not_called()
        (self.runs / "unknown" / "component-receipt.json").write_text('{}')
        with self.assertRaises(web.HTTPConflict):
            await self.session.verify_previous_cloud_ended("offline-key")
        self.session.http.get.assert_not_called()

    async def test_known_unresolved_marker_reaches_readback(self):
        self.receipt("unresolved", {"live_id": "123", "cloud_end_confirmed": False}, marker=True)
        self.session.assert_previous_cloud_ended()
        self.session.http.get.return_value = FakeResponse(live_status="ended")
        await self.session.verify_previous_cloud_ended("offline-key")
        self.session.http.get.assert_called_once()

    async def test_unknown_missing_or_invalid_marker_still_blocks_locally(self):
        run = self.receipt("unresolved", {}, marker=True)
        for value in ({}, {"live_id": "invalid-id"},
                      {"live_id": "123", "creation_outcome_unknown": True}):
            with self.subTest(value=value):
                (run / "component-receipt.json").write_text(json.dumps(value))
                with self.assertRaises(web.HTTPConflict):
                    self.session.assert_previous_cloud_ended()
        self.session.http.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
