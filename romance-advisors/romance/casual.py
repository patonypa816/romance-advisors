"""Ordinary cafe conversation, using Program's single owned Studio send queue."""
from __future__ import annotations

import asyncio
import copy
import random
import time
from collections import OrderedDict

from .expression import parse_ambient_result


class CasualPerformance:
    COMMENT_INTERVAL = 3.0
    ATTEMPT_INTERVAL = 6.0
    FAILURE_INTERVAL = 20.0
    COMMENT_RETRY_INTERVAL = 45.0
    MAX_FAILURES = 3
    MAX_COMMENTS = 32

    def __init__(self, program):
        self.program = program
        self.epoch = 0
        self.task = None
        self.candidate = None
        self.rng = random.Random()
        self.error = ""
        self.reset()

    def reset(self):
        self.invalidate()
        self.comments = OrderedDict()
        self.seen = OrderedDict()
        self.attempted_at = {}
        self.history = []
        self.next_attempt = 0.0
        self.next_topic = 0.0
        self.next_comment = 0.0
        self.failures = 0
        self.error = ""

    def invalidate(self):
        # The mode epoch also changes in Program; this local epoch protects reset
        # and close even if they occur before Program publishes its new mode.
        self.epoch += 1
        candidate = self.candidate
        if candidate:
            candidate["invalidated"] = True
            task = candidate.get("task")
            if candidate.get("state") == "planning" and task and not task.done():
                task.cancel()
        if hasattr(self, "comments"):
            self.comments.clear()
            self.attempted_at.clear()
            self.history.clear()

    def _running(self):
        p = self.program
        return (p.state == "running" and p.ready and p.content_mode == "chat"
                and not p.sender_failed and not self.error)

    def _valid(self, scope):
        p = self.program
        # switchable is only an admission check: our own reserved next slot must
        # not invalidate this closure while Studio is preparing its audio.
        return self._running() and scope == (p.run_id, p.mode_epoch, self.epoch)

    def offer(self, comment):
        """Accept canonical, already-observed comments; never model raw source events."""
        if not self._running() or not isinstance(comment, dict):
            return False
        row = {}
        for key, limit in (("id", 256), ("nickname", 256), ("text", 2000)):
            value = comment.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                return False
            row[key] = value.strip()
        if row["id"] in self.seen:
            return False
        self.seen[row["id"]] = None
        while len(self.seen) > 256:
            self.seen.popitem(last=False)
        self.comments[row["id"]] = row
        while len(self.comments) > self.MAX_COMMENTS:
            key, _ = self.comments.popitem(last=False)
            self.attempted_at.pop(key, None)
        candidate = self.candidate
        if (candidate is self.program.next and candidate and candidate.get("kind") == "proactive"
                and not candidate.get("invalidated") and candidate.get("state") in {"planning", "preparing", "prepared"}):
            # Revoke before dispatch, then let the existing Program resolve any
            # Studio preparation under the same job ID. Never open another slot.
            candidate["invalidated"] = True
            candidate["yielded_for_comments"] = True
            task = candidate.get("task")
            if candidate.get("state") == "planning" and task and not task.done():
                task.cancel()
        self.program.notify_comment()
        return True

    def tick(self, state=None):
        p, now = self.program, time.monotonic()
        if (not self._running() or not p.switchable or p.next is not None or p.current is not None
                or self.task and not self.task.done() or now < self.next_attempt):
            return
        rows = [row for key, row in self.comments.items()
                if now - self.attempted_at.get(key, -float("inf")) >= self.COMMENT_RETRY_INTERVAL]
        selected, size = [], 0
        for row in reversed(rows):
            if size + len(row["text"]) > 3500:
                continue
            selected.append(copy.deepcopy(row))
            size += len(row["text"])
            if len(selected) == 4:
                break
        selected.reverse()
        if selected:
            if now < self.next_comment:
                return
        elif self.comments or now < self.next_topic:
            # Unanswered inputs in backoff do not trigger unrelated filler.
            return
        scope = (p.run_id, p.mode_epoch, self.epoch)
        candidate = p.reserve_next(kind="comment" if selected else "proactive",
                                   valid=lambda: self._valid(scope), epoch=self.epoch, scope=scope)
        if candidate is None:
            return
        candidate["content_mode"] = "chat"
        self.candidate = candidate
        self.next_attempt = now + self.ATTEMPT_INTERVAL
        for row in selected:
            self.attempted_at[row["id"]] = now
        snapshot = {"scene": {"content_mode": "chat", "setting": "咖啡馆日常闲聊"},
                    "comments": selected, "recent_performances": copy.deepcopy(self.history[-6:]),
                    "current_performance": None}
        self.task = p.schedule_candidate(candidate, self._run(snapshot, scope, candidate))

    def _failed(self):
        self.failures += 1
        self.next_attempt = max(self.next_attempt, time.monotonic() + self.FAILURE_INTERVAL)
        if self.failures >= self.MAX_FAILURES:
            self.error = "普通聊天连续准备未完成，已暂停继续生成。"

    def _delivery_failure(self):
        self.program.sender_failed = True
        self.error = "普通聊天发送未完整确认，已停止继续发送。"

    async def _run(self, snapshot, scope, candidate):
        p, submitted = self.program, False
        try:
            if not p.candidate_valid(candidate):
                return
            async with asyncio.timeout(8):
                result = await p.workbench.model.generate("casual", snapshot)
            if not p.candidate_valid(candidate):
                return
            checked = parse_ambient_result(result, [row["id"] for row in snapshot["comments"]])
            if checked is None:
                # Silence is a successful content decision, not a provider error.
                # Retire exactly this assessed batch so it cannot block fresh
                # viewers or provoke repeated paid attempts at the same input.
                for row in snapshot["comments"]:
                    self.comments.pop(row["id"], None)
                    self.attempted_at.pop(row["id"], None)
                self.failures = 0
                now = time.monotonic()
                self.next_attempt = max(self.next_attempt, now + self.ATTEMPT_INTERVAL)
                self.next_comment = now + self.COMMENT_INTERVAL
                self.next_topic = now + self.rng.uniform(12, 20)
                return
            # A comment-triggered turn must actually answer an observed comment;
            # the model cannot silently replace viewer priority with filler.
            if snapshot["comments"] and not checked["comment_ids"]:
                self._failed()
                return
            plan = checked["plan"]
            part = plan["segments"][0]
            record = {"job_id": candidate["job_id"], "owner_id": p.run_id,
                      "generation": p.client.generation, "plan": plan, "instruction": part["instruction"]}
            candidate.update(intent=checked["intent"], comment_ids=checked["comment_ids"])
            submitted = True
            receipt = await p.send(record, part["text"], optional=True,
                                   valid=candidate["valid"], candidate=candidate)
            # This is Studio's local consumption boundary, never proof of viewer playback.
            complete = (isinstance(receipt, dict) and receipt.get("status") == "sent"
                        and receipt.get("send_complete") is True
                        and not receipt.get("worker_pending") and not receipt.get("preparation_pending"))
            if not complete:
                if isinstance(receipt, dict) and receipt.get("status") in {"cancelled", "canceled", "expired", "skipped"} and not receipt.get("delivery_started"):
                    if self._valid(scope):
                        self._failed()
                else:
                    # Even authoritative optional preparation failures must stop
                    # automatic paid retries; unknown delivery is equally blocking.
                    self._delivery_failure()
                return
            if not self._valid(scope):
                return
            self.failures = 0
            for key in checked["comment_ids"]:
                self.comments.pop(key, None)
                self.attempted_at.pop(key, None)
            self.history.append({"text": part["text"], "intent": checked["intent"],
                                 "emotion": plan["emotion"], "comment_ids": checked["comment_ids"],
                                 "recovery": copy.deepcopy(part.get("recovery"))})
            self.history = self.history[-6:]
            now = time.monotonic()
            self.next_comment = now + self.COMMENT_INTERVAL
            self.next_topic = now + self.rng.uniform(12, 20)
            self.next_attempt = max(self.next_attempt, self.next_comment)
        except asyncio.CancelledError:
            if submitted and candidate.get("state") not in {"terminal", "planning"}:
                self._delivery_failure()
        except Exception:
            if submitted:
                self._delivery_failure()
            elif self._valid(scope):
                self._failed()
        finally:
            if p.next is candidate and candidate.get("state") == "planning":
                p.next = None

    async def close(self):
        self.invalidate()
        task = self.task
        if task and task is not asyncio.current_task():
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
