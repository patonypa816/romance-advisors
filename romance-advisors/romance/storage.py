"""Small SQLite store; one transaction saves a complete game transition.

Records use JSON so early content experiments do not require schema migrations.
The database still enforces the two message uniqueness rules independently.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


class Storage:
    TABLES = ("sessions", "episodes", "rounds", "proposals", "votes", "messages", "comments")

    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(Path(path).expanduser()) if str(path) != ":memory:" else ":memory:")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        for table in self.TABLES:
            self.db.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_man_per_round
            ON messages(json_extract(body, '$.round_id'))
            WHERE json_extract(body, '$.role') = 'man'""")
        self.db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_reply_per_parent
            ON messages(json_extract(body, '$.parent_id'))
            WHERE json_extract(body, '$.parent_id') IS NOT NULL""")
        self.db.commit()
        self._bodies = {table: {} for table in self.TABLES}
        self._meta_body = None
        self._cache_loaded = False

    def load(self):
        row = self.db.execute("SELECT body FROM meta WHERE id=1").fetchone()
        self._meta_body = row[0] if row else None
        state = json.loads(row[0]) if row else {"session_id": None, "episode_id": None, "round_id": None, "error": None}
        for table in self.TABLES:
            rows = list(self.db.execute(f"SELECT id, body FROM {table} ORDER BY rowid"))
            self._bodies[table] = dict(rows)
            state[table] = [json.loads(body) for _, body in rows]
        self._cache_loaded = True
        return state

    def save(self, state):
        meta = {key: value for key, value in state.items() if key not in self.TABLES}
        return self.save_changes({table: state[table] for table in self.TABLES}, meta=meta)

    def save_changes(self, changes, *, meta=None):
        """Commit only changed records; publish the comparison cache after COMMIT.

        Callers can stage a receipt and its effective input without copying or
        serializing unrelated history. This is an upsert API, never a delete API.
        """
        if not self._cache_loaded:
            self.load()
        encoded = {}
        for table, records in changes.items():
            if table not in self.TABLES:
                raise ValueError("invalid_storage_table")
            pending = {}
            for record in records:
                body = json.dumps(record, ensure_ascii=False, allow_nan=False)
                old = pending.get(record["id"], self._bodies[table].get(record["id"]))
                if old == body:
                    continue
                # JSON formatting differences in old databases are not mutations.
                if old is not None and json.loads(old) == record:
                    continue
                if table == "messages" and old is not None:
                    raise ValueError("committed_message_is_immutable")
                pending[record["id"]] = body
            if pending:
                encoded[table] = pending
        meta_body = json.dumps(meta, ensure_ascii=False, allow_nan=False) if meta is not None else self._meta_body
        meta_changed = meta_body != self._meta_body
        if not encoded and not meta_changed:
            return {}
        # Score/reply and comment/vote pairs still share one SQLite transaction.
        with self.db:
            if meta_changed:
                self.db.execute("INSERT INTO meta VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (meta_body,))
            for table, records in encoded.items():
                for key, body in records.items():
                    if table == "messages":
                        self.db.execute("INSERT INTO messages VALUES (?, ?)", (key, body))
                    else:
                        self.db.execute(f"INSERT INTO {table} VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body", (key, body))
        if meta_changed:
            self._meta_body = meta_body
        for table, records in encoded.items():
            self._bodies[table].update(records)
        return {table: set(records) for table, records in encoded.items()}

    def close(self):
        self.db.close()
