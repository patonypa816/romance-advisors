"""One process-owned, free room watcher. Never creates a paid avatar session.

Public-page room identity proves a broadcast; SSE state proves only a transport.
All tests inject a probe and clock, so they need no Douyin connection.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

import aiohttp


def settings(base_url, room_id):
    # Reuse the transport's exact loopback policy without creating a connection.
    from urllib.parse import urlsplit
    if not isinstance(base_url, str) or not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', base_url):
        raise ValueError('invalid_source_settings')
    port = urlsplit(base_url).port
    if port is None or not 1 <= port <= 65535 or not isinstance(room_id, str) or not re.fullmatch(r'[0-9]{1,32}', room_id):
        raise ValueError('invalid_source_settings')
    return f'http://127.0.0.1:{port}', room_id


async def probe_room(base_url, room_id):
    """Talk only to the local DyHub read-only probe, never user Cookie endpoints."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=65),
                                     trust_env=False, cookie_jar=aiohttp.DummyCookieJar()) as session:
        async with session.get(f'{base_url}/api/rooms/{room_id}/live-status', allow_redirects=False) as response:
            if response.status != 200 or response.content_type != 'application/json':
                raise ValueError('probe_unavailable')
            if response.content_length is not None and response.content_length > 8192:
                raise ValueError('invalid_live_probe')
            raw = await response.content.read(8193)
            if len(raw) > 8192:
                raise ValueError('invalid_live_probe')
            return json.loads(raw)


