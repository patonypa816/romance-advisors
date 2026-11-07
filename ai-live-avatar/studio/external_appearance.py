"""Opt-in, same-person Type16 control for one external owner and episode.

Transport acceptance is deliberately separate from observed dynamic imagery.
No endpoint accepts an asset ID or a video URL from a browser/game/model.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from aiohttp import web
from motion_component import MotionComponent

KEYS = ("fish", "rough", "tired", "normal", "refined", "stellar")
IN_FLIGHT = {"pending", "processing", "ready", "prepared", "preparing", "synthesizing", "planning_actions", "sending"}


class ExternalAppearance:
    # Local admission guards, not a claim about provider playback completion.
    TRANSITION_GUARD_SECONDS = 3.0
    RECEIVER_TIMEOUT_SECONDS = 15.0
    POLL_SECONDS = .1

    def __init__(self, external):
        self.external, self.studio = external, external.studio
        self.config, self.config_error = {}, ""
        path = getattr(self.studio, "data", None)
        path = path / "external-appearance.json" if path is not None else None
        if path is not None and path.exists():
            try:
                if path.is_symlink() or path.stat().st_size > 65536:
                    raise ValueError()
                value = json.loads(path.read_text())
                if not isinstance(value, dict) or type(value.get("enabled")) is not bool:
                    raise ValueError()
                if value["enabled"]:
                    if set(value) != {"enabled", "character_id", "voice_id", "broadcast_key", "assets", "transitions"}:
                        raise ValueError()
                    if (not isinstance(value["character_id"], str) or not 1 <= len(value["character_id"]) <= 100
                            or not isinstance(value["voice_id"], str) or not value["voice_id"]
                            or value["broadcast_key"] not in KEYS or set(value["assets"]) != set(KEYS)
                            or set(value["transitions"]) != {"up", "down"}):
                        raise ValueError()
                    for key, asset in value["assets"].items():
                        if (set(asset) != {"avatar_id", "image_sha256", "description"}
                                or not re.fullmatch(r"[0-9]{1,64}", asset["avatar_id"])
                                or not re.fullmatch(r"[0-9a-f]{64}", asset["image_sha256"])
                                or not isinstance(asset["description"], str) or not 1 <= len(asset["description"]) <= 200):
                            raise ValueError()
                    if len({a["avatar_id"] for a in value["assets"].values()}) != len(KEYS):
                        raise ValueError()
                    for url in value["transitions"].values():
                        MotionComponent._switch_payload(value["assets"]["normal"]["avatar_id"], [url])
                        if urlsplit(url).scheme != "https":
                            raise ValueError()
                    self.config = value
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                self.config_error = "appearance_config_invalid"
        self.reset()

    def reset(self):
        self.context = None
        self.epoch = -1
        self.active = False
        self.context_closed = False
        self.initialized = False
        self.confirmed_key = None
        self.version = 0
        self.requests = {}
        self.pending = None
        self.fault = ""
        self._image_checks = {}

    def readiness(self):
        if self.config_error:
            return self.config_error
        if not self.config:
            return "appearance_disabled"
        try:
            for asset in self.config["assets"].values():
                actual = self.studio.library_asset(asset["avatar_id"])
                path = self.studio.avatar_library.image_path(asset["avatar_id"])
                stat = path.stat()
                fingerprint = (str(path), stat.st_size, stat.st_mtime_ns)
                checked = self._image_checks.get(asset["avatar_id"])
                if checked != fingerprint:
                    if hashlib.sha256(path.read_bytes()).hexdigest() != asset["image_sha256"]:
                        return "appearance_asset_binding_invalid"
                    self._image_checks[asset["avatar_id"]] = fingerprint
                if (actual.get("sha256") != asset["image_sha256"]
                        or self.studio.asset_tts_voice(asset["avatar_id"]) != self.config["voice_id"]):
                    return "appearance_asset_binding_invalid"
            current = self.studio.current
            if current.tts_voice != self.config["voice_id"]:
                return "appearance_voice_mismatch"
            if self.confirmed_key is None:
                matches = [key for key, a in self.config["assets"].items()
                           if a["avatar_id"] == current.avatar_id
                           and a["image_sha256"] == current.variant_info.get("image_sha256")]
                if len(matches) != 1:
                    return "appearance_identity_unknown"
                if matches[0] != self.config["broadcast_key"]:
                    return "appearance_broadcast_binding_invalid"
                self.confirmed_key = matches[0]
            expected = self.config["assets"][self.confirmed_key]
            if (current.avatar_id != expected["avatar_id"]
                    or current.variant_info.get("image_sha256") != expected["image_sha256"]):
                return "appearance_identity_unknown"
        except (web.HTTPException, ValueError, OSError, KeyError, TypeError, AttributeError):
            return "appearance_assets_not_ready"
        return self.fault

    def receipt(self, record):
        # A late attributed Type103 invalidates even an adopted binding.
        raw = getattr(self.studio.current.component, "_action_requests", {}).get(record["request_id"], {})
        if raw.get("status") == "failed" and not record.get("failure_observed"):
            late = record["status"] in {"confirmed", "applied"}
            record.update(status="failed", error_code="SWITCH_AVATAR_ERROR", failure_observed=True)
            self.fault = "appearance_late_failure" if late else "appearance_unconfirmed"
            self.studio.avatar_switch_holding = True
            if late:
                # A target was already allowed into later content. Its identity
                # is now disputed; no scheduling decision can undo that.
                s = self.studio.current
                s.latch_stop()
                s.spawn(s.request_stop("已采用外观收到迟到失败，停止核实"))
        return {k: copy.deepcopy(v) for k, v in record.items() if k not in {"task"}}

    def refresh_failures(self):
        # Poll consumed IDs; a late failure cannot disappear behind a newer ID.
        for record in self.requests.values():
            self.receipt(record)

    def public(self):
        self.refresh_failures()
        reason = self.readiness()
        pending = self.receipt(self.requests[self.pending]) if self.pending else None
        return {"enabled": bool(self.config), "ready": not reason, "message": reason,
                "active": self.active, "confirmed_key": self.confirmed_key,
                "version": self.version, "context": copy.deepcopy(self.context),
                "portrait": ({"key": self.confirmed_key, "avatar_key": "appearance-" + self.confirmed_key,
                              "description": self.config["assets"][self.confirmed_key]["description"],
                              "version": self.version, "simulated": False} if self.confirmed_key else None),
                "broadcast_key": self.config.get("broadcast_key"),
                "pending_confirmation": pending if pending and pending["status"] == "failed" else None,
                "automatic_progress": True,
                "receipt": pending, "blocked": bool(self.fault or self.pending)}

    def require_context(self, body):
        self.external.require_owner(body)
        if (not self.context or any(body.get(k) != v for k, v in self.context.items())):
            raise web.HTTPConflict(text="外观期号或模式代次已失效")

    def safe(self):
        s = self.studio.current
        receiver = self.studio.player or {}
        return (not self.studio.closing and s.started and not s.stopping
                and s.component.state == "ready" and s.component.comment_id is None
                and s.work.empty() and not any(self.external.busy(j) for j in self.external.jobs.values())
                and not any(c["status"] in IN_FLIGHT for c in s.comments)
                and self.studio.player_public().get("connected") is True
                and receiver.get("joined") is True and receiver.get("audio_live") is True
                and receiver.get("video_live") is True and not receiver.get("error")
                and time.monotonic() - receiver.get("frame_seen", 0) < 15
                and not s.worker_fault and not self.studio.automation.last_error)

    async def lifecycle(self, request):
        body = await request.json()
        if (not isinstance(body, dict) or set(body) != {"owner_id", "generation", "mode_epoch", "episode_id", "action"}
                or type(body["mode_epoch"]) is not int or body["mode_epoch"] < 0
                or body["action"] not in {"bind", "release"}):
            raise web.HTTPBadRequest(text="外观生命周期字段无效")
        self.external.identifier(body["episode_id"])
        async with self.studio.lock:
            self.external.require_owner(body)
            if body["action"] == "bind" and self.readiness():
                raise web.HTTPConflict(text="外观配置或身份尚未就绪")
            context = {k: body[k] for k in ("owner_id", "generation", "mode_epoch", "episode_id")}
            if body["action"] == "bind":
                if context == self.context and self.context_closed:
                    raise web.HTTPConflict(text="已关闭的本期外观权限不能恢复")
                if context != self.context:
                    if body["mode_epoch"] <= self.epoch or self.pending or not self.safe():
                        raise web.HTTPConflict(text="旧外观任务未收尾或模式代次无效")
                    self.context, self.epoch = context, body["mode_epoch"]
                    self.context_closed = False
                    self.initialized = self.confirmed_key == "normal"
                self.active = True
            else:
                self.require_context(body)
                self.active = False
                self.context_closed = True
                # Revoke before any awaited cleanup. A worker that has not yet
                # written observes this synchronously at its last gate.
            self.external.checkpoint()
            return web.json_response(self.public())

    async def submit(self, request):
        body = await request.json()
        fields = {"owner_id", "generation", "mode_epoch", "episode_id", "request_id", "target_key", "purpose"}
        if (not isinstance(body, dict) or set(body) != fields or body["target_key"] not in KEYS
                or type(body["mode_epoch"]) is not int or body["mode_epoch"] < 0
                or body["purpose"] not in {"game", "restore"}):
            raise web.HTTPBadRequest(text="外观请求只接受本期目标键")
        ident = body["request_id"]
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", ident):
            raise web.HTTPBadRequest(text="外观请求标识无效")
        async with self.studio.lock:
            self.require_context(body)
            previous = self.requests.get(ident)
            if previous:
                if any(previous.get(k) != v for k, v in body.items()):
                    raise web.HTTPConflict(text="同一请求不能修改冻结外观")
                return web.json_response(self.receipt(previous))
            self.refresh_failures()
            if (len(self.requests) >= 300 or self.readiness() or self.pending or not self.safe()
                    or (body["purpose"] == "game") != self.active
                    or body["purpose"] == "game" and not self.initialized and body["target_key"] != "normal"
                    or body["purpose"] == "restore" and body["target_key"] != self.config["broadcast_key"]):
                raise web.HTTPConflict(text="外观身份、所有权或发送空档未就绪")
            record = {**body, "status": "pending", "sent": False, "simulated": False,
                      "from_key": self.confirmed_key, "version": self.version,
                      "playback_confirmed": False, "operator_confirmed": False,
                      "visual_confirmed": False, "program_adopted": False,
                      "success_ack_available": False, "success_acknowledged": False}
            self.requests[ident] = record
            if self.confirmed_key == body["target_key"]:
                record.update(status="applied", unchanged=True, program_adopted=True,
                              confirmed_key=self.confirmed_key, evidence="existing_asset_binding")
                return web.json_response(self.receipt(record))
            self.pending = ident
            self.studio.avatar_switch_holding = True
            try:
                self.external.checkpoint()  # Persist intent before the first network await.
            except Exception:
                record.update(status="cancelled", error_code="ARCHIVE_FAILED")
                self.pending = None
                self.studio.avatar_switch_holding = False
                return web.json_response(self.receipt(record))
            record["task"] = self.studio.current.spawn(self.perform(record))
            return web.json_response(self.receipt(record))

    async def perform(self, record):
        s = self.studio.current
        def valid():
            return (s is self.studio.current and self.context is not None
                    and all(record[k] == self.context[k] for k in self.context)
                    and self.external.owner_id == record["owner_id"]
                    and self.studio.generation == record["generation"]
                    and (record["purpose"] == "game") == self.active and not self.fault and self.safe())
        try:
            target = self.config["assets"][record["target_key"]]
            direction = "up" if KEYS.index(record["target_key"]) > KEYS.index(record["from_key"]) else "down"
            record["direction"] = direction
            self.studio.idle_enable_epoch += 1
            await s.component.stop_idle_motions(recover=True, explicit=False)
            # Respect the last Type9/Q write before occupying the same sender.
            await s.component._wait(max(0.0, getattr(s.component, "_motion_not_before", 0) - time.monotonic()))
            receiver = self.studio.player
            raw = await s.component.switch_avatar(target["avatar_id"], [self.config["transitions"][direction]],
                                                   record["request_id"], valid)
            record.update(sent=raw.get("sent") is True, seq_id=raw.get("seq_id"))
            record["status"] = {"sent": "written", "cancelled": "cancelled", "failed": "failed"}.get(raw.get("status"), "unconfirmed")
            if record["status"] == "cancelled" and not record["sent"]:
                self.pending = None
                self.studio.avatar_switch_holding = False
            elif record["status"] in {"failed", "unconfirmed"}:
                self.fault = "appearance_unconfirmed"
            elif record["status"] == "written":
                # No Type16 success ack exists. Adopt for program scheduling
                # only, after a bounded guard and fresh decoded receiver frames.
                self.external.checkpoint()
                await self.adopt_after_guard(record, s, receiver)
        except BaseException as exc:
            record.update(status="unconfirmed", error_code="SWITCH_SEND_UNCONFIRMED")
            self.fault = "appearance_unconfirmed"
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self.external.checkpoint()

    async def adopt_after_guard(self, record, session, receiver):
        started = time.monotonic()
        guard_end = started + self.TRANSITION_GUARD_SECONDS
        deadline = guard_end + self.RECEIVER_TIMEOUT_SECONDS
        baseline = None
        initial_frames = (receiver or {}).get("frames", 0)
        record["transition_guard_seconds"] = self.TRANSITION_GUARD_SECONDS
        while True:
            now = time.monotonic()
            self.receipt(record)  # An attributed immediate/late Type103 wins.
            if record["status"] == "failed":
                return
            if (session is not self.studio.current or self.studio.player is not receiver
                    or receiver is None or self.external.owner_id != record["owner_id"]
                    or self.studio.generation != record["generation"] or not self.safe()
                    or self.fault or receiver.get("error") or receiver.get("released")
                    or not receiver.get("joined") or not receiver.get("audio_live")
                    or not receiver.get("video_live") or receiver.get("frames", 0) < initial_frames):
                record.update(status="unconfirmed", error_code="APPEARANCE_RECEIVER_UNHEALTHY")
                self.fault = "appearance_unconfirmed"
                return
            if now >= deadline:
                record.update(status="unconfirmed", error_code="APPEARANCE_RECEIVER_TIMEOUT")
                self.fault = "appearance_unconfirmed"
                return
            if now >= guard_end:
                if baseline is None:
                    baseline = receiver.get("frames", 0)
                elif (receiver.get("frames", 0) > baseline and receiver.get("frame_seen", 0) >= guard_end
                      and receiver.get("seen", 0) >= guard_end):
                    # These frames establish receiver health, not target identity,
                    # clip playback, a moving face or provider-side success.
                    self.apply_binding(record["target_key"])
                    record.update(status="applied", program_adopted=True, confirmed_key=self.confirmed_key,
                                  version=self.version, evidence="transport_guard_healthy_receiver",
                                  receiver_progress_observed=True, visual_confirmed=False,
                                  playback_confirmed=False, operator_confirmed=False)
                    self.pending = None
                    self.studio.avatar_switch_holding = False
                    return
            await asyncio.sleep(self.POLL_SECONDS)

    def apply_binding(self, key):
        asset = self.config["assets"][key]
        session = self.studio.current
        for ident in list(getattr(session, "_prepared_comments", {})):
            session.discard_prepared(ident)
        for job in self.external.jobs.values():
            session.discard_prepared(job["item"])
        session.apply_avatar(self.studio.avatar_library.image_path(asset["avatar_id"]),
            asset["avatar_id"], dict(self.studio.config["settings"]), reset_history=False)
        self.confirmed_key = key  # Compatibility field: adopted binding, not visual proof.
        if key == "normal" and self.active:
            self.initialized = True
        self.version += 1

    async def state(self, request):
        self.external.require_owner(request.query)
        return web.json_response(self.public())

    async def get(self, request):
        self.external.require_owner(request.query)
        record = self.requests.get(request.match_info["request_id"])
        if record is None:
            raise web.HTTPNotFound(text="未找到当前场次外观请求")
        return web.json_response(self.receipt(record))

    async def confirm(self, request):
        body = await request.json()
        fields = {"owner_id", "generation", "mode_epoch", "episode_id", "outcome"}
        if not isinstance(body, dict) or set(body) != fields or body["outcome"] not in {"target_dynamic", "original_dynamic"}:
            raise web.HTTPBadRequest(text="请明确确认本次目标或原貌的动态画面")
        async with self.studio.lock:
            self.require_context(body)
            ident = request.match_info["request_id"]
            record = self.requests.get(ident)
            if record is None or self.pending != ident:
                raise web.HTTPConflict(text="确认未绑定当前在途外观")
            receipt = self.receipt(record)
            task = record.get("task")
            if task and not task.done():
                raise web.HTTPConflict(text="切换写入尚未结束")
            if not self.safe():
                raise web.HTTPConflict(text="当前接收端或组件状态未确认")
            target = body["outcome"] == "target_dynamic"
            if target:
                raise web.HTTPConflict(text="正常换形由程序自动衔接，无需人工确认")
            if receipt["status"] != "failed":
                raise web.HTTPConflict(text="仅明确失败可人工核实保留原貌")
            key = record["from_key"]
            self.apply_binding(key)
            # The operator identified a moving face. They did not attest to
            # transition clip playback, audio, or a recorded stream.
            record.update(status="retained", operator_confirmed=True,
                          confirmed_key=key, version=self.version, visual_confirmation=body["outcome"],
                          playback_confirmed=False, failure_observed=True)
            self.pending, self.fault = None, ""
            self.studio.avatar_switch_holding = False
            self.external.checkpoint()
            return web.json_response(self.receipt(record))
