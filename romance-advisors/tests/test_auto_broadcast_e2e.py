"""Whole A/B broadcast contract through local HTTP and the real SSE parser.

Only temporary SQLite/settings and two random 127.0.0.1 listeners are allowed.
The room website, Cookie store, model provider and avatar cloud are never used.
"""
import asyncio
import json
from pathlib import Path
import socket
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from romance.dyhub_intake import source_identity
from romance.server import create_app
from test_game import FakeModel
from test_program import FakeAvatar


class AutoBroadcastHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_room_auto_sse_a_reconnect_end_then_b_has_separate_real_board(self):
        with tempfile.TemporaryDirectory(prefix='broadcast-e2e-') as directory:
            root = Path(directory).resolve()
            queues, requests, allowed_ports = [], [], set()
            platform = {'status': 'live', 'liveId': '900001'}
            audit = {'network': 0, 'temporary_reads': 0, 'sqlite': 0}

            async def probe(request):
                requests.append(('probe', request.path))
                return web.json_response({'roomId': '123', **platform,
                    'evidence': 'roomStore.roomInfo.room', 'checkedAt': int(time.time() * 1000)})

            async def events(request):
                requests.append(('sse', dict(request.query)))
                queue = asyncio.Queue()
                queues.append(queue)
                response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
                await response.prepare(request)
                await response.write(b'data: {"type":"__connected","roomId":"123"}\n\n')
                try:
                    while True:
                        event = await queue.get()
                        if event is None:
                            return response
                        await response.write(('data: ' + json.dumps(event) + '\n\n').encode())
                except (ConnectionError, asyncio.CancelledError):
                    return response

            source_app = web.Application()
            source_app.router.add_get('/api/rooms/123/live-status', probe)
            source_app.router.add_get('/api/events', events)
            source = TestServer(source_app, host='127.0.0.1')
            await source.start_server()
            allowed_ports.add(source.port)
            database = root / 'game.sqlite'
            (root / 'game.source.json').write_text(json.dumps({
                'base_url': str(source.make_url('')).rstrip('/'), 'room_id': '123',
                'enabled': True, 'paused_live_id': None}))

            original_connect = socket.socket.connect
            original_read_text, original_read_bytes = Path.read_text, Path.read_bytes
            original_sqlite = sqlite3.connect

            def local_connect(sock, address):
                # This guard includes the game HTTP client and collector's probe
                # and SSE connections. Accidental :8757 or external traffic fails.
                self.assertIsInstance(address, tuple)
                self.assertEqual(address[0], '127.0.0.1')
                self.assertIn(address[1], allowed_ports)
                audit['network'] += 1
                return original_connect(sock, address)

            def temporary_read(path, original, *args, **kwargs):
                self.assertTrue(path.resolve().is_relative_to(root), str(path))
                audit['temporary_reads'] += 1
                return original(path, *args, **kwargs)

            def temporary_sqlite(path, *args, **kwargs):
                self.assertTrue(Path(path).resolve().is_relative_to(root), str(path))
                audit['sqlite'] += 1
                return original_sqlite(path, *args, **kwargs)

            # Source modules are imported above; runtime data access is restricted
            # from here through shutdown, including every fake provider callback.
            with patch.object(socket.socket, 'connect', local_connect), \
                 patch.object(Path, 'read_text', lambda path, *a, **k: temporary_read(path, original_read_text, *a, **k)), \
                 patch.object(Path, 'read_bytes', lambda path, *a, **k: temporary_read(path, original_read_bytes, *a, **k)), \
                 patch.object(sqlite3, 'connect', temporary_sqlite), \
                 patch('romance.game.WOMAN_TYPING_SECONDS', 0):
                model, avatar = FakeModel(), FakeAvatar()
                app = create_app(database, model=model, avatar_client=avatar, obs_output=True)
                auto, intake, wb = app['auto_source'], app['dyhub_intake'], app['workbench']
                # Accelerate only routine polling; the actual 5-second live and
                # end confirmation conditions retain their production duration.
                auto.POLL_SECONDS = .05
                client = TestClient(TestServer(app, host='127.0.0.1'))
                await client.start_server()
                allowed_ports.add(client.server.port)

                async def wait_for(predicate, *, timeout=9):
                    async with asyncio.timeout(timeout):
                        while not predicate():
                            await asyncio.sleep(.01)

                async def post(path, body=None):
                    response = await client.post('/api/' + path, json=body or {})
                    value = await response.json()
                    self.assertLess(response.status, 300, value)
                    if wb.job:
                        await wb.job
                        self.assertIsNone(wb.error)
                        self.assertIsNone(wb.game.data['error'])
                    return value

                async def boards():
                    response = await client.get('/api/obs-output')
                    self.assertEqual(response.status, 200)
                    return (await response.json())['leaderboards']

                async def settle(viewer, ident):
                    stamp = int(time.time() * 1000) + 1
                    queue = queues[-1]
                    await queue.put({'id': ident, 'type': 'chat', 'platform': 'douyin',
                        'roomId': '123', 'ts': stamp, 'receivedAt': stamp,
                        'user': {'id': viewer, 'nickname': viewer},
                        'data': {'content': '建议认真解释'}})
                    receipt_id = source_identity('message', '123', ident)
                    await wait_for(lambda: wb.game._get('comments', receipt_id))
                    receipt = wb.game._get('comments', receipt_id)
                    self.assertEqual(receipt['live_broadcast_id'], wb.game.data['live_broadcast_id'])
                    if wb.program.active:
                        await wait_for(lambda: wb.game.current_round['phase'] == 'DONE', timeout=20)
                    else:
                        await post('collection/close')
                        if wb.game.current_round['phase'] == 'VOTING':
                            await post('voting/close')
                    return await boards()

                try:
                    # No /api/source POST: the persisted room alone starts one
                    # process-owned watcher, then real local HTTP -> SSE intake.
                    await wait_for(lambda: intake.active and bool(queues))
                    self.assertEqual(auto.live_id, '900001')
                    self.assertEqual(len(queues), 1)
                    self.assertEqual(avatar.calls, [])
                    binding_a = wb.game.data['live_broadcast_id']
                    await post('episode/new')
                    board_a = await settle('Alice', 'a-advice')
                    self.assertEqual(board_a['best'][0]['author_id'], source_identity('viewer', '123', 'Alice'))
                    self.assertEqual(board_a['best'][0]['adopted'], 1)

                    # EOF forces the real bounded SSE retry path; the same
                    # broadcast binding and official board must survive.
                    await queues[-1].put(None)
                    await wait_for(lambda: len(queues) == 2)
                    self.assertEqual(wb.game.data['live_broadcast_id'], binding_a)
                    self.assertEqual(await boards(), board_a)

                    # The normal manual start API uses only FakeAvatar. Its run
                    # is associated synchronously before platform end arrives.
                    await post('program/start', {'mode': 'chat'})
                    await wb.program.task
                    self.assertEqual(wb.program.state, 'running')
                    wb.program.casual.next_attempt = time.monotonic() + 60
                    run_a = wb.program.run_id
                    platform['status'] = 'ended'
                    await wait_for(lambda: auto.stop_result is not None)
                    stop = auto.stop_result
                    self.assertEqual(stop['run_id'], run_a)
                    self.assertTrue(stop['cloud_stop_confirmed'])
                    self.assertEqual(avatar.state, 'ended')
                    self.assertEqual(avatar.calls.count('stop'), 1)
                    self.assertFalse(intake.active)
                    self.assertEqual(await boards(), board_a)

                    platform.update(status='live', liveId='900002')
                    await wait_for(lambda: intake.active and auto.live_id == '900002' and len(queues) == 3)
                    self.assertNotEqual(wb.game.data['live_broadcast_id'], binding_a)
                    self.assertEqual(await boards(), {'best': [], 'worst': []})
                    # Starting B's game remains an explicit operator action.
                    # The real scheduler drives the round; only its model and
                    # avatar transports are synthetic. No stopped actor is reused.
                    await post('settings', {'auto_advance': False, 'collect_seconds': 5, 'vote_seconds': 5})
                    wb.program.ambient.tick = Mock()  # unrelated ambient authoring is outside this fixture
                    await post('program/start', {'mode': 'game'})
                    await wb.program.task
                    await wb.job
                    self.assertEqual(wb.program.state, 'running')
                    board_b = await settle('Bob', 'b-advice')
                    self.assertEqual(len(board_b['best']), 1)
                    self.assertEqual(board_b['best'][0]['author_id'], source_identity('viewer', '123', 'Bob'))
                    self.assertEqual(board_b['best'][0]['adopted'], 1)
                    self.assertEqual(len(wb.game.data['live_broadcasts']), 2)
                    self.assertEqual(len([r for kind, r in requests if kind == 'sse']), 3)
                    self.assertEqual(avatar.calls.count('start'), 2)
                    self.assertGreater(audit['network'], 0)
                    self.assertGreater(audit['temporary_reads'], 0)
                    self.assertEqual(audit['sqlite'], 1)
                finally:
                    await client.close()
                    for queue in queues:
                        await queue.put(None)
                    await source.close()


if __name__ == '__main__':
    unittest.main()
