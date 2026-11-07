"""Loopback-only game workbench, transparent overlay and bounded round automation."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import inspect
import json
import os
import signal
import socket
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web

from .game import Game
from .model import QwenModel, strict_json
from .program import Program
from .viewer_avatar import AVATAR_CDN_SUFFIXES
from .dyhub_intake import register_dyhub_routes
from .appearance_workbench import AppearanceWorkbench, register_appearance_routes
from .appearance import live_gift_readiness

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "romance-advisors"
DEFAULT_LAYOUT = {"top": 336, "right": 48, "bottom": 312, "left": 48,
                  "avatarX": 552, "avatarY": 360, "avatarWidth": 480}


def error_text(code):
    messages = {
        "operation_in_progress": "正在生成，请稍候；需要中断可点结束本期。",
        "invalid_option": "这个选项不属于当前投票，请刷新后重选。",
        "invalid_proposal": "请填写1—2000字的建议。",
        "invalid_author": "请先选择或添加一位模拟观众。",
        "invalid_nickname": "请填写有效昵称。",
        "invalid_comment": "请填写1—2000字的评论。",
        "invalid_comment_id": "评论标识无效，请重新发送。",
        "invalid_comment_context": "评论缺少发送时的回合信息，请刷新页面。",
        "invalid_comment_conflict": "这条评论的重试内容已改变，请作为新评论发送。",
        "input_window_closed": "本轮收集窗口已截止，请等待下一轮。",
        "requires_collecting": "当前没有开放建议征集。",
        "requires_voting": "当前没有开放投票。",
        "requires_active_episode": "请先开始一期故事。",
        "requires_finished_round": "当前回合尚未完成。",
        "cleanup_failed": "停止过程中有步骤未能确认；其余清理已尝试，请检查运行状态。",
        "invalid_program_mode": "直播模式无效，请刷新后重试。",
        "invalid_program_run": "本次直播标识已失效，请刷新后重试。",
        "game_mode_required": "当前未开启游戏，模拟评论不会计入旧故事。",
    }
    return messages.get(code, "当前操作未完成，请刷新状态后重试。")


class Workbench:
    def __init__(self, db_path: Path | str, model=None, avatar_client=None, *, obs_output=False):
        self.obs_output = obs_output
        self.model = model or QwenModel()
        self.game = Game(db_path, self.model)
        self.settings_path = Path(db_path).with_suffix(".settings.json") if str(db_path) != ":memory:" else None
        self.settings = {"auto_advance": False, "collect_seconds": 45, "vote_seconds": 30}
        if self.settings_path and self.settings_path.exists():
            try:
                saved = strict_json(self.settings_path.read_text())
                for key in ("collect_seconds", "vote_seconds"):
                    if type(saved.get(key)) is int and 5 <= saved[key] <= 600:
                        self.settings[key] = saved[key]
            except (ValueError, OSError):
                pass
        # Restart restores the game but never silently restarts paid generation.
        self.job = None
        self.scheduler = None
        self.error = None
        self.cleanup = None
        self.event_loop_delay = {"samples": 0, "last_seconds": None, "max_seconds": 0.0}
        self.auto_started = None
        self.automation_note = None
        interrupted = ((self.game.episode or {}).get("status") == "PREPARING" or
                       (self.game.current_round or {}).get("phase") in ("SELECTING", "DECIDING", "DRAFTING", "WAITING_REPLY"))
        if interrupted and not self.game.data.get("error"):
            self.error = "上次生成在中途停止，已有进度保留。点击重试当前步骤继续。"
        self.clock_key = None
        self.deadline = None
        self.layout = dict(DEFAULT_LAYOUT)
        self.layout_path = Path(db_path).with_suffix(".layout.json") if str(db_path) != ":memory:" else None
        if self.layout_path and self.layout_path.exists():
            try:
                self.layout = normalize_layout(strict_json(self.layout_path.read_text()))
            except (ValueError, OSError):
                pass
        self.program = Program(self, client=avatar_client)
        self.game.performer = self.program
        self.appearance = AppearanceWorkbench(self)

    def appearance_requested(self):
        config = self.appearance.service.settings()
        return live_gift_readiness(config)["live_gift_mode"] in {"verified", "one_diamond_test"}

    def appearance_gifts_enabled(self, room_id):
        config = self.appearance.service.settings()
        return self.appearance_requested() and config["room_id"] == room_id

    def appearance_episode_started(self, episode_id):
        self.appearance.service.begin_episode(episode_id, owner_id=self.program.run_id)

    def appearance_episode_ended(self, episode_id):
        service = self.appearance.service
        if service.snapshot()["episode_id"] == episode_id:
            service.close_episode()

    def appearance_operation(self, **fields):
        self.appearance.service.save_state(**fields)

    def accept_appearance_gift(self, event):
        episode_id = self.program.game_episode_id
        if (not episode_id or self.program.sender_failed or event.get("simulated") is True
                or self.appearance.service.snapshot()["episode_id"] != episode_id):
            return {"accepted": False, "reason": "appearance_inactive"}
        receipt = self.appearance.service.apply(event)
        if receipt["accepted"]:
            self.program.request_appearance(receipt["target"])
        return receipt

    def save_settings(self):
        if self.settings_path:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.settings_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2))
            temporary.replace(self.settings_path)

    def set_settings(self, body):
        if set(body) - set(self.settings):
            raise ValueError("invalid_settings")
        updated = dict(self.settings)
        for key, value in body.items():
            if key == "auto_advance":
                if type(value) is not bool:
                    raise ValueError("invalid_settings")
            elif type(value) is not int or not 5 <= value <= 600:
                raise ValueError("invalid_settings")
            updated[key] = value
        if updated["auto_advance"] and not self.settings["auto_advance"]:
            self.auto_started = time.monotonic()
            self.automation_note = None
        elif not updated["auto_advance"]:
            self.auto_started = None
        self.settings = updated
        self.clock_key = None
        self.save_settings()

    def state(self, operator=False, *, debug=False, **cursor):
        state = self.game.operator_state(live_only=not debug, **cursor) if operator else self.game.public_state(live_only=not debug)
        state["busy"] = state["busy"] or bool(self.job and not self.job.done())
        if self.error and not state["error"]:
            state["error"] = {"message": self.error, "code": "request_error", "stage": "WORKBENCH"}
        state["server_time"] = time.time()
        state["settings"] = dict(self.settings)
        state["layout"] = dict(self.layout)
        state["program"] = self.program.public(operator=operator)
        state["appearance"] = self.appearance.snapshot(operator=operator)
        state["automation"] = {"limit_minutes": 20, "note": self.automation_note,
                               "status": "paused_error" if state["error"] else "running" if self.settings["auto_advance"] else "stopped"}
        self.sync_clock(state)
        if state["round"]:
            state["round"]["deadline"] = self.deadline
            state["round"]["remaining_seconds"] = max(0, int(self.deadline - time.time()) + 1) if self.deadline else None
        if operator:
            state["model"] = self.model.public() if hasattr(self.model, "public") else {"ready": True, "name": "测试模型"}
            state["model_history"] = getattr(self.model, "history", [])
            state["cleanup"] = self.cleanup
            metrics = getattr(self.game, "metrics", None)
            state["performance_events"] = metrics.snapshot() if metrics else []
            state["event_loop_delay"] = dict(self.event_loop_delay)
        return state

    def accept_comment(self, event, *, source="local", live_broadcast=None):
        if self.program.active and not self.program.game_enabled:
            raise ValueError("game_mode_required")
        blocked = self.input_blocked()
        reason = {"operation_in_progress": "正在生成，评论已记录，未计入建议或投票。",
                  "input_window_closed": "本轮收集窗口已截止，评论已记录，未计入建议或投票。"}.get(blocked)
        result = self.game.accept_comment(event, blocked_reason=reason, source=source,
                                          live_broadcast=live_broadcast)
        # Persist and apply the original vote/proposal rules first. Waking the
        # optional speaker is cheap and cannot delay the comment response.
        self.program.notify_comment()
        return result

    def input_blocked(self):
        if self.program.active and not self.program.game_enabled:
            return "game_mode_required"
        state = self.state()
        if state["busy"]:
            return "operation_in_progress"
        if (self.settings["auto_advance"] and self.deadline is not None
              and time.time() >= self.deadline and state["round"]
              and state["round"]["phase"] in ("COLLECTING", "VOTING")):
            # The scheduler polls twice a second. Enforce the actual deadline
            # at receipt too, so this short gap cannot admit late suggestions/votes.
            return "input_window_closed"
        return None

    def sync_clock(self, state):
        r = state.get("round")
        key = (r["id"], r["phase"], self.settings["auto_advance"]) if r else None
        if key == self.clock_key:
            return
        self.clock_key = key
        seconds = {"COLLECTING": self.settings["collect_seconds"], "VOTING": self.settings["vote_seconds"], "DONE": 8}
        self.deadline = time.time() + seconds[r["phase"]] if r and self.settings["auto_advance"] and r["phase"] in seconds else None
        finale = state.get("finale")
        if self.deadline is not None and r["phase"] == "DONE" and finale:
            # Use the committed conclusion, not the first browser poll, as the
            # finale clock. Keep the existing 8s total dwell around the 6.8s sequence.
            self.deadline = finale["started_at"] + max(8, finale["duration_ms"] / 1000)

    def launch(self, action):
        if self.job and not self.job.done():
            raise ValueError("operation_in_progress")
        self.error = None

        async def run():
            try:
                result = action()
                if inspect.isawaitable(result):
                    await result
            except asyncio.CancelledError:
                raise
            except ValueError as exc:
                self.error = error_text(str(exc))
            except Exception:
                self.error = "操作未能完成，已有记录保留，请重试。"

        self.job = asyncio.create_task(run())

    async def abort(self):
        # Invalidate the engine's generation first; a late network callback then
        # cannot append to the next episode even if cancellation is delayed.
        failures, steps = [], {}
        await self._cleanup_step("episode", self.game.abort_episode, failures, steps)
        await self._cleanup_step("job", lambda: self._cancel_task(self.job), failures, steps)
        self.job = None
        self.settings["auto_advance"] = False
        self.auto_started = None
        self.deadline = None
        self.clock_key = None
        self.error = None
        await self._cleanup_step("settings", self.save_settings, failures, steps)
        self._finish_cleanup("abort", failures, steps)

    async def tick(self):
        while True:
            expected_wakeup = time.monotonic() + .5
            await asyncio.sleep(.5)
            # Measure scheduler lateness only; time spent doing this tick's own
            # work is excluded. Keep constant-size counters instead of adding
            # an event every half second and crowding out useful stage records.
            delay = round(max(0, time.monotonic() - expected_wakeup), 6)
            self.event_loop_delay["samples"] += 1
            self.event_loop_delay["last_seconds"] = delay
            self.event_loop_delay["max_seconds"] = max(self.event_loop_delay["max_seconds"], delay)
            if self.program.active:
                await self.program.step()
                continue
            state = self.state()
            if not self.settings["auto_advance"] or state["busy"] or state["error"]:
                continue
            if self.auto_started is not None and time.monotonic() - self.auto_started >= 20 * 60:
                # A story may never reach 0/100. A separate time budget bounds
                # unattended API spending without inventing a narrative ending.
                self.settings["auto_advance"] = False
                self.auto_started = None
                self.automation_note = "本次自动推进已运行20分钟，现已暂停。剧情与分数保留，可手动继续或重新开启。"
                self.deadline = None
                self.clock_key = None
                self.save_settings()
                continue
            if state["episode"] and state["episode"]["status"] in ("SUCCEEDED", "FAILED", "TECH_ENDED"):
                # Local first-release automation is bounded to the episode the
                # operator started. A new episode needs an explicit start.
                self.settings["auto_advance"] = False
                self.auto_started = None
                self.automation_note = "本期已结束，自动推进已关闭。可手动开始下一期。"
                self.save_settings()
                self.deadline = None
                self.clock_key = None
                continue
            if not self.deadline or time.time() < self.deadline or not state["round"]:
                continue
            operation = {"COLLECTING": self.game.close_collection, "VOTING": self.game.close_voting,
                         "DONE": self.game.start_round}.get(state["round"]["phase"])
            if operation:
                self.launch(operation)
                self.deadline = None

    async def close(self):
        self.appearance.rehearsal.stop()
        failures, steps = [], {}
        # Each resource owns an independent cleanup step. A failed cloud stop or
        # cancelled scheduler must never strand the local HTTP client or SQLite.
        await self._cleanup_step("scheduler", lambda: self._cancel_task(self.scheduler), failures, steps)
        await self._cleanup_step("program", self.program.close, failures, steps)
        await self._cleanup_step("job", lambda: self._cancel_task(self.job), failures, steps)
        close_model = getattr(self.model, "aclose", None)
        if close_model:
            await self._cleanup_step("model", close_model, failures, steps)
        await self._cleanup_step("storage", self.game.close, failures, steps)
        self._finish_cleanup("close", failures, steps)

    @staticmethod
    async def _cancel_task(task):
        if task is not None:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    @staticmethod
    async def _cleanup_step(stage, operation, failures, steps):
        try:
            result = operation()
            if inspect.isawaitable(result):
                await result
            steps[stage] = "closed"
        except (Exception, asyncio.CancelledError):
            # Stable fields deliberately omit exception text, URLs and secrets.
            steps[stage] = "failed"
            failures.append(stage)

    def _finish_cleanup(self, action, failures, steps):
        self.cleanup = {"action": action, "first_error": failures[0] if failures else None,
                        "steps": steps, "errors": failures}
        if failures:
            raise ValueError("cleanup_failed")

    def set_layout(self, body):
        self.layout = normalize_layout(body)
        if self.layout_path:
            temporary = self.layout_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.layout, ensure_ascii=False, indent=2))
            temporary.replace(self.layout_path)


def normalize_layout(raw):
    """Same constraints as the renderer; server is the cross-browser source."""
    import math
    defaults = DEFAULT_LAYOUT
    if type(raw) is not dict or set(raw) - set(defaults):
        raise ValueError("invalid_layout")
    def number(key, low, high):
        value = raw.get(key, defaults[key])
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("invalid_layout")
        return max(low, min(high, value))
    top, left = number("top", 320, 380), number("left", 0, 120)
    right = number("right", 0, 120)
    # Match the undistorted reference: keep the entire design inside y=1640.
    # Below the portrait reserve 24px + 256px standings, then 24px + 304px poll.
    bottom = number("bottom", 280, 400)
    bottom_edge = 1920 - bottom
    phone_width = 480
    right_column_x = left + phone_width + 24
    right_column_width = 1080 - right - right_column_x
    y = number("avatarY", top, 440)
    avatar_width = number("avatarWidth", 200, min(right_column_width, (bottom_edge - y - 608) * 3 / 4))
    x = number("avatarX", right_column_x, 1080 - right - avatar_width)
    return {"top": top, "right": right, "bottom": bottom, "left": left,
            "avatarX": x, "avatarY": y, "avatarWidth": avatar_width}


@web.middleware
async def local_only(request, handler):
    try:
        parsed_host = urlsplit("http://" + request.host)
        if parsed_host.hostname not in ("127.0.0.1", "localhost", "::1") or parsed_host.username or parsed_host.password:
            return web.json_response({"error": "仅允许本机访问。"}, status=403)
        origin = request.headers.get("Origin")
        if origin and origin != f"http://{request.host}":
            return web.json_response({"error": "拒绝跨站请求。"}, status=403)
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return web.json_response({"error": "拒绝跨站请求。"}, status=403)
        if request.method == "POST" and request.content_type != "application/json":
            return web.json_response({"error": "请求需要JSON格式。"}, status=415)
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        origin = request.app["workbench"].program.client.origin
        frames = "'self'" + (" " + origin if origin else "")
        images = "'self' data:" + "".join(f" https://{host} https://*.{host}" for host in AVATAR_CDN_SUFFIXES)
        response.headers["Content-Security-Policy"] = f"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src {images}; font-src 'self'; connect-src 'self'; frame-src {frames}; frame-ancestors 'self'; object-src 'none'; base-uri 'none'"
        return response
    except web.HTTPException:
        raise
    except (ValueError, KeyError, TypeError):
        return web.json_response({"error": "请求内容不正确，请检查输入。"}, status=400)


def create_app(db_path=ROOT / "data/game.sqlite", *, model=None, avatar_client=None, obs_output=False):
    app = web.Application(middlewares=[local_only], client_max_size=32_768)
    workbench = Workbench(db_path, model=model, avatar_client=avatar_client, obs_output=obs_output)
    app["workbench"] = workbench
    register_dyhub_routes(app, workbench)
    register_appearance_routes(app, workbench)

    async def health(request):
        database = str(Path(db_path).resolve()) if str(db_path) != ":memory:" else ":memory:"
        return web.json_response({"app": APP_NAME, "version": "0.1.0", "pid": os.getpid(), "database": database})

    async def state(request):
        incremental = request.path == "/api/operator-state"
        cursor = {}
        if incremental:
            try:
                comment_cursor = int(request.query["comment_cursor"])
            except (KeyError, ValueError):
                comment_cursor = None
            cursor = {"incremental": True, "session_id": request.query.get("session_id"),
                      "comment_cursor": comment_cursor,
                      "proposal_version": request.query.get("proposal_version"),
                      "vote_version": request.query.get("vote_version"),
                      "comment_limit": request.query.get("comment_limit", "500")}
        # Keep the original /api/state contract for standalone text/debug tools.
        # Normal workbench/public reads opt into synthetic results explicitly.
        debug = request.path == "/api/state" or request.query.get("debug") == "1"
        return web.json_response(workbench.state(operator=request.path != "/api/public", debug=debug, **cursor), dumps=lambda x: json.dumps(x, ensure_ascii=False, allow_nan=False))

    async def obs_state(request):
        # Only the chosen receiver gets the player URL. This read must never
        # count as operator presence or start a session when OBS opens the scene.
        state = workbench.state()
        state["obs_output"] = workbench.obs_output
        state["player_url"] = (workbench.program.player_url or None) if workbench.obs_output else None
        return web.json_response(state)

    async def mutation(request):
        try:
            body = strict_json(await request.text())
        except (ValueError, UnicodeError):
            return web.json_response({"error": "请发送有效JSON对象。"}, status=400)
        action = request.path.removeprefix("/api/")
        actions = {"session/new": workbench.game.new_session, "episode/new": workbench.game.new_episode,
                   "round/start": workbench.game.start_round, "collection/close": workbench.game.close_collection,
                   "voting/close": workbench.game.close_voting, "retry": workbench.game.retry}
        try:
            receipt = None
            if workbench.appearance.rehearsal.ledger is not None and (action in actions or action == "program/start"):
                return web.json_response({"error": "请先结束本地造型试玩。"}, status=409)
            if action == "program/start":
                if set(body) - {"mode"}:
                    raise ValueError("invalid_program_mode")
                workbench.program.start(mode=body.get("mode", "game"))
                app['auto_source'].observe_program_run()
            elif action == "program/mode":
                if set(body) != {"run_id", "mode"}:
                    raise ValueError("invalid_program_mode")
                workbench.program.set_mode(body["run_id"], body["mode"])
            elif action == "program/stop":
                await workbench.program.stop()
            elif action == "program/heartbeat":
                workbench.program.heartbeat(body.get("run_id"))
            elif workbench.program.active and action not in {"comments", "layout"}:
                raise ValueError("operation_in_progress")
            elif action == "episode/abort":
                await workbench.abort()
            elif action == "settings":
                workbench.set_settings(body)
            elif action == "layout":
                workbench.set_layout(body)
            elif action in actions:
                workbench.launch(actions[action])
            elif action == "comments":
                receipt = workbench.accept_comment(body)
            elif action == "proposals":
                blocked = workbench.input_blocked()
                if blocked:
                    raise ValueError(blocked)
                workbench.game.submit_proposal(body.get("author_id"), body.get("nickname"), body.get("text"), avatar_url=body.get("avatar_url"))
            elif action == "votes":
                blocked = workbench.input_blocked()
                if blocked:
                    raise ValueError(blocked)
                workbench.game.vote(body.get("author_id"), body.get("nickname"), body.get("option_id"))
            else:
                raise web.HTTPNotFound()
        except ValueError as exc:
            return web.json_response({"error": error_text(str(exc))}, status=409)
        return web.json_response({"ok": True, **({"comment": receipt} if receipt is not None else {}),
                                  **({"layout": workbench.layout} if action == "layout" else {})}, status=202 if action in actions else 200)

    async def index(request):
        name = "overlay.html" if request.path == "/overlay" else "index.html"
        return web.FileResponse(ROOT / "static" / name)

    async def start(app):
        workbench.scheduler = asyncio.create_task(workbench.tick())

    async def cleanup(app):
        await workbench.close()

    app.router.add_get("/api/health", health)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/operator-state", state)
    app.router.add_get("/api/public", state)
    app.router.add_get("/api/obs-output", obs_state)
    for path in ("session/new", "episode/new", "round/start", "collection/close", "voting/close", "retry", "episode/abort", "comments", "proposals", "votes", "settings", "layout", "program/start", "program/stop", "program/heartbeat", "program/mode"):
        app.router.add_post("/api/" + path, mutation)
    app.router.add_get("/", index)
    app.router.add_get("/overlay", index)
    app.router.add_static("/static/", ROOT / "static", show_index=False)
    app.on_startup.append(start)
    app.on_cleanup.append(cleanup)
    return app


async def serve(args):
    database = Path(args.db).expanduser().resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    # The state engine is deliberately single-process. This lock prevents two
    # workbenches from loading separate in-memory copies of the same database.
    lock = database.with_suffix(".lock").open("a+")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("这个游戏数据库已有工作台在运行，请使用已有窗口。")
    runner = web.AppRunner(create_app(database, obs_output=args.obs_output, model=QwenModel(model=args.model, credentials_file=Path(args.credentials_file) if args.credentials_file else None)), access_log=None)
    await runner.setup()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", args.port))
    site = web.SockSite(runner, listener)
    await site.start()
    port = listener.getsockname()[1]
    runtime = database.parent / "runtime.json"
    runtime.write_text(json.dumps({"app": APP_NAME, "pid": os.getpid(), "url": f"http://127.0.0.1:{port}", "database": str(database)}, ensure_ascii=False, indent=2))
    print(f"游戏工作台：http://127.0.0.1:{port}", flush=True)
    print(f"透明叠层：http://127.0.0.1:{port}/overlay", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
        listener.close()
        lock.close()


def main():
    parser = argparse.ArgumentParser(description="恋爱军师团本地游戏工作台（仅127.0.0.1）")
    parser.add_argument("--obs-output", action="store_true", help="由 OBS 节目页独占接收数字人画面")
    parser.add_argument("--port", type=int, default=0, help="0使用空闲端口")
    parser.add_argument("--db", default=str(ROOT / "data/game.sqlite"))
    parser.add_argument("--model", default=None)
    parser.add_argument("--credentials-file", default=None, help="现有百炼凭据CSV路径，不复制密钥")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("端口必须在0—65535之间")
    asyncio.run(serve(args))


if __name__ == "__main__":
    main()

