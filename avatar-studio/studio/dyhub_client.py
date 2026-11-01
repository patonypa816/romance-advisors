"""Bounded, local-only dyhub SSE consumer.

GET /api/events automatically starts dyhub collection when the requested web_rid
is inactive. Closing this consumer removes only its SSE subscription: dyhub keeps
collection alive. We deliberately never call the shared service's disconnect API;
collection lifecycle must be handled separately by its owner.

The inspected dyhub normalizer uses the session's input web_rid as event.roomId,
including with lightweightSession (its resolved webcast ID stays internal).
No replay buffer is kept here; candidate selection owns duplicate suppression.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import urlsplit

import aiohttp

MAX_LINE_BYTES = 16 * 1024
MAX_FRAME_BYTES = 64 * 1024


class RoomConnectionError(Exception):
    """dyhub could not start the requested room."""


class StreamProtocolError(Exception):
    """Invalid or oversized SSE input; exception text never enters UI status."""


def normalize_endpoint(base_url: str) -> str:
    """Accept only an explicit IPv4 loopback HTTP port, without extra URL parts."""
    if not isinstance(base_url, str) or not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}", base_url):
        raise ValueError("invalid_dyhub_endpoint")
    parts = urlsplit(base_url)
    try:
        port = parts.port
    except ValueError:
        raise ValueError("invalid_dyhub_endpoint") from None
    if port is None or not 1 <= port <= 65535:
        raise ValueError("invalid_dyhub_endpoint")
    return f"http://127.0.0.1:{port}"


async def parse_sse(chunks: AsyncIterator[bytes]) -> AsyncIterator[dict]:
    """Parse LF/CRLF/CR, multi-line data and comments with bounded memory.

    EOF does not dispatch an unfinished frame, as required by EventSource. SSE
    IDs and retry hints are ignored: replay and reconnect policy belong upstream.
    """
    line = bytearray()
    data: list[str] = []
    frame_bytes = 0
    previous_cr = False
    first_line = True
    async for chunk in chunks:
        for byte in chunk:
            if previous_cr and byte == 10:
                previous_cr = False
                continue
            previous_cr = byte == 13
            frame_bytes += 1
            if frame_bytes > MAX_FRAME_BYTES:
                raise StreamProtocolError()
            if byte not in (10, 13):
                line.append(byte)
                if len(line) > MAX_LINE_BYTES:
                    raise StreamProtocolError()
                continue
            try:
                value = line.decode("utf-8")
            except UnicodeDecodeError:
                raise StreamProtocolError() from None
            line.clear()
            if first_line:
                value = value.removeprefix("\ufeff")
                first_line = False
            if not value:
                frame_bytes = 0
                if data:
                    try:
                        event = json.loads("\n".join(data))
                    except (ValueError, RecursionError):
                        raise StreamProtocolError() from None
                    data.clear()
                    if not isinstance(event, dict):
                        raise StreamProtocolError()
                    yield event
            elif not value.startswith(":"):
                field, separator, content = value.partition(":")
                if field == "data":
                    data.append(content.removeprefix(" ") if separator else "")


class DyhubClient:
    """One local SSE subscription; callbacks receive only allowlisted fields."""

    RETRY_INITIAL = 0.5
    RETRY_MAX = 10.0

    def __init__(self, base_url: str, room_id: str,
                 on_event: Callable[[dict], Awaitable[None]],
                 on_status: Callable[[dict], None]):
        self.base_url = normalize_endpoint(base_url)
        if not isinstance(room_id, str) or not re.fullmatch(r"[0-9]{1,32}", room_id):
            raise ValueError("invalid_dyhub_room")
        self.room_id = room_id
        self.on_event = on_event
        self.on_status = on_status
        self.status = {"state": "stopped", "room_id": room_id,
                       "last_event_received_at": None, "reconnect_count": 0,
                       "error_code": None}
        self._session: aiohttp.ClientSession | None = None
        self._response: aiohttp.ClientResponse | None = None
        self._closed = asyncio.Event()
        self._running = False

    def _status(self, state: str, error_code: str | None = None) -> None:
        self.status.update(state=state, error_code=error_code)
        self.on_status(dict(self.status))

    async def _consume(self) -> None:
        assert self._session is not None
        async with self._session.get(
            self.base_url + "/api/events",
            params={"roomId": self.room_id, "types": "chat,roomStatus"},
            allow_redirects=False,
        ) as response:
            self._response = response
            if 300 <= response.status < 400:
                self._status("error", "redirect_rejected")
                self._closed.set()
                return
            if response.status != 200:
                raise aiohttp.ClientConnectionError()
            if response.content_type != "text/event-stream":
                raise StreamProtocolError()
            self._status("connected")
            async for event in parse_sse(response.content.iter_chunked(4096)):
                # No aliases or guessed webcast IDs: the inspected normalizer
                # guarantees that roomId is the input web_rid.
                if event.get("roomId") != self.room_id:
                    continue
                self.status["last_event_received_at"] = int(time.time() * 1000)
                kind = event.get("type")
                if kind == "__connecting":
                    self._status("connecting")
                elif kind == "__connected":
                    self._status("connected")
                elif kind == "__error":
                    self._status("error", "room_connect_failed")
                    raise RoomConnectionError()
                elif kind == "roomStatus":
                    payload = event.get("data")
                    state = payload.get("status") if isinstance(payload, dict) else None
                    if state in {"connecting", "live", "closed", "error", "stopped"}:
                        self._status(state, "room_error" if state == "error" else None)
                elif kind == "chat":
                    if event.get("platform") != "douyin":
                        continue
                    payload, user = event.get("data"), event.get("user")
                    event_id = event.get("id")
                    if (not isinstance(payload, dict)
                            or not isinstance(payload.get("content"), str)
                            or not isinstance(event_id, str) or not 1 <= len(event_id) <= 256):
                        continue
                    safe = {"type": "chat", "platform": "douyin", "id": event_id, "roomId": self.room_id,
                            "data": {"content": payload["content"][:8000]}}
                    for key in ("ts", "receivedAt"):
                        number = event.get(key)
                        if type(number) in (int, float) and 0 <= number <= 10**16:
                            safe[key] = number
                    if isinstance(user, dict):
                        safe["user"] = {key: user[key][:256] for key in ("id", "nickname")
                                        if isinstance(user.get(key), str)}
                    try:
                        await self.on_event(safe)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self._status("error", "event_callback_failed")
                    else:
                        self.on_status(dict(self.status))

    async def _worker(self) -> None:
        delay = self.RETRY_INITIAL
        while not self._closed.is_set():
            self._status("connecting")
            started = time.monotonic()
            code = "stream_closed"
            try:
                await self._consume()
            except RoomConnectionError:
                code = "room_connect_failed"
            except (StreamProtocolError, ValueError, aiohttp.ClientPayloadError):
                code = "invalid_stream"
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                code = "connection_failed"
            finally:
                self._response = None
            if self._closed.is_set():
                break
            self._status("retrying", code)
            # Reset only after a stable connection, preventing rapid EOF loops.
            if time.monotonic() - started >= 30:
                delay = self.RETRY_INITIAL
            try:
                await asyncio.wait_for(self._closed.wait(), timeout=delay)
            except asyncio.TimeoutError:
                self.status["reconnect_count"] += 1
            delay = min(delay * 2, self.RETRY_MAX)

    async def run(self, stop_event: asyncio.Event) -> None:
        if self._running:
            raise RuntimeError("dyhub_already_running")
        if stop_event.is_set() or self._closed.is_set():
            self._status("stopped")
            return
        self._running = True
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=45),
            trust_env=False, cookie_jar=aiohttp.DummyCookieJar(),
        )
        worker = asyncio.create_task(self._worker())
        stopper = asyncio.create_task(stop_event.wait())
        closer = asyncio.create_task(self._closed.wait())
        try:
            done, _ = await asyncio.wait((worker, stopper, closer), return_when=asyncio.FIRST_COMPLETED)
            if worker in done:
                await worker
        finally:
            await self.close()
            for task in (worker, stopper, closer):
                task.cancel()
            await asyncio.gather(worker, stopper, closer, return_exceptions=True)
            self._running = False
            self._status("stopped")

    async def close(self) -> None:
        """Close only our HTTP connection, never disconnect a shared room."""
        self._closed.set()
        if self._response is not None:
            self._response.close()
        if self._session is not None:
            await self._session.close()
