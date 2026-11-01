"""Actual v3 schema: upgrade preserves M1–M3 state and original command receipts."""
import os
import unittest
import tempfile
from pathlib import Path
from hell_inc.app import STATE, create_app, lifecycle
from hell_inc.config import Settings
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from hell_inc import db
from hell_inc.records.service import RecordService


class M4MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_v3_upgrade_twice_preserves_m1_m2_m3_rows_and_receipts(self):
        url = os.environ['HELL_INC_DATABASE_URL']; schema = 'm4_upgrade_' + uuid4().hex
        admin = create_async_engine(url)
        engine = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
        world = 'existing-m2'; player, interview, task, settlement, command, message = [str(uuid4()) for _ in range(6)]
        now = datetime.now(timezone.utc)
        legacy_tables = [t for t in db.metadata.sorted_tables if not t.name.startswith('performance')]
        try:
            async with admin.begin() as conn: await conn.execute(text(f'CREATE SCHEMA {schema}'))
            async with engine.begin() as conn:
                for table in legacy_tables: await conn.run_sync(lambda sync, table=table: table.create(sync))
                await conn.execute(db.versions.insert().values(version=3))
                await conn.execute(db.worlds.insert().values(world_id=world, revision=7))
                await conn.execute(db.players.insert().values(player_id=player, world_id=world, provider='simulated', identity_scope='local-test', subject_id='legacy', display_name='旧员工', joined_at=now, updated_at=now))
                await conn.execute(db.memberships.insert().values(player_id=player, world_id=world, role='employee', department='危机公关', position='危机公关实习生', performance=4))
                await conn.execute(db.interviews.insert().values(id=interview, world_id=world, player_id=player, status='COMPLETED', version=4, body={'id': interview, 'status': 'COMPLETED', 'answer': {'text': ' 保留原文\n'}}, created_at=now))
                await conn.execute(db.model_tasks.insert().values(id=task, interview_id=interview, frozen_input={'answer': ' 保留原文\n'}, input_hash='a'*64, policy_hash='b'*64, attempts=[{'status': 'accepted'}]))
                await conn.execute(db.settlements.insert().values(id=settlement, world_id=world, interview_id=interview, player_id=player, payload_hash='c'*64, result={'decision': 'hire'}, created_at=now))
                await conn.execute(db.reward_ledger.insert().values(settlement_id=settlement, player_id=player, award_type='hire', before={'role': 'guest'}, after={'role': 'employee'}))
                await conn.execute(db.game_events.insert().values(interview_id=interview, player_id=player, world_id=world, event_type='interview', payload={'decision': 'hire'}, created_at=now))
                for table, key in ((db.commands, 'command_id'), (db.interview_commands, 'command_id'), (db.input_records, 'message_id')):
                    await conn.execute(table.insert().values(**{key: message if key == 'message_id' else command}, world_id=world, payload={'typed': 1}, response={'original': True, 'revision': 7}, created_at=now))
                competition, csettlement = str(uuid4()), str(uuid4())
                await conn.execute(db.competitions.insert().values(id=competition, world_id=world, status='COMPLETED', version=9, body={'id': competition, 'status': 'COMPLETED', 'submissions': [{'text': ' 原稿\n', 'evaluation': {'comment': '保留点评'}}], 'result': {'awards': []}}, created_at=now))
                await conn.execute(db.competition_settlements.insert().values(id=csettlement, world_id=world, competition_id=competition, payload_hash='d'*64, result={'awards': []}, created_at=now))
                await conn.execute(db.competition_reward_ledger.insert().values(settlement_id=csettlement, player_id=player, award_type='creativity', before={'performance': 3}, after={'performance': 4}))
                await conn.execute(db.competition_events.insert().values(competition_id=competition, player_id=player, world_id=world, event_type='competition', payload={'original': True}, created_at=now))
                await conn.execute(db.competition_commands.insert().values(command_id=command, world_id=world, payload={'typed': 1}, response={'original': True, 'revision': 7}, created_at=now))
                before = {t.name: [dict(row) for row in (await conn.execute(select(t))).mappings()] for t in legacy_tables if t is not db.versions}
            # A v3 database must be refused by startup without implicitly migrating it.
            with tempfile.TemporaryDirectory() as lock_dir:
                app = create_app(Settings(database_url=url, world_id=world, lock_dir=Path(lock_dir)), startup_token='migration-test-only')
                await app[STATE]['engine'].dispose()
                app[STATE]['engine'] = engine
                startup = lifecycle(app)
                with self.assertRaisesRegex(RuntimeError, 'migrate'):
                    await anext(startup)
            async with engine.connect() as conn:
                self.assertEqual((await conn.execute(select(db.versions.c.version))).scalars().all(), [3])
                self.assertIsNone((await conn.execute(text("SELECT to_regclass('performance_sessions')"))).scalar_one())
            await db.migrate(engine); await db.migrate(engine)
            async with engine.connect() as conn:
                self.assertEqual((await conn.execute(select(db.versions.c.version))).scalars().all(), [4])
                for table in legacy_tables:
                    if table is not db.versions:
                        self.assertEqual([dict(row) for row in (await conn.execute(select(table))).mappings()], before[table.name], table.name)
                for table in (db.performance_sessions, db.performances, db.performance_commands):
                    self.assertEqual((await conn.execute(select(table))).all(), [])
            profile = (await RecordService(engine, world).get_player(player))['player']
            self.assertEqual(profile['role'], 'employee'); self.assertEqual(profile['performance'], 4)
            self.assertEqual(len(profile['recent_events']), 2)
        finally:
            await engine.dispose()
            async with admin.begin() as conn: await conn.execute(text(f'DROP SCHEMA IF EXISTS {schema} CASCADE'))
            await admin.dispose()
