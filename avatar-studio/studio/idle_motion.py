"""Low priority, local scheduling only; successful writes do not prove rendering."""
from __future__ import annotations

import asyncio
import copy
from collections import deque
import inspect
import math
import random
import re
import time
import uuid

from component_live import _Stopped


class IdleRecoveryError(RuntimeError):
    """Blocked before content starts; leave the connection available for manual Q."""


class IdleMotionScheduler:
    POLL_SECONDS = .05
    I_GUARD_SECONDS = 1.1
    Q_GUARD_SECONDS = 1.0
    WRITE_TIMEOUT = 5.0

    def __init__(self, component):
        self.c = component
        self.enabled = False
        self.task = None
        self.epoch = 0
        self.status = "off"
        self.error = ""
        self.needs_recovery = False
        self.recovery_failed = False
        self.current = None
        self.hold_until = self.not_before = self.next_tick = 0.0
        self.last_id = None
        self.cooldowns = {}
        self.events = deque(maxlen=64)
        self.i_sent = self.q_sent = 0
        self.actions = []
        self.settings = {}
        self.allowed = lambda: False
        self.interrupt = lambda: False
        self.valid = lambda: True
        self.once = None

    @staticmethod
    def callback(callback):
        value = callback()
        if inspect.isawaitable(value):
            if inspect.iscoroutine(value):
                value.close()
            raise ValueError("待机检查必须同步返回布尔值")
        if type(value) is not bool:
            raise ValueError("待机检查必须同步返回布尔值")
        return value

    @staticmethod
    def number(value, low, high=86400):
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError("待机时间或权重不符合范围")
        return float(value)

    @classmethod
    def validate(cls, actions, settings):
        if not isinstance(actions, list) or not actions:
            raise ValueError("请先选择至少一个待机动作")
        result, ids = [], set()
        for source in actions:
            if not isinstance(source, dict):
                raise ValueError("待机动作格式不正确")
            item = copy.deepcopy(source)
            for field in ("id", "name", "instruction", "recovery_instruction"):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    raise ValueError("待机动作必须包含完整动作和回正指令")
            if item["id"] in ids:
                raise ValueError("待机动作不能重复")
            ids.add(item["id"])
            if (not isinstance(item.get("description_sha256"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["description_sha256"])):
                raise ValueError("待机动作缺少描述摘要")
            item["hold_seconds"] = cls.number(item.get("hold_seconds"), 2, 4)
            item["cooldown_seconds"] = cls.number(item.get("cooldown_seconds"), 0)
            item["weight"] = cls.number(item.get("weight", 1), .000001)
            result.append(item)
        if not isinstance(settings, dict):
            raise ValueError("待机间隔格式不正确")
        low = cls.number(settings.get("min_interval_seconds", 15), .001)
        high = cls.number(settings.get("max_interval_seconds", 25), low)
        return result, {"min_interval_seconds": low, "max_interval_seconds": high}

    def alive(self):
        c = self.c
        return (not c._stop.is_set() and not c._closing and c.state not in c.TERMINAL
                and self.callback(self.valid))

    def can_start(self, epoch, *, owns_speech=False):
        c = self.c
        return (self.enabled and self.epoch == epoch and self.alive()
                and c.state == "ready" and c._ready.is_set() and c._joined.is_set()
                and not c._pending_speech and not c._motion_control_pending
                and (owns_speech or not c._speech_lock.locked())
                and self.callback(self.allowed) and not self.callback(self.interrupt))

    def public(self):
        return {"enabled": self.enabled, "status": self.status,
                "task_running": bool(self.task and not self.task.done()),
                "needs_recovery": self.needs_recovery, "recovery_failed": self.recovery_failed,
                "last_error": self.error, "current_action_id": self.current["id"] if self.current else None,
                "last_action_id": self.last_id, "i_sent": self.i_sent, "q_sent": self.q_sent,
                "next_tick_at_monotonic": self.next_tick or None,
                "seconds_until_next": max(0.0, self.next_tick - time.monotonic()) if self.next_tick else None,
                "hold_until_monotonic": self.hold_until or None,
                "cooldowns": dict(self.cooldowns), "settings": dict(self.settings),
                "action_ids": [a["id"] for a in self.actions], "once": self.once,
                "recent": [dict(row) for row in self.events], "visual_effect_confirmed": False}

    def start(self, actions, settings, idle_allowed, interrupt_requested, valid_session=None, *, once=None):
        actions, settings = self.validate(actions, settings)
        for check in (idle_allowed, interrupt_requested,
                      (lambda: True) if valid_session is None else valid_session):
            if not callable(check) or inspect.iscoroutinefunction(check):
                raise ValueError("待机需要同步空闲、打断和场次检查")
            self.callback(check)
        c = self.c
        if (c.motion_mode != "normal" or c._stop.is_set() or c._closing
                or c.state not in {"ready", "sending", "draining"}
                or not c._ready.is_set() or not c._joined.is_set()):
            raise ValueError("待机只可在正常且已就绪的当前场次启用")
        if self.enabled or (self.task and not self.task.done()) or self.needs_recovery:
            raise ValueError("已有待机运行或尚未完成回正，请先关闭并恢复")
        if once is not None and once not in {a["id"] for a in actions}:
            raise ValueError("单次待机动作不在允许列表")
        self.allowed, self.interrupt = idle_allowed, interrupt_requested
        self.valid = valid_session or (lambda: True)
        if not self.alive():
            raise ValueError("待机场次已失效")
        self.actions, self.settings, self.once = actions, settings, once
        self.enabled = True
        self.epoch += 1
        if once is not None and not self.can_start(self.epoch):
            self.enabled = False
            raise ValueError("当前忙碌，不能试播待机动作")
        self.error = ""
        self.recovery_failed = False
        self.next_tick = 0.0
        self.status = "paused"
        self.task = asyncio.create_task(self.run(self.epoch), name="idle-motion-loop")
        return self.public()

    def latch_stop(self):
        self.enabled = False
        self.epoch += 1
        self.next_tick = 0.0
        self.status = "stopped"

    def fail(self, code):
        self.enabled = False
        self.epoch += 1
        self.next_tick = 0.0
        self.status, self.error = "error", code

    def record(self, role, status, action, **extra):
        self.events.append({"source": "idle_motions", "role": role, "status": status,
            "action_id": action["id"], "description_sha256": action["description_sha256"],
            "visual_effect_confirmed": False, **extra})
        try:
            self.c._save("idle-motions-log.json", self.public())
        except (OSError, ValueError, UnicodeError):
            self.fail("idle_motion_log_failed")

    async def write(self, role, action, allowed):
        c = self.c
        while allowed():
            deadline = max(c._motion_not_before, self.not_before)
            if deadline > time.monotonic():
                await c._wait(min(self.POLL_SECONDS, deadline - time.monotonic()))
                continue
            async with c._send_lock:
                if not allowed():
                    return False
                if max(c._motion_not_before, self.not_before) > time.monotonic():
                    continue
                c._check()
                if c._ws is None or c._ws.closed:
                    raise ConnectionError("待机控制通道未连接")
                content = action["instruction" if role == "I" else "recovery_instruction"]
                msg_id = uuid.uuid4().hex[:12]
                frame = {"type": 9, "live_id": c.live_id, "conn_id": c._conn_id,
                         "seq_id": next(c._seq), "payload": {"text_msg": {
                             "content": content, "msg_id": msg_id, "timestamp": int(time.time()*1000)}}}
                if role == "I":
                    # Socket failure is ambiguous: remember this exact action's Q
                    # obligation before the await, but never retry its I.
                    self.current, self.needs_recovery = action, True
                self.not_before = time.monotonic() + self.I_GUARD_SECONDS
                try:
                    async with asyncio.timeout(self.WRITE_TIMEOUT):
                        await c._ws.send_json(frame)
                except BaseException as error:
                    self.record(role, "send_unconfirmed", action, seq_id=frame["seq_id"],
                                error_type=type(error).__name__)
                    raise
                sent = time.monotonic()
                guard = self.I_GUARD_SECONDS if role == "I" else self.Q_GUARD_SECONDS
                c._motion_not_before = self.not_before = sent + guard
                if role == "I":
                    self.i_sent += 1
                    self.last_id = action["id"]
                    self.cooldowns[action["id"]] = sent + action["cooldown_seconds"]
                    self.hold_until = sent + action["hold_seconds"]
                    self.status = "holding"
                else:
                    self.q_sent += 1
                    self.needs_recovery = False
                    self.hold_until = 0.0
                self.record(role, "sent", action, seq_id=frame["seq_id"], msg_id=msg_id,
                            instruction=content, sent_at=time.time(), sent_at_monotonic=sent)
                return True
        return False

    async def recover(self, reason, *, explicit=False):
        c = self.c
        async with c._idle_motion_lock:
            if self.recovery_failed and not explicit:
                raise IdleRecoveryError("待机回正未确认，请显式关闭并恢复后再发送内容")
            if self.needs_recovery:
                if not self.alive():
                    raise _Stopped()
                self.status = "recovering"
                try:
                    if not await self.write("Q", self.current, self.alive):
                        raise _Stopped()
                except _Stopped:
                    raise
                except asyncio.CancelledError:
                    self.recovery_failed = True
                    self.fail("idle_motion_recovery_failed")
                    raise
                except Exception:
                    self.recovery_failed = True
                    self.fail("idle_motion_recovery_failed")
                    raise IdleRecoveryError("待机回正未确认，未开始后续内容") from None
                if explicit:
                    self.recovery_failed = False
                    self.error = ""
            # A competing content caller must wait for the same successful Q's
            # guard, even after needs_recovery was cleared by the background loop.
            await c._wait(max(0.0, self.not_before - time.monotonic()))
            c._check()
            self.current = None
            self.next_tick = 0.0
            if self.once:
                self.enabled = False
            self.status = "paused" if self.enabled else "error" if self.error else "off"

    async def pause_for_content(self):
        self.next_tick = 0.0
        if self.recovery_failed or self.needs_recovery or self.not_before > time.monotonic():
            await self.recover("normal_content")

    def candidates(self):
        now = time.monotonic()
        return [a for a in self.actions if self.cooldowns.get(a["id"], 0) <= now
                and (a["id"] != self.last_id or len(self.actions) == 1)
                and (self.once is None or a["id"] == self.once)]

    async def run(self, epoch):
        c = self.c
        try:
            while self.enabled and epoch == self.epoch:
                if not self.alive():
                    self.latch_stop()
                    break
                if self.needs_recovery:
                    if (time.monotonic() >= self.hold_until or self.callback(self.interrupt)
                            or c._pending_speech or c._motion_control_pending):
                        if not c._speech_lock.locked():
                            async with c._speech_lock:
                                await self.recover("hold_or_interrupt")
                            if self.once:
                                self.enabled, self.status = False, "off"
                    await c._wait(self.POLL_SECONDS)
                    continue
                if not self.can_start(epoch):
                    self.next_tick, self.status = 0.0, "paused"
                    if self.once:
                        self.enabled, self.status = False, "off"
                else:
                    candidates = self.candidates()
                    if not candidates:
                        self.next_tick, self.status = 0.0, "waiting_candidates"
                    else:
                        if not self.next_tick:
                            interval = 0 if self.once else random.uniform(
                                self.settings["min_interval_seconds"], self.settings["max_interval_seconds"])
                            self.next_tick = time.monotonic() + interval
                            self.status = "waiting"
                        if (time.monotonic() >= self.next_tick
                                and time.monotonic() >= max(c._motion_not_before, self.not_before)):
                            action = random.choices(candidates, weights=[a["weight"] for a in candidates], k=1)[0]
                            # Only the short socket write owns speech; holding and
                            # random waiting never delay a higher priority reply.
                            async with c._speech_lock:
                                async with c._idle_motion_lock:
                                    sent = await self.write("I", action, lambda: self.can_start(epoch, owns_speech=True))
                                    if not sent and self.once:
                                        self.enabled, self.status = False, "off"
                            self.next_tick = 0.0
                await c._wait(self.POLL_SECONDS)
        except _Stopped:
            self.latch_stop()
        except asyncio.CancelledError:
            self.fail("idle_motion_cancelled")
            raise
        except Exception:
            if not self.recovery_failed:
                self.fail("idle_motion_send_failed")

    async def stop(self, recover=True, *, explicit=True):
        if type(recover) is not bool:
            raise ValueError("待机关闭恢复选项必须为布尔值")
        self.enabled = False
        self.epoch += 1
        self.next_tick = 0.0
        if self.task and self.task is not asyncio.current_task():
            await asyncio.gather(self.task, return_exceptions=True)
        if recover and (self.needs_recovery or self.not_before > time.monotonic()) and self.alive():
            async with self.c._speech_lock:
                await self.recover("manual_off" if explicit else "control_handoff", explicit=explicit)
        self.status = "stopped" if self.c._stop.is_set() else "error" if self.error else "off"
        return self.public()
