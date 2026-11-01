"""One persistent Vidu S2 Component session with serial, bounded speech turns.

Importing this module does nothing. Only start() can create a cloud session, once.
Credentials, RTC grants, image data URIs and signed media URLs stay in memory.
The caller owns brain/TTS, receive-only RTC, recording and browser liveness.
This module never captures local media and never imposes a total runtime cutoff.
Protocol reused from the reviewed 2026-10-29 short-clip trial:
https://platform.vidu.cn/vidu-stream/doc/s2-avatar/component/parameters
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
import re
import time
import uuid
from pathlib import Path

import aiohttp
from diagnostics import Diagnostics, scrub


class _Stopped(Exception):
    pass


class _HTTPError(Exception):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"HTTP {status}")


class PersistentComponent:
    RATE = 24000
    FRAME_BYTES = 4800  # 100 ms of 24 kHz mono signed 16-bit little-endian PCM.
    MAX_AUDIO_SECONDS = 30
    MAX_TEXT_CODEPOINTS = 30
    MAX_ACTION_CODEPOINTS = 200
    BASE = "https://api.vidu.cn"
    REQUEST_TIMEOUT = 60.0
    READY_TIMEOUT = 90.0
    JOIN_TIMEOUT = 45.0
    END_TIMEOUT = 35.0
    POLL_INTERVAL = 1.5
    TAIL_SILENCE_SECONDS = 0.0
    DRAIN_SECONDS = 4.0
    PING_INTERVAL = 8.0
    RECORDING_ATTEMPTS = 20
    RECORDING_INTERVAL = 3.0
    TERMINAL = {"ended", "error", "stop_unconfirmed", "creation_unknown"}
    CLOUD_FIELDS = {
        "id", "trace_id", "status", "state", "billed_seconds", "credits_cost",
        "created_at", "started_at", "ended_at", "start_time", "end_time",
        "on_live_at", "close_reason", "live_duration",
    }

    def __init__(self, http: aiohttp.ClientSession, out: Path, image: Path, diagnostics=None):
        self.http, self.out, self.image = http, Path(out), Path(image)
        self.diagnostics = diagnostics or Diagnostics(out)
        self.comment_id = None
        self.state = "idle"
        self.error = ""
        self.live_id: str | None = None
        self.creation_attempted = False
        self.creation_unknown = False
        self.creation_input_mode: str | None = None
        self.creation_avatar_id: str | None = None
        self.cloud_end_confirmed = False
        self.audio_sent_complete = False
        self.sent_audio_bytes = 0
        self.audio_seconds = 0.0
        self.utterances_started = 0
        self.utterances_sent = 0
        self.last_utterance: dict = {}
        self.idle_action: dict = {}
        self.baseline_reset: dict = {}
        self._action_requests: dict[str, dict] = {}
        self._pending_speech = 0
        self.recording_saved = False
        self.recording_error = ""
        self._key = ""
        self._rtc: dict = {}
        self._cloud: dict = {}
        self._reason = ""
        self._attempted = False
        self._create_rejected = False
        self._closing = False
        self._finalized_once = False
        self._stop = asyncio.Event()
        self._ready = asyncio.Event()
        self._joined = asyncio.Event()
        self._creation_done = asyncio.Event()
        self._creation_done.set()
        self._finish_lock = asyncio.Lock()
        self._ws_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._speech_lock = asyncio.Lock()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None
        self._task: asyncio.Task | None = None
        self._recording_task: asyncio.Task | None = None
        self._seq = itertools.count(1)
        self._conn_id = "qwen31-live-" + uuid.uuid4().hex[:12]
        self._created_at: float | None = None
        self._ended_at: float | None = None

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, value):
        previous = getattr(self, "_state", None)
        self._state = value
        if previous != value:
            self.diagnostics.record("local", "state", details={"previous": previous, "state": value})
            if value in self.TERMINAL:
                self.diagnostics.closed = True

    def public(self) -> dict:
        """Safe for UI, logs and receipts: deliberately excludes the RTC object."""
        return {
            "state": self.state, "error": self.error, "live_id": self.live_id,
            "stop_requested": self._stop.is_set(), "stop_reason": self._reason,
            "creation_attempted": self.creation_attempted,
            "creation_outcome_unknown": self.creation_unknown,
            "creation_input_mode": self.creation_input_mode,
            "creation_avatar_id": self.creation_avatar_id,
            "cloud_end_confirmed": self.cloud_end_confirmed,
            "no_cloud_session": not self.creation_attempted or self._create_rejected,
            "rtc_ready": self._ready.is_set(), "stage_joined": self._joined.is_set(),
            "ready": self.state == "ready" and not self._stop.is_set(),
            "busy": self.state in {"sending", "draining"},
            "connected": self._ready.is_set() and self._joined.is_set() and not self._stop.is_set(),
            "elapsed_seconds": round((self._ended_at or time.monotonic()) - self._created_at, 1) if self._created_at else 0,
            "utterances_started": self.utterances_started,
            "utterances_sent": self.utterances_sent,
            "last_utterance": dict(self.last_utterance),
            "idle_action": dict(self.idle_action),
            "baseline_reset": dict(self.baseline_reset),
            "audio_seconds": round(self.audio_seconds, 3),
            "sent_audio_seconds": round(self.sent_audio_bytes / (self.RATE * 2), 3),
            "audio_sent_complete": self.audio_sent_complete,
            "playback_complete_confirmed": False,
            "cloud": dict(self._cloud),
            "recording_saved": self.recording_saved,
            "recording_file": "component-cloud.mp4" if self.recording_saved else None,
            "recording_error": self.recording_error,
            "recording_pending": bool(self._recording_task and not self._recording_task.done()),
        }

    def rtc(self) -> dict:
        """Return only the browser's own short-lived RTC join grant; never log it."""
        return dict(self._rtc) if self.state in {"waiting_stage", "ready", "sending", "draining"} else {}

    def joined(self) -> None:
        # The host calls this only after actual remote audio/video tracks and decoded video frames exist. OBS owns local recording.
        if self.state == "waiting_stage" and not self._stop.is_set():
            self._joined.set()
            self._event("stage_audio_video_decoded_ready")

    async def start(self, api_key: str, *, avatar_id: str | None = None) -> dict:
        if self._attempted or self._closing or self._stop.is_set():
            raise RuntimeError("本轮已启动或已请求停止，不能再次创建会话")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("缺少本次Vidu凭据")
        if avatar_id is not None and (not isinstance(avatar_id, str)
                                      or not re.fullmatch(r"[0-9]{1,64}", avatar_id)):
            raise ValueError("开场形象ID须为1至64位ASCII数字字符串")
        # 两种开场方式都检查本地底图，方便对照来源；asset模式无需编码或上传底图。
        raw = self.image.read_bytes()
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(self.image.suffix.lower())
        if not mime or not raw or len(raw) >= 20_000_000:
            raise ValueError("形象图必须为小于20MB的PNG/JPG/WEBP")
        self.out.mkdir(parents=True, exist_ok=True)
        if any((self.out / name).exists() for name in ("component-cloud.mp4", "component-events.jsonl", "component-outbound.jsonl", "component-receipt.json")):
            raise FileExistsError("本目录已有组件会话记录，请核对后使用新的独立目录")
        body = {"model": "vidu-s2",
                "managed_rtc": {"provider": "artc", "enable_recording": True},
                "action": {"enabled": True}}
        if avatar_id is None:
            body["image_uri"] = f"data:{mime};base64," + base64.b64encode(raw).decode()
        else:
            body["avatar_id"] = avatar_id
        self.creation_input_mode = "avatar_id" if avatar_id is not None else "image_uri"
        self.creation_avatar_id = avatar_id
        self._attempted, self._key = True, api_key.strip()
        self.diagnostics.register_secret(self._key)
        self.state = "starting"
        self._event("start_requested", creation_input_mode=self.creation_input_mode,
                    creation_avatar_id=self.creation_avatar_id)
        self._task = asyncio.create_task(self._run(body), name="component-persistent-session")
        return self.public()

    def _control_allowed(self, still_valid) -> bool:
        # The host callback includes its comment/epoch checks; call it again inside
        # both locks, because a speech turn can arrive while a control frame waits.
        return (still_valid() is True and not self._stop.is_set()
                and self.state == "ready" and self._ready.is_set()
                and self._joined.is_set() and not self._pending_speech)

    async def send_idle_action(self, instruction: str, request_id: str, still_valid) -> dict:
        """Request one type9 action; a sent receipt is not visual confirmation."""
        if not isinstance(instruction, str) or not 1 <= len(instruction.strip()) <= self.MAX_ACTION_CODEPOINTS:
            raise ValueError("待机动作须为1至200字")
        return await self._manual_control(9, request_id, still_valid, instruction.strip())

    async def reset_baseline(self, request_id: str, still_valid) -> dict:
        """Manual type15 fallback only; the protocol has no success acknowledgement."""
        return await self._manual_control(15, request_id, still_valid)

    async def _manual_control(self, kind: int, request_id: str, still_valid, instruction: str = "") -> dict:
        if not isinstance(request_id, str) or not 1 <= len(request_id.strip()) <= 128 or not callable(still_valid):
            raise ValueError("动作请求需要有效的request_id与同步有效性检查")
        request_id = request_id.strip()
        previous = self._action_requests.get(request_id)
        if previous is not None:
            if previous["signal_type"] != kind or previous.get("action_instruction", "") != instruction:
                raise ValueError("同一request_id不能用于不同动作")
            return {**previous, "duplicate": True}
        result = {"request_id": request_id, "signal_type": kind,
                  "source": "application_manual_idle" if kind == 9 else "application_manual_baseline_reset",
                  "attempted": True, "sent": False, "cancelled": False,
                  "status": "pending", "visual_effect_confirmed": False,
                  "success_acknowledged": False}
        if kind == 9:
            result["action_instruction"] = instruction
            self.idle_action = result
        else:
            result["success_ack_available"] = False
            self.baseline_reset = result
        # Reserve before any await so simultaneous duplicate HTTP requests cannot
        # create two frames. A cancelled ID stays consumed and is never retried.
        self._action_requests[request_id] = result

        def cancelled():
            result.update(cancelled=True, status="cancelled")
            self._event("manual_control_cancelled", **result)
            return dict(result)

        try:
            if not self._control_allowed(still_valid) or self._speech_lock.locked():
                return cancelled()
            async with self._speech_lock:
                if not self._control_allowed(still_valid):
                    return cancelled()
                payload = ({"text_msg": {"msg_id": uuid.uuid4().hex[:12], "content": instruction,
                                         "timestamp": int(time.time() * 1000)}} if kind == 9 else None)
                self._event("manual_control_requested", **result)
                sent = await self._send(kind, payload, still_valid=still_valid)
                if not sent:
                    return cancelled()
                result.update(sent=True, status="sent")
                self._event("manual_control_sent", **result)
                return dict(result)
        except _Stopped:
            return cancelled()
        except asyncio.CancelledError:
            # Cancelling a task may interrupt a write already in progress; retain
            # the existing fail-closed lifecycle instead of claiming it was unsent.
            self._fail("手动动作调用被取消")
            raise
        except Exception:
            result["status"] = "send_unconfirmed"
            self._fail("手动动作发送未完成")
            raise RuntimeError("手动动作发送未完成，已请求停止") from None

    async def send_audio(self, pcm: bytes, text: str, action_instruction: str = "") -> dict:
        # Count active and queued sends so an idle action never overtakes speech.
        self._pending_speech += 1
        try:
            return await self._send_audio(pcm, text, action_instruction)
        finally:
            self._pending_speech -= 1

    async def _send_audio(self, pcm: bytes, text: str, action_instruction: str = "") -> dict:
        """Queue one short turn; success means sent plus safety buffer, not heard.

        Validation precedes the serial lock. Concurrent valid calls wait their turn;
        stop() wakes the active send and makes every queued call refuse transmission.
        """
        if not isinstance(pcm, bytes) or not pcm or len(pcm) % 2:
            raise ValueError("音频必须是非空、16位对齐的PCM字节")
        if len(pcm) > self.MAX_AUDIO_SECONDS * self.RATE * 2:
            raise ValueError("每段音频不能超过30秒")
        if not isinstance(text, str) or not text.strip() or len(text) > self.MAX_TEXT_CODEPOINTS:
            raise ValueError("每句台词必须为1至30个Unicode码点，含标点和空格")
        if not isinstance(action_instruction, str) or len(action_instruction) > self.MAX_ACTION_CODEPOINTS:
            raise ValueError("动作描述必须是至多200个Unicode码点的字符串，空串表示不发送动作指令")
        if self._stop.is_set() or self.state not in {"ready", "sending", "draining"}:
            raise RuntimeError("组件尚未接通或已经停止，不能发送")
        async with self._speech_lock:
            try:
                self._check()
                if self.state != "ready" or not self._ready.is_set() or not self._joined.is_set():
                    raise RuntimeError("组件当前不能接收新台词")
                self.utterances_started += 1
                turn_id = self.utterances_started
                self.audio_seconds += len(pcm) / (self.RATE * 2)
                self.audio_sent_complete = False
                self.state = "sending"
                self.last_utterance = {"id": turn_id, "audio_seconds": round(len(pcm) / (self.RATE * 2), 3),
                                       "text_codepoints": len(text), "action_instruction": action_instruction, "send_complete": False,
                                       "buffer_complete": False, "playback_complete_confirmed": False}
                self._event("utterance_started", **self.last_utterance)
                # type9驱动运行时动作匹配；大脑描述原样发送，不套固定预设。
                # 平台能否匹配任意自由动作仍需真实画面验收，不能仅凭发送成功保证。
                if action_instruction:
                    await self._send(9, {"text_msg": {"msg_id": uuid.uuid4().hex[:12], "content": action_instruction, "timestamp": int(time.time() * 1000)}})
                await self._send(10, {"text_msg": {"msg_id": uuid.uuid4().hex[:12], "content": text, "timestamp": int(time.time() * 1000)}})
                await self._audio(pcm, count_audio=True)
                self.audio_sent_complete = True
                self.utterances_sent += 1
                self.last_utterance["send_complete"] = True
                self.state = "draining"
                self._event("utterance_audio_sent", id=turn_id, audio_bytes=len(pcm))
                # This controlled trial stops PCM after the unchanged source audio.
                # The following wait is local only; no zero-PCM or reset is sent.
                await self._wait(self.DRAIN_SECONDS)
                self.last_utterance["buffer_complete"] = True
                self._event("utterance_buffer_complete", id=turn_id)
                self.state = "ready"
                return dict(self.last_utterance)
            except _Stopped:
                self._event("utterance_interrupted")
                raise RuntimeError("本次会话已请求停止，台词发送取消") from None
            except asyncio.CancelledError:
                # A cancelled HTTP caller must not leave a half-sent turn while the
                # paid session silently survives. The background owner performs stop.
                self._fail("台词调用被取消")
                raise
            except Exception as error:
                self._event("utterance_error", error_type=type(error).__name__)
                self._fail("台词发送未完成")
                raise RuntimeError("台词发送未完成，已请求停止同一会话") from None

    def _event(self, event: str, **safe_fields) -> None:
        self.diagnostics.record("local", event, details={"state": self.state, "comment_id": self.comment_id, **safe_fields})
        try:
            self.out.mkdir(parents=True, exist_ok=True)
            with (self.out / "component-events.jsonl").open("a", encoding="utf-8") as target:
                target.write(json.dumps(self.diagnostics.clean({"time": time.time(), "event": event, **safe_fields}), ensure_ascii=False) + "\n")
        except (OSError, UnicodeError):
            self.diagnostics.record("local", "legacy_event_write_failed")

    def _save(self, filename: str, value: dict) -> None:
        self.out.mkdir(parents=True, exist_ok=True)
        # Replace only this module's own evolving receipt, never prior media files.
        scratch = self.out / (filename + ".tmp-" + uuid.uuid4().hex[:8])
        scratch.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        scratch.replace(self.out / filename)

    @classmethod
    def _safe_cloud(cls, live: dict) -> dict:
        result = {}
        for name in cls.CLOUD_FIELDS:
            value = live.get(name)
            if isinstance(value, (int, float, bool)) or value is None:
                if name in live:
                    result[name] = value
            elif isinstance(value, str) and len(value) <= 160 and re.fullmatch(r"[A-Za-z0-9_.: +\-]*", value):
                if not any(marker in value.lower() for marker in ("vda_", "token", "secret", "http")):
                    result[name] = value
        action = live.get("action")
        if isinstance(action, dict):
            # 只保留动作配置的两个安全字段，不把服务端嵌套对象整体写入日志。
            safe_action = {}
            if isinstance(action.get("enabled"), bool):
                safe_action["enabled"] = action["enabled"]
            library_id = action.get("library_id")
            if isinstance(library_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", library_id):
                if not any(marker in library_id.lower() for marker in ("vda_", "token", "secret", "http")):
                    safe_action["library_id"] = library_id
            if safe_action:
                result["action"] = safe_action
        return result

    async def _json(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        async with self.http.request(method, self.BASE + path, json=body,
                                     headers={"Authorization": "Token " + self._key},
                                     timeout=aiohttp.ClientTimeout(total=timeout or self.REQUEST_TIMEOUT)) as response:
            if response.status >= 300:
                # Error bodies and exception strings may contain credentials or signed URLs.
                raise _HTTPError(response.status)
            value = await response.json(content_type=None)
            if not isinstance(value, dict):
                raise ValueError("服务端响应格式不符")
            return value

    def _check(self) -> None:
        if self._stop.is_set():
            raise _Stopped()

    def _fail(self, message: str) -> None:
        # Callers supply only fixed internal messages, never provider error bodies.
        self.error = self.error or message
        if self.state not in self.TERMINAL:
            self.state = "stopping"
        self._stop.set()

    async def _wait(self, seconds: float) -> None:
        self._check()
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.001, seconds))
        except asyncio.TimeoutError:
            return
        raise _Stopped()

    async def _until(self, event: asyncio.Event, timeout: float, message: str) -> None:
        deadline = time.monotonic() + timeout
        while not event.is_set():
            self._check()
            if time.monotonic() >= deadline:
                raise TimeoutError(message)
            await self._wait(min(0.1, deadline - time.monotonic()))
        self._check()

    async def _connect(self) -> None:
        async with self._ws_lock:
            if self._ws is not None and not self._ws.closed:
                return
            url = self.BASE.replace("https://", "wss://").replace("http://", "ws://")
            url += f"/live/v1/external-lives/{self.live_id}/stream?conn_id={self._conn_id}"
            self._ws = await asyncio.wait_for(
                self.http.ws_connect(url, headers={"Authorization": "Token " + self._key},
                                     heartbeat=None, autoping=True,
                                     timeout=aiohttp.ClientWSTimeout(ws_close=5), max_msg_size=16 * 1024 * 1024),
                timeout=15.0,
            )
            # Receiving continuously is necessary: aiohttp answers the server pings here.
            self._reader = asyncio.create_task(self._read(self._ws), name="component-control-reader")
            await self._send(1, {"conn_init": {"version": 1}}, stopping=True)
            if not self._stop.is_set():
                self._keepalive = asyncio.create_task(self._ping(self._ws), name="component-control-ping")

    async def _ping(self, ws) -> None:
        # Proactive ping supplements automatic pong replies, including long waits
        # between viewer comments. No heartbeat timeout that could falsely cut off
        # a busy renderer is enabled; transport/read errors trigger owner shutdown.
        try:
            while not self._stop.is_set() and not ws.closed:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.PING_INTERVAL)
                    return
                except asyncio.TimeoutError:
                    if not self._stop.is_set() and not ws.closed:
                        await ws.ping()
                        self.diagnostics.record("out", "ws_ping_sent")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._event("control_ping_error", error_type=type(error).__name__)
            self._fail("组件保活连接异常")

    async def _send(self, kind: int, payload: dict | None, stopping: bool = False, still_valid=None) -> bool:
        async with self._send_lock:
            # No await may separate this last validity check from the socket write.
            if still_valid is not None and not self._control_allowed(still_valid):
                return False
            if not stopping:
                self._check()
            if self._ws is None or self._ws.closed:
                raise ConnectionError("控制通道未连接")
            frame = {"type": kind, "live_id": self.live_id, "conn_id": self._conn_id,
                     "seq_id": next(self._seq)}
            if payload is not None:
                frame["payload"] = payload
            # Match aiohttp.send_json's default serializer, including Unicode escapes.
            size = len(json.dumps(frame).encode("utf-8"))
            context = {"comment_id": self.comment_id, "utterance_id": self.utterances_started}
            self.diagnostics.frame("out", "attempt", frame, size, **context)
            try:
                await self._ws.send_json(frame)
            except BaseException as error:
                self.diagnostics.frame("out", "failed", frame, size, error_type=type(error).__name__, **context)
                raise
            self.diagnostics.frame("out", "sent", frame, size, **context)
            if kind in (9, 10, 15):
                # A disk failure after send must never suggest that sending failed.
                try:
                    self.out.mkdir(parents=True, exist_ok=True)
                    with (self.out / "component-outbound.jsonl").open("a", encoding="utf-8") as target:
                        target.write(json.dumps(self.diagnostics.clean(frame), ensure_ascii=False) + "\n")
                except (OSError, UnicodeError):
                    self.diagnostics.record("local", "legacy_outbound_write_failed")
            return True

    def _handle_control_ack(self, frame: dict) -> bool:
        """Optional acknowledgements are dispatched by the single reader."""
        return False

    def _handle_control_error(self, frame: dict) -> bool:
        """Only a subclass with a matching outbound attempt may handle an error."""
        return False

    async def _read(self, ws) -> None:
        try:
            async for message in ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    size = len(message.data.encode("utf-8"))
                    try:
                        frame = json.loads(message.data)
                    except ValueError:
                        self.diagnostics.record("in", "ws_malformed_text", details={"frame_bytes": size})
                        raise
                    self.diagnostics.frame("in", "received", frame, size)
                    kind = frame.get("type")
                    if kind == 2:
                        ack = (frame.get("payload") or {}).get("conn_init_ack") or {}
                        if ack.get("success"):
                            self._ready.set()
                        elif ack.get("error_code") != "NOT_READY":
                            self._fail("组件初始化被拒绝")
                    elif kind == 12:
                        self._handle_control_ack(frame)
                    elif kind in (6, 103):
                        self._event("server_signal", signal_type=kind)
                        if kind == 6 or not self._handle_control_error(frame):
                            self._fail("平台结束组件会话" if kind == 6 else "平台返回组件错误")
                else:
                    data = message.data
                    self.diagnostics.record("in", "ws_transport", details={
                        "message_type": int(message.type), "frame_bytes": len(data) if isinstance(data, (str, bytes)) else 0})
                    self.diagnostics.counts["in_total"] += 1
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._event("control_reader_error", error_type=type(error).__name__)
            if not self._stop.is_set():
                self._fail("组件控制连接异常")
        finally:
            if not self._stop.is_set():
                self._fail("组件控制连接已断开")

    async def _run(self, body: dict) -> None:
        try:
            self._check()
            self.state = "creating"
            self._creation_done.clear()
            self.creation_attempted = True
            self.creation_unknown = True
            self._event("create_submitting")
            try:
                # Exactly one POST. Never retry an ambiguous creation response.
                created = await self._json("POST", "/live/s_avatar/component", body)
                live = created.get("live") or {}
                if not live.get("id") or not re.fullmatch(r"[A-Za-z0-9_-]+", str(live["id"])):
                    raise ValueError("创建响应缺少会话ID")
                self.live_id = str(live["id"])
                self._created_at = time.monotonic()
                self.creation_unknown = False
                self._cloud = self._safe_cloud(live)
                source = created.get("managed_rtc") or {}
                self._rtc = {name: source[name] for name in ("provider", "app_id", "channel_id", "user_id", "token", "token_expire_at") if name in source}
                self.diagnostics.register_secret(self._rtc.get("token"))
                self._event("created", live_id=self.live_id)
                self._save("component-cloud-status.json", self._cloud)
            except _HTTPError as error:
                if 400 <= error.status < 500:
                    self.creation_unknown = False
                    self._create_rejected = True
                raise
            finally:
                self._creation_done.set()
            self._check()  # A stop during POST must never proceed into audio or RTC.
            if not self._rtc.get("token") or not self._rtc.get("user_id"):
                raise ValueError("组件响应缺少RTC入会信息")
            self.state = "connecting"
            await self._connect()
            deadline = time.monotonic() + self.READY_TIMEOUT
            while not self._ready.is_set():
                self._check()
                if time.monotonic() >= deadline:
                    raise TimeoutError("组件渲染未及时就绪")
                try:
                    await self._until(self._ready, min(3.0, deadline - time.monotonic()), "继续等待初始化")
                except TimeoutError:
                    await self._send(1, {"conn_init": {"version": 1}})
            self._check()
            self.state = "waiting_stage"
            self._event("renderer_ready")
            await self._until(self._joined, self.JOIN_TIMEOUT, "浏览器音画和录像未及时就绪")
            self.state = "ready"
            self._event("session_ready")
            # Keep exactly this live running until manual stop or a genuine failure.
            # The host separately monitors browser heartbeats; there is no total
            # time/credit cutoff, autonomous speech, warm-up or automatic restart.
            await self._stop.wait()
        except _Stopped:
            pass
        except asyncio.CancelledError:
            self.error = self.error or "组件后台任务被取消，需核对云端状态"
            raise
        except Exception as error:
            self.error = "组件试播未完成：" + type(error).__name__
            self._event("trial_error", error_type=type(error).__name__, **({"http_status": error.status} if isinstance(error, _HTTPError) else {}))
        finally:
            self._creation_done.set()
            self._stop.set()
            await self._finish()

    async def _audio(self, pcm: bytes, count_audio: bool, *, on_frame_sent=None) -> None:
        zero, sent = time.monotonic(), 0
        context = {"comment_id": self.comment_id, "utterance_id": self.utterances_started,
                   "audio_kind": "speech" if count_audio else "tail_silence",
                   "planned_bytes": len(pcm), "planned_seconds": round(len(pcm)/(self.RATE*2), 3)}
        self.diagnostics.record("out", "audio_start", details=context)
        try:
            for offset in range(0, len(pcm), self.FRAME_BYTES):
                self._check()
                chunk = pcm[offset:offset + self.FRAME_BYTES]
                async with self._send_lock:
                    self._check()
                    if self._ws is None or self._ws.closed:
                        raise ConnectionError("音频发送连接已关闭")
                    await self._ws.send_bytes(chunk)
                    # 仅报告本地写入成功的时钟；它不能证明云端已经播放。
                    sent_at = time.monotonic() if on_frame_sent is not None else None
                    sent += len(chunk)
                    if on_frame_sent is not None:
                        on_frame_sent(sent, sent_at)
                    self.diagnostics.counts["audio_bytes"] += len(chunk)
                    if count_audio:
                        self.sent_audio_bytes += len(chunk)
                await self._wait(max(0.001, zero + (offset + len(chunk)) / (self.RATE * 2) - time.monotonic()))
        except BaseException as error:
            self.diagnostics.record("out", "audio_interrupted", details={**context, "sent_bytes": sent,
                "sent_seconds": round(sent/(self.RATE*2), 3), "error_type": type(error).__name__})
            raise
        else:
            self.diagnostics.record("out", "audio_end", details={**context, "sent_bytes": sent,
                "sent_seconds": round(sent/(self.RATE*2), 3)})

    def latch_stop(self) -> None:
        """Synchronously block all writes before the HTTP handler schedules cleanup."""
        first = not self._stop.is_set()
        self._stop.set()
        self._reason = "本机请求停止"  # Caller text is not trusted with keys or URLs.
        if self.state not in self.TERMINAL:
            self.state = "stopping"
        if first:
            self._event("stop_requested")

    async def stop(self, reason: str = "用户停止") -> dict:
        # A stop before the owner task begins prevents even session creation.
        self.latch_stop()
        # Do not cancel an in-flight POST: retain its returned ID so it can be ended.
        await self._creation_done.wait()
        await self._finish(force=True)
        return self.public()

    async def _close_ws(self) -> None:
        if self._ws is not None and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._keepalive and self._keepalive is not asyncio.current_task() and not self._keepalive.done():
            self._keepalive.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._keepalive
        if self._reader and self._reader is not asyncio.current_task() and not self._reader.done():
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader

    async def _finish(self, force: bool = False) -> None:
        async with self._finish_lock:
            if self._finalized_once and not force:
                return
            self._finalized_once = True
            if not self.live_id:
                self.state = "creation_unknown" if self.creation_unknown else ("error" if self.error else "ended")
                self._save("component-receipt.json", self.public())
                return
            if not self.cloud_end_confirmed:
                self.state = "stopping"
                try:
                    await self._connect()  # Reconnect only to this same live to hang it up.
                    await self._send(5, {"hangup": {"hangup_reason": "client_hangup"}}, stopping=True)
                    self._event("hangup_sent", live_id=self.live_id)
                except Exception as error:
                    self._event("hangup_send_error", error_type=type(error).__name__)
                finally:
                    await self._close_ws()  # Official protocol: WS close is also hangup.
                deadline = time.monotonic() + self.END_TIMEOUT
                while time.monotonic() < deadline:
                    try:
                        response = await self._json("GET", f"/live/v1/lives/{self.live_id}", timeout=min(6.0, max(0.1, deadline - time.monotonic())))
                        self._cloud = self._safe_cloud(response.get("live") or {})
                        self._save("component-cloud-status.json", self._cloud)
                        status = str(self._cloud.get("status", self._cloud.get("state", ""))).lower()
                        if status == "ended":
                            self.cloud_end_confirmed = True
                            self._ended_at = time.monotonic()
                            break
                    except Exception as error:
                        self._event("end_readback_error", error_type=type(error).__name__)
                    await asyncio.sleep(min(self.POLL_INTERVAL, max(0, deadline - time.monotonic())))
            self.state = "ended" if self.cloud_end_confirmed else "stop_unconfirmed"
            self._event("stop_result", live_id=self.live_id, cloud_end_confirmed=self.cloud_end_confirmed)
            if self.cloud_end_confirmed and self._recording_task is None:
                self._recording_task = asyncio.create_task(self._collect_recording(), name="component-recording")
            self._save("component-receipt.json", self.public())

    async def _collect_recording(self) -> None:
        target = self.out / "component-cloud.mp4"
        for attempt in range(self.RECORDING_ATTEMPTS):
            try:
                result = await self._json("GET", f"/live/v1/lives/{self.live_id}/recording", timeout=10)
                recording = result.get("recording") or {}
                url = recording.get("video_url")
                if url:
                    if target.exists():
                        raise FileExistsError("录像目标已存在")
                    partial = self.out / ("component-cloud.mp4.part-" + uuid.uuid4().hex[:8])
                    # Download without Vidu authorization headers: the URL has its own
                    # short-lived signature and may point at another storage host.
                    async with self.http.get(url, timeout=aiohttp.ClientTimeout(total=90, sock_connect=15, sock_read=30)) as response:
                        response.raise_for_status()
                        with partial.open("xb") as handle:
                            async for chunk in response.content.iter_chunked(1 << 20):
                                handle.write(chunk)
                    partial.rename(target)
                    self.recording_saved = True
                    self._event("recording_saved", file=target.name, bytes=target.stat().st_size)
                    break
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._event("recording_wait", attempt=attempt + 1, error_type=type(error).__name__)
            if attempt + 1 < self.RECORDING_ATTEMPTS:
                await asyncio.sleep(self.RECORDING_INTERVAL)
        if not self.recording_saved:
            self.recording_error = "官方录像尚未取回，可用同一会话ID再次查询"
        # Billing may settle after ended and recording creation; save a later receipt.
        try:
            result = await self._json("GET", f"/live/v1/lives/{self.live_id}", timeout=10)
            self._cloud = self._safe_cloud(result.get("live") or {})
            self._save("component-cloud-status.json", self._cloud)
        except Exception as error:
            self._event("final_billing_readback_error", error_type=type(error).__name__)
        self._save("component-receipt.json", {**self.public(), "recording_pending": False})

    async def close(self) -> None:
        self._closing = True
        await self.stop("服务关闭")
        if self._task and self._task is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._recording_task:
            with contextlib.suppress(asyncio.CancelledError):
                await self._recording_task
        await self._close_ws()
        self._rtc.clear()
        self._key = ""
