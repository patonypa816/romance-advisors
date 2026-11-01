import hashlib
import secrets
import time
from pathlib import Path
from aiohttp import web
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from .config import Settings
from .contracts import ValidationError, validate_registration
from .db import make_engine, versions
from .instance import InstanceLock
from .records.service import RecordService, PlayerNotFound, CommandConflict

COOKIE_NAME = "hell_inc_session"
STATE = web.AppKey("state", dict)


def error(code, message, status):
    return web.json_response({"error": {"code": code, "message": message}}, status=status)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def authenticated(request):
    state = request.app[STATE]
    cookie = request.cookies.get(COOKIE_NAME, "")
    return bool(cookie and state["session_hash"] and
                time.monotonic() < state["session_expires"] and
                secrets.compare_digest(digest(cookie), state["session_hash"]))


@web.middleware
async def guard(request, handler):
    origin = request.app[STATE]["settings"].public_origin
    if request.path.startswith("/api/"):
        # No CORS. Mutations must carry exact same-origin Origin, including login.
        if request.method not in ("GET", "HEAD") and request.headers.get("Origin") != origin:
            return error("origin_forbidden", "仅允许同源操作。", 403)
        if request.headers.get("Origin") not in (None, origin):
            return error("origin_forbidden", "仅允许同源访问。", 403)
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return error("origin_forbidden", "不允许跨站访问。", 403)
        if request.path not in ("/api/session", "/api/health") and not authenticated(request):
            return error("unauthorized", "请使用本次启动的一次性链接登录。", 401)
    try:
        response = await handler(request)
    except ValidationError as exc:
        response = error("validation_error", str(exc), 400)
    except PlayerNotFound:
        response = error("player_not_found", "未找到该玩家。", 404)
    except CommandConflict as exc:
        response = error("command_conflict", str(exc), 409)
    except web.HTTPException as exc:
        response = error("http_error", "请求路径、方法或请求大小不受支持。", exc.status)
    except (SQLAlchemyError, OSError):
        # Do not print database URLs, parameters, tokens or personal records in logs.
        response = error("database_unavailable", "数据库暂不可用；请保留原操作并重试。", 503)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


async def body(request):
    if request.content_type != "application/json":
        raise ValidationError("请求必须为 application/json。")
    try:
        return await request.json()
    except (ValueError, UnicodeDecodeError):
        raise ValidationError("JSON 格式不合法。") from None


async def session(request):
    if request.method == "GET":
        return web.json_response({"authenticated": authenticated(request)})
    state = request.app[STATE]
    data = await body(request)
    if not isinstance(data, dict) or set(data) != {"token"} or not isinstance(data["token"], str):
        raise ValidationError("请提供一次性 token。")
    if (state["token_hash"] is None or time.monotonic() >= state["token_expires"] or
            not secrets.compare_digest(digest(data["token"]), state["token_hash"])):
        return error("invalid_token", "启动链接无效、过期或已使用；请重启以取得新链接。", 401)
    # No await between checking and consuming; one process / one event loop.
    state["token_hash"] = None
    cookie = secrets.token_urlsafe(32)
    settings = state["settings"]
    state["session_hash"] = digest(cookie)
    state["session_expires"] = time.monotonic() + settings.session_seconds
    response = web.json_response({"authenticated": True})
    response.set_cookie(COOKIE_NAME, cookie, httponly=True, samesite="Strict", path="/",
                        secure=settings.public_origin.startswith("https://"),
                        max_age=settings.session_seconds)
    return response


async def health(request):
    state = request.app[STATE]
    status = "ok"
    try:
        async with state["engine"].connect() as conn:
            await conn.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError):
        status = "unavailable"
    return web.json_response({"status": status, "mode": "local-test",
        "world_id": state["settings"].world_id}, status=200 if status == "ok" else 503)


async def register(request):
    data = validate_registration(await body(request))
    return web.json_response(await request.app[STATE]["service"].register(data))


async def list_players(request):
    query = request.query.get("q", "")
    if len(query) > 128 or "\x00" in query:
        raise ValidationError("查询最多 128 字符，且不可含空字符。")
    return web.json_response(await request.app[STATE]["service"].list_players(query))


async def get_player(request):
    return web.json_response(await request.app[STATE]["service"].get_player(request.match_info["player_id"]))


async def index(request):
    path = Path(request.app[STATE]["settings"].frontend_dir) / "index.html"
    if not path.is_file():
        return error("frontend_not_built", "请先在 frontend 运行 npm ci && npm run build。", 503)
    return web.FileResponse(path)


async def lifecycle(app):
    state = app[STATE]
    state["lock"].acquire()
    try:
        async with state["engine"].connect() as conn:
            if (await conn.execute(select(versions.c.version))).scalars().all() != [1]:
                raise RuntimeError("请先单独执行 migrate；启动不会修改表结构。")
        await state["service"].initialize_world()
        yield
    finally:
        await state["engine"].dispose()
        state["lock"].release()


def create_app(settings: Settings, *, startup_token: str | None = None):
    engine = make_engine(settings.database_url)
    token = startup_token or secrets.token_urlsafe(32)
    app = web.Application(middlewares=[guard], client_max_size=8192)
    app[STATE] = {"settings": settings, "engine": engine,
        "service": RecordService(engine, settings.world_id),
        "lock": InstanceLock(settings.database_url, settings.world_id, settings.lock_dir),
        "token_hash": digest(token), "token_expires": time.monotonic() + 600,
        "session_hash": None, "session_expires": 0}
    app.cleanup_ctx.append(lifecycle)
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/session", session)
    app.router.add_post("/api/session", session)
    app.router.add_get("/api/players", list_players)
    app.router.add_post("/api/players", register)
    app.router.add_get("/api/players/{player_id}", get_player)
    app.router.add_get("/", index)
    assets = Path(settings.frontend_dir) / "assets"
    if assets.is_dir():
        app.router.add_static("/assets/", assets, show_index=False, follow_symlinks=False)
    return app
