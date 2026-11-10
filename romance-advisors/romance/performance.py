"""One optional next candidate; content preparation never owns the game clock."""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import random
import time

from .expression import ExpressionValidationError, parse_ambient_result


# 正式口播后固定执行；GE/S/Q 与间隔仍由 Studio 编译，recovery 只描述眼神和表情。
MESSAGE_MOTION_PLAN = {
    "emotion": "专注输入消息",
    "actions": [{"kind": "S", "delay_seconds": 0, "hold_seconds": 0,
                 "motions": [{"part": "head", "description": "垂下下巴，低头看向桌面靠近自己的位置，脸颊轻靠托腮的手，实体手机不出镜"},
                             {"part": "right_arm", "description": "空闲的右手在画面下沿轻点两下，像在输入文字"}],
                 "gaze": "专注看向近处桌面下沿", "expression": "嘴唇自然轻合，专注认真"}],
    "recovery": {"gaze": "视线短暂停在下方", "expression": "嘴唇自然轻合，神情放松"},
}


class AmbientPerformance:
    def __init__(self, program):
        self.program = program
        self.task = None
        self.record = None
        self.scope = None
        self.epoch = 0
        self.episode_id = None
        self.next_attempt = 0.0
        self.last_sent = 0.0
        self.responded_ids = set()
        self.seen = self.responded_ids  # Compatibility: seen now means really answered.
        self.inflight_ids = set()
        self._inflight_owners = {}
        self.attempted_at = {}
        self.next_dispatch = 0.0
        self._remembered_jobs = set()
        self.attempts = 0
        self.nudged = False
        self.history = []
        self.events = []
        self.error = ""
        self.rng = random.Random()
        self.preparation_samples = []
        self.delivery_samples = []

    def reset(self):
        # Start is already guarded against an active program. Nothing queued
        # in a previous run may become the new character's first line.
        self.invalidate()
        self.scope = self.episode_id = None
        self.record = None
        self.next_attempt = self.last_sent = 0.0
        self.history = []
        self._remembered_jobs.clear()
        self.next_dispatch = 0.0
        self.events = []
        self.error = ""
        self.preparation_samples = []
        self.delivery_samples = []

    def event(self, status, **fields):
        self.events.append({"status": status, "at": time.time(), **fields})
        self.events = self.events[-20:]

    def public(self):
        return {"preparing": bool(self.program.next and self.program.next["optional"]),
                "history": copy.deepcopy(self.history), "recent_events": copy.deepcopy(self.events)}

    def invalidate(self):
        self.epoch += 1
        candidate = self.program.next
        if candidate and candidate["optional"]:
            candidate["invalidated"] = True
            # Content generation has no Studio job yet. A started prepare must
            # finish/cancel by the same ID before the next slot can be reused.
            task = candidate.get("task")
            if candidate["state"] == "planning" and task and not task.done():
                task.cancel()

    def scope_for(self, state):
        episode, round_ = state.get("episode") or {}, state.get("round") or {}
        return (self.program.run_id, episode.get("id"), round_.get("id"),
                round_.get("phase"), self.program.workbench.deadline)

    def facts_for(self, state=None):
        # A round/phase change does not itself change what the audience knows.
        # Use public messages: a saved but not yet spoken formal choice is private.
        state = state or self.program.workbench.game.public_state()
        episode = state.get("episode") or {}
        return (self.program.run_id, episode.get("id"), episode.get("score"), episode.get("status"),
                tuple((m["id"], m["role"], m["text"]) for m in state.get("messages", [])[-8:]))

    def current(self, epoch, scope, *, content_mode="phase_bound", facts=None):
        p, wb = self.program, self.program.workbench
        r, e = wb.game.current_round or {}, wb.game.episode or {}
        current = (p.enabled and not p.formal_waiting and self.epoch == epoch
                   and scope[:2] == (p.run_id, e.get("id"))
                   and not wb.game.data.get("error") and not wb.error)
        if content_mode == "conversation":
            return (current and e.get("status") != "TECH_ENDED" and r.get("phase") != "ABORTED"
                    and facts == self.facts_for())
        # A call for participation depends on this phase.
        # Ordinary conversation instead follows its public story facts above.
        return current and scope == self.scope_for(wb.game.public_state()) and (scope[3] == "DRAFTING" or (
            wb.deadline is not None and time.time() < wb.deadline and not wb.game.busy))

    def invalidate_stale(self):
        candidate = self.program.next
        if (candidate and candidate["optional"] and not candidate["invalidated"]
                and not self.program.candidate_valid(candidate)):
            self.invalidate()

    def participation_budget(self):
        # Conservative cold-start defaults are replaced by subsequent actual
        # receipts. This only chooses a task's purpose; it never bans chat.
        return max(self.preparation_samples[-6:] or [5.5]) + max(self.delivery_samples[-6:] or [9.0])

    def prepared(self, candidate, receipt):
        if candidate.get("planning_started") is None:
            return
        self.preparation_samples.append(max(0.0, time.monotonic() - candidate["planning_started"]))
        self.preparation_samples = self.preparation_samples[-6:]
        seconds = receipt.get("estimated_delivery_seconds")
        if type(seconds) in (int, float) and 0 < seconds < float("inf"):
            self.delivery_samples.append(seconds)
            self.delivery_samples = self.delivery_samples[-6:]

    def selected_comments(self, scope, now):
        # Game admission and conversational relevance are separate. A late
        # proposal is still a real comment, and an unanswered earlier-round
        # comment may still be worth answering. Only a new cast changes whose
        # conversation this is; the model chooses relevance within that story.
        game = self.program.workbench.game

        def same_cast(comment):
            # A request sent from the previous cast can arrive after the new
            # episode is created. Receipt episode alone would misattribute it.
            sent_round = game._get("rounds", comment.get("expected_round_id"))
            return sent_round is None or sent_round["episode_id"] == scope[1]

        rows = [c for c in game.data["comments"]
                if c.get("episode_id") == scope[1]
                and same_cast(c)
                and c.get("kind") != "vote" and c["id"] not in self.responded_ids
                and c["id"] not in self.inflight_ids
                and now - self.attempted_at.get(c["id"], -float("inf")) >= 6]
        selected, authors, chars = [], set(), 0
        for row in reversed(rows):
            if row["author_id"] in authors or chars + len(row["text"]) > 3500:
                continue
            selected.append(row)
            authors.add(row["author_id"])
            chars += len(row["text"])
            if len(selected) >= 8:
                break
        return list(reversed(selected))

    def tick(self, state):
        """Cheap synchronous checks; never set game.busy or await generation."""
        p, wb = self.program, self.program.workbench
        now = time.monotonic()
        scope = self.scope_for(state)
        if scope != self.scope:
            self.invalidate_stale()
            self.scope = scope
            self.attempts, self.nudged = 0, False
            # A 20-second collection window should not waste its first second
            # before any generation has even begun. Failure backoff is separate.
            self.next_attempt = 0.0
            if scope[1] != self.episode_id:
                self.episode_id = scope[1]
                self.history = []
                self._remembered_jobs.clear()
                self.responded_ids.clear()
                self.inflight_ids.clear()
                self._inflight_owners.clear()
                self.attempted_at.clear()
        self.invalidate_stale()
        phase = scope[3]
        remaining = (wb.deadline or 0) - time.time()
        if (not p.enabled or p.formal_waiting or state.get("error")
                or phase not in {"COLLECTING", "SELECTING", "VOTING", "DECIDING",
                                 "DRAFTING", "WAITING_REPLY", "DONE"}
                or not wb.game.episode or wb.game.episode.get("status") in {"PREPARING", "TECH_ENDED"}):
            return
        selected = self.selected_comments(scope, now)
        if p.current and ((p.current.get("record") or {}).get("mode") == "motion"
                          or not (p.current.get("receipt") or {}).get("delivery_started")):
            # Until delivery is confirmed the model cannot treat current as
            # spoken context. Waiting here prevents two ungrounded near-copies.
            return
        candidate = p.next
        if candidate is not None:
            if (selected and candidate["optional"] and candidate["kind"] == "proactive"
                    and not candidate.get("yielded_for_comments") and not candidate["invalidated"]):
                # Yield once to real comments. Comment candidates themselves are
                # stable, so a flood cannot cancel every sentence before speech.
                candidate["yielded_for_comments"] = True
                candidate["invalidated"] = True
                task = candidate.get("task")
                if candidate["state"] == "planning" and task and not task.done():
                    task.cancel()
            return
        if p.sender_failed or now < self.next_attempt:
            return
        if selected:
            trigger = "new_comments"
        elif phase == "DONE" and not self.attempts:
            trigger = "committed_result"
        elif (phase in {"COLLECTING", "VOTING"} and not state.get("busy")
              and self.participation_budget() <= remaining <= 28 and not self.nudged):
            trigger = "closing_soon"
        elif not self.attempts:
            trigger = "phase_open"
        else:
            trigger = "quiet_moment"
        self.attempts += 1
        self.nudged |= trigger == "closing_soon"
        epoch = self.epoch
        content_mode = "phase_bound" if trigger == "closing_soon" else "conversation"
        facts = self.facts_for(state)
        candidate = p.reserve_next(kind="comment" if selected else "proactive",
                                   valid=lambda: self.current(epoch, scope, content_mode=content_mode, facts=facts),
                                   epoch=epoch, scope=scope)
        if candidate:
            candidate.update(content_mode=content_mode, trigger=trigger)
            snapshot = self.context(state, selected, trigger, remaining, content_mode=content_mode)
            self.task = p.schedule_candidate(candidate, self.run(snapshot, epoch, scope, candidate))

    def context(self, state, comments, trigger, remaining, *, content_mode="phase_bound"):
        game = self.program.workbench.game
        payload = game._man_payload()
        # A formal reply can already be saved while its delivery is pending.
        # Ambient speech must see exactly the facts used by facts_for(), not
        # private saved dialogue that the audience has not received yet.
        payload["messages"] = [{"role": m["role"], "text": m["text"]}
                               for m in state.get("messages", [])[-8:]]
        r, episode = game.current_round, game.episode
        previous = next((row for row in reversed(game.data["rounds"])
                         if row["episode_id"] == episode["id"] and row.get("delta") is not None), None)
        scene = {"content_mode": content_mode, "score": episode["score"],
                              "last_delta": previous.get("delta") if previous else None,
                              "episode_status": episode["status"],
                              "input_source": "local_simulation"}
        if content_mode == "phase_bound":
            # Counts can change during TTS. Do not offer an instant snapshot as
            # a fact the character is invited to announce a few seconds later.
            scene.update(phase=r["phase"], remaining_seconds=max(0, int(remaining)), trigger=trigger)
        payload.update(scene=scene,
                       comments=[{"id": c["id"], "nickname": c["nickname"], "text": c["text"]} for c in comments],
                       **self.program.expression_context())
        return payload

    async def run(self, snapshot, epoch, scope, candidate):
        sent = False
        input_ids = [c["id"] for c in snapshot["comments"]]
        try:
            if not self.program.candidate_valid(candidate):
                return
            # Only rows actually passed to the model count as attempted input.
            self.inflight_ids.update(input_ids)
            self._inflight_owners.update({i: candidate for i in input_ids})
            self.attempted_at.update({i: time.monotonic() for i in input_ids})
            phase_bound = candidate.get("content_mode", "phase_bound") == "phase_bound"
            remaining = scope[4] - time.time() if phase_bound else None
            candidate["planning_started"] = time.monotonic()
            trigger = candidate.get("trigger", snapshot["scene"].get("trigger", "quiet_moment"))
            self.event("planning", job_id=candidate["job_id"], trigger=trigger,
                       content_mode=candidate.get("content_mode", "phase_bound"))
            # An ordinary thought does not get a near-zero generation timeout
            # merely because the input/voting clock is about to close.
            async with asyncio.timeout(min(8, max(.1, remaining)) if phase_bound else 8):
                result = await self.program.workbench.model.generate("ambient", snapshot)
            if not self.program.candidate_valid(candidate):
                self.event("discarded", reason="scene_changed")
                return
            checked = parse_ambient_result(result, [c["id"] for c in snapshot["comments"]])
            if checked is None:
                self.event("silent")
                return
            plan = checked["plan"]
            part = plan["segments"][0]
            record = {"job_id": candidate["job_id"],
                      "owner_id": self.program.run_id, "generation": self.program.client.generation,
                      "plan": plan, "instruction": part["instruction"]}
            if phase_bound:
                # Calls to vote/submit need this window. Story reflections have
                # no phase deadline; their facts/identity are checked at dispatch.
                record.update(expires_at=scope[4], finish_before=scope[4])
            # Set before the first network await. From here, cancellation is
            # cooperative and the accepted job is always checked by identity.
            self.record = record
            self.event("frozen", job_id=record["job_id"])
            candidate.update(intent=checked["intent"], comment_ids=checked["comment_ids"])
            receipt = await self.program.send(record, part["text"], optional=True,
                                              valid=candidate["valid"], candidate=candidate)
            if receipt["status"] == "sent":
                sent = True
                if self.episode_id == scope[1]:
                    self.program.remember_sent(record, part["text"], receipt,
                                               intent=checked["intent"], comment_ids=checked["comment_ids"])
                self.event("sent", text=part["text"], emotion=plan["emotion"],
                           trigger=trigger)
            else:
                detail = {}
                if receipt["status"] == "expired":
                    detail = {"required_delivery_seconds": receipt.get("estimated_delivery_seconds")}
                    if phase_bound:
                        detail["remaining_seconds"] = round(max(0, scope[4] - time.time()), 3)
                self.event("skipped", reason=receipt["status"], job_id=candidate["job_id"], **detail)
        except asyncio.CancelledError:
            self.event("discarded", reason="scene_changed")
        except Exception as exc:
            # Before submitting, a provider failure is still just an optional
            # line. Afterwards its transport outcome must be resolved safely.
            if candidate.get("record") is None:
                reason = ("generation_timeout" if isinstance(exc, TimeoutError) else
                          "invalid_ambient_result" if isinstance(exc, ValueError)
                          and str(exc) == "invalid_ambient_result" else "generation_unavailable")
                diagnostic = exc.diagnostic() if isinstance(exc, ExpressionValidationError) else None
                detail = {"validation": diagnostic} if diagnostic else {}
                self.event("skipped", reason=reason, job_id=candidate["job_id"], **detail)
                if diagnostic:
                    # Persist only structured metadata in the existing server log.
                    # Never log model output, exception repr or arbitrary keys.
                    logging.getLogger(__name__).warning("ambient_validation %s", json.dumps({
                        "run_id": self.program.run_id, "job_id": candidate["job_id"],
                        "validation": diagnostic}, ensure_ascii=False))
            else:
                self.error = "人物互动的发送状态未确认，自动联调已停止。"
                self.event("error", reason="delivery_unconfirmed")
        finally:
            if self.record is candidate.get("record"):
                self.record = None
            # Cleanup belongs to this exact candidate. A late old task must not
            # erase a new scope's input tracking or its replacement slot.
            for comment_id in input_ids:
                if self._inflight_owners.get(comment_id) is candidate:
                    self._inflight_owners.pop(comment_id)
                    self.inflight_ids.discard(comment_id)
            if self.program.next is candidate and candidate["state"] == "planning":
                self.program.next = None
            if candidate.get("yielded_for_comments"):
                self.program.notify_comment()
            elif not sent and self.scope == scope and self.epoch == epoch:
                self.next_attempt = max(self.next_attempt, time.monotonic() + self.rng.uniform(4, 6))

    def remember(self, text, intent, emotion, comment_ids=(), *, job_id=None, recovery=None,
                 post_audio_wait_seconds=None):
        if job_id and job_id in self._remembered_jobs:
            return
        if job_id:
            self._remembered_jobs.add(job_id)
        self.history.append({"job_id": job_id, "text": text, "intent": intent, "emotion": emotion,
                             "comment_ids": list(comment_ids), "recovery": copy.deepcopy(recovery)})
        self.history = self.history[-6:]
        self.responded_ids.update(comment_ids)
        self.last_sent = time.monotonic()
        # Count the completed audio tail toward the same breathing pause, rather
        # than adding a new pause after it. Older Studio receipts keep the old
        # delay; neither preparation nor an unfinished send earns this credit.
        completed_wait = post_audio_wait_seconds
        if type(completed_wait) not in (int, float) or not 0 <= completed_wait < float("inf"):
            completed_wait = 0.0
        breath = self.rng.uniform(1, 3)
        self.next_dispatch = self.last_sent + (breath - min(breath, completed_wait))

    async def close(self):
        self.invalidate()
        if self.program._comment_wakeup is not None:
            self.program._comment_wakeup.cancel()
            self.program._comment_wakeup = None
        candidates = [self.program.current, self.program.next]
        tasks = {c["task"] for c in candidates if c and c["optional"] and c.get("task")}
        if self.task:
            tasks.add(self.task)
        tasks.discard(asyncio.current_task())
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.task = None
