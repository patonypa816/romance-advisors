"""Explicit, episode-scoped local DyHub subscription. No image downloads or LLMs.

Only the documented DanmakuUser.avatar field is mapped. The upstream collector
is a separate dependency; starting this subscription does not install it.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import math
from pathlib import Path
import re
import time

from aiohttp import web

from .viewer_avatar import safe_avatar_url

MAX_EPISODE_COMMENTS = 5000
MAX_EVENT_AGE_SECONDS = 45
_CLIENT_CLASS = None


def _client_class():
    global _CLIENT_CLASS
    if _CLIENT_CLASS is None:
        path = Path(__file__).resolve().parents[2] / "ai-live-avatar/studio/dyhub_client.py"
        spec = importlib.util.spec_from_file_location("romance_dyhub_client", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _CLIENT_CLASS = module.DyhubClient
    return _CLIENT_CLASS


def source_identity(kind, room, value):
    """Stable, room-scoped identity; never merge viewers by nickname."""
    digest = hashlib.sha256((room + "\0" + value).encode("utf-8")).hexdigest()
    return "dyhub:" + kind + ":" + digest


class DyhubIntake:
    def __init__(self, workbench, *, client_factory=None, clock=time.time):
        self.workbench = workbench
        self.client_factory = client_factory
        self.clock = clock
        self.lock = asyncio.Lock()
        self.client = None
        self.task = None
        self.stop_event = None
        self.episode_id = None
        self.base_url = "http://127.0.0.1:8757"
        self.room_id = ""
        self.counts = {"accepted": 0, "duplicate": 0, "ignored": 0}
        self.state = "stopped"
        self.error_code = None
        self.last_event_received_at = None
        self.reconnect_count = 0
        self.started_at = 0
        self.episode_count = 0

    @property
    def active(self):
        return bool(self.task and not self.task.done())

    def snapshot(self):
        # No event payload, nickname, raw user ID, avatar URL or exception text.
        return {"active": self.active, "state": self.state, "error_code": self.error_code,
                "base_url": self.base_url, "room_id": self.room_id,
                "counts": dict(self.counts), "episode_count": self.episode_count,
                "episode_limit": MAX_EPISODE_COMMENTS,
                "last_event_received_at": self.last_event_received_at,
                "reconnect_count": self.reconnect_count}

    def on_status(self, status):
        if self.error_code in {"episode_limit", "episode_ended", "source_failed"}:
            return
        self.state = status["state"]
        self.error_code = status["error_code"]
        self.last_event_received_at = status["last_event_received_at"]
        self.reconnect_count = status["reconnect_count"]

    def _episode_active(self):
        episode = self.workbench.game.episode
        return bool(episode and episode["id"] == self.episode_id and episode["status"] == "ACTIVE")

    async def start(self, base_url, room_id):
        async with self.lock:
            if self.active:
                if (base_url, room_id) != (self.base_url, self.room_id):
                    raise ValueError("source_already_running")
                return self.snapshot()
            episode = self.workbench.game.episode
            if not episode or episode["status"] != "ACTIVE":
                raise ValueError("requires_active_episode")
            factory = self.client_factory or _client_class()
            client = factory(base_url, room_id, self.on_event, self.on_status,
                             avatar_validator=safe_avatar_url, event_types=("chat",))
            self.base_url, self.room_id = client.base_url, client.room_id
            self.client = client
            self.episode_id = episode["id"]
            self.episode_count = sum(row["episode_id"] == self.episode_id
                                     and row["id"].startswith("dyhub:message:")
                                     for row in self.workbench.game.data["comments"])
            if self.episode_count >= MAX_EPISODE_COMMENTS:
                raise ValueError("episode_limit")
            self.counts = {"accepted": 0, "duplicate": 0, "ignored": 0}
            self.state, self.error_code = "connecting", None
            self.last_event_received_at, self.reconnect_count = None, 0
            self.started_at = self.clock()
            self.stop_event = asyncio.Event()
            self.task = asyncio.create_task(self._run())
            return self.snapshot()

    async def _watch_episode(self):
        while not self.stop_event.is_set():
            if not self._episode_active():
                self.error_code = "episode_ended"
                self.stop_event.set()
                return
            try:
                await asyncio.wait_for(self.stop_event.wait(), .2)
            except asyncio.TimeoutError:
                pass

    async def _run(self):
        source = asyncio.create_task(self.client.run(self.stop_event))
        watcher = asyncio.create_task(self._watch_episode())
        try:
            done, _ = await asyncio.wait((source, watcher), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except asyncio.CancelledError:
            raise
        except Exception:
            self.error_code = "source_failed"
        finally:
            self.stop_event.set()
            await self.client.close()
            for task in (source, watcher):
                task.cancel()
            await asyncio.gather(source, watcher, return_exceptions=True)
            self.state = "stopped"

    async def stop(self):
        async with self.lock:
            if self.stop_event:
                self.stop_event.set()
            if self.task:
                await self.task
            self.state = "stopped"
            return self.snapshot()

    async def on_event(self, event):
        # This callback deliberately has no await before the game commits. A
        # phase cannot change between capturing its context and accepting it.
        if not self._episode_active() or self.stop_event.is_set():
            return
        game = self.workbench.game
        user, data = event.get("user"), event.get("data")
        if (event.get("type") != "chat" or event.get("platform") != "douyin"
                or event.get("roomId") != self.room_id or not isinstance(user, dict)
                or not isinstance(data, dict)):
            self.counts["ignored"] += 1
            return
        values = (event.get("id"), user.get("id"))
        if any(not isinstance(value, str) or not value.strip() or len(value) > 256 for value in values):
            self.counts["ignored"] += 1
            return
        event_id = source_identity("message", self.room_id, event["id"])
        # Persistent receipt IDs suppress reconnect/restart replay. Never turn a
        # redelivered old suggestion into a vote/new-round suggestion.
        if game._get("comments", event_id):
            self.counts["duplicate"] += 1
            return
        now = self.clock()
        times = [event.get(key) for key in ("ts", "receivedAt")]
        if any(type(value) not in (int, float) or not math.isfinite(value)
               or not 0 < value <= (now + 5) * 1000 for value in times):
            self.counts["ignored"] += 1
            return
        if (min(times) / 1000 < max(now - MAX_EVENT_AGE_SECONDS, self.started_at - 5)
                or times[0] > times[1] + 5000):
            self.counts["ignored"] += 1
            return
        text = data.get("content")
        round_ = game.current_round
        phase = round_["phase"] if round_ else None
        vote = re.fullmatch(r"投([1-4])", text.strip()) if isinstance(text, str) else None
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            self.counts["ignored"] += 1
            return
        if self.episode_count >= MAX_EPISODE_COMMENTS:
            self.error_code = "episode_limit"
            self.stop_event.set()
            return
        nickname = user.get("nickname")
        nickname = nickname.strip()[:256] if isinstance(nickname, str) else ""
        payload = {"event_id": event_id, "author_id": source_identity("viewer", self.room_id, user["id"]),
                   "nickname": nickname or "观众", "text": text,
                   "expected_round_id": round_["id"] if round_ else None, "expected_phase": phase}
        # Only suggestion participants keep an optional picture, never everyone
        # entering the room, gifts, closed windows or vote-only spectators.
        if (phase == "COLLECTING" and not vote and not self.workbench.input_blocked()
                and (avatar := safe_avatar_url(user.get("avatar")))):
            payload["avatar_url"] = avatar
        # Keep the existing contract: ordinary interaction is independent of
        # suggestion/vote eligibility. Closed-window/busy chat still gets a
        # receipt and may be used by the existing optional interaction worker.
        self.workbench.accept_comment(payload)
        self.episode_count += 1
        self.counts["accepted"] += 1
        if self.episode_count >= MAX_EPISODE_COMMENTS:
            self.error_code = "episode_limit"
            self.stop_event.set()


def register_dyhub_routes(app, workbench):
    intake = DyhubIntake(workbench)
    app["dyhub_intake"] = intake

    async def status(request):
        return web.json_response(intake.snapshot())

    async def mutation(request):
        from .model import strict_json
        try:
            body = strict_json(await request.text())
            if request.path.endswith("/start"):
                if set(body) != {"base_url", "room_id"}:
                    raise ValueError("invalid_source_settings")
                result = await intake.start(body["base_url"], body["room_id"])
            else:
                if body:
                    raise ValueError("invalid_source_settings")
                result = await intake.stop()
        except ValueError as exc:
            code = str(exc)
            messages = {"requires_active_episode": "请先开始一期，再连接本机评论来源。",
                        "source_already_running": "请先停止接收，再更换来源。",
                        "episode_limit": "本期评论接收已达到安全上限，请停止并检查流量。"}
            return web.json_response({"error": messages.get(code, "请检查本机地址和纯数字房间号。")}, status=409)
        except (OSError, ImportError, AttributeError):
            return web.json_response({"error": "本地采集客户端文件不完整，请保留两个项目的同级目录结构。"}, status=503)
        return web.json_response(result)

    async def shutdown(app):
        await intake.stop()

    app.router.add_get("/api/source", status)
    app.router.add_post("/api/source/start", mutation)
    app.router.add_post("/api/source/stop", mutation)
    app.on_shutdown.append(shutdown)
