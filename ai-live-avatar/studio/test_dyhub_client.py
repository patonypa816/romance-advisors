"""Local fake-service contracts; never connects to Douyin or starts dyhub."""
import asyncio
import json
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer

from dyhub_client import (DyhubClient, StreamProtocolError, _safe_business_event,
                         normalize_endpoint, parse_sse)


async def chunks(*values):
    for value in values:
        yield value


class ParserTests(unittest.IsolatedAsyncioTestCase):
    async def test_multiline_comments_chunk_boundaries_and_cr(self):
        values = [value async for value in parse_sse(chunks(
            b'\xef\xbb\xbf:hb\r', b'\nretry: 1\r\ndata: {"type":\r\ndata: "chat"}\r\n\r\n',
            b'data: {"a":1}\r\r', b'data: {"unfinished":true}'))]
        self.assertEqual(values, [{"type": "chat"}, {"a": 1}])

    async def test_invalid_and_oversized_input(self):
        for value in (b'data: invalid\n\n', b'data: []\n\n', b'data: \xff\n\n',
                      b'x' * 16385, b':hb\n' * 20000):
            with self.subTest(size=len(value)):
                with self.assertRaises(StreamProtocolError):
                    async for _ in parse_sse(chunks(value)):
                        pass

    def test_endpoint_and_room_validation(self):
        self.assertEqual(normalize_endpoint('http://127.0.0.1:8000'), 'http://127.0.0.1:8000')
        for url in ('https://127.0.0.1:80', 'http://localhost:80', 'http://127.0.0.2:80',
                    'http://u:p@127.0.0.1:80', 'http://127.0.0.1:80/',
                    'http://127.0.0.1:80?token=x', 'http://127.0.0.1:0',
                    'http://127.0.0.1:65536', 'http://127.0.0.1:80#x'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_endpoint(url)
        for room in ('', '12abc', '1' * 33, 123, '１２３'):
            with self.subTest(room=room), self.assertRaises(ValueError):
                DyhubClient('http://127.0.0.1:80', room, None, None)


class ClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.servers = []
        self.clients = []
        self.tasks = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        for server in self.servers:
            await server.close()

    async def start(self, handler):
        app = web.Application()
        app.router.add_get('/api/events', handler)
        server = TestServer(app)
        await server.start_server()
        self.servers.append(server)
        return str(server.make_url('')).rstrip('/')

    def client(self, url, on_event, on_status):
        client = DyhubClient(url, '123', on_event, on_status)
        client.RETRY_INITIAL = .01
        client.RETRY_MAX = .02
        self.clients.append(client)
        return client

    def run_client(self, client, stop):
        task = asyncio.create_task(client.run(stop))
        self.tasks.append(task)
        return task

    async def test_controls_filter_sanitize_and_stop_idle(self):
        release = asyncio.Event()
        received = asyncio.Event()
        events, statuses = [], []
        async def handler(request):
            self.assertEqual(dict(request.query), {'roomId': '123', 'types': 'chat,gift,member,roomStatus'})
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            frames = [
                {'type': '__connecting', 'roomId': '123'},
                {'type': '__connected', 'roomId': '123', 'room': {'secret': 'hidden'}},
                {'type': 'roomStatus', 'roomId': '123', 'data': {'status': 'live', 'message': 'secret'}},
                {'type': 'chat', 'platform': 'douyin', 'roomId': '999', 'id': 'other', 'data': {'content': 'wrong'}},
                {'type': 'chat', 'platform': 'other', 'roomId': '123', 'id': 'bad-platform', 'data': {'content': 'wrong'}},
                {'type': 'chat', 'roomId': '123', 'id': 'missing-platform', 'data': {'content': 'wrong'}},
                {'type': 'chat', 'platform': 'Douyin', 'roomId': '123', 'id': 'wrong-case', 'data': {'content': 'wrong'}},
                {'type': 'chat', 'platform': 'douyin', 'roomId': '123', 'id': 'one', 'data': {'content': 'hello', 'raw': 'secret'},
                 'user': {'id': 'u', 'nickname': 'n', 'avatar': 'https://secret', 'secUid': 'secret'},
                 'ts': 123, 'receivedAt': 456, 'raw': 'secret'},
            ]
            for frame in frames:
                await response.write(('data: ' + json.dumps(frame) + '\n\n').encode())
            await release.wait()
            return response
        async def callback(event):
            events.append(event)
            received.set()
        stop = asyncio.Event()
        client = self.client(await self.start(handler), callback, statuses.append)
        task = self.run_client(client, stop)
        await asyncio.wait_for(received.wait(), 1)
        stop.set()
        await asyncio.wait_for(task, 1)
        release.set()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['platform'], 'douyin')
        self.assertEqual(events[0]['user'], {'id': 'u', 'nickname': 'n'})
        self.assertNotIn('secret', json.dumps(events + statuses))
        self.assertTrue(any(s['state'] == 'live' for s in statuses))
        self.assertIsNotNone(client.status['last_event_received_at'])
        self.assertEqual(client.status['state'], 'stopped')
        self.assertTrue(client._session.closed)

    async def test_gift_and_member_stream_delivers_only_safe_fields(self):
        received, release = asyncio.Event(), asyncio.Event()
        events, statuses = [], []
        frames = [
            {'type': 'gift', 'platform': 'douyin', 'roomId': '123', 'id': 'gift-one',
             'ts': 123, 'receivedAt': 456,
             'user': {'id': 'u', 'nickname': '观众', 'payGradeLevel': 11,
                      'level': 99, 'avatar': 'https://secret', 'secUid': 'secret'},
             'data': {'giftId': '9007199254740993', 'giftName': '测试礼物', 'groupId': 'group-one',
                      'repeatCount': 3, 'comboCount': 4, 'groupCount': 2, 'repeatEnd': False,
                      'diamondCount': 100, 'traceId': 'secret', 'giftIcon': 'https://secret'}},
            {'type': 'member', 'platform': 'douyin', 'roomId': '123', 'id': 'member-one',
             'user': {'id': 'u', 'nickname': '观众', 'payGradeLevel': 0},
             'data': {'action': 1, 'isSetToAdmin': False, 'enterType': 0,
                      'memberCount': 200, 'operator': {'secret': 'hidden'}}},
            {'type': 'member', 'platform': 'douyin', 'roomId': '123', 'id': 'unknown-member',
             'user': {'id': 'unknown', 'nickname': '观众', 'level': 99}, 'data': {}},
        ]

        async def handler(request):
            self.assertEqual(request.query['types'], 'chat,gift,member,roomStatus')
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            for frame in frames:
                await response.write(('data: ' + json.dumps(frame) + '\n\n').encode())
            await release.wait()
            return response

        async def callback(event):
            events.append(event)
            if len(events) == len(frames):
                received.set()

        stop = asyncio.Event()
        client = self.client(await self.start(handler), callback, statuses.append)
        task = self.run_client(client, stop)
        try:
            await asyncio.wait_for(received.wait(), 1)
            self.assertEqual(events[0]['data'], {
                'giftId': '9007199254740993', 'giftName': '测试礼物', 'groupId': 'group-one',
                'diamondCount': 100, 'repeatCount': 3, 'comboCount': 4, 'groupCount': 2, 'repeatEnd': False})
            self.assertEqual(events[0]['user'], {'id': 'u', 'nickname': '观众', 'payGradeLevel': 11})
            self.assertEqual(events[1]['data'], {'action': 1, 'isSetToAdmin': False, 'enterType': 0})
            self.assertEqual(events[1]['user']['payGradeLevel'], 0)
            self.assertEqual(events[2]['data'], {})
            self.assertNotIn('payGradeLevel', events[2]['user'])
            self.assertNotIn('secret', json.dumps(events + statuses))
        finally:
            stop.set()
            await asyncio.wait_for(task, 1)
            release.set()

    async def test_redirect_is_not_followed(self):
        hits = []
        async def target(request):
            hits.append(True)
            return web.Response()
        target_url = await self.start(target)
        async def redirect(request):
            return web.Response(status=302, headers={'Location': target_url + '/api/events?secret=x'})
        statuses = []
        client = self.client(await self.start(redirect), None, statuses.append)
        await asyncio.wait_for(client.run(asyncio.Event()), 1)
        self.assertEqual(hits, [])
        self.assertTrue(any(s['error_code'] == 'redirect_rejected' for s in statuses))
        self.assertNotIn('secret', json.dumps(statuses))

    async def test_reconnect_bad_frame_eof_and_close(self):
        attempts, statuses = [], []
        async def handler(request):
            attempts.append(asyncio.get_running_loop().time())
            return web.Response(body=b'data: invalid\n\n' if len(attempts) == 1 else b':hb\n\n',
                                headers={'Content-Type': 'text/event-stream'})
        client = self.client(await self.start(handler), None, statuses.append)
        task = self.run_client(client, asyncio.Event())
        async def wait_attempts():
            while len(attempts) < 4:
                await asyncio.sleep(.005)
        await asyncio.wait_for(wait_attempts(), 1)
        await client.close()
        await asyncio.wait_for(task, 1)
        self.assertGreaterEqual(client.status['reconnect_count'], 3)
        self.assertGreaterEqual(attempts[1] - attempts[0], .009)
        self.assertGreaterEqual(attempts[2] - attempts[1], .019)
        self.assertLess(attempts[3] - attempts[2], .1)
        self.assertTrue(any(s['error_code'] == 'invalid_stream' for s in statuses))

    async def test_room_error_is_safe_and_retried(self):
        attempts = []
        statuses = []
        async def handler(request):
            attempts.append(True)
            return web.Response(body=b'data: {"type":"__error","roomId":"123","error":"https://secret"}\n\n',
                                headers={'Content-Type': 'text/event-stream'})
        client = self.client(await self.start(handler), None, statuses.append)
        task = self.run_client(client, asyncio.Event())
        async def wait_retry():
            while len(attempts) < 2:
                await asyncio.sleep(.005)
        await asyncio.wait_for(wait_retry(), 1)
        await client.close()
        await asyncio.wait_for(task, 1)
        self.assertTrue(any(s['error_code'] == 'room_connect_failed' for s in statuses))
        self.assertNotIn('secret', json.dumps(statuses))

    async def test_already_stopped_does_not_connect(self):
        stop = asyncio.Event()
        stop.set()
        client = self.client('http://127.0.0.1:1', None, lambda _: None)
        await client.run(stop)
        self.assertIsNone(client._session)


