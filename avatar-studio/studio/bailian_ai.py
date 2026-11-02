"""Bounded, single-attempt Qwen HTTP transport; construction is entirely local."""
from __future__ import annotations

import asyncio
import re

import aiohttp

try:
    from .connection import client_session
    from .credentials import is_environment_bailian_key
except ImportError:
    from connection import client_session
    from credentials import is_environment_bailian_key

try:
    from .cloudbase_ai import (MAX_INPUT_BYTES, MAX_RESPONSE_BYTES, REQUEST_TIMEOUT_SECONDS,
                               ModelError, StructuredBrain, _json, _json_object, _REQUEST_ID)
except ImportError:
    from cloudbase_ai import (MAX_INPUT_BYTES, MAX_RESPONSE_BYTES, REQUEST_TIMEOUT_SECONDS,
                              ModelError, StructuredBrain, _json, _json_object, _REQUEST_ID)

MODEL = "qwen3.8-flash"
REGION = "cn-beijing"
# A workspace is one DNS label, never a URL, path, port or user-info component.
_WORKSPACE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_KEY = re.compile(r"sk-[!-~]{8,253}\Z")
_HTTP_ERRORS = {401: "unauthorized", 403: "forbidden", 404: "model_not_found",
                408: "timeout", 429: "rate_limited"}


def _contains_secret(value, secret):
    # Decode JSON before checking, so Unicode escapes cannot conceal a key.
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str) and secret in item:
            return True
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    return False


def _load_credentials():
    # Import lazily: importing or constructing a brain must never read a key.
    try:
        from .credentials import load_bailian
    except ImportError:
        from credentials import load_bailian
    return load_bailian()


class BailianAI(StructuredBrain):
    def __init__(self, config: dict, *, credentials_loader=None, session_factory=None):
        super().__init__(config)
        self._credentials_loader = credentials_loader or _load_credentials
        self._session_factory = session_factory or client_session
        self._active = set()

    def _validate_config(self):
        if (self.config.get("model") != MODEL or self.config.get("region") != REGION
                or self.config.get("enable_thinking") is not False):
            raise ModelError("configuration_invalid")

    def _credentials(self):
        try:
            value = self._credentials_loader()
            if not isinstance(value, (tuple, list)) or len(value) != 2:
                raise ValueError()
            key, workspace = value
            if (not isinstance(key, str) or not (_KEY.fullmatch(key) or is_environment_bailian_key(key))
                    or not isinstance(workspace, str) or not _WORKSPACE.fullmatch(workspace)):
                raise ValueError()
            return key, workspace
        except Exception:
            # Loader errors may contain local filenames or credential contents.
            raise ModelError("credential_invalid") from None

    def configured(self):
        if self._closed:
            return False
        try:
            self._validate_config()
            self._credentials()  # Local validation; hot reload on every use.
            return True
        except ModelError:
            return False

    def public(self):
        return {"provider": "bailian", "transport": "openai_compatible_http", "model": MODEL,
                "enable_thinking": False, "configured": self.configured(), "last_error": self.last_error,
                "requests": dict(self.requests), "usage": {r: dict(v) for r, v in self.usage.items()}}

    async def _request(self, workspace, key, encoded):
        try:
            # A new session gives each attempt its own socket lifetime. It also
            # avoids pooled stale-connection retries. Only HTTPS_PROXY is opted in.
            async with self._session_factory(
                    trust_env=False, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)) as session:
                endpoint = f"https://{workspace}.{REGION}.maas.aliyuncs.com/compatible-mode/v1/chat/completions"
                async with session.post(endpoint, data=encoded,
                                        headers={"Authorization": "Bearer " + key,
                                                 "Content-Type": "application/json"},
                                        allow_redirects=False) as response:
                    if response.status != 200:
                        # Never parse or expose a provider's error body or URL.
                        raise ModelError(_HTTP_ERRORS.get(response.status, "provider_error"))
                    if response.content_length is not None and response.content_length > MAX_RESPONSE_BYTES:
                        raise ModelError("response_too_large")
                    raw = bytearray()
                    while True:
                        chunk = await response.content.read(min(16384, MAX_RESPONSE_BYTES + 1 - len(raw)))
                        if not chunk:
                            break
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE_BYTES:
                            raise ModelError("response_too_large")
                    if self._closed:
                        raise ModelError("client_closed")
                    try:
                        return _json_object(bytes(raw))
                    except (ValueError, UnicodeError, RecursionError):
                        raise ModelError("response_invalid_json") from None
        finally:
            self._active.discard(asyncio.current_task())

    async def _call(self, role, messages):
        attempted = usage_recorded = False
        request_id = None
        key = None
        try:
            if self._closed:
                raise ModelError("client_closed")
            self._validate_config()
            key, workspace = self._credentials()
            if role not in self.requests:
                raise ModelError("configuration_invalid")
            encoded = _json({"model": MODEL, "messages": messages, "enable_thinking": False,
                             "response_format": {"type": "json_object"}, "stream": False,
                             "max_tokens": 512 if role == "selection" else 4096}).encode("utf-8")
            if len(encoded) > MAX_INPUT_BYTES:
                raise ModelError("input_too_large")
            self.requests[role] += 1
            attempted = True
            task = asyncio.create_task(self._request(workspace, key, encoded))
            self._active.add(task)
            task.add_done_callback(self._active.discard)
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                body = await task
            if self._closed:
                raise ModelError("client_closed")
            rid = body.get("request_id", body.get("id", ""))
            if isinstance(rid, str) and _REQUEST_ID.fullmatch(rid) and key not in rid:
                request_id = rid
            if body.get("error") is not None or (body.get("code") is not None and body["code"] != "NORMAL"):
                raise ModelError("provider_error", request_id)
            if body.get("model") != MODEL:
                raise ModelError("model_mismatch", request_id)
            self._usage(role, body.get("usage"), request_id)
            usage_recorded = True
            choices = body.get("choices")
            if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
                raise ModelError("response_invalid", request_id)
            choice = choices[0]
            if choice.get("finish_reason") != "stop":
                raise ModelError("output_truncated" if choice.get("finish_reason") == "length"
                                 else "output_unfinished", request_id)
            message = choice.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, str) or not content.strip():
                raise ModelError("content_missing", request_id)
            if key in content:
                raise ModelError("sensitive_output", request_id)
            try:
                result = _json_object(content)
                if _contains_secret(result, key):
                    raise ModelError("sensitive_output", request_id)
                return result
            except (ValueError, UnicodeError, RecursionError):
                raise ModelError("output_invalid_json", request_id) from None
        except asyncio.CancelledError:
            if self._closed:
                self.last_error = "client_closed"
                raise ModelError("client_closed", request_id) from None
            self.last_error = "cancelled"
            raise
        except ModelError as exc:
            self.last_error = exc.code
            raise
        except (asyncio.TimeoutError, TimeoutError):
            self.last_error = "timeout"
            raise ModelError("timeout", request_id) from None
        except Exception:
            self.last_error = "transport_error"
            raise ModelError("transport_error", request_id) from None
        finally:
            if attempted and not usage_recorded:
                self.usage[role]["unverified_requests"] += 1
            key = None

    async def close(self):
        self._closed = True
        tasks = tuple(self._active)
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        if tasks:
            closing = asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.shield(closing)
            except asyncio.CancelledError:
                # Even cancellation of close must finish releasing our sockets.
                await closing
                raise
