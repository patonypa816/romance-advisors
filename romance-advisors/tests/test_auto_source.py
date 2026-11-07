"""Synthetic broadcast identities only; no real DyHub, Douyin or avatar calls."""
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from romance.auto_source import AutoSource, probe_room, settings
from romance.dyhub_intake import DyhubIntake
from romance.server import Workbench, create_app
from test_game import FakeModel
from test_program import FakeAvatar
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


class Intake:
    def __init__(self):
        self.base_url, self.room_id = 'http://127.0.0.1:8757', ''
        self.active, self.state, self.live_broadcast = False, 'stopped', None
        self.starts, self.stops = [], 0
        self.auto_source = None
    async def start(self, base, room, scope):
        if not self.active:
            self.starts.append((base, room, scope))
        self.active, self.state = True, 'connecting'
    async def stop(self):
        self.active, self.state = False, 'stopped'
        self.stops += 1
    def snapshot(self):
        return {'auto': self.auto_source.snapshot()}


class Program:
    def __init__(self):
        self.run_id, self.state = 'run-one', 'running'
        self.active = True
        self.cleanup = {'cloud_stop_confirmed': True}
        self.calls = 0
    async def stop(self):
        self.calls += 1
        self.state = 'stopped'
        self.active = False


class AutoSourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bindings, self.now = [], 1000
        self.program = Program()
        self.wb = SimpleNamespace(settings_path=None, appearance=None, program=self.program,
                                  game=SimpleNamespace(bind_live_broadcast=lambda **value: self.bindings.append(value)))
        self.intake = Intake()
        self.result = self.identity()
        async def probe(base, room):
            return self.result
        self.auto = AutoSource(self.wb, self.intake, probe=probe, clock=lambda: self.now)
        self.auto.enabled, self.auto.room_id = True, '123'
        self.intake.auto_source = self.auto

    def identity(self, state='live', ident='900001', **changes):
        return dict(roomId='123', status=state, liveId=ident,
                    evidence='roomStore.roomInfo.room', **changes)

    async def asyncTearDown(self):
        await self.auto.close()

    async def live(self, ident='900001'):
        self.result = self.identity(ident=ident)
        await self.auto.check_once()
        self.now += 5
        await self.auto.check_once()

    async def test_two_same_id_observations_wait_five_seconds_then_bind_before_single_intake(self):
        await self.auto.check_once()
        self.assertEqual(self.auto.state, 'confirming_live')
        self.assertEqual(self.bindings, [])
        self.now += 4.9
        await self.auto.check_once()
        self.assertEqual(self.intake.starts, [])
        self.now += .1
        await self.auto.check_once()
        await self.auto.check_once()
        self.assertEqual(self.intake.starts, [('http://127.0.0.1:8757', '123', 'broadcast')])
        self.assertEqual(self.intake.live_broadcast, {'room_id': '123', 'live_id': '900001'})
        self.assertEqual(self.program.calls, 0)

    async def test_unknown_bad_evidence_recommendation_and_wrong_room_never_create_broadcast(self):
        for result in ({}, {'roomId': '123', 'status': 'live', 'liveId': '900001'},
                       self.identity(ident='123'), dict(self.identity(), roomId='456'),
                       self.identity('unknown'), self.identity('live', 'not-a-platform-id')):
            self.result = result
            await self.auto.check_once()
            self.now += 20
            await self.auto.check_once()
        self.assertEqual(self.intake.starts, [])
        self.assertEqual(self.bindings, [])

    async def test_unknown_or_changed_candidate_restarts_full_confirm_window(self):
        await self.auto.check_once()
        self.now += 5
        self.result = self.identity('unknown')
        await self.auto.check_once()
        self.result = self.identity()
        await self.auto.check_once()
        self.assertEqual(self.intake.starts, [])
        self.now += 5
        self.result = self.identity(ident='900002')
        await self.auto.check_once()
        self.assertEqual(self.intake.starts, [])
        self.now += 5
        await self.auto.check_once()
        self.assertEqual(self.auto.live_id, '900002')

    async def test_closed_socket_rebuilds_same_room_without_new_broadcast(self):
        await self.live()
        self.intake.state = 'closed'
        await self.auto.check_once()
        self.assertEqual(len(self.intake.starts), 2)
        self.assertEqual({x['live_id'] for x in self.bindings}, {'900001'})
        self.assertEqual(self.program.calls, 0)

    async def test_pause_current_broadcast_survives_observation_new_identity_resumes(self):
        await self.live()
        # Avoid starting background work in this direct state-machine test.
        self.auto.launch = lambda: None
        await self.auto.pause()
        self.now += 60
        await self.auto.check_once()
        self.assertFalse(self.intake.active)
        self.assertEqual(self.auto.state, 'paused')
        await self.live('900002')
        self.assertTrue(self.intake.active)
        self.assertFalse(self.auto.snapshot()['paused'])
        self.assertEqual(self.program.calls, 1)  # B proves A ended even while intake was paused.

    async def test_three_explicit_end_samples_finish_same_run_once(self):
        await self.live()
        self.result = self.identity('ended')
        await self.auto.check_once()
        self.now += 2.5
        await self.auto.check_once()
        self.assertEqual(self.program.calls, 0)
        self.now += 2.5
        await self.auto.check_once()
        for _ in range(3):
            await self.auto.check_once()
        self.assertEqual(self.program.calls, 1)
        self.assertFalse(self.intake.active)
        self.assertTrue(self.auto.stop_result['cloud_stop_confirmed'])

    async def test_end_evidence_interrupted_by_unknown_or_network_does_not_stop(self):
        await self.live()
        self.result = self.identity('ended')
        await self.auto.check_once()
        self.now += 5
        self.result = self.identity('unknown')
        await self.auto.check_once()
        self.result = self.identity('ended')
        await self.auto.check_once()
        self.assertEqual(self.program.calls, 0)

    async def test_late_old_end_never_stops_new_broadcast_and_run_changed_is_guarded(self):
        await self.live()
        await self.live('900002')
        # Switching broadcasts safely stopped A's still-running actor once.
        self.assertEqual(self.program.calls, 1)
        self.program.active = True
        self.program.run_id = 'run-two'
        self.auto.observe_program_run()
        self.result = self.identity('ended', '900001')
        for _ in range(3):
            self.now += 5
            await self.auto.check_once()
        self.assertEqual(self.program.calls, 1)
        self.result = self.identity('ended', '900002')
        await self.auto.check_once()
        self.program.run_id = 'operator-new-run'
        for _ in range(2):
            self.now += 2.5
            await self.auto.check_once()
        self.assertEqual(self.program.calls, 1)
        self.assertEqual(self.auto.stop_result['state'], 'run_changed')

    async def test_a_to_b_without_observed_status_four_finishes_old_bound_run_before_new_binding(self):
        await self.live()
        stopped_with = []
        old_stop = self.program.stop
        async def stop():
            stopped_with.append((self.auto.live_id, self.intake.active, len(self.bindings)))
            await old_stop()
        self.program.stop = stop
        await self.live('900002')
        self.assertEqual(stopped_with, [('900001', False, 1)])
        self.assertEqual(self.auto.live_id, '900002')
        self.assertEqual(self.program.calls, 1)
        self.assertTrue(self.intake.active)

    async def test_new_operator_run_after_last_live_observation_is_never_guessed_as_old_end(self):
        await self.live()
        self.program.run_id = 'new-unobserved-run'
        self.result = self.identity('ended')
        for _ in range(3):
            self.now += 2.5
            await self.auto.check_once()
        self.assertEqual(self.program.calls, 0)
        self.assertEqual(self.auto.stop_result['state'], 'run_changed')

    async def test_manual_start_during_initial_confirmation_window_is_bound_to_same_live(self):
        self.program.active = False
        await self.auto.check_once()
        self.program.run_id, self.program.active = 'manual-gap-run', True
        self.auto.observe_program_run()  # HTTP handler's synchronous callback
        self.now += 5
        await self.auto.check_once()
        self.result = self.identity('ended')
        for _ in range(3):
            self.now += 2.5
            await self.auto.check_once()
        self.assertEqual(self.program.calls, 1)
        self.assertEqual(self.auto.stop_result['run_id'], 'manual-gap-run')

    async def test_manual_new_run_during_b_confirmation_is_not_stopped_as_a(self):
        await self.live()
        self.result = self.identity(ident='900002')
        await self.auto.check_once()
        self.program.run_id = 'new-b-run'
        self.auto.observe_program_run()
        self.now += 5
        await self.auto.check_once()
        self.assertEqual(self.program.calls, 0)
        self.assertEqual(self.auto.stop_result['state'], 'run_changed')
        self.result = self.identity('ended', '900002')
        for _ in range(3):
            self.now += 2.5
            await self.auto.check_once()
        self.assertEqual(self.program.calls, 1)
        self.assertEqual(self.auto.stop_result['run_id'], 'new-b-run')

    async def test_unknown_suspends_old_attribution_and_requires_fresh_confirmation(self):
        await self.live()
        self.result = self.identity('unknown')
        await self.auto.check_once()
        self.assertFalse(self.intake.active)
        self.assertIsNone(self.intake.live_broadcast)
        self.assertEqual(self.program.calls, 0)
        self.result = self.identity()
        self.now += 60
        await self.auto.check_once()
        self.assertFalse(self.intake.active)
        self.now += 5
        await self.auto.check_once()
        self.assertTrue(self.intake.active)
        self.assertEqual(self.auto.live_id, '900001')
        self.assertEqual(self.program.calls, 0)

    async def test_pause_configure_and_close_join_an_already_started_cloud_stop(self):
        for operation in ('pause', 'configure', 'close'):
            with self.subTest(operation=operation):
                self.auto.finalized.clear()
                self.auto.launch = lambda: None
                self.program.run_id, self.program.active = operation, True
                self.result = self.identity()
                self.auto.candidate_id = None
                await self.live()
                entered, release = asyncio.Event(), asyncio.Event()
                async def slow_stop():
                    entered.set()
                    await release.wait()
                    self.program.calls += 1
                    self.program.active = False
                self.program.stop = slow_stop
                self.result = self.identity('ended')
                await self.auto.check_once()
                self.now += 2.5
                await self.auto.check_once()
                self.now += 2.5
                self.auto.task = asyncio.create_task(self.auto.check_once())
                await entered.wait()
                original = self.auto.task
                if operation == 'configure':
                    action = asyncio.create_task(self.auto.configure(self.auto.base_url, '456'))
                else:
                    action = asyncio.create_task(getattr(self.auto, operation)())
                await asyncio.sleep(0)
                self.assertFalse(action.done())
                self.assertFalse(self.auto.finish_task.done())
                release.set()
                await action
                self.assertTrue(original.done())
                self.assertTrue(self.auto.finish_task.done())
                self.auto.room_id, self.auto.paused_live_id = '123', None

    async def test_wrong_ended_identity_breaks_consecutive_end_confirmation(self):
        await self.live()
        self.result = self.identity('ended')
        await self.auto.check_once()
        self.assertEqual(self.auto.ended_count, 1)
        self.now += 5
        self.result = self.identity('ended', '900099')
        await self.auto.check_once()
        self.assertEqual(self.auto.ended_count, 0)
        self.result = self.identity('ended')
        await self.auto.check_once()
        self.now += 2.5
        await self.auto.check_once()
        self.assertEqual(self.program.calls, 0)
        self.now += 2.5
        await self.auto.check_once()
        self.assertEqual(self.program.calls, 1)

    async def test_change_room_refuses_to_abandon_bound_running_actor_then_clears_old_context(self):
        await self.live()
        self.auto.launch = lambda: None
        with self.assertRaisesRegex(ValueError, 'source_room_in_use'):
            await self.auto.configure(self.auto.base_url, '456')
        self.assertEqual(self.auto.room_id, '123')
        self.assertEqual(self.auto.live_id, '900001')
        self.assertTrue(self.intake.active)
        self.assertEqual(self.program.calls, 0)
        self.program.active = False  # Operator has completed a separate normal stop.
        self.auto.stop_result = {'room_id': '123', 'live_id': '900001', 'cloud_stop_confirmed': True}
        self.auto.ended_id, self.auto.ended_at, self.auto.ended_count = '900001', self.now, 2
        await self.auto.configure(self.auto.base_url, '456')
        self.assertIsNone(self.auto.live_id)
        self.assertIsNone(self.auto.candidate_id)
        self.assertIsNone(self.auto.ended_id)
        self.assertIsNone(self.auto.stop_result)
        self.assertIsNone(self.intake.live_broadcast)

    async def test_pause_clears_candidate_but_keeps_tracking_new_manual_run_and_actual_end(self):
        await self.live()
        self.auto.launch = lambda: None
        self.auto.stop_result = {'live_id': 'old', 'cloud_stop_confirmed': True}
        await self.auto.pause()
        self.assertIsNone(self.auto.candidate_id)
        self.assertIsNone(self.auto.stop_result)
        self.program.run_id = 'manual-while-paused'
        self.result = self.identity()
        await self.auto.check_once()
        self.now += 5
        await self.auto.check_once()
        self.assertFalse(self.intake.active)
        self.assertEqual(self.auto.run_bindings[('123', '900001')], 'manual-while-paused')
        self.result = self.identity('ended')
        for _ in range(3):
            self.now += 2.5
            await self.auto.check_once()
        self.assertEqual(self.program.calls, 1)
        self.assertEqual(self.auto.stop_result['run_id'], 'manual-while-paused')

    async def test_cloud_stop_unknown_is_once_and_free_new_broadcast_can_connect(self):
        await self.live()
        self.program.cleanup['cloud_stop_confirmed'] = False
        self.result = self.identity('ended')
        for _ in range(3):
            self.now += 2.5
            await self.auto.check_once()
        self.assertEqual(self.auto.state, 'stop_unconfirmed')
        await self.live('900002')
        self.assertTrue(self.intake.active)
        self.assertEqual(self.auto.error_code, 'cloud_stop_unconfirmed')
        self.assertEqual(self.program.calls, 1)

    async def test_service_restores_public_room_and_pause_from_isolated_settings(self):
        with tempfile.TemporaryDirectory() as temp:
            self.wb.settings_path = Path(temp) / 'game.settings.json'
            self.wb.appearance = SimpleNamespace(service=SimpleNamespace(settings=lambda: {'room_id': '123'}))
            first = AutoSource(self.wb, Intake())
            self.assertTrue(first.enabled)
            self.assertEqual(first.room_id, '123')
            first.paused_live_id = '900001'
            first._save()
            saved = json.loads((Path(temp) / 'game.source.json').read_text())
            self.assertEqual(set(saved), {'base_url', 'room_id', 'enabled', 'paused_live_id'})
            resumed = AutoSource(self.wb, Intake())
            self.assertEqual(resumed.paused_live_id, '900001')
            self.assertEqual(resumed.room_id, '123')

    async def test_background_failure_backs_off_and_shutdown_releases_only_owned_task(self):
        entered = asyncio.Event()
        async def failing(base, room):
            entered.set()
            raise OSError('synthetic')
        self.auto.probe = failing
        self.auto.launch()
        await entered.wait()
        await asyncio.sleep(0)
        self.assertEqual(self.auto.state, 'retrying')
        self.assertEqual(self.auto.next_check_at, self.now + 10)
        original = self.auto.task
        self.auto.launch()
        self.assertIs(self.auto.task, original)
        await self.auto.close()
        self.assertTrue(original.done())
        self.assertIsNone(self.auto.task)


class LocalProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_loopback_plain_endpoint_and_numeric_room_accepted(self):
        for endpoint in ('https://127.0.0.1:8757', 'http://localhost:8757',
                         'http://127.0.0.1:8757/else', 'http://127.0.0.1:0'):
            with self.assertRaises(ValueError):
                settings(endpoint, '123')
        for room in ('abc', 123, '123/anything', ''):
            with self.assertRaises(ValueError):
                settings('http://127.0.0.1:8757', room)

    async def test_local_probe_rejects_redirect_and_reads_bounded_identity(self):
        async def status(request):
            return web.json_response({'roomId': '123', 'status': 'live', 'liveId': '900001',
                                      'evidence': 'roomStore.roomInfo.room'})
        app = web.Application()
        app.router.add_get('/api/rooms/123/live-status', status)
        async with TestServer(app) as source:
            result = await probe_room(str(source.make_url('')).rstrip('/'), '123')
            self.assertEqual(result['liveId'], '900001')

    async def test_auto_api_single_process_subscription_and_pause_without_network(self):
        app = create_app(':memory:', model=FakeModel(), avatar_client=FakeAvatar())
        called = asyncio.Event()
        async def offline(base, room):
            called.set()
            await asyncio.Event().wait()
        app['auto_source'].probe = offline
        async with TestClient(TestServer(app)) as client:
            body = {'base_url': 'http://127.0.0.1:8757', 'room_id': '123'}
            response = await client.post('/api/source/auto', json=body)
            self.assertEqual(response.status, 200)
            await called.wait()
            task = app['auto_source'].task
            await client.post('/api/source/auto', json=body)
            self.assertIs(app['auto_source'].task, task)
            result = await (await client.post('/api/source/stop', json={})).json()
            self.assertTrue(result['auto']['paused'])
            self.assertFalse(result['active'])
        self.assertIsNone(app['auto_source'].task)

    async def test_confirmed_auto_intake_binds_real_game_receipt_and_eligible_gifts_without_avatar_start(self):
        class Client:
            def __init__(self, base, room, *_args, **kwargs):
                self.base_url, self.room_id, self.types = base, room, kwargs['event_types']
            async def run(self, stopped):
                await stopped.wait()
            async def close(self):
                pass
        wb = Workbench(':memory:', model=FakeModel(), avatar_client=FakeAvatar())
        intake = DyhubIntake(wb, client_factory=Client)
        now = [1000]
        async def probe(base, room):
            return {'roomId': room, 'status': 'live', 'liveId': '900001',
                    'evidence': 'roomStore.roomInfo.room'}
        auto = AutoSource(wb, intake, probe=probe, clock=lambda: now[0])
        auto.enabled, auto.room_id = True, '123'
        # Existing legal one-diamond probe; enable no paid actor or scoring.
        wb.appearance.configure({'enabled': False, 'room_id': '123',
                                 'positive_gift_ids': ['463'], 'negative_gift_ids': ['164'],
                                 'quantity_verified': False})
        try:
            await wb.game.new_episode()
            episode = wb.game.episode['id']
            await auto.check_once()
            now[0] += 5
            await auto.check_once()
            self.assertEqual(wb.game.episode['id'], episode)
            self.assertEqual(intake.client.types, ('chat', 'gift'))
            self.assertTrue(wb.appearance.gift_probe.active)
            stamp = int(time.time() * 1000) + 1
            await intake.on_event({'id': 'trusted', 'type': 'chat', 'platform': 'douyin',
                                   'roomId': '123', 'ts': stamp, 'receivedAt': stamp,
                                   'user': {'id': 'viewer', 'nickname': '测试观众'},
                                   'data': {'content': '建议先解释清楚'}})
            receipt = wb.game.data['comments'][-1]
            self.assertEqual(receipt['live_broadcast_id'], wb.game.data['live_broadcast_id'])
            self.assertFalse(wb.program.active)
            self.assertEqual(wb.program.client.sent, [])
        finally:
            await auto.close()
            await wb.close()
