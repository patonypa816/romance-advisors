"""Offline stop recovery when recording evidence fails; no cloud connections."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from component_live import PersistentComponent
from live_session import LiveSession


class StopEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_stop_is_not_blocked_by_checkpoint_failure(self):
        session = object.__new__(LiveSession)
        session.stopping = False
        session._timed_action_tasks = set()
        session._prepared_comments = {}
        session.comments = []
        session.signal = asyncio.Event()
        session.checkpoint = Mock(side_effect=OSError("offline disk failure"))
        session.component = SimpleNamespace(latch_stop=Mock(), stop=AsyncMock(return_value={"state": "ended"}))
        result = await session.request_stop("offline")
        session.component.latch_stop.assert_called_once()
        session.component.stop.assert_awaited_once_with("offline")
        self.assertEqual(result["state"], "ended")

    async def test_provider_end_is_not_hidden_by_receipt_write_failure(self):
        with tempfile.TemporaryDirectory(prefix="studio-stop-offline-") as tmp:
            root = Path(tmp)
            component = PersistentComponent(None, root, root / "unused")
            component.live_id = "123"
            component.creation_attempted = True
            component.state = "ready"
            component._connect = AsyncMock()
            component._send = AsyncMock()
            component._close_ws = AsyncMock()
            component._json = AsyncMock(return_value={"live": {"status": "ended"}})
            component._collect_recording = AsyncMock()
            with patch.object(component, "_save", side_effect=OSError("offline disk failure")):
                result = await component.stop("offline")
            component._send.assert_awaited_once()
            component._json.assert_awaited_once()
            self.assertTrue(result["cloud_end_confirmed"])
            self.assertEqual(result["state"], "ended")
            await component._recording_task

    async def test_write_failure_never_forges_provider_end(self):
        with tempfile.TemporaryDirectory(prefix="studio-stop-offline-") as tmp:
            root = Path(tmp)
            component = PersistentComponent(None, root, root / "unused")
            component.live_id = "123"
            component.creation_attempted = True
            component.state = "ready"
            component.END_TIMEOUT = .01
            component.POLL_INTERVAL = .001
            component._connect = AsyncMock()
            component._send = AsyncMock()
            component._close_ws = AsyncMock()
            component._json = AsyncMock(return_value={"live": {"status": "running"}})
            with patch.object(component, "_save", side_effect=OSError("offline disk failure")):
                result = await component.stop("offline")
            self.assertFalse(result["cloud_end_confirmed"])
            self.assertEqual(result["state"], "stop_unconfirmed")
            self.assertIsNone(component._recording_task)


if __name__ == "__main__":
    unittest.main()
