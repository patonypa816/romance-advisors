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
            response = {"player": await self._profile(conn, row), "revision": revision, "replayed": False}
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
                    result["player"] = await self._profile(conn, rows[0])
                else:
                    result["players"] = [await self._profile(conn, row) for row in rows]
                return result

    async def list_players(self, query: str = ""):
        return await self._read(query=query)

    async def get_player(self, player_id: str):
        return await self._read(player_id=player_id)

    async def apply_interview_result(self, conn, world_id, interview_id, player_id, frozen_plan):
        """Only gameplay calls this; caller owns world lock and atomic transaction."""
        from ..db import settlements, reward_ledger, memberships, game_events
        from ..evaluation import content_hash
        from sqlalchemy import text
        if world_id != self.world_id:
            raise ValueError('wrong world')
        plan_hash = content_hash(frozen_plan)
        old = (await conn.execute(select(settlements).where(
            settlements.c.world_id == world_id, settlements.c.interview_id == interview_id))).mappings().first()
        if old:
            if old['payload_hash'] != plan_hash:
                raise CommandConflict('同一结算来源不得改变结果。')
            return old['result']
        player = (await conn.execute(select(players).where(players.c.world_id == world_id,
            players.c.player_id == player_id))).mappings().first()
        if not player:
            raise PlayerNotFound(player_id)
        member = (await conn.execute(select(memberships).where(memberships.c.player_id == player_id,
            memberships.c.world_id == world_id))).mappings().first()
        if member and member['role'] != 'guest':
            raise CommandConflict('已入职玩家不可再次录用。')
        timestamp = (await conn.execute(text('SELECT clock_timestamp()'))).scalar_one()
        hired = frozen_plan['decision'] == 'hire'
        settlement_id = str(uuid4())
        result = {'decision': frozen_plan['decision'], 'settlement_id': settlement_id,
            'summary': ('录用为危机公关实习生。' if hired else '本次面试未录用；游客档案不变。'),
            'completed_at': timestamp.isoformat()}
        await conn.execute(settlements.insert().values(id=settlement_id, world_id=world_id,
            interview_id=interview_id, player_id=player_id, payload_hash=plan_hash,
            result=result, created_at=timestamp))
        if hired:
            before = {'role':'guest','department':None,'position':None,'performance':0}
            after = {'role':'employee','department':frozen_plan['department'],
                'position':frozen_plan['position'],'performance':0}
            await conn.execute(reward_ledger.insert().values(settlement_id=settlement_id,
                player_id=player_id, award_type='hire', before=before, after=after))
            await conn.execute(insert(memberships).values(player_id=player_id, world_id=world_id, **after)
                .on_conflict_do_update(index_elements=[memberships.c.player_id], set_=after))
        await conn.execute(game_events.insert().values(interview_id=interview_id, player_id=player_id,
            world_id=world_id,event_type='interview_hired' if hired else 'interview_rejected',
            payload=result,created_at=timestamp))
        return result

    async def _profile(self, conn, row):
        from ..db import memberships, game_events
        value = player_json(row)
        member = (await conn.execute(select(memberships).where(
            memberships.c.player_id == row['player_id'], memberships.c.world_id == self.world_id))).mappings().first()
        if member:
            value.update({k:member[k] for k in ('role','department','position','performance')})
        events = (await conn.execute(select(game_events).where(game_events.c.world_id == self.world_id,
            game_events.c.player_id == row['player_id']).order_by(game_events.c.created_at.desc()).limit(10))).mappings().all()
        value['recent_events'] = [{'type':e['event_type'],'interview_id':e['interview_id'],
            'created_at':e['created_at'].isoformat(), **e['payload']} for e in events]
        return value
