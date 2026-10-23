"""SQLite investigation journal with explicit uncertain remote requests."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


class RunStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS investigation_runs (
                    run_id TEXT PRIMARY KEY, identity TEXT NOT NULL, state TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    metadata_json TEXT NOT NULL, checkpoint_json TEXT NOT NULL DEFAULT '{}');
                CREATE INDEX IF NOT EXISTS investigation_identity ON investigation_runs(identity,state);
                CREATE TABLE IF NOT EXISTS investigation_steps (
                    run_id TEXT NOT NULL, step_key TEXT NOT NULL, tool TEXT NOT NULL,
                    state TEXT NOT NULL, arguments_json TEXT NOT NULL, result_json TEXT,
                    started_at TEXT NOT NULL, completed_at TEXT,
                    PRIMARY KEY(run_id,step_key));
                CREATE TABLE IF NOT EXISTS investigation_requests (
                    run_id TEXT NOT NULL, request_id TEXT NOT NULL, state TEXT NOT NULL,
                    request_hash TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
                    response_json TEXT, PRIMARY KEY(run_id,request_id));
                CREATE TABLE IF NOT EXISTS derivation_cache (
                    layer TEXT NOT NULL, cache_key TEXT NOT NULL, value_json TEXT NOT NULL,
                    PRIMARY KEY(layer,cache_key));
                CREATE TABLE IF NOT EXISTS derivation_dependencies (
                    layer TEXT NOT NULL, cache_key TEXT NOT NULL, dependency TEXT NOT NULL,
                    PRIMARY KEY(layer,cache_key,dependency));
                CREATE INDEX IF NOT EXISTS derivation_dependency ON derivation_dependencies(dependency);
            """)

    def connect(self):
        # Closing is essential on Windows before an output workspace is moved.
        class Connection:
            def __enter__(inner):
                inner.db = sqlite3.connect(self.path, timeout=5)
                inner.db.row_factory = sqlite3.Row
                return inner.db
            def __exit__(inner, kind, value, traceback):
                try:
                    inner.db.commit() if kind is None else inner.db.rollback()
                finally:
                    inner.db.close()
        return Connection()

    def create(self, run_id, metadata):
        identity = fingerprint(metadata)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO investigation_runs(run_id,identity,state,created_at,updated_at,metadata_json) "
                       "VALUES(?,?,'running',?,?,?)", (run_id, identity, _now(), _now(), json.dumps(metadata)))
        return identity

    def checkpoint(self, run_id, state, *, status="running"):
        with self.connect() as db:
            db.execute("UPDATE investigation_runs SET checkpoint_json=?,state=?,updated_at=? WHERE run_id=?",
                       (json.dumps(state, ensure_ascii=False), status, _now(), run_id))

    def load(self, run_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM investigation_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("investigation_run_not_found")
            result = dict(row)
            result["metadata"] = json.loads(result.pop("metadata_json"))
            result["checkpoint"] = json.loads(result.pop("checkpoint_json"))
            result["uncertain_requests"] = [dict(item) for item in db.execute(
                "SELECT request_id,request_hash,started_at FROM investigation_requests WHERE run_id=? AND state='started'",
                (run_id,))]
            result["completed_steps"] = [dict(item) for item in db.execute(
                "SELECT * FROM investigation_steps WHERE run_id=? AND state='completed' ORDER BY completed_at,step_key", (run_id,))]
            result["requests"] = [dict(item) for item in db.execute(
                "SELECT * FROM investigation_requests WHERE run_id=? ORDER BY started_at,request_id", (run_id,))]
            return result

    def unfinished(self, question_id, model_fingerprint, analysis_identity=None):
        with self.connect() as db:
            rows = db.execute("SELECT run_id,metadata_json FROM investigation_runs WHERE state IN ('running','attention') "
                              "ORDER BY updated_at DESC")
            for row in rows:
                metadata = json.loads(row["metadata_json"])
                if (metadata.get("question_id") == question_id
                    and metadata.get("configuration", {}).get("model_fingerprint") == model_fingerprint
                    and (analysis_identity is None or metadata.get("analysis_identity") == analysis_identity)):
                    return self.load(row["run_id"])
        return None

    def uncertain(self, question_id, model_fingerprint, analysis_identities):
        """Find pending remote work even when a newer safe run has the same identity."""
        identities = set(analysis_identities)
        with self.connect() as db:
            rows = db.execute("SELECT run_id,metadata_json FROM investigation_runs r "
                              "WHERE state IN ('running','attention') AND EXISTS "
                              "(SELECT 1 FROM investigation_requests q WHERE q.run_id=r.run_id AND q.state='started') "
                              "ORDER BY updated_at DESC")
            for row in rows:
                metadata = json.loads(row["metadata_json"])
                if (metadata.get("question_id") == question_id
                    and metadata.get("configuration", {}).get("model_fingerprint") == model_fingerprint
                    and metadata.get("analysis_identity") in identities):
                    return self.load(row["run_id"])
        return None

    def start_step(self, run_id, tool, arguments):
        key = fingerprint({"tool": tool, "arguments": arguments})
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO investigation_steps VALUES(?,?,?,'started',?,NULL,?,NULL)",
                       (run_id, key, tool, json.dumps(arguments, sort_keys=True), _now()))
        return key

    def supersede_uncertain(self, question_id, model_fingerprint, analysis_identities, replacement_run_id):
        """Retire blocked recovery only after the user starts a replacement run.

        Request outcomes remain unknown in the original ledger. Superseding a
        local investigation neither cancels nor confirms the provider request.
        """
        identities = set(analysis_identities)
        retired = []
        with self.connect() as db:
            rows = db.execute("SELECT run_id,metadata_json,checkpoint_json FROM investigation_runs r "
                "WHERE state IN ('running','attention') AND run_id<>? AND EXISTS "
                "(SELECT 1 FROM investigation_requests q WHERE q.run_id=r.run_id AND q.state='started')",
                (replacement_run_id,)).fetchall()
            for row in rows:
                metadata = json.loads(row["metadata_json"])
                if (metadata.get("question_id") != question_id
                        or metadata.get("configuration", {}).get("model_fingerprint") != model_fingerprint
                        or metadata.get("analysis_identity") not in identities):
                    continue
                checkpoint = json.loads(row["checkpoint_json"])
                checkpoint.update(superseded_by_explicit_retry=True,
                                  replacement_run_id=replacement_run_id)
                db.execute("UPDATE investigation_runs SET state='superseded',checkpoint_json=?,updated_at=? "
                           "WHERE run_id=?", (json.dumps(checkpoint, ensure_ascii=False), _now(), row["run_id"]))
                retired.append(row["run_id"])
        return retired

    def finish_step(self, run_id, step_key, result):
        with self.connect() as db:
            db.execute("UPDATE investigation_steps SET state='completed',result_json=?,completed_at=? "
                       "WHERE run_id=? AND step_key=?", (json.dumps(result, ensure_ascii=False), _now(), run_id, step_key))

    def start_request(self, run_id, request_id, request_hash):
        with self.connect() as db:
            db.execute("INSERT INTO investigation_requests VALUES(?,?,'started',?,?,NULL,NULL)",
                       (run_id, request_id, request_hash, _now()))

    def finish_request(self, run_id, request_id, response):
        with self.connect() as db:
            db.execute("UPDATE investigation_requests SET state='completed',response_json=?,completed_at=? "
                       "WHERE run_id=? AND request_id=? AND state='started'",
                       (json.dumps(response, ensure_ascii=False), _now(), run_id, request_id))

    def cached(self, layer, key):
        with self.connect() as db:
            row = db.execute("SELECT value_json FROM derivation_cache WHERE layer=? AND cache_key=?", (layer, key)).fetchone()
            return json.loads(row[0]) if row else None

    def cache(self, layer, key, value, dependencies=()):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO derivation_cache VALUES(?,?,?)", (layer, key, json.dumps(value)))
            db.execute("DELETE FROM derivation_dependencies WHERE layer=? AND cache_key=?", (layer, key))
            db.executemany("INSERT OR IGNORE INTO derivation_dependencies VALUES(?,?,?)",
                           ((layer, key, item) for item in dependencies))

    def invalidate(self, dependency):
        """Invalidate dependents transitively, including summaries of callers."""
        removed, frontier, seen = [], [dependency], set()
        with self.connect() as db:
            while frontier:
                changed = frontier.pop()
                if changed in seen:
                    continue
                seen.add(changed)
                rows = db.execute("SELECT layer,cache_key FROM derivation_dependencies WHERE dependency=?", (changed,)).fetchall()
                for layer, key in rows:
                    db.execute("DELETE FROM derivation_cache WHERE layer=? AND cache_key=?", (layer, key))
                    db.execute("DELETE FROM derivation_dependencies WHERE layer=? AND cache_key=?", (layer, key))
                    removed.append((layer, key))
                    frontier.append(layer + ":" + key)
        return removed
