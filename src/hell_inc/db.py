"""Explicit schema v1 migration. Never called by web startup."""
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
    # One explicit, transactional baseline migration; future versions append migrations.
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(721023061)"))
        await conn.run_sync(metadata.create_all)
        existing = (await conn.execute(versions.select())).scalars().all()
        if any(v != 1 for v in existing):
            raise RuntimeError("未知数据库版本，拒绝迁移。")
        if not existing:
            await conn.execute(versions.insert().values(version=1))
