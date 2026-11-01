"""Offline catalogue contracts. No real credential reads or cloud mutations."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from idle_action_library import IdleActionLibrary, IdleActionError, ViduIdleActionClient


DESCRIPTION = "轻轻点头一次"
INSTRUCTION = "动作：轻轻点头一次。\n眼神：看向镜头。\n表情：自然平静。"
RECOVERY = "动作：正常坐姿。\n眼神：看向镜头。\n表情：自然平静。"


def config():
    return {"version": 1, "library_id": "lib-1", "avatar_id": "avatar-1", "actions": [
        {"id": "action-1", "name": "轻轻点头", "instruction": INSTRUCTION,
         "recovery_instruction": RECOVERY,
         "description_sha256": hashlib.sha256(DESCRIPTION.encode()).hexdigest(),
         "hold_seconds": 3, "cooldown_seconds": 60, "weight": 1,
         "enabled": True, "visual_verified": False}]}


class FakeClient:
    def __init__(self):
        self.library = {"id": "lib-1"}
        self.pages = [{"total": 1, "items": [{"id": "action-1", "name": "轻轻点头", "description": DESCRIPTION}]}]
        self.calls = []
        self.failure = None

    async def get_library(self, library_id):
        self.calls.append(("library", library_id))
        if self.failure:
            raise self.failure
        return self.library

    async def list_actions(self, library_id, page, size):
        self.calls.append(("actions", library_id, page, size))
        return self.pages[page]

    async def close(self):
        pass


class LibraryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.path = self.data / "idle-action-library.json"
        self.path.write_text(json.dumps(config()))
        self.client = FakeClient()
        self.library = IdleActionLibrary(self.data, self.client)

    def tearDown(self):
        self.temp.cleanup()

    async def test_startup_offline_public_redacted_and_unverified(self):
        with patch("idle_action_library.credentials.load_vidu", side_effect=AssertionError("credential read")):
            library = IdleActionLibrary(self.data)
            public = library.public("avatar-1")
            self.assertTrue(public["matches_avatar"])
            self.assertNotIn("instruction", public["actions"][0])
            self.assertFalse(public["actions"][0]["visual_verified"])
            self.assertEqual(public["settings"], {"min_interval_seconds": 15, "max_interval_seconds": 25})
            with self.assertRaises(IdleActionError):
                library.snapshot("avatar-1")
            await library.close()
        self.assertEqual(self.client.calls, [])

    async def test_sync_and_immutable_snapshot(self):
        self.assertEqual((await self.library.sync("avatar-1"))["sync_status"], "verified")
        actions, settings = self.library.snapshot("avatar-1")
        actions[0]["instruction"] = "tampered"
        settings["min_interval_seconds"] = 99
        second, interval = self.library.snapshot("avatar-1")
        self.assertEqual(second[0]["instruction"], INSTRUCTION)
        self.assertEqual(interval["min_interval_seconds"], 15)
        public = self.library.public("avatar-1")
        public["actions"].clear()
        self.assertEqual(len(self.library.snapshot("avatar-1")[0]), 1)

    async def test_cross_avatar_cannot_fetch_or_snapshot(self):
        result = await self.library.sync("another-avatar")
        self.assertEqual(result["sync_status"], "review_required")
        self.assertEqual(self.client.calls, [])
        with self.assertRaises(IdleActionError):
            self.library.snapshot("another-avatar")

    async def test_drift_addition_deletion_and_malformed_fail_closed(self):
        original = self.path.read_bytes()
        for replacement in (
                {"id": "action-1", "name": "改名", "description": DESCRIPTION},
                {"id": "action-1", "name": "轻轻点头", "description": DESCRIPTION + "改写"},
                {"id": "new-action", "name": "轻轻点头", "description": DESCRIPTION},
                {"id": "action-1", "name": "轻轻点头", "description": "x" * 2001},
                None):
            with self.subTest(replacement=replacement):
                self.client.pages = [{"total": 1, "items": [replacement]}]
                self.assertEqual((await self.library.sync("avatar-1"))["sync_status"], "review_required")
                self.assertEqual(self.path.read_bytes(), original)
        self.client.pages = [{"total": 0, "items": []}]
        self.assertEqual((await self.library.sync("avatar-1"))["sync_status"], "review_required")

    async def test_partial_sync_keeps_running_copy_but_blocks_new_snapshot(self):
        await self.library.sync("avatar-1")
        running = self.library.snapshot("avatar-1")
        self.client.pages[0]["total"] = 2
        self.client.pages.append({"total": 2, "items": []})
        result = await self.library.sync("avatar-1")
        self.assertEqual(result["sync_status"], "review_required")
        self.assertEqual(running[0][0]["instruction"], INSTRUCTION)
        self.assertIsNotNone(self.library._verified)
        with self.assertRaises(IdleActionError):
            self.library.snapshot("avatar-1")

    async def test_redacts_injected_client_exception(self):
        self.client.failure = RuntimeError("Token super-secret upstream body")
        public = await self.library.sync("avatar-1")
        self.assertNotIn("super-secret", json.dumps(public))
        self.assertIn("不影响语音和主讲", public["error"])

    async def test_configure_atomic_local_only_and_preserves_running_copy(self):
        await self.library.sync("avatar-1")
        running = self.library.snapshot("avatar-1")
        calls = copy.deepcopy(self.client.calls)
        self.library.configure({"min_interval_seconds": 5, "max_interval_seconds": 10}, [])
        self.assertEqual(self.client.calls, calls)
        self.assertFalse(self.library.snapshot("avatar-1")[0][0]["enabled"])
        self.assertTrue(running[0][0]["enabled"])
        loaded = IdleActionLibrary(self.data, FakeClient())
        self.assertFalse(loaded.public()["actions"][0]["enabled"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        before = self.path.read_bytes()
        for settings, ids in (({"min_interval_seconds": True}, []), ({"max_interval_seconds": 4}, []),
                              ({"min_interval_seconds": 26}, []), ({"url": "https://evil"}, []), ({}, ["unknown"])):
            with self.assertRaises(IdleActionError):
                self.library.configure(settings, ids)
        with patch("idle_action_library.os.replace", side_effect=OSError("secret")):
            with self.assertRaisesRegex(IdleActionError, "保存失败"):
                self.library.configure({}, ["action-1"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.library.snapshot("avatar-1")[0][0]["enabled"])

    async def test_rejects_invalid_local_content_without_overwrite(self):
        for field, value in (("instruction", "单行"), ("instruction", "动作：x\n眼神：y\n表情："),
                              ("instruction", "动作：" + "x" * 200 + "\n眼神：y\n表情：z"),
                              ("hold_seconds", True), ("hold_seconds", 1), ("hold_seconds", 1.9),
                              ("hold_seconds", 4.1), ("weight", float("nan")),
                              ("description_sha256", "x"), ("id", "../escape")):
            raw = config()
            raw["actions"][0][field] = value
            self.path.write_text(json.dumps(raw))
            original = self.path.read_bytes()
            with self.subTest(field=field, value=value):
                broken = IdleActionLibrary(self.data, self.client)
                self.assertIsNone(broken.state)
                self.assertEqual(broken.public("avatar-1")["sync_status"], "review_required")
                self.assertFalse(broken.public("avatar-1")["configured"])
                with self.assertRaises(IdleActionError):
                    broken.snapshot("avatar-1")
                with self.assertRaises(IdleActionError):
                    broken.configure({}, [])
                self.assertEqual((await broken.sync("avatar-1"))["sync_status"], "review_required")
                self.assertEqual(self.client.calls, [])
            self.assertEqual(self.path.read_bytes(), original)

    async def test_client_fixed_paths_allowlist_and_zero_mutations(self):
        client = ViduIdleActionClient()
        calls = []
        async def get(path, params=None):
            calls.append((path, params))
            return {"id": "lib-1", "name": "库", "secret": "never-return",
                    "total": 1, "items": [{"id": "action-1", "name": "动作", "description": DESCRIPTION,
                                             "url": "https://evil", "token": "secret"}]}
        client._get = get
        library = await client.get_library("lib-1")
        listing = await client.list_actions("lib-1", 0, 50)
        self.assertNotIn("secret", library)
        self.assertEqual(set(listing["items"][0]), {"id", "name", "description"})
        self.assertEqual(calls, [("/live/v1/action-libraries/lib-1", None),
                                ("/live/v1/action-libraries/lib-1/actions", {"pager.page": 0, "pager.pagesz": 50})])
        with self.assertRaises(IdleActionError):
            await client.get_library("../../escape?secret")
        self.assertEqual(len(calls), 2)

    async def test_malformed_optional_file_does_not_break_workbench_startup(self):
        for raw in ('{broken Token should-never-appear', '[]', '{"version": 99}'):
            self.path.write_text(raw)
            with patch("idle_action_library.credentials.load_vidu", side_effect=AssertionError("offline")):
                broken = IdleActionLibrary(self.data)
                public = broken.public("avatar-1")
                self.assertFalse(public["configured"])
                self.assertEqual(public["actions"], [])
                self.assertEqual(public["sync_status"], "review_required")
                self.assertIn("不影响语音和主讲", public["error"])
                self.assertNotIn("should-never-appear", json.dumps(public))
                with self.assertRaises(IdleActionError):
                    broken.snapshot("avatar-1")
                with self.assertRaises(IdleActionError):
                    broken.configure({}, [])
                await broken.close()
            self.assertEqual(self.path.read_text(), raw)

    async def test_hold_contract_matches_scheduler_two_to_four_seconds(self):
        for hold in (2, 3, 4):
            raw = config()
            raw["actions"][0]["hold_seconds"] = hold
            self.path.write_text(json.dumps(raw))
            library = IdleActionLibrary(self.data, self.client)
            self.assertTrue(library.public("avatar-1")["configured"])
            await library.sync("avatar-1")
            self.assertEqual(library.snapshot("avatar-1")[0][0]["hold_seconds"], hold)


class HttpClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_limits_redirects_and_safe_errors(self):
        class Content:
            async def iter_chunked(self, size):
                yield b"x" * (1024 * 1024 + 1)

        class Response:
            status = 200
            content = Content()
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass

        class Session:
            def __init__(self):
                self.response = Response()
                self.calls = []
                self.failure = False
            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if self.failure:
                    raise RuntimeError("Token private-key upstream response")
                return self.response
            async def close(self):
                pass

        client = ViduIdleActionClient()
        session = Session()
        client._api = session
        with patch("idle_action_library.credentials.load_vidu", return_value="fake-offline-key"):
            with self.assertRaisesRegex(IdleActionError, "响应过大"):
                await client.get_library("lib-1")
            url, options = session.calls[0]
            self.assertEqual(url, "https://api.vidu.cn/live/v1/action-libraries/lib-1")
            self.assertFalse(options["allow_redirects"])
            self.assertEqual(options["timeout"].total, 20)
            session.response.status = 302
            with self.assertRaisesRegex(IdleActionError, "HTTP 302"):
                await client.get_library("lib-1")
            session.failure = True
            with self.assertRaises(IdleActionError) as caught:
                await client.get_library("lib-1")
            self.assertNotIn("private-key", str(caught.exception))
        await client.close()
