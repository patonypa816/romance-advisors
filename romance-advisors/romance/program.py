"""One bounded game/avatar run, driven by confirmed local send-queue progress."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import inspect
import re
import time
import uuid

from .avatar_client import AvatarClient, AvatarClientError, check_startup_blocker
from .performance import AmbientPerformance, MESSAGE_MOTION_PLAN

ACTIVE = {"starting", "running", "stopping"}
TERMINAL_AVATAR = {"ended", "error", "stop_unconfirmed", "creation_unknown"}
REPAIRABLE_CONTENT_ERRORS = {
    "PREPARING": {"invalid_model_result", "invalid_adult_character", "invalid_name",
                  "invalid_personality", "invalid_background", "invalid_speech_style",
                  "invalid_private_preferences", "invalid_public_hints", "invalid_title", "invalid_opening"},
    "SELECTING": {"invalid_model_result", "invalid_options", "invalid_option",
                  "invalid_option_source", "invalid_summary", "invalid_conflict_reason"},
    "DRAFTING": {"invalid_model_result", "invalid_selected_source", "invalid_self_selection",
                 "invalid_man_message", "invalid_reaction", "invalid_reaction_instruction",
                 "invalid_reaction_expression"},
    "WAITING_REPLY": {"invalid_model_result", "invalid_woman_message", "invalid_delta",
                      "invalid_ending_score_consistency"},
}


class Program:
    def __init__(self, workbench, client=None):
        self.workbench = workbench
        self.client = client or AvatarClient(obs_output=workbench.obs_output)
        if hasattr(self.client, "discover"):
            with contextlib.suppress(ValueError):
                self.client.discover()
        self.state = "idle"
        self.run_id = None
        self.started_at = None
        self.ended_at = None
        self.page_seen = None
        self.last_heartbeat = 0
        self.last_poll = 0
        self.avatar_state = "idle"
        self.startup_stage = ""
        self.ready = False
        self.error = ""
        self.last_failure = None
        self.cleanup = {"cloud_stop_confirmed": None, "errors": [], "steps": {}}
        self.task = None
        self._stop_task = None
        self._stop_error = ""
        self.stopping = False
        self._player_url = ""
        self.validation_repairs = {}
        self.send_lock = asyncio.Lock()
        self.formal_waiting = 0
        self.sender_failed = False
        self.current = None
        self.next = None
        self._comment_wakeup = None
        self.ambient = AmbientPerformance(self)

    @property
    def active(self):
        return self.state in ACTIVE

    @property
    def enabled(self):
        return self.state == "running"

    @property
    def player_url(self):
        return self._player_url if self.active else ""

    def public(self, *, operator=False):
        now = self.ended_at or time.monotonic()
        value = {"state": self.state, "ready": self.ready, "run_id": self.run_id,
                 "elapsed_seconds": max(0, now - self.started_at) if self.started_at is not None else 0,
                 "limit_minutes": 20, "avatar_name": "咖啡馆里的温柔凝视",
                 "avatar_state": self.avatar_state, "startup_stage": self.startup_stage, "error": self.error}
        if operator:
            value["obs_output"] = self.workbench.obs_output
            value["player_url"] = "" if self.workbench.obs_output else self.player_url
            value["last_failure"] = dict(self.last_failure) if self.last_failure else None
            value["cleanup"] = {"cloud_stop_confirmed": self.cleanup["cloud_stop_confirmed"],
                                "errors": [dict(item) for item in self.cleanup["errors"]],
                                "steps": dict(self.cleanup["steps"])}
            value["ambient"] = self.ambient.public()
        return value

    def heartbeat(self, run_id):
        if not self.active or run_id != self.run_id:
            raise ValueError("invalid_program_run")
        self.page_seen = time.monotonic()

    def start(self):
        wb = self.workbench
        if self.active or (wb.job and not wb.job.done()) or wb.game.busy:
            raise ValueError("operation_in_progress")
        # Every explicit run begins a fresh episode. Existing history is kept;
        # an old text-only reaction is never unexpectedly spoken on activation.
        self.run_id = uuid.uuid4().hex
        self.started_at = self.page_seen = time.monotonic()
        self.ended_at = None
        self.last_heartbeat = self.last_poll = 0
        self.state, self.ready, self.error = "starting", False, ""
        self.startup_stage = "preparing_session"
        self.last_failure = None
        self.cleanup = {"cloud_stop_confirmed": None, "errors": [], "steps": {}}
        self._player_url = ""
        self.validation_repairs = {}
        self.ambient.reset()
        self.sender_failed = False
        self.current = self.next = None
        wb.settings["auto_advance"] = False
        wb.clock_key = None
        wb.deadline = None
        wb.error = None
        self.task = asyncio.create_task(self._start(self.run_id))

    async def _start(self, run_id):
        try:
            await self.client.prepare_session(run_id)
            if not self._starting(run_id):
                return
            self._player_url = self.client.player_url
            self.startup_stage = "waiting_player"
            deadline = time.monotonic() + 90
            while self._starting(run_id):
                snapshot = await self.client.snapshot()
                if not self._starting(run_id):
                    return
                self.avatar_state = snapshot.get("session", {}).get("state", "idle")
                check_startup_blocker(snapshot)
                if snapshot.get("studio", {}).get("can_start"):
                    break
                if time.monotonic() >= deadline:
                    raise AvatarClientError("player_timeout")
                await asyncio.sleep(.5)
            if not self._starting(run_id):
                return
            self.startup_stage = "starting"
            await self.client.start()
            deadline = time.monotonic() + 120
            while self._starting(run_id):
                snapshot = await self.client.snapshot()
                if not self._starting(run_id):
                    return
                session = snapshot.get("session", {})
                self.avatar_state = session.get("state", "starting")
                self.startup_stage = (self.avatar_state if self.avatar_state in {
                    "creating", "connecting", "waiting_stage"} else "starting")
                if self.avatar_state == "ready" or session.get("ready") is True:
                    self.state, self.ready = "running", True
                    self.startup_stage = "ready"
                    wb = self.workbench
                    wb.settings["auto_advance"] = True
                    wb.auto_started = self.started_at
                    wb.clock_key = None
                    wb.launch(wb.game.new_episode)
                    return
                if self.avatar_state in TERMINAL_AVATAR:
                    raise AvatarClientError(self.avatar_state if self.avatar_state in {
                        "creation_unknown", "stop_unconfirmed"} else "startup_failed")
                if time.monotonic() >= deadline:
                    raise AvatarClientError("startup_timeout")
                await asyncio.sleep(.5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, AvatarClientError) else "avatar_prepare_failed"
            self._remember_failure("STARTING", code, exc)
            message = str(exc) if isinstance(exc, AvatarClientError) else "数字人准备未完成，请检查预览连接、模型凭据和会话状态。"
            await self.stop(error=message)

    def _starting(self, run_id):
        # A stop request takes priority even before its cleanup task first runs.
        return (self.state == "starting" and self.run_id == run_id
                and not (self._stop_task and not self._stop_task.done()))

    @staticmethod
    def _error_type(exc):
        name = type(exc).__name__
        return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) else "Exception"

    def _remember_failure(self, stage, code, exc=None):
        if self.last_failure is None:
            self.last_failure = {"stage": stage, "code": code}
            if exc is not None:
                self.last_failure["error_type"] = self._error_type(exc)

    def _metric(self, stage, *, record=None, **kwargs):
        game = self.workbench.game
        metrics = getattr(game, "metrics", None)
        if metrics is not None:
            metrics.record(stage, run_id=self.run_id, session_id=game.data["session_id"],
                           episode_id=game.data["episode_id"], round_id=game.data["round_id"],
                           job_id=(record or {}).get("job_id"), **kwargs)

    def prepare(self, round_id, text):
        if not isinstance(text, str) or not 1 <= len(text) <= 120 or any(c in text for c in "<>[]［］"):
            raise ValueError("invalid_reaction")
        if not self.enabled or not self.ready:
            raise ValueError("invalid_performance_unavailable")
        return {"job_id": round_id + ":reaction", "owner_id": self.run_id,
                "generation": self.client.generation, "status": "pending", "man_released": False,
                "message_motion_plan": copy.deepcopy(MESSAGE_MOTION_PLAN)}

    async def perform(self, record, text, *, on_message_action=None):
        # Set priority before the first await: a just-finished optional prepare
        # cannot race this formal line into the sender.
        self.formal_waiting += 1
        self.ambient.invalidate()
        try:
            await self._release_optional_next()
            receipt = await self.send(record, text)
            self.remember_sent(record, text, receipt, intent="正式选择")
            if "message_motion_plan" in record:
                # Keep formal priority across both jobs. The silent action has
                # its own stable ID, so a lost reply cannot replay the speech.
                motion = {"job_id": record["job_id"] + ":typing", "owner_id": record["owner_id"],
                          "generation": record["generation"], "mode": "motion",
                          "plan": copy.deepcopy(record["message_motion_plan"])}
                motion_receipt = await self.send(motion, "", on_message_action=on_message_action,
                                                 require_short_action=True)
                receipt = dict(receipt, message_motion_receipt=motion_receipt)
            return receipt
        finally:
            self.formal_waiting -= 1

    def reserve_next(self, *, kind, valid, epoch=None, scope=None):
        if self.next is not None or self.sender_failed or not self.enabled:
            return None
        candidate = {"kind": kind, "optional": kind != "formal", "state": "planning",
                     "job_id": "ambient:" + uuid.uuid4().hex,
                     "valid": valid, "epoch": epoch, "scope": scope,
                     "invalidated": False, "record": None, "receipt": None,
                     "text": "", "task": None, "intent": "正式选择" if kind == "formal" else "",
                     "comment_ids": []}
        self.next = candidate
        return candidate

    def candidate_valid(self, candidate):
        return (self.enabled and not self.sender_failed and not candidate["invalidated"]
                and candidate["valid"]()
                and (not candidate["optional"] or not self.formal_waiting))

    def schedule_candidate(self, candidate, coroutine):
        task = candidate["task"] = asyncio.create_task(coroutine)

        def completed(_task):
            # Cancellation before a coroutine's first turn skips its finally.
            # Release only that unsubmitted planning slot, never a replacement.
            if self.next is candidate and candidate["state"] == "planning":
                self.next = None
                if candidate.get("yielded_for_comments"):
                    self.notify_comment()

        task.add_done_callback(completed)
        return task

    def notify_comment(self):
        # One callback per event-loop turn, never an awaited model call in HTTP.
        if self.enabled and self._comment_wakeup is None:
            self._comment_wakeup = asyncio.get_running_loop().call_soon(self._wake_ambient)

    def _wake_ambient(self):
        self._comment_wakeup = None
        if self.enabled:
            self.ambient.tick(self.workbench.state())

    async def _release_optional_next(self):
        while self.next is not None:
            candidate = self.next
            if candidate["optional"]:
                candidate["invalidated"] = True
                task = candidate.get("task")
                if candidate["state"] == "planning" and task and not task.done():
                    task.cancel()
                if task and task is not asyncio.current_task():
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.shield(task)
                else:
                    await asyncio.sleep(.05)
            else:
                await asyncio.sleep(.05)
            if self.sender_failed or not self.enabled:
                raise ValueError("invalid_performance_send")

    def expression_context(self):
        candidate = self.current
        receipt = (candidate or {}).get("receipt") or {}
        current = None
        # Dispatch accepted/queued is not yet an expression the audience saw.
        if candidate and receipt.get("delivery_started"):
            current = self._expression(candidate["record"], candidate["text"],
                                       candidate.get("intent", ""), candidate.get("comment_ids", []))
        return {"recent_performances": copy.deepcopy(self.ambient.history[-6:]),
                "current_performance": current}

    @staticmethod
    def _expression(record, text, intent, comment_ids):
        plan = (record or {}).get("plan") or {}
        part = (plan.get("segments") or [{}])[0]
        return {"job_id": (record or {}).get("job_id"), "text": text, "intent": intent,
                "emotion": plan.get("emotion", "自然"), "comment_ids": list(comment_ids),
                "recovery": copy.deepcopy(part.get("recovery")) if part.get("actions") else None}

    def remember_sent(self, record, text, receipt, *, intent="", comment_ids=()):
        if (receipt.get("status") == "sent" and receipt.get("send_complete") is True
                and not self._pending(receipt)):
            value = self._expression(record, text, intent, comment_ids)
            completed_wait = (receipt.get("post_audio_wait_seconds")
                              if record.get("mode", "speech") == "speech"
                              and receipt.get("handoff_wait_elapsed") is True else None)
            self.ambient.remember(**value, post_audio_wait_seconds=completed_wait)

    @staticmethod
    def _pending(receipt):
        return receipt.get("worker_pending", False) or receipt.get("preparation_pending", False)

    def _check_receipt(self, record, receipt):
        if (receipt.get("generation") != record["generation"] or receipt.get("job_id") != record["job_id"]
                or ("owner_id" in receipt and receipt["owner_id"] != record["owner_id"])):
            raise ValueError("invalid_performance_receipt")

    async def _request_same_job(self, operation, record, *args):
        try:
            return await operation(record, *args)
        except Exception:
            # Lost replies never create a second ID, synthesis or performance.
            return await self.client.job(record)

    async def _follow(self, candidate, receipt, *, preparing=False):
        record = candidate["record"]
        deadline = time.monotonic() + 180
        cancellation_sent, cancellation_attempts = False, 0
        terminal = {"sent", "error", "failed", "cancelled", "canceled", "expired", "skipped"}
        while True:
            self._check_receipt(record, receipt)
            candidate["receipt"] = receipt
            if candidate.get("require_short_action"):
                # Only Studio's successful S receipt for the currently owned
                # job can expose the message. GE/start/another run is not S.
                if (not self.enabled or self.current is not candidate
                        or record["owner_id"] != self.run_id
                        or record["generation"] != self.client.generation
                        or candidate["job_id"] != record["job_id"]):
                    raise ValueError("invalid_performance_owner")
                if receipt.get("short_action_sent") is True and not candidate.get("message_action_notified"):
                    candidate["message_action_notified"] = True
                    callback = candidate.get("on_message_action")
                    if callback is not None:
                        callback()
            status = receipt.get("status")
            if status in terminal and not self._pending(receipt):
                if status == "sent" and receipt.get("send_complete") is True:
                    if candidate.get("require_short_action") and receipt.get("short_action_sent") is not True:
                        raise ValueError("invalid_performance_send")
                    return dict(receipt, playback_confirmed=False)
                if candidate["optional"] and not receipt.get("delivery_started"):
                    return receipt
                raise ValueError("invalid_performance_send")
            if (preparing and status == "prepared" and not self._pending(receipt)
                    and self.candidate_valid(candidate)):
                return receipt
            if time.monotonic() >= deadline or not self.enabled or record["owner_id"] != self.run_id:
                raise ValueError("invalid_performance_send")
            if (not self.candidate_valid(candidate) and not cancellation_sent and status not in terminal):
                if cancellation_attempts >= 2:
                    raise ValueError("invalid_performance_send")
                cancellation_attempts += 1
                receipt = await self._request_same_job(self.client.cancel_job, record)
                cancellation_sent = (receipt.get("cancel_result") in {
                    "cancelled_before_delivery", "already_started", "already_terminal"}
                    or receipt.get("delivery_started") or receipt.get("status") in terminal)
                continue
            await asyncio.sleep(.2)
            receipt = await self.client.job(record)

    async def send(self, record, text, *, optional=False, valid=None, candidate=None,
                   on_message_action=None, require_short_action=False):
        """Prepare outside the one send lock; occupy next before any network I/O."""
        if (not self.enabled or record["owner_id"] != self.run_id
                or record["generation"] != self.client.generation):
            raise ValueError("invalid_performance_owner")
        if candidate is None:
            candidate = self.reserve_next(kind="motion" if optional and record.get("mode") == "motion"
                                          else "proactive" if optional else "formal", valid=valid or (lambda: True))
        if candidate is None or self.next is not candidate:
            raise ValueError("invalid_performance_slot")
        candidate.update(record=record, job_id=record["job_id"], text=text, task=asyncio.current_task(),
                         on_message_action=on_message_action, require_short_action=require_short_action)
        resolved = False
        try:
            if not self.candidate_valid(candidate):
                resolved = True
                return {"status": "skipped"}
            if record.get("mode") != "motion":
                # Legacy motion owns Studio's preparation resources too. Wait
                # for its short tail; speech current still overlaps this TTS.
                while self.current and (self.current.get("record") or {}).get("mode") == "motion":
                    if not self.candidate_valid(candidate):
                        resolved = True
                        return {"status": "skipped"}
                    await asyncio.sleep(.05)
                candidate["state"] = "preparing"
                started = time.monotonic()
                self._metric("avatar_prepare_start", record=record)
                receipt = await self._request_same_job(self.client.prepare, record, text)
                receipt = await self._follow(candidate, receipt, preparing=True)
                if optional:
                    self.ambient.prepared(candidate, receipt)
                self._metric("avatar_prepare_end", record=record, started=started, outcome=receipt.get("status"))
                if receipt.get("status") != "prepared":
                    resolved = True
                    return receipt
                candidate["state"] = "prepared"
            waiting_started = time.monotonic()
            self._metric("sender_wait_start", record=record)
            # A prepared next may be revoked without waiting for current's lock.
            # This lets the formal replacement prepare while current finishes.
            while self.send_lock.locked() or (optional and time.monotonic() < self.ambient.next_dispatch):
                if not self.candidate_valid(candidate):
                    if record.get("mode") != "motion":
                        receipt = await self._request_same_job(self.client.cancel_job, record)
                        receipt = await self._follow(candidate, receipt)
                    else:
                        receipt = {"status": "skipped"}
                    resolved = True
                    return receipt
                await asyncio.sleep(.05)
            if not self.candidate_valid(candidate):
                if record.get("mode") != "motion":
                    receipt = await self._request_same_job(self.client.cancel_job, record)
                    receipt = await self._follow(candidate, receipt)
                else:
                    receipt = {"status": "skipped"}
                resolved = True
                return receipt
            async with self.send_lock:
                self._metric("sender_wait_end", record=record, started=waiting_started)
                if self.sender_failed:
                    raise ValueError("invalid_performance_send")
                self.next = None
                self.current = candidate  # Reserve current before dispatch's first await.
                candidate["state"] = "dispatching"
                started = time.monotonic()
                self._metric("avatar_job_start", record=record)
                operation = self.client.submit if record.get("mode") == "motion" else self.client.dispatch
                args = (text,) if record.get("mode") == "motion" else ()
                receipt = await self._request_same_job(operation, record, *args)
                if record.get("mode") != "motion" and receipt.get("status") == "prepared":
                    # A query proving it is still prepared means the lost
                    # dispatch never started. Retry that ID once, not the text.
                    receipt = await self._request_same_job(operation, record, *args)
                    if receipt.get("status") == "prepared":
                        raise ValueError("invalid_performance_send")
                candidate["state"] = "sending"
                receipt = await self._follow(candidate, receipt)
                self._metric("avatar_job_end", record=record, started=started, outcome=receipt.get("status"))
                resolved = True
                return receipt
        except Exception:
            # Unknown delivery keeps current occupied; the latch prohibits a
            # second voice even after the Python lock has unwound.
            self.sender_failed = True
            candidate["state"] = "unconfirmed"
            raise
        finally:
            if resolved:
                candidate["state"] = "terminal"
                if self.next is candidate:
                    self.next = None
                if self.current is candidate:
                    self.current = None

    async def step(self):
        if not self.active or self.state == "stopping":
            return
        now = time.monotonic()
        if self.page_seen is None or now - self.page_seen > 20:
            await self.stop(error="游戏页面心跳已断开，自动推进和数字人会话已停止。")
            return
        if self.started_at is not None and now - self.started_at >= 20 * 60:
            await self.stop()
            self.workbench.automation_note = "本次自动节目已达跨期累计20分钟上限。"
            return
        try:
            if now - self.last_heartbeat >= 5 and self.client.generation is not None:
                await self.client.heartbeat()
                self.last_heartbeat = now
            if self.state != "running":
                return
            if now - self.last_poll >= 1:
                external = await self.client.external_state()
                self.last_poll = now
                self.avatar_state = external.get("state", "error")
                if (external.get("owner_id") != self.run_id
                        or external.get("generation") != self.client.generation
                        or self.avatar_state in TERMINAL_AVATAR):
                    raise RuntimeError()
            wb = self.workbench
            state = wb.state()
            self.ambient.tick(state)
            if self.ambient.error:
                await self.stop(error=self.ambient.error)
                return
            if state["error"]:
                if self._repair_content(state):
                    return
                await self.stop(error="游戏生成或发送未完成，已停止并保留实际记录。")
                return
            if state["busy"]:
                return
            if state["episode"] and state["episode"]["status"] == "TECH_ENDED":
                await self.stop(error="本期技术结束，自动节目已停止。")
                return
            if not wb.deadline or time.time() < wb.deadline or not state["round"]:
                return
            if state["round"]["phase"] == "DONE":
                changing_cast = state["episode"]["status"] in {"SUCCEEDED", "FAILED"}
                if changing_cast and self.current is not None:
                    # The end-card's 8s is a default dwell, not permission to
                    # swap characters while their already-started line is ending.
                    return
                operation = wb.game.new_episode if changing_cast else wb.game.start_round
            else:
                operation = {"COLLECTING": wb.game.close_collection, "VOTING": wb.game.close_voting}.get(state["round"]["phase"])
            if operation:
                # Public facts and participation deadlines invalidate their own
                # candidates. Merely closing votes must not erase a valid aside.
                self.ambient.invalidate_stale()
                wb.launch(operation)
                wb.deadline = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._remember_failure("RUNNING", "avatar_state_unconfirmed", exc)
            await self.stop(error="数字人连接或发送状态未确认，自动节目已停止。")

    def _repair_content(self, state):
        wb = self.workbench
        error = wb.game.data.get("error") or {}
        stage, code = error.get("stage"), error.get("code")
        if state["busy"] or code not in REPAIRABLE_CONTENT_ERRORS.get(stage, set()):
            return False
        if stage == "WAITING_REPLY":
            performance = (wb.game.current_round or {}).get("performance") or {}
            if performance.get("man_released") is not True:
                return False
        key = (wb.game.data["episode_id"], wb.game.data["round_id"], stage)
        if self.validation_repairs.get(key, 0) >= 1:
            return False
        # This is one correction of an invalid model result, never a transport
        # retry. Game.retry preserves source/dice and passes validation_error;
        # a released reaction skips the sender when only her reply needs repair.
        self.validation_repairs[key] = 1
        wb.launch(wb.game.retry)
        return True

    async def stop(self, *, error=""):
        initiator = asyncio.current_task()
        if self._stop_task is None or self._stop_task.done():
            if not self.active and not error:
                return
            self._stop_error = error
            # Close admission in this turn, before the cleanup task gets its
            # first turn. Already-queued comment/prepare callbacks cannot sneak
            # one last job in between the stop request and cloud cleanup.
            self.state, self.ready, self.sender_failed = "stopping", False, True
            self.ambient.invalidate()
            # Share one cleanup operation. Cancelling an HTTP handler or the
            # scheduler must not cancel the cloud stop that it already started.
            self._stop_task = asyncio.create_task(self._finish_stop(initiator))
        else:
            if error and not self._stop_error:
                self._stop_error = error
            # Cleanup cancels and joins the startup task. If startup failed at
            # the same time, it must leave instead of joining its own joiner.
            if initiator is self.task:
                return
        try:
            await asyncio.shield(self._stop_task)
        except asyncio.CancelledError:
            await asyncio.shield(self._stop_task)
            raise

    async def _cleanup_step(self, stage, operation):
        try:
            result = operation()
            if inspect.isawaitable(result):
                await result
            self.cleanup["steps"][stage] = "complete"
            return True
        except (Exception, asyncio.CancelledError) as exc:
            self.cleanup["steps"][stage] = "failed"
            self.cleanup["errors"].append({"stage": stage, "error_type": self._error_type(exc)})
            return False

    async def _finish_stop(self, initiator):
        self.stopping = True
        self.state, self.ready = "stopping", False
        self.sender_failed = True
        self.cleanup = {"cloud_stop_confirmed": None, "errors": [], "steps": {}}
        started = time.monotonic()
        self._metric("program_stop_start")
        try:
            # abort clears the engine's transient error. Keep only its safe
            # diagnostic identifiers so the operator can inspect why we stopped.
            failure = self.workbench.game.data.get("error")
            if self._stop_error and failure and self.last_failure is None:
                self.last_failure = {key: failure.get(key) for key in ("code", "stage")}
            # The cloud stop starts before joining slow model/TTS tasks. Closing
            # admission is immediate; cancellation alone does not stop billing.
            async def stop_cloud():
                await self.client.stop()
                if self.client.generation is not None:
                    deadline = time.monotonic() + 30
                    while True:
                        external = await self.client.external_state()
                        self.avatar_state = external.get("state", "error")
                        if self.avatar_state == "ended":
                            break
                        if self.avatar_state in TERMINAL_AVATAR or time.monotonic() >= deadline:
                            raise RuntimeError()
                        await asyncio.sleep(.5)

            cloud_task = None
            if self.client.owner_id is not None and self.client.generation is not None:
                cloud_task = asyncio.create_task(self._cleanup_step("cloud_stop", stop_cloud))
            else:
                self.cleanup["steps"]["cloud_stop"] = "not_applicable"
            await self._cleanup_step("ambient", self.ambient.close)

            async def cancel_start():
                if self.task and self.task is not initiator and not self.task.done():
                    self.task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self.task

            await self._cleanup_step("startup_task", cancel_start)
            await self._cleanup_step("game_abort", self.workbench.abort)

            cloud_confirmed = await cloud_task if cloud_task is not None else None
            self.cleanup["cloud_stop_confirmed"] = cloud_confirmed
            # Keep the initiating failure first. Cleanup outcomes have their own
            # safe fields, so a second failure cannot erase the original cause.
            self.error = self._stop_error or ("数字人停止请求未确认，请到数字人工作台检查云会话。" if cloud_confirmed is False
                                   else "数字人已停止，但部分本地清理失败，请检查诊断。" if self.cleanup["errors"] else "")
            self.state = "error" if self.error else "stopped"
            self.ended_at = time.monotonic()
        finally:
            self.stopping = False
            self._metric("program_stop_end", started=started,
                         outcome="error" if self.cleanup["errors"] else "complete")

    async def close(self):
        try:
            if self.active or (self._stop_task and not self._stop_task.done()):
                await self.stop()
        finally:
            if not await self._cleanup_step("http_close", self.client.close):
                raise ValueError("cleanup_failed")
