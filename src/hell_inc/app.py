import asyncio
import json
from contextlib import suppress
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
from .interviews import InterviewService, InterviewError
from .competitions import CompetitionService
from .performances import PerformanceService
from .performance_contract import PerformanceError
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
        response = await dispatch(request, handler)
    except ValidationError as exc:
        response = error("validation_error", str(exc), 400)
    except PerformanceError as exc:
        response = error(exc.code, '演出来源、场次或状态不允许此操作；未知发送不会重播。', exc.status)
    except InterviewError as exc:
        response = error(exc.code, '面试状态或版本不允许此操作。', exc.status)
    except PlayerNotFound:
        response = error("player_not_found", "未找到该玩家。", 404)
    except CommandConflict as exc:
        response = error("command_conflict", str(exc), 409)
    except web.HTTPException as exc:
        response = error("http_error", "请求路径、方法或请求大小不受支持。", exc.status)
    except (SQLAlchemyError, OSError):
        # Gate was closed inside dispatch before releasing its mutex.
        # Do not print database URLs, parameters, tokens or personal records in logs.
        response = error("database_unavailable", "数据库暂不可用；请保留原操作并重试。", 503)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


async def dispatch(request, handler):
    state = request.app[STATE]
    if not request.path.startswith('/api/'):
        return await handler(request)
    # This small process-local mutex couples outage detection, recovery and every
    # DB operation. A queued result cannot pass an older pre-lock gate check.
    async with state['game_lock']:
        try:
            if (state['game_gate'] or state['performances'].gate) and request.path not in ('/api/session','/api/health'):
                await state['interviews'].recover('database_recovery')
                await state['competitions'].recover('database_recovery')
                await state['performances'].recover('database_recovery')
                state['game_gate'] = False
            return await handler(request)
        except (SQLAlchemyError, OSError):
            state['game_gate'] = True
            await state['performances'].close_gate()
            raise


async def body(request):
    if request.content_type != "application/json":
        raise ValidationError("请求必须为 application/json。")
    try:
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError('duplicate key')
                result[key] = value
            return result
        def constant(_value):
            raise ValueError('nonfinite number')
        result = json.loads(await request.text(), object_pairs_hook=pairs, parse_constant=constant)
        # Reject escaped lone surrogates before hashing or database encoding.
        json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
        def check_strings(value):
            if isinstance(value, str) and '\x00' in value:
                raise ValueError('NUL is not supported by PostgreSQL JSONB')
            if isinstance(value, dict):
                for key, item in value.items():
                    check_strings(key)
                    check_strings(item)
            elif isinstance(value, list):
                for item in value:
                    check_strings(item)
        check_strings(result)
        return result
    except (ValueError, UnicodeError, RecursionError):
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
        state["game_gate"] = True
        await state["performances"].close_gate()
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
            if (await conn.execute(select(versions.c.version))).scalars().all() != [4]:
                raise RuntimeError("请先单独执行 migrate；启动不会修改表结构。")
        await state["service"].initialize_world()
        await state['interviews'].recover('restart')
        await state['competitions'].recover('restart')
        await state['performances'].recover('restart')
        async def tick():
            while True:
                await asyncio.sleep(0.5)
                async with state['game_lock']:
                    try:
                        if state['game_gate'] or state['performances'].gate:
                            await state['interviews'].recover('database_recovery')
                            await state['competitions'].recover('database_recovery')
                            await state['performances'].recover('database_recovery')
                            state['game_gate'] = False
                        else:
                            await state['interviews'].expire_due()
                            await state['competitions'].expire_due()
                            await state['performances'].expire_due()
                    except (SQLAlchemyError, OSError):
                        state['game_gate'] = True
                        await state['performances'].close_gate()
        timer = asyncio.create_task(tick())
        try:
            yield
        finally:
            timer.cancel()
            with suppress(asyncio.CancelledError):
                await timer
            await state['performances'].shutdown()
    finally:
        await state["engine"].dispose()
        state["lock"].release()


