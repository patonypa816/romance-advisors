"""Finite standby topics, honest send bookmarks and one future preparation slot."""
from __future__ import annotations

import copy
import time
import uuid
from live_session import save
from interaction_delivery import delivery_priority

IN_FLIGHT = {"pending", "processing", "ready", "prepared", "preparing", "synthesizing", "planning_actions", "sending"}
MAX_INITIAL_ROUNDS = 6


class Director:
    def __init__(self, studio):
        self.studio = studio
        self.topic = None

    def reset(self):
        self.topic = None
        self.checkpoint()

    def checkpoint(self):
        if self.studio.current:
            save(self.studio.current.out / "director-state.json", {
                "topic": self.topic,
                "progress_basis": "successful_send_and_handoff_not_confirmed_playback"})

    def public(self):
        self.sync()
        items = self.studio.current.comments
        topic = copy.deepcopy(self.topic)
        if topic:
            state = topic["state"]
            speaking = next((c for c in items if c.get("topic_id") == topic["id"] and c.get("delivery_started") and c["status"] in IN_FLIGHT), None)
            if state == "paused":
                view = "本段结束后暂停" if speaking else "主讲已暂停"
            elif state == "stopped":
                view = "本段结束后停止" if speaking else "已停止"
            elif state == "running":
                view = "持续互动·主讲等待空闲" if self.priority_waiting() else ("正在主讲" if speaking else ("准备接回" if topic["resume_required"] else "准备主讲"))
            else:
                view = {"stopped": "已停止", "completed": "已完成", "error": "需人工核对"}.get(state, state)
            topic.update(view_state=view, current_round=speaking.get("topic_round") if speaking else None,
                         prepared_rounds=[c["topic_round"] for c in items if c.get("topic_id") == topic["id"] and c["status"] == "prepared"],
                         next_action=("等待新安排" if state in {"completed", "stopped", "error"} else "等待手动继续" if state == "paused" else "优先回应互动，空闲时按书签续讲"))
        return {"topic": topic,
                "pending_count": sum(c.get("input_kind") == "operator" and c["status"] in IN_FLIGHT for c in items),
                "busy": any(c.get("input_kind") == "operator" and c["status"] in IN_FLIGHT - {"pending", "prepared"} for c in items)}

    def item(self, ident):
        return next((c for c in self.studio.current.comments if c["id"] == ident), None)

    def interaction_history(self):
        # These are bounded excerpts for the model; full original records stay on disk.
        return [{"id": c["id"], "text": c["text"][:160], "answer": c["answer"][:240]}
                for c in self.studio.current.comments if c["status"] == "sent" and c.get("answer") and c.get("operator_mode") != "topic"][-12:]

    def context(self):
        t = self.topic
        if not t:
            return None
        result = {key: copy.deepcopy(t[key]) for key in
                  ("id", "instruction", "state", "total_rounds", "completed_rounds", "sent_history", "resume_required")}
        # Selection can happen before standby preparation. An absent outline is
        # honest context; an empty list would claim a malformed finite outline.
        if t["outline"]:
            result["outline"] = copy.deepcopy(t["outline"])
        return result

    def model_context(self, item):
        if not self.is_current(item):
            raise ValueError("主讲准备已失效")
        result = self.context()
        result.update(next_round=item["topic_round"], resume_required=item.get("resume_required", False),
                      interaction_history=self.interaction_history())
        parent = self.item(item.get("predecessor_id"))
        if parent:
            result["predecessor"] = {"id": parent["id"], "round": parent["topic_round"],
                                     "text": parent["answer"], "status": "sent" if parent["status"] == "sent" else "conditional_draft"}
        return result

    def is_current(self, item):
        if item.get("operator_mode") != "topic":
            return not item.get("cancel_requested")
        t = self.topic
        if not (t and t["state"] == "running" and item.get("topic_id") == t["id"]
                and item.get("topic_version") == t["version"] and not item.get("cancel_requested")
                and item.get("return_context_version") == t["return_context_version"]):
            return False
        round_ = item["topic_round"]
        if round_ == t["completed_rounds"] + 1:
            return True
        parent = self.item(item.get("predecessor_id"))
        return bool(round_ == t["completed_rounds"] + 2 and parent and parent.get("topic_id") == t["id"]
                    and parent.get("topic_round") == round_ - 1 and parent.get("answer")
                    and parent["status"] in IN_FLIGHT and parent.get("fingerprint"))

    def submit(self, body):
        if not isinstance(body, dict) or set(body) - {"text", "mode", "rounds"}:
            raise ValueError("主持输入只接受内容、执行方式和主线段数")
        text, mode = body.get("text"), body.get("mode")
        if getattr(self.studio, "short_performance_test", False) and mode not in ("once", "motion"):
            raise ValueError("短表演试验只开放一次安排与只做动作，主线暂不接受")
        if getattr(self.studio, "continuous_speech_test", False) and mode != "once":
            raise ValueError("连续口播试验只开放模拟弹幕与一次安排，主线和纯动作暂不接受")
        if getattr(self.studio, "long_speech_test", False) and mode != "once":
            raise ValueError("长文试验只开放模拟弹幕与一次安排，主线和纯动作暂不接受")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 600:
            raise ValueError("主持指令需为1至600字")
        if mode not in {"once", "topic", "motion"}:
            raise ValueError("请选择一次安排、主讲话题或只做动作")
        text = text.strip()
        if mode == "topic":
            rounds = body.get("rounds", 3)
            if type(rounds) is not int or not 1 <= rounds <= MAX_INITIAL_ROUNDS:
                raise ValueError("首次主讲段数需为1至6")
            self.sync()
            self.cancel_unsent_topic("topic_replace")
            self.topic = {"id": uuid.uuid4().hex[:16], "instruction": text,
                          "state": "running", "total_rounds": rounds, "completed_rounds": 0,
                          "sent_history": [], "segments": [],
                          "outline": [], "version": 1, "return_context_version": 0,
                          "resume_required": False, "preparation_state": "pending", "error": ""}
            self.studio.current.event("director_topic_set", topic_id=self.topic["id"],
                                      instruction=text, total_rounds=rounds)
            self.checkpoint()
            return {"topic_id": self.topic["id"]}
        if "rounds" in body:
            raise ValueError("只有主讲话题需要段数")
        if sum(c.get("input_kind") == "operator" and c.get("operator_mode") != "topic"
               and c["status"] in IN_FLIGHT for c in self.studio.current.comments) >= 3:
            raise ValueError("已有三条主持安排在途，请稍候")
        c = self.studio.enqueue_comment(text, "主持指令", source="operator", input_kind="operator", operator_mode=mode)
        self.studio.current.event("director_instruction_queued", comment_id=c["id"], mode=mode)
        return {"id": c["id"]}

    def cancel_unsent_topic(self, reason):
        if not self.topic:
            return
        for c in self.studio.current.comments:
            if c.get("topic_id") == self.topic["id"] and c["status"] in IN_FLIGHT and not c.get("delivery_started"):
                c.update(cancel_requested=True, cancel_reason=reason, status="cancelled")
                discard = getattr(self.studio.current, "discard_prepared", None)
                if discard:
                    discard(c["id"])
                for task in (getattr(self.studio.automation, "topic_task", None), self.studio.automation.reply_task):
                    if task and c is getattr(task, "pipeline_item", None):
                        task.cancel()
        self.studio.current.checkpoint()

    def pause(self):
        self.sync()
        if not self.topic or self.topic["state"] != "running":
            raise ValueError("当前没有可暂停的主讲")
        t = self.topic
        t["resume_required"] = bool(t["completed_rounds"] or any(c.get("topic_id") == t["id"] and c.get("delivery_started") for c in self.studio.current.comments))
        t["state"] = "paused"
        t["version"] += 1
        self.cancel_unsent_topic("topic_pause")
        self.checkpoint()

    def resume(self):
        self.sync()
        if not self.topic or self.topic["state"] != "paused":
            raise ValueError("仅手动暂停的主讲可继续；完成或停止后请新安排")
        self.topic["state"] = "running"
        self.checkpoint()

    def stop(self, reason="topic_stop"):
        self.sync()
        if self.topic and self.topic["state"] not in {"completed", "stopped"}:
            self.topic["state"] = "stopped"
            self.topic["version"] += 1
            self.cancel_unsent_topic(reason)
            self.checkpoint()

    def sync(self):
        t = self.topic
        changed = False
        for c in self.studio.current.comments:
            if c["status"] not in IN_FLIGHT and c.get("delivery_started") and not c.get("director_receipted"):
                c["director_receipted"] = True
                changed = True
                if c.get("operator_mode") == "topic" and t and c.get("topic_id") == t["id"]:
                    if c["status"] == "sent" and c["topic_round"] == t["completed_rounds"] + 1:
                        t["completed_rounds"] += 1
                        t["sent_history"].append(c["answer"])
                        t["segments"].append({"id": c["id"], "round": c["topic_round"], "text": c["answer"], "status": "sent"})
                        if (c.get("resume_required") and c.get("topic_version") == t["version"]
                                and c.get("return_context_version") == t["return_context_version"]):
                            # A new manual pause while this segment is sending
                            # creates a fresh return intent; this receipt cannot consume it.
                            t["resume_required"] = False
                        if t["completed_rounds"] == t["total_rounds"] and t["state"] in {"running", "paused"}:
                            t["state"] = "completed"
                    elif c["status"] != "sent" and t["state"] in {"running", "paused"}:
                        t.update(state="error", error="本段未完整发送，需人工核对；不会自动重播", error_phase="sending")
                        self.cancel_unsent_topic("predecessor_failed")
                elif c.get("operator_mode") != "topic" and c["status"] == "sent" and t and t["state"] in {"running", "paused"} and t["completed_rounds"] < t["total_rounds"]:
                    # Only a real successful interruption changes the return context.
                    t["return_context_version"] += 1
                    t["resume_required"] = bool(t["completed_rounds"])
                    self.cancel_unsent_topic("interaction_context_changed")
        if changed:
            self.checkpoint()

    def priority_waiting(self):
        now = time.time()
        for c in self.studio.current.comments:
            if c.get("operator_mode") == "topic" or c["status"] not in IN_FLIGHT:
                continue
            if not c.get("delivery_started") and c.get("expires_at", float("inf")) <= now:
                c["status"] = "expired"
                continue
            return True
        a = self.studio.automation
        return (a.interactions_waiting() or bool(a.selection_task and not a.selection_task.done())
                or bool(a.enabled and not self.studio.paused and a.pool.snapshot()["candidate_count"]))

    def delivery_busy(self):
        s = self.studio.current
        return (s.component.comment_id is not None or not s.work.empty()
                or any((c.get("delivery_started") and c["status"] in IN_FLIGHT)
                       or (not c.get("pipeline_managed") and c["status"] in {"ready", "synthesizing", "planning_actions", "sending"})
                       for c in s.comments))

    def delivery_gate(self, c):
        c["delivery_queued"] = False
        external = getattr(self.studio, "external_game", None)
        if external and external.active:
            return external.delivery_allowed(c)
        self.sync()
        if self.studio.automation.last_error:
            return False
        if self.studio.automation.unified_selection():
            if c.get("cancel_requested"):
                return False
            jobs = c.get("interaction_jobs", [])
            if any(not self.studio.automation.interactions.reservation_active(j) for j in jobs):
                c.update(status="cancelled", cancel_requested=True, cancel_reason="reservation_expired")
                return False
            waiting = [i for i in self.studio.current.comments if i is not c and i["status"] in IN_FLIGHT
                       and not i.get("delivery_started") and not i.get("cancel_requested")
                       and i.get("expires_at", float("inf")) > time.time()]
            if any(delivery_priority(i) == 0 and (delivery_priority(c) != 0 or i["created_at"] < c["created_at"])
                   for i in waiting):
                return False
            if c.get("operator_mode") == "topic":
                # 已投入准备的主讲也属于唯一下一轮，普通新事件不能让两边互等。
                return bool(self.is_current(c) and c["topic_round"] == self.topic["completed_rounds"] + 1)
            return not any(delivery_priority(i) == delivery_priority(c) and i["created_at"] < c["created_at"]
                           for i in waiting)
        if c.get("operator_mode") == "topic":
            return bool(self.is_current(c) and c["topic_round"] == self.topic["completed_rounds"] + 1 and not self.priority_waiting())
        if c.get("cancel_requested"):
            return False
        rank = delivery_priority(c)
        waiting = [i for i in self.studio.current.comments if i["status"] in IN_FLIGHT
                   and not i.get("delivery_started") and not i.get("cancel_requested")
                   and i.get("expires_at", float("inf")) > time.time()]
        # 收到礼物后，已经准备好的弹幕也要在真正发送前复核；不打断已开始的一轮。
        if rank > 1 and self.studio.automation.interactions_waiting("gift"):
            return False
        if any(delivery_priority(i) < rank for i in waiting):
            return False
        if rank == 0:
            manual = [i for i in waiting if delivery_priority(i) == 0]
            return not manual or manual[0] is c
        return True

    def delivery_started(self, c):
        c["delivery_started"] = True
        jobs = c.get("interaction_jobs", []) or ([c["interaction_job"]] if c.get("interaction_job") else [])
        for job in jobs:
            self.studio.automation.interactions.mark_started(job)
        self.checkpoint()

    def delivery_finished(self, c):
        self.sync()
        self.studio.automation.settle_turns()
        if c["status"] != "sent" and not self.studio.current.stopping:
            c["error_phase"] = "sending"
            self.studio.automation.fail("delivery_failed", "sending")
        self.studio.automation.wake()

    def next_item(self):
        """Choose preparation, independent of the one actual sender."""
        self.sync()
        s = self.studio.current
        for c in s.comments:
            if c["status"] in IN_FLIGHT and not c.get("delivery_started") and c.get("expires_at", float("inf")) <= time.time():
                c["status"] = "expired"
                discard = getattr(s, "discard_prepared", None)
                if discard:
                    discard(c)
        active = [c for c in s.comments if c["status"] in IN_FLIGHT and not c.get("cancel_requested")]
        unified = self.studio.automation.unified_selection()
        if not unified and not any(delivery_priority(c) == 0 for c in active):
            self.studio.automation.materialize_interaction("gift")
            if (not self.studio.automation.interactions_waiting("gift")
                    and not any(delivery_priority(c) <= 2 for c in s.comments if c["status"] in IN_FLIGHT)):
                self.studio.automation.materialize_interaction("welcome")
        pending = [c for c in s.comments if c["status"] == "pending" and not c.get("cancel_requested")
                   and c.get("operator_mode") != "topic"]
        priority = min(pending, key=lambda c: (delivery_priority(c), c["created_at"]), default=None)
        if unified:
            committed = [c for c in active if not c.get("delivery_started") and c["status"] != "pending"]
            if committed:
                # 人工主持明确插队是既有控制权限；不能一边挡住已备轮，一边不给主持准备机会。
                if priority and delivery_priority(priority) == 0 and all(c["status"] == "prepared" for c in committed):
                    return priority
                return None
            if priority:
                return priority
        # 已准备好的低优先级内容可以等，不能占住更高优先级任务需要的准备位置。
        preparing_priority = any(c.get("operator_mode") != "topic" and not c.get("delivery_started")
            and c["status"] in IN_FLIGHT - {"pending", "sending"}
            and (c["status"] != "prepared" or not priority or delivery_priority(c) <= delivery_priority(priority))
            for c in s.comments)
        if priority and not preparing_priority:
            return priority
        t = self.topic
        if not t or t["state"] != "running" or self.priority_waiting() or t["completed_rounds"] >= t["total_rounds"]:
            return None
        active = [c for c in s.comments if c.get("topic_id") == t["id"] and c["status"] in IN_FLIGHT]
        if active:
            pending_topic = next((c for c in active if c["status"] == "pending"), None)
            if pending_topic:
                return pending_topic
            # The current final draft is a conditional parent, never sent history.
            parent = max(active, key=lambda c: c["topic_round"])
            if len(active) >= 2 or not parent.get("delivery_started") or not parent.get("fingerprint") or parent["topic_round"] >= t["total_rounds"]:
                return None
            round_ = parent["topic_round"] + 1
        else:
            parent = None
            round_ = t["completed_rounds"] + 1
        c = self.studio.enqueue_comment(t["instruction"], "主持指令", source="operator", input_kind="operator",
                                      operator_mode="topic", topic_id=t["id"], topic_round=round_, topic_version=t["version"],
                                      predecessor_id=parent["id"] if parent else None,
                                      return_context_version=t["return_context_version"], resume_required=t["resume_required"] if not parent else False)
        self.checkpoint()
        return c
