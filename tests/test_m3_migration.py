"""An actual v2-shaped schema with old M2 records, not v3 relabelled as v2."""
import os
import unittest
from datetime import datetime, timezone
from uuid import uuid4
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from hell_inc import db
from hell_inc.records.service import RecordService


class M3MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_v2_upgrade_twice_preserves_employee_m2_ledger_events_and_receipts(self):
        url = os.environ['HELL_INC_DATABASE_URL']; schema = 'm3_upgrade_' + uuid4().hex
        admin = create_async_engine(url)
        engine = create_async_engine(url, connect_args={'server_settings': {'search_path': schema}})
        world = 'existing-m2'; player, interview, task, settlement, command, message = [str(uuid4()) for _ in range(6)]
        now = datetime.now(timezone.utc)
        legacy_tables = [t for t in db.metadata.sorted_tables if not t.name.startswith(('competition', 'performance'))]
        try:
            async with admin.begin() as conn: await conn.execute(text(f'CREATE SCHEMA {schema}'))
            async with engine.begin() as conn:
                for table in legacy_tables: await conn.run_sync(lambda sync, table=table: table.create(sync))
                await conn.execute(db.versions.insert().values(version=2))
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
                before = {t.name: [dict(row) for row in (await conn.execute(select(t))).mappings()] for t in legacy_tables if t is not db.versions}
            await db.migrate(engine); await db.migrate(engine)
            async with engine.connect() as conn:
                self.assertEqual((await conn.execute(select(db.versions.c.version))).scalars().all(), [4])
                for table in legacy_tables:
                    if table is not db.versions:
                        self.assertEqual([dict(row) for row in (await conn.execute(select(table))).mappings()], before[table.name], table.name)
                for table in (db.competitions, db.competition_commands, db.competition_settlements, db.competition_reward_ledger, db.competition_events):
                    self.assertEqual((await conn.execute(select(table))).all(), [])
            profile = (await RecordService(engine, world).get_player(player))['player']
            self.assertEqual(profile['role'], 'employee'); self.assertEqual(profile['performance'], 4)
            self.assertEqual(len(profile['recent_events']), 1)
        finally:
            await engine.dispose()
            async with admin.begin() as conn: await conn.execute(text(f'DROP SCHEMA IF EXISTS {schema} CASCADE'))
            await admin.dispose()
