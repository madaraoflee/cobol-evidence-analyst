"""Private, versioned storage for large online snapshot investigations.

Working records are partitioned independently of the small model checkpoint.
Published handles retain a fixed record generation and never follow later edits.
"""
from __future__ import annotations
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import json
import re
import secrets
import sqlite3

from online_material_format import MaterialError, canonical_json, decode_json, digest

_ID = re.compile(r"[0-9a-f]{32}\Z")
_MAX_RECORD = 4 * 1024 * 1024


class OnlineInventoryStore:
    def __init__(self, root):
        self.root = Path(root)
        if self.root.is_symlink():
            raise MaterialError("ONLINE_STATE_INVALID")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "online-inventories.sqlite"
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, owner BLOB NOT NULL, meta BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS versions(seq INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL,
                    kind TEXT NOT NULL, key TEXT NOT NULL, path TEXT NOT NULL, data BLOB NOT NULL);
                CREATE INDEX IF NOT EXISTS version_lookup ON versions(session,kind,key,seq);
                CREATE TABLE IF NOT EXISTS records(session TEXT NOT NULL,kind TEXT NOT NULL,key TEXT NOT NULL,
                    path TEXT NOT NULL,seq INTEGER NOT NULL,data BLOB NOT NULL,PRIMARY KEY(session,kind,key));
                CREATE INDEX IF NOT EXISTS record_path ON records(session,kind,path);
                CREATE TABLE IF NOT EXISTS tasks(session TEXT NOT NULL,group_key TEXT NOT NULL,ordinal INTEGER NOT NULL,
                    data BLOB NOT NULL,PRIMARY KEY(session,group_key,ordinal));
                CREATE TABLE IF NOT EXISTS publications(id TEXT PRIMARY KEY,session TEXT NOT NULL,generation INTEGER NOT NULL,data BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS actions(session TEXT NOT NULL,key TEXT NOT NULL,PRIMARY KEY(session,key));
            """)

    @contextmanager
    def connection(self):
        if any(Path(str(self.path) + suffix).is_symlink() for suffix in ("", "-wal", "-shm", "-journal")):
            raise MaterialError("ONLINE_STATE_INVALID")
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=30)
            self.path.chmod(0o600)
            db.execute("PRAGMA synchronous=FULL")
            yield db
            db.commit()
        except sqlite3.Error:
            if db is not None:
                db.rollback()
            raise MaterialError("ONLINE_INVENTORY_STORAGE_UNAVAILABLE") from None
        except BaseException:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def session(self, *, workspace_id, conversation_id, user_message_id, manifest, question, identifier=None, guard=None):
        owner = {"workspace_id": workspace_id, "conversation_id": conversation_id, "user_message_id": user_message_id,
                 "repository": manifest["repository"], "revision": manifest["revision"], "question_hash": digest(question.encode("utf-8"))}
        with self.connection() as db:
            if identifier is None:
                identifier = secrets.token_hex(16)
                db.execute("INSERT INTO sessions VALUES (?,?,?)", (identifier, canonical_json(owner), canonical_json({})))
            elif not isinstance(identifier, str) or not _ID.fullmatch(identifier):
                raise MaterialError("ONLINE_INVALID_INVENTORY_SESSION")
            row = db.execute("SELECT owner FROM sessions WHERE id=?", (identifier,)).fetchone()
            if row is None or bytes(row[0]) != canonical_json(owner):
                raise MaterialError("ONLINE_INVENTORY_NOT_FOUND")
        return InventorySession(self, identifier, owner, guard=guard)

    def publication(self, identifier, *, conversation_id, workspace_id, revision):
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise MaterialError("ONLINE_INVENTORY_NOT_FOUND")
        with self.connection() as db:
            row = db.execute("SELECT p.session,p.generation,p.data,s.owner FROM publications p JOIN sessions s ON s.id=p.session WHERE p.id=?", (identifier,)).fetchone()
            if row is None:
                raise MaterialError("ONLINE_INVENTORY_NOT_FOUND")
            owner = json.loads(row[3])
            if (owner["conversation_id"] != conversation_id or owner["workspace_id"] != workspace_id or owner["revision"] != revision):
                raise MaterialError("ONLINE_INVENTORY_NOT_FOUND")
        return InventoryPublication(self, identifier, row[0], row[1], json.loads(row[2]), owner)

    def delete_conversation(self, identifier):
        with self.connection() as db:
            sessions = [row[0] for row in db.execute("SELECT id,owner FROM sessions") if json.loads(row[1])["conversation_id"] == identifier]
            for session in sessions:
                for table in ("versions", "records", "tasks", "actions", "publications"):
                    db.execute("DELETE FROM " + table + " WHERE session=?", (session,))
                db.execute("DELETE FROM sessions WHERE id=?", (session,))


class InventorySession:
    def __init__(self, store, identifier, owner, guard=None):
        self.store, self.id, self.owner = store, identifier, owner
        self.guard = guard or (lambda value: value)

    def _put(self, db, kind, key, value, path=""):
        data = canonical_json(self.guard(value))
        if len(data) > _MAX_RECORD:
            raise MaterialError("ONLINE_INVENTORY_RECORD_CAPACITY_LIMIT")
        old = db.execute("SELECT data FROM records WHERE session=? AND kind=? AND key=?", (self.id, kind, key)).fetchone()
        if old is not None and bytes(old[0]) == data:
            return
        cursor = db.execute("INSERT INTO versions(session,kind,key,path,data) VALUES (?,?,?,?,?)", (self.id, kind, key, path, data))
        db.execute("INSERT INTO records VALUES (?,?,?,?,?,?) ON CONFLICT(session,kind,key) DO UPDATE SET path=excluded.path,seq=excluded.seq,data=excluded.data",
                   (self.id, kind, key, path, cursor.lastrowid, data))

    def get(self, kind, key):
        with self.store.connection() as db:
            row = db.execute("SELECT data FROM records WHERE session=? AND kind=? AND key=?", (self.id, kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def rows(self, kind, *, path=None, offset=0, limit=40):
        sql, args = "SELECT key,data FROM records WHERE session=? AND kind=?", [self.id, kind]
        if path is not None:
            sql += " AND path=?"
            args.append(path)
        sql += " ORDER BY key LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self.store.connection() as db:
            return [(key, json.loads(data)) for key, data in db.execute(sql, args)]

    def count(self, kind):
        with self.store.connection() as db:
            return db.execute("SELECT count(*) FROM records WHERE session=? AND kind=?", (self.id, kind)).fetchone()[0]

    def verify_sources(self, query, sources, state, verified):
        from online_investigation import _tick
        meta = self.meta()
        verification = meta.get("verification")
        if not verification or not verification.get("active"):
            verification = {"active": True, "after": "", "revision": self.owner["revision"]}
            meta["verification"] = verification
            with self.store.connection() as db:
                self._save_meta(db, meta)
        while True:
            with self.store.connection() as db:
                row = db.execute("SELECT key,data FROM records WHERE session=? AND kind='files' AND key>? ORDER BY key LIMIT 1", (self.id, verification["after"])).fetchone()
            if row is None:
                verification["active"] = False
                with self.store.connection() as db:
                    self._save_meta(db, meta)
                return
            path, raw = row
            entry = json.loads(raw)
            _tick(sources.cancel)
            value = query.evidence(path, 1, 1, sources, max_characters=1000)
            if value["citation"]["hash"] != entry["sha256"]:
                raise MaterialError("ONLINE_EVIDENCE_CHANGED")
            verification["after"] = path
            with self.store.connection() as db:
                self._save_meta(db, meta)

    def source_reviews(self, *, offset=0, limit=40, path=None):
        sql = " FROM records r,json_each(r.data,'$.reviews') j WHERE r.session=? AND r.kind='files'"
        args = [self.id]
        if path is not None:
            sql += " AND r.path=?"
            args.append(path)
        with self.store.connection() as db:
            total = db.execute("SELECT count(*)" + sql, args).fetchone()[0]
            rows = [{"path": name, **json.loads(value)} for name, value in db.execute(
                "SELECT r.path,j.value" + sql + " ORDER BY r.seq DESC,j.key LIMIT ? OFFSET ?", [*args, limit, offset])]
        return rows, total

    def meta(self):
        with self.store.connection() as db:
            return json.loads(db.execute("SELECT meta FROM sessions WHERE id=?", (self.id,)).fetchone()[0])

    def _save_meta(self, db, meta):
        db.execute("UPDATE sessions SET meta=? WHERE id=?", (canonical_json(self.guard(meta)), self.id))

    def attach(self, state):
        work = state["work_ledger"]
        meta = self.meta()
        for key, value in meta.get("work", {}).items():
            work[key] = deepcopy(value)
        work["_session"] = self
        self.prepare(state, {})

    def prepare(self, state, action):
        work = state["work_ledger"]
        for kind in ("files", "candidates", "findings", "queries"):
            work[kind] = {}
        work["presentation_pending"] = []
        path = action.get("path")
        if action.get("candidate_id"):
            candidate = self.get("candidates", action["candidate_id"])
            path = candidate["path"] if candidate else None
        if path is None:
            pending = self.pending(limit=1)
            path = pending[0].get("path") if pending else None
        if path:
            entry = self.get("files", path)
            if entry:
                work["files"][path] = entry
                for kind in ("candidates", "findings"):
                    # Each file is a storage shard; page the records while loading
                    # to avoid a query silently hiding entries after a fixed limit.
                    offset = 0
                    while True:
                        rows = self.rows(kind, path=path, offset=offset, limit=100)
                        work[kind].update(rows)
                        offset += len(rows)
                        if len(rows) < 100:
                            break
                work["presentation_pending"] = [key for key, value in self.rows("rewrite", path=path, limit=1000000) if value["pending"]]
        if action.get("tool") in {"search", "relations", "incoming"}:
            base = {key: value for key, value in action.items() if key not in {"offset", "limit"}}
            key = digest(canonical_json(base))
            value = self.get("queries", key)
            if value:
                work["queries"][key] = value

    def capture(self, state, *, action_key=None):
        from online_investigation import _new_work, _work_pending
        work = state["work_ledger"]
        with self.store.connection() as db:
            meta = json.loads(db.execute("SELECT meta FROM sessions WHERE id=?", (self.id,)).fetchone()[0])
            meta["work"] = {key: deepcopy(value) for key, value in work.items()
                            if key not in {"files", "candidates", "findings", "queries", "presentation_pending", "_session"}}
            for kind in ("files", "candidates", "findings", "queries"):
                for key, value in work[kind].items():
                    self._put(db, kind, key, value, key if kind == "files" else value.get("path", ""))
            for key in work["findings"]:
                if key not in work["presentation_pending"]:
                    self._put(db, "rewrite", key, {"pending": False}, work["findings"][key]["path"])
            for page in state["evidence"] + state["archived_evidence"]:
                self._put(db, "evidence", page["id"], self.archive(page), page["citation"]["path"])
            for path, entry in work["files"].items():
                mini = _new_work(state["question"])
                mini["inventory_complete"] = True
                mini["files"] = {path: entry}
                for record_kind in ("candidates", "findings"):
                    mini[record_kind] = {key: json.loads(value) for key, value in db.execute(
                        "SELECT key,data FROM records WHERE session=? AND kind=? AND path=?", (self.id, record_kind, path))}
                mini["presentation_pending"] = [row[0] for row in db.execute(
                    "SELECT key FROM records WHERE session=? AND kind='rewrite' AND path=? AND json_extract(data,'$.pending')=1", (self.id, path))]
                tasks = _work_pending(mini)
                self._tasks(db, "file:" + path, tasks)
            for key, value in work["queries"].items():
                mini = _new_work(state["question"])
                mini["inventory_complete"] = True
                mini["queries"] = {key: value}
                self._tasks(db, "query:" + key, _work_pending(mini))
            if action_key:
                db.execute("INSERT OR IGNORE INTO actions VALUES (?,?)", (self.id, action_key))
            self._save_meta(db, meta)
        # Every raw page is durable. Keep only a bounded current source window;
        # omitted pages remain discoverable and can be recalled by pinned ID.
        protected = set(state["request_evidence"] + state["answer_citations"])
        retained = state["evidence"][-16:]
        total = sum(len(canonical_json(page)) for page in retained)
        while len(retained) > 1 and total > 800000:
            total -= len(canonical_json(retained.pop(0)))
        state["evidence"] = retained
        active = {page["id"] for page in retained}
        state["focus_evidence"] = [key for key in state["focus_evidence"] if key in active]
        state["archived_evidence"] = [self.archive(self.get("evidence", key)) for key in protected - active]
        state["completed_actions"] = state["completed_actions"][-32:]

    @staticmethod
    def archive(page):
        if page is None:
            raise MaterialError("ONLINE_EVIDENCE_NOT_FOUND")
        value = deepcopy(page)
        if "text" in value:
            value["text_sha256"] = digest(value.pop("text").encode("utf-8"))
        return value

    def _tasks(self, db, group, tasks):
        db.execute("DELETE FROM tasks WHERE session=? AND group_key=?", (self.id, group))
        db.executemany("INSERT INTO tasks VALUES (?,?,?,?)", [(self.id, group, i, canonical_json(task)) for i, task in enumerate(tasks)])

    def pending(self, *, offset=0, limit=40):
        with self.store.connection() as db:
            rows = [json.loads(row[0]) for row in db.execute("SELECT data FROM tasks WHERE session=? ORDER BY group_key,ordinal LIMIT ? OFFSET ?", (self.id, limit, offset))]
            total = db.execute("SELECT count(*) FROM tasks WHERE session=?", (self.id,)).fetchone()[0]
        meta = self.meta().get("work", {})
        if not meta.get("inventory_complete") and offset <= total < offset + limit:
            rows.append({"kind": "inventory_page", "action": {"tool": "inventory", "limit": 20, "cursor": meta.get("inventory_cursor")}})
        return rows

    def summary(self):
        with self.store.connection() as db:
            counts = dict(db.execute("SELECT kind,count(*) FROM records WHERE session=? GROUP BY kind", (self.id,)))
            pending = db.execute("SELECT count(*) FROM tasks WHERE session=?", (self.id,)).fetchone()[0]
            occurrences = db.execute("SELECT coalesce(sum(json_extract(data,'$.occurrence_count')),0) FROM records WHERE session=? AND kind='candidates'", (self.id,)).fetchone()[0]
            inventoried = db.execute("SELECT count(*) FROM records WHERE session=? AND kind='files' AND json_extract(data,'$.inventoried')=1", (self.id,)).fetchone()[0]
            unfinished_files = db.execute("SELECT count(DISTINCT group_key) FROM tasks WHERE session=? AND group_key LIKE 'file:%'", (self.id,)).fetchone()[0]
        work = self.meta().get("work", {})
        count = counts.get("files", 0)
        complete = bool(work.get("inventory_complete") and work.get("expected_files") == inventoried)
        return {"scope": "indexed_snapshot", "active": True, "inventory_complete": complete,
                "files_discovered": count, "files_reviewed": count - unfinished_files,
                "candidates_total": occurrences, "candidates_reviewed": counts.get("findings", 0),
                "pending_count": pending + (not complete), "legacy_checkpoint_recheck": work.get("legacy_checkpoint_recheck", False),
                "complete": bool(complete and not pending), "full_repository_or_runtime_proof": False}

    def action_done(self, key):
        with self.store.connection() as db:
            return db.execute("SELECT 1 FROM actions WHERE session=? AND key=?", (self.id, key)).fetchone() is not None

    def revise(self):
        with self.store.connection() as db:
            for key, path, data in db.execute("SELECT key,path,data FROM records WHERE session=? AND kind='findings'", (self.id,)):
                finding = json.loads(data)
                self._put(db, "rewrite", key, {"pending": True}, path)
                candidate = json.loads(db.execute("SELECT data FROM records WHERE session=? AND kind='candidates' AND key=?",
                    (self.id, finding["candidate_id"])).fetchone()[0])
                task = {"kind": "candidate_review", **candidate,
                        "missing_occurrences": [finding["occurrence"]], "revision_required": True}
                # Distinct rewrite group avoids replacing original source debt.
                self._tasks(db, "rewrite:" + key, [task])

    def clear_rewrites(self, state):
        with self.store.connection() as db:
            for key in state["work_ledger"]["findings"]:
                if key not in state["work_ledger"]["presentation_pending"]:
                    db.execute("DELETE FROM tasks WHERE session=? AND group_key=?", (self.id, "rewrite:" + key))

    def publish(self, state, answer, complete):
        self.capture(state)
        summary = self.summary()
        with self.store.connection() as db:
            generation = db.execute("SELECT coalesce(max(seq),0) FROM versions WHERE session=?", (self.id,)).fetchone()[0]
            identifier = secrets.token_hex(16)
            data = {"scope": "indexed_snapshot", "repository": self.owner["repository"], "revision": self.owner["revision"],
                    "counts": summary, "complete": bool(complete), "answer": answer, "question": state["question"],
                    "answer_requirements": deepcopy(state["answer_requirements"]),
                    "answer_citations": [{"id": key, **self.get("evidence", key)["citation"]}
                                         for key in state["answer_citations"]]}
            db.execute("INSERT INTO publications VALUES (?,?,?,?)", (identifier, self.id, generation, canonical_json(self.guard(data))))
        return InventoryPublication(self.store, identifier, self.id, generation, data, self.owner)


class InventoryPublication:
    def __init__(self, store, identifier, session, generation, data, owner):
        self.store, self.id, self.session, self.generation, self.data, self.owner = store, identifier, session, generation, data, owner

    def _rows(self, kind):
        with self.store.connection() as db:
            rows = db.execute("SELECT v.key,v.data FROM versions v JOIN (SELECT key,max(seq) seq FROM versions WHERE session=? AND kind=? AND seq<=? GROUP BY key) selected ON selected.seq=v.seq ORDER BY v.key",
                              (self.session, kind, self.generation)).fetchall()
        return [(key, json.loads(data)) for key, data in rows]

    def evidence(self, identifier):
        with self.store.connection() as db:
            row = db.execute("SELECT data FROM versions WHERE session=? AND kind='evidence' AND key=? AND seq<=? ORDER BY seq DESC LIMIT 1", (self.session, identifier, self.generation)).fetchone()
        if row is None:
            raise MaterialError("ONLINE_CITATION_NOT_FOUND")
        return json.loads(row[0])

    def _finding(self, value):
        value = deepcopy(value)
        value["citation_refs"] = [{"id": key, **self.evidence(key)["citation"]} for key in value["citations"]]
        value["source_sha256"] = next((item["hash"] for item in value["citation_refs"] if item["path"] == value["path"]), None)
        return value

    def page(self, offset=0, limit=40, *, preview=True):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise MaterialError("ONLINE_INVALID_INVENTORY_PAGE")
        # The immutable query selects a fixed generation; later session records
        # cannot change an old result's page contents or count.
        selection = "FROM versions v JOIN (SELECT key,max(seq) seq FROM versions WHERE session=? AND kind='findings' AND seq<=? GROUP BY key) selected ON selected.seq=v.seq WHERE json_extract(v.data,'$.disposition')='included'"
        selection += " AND NOT EXISTS (SELECT 1 FROM versions r WHERE r.session=v.session AND r.kind='rewrite' AND r.key=v.key AND r.seq=(SELECT max(r2.seq) FROM versions r2 WHERE r2.session=r.session AND r2.kind='rewrite' AND r2.key=r.key AND r2.seq<=" + str(self.generation) + ") AND json_extract(r.data,'$.pending')=1)"
        with self.store.connection() as db:
            total = db.execute("SELECT count(*) " + selection, (self.session, self.generation)).fetchone()[0]
            selected = [(key, json.loads(value)) for key, value in db.execute("SELECT v.key,v.data " + selection + " ORDER BY v.path,v.key LIMIT ? OFFSET ?", (self.session, self.generation, limit, offset))] if preview else []
        return {"id": self.id, "scope": self.data["scope"], "repository": deepcopy(self.data["repository"]),
                "revision": self.data["revision"], "total": total, "counts": deepcopy(self.data["counts"]),
                "complete": self.data["complete"], "offset": offset,
                "findings": [self._finding(value) for _, value in selected],
                "next_offset": offset + len(selected) if offset + len(selected) < total else None,
                **({"answer": self.data["answer"], "answer_citations": deepcopy(self.data.get("answer_citations", [])),
                    "answer_preview": False} if preview and offset == 0 else {})}

    def export_data(self):
        presentation_pending = {key for key, value in self._rows("rewrite") if value["pending"]}
        return {"schema_version": "online-inventory-export/v1", "id": self.id, **deepcopy(self.data),
                "findings": [{**self._finding(value), "presentation_current": key not in presentation_pending}
                             for key, value in self._rows("findings")],
                "presentation_pending": sorted(presentation_pending),
                "source_reviews": [{"path": path, **review} for path, value in self._rows("files") for review in value["reviews"]],
                "evidence": [InventorySession.archive(value) for _, value in self._rows("evidence")]}

    def export(self):
        return {"filename": "online-inventory-" + self.id + ".json", "mime_type": "application/json",
                "content": json.dumps(self.export_data(), ensure_ascii=False, indent=2)}
