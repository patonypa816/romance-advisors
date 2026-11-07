"""Synthetic source -> real SSE parser -> game receipt -> frozen award."""
import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from romance.dyhub_intake import DyhubIntake, source_identity
from romance.server import Workbench, create_app
from test_game import FakeModel
from test_program import FakeAvatar

AVATAR = 'https://p3.douyinpic.com/aweme/synthetic-only.jpeg'


class IntakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.model = FakeModel()
        self.avatar = FakeAvatar()
        self.wb = Workbench(Path(self.temp.name) / 'game.sqlite', model=self.model, avatar_client=self.avatar)
        await self.wb.game.new_episode()
        self.intake = DyhubIntake(self.wb)
        self.intake.episode_id = self.wb.game.episode['id']
        self.intake.room_id = '123'
        self.intake.stop_event = asyncio.Event()
        self.intake.started_at = time.time()

    async def asyncTearDown(self):
        await self.intake.stop()
        await self.wb.close()
        self.temp.cleanup()

    def event(self, ident='one', viewer='viewer', text='建议认真解释', **changes):
        now = int(time.time() * 1000)
        return {'platform': 'douyin', 'type': 'chat', 'roomId': '123', 'id': ident,
                'ts': now, 'receivedAt': now, 'data': {'content': text},
                'user': {'id': viewer, 'nickname': '同名测试', 'avatar': AVATAR, 'secUid': 'SOURCE_PRIVATE'}, **changes}

    async def test_author_and_avatar_reach_award_not_model_or_status(self):
        await self.intake.on_event(self.event())
        game = self.wb.game
        self.assertEqual(game.data['proposals'][0]['avatar_url'], AVATAR)
        self.model.delta = 100
        await game.close_collection()
        if game.current_round['phase'] == 'VOTING':
            await game.close_voting()
        award = game.public_state()['finale']['awards']['best']
        self.assertEqual(award['author_id'], source_identity('viewer', '123', 'viewer'))
        self.assertEqual(award['avatar_url'], AVATAR)
        self.assertNotIn(AVATAR, json.dumps(self.model.calls))
        self.assertNotIn(AVATAR, json.dumps(self.intake.snapshot()))
        self.assertNotIn('SOURCE_PRIVATE', json.dumps(game.data))
        self.assertEqual(self.avatar.sent, [])

    async def test_duplicate_survives_phase_change_and_no_new_avatar_overwrites(self):
        event = self.event()
        await self.intake.on_event(event)
        await self.wb.game.close_collection()
        changed = copy.deepcopy(event)
        changed['user']['avatar'] = AVATAR + '?changed=1'
        await self.intake.on_event(changed)
        self.assertEqual(len(self.wb.game.data['comments']), 1)
        self.assertEqual(self.intake.counts['duplicate'], 1)
        self.assertEqual(self.wb.game.data['comments'][0]['avatar_url'], AVATAR)

    async def test_same_nickname_has_distinct_room_scoped_identity(self):
        await self.intake.on_event(self.event('one', 'a'))
        await self.intake.on_event(self.event('two', 'b'))
        ids = [p['author_id'] for p in self.wb.game.data['proposals']]
        self.assertEqual(len(set(ids)), 2)
        self.assertNotEqual(source_identity('viewer', '123', 'a'), source_identity('viewer', '456', 'a'))

    async def test_missing_or_unsafe_avatar_does_not_drop_suggestion(self):
        for i, value in enumerate((None, 'http://127.0.0.1/private', 'data:image/png;base64,AAA',
                                   'https://douyinpic.com.evil.invalid/a', 'https://p3.douyinpic.com/a.svg')):
            event = self.event(str(i))
            event['user']['avatar'] = value
            await self.intake.on_event(event)
        self.assertEqual(len(self.wb.game.data['proposals']), 5)
        self.assertTrue(all('avatar_url' not in p for p in self.wb.game.data['proposals']))

    async def test_busy_chat_still_reaches_interaction_without_retaining_avatar(self):
        for i, text in enumerate(('1', '投1', '投 1')):
            await self.intake.on_event(self.event(f'early-vote-{i}', text=text))
        await self.intake.on_event(self.event('gift', type='gift'))
        await self.intake.on_event(self.event('room', roomId='999'))
        self.wb.game.busy = True
        await self.intake.on_event(self.event('busy'))
        self.wb.game.busy = False
        comments = self.wb.game.data['comments']
        self.assertEqual(len(comments), 4)
        self.assertEqual(self.wb.game.data['proposals'], [])
        self.assertTrue(all('avatar_url' not in row for row in comments))
        self.assertTrue(all(row['result'] == 'ignored' for row in comments))

    async def test_votes_are_accepted_without_retaining_pictures(self):
        await self.intake.on_event(self.event())
        await self.wb.game.close_collection()
        self.assertEqual(self.wb.game.current_round['phase'], 'VOTING')
        for i, text in enumerate(('1', '投1', '投 1'), start=1):
            await self.intake.on_event(self.event(f'vote-{i}', f'viewer-{i}', text))
            ballot = self.wb.game.data['comments'][-1]
            self.assertEqual((ballot['text'], ballot['result']), (text, 'voted'))
            self.assertNotIn('avatar_url', ballot)
            self.assertEqual(self.wb.game.public_state()['round']['options'][0]['votes'], i)
        await self.intake.on_event(self.event('chat', 'voter', '围观'))
        comments = self.wb.game.data['comments']
        self.assertEqual(len(comments), 5)
        self.assertEqual(comments[-2]['result'], 'voted')
        self.assertEqual(comments[-1]['result'], 'ignored')
        self.assertNotIn('avatar_url', comments[-1])
        self.assertNotIn('avatar_url', comments[-2])

    async def test_between_round_chat_retained_without_avatar_or_new_proposal(self):
        await self.intake.on_event(self.event())
        await self.wb.game.close_collection()
        await self.wb.game.close_voting()
        self.assertEqual(self.wb.game.current_round['phase'], 'DONE')
        await self.intake.on_event(self.event('between-rounds', 'chat-only', '这段有意思'))
        last = self.wb.game.data['comments'][-1]
        self.assertEqual(last['text'], '这段有意思')
        self.assertEqual(last['result'], 'ignored')
        self.assertNotIn('avatar_url', last)
        self.assertEqual(len(self.wb.game.data['proposals']), 1)

    async def test_stale_future_missing_and_oversized_event_rejected(self):
        now = time.time() * 1000
        for i, change in enumerate(({'ts': now - 46000}, {'ts': now + 6000}, {'ts': None},
                                    {'receivedAt': float('nan')}, {'user': {'id': ''}},
                                    {'data': {'content': 'x' * 2001}})):
            await self.intake.on_event(self.event(str(i), **change))
        self.assertEqual(self.wb.game.data['comments'], [])

    async def test_episode_limit_closes_subscription(self):
        with patch('romance.dyhub_intake.MAX_EPISODE_COMMENTS', 2):
            await self.intake.on_event(self.event('one'))
            await self.intake.on_event(self.event('two'))
            await self.intake.on_event(self.event('three'))
        self.assertEqual(len(self.wb.game.data['comments']), 2)
        self.assertTrue(self.intake.stop_event.is_set())
        self.assertEqual(self.intake.error_code, 'episode_limit')

    def broadcast(self, mode="chat"):
        self.intake.scope = "broadcast"
        self.intake.started_at -= 1
        self.offered = []
        self.wb.program.ready = True
        self.wb.program.state = "running"
        self.wb.program.content_mode = mode
        self.wb.program._game_baseline_episode_id = None
        self.wb.program.casual.offer = self.offered.append

    async def test_broadcast_chat_ignores_old_active_game_and_routes_only_once(self):
        self.broadcast()
        event = self.event(text='<b>真实评论</b>')
        await self.intake.on_event(event)
        self.wb.program.content_mode = 'game'
        await self.intake.on_event(event)
        self.assertEqual(self.wb.game.data['comments'], [])
        self.assertEqual(len(self.offered), 1)
        self.assertEqual(self.intake.counts['duplicate'], 1)
        recent = self.intake.snapshot()['recent_comments']
        self.assertEqual(recent[0]['text'], '<b>真实评论</b>')
        self.assertNotIn(AVATAR, json.dumps(recent))

    async def test_broadcast_transition_and_wrong_episode_never_vote_or_replay(self):
        self.broadcast('to_game')
        event = self.event(text='投1')
        await self.intake.on_event(event)
        self.wb.program.content_mode = 'game'
        self.wb.program._game_baseline_episode_id = self.wb.game.episode['id']
        await self.intake.on_event(self.event('wrong-episode'))
        self.wb.program._game_baseline_episode_id = None
        await self.intake.on_event(event)
        self.assertEqual(self.wb.game.data['comments'], [])
        self.assertEqual(self.offered, [])
        await self.intake.on_event(self.event('stable-game'))
        self.assertEqual(len(self.wb.game.data['comments']), 1)
        self.assertEqual(len(self.intake.snapshot()['recent_comments']), 2)

    async def test_broadcast_game_limit_keeps_room_and_chat_alive_across_new_episode(self):
        self.broadcast('game')
        with patch('romance.dyhub_intake.MAX_EPISODE_COMMENTS', 1):
            await self.intake.on_event(self.event('one'))
            await self.intake.on_event(self.event('over-limit'))
            self.assertFalse(self.intake.stop_event.is_set())
            self.assertEqual(len(self.wb.game.data['comments']), 1)
            self.wb.program.content_mode = 'chat'
            await self.intake.on_event(self.event('chat-after-game'))
            self.assertEqual(len(self.offered), 1)
            await self.wb.game.new_episode()
            self.wb.program.content_mode = 'game'
            self.wb.program._game_baseline_episode_id = None
            await self.intake.on_event(self.event('new-game'))
            self.assertEqual(self.intake.episode_count, 1)
            self.assertEqual(len(self.wb.game.data['comments']), 2)

    async def test_broadcast_inactive_program_still_previews_bounded_real_comments(self):
        self.broadcast()
        self.wb.program.ready = False
        for i in range(45):
            await self.intake.on_event(self.event(str(i)))
        self.assertEqual(len(self.intake.snapshot()['recent_comments']), 40)
        self.assertEqual(self.offered, [])
        self.assertEqual(self.wb.game.data['comments'], [])
        self.assertEqual(self.intake.counts['received'], 45)

    async def test_broadcast_seen_capacity_is_bounded_without_replayable_eviction(self):
        self.broadcast()
        with patch('romance.dyhub_intake.MAX_SEEN_EVENTS', 2):
            await self.intake.on_event(self.event('one'))
            await self.intake.on_event(self.event('two'))
            await self.intake.on_event(self.event('three'))
            await self.intake.on_event(self.event('one'))
        self.assertEqual(len(self.intake.seen), 2)
        self.assertEqual(len(self.offered), 2)
        self.assertEqual(self.intake.counts['duplicate'], 1)

    async def test_broadcast_start_does_not_replay_preconnection_backlog(self):
        self.broadcast()
        self.intake.started_at = time.time()
        await self.intake.on_event(self.event(ts=(time.time() - 1) * 1000))
        self.assertEqual(self.offered, [])
        self.assertEqual(self.intake.snapshot()['recent_comments'], [])


class SSEIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_start_real_parser_dedupe_award_and_automatic_stop(self):
        model, avatar = FakeModel(), FakeAvatar()
        app = create_app(':memory:', model=model, avatar_client=avatar)
        client = TestClient(TestServer(app))
        release = asyncio.Event()
        hits = []
        async def events(request):
            hits.append(dict(request.query))
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            now = int(time.time() * 1000)
            frame = {'id': 'synthetic', 'platform': 'douyin', 'type': 'chat', 'roomId': '123',
                     'ts': now, 'receivedAt': now, 'user': {'id': 'viewer', 'nickname': '军师', 'avatar': AVATAR,
                     'secUid': 'SOURCE_PRIVATE'}, 'data': {'content': '建议认真解释', 'raw': 'SOURCE_PRIVATE'}}
            for event in (dict(frame, type='member'), frame, frame):
                await response.write(('data: ' + json.dumps(event) + '\n\n').encode())
            await release.wait()
            return response
        source_app = web.Application()
        source_app.router.add_get('/api/events', events)
        source = TestServer(source_app)
        await source.start_server()
        await client.start_server()
        try:
            initial = await (await client.get('/api/source')).json()
            self.assertFalse(initial['active'])
            self.assertEqual(hits, [])
            body = {'base_url': str(source.make_url('')).rstrip('/'), 'room_id': '123'}
            self.assertEqual((await client.post('/api/source/start', json=body)).status, 409)
            await app['workbench'].game.new_episode()
            self.assertEqual((await client.post('/api/source/start', json=body)).status, 200)
            self.assertEqual((await client.post('/api/source/start', json=body)).status, 200)
            async def accepted():
                while app['dyhub_intake'].counts['duplicate'] != 1:
                    await asyncio.sleep(.01)
            await asyncio.wait_for(accepted(), 2)
            self.assertEqual(hits, [{'roomId': '123', 'types': 'chat,roomStatus'}])
            game = app['workbench'].game
            self.assertEqual(len(game.data['comments']), 1)
            self.assertEqual(game.data['proposals'][0]['avatar_url'], AVATAR)
            self.assertNotIn('SOURCE_PRIVATE', json.dumps(game.data))
            self.assertNotIn(AVATAR, json.dumps(await (await client.get('/api/source')).json()))
            model.delta = 100
            await game.close_collection()
            await game.close_voting()
            self.assertEqual(game.public_state()['finale']['awards']['best']['avatar_url'], AVATAR)
            await asyncio.wait_for(app['dyhub_intake'].task, 2)
            self.assertEqual(app['dyhub_intake'].error_code, 'episode_ended')
            self.assertFalse(app['dyhub_intake'].active)
            self.assertEqual(avatar.sent, [])
            self.assertNotIn(AVATAR, json.dumps(model.calls))
            self.assertEqual((await client.post('/api/source/stop', json={})).status, 200)
        finally:
            release.set()
            await client.close()
            await source.close()

    async def test_api_rejects_remote_and_cross_site(self):
        app = create_app(':memory:', model=FakeModel(), avatar_client=FakeAvatar())
        async with TestClient(TestServer(app)) as client:
            await app['workbench'].game.new_episode()
            for url in ('https://evil.invalid', 'http://127.0.0.1:8757?token=x', 'http://localhost:8757'):
                response = await client.post('/api/source/start', json={'base_url': url, 'room_id': '123'})
                self.assertEqual(response.status, 409)
            response = await client.post('/api/source/start', json={'base_url': 'http://127.0.0.1:8757', 'room_id': '123'},
                                         headers={'Origin': 'https://evil.invalid'})
            self.assertEqual(response.status, 403)


class BroadcastLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_broadcast_connects_without_episode_and_keeps_one_subscription(self):
        class LocalClient:
            instances = []
            def __init__(self, base_url, room_id, *args, **kwargs):
                self.base_url, self.room_id = base_url, room_id
                self.instances.append(self)
            async def run(self, stopped):
                await stopped.wait()
            async def close(self):
                pass
        app = create_app(':memory:', model=FakeModel(), avatar_client=FakeAvatar())
        intake = app['dyhub_intake']
        intake.client_factory = LocalClient
        async with TestClient(TestServer(app)) as client:
            body = {'base_url': 'http://127.0.0.1:8757', 'room_id': '123', 'scope': 'broadcast'}
            self.assertEqual((await client.post('/api/source/start', json=body)).status, 200)
            self.assertEqual((await client.post('/api/source/start', json=body)).status, 200)
            await app['workbench'].game.new_episode()
            await asyncio.sleep(.25)
            self.assertTrue(intake.active)
            self.assertEqual(len(LocalClient.instances), 1)
            changed = dict(body, room_id='456')
            self.assertEqual((await client.post('/api/source/start', json=changed)).status, 409)
            self.assertEqual((await client.post('/api/source/stop', json={})).status, 200)
            self.assertFalse(intake.active)
            for value in ('other', [], None):
                self.assertEqual((await client.post('/api/source/start', json=dict(body, scope=value))).status, 409)
