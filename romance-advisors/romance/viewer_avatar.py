"""Optional audience pictures, never identity and never fetched by the server.

The DyHub adapter maps the verified upstream user.avatar field to avatar_url.
The installed collector version and real pictures still need local validation.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


# Keep URLs on known picture CDNs. No arbitrary-host proxy, localhost request,
# credentials, data URL, SVG or application route is accepted as an avatar.
AVATAR_CDN_SUFFIXES = ("douyinpic.com", "douyinstatic.com", "byteimg.com", "ibytedtos.com")


def safe_avatar_url(value):
    if not isinstance(value, str) or not value or len(value) > 2048:
        return None
    if re.search(r"[\s\\\x00-\x1f\x7f]", value):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment
                or not any(host == suffix or host.endswith("." + suffix) for suffix in AVATAR_CDN_SUFFIXES)
                or not parsed.path or parsed.path.lower().endswith(".svg")):
            return None
    except ValueError:
        return None
    return value
