"""Managed SQLite connections for online state only.

The local source index and its SQLite policy do not use this module. Older
engines retain per-operation rollback journals; WAL needs the documented race
fix and the connection configuration APIs before it can be selected.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import math
import sqlite3
import threading
import weakref

from online_material_format import MaterialError


_ROLLBACK_MODES = frozenset({"DELETE", "PERSIST", "TRUNCATE"})
_SIDECARS = ("", "-wal", "-shm", "-journal")


def wal_version_is_safe(version):
    """SQLite's WAL-reset fix, including the two documented backports."""
    version = tuple(version)
    return (version >= (3, 51, 3)
            or version[:2] == (3, 50) and version >= (3, 50, 7)
            or version[:2] == (3, 44) and version >= (3, 44, 6))


def supports_managed_wal():
    return (wal_version_is_safe(sqlite3.sqlite_version_info)
            and sqlite3.threadsafety > 0
            and hasattr(sqlite3, "SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE")
            and callable(getattr(sqlite3.Connection, "setconfig", None))
            and callable(getattr(sqlite3.Connection, "getconfig", None)))


def _identity(path):
    try:
        value = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    return value.st_dev, value.st_ino


def _quiet_close(connection):
    try:
        connection.close()
    except sqlite3.Error:
        pass


def _cleanup_note(error, stage, cleanup=None):
    detail = " (" + type(cleanup).__name__ + ")" if cleanup is not None else ""
    try:
        add_note = getattr(error, "add_note", None)
        if callable(add_note):
            add_note("Online SQLite " + stage + detail + "; the original exception is preserved.")
    except BaseException:
        # Diagnostic annotation must never replace the operation's exception.
        pass


