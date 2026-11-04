"""Frozen performance and cancellation wire contracts; local HTTP only."""
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer

from romance.avatar_client import AvatarClient


class AvatarPerformanceClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_preserves_full_plan_and_deadlines_and_legacy_body(self):
        calls = []
        app = web.Application()

        async def submit(request):
            calls.append(await request.json())
            return web.json_response({"status": "ready"})

        app.router.add_post("/external/jobs", submit)
        app.router.add_post("/external/jobs/prepare", submit)
        async with TestServer(app) as server:
            client = AvatarClient()
            client.control_url = str(server.make_url("/"))
            client.origin = client.control_url.rstrip("/")
            client.owner_id, client.generation = "program:one", "generation:one"
            plan = {"emotion": "嘴硬", "reading": None, "segments": [{
                "text": "这招我先试试。", "ssml": '<speak>这招<break time="150ms"/>我先试试。</speak>',
                "instruction": "嘴硬但心虚，自嘲带笑。", "actions": [], "recovery": None}]}
            record = {"job_id": "one:reaction", "plan": plan,
                      "expires_at": 1000.5, "finish_before": 1010.5, "phase": "private-program-context"}
            try:
                await client.submit(record, "这招我先试试。")
                self.assertEqual(calls[0], {**client.owner(), "job_id": record["job_id"], "text": "这招我先试试。",
                                           "plan": plan, "expires_at": 1000.5, "finish_before": 1010.5})
                await client.prepare(record, "这招我先试试。")
                self.assertEqual(calls[1], calls[0])
                await client.submit({"job_id": "legacy"}, "旧正文。")
                self.assertEqual(calls[2], {**client.owner(), "job_id": "legacy", "text": "旧正文。"})
            finally:
                await client.close()

    async def test_cancel_uses_same_job_id_and_current_owner_without_resubmit(self):
        calls = []
        app = web.Application()

        async def cancel(request):
            calls.append((request.match_info["job_id"], await request.json()))
            return web.json_response({"job_id": request.match_info["job_id"], "status": "cancelled",
                                      "delivery_started": False, "worker_pending": True,
                                      "cancel_result": "cancelled_before_delivery"})

        app.router.add_post("/external/jobs/{job_id}/cancel", cancel)
        async with TestServer(app) as server:
            client = AvatarClient()
            client.control_url = str(server.make_url("/"))
            client.origin = client.control_url.rstrip("/")
            client.owner_id, client.generation = "program:one", "generation:one"
            try:
                result = await client.cancel_job({"job_id": "one:round:waiting"})
                self.assertEqual(calls, [("one:round:waiting", client.owner())])
                self.assertEqual(result["cancel_result"], "cancelled_before_delivery")
                self.assertTrue(result["worker_pending"])
            finally:
                await client.close()


if __name__ == "__main__":
    unittest.main()
