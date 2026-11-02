"""Use the existing managed HTTPS proxy without changing auth or TLS policy.

No proxy: retain aiohttp >= 3.9 behavior. With HTTPS_PROXY: require the public
ClientSession(proxy=...) API (aiohttp >= 3.12; checked, never auto-upgraded).
Do not opt into trust_env: that would also read netrc and other proxy settings.
"""
import inspect
import os

import aiohttp

_SUPPORTS_PROXY = "proxy" in inspect.signature(aiohttp.ClientSession).parameters


def client_session(**kwargs):
    proxy = os.environ.get("HTTPS_PROXY")
    if proxy:
        if not _SUPPORTS_PROXY:
            raise RuntimeError("HTTPS_PROXY requires aiohttp ClientSession proxy support (>=3.12)")
        kwargs["proxy"] = proxy
    return aiohttp.ClientSession(**kwargs)
