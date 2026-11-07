"""Real Program -> AvatarClient HTTP -> ExternalAppearance -> shared Type16 sender.

Only HTTP control is loopback; PCM, WS and receiver progress are synthetic.
The 3s/15s guard is shortened here. No provider, credentials or real data used.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer

from appearance_studio_double import OfflineStudioAvatar, STUDIO
from romance.avatar_client import AvatarClient
from romance.dyhub_intake import DyhubIntake
from test_appearance_gift_probe import HoldClient
from romance.server import Workbench
from test_server import SyntheticModel

sys.path.insert(0, str(STUDIO))
try:
    from external_game import ExternalGame
    from external_appearance import KEYS
finally:
    sys.path.remove(str(STUDIO))


class ProtocolAvatar(OfflineStudioAvatar):
    supports_real_appearance = True
    supports_simulated_appearance = False

    def __init__(self):
        super().__init__(drain_seconds=0)
        root = Path(self._temp.name)
        assets, self.images = {}, {}
        for index, key in enumerate(KEYS):
            image = root / (key + '.fixture')
            image.write_bytes(('synthetic-image-' + key).encode())
            ident = str(4000 + index)
            self.images[ident] = image
            assets[key] = {'avatar_id': ident, 'image_sha256': hashlib.sha256(image.read_bytes()).hexdigest(), 'description': key}
        config = {'enabled': True, 'character_id': 'offline-only', 'voice_id': 'offline-voice',
                  'broadcast_key': 'stellar', 'assets': assets,
                  'transitions': {'up': 'https://example.com/up.mp4', 'down': 'https://example.com/down.mp4'}}
        (root / 'external-appearance.json').write_text(json.dumps(config))
        self.session = SimpleNamespace(component=self.component, started=True, stopping=False,
            worker_fault=False, comments=[], work=asyncio.Queue(), tts_voice='offline-voice',
            avatar_id=assets['stellar']['avatar_id'], variant_info={'image_sha256': assets['stellar']['image_sha256']},
            _prepared_comments={}, out=root, spawn=self.spawn, discard_prepared=lambda item: None,
            apply_avatar=self.apply_avatar, latch_stop=self.component.latch_stop,
            request_stop=self.stop_for_fault)
        self.studio = SimpleNamespace(data=root, current=self.session, generation=None, closing=False,
            lock=asyncio.Lock(), automation=SimpleNamespace(last_error=''), config={'settings': {}},
            avatar_switch_holding=False, idle_enable_epoch=0,
            library_asset=lambda ident: {'sha256': hashlib.sha256(self.images[ident].read_bytes()).hexdigest()},
            avatar_library=SimpleNamespace(image_path=lambda ident: self.images[ident]),
            asset_tts_voice=lambda ident: 'offline-voice', player_public=lambda: {'connected': True},
            player={'client_id': 'only-offline-receiver', 'joined': True, 'audio_live': True,
                    'video_live': True, 'frames': 1, 'seen': time.monotonic(), 'frame_seen': time.monotonic()})
        self.external = ExternalGame(self.studio)
        self.control = self.external.appearance
        self.control.TRANSITION_GUARD_SECONDS = .02
        self.control.RECEIVER_TIMEOUT_SECONDS = .1
        self.control.POLL_SECONDS = .005
        self.http_client = AvatarClient()
        self.http_paths = []

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def apply_avatar(self, image, ident, settings, **kwargs):
        self.session.avatar_id = ident
        self.session.variant_info = {'image_sha256': hashlib.sha256(Path(image).read_bytes()).hexdigest()}
        self.confirmed_appearance_key = next(key for key, value in self.control.config['assets'].items() if value['avatar_id'] == ident)

    async def stop_for_fault(self, reason):
        await self.stop()

    async def open_http(self):
        app = web.Application()
        app.router.add_get('/external/appearance', self.control.state)
        app.router.add_post('/external/appearance', self.control.submit)
        app.router.add_post('/external/appearance/lifecycle', self.control.lifecycle)
        app.router.add_get('/external/appearance/{request_id}', self.control.get)
        # Deliberately no /confirm endpoint; normal flow must never need one.
        self.server = TestServer(app, host='127.0.0.1')
        await self.server.start_server()
        self.http_client.control_url = str(self.server.make_url('/'))
        self.http_client.origin = str(self.server.make_url('/')).rstrip('/')
        async def pulse():
            while True:
                await asyncio.sleep(.005)
                now = time.monotonic()
                self.studio.player.update(frames=self.studio.player['frames'] + 1, seen=now, frame_seen=now)
        self.receiver = asyncio.create_task(pulse())

    async def prepare_session(self, owner_id):
        result = await super().prepare_session(owner_id)
        self.studio.generation = self.external.generation = self.generation
        self.external.owner_id = owner_id
        self.http_client.owner_id, self.http_client.generation = owner_id, self.generation
        return result

    async def appearance_status(self):
        return await self.http_client.appearance_status()

    async def appearance_lifecycle(self, record, action):
        return await self.http_client.appearance_lifecycle(record, action)

    async def switch_appearance(self, record):
        return await self.http_client.switch_appearance(record)

    async def appearance(self, record):
        return await self.http_client.appearance(record)

    async def confirm_appearance(self, *args, **kwargs):
        raise AssertionError('Normal program cannot use a confirmation POST')

    async def close(self):
        self.receiver.cancel()
        await asyncio.gather(self.receiver, return_exceptions=True)
        if self.http_client.http:
            await self.http_client.http.close()
        await self.server.close()
        await super().close()


class AutomaticAppearanceProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.avatar = ProtocolAvatar()
        await self.avatar.open_http()
        self.wb = Workbench(':memory:', model=SyntheticModel(), avatar_client=self.avatar)
        self.wb.appearance.configure({"enabled": True, "quantity_verified": False, "room_id": "123",
                                     "positive_gift_ids": ["463"], "negative_gift_ids": ["164"]})
        self.addAsyncCleanup(self.wb.close)
        self.program = self.wb.program

    async def start(self):
        self.program.start()
        await self.program.task
        await self.wb.job
        self.assertFalse(self.program.sender_failed, self.program.error)

    async def test_normal_open_continuous_crossings_and_chat_restore_without_confirmation(self):
        await self.start()
        self.assertTrue(self.program.appearance_status()['active'])
        self.assertEqual(self.program.confirmed_appearance_key, 'normal')
        self.assertEqual(self.avatar.control.requests[next(iter(self.avatar.control.requests))]['status'], 'applied')
        for target in ('refined', 'fish', 'rough', 'tired', 'stellar', 'normal'):
            self.program.request_appearance(target)
            await self.program.wait_for_appearance()
            self.assertEqual(self.program.confirmed_appearance_key, target)
            receipt = self.program.appearance_status()['receipt']
            self.assertEqual(receipt['status'], 'applied')
            self.assertTrue(receipt['program_adopted'])
            for field in ('visual_confirmed', 'operator_confirmed', 'playback_confirmed', 'success_acknowledged'):
                self.assertFalse(receipt[field])
        self.program.set_mode(self.program.run_id, 'chat')
        await self.program._mode_task
        self.assertEqual(self.program.content_mode, 'chat')
        self.assertEqual(self.program.confirmed_appearance_key, 'stellar')
        self.assertEqual(self.avatar.calls.count('start'), 1)
        self.assertEqual(len(self.avatar.control.requests), 8)
        self.assertEqual(len([event for event in self.avatar.wire_events if event.get('type') == 16]), 8)

    async def test_release_during_written_switch_resolves_then_restores_only_once(self):
        await self.start()
        self.program.request_appearance('refined')
        async with asyncio.timeout(2):
            while not self.avatar.control.pending:
                await asyncio.sleep(.001)
        self.program.set_mode(self.program.run_id, 'chat')
        await self.program._mode_task
        self.assertEqual(self.program.content_mode, 'chat')
        self.assertEqual(self.program.confirmed_appearance_key, 'stellar')
        self.assertEqual(self.avatar.calls.count('start'), 1)
        self.assertLessEqual(len(self.avatar.control.requests), 3)

    async def test_auto_switch_obeys_formal_speech_typing_pair_and_tail_q_guard(self):
        await self.start()
        opening_count = len(self.avatar.control.requests)
        game = self.wb.game
        game.submit_proposal('advisor', '军师', '约她周六见面')
        await game.close_collection()
        game.vote('viewer', '观众', '1')
        speech = game.current_round['id'] + ':reaction'
        typing = speech + ':typing'
        self.avatar.hold(speech, 'pcm')
        self.avatar.hold(typing, 'Q')
        task = asyncio.create_task(game.close_voting())
        async def cleanup():
            self.avatar.release(speech, 'pcm')
            self.avatar.release(typing, 'Q')
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.addAsyncCleanup(cleanup)
        await self.avatar.wait_node(speech, 'pcm')
        self.program.request_appearance('refined')
        self.avatar.release(speech, 'pcm')
        await self.avatar.wait_node(typing, 'before_Q')
        self.assertEqual(len(self.avatar.control.requests), opening_count)
        self.avatar.release(typing, 'Q')
        await asyncio.wait_for(task, 5)
        await self.program.wait_for_appearance()
        self.assertEqual(self.program.confirmed_appearance_key, 'refined')
        switches = [row for row in self.avatar.wire_events if row.get('type') == 16]
        self.assertEqual(len(switches), opening_count + 1)
        self.assertGreater(switches[-1]['order'], self.avatar.boundaries[-1]['end_order'])
        tail = [row for row in self.avatar.wire_events if row.get('type') == 9 and row.get('role') == 'Q'][-1]
        self.assertGreaterEqual(switches[-1]['at'] - tail['at'], 1.0)

    async def test_unknown_receiver_result_never_retries_or_adopts(self):
        await self.start()
        self.avatar.receiver.cancel()
        await asyncio.gather(self.avatar.receiver, return_exceptions=True)
        self.program.request_appearance('refined')
        await self.program.wait_for_appearance()
        self.assertTrue(self.program.sender_failed)
        self.assertEqual(self.program.confirmed_appearance_key, 'normal')
        receipt = self.program.appearance_status()['receipt']
        self.assertEqual(receipt['status'], 'unconfirmed')
        self.assertFalse(receipt['program_adopted'])
        self.assertEqual(len(self.avatar.control.requests), 2)
        self.assertTrue(self.avatar.studio.avatar_switch_holding)

    async def test_written_status_has_a_failure_deadline_and_no_resend(self):
        await self.start()
        self.program.APPEARANCE_TIMEOUT_SECONDS = .02
        original_query = self.avatar.appearance
        async def still_written(record):
            receipt = await original_query(record)
            receipt.update(status='written', program_adopted=False)
            return receipt
        self.avatar.appearance = still_written
        self.program.request_appearance('refined')
        await self.program.wait_for_appearance()
        self.assertTrue(self.program.sender_failed)
        self.assertEqual(self.program.confirmed_appearance_key, 'normal')
        self.assertEqual(len(self.avatar.control.requests), 2)
        self.assertEqual(len([row for row in self.avatar.wire_events if row.get('type') == 16]), 2)

    async def test_applied_receipt_cannot_claim_visual_or_operator_confirmation(self):
        await self.start()
        original_query = self.avatar.appearance
        async def fabricated_proof(record):
            receipt = await original_query(record)
            if receipt['status'] == 'applied':
                receipt['operator_confirmed'] = True
            return receipt
        self.avatar.appearance = fabricated_proof
        self.program.request_appearance('refined')
        await self.program.wait_for_appearance()
        self.assertTrue(self.program.sender_failed)
        self.assertEqual(self.program.confirmed_appearance_key, 'normal')
        self.assertEqual(len(self.avatar.control.requests), 2)

    async def test_synthetic_real_gift_packets_score_across_all_bands_without_confirmation(self):
        await self.start()
        intake = DyhubIntake(self.wb, client_factory=HoldClient)
        self.addAsyncCleanup(intake.stop)
        await intake.start('http://127.0.0.1:8757', '123', 'broadcast')
        for index, (gift_id, count, score, target) in enumerate([
                ('463', 20, 70, 'refined'), ('463', 30, 100, 'stellar'),
                ('164', 90, 10, 'rough'), ('164', 10, 0, 'fish'),
                ('463', 25, 25, 'tired'), ('463', 25, 50, 'normal')]):
            now = time.time() * 1000
            result = await intake.on_event({'type': 'gift', 'platform': 'douyin', 'roomId': '123',
                'id': 'protocol-gift-' + str(index), 'ts': now, 'receivedAt': now,
                'user': {'id': 'offline-viewer'}, 'data': {'giftId': gift_id,
                    'diamondCount': 1, 'repeatCount': count, 'sourceTimeKnown': True,
                    'groupId': 'protocol-combo-' + str(index)}})
            self.assertTrue(result['accepted'], result)
            self.assertEqual(result['score'], score)
            await self.program.wait_for_appearance()
            self.assertEqual(self.program.confirmed_appearance_key, target)
            self.assertEqual(self.program.appearance_status()['receipt']['status'], 'applied')
        self.assertFalse(self.wb.appearance.service.settings()['quantity_verified'])
        self.program.set_mode(self.program.run_id, 'chat')
        await self.program._mode_task
        self.assertEqual(self.program.content_mode, 'chat')
        self.assertEqual(self.program.confirmed_appearance_key, 'stellar')
        self.assertEqual(self.avatar.calls.count('start'), 1)
