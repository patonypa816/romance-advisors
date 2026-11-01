"""Avatar catalogue contracts: all cloud creation is a fake; no credentials read."""
import asyncio
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from avatar_library import (AvatarLibrary, AvatarError, CreateRejected, ViduAvatarClient,
                            inspect_image, validate_image_url, _PublicResolver)


def png(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (8, 12), color).save(stream, "PNG")
    return stream.getvalue()


def remote(asset_id="a1", **changes):
    return {"id": asset_id, "name": "云资产", "status": "success", **changes}


class FakeClient:
    def __init__(self):
        self.pages = [{"total": 1, "items": [remote()], "next_page_token": ""}]
        self.calls = []
        self.created = remote("new", status="processing")
        self.error = None
        self.gate = None
        self.fetched = remote()

    async def list_page(self, page, size, token=None):
        self.calls.append(("list", page, size, token))
        return self.pages[page]

    async def create_avatar(self, image_uri, name):
        self.calls.append(("post", name))
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        return self.created

    async def get_avatar(self, asset_id):
        self.calls.append(("get", asset_id))
        return self.fetched

    async def download_image(self, url):
        self.calls.append(("image",))
        return png()

    async def close(self):
        pass


class LibraryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "data"
        self.client = FakeClient()
        self.library = AvatarLibrary(self.directory, client=self.client)

    def tearDown(self):
        self.temporary.cleanup()

    async def create(self, request_id="upload-1", **changes):
        return await self.library.create(**{"raw": png(), "filename": "../../avatar.png", "name": "新资产",
            "settings": {"name": "角色", "persona": "历史角色", "reply_style": "简洁"},
            "request_id": request_id, **changes})

    async def test_offline_construction_and_persistence_permissions(self):
        with patch("avatar_library.credentials.load_vidu", side_effect=AssertionError("offline")):
            empty = AvatarLibrary(self.directory / "empty")
            self.assertEqual(empty.public()["assets"], [])
            await empty.close()
            await self.create()
            loaded = AvatarLibrary(self.directory)
            self.assertEqual(loaded.public()["assets"][0]["settings"]["persona"], "历史角色")
            self.assertEqual(self.library.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.library.directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(self.library.image_path("new").stat().st_mode & 0o777, 0o600)
            await loaded.close()

    async def test_unknown_upload_new_request_id_cannot_duplicate_same_image(self):
        self.client.error = RuntimeError("response lost")
        first = await self.create("attempt-first")
        self.assertEqual(first["status"], "creation_unknown")
        self.client.error = None
        repeated = await self.create("new-browser-id")
        self.assertEqual(repeated["request_id"], "attempt-first")
        self.assertEqual(len([c for c in self.client.calls if c[0] == "post"]), 1)

    async def test_same_request_concurrent_and_restart_is_one_post(self):
        first, second = await asyncio.gather(self.create(), self.create())
        self.assertEqual(first, second)
        loaded = AvatarLibrary(self.directory, client=self.client)
        same = await loaded.create(png(), "different.png", "名称", {}, "upload-1")
        self.assertEqual(same["asset_id"], "new")
        self.assertEqual(sum(c[0] == "post" for c in self.client.calls), 1)
        with self.assertRaises(AvatarError):
            await self.create(raw=png("blue"))

    async def test_unknown_create_is_persisted_and_never_reposted(self):
        self.client.error = RuntimeError("secret https://example.com?signature=private vda_private")
        result = await self.create()
        self.assertEqual(result["status"], "creation_unknown")
        self.assertNotIn("private", json.dumps(self.library.public()))
        loaded = AvatarLibrary(self.directory, client=self.client)
        await loaded.create(png(), "a.png", "资产", {}, "upload-1")
        self.assertEqual(sum(c[0] == "post" for c in self.client.calls), 1)
        self.assertNotIn("signature", self.library.path.read_text())

    async def test_cancelled_post_is_not_retried(self):
        self.client.gate = asyncio.Event()
        task = asyncio.create_task(self.create())
        while not self.client.calls:
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await self.create()
        self.assertEqual(sum(c[0] == "post" for c in self.client.calls), 1)
        self.assertEqual(self.library.public()["uploads"][0]["status"], "creation_unknown")

    async def test_known_id_with_unrecognized_status_can_be_refreshed_without_post(self):
        self.client.created = remote("new", status="future-status")
        result = await self.create()
        self.assertEqual(result["status"], "creation_unknown")
        self.assertEqual(result["asset_id"], "new")
        self.client.fetched = remote("new")
        self.assertEqual((await self.library.refresh("new"))["status"], "success")
        await self.create()
        self.assertEqual(sum(c[0] == "post" for c in self.client.calls), 1)

    async def test_http_rejection_is_definitive_but_same_request_not_retried(self):
        self.client.error = CreateRejected("Vidu 拒绝创建（HTTP 400）")
        self.assertEqual((await self.create())["status"], "creation_failed")
        await self.create()
        self.assertEqual(sum(c[0] == "post" for c in self.client.calls), 1)

    async def test_sync_all_pages_preserves_settings_and_hides_urls(self):
        await self.create()
        self.library.update_settings("new", {"name": "本地角色", "persona": "保留我", "reply_style": "口语"})
        url = "https://prod-ss-vidu.s3.oss-cn-beijing.aliyuncs.com/avatar.png?Signature=private"
        self.client.pages = [
            {"total": 2, "items": [remote("new", image_uri=url)], "next_page_token": "next"},
            {"total": 2, "items": [remote("a2", status=11)], "next_page_token": ""}]
        result = await self.library.sync()
        self.assertIsNone(result["error"])
        self.assertEqual(len(result["assets"]), 2)
        self.assertEqual(self.library.get("new")["settings"]["persona"], "保留我")
        self.assertEqual(self.client.calls[-1], ("image",))
        self.assertIn(("list", 1, 100, "next"), self.client.calls)
        self.assertNotIn("Signature", json.dumps(result) + self.library.path.read_text())
        self.assertIsNotNone(self.library.image_path("new"))

    async def test_settings_saved_during_slow_sync_are_not_overwritten(self):
        await self.create()
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed_download(url):
            entered.set()
            await release.wait()
            return png()
        self.client.download_image = delayed_download
        self.client.pages[0]["items"] = [remote("new", image_uri="https://cdn.vidu.cn/image.png")]
        task = asyncio.create_task(self.library.sync())
        await entered.wait()
        self.library.update_settings("new", {"persona": "同步中刚刚保存"})
        release.set()
        await task
        self.assertEqual(self.library.get("new")["settings"]["persona"], "同步中刚刚保存")

    async def test_numbered_paging_without_token(self):
        self.client.pages = [
            {"total": 2, "items": [remote("a1")]},
            {"total": 2, "items": [remote("a2")]}]
        result = await self.library.sync()
        self.assertIsNone(result["error"])
        self.assertEqual(len(result["assets"]), 2)

    async def test_bad_pagination_never_partially_replaces_catalogue(self):
        await self.create()
        for pages in ([{"data": []}], [
                {"total": 2, "items": [remote("a1")], "next_page_token": "same"},
                {"total": 2, "items": [remote("a1")], "next_page_token": "same"}]):
            self.client.pages = pages
            result = await self.library.sync()
            self.assertTrue(result["error"])
            self.assertEqual([a["id"] for a in result["assets"]], ["new"])

    async def test_refresh_only_reads_known_asset_and_updates_status(self):
        await self.create()
        self.client.fetched = remote("new")
        self.assertEqual((await self.library.refresh("new"))["status"], "success")
        self.assertEqual(self.library.public()["uploads"][0]["status"], "success")
        self.assertEqual(sum(c[0] == "post" for c in self.client.calls), 1)
        self.client.fetched = remote("wrong")
        with self.assertRaises(AvatarError):
            await self.library.refresh("new")
        self.assertEqual(self.library.get("new")["status"], "success")

    async def test_validation_and_path_escape_are_rejected(self):
        for changes in ({"raw": b"not an image"}, {"request_id": "../x"}, {"name": ""},
                        {"settings": {"persona": 7}}, {"raw": b""}):
            with self.subTest(changes=changes), self.assertRaises(AvatarError):
                await self.create(**changes)
        self.assertEqual(self.client.calls, [])
        await self.create()
        for asset_id in ("../../secret", "x/y", ""):
            with self.assertRaises(AvatarError):
                self.library.image_path(asset_id)
        self.library.state["assets"][0]["image_file"] = "../outside.png"
        self.assertIsNone(self.library.image_path("new"))

    async def test_symlink_cannot_be_served_as_cached_image(self):
        await self.create()
        path = self.library.image_path("new")
        path.unlink()
        path.symlink_to(self.library.path)
        self.assertIsNone(self.library.image_path("new"))
        with self.assertRaises(AvatarError):
            await self.create("another")

    async def test_local_seed_and_selection_never_call_cloud(self):
        source = Path(self.temporary.name) / "source.png"
        source.write_bytes(png())
        self.library.import_local("known", source, name="已有资产", settings={"persona": "角色"})
        self.library.set_selected("known")
        self.library.import_local("known", source, settings={"persona": "不能覆盖"})
        self.assertEqual(self.library.get("known")["settings"]["persona"], "角色")
        self.assertEqual(self.library.public()["selected_id"], "known")
        self.assertEqual(self.client.calls, [])

    async def test_unsafe_image_url_not_downloaded(self):
        self.client.pages[0]["items"][0]["image_uri"] = "https://localhost/private"
        result = await self.library.sync()
        self.assertEqual(result["assets"][0]["image_available"], False)
        self.assertTrue(result["assets"][0]["image_error"])
        self.assertEqual([c[0] for c in self.client.calls], ["list"])


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    def test_url_restrictions(self):
        for url in ("http://cdn.vidu.cn/a", "https://vidu.cn.attacker.com/a", "https://127.0.0.1/a",
                    "https://cdn.vidu.cn:443@localhost/a", "https://cdn.vidu.cn:444/a",
                    "https://user:pass@cdn.vidu.cn/a", "https://cdn.vidu.cn/a#fragment"):
            with self.subTest(url=url), self.assertRaises(AvatarError):
                validate_image_url(url)
        self.assertEqual(validate_image_url("https://cdn.vidu.cn/a?signature=x"), "https://cdn.vidu.cn/a?signature=x")

    async def test_resolver_rejects_private_dns_result(self):
        resolver = _PublicResolver()
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", return_value=[
            (2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(AvatarError):
                await resolver.resolve("cdn.vidu.cn", 443)

    async def test_tun_exception_only_for_observed_vidu_cdn(self):
        resolver = _PublicResolver()
        known = "prod-ss-vidu.s3.oss-cn-beijing.aliyuncs.com"
        with patch.object(asyncio.get_running_loop(), "getaddrinfo", return_value=[
            (2, 1, 6, "", ("198.18.1.25", 443))]):
            self.assertEqual((await resolver.resolve(known, 443))[0]["host"], "198.18.1.25")
            with self.assertRaises(AvatarError):
                await resolver.resolve("other.aliyuncs.com", 443)
        for ip in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "::1"):
            with patch.object(asyncio.get_running_loop(), "getaddrinfo", return_value=[
                (2, 1, 6, "", (ip, 443))]):
                with self.assertRaises(AvatarError):
                    await resolver.resolve(known, 443)

    def test_pixels_are_bounded_and_real_format_is_used(self):
        self.assertEqual(inspect_image(png())[:3], ("png", 8, 12))
        with patch("avatar_library.MAX_PIXELS", 20), self.assertRaises(AvatarError):
            inspect_image(png())


if __name__ == "__main__":
    unittest.main()
