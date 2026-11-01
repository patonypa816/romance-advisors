"""Speech and structured Type 9 groups share one cancellable live connection.

A simultaneous pair is one text frame. Sequential nodes are separate frames.
Planned/actual times describe transport writes, never visual completion.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import ipaddress
import json
import re
import time
from urllib.parse import urlsplit
import uuid

from action_schema import (ACTION_POST_SEND_SECONDS, READING_LEAD_SECONDS, ANSWER_START_SECONDS, compile_reading_timeline, compile_timeline,
                           compile_short_performance_timeline, compile_continuous_speech_timeline,
                           CONTINUOUS_SECOND_GE_SECONDS, CONTINUOUS_MIN_AUDIO_SECONDS,
                           compile_long_speech_timeline, long_speech_required_audio_seconds,
                           compile_long_action_replay_timeline,
                           validate_long_action_group_count,
                           validate_long_speech_group_interval,
                           LONG_SPEECH_MAX_TEXT, LONG_SPEECH_MAX_GROUPS,
                           LONG_SPEECH_GROUP_INTERVAL, LONG_SPEECH_TAIL_SECONDS)
from component_live import PersistentComponent, _Stopped
from semantic_speech import _semantic_limits, compile_semantic_timeline, validate_alignment, LocalActionAlignment
from idle_motion import IdleMotionScheduler, IdleRecoveryError


MOTION_CONTROL_WRITE_TIMEOUT = 5.0


class MotionComponent(PersistentComponent):
    def __init__(self, *args, short_performance_test=False, continuous_speech_test=False,
                 long_speech_test=False, long_speech_group_interval=LONG_SPEECH_GROUP_INTERVAL,
                 long_speech_action_kind="S", long_action_group_count=1, **kwargs):
        if sum(bool(flag) for flag in (short_performance_test, continuous_speech_test, long_speech_test)) > 1:
            raise ValueError("短表演、连续口播与长文口播试验不能同时启用")
        group_interval = validate_long_speech_group_interval(long_speech_group_interval)
        if not long_speech_test and group_interval != LONG_SPEECH_GROUP_INTERVAL:
            raise ValueError("非长文口播模式不能使用自定义组间隔")
        if not isinstance(long_speech_action_kind, str) or long_speech_action_kind not in ("S", "L"):
            raise ValueError("长文口播动作种类只能为S或L")
        if not long_speech_test and long_speech_action_kind != "S":
            raise ValueError("非长文口播模式不能使用单L长动作回放")
        long_action_group_count = validate_long_action_group_count(long_action_group_count)
        if long_action_group_count != 1 and not (long_speech_test and long_speech_action_kind == "L"):
            raise ValueError("双L回放只能在长文口播L模式启用")
        super().__init__(*args, **kwargs)
        self.short_performance_test = bool(short_performance_test)
        self.continuous_speech_test = bool(continuous_speech_test)
        self.long_speech_test = bool(long_speech_test)
        self.long_speech_group_interval = group_interval
        self.long_speech_action_kind = long_speech_action_kind
        self.long_action_group_count = long_action_group_count
        self.motion_events = []
        self.active_motion_plan = {}
        self._motion_epoch = 0
        self._motion_task = None
        self._motion_claims = set()
        self._motion_not_before = 0.0
        self.motion_groups_started = 0
        self.motion_groups_sent = 0
        self.last_motion_receipt = {}
        self.avatar_switch = {}
        self._avatar_switch_fingerprints = {}
        self._avatar_switch_attempts = {}
        self._conn_id = "terra-action-rules-" + uuid.uuid4().hex[:12]
        self._idle_motion_lock = asyncio.Lock()
        self._motion_control_pending = 0
        self._idle_motions = IdleMotionScheduler(self)

    @property
    def motion_mode(self):
        return ("long_action_replay" if self._long_action_replay else
                "long_speech_test" if self.long_speech_test else
                "continuous_speech_test" if self.continuous_speech_test else
                "short_performance_test" if self.short_performance_test else "normal")

    @property
    def _multi_group_speech(self):
        return self.continuous_speech_test or self.long_speech_test

    @property
    def _long_action_replay(self):
        return self.long_speech_test and self.long_speech_action_kind == "L"

    def public(self):
        return {**super().public(), "short_performance_test": self.short_performance_test,
                "continuous_speech_test": self.continuous_speech_test, "mode": self.motion_mode,
                "long_speech_test": self.long_speech_test,
                "long_speech_group_interval": self.long_speech_group_interval,
                "long_speech_action_kind": self.long_speech_action_kind,
                "long_action_group_count": self.long_action_group_count,
                "motion_events": list(self.motion_events),
                "active_motion_plan": dict(self.active_motion_plan),
                "motion_groups_started": self.motion_groups_started,
                "motion_groups_sent": self.motion_groups_sent,
                "last_motion_receipt": dict(self.last_motion_receipt),
                "avatar_switch": dict(self.avatar_switch),
                "idle_motions": self.idle_motions_public(),
                "motion_rules": {"max_simultaneous_short_actions": 2,
                                 "sequential_node_count_limit": None,
                                 "short_to_q_seconds": 1.5,
                                 "minimum_post_action_seconds": ACTION_POST_SEND_SECONDS,
                                 "post_action_kinds": ["S", "L", "Q"],
                                 "reading_lead_seconds": READING_LEAD_SECONDS,
                                 "gaze_expression_overlay": "every_R_S0_S_L_Q_instruction",
                                 **({"max_simultaneous_short_actions": 1,
                                     "sequential_node_count_limit": 1, "reading_lead_seconds": 0,
                                     "short_performance_lead_seconds": 0.5,
                                     "gaze_expression_overlay": "GE_lead_then_S_and_Q"}
                                    if self.short_performance_test else {}),
                                 **({"max_simultaneous_short_actions": 1,
                                     "sequential_node_count_limit": 2, "reading_lead_seconds": 0,
                                     "minimum_audio_seconds": CONTINUOUS_MIN_AUDIO_SECONDS,
                                     "second_GE_audio_seconds": CONTINUOUS_SECOND_GE_SECONDS,
                                     "gaze_expression_overlay": "GE_S_Q_twice_in_one_audio"}
                                    if self.continuous_speech_test else {}),
                                 **({"max_simultaneous_short_actions": 1,
                                     "sequential_node_count_limit": LONG_SPEECH_MAX_GROUPS,
                                     "reading_lead_seconds": 0,
                                     "max_text_codepoints": LONG_SPEECH_MAX_TEXT,
                                     "group_interval_seconds": self.long_speech_group_interval,
                                     "minimum_tail_seconds": LONG_SPEECH_TAIL_SECONDS,
                                     "gaze_expression_overlay": "GE_S_Q_per_group_in_one_audio"}
                                    if self.long_speech_test and not self._long_action_replay else {}),
                                 **({"max_simultaneous_short_actions": 0,
                                     "max_simultaneous_long_actions": 1,
                                     "action_kind": "L",
                                     "sequential_node_count_limit": self.long_action_group_count,
                                     "long_action_group_count": self.long_action_group_count,
                                     "reading_lead_seconds": 0,
                                     "max_text_codepoints": LONG_SPEECH_MAX_TEXT,
                                     "short_to_q_seconds": None,
                                     "long_to_q_timing": "hold_seconds_after_successful_write",
                                     "gaze_expression_overlay": "GE_lead_then_L_and_Q"}
                                    if self._long_action_replay else {})}}

    def latch_stop(self):
        # Stop is synchronous: invalidate even a tick waiting on the socket lock.
        self._idle_motions.latch_stop()
        self._motion_epoch += 1
        super().latch_stop()

    async def _pause_idle_for_content(self):
        await self._idle_motions.pause_for_content()

    def start_idle_motions(self, actions, settings, idle_allowed, interrupt_requested,
                           valid_session=None, *, once=None):
        return self._idle_motions.start(actions, settings, idle_allowed, interrupt_requested,
                                        valid_session, once=once)

    def test_idle_motion(self, action, idle_allowed, interrupt_requested, valid_session=None):
        return self.start_idle_motions([action], {"min_interval_seconds": 15, "max_interval_seconds": 25},
                                       idle_allowed, interrupt_requested, valid_session, once=action["id"])

    async def stop_idle_motions(self, recover=True, *, explicit=True):
        return await self._idle_motions.stop(recover, explicit=explicit)

    async def recover_idle_motions(self):
        # Explicit user recovery is the only path allowed to retry a failed Q.
        return await self.stop_idle_motions(recover=True)

    def idle_motions_public(self):
        return self._idle_motions.public()

    async def stop(self, reason="用户停止"):
        self.latch_stop()
        await self.stop_idle_motions(recover=False)
        return await super().stop(reason)

    async def close(self):
        self.latch_stop()
        await self.stop_idle_motions(recover=False)
        await super().close()

    async def _send(self, kind, payload, stopping=False, still_valid=None):
        # All Type 9 writes must come from the validated group scheduler. In
        # particular, direct Type 15/16/97 writes cannot bypass it.
        if kind not in {1, 5, 10}:
            raise ValueError("本场只允许组内Type9、台词、初始化和结束信令")
        return await super()._send(kind, payload, stopping=stopping, still_valid=still_valid)

    async def _send_audio(self, pcm, text, action_instruction=""):
        # 旧入口即使没有动作文字也会直发PCM；新试验必须先经过整段与完整动作组校验。
        if self._multi_group_speech:
            raise ValueError("连续口播与长文口播试验必须通过完整表演入口发送")
        self._pending_speech += 1
        try:
            async with self._speech_lock:
                await self._pause_idle_for_content()
            return await super()._send_audio(pcm, text, action_instruction)
        finally:
            self._pending_speech -= 1

    @staticmethod
    def _switch_payload(avatar_id, switch_videos):
        if not isinstance(avatar_id, str) or not re.fullmatch(r"[0-9]{1,64}", avatar_id):
            raise ValueError("形象ID须为1至64位数字字符串")
        if not isinstance(switch_videos, list) or not 1 <= len(switch_videos) <= 10:
            raise ValueError("切换视频须为1至10个HTTP(S)地址")
        for value in switch_videos:
            try:
                if (not isinstance(value, str) or not 1 <= len(value) <= 4096
                        or any(c.isspace() or ord(c) < 32 for c in value)):
                    raise ValueError()
                url = urlsplit(value)
                host = (url.hostname or "").lower().rstrip(".")
                if (url.scheme not in {"http", "https"} or not host
                        or url.username is not None or url.password is not None
                        or url.port == 0 or "%" in host or "\\" in url.netloc
                        or host == "localhost" or host.endswith(".localhost")):
                    raise ValueError()
                try:
                    address = ipaddress.ip_address(host)
                except ValueError:
                    # 拒绝127.1、整数/十六进制等可被网络库解释为本机IP的旧写法。
                    if all(re.fullmatch(r"(?:[0-9]+|0[xX][0-9a-fA-F]+)", part)
                           for part in host.split(".")):
                        raise ValueError()
                else:
                    if not address.is_global or address.is_multicast:
                        raise ValueError()
            except (ValueError, TypeError):
                # 验证异常不拼接原地址，避免把访问凭据带到界面或日志。
                raise ValueError("切换视频需要合法的公网HTTP(S)地址，不能含登录信息或本机/私网IP") from None
        return {"switch_avatar": {"switch_videos": list(switch_videos), "avatar_id": avatar_id}}

    async def switch_avatar(self, avatar_id, switch_videos, request_id, still_valid):
        """Send one manual Type16; the host owns the subsequent observation hold.

        A returned sent receipt means a transport write, never visual completion.
        Cancelled/failed/ambiguous request IDs remain consumed and are not retried.
        """
        payload = self._switch_payload(avatar_id, switch_videos)
        if (not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id)
                or not callable(still_valid)):
            raise ValueError("切换请求需要安全的request_id与同步有效性检查")
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        previous = self._action_requests.get(request_id)
        if previous is not None:
            if (previous["signal_type"] != 16
                    or self._avatar_switch_fingerprints.get(request_id) != fingerprint):
                raise ValueError("同一request_id不能用于不同切换参数")
            return {**previous, "duplicate": True}
        result = {"request_id": request_id, "signal_type": 16,
                  "source": "application_manual_avatar_switch", "avatar_id": avatar_id,
                  "video_count": len(switch_videos), "attempted": True,
                  "sent": False, "cancelled": False, "status": "pending",
                  "seq_id": None, "sent_at": None, "visual_effect_confirmed": False,
                  "success_acknowledged": False, "success_ack_available": False}
        # receipt只存数量与安全ID；去重使用摘要，不把视频URL留在public或事件中。
        self._action_requests[request_id] = result
        self._avatar_switch_fingerprints[request_id] = fingerprint
        self.avatar_switch = result

        def cancelled():
            result.update(cancelled=True, status="cancelled")
            self._event("avatar_switch_cancelled", **result)
            return dict(result)

        self._motion_control_pending += 1
        try:
            if (not self._control_allowed(still_valid)
                    or self._speech_lock.locked()):
                return cancelled()
            async with self._speech_lock:
                if not self._control_allowed(still_valid):
                    return cancelled()
                await self._pause_idle_for_content()
                if not self._control_allowed(still_valid):
                    return cancelled()
                if not await self._write_avatar_switch(payload, result, still_valid):
                    return cancelled()
                return dict(result)
        except _Stopped:
            return cancelled()
        except asyncio.CancelledError:
            if result["status"] != "failed":
                result["status"] = "send_unconfirmed"
            self._fail("手动形象切换调用被取消")
            raise
        except IdleRecoveryError:
            raise
        except Exception:
            if result["status"] != "failed":
                result["status"] = "send_unconfirmed"
            self._fail("手动形象切换发送未完成")
            raise RuntimeError("手动形象切换发送未完成，已请求停止") from None
        finally:
            self._motion_control_pending -= 1

    async def _write_avatar_switch(self, payload, result, still_valid):
        async with self._send_lock:
            # 等待发送锁时可能出现新任务或停止；最后复核与写入之间不再await。
            if not self._control_allowed(still_valid):
                return False
            self._check()
            if self._ws is None or self._ws.closed:
                raise ConnectionError("控制通道未连接")
            frame = {"type": 16, "live_id": self.live_id, "conn_id": self._conn_id,
                     "seq_id": next(self._seq), "payload": payload}
            result["seq_id"] = frame["seq_id"]
            # 在send_json让出执行权前登记，才能关联立即返回的103失败帧。
            self._avatar_switch_attempts[frame["seq_id"]] = result
            size = len(json.dumps(frame).encode())
            self.diagnostics.frame("out", "attempt", frame, size, request_id=result["request_id"])
            try:
                await self._ws.send_json(frame)
            except BaseException as error:
                self.diagnostics.frame("out", "failed", frame, size, error_type=type(error).__name__)
                raise
            result.update(sent=True, sent_at=time.time())
            # 已收到失败时只补传输事实，不能用“已发送”覆盖云端拒绝。
            if result["status"] != "failed":
                result["status"] = "sent"
            self.diagnostics.frame("out", "sent", frame, size, request_id=result["request_id"])
            self._event("avatar_switch_sent", **result)
            return True

    def _handle_control_error(self, frame):
        payload = frame.get("payload")
        error = payload.get("error") if isinstance(payload, dict) else None
        seq_id = frame.get("seq_id")
        if (frame.get("type") != 103 or not isinstance(error, dict) or error.get("source") != "switch_avatar"
                or type(seq_id) is not int or seq_id not in self._avatar_switch_attempts):
            return False
        result = self._avatar_switch_attempts[seq_id]
        code = error.get("code")
        result.update(status="failed", error_code=(code if isinstance(code, str)
                      and re.fullmatch(r"[A-Za-z0-9_]{1,100}", code) else "SWITCH_AVATAR_ERROR"))
        self._event("avatar_switch_failed", **result)
        return True

    async def _write_motion(self, node, epoch, group_id, segment_index, origin, previous):
        while True:
            self._check_aligned_plan(epoch, group_id)
            # Waiting outside the socket lock lets Type10 and PCM continue.
            # The deadline survives group/utterance boundaries after a tail Q.
            remaining = self._motion_not_before - time.monotonic()
            if remaining > 0:
                await self._wait(remaining)
            async with self._send_lock:
                self._check_aligned_plan(epoch, group_id)
                # Another motion may have completed while this task awaited
                # the lock. Recheck without holding up the audio transport.
                if time.monotonic() < self._motion_not_before:
                    continue
                claim = (group_id, node["index"])
                if claim in self._motion_claims:
                    raise RuntimeError("同一动作节点不可重复写入")
                if self._ws is None or self._ws.closed:
                    raise ConnectionError("动作通道未连接")
                self._motion_claims.add(claim)
                msg_id = uuid.uuid4().hex[:12]
                frame = {"type": 9, "live_id": self.live_id, "conn_id": self._conn_id,
                         "seq_id": next(self._seq), "payload": {"text_msg": {
                             "content": node["instruction"], "msg_id": msg_id,
                             "timestamp": int(time.time()*1000)}}}
                size = len(json.dumps(frame).encode())
                context = {"comment_id": self.comment_id, "segment_index": segment_index,
                           "group_id": group_id, "node_index": node["index"], "role": node["role"]}
                if self._multi_group_speech:
                    context.update(mode=self.motion_mode)
                if "group_index" in node:
                    context["group_index"] = node["group_index"]
                self.diagnostics.frame("out", "attempt", frame, size, **context)
                try:
                    await self._ws.send_json(frame)
                except BaseException as error:
                    self.diagnostics.frame("out", "failed", frame, size,
                                           error_type=type(error).__name__, **context)
                    raise
                sent = time.monotonic()
                if node["kind"] in {"S", "L", "Q"}:
                    self._motion_not_before = sent + ACTION_POST_SEND_SECONDS
                event = {**context, "planned_at_seconds": node["at_seconds"],
                         "actual_at_seconds": round(sent-origin, 6) if origin is not None else 0.0,
                         "actual_interval_seconds": round(sent-previous, 6) if previous is not None else 0.0,
                         "sent_at": time.time(), "sent_at_monotonic": sent,
                         "instruction": node["instruction"],
                         "gaze": node["gaze"], "expression": node["expression"],
                         "seq_id": frame["seq_id"], "msg_id": msg_id,
                         "simultaneous_count": len(node["motions"]), "visual_effect_confirmed": False}
                if node.get("timebase") == "answer_first_pcm":
                    event["timebase"] = "answer_first_pcm" if origin is not None else "answer_first_pcm_pending"
                    if origin is None:
                        # Before first PCM there is no measured audio origin yet.
                        # The final aligned receipt fills in the signed offset.
                        event["actual_at_seconds"] = None
                self.motion_events.append(event)
                self.active_motion_plan["actions_sent"] = sum(
                    row["group_id"] == group_id for row in self.motion_events)
                if self.last_motion_receipt.get("group_id") == group_id:
                    self.last_motion_receipt["actions_sent"] = self.active_motion_plan["actions_sent"]
                for group in self.active_motion_plan.get("groups", []):
                    if group["group_index"] == node.get("group_index"):
                        group["actions_sent"] += 1
                        group["actions_sent_complete"] = group["actions_sent"] == 3
                        group["last_sent_role"] = node["role"]
                        group["last_actual_at_seconds"] = event["actual_at_seconds"]
                self.diagnostics.frame("out", "sent", frame, size, **context)
                self._event("motion_node_sent", **event)
                try:
                    with (self.out / "component-outbound.jsonl").open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(self.diagnostics.clean(frame), ensure_ascii=False)+"\n")
                except (OSError, UnicodeError):
                    self.diagnostics.record("local", "legacy_outbound_write_failed")
                return sent

    async def _run_remaining(self, timeline, epoch, group_id, segment_index, first_sent, *, origin=None):
        origin = first_sent if origin is None else origin
        previous = first_sent
        try:
            for node in timeline[1:]:
                # Start each gap at the previous successful write. This keeps Q
                # 1.5 seconds after the last S even if a socket write was delayed.
                await self._wait(max(0.0, previous+node["delay_seconds"]-time.monotonic()))
                previous = await self._write_motion(node, epoch, group_id, segment_index,
                                                    origin, previous)
        except BaseException as error:
            if not self._stop.is_set() and not isinstance(error, asyncio.CancelledError):
                self._fail("动作组未能完整发送，已停止本场")
            raise

    async def _run_continuous_remaining(self, timeline, epoch, group_id, segment_index,
                                        first_sent, origin, audio_progress):
        previous = first_sent
        try:
            for node in timeline[2:]:
                if node["role"] == "GE":
                    # 前Q已经成功；后续GE各有真实PCM进度和首帧时钟两个闸门。
                    # 所有路径还由_write_motion保护S/L/Q后的1秒下限。
                    gate = audio_progress["gates"][node["group_index"]]
                    waits = [asyncio.create_task(gate["ready"].wait()),
                             asyncio.create_task(self._stop.wait())]
                    try:
                        done, _ = await asyncio.wait(waits, timeout=self.MAX_AUDIO_SECONDS,
                                                     return_when=asyncio.FIRST_COMPLETED)
                        self._check()
                        if not done:
                            raise TimeoutError("口播PCM发送进度未到达下一动作窗口")
                    finally:
                        for waiter in waits:
                            if not waiter.done():
                                waiter.cancel()
                        await asyncio.gather(*waits, return_exceptions=True)
                    deadline = audio_progress["first_sent_at_monotonic"] + node["audio_at_seconds"]
                    if self._long_action_replay and self.long_action_group_count == 2:
                        # 双L之间的Q→GE等待也锚定成功写入，不能被PCM时钟追赶压缩。
                        deadline = max(deadline, previous + node["delay_seconds"])
                else:
                    # 每次从成功写入重新计时，GE2延迟也不能挤压S2/Q2窗口。
                    deadline = previous + node["delay_seconds"]
                await self._wait(max(0.0, deadline - time.monotonic()))
                previous = await self._write_motion(node, epoch, group_id, segment_index, origin, previous)
        except BaseException as error:
            if not self._stop.is_set() and not isinstance(error, asyncio.CancelledError):
                self._fail("长文口播动作未能完整发送，已停止本场" if self.long_speech_test
                           else "连续口播动作未能完整发送，已停止本场")
            raise

    async def send_motion_expression(self, actions, *, recovery, number=1):
        """纯动作与语音共用串行锁；不生成台词、PCM或增加语音计数。"""
        if self._multi_group_speech:
            raise ValueError("连续口播与长文口播试验只接受一段语音，不接受纯动作轮")
        timeline = (compile_short_performance_timeline(actions, recovery) if self.short_performance_test
                    else compile_timeline(actions, recovery))
        if not timeline:
            raise ValueError("纯动作轮至少需要一个S或L节点")
        if type(number) is not int or number < 1:
            raise ValueError("段号必须为正整数")
        if self._stop.is_set() or self.state not in {"ready", "sending", "draining"}:
            raise RuntimeError("会话未就绪或已经停止")
        # 沿用现有busy计数，避免另一个入口在排队或发送中插入动作。
        self._pending_speech += 1
        try:
            async with self._speech_lock:
                group_id = uuid.uuid4().hex[:12]
                receipt = None
                try:
                    self._check()
                    await self._pause_idle_for_content()
                    if self.state != "ready" or not self._ready.is_set() or not self._joined.is_set():
                        raise RuntimeError("组件不能接收新一轮")
                    self._motion_epoch += 1
                    epoch = self._motion_epoch
                    self.motion_groups_started += 1
                    self.state = "sending"
                    self.active_motion_plan = {
                        "group_id": group_id, "comment_id": self.comment_id,
                        "segment_index": number, "input_kind": "motion", "is_active": True,
                        "short_performance_test": self.short_performance_test,
                        "status": "sending", "actions_sent": 0,
                        "timeline": timeline, "recovery": recovery}
                    receipt = {
                        "id": self.motion_groups_started, "group_id": group_id,
                        "comment_id": self.comment_id, "segment_index": number,
                        "input_kind": "motion", "status": "sending", "audio_seconds": 0.0,
                        "short_performance_test": self.short_performance_test,
                        "actions_sent": 0, "send_complete": False,
                        "actions_sent_complete": False, "visual_effect_confirmed": False,
                        "playback_complete_confirmed": False, "timeline": timeline}
                    self.last_motion_receipt = receipt
                    self._event("motion_group_started", **self.active_motion_plan)
                    first_sent = await self._write_motion(timeline[0], epoch, group_id, number, None, None)
                    # 与语音动作使用同一个调度器：每个间隔都锚定前次成功写入，
                    # L窗口和唯一尾Q的规则因此不会在纯动作分支发生漂移。
                    await self._run_remaining(timeline, epoch, group_id, number, first_sent)
                    self._check()
                    receipt.update(status="sent", send_complete=True, actions_sent_complete=True)
                    receipt["motion_events"] = [dict(e) for e in self.motion_events
                                                if e["group_id"] == group_id]
                    self.active_motion_plan["status"] = "sent"
                    self.motion_groups_sent += 1
                    self._event("motion_group_sent", group_id=group_id, segment_index=number,
                                input_kind="motion")
                    self.state = "ready"
                    return dict(receipt)
                except _Stopped:
                    if receipt is not None:
                        receipt["status"] = "cancelled"
                    self._event("motion_group_cancelled", group_id=group_id)
                    raise RuntimeError("会话已停止，剩余动作与恢复指令取消") from None
                except asyncio.CancelledError:
                    if receipt is not None:
                        receipt["status"] = "cancelled"
                    self._fail("动作组调用已取消")
                    raise
                except IdleRecoveryError:
                    raise
                except Exception:
                    if receipt is not None:
                        receipt["status"] = "error"
                    self._fail("动作未完整发送")
                    raise
                finally:
                    if receipt is not None:
                        receipt["motion_events"] = [dict(e) for e in self.motion_events
                                                    if e["group_id"] == group_id]
                    if self.active_motion_plan.get("group_id") == group_id:
                        self.active_motion_plan["is_active"] = False
                        self.active_motion_plan["status"] = receipt["status"] if receipt else "error"
        finally:
            self._pending_speech -= 1

    async def send_expression(self, pcm, text, actions, segment_index, *, recovery=None, reading=None):
        # Validate again at the transport boundary before any frame is sent.
        has_reading = reading is not None
        if (self.short_performance_test or self._multi_group_speech) and has_reading:
            raise ValueError("短表演、连续口播与长文口播试验不接受reading")
        if has_reading and (not isinstance(reading, dict) or set(reading) != {"text", "pcm"}):
            raise ValueError("传输reading必须且只能包含text与pcm")
        read_pcm = reading["pcm"] if has_reading else None
        # 两份PCM必须在任何Type9/10之前一起验证；不能读完弹幕才发现回答不可发送。
        for audio in ([read_pcm, pcm] if has_reading else [pcm]):
            if (not isinstance(audio, bytes) or not audio or len(audio)%2
                    or len(audio) > self.MAX_AUDIO_SECONDS*self.RATE*2):
                raise ValueError("音频格式或时长不符合本场约束")
        reading_audio_seconds = len(read_pcm)/(self.RATE*2) if has_reading else 0.0
        total_audio_seconds = len(pcm)/(self.RATE*2) + reading_audio_seconds
        if self.continuous_speech_test and total_audio_seconds < CONTINUOUS_MIN_AUDIO_SECONDS:
            raise ValueError("连续口播试验真实PCM须至少5秒；不会延长、补静音或裁切")
        timeline = (compile_long_action_replay_timeline(actions, recovery, group_count=self.long_action_group_count)
                    if self._long_action_replay else
                    compile_long_speech_timeline(actions, recovery, group_interval=self.long_speech_group_interval)
                    if self.long_speech_test else
                    compile_continuous_speech_timeline(actions, recovery) if self.continuous_speech_test else
                    compile_short_performance_timeline(actions, recovery) if self.short_performance_test else
                    compile_reading_timeline(reading, actions, recovery, reading_audio_seconds)
                    if has_reading else compile_timeline(actions, recovery))
        required_audio_seconds = None
        if self._long_action_replay:
            # 从首L到末L窗口结束均须有原PCM覆盖；不把两个L窗口简单相加。
            required_audio_seconds = actions[-1]["hold_seconds"]
            if self.long_action_group_count == 2:
                required_audio_seconds += actions[1]["delay_seconds"]
        elif self.long_speech_test:
            required_audio_seconds = long_speech_required_audio_seconds(
                actions, group_interval=self.long_speech_group_interval)
        if self.long_speech_test and total_audio_seconds < required_audio_seconds:
            subject = "长动作口播试验完整L窗口" if self._long_action_replay else f"长文口播试验{len(actions)}组动作"
            raise ValueError(f"{subject}需要至少{required_audio_seconds:g}秒真实PCM，"
                             f"实际{total_audio_seconds:.3f}秒；不会删动作、补静音或裁切")
        max_text = LONG_SPEECH_MAX_TEXT if self.long_speech_test else self.MAX_TEXT_CODEPOINTS
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= max_text:
            raise ValueError(f"台词须为1至{max_text}字")
        if type(segment_index) is not int or segment_index < 1:
            raise ValueError("段号必须为正整数")
        if self._stop.is_set() or self.state not in {"ready", "sending", "draining"}:
            raise RuntimeError("会话未就绪或已经停止")
        self._pending_speech += 1
        try:
            async with self._speech_lock:
                task = None
                group_id = uuid.uuid4().hex[:12]
                try:
                    self._check()
                    await self._pause_idle_for_content()
                    if self.state != "ready" or not self._ready.is_set() or not self._joined.is_set():
                        raise RuntimeError("组件不能接收新一轮")
                    self._motion_epoch += 1
                    epoch = self._motion_epoch
                    self.utterances_started += 1
                    self.audio_seconds += total_audio_seconds
                    self.audio_sent_complete = False
                    self.state = "sending"
                    self.active_motion_plan = {"group_id": group_id, "comment_id": self.comment_id,
                                               "segment_index": segment_index, "is_active": True,
                                               "short_performance_test": self.short_performance_test,
                                               "continuous_speech_test": self.continuous_speech_test,
                                               "long_speech_test": self.long_speech_test,
                                               "mode": self.motion_mode,
                                               "timeline": timeline, "recovery": recovery,
                                               "reading_text": reading["text"] if has_reading else None,
                                               "reading_audio_seconds": reading_audio_seconds}
                    receipt = {"id": self.utterances_started, "group_id": group_id,
                               "segment_index": segment_index, "audio_seconds": total_audio_seconds,
                               "short_performance_test": self.short_performance_test,
                               "continuous_speech_test": self.continuous_speech_test,
                               "long_speech_test": self.long_speech_test,
                               "mode": self.motion_mode,
                               "reading_text": reading["text"] if has_reading else None,
                               "reading_audio_seconds": reading_audio_seconds,
                               "reading_lead_seconds": READING_LEAD_SECONDS if has_reading else 0.0,
                               "reading_sent_complete": not has_reading,
                               "send_complete": False, "buffer_complete": False,
                               "actions_sent_complete": not timeline, "visual_effect_confirmed": False,
                               "playback_complete_confirmed": False, "timeline": timeline}
                    self.last_utterance = receipt
                    audio_progress = None
                    if self._multi_group_speech:
                        group_indexes = sorted({node["group_index"] for node in timeline})
                        groups = [{"group_index": index, "node_indexes": [n["index"] for n in timeline
                                   if n["group_index"] == index], "actions_sent": 0,
                                   "planned_start_at_seconds": timeline[(index-1)*3]["at_seconds"],
                                   "actions_sent_complete": False} for index in group_indexes]
                        self.active_motion_plan["groups"] = groups
                        receipt["groups"] = groups
                        audio_progress = {"first_sent_at_monotonic": None, "sent_bytes": 0,
                                          "gates": {node["group_index"]: {
                                              "seconds": node["audio_at_seconds"], "ready": asyncio.Event()}
                                              for node in timeline if node["role"] == "GE"
                                              and node["group_index"] > 1}}
                        if self.long_speech_test:
                            receipt["required_audio_seconds"] = required_audio_seconds
                            self.active_motion_plan["required_audio_seconds"] = required_audio_seconds
                            receipt["long_speech_action_kind"] = self.long_speech_action_kind
                            self.active_motion_plan["long_speech_action_kind"] = self.long_speech_action_kind
                            if self._long_action_replay:
                                holds = [node["hold_seconds"] for node in timeline if node["role"] == "L"]
                                for target in (receipt, self.active_motion_plan):
                                    target["long_action_group_count"] = self.long_action_group_count
                                    target["group_hold_seconds"] = holds
                                    if self.long_action_group_count == 1:
                                        target["hold_seconds"] = holds[0]
                                for group, hold in zip(groups, holds):
                                    group["hold_seconds"] = hold
                            else:
                                receipt["long_speech_group_interval"] = self.long_speech_group_interval
                                self.active_motion_plan["long_speech_group_interval"] = self.long_speech_group_interval
                    self._event("motion_group_started", **self.active_motion_plan)
                    if has_reading:
                        origin = await self._write_motion(timeline[0], epoch, group_id, segment_index, None, None)
                        # 从R成功写入后计时，并复用可被停止打断的等待；S0仍跟在短读PCM后。
                        await self._wait(max(0.0, origin + READING_LEAD_SECONDS - time.monotonic()))
                        await self._send(10, {"text_msg": {"msg_id": uuid.uuid4().hex[:12],
                                         "content": reading["text"], "timestamp": int(time.time()*1000)}})
                        await self._audio(read_pcm, count_audio=True)
                        receipt["reading_sent_complete"] = True
                        # _audio按真实PCM长度节拍发送。S0只说明运输层开始回正，
                        # 不能据此断言云端已播放完阅读音频或已完成视觉动作。
                        first_sent = await self._write_motion(timeline[1], epoch, group_id, segment_index, origin, origin)
                        task = asyncio.create_task(self._run_remaining(timeline[1:], epoch, group_id,
                                                                     segment_index, first_sent, origin=origin))
                        self._motion_task = task
                    elif self.short_performance_test or self._multi_group_speech:
                        origin = await self._write_motion(timeline[0], epoch, group_id, segment_index, None, None)
                        await self._wait(max(0.0, origin + timeline[1]["delay_seconds"] - time.monotonic()))
                        first_sent = await self._write_motion(timeline[1], epoch, group_id, segment_index, origin, origin)
                        # S或L成功写入后立即放行Type10/PCM；Q独立计时，不阻断音频。
                        task = asyncio.create_task(
                            self._run_continuous_remaining(timeline, epoch, group_id, segment_index,
                                                           first_sent, origin, audio_progress)
                            if self._multi_group_speech else
                            self._run_remaining(timeline[1:], epoch, group_id,
                                                segment_index, first_sent, origin=origin))
                        self._motion_task = task
                    elif timeline:
                        first_sent = await self._write_motion(timeline[0], epoch, group_id, segment_index, None, None)
                        task = asyncio.create_task(self._run_remaining(timeline, epoch, group_id,
                                                                     segment_index, first_sent))
                        self._motion_task = task
                    await self._send(10, {"text_msg": {"msg_id": uuid.uuid4().hex[:12],
                                     "content": text, "timestamp": int(time.time()*1000)}})
                    if self._multi_group_speech:
                        def on_frame_sent(sent_bytes, sent_at):
                            if audio_progress["first_sent_at_monotonic"] is None:
                                audio_progress["first_sent_at_monotonic"] = sent_at
                                receipt["first_pcm_sent_at_monotonic"] = sent_at
                            audio_progress["sent_bytes"] = sent_bytes
                            receipt["pcm_transport_sent_bytes"] = sent_bytes
                            receipt["last_pcm_sent_at_monotonic"] = sent_at
                            for gate in audio_progress["gates"].values():
                                if sent_bytes >= gate["seconds"] * self.RATE * 2:
                                    gate["ready"].set()
                        await self._audio(pcm, count_audio=True, on_frame_sent=on_frame_sent)
                    else:
                        await self._audio(pcm, count_audio=True)
                    self.audio_sent_complete = True
                    self.utterances_sent += 1
                    receipt["send_complete"] = True
                    self.state = "draining"
                    await self._wait(self.DRAIN_SECONDS)
                    receipt["buffer_complete"] = True
                    if task is not None:
                        await task
                    self._check()
                    receipt["actions_sent_complete"] = True
                    receipt["motion_events"] = [dict(e) for e in self.motion_events if e["group_id"] == group_id]
                    self._event("motion_group_sent", group_id=group_id, segment_index=segment_index)
                    self.state = "ready"
                    return dict(receipt)
                except _Stopped:
                    self._event("motion_group_cancelled", group_id=group_id)
                    raise RuntimeError("会话已停止，剩余动作与恢复指令取消") from None
                except asyncio.CancelledError:
                    self._fail("动作组调用已取消")
                    raise
                except IdleRecoveryError:
                    raise
                except Exception:
                    self._fail("动作或台词未完整发送")
                    raise
                finally:
                    if task is not None:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    self._motion_task = None
                    if self.active_motion_plan.get("group_id") == group_id:
                        self.active_motion_plan["is_active"] = False
        finally:
            self._pending_speech -= 1


    def _check_aligned_plan(self, epoch, group_id):
        self._check()
        if epoch != self._motion_epoch or self.active_motion_plan.get("group_id") != group_id:
            raise _Stopped()

    async def _run_aligned_remaining(self, timeline, epoch, group_id, segment_index,
                                     progress, *, previous=None, max_audio_seconds=30):
        """Both PCM progress and elapsed time must reach each semantic node.

        The previous successful write supplies a third gate: a slow socket can
        postpone later nodes, but can never squeeze GE→L or L→Q to catch up.
        """
        try:
            for node in timeline:
                target = max(0.0, node["audio_at_seconds"])
                deadline = time.monotonic() + max_audio_seconds + 5
                while (progress["first_sent_at_monotonic"] is None or
                       progress["sent_bytes"] < node["pcm_required_bytes"]):
                    self._check_aligned_plan(epoch, group_id)
                    if time.monotonic() >= deadline:
                        raise TimeoutError("语义动作等待PCM发送进度超时")
                    await self._wait(.02)
                origin = progress["first_sent_at_monotonic"]
                at = origin + target
                if previous is not None:
                    at = max(at, previous + node["delay_seconds"])
                await self._wait(max(0.0, at-time.monotonic()))
                self._check_aligned_plan(epoch, group_id)
                previous = await self._write_motion(node, epoch, group_id, segment_index,
                                                     origin, previous)
        except BaseException as error:
            if not self._stop.is_set() and not isinstance(error, asyncio.CancelledError):
                self._fail("语义动作未能完整发送，已停止本场")
            raise

    async def send_aligned_expression(self, pcm, text, actions, segment_index, *,
                                      recovery=None, alignment=None, reading=None, action_kind="L", ge_lead_seconds=.5,
                                      max_audio_seconds=30, max_text_codepoints=120, allow_tail_q_after_audio=False,
                                      local_action_anchors=False):
        """Send the validated semantic path; receipts describe writes, not visuals."""
        _semantic_limits(max_audio_seconds, max_text_codepoints)
        if (type(local_action_anchors) is not bool or (local_action_anchors and
                (action_kind != "S" or ge_lead_seconds != 1.1 or not allow_tail_q_after_audio
                 or max_audio_seconds != 30))):
            raise ValueError("局部落点核验只允许正式A方案对话")
        if max_audio_seconds == 60 and (action_kind != "S" or ge_lead_seconds != 1.1 or reading is not None):
            raise ValueError("分钟素材只允许无reading的显式S与1.1秒GE前导")
        if self.short_performance_test or self._multi_group_speech:
            raise ValueError("语义锚点入口不能与旧口播试验混用")
        if reading is not None and (not isinstance(reading, dict) or
                                   set(reading) != {"text", "pcm"}):
            raise ValueError("传输reading必须且只能包含text与pcm")
        read_pcm = reading["pcm"] if reading is not None else None
        for audio in ([read_pcm, pcm] if reading is not None else [pcm]):
            if (not isinstance(audio, bytes) or not audio or len(audio) % 2 or
                    len(audio) > max_audio_seconds * self.RATE * 2):
                raise ValueError("音频格式或时长不符合本场约束")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= max_text_codepoints:
            raise ValueError(f"台词须为1至{max_text_codepoints}字")
        if type(segment_index) is not int or segment_index < 1:
            raise ValueError("段号必须为正整数")
        seconds = len(pcm) / (self.RATE * 2)
        read_seconds = len(read_pcm) / (self.RATE * 2) if read_pcm is not None else 0.0
        # Snapshot and bind timestamps to these exact PCM bytes before any R/9/10.
        alignment = copy.deepcopy(alignment)
        if actions:
            if local_action_anchors:
                # Recheck final anchors and the exact PCM before any reading,
                # action or audio frame. A planner receipt alone is insufficient.
                LocalActionAlignment(alignment, text, seconds,
                                     pcm_sha256=hashlib.sha256(pcm).hexdigest()).verify_actions(actions)
            else:
                validate_alignment(alignment, text, seconds,
                                   pcm_sha256=hashlib.sha256(pcm).hexdigest(), max_audio_seconds=max_audio_seconds,
                                   max_text_codepoints=max_text_codepoints)
        timeline = [{**node, "timebase": "answer_first_pcm"} for node in
                    compile_semantic_timeline(actions, recovery, alignment, seconds, action_kind=action_kind,
                                              ge_lead_seconds=ge_lead_seconds, max_audio_seconds=max_audio_seconds,
                                              max_text_codepoints=max_text_codepoints,
                                              allow_tail_q_after_audio=allow_tail_q_after_audio)]
        for node in timeline:
            # The compiler alone permits an after-audio final Q. Its PCM gate
            # ends at the real final sample; the independent clock still waits
            # for Q's original time and the previous successful write's gap.
            at = max(0.0, node["audio_at_seconds"])
            after_audio_q = (allow_tail_q_after_audio and node is timeline[-1]
                             and node["role"] == "Q" and at > seconds)
            node["pcm_required_bytes"] = (len(pcm) if after_audio_q or abs(at-seconds) < 1e-9 else
                                          round(at * self.RATE) * 2)
        reading_timeline = []
        if reading is not None:
            # Semantic actions never pass through the old S/L validator. With
            # no answer actions, retain its one reading recovery Q after S0.
            reading_timeline = compile_reading_timeline(
                reading, [], recovery, read_seconds)
            if timeline:
                reading_timeline = reading_timeline[:2]
            timeline = [{**node, "index": node["index"] + 2} for node in timeline]
        if self._stop.is_set() or self.state not in {"ready", "sending", "draining"}:
            raise RuntimeError("会话未就绪或已经停止")
        self._pending_speech += 1
        try:
            async with self._speech_lock:
                task = None
                receipt = None
                group_id = uuid.uuid4().hex[:12]
                try:
                    self._check()
                    await self._pause_idle_for_content()
                    if self.state != "ready" or not self._ready.is_set() or not self._joined.is_set():
                        raise RuntimeError("组件不能接收新一轮")
                    self._motion_epoch += 1
                    epoch = self._motion_epoch
                    self.utterances_started += 1
                    self.audio_seconds += seconds + read_seconds
                    self.audio_sent_complete = False
                    self.state = "sending"
                    receipt = {"id": self.utterances_started, "group_id": group_id,
                               "comment_id": self.comment_id,
                               "segment_index": segment_index, "status": "sending",
                               "mode": "semantic_speech", "semantic_speech": True, "action_kind": action_kind,
                               "ge_lead_seconds": ge_lead_seconds, "independent_ge": ge_lead_seconds is not None,
                               "max_audio_seconds": max_audio_seconds, "max_text_codepoints": max_text_codepoints,
                               "audio_seconds": seconds + read_seconds,
                               "answer_audio_seconds": seconds, "alignment": alignment,
                               "timeline": timeline, "reading_timeline": reading_timeline,
                               "reading_text": reading["text"] if reading is not None else None,
                               "reading_audio_seconds": read_seconds,
                               "reading_lead_seconds": READING_LEAD_SECONDS if reading is not None else 0.0,
                               "reading_sent_complete": reading is None,
                               "send_complete": False, "buffer_complete": False,
                               "actions_sent": 0, "actions_sent_complete": not (timeline or reading_timeline),
                               "visual_effect_confirmed": False,
                               "playback_complete_confirmed": False}
                    self.last_utterance = self.last_motion_receipt = receipt
                    self.active_motion_plan = {**receipt, "comment_id": self.comment_id,
                                               "is_active": True, "recovery": recovery}
                    self._event("motion_group_started", **self.active_motion_plan)
                    if reading is not None:
                        read_origin = await self._write_motion(reading_timeline[0], epoch, group_id,
                                                              segment_index, None, None)
                        await self._wait(max(0.0, read_origin + READING_LEAD_SECONDS-time.monotonic()))
                        self._check_aligned_plan(epoch, group_id)
                        await self._send(10, {"text_msg": {"msg_id": uuid.uuid4().hex[:12],
                                         "content": reading["text"], "timestamp": int(time.time()*1000)}})
                        await self._audio(read_pcm, count_audio=True)
                        receipt["reading_sent_complete"] = True
                        s0_sent = await self._write_motion(reading_timeline[1], epoch, group_id,
                                                           segment_index, read_origin, read_origin)
                        if not timeline:
                            # Q belongs to the reading posture recovery, so its
                            # S0+1.5s clock remains valid even for a .1s answer.
                            task = asyncio.create_task(self._run_remaining(
                                reading_timeline[1:], epoch, group_id, segment_index,
                                s0_sent, origin=read_origin))
                            self._motion_task = task
                        await self._wait(max(0.0, s0_sent + ANSWER_START_SECONDS-time.monotonic()))
                    progress = {"first_sent_at_monotonic": None, "sent_bytes": 0}
                    previous = None
                    # Send the nonpositive prefix before PCM. Derive its waits
                    # from nodes rather than assuming a GE/S pair or .5s lead:
                    # a zero-anchor S without GE is also a valid one-node prefix.
                    prefix_count = 0
                    last_preroll = None
                    for node in timeline:
                        if node["audio_at_seconds"] > 0:
                            break
                        if previous is not None:
                            await self._wait(max(0.0, previous + node["delay_seconds"]-time.monotonic()))
                        previous = await self._write_motion(node, epoch, group_id,
                                                             segment_index, None, previous)
                        last_preroll = node
                        prefix_count += 1
                    if last_preroll is not None and last_preroll["audio_at_seconds"] < 0:
                        # Preserve the positive action's original audio anchor;
                        # extending a GE lead moves GE earlier, never S later.
                        await self._wait(max(0.0, previous - last_preroll["audio_at_seconds"]-time.monotonic()))
                    remaining = timeline[prefix_count:]
                    self._check_aligned_plan(epoch, group_id)
                    await self._send(10, {"text_msg": {"msg_id": uuid.uuid4().hex[:12],
                                     "content": text, "timestamp": int(time.time()*1000)}})
                    def on_frame_sent(sent_bytes, sent_at):
                        if progress["first_sent_at_monotonic"] is None:
                            progress["first_sent_at_monotonic"] = sent_at
                            receipt["first_pcm_sent_at_monotonic"] = sent_at
                            # Pre-roll events become answer-relative once the
                            # actual first PCM write establishes that origin.
                            for event in self.motion_events:
                                if event["group_id"] == group_id and event.get("timebase") == "answer_first_pcm_pending":
                                    event["actual_at_seconds"] = round(event["sent_at_monotonic"]-sent_at, 6)
                                    event["timebase"] = "answer_first_pcm"
                        progress["sent_bytes"] = sent_bytes
                        receipt["pcm_transport_sent_bytes"] = sent_bytes
                        receipt["last_pcm_sent_at_monotonic"] = sent_at
                    if remaining:
                        task = asyncio.create_task(self._run_aligned_remaining(
                            remaining, epoch, group_id, segment_index, progress, previous=previous,
                            max_audio_seconds=max_audio_seconds))
                        self._motion_task = task
                    await self._audio(pcm, count_audio=True, on_frame_sent=on_frame_sent)
                    self.audio_sent_complete = True
                    self.utterances_sent += 1
                    receipt["send_complete"] = True
                    self.state = "draining"
                    await self._wait(self.DRAIN_SECONDS)
                    receipt["buffer_complete"] = True
                    if task is not None:
                        await task
                    self._check_aligned_plan(epoch, group_id)
                    receipt.update(status="sent", actions_sent_complete=True)
                    self._event("motion_group_sent", group_id=group_id, segment_index=segment_index)
                    self.state = "ready"
                    return dict(receipt, motion_events=[dict(e) for e in self.motion_events
                                                        if e["group_id"] == group_id])
                except _Stopped:
                    if receipt is not None:
                        receipt["status"] = "cancelled"
                    self._event("motion_group_cancelled", group_id=group_id)
                    raise RuntimeError("会话已停止，剩余动作与恢复指令取消") from None
                except asyncio.CancelledError:
                    if receipt is not None:
                        receipt["status"] = "cancelled"
                    self._fail("语义口播调用已取消")
                    raise
                except IdleRecoveryError:
                    raise
                except Exception:
                    if receipt is not None:
                        receipt["status"] = "error"
                    self._fail("语义动作或台词未完整发送")
                    raise
                finally:
                    if task is not None:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    self._motion_task = None
                    if receipt is not None:
                        receipt["motion_events"] = [dict(e) for e in self.motion_events
                                                    if e["group_id"] == group_id]
                        self._event("aligned_expression_receipt", **receipt)
                    if self.active_motion_plan.get("group_id") == group_id:
                        if receipt is not None:
                            self.active_motion_plan.update(receipt)
                        self.active_motion_plan["is_active"] = False
                        self.active_motion_plan["status"] = receipt["status"] if receipt else "error"
        finally:
            self._pending_speech -= 1
