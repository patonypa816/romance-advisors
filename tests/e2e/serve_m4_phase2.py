"""Static built UI for explicitly mocked receiver browser tests; no database/providers."""
from pathlib import Path
from aiohttp import web

ROOT=Path(__file__).resolve().parents[2]/'frontend'/'dist'
async def index(request): return web.FileResponse(ROOT/'index.html')
async def health(request): return web.json_response({'status':'mocked-browser-fixture'})
if __name__=='__main__':
    app=web.Application()
    app.router.add_get('/api/health',health)
    app.router.add_get('/',index)
    app.router.add_static('/assets',ROOT/'assets')
    web.run_app(app,host='127.0.0.1',port=8084,access_log=None,print=None)
