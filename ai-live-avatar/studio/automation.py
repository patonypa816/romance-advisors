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
from live_interactions import DEFAULT_INTERACTIONS, InteractionQueue
from interaction_delivery import delivery_priority, interaction_plan
from interaction_delivery import compose_turn_plan
from interaction_fairness import InteractionFairness

IN_FLIGHT = {"pending", "processing", "ready", "prepared", "preparing", "synthesizing", "planning_actions", "sending"}
READY = {"ready", "sending", "draining"}
SELECTION_INTERVAL_SECONDS = 1
MAINTENANCE_INTERVAL_SECONDS = .25


class Automation:
    def __init__(self, studio, *, ai=None, source_factory=DyhubClient):
        self.studio = studio
        self.ai = ai if ai is not None else BailianAI(bailian_config(studio.data))
        self.source_factory = source_factory
        self.pool = CandidatePool()
        self.interactions = InteractionQueue(studio.config.get("interactions", DEFAULT_INTERACTIONS),
                                             viewer_key=self.pool.viewer_key)
        self.fairness = InteractionFairness()
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
        self._wakeup = asyncio.Event()
        self.last_error = ""
        self.error_since = None
        self.discarded_count = 0
        self.closing = False

    def start(self):
        self.loop_task = asyncio.create_task(self.run())

    def running(self):
        return bool(self.loop_task and not self.loop_task.done())

    def wake(self, _task=None):
        # Event 合并重复唤醒；采集回调不等待模型，也不为每条评论创建任务。
        self._wakeup.set()

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
                "selection_mode": "event_driven",
                "selection_min_interval_seconds": SELECTION_INTERVAL_SECONDS,
                "orchestration": self.fairness.snapshot(),
                "discarded_count": self.discarded_count, "pool": self.pool.snapshot()}

    def unified_selection(self):
        # 旧离线适配器和固定实验仍可运行原合同；正式百炼脑提供统一选择接口。
        return self.interactions_supported() and callable(getattr(self.ai, "select_turn", None))

    def future_slot_free(self):
        return not any(c["status"] in IN_FLIGHT and not c.get("delivery_started")
                       and not c.get("cancel_requested") for c in self.studio.current.comments)

    def served_kinds(self, item):
        if item.get("unified_turn"):
            return set(item.get("served_kinds", []))
        if item.get("interaction_kind") in {"gift", "welcome"}:
            return {item["interaction_kind"]}
        return {"comment"} if item.get("input_kind", "viewer") == "viewer" else set()

    def turn_viewer_keys(self, item):
        keys = {c["viewer_key"] for c in item.get("selected_comments", []) if c.get("viewer_key")}
        for job in item.get("interaction_jobs", []):
            if job["kind"] == "welcome":
                keys.update(job.get("meta", {}).get("representative_viewer_keys", []))
            else:
                keys.update(s["meta"]["viewer_key"] for s in job.get("meta", {}).get("sources", [])
                            if s.get("meta", {}).get("viewer_key"))
        return sorted(keys)

    def available_kinds(self, *, include_reserved=True):
        available = set()
        pool = self.pool.snapshot()
        if pool["candidate_count"] or pool["selecting_count"]:
            available.add("comment")
        for kind in ("gift", "welcome"):
            if self.interactions_waiting(kind):
                available.add(kind)
        if include_reserved:
            for c in self.studio.current.comments:
                if c["status"] in IN_FLIGHT and not c.get("cancel_requested"):
                    available.update(self.served_kinds(c))
        return available

    def settle_turns(self):
        """统一结算成功与未发送终态；调用多次不会重复计账或重复释放批次。"""
        for c in self.studio.current.comments:
            if c["status"] in IN_FLIGHT or c.get("turn_settled"):
                continue
            c["turn_settled"] = True
            success = c["status"] == "sent" and bool(c.get("delivery_started"))
            jobs = c.get("interaction_jobs", []) or ([c["interaction_job"]] if c.get("interaction_job") else [])
            for job in jobs:
                # 已进入发送但结果不完整时保守不重放，不能用缺少回执推断零发送。
                self.interactions.finish(job, success=success, sent_any=bool(c.get("delivery_started")),
                                         reason=c.get("cancel_reason") or c["status"])
            if success:
                self.fairness.complete(self.served_kinds(c), self.available_kinds())
                self.studio.current.event("interaction_round_completed", comment_id=c["id"],
                    served_kinds=sorted(self.served_kinds(c)), **self.fairness.snapshot())
        self.fairness.observe(self.available_kinds())

    def queue_count(self):
        return sum(c["status"] in IN_FLIGHT and c.get("operator_mode") != "topic"
                   and c.get("input_kind") != "interaction" for c in self.studio.current.comments)

    def interactions_supported(self):
        return (not getattr(getattr(self.studio, "external_game", None), "active", False)
                and not getattr(self.studio, "replay_fixture", None)
                and not any(getattr(self.studio, flag, False) for flag in
                            ("short_performance_test", "continuous_speech_test", "long_speech_test")))

    def interactions_waiting(self, kind=None):
        return (self.interactions_supported() and not self.studio.paused
                and not self.last_error and self.interactions.has_pending(kind))

    def cancel_interactions(self, reason, *, reset_seen=False):
        self.interactions.clear(reason, reset_seen=reset_seen,
                                preserve_reservation_ids=self.started_reservations())
        s = getattr(self.studio, "current", None)
        if s is not None:
            for c in s.comments:
                if ((c.get("input_kind") == "interaction" or c.get("unified_turn")) and c["status"] in IN_FLIGHT
                        and not c.get("delivery_started")):
                    c.update(status="cancelled", cancel_requested=True, cancel_reason=reason)
                    s.discard_prepared(c)
                    if self.reply_task and getattr(self.reply_task, "pipeline_item", None) is c:
                        self.reply_task.cancel()
            s.checkpoint()
        self.wake()

    def started_reservations(self):
        ids = []
        for c in getattr(self.studio.current, "comments", []):
            if c.get("delivery_started") and c["status"] in IN_FLIGHT:
                jobs = c.get("interaction_jobs", []) or ([c["interaction_job"]] if c.get("interaction_job") else [])
                ids.extend(j.get("reservation_id", j["id"]) for j in jobs)
        return ids

    def materialize_interaction(self, kind):
        """只在轮间从独立队列领取一项；忙时继续合并，不提前固定连击数量。"""
        s = self.studio.current
        if (not self.live_ready(s) or not self.interactions_waiting(kind)
                or self.studio.director.delivery_busy()
                or any(c.get("interaction_kind") == kind and c["status"] in IN_FLIGHT for c in s.comments)):
            return None
        job = self.interactions.pop(kind)
        if job is None:
            return None
        c = self.studio.enqueue_comment(job["text"] or "礼物动作反馈", job["nickname"], source="interaction",
            input_kind="interaction", interaction_kind=job["kind"],
            interaction_mode=job.get("mode", "speech"),
            interaction_job=job, expires_at=job["expires_at"])
        s.event("interaction_queued", comment_id=c["id"], interaction_kind=job["kind"])
        return c

    def live_ready(self, session=None):
        s = session or self.studio.current
        return (s is self.studio.current and s.started and not s.stopping
                and s.component.state in READY and not self.closing
                and not getattr(self.studio, "performance_holding",
                                getattr(self.studio, "avatar_switch_holding", False)))

    def can_select(self):
        return (not getattr(getattr(self.studio, "external_game", None), "active", False)
                and not getattr(self.studio, "replay_fixture", None)
                and (self.enabled or (self.unified_selection() and self.interactions_waiting()))
                and self.live_ready() and not self.studio.paused
                and not self.last_error and self.ai.configured()
                and (self.future_slot_free() if self.unified_selection() else self.queue_count() < 2))

    def schedule_selection(self, now):
        """有候选和处理余量就筛选；只限制两次实际请求的最短启动间隔。"""
        if (self.selection_task or not self.can_select()
                or (self.source_status.get("state") not in {"connected", "live"}
                    and not (self.unified_selection() and self.interactions_waiting()))):
            return
        if self.next_selection_at is not None and now < self.next_selection_at:
            return
        if not (self.available_kinds(include_reserved=False) if self.unified_selection()
                else self.pool.snapshot()["candidate_count"]):
            return
        # 先占用唯一筛选槽；同一轮中的多个唤醒不能各自发起一次请求。
        self.selection_task = asyncio.create_task(self.select_once())
        self.selection_task.add_done_callback(self.wake)

    def wake_timeout(self):
        # 保留原维护节奏，覆盖跳过回复、播放结束等非来源事件；限频到点也会唤醒。
        remaining = (self.next_selection_at or 0) - time.monotonic()
        return min(MAINTENANCE_INTERVAL_SECONDS, remaining) if remaining > 0 else MAINTENANCE_INTERVAL_SECONDS

    def invalidate(self, reason, *, replies=False):
        # 清候选不清已发生请求的限频，快速暂停/恢复或重连不能绕过间隔。
        self.pool.clear(reason)
        if reason == "new_session":
            self.fairness.reset()
        self.cancel_interactions(reason, reset_seen=reason == "new_session")
        if self.selection_task:
            self.selection_task.cancel()
        if replies:
            for task in (self.reply_task, self.topic_task):
                if task:
                    task.cancel()
        self.wake()

    def fail(self, code, role):
        # 只用于发送结果不明或工作循环失效；单条内容准备失败留在本条记录。
        self.last_error = code
        if self.error_since is None:
            self.error_since = time.monotonic()
        self.pool.clear("model_error")
        self.cancel_interactions("model_error")
        self.studio.current.event("brain_ai_error", role=role, error_code=code)
        self.wake()

    def resume(self):
        # Resume authorizes future work, never replays an uncertain paid request.
        if not self.ai.configured():
            raise ValueError("百炼大脑凭据或模型配置未就绪")
        self.last_error = ""
        self.ai.last_error = ""
        self.error_since = None
        self.pool.clear("operator_resume")
        self.wake()

    async def on_event(self, event):
        if (self.enabled and self.live_ready() and not self.studio.paused
                and not self.last_error):
            if isinstance(event, dict) and event.get("type") in ("gift", "member"):
                if self.interactions_supported():
                    result = self.interactions.ingest(event)
                    if result.get("status") in {"queued", "merged", "candidate"}:
                        self.wake()
                return
            result = self.pool.ingest(event)
            if result["status"] == "candidate":
                self.wake()
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
            self.pool.clear("source_connect")
            self.source_stop = asyncio.Event()
            self.source = source
            self.enabled = True
            self.source_task = asyncio.create_task(source.run(self.source_stop))

    def on_status(self, status):
        changed = status.get("state") != self.source_status.get("state")
        self.source_status = dict(status)
        if status.get("state") in {"closed", "error", "retrying", "stopped"}:
            self.invalidate("source_unavailable")
        elif changed:
            self.wake()

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
        if self.unified_selection():
            return await self.select_turn_once()
        if not self.can_select():
            return
        batch = self.pool.lease_batch()
        if not batch:
            return
        s = self.studio.current
        version = self.pool.version
        try:
            # 从实际开始模型请求计时，空池、繁忙和协程等待不消耗限频窗口。
            self.next_selection_at = time.monotonic() + SELECTION_INTERVAL_SECONDS
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
                # 这一批已经由 failed() 消费，不重试收费请求；新评论可继续处理。
                s.event("selection_failed", error_code=code, batch_id=batch["batch_id"])
                self.wake()

    @staticmethod
    def fit_turn_selection(result, snapshot, required):
        """在付费准备前缩小过长的组合；不截断名字、模板或声称覆盖被舍弃项。"""
        result = {**result, "gift_ids": list(result["gift_ids"]), "comment_ids": list(result["comment_ids"])}
        gifts = {g["id"]: g for g in snapshot["gifts"]}
        welcome_text = snapshot["welcome"]["text"] if result["welcome_id"] else ""
        # 保守按原模板长度计预算；礼物队列实际合并通常更短。
        def size():
            return len(welcome_text) + sum(len(gifts[g]["text"]) for g in result["gift_ids"])
        while size() > 120 and len(result["gift_ids"]) > 1:
            result["gift_ids"].pop()
        if size() > 120:
            keep = required or result["primary_kind"]
            if keep == "welcome":
                result["gift_ids"] = []
            else:
                result["welcome_id"] = None
                welcome_text = ""
        if (result["comment_ids"] and result["gift_ids"] and not result["welcome_id"]
                and all(gifts[g]["mode"] == "motion" for g in result["gift_ids"])):
            # 纯动作礼物没有独立台词锚点，不能用它覆盖评论明确要求的动作。
            # 在准备前拆轮，保留另一类候选供下一次选择。
            if (required or result["primary_kind"]) == "gift":
                result["comment_ids"] = []
            else:
                result["gift_ids"] = []
        covered = ({"comment"} if result["comment_ids"] else set()) | ({"gift"} if result["gift_ids"] else set()) | ({"welcome"} if result["welcome_id"] else set())
        if result["primary_kind"] not in covered:
            result["primary_kind"] = required if required in covered else next(iter(sorted(covered)), None)
        return result

    async def select_turn_once(self):
        if not self.can_select():
            return
        self.settle_turns()
        lease = self.pool.lease_batch()
        interaction = self.interactions.snapshot_candidates()
        snapshot = {"comments": lease["candidates"] if lease else [],
                    "gifts": interaction["gift"], "welcome": interaction["welcome"]}
        available = ({"comment"} if snapshot["comments"] else set()) | ({"gift"} if snapshot["gifts"] else set()) | ({"welcome"} if snapshot["welcome"] else set())
        if not available:
            return
        s, version = self.studio.current, self.pool.version
        current = next((c for c in s.comments if c.get("delivery_started") and c["status"] in IN_FLIGHT), None)
        policy = self.fairness.choose(available, self.served_kinds(current) if current is not None else None)
        context = {"operator_settings": self.studio.config["settings"],
                   "recent_history": self.studio.reply_history(s), "current_topic": self.studio.director.context(),
                   **policy, "current_turn": ({"id": current["id"], "status": current["status"],
                       "served_kinds": sorted(self.served_kinds(current)), "comment_text": current["text"],
                       "answer": current["answer"], "nicknames": [current["nickname"]],
                       "viewer_keys": self.turn_viewer_keys(current)} if current else None)}
        jobs = []
        try:
            self.next_selection_at = time.monotonic() + SELECTION_INTERVAL_SECONDS
            result = await self.ai.select_turn(snapshot, context)
            if s is not self.studio.current or version != self.pool.version or not self.can_select():
                if lease:
                    self.pool.release_batch(lease["batch_id"])
                return
            result = self.fit_turn_selection(result, snapshot, policy["required_kind"])
            if result["primary_kind"] is None:
                if lease:
                    self.pool.resolve_many(lease["batch_id"], [], result["reason"])
                # 如果只剩互动而脑选择略过，明确消费本次快照，避免每秒付费重问同一批。
                if not lease:
                    ids = [g["id"] for g in snapshot["gifts"][:3]]
                    skipped = self.interactions.reserve_selection(gift_ids=ids, welcome_id=(snapshot["welcome"] or {}).get("id"))
                    for job in skipped:
                        self.interactions.finish(job, success=False, sent_any=False, reason="selection_skipped")
                s.event("interaction_selection_skipped", reason=result["reason"])
                return
            jobs = self.interactions.reserve_selection(gift_ids=result["gift_ids"], welcome_id=result["welcome_id"])
            requested = ({"gift"} if result["gift_ids"] else set()) | ({"welcome"} if result["welcome_id"] else set())
            if requested != {j["kind"] for j in jobs}:
                raise ValueError("interaction_snapshot_expired")
            selected = []
            if lease:
                if result["comment_ids"]:
                    selected = self.pool.resolve_many(lease["batch_id"], result["comment_ids"], result["reason"])
                    if len(selected) != len(result["comment_ids"]):
                        raise ValueError("comment_snapshot_expired")
                else:
                    self.pool.release_batch(lease["batch_id"])
            served = sorted(requested | ({"comment"} if selected else set()))
            text = selected[0]["text"] if selected else "".join(j["text"] for j in jobs) or "礼物动作反馈"
            nickname = selected[0]["nickname"] if selected else "、".join(j["nickname"] for j in jobs)
            pure_motion = not selected and len(jobs) == 1 and jobs[0].get("mode") == "motion"
            c = self.studio.enqueue_comment(text, nickname, source="dyhub" if selected else "interaction",
                input_kind="viewer" if selected else "interaction", unified_turn=True,
                primary_kind=result["primary_kind"], served_kinds=served, selected_comments=selected,
                interaction_jobs=jobs, interaction_kind=(jobs[0]["kind"] if len(jobs) == 1 else "mixed"),
                interaction_mode="motion" if pure_motion else "speech",
                selection_reason=result["reason"], expires_at=time.time()+120)
            for candidate in selected:
                self.pool.mark_delivery(candidate["id"], "enqueued", result["reason"])
            s.event("interaction_turn_selected", comment_id=c["id"], served_kinds=served,
                    primary_kind=c["primary_kind"], required_kind=policy["required_kind"],
                    waiting_rounds=policy["waiting_rounds"], welcome_count=sum(j.get("meta", {}).get("batch_size", 0) for j in jobs if j["kind"] == "welcome"))
        except asyncio.CancelledError:
            if lease:
                self.pool.release_batch(lease["batch_id"])
            raise
        except Exception as exc:
            if lease:
                self.pool.failed(lease["batch_id"], "turn_selection_failed")
            for job in jobs:
                self.interactions.finish(job, success=False, sent_any=False, reason="selection_failed")
            if not jobs:
                # 模型请求失败不立即对同一批互动无限重试；本次候选明确略过。
                skipped = self.interactions.reserve_selection(
                    gift_ids=[g["id"] for g in snapshot["gifts"][:3]],
                    welcome_id=(snapshot["welcome"] or {}).get("id"))
                for job in skipped:
                    self.interactions.finish(job, success=False, sent_any=False, reason="selection_failed")
            s.event("selection_failed", error_code=getattr(exc, "code", "turn_selection_failed"))
        finally:
            self.wake()

    async def reply_once(self, item=None):
        if (getattr(self.studio, "replay_fixture", None)
                or getattr(getattr(self.studio, "external_game", None), "active", False)):
            return
        s = self.studio.current
        if not self.live_ready(s) or self.last_error:
            return
        c = item if item is not None else self.studio.director.next_item()
        if not c:
            return
        if c.get("input_kind") != "interaction" and not self.ai.configured():
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
            if c.get("unified_turn"):
                selected = c.get("selected_comments", [])
                jobs = c.get("interaction_jobs", [])
                if selected:
                    if jobs or len(selected) > 1:
                        answer = await self.ai.generate_turn_reply(c, self.studio.reply_history(s, c), context, comments=selected)
                    else:
                        answer = await self.ai.generate_reply(c, self.studio.reply_history(s, c), context)
                    plan = compose_turn_plan(jobs, answer)
                else:
                    plan = compose_turn_plan(jobs)
            elif c.get("input_kind") == "interaction":
                plan = interaction_plan(c["interaction_job"])
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
            if phase == "accept_plan" and isinstance(exc, (ValueError, TypeError, KeyError)):
                validation = {"stage": "accept_plan", "reason": str(exc) or type(exc).__name__}
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
                self.preparation_failed(c, code)
        finally:
            if self.active_item is c:
                self.active_item = None
            self.studio.director.sync()
            self.settle_turns()
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
        # 观众、礼物和一次主持安排失败只结束本条，后续队列继续；不自动重试。
        self.wake()

    def dispatch_prepared(self):
        s = self.studio.current
        if not self.live_ready(s) or self.last_error or self.studio.director.delivery_busy():
            return
        candidates = [c for c in s.comments if c.get("pipeline_managed") and c["status"] == "prepared" and not c.get("delivery_queued")]
        candidates.sort(key=lambda c: (delivery_priority(c), c["created_at"]))
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
            # 先清信号再检查当前状态；本轮 await 期间的新事件仍能唤醒下一轮。
            self._wakeup.clear()
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
            self.settle_turns()
            if s.stopping or s.component.state in TERMINAL:
                if self.interactions.has_pending() or any(c.get("input_kind") == "interaction"
                        and c["status"] in IN_FLIGHT and not c.get("delivery_started") for c in s.comments):
                    self.cancel_interactions("session_stop")
                self.studio.director.stop("session_stop")
                if self.enabled:
                    await self.disconnect("session_stopped")
                for task in (self.reply_task, self.topic_task):
                    if task:
                        task.cancel()
            elif (self.live_ready(s) and not self.last_error and not getattr(self.studio, "replay_fixture", None)
                  and not getattr(getattr(self.studio, "external_game", None), "active", False)):
                self.dispatch_prepared()
                item = self.studio.director.next_item()
                if item is not None and (item.get("input_kind") == "interaction" or self.ai.configured()):
                    name = "topic_task" if item.get("operator_mode") == "topic" else "reply_task"
                    if not getattr(self, name):
                        task = asyncio.create_task(self.reply_once(item))
                        setattr(self, name, task)
                        task.add_done_callback(self.wake)
            self.schedule_selection(time.monotonic())
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=self.wake_timeout())
            except asyncio.TimeoutError:
                pass

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
