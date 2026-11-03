"""Join the local comment source to two AI roles, without starting a paid live.

The generation and pool version are cancellation fences: a late model response
cannot become speech after pause, source disconnect, stop or a new session.
"""
from __future__ import annotations

import asyncio
import time

from bailian_ai import BailianAI
from cloudbase_ai import ModelError
from comment_intake import CandidatePool
from credentials import bailian_config
from dyhub_client import DyhubClient
from live_session import TERMINAL

IN_FLIGHT = {"pending", "processing", "ready", "prepared", "preparing", "synthesizing", "planning_actions", "sending"}
READY = {"ready", "sending", "draining"}
SELECTION_INTERVAL_SECONDS = 5


class Automation:
    def __init__(self, studio, *, ai=None, source_factory=DyhubClient):
        self.studio = studio
        self.ai = ai if ai is not None else BailianAI(bailian_config(studio.data))
        self.source_factory = source_factory
        self.pool = CandidatePool()
        self.enabled = False
        self.source = None
        self.source_task = None
        self.source_stop = asyncio.Event()
        self.source_lock = asyncio.Lock()
        self.source_version = 0
        self.source_status = {"state": "stopped", "room_id": "", "error_code": None}
        self.loop_task = self.selection_task = self.reply_task = self.topic_task = None
        self.active_item = None
        self.last_tick = time.monotonic()
        self.next_selection_at = None
        self.last_error = ""
        self.error_since = None
        self.discarded_count = 0
        self.closing = False

    def start(self):
        self.loop_task = asyncio.create_task(self.run())

    def running(self):
        return bool(self.loop_task and not self.loop_task.done())

    def brain_public(self):
        if getattr(self.studio, "replay_fixture", None):
            # 回放状态页也不检查在线模型凭据；开场只需数字人连接。
            return {"provider": "fixed_replay", "model": None, "configured": True,
                    "worker_running": self.running(), "busy": False, "last_error": "",
                    "requests": {"selection": 0, "reply": 0}, "usage": {}}
        return {**self.ai.public(), "worker_running": self.running(),
                "busy": bool(self.reply_task or self.topic_task or self.selection_task or
                             getattr(self.studio.current, "timed_action_task", None)),
                "last_error": self.last_error}

    def public(self):
        return {"enabled": self.enabled, "source": dict(self.source_status),
                "settings": dict(self.studio.config["intake"]), "last_error": self.last_error,
                "selection_interval_seconds": SELECTION_INTERVAL_SECONDS,
                "discarded_count": self.discarded_count, "pool": self.pool.snapshot()}

    def queue_count(self):
        return sum(c["status"] in IN_FLIGHT and c.get("operator_mode") != "topic" for c in self.studio.current.comments)

    def live_ready(self, session=None):
        s = session or self.studio.current
        return (s is self.studio.current and s.started and not s.stopping
                and s.component.state in READY and not self.closing
                and not getattr(self.studio, "performance_holding",
                                getattr(self.studio, "avatar_switch_holding", False)))

    def can_select(self):
        return (not getattr(self.studio, "replay_fixture", None)
                and self.enabled and self.live_ready() and not self.studio.paused
                and not self.last_error and self.ai.configured() and self.queue_count() < 2)

    def schedule_selection(self, now):
        """按时间开窗口；评论到达、队列腾空和模型完成都不提前触发下一批。"""
        if not (self.enabled and self.live_ready() and not self.studio.paused
                and not self.last_error and self.source_status.get("state") in {"connected", "live"}):
            self.next_selection_at = None
            return
        if self.next_selection_at is None:
            self.next_selection_at = now + SELECTION_INTERVAL_SECONDS
            return
        if now < self.next_selection_at:
            return
        # 空池或繁忙也消费本窗口。循环晚到时从现在再等满间隔，避免集中补跑。
        self.next_selection_at = now + SELECTION_INTERVAL_SECONDS
        if (not self.selection_task and self.can_select()
                and self.pool.snapshot()["candidate_count"]):
            self.selection_task = asyncio.create_task(self.select_once())

    def invalidate(self, reason, *, replies=False):
        self.next_selection_at = None
        self.pool.clear(reason)
        if self.selection_task:
            self.selection_task.cancel()
        if replies:
            for task in (self.reply_task, self.topic_task):
                if task:
                    task.cancel()

    def fail(self, code, role):
        self.last_error = code
        self.next_selection_at = None
        if self.error_since is None:
            self.error_since = time.monotonic()
        self.pool.clear("model_error")
        self.studio.current.event("brain_ai_error", role=role, error_code=code)

    def resume(self):
        # Resume authorizes future work, never replays an uncertain paid request.
        if not self.ai.configured():
            raise ValueError("百炼大脑凭据或模型配置未就绪")
        self.last_error = ""
        self.ai.last_error = ""
        self.error_since = None
        self.next_selection_at = None
        self.pool.clear("operator_resume")

    async def on_event(self, event):
        if (self.enabled and self.live_ready() and not self.studio.paused
                and not self.last_error):
            self.pool.ingest(event)
        else:
            # Don't buffer comments for a future live or replay after resume.
            self.discarded_count += 1

    async def connect(self):
        if getattr(self.studio, "short_performance_test", False):
            raise ValueError("短表演试验期间不连接评论来源")
        if getattr(self.studio, "continuous_speech_test", False):
            raise ValueError("连续口播试验期间不连接评论来源")
        if getattr(self.studio, "long_speech_test", False):
            raise ValueError("长文试验期间不连接评论来源")
        session = self.studio.current
        async with self.source_lock:
            if (self.closing or self.studio.closing or session is not self.studio.current
                    or session.stopping or session.component.state in TERMINAL):
                raise ValueError("source_session_unavailable")
            if self.enabled:
                return
            settings = self.studio.config["intake"]
            self.source_version += 1
            version = self.source_version

            async def on_event(event):
                if version == self.source_version:
                    await self.on_event(event)

            def on_status(status):
                if version == self.source_version:
                    self.on_status(status)

            source = self.source_factory(settings["base_url"], settings["room_id"], on_event, on_status)
            self.next_selection_at = None
            self.pool.clear("source_connect")
            self.source_stop = asyncio.Event()
            self.source = source
            self.enabled = True
            self.source_task = asyncio.create_task(source.run(self.source_stop))

    def on_status(self, status):
        self.source_status = dict(status)
        if status.get("state") in {"closed", "error", "retrying", "stopped"}:
            self.invalidate("source_unavailable")

    async def disconnect(self, reason="source_disconnect"):
        async with self.source_lock:
            self.enabled = False  # Gate before closing the HTTP stream.
            self.source_version += 1
            self.invalidate(reason)
            source, task, stop = self.source, self.source_task, self.source_stop
            stop.set()
            if source:
                await source.close()
            if task:
                await asyncio.gather(task, return_exceptions=True)
            self.source = self.source_task = None
            self.source_status["state"] = "stopped"

    async def select_once(self):
        if not self.can_select():
            return
        batch = self.pool.lease_batch()
        if not batch:
            return
        s = self.studio.current
        version = self.pool.version
        try:
            result = await self.ai.select_comments(batch, {
                "operator_settings": self.studio.config["settings"],
                "recent_history": self.studio.reply_history(s), "queue_count": self.queue_count(),
                "current_topic": self.studio.director.context()})
            if (s is not self.studio.current or version != self.pool.version
                    or not self.can_select()):
                self.pool.failed(batch["batch_id"], "intake_changed")
                return
            outcome = self.pool.resolve(batch["batch_id"], result["selected_id"], result["reason"])
            candidate = outcome["selected"]
            if candidate:
                if candidate["expires_at"] <= time.time() or self.queue_count() >= 2:
                    self.pool.mark_delivery(candidate["id"], "not_enqueued", "expired_or_busy")
                    return
                self.studio.enqueue_comment(candidate["text"], candidate["nickname"], source="dyhub",
                                            candidate_id=candidate["id"], expires_at=candidate["expires_at"])
                self.pool.mark_delivery(candidate["id"], "enqueued", result["reason"])
                s.event("comment_selected", candidate_id=candidate["id"], reason=result["reason"])
        except asyncio.CancelledError:
            self.pool.failed(batch["batch_id"], "cancelled")
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, ModelError) else "selection_failed"
            self.pool.failed(batch["batch_id"], code)
            if s is self.studio.current and version == self.pool.version and self.live_ready(s):
                self.fail(code, "selection")

    async def reply_once(self, item=None):
        if getattr(self.studio, "replay_fixture", None):
            return
        s = self.studio.current
        if not self.live_ready(s) or self.last_error or not self.ai.configured():
            return
        c = item if item is not None else self.studio.director.next_item()
        if not c:
            return
        # Scheduling a coroutine is not the same as starting it. Pause, skip,
        # or a topic replacement may invalidate this pending item in that gap.
        if c["status"] != "pending":
            return
        if (c.get("cancel_requested") or
                (c.get("input_kind") == "operator" and not self.studio.director.is_current(c))):
            c["status"] = "cancelled"
            s.checkpoint()
            return
        if c.get("expires_at", float("inf")) <= time.time():
            c["status"] = "expired"
            s.checkpoint()
            return
        c["status"] = "processing"
        self.active_item = c
        task = asyncio.current_task()
        if task:
            task.pipeline_item = c
        c["pipeline_managed"] = not any(getattr(self.studio, flag, False) for flag in
                                        ("short_performance_test", "continuous_speech_test", "long_speech_test"))
        s.checkpoint()
        phase = "model"
        try:
            context = {**s.brain_context, "current_topic": self.studio.director.context()}
            if c.get("performance_request_id"):
                phase = "performance_plan"
                plan = await self.studio.performance.prepare_plan(c, self.ai, s.brain_context)
            elif c.get("input_kind") == "operator":
                topic = None
                if c.get("operator_mode") == "topic":
                    t = self.studio.director.topic
                    if not t["outline"]:
                        t["preparation_state"] = "outline"
                        outline_job = {**c, "rounds": t["total_rounds"], "total_rounds": t["total_rounds"]}
                        generate_outline = getattr(self.ai, "generate_topic_outline", None)
                        if generate_outline:
                            outline = await generate_outline(outline_job, context)
                        else:
                            # Offline/legacy adapters still have a finite, reviewable endpoint.
                            outline = [{"id": f"part-{i}", "title": f"第{i}段", "goal": t["instruction"]} for i in range(1, t["total_rounds"] + 1)]
                        if not self.studio.director.is_current(c) or not self.live_ready(s):
                            c["status"] = "cancelled"
                            return
                        if not isinstance(outline, list) or len(outline) != t["total_rounds"]:
                            raise ValueError("finite_outline_invalid")
                        t["outline"] = outline
                        s.event("director_outline_prepared", topic_id=t["id"], outline=outline)
                    t["preparation_state"] = "model"
                    topic = self.studio.director.model_context(c)
                plan = await self.ai.generate_director(c, self.studio.reply_history(s, c), context, topic)
            else:
                plan = await self.ai.generate_reply(c, self.studio.reply_history(s, c), context)
            if (not self.live_ready(s) or c["status"] != "processing" or c.get("cancel_requested")
                    or (c.get("input_kind") == "operator" and not self.studio.director.is_current(c))):
                c["status"] = "cancelled"
                return
            if c.get("expires_at", float("inf")) <= time.time():
                c["status"] = "expired"
                return
            phase = "accept_plan"
            if not c["pipeline_managed"]:
                # Fixed experimental modes keep their established worker and
                # validation contract; they do not permit standby topics.
                self.studio.accept_plan(s, c, plan)
                return
            self.studio.accept_plan(s, c, plan, enqueue=False)
            phase = "preparation"
            if c.get("operator_mode") == "topic":
                self.studio.director.topic["preparation_state"] = "audio_actions"
            prepared = await s.prepare_comment(c)
            if (s is not self.studio.current or c.get("cancel_requested")
                    or (c.get("operator_mode") == "topic" and not self.studio.director.is_current(c))):
                c["status"] = "cancelled"
                s.discard_prepared(c)
                return
            if not prepared:
                if c["status"] == "error":
                    self.preparation_failed(c, c.get("error", "preparation_failed"))
                return
            if c.get("operator_mode") == "topic":
                self.studio.director.topic["preparation_state"] = "prepared"
            self.dispatch_prepared()
        except asyncio.CancelledError:
            if c["status"] == "processing":
                c["status"] = "cancelled"
            raise
        except Exception as exc:
            if c.get("cancel_requested") or c["status"] == "cancelled":
                c["status"] = "cancelled"
                return
            code = exc.code if isinstance(exc, ModelError) else "reply_failed"
            validation = getattr(exc, "validation", None) if isinstance(exc, ModelError) else None
            if phase in {"accept_plan", "performance_plan"} and isinstance(exc, (ValueError, TypeError, KeyError)):
                validation = {"stage": phase, "reason": str(exc) or type(exc).__name__}
            c["status"], c["model_error_code"] = "error", code
            if isinstance(validation, dict) and isinstance(validation.get("reason"), str):
                # Attach diagnostics to this reply, never to shared AI state:
                # a later failure must not overwrite another comment's cause.
                detail = s.diagnostics.clean({key: validation[key] for key in
                    ("stage", "reason", "response_shape") if key in validation})
                detail["reason"] = detail["reason"][:500]
                c["plan_error"] = detail
                c["error"] = f"模型计划未通过：{detail['reason']}；本条不会自动重试"
                s.event("llm_plan_rejected", comment_id=c["id"], input_kind=c.get("input_kind", "viewer"),
                        operator_mode=c.get("operator_mode"), error_code=code, **detail)
            else:
                c["error"] = f"模型处理失败（{code}）；本条不会自动重试"
            c["error_phase"] = "preparation"
            if s is self.studio.current and self.live_ready(s):
                if c.get("operator_mode") == "topic":
                    self.preparation_failed(c, code)
                elif not c.get("performance_request_id"):
                    self.fail(code, "reply")
        finally:
            if self.active_item is c:
                self.active_item = None
            self.studio.director.sync()
            performance = getattr(self.studio, "performance", None)
            if performance:
                performance.sync()
                performance.checkpoint()
            s.checkpoint()

    def preparation_failed(self, c, code):
        c["error_phase"] = "preparation"
        self.studio.current.event("preparation_failed", comment_id=c["id"], error_code=code)
        t = self.studio.director.topic
        if c.get("operator_mode") == "topic" and t and c.get("topic_id") == t["id"]:
            t.update(state="error", preparation_state="failed", error_phase="preparation",
                     error="未来段准备失败，当前已开始的小段继续收尾；请检查后新安排，不会自动重试")
            self.studio.director.cancel_unsent_topic("preparation_failed")
            self.studio.director.checkpoint()
        elif c.get("input_kind") != "operator":
            self.fail("preparation_failed", "preparation")

    def dispatch_prepared(self):
        s = self.studio.current
        if not self.live_ready(s) or self.last_error or self.studio.director.delivery_busy():
            return
        candidates = [c for c in s.comments if c.get("pipeline_managed") and c["status"] == "prepared" and not c.get("delivery_queued")]
        candidates.sort(key=lambda c: (2 if c.get("operator_mode") == "topic" else 0 if c.get("input_kind") == "operator" else 1, c["created_at"]))
        for c in candidates:
            if c.get("operator_mode") == "topic" and not self.studio.director.is_current(c):
                c.update(status="cancelled", cancel_requested=True, cancel_reason="stale_preparation")
                s.discard_prepared(c)
                continue
            if c.get("expires_at", float("inf")) <= time.time():
                c["status"] = "expired"
                s.discard_prepared(c)
                continue
            if not self.studio.director.delivery_gate(c):
                continue
            c["delivery_queued"] = True
            s.work.put_nowait(c)
            s.checkpoint()
            return

    async def run(self):
        while not self.closing:
            self.last_tick = time.monotonic()
            for name in ("selection_task", "reply_task", "topic_task"):
                task = getattr(self, name)
                if task and task.done():
                    # Retrieve failures instead of silently losing a worker.
                    if not task.cancelled() and task.exception():
                        self.fail("worker_failed", name)
                    setattr(self, name, None)
            s = self.studio.current
            self.studio.director.sync()
            if s.stopping or s.component.state in TERMINAL:
                self.studio.director.stop("session_stop")
                performance = getattr(self.studio, "performance", None)
                if performance:
                    performance.stop()
                if self.enabled:
                    await self.disconnect("session_stopped")
                for task in (self.reply_task, self.topic_task):
                    if task:
                        task.cancel()
            elif self.live_ready(s) and not self.last_error and not getattr(self.studio, "replay_fixture", None):
                if not self.ai.configured():
                    self.fail("credential_unavailable", "configuration")
                    continue
                self.dispatch_prepared()
                item = self.studio.director.next_item()
                if item is not None:
                    name = "topic_task" if item.get("operator_mode") == "topic" else "reply_task"
                    if not getattr(self, name):
                        setattr(self, name, asyncio.create_task(self.reply_once(item)))
            self.schedule_selection(time.monotonic())
            await asyncio.sleep(.25)

    async def close(self):
        self.closing = True
        self.studio.director.stop("session_stop")
        self.invalidate("service_closed", replies=True)
        await self.disconnect("service_closed")
        tasks = [t for t in (self.loop_task, self.selection_task, self.reply_task, self.topic_task) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.ai.close()
