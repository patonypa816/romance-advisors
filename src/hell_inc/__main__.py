"""Explicit migrate / same-origin serve; no paid services or implicit schema change."""
import argparse
import asyncio
import os
from pathlib import Path
import secrets
import sys
from aiohttp import web
from .app import create_app
from .config import Settings
from .db import make_engine, migrate


async def run_migration(settings):
    engine = make_engine(settings.database_url)
    try:
        await migrate(engine)
    finally:
        await engine.dispose()


def deliver_login(url: str, filename: str | None):
    if filename:
        path = Path(filename).expanduser().resolve()
        # Secret belongs outside checkout; explicit file is exclusive and owner-only.
        checkout = Path.cwd().resolve()
        if path == checkout or checkout in path.parents:
            raise ValueError("登录链接文件必须放在仓库外，避免意外提交。")
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(url + "\n")
    else:
        try:
            with open("/dev/tty", "w") as tty:
                tty.write("一次性登录链接（10 分钟有效；不应分享或录屏）：\n" + url + "\n")
        except OSError:
            raise ValueError("无交互终端；请用 --login-file /仓库外路径 保存权限为 0600 的一次性链接。") from None


def main():
    parser = argparse.ArgumentParser(description="HELL INC isolated game and offline performance workbench")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="explicit transactional schema migration")
    serve = sub.add_parser("serve", help="serve protected same-origin operator page")
    serve.add_argument("--login-file", help="exclusive 0600 startup URL file outside checkout; delete after login")
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        if args.command == "migrate":
            asyncio.run(run_migration(settings))
            print("M4 schema version 4 is ready.")
        else:
            token = secrets.token_urlsafe(32)
            app = create_app(settings, startup_token=token)
            # Deliver only after DB and singleton startup have succeeded.
            async def ready(_app):
                deliver_login(settings.public_origin + "/#token=" + token, args.login_file)
            app.on_startup.append(ready)
            web.run_app(app, host=settings.host, port=settings.port, access_log=None,
                        print=lambda _: print("HELL INC running (local-test; offline performance only)."))
    except (Exception, KeyboardInterrupt):
        # Avoid default exception reprs containing connection strings/parameters.
        print("启动或迁移未完成：检查数据库连接、迁移版本、实例锁及仓库外登录文件路径。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
