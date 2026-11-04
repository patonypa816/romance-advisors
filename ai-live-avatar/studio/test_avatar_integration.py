"""HTTP contracts for image/persona binding; no real upload or live is created."""
import asyncio
import hashlib
import io
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
import unittest

from aiohttp import FormData
from PIL import Image

import test_studio as harness
from avatar_library import AvatarError


class AvatarIntegration(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = harness.StudioContracts.asyncSetUp
    asyncTearDown = harness.StudioContracts.asyncTearDown
    post = harness.StudioContracts.post
    claim = harness.StudioContracts.claim

    def seed(self, asset_id, name, color):
        stream = io.BytesIO()
        Image.new("RGB", (12, 16), color).save(stream, "PNG")
        source = self.data / (asset_id + ".png")
        source.write_bytes(stream.getvalue())
        self.control.avatar_library.import_local(asset_id, source, name=name,
            settings={"name": name, "persona": name + "的独立人设", "reply_style": name + "的说话方式"})
        return source

    async def select(self, asset_id="101"):
        return await self.post("avatars/select", {"avatar_id": asset_id, "generation": self.control.generation})

    async def prepare(self):
        self.seed("101", "甲角色", "red")
        self.seed("202", "乙角色", "blue")
        self.assertEqual((await self.select()).status, 200)
        (self.data / "avatar-transition.json").write_text(json.dumps({
            "switch_videos": ["https://media.example.test/common.mp4"], "transition_label": "共用单帧转场"}))
        s = self.control.current
        s.started, s.component.state = True, "ready"
        s.component.switch_avatar = AsyncMock(return_value={"request_id": "integration-switch",
            "status": "sent", "sent": True, "operator_confirmed": False})
        return s

    async def switch(self, asset_id="202", ident="integration-switch", generation=None):
        return await self.post("avatars/switch", {"avatar_id": asset_id,
            "generation": generation or self.control.generation, "request_id": ident})

    async def catalogue(self):
        response = await self.client.get(self.control.prefix + "/avatars")
        self.assertEqual(response.status, 200)
        return await response.json()

    async def test_selection_binds_image_component_and_full_prompt_before_start(self):
        source = self.seed("101", "新人物", "red")
        await self.claim()
        response = await self.select()
        self.assertEqual(response.status, 200)
        s = self.control.current
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.assertEqual(s.image.read_bytes(), source.read_bytes())
        self.assertEqual(s.component.image, s.image)
        self.assertEqual(s.variant_info["image_sha256"], digest)
        self.assertEqual(s.variant_info["avatar_id"], "101")
        self.assertIn("新人物", s.brain_context["character"]["identity"])
        self.assertNotIn("朱元璋", json.dumps(s.brain_context, ensure_ascii=False))
        self.assertEqual((await self.catalogue())["current_id"], None)
        self.assertEqual((await self.post("start")).status, 200)
        await asyncio.sleep(0)
        s.component.start.assert_awaited_once_with("offline-key", avatar_id="101")
        self.assertEqual((await self.catalogue())["current_id"], "101")

    async def test_asset_voice_survives_new_session_and_unbound_asset_clears_it(self):
        self.seed("101", "甲", "red")
        self.seed("202", "乙", "blue")
        self.control.config["tts_voices"] = {"101": "guyunshu_v3.1"}
        self.assertEqual((await self.select()).status, 200)
        self.assertEqual(self.control.current.snapshot()["tts_voice"], "guyunshu_v3.1")
        self.assertEqual((await self.post("stop")).status, 200)
        await asyncio.sleep(0)
        self.assertEqual((await self.post("new")).status, 200)
        s = self.control.current
        self.assertEqual(s.tts_voice, "guyunshu_v3.1")
        part = {"text": "你好。", "instruction": "自然清晰。"}
        with patch("live_session.synthesize_segment", new=AsyncMock(return_value={"status": "offline_failure"})) as tts:
            comment = self.control.enqueue_comment("测试", "观众")
            await s.prepare_audio("offline", "offline", comment, part, 1)
            self.assertEqual(tts.call_args.kwargs["voice"], "guyunshu_v3.1")
            self.assertEqual((await self.select("202")).status, 200)
            self.assertIsNone(s.tts_voice)
            self.assertEqual(s.snapshot()["tts_voice"], "libai_v3.1")
            comment = self.control.enqueue_comment("另一条", "观众")
            await s.prepare_audio("offline", "offline", comment, part, 1)
            self.assertNotIn("voice", tts.call_args.kwargs)

    async def test_invalid_voice_config_blocks_binding_and_new_session(self):
        self.seed("101", "甲", "red")
        await self.select()
        s = self.control.current
        for invalid in (None, [], {"101": ""}, {"101": "voice\n"}, {"101": False}):
            self.control.config["tts_voices"] = invalid
            with self.assertRaisesRegex(ValueError, "tts_voices"):
                self.control.apply_library_asset("101")
            with self.assertRaisesRegex(ValueError, "tts_voices"):
                await self.control.new_session()
            self.assertIs(self.control.current, s)
            self.assertIsNone(s.tts_voice)

    async def test_live_switch_changes_voice_only_after_visual_confirmation(self):
        self.control.config["tts_voices"] = {"101": "guyunshu_v3.1"}
        s = await self.prepare()
        self.assertEqual(s.tts_voice, "guyunshu_v3.1")
        self.assertEqual((await self.switch()).status, 200)
        self.assertEqual(s.tts_voice, "guyunshu_v3.1")
        self.assertEqual((await self.post("avatar-switch/resume", {"generation": self.control.generation})).status, 200)
        self.assertIsNone(s.tts_voice)

    async def test_invalid_target_voice_blocks_live_switch_before_send(self):
        s = await self.prepare()
        self.control.config["tts_voices"] = {"202": "invalid voice"}
        with self.assertRaisesRegex(ValueError, "tts_voices"):
            await self.control.perform_library_switch({"avatar_id": "202",
                "generation": self.control.generation, "request_id": "bad-voice-switch"})
        s.component.switch_avatar.assert_not_awaited()
        self.assertFalse(self.control.avatar_switch_holding)

    async def test_selection_rejects_missing_image_settings_processing_and_stale_generation(self):
        source = self.seed("101", "角色", "red")
        stale = await self.post("avatars/select", {"avatar_id": "101", "generation": "stale"})
        self.assertEqual(stale.status, 409)
        self.control.avatar_library.update_settings("101", {"name": "", "persona": "", "reply_style": ""})
        self.assertEqual((await self.select()).status, 400)
        self.control.avatar_library.update_settings("101", {"name": "角色", "persona": "角色", "reply_style": "角色"})
        image = self.control.avatar_library.image_path("101")
        image.unlink()
        self.assertEqual((await self.select()).status, 409)
        self.assertIsNone(self.control.avatar_library.public()["selected_id"])

    async def test_settings_persist_to_one_asset_and_quick_settings_follow_selection(self):
        self.seed("101", "甲", "red")
        self.seed("202", "乙", "blue")
        await self.select()
        settings = {"name": "修改的甲", "persona": "只改甲", "reply_style": "口语"}
        self.assertEqual((await self.post("settings", settings)).status, 200)
        self.assertEqual(self.control.avatar_library.get("101")["settings"], settings)
        self.assertEqual(self.control.avatar_library.get("202")["settings"]["name"], "乙")
        self.assertEqual(self.control.current.brain_context["character"]["temperament"], "只改甲")
        self.assertEqual((await self.post("avatars/202/settings", {**settings, "name": "x" * 31})).status, 400)
        await self.select("202")
        self.assertEqual(self.control.config["settings"]["name"], "乙")

    async def test_live_switch_applies_identity_only_after_explicit_visual_confirmation(self):
        s = await self.prepare()
        old_image = s.image
        old = self.control.enqueue_comment("旧台词", "观众")
        old.update(status="sent", answer="甲的回复")
        self.control.director.topic = {"state": "paused", "instruction": "甲的话题"}
        response = await self.switch()
        self.assertEqual(response.status, 200)
        self.assertEqual(s.avatar_id, "101")
        self.assertEqual(s.image, old_image)
        self.assertEqual(s.brain_context["operator_settings"]["name"], "甲角色")
        state = await self.catalogue()
        self.assertEqual((state["current_id"], state["pending_id"], state["selected_id"]), ("101", "202", "101"))
        self.assertFalse(self.control.automation.live_ready())
        self.assertEqual((await self.post("comments", {"text": "不能说"})).status, 409)
        duplicate = await self.switch()
        self.assertTrue((await duplicate.json())["duplicate"])
        self.assertEqual(s.component.switch_avatar.await_count, 1)
        self.assertEqual((await self.post("avatar-switch/resume", {"generation": self.control.generation})).status, 200)
        self.assertEqual(s.avatar_id, "202")
        self.assertEqual(s.component.image, s.image)
        self.assertNotEqual(s.image, old_image)
        self.assertEqual(s.brain_context["character"]["temperament"], "乙角色的独立人设")
        self.assertEqual(self.control.reply_history(s), [])
        self.assertIsNone(self.control.director.topic)
        self.assertTrue(self.control.automation.live_ready())
        self.assertIsNone((await self.catalogue())["pending_id"])
        self.assertEqual(self.control.avatar_library.public()["selected_id"], "202")

    async def test_unknown_switch_and_late_failure_cannot_be_confirmed(self):
        s = await self.prepare()
        s.component.switch_avatar = AsyncMock(side_effect=RuntimeError("sensitiveSignedUrlSentinel"))
        response = await self.switch()
        self.assertEqual((await response.json())["status"], "send_unconfirmed")
        self.assertTrue(self.control.avatar_switch_holding)
        self.assertFalse(self.control.avatar_switch_public()["resume_available"])
        self.assertEqual((await self.post("avatar-switch/resume", {"generation": self.control.generation})).status, 409)
        self.assertEqual(s.avatar_id, "101")
        self.assertNotIn("sensitiveSignedUrlSentinel", json.dumps(self.control.snapshot()))
        duplicate = await self.switch()
        self.assertTrue((await duplicate.json())["duplicate"])
        self.assertEqual(s.component.switch_avatar.await_count, 1)

    async def test_late_failed_receipt_blocks_confirmation_without_changing_persona(self):
        s = await self.prepare()
        await self.switch()
        previous = s.component.public
        s.component.public = lambda: {**previous(), "avatar_switch": {"request_id": "integration-switch",
            "status": "failed", "sent": True, "error_code": "SWITCH_AVATAR_PARAM_INVALID"}}
        self.assertEqual((await self.post("avatar-switch/resume", {"generation": self.control.generation})).status, 409)
        self.assertEqual(s.avatar_id, "101")
        self.assertEqual(self.control.config["settings"]["name"], "甲角色")

    async def test_cancelled_unsent_switch_releases_hold_without_applying_target(self):
        s = await self.prepare()
        s.component.switch_avatar = AsyncMock(return_value={"request_id": "integration-switch", "status": "cancelled", "sent": False})
        await self.switch()
        self.assertFalse(self.control.avatar_switch_holding)
        self.assertIsNone(self.control.avatar_pending_id)
        self.assertEqual(s.avatar_id, "101")

    async def test_switch_rejects_unconfigured_target_before_sending(self):
        s = await self.prepare()
        self.control.avatar_library.update_settings("202", {"name": "乙", "persona": "", "reply_style": ""})
        self.assertEqual((await self.switch()).status, 400)
        self.assertFalse(self.control.avatar_switch_holding)
        s.component.switch_avatar.assert_not_awaited()

    async def test_switch_gates_generation_queue_topic_and_transition(self):
        s = await self.prepare()
        self.assertEqual((await self.switch(generation="old")).status, 409)
        (self.data / "avatar-transition.json").unlink()
        self.assertEqual((await self.switch()).status, 409)
        (self.data / "avatar-transition.json").write_text(json.dumps({
            "switch_videos": ["https://media.example.test/common.mp4"], "transition_label": "共用转场"}))
        comment = self.control.enqueue_comment("队列", "观众")
        self.assertEqual((await self.switch()).status, 409)
        comment["status"] = "sent"
        self.control.director.topic = {"state": "running"}
        self.assertEqual((await self.switch()).status, 409)
        self.control.director.topic = None
        s.component.state = "draining"
        self.assertEqual((await self.switch()).status, 409)
        s.component.switch_avatar.assert_not_awaited()
        self.assertEqual((await self.select()).status, 409)

    async def test_image_and_static_routes_and_no_eager_cloud_reads(self):
        self.seed("101", "角色", "red")
        self.control.avatar_library.client.list_page = AsyncMock(side_effect=AssertionError("unexpected cloud read"))
        self.assertEqual(len((await self.catalogue())["assets"]), 1)
        response = await self.client.get(self.control.prefix + "/avatars/101/image")
        self.assertEqual(response.status, 200)
        self.assertTrue(response.headers["Content-Type"].startswith("image/png"))
        for name in ("avatar-library.js", "avatar-library.css"):
            self.assertEqual((await self.client.get(self.control.prefix + "/" + name)).status, 200)
        self.control.avatar_library.client.list_page.assert_not_awaited()

    async def test_multipart_upload_over_json_size_limit_is_bounded_and_mocked(self):
        # A noisy valid image exceeds the 64 KiB JSON limit; only this route
        # streams it with its separate 20 MB bound.
        stream = io.BytesIO()
        Image.effect_noise((256, 256), 100).convert("RGB").save(stream, "PNG")
        raw = stream.getvalue()
        self.assertGreater(len(raw), 65536)
        self.control.avatar_library.client.create_avatar = AsyncMock(return_value={"id": "303", "name": "新角色", "status": "created"})
        form = FormData()
        form.add_field("image", raw, filename="avatar.png", content_type="image/png")
        for name, value in {"name": "新角色", "persona": "新人物", "reply_style": "自然", "request_id": "integration-upload"}.items():
            form.add_field(name, value)
        response = await self.client.post(self.control.prefix + "/avatars/create", data=form,
                                         headers={"Origin": self.control.origin})
        self.assertEqual(response.status, 200, await response.text())
        result = await response.json()
        self.assertEqual((result["asset_id"], result["status"]), ("303", "created"))
        self.assertIsNone(self.control.avatar_library.public()["selected_id"])
        self.control.avatar_library.client.create_avatar.assert_awaited_once()
        self.control.current.component.start.assert_not_awaited()

    async def test_ended_session_requires_new_before_editing_or_selection(self):
        self.seed("101", "角色", "red")
        await self.select()
        self.control.current.started = True
        await self.post("stop")
        await asyncio.sleep(0)
        state = await self.catalogue()
        self.assertIsNone(state["current_id"])
        self.assertIsNone(self.control.snapshot()["studio"]["avatar_current_id"])
        self.assertFalse(state["editing_allowed"])
        self.assertFalse(state["can_select"])
        self.assertIn("新建", state["blocking_reason"])
        self.assertEqual((await self.select()).status, 409)
        self.assertEqual((await self.post("new")).status, 200)
        self.assertEqual((await self.select()).status, 200)


if __name__ == "__main__":
    unittest.main()
