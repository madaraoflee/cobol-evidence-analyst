"""Local source-content versions and change counts, separate from answer archives."""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


def _key(source):
    return hashlib.sha256(str(Path(source).resolve()).encode("utf-8")).hexdigest()


def _path(output):
    path = Path(output) / "source-versions.sqlite"
    if any(Path(str(path) + suffix).is_symlink() for suffix in ("", "-wal", "-shm", "-journal")):
        raise ValueError("SOURCE_VERSION_PATH_INVALID")
    return path


def _summary(row):
    return {key: row[key] for key in ("source_key", "version_id", "snapshot_id", "created_at", "checked_at",
                                    "file_count", "previous_version_id")} | {
        "provider": "local", "changes": json.loads(row["changes_json"])}


def versions(output, source):
    path = _path(output)
    if not path.is_file():
        return []
    with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [_summary(row) for row in db.execute(
            "SELECT * FROM source_versions WHERE source_key=? ORDER BY revision DESC LIMIT 20", (_key(source),))]


def record_version(output, source, snapshot_id):
    """Record indexed content hashes; this is not a full-source backup or atomic disk snapshot."""
    index = Path(output) / "structural-index.sqlite"
    with closing(sqlite3.connect(f"{index.resolve().as_uri()}?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_files'").fetchone():
            return None
        files = dict(db.execute("SELECT relative_path,sha256 FROM source_files ORDER BY relative_path"))
    if not files:
        return None
    manifest = json.dumps(files, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    version_id = "local:" + hashlib.sha256(manifest.encode("utf-8")).hexdigest()
    source_key, now = _key(source), datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(_path(output))) as db:
        db.row_factory = sqlite3.Row
        db.execute("""CREATE TABLE IF NOT EXISTS source_versions (
            revision INTEGER PRIMARY KEY, source_key TEXT NOT NULL, version_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL, created_at TEXT NOT NULL, checked_at TEXT NOT NULL,
            file_count INTEGER NOT NULL, previous_version_id TEXT,
            changes_json TEXT NOT NULL, manifest_json TEXT NOT NULL)""")
        with db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM source_versions WHERE source_key=? ORDER BY revision DESC LIMIT 1",
                                  (source_key,)).fetchone()
            if previous and previous["version_id"] == version_id:
                db.execute("UPDATE source_versions SET checked_at=?,snapshot_id=? WHERE revision=?",
                           (now, snapshot_id, previous["revision"]))
            else:
                old = json.loads(previous["manifest_json"]) if previous else {}
                shared = files.keys() & old.keys()
                changed = sum(files[name] != old[name] for name in shared)
                changes = {"added": len(files.keys() - old.keys()), "modified": changed,
                           "removed": len(old.keys() - files.keys()), "unchanged": len(shared) - changed}
                db.execute("""INSERT INTO source_versions
                    (source_key,version_id,snapshot_id,created_at,checked_at,file_count,
                     previous_version_id,changes_json,manifest_json) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (source_key, version_id, snapshot_id, now, now, len(files),
                     previous["version_id"] if previous else None, json.dumps(changes), manifest))
    return versions(output, source)[0]