def create_app(settings: Settings, *, startup_token: str | None = None):
    engine = make_engine(settings.database_url)
    token = startup_token or secrets.token_urlsafe(32)
    app = web.Application(middlewares=[guard], client_max_size=32768)
    app[STATE] = {"settings": settings, "engine": engine,
        "service": RecordService(engine, settings.world_id),
        "performances": PerformanceService(engine, settings.world_id),
        "competitions": CompetitionService(engine, settings.world_id),
        "interviews": InterviewService(engine, settings.world_id), "game_gate": False, "game_lock": asyncio.Lock(),
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
    app.router.add_get('/api/interviews', interview_list)
    app.router.add_post('/api/interviews', interview_start)
    app.router.add_get('/api/interviews/{id}', interview_get)
    app.router.add_get('/api/interviews/{id}/evaluation-request', interview_export)
    app.router.add_post('/api/interviews/{id}/evaluation-result', interview_import)
    app.router.add_post('/api/interviews/{id}/answers', interview_answer)
    for action in ('open','pause','resume','retry','cancel'):
        app.router.add_post('/api/interviews/{id}/' + action, interview_command)
    app.router.add_get('/api/competitions', competition_list)
    app.router.add_post('/api/competitions', competition_start)
    app.router.add_get('/api/competitions/{id}', competition_get)
    app.router.add_get('/api/competitions/{id}/vote-state', competition_vote_state)
    app.router.add_post('/api/competitions/{id}/submissions', competition_submit)
    app.router.add_post('/api/competitions/{id}/votes', competition_vote)
    app.router.add_get('/api/competitions/{id}/submissions/{sid}/evaluation-request', competition_export)
    app.router.add_post('/api/competitions/{id}/submissions/{sid}/evaluation-result', competition_import)
    for action in ('retry','confirm-display'):
        app.router.add_post('/api/competitions/{id}/submissions/{sid}/'+action, competition_submission_command)
    for action in ('open','close-submissions','open-voting','close-voting','settle','pause','resume','cancel'):
        app.router.add_post('/api/competitions/{id}/'+action, competition_command)
    app.router.add_get('/api/performance-session', performance_session_get)
    app.router.add_post('/api/performance-session', performance_session_create)
    for action in ('pause','resume','close'):
        app.router.add_post('/api/performance-session/{id}/'+action, performance_session_command)
    app.router.add_post('/api/performance-sources/freeze', performance_freeze)
    app.router.add_get('/api/performances', performance_list)
    app.router.add_post('/api/performances', performance_submit)
    app.router.add_get('/api/performances/{id}', performance_get)
    for action in ('execute','cancel'):
        app.router.add_post('/api/performances/{id}/'+action, performance_command)
    app.router.add_get("/", index)
    assets = Path(settings.frontend_dir) / "assets"
    if assets.is_dir():
        app.router.add_static("/assets/", assets, show_index=False, follow_symlinks=False)
    return app


async def interview_list(request):
    return web.json_response(await request.app[STATE]['interviews'].list_interviews())

async def interview_start(request):
    return web.json_response(await request.app[STATE]['interviews'].start(await body(request)))

async def interview_get(request):
    return web.json_response(await request.app[STATE]['interviews'].get(request.match_info['id']))

async def interview_command(request):
    action=request.path.rsplit('/',1)[-1]
    data=await body(request)
    result=await request.app[STATE]['interviews'].command(request.match_info['id'],action,data)
    if action=='pause':
        result['performance_pause']=await request.app[STATE]['performances'].pause_for_game(data['command_id'],'interview:'+request.match_info['id'])
    return web.json_response(result)

async def interview_answer(request):
    return web.json_response(await request.app[STATE]['interviews'].answer(request.match_info['id'],await body(request)))

async def interview_export(request):
    return web.json_response(await request.app[STATE]['interviews'].evaluation_request(request.match_info['id']))

async def interview_import(request):
    return web.json_response(await request.app[STATE]['interviews'].evaluation_result(request.match_info['id'],await body(request)))


async def competition_list(request):
    return web.json_response(await request.app[STATE]['competitions'].list_competitions())
async def competition_start(request):
    return web.json_response(await request.app[STATE]['competitions'].start(await body(request)))
async def competition_get(request):
    return web.json_response(await request.app[STATE]['competitions'].get(request.match_info['id']))
async def competition_command(request):
    action=request.path.rsplit('/',1)[-1]
    data=await body(request)
    result=await request.app[STATE]['competitions'].command(request.match_info['id'],action,data)
    if action=='pause':
        result['performance_pause']=await request.app[STATE]['performances'].pause_for_game(data['command_id'],'competition:'+request.match_info['id'])
    return web.json_response(result)

async def competition_submit(request):
    return web.json_response(await request.app[STATE]['competitions'].submit(request.match_info['id'],await body(request)))
async def competition_vote(request):
    return web.json_response(await request.app[STATE]['competitions'].vote(request.match_info['id'],await body(request)))
async def competition_vote_state(request):
    if set(request.query)!={'player_id'}:raise ValidationError('需要 player_id。')
    return web.json_response(await request.app[STATE]['competitions'].vote_state(request.match_info['id'],request.query['player_id']))
async def competition_export(request):
    return web.json_response(await request.app[STATE]['competitions'].evaluation_request(request.match_info['id'],request.match_info['sid']))
async def competition_import(request):
    return web.json_response(await request.app[STATE]['competitions'].evaluation_result(request.match_info['id'],request.match_info['sid'],await body(request)))
async def competition_submission_command(request):
    return web.json_response(await request.app[STATE]['competitions'].submission_command(request.match_info['id'],request.match_info['sid'],request.path.rsplit('/',1)[-1],await body(request)))


async def performance_session_get(request):
    return web.json_response(await request.app[STATE]['performances'].session_get())
async def performance_session_create(request):
    result,created=await request.app[STATE]['performances'].session_create(await body(request))
    return web.json_response(result,status=201 if created else 200)
async def performance_session_command(request):
    return web.json_response(await request.app[STATE]['performances'].session_command(request.match_info['id'],request.path.rsplit('/',1)[-1],await body(request)))
async def performance_freeze(request):
    return web.json_response(await request.app[STATE]['performances'].freeze(await body(request)))
async def performance_submit(request):
    result,created=await request.app[STATE]['performances'].submit(await body(request))
    return web.json_response(result,status=201 if created else 200)
async def performance_list(request):
    if set(request.query)-{'session_id'}:raise ValidationError('不支持的查询字段。')
    return web.json_response(await request.app[STATE]['performances'].list(request.query.get('session_id')))
async def performance_get(request):
    return web.json_response(await request.app[STATE]['performances'].get(request.match_info['id']))
async def performance_command(request):
    return web.json_response(await request.app[STATE]['performances'].command(request.match_info['id'],request.path.rsplit('/',1)[-1],await body(request)))
