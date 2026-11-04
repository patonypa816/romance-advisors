"""固定回放工作台到worker的离线接线测试；不启动服务、不读凭据、不调用云端。"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web

import live_session
import test_fixed_replay as fixture_tests
from fixed_replay import FixedReplay
from motion_component import MotionComponent
from test_motion_component import FakeSocket, NoNetwork


spec = importlib.util.spec_from_file_location("replay_integration_server", Path(__file__).with_name("server.py"))
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class ReplayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="replay-integration-offline-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source = fixture_tests.FixedReplayTests.source(self)
        self.fixture = FixedReplay(source)

    async def studio(self, *, bind=True, ready=True):
        ai = SimpleNamespace(configured=lambda: False, generate_reply=AsyncMock(), generate_director=AsyncMock(),
                             select_comments=AsyncMock())
        studio = server.Studio(self.root / "studio", ai=ai, preview=True,
                               long_speech_test=True, replay_fixture=self.fixture)

        async def offline_startup(session, app):
            session.http = NoNetwork()
            component = MotionComponent(NoNetwork(), session.out, session.image, session.diagnostics,
                                        long_speech_test=True, long_speech_group_interval=5.0)
            component._ws = FakeSocket()
            component.live_id = "offline-only"
            component.state = "idle"
            session.component = component

        # 执行真实new_session的快照/绑定接线，只替换建立HTTP会话及启动worker的部分。
        with patch.object(live_session.LiveSession, "startup", offline_startup):
            await studio.new_session()
        if bind:
            self.bind_identity(studio)
        if ready:
            studio.current.started = True
            studio.current.component.state = "ready"
            studio.current.component._ready.set()
            studio.current.component._joined.set()
        return studio, ai

    def bind_identity(self, studio):
        # 临时fixture只需验证身份哈希；不创建或下载真实数字人资产。
        variant = json.loads((self.fixture.source_run / "variant.json").read_text())
        image = self.fixture.source_run / Path(variant["used_image_path"]).name
        studio.current.image.write_bytes(image.read_bytes())
        studio.current.avatar_id = self.fixture.avatar_id

    def request(self, studio, **changes):
        body = {"generation": studio.generation, "fingerprint": self.fixture.fingerprint,
                "request_id": "offline-request-0001", **changes}
        return SimpleNamespace(json=AsyncMock(return_value=body))

    async def enqueue(self, studio):
        response = await studio.replay(self.request(studio))
        ident = json.loads(response.body)["id"]
        return next(c for c in studio.current.comments if c["id"] == ident)

    async def test_mode_snapshot_and_avatar_image_identity_bind_before_replay(self):
        ai = SimpleNamespace(configured=lambda: False)
        with self.assertRaises(ValueError):
            server.Studio(self.root / "invalid", ai=ai, preview=True, replay_fixture=self.fixture)
        with self.assertRaises(ValueError):
            live_session.LiveSession(self.root / "invalid-run", "A", replay_fixture=self.fixture)
        studio, _ = await self.studio(bind=False)
        self.assertTrue(studio.replay_identity_reason())
        self.assertFalse(studio.replay_public()["available"])
        snapshot = studio.current.out / "replay-source"
        manifest = json.loads((snapshot / "manifest.json").read_text())
        self.assertEqual(manifest["fingerprint"], self.fixture.fingerprint)
        self.assertEqual((snapshot / "sample.pcm").read_bytes(), self.fixture.pcm)
        self.assertEqual(manifest["image_sha256"], self.fixture.image_sha256)
        mode = json.loads((studio.current.out / "source-used/run-mode.json").read_text())
        self.assertEqual(mode["replay_fixture_fingerprint"], self.fixture.fingerprint)
        self.assertEqual(mode["long_speech_group_interval"], 5)
        self.assertTrue(mode["fixed_replay"])
        self.bind_identity(studio)
        self.assertEqual(studio.replay_identity_reason(), "")
        self.assertTrue(studio.replay_public()["available"])
        studio.current.avatar_id = "999"
        self.assertFalse(studio.replay_public()["available"])
        studio.current.avatar_id = self.fixture.avatar_id
        studio.current.image.write_bytes(b"different-image")
        self.assertFalse(studio.replay_public()["available"])

    async def test_ready_generation_fingerprint_and_duplicate_request_queue_exactly_once(self):
        studio, _ = await self.studio()
        for values in ({"generation": "old-run"}, {"fingerprint": "wrong"}):
            with self.subTest(values=values), self.assertRaises(web.HTTPConflict):
                await studio.replay(self.request(studio, **values))
        studio.current.component.state = "sending"
        with self.assertRaises(web.HTTPConflict):
            await studio.replay(self.request(studio))
        studio.current.component.state = "ready"
        comment = await self.enqueue(studio)
        response = await studio.replay(self.request(studio))
        self.assertEqual(json.loads(response.body), {"id": comment["id"], "duplicate": True})
        self.assertEqual(len(studio.current.comments), 1)
        self.assertEqual(studio.current.work.qsize(), 1)
        self.assertEqual(comment["status"], "ready")
        self.assertEqual(comment["plan"], self.fixture.plan)
        with self.assertRaises(web.HTTPConflict):
            await studio.replay(self.request(studio, request_id="another-request-0002"))
        self.assertEqual(studio.current.work.qsize(), 1)

    async def test_normal_inputs_and_ai_paths_cannot_mix_into_fixed_replay(self):
        studio, ai = await self.studio()
        with self.assertRaises(web.HTTPConflict):
            studio.enqueue_comment("新台词", "观众")
        with self.assertRaises(web.HTTPConflict):
            await studio.comments(SimpleNamespace(json=AsyncMock(return_value={"text": "新弹幕"})))
        with self.assertRaises(web.HTTPConflict):
            studio.director.submit({"mode": "once", "text": "新主持安排"})
        app = studio.build()
        studio.origin = "http://127.0.0.1:12345"
        handler = AsyncMock(return_value=web.json_response({"unexpected": True}))
        for route in ("comments", "director/submit", "settings", "intake/start", "avatar-switch"):
            request = SimpleNamespace(host="127.0.0.1:12345", method="POST", path=studio.prefix + "/" + route,
                                      headers={"Origin": studio.origin})
            response = await app.middlewares[0](request, handler)
            self.assertEqual(response.status, 409)
        handler.assert_not_awaited()
        await studio.automation.reply_once()
        self.assertFalse(studio.automation.can_select())
        ai.generate_reply.assert_not_awaited()
        ai.generate_director.assert_not_awaited()
        ai.select_comments.assert_not_awaited()

    async def test_accept_plan_rejects_modified_words_or_actions_before_queue(self):
        studio, _ = await self.studio()
        for index, field in enumerate(("text", "gaze")):
            comment = studio.enqueue_comment("固定对照", "固定对照", source="operator", input_kind="operator",
                                             operator_mode="once", replay_fixture_fingerprint=self.fixture.fingerprint)
            comment["status"] = "processing"
            changed = self.fixture.plan
            if field == "text":
                changed["segments"][0]["text"] = "这段台词已经被改动。"
            else:
                changed["segments"][0]["actions"][0]["gaze"] = "望向远方"
            with self.subTest(field=field), self.assertRaises(ValueError):
                studio.accept_plan(studio.current, comment, changed)
            self.assertTrue(studio.current.work.empty())
            comment["status"] = "error"

    async def test_worker_uses_original_pcm_once_without_tts_credentials_or_new_usage(self):
        studio, ai = await self.studio()
        # 资产覆盖不能让历史固定回放重新合成或改变已核验的原始 PCM。
        studio.current.tts_voice = "guyunshu_v3.1"
        comment = await self.enqueue(studio)
        receipt = {"group_id": "offline-send-proof", "send_complete": True,
                   "actions_sent_complete": True, "playback_complete_confirmed": False}
        studio.current.work.put_nowait(None)
        with patch("live_session.load_bailian", side_effect=AssertionError("不得读取语音凭据")) as credentials, \
             patch("live_session.synthesize_segment", side_effect=AssertionError("不得重新合成")) as tts, \
             patch.object(studio.current.component, "send_expression", AsyncMock(return_value=receipt)) as sender:
            await studio.current.process_answers()
        credentials.assert_not_called()
        tts.assert_not_called()
        sender.assert_awaited_once()
        self.assertEqual(sender.await_args.args[0], self.fixture.pcm)
        self.assertEqual(sender.await_args.args[1], self.fixture.plan["segments"][0]["text"])
        self.assertEqual(sender.await_args.args[2], self.fixture.plan["segments"][0]["actions"])
        self.assertEqual(comment["status"], "sent")
        self.assertEqual(comment["segments_sent"], 1)
        self.assertEqual(comment["usage"], {"input_tokens": 0, "output_tokens": 0, "cost_cny": 0})
        self.assertEqual(studio.current.totals["audio_seconds"], 17.36)
        for field in ("input_tokens", "output_tokens", "cost_cny", "usage_unverified_segments"):
            self.assertEqual(studio.current.totals[field], 0)
        folder = studio.current.out / "tts" / f'{comment["id"]}-1'
        result = json.loads((folder / "result.json").read_text())
        self.assertEqual(result["status"], "reused")
        self.assertEqual(result["pcm_sha256"], hashlib.sha256(self.fixture.pcm).hexdigest())
        self.assertEqual(json.loads((folder / "component-send.json").read_text()), receipt)
        archived = json.loads((studio.current.out / "conversation.json").read_text())
        self.assertEqual(archived["comments"][0]["motion_receipts"], [receipt])
        self.assertEqual(archived["comments"][0]["status"], "sent")
        ai.generate_reply.assert_not_awaited()
        ai.generate_director.assert_not_awaited()

    async def test_stop_after_queue_cancels_without_preparing_or_sending_audio(self):
        studio, _ = await self.studio()
        comment = await self.enqueue(studio)
        with patch.object(studio.current.component, "stop", AsyncMock(return_value={"state": "ended"})):
            await studio.current.request_stop("离线取消")
        self.assertEqual(comment["status"], "cancelled")
        studio.current.work.put_nowait(None)
        with patch.object(self.fixture, "prepare", side_effect=AssertionError("停止后不得准备音频")) as prepare, \
             patch.object(studio.current.component, "send_expression", AsyncMock()) as sender, \
             patch("live_session.load_bailian", side_effect=AssertionError("不得读取凭据")):
            await studio.current.process_answers()
        prepare.assert_not_called()
        sender.assert_not_awaited()
        self.assertFalse(studio.current.component._ws.attempts)
        self.assertFalse(studio.current.component._ws.binary)
        self.assertFalse((studio.current.out / "tts").exists())

    async def test_worker_rechecks_fingerprint_and_content_after_enqueue(self):
        for index, changed_field in enumerate(("fingerprint", "content")):
            # 每个变体独立run，避免前次失败影响另一次校验。
            if index:
                self.root = self.root / "second-case"
                self.root.mkdir()
            studio, _ = await self.studio()
            comment = await self.enqueue(studio)
            if changed_field == "fingerprint":
                comment["replay_fixture_fingerprint"] = "changed"
            else:
                comment["plan"]["segments"][0]["actions"][1]["expression"] = "改变的表情"
            studio.current.work.put_nowait(None)
            with patch.object(self.fixture, "prepare", side_effect=AssertionError("素材核验前不得准备")) as prepare, \
                 patch("live_session.load_bailian", side_effect=AssertionError("不得读取凭据")), \
                 patch("live_session.synthesize_segment", side_effect=AssertionError("不得重新合成")), \
                 patch.object(studio.current.component, "send_expression", AsyncMock()) as sender:
                await studio.current.process_answers()
            with self.subTest(field=changed_field):
                prepare.assert_not_called()
                sender.assert_not_awaited()
                self.assertEqual(comment["status"], "error")
                self.assertEqual(studio.current.totals["audio_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
