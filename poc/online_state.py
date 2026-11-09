"""Transactional, isolated online conversation state; never a source index."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3

from online_material_format import MaterialError, canonical_json, decode_json


MAX_STATE_RECORD_BYTES = 16 * 1024 * 1024
_TABLES = frozenset({"workspaces", "conversations", "conversation_summaries", "jobs", "settings"})


class OnlineState:
    def __init__(self, root):
        self.root = Path(root)
        if self.root.is_symlink():
            raise MaterialError("ONLINE_STATE_INVALID")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = self.root.resolve(strict=True)
        self.path = self.root / "online-state.sqlite"
        with self.transaction() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise MaterialError("ONLINE_STATE_VERSION")
            for table in sorted(_TABLES):
                connection.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, data BLOB NOT NULL)")
            connection.execute("PRAGMA user_version=1")

    @contextmanager
    def transaction(self):
        if any(Path(str(self.path) + suffix).is_symlink() for suffix in ("", "-wal", "-shm", "-journal")):
            raise MaterialError("ONLINE_STATE_INVALID")
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=15)
            self.path.chmod(0o600)
            connection.execute("PRAGMA busy_timeout=15000")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except sqlite3.Error:
            if connection is not None:
                connection.rollback()
            raise MaterialError("ONLINE_STATE_UNAVAILABLE") from None
        except BaseException:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def put(connection, table, identifier, value):
        if table not in _TABLES or not isinstance(identifier, str):
            raise MaterialError("ONLINE_STATE_INVALID")
        data = canonical_json(value)
        if len(data) > MAX_STATE_RECORD_BYTES:
            raise MaterialError("ONLINE_HISTORY_LIMIT")
        connection.execute(f"INSERT INTO {table}(id,data) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                           (identifier, data))

    @staticmethod
    def get(connection, table, identifier):
        if table not in _TABLES:
            raise MaterialError("ONLINE_STATE_INVALID")
        row = connection.execute(f"SELECT data FROM {table} WHERE id=?", (identifier,)).fetchone()
        return decode_json(bytes(row[0]), maximum=MAX_STATE_RECORD_BYTES) if row else None

    @staticmethod
    def all(connection, table):
        if table not in _TABLES:
            raise MaterialError("ONLINE_STATE_INVALID")
        # Bound corrupted or externally edited state before materializing it.
        rows = connection.execute(f"SELECT id,length(data) FROM {table} LIMIT 1001").fetchall()
        if (len(rows) > 1000 or any(row[1] > MAX_STATE_RECORD_BYTES for row in rows)
                or sum(row[1] for row in rows) > 64 * 1024 * 1024):
            raise MaterialError("ONLINE_HISTORY_LIMIT")
        return [OnlineState.get(connection, table, row[0]) for row in rows]

    @staticmethod
    def delete(connection, table, identifier):
        if table not in _TABLES:
            raise MaterialError("ONLINE_STATE_INVALID")
        connection.execute(f"DELETE FROM {table} WHERE id=?", (identifier,))
