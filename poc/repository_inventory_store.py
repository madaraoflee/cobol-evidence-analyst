"""Versioned local inventory records, independent of bounded model context."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


class InventoryStore:
    def __init__(self, database_path):
        self.path = Path(database_path).resolve().with_name("repository-inventory.sqlite")
        self._validate_path()
        with self.connect() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS inventory_sessions(session_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, snapshot_id TEXT NOT NULL, latest INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS inventory_versions(handle TEXT PRIMARY KEY,session_id TEXT NOT NULL,version INTEGER NOT NULL,run_id TEXT,state_json TEXT NOT NULL,counts_json TEXT NOT NULL,update_key TEXT UNIQUE);
            CREATE INDEX IF NOT EXISTS inventory_version_session ON inventory_versions(session_id,version);
            CREATE TABLE IF NOT EXISTS inventory_catalog(session_id TEXT NOT NULL,ordinal INTEGER NOT NULL,candidate_id TEXT NOT NULL,item_json TEXT NOT NULL,introduced INTEGER NOT NULL,PRIMARY KEY(session_id,candidate_id));
            CREATE INDEX IF NOT EXISTS inventory_catalog_order ON inventory_catalog(session_id,ordinal);
            CREATE TABLE IF NOT EXISTS inventory_findings(session_id TEXT NOT NULL,candidate_id TEXT NOT NULL,start_version INTEGER NOT NULL,end_version INTEGER,payload_json TEXT NOT NULL,PRIMARY KEY(session_id,candidate_id,start_version));
            CREATE INDEX IF NOT EXISTS inventory_finding_version ON inventory_findings(session_id,start_version,end_version);
            CREATE TABLE IF NOT EXISTS inventory_sources(session_id TEXT NOT NULL,evidence_id TEXT NOT NULL,payload_json TEXT NOT NULL,payload_sha256 TEXT NOT NULL,PRIMARY KEY(session_id,evidence_id));
            CREATE TABLE IF NOT EXISTS inventory_reviews(session_id TEXT NOT NULL,review_key TEXT NOT NULL,review_json TEXT NOT NULL,PRIMARY KEY(session_id,review_key));
            CREATE TABLE IF NOT EXISTS inventory_narrative_sources(session_id TEXT NOT NULL,narrative_key TEXT NOT NULL,start_version INTEGER NOT NULL,source_ids_json TEXT NOT NULL,PRIMARY KEY(session_id,narrative_key,start_version));
            CREATE TABLE IF NOT EXISTS inventory_navigation(session_id TEXT NOT NULL,entry_key TEXT NOT NULL,start_version INTEGER NOT NULL,payload_json TEXT NOT NULL,PRIMARY KEY(session_id,entry_key));
            CREATE TABLE IF NOT EXISTS inventory_narratives(session_id TEXT NOT NULL,narrative_key TEXT NOT NULL,start_version INTEGER NOT NULL,end_version INTEGER,text TEXT NOT NULL,PRIMARY KEY(session_id,narrative_key,start_version));
            ''')

    def _validate_path(self):
        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = Path(str(self.path) + suffix)
            if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
                raise ValueError("INVENTORY_STORE_PATH_INVALID")

    @contextmanager
    def connect(self):
        self._validate_path()
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _meta(state):
        omitted = {"items", "findings", "additional_findings", "narratives", "previous_narratives", "narrative_history",
            "finding_history", "semantic_reviews", "_store_database", "store_handle", "presentation_pending"}
        return {key: value for key, value in state.items() if key not in omitted}

    @staticmethod
    def _counts(state):
        rows = state["findings"].values()
        included = sum(row.get("kind") == "formula" and row.get("disposition") == "included" for row in rows)
        excluded = sum(row.get("kind") == "formula" and row.get("disposition") == "excluded" for row in state["findings"].values())
        pending = len(state["items"]) - state["reviewed_count"] + len(state.get("presentation_pending", []))
        return {"files": state["file_count"], "reviewed": state["reviewed_count"], "pending": pending,
            "included": included, "excluded": excluded, "candidates": len(state["items"]),
            "review_pending": state.get("review_pending", len(state["items"]))}

    def create(self, state):
        session, handle = secrets.token_hex(16), secrets.token_hex(16)
        with self.connect() as db:
            db.execute("INSERT INTO inventory_sessions VALUES(?,?,?,0)", (session,state["scope_key"],state["snapshot_id"]))
            db.executemany("INSERT INTO inventory_catalog VALUES(?,?,?,?,0)",
                ((session,n,row["candidate_id"],_json(row)) for n,row in enumerate(state["items"])))
            db.execute("INSERT INTO inventory_versions VALUES(?,?,0,NULL,?,?,NULL)",
                (handle,session,_json(self._meta(state)),_json(self._counts(state))))
        return handle

    @staticmethod
    def _version(db, handle):
        row = db.execute("SELECT v.*,s.scope_key,s.snapshot_id,s.latest FROM inventory_versions v "
            "JOIN inventory_sessions s USING(session_id) WHERE handle=?",(handle,)).fetchone()
        if row is None:
            raise ValueError("INVENTORY_NOT_FOUND")
        return row

    def load(self, handle, *, scope_key=None):
        with self.connect() as db:
            version = self._version(db,handle)
            if scope_key is not None and version["scope_key"] != scope_key:
                raise ValueError("INVENTORY_SCOPE_CHANGED")
            session, number = version["session_id"],version["version"]
            state = json.loads(version["state_json"])
            state.update(items=[json.loads(row[0]) for row in db.execute("SELECT item_json FROM inventory_catalog "
                "WHERE session_id=? AND introduced<=? ORDER BY ordinal",(session,number))],
                findings={row["candidate_id"]:json.loads(row["payload_json"]) for row in db.execute(
                    "SELECT candidate_id,payload_json FROM inventory_findings WHERE session_id=? AND start_version<=? "
                    "AND (end_version IS NULL OR end_version>?)",(session,number,number))},
                narratives={row["narrative_key"]:row["text"] for row in db.execute("SELECT narrative_key,text FROM inventory_narratives "
                    "WHERE session_id=? AND start_version<=? AND (end_version IS NULL OR end_version>?)",(session,number,number))},
                additional_findings={},semantic_reviews={},store_handle=handle,paged=True)
            if state.get("presentation_key"):
                state["presentation_pending"] = [key for key,row in state["findings"].items()
                    if row.get("kind")=="formula" and row.get("disposition") in {"included","excluded"}
                    and row.get("presentation_key")!=state["presentation_key"]]
            return state

    def commit(self, state, changed_ids, pages=(), *, run_id=None, update_key=None, navigation=None):
        """Append one bounded batch. Previously published handles remain stable."""
        changed_ids = list(dict.fromkeys(changed_ids))
        handle = state["store_handle"]
        with self.connect() as db:
            previous = self._version(db,handle)
            dedup = _hash([handle,update_key]) if update_key else None
            if dedup:
                found = db.execute("SELECT handle FROM inventory_versions WHERE update_key=?",(dedup,)).fetchone()
                if found:
                    return found[0]
            session, number = previous["session_id"],previous["latest"] + 1
            if previous["version"] != previous["latest"]:
                # An intentional branch from an old answer owns a separate
                # result history; it cannot overwrite the later answer.
                new_session = secrets.token_hex(16)
                db.execute("INSERT INTO inventory_sessions VALUES(?,?,?,?)",(new_session,previous["scope_key"],previous["snapshot_id"],0))
                db.execute("INSERT INTO inventory_catalog SELECT ?,ordinal,candidate_id,item_json,0 FROM inventory_catalog "
                    "WHERE session_id=? AND introduced<=?",(new_session,session,previous["version"]))
                db.execute("INSERT INTO inventory_findings SELECT ?,candidate_id,0,NULL,payload_json FROM inventory_findings "
                    "WHERE session_id=? AND start_version<=? AND (end_version IS NULL OR end_version>?)",
                    (new_session,session,previous["version"],previous["version"]))
                db.execute("INSERT INTO inventory_sources SELECT ?,evidence_id,payload_json,payload_sha256 FROM inventory_sources WHERE session_id=?",(new_session,session))
                db.execute("INSERT INTO inventory_narratives SELECT ?,narrative_key,0,NULL,text FROM inventory_narratives "
                    "WHERE session_id=? AND start_version<=? AND (end_version IS NULL OR end_version>?)",(new_session,session,previous["version"],previous["version"]))
                db.execute("INSERT INTO inventory_narrative_sources SELECT ?,b.narrative_key,0,b.source_ids_json FROM inventory_narrative_sources b JOIN inventory_narratives n ON n.session_id=b.session_id AND n.narrative_key=b.narrative_key AND n.start_version=b.start_version WHERE n.session_id=? AND n.start_version<=? AND (n.end_version IS NULL OR n.end_version>?)", (new_session,session,previous["version"],previous["version"]))
                db.execute("INSERT INTO inventory_navigation SELECT ?,entry_key,0,payload_json FROM inventory_navigation WHERE session_id=? AND start_version<=?", (new_session,session,previous["version"]))
                session,number = new_session,1
            all_items = {row["candidate_id"]:(ordinal,row) for ordinal,row in enumerate(state["items"])}
            for identifier in changed_ids:
                ordinal,item=all_items[identifier]
                db.execute("INSERT OR IGNORE INTO inventory_catalog VALUES(?,?,?,?,?)",(session,ordinal,identifier,_json(item),number))
                db.execute("UPDATE inventory_findings SET end_version=? WHERE session_id=? AND candidate_id=? AND end_version IS NULL",(number,session,identifier))
                db.execute("INSERT INTO inventory_findings VALUES(?,?,?,NULL,?)",(session,identifier,number,_json(state["findings"][identifier])))
            for page in pages:
                # Selection reasons and UI aliases are mutable projections, not
                # source identity. Bind only the immutable original span.
                from prior_source_memory import _binding
                original = {**_binding(page), "evidence_id": page["evidence_id"],
                    "source_text": page["source_text"], "span_truncated": bool(page.get("span_truncated"))}
                body=_json(original);digest=hashlib.sha256(body.encode()).hexdigest()
                old=db.execute("SELECT payload_sha256 FROM inventory_sources WHERE session_id=? AND evidence_id=?",(session,page["evidence_id"])).fetchone()
                if old and old[0]!=digest:
                    raise ValueError("INVENTORY_SOURCE_CONFLICT")
                db.execute("INSERT OR IGNORE INTO inventory_sources VALUES(?,?,?,?)",(session,page["evidence_id"],body,digest))
            current_narratives = state.get("narratives", {})
            for old in db.execute("SELECT narrative_key FROM inventory_narratives WHERE session_id=? AND end_version IS NULL", (session,)).fetchall():
                if old[0] not in current_narratives:
                    db.execute("UPDATE inventory_narratives SET end_version=? WHERE session_id=? AND narrative_key=? AND end_version IS NULL", (number, session, old[0]))
            for key,value in current_narratives.items():
                old=db.execute("SELECT text FROM inventory_narratives WHERE session_id=? AND narrative_key=? AND end_version IS NULL",(session,key)).fetchone()
                if old and old[0]==value:
                    continue
                db.execute("UPDATE inventory_narratives SET end_version=? WHERE session_id=? AND narrative_key=? AND end_version IS NULL",(number,session,key))
                db.execute("INSERT INTO inventory_narratives VALUES(?,?,?,NULL,?)",(session,key,number,value))
                db.execute("INSERT INTO inventory_narrative_sources VALUES(?,?,?,?)", (session,key,number,_json(list(dict.fromkeys(page["evidence_id"] for page in pages)))))
            if navigation:
                body = _json(navigation)
                db.execute("INSERT OR IGNORE INTO inventory_navigation VALUES(?,?,?,?)", (session,_hash(navigation),number,body))
            new_handle=secrets.token_hex(16)
            db.execute("INSERT INTO inventory_versions VALUES(?,?,?,?,?,?,?)",(new_handle,session,number,run_id,_json(self._meta(state)),_json(self._counts(state)),dedup))
            db.execute("UPDATE inventory_sessions SET latest=? WHERE session_id=?",(number,session))
            return new_handle

    def page(self, handle, *, cursor=0, page_size=50, audit=False):
        if type(cursor) is not int or cursor<0 or type(page_size) is not int or not 1<=page_size<=100:
            raise ValueError("INVENTORY_PAGE_INVALID")
        with self.connect() as db:
            version=self._version(db,handle);counts=json.loads(version["counts_json"])
            filter_sql="" if audit else " AND json_extract(f.payload_json,'$.kind')='formula' AND json_extract(f.payload_json,'$.disposition')='included'"
            rows=db.execute("SELECT c.ordinal,c.item_json,f.payload_json FROM inventory_catalog c LEFT JOIN inventory_findings f "
                "ON f.session_id=c.session_id AND f.candidate_id=c.candidate_id AND f.start_version<=? AND (f.end_version IS NULL OR f.end_version>?) "
                "WHERE c.session_id=? AND c.introduced<=?"+filter_sql+" ORDER BY c.ordinal LIMIT ? OFFSET ?",
                (version["version"],version["version"],version["session_id"],version["version"],page_size+1,cursor)).fetchall()
            selected=rows[:page_size]
            output=[{**json.loads(row["payload_json"] or row["item_json"]),"record_type":json.loads(row["item_json"])["kind"]} for row in selected]
            return {"handle":handle,"snapshot_id":version["snapshot_id"],"run_id":version["run_id"],"paged":True,
                "total":counts["candidates"] if audit else counts["included"],"counts":counts,"complete":counts["pending"]==0 and counts.get("review_pending",0)==0,
                "rows":output,"page_size":page_size,"next_cursor":cursor+len(selected) if len(rows)>page_size else None}

    def export(self, handle):
        parts,cursor=[],0
        while True:
            page=self.page(handle,cursor=cursor,page_size=100,audit=True)
            parts.extend(_json(row) for row in page["rows"])
            if page["next_cursor"] is None:
                with self.connect() as db:
                    version=self._version(db,handle)
                    for row in db.execute("SELECT payload_json FROM inventory_navigation WHERE session_id=? AND start_version<=? ORDER BY start_version,entry_key", (version["session_id"],version["version"])):
                        parts.append(_json({"record_type":"investigation_navigation", **json.loads(row[0])}))
                    for row in db.execute("SELECT narrative_key,start_version,end_version,text FROM inventory_narratives WHERE session_id=? AND start_version<=? ORDER BY start_version,narrative_key",(version["session_id"],version["version"])):
                        parts.append(_json({"record_type":"business_narrative","narrative_id":row["narrative_key"],
                            "version":row["start_version"],"superseded":row["end_version"] is not None and row["end_version"]<=version["version"],"text":row["text"]}))
                return {"handle":handle,"snapshot_id":page["snapshot_id"],"total":len(parts),"jsonl":"\n".join(parts)+("\n" if parts else "")}
            cursor=page["next_cursor"]

    def evidence(self, handle, candidate_id, index=0):
        if type(index) is not int or index<0:
            raise ValueError("INVENTORY_EVIDENCE_INVALID")
        with self.connect() as db:
            version=self._version(db,handle)
            row=db.execute("SELECT payload_json FROM inventory_findings WHERE session_id=? AND candidate_id=? AND start_version<=? "
                "AND (end_version IS NULL OR end_version>?)",(version["session_id"],candidate_id,version["version"],version["version"])).fetchone()
            record=json.loads(row[0]) if row else {};ids=record.get("evidence_ids",[])
            if index>=len(ids):raise ValueError("INVENTORY_EVIDENCE_NOT_FOUND")
            page=db.execute("SELECT payload_json,payload_sha256 FROM inventory_sources WHERE session_id=? AND evidence_id=?",(version["session_id"],ids[index])).fetchone()
            if page is None or hashlib.sha256(page[0].encode()).hexdigest()!=page[1]:raise ValueError("INVENTORY_EVIDENCE_INVALID")
            value=json.loads(page[0])
            return {"status":"READY","integrity":"VALID","spans":[{**value,"text":value["source_text"],"integrity":"VALID"}],"span_count":1,
                "run_id":version["run_id"],"snapshot_id":version["snapshot_id"]}

    def narrative_page(self, handle, *, cursor=0, page_size=8, narrative_id=None):
        with self.connect() as db:
            version=self._version(db,handle)
            rows=db.execute("SELECT n.narrative_key,n.text,b.source_ids_json FROM inventory_narratives n LEFT JOIN inventory_narrative_sources b ON b.session_id=n.session_id AND b.narrative_key=n.narrative_key AND b.start_version=n.start_version WHERE n.session_id=? AND n.start_version<=? AND (n.end_version IS NULL OR n.end_version>?) AND (? IS NULL OR n.narrative_key=?) ORDER BY n.narrative_key LIMIT ? OFFSET ?", (version["session_id"],version["version"],version["version"],narrative_id,narrative_id,page_size+1,cursor)).fetchall()
            chosen=rows[:page_size];output=[]
            for row in chosen:
                pages=[]
                for identifier in json.loads(row["source_ids_json"] or "[]"):
                    source=db.execute("SELECT payload_json,payload_sha256 FROM inventory_sources WHERE session_id=? AND evidence_id=?", (version["session_id"],identifier)).fetchone()
                    if source is None or hashlib.sha256(source[0].encode()).hexdigest()!=source[1]:
                        raise ValueError("INVENTORY_EVIDENCE_INVALID")
                    pages.append(json.loads(source[0]))
                output.append({"narrative_id":row["narrative_key"],"text":row["text"],"pages":pages})
            return {"rows":output,"next_cursor":cursor+len(chosen) if len(rows)>page_size else None}

    def review(self, handle, key, value=None):
        with self.connect() as db:
            version=self._version(db,handle)
            if value is not None:
                db.execute("INSERT OR REPLACE INTO inventory_reviews VALUES(?,?,?)",(version["session_id"],key,_json(value)))
            row=db.execute("SELECT review_json FROM inventory_reviews WHERE session_id=? AND review_key=?",(version["session_id"],key)).fetchone()
            return json.loads(row[0]) if row else None

    def search(self, handle, query, *, cursor=0, page_size=16):
        if not isinstance(query,str) or not 1<=len(query.strip())<=256 or type(cursor) is not int or cursor<0:
            raise ValueError("INVENTORY_QUERY_INVALID")
        page_size=min(16,max(1,int(page_size)))
        with self.connect() as db:
            version=self._version(db,handle)
            rows=db.execute("SELECT c.ordinal,f.payload_json FROM inventory_catalog c JOIN inventory_findings f "
                "ON f.session_id=c.session_id AND f.candidate_id=c.candidate_id "
                "WHERE c.session_id=? AND f.start_version<=? AND (f.end_version IS NULL OR f.end_version>?) "
                "AND c.ordinal>=? AND instr(lower(f.payload_json),lower(?))>0 ORDER BY c.ordinal LIMIT ?",
                (version["session_id"],version["version"],version["version"],cursor,query.strip(),page_size+1)).fetchall()
            chosen=rows[:page_size]
            return {"query":query,"rows":[json.loads(row["payload_json"]) for row in chosen],
                "next_cursor":chosen[-1]["ordinal"]+1 if len(rows)>page_size else None,"snapshot_id":version["snapshot_id"]}

    def has_source(self, handle, evidence_id):
        with self.connect() as db:
            version=self._version(db,handle)
            row=db.execute("SELECT payload_json,payload_sha256 FROM inventory_sources WHERE session_id=? AND evidence_id=?",(version["session_id"],evidence_id)).fetchone()
            return bool(row and hashlib.sha256(row[0].encode()).hexdigest()==row[1])
