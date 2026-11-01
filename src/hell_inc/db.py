"""Explicit additive schema v3 migration. Never called by web startup."""
from sqlalchemy import (MetaData, Table, Column, String, BigInteger, DateTime,
                        UniqueConstraint, ForeignKey, CheckConstraint, Integer, text)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import create_async_engine

metadata = MetaData()
versions = Table("schema_versions", metadata, Column("version", Integer, primary_key=True))
worlds = Table("worlds", metadata, Column("world_id", String(128), primary_key=True),
               Column("revision", BigInteger, nullable=False, server_default="0"))
players = Table(
    "players", metadata,
    Column("player_id", UUID(as_uuid=False), primary_key=True),
    Column("world_id", String(128), ForeignKey("worlds.world_id"), nullable=False),
    Column("provider", String(32), nullable=False),
    Column("identity_scope", String(64), nullable=False),
    Column("subject_id", String(128), nullable=False),
    Column("display_name", String(80), nullable=False),
    Column("joined_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("world_id", "provider", "identity_scope", "subject_id"),
    CheckConstraint("provider = 'simulated' AND identity_scope = 'local-test'"),
)
commands = Table(
    "registration_commands", metadata,
    Column("world_id", String(128), ForeignKey("worlds.world_id"), primary_key=True),
    Column("command_id", UUID(as_uuid=False), primary_key=True),
    Column("payload", JSONB, nullable=False),
    Column("response", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def make_engine(url: str):
    if not url.startswith("postgresql+asyncpg://"):
        raise ValueError("需要 postgresql+asyncpg:// 数据库 URL；不支持 SQLite 替代验收。")
    return create_async_engine(url, pool_pre_ping=True, echo=False, hide_parameters=True)


async def migrate(engine):
    """Explicit, transactional fresh baseline or v1/v2 -> v3; never startup."""
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(721023061)"))
        await conn.run_sync(lambda sync: versions.create(sync, checkfirst=True))
        existing = (await conn.execute(versions.select())).scalars().all()
        if existing not in ([], [1], [2], [3]):
            raise RuntimeError("未知数据库版本，拒绝迁移。")
        await conn.run_sync(metadata.create_all)
        if existing != [3]:
            await conn.execute(versions.delete())
            await conn.execute(versions.insert().values(version=3))


# M2 is additive: M1 tables and original command receipts remain untouched.
memberships = Table('memberships', metadata,
    Column('player_id', UUID(as_uuid=False), ForeignKey('players.player_id'), primary_key=True),
    Column('world_id', String(128), ForeignKey('worlds.world_id'), nullable=False),
    Column('role', String(32), nullable=False), Column('department', String(80)),
    Column('position', String(80)), Column('performance', Integer, nullable=False))
interviews = Table('interviews', metadata,
    Column('id', UUID(as_uuid=False), primary_key=True),
    Column('world_id', String(128), ForeignKey('worlds.world_id'), nullable=False),
    Column('player_id', UUID(as_uuid=False), ForeignKey('players.player_id'), nullable=False),
    Column('status', String(32), nullable=False), Column('version', Integer, nullable=False),
    Column('body', JSONB, nullable=False), Column('created_at', DateTime(timezone=True), nullable=False))
from sqlalchemy import Index
Index('one_active_interview_per_world', interviews.c.world_id, unique=True,
      postgresql_where=interviews.c.status.in_(['READY','ANSWERING','EVALUATING','REVIEW_FAILED','PAUSED']))
interview_commands = Table('interview_commands', metadata,
    Column('world_id', String(128), ForeignKey('worlds.world_id'), primary_key=True),
    Column('command_id', UUID(as_uuid=False), primary_key=True),
    Column('payload', JSONB, nullable=False), Column('response', JSONB, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False))
input_records = Table('input_records', metadata,
    Column('world_id', String(128), ForeignKey('worlds.world_id'), primary_key=True),
    Column('message_id', UUID(as_uuid=False), primary_key=True),
    Column('payload', JSONB, nullable=False), Column('response', JSONB, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False))
model_tasks = Table('model_tasks', metadata,
    Column('id', UUID(as_uuid=False), primary_key=True),
    Column('interview_id', UUID(as_uuid=False), ForeignKey('interviews.id'), nullable=False, unique=True),
    Column('frozen_input', JSONB, nullable=False), Column('input_hash', String(64), nullable=False),
    Column('policy_hash', String(64), nullable=False), Column('attempts', JSONB, nullable=False))
settlements = Table('settlements', metadata,
    Column('id', UUID(as_uuid=False), primary_key=True),
    Column('world_id', String(128), ForeignKey('worlds.world_id'), nullable=False),
    Column('interview_id', UUID(as_uuid=False), ForeignKey('interviews.id'), nullable=False, unique=True),
    Column('player_id', UUID(as_uuid=False), ForeignKey('players.player_id'), nullable=False),
    Column('payload_hash', String(64), nullable=False), Column('result', JSONB, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False))
reward_ledger = Table('reward_ledger', metadata,
    Column('settlement_id', UUID(as_uuid=False), ForeignKey('settlements.id'), primary_key=True),
    Column('player_id', UUID(as_uuid=False), ForeignKey('players.player_id'), primary_key=True),
    Column('award_type', String(32), primary_key=True), Column('before', JSONB, nullable=False),
    Column('after', JSONB, nullable=False))
game_events = Table('game_events', metadata,
    Column('interview_id', UUID(as_uuid=False), ForeignKey('interviews.id'), primary_key=True),
    Column('player_id', UUID(as_uuid=False), ForeignKey('players.player_id'), primary_key=True),
    Column('world_id', String(128), ForeignKey('worlds.world_id'), nullable=False),
    Column('event_type', String(32), nullable=False), Column('payload', JSONB, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False))


# M3 keeps interview-specific foreign keys and every M1/M2 receipt unchanged.
competitions = Table('competitions', metadata,
    Column('id', UUID(as_uuid=False), primary_key=True),
    Column('world_id', String(128), ForeignKey('worlds.world_id'), nullable=False),
    Column('status', String(32), nullable=False), Column('version', Integer, nullable=False),
    Column('body', JSONB, nullable=False), Column('created_at', DateTime(timezone=True), nullable=False))
Index('one_active_competition_per_world', competitions.c.world_id, unique=True,
      postgresql_where=competitions.c.status.in_(['READY','SUBMITTING','REVIEWING',
          'WAITING_REVIEW','DISPLAYING','VOTING','READY_TO_SETTLE','PAUSED']))
competition_commands = Table('competition_commands', metadata,
    Column('world_id', String(128), ForeignKey('worlds.world_id'), primary_key=True),
    Column('command_id', UUID(as_uuid=False), primary_key=True),
    Column('payload', JSONB, nullable=False), Column('response', JSONB, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False))
competition_settlements = Table('competition_settlements', metadata,
    Column('id', UUID(as_uuid=False), primary_key=True),
    Column('world_id', String(128), ForeignKey('worlds.world_id'), nullable=False),
    Column('competition_id', UUID(as_uuid=False), ForeignKey('competitions.id'), nullable=False, unique=True),
    Column('payload_hash', String(64), nullable=False), Column('result', JSONB, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False))
competition_reward_ledger = Table('competition_reward_ledger', metadata,
    Column('settlement_id', UUID(as_uuid=False), ForeignKey('competition_settlements.id'), primary_key=True),
    Column('player_id', UUID(as_uuid=False), ForeignKey('players.player_id'), primary_key=True),
    Column('award_type', String(32), primary_key=True), Column('before', JSONB, nullable=False),
    Column('after', JSONB, nullable=False))
competition_events = Table('competition_events', metadata,
    Column('competition_id', UUID(as_uuid=False), ForeignKey('competitions.id'), primary_key=True),
    Column('player_id', UUID(as_uuid=False), ForeignKey('players.player_id'), primary_key=True),
    Column('world_id', String(128), ForeignKey('worlds.world_id'), nullable=False),
    Column('event_type', String(32), nullable=False), Column('payload', JSONB, nullable=False),
    Column('created_at', DateTime(timezone=True), nullable=False))
