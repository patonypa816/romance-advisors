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
from .casual import CasualPerformance
from .appearance import appearance_snapshot

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
    APPEARANCE_TIMEOUT_SECONDS = 45.0

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
        self.casual = CasualPerformance(self)
        self.content_mode = "game"
        self.broadcast_mode = False
        self.mode_epoch = 0
        self._mode_task = None
        self._game_baseline_episode_id = None
        # Real appearance control additionally requires an explicit Workbench
        # opt-in and a fully validated Studio configuration.
        self.confirmed_appearance_key = "normal"
        self._appearance_target = "normal"
        self._appearance_episode_id = None
        self._appearance_task = None
        self._appearance_busy = False
        self._appearance_receipt = None
        self._appearance_real = False
        self._appearance_active = False
        self._retained_appearance_episode = None
        self._appearance_context = None
        self._appearance_remote = {}
        self._appearance_message = "appearance_disabled"
        self._appearance_sequence = 0

    @property
    def active(self):
        return self.state in ACTIVE

    @property
    def enabled(self):
        return self.state == "running"

    @property
    def game_enabled(self):
        return self.enabled and self.content_mode == "game"

    @property
    def game_episode_id(self):
        episode = self.workbench.game.episode or {}
        episode_id = episode.get("id")
        return episode_id if self.game_enabled and episode_id != self._game_baseline_episode_id else None

    @property
    def switchable(self):
        return (self.enabled and self.ready and not self.sender_failed
                and self.content_mode in {"chat", "game"})

    @property
    def player_url(self):
        return self._player_url if self.active else ""

    @property
    def simulated_appearance_enabled(self):
        return getattr(self.client, "supports_simulated_appearance", False) is True

    @property
    def real_appearance_enabled(self):
        return self._appearance_real

    def appearance_status(self):
        receipt = self._appearance_receipt or {}
        pending = receipt if receipt.get("status") in {"written", "failed"} else None
        return {"enabled": self._appearance_real or self.simulated_appearance_enabled,
                "ready": bool(self._appearance_active and not self.sender_failed and not self._appearance_busy),
                "active": self._appearance_active, "message": self._appearance_message,
                "confirmed_key": self.confirmed_appearance_key if (self._appearance_real or self.simulated_appearance_enabled) else None,
                "pending_confirmation": copy.deepcopy(pending), "receipt": copy.deepcopy(self._appearance_receipt),
                "context": copy.deepcopy(self._appearance_context),
                "version": self._appearance_remote.get("version", 0)}

    def _appearance_hook(self, name, *args, **kwargs):
        hook = getattr(self.workbench, name, None)
        if hook is not None:
            return hook(*args, **kwargs)

    async def _configure_appearance(self):
        requested = self._appearance_hook("appearance_requested")
        if not requested or getattr(self.client, "supports_real_appearance", False) is not True:
            return
        remote = await self.client.appearance_status()
        self._appearance_remote = remote
        self._appearance_message = remote.get("message", "appearance_not_ready")
        # Explicit opt-in must pass before paid start. Turning the effect off
        # retains the original game path without any appearance requests.
        self._appearance_real = remote.get("ready") is True and remote.get("enabled") is True
        if not self._appearance_real:
            raise AvatarClientError("appearance_not_ready")
        if self._appearance_real:
            self.confirmed_appearance_key = self._appearance_target = remote["confirmed_key"]

    async def begin_appearance_episode(self, episode_id):
        if not self._appearance_real:
            return self.appearance_status()
        if self._appearance_context:
            await self.end_appearance_episode(restore=True)
        self._appearance_sequence = max(self._appearance_sequence + 1, self.mode_epoch)
        self._appearance_context = {"owner_id": self.run_id, "generation": self.client.generation,
                                    "mode_epoch": self._appearance_sequence, "episode_id": episode_id}
        self._appearance_episode_id = episode_id
        self._appearance_receipt = None
        # Close optional admission before binding yields to HTTP. The freshly
        # COLLECTING episode must not race an ambient preparation into Studio.
        self._appearance_busy = True
        await self._release_optional_next()
        while self.current is not None:
            if not self.enabled or self.sender_failed:
                raise ValueError("appearance_start_slot_unconfirmed")
            await asyncio.sleep(.05)
        remote = await self.client.appearance_lifecycle(self._appearance_context, "bind")
        self._appearance_remote = remote
        self.confirmed_appearance_key = remote["confirmed_key"]
        self._appearance_target = "normal"
        self._appearance_message = "appearance_initializing"
        self._appearance_busy = True
        await self._release_optional_next()
        # Open scoring only after Studio adopts the normal binding. This is
        # scheduling evidence; actual visual identity still needs live review.
        self._appearance_task = asyncio.create_task(self._switch_real_target("normal", purpose="game"))
        await self.wait_for_appearance()
        if (self.sender_failed or not self.game_enabled or self.game_episode_id != episode_id
                or self.confirmed_appearance_key != "normal"
                or (self._appearance_receipt or {}).get("status") == "retained"):
            self._appearance_message = "appearance_paused"
            return self.appearance_status()
        self._appearance_active = True
        self._appearance_message = ""
        self._appearance_hook("appearance_episode_started", episode_id)
        return self.appearance_status()

    async def end_appearance_episode(self, *, restore=True):
        context = self._appearance_context
        self._appearance_active = False
        if context:
            self._appearance_hook("appearance_episode_ended", context["episode_id"])
        if not self._appearance_real or not context:
            return
        # Keep the consumed in-flight request queryable. A mode handoff cannot
        # cancel an uncertain write and then resume talking with an assumed face.
        await self.client.appearance_lifecycle(context, "release")
        await self.wait_for_appearance()
        if restore and self.enabled and not self.sender_failed:
            self._appearance_busy = True
            await self._release_optional_next()
            self._appearance_task = asyncio.create_task(self._switch_real_target(self._appearance_remote["broadcast_key"], purpose="restore"))
            await self.wait_for_appearance()
            if self.confirmed_appearance_key != self._appearance_remote["broadcast_key"]:
                self.sender_failed = True
                self._appearance_message = "appearance_restore_failed"
                raise ValueError("appearance_restore_failed")
        if not self.sender_failed:
            self._appearance_context = None
            self._appearance_episode_id = None
            self._appearance_target = self.confirmed_appearance_key

    async def _new_episode(self):
        if self._appearance_context:
            await self.end_appearance_episode(restore=True)
        if self._appearance_real:
            self._appearance_busy = True
            await self._release_optional_next()
            while self.current is not None:
                if not self.enabled or self.sender_failed:
                    raise ValueError("appearance_start_slot_unconfirmed")
                await asyncio.sleep(.05)
        result = await self.workbench.game.new_episode()
        episode_id = (self.workbench.game.episode or {}).get("id")
        if episode_id and self.game_enabled:
            await self.begin_appearance_episode(episode_id)
        return result

    async def confirm_appearance(self, request_id, *, outcome="target_dynamic"):
        record = self._appearance_receipt
        if (not self._appearance_real or not record or record.get("request_id") != request_id
                or record.get("status") not in {"written", "failed"}):
            raise ValueError("appearance_confirmation_invalid")
        result = await self.client.confirm_appearance(record, outcome=outcome)
        self._appearance_receipt = result
        self._appearance_hook("appearance_operation", **result)
        return self.appearance_status()

    async def _switch_real_target(self, key, *, purpose):
        if self.confirmed_appearance_key == key:
            self._appearance_busy = False
            return
        # Stop admission before waiting for the sending slot. Formal pairs that
        # reserved first retain their priority across the speech-to-typing gap.
        self._appearance_busy = True
        context = copy.deepcopy(self._appearance_context)
        record = {**context, "request_id": "appearance_" + uuid.uuid4().hex,
                  "target_key": key, "purpose": purpose}
        resolved = False
        try:
            async with self.send_lock:
                if not self.enabled or self.sender_failed or context != self._appearance_context:
                    return
                self._appearance_hook("appearance_operation", **record, status="intent")
                # Start the bound only after obtaining our sending slot. A long
                # protected speech/typing pair is allowed to finish first.
                deadline = time.monotonic() + self.APPEARANCE_TIMEOUT_SECONDS
                try:
                    receipt = await self.client.switch_appearance(record)
                except Exception:
                    receipt = await self.client.appearance(record)
                while True:
                    if (not isinstance(receipt, dict) or receipt.get("simulated") is not False
                            or any(receipt.get(k) != v for k, v in record.items())):
                        raise ValueError("appearance_receipt_invalid")
                    if receipt != self._appearance_receipt:
                        self._appearance_hook("appearance_operation", **receipt)
                    self._appearance_receipt = copy.deepcopy(receipt)
                    if receipt.get("status") == "applied" and (
                            receipt.get("program_adopted") is not True
                            or receipt.get("confirmed_key") != key
                            or not (receipt.get("sent") is True or receipt.get("unchanged") is True)
                            or receipt.get("operator_confirmed") is not False
                            or receipt.get("playback_confirmed") is not False
                            or receipt.get("visual_confirmed") is not False
                            or receipt.get("success_acknowledged") is not False
                            or receipt.get("evidence") not in {"transport_guard_healthy_receiver", "existing_asset_binding"}
                            or receipt.get("evidence") == "transport_guard_healthy_receiver"
                               and receipt.get("receiver_progress_observed") is not True):
                        raise ValueError("appearance_adoption_invalid")
                    if receipt.get("status") in {"applied", "confirmed", "retained"}:
                        adopted_key = receipt.get("confirmed_key", key)
                        remote = await self.client.appearance_status()
                        if (remote.get("ready") is not True or remote.get("blocked")
                                or remote.get("confirmed_key") != adopted_key):
                            raise ValueError("appearance_binding_unconfirmed")
                        self.confirmed_appearance_key = adopted_key
                        self._appearance_remote = remote
                        resolved = True
                        if receipt["status"] == "retained":
                            # Explicit failed/original-dynamic resolution pauses
                            # this episode's effect; it must not auto-retry target.
                            if self._appearance_active and purpose == "game":
                                self._retained_appearance_episode = context["episode_id"]
                            self._appearance_message = "appearance_paused"
                            self._appearance_active = False
                            self._appearance_hook("appearance_episode_ended", context["episode_id"])
                            self._appearance_target = self.confirmed_appearance_key
                        break
                    if receipt.get("status") == "cancelled" and not receipt.get("sent"):
                        resolved = True
                        break
                    if receipt.get("status") not in {"pending", "written"}:
                        raise ValueError("appearance_unconfirmed")
                    # Studio resolves its bounded automatic admission. Polling
                    # never re-sends Type16 and the deadline is failure only.
                    if time.monotonic() >= deadline:
                        raise ValueError("appearance_progress_timeout")
                    if not self.enabled or context != self._appearance_context:
                        raise ValueError("appearance_owner_changed")
                    await asyncio.sleep(.1)
                    receipt = await self.client.appearance(record)
        except (Exception, asyncio.CancelledError) as exc:
            self.sender_failed = True
            self._appearance_message = "appearance_unconfirmed"
            self._remember_failure("APPEARANCE", "appearance_unconfirmed", exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            if resolved:
                self._appearance_busy = False

    async def _switch_real_appearance(self, run_id, epoch, episode_id):
        while (self._appearance_active and self.game_enabled and self.run_id == run_id
               and self.mode_epoch == epoch and self.game_episode_id == episode_id
               and not self.sender_failed and self._appearance_target != self.confirmed_appearance_key):
            while self.formal_waiting or self.current is not None:
                if not self._appearance_active or not self.game_enabled or self.sender_failed:
                    return
                await asyncio.sleep(.05)
            await self._release_optional_next()
            if not self._appearance_active or not self.game_enabled or self.sender_failed:
                return
            if self.formal_waiting or self.current is not None or self.next is not None:
                continue
            await self._switch_real_target(self._appearance_target, purpose="game")

    def message_appearance(self):
        """Freeze the last adopted binding on messages; this is not visual proof."""
        episode_id = self.game_episode_id
        if self._appearance_real:
            if (episode_id and episode_id == self._appearance_episode_id and not self.sender_failed
                    and not self._appearance_busy and (self._appearance_active or self._retained_appearance_episode == episode_id)):
                return copy.deepcopy(self._appearance_remote.get("portrait"))
            return None
        if (not self.simulated_appearance_enabled or not episode_id or self.sender_failed or self._appearance_busy
                or self._appearance_episode_id not in {None, episode_id}):
            return None
        self._appearance_episode_id = episode_id
        return appearance_snapshot(self.confirmed_appearance_key)

    def request_appearance(self, key):
        """Merge targets in memory; this never enables a real Studio request."""
        if self._appearance_real:
            if key not in {"fish", "rough", "tired", "normal", "refined", "stellar"}:
                raise ValueError("invalid_appearance")
            episode_id = self.game_episode_id
            if (not self._appearance_active or episode_id != self._appearance_episode_id
                    or not self.ready or self.sender_failed
                    or (self.workbench.game.episode or {}).get("status") != "ACTIVE"):
                raise ValueError("appearance_unavailable")
            self._appearance_target = key
            if self._appearance_task is None or self._appearance_task.done():
                self._appearance_task = asyncio.create_task(self._switch_real_appearance(self.run_id, self.mode_epoch, episode_id))
            return
        if not isinstance(key, str) or key not in {"normal", "refined"}:
            raise ValueError("invalid_simulated_appearance")
        episode_id = self.game_episode_id
        if (not self.simulated_appearance_enabled or not episode_id or not self.ready
                or (self.workbench.game.episode or {}).get("status") != "ACTIVE"
                or self.sender_failed or self._appearance_episode_id not in {None, episode_id}):
            raise ValueError("simulated_appearance_unavailable")
        self._appearance_episode_id = episode_id
        self._appearance_target = key
        if self._appearance_task is None or self._appearance_task.done():
            self._appearance_task = asyncio.create_task(self._switch_simulated_appearance(
                self.run_id, self.mode_epoch, episode_id))

    async def wait_for_appearance(self):
        """Join the existing worker; waiting/querying never sends another ID."""
        task = self._appearance_task
        if task is not None:
            await asyncio.shield(task)
        return copy.deepcopy(self._appearance_receipt)

    async def _switch_simulated_appearance(self, run_id, epoch, episode_id):
        def owned():
            return (self.game_enabled and not self.sender_failed and self.run_id == run_id
                    and self.mode_epoch == epoch and self.game_episode_id == episode_id
                    and (self.workbench.game.episode or {}).get("status") == "ACTIVE")

        while owned() and self._appearance_target != self.confirmed_appearance_key:
            # formal_waiting spans speech, its drain, the gap, and typing Q.
            # Cancel only an optional next; a current sender always finishes.
            while owned() and (self.formal_waiting or self.current is not None):
                await asyncio.sleep(.05)
            if not owned():
                return
            await self._release_optional_next()
            if not owned():
                return
            if self.formal_waiting or self.current is not None or self.next is not None:
                continue
            if self._appearance_target == self.confirmed_appearance_key:
                return
            # No await between the final safety check and closing admission.
            self._appearance_busy = True
            record = {"request_id": "appearance_" + uuid.uuid4().hex, "owner_id": run_id,
                      "generation": self.client.generation, "mode_epoch": epoch,
                      "episode_id": episode_id, "target_key": self._appearance_target}
            resolved = False
            try:
                async with self.send_lock:
                    # Exactly one write. A lost response only queries this ID.
                    try:
                        receipt = await self.client.switch_appearance(record)
                    except Exception:
                        receipt = await self.client.appearance(record)
                    deadline = time.monotonic() + 30
                    while True:
                        if (not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in record.items())
                                or receipt.get("simulated") is not True):
                            raise ValueError("invalid_simulated_appearance_receipt")
                        self._appearance_receipt = copy.deepcopy(receipt)
                        if not owned():
                            raise ValueError("simulated_appearance_owner_changed")
                        if receipt.get("status") == "confirmed":
                            self.confirmed_appearance_key = record["target_key"]
                            resolved = True
                            break
                        if (receipt.get("status") not in {"pending", "written"}
                                or time.monotonic() >= deadline):
                            raise ValueError("simulated_appearance_unconfirmed")
                        await asyncio.sleep(.05)
                        receipt = await self.client.appearance(record)
            except (Exception, asyncio.CancelledError) as exc:
                # Failure/unknown is never evidence of either face. Keep the
                # sender latched, including when the caller cancels its wait.
                self.sender_failed = True
                status = (self._appearance_receipt or {}).get("status")
                self._appearance_receipt = dict(record, status="failed" if status == "failed" else "unconfirmed",
                                                simulated=True)
                self._remember_failure("APPEARANCE", "simulated_appearance_unconfirmed", exc)
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return
            finally:
                if resolved:
                    self._appearance_busy = False
            # A newly reserved formal pair gets priority over another switch.

    def public(self, *, operator=False):
        now = self.ended_at or time.monotonic()
        value = {"state": self.state, "ready": self.ready, "run_id": self.run_id,
                 "switchable": self.switchable, "content_mode": self.content_mode,
                 "broadcast_mode": self.broadcast_mode,
                 "game_episode_id": self.game_episode_id,
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

    def start(self, mode="game"):
        wb = self.workbench
        if not isinstance(mode, str) or mode not in {"chat", "game"}:
            raise ValueError("invalid_program_mode")
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
        self.casual.reset()
        self.content_mode = mode
        self.broadcast_mode = mode == "chat"
        self.mode_epoch += 1
        self._game_baseline_episode_id = (wb.game.episode or {}).get("id")
        self._mode_task = None
        self.sender_failed = False
        self.current = self.next = None
        self.confirmed_appearance_key = self._appearance_target = "normal"
        self._appearance_episode_id = None
        self._appearance_task = None
        self._appearance_busy = False
        self._appearance_receipt = None
        self._appearance_real = False
        self._appearance_active = False
        self._appearance_context = None
        self._appearance_remote = {}
        self._appearance_message = "appearance_disabled"
        self._appearance_sequence = 0
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
            await self._configure_appearance()
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
                    wb.auto_started = self.started_at
                    wb.clock_key = None
                    if self.content_mode == "game":
                        wb.settings["auto_advance"] = True
                        wb.launch(self._new_episode)
                    else:
                        # An old restored episode has no participation rights in
                        # the newly started chat broadcast.
                        wb.game.abort_episode()
                        self.notify_comment()
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

    def set_mode(self, run_id, mode):
        """Close admission synchronously; a private worker owns the handoff.

        The HTTP request never owns cleanup, so a disconnected browser cannot
        cancel an uncertain Studio job and silently reuse its sender slot.
        """
        if not self.enabled or run_id != self.run_id:
            raise ValueError("invalid_program_run")
        if not isinstance(mode, str) or mode not in {"chat", "game"}:
            raise ValueError("invalid_program_mode")
        # Visiting ordinary chat opts this run into explicit, one-episode game
        # sessions. Legacy direct game runs keep their existing cast rollover.
        if mode == "chat":
            self.broadcast_mode = True
        if self.content_mode in {mode, "to_" + mode}:
            return
        if not self.switchable:
            raise ValueError("operation_in_progress")
        self._appearance_active = False
        if self._appearance_context:
            self._appearance_hook("appearance_episode_ended", self._appearance_context["episode_id"])
        self.content_mode = "to_" + mode
        self.mode_epoch += 1
        self.ambient.invalidate()
        self.casual.invalidate()
        wb = self.workbench
        wb.settings["auto_advance"] = False
        wb.clock_key = wb.deadline = None
        # Revoke game model/scoring/typing write authority now, even when a
        # model ignores cancellation or an already-started sentence is ending.
        try:
            wb.game.abort_episode()
        except Exception as exc:
            self.sender_failed = True
            self._remember_failure("MODE", "game_abort_failed", exc)
            self._mode_task = asyncio.create_task(self.stop(error="游戏结束状态未确认，直播已停止。"))
            raise ValueError("invalid_program_mode") from None
        self._mode_task = asyncio.create_task(self._finish_mode(self.run_id, self.mode_epoch, mode))

    async def _finish_mode(self, run_id, epoch, mode):
        try:
            wb = self.workbench
            candidates = [c for c in (self.current, self.next) if c]
            tasks = {c.get("task") for c in candidates if c.get("task")}
            tasks.discard(asyncio.current_task())
            for candidate in candidates:
                candidate["invalidated"] = True
                task = candidate.get("task")
                # Before send() has made network I/O it is safe to cancel only
                # model planning. Preparing/dispatching jobs resolve by ID.
                if candidate["state"] == "planning" and task and not task.done():
                    task.cancel()
            # Await a sentence's complete send/handoff, not just dispatch's
            # acceptance. Shield protects the transaction from worker teardown.
            async with asyncio.timeout(190):
                if wb.job and wb.job not in tasks:
                    await wb._cancel_task(wb.job)
                if tasks:
                    results = await asyncio.shield(asyncio.gather(*tasks, return_exceptions=True))
                    if any(isinstance(result, Exception) for result in results):
                        raise ValueError("invalid_performance_send")
                while self.current is not None or self.next is not None:
                    if self.sender_failed:
                        raise ValueError("invalid_performance_send")
                    await asyncio.sleep(.05)
            if wb.job:
                await wb._cancel_task(wb.job)
            wb.job = None
            if not self.enabled or self.run_id != run_id or self.mode_epoch != epoch:
                return
            if self.sender_failed:
                raise ValueError("invalid_performance_send")
            wb.error = None
            await self.end_appearance_episode(restore=True)
            if self.sender_failed:
                raise ValueError("appearance_handoff_unconfirmed")
            self.ambient.reset()
            self.casual.reset()
            self.content_mode = mode
            if mode == "game":
                self._game_baseline_episode_id = (wb.game.episode or {}).get("id")
                wb.settings["auto_advance"] = True
                wb.launch(self._new_episode)
            self.notify_comment()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._remember_failure("MODE", "mode_handoff_unconfirmed", exc)
            await self.stop(error="模式切换的发送状态未确认，直播已停止。")

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
        if not self.game_enabled or not self.ready:
            raise ValueError("invalid_performance_unavailable")
        return {"job_id": round_id + ":reaction", "owner_id": self.run_id,
                "mode_epoch": self.mode_epoch,
                "generation": self.client.generation, "status": "pending", "man_released": False,
                "message_motion_plan": copy.deepcopy(MESSAGE_MOTION_PLAN)}

    async def perform(self, record, text, *, on_message_action=None):
        if not self.game_enabled or record.get("mode_epoch", self.mode_epoch) != self.mode_epoch:
            raise ValueError("invalid_performance_unavailable")
        # Set priority before the first await: a just-finished optional prepare
        # cannot race this formal line into the sender.
        self.formal_waiting += 1
        self.ambient.invalidate()
        completed = False
        try:
            await self._release_optional_next()
            receipt = await self.send(record, text)
            self.remember_sent(record, text, receipt, intent="正式选择")
            if not self.game_enabled or record.get("mode_epoch", self.mode_epoch) != self.mode_epoch:
                return receipt
            if "message_motion_plan" in record:
                # Keep formal priority across both jobs. The silent action has
                # its own stable ID, so a lost reply cannot replay the speech.
                motion = {"job_id": record["job_id"] + ":typing", "owner_id": record["owner_id"],
                          "generation": record["generation"], "mode": "motion",
                          "mode_epoch": self.mode_epoch,
                          "plan": copy.deepcopy(record["message_motion_plan"])}
                # Typing is a required second half, not an optional idle move.
                # Keep formal ownership while the preceding Q cooldown clears.
                deadline = time.monotonic() + 30
                while receipt.get("motion_ready_in_seconds", 0) > 0:
                    if (not self.game_enabled or record.get("mode_epoch", self.mode_epoch) != self.mode_epoch
                            or self.sender_failed or time.monotonic() >= deadline):
                        raise ValueError("typing_slot_unconfirmed")
                    await asyncio.sleep(min(.1, receipt["motion_ready_in_seconds"]))
                    receipt = await self.client.job(record)
                    self._check_receipt(record, receipt)
                motion_receipt = await self.send(motion, "", on_message_action=on_message_action,
                                                 require_short_action=True)
                receipt = dict(receipt, message_motion_receipt=motion_receipt)
            completed = True
            return receipt
        finally:
            self.formal_waiting -= 1
            if completed and self._appearance_task is not None:
                # Release the entire pair only after typing Q/worker completion.
                # A pending face can then settle before Game generates her reply.
                await self.wait_for_appearance()
                if self.sender_failed:
                    raise ValueError("simulated_appearance_unconfirmed")

    def reserve_next(self, *, kind, valid, epoch=None, scope=None):
        if (self.next is not None or self.sender_failed or not self.enabled
                or self.content_mode not in {"chat", "game"} or getattr(self, "_appearance_busy", False)
                or (kind != "formal" and getattr(self, "_appearance_target", "normal")
                    != getattr(self, "confirmed_appearance_key", "normal"))):
            return None
        candidate = {"kind": kind, "optional": kind != "formal", "state": "planning",
                     "job_id": "ambient:" + uuid.uuid4().hex,
                     "valid": valid, "epoch": epoch, "scope": scope,
                     "invalidated": False, "record": None, "receipt": None,
                     "text": "", "task": None, "intent": "正式选择" if kind == "formal" else "",
                     "comment_ids": []}
        candidate["mode_epoch"] = self.mode_epoch
        self.next = candidate
        return candidate

    def candidate_valid(self, candidate):
        return (self.enabled and self.content_mode in {"chat", "game"}
                and candidate.get("mode_epoch", self.mode_epoch) == self.mode_epoch
                and not self.sender_failed and not candidate["invalidated"]
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
        if self.game_enabled:
            self.ambient.tick(self.workbench.state())
        elif self.enabled and self.content_mode == "chat":
            self.casual.tick(self.workbench.state())

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
                if (self.game_enabled and candidate.get("mode_epoch", self.mode_epoch) == self.mode_epoch
                        and receipt.get("short_action_sent") is True and not candidate.get("message_action_notified")):
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
                if ((candidate["optional"] or candidate.get("mode_epoch", self.mode_epoch) != self.mode_epoch)
                        and not receipt.get("delivery_started")):
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
        if (not self.enabled or self.content_mode not in {"chat", "game"}
                or record.get("mode_epoch", self.mode_epoch) != self.mode_epoch
                or record["owner_id"] != self.run_id
                or record["generation"] != self.client.generation):
            raise ValueError("invalid_performance_owner")
        while getattr(self, "_appearance_busy", False) and not self.sender_failed:
            await asyncio.sleep(.05)
            if not self.enabled or record.get("mode_epoch", self.mode_epoch) != self.mode_epoch:
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
                    if not self.candidate_valid(candidate):
                        receipt = await self._request_same_job(self.client.cancel_job, record)
                    else:
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
        except asyncio.CancelledError:
            # Cancellation is not a provider receipt. Once I/O started, leave
            # the slot latched until full cloud shutdown confirms the outcome.
            if candidate["state"] != "planning":
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
            if self.sender_failed:
                await self.stop(error="数字人发送状态未确认，直播已停止。")
                return
            if self.content_mode not in {"chat", "game"}:
                return
            if self.content_mode == "chat":
                self.casual.tick(state)
                if self.casual.error or self.sender_failed:
                    await self.stop(error=self.casual.error or "聊天发送状态未确认，直播已停止。")
                return
            self.ambient.tick(state)
            if self.ambient.error:
                await self.stop(error=self.ambient.error)
                return
            if state["error"]:
                if self._repair_content(state):
                    return
                await self.stop(error="游戏生成或发送未完成，已停止并保留实际记录。")
                return
            if (self._appearance_active and state.get("episode")
                    and state["episode"].get("status") != "ACTIVE"):
                self._appearance_active = False
                self._appearance_hook("appearance_episode_ended", state["episode"]["id"])
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
                if changing_cast and self.broadcast_mode:
                    # The committed finale clock supplies the existing 8s
                    # dwell. Its victory/defeat record survives the handoff.
                    self.set_mode(self.run_id, "chat")
                    return
                operation = self._new_episode if changing_cast else wb.game.start_round
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
            self._appearance_active = False
            if self._appearance_context:
                self._appearance_hook("appearance_episode_ended", self._appearance_context["episode_id"])
            self.state, self.ready, self.sender_failed = "stopping", False, True
            self.mode_epoch += 1
            self.ambient.invalidate()
            self.casual.invalidate()
            # Share one cleanup operation. Cancelling an HTTP handler or the
            # scheduler must not cancel the cloud stop that it already started.
            self._stop_task = asyncio.create_task(self._finish_stop(initiator))
        else:
            if error and not self._stop_error:
                self._stop_error = error
            # Cleanup cancels and joins the startup task. If startup failed at
            # the same time, it must leave instead of joining its own joiner.
            if initiator is self.task or initiator is self._mode_task:
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
            await self._cleanup_step("casual", self.casual.close)

            async def cancel_appearance():
                task = self._appearance_task
                if task and task is not initiator and not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

            await self._cleanup_step("appearance_task", cancel_appearance)

            async def cancel_mode():
                if self._mode_task and self._mode_task is not initiator and not self._mode_task.done():
                    self._mode_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._mode_task

            await self._cleanup_step("mode_task", cancel_mode)

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
