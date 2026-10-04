"""Restore owned index artifacts when an explicit local update cannot finish."""

from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile


class SourceUpdateBackup:
    def __init__(self, output, names):
        self.output, self.names = Path(output), tuple(names)
        self.temporary = tempfile.TemporaryDirectory(prefix="source-update-")
        self.root = Path(self.temporary.name)
        self.existing = set()
        try:
            for name in self.names:
                path = self.output / name
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise ValueError("SOURCE_UPDATE_PATH_INVALID")
                if not path.is_file():
                    continue
                self.existing.add(name)
                if name.endswith(".sqlite"):
                    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as source:
                        with closing(sqlite3.connect(self.root / name)) as target:
                            source.backup(target)
                else:
                    shutil.copyfile(path, self.root / name)
        except BaseException:
            self.close()
            raise

    def restore(self):
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
