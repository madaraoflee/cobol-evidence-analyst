"""Transactional, isolated online conversation state; never a source index."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading

from online_material_format import MaterialError, canonical_json, decode_json
from online_sqlite_store import OnlineSQLiteStore, supports_managed_wal, _cleanup_note


MAX_STATE_RECORD_BYTES = 16 * 1024 * 1024
_TABLES = frozenset({"workspaces", "conversations", "conversation_summaries", "jobs", "settings"})


class OnlineState:
    def __init__(self, root, *, journal_mode=None):
        self.root = Path(root)
        if self.root.is_symlink():
            raise MaterialError("ONLINE_STATE_INVALID")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root = self.root.resolve(strict=True)
        self.path = self.root / "online-state.sqlite"
        self._mode_lock = threading.RLock()
        self._transaction_depth = 0
        self._analysis_depth = 0
        self._analysis_database = None
        self._explicit_journal_mode = journal_mode
        try:
            # Idle history retains its original single-file rollback layout.
            # A running analysis may take the explicitly scoped WAL lease below.
            self._database = OnlineSQLiteStore(self.path,
                journal_mode="DELETE" if journal_mode is None else journal_mode, timeout=15)
        except sqlite3.Error:
            raise MaterialError("ONLINE_STATE_UNAVAILABLE") from None
        try:
            with self.transaction() as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1):
                    raise MaterialError("ONLINE_STATE_VERSION")
                for table in sorted(_TABLES):
                    connection.execute(f"CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, data BLOB NOT NULL)")
                connection.execute("PRAGMA user_version=1")
        except BaseException:
            try:
                self.close()
            except sqlite3.Error:
                pass
            raise

    @contextmanager
    def _mode_locked(self):
        if not self._mode_lock.acquire(timeout=15):
            raise sqlite3.OperationalError("Online state mode is busy")
        try:
            yield
        finally:
            self._mode_lock.release()

    def close(self, *, checkpoint=False):
        with self._mode_locked():
            if self._analysis_depth or self._transaction_depth:
                raise sqlite3.ProgrammingError("Cannot close active online state")
            self._database.close(checkpoint=checkpoint)

    @contextmanager
    def analysis_writes(self):
        """Share safe WAL writes only while jobs own an analysis lease.

        The final lease enables SQLite's own checkpoint-on-close before any
        job publishes its completed state. No journal file is deleted here.
        """
        if self._explicit_journal_mode is not None or not supports_managed_wal():
            yield
            return
        try:
            with self._mode_locked():
                if self._transaction_depth:
                    raise sqlite3.ProgrammingError("Cannot switch active online state transaction")
                if self._analysis_depth == 0:
                    self._database.close()
                    managed = OnlineSQLiteStore(self.path, journal_mode="WAL", timeout=15)
                    try:
                        with managed.connection():
                            pass
                    except BaseException:
                        try:
                            managed.close()
                        except sqlite3.Error:
                            pass
                        raise
                    self._analysis_database = managed
                self._analysis_depth += 1
            primary_error = None
            try:
                yield
            except BaseException as error:
                primary_error = error
                raise
            finally:
                try:
                    with self._mode_locked():
                        self._analysis_depth -= 1
                        if self._analysis_depth == 0:
                            managed = self._analysis_database
                            try:
                                managed.close(checkpoint=True)
                            finally:
                                self._analysis_database = None
                except sqlite3.Error as cleanup:
                    if primary_error is None:
                        raise
                    _cleanup_note(primary_error, "online state lease cleanup failed", cleanup)
        except sqlite3.Error:
            raise MaterialError("ONLINE_STATE_UNAVAILABLE") from None

    @contextmanager
    def transaction(self):
        try:
            with self._mode_locked():
                database = self._analysis_database or self._database
                with database.connection() as connection:
                    self._transaction_depth += 1
                    try:
                        if not connection.in_transaction:
                            connection.execute("BEGIN IMMEDIATE")
                        yield connection
                    finally:
                        self._transaction_depth -= 1
        except sqlite3.Error:
            raise MaterialError("ONLINE_STATE_UNAVAILABLE") from None

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
