"""One local game owns frozen speech/motion; receipts never claim observed playback."""
from __future__ import annotations

import asyncio
import hashlib
import json
import copy
import math
import re
import time
from collections.abc import Mapping

from aiohttp import web

from live_session import save
from action_schema import parse_director_plan
from semantic_speech import parse_dialogue_semantic_plan, compile_dialogue_motion_timeline

IN_FLIGHT = {"pending", "processing", "ready", "prepared", "preparing", "synthesizing", "planning_actions", "sending"}
IDENTIFIER = re.compile(r"[A-Za-z0-9_:.-]{1,128}\Z")
LEGACY_INSTRUCTION = "自然清晰，正常语速，逐字说出台词"


class ExternalGame:
    def __init__(self, studio):
        self.studio = studio
        self.owner_id = None
        self.generation = None
        self.jobs = {}

    @property
    def active(self):
        return self.owner_id is not None

    def reset(self):
        self.owner_id = None
        self.generation = self.studio.generation
        self.jobs = {}
        self.studio.current.external_checkpoint = self.checkpoint
        self.studio.current.external_preparation_gate = self.preparation_allowed
        self.checkpoint()

    @staticmethod
    def identifier(value):
        if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
            raise web.HTTPBadRequest(text="游戏标识格式不正确")
        return value

    def require_owner(self, body):
        if (not isinstance(body, Mapping) or not self.active or body.get("owner_id") != self.owner_id
                or body.get("generation") != self.generation
                or self.generation != self.studio.generation):
            raise web.HTTPConflict(text="游戏未持有当前场次")

    def checkpoint(self):
        # This archive belongs to one generation. A restart creates a new one;
        # old records are evidence only and are never restored into the sender.
        if not self.active:
            return
        save(self.studio.current.out / "external-game.json", {
            "generation": self.generation, "owner_id": self.owner_id,
            "progress_basis": "successful_send_and_handoff_not_confirmed_playback",
            "jobs": [{**self.job_public(job), "text": job["text"], "text_sha256": job["text_sha256"],
                      "instruction": job.get("instruction", LEGACY_INSTRUCTION),
                      "plan": self.submitted_plan(job),
                      "expires_at": job["item"].get("expires_at"),
                      "finish_before": job["item"].get("finish_before")}
                     for job in self.jobs.values()]})

    def job_public(self, job):
        item = job["item"]
        receipts = item.get("motion_receipts", [])
        completed_wait = None
        if (job.get("mode", "speech") == "speech" and item["status"] == "sent"
                and not item.get("external_worker_pending") and not item.get("external_preparation_pending")
                and receipts and all(r.get("send_complete") and r.get("handoff_wait_elapsed") for r in receipts)
                and receipts[-1].get("status") == "sent"):
            # Only the final segment's actual tail belongs after this line.
            # Missing old receipts must not borrow a previous segment's wait.
            value = receipts[-1].get("post_audio_wait_seconds")
            if type(value) in (int, float) and 0 <= value < math.inf:
                completed_wait = value
        return {"job_id": job["job_id"], "generation": self.generation,
                "mode": job.get("mode", "speech"),
                "status": item["status"], "segments_sent": item.get("segments_sent", 0),
                "delivery_started": bool(item.get("delivery_started")),
                "worker_pending": bool(item.get("external_worker_pending")),
                "preparation_pending": bool(item.get("external_preparation_pending")),
                "submission_mode": job.get("submission_mode", "legacy"),
                "estimated_delivery_seconds": item.get("external_send_seconds"),
                "cancel_result": item.get("cancel_result", ""),
                "send_complete": bool(receipts and all(r.get("send_complete") for r in receipts)),
                "handoff_wait_elapsed": bool(receipts and all(r.get("handoff_wait_elapsed") for r in receipts)),
                "post_audio_wait_seconds": completed_wait,
                "playback_confirmed": False,
                "failure_code": item.get("failure_code", ""),
                "delivery_outcome": item.get("delivery_outcome", ""),
                "error": "游戏表演未完成，请核对本场状态" if item.get("error") else ""}

    def public(self):
        current = next((j for j in self.jobs.values() if j["item"].get("external_worker_pending")), None)
        candidate = self.next_job()
        return {"owner_id": self.owner_id, "generation": self.studio.generation,
                "state": self.studio.current.component.state,
                "current_job": self.job_public(current) if current else None,
                "next_job": self.job_public(candidate) if candidate else None,
                "playback_confirmed": False}

    async def claim(self, request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"owner_id"}:
            raise web.HTTPBadRequest(text="游戏接管只接受owner_id")
        owner = self.identifier(body["owner_id"])
        async with self.studio.lock:
            if self.active:
                if owner != self.owner_id:
                    raise web.HTTPConflict(text="当前场次已由另一游戏接管")
                return web.json_response({"generation": self.generation, "owner_id": self.owner_id})
            s, a = self.studio.current, self.studio.automation
            topic = self.studio.director.topic
            if (self.studio.receiver_mode != "preview" or not self.studio.semantic_speech
                    or self.studio.replay_fixture or self.studio.closing or s.started or s.stopping
                    or s.component.state != "idle" or a.enabled
                    or any(c["status"] in IN_FLIGHT for c in s.comments)
                    or any(getattr(a, name, None) and not getattr(a, name).done()
                           for name in ("selection_task", "reply_task", "topic_task", "source_task"))
                    or a.pool.snapshot()["candidate_count"] or a.interactions.has_pending()
                    or (topic and topic["state"] not in {"completed", "stopped"})
                    or self.studio.idle_motion_busy()
                    or s.component.idle_motions_public().get("recovery_failed")):
                raise web.HTTPConflict(text="游戏只能接管未开场且无在途内容的普通预览会话")
            self.owner_id, self.generation = owner, self.studio.generation
            # Explicit game ownership is granted only before any paid start.
            # Drop an old idle preview so can_start waits for the game's iframe;
            # the player claim gate prevents the old page from reclaiming it.
            self.studio.player = None
            self.checkpoint()
            return web.json_response({"generation": self.generation, "owner_id": owner})

    async def heartbeat(self, request):
        body = await request.json()
        self.require_owner(body)
        self.studio.browser_seen = time.monotonic()
        return web.json_response({"ok": True})

    @staticmethod
    def frozen_plan(text, instruction=LEGACY_INSTRUCTION):
        return parse_dialogue_semantic_plan({"emotion": "自然", "reading": None,
            "segments": [{"text": text, "instruction": instruction,
                          "actions": [], "recovery": None}]}, mode="once")

    def submitted_plan(self, job):
        return job.get("submitted_plan") or self.frozen_plan(job["text"], job.get("instruction", LEGACY_INSTRUCTION))

    @staticmethod
    def busy(job):
        # A cancelled TTS request still owns the worker until its await unwinds.
        return (job["item"].get("external_worker_pending")
                or job["item"].get("external_preparation_pending")
                or job["item"]["status"] in IN_FLIGHT)

    def next_job(self):
        return next((j for j in self.jobs.values()
                     if j.get("submission_mode") == "prepared" and not j["item"].get("external_dispatched")
                     and self.busy(j)), None)

    def preparation_allowed(self, item):
        job = self.jobs.get(item.get("external_job_id"))
        return bool(self.active and job and job["item"] is item
                    and self.generation == self.studio.generation
                    and item.get("external_owner_id") == self.owner_id
                    and item.get("external_generation") == self.generation
                    and not self.studio.closing and not self.studio.current.stopping
                    and not self.studio.automation.last_error
                    and not item.get("cancel_requested"))

    def validate_plan(self, item, body):
        job = self.jobs.get(item.get("external_job_id"))
        if (not self.active or not job or job["item"] is not item
                or item.get("external_generation") != self.studio.generation
                or item.get("external_mode", "speech") != job.get("mode", "speech")
                or body != self.submitted_plan(job)):
            raise ValueError("external_frozen_plan_changed")
        # Runtime action alignment may change anchors/add timeline metadata.
        # Keep the submitted copy isolated for idempotency and the archive.
        return copy.deepcopy(self.submitted_plan(job))

    def delivery_allowed(self, item):
        job = self.jobs.get(item.get("external_job_id"))
        allowed = bool(job and job["item"] is item and not item.get("cancel_requested")
                    and item.get("external_generation") == self.studio.generation
                    and not self.studio.automation.last_error)
        if not allowed or not self.preparation_allowed(item):
            if not item.get("delivery_started"):
                item.update(status="cancelled", cancel_requested=True, cancel_reason="external_identity_invalid")
            return False
        if item.get("requires_prepared"):
            if not item.get("external_dispatched") or not item.get("external_worker_pending"):
                return False
            try:
                if self.studio.current._cached_preparation(item) is None:
                    raise ValueError("external_prepared_cache_missing")
            except ValueError:
                self.reject_prepared(item)
                return False
        now = time.time()
        if (item.get("expires_at", float("inf")) <= now
                or now + item.get("external_send_seconds", 0) >= item.get("finish_before", float("inf"))):
            item.update(status="expired", cancel_reason="external_deadline")
            return False
        return True

    @staticmethod
    def reject_prepared(item):
        item.update(status="error", failure_code="prepared_cache_invalid",
                    error="预备素材缺失或身份不符；本条不会重新合成")

    async def submit(self, request):
        return await self._submit(request, submission_mode="legacy")

    async def prepare(self, request):
        return await self._submit(request, submission_mode="prepared")

    async def _submit(self, request, *, submission_mode):
        body = await request.json()
        mode = body.get("mode", "speech") if isinstance(body, dict) else None
        if mode not in ("speech", "motion") or submission_mode == "prepared" and mode != "speech":
            raise web.HTTPBadRequest(text="游戏表演模式需为speech或motion")
        fields = {"owner_id", "generation", "job_id"} | ({"text"} if mode == "speech" else {"plan"})
        optional = {"mode", "expires_at", "finish_before"} | ({"instruction", "plan"} if mode == "speech" else set())
        if (not isinstance(body, dict) or not fields <= set(body)
                or set(body) - fields - optional):
            raise web.HTTPBadRequest(text="游戏表演字段不正确")
        self.require_owner(body)
        job_id = self.identifier(body["job_id"])
        text = body.get("text", "")
        # Reject whitespace changes rather than silently modifying frozen copy.
        if mode == "speech" and (not isinstance(text, str) or text != text.strip() or not 1 <= len(text) <= 120):
            raise web.HTTPBadRequest(text="游戏定稿台词需1至120字且无首尾空白")
        plan = None
        if mode == "motion":
            try:
                plan = parse_director_plan(body["plan"], mode="motion")
                # 过渡动作只能占用一个短S窗口；不能借此提交长动作拖延正式口播。
                actions = plan["actions"]
                if (len(actions) != 1 or actions[0]["kind"] != "S"
                        or actions[0]["delay_seconds"] != 0 or actions[0]["hold_seconds"] != 0):
                    raise ValueError("游戏纯动作只接受一个立即执行的短S动作及收束")
                plan["timeline"] = compile_dialogue_motion_timeline(actions, plan["recovery"])
            except (ValueError, TypeError) as exc:
                raise web.HTTPBadRequest(text=str(exc)) from None
            instruction = ""
        elif "plan" in body:
            try:
                plan = parse_dialogue_semantic_plan(body["plan"], mode="once")
                if len(plan["segments"]) != 1 or len(plan["segments"][0]["actions"]) > 1:
                    raise ValueError("首版表演只接受一段台词和至多一组动作")
                if plan["segments"][0]["text"] != text:
                    raise ValueError("表演台词与定稿不一致")
            except (ValueError, TypeError) as exc:
                raise web.HTTPBadRequest(text=str(exc)) from None
        if mode == "speech":
            instruction = body.get("instruction", plan["segments"][0]["instruction"] if plan else LEGACY_INSTRUCTION)
            if (not isinstance(instruction, str) or instruction != instruction.strip()
                    or not 1 <= len(instruction) <= 160):
                raise web.HTTPBadRequest(text="游戏定稿语气需1至160字且无首尾空白")
            if plan and plan["segments"][0]["instruction"] != instruction:
                raise web.HTTPBadRequest(text="表演语气与定稿不一致")
            try:
                plan = plan or self.frozen_plan(text, instruction)
            except (ValueError, TypeError) as exc:
                raise web.HTTPBadRequest(text=str(exc)) from None
        deadlines = {}
        for name in ("expires_at", "finish_before"):
            if name in body:
                value = body[name]
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise web.HTTPBadRequest(text="表演截止时间需为有效的绝对秒数")
                deadlines[name] = value
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        # 冻结请求与运行时锚点分开：重查同 ID 时不能借另一入口改语气或期限。
        request_fingerprint = hashlib.sha256(json.dumps({"mode": mode, "submission_mode": submission_mode,
            "plan": plan, "deadlines": deadlines}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        async with self.studio.lock:
            self.require_owner(body)
            previous = self.jobs.get(job_id)
            if previous:
                if (submission_mode != previous.get("submission_mode", "legacy")
                        or mode != previous.get("mode", "speech") or plan != self.submitted_plan(previous)
                        or deadlines != {k: previous["item"][k] for k in ("expires_at", "finish_before") if k in previous["item"]}):
                    raise web.HTTPConflict(text="同一游戏任务不能更换冻结表演或截止时间")
                return web.json_response(self.job_public(previous))
            s = self.studio.current
            occupied = [j for j in self.jobs.values() if self.busy(j)]
            # 只让已准备协议的 current 与 next 重叠。旧入口还会现场合成，
            # 不能把它误当成可安全并行的发送阶段；motion 也不能插队。
            blocked = (bool(occupied) if submission_mode == "legacy" else
                       self.next_job() is not None or any(j.get("submission_mode", "legacy") == "legacy"
                                                         for j in occupied))
            if (not self.studio.automation.live_ready() or self.studio.automation.last_error
                    or self.studio.closing or s.stopping or blocked):
                raise web.HTTPConflict(text="请等待本场音画就绪或当前口播结束")
            if len(self.jobs) >= 300:
                raise web.HTTPConflict(text="本场游戏任务已达上限，请结束本场")
            item = self.studio.enqueue_comment(text, "游戏定稿", input_kind="external_game",
                source="external_game", external_job_id=job_id, external_generation=self.generation,
                external_mode=mode, external_owner_id=self.owner_id,
                external_submission_mode=submission_mode, external_request_fingerprint=request_fingerprint,
                requires_prepared=submission_mode == "prepared", checkpoint=False)
            item.update(status="processing", external_worker_pending=submission_mode == "legacy",
                        external_preparation_pending=submission_mode == "prepared", **deadlines)
            job = {"job_id": job_id, "mode": mode, "text": text, "instruction": instruction,
                   "text_sha256": digest, "submitted_plan": copy.deepcopy(plan), "item": item,
                   "submission_mode": submission_mode, "request_fingerprint": request_fingerprint}
            self.jobs[job_id] = job
            try:
                self.studio.accept_plan(s, item, plan, enqueue=submission_mode == "legacy")
                if submission_mode == "prepared":
                    item["status"] = "preparing"
                    task = asyncio.create_task(self.prepare_job(s, job))
                    job["preparation_task"] = task
                    s._external_preparation_tasks.add(task)
                    def finished(done):
                        # 槽到 task 真正结束才释放，取消后的慢 TTS 不能被下一次请求覆盖。
                        s._external_preparation_tasks.discard(done)
                        item["external_preparation_pending"] = False
                        if done.cancelled():
                            item.update(status="cancelled", cancel_requested=True)
                        elif done.exception() is not None:
                            item.update(status="error", failure_code="preparation_failed",
                                        error="本条准备未完成；未发送且不会自动重试")
                            s.discard_prepared(item)
                        try:
                            s.checkpoint()
                        except Exception as exc:
                            s.record_failure("external_preparation_archive", exc)
                    task.add_done_callback(finished)
            except Exception as exc:
                # No await occurs before put_nowait: a failed acceptance cannot
                # race the worker. Keep the ID terminal so retry never pays again.
                item.update(status="error", external_worker_pending=False, external_preparation_pending=False,
                            failure_code="external_accept_failed_before_queue",
                            error="发送前归档或入队失败；本条不会自动重试")
                s.record_failure("external_accept", exc)
                try:
                    s.checkpoint()
                except Exception as archive_error:
                    s.record_failure("external_accept_archive", archive_error)
            return web.json_response(self.job_public(job))

    async def prepare_job(self, session, job):
        item = job["item"]
        ready = await session.prepare_comment(item)
        if ready and self.preparation_allowed(item):
            cached = session._cached_preparation(item)
            item["external_send_seconds"] = session.external_delivery_seconds(item, cached)
            if (item.get("expires_at", float("inf")) <= time.time()
                    or time.time() + item["external_send_seconds"] >= item.get("finish_before", float("inf"))):
                item.update(status="expired", cancel_reason="external_deadline")
                session.discard_prepared(item)
        elif not self.preparation_allowed(item) and item["status"] not in {"error", "expired", "cancelled"}:
            item.update(status="cancelled", cancel_requested=True)
            session.discard_prepared(item)
        if item["status"] == "error":
            item["failure_code"] = "preparation_failed"

    async def dispatch(self, request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"owner_id", "generation"}:
            raise web.HTTPBadRequest(text="派发游戏任务只接受owner_id和generation")
        async with self.studio.lock:
            self.require_owner(body)
            job = self.jobs.get(self.identifier(request.match_info["job_id"]))
            if job is None:
                raise web.HTTPNotFound(text="未找到本场游戏任务")
            if job.get("submission_mode") != "prepared":
                raise web.HTTPConflict(text="旧入口任务不能改用准备派发协议")
            item, session = job["item"], self.studio.current
            # 网络响应可能丢失；同 ID 已入队或终态都只回原收据，绝不二次入队。
            if item.get("external_dispatched") or item["status"] in {"sent", "error", "expired", "cancelled"}:
                return web.json_response(self.job_public(job))
            task = job.get("preparation_task")
            if (item["status"] != "prepared" or item.get("external_preparation_pending")
                    or task is None or not task.done()):
                raise web.HTTPConflict(text="游戏任务仍在准备，请查询同一任务")
            if any(j is not job and j["item"].get("external_worker_pending") for j in self.jobs.values()):
                raise web.HTTPConflict(text="当前口播尚未结束，不能预约稍后自动发送")
            if (not self.preparation_allowed(item) or not self.studio.automation.live_ready()
                    or session.worker_fault or session.component.comment_id is not None):
                item.update(status="cancelled", cancel_requested=True, cancel_reason="external_dispatch_invalid")
                session.discard_prepared(item)
            else:
                try:
                    cached = session._cached_preparation(item)
                    if cached is None:
                        raise ValueError("external_prepared_cache_missing")
                    item["external_send_seconds"] = session.external_delivery_seconds(item, cached)
                    now = time.time()
                    if (item.get("expires_at", float("inf")) <= now
                            or now + item["external_send_seconds"] >= item.get("finish_before", float("inf"))):
                        item.update(status="expired", cancel_reason="external_deadline")
                        session.discard_prepared(item)
                    else:
                        # 占 current 与入队之间无 await，竞争 cancel/dispatch 只能有一个胜者。
                        item.update(external_dispatched=True, external_worker_pending=True,
                                    queued_at_monotonic=time.monotonic())
                        session.work.put_nowait(item)
                except ValueError:
                    self.reject_prepared(item)
                    session.discard_prepared(item)
            session.checkpoint()
            return web.json_response(self.job_public(job))

    async def cancel_job(self, request):
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"owner_id", "generation"}:
            raise web.HTTPBadRequest(text="取消游戏任务只接受owner_id和generation")
        async with self.studio.lock:
            self.require_owner(body)
            job = self.jobs.get(self.identifier(request.match_info["job_id"]))
            if job is None:
                raise web.HTTPNotFound(text="未找到本场游戏任务")
            item = job["item"]
            if item.get("delivery_started"):
                item["cancel_result"] = "already_started"
            elif item["status"] in {"sent", "error", "expired"}:
                item["cancel_result"] = "already_terminal"
            else:
                item.update(cancel_requested=True, status="cancelled", cancel_reason="external_cancel",
                            cancel_result="cancelled_before_delivery")
                self.studio.current.discard_prepared(item)
                task = job.get("preparation_task")
                if task and not task.done():
                    task.cancel()
            self.studio.current.checkpoint()
            return web.json_response(self.job_public(job))

    async def get_job(self, request):
        self.require_owner(request.query)
        job = self.jobs.get(self.identifier(request.match_info["job_id"]))
        if job is None:
            raise web.HTTPNotFound(text="未找到本场游戏任务")
        return web.json_response(self.job_public(job))

    async def state(self, request):
        return web.json_response(self.public())

    async def stop(self, request):
        self.require_owner(await request.json())
        self.studio.director.stop("external_game_stop")
        self.studio.automation.invalidate("external_game_stop", replies=True)
        s = self.studio.current
        s.latch_stop()
        s.spawn(s.request_stop("游戏工作台结束会话"))
        self.checkpoint()
        return web.json_response({"requested": True})
