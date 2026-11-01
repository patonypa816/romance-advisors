"""Isolated browser-test entrypoint; deliberately not production CLI configuration."""
from aiohttp import web
from hell_inc.app import create_app
from hell_inc.config import Settings

if __name__ == "__main__":
    settings = Settings.from_env()
    web.run_app(create_app(settings, startup_token="browser-m2-test-token-only"),
                host=settings.host, port=settings.port, access_log=None, print=None)
