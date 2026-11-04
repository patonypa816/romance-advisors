"""真实状态来源与公开投影的离线回归；不会访问云端或模型。"""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
import live_session
from live_session import LiveSession


class PreviousCloudContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old = self.root / "data/runs/old"
        self.old.mkdir(parents=True)
        self.session = LiveSession.__new__(LiveSession)
        self.session.out = self.root / "new"
        self.session.out.mkdir()
        self.session.http = SimpleNamespace(get=Mock(side_effect=AssertionError("unexpected network read")))
        self.patches = [patch.object(live_session, "HERE", self.root),
                        patch.object(live_session, "ARTIFACTS", self.root / "artifacts"),
                        patch.object(live_session, "PREVIOUS_COMPONENT_DIRS", ())]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.tmp.cleanup()

    def receipt(self, **values):
        (self.old / "component-receipt.json").write_text(json.dumps({"live_id": "123456", **values}))
        (self.old / "start-attempted.json").write_text("{}")

    async def test_confirmed_terminal_needs_no_historical_network(self):
        self.receipt(cloud_end_confirmed=True)
        self.session.assert_previous_cloud_ended()
        await self.session.verify_previous_cloud_ended("offline-key")
        self.session.http.get.assert_not_called()
        evidence = json.loads((self.session.out / "preflight-previous-ended.json").read_text())
        self.assertEqual(evidence[0]["source"], "saved_confirmation")

    async def test_unknown_creation_remains_blocking_even_with_conflicting_flags(self):
        self.receipt(creation_outcome_unknown=True, cloud_end_confirmed=True)
        with self.assertRaises(web.HTTPConflict):
            self.session.assert_previous_cloud_ended()
        with self.assertRaises(web.HTTPConflict):
            await self.session.verify_previous_cloud_ended("offline-key")
        self.session.http.get.assert_not_called()

    async def test_unconfirmed_receipt_is_not_assumed_ended_after_404(self):
        self.receipt(cloud_end_confirmed=False)
        with self.assertRaises(web.HTTPConflict):
            self.session.assert_previous_cloud_ended()
        response = SimpleNamespace(status=404)
        context = AsyncMock()
        context.__aenter__.return_value = response
        self.session.http.get.side_effect = None
        self.session.http.get.return_value = context
        with self.assertRaises(web.HTTPConflict):
            await self.session.verify_previous_cloud_ended("offline-key")
        self.session.http.get.assert_called_once()

    async def test_unconfirmed_receipt_can_only_be_verified_from_cloud_ended(self):
        self.receipt(cloud_end_confirmed=False)
        response = SimpleNamespace(status=200, json=AsyncMock(return_value={"live": {"status": "ended"}}))
        context = AsyncMock()
        context.__aenter__.return_value = response
        self.session.http.get.side_effect = None
        self.session.http.get.return_value = context
        await self.session.verify_previous_cloud_ended("offline-key")
        evidence = json.loads((self.session.out / "preflight-previous-ended.json").read_text())
        self.assertEqual(evidence[0]["source"], "cloud_readback")


class PublicProjectionContracts(unittest.TestCase):
    def test_page_projection_preserves_display_but_excludes_internal_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = LiveSession.__new__(LiveSession)
            s.out, s.totals = Path(tmp), {}
            # 公开投影仍调用真实归档；__new__ fixture 须补齐归档依赖的状态。
            s.failure, s.cleanup_errors, s.worker_fault = None, [], None
            s.comments = [{"id": "c1", "status": "prepared", "text": "你好", "answer": "谢谢",
                "pipeline_managed": True, "persona_epoch": 7, "delivery_started": False,
                "lease": "private-lease", "fingerprint": "private-fingerprint", "plan": {},
                "interaction_job": {"private": "rule snapshot"},
                "delivery": [{"text": "谢谢", "instruction": "自然", "alignment": {"raw": "provider"},
                    "actions": [{"anchor": {"quote": "谢谢", "occurrence": 1},
                                 "motions": [{"description": "点头"}]}],
                    "timeline": [{"role": "S", "group_index": 1, "at_seconds": 0, "private": "unused"}]}],
                "motion_receipts": [{"group_id": "g", "motion_events": [{"seq_id": 3, "actual_interval_seconds": 2,
                                                                            "transport_details": "unused"}]}]}]
            before = copy.deepcopy(s.comments)
            public = s.public_comments()[0]
            self.assertEqual(public["actions"], ["点头"])
            self.assertEqual(public["delivery"][0]["actions"][0]["anchor"]["quote"], "谢谢")
            self.assertEqual(public["motion_receipts"][0]["motion_events"][0]["actual_interval_seconds"], 2)
            for key in ("pipeline_managed", "persona_epoch", "delivery_started", "interaction_job", "plan", "lease"):
                self.assertNotIn(key, public)
            self.assertNotIn("alignment", public["delivery"][0])
            s.checkpoint()
            archived = json.loads((s.out / "conversation.json").read_text())["comments"][0]
            self.assertEqual(archived["interaction_job"], s.comments[0]["interaction_job"])
            self.assertEqual(archived["persona_epoch"], 7)
            self.assertEqual(s.comments, before)
            public["delivery"][0]["actions"][0]["anchor"]["quote"] = "changed"
            self.assertEqual(s.comments, before)


if __name__ == "__main__":
    unittest.main()
