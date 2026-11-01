"""Contract tests against real, migrated PostgreSQL; never substitute SQLite."""
import asyncio
import os
import unittest
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from hell_inc.records.service import CommandConflict, PlayerNotFound, RecordService


def registration(**changes):
    return {"command_id": str(uuid4()), "provider": "simulated", "identity_scope": "local-test",
            "subject_id": "visitor-001", "display_name": "小林", **changes}


class RecordsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        url = os.environ.get("HELL_INC_DATABASE_URL")
        if not url or not url.startswith("postgresql+asyncpg://"):
            self.fail("Set HELL_INC_DATABASE_URL to an isolated, migrated PostgreSQL database")
        self.engine = create_async_engine(url)
        self.addAsyncCleanup(self.engine.dispose)
        self.world_id = "test-" + uuid4().hex
        self.service = RecordService(self.engine, self.world_id)
        await self.service.initialize_world()

    async def test_identity_rename_and_same_name_are_distinct(self):
        a = await self.service.register(registration())
        unchanged = await self.service.register(registration())
        self.assertEqual(a["player"], unchanged["player"])
        self.assertEqual(a["revision"], unchanged["revision"])
        renamed = await self.service.register(registration(display_name="新昵称"))
        b = await self.service.register(registration(subject_id="visitor-002", display_name="新昵称"))
        self.assertEqual(a["player"]["player_id"], renamed["player"]["player_id"])
        self.assertNotEqual(b["player"]["player_id"], renamed["player"]["player_id"])
        self.assertEqual(a["player"]["joined_at"], renamed["player"]["joined_at"])
        self.assertEqual(renamed["revision"], a["revision"] + 1)
        self.assertEqual(b["revision"], renamed["revision"] + 1)
        self.assertEqual(b["player"]["recent_events"], [])
        self.assertEqual(b["player"]["role"], "guest")
        self.assertEqual(b["player"]["performance"], 0)
        self.assertIsNone(b["player"]["department"])
        self.assertIsNone(b["player"]["position"])

    async def test_replay_returns_original_result_not_current_player(self):
        payload = registration()
        original = await self.service.register(payload)
        await self.service.register(registration(display_name="后来改名"))
        replay = await self.service.register(payload)
        self.assertFalse(original["replayed"])
        self.assertEqual(replay, {**original, "replayed": True})
        with self.assertRaises(CommandConflict):
            await self.service.register({**payload, "display_name": "冲突"})
        current = await self.service.get_player(original["player"]["player_id"])
        self.assertEqual(current["player"]["display_name"], "后来改名")

    async def test_concurrent_registrations_and_commands(self):
        payload = registration()
        results = await asyncio.gather(*(self.service.register(payload) for _ in range(12)))
        self.assertEqual(sum(not x["replayed"] for x in results), 1)
        self.assertEqual(len({x["player"]["player_id"] for x in results}), 1)
        self.assertEqual(len({x["revision"] for x in results}), 1)
        await asyncio.gather(*(self.service.register(registration()) for _ in range(12)))
        snapshot = await self.service.list_players()
        self.assertEqual(len(snapshot["players"]), 1)
        self.assertEqual(snapshot["revision"], results[0]["revision"])

    async def test_snapshots_are_revision_consistent_during_writes(self):
        start = (await self.service.list_players())["revision"]
        async def writer():
            for i in range(20):
                await self.service.register(registration(subject_id=f"concurrent-{i}"))
        task = asyncio.create_task(writer())
        try:
            for _ in range(30):
                snapshot = await self.service.list_players()
                self.assertEqual(snapshot["revision"] - start, len(snapshot["players"]))
                self.assertIn("server_time", snapshot)
        finally:
            await task

    async def test_world_isolation_search_literal_and_missing(self):
        a = await self.service.register(registration(subject_id="literal_%", display_name="<img src=x onerror=alert(1)>"))
        await self.service.register(registration(subject_id="other"))
        self.assertEqual(len((await self.service.list_players("literal_%"))["players"]), 1)
        self.assertEqual(len((await self.service.list_players("%"))["players"]), 1)
        other = RecordService(self.engine, "test-" + uuid4().hex)
        await other.initialize_world()
        self.assertEqual((await other.list_players())["players"], [])
        with self.assertRaises(PlayerNotFound):
            await other.get_player(a["player"]["player_id"])
        with self.assertRaises(PlayerNotFound):
            await self.service.get_player(str(uuid4()))

    async def test_new_engine_preserves_committed_record_and_replay(self):
        payload = registration()
        original = await self.service.register(payload)
        await self.engine.dispose()
        fresh = create_async_engine(os.environ["HELL_INC_DATABASE_URL"])
        try:
            service = RecordService(fresh, self.world_id)
            await service.initialize_world()
            self.assertEqual((await service.get_player(original["player"]["player_id"]))["player"], original["player"])
            self.assertEqual(await service.register(payload), {**original, "replayed": True})
        finally:
            await fresh.dispose()

    async def test_transaction_fault_does_not_leave_partial_registration(self):
        before = await self.service.list_players()
        payload = registration()
        # Inject at the final command-result write, after player/revision writes.
        def fault(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("INSERT INTO") and "commands" in statement.lower():
                raise RuntimeError("injected transaction failure")
        event.listen(self.engine.sync_engine, "before_cursor_execute", fault)
        try:
            with self.assertRaisesRegex(RuntimeError, "injected transaction failure"):
                await self.service.register(payload)
        finally:
            event.remove(self.engine.sync_engine, "before_cursor_execute", fault)
        after = await self.service.list_players()
        self.assertEqual(after["players"], before["players"])
        self.assertEqual(after["revision"], before["revision"])
        self.assertFalse((await self.service.register(payload))["replayed"])