class AutoSource:
    CONFIRM_SECONDS = 5
    POLL_SECONDS = 10
    END_SAMPLE_SECONDS = 2.5
    ERROR_MAX_SECONDS = 60

    def __init__(self, workbench, intake, *, probe=probe_room, clock=time.time):
        self.workbench, self.intake = workbench, intake
        self.probe, self.clock = probe, clock
        path = getattr(workbench, 'settings_path', None)
        self.path = path.with_name(path.name.removesuffix('.settings.json') + '.source.json') if path else None
        self.base_url, self.room_id = intake.base_url, ''
        self.enabled, self.paused_live_id = False, None
        self.state, self.platform_status, self.error_code = 'unconfigured', 'unknown', None
        self.live_id, self.candidate_id, self.candidate_at = None, None, None
        self.ended_id, self.ended_at, self.ended_count = None, None, 0
        self.next_check_at, self.task = None, None
        self.finalized = set()
        self.stop_result = None
        self.run_bindings = {}
        self.run_broadcasts = {}
        self.finish_task = None
        self.lock = asyncio.Lock()
        self._load()
        if not self.room_id:
            # The existing appearance configuration already saves the operator's
            # room number. Reading this public setting never reads its credentials.
            appearance = getattr(workbench, 'appearance', None)
            service = getattr(appearance, 'service', None)
            room = service.settings().get('room_id') if service else None
            if isinstance(room, str) and re.fullmatch(r'[0-9]{1,32}', room):
                self.room_id, self.enabled, self.state = room, True, 'waiting'

    def _load(self):
        if not self.path or not self.path.exists():
            return
        try:
            value = json.loads(self.path.read_text())
            self.base_url, self.room_id = settings(value['base_url'], value['room_id'])
            self.enabled = value.get('enabled') is True
            pause = value.get('paused_live_id')
            self.paused_live_id = pause if isinstance(pause, str) and len(pause) <= 32 else None
            self.state = 'paused' if self.paused_live_id else 'waiting'
        except (OSError, ValueError, KeyError, TypeError):
            self.state, self.error_code = 'unconfigured', 'invalid_saved_source'

    def _save(self):
        if self.path:
            value = {'base_url': self.base_url, 'room_id': self.room_id,
                     'enabled': self.enabled, 'paused_live_id': self.paused_live_id}
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(value, ensure_ascii=False))
            temp.replace(self.path)

    def snapshot(self):
        return {'enabled': self.enabled, 'paused': self.paused_live_id is not None,
                'state': self.state, 'platform_status': self.platform_status,
                'base_url': self.base_url, 'room_id': self.room_id,
                'live_id': self.live_id, 'error_code': self.error_code,
                'next_check_at': self.next_check_at, 'stop_result': self.stop_result}

    def launch(self):
        if self.enabled and self.room_id and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self._run())

    async def _cancel(self):
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = None

    async def configure(self, base_url, room_id):
        base_url, room_id = settings(base_url, room_id)
        async with self.lock:
            changed = (base_url, room_id) != (self.base_url, self.room_id)
            program = self.workbench.program
            owned_running = (getattr(program, 'active', False)
                             and getattr(program, 'run_id', None) in self.run_broadcasts)
            finishing = self.finish_task is not None and not self.finish_task.done()
            if changed and owned_running and not finishing:
                # Moving the watcher would abandon the only known room/run
                # relationship while its avatar could still be billing.
                raise ValueError('source_room_in_use')
            # Refreshes/multiple tabs migrate the same saved room idempotently.
            if self.enabled and (base_url, room_id) == (self.base_url, self.room_id) and self.paused_live_id is None:
                self.launch()
                return self.intake.snapshot()
            await self._cancel()
            if (changed and getattr(program, 'active', False)
                    and getattr(program, 'run_id', None) in self.run_broadcasts):
                self.launch()
                raise ValueError('source_room_in_use')
            await self.intake.stop()
            self.base_url, self.room_id, self.enabled = base_url, room_id, True
            self.paused_live_id = None
            self.candidate_id, self.candidate_at = None, None
            self.ended_id, self.ended_at, self.ended_count = None, None, 0
            if changed:
                self.live_id = None
                self.intake.live_broadcast = None
                # Retain an unresolved cloud stop as an explicitly old-room
                # warning. Completed outcomes must not describe the new room.
                if not self.stop_result or self.stop_result.get('cloud_stop_confirmed') is not False:
                    self.stop_result = None
            self.state, self.error_code = 'waiting', None
            self._save()
            self.launch()
            return self.intake.snapshot()

    async def pause(self):
        async with self.lock:
            await self._cancel()
            self.paused_live_id = self.live_id or self.candidate_id or 'pending'
            self.candidate_id, self.candidate_at = None, None
            self.ended_id, self.ended_at, self.ended_count = None, None, 0
            if self.stop_result and self.stop_result.get('cloud_stop_confirmed') is not False:
                self.stop_result = None
            self.state = 'paused'
            self._save()
            await self.intake.stop()
            # Continue the read-only watcher: a genuinely new broadcast resumes.
            self.launch()
            return self.intake.snapshot()

    async def close(self):
        await self._cancel()
        if self.finish_task and not self.finish_task.done():
            await asyncio.shield(self.finish_task)
        await self.intake.stop()

    def observe_program_run(self):
        """Bind only while a platform-live observation names this candidate.

        The HTTP start handler invokes this synchronously: a manual start in the
        five-second confirmation window cannot become a guessed old run at end.
        A run already bound to A stays A when a new B first appears.
        """
        program = self.workbench.program
        run_id = getattr(program, 'run_id', None)
        if (self.platform_status != 'live' or not self.candidate_id or not run_id
                or not getattr(program, 'active', False)):
            return
        identity = (self.room_id, self.candidate_id)
        previous = self.run_broadcasts.get(run_id)
        if previous is not None and previous != identity:
            return
        self.run_broadcasts[run_id] = identity
        self.run_bindings[identity] = run_id

    async def _finish_broadcast(self, ident):
        if ident in self.finalized:
            return
        self.finalized.add(ident)
        room = self.room_id
        bound_run = self.run_bindings.get((room, ident))

        async def finish():
            self.state = 'finishing'
            await self.intake.stop()
            program = self.workbench.program
            if bound_run is None or getattr(program, 'run_id', None) != bound_run:
                self.stop_result = {'room_id': room, 'live_id': ident, 'run_id': bound_run,
                                    'state': 'run_changed' if bound_run else 'no_bound_run',
                                    'cloud_stop_confirmed': None}
                self.state = 'waiting'
                return
            try:
                # Use the operator's normal stop and cloud-ended verification.
                await program.stop()
            except Exception:
                self.stop_result = {'room_id': room, 'live_id': ident, 'run_id': bound_run,
                                    'state': 'stop_unconfirmed', 'cloud_stop_confirmed': False}
                self.state, self.error_code = 'stop_unconfirmed', 'cloud_stop_unconfirmed'
                return
            cleanup = getattr(program, 'cleanup', {}) or {}
            self.stop_result = {'room_id': room, 'live_id': ident, 'run_id': bound_run,
                                'cloud_stop_confirmed': cleanup.get('cloud_stop_confirmed'),
                                'state': getattr(program, 'state', 'stopped')}
            self.state = ('stop_unconfirmed' if self.stop_result['cloud_stop_confirmed'] is False
                          else 'waiting')

        self.finish_task = asyncio.create_task(finish())
        try:
            await asyncio.shield(self.finish_task)
        except asyncio.CancelledError:
            # pause, change-room and shutdown may cancel the watcher. Once stop
            # starts, its cloud verification must finish before releasing it.
            await asyncio.shield(self.finish_task)
            raise

    async def _unknown(self):
        self.platform_status = 'unknown'
        self.candidate_id, self.candidate_at = None, None
        self.ended_id, self.ended_at, self.ended_count = None, None, 0
        self.intake.live_broadcast = None
        if self.intake.active:
            await self.intake.stop()

    def _identity(self, value):
        if not isinstance(value, dict) or value.get('roomId') != self.room_id:
            return 'unknown', None
        ident = value.get('liveId')
        if (value.get('evidence') != 'roomStore.roomInfo.room' or not isinstance(ident, str)
                or not re.fullmatch(r'[0-9]{1,32}', ident) or ident == self.room_id):
            return 'unknown', None
        state = value.get('status')
        return (state, ident) if state in {'live', 'ended'} else ('unknown', None)

    async def check_once(self):
        value = await self.probe(self.base_url, self.room_id)
        status, ident = self._identity(value)
        self.platform_status, self.error_code = status, None
        now = self.clock()
        if status != 'ended':
            self.ended_id, self.ended_at, self.ended_count = None, None, 0
        if status == 'unknown':
            await self._unknown()
            self.state = 'paused' if self.paused_live_id else 'unknown'
            return self.POLL_SECONDS
        if status == 'ended':
            self.candidate_id, self.candidate_at = None, None
            self.state = 'paused' if self.paused_live_id else 'waiting'
            # Three explicit observations of OUR confirmed broadcast, separated
            # by >=5 seconds. A stale old id must never stop a new cloud session.
            if ident != self.live_id or ident in self.finalized:
                self.ended_id, self.ended_at, self.ended_count = None, None, 0
                return self.POLL_SECONDS
            if self.intake.active:
                await self.intake.stop()
            if self.ended_id != ident:
                self.ended_id, self.ended_at, self.ended_count = ident, now, 1
            else:
                self.ended_count += 1
            self.state = 'confirming_end'
            if self.ended_count >= 3 and now - self.ended_at >= self.CONFIRM_SECONDS:
                await self._finish_broadcast(ident)
            return self.END_SAMPLE_SECONDS
        if self.stop_result and self.stop_result.get('cloud_stop_confirmed') is False:
            # Free capture can recover; Program's existing cloud-state guard
            # still prevents opening another paid session after uncertain stop.
            self.error_code = 'cloud_stop_unconfirmed'
        if ident in self.finalized:
            self.state = 'waiting'
            return self.POLL_SECONDS
        if self.paused_live_id:
            if self.paused_live_id == 'pending':
                self.paused_live_id = ident
                self._save()
            if self.paused_live_id == ident:
                # Pause stops collection only. Keep observing this real room
                # and its manually started run so actual down can still close it.
                if self.candidate_id != ident:
                    self.candidate_id, self.candidate_at = ident, now
                    self.observe_program_run()
                    self.state = 'paused'
                    return self.CONFIRM_SECONDS
                self.observe_program_run()
                if now - self.candidate_at >= self.CONFIRM_SECONDS:
                    self.live_id = ident
                self.state = 'paused'
                return self.POLL_SECONDS
            self.paused_live_id = None
            self._save()
        if self.candidate_id != ident:
            # Do not admit comments from a newly observed stream into old boards.
            if self.intake.active:
                await self.intake.stop()
            self.candidate_id, self.candidate_at = ident, now
            self.observe_program_run()
            self.state = 'confirming_live'
            return self.CONFIRM_SECONDS
        if now - self.candidate_at < self.CONFIRM_SECONDS:
            self.observe_program_run()
            self.state = 'confirming_live'
            return self.CONFIRM_SECONDS - (now - self.candidate_at)
        self.observe_program_run()
        if self.live_id and self.live_id != ident:
            # A twice-confirmed new platform room also proves the prior room
            # ended when its explicit status=4 was missed during a disconnect.
            await self._finish_broadcast(self.live_id)
        binding = {'room_id': self.room_id, 'live_id': ident}
        self.workbench.game.bind_live_broadcast(**binding)
        self.live_id, self.intake.live_broadcast = ident, binding
        if self.intake.active and self.intake.state in {'closed', 'error', 'stopped'}:
            await self.intake.stop()
        await self.intake.start(self.base_url, self.room_id, 'broadcast')
        self.state = 'receiving'
        return self.POLL_SECONDS

    async def _run(self):
        delay = self.POLL_SECONDS
        try:
            while self.enabled:
                try:
                    interval = await self.check_once()
                    if self.platform_status == 'unknown':
                        interval, delay = max(interval, delay), min(delay * 2, self.ERROR_MAX_SECONDS)
                    else:
                        delay = self.POLL_SECONDS
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await self._unknown()
                    self.platform_status, self.error_code = 'unknown', 'probe_unavailable'
                    self.state = 'paused' if self.paused_live_id else 'retrying'
                    interval, delay = delay, min(delay * 2, self.ERROR_MAX_SECONDS)
                self.next_check_at = self.clock() + interval
                await asyncio.sleep(interval)
        finally:
            self.next_check_at = None