class BusinessEventTests(unittest.TestCase):
    def event(self, kind='member', **overrides):
        return {'type': kind, 'platform': 'douyin', 'roomId': '123', 'id': 'event-one',
                'data': {}, 'user': {'id': 'u', 'nickname': '观众'}, **overrides}

    def test_invalid_numeric_fields_remain_unknown(self):
        for invalid in (True, False, -1, 1.5, 11.0, '11', None, {}, 2**53):
            with self.subTest(value=invalid):
                member = _safe_business_event(self.event(
                    data={'action': invalid, 'enterType': invalid, 'isSetToAdmin': invalid},
                    user={'id': 'u', 'payGradeLevel': invalid, 'level': 99}), '123')
                self.assertNotIn('action', member['data'])
                self.assertNotIn('enterType', member['data'])
                self.assertNotIn('payGradeLevel', member['user'])
                if type(invalid) is not bool:
                    self.assertNotIn('isSetToAdmin', member['data'])
                gift = _safe_business_event(self.event('gift', data={
                    'giftId': 'gift', 'giftName': '礼物', 'repeatCount': invalid,
                    'comboCount': invalid, 'groupCount': invalid, 'repeatEnd': invalid}), '123')
                for field in ('repeatCount', 'comboCount', 'groupCount'):
                    self.assertNotIn(field, gift['data'])
                if type(invalid) is not bool:
                    self.assertNotIn('repeatEnd', gift['data'])

    def test_wrong_room_platform_and_malformed_identity_are_rejected(self):
        variants = ({'roomId': '999'}, {'platform': 'other'}, {'platform': None},
                    {'id': ''}, {'id': 'x' * 257}, {'id': 123}, {'data': None},
                    {'type': 'unknown'}, {'type': []}, {'type': {}})
        for overrides in variants:
            with self.subTest(overrides=overrides):
                self.assertIsNone(_safe_business_event(self.event(**overrides), '123'))
        for gift_id in ('', 'x' * 257, 123, None):
            self.assertIsNone(_safe_business_event(self.event('gift', data={
                'giftId': gift_id, 'giftName': '礼物'}), '123'))

    def test_bounds_explicit_zero_and_false_and_unknown_fields(self):
        safe = _safe_business_event(self.event('gift', data={
            'giftId': 'gift', 'giftName': '礼' * 1000, 'groupId': '0',
            'repeatCount': 0, 'comboCount': 2**53 - 1, 'groupCount': 2,
            'repeatEnd': False, 'raw': 'secret'},
            user={'id': 'u' * 300, 'nickname': 'n' * 300, 'payGradeLevel': 0,
                  'payGrade': {'level': 99}, 'secret': 'hidden'}), '123')
        self.assertEqual(len(safe['data']['giftName']), 256)
        self.assertEqual(len(safe['user']['id']), 256)
        self.assertEqual(len(safe['user']['nickname']), 256)
        self.assertEqual(safe['data']['repeatCount'], 0)
        self.assertEqual(safe['data']['comboCount'], 2**53 - 1)
        self.assertIs(safe['data']['repeatEnd'], False)
        self.assertEqual(safe['user']['payGradeLevel'], 0)
        self.assertNotIn('secret', json.dumps(safe))
        self.assertNotIn('payGrade', safe['user'])


if __name__ == '__main__':
    unittest.main()


class AppearancePriceFieldTests(unittest.TestCase):
    def test_diamond_price_is_safe_and_source_markers_are_removed(self):
        base = {'type': 'gift', 'platform': 'douyin', 'roomId': '123', 'id': 'g',
                'simulated': True, 'data': {'giftId': '1', 'giftName': '礼物',
                'diamondCount': 6, 'repeatCount': 2, 'sourceTimeKnown': True, 'combo': False}}
        safe = _safe_business_event(base, '123')
        self.assertEqual(safe['data']['diamondCount'], 6)
        self.assertEqual(safe['data']['repeatCount'], 2)
        self.assertTrue(safe['data']['sourceTimeKnown'])
        self.assertFalse(safe['data']['combo'])
        self.assertNotIn('simulated', safe)
        for invalid in (True, '6', 1.5, -1, 2**53):
            base['data']['diamondCount'] = invalid
            self.assertNotIn('diamondCount', _safe_business_event(base, '123')['data'])
