"""One question's immutable selected-file captures and transactional sparse refresh."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import codecs
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

from source_reading import _safe_file, _identify_page, _persist_page, _verified_pages
from repo_inventory import _looks_binary


@dataclass(frozen=True)
class CapturedSource:
    relative_path: str
    sha256: str
    encoding: str
    source_format: str
    captured_at_utc: str
    path: Path
    size: int
    mtime_ns: int


class QuestionSourceSession:
    def __init__(self, database_path, source_root, *, check_cancel=None):
        self.database_path = Path(database_path).resolve()
        self.source_root = Path(source_root).expanduser().resolve(strict=True)
        self.check_cancel = check_cancel
        self._temporary = tempfile.TemporaryDirectory(prefix="question-source-")
        self.mirror_root = Path(self._temporary.name).resolve()
        self._captured = {}
        self._scopes = []
        self.capture_seconds = 0.0
        self.captured_bytes = 0
        with closing(sqlite3.connect(self.database_path)) as db:
            self.options = json.loads(db.execute("SELECT value FROM metadata WHERE key='source_options'").fetchone()[0])

    def close(self):
        try:
            for scope in reversed(self._scopes):
                scope.close()
        finally:
            self._temporary.cleanup()

    def register_scope(self, scope):
        self._scopes.append(scope)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def capture(self, relative_path):
        if relative_path in self._captured:
            return self._captured[relative_path]
        started = time.monotonic()
        source = _safe_file(self.source_root, relative_path)
        destination = self.mirror_root / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        before = source.stat()
        digest = hashlib.sha256()
        configured = self.options.get("encoding", "auto")
        valid = {}
        decoders = []
        with source.open("rb") as input_file, destination.open("wb") as output_file:
            opened = os.fstat(input_file.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("SOURCE_CHANGED_DURING_READ")
            while True:
                if self.check_cancel:
                    self.check_cancel()
                chunk = input_file.read(65536)
                if not chunk:
                    break
                if not decoders:
                    if configured == "auto":
                        if _looks_binary(chunk):
                            raise ValueError("SOURCE_ENCODING_INVALID")
                        bom = ((b"\xff\xfe\x00\x00", "utf-32"),
                               (b"\x00\x00\xfe\xff", "utf-32"),
                               (b"\xef\xbb\xbf", "utf-8-sig"),
                               (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16"))
                        selected = next((name for prefix, name in bom if chunk.startswith(prefix)), None)
                        decoders = [selected] if selected else ["utf-8", "cp950", "big5", "cp1252", "latin-1"]
                    else:
                        decoders = [configured]
                    valid = {name: codecs.getincrementaldecoder(name)(errors="strict") for name in decoders}
                output_file.write(chunk)
                digest.update(chunk)
                for name in list(valid):
                    try:
                        valid[name].decode(chunk)
                    except UnicodeError:
                        del valid[name]
            for name in list(valid):
                try:
                    valid[name].decode(b"", final=True)
                except UnicodeError:
                    del valid[name]
            if not decoders:
                decoders = ["utf-8" if configured == "auto" else configured]
                valid = {decoders[0]: codecs.getincrementaldecoder(decoders[0])(errors="strict")}
            final = os.fstat(input_file.fileno())
        after = _safe_file(self.source_root, relative_path).stat()
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        if identity(before) != identity(final) or identity(before) != identity(after):
            raise ValueError("SOURCE_CHANGED_DURING_READ")
        if not valid:
            raise ValueError("SOURCE_ENCODING_INVALID")
        encoding = next(name for name in decoders if name in valid)
        captured = CapturedSource(relative_path, digest.hexdigest(), encoding,
            self.options.get("source_format", "auto"), datetime.now(timezone.utc).isoformat(),
            destination, before.st_size, before.st_mtime_ns)
        self._captured[relative_path] = captured
        self.capture_seconds += time.monotonic() - started
        self.captured_bytes += captured.size
        return captured

    def source_manifest(self):
        return [{"relative_path": c.relative_path, "sha256": c.sha256,
                 "encoding": c.encoding, "source_format": c.source_format,
                 "captured_at_utc": c.captured_at_utc} for c in self._captured.values()]


def _archive_path(database_path):
    return Path(database_path).resolve().parent / "versioned-evidence.sqlite"


def archive_evidence(database_path, pages):
    path = _archive_path(database_path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS excerpts (evidence_id TEXT PRIMARY KEY, relative_path TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, source_sha256 TEXT NOT NULL, text TEXT NOT NULL, include_chain_json TEXT NOT NULL DEFAULT '[]')")
        if "include_chain_json" not in {row[1] for row in db.execute("PRAGMA table_info(excerpts)")}:
            db.execute("ALTER TABLE excerpts ADD COLUMN include_chain_json TEXT NOT NULL DEFAULT '[]'")
        for page in pages:
            values = (page["evidence_id"], page["relative_path"], page["start_line"],
                      page["end_line"], page["source_sha256"], page["source_text"],
                      json.dumps(page.get("include_chain", []), ensure_ascii=False))
            if _identify_page(page) == page["evidence_id"]:
                db.execute("INSERT OR IGNORE INTO excerpts VALUES (?,?,?,?,?,?,?)", values)


def read_archived_evidence(database_path, evidence_id):
    path = _archive_path(database_path)
    if not path.is_file() or path.is_symlink():
        return None
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM excerpts WHERE evidence_id=?", (evidence_id,)).fetchone()
    if row is None:
        return None
    page = dict(row)
    page["source_text"] = page.pop("text")
    page["include_chain"] = json.loads(page.pop("include_chain_json"))
    if _identify_page(page) != evidence_id:
        raise ValueError("EVIDENCE_ID_CONFLICT")
    return page


def refresh_selected_sources(database_path, captures, *, expected_snapshot_id):
    """Replace only captured changed files and their search facts in one transaction."""
    from business_index import _Facts, _delete_business_rules, _physical_lines, _resolve_copies
    from repository_discovery import PAGE_CHARS, SEARCH_VERSION, _remove_file, _tokens
    from structural_index import _delete_file_facts, _resolve_relations

    captures = list(captures)
    if not captures:
        return {"updated": [], "snapshot_id": expected_snapshot_id}
    database = Path(database_path).resolve()
    with closing(sqlite3.connect(database)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE")
        try:
            metadata = dict(db.execute("SELECT key,value FROM metadata"))
            if metadata.get("snapshot_id") != expected_snapshot_id:
                raise ValueError("SNAPSHOT_CHANGED")
            stats = json.loads(metadata.get("file_stats", "{}"))
            boundaries = json.loads(metadata.get("business_file_boundaries", "{}"))
            changed = []
            changed_names = set()
            for capture in captures:
                old = db.execute("SELECT sha256 FROM source_files WHERE relative_path=?", (capture.relative_path,)).fetchone()
                if old is None:
                    raise ValueError("SOURCE_NOT_INDEXED")
                if old[0] == capture.sha256:
                    continue
                changed_names.update(row[0] for row in db.execute(
                    "SELECT name FROM symbols WHERE relative_path=?", (capture.relative_path,)))
                changed_names.update({capture.relative_path.upper(),
                                      Path(capture.relative_path).name.upper(),
                                      Path(capture.relative_path).stem.upper()})
                # Older accepted citations must remain expandable after fact replacement.
                prior = [dict(row) for row in db.execute("SELECT evidence_id,relative_path,start_line,end_line,source_sha256,text FROM evidence_spans WHERE relative_path=? AND evidence_id LIKE 'ev_page_%'", (capture.relative_path,))]
                archive_evidence(database, [{**row, "source_text": row["text"]} for row in prior])
                _remove_file(db, capture.relative_path)
                _delete_business_rules(db, capture.relative_path)
                _delete_file_facts(db, capture.relative_path)
                info = capture.path.stat()
                facts = _Facts(db, capture.relative_path,
                    {"sha256": capture.sha256, "encoding": capture.encoding, "stat": info,
                     "used_fallback_encoding": capture.encoding == "latin-1"}, capture.source_format,
                    lambda _: None)
                for number, line in _physical_lines(capture.path, capture.encoding, info):
                    facts.consume(number, line)
                facts.finish()
                changed_names.update(row[0] for row in db.execute(
                    "SELECT name FROM symbols WHERE relative_path=?", (capture.relative_path,)))
                artifact = "copybook" if facts.copybook else "program" if facts.program else "unknown"
                db.execute("INSERT INTO source_files VALUES (?,?,?,?,?,?,?,?)", (
                    capture.relative_path, capture.sha256, capture.encoding, int(capture.encoding == "latin-1"),
                    capture.source_format, artifact, facts.line_count, datetime.now(timezone.utc).isoformat()))
                count = truncated = 0
                # Captures share a mirror root and cannot race later live edits.
                for page in _verified_pages(capture.path.parents[len(Path(capture.relative_path).parts) - 1],
                        {"relative_path": capture.relative_path, "sha256": capture.sha256,
                         "encoding": capture.encoding, "line_count": facts.line_count}, None, set(), PAGE_CHARS):
                    page["evidence_id"] = _identify_page(page)
                    _persist_page(db, page)
                    cursor = db.execute("INSERT INTO repo_pages(evidence_id,relative_path,start_line,end_line,source_sha256,span_truncated,fallback_text) VALUES (?,?,?,?,?,?,?)",
                        (None if page["span_truncated"] else page["evidence_id"], capture.relative_path,
                         page["start_line"], page["end_line"], capture.sha256,
                         int(page["span_truncated"]), page["source_text"] if page["span_truncated"] else ""))
                    db.execute("INSERT INTO repo_fts(rowid,tokens) VALUES (?,?)",
                        (cursor.lastrowid, " ".join(_tokens(capture.relative_path + "\n" + page["source_text"]))))
                    count += 1
                    truncated += int(page["span_truncated"])
                db.execute("INSERT INTO repo_sources VALUES (?,?,?,?,?,?)", (
                    capture.relative_path, capture.sha256, capture.encoding, facts.line_count, count, truncated))
                db.execute("INSERT OR REPLACE INTO repo_source_state VALUES (?,?,?,?)", (
                    capture.relative_path, capture.sha256, capture.size, capture.mtime_ns))
                stats[capture.relative_path] = [capture.size, capture.mtime_ns]
                boundaries[capture.relative_path] = dict(facts.boundaries)
                changed.append(capture.relative_path)
            if changed:
                _resolve_relations(db, affected_paths=changed,
                                   affected_target_names=changed_names)
                _resolve_copies(db, affected_paths=changed,
                                affected_target_names=changed_names)
                digest = hashlib.sha256()
                for relative, sha in sorted(db.execute("SELECT relative_path,sha256 FROM source_files"), key=lambda row: row[0].casefold()):
                    digest.update(relative.encode() + b"\0" + sha.encode() + b"\n")
                snapshot = "sha256:" + digest.hexdigest()
                db.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)", (
                    ("snapshot_id", snapshot), ("file_stats", json.dumps(stats)),
                    ("business_file_boundaries", json.dumps(boundaries))))
                overview_row = db.execute("SELECT value FROM repo_metadata WHERE key='overview'").fetchone()
                overview = json.loads(overview_row[0]) if overview_row else {}
                overview.update(snapshot_id=snapshot, indexed_pages=db.execute("SELECT COUNT(*) FROM repo_pages").fetchone()[0],
                                total_lines=db.execute("SELECT SUM(line_count) FROM source_files").fetchone()[0])
                db.executemany("INSERT OR REPLACE INTO repo_metadata VALUES (?,?)", (
                    ("snapshot_id", snapshot), ("version", SEARCH_VERSION), ("overview", json.dumps(overview)), ("ready", "1")))
            else:
                snapshot = expected_snapshot_id
            db.commit()
            return {"updated": changed, "snapshot_id": snapshot}
        except BaseException:
            db.rollback()
            raise
