"""Restore owned index artifacts when an explicit local update cannot finish."""

from contextlib import closing
from pathlib import Path
import math
import shutil
import sqlite3
import tempfile
import time


_SQLITE_BATCH_PAGES = 256
_FILE_BATCH_BYTES = 1024 * 1024
_PROGRESS_INTERVAL_SECONDS = 0.1
_BUSY_POLL_SECONDS = 0.05


class SourceUpdateBackup:
    def __init__(self, output, names, *, progress=None, check_cancel=None,
                 busy_timeout_seconds=5.0):
        self.output, self.names = Path(output), tuple(names)
        self.progress, self.check_cancel = progress, check_cancel
        self.busy_timeout_seconds = float(busy_timeout_seconds)
        if not math.isfinite(self.busy_timeout_seconds) or self.busy_timeout_seconds < 0:
            raise ValueError("Backup lock timeout must be finite and nonnegative.")
        # A sibling stays on the same filesystem in ordinary workspaces and
        # cannot leave an unknown entry inside the managed output directory.
        try:
            self.temporary = tempfile.TemporaryDirectory(prefix="source-update-", dir=self.output.parent)
        except PermissionError:
            self.temporary = tempfile.TemporaryDirectory(prefix="source-update-")
        self.root = Path(self.temporary.name)
        self.existing = set()
        self.file_sizes = {}
        self.completed_bytes = 0
        self.last_progress_at = None
        self.last_file_bytes = 0
        try:
            for name in self.names:
                self._check_cancel()
                path = self.output / name
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise ValueError("SOURCE_UPDATE_PATH_INVALID")
                if path.is_file():
                    self.existing.add(name)
                    self.file_sizes[name] = path.stat().st_size
            self._emit(force=True)
            for name in self.names:
                self._check_cancel()
                if name not in self.existing:
                    continue
                path = self.output / name
                self._emit(name, force=True)
                if name.endswith(".sqlite"):
                    self._backup_database(path, name)
                else:
                    self._backup_file(path, name)
                self.completed_bytes += self.file_sizes[name]
                self._emit(name, self.file_sizes[name], force=True, finished=True)
            self._check_cancel()
        except BaseException:
            self.close()
            raise

    def _check_cancel(self):
        if self.check_cancel:
            self.check_cancel()

    def _emit(self, name=None, file_bytes=0, *, force=False, finished=False):
        if self.progress is None:
            return
        now = time.monotonic()
        force = force or (file_bytes > 0 and self.last_file_bytes == 0)
        if not force and self.last_progress_at is not None and now - self.last_progress_at < _PROGRESS_INTERVAL_SECONDS:
            return
        self.last_progress_at = now
        self.last_file_bytes = file_bytes
        completed = self.completed_bytes + (0 if finished else file_bytes)
        total = sum(self.file_sizes.values())
        event = {"phase": "backing_up", "completed": completed, "total": total,
                 "unit": "bytes", "current_file": name,
                 "bytes_completed": completed, "bytes_total": total}
        if name is not None:
            event.update(file_completed=file_bytes, file_total=self.file_sizes[name], file_unit="bytes")
        self.progress(event)

    def _check_busy(self, started_at):
        self._check_cancel()
        now = time.monotonic()
        if started_at is None:
            started_at = now
        if now - started_at >= self.busy_timeout_seconds:
            raise ValueError("SOURCE_UPDATE_BACKUP_BUSY")
        return started_at

    def _backup_database(self, path, name):
        with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True,
                                     timeout=_BUSY_POLL_SECONDS)) as source:
            busy_started_at = None
            while True:
                self._check_cancel()
                try:
                    page_size = source.execute("PRAGMA page_size").fetchone()[0]
                    break
                except sqlite3.OperationalError as error:
                    if getattr(error, "sqlite_errorcode", None) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        raise
                    busy_started_at = self._check_busy(busy_started_at)
                    self._emit(name)
                    time.sleep(_BUSY_POLL_SECONDS)
            busy_started_at = None

            def advance(status, remaining, total):
                nonlocal busy_started_at
                self._check_cancel()
                if status in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    busy_started_at = self._check_busy(busy_started_at)
                    self._emit(name)
                    return
                busy_started_at = None
                self.file_sizes[name] = total * page_size
                self._emit(name, (total - remaining) * page_size, force=status == sqlite3.SQLITE_DONE)

            with closing(sqlite3.connect(self.root / name)) as target:
                source.backup(target, pages=_SQLITE_BATCH_PAGES, progress=advance, sleep=_BUSY_POLL_SECONDS)

    def _backup_file(self, path, name):
        copied = 0
        with path.open("rb") as source, (self.root / name).open("wb") as target:
            while True:
                self._check_cancel()
                chunk = source.read(_FILE_BATCH_BYTES)
                if not chunk:
                    break
                target.write(chunk)
                copied += len(chunk)
                self._emit(name, copied)
        self.file_sizes[name] = copied

    def restore(self):
        # Rollback must finish even when the update's cancellation flag is set.
        self.output.mkdir(parents=True, exist_ok=True)
        for name in self.names:
            path = self.output / name
            if path.is_symlink():
                raise ValueError("SOURCE_UPDATE_PATH_INVALID")
            if name.endswith(".sqlite"):
                for suffix in ("-wal", "-shm", "-journal"):
                    Path(str(path) + suffix).unlink(missing_ok=True)
            if name in self.existing:
                with tempfile.NamedTemporaryFile(dir=self.output, delete=False) as handle:
                    temporary = Path(handle.name)
                try:
                    shutil.copyfile(self.root / name, temporary)
                    temporary.replace(path)
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                path.unlink(missing_ok=True)

    def close(self):
        self.temporary.cleanup()
