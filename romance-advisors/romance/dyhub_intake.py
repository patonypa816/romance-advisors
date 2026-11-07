"""Explicit local DyHub subscription, scoped to an episode or broadcast. No image downloads or LLMs.

Only the documented DanmakuUser.avatar field is mapped. The upstream collector
is a separate dependency; starting this subscription does not install it.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
import hashlib
import importlib.util
import math
from pathlib import Path
import time

from aiohttp import web

from .game import vote_number
from .viewer_avatar import safe_avatar_url

MAX_EPISODE_COMMENTS = 5000
MAX_EVENT_AGE_SECONDS = 45
MAX_SEEN_EVENTS = 10000
MAX_RECENT_COMMENTS = 40
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
    def __init__(self, workbench, *, client_factory=None, clock=time.time,
                 on_simulated_gift=None):
        self.workbench = workbench
        self.client_factory = client_factory
        self.clock = clock
        # Only an explicitly injected offline harness gets this branch. The
        # real client strips simulated markers and uses a separate durable ledger.
        self.on_simulated_gift = on_simulated_gift
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
        self.scope = "episode"
        self.live_broadcast = None
        self.auto_source = None
        self.seen = OrderedDict()
        self.recent_comments = deque(maxlen=MAX_RECENT_COMMENTS)

    @property
    def active(self):
        return bool(self.task and not self.task.done())

    def snapshot(self):
        # Operator-only source endpoint: bounded plain-text non-game comments.
        # Never expose raw source/user IDs, avatars or exception details.
        value = {"active": self.active, "state": self.state, "error_code": self.error_code,
                "base_url": self.base_url, "room_id": self.room_id,
                "counts": dict(self.counts), "episode_count": self.episode_count,
                "episode_limit": MAX_EPISODE_COMMENTS,
                "last_event_received_at": self.last_event_received_at,
                "reconnect_count": self.reconnect_count, "scope": self.scope,
                "recent_comments": [dict(row) for row in self.recent_comments]}
        if self.auto_source:
            value['auto'] = self.auto_source.snapshot()
        return value

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

    async def start(self, base_url, room_id, scope="episode"):
        async with self.lock:
            if not isinstance(scope, str) or scope not in {"episode", "broadcast"}:
                raise ValueError("invalid_source_settings")
            if self.active:
                if (base_url, room_id, scope) != (self.base_url, self.room_id, self.scope):
                    raise ValueError("source_already_running")
                return self.snapshot()
            episode = self.workbench.game.episode
            if scope == "episode" and (not episode or episode["status"] != "ACTIVE"):
                raise ValueError("requires_active_episode")
            factory = self.client_factory or _client_class()
            gifts_enabled = getattr(self.workbench, "appearance_gifts_enabled", lambda room: False)(room_id)
            appearance = getattr(self.workbench, "appearance", None)
            probe = getattr(appearance, "gift_probe", None)
            probe_enabled = bool(probe and probe.eligible(appearance.service.settings(), room_id))
            client = factory(base_url, room_id, self.on_event, self.on_status,
                             avatar_validator=safe_avatar_url,
                             event_types=("chat", "gift") if gifts_enabled or probe_enabled else ("chat",))
            self.base_url, self.room_id = client.base_url, client.room_id
            if self.live_broadcast and (scope != 'broadcast' or self.live_broadcast['room_id'] != self.room_id):
                self.live_broadcast = None
            self.client = client
            self.scope = scope
            self.episode_id = episode["id"] if scope == "episode" else None
            self.recent_comments.clear()
            self.episode_count = sum(row["episode_id"] == self.episode_id
                                     and row["id"].startswith("dyhub:message:")
                                     for row in self.workbench.game.data["comments"])
            if scope == "episode" and self.episode_count >= MAX_EPISODE_COMMENTS:
                raise ValueError("episode_limit")
            self.counts = {"accepted": 0, "duplicate": 0, "ignored": 0}
            self.state, self.error_code = "connecting", None
            self.last_event_received_at, self.reconnect_count = None, 0
            self.started_at = self.clock()
            if probe:
                if probe_enabled:
                    probe.begin(self.room_id, started_at=self.started_at)
                else:
                    probe.stop()
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
        watcher = asyncio.create_task(self._watch_episode()) if self.scope == "episode" else None
        try:
            tasks = (source, watcher) if watcher else (source,)
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except asyncio.CancelledError:
            raise
        except Exception:
            self.error_code = "source_failed"
        finally:
            self.stop_event.set()
            await self.client.close()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.state = "stopped"
            probe = getattr(getattr(self.workbench, "appearance", None), "gift_probe", None)
            if probe:
                probe.stop()

    async def stop(self):
        async with self.lock:
            if self.stop_event:
                self.stop_event.set()
            if self.task:
                await self.task
            self.state = "stopped"
            probe = getattr(getattr(self.workbench, "appearance", None), "gift_probe", None)
            if probe:
                probe.stop()
            return self.snapshot()

    async def on_event(self, event):
        # This callback deliberately has no await before the game commits. A
        # phase cannot change between capturing its context and accepting it.
        if (self.scope == "episode" and not self._episode_active()) or not self.stop_event or self.stop_event.is_set():
            return
        game = self.workbench.game
        if (self.scope == "episode" and event.get("type") == "gift" and event.get("simulated") is True
                and self.on_simulated_gift is not None
                and event.get("platform") == "douyin" and event.get("roomId") == self.room_id):
            return self.on_simulated_gift(event)
        if event.get("type") == "gift":
            probe = getattr(getattr(self.workbench, "appearance", None), "gift_probe", None)
            if probe and probe.active:
                # A real source remains real: this bypass has its own in-memory
                # counters and never enters formal scoring, storage or rendering.
                return probe.apply(event)
            # This is a separate receipt path. Gifts never enter comment inputs,
            # advice compilation, votes, or the optional conversation worker.
            handler = getattr(self.workbench, "accept_appearance_gift", None)
            program = getattr(self.workbench, "program", None)
            episode = game.episode
            active = bool(episode and episode["status"] == "ACTIVE")
            if getattr(program, "switchable", False):
                active = (active and program.state == "running" and program.ready
                          and program.content_mode == "game"
                          and getattr(program, "game_episode_id", None) == episode["id"])
            elif program is not None:
                active = (active and not getattr(program, "sender_failed", False)
                          and getattr(program, "content_mode", None) not in {"to_game", "to_chat"}
                          and (program.state == "idle" or
                               (program.state == "running" and program.ready)))
            now = self.clock()
            times = [event.get(key) for key in ("ts", "receivedAt")]
            fresh = (all(type(value) in (int, float) and math.isfinite(value)
                         and 0 < value <= (now + 5) * 1000 for value in times)
                     and min(times) / 1000 >= max(now - MAX_EVENT_AGE_SECONDS, self.started_at)
                     and times[0] <= times[1] + 5000)
            if (handler is None or not active or not fresh or event.get("simulated") is True
                    or event.get("platform") != "douyin" or event.get("roomId") != self.room_id
                    or event.get("episodeId", episode["id"] if episode else None) != (episode or {}).get("id")):
                self.counts["ignored"] += 1
                return
            receipt = handler(event)
            counter = "gift_accepted" if isinstance(receipt, dict) and receipt.get("accepted") else "gift_ignored"
            self.counts[counter] = self.counts.get(counter, 0) + 1
            return receipt
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
        if event_id in self.seen or game._get("comments", event_id):
            self.counts["duplicate"] += 1
            return
        now = self.clock()
        times = [event.get(key) for key in ("ts", "receivedAt")]
        if any(type(value) not in (int, float) or not math.isfinite(value)
               or not 0 < value <= (now + 5) * 1000 for value in times):
            self.counts["ignored"] += 1
            return
        if (min(times) / 1000 < max(now - MAX_EVENT_AGE_SECONDS, self.started_at - (5 if self.scope == "episode" else 0))
                or times[0] > times[1] + 5000):
            self.counts["ignored"] += 1
            return
        text = data.get("content")
        round_ = game.current_round
        phase = round_["phase"] if round_ else None
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            self.counts["ignored"] += 1
            return
        # Retain IDs for the entire admissible age horizon, across reconnects
        # and mode switches. At capacity, reject new arrivals rather than evict
        # a still-replayable ID and accidentally feed it to a different brain.
        while self.seen and next(iter(self.seen.values())) < now - MAX_EVENT_AGE_SECONDS - 10:
            self.seen.popitem(last=False)
        if len(self.seen) >= MAX_SEEN_EVENTS:
            self.counts["ignored"] += 1
            return
        self.seen[event_id] = now
        nickname = user.get("nickname")
        nickname = nickname.strip()[:256] if isinstance(nickname, str) else ""
        nickname = nickname or "观众"
        self.counts["received"] = self.counts.get("received", 0) + 1
        program = self.workbench.program
        episode = game.episode
        game_active = bool(episode and episode["status"] == "ACTIVE")
        if (getattr(program, "content_mode", None) in {"to_game", "to_chat"}
                or getattr(program, "sender_failed", False)):
            route_game = route_chat = False
        elif getattr(program, "switchable", False):
            # The stable episode binding excludes the previous ACTIVE game
            # while a new game is being prepared, and every transition window.
            route_game = (program.state == "running" and program.ready
                          and program.content_mode == "game" and game_active
                          and getattr(program, "game_episode_id", None) == episode["id"])
            route_chat = (program.state == "running" and program.ready
                          and program.content_mode == "chat")
        else:
            route_game = game_active and (program.state == "idle" or
                                         (program.state == "running" and program.ready))
            route_chat = False
        if self.scope == "broadcast" and route_game and self.episode_id != episode["id"]:
            self.episode_id = episode["id"]
            self.episode_count = sum(row["episode_id"] == self.episode_id
                                     and row["id"].startswith("dyhub:message:")
                                     for row in game.data["comments"])
        if not route_game or self.episode_count >= MAX_EPISODE_COMMENTS:
            if self.scope == "episode" and self.episode_count >= MAX_EPISODE_COMMENTS:
                self.error_code = "episode_limit"
                self.stop_event.set()
                return
            if self.scope == "broadcast":
                self.recent_comments.append({"id": event_id, "nickname": nickname, "text": text})
                if route_chat:
                    program.casual.offer({"id": event_id, "nickname": nickname, "text": text})
                else:
                    self.counts["ignored"] += 1
            return
        payload = {"event_id": event_id, "author_id": source_identity("viewer", self.room_id, user["id"]),
                   "nickname": nickname or "观众", "text": text,
                   "expected_round_id": round_["id"] if round_ else None, "expected_phase": phase}
        # Only suggestion participants keep an optional picture, never everyone
        # entering the room, gifts, closed windows or vote-only spectators.
        if (phase == "COLLECTING" and vote_number(text) is None and not self.workbench.input_blocked()
                and (avatar := safe_avatar_url(user.get("avatar")))):
            payload["avatar_url"] = avatar
        # Keep the existing contract: ordinary interaction is independent of
        # suggestion/vote eligibility. Closed-window/busy chat still gets a
        # receipt and may be used by the existing optional interaction worker.
        self.workbench.accept_comment(payload, source="dyhub", live_broadcast=self.live_broadcast)
        self.episode_count += 1
        self.counts["accepted"] += 1
        if self.scope == "episode" and self.episode_count >= MAX_EPISODE_COMMENTS:
            self.error_code = "episode_limit"
            self.stop_event.set()


def register_dyhub_routes(app, workbench):
    from .auto_source import AutoSource
    intake = DyhubIntake(workbench)
    auto = AutoSource(workbench, intake)
    intake.auto_source = auto
    app["dyhub_intake"] = intake
    app['auto_source'] = auto

    async def status(request):
        return web.json_response(intake.snapshot())

    async def mutation(request):
        from .model import strict_json
        try:
            body = strict_json(await request.text())
            if request.path.endswith('/auto'):
                if set(body) != {'base_url', 'room_id'}:
                    raise ValueError('invalid_source_settings')
                result = await auto.configure(body['base_url'], body['room_id'])
            elif request.path.endswith("/start"):
                if set(body) not in ({"base_url", "room_id"}, {"base_url", "room_id", "scope"}):
                    raise ValueError("invalid_source_settings")
                if auto.enabled:
                    if body.get('scope', 'episode') != 'broadcast':
                        raise ValueError('source_already_running')
                    result = await auto.configure(body['base_url'], body['room_id'])
                else:
                    result = await intake.start(body["base_url"], body["room_id"], body.get("scope", "episode"))
            else:
                if body:
                    raise ValueError("invalid_source_settings")
                result = await auto.pause() if auto.enabled else await intake.stop()
        except ValueError as exc:
            code = str(exc)
            messages = {"requires_active_episode": "请先开始一期，再连接本机评论来源。",
                        "source_already_running": "请先停止接收，再更换来源。",
                        "source_room_in_use": "当前房间仍有在播数字人，请先结束数字人并核对停场结果，再更换房间。",
                        "episode_limit": "本期评论接收已达到安全上限，请停止并检查流量。"}
            return web.json_response({"error": messages.get(code, "请检查本机地址和纯数字房间号。")}, status=409)
        except (OSError, ImportError, AttributeError):
            return web.json_response({"error": "本地采集客户端文件不完整，请保留两个项目的同级目录结构。"}, status=503)
        return web.json_response(result)

    async def shutdown(app):
        await auto.close()

    async def startup(app):
        auto.launch()

    app.router.add_get("/api/source", status)
    app.router.add_post("/api/source/start", mutation)
    app.router.add_post('/api/source/auto', mutation)
    app.router.add_post("/api/source/stop", mutation)
    app.on_shutdown.append(shutdown)
    app.on_startup.append(startup)
