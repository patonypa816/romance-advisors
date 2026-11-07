"""Local control plane. Opening the app never starts a paid Vidu session.

One OBS or explicitly selected preview page owns the RTC receiver. UI, player and brain have distinct capabilities
and independent liveness checks, so an open dashboard cannot hide a lost player.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import time
import uuid

from aiohttp import web

from action_schema import (parse_plan, parse_director_plan, validate_short_performance_plan,
                           short_performance_input_mode, validate_continuous_speech_plan,
                           continuous_speech_input_mode, parse_long_speech_plan,
                           long_speech_input_mode, validate_long_speech_plan, validate_long_action_replay_plan)
from semantic_speech import (parse_dialogue_semantic_plan, DIALOGUE_ACTION_KIND,
                             DIALOGUE_GE_LEAD_SECONDS, DIALOGUE_S_TO_Q_SECONDS)
from avatar_library import AvatarLibrary
from idle_action_library import IdleActionLibrary, IdleActionError
from idle_motion import IdleRecoveryError
from credentials import LEGACY_SETTINGS, bailian_config, load_bailian, load_vidu
from automation import Automation, IN_FLIGHT
from director import Director
from dyhub_client import normalize_endpoint
from live_session import LiveSession, RunGuard, STUDY, TERMINAL, SOURCE_FILES, save
from fixed_replay import FixedReplay
from semantic_replay import SemanticFixedReplay, PreparedSemanticReplay, PreparedMinuteSemanticReplay
from live_interactions import DEFAULT_INTERACTIONS, InteractionQueue, validate_settings as validate_interaction_settings
from interaction_delivery import is_motion_item, validate_interaction_plan
from tts_live import valid_voice_id
from external_game import ExternalGame

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
DEFAULT_SETTINGS = {
    "name": "朱元璋",
    "persona": "朱元璋历史人物的AI虚构演绎。质朴直接，有威严也能亲切幽默；不机械笑着附和。",
    "reply_style": "自然简短的中文口语，结合上下文接话；事实不确定时坦率说明。",
}
ACTIVE = {"starting", "creating", "connecting", "waiting_stage", "ready", "sending", "draining"}


def _startup_source_version():
    """Identify this launch's source once, never relabel a live process on refresh.

    A new session snapshots files again for its archive; that disk snapshot can
    change during development without changing the already running Python code.
    """
    paths = [HERE / name for name in (*SOURCE_FILES, "external_appearance.py")]
    paths.extend(path for path in (HERE / "static").iterdir() if path.is_file())
    paths.extend(HERE / "sdk" / name for name in ("helper.mjs", "package.json", "package-lock.json"))
    manifest = {str(path.relative_to(HERE)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in paths if path.is_file()}
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class Studio:
    def __init__(self, data: Path = DATA, *, ai=None, preview=False, short_performance_test=False,
                 continuous_speech_test=False, long_speech_test=False, replay_fixture=None):
        if sum(bool(value) for value in (short_performance_test, continuous_speech_test, long_speech_test)) > 1:
            raise ValueError("单S、双动作组与长文试验不能同时启用")
        if short_performance_test and not preview:
            raise ValueError("短表演试验必须同时显式启用--preview")
        if continuous_speech_test and not preview:
            raise ValueError("连续口播试验必须同时显式启用--preview")
        if long_speech_test and not preview:
            raise ValueError("长文试验必须同时显式启用--preview")
        self.short_performance_test = bool(short_performance_test)
        self.continuous_speech_test = bool(continuous_speech_test)
        self.long_speech_test = bool(long_speech_test)
        self.semantic_speech = not any((self.short_performance_test, self.continuous_speech_test, self.long_speech_test))
        self.semantic_replay = bool(getattr(replay_fixture, "semantic_speech", False))
        if replay_fixture is not None and (not preview or not (
                self.semantic_replay and self.semantic_speech
                or not self.semantic_replay and self.long_speech_test)):
            raise ValueError("固定回放必须启用预览并匹配素材的语义或长文模式")
        self._runtime = {"pid": os.getpid(), "started_at": time.time(),
                         "source_version": _startup_source_version()}
        self.replay_fixture = replay_fixture
        self.replay_requests = {}
        self.data = data
        # Process-local test mode: restarting normally restores the OBS receiver.
        # Fixing the role before startup avoids any live-session handover.
        self.receiver_mode = "preview" if preview else "obs"
        data.mkdir(parents=True, exist_ok=True)
        os.chmod(data, 0o700)
        path = data / "local.json"
        if path.exists():
            self.config = json.loads(path.read_text())
        else:
            self.config = {"control_token": secrets.token_urlsafe(24),
                           "player_token": secrets.token_urlsafe(24), "port": 0,
                           "settings": dict(DEFAULT_SETTINGS)}
            save(path, self.config)
            os.chmod(path, 0o600)
        self.config.setdefault("intake", {"base_url": "http://127.0.0.1:8757", "room_id": ""})
        self.config["interactions"] = validate_interaction_settings(self.config.get("interactions", DEFAULT_INTERACTIONS))
        self.director = Director(self)
        self.automation = Automation(self, ai=ai)
        self.prefix = "/studio/" + self.config["control_token"]
        self.player_prefix = "/player/" + self.config["player_token"]
        self.origin = ""
        self.current = None
        self.avatar_library = AvatarLibrary(data)
        self.idle_library = IdleActionLibrary(data)
        self.idle_catalog_lock = asyncio.Lock()
        self.idle_enable_epoch = 0
        self.avatar_pending_id = None
        self.avatar_switch_library = False
        self.avatar_switch_requests = {}
        self.brain_token = secrets.token_urlsafe(32)
        self.browser_seen = time.monotonic()
        self.player = None
        self.paused = False
        self.comment_sequence = 0
        self.avatar_switch_holding = False
        self.avatar_switch_sending = False
        self.avatar_switch_receipt = {}
        self.avatar_switch_config = self.load_avatar_switch_config()
        self.lock = asyncio.Lock()
        self.watchdog = None
        self.closing = False
        self.done = asyncio.Event()
        self.external_game = ExternalGame(self)

    async def startup(self, app):
        await self.new_session()
        self.automation.start()
        self.watchdog = asyncio.create_task(self.watch())

    async def shutdown(self, app):
        self.closing = True
        if self.watchdog:
            self.watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.watchdog
        if self.current:
            self.current.latch_stop()
            # 共享 AI 在外部准备结束后关闭，旧主讲也先收尾，最后再关本场 HTTP。
            await self.current.shutdown(None, close_planner=self.automation.close)
        else:
            await self.automation.close()
        await self.avatar_library.close()
        await self.idle_library.close()

    async def new_session(self):
        selected_id = self.avatar_library.public()["selected_id"]
        if selected_id:
            # 先验配置，避免关闭旧场或被下面的资产未就绪兜底吞掉错误。
            self.asset_tts_voice(selected_id)
        self.idle_enable_epoch += 1
        if self.current:
            if not self.can_new():
                raise web.HTTPConflict(text="旧场结束或云端录像仍未确认")
            await self.automation.disconnect("new_session")
            await self.current.shutdown(None)
        self.automation.invalidate("new_session", replies=True)
        generation = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        self.current = LiveSession(self.data / "runs" / generation, "A",
                                   short_performance_test=self.short_performance_test,
                                   continuous_speech_test=self.continuous_speech_test,
                                   long_speech_test=self.long_speech_test,
                                   replay_fixture=self.replay_fixture)
        self.current.brain_token = self.brain_token
        self.current.diagnostics.register_secret(self.brain_token)
        self.current.brain_public = self.automation.brain_public
        self.current.timed_action_planner = getattr(self.automation.ai, "plan_timed_actions", None)
        self.current.delivery_gate = self.director.delivery_gate
        self.current.on_delivery_started = self.director.delivery_started
        self.current.on_delivery_finished = self.director.delivery_finished
        selected_id = self.avatar_library.public()["selected_id"]
        if selected_id:
            try:
                self.apply_library_asset(selected_id)
            except web.HTTPException:
                # Preserve the selected ID; opening is gated until its image and
                # settings are valid, never silently fall back to another face.
                self.current.apply_character(self.config["settings"])
        else:
            self.current.apply_character(self.config["settings"])
        if self.replay_fixture:
            self.replay_fixture.snapshot(self.current.out)
        # Fixed replay must not even inspect live model credentials or status.
        brain_state = self.automation.ai.public() if not self.replay_fixture else {}
        self.current.brain_context["routing"]["brain"] = ("固定样本回放，本轮不调用LLM或TTS" if self.replay_fixture
                                                         else f"{brain_state['provider']} {brain_state['model']}，独立筛选与回复上下文")
        save(self.current.out / "brain-context.json", self.current.brain_context)
        # Include the actual shipped UI/player in this run's immutable source record.
        import shutil
        for source in (HERE / "static").glob("*"):
            if source.is_file():
                shutil.copyfile(source, self.current.out / "source-used" / source.name)
        shutil.copyfile(HERE / "external_appearance.py", self.current.out / "source-used" / "external_appearance.py")
        sdk_snapshot = self.current.out / "source-used" / "sdk"
        sdk_snapshot.mkdir(exist_ok=True)
        for name in ("helper.mjs", "package.json", "package-lock.json"):
            shutil.copyfile(HERE / "sdk" / name, sdk_snapshot / name)
        save(self.current.out / "source-manifest.json", {
            str(p.relative_to(self.current.out / "source-used")): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (self.current.out / "source-used").rglob("*") if p.is_file()})
        await self.current.startup(None)
        self.director.reset()
        self.player = None
        self.paused = False
        self.comment_sequence = 0
        self.replay_requests = {}
        self.avatar_switch_holding = False
        self.avatar_switch_sending = False
        self.avatar_switch_receipt = {}
        self.avatar_pending_id = None
        self.avatar_switch_library = False
        self.avatar_switch_requests = {}
        self.browser_seen = time.monotonic()
        self.external_game.reset()
        self.write_runtime()

    @property
    def generation(self):
        return self.current.out.name

    @property
    def performance_holding(self):
        return self.avatar_switch_holding

    def write_runtime(self):
        if not self.origin or not self.current:
            return
        runtime = {**self._runtime, "origin": self.origin,
                   "url": self.origin + self.prefix + "/",
                   "player_url": self.origin + self.player_prefix + "/",
                   "brain_token": self.brain_token, "run_dir": str(self.current.out),
                   "generation": self.generation}
        save(self.data / "runtime.json", runtime)
        os.chmod(self.data / "runtime.json", 0o600)

    def can_preview_takeover(self):
        s, c = self.current, self.current.component
        # Explicit takeover is only a local preflight operation. Both start
        # latches matter: neither an accepted start nor an ambiguous creation
        # may lose its original receiver even if the state still reads idle.
        return (self.receiver_mode == "preview" and not self.closing
                and not s.started and not s.stopping and c.state == "idle"
                and not c._attempted and not c.creation_attempted
                and not c.creation_unknown and not c.live_id)

    def player_public(self):
        p = self.player or {}
        age = time.monotonic() - p.get("seen", 0)
        connected = bool(p and age < 15 and not p.get("released"))
        return {"connected": connected, "audio_live": connected and p.get("audio_live", False),
                "video_live": connected and p.get("video_live", False),
                "last_seen_seconds": round(age, 1) if p else None,
                "joined": bool(p.get("joined")), "error": p.get("error", "")}

    def idle_motion_avatar(self):
        return self.current.avatar_id or self.avatar_library.public()["selected_id"]

    def idle_motion_session_valid(self, session, avatar_id):
        return (session is self.current and session.started and not session.stopping
                and not self.closing and session.component.state not in TERMINAL
                and not self.replay_fixture and self.semantic_speech and bool(avatar_id)
                and avatar_id == self.idle_motion_avatar())

    def idle_motion_interrupt(self, session):
        """只有已接受的内容占用表演；候选弹幕的筛选不打断现有小动作。"""
        if (session is not self.current or self.performance_holding or self.automation.last_error
                or session.component.comment_id is not None or not session.work.empty()):
            return True
        topic = self.director.topic
        if topic and topic["state"] in {"running", "error"}:
            return True
        for item in session.comments:
            if item["status"] not in IN_FLIGHT:
                continue
            # 暂停的预备主讲稿没有占用播放资格；已交给发送队列的仍须等结束。
            if item.get("operator_mode") == "topic" and not item.get("delivery_started"):
                continue
            return True
        return False

    def idle_motion_allowed(self, session):
        state = session.component.idle_motions_public()
        if state["needs_recovery"] or state.get("recovery_failed"):
            return False
        if (not self.idle_motion_session_valid(session, self.idle_motion_avatar())
                or session.component.state != "ready" or self.idle_motion_interrupt(session)):
            return False
        task = self.automation.selection_task
        if task is not None and not task.done():
            return False
        if self.automation.enabled and not self.paused and self.automation.pool.snapshot()["candidate_count"]:
            return False
        if self.automation.interactions_waiting():
            return False
        return True

    def idle_motion_busy(self):
        state = self.current.component.idle_motions_public()
        return bool(state["enabled"] or state["task_running"] or state["needs_recovery"])

    def idle_motion_reason(self):
        if self.replay_fixture or not self.semantic_speech:
            return "固定回放及专项动作对照期间不开放待机小动作"
        session = self.current
        if self.closing or not session.started or session.stopping or session.component.state != "ready":
            return "请先开始会话，等待音画就绪"
        if self.performance_holding:
            return "请先结束当前画面观察"
        catalog = self.idle_library.public(self.idle_motion_avatar())
        if not catalog["configured"] or not catalog["matches_avatar"]:
            return catalog["error"] or "待机动作库不适用于当前形象"
        if self.idle_motion_busy():
            return "请先关闭当前待机并完成回正"
        return ""

    def idle_motions_public(self):
        state = self.current.component.idle_motions_public()
        reason = self.idle_motion_reason()
        test_reason = reason or ("请等待弹幕、主讲和主持安排全部空闲" if not self.idle_motion_allowed(self.current) else "")
        return {"scheduler": state, "catalog": self.idle_library.public(self.idle_motion_avatar()),
                "available": not reason, "reason": reason,
                "test_available": not test_reason, "test_reason": test_reason,
                "recover_available": bool(state["needs_recovery"] and
                    self.idle_motion_session_valid(self.current, self.idle_motion_avatar()) and
                    self.current.component.state == "ready" and not self.performance_holding and
                    self.current.component.comment_id is None and self.current.work.empty())}

    async def idle_motion_body(self, request, fields):
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"generation", *fields}:
            raise web.HTTPBadRequest(text="待机请求字段不正确")
        self.idle_motion_generation(body)
        return body

    def idle_motion_generation(self, body):
        if body["generation"] != self.generation:
            raise web.HTTPConflict(text="场次已变化，请刷新工作台后再试")

    async def idle_motion_enable(self, body, action_id=None):
        epoch = self.idle_enable_epoch
        async with self.idle_catalog_lock:
            async with self.lock:
                self.idle_motion_generation(body)
                if epoch != self.idle_enable_epoch:
                    raise web.HTTPConflict(text="待机启用已取消")
                reason = self.idle_motion_reason()
                if reason:
                    raise web.HTTPConflict(text=reason)
                session, avatar = self.current, self.idle_motion_avatar()
            # 网络复核不占用控制锁，关闭/停场无需等它返回。
            await self.idle_library.sync(avatar)
            async with self.lock:
                self.idle_motion_generation(body)
                if (epoch != self.idle_enable_epoch or session is not self.current
                        or avatar != self.idle_motion_avatar()):
                    raise web.HTTPConflict(text="待机启用已取消或形象已变化")
                reason = self.idle_motion_reason()
                if reason:
                    raise web.HTTPConflict(text=reason)
                try:
                    actions, settings = self.idle_library.snapshot(avatar)
                    allowed = lambda: self.idle_motion_allowed(session)
                    interrupt = lambda: self.idle_motion_interrupt(session)
                    valid = lambda: self.idle_motion_session_valid(session, avatar)
                    if action_id is None:
                        actions = [a for a in actions if a["enabled"]]
                        session.component.start_idle_motions(actions, settings, allowed, interrupt, valid)
                    else:
                        action = next((a for a in actions if a["id"] == action_id), None)
                        if action is None:
                            raise ValueError("待机动作不在已复核的动作库中")
                        session.component.test_idle_motion(action, allowed, interrupt, valid)
                except (IdleActionError, ValueError) as exc:
                    raise web.HTTPConflict(text=str(exc)) from None
                session.event("idle_motions_operator_enable", once=action_id, avatar_id=avatar)
                return web.json_response(self.idle_motions_public())

    async def idle_motions(self, request):
        body = await self.idle_motion_body(request, {"enabled"})
        if type(body["enabled"]) is not bool:
            raise web.HTTPBadRequest(text="待机开关必须为布尔值")
        if body["enabled"]:
            return await self.idle_motion_enable(body)
        async with self.lock:
            self.idle_motion_generation(body)
            self.idle_enable_epoch += 1
            try:
                await self.current.component.stop_idle_motions(recover=True)
            except IdleRecoveryError:
                raise web.HTTPConflict(text="待机已关闭，回正未确认；请检查连接后点击回正") from None
            return web.json_response(self.idle_motions_public())

    async def idle_motion_test(self, request):
        body = await self.idle_motion_body(request, {"action_id"})
        if not isinstance(body["action_id"], str):
            raise web.HTTPBadRequest(text="请选择待机动作")
        return await self.idle_motion_enable(body, body["action_id"])

    async def idle_motions_recover(self, request):
        body = await self.idle_motion_body(request, set())
        async with self.lock:
            self.idle_motion_generation(body)
            self.idle_enable_epoch += 1
            if not self.idle_motions_public()["recover_available"]:
                raise web.HTTPConflict(text="当前无需回正或场次已不可恢复")
            try:
                await self.current.component.recover_idle_motions()
            except IdleRecoveryError:
                raise web.HTTPConflict(text="回正仍未确认；请检查连接后再处理") from None
            return web.json_response(self.idle_motions_public())

    async def idle_motions_sync(self, request):
        body = await self.idle_motion_body(request, set())
        async with self.idle_catalog_lock:
            async with self.lock:
                self.idle_motion_generation(body)
                if self.idle_motion_busy():
                    raise web.HTTPConflict(text="请先关闭待机并完成回正，再刷新动作库")
                avatar = self.idle_motion_avatar()
            await self.idle_library.sync(avatar)
            async with self.lock:
                self.idle_motion_generation(body)
                if avatar != self.idle_motion_avatar():
                    raise web.HTTPConflict(text="形象已变化，请重新复核动作库")
                if self.idle_library.sync_status != "verified":
                    raise web.HTTPConflict(text=self.idle_library.error)
                return web.json_response(self.idle_motions_public())

    async def idle_motions_config(self, request):
        body = await self.idle_motion_body(request, {"min_interval_seconds", "max_interval_seconds", "enabled_ids"})
        async with self.idle_catalog_lock:
            async with self.lock:
                self.idle_motion_generation(body)
                if self.idle_motion_busy():
                    raise web.HTTPConflict(text="请先关闭待机并完成回正，再修改设置")
                if not self.idle_library.public(self.idle_motion_avatar())["matches_avatar"]:
                    raise web.HTTPConflict(text="待机动作库不适用于当前形象")
                try:
                    self.idle_library.configure({k: body[k] for k in ("min_interval_seconds", "max_interval_seconds")}, body["enabled_ids"])
                except IdleActionError as exc:
                    raise web.HTTPBadRequest(text=str(exc)) from None
                return web.json_response(self.idle_motions_public())

    def can_new(self):
        c = self.current.component.public()
        return (c["state"] in TERMINAL and (c["cloud_end_confirmed"] or c["no_cloud_session"])
                and not c["creation_outcome_unknown"] and not c["recording_pending"]
                and not self.current.background and self.current.work.empty()
                and self.current.component.comment_id is None)

    def startup_blocker(self):
        """Separate a waitable player connection from conditions requiring repair."""
        def blocked(code, reason):
            return {"code": code, "reason": reason}

        s = self.current
        if self.closing:
            return blocked("service_closing", "本机服务正在退出")
        if s.started or s.stopping or s.component.state != "idle":
            return blocked("session_used", "本场已使用；结束并保存后可新建会话")
        replay_reason = self.replay_identity_reason()
        if replay_reason:
            return blocked("replay_identity_mismatch", replay_reason)
        binding_reason = self.avatar_switch_binding_reason()
        if binding_reason:
            return blocked("avatar_binding_invalid", binding_reason)
        if not LEGACY_SETTINGS.exists():
            return blocked("credentials_missing", "已有语音与Vidu凭据配置未找到")
        if not self.replay_fixture and not self.automation.ai.configured():
            return blocked("model_not_configured", "请检查百炼大脑模型配置及已有百炼凭据文件")
        if not self.automation.running():
            return blocked("model_worker_stopped", "大脑回复工作循环未运行")
        if self.automation.last_error:
            return blocked("model_error", "请先处理百炼模型错误并手动恢复")
        # A stale cloud receipt cannot be fixed by waiting for the player.
        # Surface it first, before the only transient pre-start condition.
        try:
            s.assert_previous_cloud_ended()
        except web.HTTPException as exc:
            return blocked("previous_session_unconfirmed", exc.text)
        if not self.player_public()["connected"]:
            return blocked("player_not_connected", "请等待工作台预览连接" if self.receiver_mode == "preview" else "请先把播放地址加入 OBS 浏览器源")
        return blocked("", "")

    def blocking_reason(self):
        return self.startup_blocker()["reason"]

    def settings_editing_allowed(self):
        return (not self.replay_fixture and not self.closing and not self.current.started and not self.current.stopping
                and self.current.component.state == "idle" and not self.performance_holding)

    def replay_identity_reason(self):
        if not self.replay_fixture:
            return ""
        if (str(self.current.avatar_id or "") != str(self.replay_fixture.avatar_id or "")
                or hashlib.sha256(self.current.image.read_bytes()).hexdigest() != self.replay_fixture.image_sha256):
            return "固定对照的数字人或底图与原场次不一致，请检查后再开场"
        return ""

    def replay_public(self):
        if not self.replay_fixture:
            return None
        available = (self.current.started and not self.current.stopping and not self.closing
                     and self.current.component.state == "ready" and not self.director.delivery_busy()
                     and not self.performance_holding and not self.replay_identity_reason()
                     and not any(c["status"] in IN_FLIGHT for c in self.current.comments))
        plan = self.replay_fixture.plan
        return {**self.replay_fixture.public(), "available": bool(available),
                "timeline": plan["segments"][0]["timeline"], "interaction_context": plan["observation"]}

    async def replay(self, request):
        body = await request.json()
        if (not isinstance(body, dict) or set(body) != {"generation", "fingerprint", "request_id"}
                or not isinstance(body["request_id"], str)
                or not re.fullmatch(r"[A-Za-z0-9-]{16,80}", body["request_id"])):
            raise web.HTTPBadRequest(text="回放请求格式不正确")
        async with self.lock:
            fixture = self.replay_fixture
            if (not fixture or body["generation"] != self.generation
                    or body["fingerprint"] != fixture.fingerprint):
                raise web.HTTPConflict(text="固定素材或场次已变化，请刷新工作台")
            # 同一点击重传只返回原条目；用户下一次明确点击才创建新回放。
            if body["request_id"] in self.replay_requests:
                return web.json_response({"id": self.replay_requests[body["request_id"]], "duplicate": True})
            if not self.replay_public()["available"]:
                raise web.HTTPConflict(text="请等待本场音画就绪且当前回放发送结束")
            description = (fixture.public()["comparison_label"] + "；复用原音频与最终语义位置" if self.semantic_replay else
                           "复用原音频，原四动作分为两组L，组间加Q" if fixture.action_kind == "L" and fixture.long_action_group_count == 2 else
                           "复用原音频，原四动作合为一条L描述" if fixture.action_kind == "L" else
                           "复用原音频与原动作，仅组间距改为5秒")
            if fixture.second_l_at_seconds is not None:
                description += "；第二L在“百废待兴”7.36秒处发送"
            c = self.enqueue_comment(description, "固定对照",
                                     source="operator", input_kind="operator", operator_mode="once",
                                     replay_fixture_fingerprint=fixture.fingerprint,
                                     replay_source_comment=fixture.comment_id)
            c["status"] = "processing"
            self.accept_plan(self.current, c, fixture.plan)
            self.replay_requests[body["request_id"]] = c["id"]
            return web.json_response({"id": c["id"]})

    @staticmethod
    def validated_settings(body):
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text="角色设定必须为对象")
        for key, limit in (("name", 30), ("persona", 1000), ("reply_style", 500)):
            if (not isinstance(body.get(key), str) or not 1 <= len(body[key].strip()) <= limit
                    or "\x00" in body[key]):
                raise web.HTTPBadRequest(text="名称、人设和回复偏好均需填写且不超过限制")
        return {key: body[key].strip() for key in DEFAULT_SETTINGS}

    def common_transition(self):
        path = self.data / "avatar-transition.json"
        if not path.exists():
            return {}, "尚未配置共用转场视频"
        try:
            from motion_component import MotionComponent
            if path.is_symlink() or path.stat().st_size > 8192:
                raise ValueError()
            value = json.loads(path.read_text())
            if (not isinstance(value, dict) or set(value) != {"switch_videos", "transition_label"}
                    or not isinstance(value["transition_label"], str)
                    or not 1 <= len(value["transition_label"]) <= 40):
                raise ValueError()
            MotionComponent._switch_payload("1", value["switch_videos"])
            if any(not url.startswith("https://") for url in value["switch_videos"]):
                raise ValueError()
            return value, ""
        except (OSError, ValueError, TypeError, KeyError):
            return {}, "共用转场配置无效，请核对本机配置"

    def library_asset(self, asset_id, *, ready=True):
        asset = self.avatar_library.get(asset_id)
        if not asset:
            raise web.HTTPNotFound(text="未找到数字人资产")
        if ready and (asset["status"] != "success" or not self.avatar_library.image_path(asset_id)):
            raise web.HTTPConflict(text="数字人尚未就绪或底图未缓存，请同步或刷新后再试")
        return asset

    def asset_tts_voice(self, asset_id):
        voices = self.config.get("tts_voices", {})
        if not isinstance(voices, dict):
            raise ValueError("本机 tts_voices 配置必须是资产 ID 到音色 ID 的映射")
        if asset_id not in voices:
            return None
        voice = voices[asset_id]
        if not valid_voice_id(voice):
            raise ValueError("本机 tts_voices 音色 ID 无效")
        return voice

    def apply_library_asset(self, asset_id, *, confirmed_switch=False):
        asset = self.library_asset(asset_id)
        settings = self.validated_settings(asset["settings"])
        voice = self.asset_tts_voice(asset_id)
        self.current.apply_avatar(self.avatar_library.image_path(asset_id), asset_id, settings,
                                  reset_history=confirmed_switch)
        # 每次确认资产都重设，选到未绑定音色的资产时清除上一人的覆盖。
        self.current.tts_voice = voice
        self.config["settings"] = dict(settings)
        save(self.data / "local.json", self.config)
        if confirmed_switch:
            self.automation.invalidate("avatar_identity_confirmed", replies=True)
            self.director.reset()
            self.current.checkpoint()

    def library_switch_reason(self):
        s = self.current
        if s.component.idle_motions_public().get("recovery_failed"):
            return "请先在待机面板显式回正，再切换形象"
        if self.closing or not s.started or s.stopping or s.component.state != "ready":
            return "请等待本场音画就绪且当前发送结束"
        _, reason = self.common_transition()
        if reason:
            return reason
        if (s.component.comment_id is not None or not s.work.empty()
                or any(c["status"] in IN_FLIGHT for c in s.comments)
                or self.automation.active_item is not None
                or self.automation.reply_task is not None):
            return "请等待待处理台词与动作全部发送结束"
        if self.director.topic and self.director.topic["state"] == "running":
            return "请先暂停主讲话题，再切换数字人"
        return ""

    def current_avatar_id(self):
        return (self.current.avatar_id if self.current.started
                and self.current.component.state not in TERMINAL else None)

    def avatar_catalogue(self):
        editable = self.settings_editing_allowed()
        selected = self.avatar_library.public()["selected_id"]
        return {**self.avatar_library.public(),
                "current_id": self.current_avatar_id(),
                "selected_id": selected, "pending_id": self.avatar_pending_id,
                "switch_pending": self.avatar_switch_holding,
                "editing_allowed": editable, "can_select": editable,
                "can_switch": not self.library_switch_reason() and not self.avatar_switch_holding,
                "blocking_reason": ("请先新建会话，再选择开场角色或修改角色语气" if self.can_new()
                    else self.library_switch_reason() if not editable else "")}

    async def avatars_state(self, request):
        return web.json_response(self.avatar_catalogue())

    async def avatars_image(self, request):
        path = self.avatar_library.image_path(request.match_info["avatar_id"])
        if path is None:
            raise web.HTTPNotFound(text="底图尚未缓存")
        return web.FileResponse(path)

    async def avatars_sync(self, request):
        await self.avatar_library.sync()
        return web.json_response(self.avatar_catalogue())

    async def avatars_refresh(self, request):
        value = await self.avatar_library.refresh(request.match_info["avatar_id"])
        return web.json_response(value)

    async def avatars_settings(self, request):
        body = self.validated_settings(await request.json())
        async with self.lock:
            if not self.settings_editing_allowed():
                raise web.HTTPConflict(text="角色设置只可在空闲会话修改；结束后请先新建会话")
            asset_id = request.match_info["avatar_id"]
            self.library_asset(asset_id, ready=False)
            value = self.avatar_library.update_settings(asset_id, body)
            if asset_id == self.avatar_library.public()["selected_id"]:
                self.apply_library_asset(asset_id)
        return web.json_response(value)

    async def avatars_create(self, request):
        # Keep JSON control messages small; only this streaming route accepts
        # image bytes, with its own aggregate cap and no temporary public file.
        async with self.lock:
            if not self.settings_editing_allowed():
                raise web.HTTPConflict(text="上传只可在空闲会话进行；结束后请先新建会话")
            reader = await request.multipart()
            fields, raw, filename, total = {}, None, None, 0
            async for part in reader:
                if part.name not in {"image", "name", "persona", "reply_style", "request_id"}:
                    raise web.HTTPBadRequest(text="上传表单字段不正确")
                chunks, length = [], 0
                while True:
                    chunk = await part.read_chunk(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    length += len(chunk)
                    total += len(chunk)
                    if total >= 20 * 1024 * 1024 + 16384 or (part.name != "image" and length > 5000):
                        raise web.HTTPRequestEntityTooLarge(max_size=20 * 1024 * 1024, actual_size=total)
                content = b"".join(chunks)
                if part.name == "image":
                    if raw is not None or not part.filename:
                        raise web.HTTPBadRequest(text="请只选择一张图片")
                    raw, filename = content, part.filename
                else:
                    if part.name in fields:
                        raise web.HTTPBadRequest(text="上传表单字段重复")
                    fields[part.name] = content.decode("utf-8")
            settings = self.validated_settings(fields)
            if raw is None or "request_id" not in fields:
                raise web.HTTPBadRequest(text="请提供图片与上传请求标识")
            result = await self.avatar_library.create(raw, filename, fields["name"], settings, fields["request_id"])
            status = {"creation_unknown": "unknown", "creation_failed": "failed"}.get(result["status"], result["status"])
            return web.json_response({**result, "status": status})

    async def avatars_select(self, request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"avatar_id", "generation"}:
            raise web.HTTPBadRequest(text="选择请求仅接受资产与场次标识")
        async with self.lock:
            if body["generation"] != self.generation:
                raise web.HTTPConflict(text="场次已变化，请刷新后再试")
            if not self.settings_editing_allowed():
                raise web.HTTPConflict(text="请先结束并新建会话，再选择开场角色")
            self.apply_library_asset(body["avatar_id"])
            self.avatar_library.set_selected(body["avatar_id"])
        return web.json_response(self.avatar_catalogue())

    async def avatars_switch(self, request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"avatar_id", "generation", "request_id"}:
            raise web.HTTPBadRequest(text="切换请求仅接受资产、场次与请求标识")
        return await self.perform_library_switch(body)

    async def perform_library_switch(self, body):
        ident = body["request_id"]
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", ident):
            raise web.HTTPBadRequest(text="请求标识格式不正确")
        async with self.lock:
            if body["generation"] != self.generation:
                raise web.HTTPConflict(text="场次已变化，请刷新后再试")
            target = self.library_asset(body["avatar_id"])
            self.validated_settings(target["settings"])
            self.asset_tts_voice(body["avatar_id"])
            previous = self.avatar_switch_requests.get(ident)
            if previous:
                if previous["avatar_id"] != body["avatar_id"]:
                    raise web.HTTPConflict(text="同一切换请求不能更换数字人")
                latest = self.avatar_switch_public()["receipt"]
                receipt = latest if latest.get("request_id") == ident else previous["receipt"]
                return web.json_response({**receipt, "duplicate": True})
            reason = self.library_switch_reason()
            if reason or self.avatar_switch_holding:
                raise web.HTTPConflict(text=reason or "请先确认当前切换画面")
            transition, _ = self.common_transition()
            from motion_component import MotionComponent
            MotionComponent._switch_payload(body["avatar_id"], transition["switch_videos"])
            s = self.current
            self.avatar_pending_id = body["avatar_id"]
            self.avatar_switch_library = True
            self.avatar_switch_holding = self.avatar_switch_sending = True
            self.avatar_switch_receipt = {"request_id": ident, "avatar_id": body["avatar_id"],
                "status": "pending", "sent": False, "operator_confirmed": False}
            self.avatar_switch_requests[ident] = {"avatar_id": body["avatar_id"], "receipt": self.avatar_switch_receipt}
            self.automation.invalidate("avatar_switch_operator_review", replies=True)
            def valid():
                return (s is self.current and body["generation"] == self.generation
                        and self.avatar_switch_holding and not self.library_switch_reason())
            try:
                self.idle_enable_epoch += 1
                await s.component.stop_idle_motions(recover=True, explicit=False)
                result = await s.component.switch_avatar(body["avatar_id"], transition["switch_videos"], ident, valid)
                self.avatar_switch_receipt = result
                if not result.get("sent") and result.get("status") == "cancelled":
                    # An explicit pre-write rejection is safe to release. Failed
                    # and uncertain writes retain the hold and pending identity.
                    self.avatar_switch_holding = False
                    self.avatar_pending_id = None
                return web.json_response(result)
            except IdleRecoveryError:
                # 回正之前尚未触发Type16，无需把未发生的切换锁在观察状态。
                self.avatar_switch_holding = False
                self.avatar_pending_id = None
                self.avatar_switch_receipt.update(status="cancelled", error_code="IDLE_RECOVERY_REQUIRED")
                return web.json_response(self.avatar_switch_receipt, status=409)
            except asyncio.CancelledError:
                self.avatar_switch_receipt.update(status="send_unconfirmed", error_code="SWITCH_SEND_UNCONFIRMED")
                raise
            except Exception:
                self.avatar_switch_receipt.update(status="send_unconfirmed", error_code="SWITCH_SEND_UNCONFIRMED")
                return web.json_response(self.avatar_switch_receipt)
            finally:
                self.avatar_switch_requests[ident]["receipt"] = dict(self.avatar_switch_receipt)
                self.avatar_switch_sending = False

    def load_avatar_switch_config(self):
        # 试验素材只由本机配置指定；浏览器、评论与模型不能提供目标URL或形象ID。
        self.avatar_switch_config_error = ""
        path = self.data / "type16-test.json"
        if not path.exists() and not path.is_symlink():
            return {}
        try:
            if path.stat().st_size > 8192:
                raise ValueError()
            value = json.loads(path.read_text())
            required = {"avatar_id", "switch_videos", "source_image_sha256", "transition_label"}
            if (not isinstance(value, dict) or set(value) not in (required, required | {"initial_avatar", "target_image"})
                    or not self._valid_avatar_binding(value)
                    or not isinstance(value["transition_label"], str) or not 1 <= len(value["transition_label"]) <= 40
                    or not isinstance(value["switch_videos"], list) or len(value["switch_videos"]) != 1
                    or not isinstance(value["switch_videos"][0], str) or not value["switch_videos"][0].startswith("https://")
                    or len(value["switch_videos"][0]) > 2048):
                raise ValueError()
            if "initial_avatar" in value:
                initial = value["initial_avatar"]
                if (not isinstance(initial, dict) or set(initial) != {"avatar_id", "source_image_sha256"}
                        or not self._valid_avatar_binding(initial)):
                    raise ValueError()
                self._avatar_target_path(value["target_image"])
            return value
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            # 存在但损坏的配置不能当成“未配置”，否则会意外回落到底图开场。
            self.avatar_switch_config_error = "Type16试验配置无效，请核对本机配置"
            return {}

    @staticmethod
    def _valid_avatar_binding(value):
        return (isinstance(value.get("avatar_id"), str)
                and re.fullmatch(r"[0-9]{1,64}", value["avatar_id"]) is not None
                and isinstance(value.get("source_image_sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", value["source_image_sha256"]) is not None)

    @staticmethod
    def _avatar_target_path(name):
        if (not isinstance(name, str) or not 1 <= len(name) <= 255
                or "/" in name or "\\" in name or name in {".", ".."}
                or Path(name).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}):
            raise ValueError()
        assets = (HERE / "assets").resolve()
        target = (assets / name).resolve()
        # resolve后再检查父目录，防止assets中的符号链接逃出素材目录。
        if target.parent != assets or not target.is_file() or not 0 < target.stat().st_size < 20_000_000:
            raise ValueError()
        return target

    def avatar_switch_binding_reason(self):
        selected = self.avatar_library.public()["selected_id"]
        if selected:
            bound = self.current.avatar_id
            asset = self.avatar_library.get(bound) if bound else None
            if (not asset or asset["status"] != "success" or not self.avatar_library.image_path(bound)
                    or asset.get("sha256") != self.current.variant_info["image_sha256"]
                    or (not self.current.started and bound != selected)):
                return "开场资产、缓存底图或角色绑定未就绪，请重新选择数字人"
            return ""
        if self.avatar_switch_config_error:
            return self.avatar_switch_config_error
        config = self.avatar_switch_config
        if not config:
            return ""
        initial = config.get("initial_avatar", config)
        if initial["source_image_sha256"] != self.current.variant_info["image_sha256"]:
            return "开场形象资产与本场底图不一致，请先核对配置"
        if "initial_avatar" in config:
            try:
                target = self._avatar_target_path(config["target_image"])
                if hashlib.sha256(target.read_bytes()).hexdigest() != config["source_image_sha256"]:
                    return "Type16目标图像与资产绑定不一致，请核对本机素材"
            except (OSError, ValueError, KeyError, RuntimeError):
                return "Type16目标图像不存在或不符合素材目录、格式和大小约束"
        return ""

    def avatar_switch_blocking_reason(self):
        if self.current.component.idle_motions_public().get("recovery_failed"):
            return "请先在待机面板显式回正，再切换形象"
        if self.avatar_library.public()["selected_id"]:
            return self.library_switch_reason()
        s = self.current
        binding_reason = self.avatar_switch_binding_reason()
        if binding_reason:
            return binding_reason
        if not self.avatar_switch_config:
            return "尚未配置 Type16 试验素材"
        if self.closing or not s.started or s.stopping or s.component.state != "ready":
            return "请等待本场音画就绪且当前发送结束"
        if (s.component.comment_id is not None or not s.work.empty()
                or any(c["status"] in IN_FLIGHT for c in s.comments)):
            return "请等待待处理台词与动作全部发送结束"
        if self.director.topic and self.director.topic["state"] == "running":
            return "请先暂停主讲话题，再单独观察切换效果"
        return ""

    def avatar_switch_public(self):
        library_mode = bool(self.avatar_library.public()["selected_id"]) or self.avatar_switch_library
        reason = self.library_switch_reason() if library_mode else self.avatar_switch_blocking_reason()
        candidate = self.current.component.public().get("avatar_switch") or {}
        receipt = (candidate if candidate.get("request_id") == self.avatar_switch_receipt.get("request_id")
                   else self.avatar_switch_receipt)
        if self.avatar_switch_library and self.avatar_switch_receipt.get("operator_confirmed"):
            receipt = {**receipt, "operator_confirmed": True}
        transition, transition_error = self.common_transition() if library_mode else (self.avatar_switch_config, "")
        confirmable = (not library_mode or (receipt.get("sent") is True and receipt.get("status") == "sent"))
        if library_mode and self.avatar_switch_holding and not confirmable and not self.avatar_switch_sending:
            reason = "切换失败或发送结果未知；请结束会话核对，不能确认继续"
        return {"configured": bool(transition) and not transition_error,
                "available": not reason and not self.avatar_switch_holding,
                "reason": reason or ("请观察转场后的画面，再点击继续表演" if self.avatar_switch_holding else ""),
                "holding": self.avatar_switch_holding,
                "resume_available": self.avatar_switch_holding and not self.avatar_switch_sending and not reason and confirmable,
                "receipt": receipt, "transition_label": transition.get("transition_label", "")}

    async def avatar_switch(self, request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"generation", "request_id"}:
            raise web.HTTPBadRequest(text="切换请求仅接受场次与请求标识")
        if self.avatar_library.public()["selected_id"]:
            return await self.perform_library_switch({**body, "avatar_id": self.current.avatar_id})
        ident = body["request_id"]
        if (not isinstance(ident, str) or not 8 <= len(ident) <= 128 or not ident.isascii()
                or not ident.replace("-", "").replace("_", "").isalnum()):
            raise web.HTTPBadRequest(text="请求标识格式不正确")
        async with self.lock:
            if body["generation"] != self.generation:
                raise web.HTTPConflict(text="场次已变化，请刷新工作台后再试")
            latest = self.avatar_switch_public()["receipt"]
            if latest.get("request_id") == ident:
                return web.json_response({**latest, "duplicate": True})
            reason = self.avatar_switch_blocking_reason()
            if reason or self.avatar_switch_holding:
                raise web.HTTPConflict(text=reason or "请先观察当前切换，再点击继续表演")
            s, config = self.current, self.avatar_switch_config
            self.avatar_switch_holding = self.avatar_switch_sending = True
            self.avatar_switch_receipt = {"request_id": ident, "status": "pending", "sent": False,
                                          "operator_confirmed": False}
            # 没有成功回执就不能按0.04秒推算切换已完成。先锁住新任务，
            # 清掉尚未选中的评论，等操作者看见动态画面后显式恢复。
            self.automation.invalidate("avatar_switch_operator_review")
            def valid():
                return (s is self.current and body["generation"] == self.generation
                        and self.avatar_switch_holding and not self.avatar_switch_blocking_reason())
            try:
                self.idle_enable_epoch += 1
                await s.component.stop_idle_motions(recover=True, explicit=False)
                result = await s.component.switch_avatar(config["avatar_id"], config["switch_videos"], ident, valid)
                self.avatar_switch_receipt = result
                if not result.get("sent") and result.get("status") == "cancelled":
                    self.avatar_switch_holding = False
                return web.json_response(result)
            except IdleRecoveryError:
                self.avatar_switch_holding = False
                self.avatar_switch_receipt.update(status="cancelled", error_code="IDLE_RECOVERY_REQUIRED")
                return web.json_response(self.avatar_switch_receipt, status=409)
            finally:
                self.avatar_switch_sending = False

    async def avatar_switch_resume(self, request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"generation"}:
            raise web.HTTPBadRequest(text="恢复请求只接受当前场次")
        async with self.lock:
            if body["generation"] != self.generation:
                raise web.HTTPConflict(text="场次已变化，请刷新工作台后再试")
            if not self.avatar_switch_public()["resume_available"]:
                raise web.HTTPConflict(text="当前不能恢复表演，请查看会话状态")
            if self.avatar_switch_library and self.avatar_pending_id:
                self.apply_library_asset(self.avatar_pending_id, confirmed_switch=True)
                self.avatar_library.set_selected(self.avatar_pending_id)
                self.avatar_pending_id = None
                self.avatar_switch_receipt["operator_confirmed"] = True
            self.avatar_switch_holding = False
            self.current.event("avatar_switch_operator_confirmed", operator_confirmed=self.avatar_switch_library)
            return web.json_response({"ok": True})

    def snapshot(self):
        value = self.current.snapshot()
        value["session"]["generation"] = self.generation
        value["brain"] = self.automation.brain_public()
        value["intake"] = self.automation.public()
        value["interactions"] = {**self.automation.interactions.public(),
                                 "supported": self.automation.interactions_supported()}
        value["director"] = self.director.public()
        blocker = self.startup_blocker()
        reason = blocker["reason"]
        value["studio"] = {"paused": self.paused, "obs_url": self.origin + self.player_prefix + "/",
                           "runtime": dict(self._runtime),
                           "receiver_mode": self.receiver_mode,
                           "short_performance_test": self.short_performance_test,
                           "continuous_speech_test": self.continuous_speech_test,
                           "long_speech_test": self.long_speech_test,
                           "semantic_speech": self.semantic_speech,
                           "semantic_replay": self.semantic_replay,
                           "dialogue_action_policy": ({"name": "A", "action_kind": DIALOGUE_ACTION_KIND,
                               "ge_lead_seconds": DIALOGUE_GE_LEAD_SECONDS,
                               "short_to_q_seconds": DIALOGUE_S_TO_Q_SECONDS}
                               if self.semantic_speech and not self.replay_fixture else None),
                           "fixed_replay": self.replay_public(),
                           "idle_motions": self.idle_motions_public(),
                           "player": self.player_public(), "settings": dict(self.config["settings"]),
                           "can_start": not reason,
                           "blocking_code": blocker["code"],
                           "blocking_reason": reason, "can_new": self.can_new(),
                           "avatar_switch": self.avatar_switch_public(),
                           "settings_editing_allowed": self.settings_editing_allowed(),
                           "avatar_current_id": self.current_avatar_id(),
                           "avatar_selected_id": self.avatar_library.public()["selected_id"]}
        value["diagnostics"] = {"recent": self.current.diagnostics.snapshot().get("records", [])[-12:]}
        return value

    async def watch(self):
        while True:
            await asyncio.sleep(3)
            s = self.current
            if getattr(s, "worker", None) and s.worker.done() and not s.stopping:
                await s.fail_worker(RuntimeError("answer_worker_exited"))
            if not s.started or s.stopping or s.component.state in TERMINAL:
                continue
            now = time.monotonic()
            reason = ""
            if now - self.browser_seen > 35:
                reason = "控制台心跳中断"
            elif not self.player_public()["connected"]:
                reason = "播放器心跳中断"
            elif (now - self.automation.last_tick > 90
                  or (self.automation.error_since is not None and now - self.automation.error_since > 90)):
                reason = "大脑异常超过90秒"
            elif self.player and self.player.get("joined"):
                if not self.player.get("audio_live") or not self.player.get("video_live"):
                    reason = "远端音画轨道中断"
                elif now - self.player.get("frame_seen", now) > 15:
                    reason = "视频解码帧停止推进"
            if reason:
                s.event("studio_connection_lost", reason=reason)
                self.automation.invalidate("watchdog_stop", replies=True)
                await s.request_stop(reason)

    def owner(self, body):
        if (not self.player or body.get("client_id") != self.player["client_id"]
                or body.get("generation") != self.generation or self.player.get("released")):
            raise web.HTTPConflict(text="此播放器未持有本场资格")
        return self.player

    async def start(self, request):
        # Lock and started flag precede all awaited preflight work. Concurrent
        # button presses can never create a second paid cloud session.
        async with self.lock:
            reason = self.blocking_reason()
            if reason:
                raise web.HTTPConflict(text=reason)
            s = self.current
            # 默认同资产对照；明确配置交叉试验时，初始资产与切换目标各自验图。
            # 配置损坏由blocking_reason拒绝，无配置才保留原来的底图开场。
            initial_avatar_id = (self.current.avatar_id if self.avatar_library.public()["selected_id"] else
                                 self.avatar_switch_config.get("initial_avatar", self.avatar_switch_config).get("avatar_id"))
            if not self.replay_fixture:
                load_bailian()
            key = load_vidu()
            s.started = True
            self.browser_seen = time.monotonic()
            save(s.out / "brain-config.json", ({"provider": "fixed_replay", "model": None,
                 "model_calls_disabled": True, "tts_calls_disabled": True,
                 "replay_fixture_fingerprint": self.replay_fixture.fingerprint} if self.replay_fixture else
                 {"provider": self.automation.ai.public()["provider"], "model": self.automation.ai.public()["model"]}))
            async def begin():
                try:
                    await s.verify_previous_cloud_ended(key)
                    if s.stopping:
                        return
                    save(s.out / "start-attempted.json", {"time": time.time(), "generation": self.generation})
                    if initial_avatar_id:
                        await s.component.start(key, avatar_id=initial_avatar_id)
                    else:
                        await s.component.start(key)
                except Exception as exc:
                    s.event("studio_start_failed", error_type=type(exc).__name__)
                    s.component.error = "开场核验或连接失败；请查看诊断记录"
                    await s.request_stop("开场失败")
            s.spawn(begin())
        return web.json_response({"ok": True})

    async def comments(self, request):
        s = self.current
        if self.performance_holding:
            raise web.HTTPConflict(text="请先结束当前画面观察，再接收表演")
        if self.paused:
            raise web.HTTPConflict(text="已暂停接单，现有工作继续处理")
        if s.stopping or s.component.state not in {"ready", "sending", "draining"}:
            raise web.HTTPConflict(text="请等待本场音画就绪")
        body = await request.json()
        text, name = body.get("text"), body.get("nickname", "观众")
        if (not isinstance(text, str) or not 1 <= len(text.strip()) <= 160
                or not isinstance(name, str) or not 1 <= len(name.strip()) <= 16):
            raise web.HTTPBadRequest(text="弹幕需1至160字，昵称需1至16字")
        if sum(c["status"] in IN_FLIGHT and c.get("operator_mode") != "topic"
               and c.get("input_kind") != "interaction" for c in s.comments) >= 5:
            raise web.HTTPConflict(text="已有五条弹幕排队，请稍候")
        c = self.enqueue_comment(text.strip(), name.strip())
        return web.json_response({"id": c["id"]})

    def enqueue_comment(self, text, name, *, checkpoint=True, **metadata):
        if self.external_game.active and metadata.get("input_kind") != "external_game":
            raise web.HTTPConflict(text="当前由游戏独占口播")
        if self.replay_fixture and metadata.get("replay_fixture_fingerprint") != self.replay_fixture.fingerprint:
            raise web.HTTPConflict(text="本次固定对照只接受已核验样本，不生成新台词或动作")
        if self.short_performance_test:
            try:
                short_performance_input_mode(metadata)
            except ValueError as error:
                raise web.HTTPConflict(text=str(error)) from None
        if self.continuous_speech_test:
            try:
                continuous_speech_input_mode(metadata)
            except ValueError as error:
                raise web.HTTPConflict(text=str(error)) from None
        if self.long_speech_test:
            try:
                long_speech_input_mode(metadata)
            except ValueError as error:
                raise web.HTTPConflict(text=str(error)) from None
        if self.performance_holding:
            raise web.HTTPConflict(text="画面观察期间暂不接收表演")
        s = self.current
        if s.worker_fault:
            raise web.HTTPConflict(text="发送工作循环已故障，请结束本场后检查")
        self.comment_sequence += 1
        # Keep long sessions bounded in RAM; older completed turns remain on disk.
        if len(s.comments) >= 300:
            archived = s.comments[:-250]
            if any(c["status"] in IN_FLIGHT for c in archived):
                raise web.HTTPConflict(text="队列状态异常，请结束会话后检查")
            with (s.out / "conversation-archive.jsonl").open("a") as f:
                for c in archived:
                    f.write(json.dumps({k: v for k, v in c.items() if k not in {"lease", "fingerprint", "plan"}}, ensure_ascii=False) + "\n")
            s.comments = s.comments[-250:]
        c = {"id": f"c{self.comment_sequence:04d}", "nickname": name.strip(), "text": text.strip(),
             "input_kind": "viewer",
             "status": "pending", "answer": "", "emotion": "", "error": "", "segments_sent": 0,
             "motion_receipts": [], "reading_segments_sent": 0,
             "usage": {"input_tokens": 0, "output_tokens": 0, "cost_cny": 0.0}, "created_at": time.time(),
             "persona_epoch": s.persona_epoch, **metadata}
        s.comments.append(c)
        s.signal.set()
        if checkpoint:
            s.checkpoint()
        return c

    def reply_history(self, s, current=None):
        motion = s.component.public().get("motion_events", [])
        return [{"nickname": c["nickname"], "comment": c["text"], "answer": c["answer"],
                 "viewer_keys": self.automation.turn_viewer_keys(c),
                 "input_kind": c.get("input_kind", "viewer"), "operator_mode": c.get("operator_mode"),
                 "emotion": c["emotion"], "status": c["status"], "reading": c.get("reading"),
                 "reading_status": c.get("reading_status"),
                 "reading_segments_sent": c.get("reading_segments_sent", 0),
                 "reading_motion_sent": any(e.get("comment_id") == c["id"] and e.get("role") == "R" for e in motion)}
                for c in s.comments if c is not current and c["answer"] and c["status"] == "sent"
                and c.get("persona_epoch", 0) == s.persona_epoch][-6:]

    def accept_plan(self, s, c, body, *, enqueue=True):
        # Shared synchronous boundary: the model cannot select a lease, session,
        # endpoint or executable command. No await between this gate and queueing.
        if (s is not self.current or self.performance_holding or s.stopping or s.worker_fault or s.component.state in TERMINAL or c["status"] != "processing"
                or c.get("cancel_requested")):
            raise ValueError("reply_no_longer_current")
        operator = c.get("input_kind") == "operator"
        if operator and not self.director.is_current(c):
            raise ValueError("director_no_longer_current")
        if c.get("input_kind") == "external_game":
            plan = self.external_game.validate_plan(c, body)
        elif self.semantic_replay:
            # 只有从本地已核验素材复制的完整计划能进入固定对照队列。
            if (c.get("replay_fixture_fingerprint") != self.replay_fixture.fingerprint
                    or body != self.replay_fixture.plan):
                raise ValueError("固定回放素材身份或计划不匹配")
            plan = self.replay_fixture.plan
        elif self.long_speech_test:
            # 长文要在第一次解析时选用完整合同，不能先经过旧30字合同再放宽。
            long_speech_input_mode(c)
            if self.replay_fixture:
                if c.get("replay_fixture_fingerprint") != self.replay_fixture.fingerprint:
                    raise ValueError("固定回放素材身份不匹配")
                plan = (validate_long_action_replay_plan(body, group_count=self.replay_fixture.long_action_group_count)
                        if self.replay_fixture.action_kind == "L" else
                        validate_long_speech_plan(body, group_interval=self.replay_fixture.group_interval))
                if plan != self.replay_fixture.plan:
                    raise ValueError("固定回放计划已被改变")
            else:
                plan = parse_long_speech_plan(body)
        elif c.get("unified_turn") and c.get("input_kind") == "interaction":
            from interaction_delivery import compose_turn_plan
            trusted = compose_turn_plan(c["interaction_jobs"])
            if body != trusted:
                raise ValueError("interaction_plan_changed")
            plan = (parse_director_plan(trusted, mode="motion") if is_motion_item(c)
                    else parse_dialogue_semantic_plan(trusted, mode="once"))
        elif c.get("input_kind") == "interaction":
            # 互动权限由服务端入队时决定，观众数据不能自选主持动作分支。
            plan = validate_interaction_plan(c["interaction_job"])
            if is_motion_item(c) != (c["interaction_job"].get("mode") == "motion"):
                raise ValueError("interaction_mode_changed")
        elif self.semantic_speech and not (operator and c.get("operator_mode") == "motion"):
            plan = parse_dialogue_semantic_plan(body, source_text=None if operator else c["text"],
                                       mode=c.get("operator_mode") if operator else None)
        else:
            plan = parse_director_plan(body, mode=c["operator_mode"], historical_contract=self.short_performance_test) if operator else parse_plan(body, source_text=c["text"])
        if self.short_performance_test:
            mode = short_performance_input_mode(c)
            validate_short_performance_plan(plan, motion_only=mode == "motion")
        if self.continuous_speech_test:
            continuous_speech_input_mode(c)
            validate_continuous_speech_plan(plan)
        fingerprint = hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if c.get("fingerprint"):
            raise ValueError("reply_already_accepted")
        segments, reading = plan.get("segments", []), plan.get("reading")
        c.update(plan=plan, fingerprint=fingerprint, emotion=plan["emotion"],
                 answer="".join(p["text"] for p in segments), delivery=segments, status="ready",
                 reading=reading, reading_status="pending" if reading else "skipped",
                 validation_status="accepted")
        if self.semantic_speech and segments:
            c.update(semantic_speech=True, generated_text_chars=len(c["answer"]),
                     action_group_count=sum(len(part["actions"]) for part in segments))
        if self.long_speech_test:
            c.update(long_speech_test=True, generated_text_chars=len(c["answer"]),
                     action_group_count=len(segments[0]["actions"]))
        if is_motion_item(c):
            c["motion_actions"] = plan["actions"]
        (s.out / "plans").mkdir(exist_ok=True)
        save(s.out / "plans" / (c["id"] + ".json"), {"comment_id": c["id"], "comment": c["text"], **plan})
        s.event("replay_plan_accepted" if self.replay_fixture else "interaction_plan_accepted" if c.get("input_kind") == "interaction" else "llm_plan_accepted",
                comment_id=c["id"], input_kind=c.get("input_kind", "viewer"),
                reading=reading, segments=segments, motion_actions=plan.get("actions", []))
        s.checkpoint()
        if enqueue:
            c["queued_at_monotonic"] = time.monotonic()
            s.work.put_nowait(c)

    async def interaction_settings(self, request):
        if not self.automation.interactions_supported():
            raise web.HTTPConflict(text="专项试验或固定回放期间不修改互动规则")
        settings = validate_interaction_settings(await request.json())
        # 保存规则会撤销尚未发送的旧互动；已经开始的一轮仍按旧快照完成。
        self.automation.cancel_interactions("interaction_settings_changed")
        self.automation.interactions.configure(settings,
            preserve_reservation_ids=self.automation.started_reservations())
        self.config["interactions"] = settings
        save(self.data / "local.json", self.config)
        return web.json_response({"ok": True})

    async def simulate_interaction(self, request):
        if not self.automation.interactions_supported():
            raise web.HTTPConflict(text="专项试验或固定回放期间不接收礼物与欢迎")
        body = await request.json()
        fields = {"kind", "nickname", "user_id", "gift_name", "gift_id", "group_id", "repeat_count", "level", "dry_run"}
        if not isinstance(body, dict) or set(body) - fields or body.get("kind") not in ("gift", "member"):
            raise ValueError("请选择模拟礼物或模拟入场")
        dry = body.get("dry_run", True)
        if type(dry) is not bool:
            raise ValueError("规则检查标志必须为布尔值")
        def text_field(key, default="", limit=64):
            value = body.get(key, default)
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError("模拟字段格式不正确")
            return value.strip()
        uid = text_field("user_id", "simulation-viewer", 256)
        nickname = text_field("nickname", "测试观众", 16)
        if not uid or not nickname:
            raise ValueError("模拟用户ID与昵称不能为空")
        event = {"id": "sim-" + uuid.uuid4().hex, "platform": "douyin", "type": body["kind"],
                 "roomId": "simulation", "ts": int(time.time() * 1000), "receivedAt": int(time.time() * 1000),
                 "user": {"id": uid, "nickname": nickname}}
        if body["kind"] == "gift":
            count = body.get("repeat_count", 1)
            if type(count) is not int or not 1 <= count <= 1000000:
                raise ValueError("连击次数需为1至1000000的整数")
            event["data"] = {"giftName": text_field("gift_name", "小心心"), "giftId": text_field("gift_id", limit=256),
                             "groupId": text_field("group_id", limit=256), "repeatCount": count, "repeatEnd": True}
        else:
            level = body.get("level")
            if level is not None and (type(level) is not int or not 0 <= level <= 200):
                raise ValueError("荣誉等级需为0至200的整数，未知时留空")
            if level is not None:
                event["user"]["payGradeLevel"] = level
            event["data"] = {"action": 1, "isSetToAdmin": False}
        if dry:
            queue = InteractionQueue(self.config["interactions"])
            result = queue.ingest(event)
            job = queue.pop("gift" if body["kind"] == "gift" else "welcome")
            plan = validate_interaction_plan(job) if job else None
            return web.json_response({"ok": True, "dry_run": True, "result": result, "preview": job, "plan": plan})
        if (not self.automation.live_ready() or self.paused or self.automation.last_error):
            raise web.HTTPConflict(text="先手动开始会话并等待就绪，才能加入播放队列")
        result = self.automation.interactions.ingest(event)
        self.automation.wake()
        return web.json_response({"ok": True, "dry_run": False, "result": result,
                                  "interactions": self.automation.interactions.public()})

    async def legacy_brain(self, request):
        raise web.HTTPGone(text="已改用内置百炼大脑，旧Codex脑桥已停用")

    def build(self):
        @web.middleware
        async def boundary(request, handler):
            try:
                if self.origin != f"http://{request.host}":
                    raise web.HTTPForbidden(text="只允许本机工作台地址")
                if request.method == "POST" and request.headers.get("Origin") != self.origin:
                    raise web.HTTPForbidden(text="来源不匹配")
                if (self.external_game.active and request.method == "POST"
                        and request.path.startswith(self.prefix + "/")):
                    command = request.path[len(self.prefix) + 1:]
                    if not command.startswith("external/") and command not in {"start", "stop", "new", "heartbeat"}:
                        raise web.HTTPConflict(text="当前由游戏独占口播")
                if self.replay_fixture and request.method == "POST" and request.path.startswith(self.prefix + "/"):
                    # 固定对照只开放会话生命周期与回放，避免途中换角色或混入新生成内容。
                    command = request.path[len(self.prefix) + 1:]
                    if command not in {"start", "stop", "new", "quit", "heartbeat", "replay"}:
                        raise web.HTTPConflict(text="固定对照期间仅开放会话控制与样本回放")
                if request.path.startswith(self.prefix + "/brain/"):
                    if not secrets.compare_digest(request.headers.get("X-Brain-Token", ""), self.brain_token):
                        raise web.HTTPForbidden(text="大脑连接未授权")
                result = await handler(request)
            except web.HTTPException as exc:
                result = web.json_response({"error": exc.text}, status=exc.status)
            except (ValueError, TypeError, KeyError):
                result = web.json_response({"error": "输入内容或本机配置格式不正确"}, status=400)
            except Exception as exc:
                self.current.event("studio_http_error", error_type=type(exc).__name__)
                result = web.json_response({"error": "本机处理未完成，请查看诊断"}, status=500)
            result.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                   "Referrer-Policy": "no-referrer",
                                   "Permissions-Policy": "camera=(), microphone=(), display-capture=()"})
            return result

        app = web.Application(middlewares=[boundary], client_max_size=64 * 1024)
        app.on_startup.append(self.startup)
        app.on_cleanup.append(self.shutdown)

        async def page(request):
            return web.FileResponse(HERE / "static" / "index.html")

        async def static(request):
            name = request.match_info["name"]
            if name not in {"app.css", "app.js", "avatar-library.css", "avatar-library.js", "live-interactions.js"}:
                raise web.HTTPNotFound()
            return web.FileResponse(HERE / "static" / name)

        async def state(request):
            return web.json_response(self.snapshot())

        async def stop(request):
            self.director.stop("session_stop")
            self.automation.invalidate("operator_stop", replies=True)
            self.current.latch_stop()
            self.current.spawn(self.current.request_stop("工作台结束会话"))
            await self.automation.disconnect("operator_stop")
            return web.json_response({"requested": True})

        async def new(request):
            async with self.lock:
                await self.new_session()
            return web.json_response({"ok": True})

        async def quit_service(request):
            async with self.lock:
                if self.current.started and not self.can_new():
                    raise web.HTTPConflict(text="请先结束会话，并等待云端结束与录像归档；当前不能退出服务")
                self.closing = True
                asyncio.get_running_loop().call_later(.3, self.done.set)
            return web.json_response({"ok": True})

        async def heartbeat(request):
            # A separately open studio page must not hide a lost game console.
            if not self.external_game.active:
                self.browser_seen = time.monotonic()
            return web.json_response({"ok": True})

        async def pause(request):
            body = await request.json()
            if type(body.get("paused")) is not bool:
                raise web.HTTPBadRequest(text="暂停值需为布尔值")
            self.paused = body["paused"]
            self.automation.invalidate("intake_pause_changed")
            self.current.event("intake_paused", paused=self.paused)
            return web.json_response({"ok": True})

        async def intake_settings(request):
            if self.short_performance_test:
                raise web.HTTPConflict(text="短表演试验期间不修改评论来源")
            if self.continuous_speech_test:
                raise web.HTTPConflict(text="连续口播试验期间不修改评论来源")
            if self.long_speech_test:
                raise web.HTTPConflict(text="长文试验期间不修改评论来源")
            body = await request.json()
            base_url = normalize_endpoint(body.get("base_url"))
            import re
            room = body.get("room_id")
            if not isinstance(room, str) or not re.fullmatch(r"[0-9]{1,32}", room):
                raise web.HTTPBadRequest(text="直播间ID应为抖音直播页地址中的数字ID")
            async with self.automation.source_lock:
                if self.automation.enabled:
                    raise web.HTTPConflict(text="请先停止评论订阅再修改来源")
                self.config["intake"] = {"base_url": base_url, "room_id": room}
                save(self.data / "local.json", self.config)
                self.automation.invalidate("source_changed")
            return web.json_response({"ok": True})

        async def intake_start(request):
            if self.closing or self.current.stopping or self.current.component.state in TERMINAL:
                raise web.HTTPConflict(text="请先新建空闲会话")
            await self.automation.connect()
            return web.json_response({"ok": True})

        async def intake_stop(request):
            await self.automation.disconnect()
            return web.json_response({"ok": True})

        async def ai_resume(request):
            self.automation.ai.config = bailian_config(self.data)
            self.automation.resume()
            return web.json_response({"ok": True})

        async def skip(request):
            body = await request.json()
            c = next((c for c in self.current.comments if c["id"] == body.get("id")), None)
            if not c or c["status"] != "pending":
                raise web.HTTPConflict(text="只可跳过尚未交给大脑的弹幕")
            if c.get("operator_mode") == "topic":
                raise web.HTTPConflict(text="主线请使用暂停或停止，避免误跳段")
            c["status"] = "skipped"
            self.current.checkpoint()
            return web.json_response({"ok": True})

        async def director_command(request):
            # Sharing the local authenticated control boundary does not make
            # viewer data an operator command. This route creates its own kind.
            async with self.lock:
                if self.closing or not self.automation.live_ready():
                    raise web.HTTPConflict(text="请等待本场音画就绪后安排主持内容")
                command = request.match_info["command"]
                if command == "submit":
                    if self.automation.last_error:
                        raise web.HTTPConflict(text="请先处理模型错误并手动恢复")
                    result = self.director.submit(await request.json())
                elif command == "pause":
                    self.director.pause()
                    result = {"ok": True}
                elif command == "continue":
                    if self.automation.last_error:
                        raise web.HTTPConflict(text="请先处理模型错误并手动恢复")
                    self.director.resume()
                    result = {"ok": True}
                elif command == "stop":
                    self.director.stop()
                    result = {"ok": True}
                else:
                    raise web.HTTPNotFound()
                return web.json_response(result)

        async def settings(request):
            body = self.validated_settings(await request.json())
            async with self.lock:
                if not self.settings_editing_allowed():
                    raise web.HTTPConflict(text="角色设置只可在空闲会话修改")
                selected = self.avatar_library.public()["selected_id"]
                if selected:
                    self.avatar_library.update_settings(selected, body)
                    self.apply_library_asset(selected)
                else:
                    self.config["settings"] = body
                    save(self.data / "local.json", self.config)
                    self.current.apply_character(body)
            return web.json_response({"ok": True})

        async def diagnostics(request):
            return web.json_response(self.current.diagnostics.snapshot())

        async def export(request):
            return web.json_response(self.current.diagnostics.export(), headers={"Content-Disposition": 'attachment; filename="studio-diagnostics.json"'})

        async def player_page(request):
            return web.FileResponse(HERE / "static" / "player.html")

        async def player_script(request):
            return web.FileResponse(HERE / "static" / "player.js")

        async def sdk(request):
            return web.FileResponse(HERE.parent / "static/vendor/aliyun-rtc-sdk-7.1.9.js")

        async def player_state(request):
            c = self.current.component
            return web.json_response({"session": {"generation": self.generation, "state": c.state,
                                                   "live_id": c.live_id, "rtc_available": bool(c.rtc())},
                                      "player": self.player_public(),
                                      "can_takeover": self.can_preview_takeover()})

        async def claim(request):
            body = await request.json()
            cid = body.get("client_id")
            if not isinstance(cid, str) or not 8 <= len(cid) <= 100:
                raise web.HTTPBadRequest(text="播放器标识格式不正确")
            if body.get("receiver_mode", "obs") != self.receiver_mode:
                return web.json_response({"granted": False, "generation": self.generation,
                                          "reason": "本次启动已选择另一种接收方式"})
            if self.external_game.active and body.get("game_owner") != self.external_game.owner_id:
                return web.json_response({"granted": False, "generation": self.generation,
                                          "reason": "本场仅由当前游戏工作台接收预览"})
            if "takeover" in body:
                if (body["takeover"] is not True or body.get("generation") != self.generation
                        or not self.can_preview_takeover()):
                    return web.json_response({"granted": False, "generation": self.generation,
                                              "reason": "仅可在当前未开场的预览会话中显式接管"})
                # No await between eligibility and replacement: a concurrent
                # start cannot interleave and leave a paid run without its owner.
                self.player = None
            # Before any paid creation, an idle page may be refreshed safely.
            # Once start is latched, ownership cannot change within this run.
            if (self.player and not self.current.started and self.current.component.state == "idle"
                    and (self.player.get("released") or not self.player_public()["connected"])):
                self.player = None
            if self.player and self.player["client_id"] != cid:
                return web.json_response({"granted": False, "generation": self.generation,
                                          "reason": ("另一个页面正在接收预览，可点击下方按钮切换到本页"
                                                     if self.can_preview_takeover() else
                                                     "本场已绑定另一播放器；请结束并新建会话")})
            if not self.player:
                self.player = {"client_id": cid, "seen": time.monotonic(), "frames": 0,
                               "audio_live": False, "video_live": False, "joined": False}
            return web.json_response({"granted": not self.player.get("released", False),
                                      "generation": self.generation})

        async def rtc(request):
            self.owner(request.query)
            return web.json_response(self.current.component.rtc())

        async def player_heartbeat(request):
            body = await request.json()
            p = self.owner(body)
            frames = body.get("frames", 0)
            if type(frames) not in (int, float) or not math.isfinite(frames) or frames < 0:
                raise web.HTTPBadRequest(text="视频帧进度不正确")
            now = time.monotonic()
            if frames > p.get("frames", 0):
                p["frame_seen"] = now
            p.update(seen=now, frames=frames, audio_live=body.get("audio_live") is True,
                     video_live=body.get("video_live") is True)
            return web.json_response({"ok": True})

        async def joined(request):
            body = await request.json()
            p = self.owner(body)
            frames = body.get("frames", 0)
            if body.get("audio_live") is not True or body.get("video_live") is not True or type(frames) not in (int, float) or not math.isfinite(frames) or frames <= 0:
                raise web.HTTPBadRequest(text="需要远端音视频轨道与真实解码进度")
            if self.current.component.state != "waiting_stage":
                raise web.HTTPConflict(text="本场尚未等待播放器就绪")
            p.update(joined=True, audio_live=True, video_live=True, frames=frames,
                     frame_seen=time.monotonic(), seen=time.monotonic())
            self.current.component.joined()
            return web.json_response({"ok": True})

        async def fault(request):
            body = await request.json()
            p = self.owner(body)
            p["error"] = "播放器报告连接或播放失败"
            self.current.event("player_fault", error=p["error"])
            if self.current.started:
                self.current.latch_stop()
                self.current.spawn(self.current.request_stop(p["error"]))
            return web.json_response({"ok": True})

        async def release(request):
            body = await request.json()
            p = self.owner(body)
            p["released"] = True
            if self.current.started:
                self.current.latch_stop()
                self.current.spawn(self.current.request_stop("播放器关闭"))
            return web.json_response({"ok": True})

        app.router.add_get(self.prefix + "/", page)
        for name, handler in (("state", state), ("diagnostics", diagnostics), ("diagnostics/export", export), ("brain/next", self.legacy_brain)):
            app.router.add_get(self.prefix + "/" + name, handler)
        for name, handler in (("start", self.start), ("stop", stop), ("new", new), ("quit", quit_service), ("comments", self.comments),
                              ("idle-motions", self.idle_motions),
                              ("idle-motions-config", self.idle_motions_config),
                              ("idle-motions-sync", self.idle_motions_sync),
                              ("idle-motion-test", self.idle_motion_test),
                              ("idle-motions-recover", self.idle_motions_recover),
                              ("replay", self.replay),
                              ("avatar-switch", self.avatar_switch), ("avatar-switch/resume", self.avatar_switch_resume),
                              ("heartbeat", heartbeat), ("pause", pause), ("skip", skip), ("settings", settings), ("brain/reply", self.legacy_brain),
                              ("intake/settings", intake_settings), ("intake/start", intake_start),
                              ("intake/stop", intake_stop), ("ai/resume", ai_resume)):
            app.router.add_post(self.prefix + "/" + name, handler)
        app.router.add_post(self.prefix + "/interactions/settings", self.interaction_settings)
        app.router.add_post(self.prefix + "/interactions/simulate", self.simulate_interaction)
        for path, handler in (("claim", self.external_game.claim), ("heartbeat", self.external_game.heartbeat),
                              ("jobs", self.external_game.submit), ("jobs/prepare", self.external_game.prepare),
                              ("stop", self.external_game.stop)):
            app.router.add_post(self.prefix + "/external/" + path, handler)
        app.router.add_get(self.prefix + "/external/jobs/{job_id}", self.external_game.get_job)
        app.router.add_post(self.prefix + "/external/jobs/{job_id}/cancel", self.external_game.cancel_job)
        app.router.add_post(self.prefix + "/external/jobs/{job_id}/dispatch", self.external_game.dispatch)
        app.router.add_get(self.prefix + "/external/state", self.external_game.state)
        appearance = self.external_game.appearance
        app.router.add_post(self.prefix + "/external/appearance/lifecycle", appearance.lifecycle)
        app.router.add_post(self.prefix + "/external/appearance", appearance.submit)
        app.router.add_get(self.prefix + "/external/appearance", appearance.state)
        app.router.add_get(self.prefix + "/external/appearance/{request_id}", appearance.get)
        app.router.add_post(self.prefix + "/external/appearance/{request_id}/confirm", appearance.confirm)
        app.router.add_get(self.prefix + "/avatars", self.avatars_state)
        app.router.add_get(self.prefix + "/avatars/{avatar_id}/image", self.avatars_image)
        for path, handler in (("sync", self.avatars_sync), ("create", self.avatars_create),
                              ("select", self.avatars_select), ("switch", self.avatars_switch),
                              ("{avatar_id}/settings", self.avatars_settings), ("{avatar_id}/refresh", self.avatars_refresh)):
            app.router.add_post(self.prefix + "/avatars/" + path, handler)
        # 精确注册静态资源；弃用的控制接口不会再被通配路由匹配。
        for name in ("app.css", "app.js", "avatar-library.css", "avatar-library.js", "live-interactions.js"):
            app.router.add_get(self.prefix + "/{name:" + re.escape(name) + "}", static)
        app.router.add_post(self.prefix + "/director/{command}", director_command)
        for name, handler in (("", player_page), ("player.js", player_script), ("sdk.js", sdk), ("state", player_state), ("rtc", rtc)):
            app.router.add_get(self.player_prefix + "/" + name, handler)
        for name, handler in (("claim", claim), ("heartbeat", player_heartbeat), ("joined", joined), ("fault", fault), ("release", release)):
            app.router.add_post(self.player_prefix + "/" + name, handler)
        return app


async def serve(*, preview=False, short_performance_test=False, continuous_speech_test=False,
                long_speech_test=False, replay_fixture=None):
    studio = Studio(preview=preview, short_performance_test=short_performance_test,
                    continuous_speech_test=continuous_speech_test, long_speech_test=long_speech_test,
                    replay_fixture=replay_fixture)
    runner = web.AppRunner(studio.build(), access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", studio.config["port"])
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        studio.origin = f"http://127.0.0.1:{port}"
        studio.config["port"] = port
        save(studio.data / "local.json", studio.config)
        studio.write_runtime()
        print(json.dumps({"pid": os.getpid(), "url": studio.origin + studio.prefix + "/",
                          "player_url": studio.origin + studio.player_prefix + "/"}, ensure_ascii=False), flush=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, studio.done.set)
        await studio.done.wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true", help="本次启动由工作台预览接收音画；不自动开云场")
    parser.add_argument("--short-performance-test", action="store_true", help="仅本进程启用GE→0.5秒→单S及语音→1.5秒→Q；要求--preview")
    parser.add_argument("--continuous-speech-test", action="store_true", help="仅本进程启用一条语音贯穿两个GE→S→Q组；要求--preview")
    parser.add_argument("--long-speech-test", action="store_true", help="仅本进程启用最多120字连续口播与1至6个GE→S→Q组；要求--preview")
    parser.add_argument("--replay-source", type=Path, help="从指定原场次复用音频与动作，仅将组间距改成5秒；要求--preview --long-speech-test")
    parser.add_argument("--semantic-replay-source", type=Path,
                        help="复用指定语义样本的原音频与最终锚点，两处分别换S；要求--preview，不能混用旧试验模式")
    parser.add_argument("--semantic-prepared-source", type=Path,
                        help="播放新准备的约30秒、3至6动作语义素材；要求--preview，不创建来源云场")
    parser.add_argument("--semantic-minute-source", type=Path,
                        help="播放两次真实TTS组合的一分钟六动作A素材；要求--preview，固定GE提前1.1秒")
    parser.add_argument("--semantic-replay-ge", choices=("0.5", "1.1", "none"),
                        help="语义固定回放的独立GE前导秒数，none表示G/E仅包含在S内；已播来源默认0.5，新准备素材默认1.1")
    parser.add_argument("--replay-comment", default="c0001", help="固定回放的原条目，默认c0001")
    parser.add_argument("--replay-long-action", action="store_true", help="将原四动作合成单L描述，原音频末尾发Q；要求--replay-source")
    parser.add_argument("--replay-long-action-groups", type=int, choices=(1, 2), default=1,
                        help="L回放分组数；2表示原四动作分成两组、组间加Q；要求--replay-long-action")
    parser.add_argument("--replay-first-q-after", type=float, choices=(3.0,),
                        help="将双L的第一Q提前到L1成功发送后3秒；默认保留L2起点")
    parser.add_argument("--replay-second-l-at", type=float, choices=(7.36,),
                        help="将双L的第二L移到原音频7.36秒；保留末Q时刻，要求--replay-first-q-after 3")
    args = parser.parse_args()
    modes = (args.short_performance_test, args.continuous_speech_test, args.long_speech_test)
    if sum(modes) > 1:
        parser.error("单S、双动作组与长文试验不能同时启用")
    if any(modes) and not args.preview:
        parser.error("试验模式必须与--preview同时使用")
    if args.replay_source and not (args.preview and args.long_speech_test):
        parser.error("固定回放必须与--preview --long-speech-test同时使用")
    semantic_sources = (args.semantic_replay_source, args.semantic_prepared_source, args.semantic_minute_source)
    semantic_source = next((source for source in semantic_sources if source), None)
    if sum(bool(source) for source in semantic_sources) > 1:
        parser.error("已播来源、约30秒素材与一分钟素材不能同时选择")
    if semantic_source and (not args.preview or any(modes) or args.replay_source):
        parser.error("语义固定回放必须与--preview使用，不能混用旧回放或口播试验")
    if args.semantic_replay_ge is not None and not semantic_source:
        parser.error("--semantic-replay-ge仅用于语义固定素材对照")
    if args.semantic_minute_source and args.semantic_replay_ge not in (None, "1.1"):
        parser.error("一分钟素材固定使用A方案：GE提前1.1秒")
    if args.replay_long_action and not args.replay_source:
        parser.error("单L回放必须指定--replay-source")
    if args.replay_long_action_groups != 1 and not args.replay_long_action:
        parser.error("双L回放必须指定--replay-long-action")
    if args.replay_first_q_after is not None and not (args.replay_long_action and args.replay_long_action_groups == 2):
        parser.error("提前Q回放必须指定--replay-long-action --replay-long-action-groups 2")
    if args.replay_second_l_at is not None and not (args.replay_long_action and args.replay_long_action_groups == 2
                                                  and args.replay_first_q_after == 3.0):
        parser.error("第二L语义位置回放必须指定双L模式与--replay-first-q-after 3")
    fixture = FixedReplay(args.replay_source, args.replay_comment, group_interval=5.0,
                          action_kind="L" if args.replay_long_action else "S",
                          long_action_group_count=args.replay_long_action_groups,
                          first_q_after_seconds=args.replay_first_q_after,
                          second_l_at_seconds=args.replay_second_l_at) if args.replay_source else None
    if args.semantic_replay_source:
        ge_lead = None if args.semantic_replay_ge == "none" else float(args.semantic_replay_ge or "0.5")
        fixture = SemanticFixedReplay(args.semantic_replay_source, args.replay_comment, ge_lead_seconds=ge_lead)
    if args.semantic_prepared_source:
        ge_lead = None if args.semantic_replay_ge == "none" else float(args.semantic_replay_ge or "1.1")
        fixture = PreparedSemanticReplay(args.semantic_prepared_source, ge_lead_seconds=ge_lead)
    if args.semantic_minute_source:
        fixture = PreparedMinuteSemanticReplay(args.semantic_minute_source)
    # Share the trial lock: a stale trial and the new app must never create
    # competing billable renderers. The OS releases this lock on process exit.
    STUDY.mkdir(parents=True, exist_ok=True)
    with RunGuard(STUDY / ".active-run.lock"):
        asyncio.run(serve(preview=args.preview, short_performance_test=args.short_performance_test,
                         continuous_speech_test=args.continuous_speech_test, long_speech_test=args.long_speech_test,
                         replay_fixture=fixture))
