"""Disk evidence failure must not prevent stopping this already-owned scene."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from hell_inc.avatar_transport.component_live import PersistentComponent


class ComponentStopEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def make_component(self, directory):
        component = PersistentComponent(None, Path(directory), Path('UNUSED'), recording=False)
        component._save = MagicMock(side_effect=OSError('disk unavailable'))
        component._event = MagicMock()
        component._connect = AsyncMock()
        component._send = AsyncMock()
        component._close_ws = AsyncMock()
        return component

    async def test_known_scene_stop_confirms_provider_ended_when_receipts_cannot_be_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            component = self.make_component(directory)
            component.bind_owned_for_stop('known-test-id', 'dummy-key')
            component._json = AsyncMock(return_value={'live': {'status': 'ended'}})
            result = await asyncio.wait_for(component.stop(), timeout=1)
            self.assertTrue(result['cloud_end_confirmed'])
            self.assertEqual(result['state'], 'ended')
            component._send.assert_awaited_once()
            self.assertEqual(component._send.await_args.args[0], 5)
            self.assertEqual(component._json.await_args.args[:2], ('GET', '/live/v1/lives/known-test-id'))

    async def test_created_id_still_hung_up_if_ownership_file_write_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            component = self.make_component(directory)
            async def query(method, path, *args, **kwargs):
                if method == 'POST':
                    return {'live': {'id': 'created-test-id'}, 'managed_rtc': {'token': 'dummy-rtc', 'user_id': 'dummy'}}
                return {'live': {'status': 'ended'}}
            component._json = AsyncMock(side_effect=query)
            await component.start('dummy-key', avatar_id='123')
            await asyncio.wait_for(component._task, timeout=1)
            self.assertEqual(component.live_id, 'created-test-id')
            self.assertTrue(component.cloud_end_confirmed)
            self.assertEqual(component.state, 'ended')
            component._send.assert_awaited_once()
            self.assertEqual(component._send.await_args.args[0], 5)
            self.assertEqual([c.args[0] for c in component._json.await_args_list], ['POST', 'GET'])

    async def test_disk_failure_never_fabricates_ended(self):
        with tempfile.TemporaryDirectory() as directory:
            component = self.make_component(directory)
            component.bind_owned_for_stop('known-test-id', 'dummy-key')
            component.END_TIMEOUT = 0.02
            component.POLL_INTERVAL = 0.005
            component._json = AsyncMock(return_value={'live': {'status': 'running'}})
            result = await asyncio.wait_for(component.stop(), timeout=1)
            self.assertFalse(result['cloud_end_confirmed'])
            self.assertEqual(result['state'], 'stop_unconfirmed')

    async def test_unusable_directory_does_not_block_stop_only(self):
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / 'not-a-directory'
            blocked.write_text('x')
            target = blocked / 'evidence'
            component = PersistentComponent(None, target, Path('UNUSED'), recording=False, stop_only=True)
            component._connect = AsyncMock()
            component._send = AsyncMock()
            component._close_ws = AsyncMock()
            component._json = AsyncMock(return_value={'live': {'status': 'ended'}})
            component.bind_owned_for_stop('known-test-id', 'dummy-key')
            result = await asyncio.wait_for(component.stop(), timeout=1)
            self.assertTrue(result['cloud_end_confirmed'])
            self.assertEqual(result['state'], 'ended')
            component._send.assert_awaited_once()
            self.assertEqual(component._send.await_args.args[0], 5)
            self.assertEqual(component._json.await_args.args[:2], ('GET', '/live/v1/lives/known-test-id'))

    async def test_stop_only_cannot_create_and_normal_mode_requires_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / 'not-a-directory'
            blocked.write_text('x')
            target = blocked / 'evidence'
            with self.assertRaises(OSError):
                PersistentComponent(None, target, Path('UNUSED'), recording=False)
            component = PersistentComponent(None, target, Path('UNUSED'), recording=False, stop_only=True)
            component._json = AsyncMock()
            with self.assertRaisesRegex(RuntimeError, 'stop_only_component_cannot_create'):
                await component.start('dummy-key', avatar_id='123')
            with self.assertRaisesRegex(RuntimeError, 'stop_only_component_cannot_create'):
                await component._run({})
            component._json.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
