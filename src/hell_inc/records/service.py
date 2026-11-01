from datetime import datetime, timezone
from uuid import uuid4, UUID
from sqlalchemy import select, update, or_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine
from ..contracts import validate_registration
from ..db import worlds, players, commands


class PlayerNotFound(LookupError):
    pass


class CommandConflict(ValueError):
    pass


def now():
    return datetime.now(timezone.utc)


def player_json(row):
    return {**{k: row[k] for k in ("player_id", "provider", "identity_scope", "subject_id", "display_name")},
            "role": "guest", "department": None, "position": None, "performance": 0,
            "joined_at": row["joined_at"].isoformat(), "updated_at": row["updated_at"].isoformat(),
            "recent_events": []}


class RecordService:
    def __init__(self, engine: AsyncEngine, world_id: str):
        self.engine, self.world_id = engine, world_id

    async def initialize_world(self):
        async with self.engine.begin() as conn:
            await conn.execute(insert(worlds).values(world_id=self.world_id, revision=0)
                               .on_conflict_do_nothing(index_elements=[worlds.c.world_id]))

    async def register(self, data: dict):
        data = validate_registration(data)
        payload = {k: v for k, v in data.items() if k != "command_id"}
        async with self.engine.begin() as conn:
            # Serialize all mutations for one world; no network/model work in transaction.
            revision = (await conn.execute(select(worlds.c.revision).where(
                worlds.c.world_id == self.world_id).with_for_update())).scalar_one()
            old = (await conn.execute(select(commands).where(
                commands.c.world_id == self.world_id,
                commands.c.command_id == data["command_id"]))).mappings().first()
            if old:
                if old["payload"] != payload:
                    raise CommandConflict("同一 command_id 不可使用不同载荷。")
                return {**old["response"], "replayed": True}
            identity = [players.c.world_id == self.world_id] + [players.c[k] == payload[k]
                       for k in ("provider", "identity_scope", "subject_id")]
            row = (await conn.execute(select(players).where(*identity))).mappings().first()
            changed = row is None or row["display_name"] != payload["display_name"]
            timestamp = now()
            if row is None:
                row = (await conn.execute(players.insert().values(
                    player_id=str(uuid4()), world_id=self.world_id, **payload,
                    joined_at=timestamp, updated_at=timestamp).returning(players))).mappings().one()
            elif changed:
                row = (await conn.execute(update(players).where(*identity).values(
                    display_name=payload["display_name"], updated_at=timestamp)
                    .returning(players))).mappings().one()
            if changed:
                revision += 1
                await conn.execute(update(worlds).where(worlds.c.world_id == self.world_id)
                                   .values(revision=revision))
            response = {"player": player_json(row), "revision": revision, "replayed": False}
            await conn.execute(commands.insert().values(world_id=self.world_id,
                command_id=data["command_id"], payload=payload, response=response, created_at=timestamp))
            return response

    async def _read(self, *, query=None, player_id=None):
        async with self.engine.connect() as conn:
            conn = await conn.execution_options(isolation_level="REPEATABLE READ")
            async with conn.begin():
                revision = (await conn.execute(select(worlds.c.revision).where(
                    worlds.c.world_id == self.world_id))).scalar_one()
                stmt = select(players).where(players.c.world_id == self.world_id)
                if player_id is not None:
                    try:
                        player_id = str(UUID(player_id))
                    except (ValueError, AttributeError):
                        raise PlayerNotFound(player_id) from None
                    stmt = stmt.where(players.c.player_id == player_id)
                elif query:
                    stmt = stmt.where(or_(players.c.subject_id.contains(query, autoescape=True),
                                          players.c.display_name.contains(query, autoescape=True)))
                rows = (await conn.execute(stmt.order_by(players.c.joined_at, players.c.player_id))).mappings().all()
                result = {"revision": revision, "server_time": now().isoformat()}
                if player_id is not None:
                    if not rows:
                        raise PlayerNotFound(player_id)
                    result["player"] = player_json(rows[0])
                else:
                    result["players"] = [player_json(row) for row in rows]
                return result

    async def list_players(self, query: str = ""):
        return await self._read(query=query)

    async def get_player(self, player_id: str):
        return await self._read(player_id=player_id)