class OnlineSQLiteStore:
    def __init__(self, path, *, journal_mode=None, timeout=30):
        # Keep the lexical path: resolving it could follow a database symlink
        # before the caller's original path guards have rejected it.
        timeout = float(timeout)
        if not math.isfinite(timeout) or not 0 <= timeout <= threading.TIMEOUT_MAX:
            raise ValueError("Online SQLite timeout must be finite and nonnegative")
        self.path, self.timeout = Path(path), timeout
        mode = ("WAL" if supports_managed_wal() else "DELETE") if journal_mode is None else str(journal_mode).upper()
        if mode not in _ROLLBACK_MODES | {"WAL"}:
            raise ValueError("Unsupported online SQLite journal mode")
        if mode == "WAL" and not supports_managed_wal():
            raise sqlite3.NotSupportedError("Online WAL requires a fixed SQLite engine and configuration support")
        self.journal_mode = mode
        self._lock = threading.RLock()
        self._db, self._finalizer, self._identities = None, None, None
        self._depth, self._savepoint_number, self._invalid = 0, 0, False
        self._transaction_failed, self._discard_on_exit = None, False
        self._parent_identity = _identity(self.path.parent)
        self._guard()

    def _guard(self, expected=None):
        if (self._invalid or self.path.parent.is_symlink()
                or _identity(self.path.parent) != self._parent_identity
                or any(Path(str(self.path) + suffix).is_symlink() for suffix in _SIDECARS)):
            self._invalid = True
            raise MaterialError("ONLINE_STATE_INVALID")
        if expected is not None:
            for suffix, identity in expected.items():
                if identity is not None and _identity(Path(str(self.path) + suffix)) != identity:
                    self._invalid = True
                    raise MaterialError("ONLINE_STATE_INVALID")

    def _remember(self):
        return {suffix: _identity(Path(str(self.path) + suffix)) for suffix in ("", "-wal", "-shm")}

    def _open(self, *, managed):
        self._guard()
        options = {"timeout": self.timeout}
        if managed:
            options["check_same_thread"] = False
        db = sqlite3.connect(self.path, **options)
        try:
            self.path.chmod(0o600)
            db.execute("PRAGMA synchronous=FULL")
            if db.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise sqlite3.OperationalError("Online SQLite requires synchronous FULL")
            if managed:
                if not supports_managed_wal():
                    raise sqlite3.NotSupportedError("Online WAL requires a fixed SQLite engine")
                db.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
                if db.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE) is not True:
                    raise sqlite3.NotSupportedError("Online WAL requires NO_CKPT_ON_CLOSE")
            actual = db.execute("PRAGMA journal_mode=" + self.journal_mode).fetchone()[0]
            if actual.upper() != self.journal_mode:
                raise sqlite3.OperationalError("Online SQLite journal mode was not applied")
            if managed:
                db.execute("PRAGMA wal_autocheckpoint=1000")
                if db.execute("PRAGMA wal_autocheckpoint").fetchone()[0] != 1000:
                    raise sqlite3.OperationalError("Online WAL requires automatic checkpoints")
                cursor = db.execute("SELECT count(*) FROM sqlite_schema")
                try:
                    if cursor.fetchone() is None:
                        raise sqlite3.OperationalError("Online SQLite schema read did not complete")
                finally:
                    cursor.close()
                if db.in_transaction:
                    raise sqlite3.OperationalError("Online SQLite activation left an open transaction")
            self._guard()
            return db
        except BaseException as error:
            try:
                db.close()
            except BaseException as cleanup:
                _cleanup_note(error, "open cleanup failed", cleanup)
            raise

    @contextmanager
    def _locked(self):
        if not self._lock.acquire(timeout=self.timeout):
            raise sqlite3.OperationalError("Online SQLite connection is busy")
        try:
            yield
        finally:
            self._lock.release()

    def _close_cached(self):
        db, finalizer = self._db, self._finalizer
        self._db, self._finalizer, self._identities = None, None, None
        if db is not None:
            try:
                db.close()
            finally:
                if finalizer is not None:
                    finalizer.detach()

    @contextmanager
    def connection(self):
        if self.journal_mode != "WAL":
            # The diagnostic override and older runtimes preserve independent
            # connections and SQLite's existing reader/writer busy handling.
            db = self._open(managed=False)
            expected = {"": _identity(self.path)}
            primary_error = None
            try:
                yield db
                self._guard(expected)
                db.commit()
            except BaseException as error:
                primary_error = error
                try:
                    db.rollback()
                except BaseException as cleanup:
                    _cleanup_note(error, "rollback cleanup failed", cleanup)
                raise
            finally:
                try:
                    db.close()
                except BaseException as cleanup:
                    if primary_error is None:
                        raise
                    _cleanup_note(primary_error, "close cleanup failed", cleanup)
            return
        with self._locked():
            try:
                self._guard(self._identities)
            except MaterialError as error:
                if self._depth == 0:
                    try:
                        self._close_cached()
                    except BaseException as cleanup:
                        _cleanup_note(error, "invalid-path close cleanup failed", cleanup)
                raise
            if self._db is None:
                self._db = self._open(managed=True)
                self._identities = self._remember()
                self._finalizer = weakref.finalize(self, _quiet_close, self._db)
            db, savepoint = self._db, None
            if self._depth:
                if self._transaction_failed is not None:
                    raise sqlite3.OperationalError("Online SQLite enclosing transaction requires rollback") from self._transaction_failed
                if not db.in_transaction:
                    db.execute("BEGIN")
                self._savepoint_number += 1
                savepoint = "online_nested_" + str(self._savepoint_number)
                db.execute("SAVEPOINT " + savepoint)
            self._depth += 1
            primary_error = None
            try:
                yield db
                self._guard(self._identities)
                if self._transaction_failed is not None:
                    raise sqlite3.OperationalError("Online SQLite enclosing transaction cannot commit after nested rollback") from self._transaction_failed
                if savepoint is None:
                    db.commit()
                else:
                    db.execute("RELEASE SAVEPOINT " + savepoint)
            except BaseException as error:
                primary_error = error
                try:
                    if savepoint is None:
                        db.rollback()
                    elif db.in_transaction:
                        db.execute("ROLLBACK TO SAVEPOINT " + savepoint)
                        db.execute("RELEASE SAVEPOINT " + savepoint)
                    else:
                        # RAISE(ROLLBACK) can already have removed every
                        # savepoint and all of the enclosing transaction.
                        self._transaction_failed, self._discard_on_exit = error, True
                        _cleanup_note(error, "nested transaction had already rolled back")
                except BaseException as cleanup:
                    self._transaction_failed, self._discard_on_exit = error, True
                    _cleanup_note(error, "rollback cleanup failed", cleanup)
                raise
            finally:
                self._depth -= 1
                if self._depth == 0:
                    discard = self._invalid or self._discard_on_exit
                    self._transaction_failed, self._discard_on_exit = None, False
                    if discard:
                        try:
                            self._close_cached()
                        except BaseException as cleanup:
                            if primary_error is None:
                                raise
                            _cleanup_note(primary_error, "close cleanup failed", cleanup)
                    else:
                        self._identities = self._remember()

    def close(self, *, checkpoint=False):
        with self._locked():
            if self._depth:
                raise sqlite3.ProgrammingError("Cannot close an active online transaction")
            primary_error = None
            try:
                if checkpoint and self._db is not None:
                    # Let SQLite perform its ordinary last-connection cleanup.
                    # Never enable it after a path or sidecar identity violation.
                    self._guard(self._identities)
                    self._db.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, False)
                    if self._db.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE) is not False:
                        raise sqlite3.OperationalError("Online SQLite checkpoint cleanup was not enabled")
            except BaseException as error:
                primary_error = error
                raise
            finally:
                try:
                    self._close_cached()
                except BaseException as cleanup:
                    if primary_error is None:
                        raise
                    _cleanup_note(primary_error, "checkpoint close cleanup failed", cleanup)
