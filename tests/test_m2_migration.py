"""Upgrade actual v1-shaped tables with a pre-existing guest and command receipt."""
import os
import unittest
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import create_async_engine
from hell_inc import db
from hell_inc.records.service import RecordService


class M2MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_v1_upgrade_twice_retains_guest_and_original_receipt(self):
        url = os.environ['HELL_INC_DATABASE_URL']
        schema = 'm2_upgrade_' + uuid4().hex
        admin = create_async_engine(url)
        engine = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
        try:
            async with admin.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA {schema}'))
            world, player, command = 'old-world', str(uuid4()), str(uuid4())
            now = datetime.now(timezone.utc)
            payload = dict(command_id=command, provider='simulated', identity_scope='local-test', subject_id='old-guest', display_name='旧游客')
            # Build only the four unchanged M1 tables; do NOT migrate fresh v2 and relabel it.
            async with engine.begin() as conn:
                for table in (db.versions, db.worlds, db.players, db.commands):
                    await conn.run_sync(lambda sync, table=table: table.create(sync))
                await conn.execute(db.versions.insert().values(version=1))
                await conn.execute(db.worlds.insert().values(world_id=world, revision=1))
                await conn.execute(db.players.insert().values(player_id=player, world_id=world, provider='simulated', identity_scope='local-test', subject_id='old-guest', display_name='旧游客', joined_at=now, updated_at=now))
                original = {'replayed': False, 'revision': 1, 'player': {
                    'player_id': player, 'provider': 'simulated', 'identity_scope': 'local-test',
                    'subject_id': 'old-guest', 'display_name': '旧游客', 'joined_at': now.isoformat(),
                    'updated_at': now.isoformat(), 'role': 'guest', 'department': None, 'position': None, 'performance': 0, 'recent_events': []}}
                await conn.execute(db.commands.insert().values(world_id=world, command_id=command, payload={k: v for k, v in payload.items() if k != 'command_id'}, response=original, created_at=now))
            # Latest migration still exercises the actual v1 -> M2 additive table upgrade,
            # then M3; all original guest/receipt/event assertions remain below.
            await db.migrate(engine)
            await db.migrate(engine)
            async with engine.connect() as conn:
                self.assertEqual((await conn.execute(select(db.versions.c.version))).scalars().all(), [3])
                self.assertEqual((await conn.execute(select(db.worlds.c.revision))).scalar_one(), 1)
                self.assertEqual((await conn.execute(select(db.commands.c.response))).scalar_one(), original)
                for table in (db.interviews, db.model_tasks, db.memberships, db.settlements, db.reward_ledger, db.game_events):
                    self.assertEqual((await conn.execute(select(table))).all(), [])
            service = RecordService(engine, world)
            self.assertEqual(await service.register(payload), {**original, 'replayed': True})
            current = (await service.get_player(player))['player']
            for key in ('player_id', 'subject_id', 'display_name', 'role', 'performance', 'recent_events'):
                self.assertEqual(current[key], original['player'][key])
        finally:
            await engine.dispose()
            async with admin.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS {schema} CASCADE'))
            await admin.dispose()
