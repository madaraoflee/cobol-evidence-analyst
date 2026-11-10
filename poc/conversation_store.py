"""Local durable conversations; browser input never supplies trusted history."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3

from conversation_titles import TITLE_INPUT_HEAD, TITLE_INPUT_TAIL, fallback_title, summarize_question


MODEL_HISTORY_MESSAGES = 64
MODEL_HISTORY_CHARACTERS = 24_000
MAX_MODEL_HISTORY_CHARACTERS = 128_000
MODEL_HISTORY_DETAILS_BYTES = 64_000
MODEL_HISTORY_REFERENCE_MESSAGES = 8
MODEL_HISTORY_SUBJECT_BYTES = 8_192
_CONVERSATION_METADATA_COLUMNS = (
    "c.id,c.title,c.snapshot_id,c.created_at,c.updated_at,"
    "COALESCE((SELECT m.created_at FROM messages AS m "
    "WHERE m.conversation_id=c.id AND m.role='user' ORDER BY m.position LIMIT 1),"
    "c.created_at) AS first_question_at"
)

# An answer written by a retry can be physically newer than later turns.
# Associate it with the unique user row in the same run and conversation.
# Missing or ambiguous links leave legacy rows in their physical positions.
_MESSAGE_TURN_POSITION_SQL = (
    "CASE WHEN messages.role='assistant' AND messages.run_id IS NOT NULL AND messages.run_id<>'' THEN COALESCE("
    "(SELECT min(u.position) FROM messages AS u WHERE u.conversation_id=messages.conversation_id "
    "AND u.run_id=messages.run_id AND u.role='user' HAVING count(*)=1), messages.position) "
    "ELSE messages.position END"
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def projected_conversation_title(db, identifier, current_title, *, latest_question=None):
    """Summarize legacy automatic titles without writing during a read.

    Only bounded user-question excerpts are read. Custom titles, timestamps,
    messages and model diagnostic records are never changed by projection.
    """
    query = ("SELECT substr(content,1,?),CASE WHEN length(content)>? THEN substr(content,-?) ELSE '' END "
             "FROM messages WHERE conversation_id=? AND role='user' ORDER BY position LIMIT ?")
    parameters = (TITLE_INPUT_HEAD, TITLE_INPUT_HEAD, TITLE_INPUT_TAIL, identifier)
    first = db.execute(query, (*parameters, 1)).fetchone()
    if first is None:
        return current_title
    first_question = first[0] + ("\n" + first[1] if first[1] else "")
    fallback = fallback_title(first_question)
    automatic = {"新对话", "新的對話", "New conversation", first[0][:80], fallback}
    if current_title not in automatic:
        return current_title
    title = summarize_question(first_question)
    if title is not None:
        return title
    # A greeting or contextless first turn must not hide the first actual topic.
    # Later ordinary follow-ups never replace an established summary.
    for row in db.execute(query, (*parameters, 8)):
        question = row[0] + ("\n" + row[1] if row[1] else "")
        title = summarize_question(question)
        if title is not None:
            return title
    # A new substantive question can arrive after the bounded legacy scan.
    # Appends already have its text, so do not reread an unbounded transcript.
    return summarize_question(latest_question) or fallback


def _history_subject(db, position):
    """Project local subject memory without loading the surrounding diagnostics."""
    row = db.execute(
        "SELECT CASE WHEN json_valid(details) THEN CASE "
        "WHEN json_type(details,'$.investigation_state.session_subject')='object' "
        "AND json_extract(details,'$.investigation_state.session_subject.schema')='conversation-subject/v1' "
        "THEN CASE WHEN length(CAST(json_extract(details,'$.investigation_state.session_subject') AS BLOB))<=? "
        "THEN json_extract(details,'$.investigation_state.session_subject') END END END "
        "FROM messages WHERE position=?", (MODEL_HISTORY_SUBJECT_BYTES, position)).fetchone()
    return json.loads(row[0]) if row is not None and row[0] is not None else None


class ConversationHistory(list):
    """Bounded local context with the previous projection for journal identity checks."""

    def __init__(self, messages, legacy):
        super().__init__(messages)
        self.legacy = legacy if messages != legacy else None


def _history_json(db, position, expression, budget):
    """Read a selected JSON value only when its UTF-8 bytes fit the allowance."""
    if budget <= 0:
        return None, 0
    row = db.execute("WITH selected(value) AS (SELECT CASE WHEN json_valid(details) THEN "
        + expression + " END FROM messages WHERE position=?) "
        "SELECT CASE WHEN length(CAST(value AS BLOB))<=? THEN value END FROM selected",
        (position, budget)).fetchone()
    text = row[0] if row is not None else None
    return (json.loads(text), len(text.encode("utf-8"))) if isinstance(text, str) else (None, 0)


def _history_details(db, row, budget, *, legacy):
    if legacy:
        if row["details_bytes"] > budget:
            return {}, 0
        text = db.execute("SELECT details FROM messages WHERE position=?", (row["position"],)).fetchone()[0]
        return json.loads(text), row["details_bytes"]
    # Keep small reference lists even when the display-only investigation
    # contains many source observations. Extract only the fields used here.
    keys = ["evidence_refs", "cited_evidence_ids"]
    if row["status"] == "partial":
        keys.extend(("answer_origin", "model_answer_recorded", "answer_available"))
    projected = []
    for key in keys:
        value = f"json_extract(details,'$.{key}')"
        if key in {"model_answer_recorded", "answer_available"}:
            value = (f"json(CASE json_type(details,'$.{key}') WHEN 'true' THEN 'true' "
                     "WHEN 'false' THEN 'false' ELSE 'null' END)")
        projected.append(f"'{key}',{value}")
    projected = ",".join(projected)
    details, consumed = _history_json(db, row["position"], f"json_object({projected})", budget)
    state, size = _history_json(db, row["position"], "json_object("
        "'focus_candidates',json_extract(details,'$.investigation_state.focus_candidates'),"
        "'evidence_locations',json_extract(details,'$.investigation_state.evidence_locations'),"
        "'analysis_plan',COALESCE(json_extract(details,'$.investigation_state.analysis_plan'),"
        "json_extract(details,'$.investigation_state.question_investigation.analysis_plan')))", budget - consumed)
    details = details or {}
    navigation = {key: value for key, value in (state or {}).items() if isinstance(value, (dict, list))}
    if navigation:
        details["investigation_state"] = navigation
    return details, consumed + size


def _model_history_rows(db, identifier, max_messages, before_position, *, legacy):
    position = "position" if legacy else _MESSAGE_TURN_POSITION_SQL
    position_filter = f"AND ({position})<? " if before_position is not None else ""
    parameters = ((identifier, before_position, max_messages) if before_position is not None
                  else (identifier, max_messages))
    order = "position DESC" if legacy else "turn_position DESC, (role='assistant') DESC, position DESC"
    return db.execute(
        "SELECT position,role,status,length(content) AS content_length,"
        + ("length(CAST(details AS BLOB)) AS details_bytes " if legacy else f"{position} AS turn_position ")
        + "FROM messages WHERE conversation_id=? AND role IN ('user','assistant') AND (status='completed' OR "
        "(status IN ('partial','failed','cancelled','interrupted') AND EXISTS (SELECT 1 FROM messages a "
        "WHERE a.conversation_id=messages.conversation_id AND a.run_id=messages.run_id "
        "AND a.role='assistant' AND a.status='partial' AND CASE WHEN json_valid(a.details) THEN "
        "json_extract(a.details,'$.answer_available')=1 AND "
        "(json_extract(a.details,'$.answer_origin') IN ('model','local_source_facts') OR "
        "json_extract(a.details,'$.model_answer_recorded')=1 OR "
        "(json_extract(a.details,'$.answer_origin') IS NULL AND "
        "json_extract(a.details,'$.model_answer_recorded') IS NULL)) ELSE 0 END))) "
        + position_filter + f"ORDER BY {order} LIMIT ?", parameters).fetchall()


def _project_history(db, rows, max_characters, *, source_question=False, legacy=False,
                     include_analysis_handoff=False):
    remaining, details_remaining, messages = max_characters, MODEL_HISTORY_DETAILS_BYTES, []
    handoff_selected = False
    latest_user = next((row for row in rows if row["role"] == "user"), None)
    reserved_user = min(latest_user["content_length"], max(1, max_characters // 4)) if latest_user else 0
    for offset, row in enumerate(rows):
        keep_navigation = offset < MODEL_HISTORY_REFERENCE_MESSAGES and row["role"] == "assistant"
        navigation_only = source_question and row["role"] == "assistant"
        if navigation_only and not keep_navigation:
            continue
        if remaining <= 0 and not navigation_only:
            if source_question:
                continue
            break
        if latest_user is not None and row["position"] == latest_user["position"]:
            reserved_user = 0
        available = remaining - reserved_user
        if available <= 0 and not navigation_only:
            continue
        content = ("" if navigation_only else db.execute("SELECT substr(content,1,?) FROM messages WHERE position=?",
                   (available, row["position"])).fetchone()[0])
        message = {"role": row["role"], "content": content}
        if include_analysis_handoff and row["role"] == "assistant" and not handoff_selected:
            handoff_selected = True
            handle = db.execute("SELECT CASE WHEN json_valid(details) THEN "
                "CASE WHEN json_type(details,'$.investigation_run_id')='text' "
                "AND length(json_extract(details,'$.investigation_run_id'))=24 "
                "THEN json_extract(details,'$.investigation_run_id') END END "
                "FROM messages WHERE position=?", (row["position"],)).fetchone()[0]
            if isinstance(handle, str) and re.fullmatch(r"[0-9a-f]{24}", handle):
                message["investigation_run_id"] = handle
        if row["status"] == "partial":
            message["analysis_status"] = "PARTIAL"
        remaining -= len(content)
        if row["content_length"] > len(content):
            message["context_truncated"] = True
        if keep_navigation:
            details, consumed = _history_details(db, row, details_remaining, legacy=legacy)
            details_remaining -= consumed
            for key in ("evidence_refs", "cited_evidence_ids", "investigation_state"):
                if isinstance(details.get(key), (list, dict)):
                    message[key] = details[key]
            if row["status"] == "partial":
                for key in ("answer_origin", "model_answer_recorded", "answer_available"):
                    if isinstance(details.get(key), (str, bool)):
                        message[key] = details[key]
            state = message.get("investigation_state")
            if isinstance(state, dict):
                state.pop("session_subject", None)
            subject = _history_subject(db, row["position"])
            if subject is not None:
                state = message.setdefault("investigation_state", {})
                state["session_subject"] = subject
        messages.append(message)
    messages.reverse()
    return messages


class ConversationStore:
    def __init__(self, output, source):
        self.path = Path(output) / "conversations.sqlite"
        self.source_key = hashlib.sha256(str(Path(source).resolve()).encode()).hexdigest()
        if any(Path(str(self.path) + suffix).is_symlink() for suffix in ("", "-wal", "-shm")):
            raise ValueError("CONVERSATION_PATH_INVALID")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                  id TEXT PRIMARY KEY, source_key TEXT NOT NULL, title TEXT NOT NULL,
                  snapshot_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS messages (
                  position INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                  conversation_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
                  status TEXT NOT NULL, created_at TEXT NOT NULL, run_id TEXT, details TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS conversation_messages ON messages(conversation_id,position);
                CREATE INDEX IF NOT EXISTS conversation_run_status ON messages(conversation_id,run_id,role,status);
                CREATE TABLE IF NOT EXISTS answer_revisions (
                  position INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                  conversation_id TEXT NOT NULL, message_id TEXT NOT NULL,
                  saved_at TEXT NOT NULL, message_json TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS message_answer_revisions
                  ON answer_revisions(conversation_id,message_id,position);
            """)

    def create(self, snapshot_id=None):
        identifier, now = secrets.token_hex(16), _now()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO conversations VALUES (?,?,?,?,?,?)",
                       (identifier, self.source_key, "新对话", snapshot_id, now, now))
        return self.get(identifier)

    def get(self, identifier):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT " + _CONVERSATION_METADATA_COLUMNS + " FROM conversations AS c "
                "WHERE c.id=? AND c.source_key=?", (identifier, self.source_key)).fetchone()
            if row is None:
                raise ValueError("CONVERSATION_NOT_FOUND")
            result = dict(row)
            result["title"] = projected_conversation_title(db, identifier, result["title"])
            ordered_messages = []
            revisions = {}
            for item in db.execute("SELECT * FROM answer_revisions WHERE conversation_id=? ORDER BY position", (identifier,)):
                revisions.setdefault(item["message_id"], []).append({
                    **json.loads(item["message_json"]), "revision_id": item["id"], "saved_at": item["saved_at"]})
            for item in db.execute(
                f"SELECT *, {_MESSAGE_TURN_POSITION_SQL} AS turn_position FROM messages "
                "WHERE conversation_id=? ORDER BY position", (identifier,)):
                message = {key: item[key] for key in ("id", "role", "content", "status", "created_at", "run_id")}
                details = json.loads(item["details"])
                message.update(details)
                if item["id"] in revisions:
                    message["answer_revisions"] = revisions[item["id"]]
                ordered_messages.append((item["turn_position"], item["role"] == "assistant", item["position"], message))
            ordered_messages.sort(key=lambda item: item[:3])
            result["messages"] = [item[3] for item in ordered_messages]
            return result

    def get_revision(self, conversation_id, message_id, revision_id):
        """Read a prior answer only within its owning conversation and message."""
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT r.message_json,r.saved_at FROM answer_revisions AS r "
                "JOIN conversations AS c ON c.id=r.conversation_id "
                "WHERE r.id=? AND r.conversation_id=? AND r.message_id=? AND c.source_key=?",
                (revision_id, conversation_id, message_id, self.source_key)).fetchone()
            if row is None:
                raise ValueError("ANSWER_REVISION_NOT_FOUND")
            return {**json.loads(row[0]), "revision_id": revision_id, "saved_at": row[1]}

    def get_metadata(self, identifier):
        """Look up a conversation without materializing its transcript."""
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT " + _CONVERSATION_METADATA_COLUMNS + " FROM conversations AS c "
                "WHERE c.id=? AND c.source_key=?", (identifier, self.source_key)).fetchone()
            if row is None:
                raise ValueError("CONVERSATION_NOT_FOUND")
            result = dict(row)
            result["title"] = projected_conversation_title(db, identifier, result["title"])
            return result

    def model_history(self, identifier, *, max_messages=MODEL_HISTORY_MESSAGES,
                      max_characters=MODEL_HISTORY_CHARACTERS, before_position=None,
                      question=None, include_legacy=False, include_analysis_handoff=False):
        """Read a bounded provider context; the complete transcript stays on disk.

        Large response diagnostics and framework excerpts are display records,
        not conversation memory. Recent source references and a separately
        bounded local session subject are restored.
        Length checks happen in SQLite before text is returned to Python. The
        optional old projection is only for matching saved investigation IDs.
        """
        if not 1 <= max_messages <= MODEL_HISTORY_MESSAGES or not 0 <= max_characters <= MAX_MODEL_HISTORY_CHARACTERS:
            raise ValueError("CONVERSATION_HISTORY_BUDGET_INVALID")
        if before_position is not None and (type(before_position) is not int or before_position < 1):
            raise ValueError("CONVERSATION_HISTORY_POSITION_INVALID")
        self.get_metadata(identifier)
        if not max_characters:
            return []
        source_question = False
        if question is not None:
            from business_chat import _history_projection_mode
            source_question = _history_projection_mode(question) == "source_question"
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            rows = _model_history_rows(db, identifier, max_messages, before_position, legacy=False)
            messages = _project_history(db, rows, max_characters, source_question=source_question,
                                        include_analysis_handoff=include_analysis_handoff)
            if include_legacy:
                old_rows = _model_history_rows(db, identifier, max_messages, before_position, legacy=True)
                legacy = _project_history(db, old_rows, max_characters, legacy=True)
                return ConversationHistory(messages, legacy)
            return messages

    def list(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            result = [dict(row) for row in db.execute(
                "SELECT " + _CONVERSATION_METADATA_COLUMNS + " FROM conversations AS c "
                "WHERE c.source_key=? "
                "ORDER BY first_question_at DESC,c.created_at DESC,c.id DESC LIMIT 100",
                (self.source_key,))]
            for item in result:
                item["title"] = projected_conversation_title(db, item["id"], item["title"])
            return result

    def delete(self, identifier):
        with closing(sqlite3.connect(self.path)) as db, db:
            if db.execute("SELECT 1 FROM conversations WHERE id=? AND source_key=?",
                          (identifier, self.source_key)).fetchone() is None:
                raise ValueError("CONVERSATION_NOT_FOUND")
            db.execute("DELETE FROM messages WHERE conversation_id=?", (identifier,))
            db.execute("DELETE FROM answer_revisions WHERE conversation_id=?", (identifier,))
            db.execute("DELETE FROM conversations WHERE id=? AND source_key=?",
                       (identifier, self.source_key))

    def retry_target(self, conversation_id, user_message_id):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            if db.execute("SELECT 1 FROM conversations WHERE id=? AND source_key=?",
                          (conversation_id, self.source_key)).fetchone() is None:
                raise ValueError("CONVERSATION_NOT_FOUND")
            user = db.execute(
                "SELECT position,content,run_id FROM messages WHERE id=? AND conversation_id=? "
                "AND role='user' AND status IN ('partial','failed','cancelled','interrupted')",
                (user_message_id, conversation_id)).fetchone()
            if user is None:
                raise ValueError("RETRY_NOT_AVAILABLE")
            assistant = db.execute(
                "SELECT id FROM messages WHERE conversation_id=? AND role='assistant' "
                "AND run_id=? AND status IN ('partial','failed') ORDER BY position LIMIT 1",
                (conversation_id, user["run_id"])).fetchone()
            return {"question": user["content"], "position": user["position"],
                    "assistant_message_id": assistant["id"] if assistant else None}

    def retry_for_question(self, conversation_id, question):
        """Resume only an exact repeat of the latest unfinished user turn.

        Earlier turns require their explicit retry ID, so reused wording cannot
        restore a stale conversation focus after the user has changed topics.
        """
        self.get_metadata(conversation_id)
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT id,status,content FROM messages WHERE conversation_id=? AND role='user' "
                "ORDER BY position DESC LIMIT 1", (conversation_id,)).fetchone()
        return row[0] if row and row[2] == question and row[1] in {
            "partial", "failed", "cancelled", "interrupted"} else None

    def claim_retry(self, conversation_id, user_message_id, new_run_id):
        with closing(sqlite3.connect(self.path)) as db, db:
            if db.execute("SELECT 1 FROM conversations WHERE id=? AND source_key=?",
                          (conversation_id, self.source_key)).fetchone() is None:
                raise ValueError("CONVERSATION_NOT_FOUND")
            user = db.execute(
                "SELECT run_id FROM messages WHERE id=? AND conversation_id=? AND role='user' "
                "AND status IN ('partial','failed','cancelled','interrupted')",
                (user_message_id, conversation_id)).fetchone()
            if user is None:
                raise ValueError("RETRY_NOT_AVAILABLE")
            assistant = db.execute(
                "SELECT id FROM messages WHERE conversation_id=? AND role='assistant' "
                "AND run_id=? AND status IN ('partial','failed') ORDER BY position LIMIT 1",
                (conversation_id, user[0])).fetchone()
            changed = db.execute(
                "UPDATE messages SET status='pending',run_id=? WHERE id=? AND conversation_id=? "
                "AND role='user' AND status IN ('partial','failed','cancelled','interrupted')",
                (new_run_id, user_message_id, conversation_id)).rowcount
            if changed != 1:
                raise ValueError("RETRY_NOT_AVAILABLE")
            if assistant is not None:
                db.execute("UPDATE messages SET run_id=? WHERE id=? AND conversation_id=? "
                           "AND role='assistant' AND status IN ('partial','failed')",
                           (new_run_id, assistant[0], conversation_id))
            db.execute("UPDATE conversations SET updated_at=? WHERE id=? AND source_key=?",
                       (_now(), conversation_id, self.source_key))

    def replace_assistant(self, conversation_id, assistant_message_id, content, *,
                          status="completed", run_id=None, details=None,
                          preserve_previous=False, previous_run_id=None, previous_metadata=None):
        if assistant_message_id is None:
            return self.append(conversation_id, "assistant", content, status=status,
                               run_id=run_id, details=details)
        now = _now()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.row_factory = sqlite3.Row
            if db.execute("SELECT 1 FROM conversations WHERE id=? AND source_key=?",
                          (conversation_id, self.source_key)).fetchone() is None:
                raise ValueError("CONVERSATION_NOT_FOUND")
            if preserve_previous:
                previous = db.execute("SELECT * FROM messages WHERE id=? AND conversation_id=? "
                    "AND role='assistant' AND status IN ('partial','failed')",
                    (assistant_message_id, conversation_id)).fetchone()
                if previous is None:
                    raise ValueError("RETRY_NOT_AVAILABLE")
                old_details = json.loads(previous["details"])
                new_details = details or {}
                answer_changed = content != previous["content"] or any(
                    old_details.get(key) != new_details.get(key) for key in ("snapshot_id", "source_version")) or any(
                    old_details.get(key, []) != new_details.get(key, []) for key in
                    ("evidence_refs", "cited_evidence_ids", "framework_references", "related_sources"))
                old_inventory = old_details.get("repository_inventory") or {}
                new_inventory = new_details.get("repository_inventory") or {}
                if (isinstance(old_inventory, dict) and isinstance(new_inventory, dict)
                        and old_inventory.get("paged") and new_inventory.get("paged")
                        and old_inventory.get("handle") and new_inventory.get("handle")
                        and old_inventory["handle"] != new_inventory["handle"]):
                    # The visible overview can stay unchanged while a paged
                    # business formula is corrected. Retain that result version.
                    answer_changed = True
                snapshot = {key: previous[key] for key in ("id", "role", "content", "status", "created_at", "run_id")}
                snapshot.update(old_details)
                # Legacy answers may have their investigation linkage recovered
                # on opening the conversation without rewriting the old row.
                for key in ("investigation_run_id", "analysis_options"):
                    if key in (previous_metadata or {}):
                        snapshot[key] = previous_metadata[key]
                # Revisions stay independent, even if an older client supplied
                # presentation details containing earlier revisions.
                snapshot.pop("answer_revisions", None)
                if previous_run_id is not None:
                    snapshot["run_id"] = previous_run_id
                if answer_changed:
                    db.execute("INSERT INTO answer_revisions(id,conversation_id,message_id,saved_at,message_json) VALUES(?,?,?,?,?)",
                        (secrets.token_hex(16), conversation_id, assistant_message_id, now,
                         json.dumps(snapshot, ensure_ascii=False)))
            changed = db.execute(
                "UPDATE messages SET content=?,status=?,run_id=?,details=? WHERE id=? "
                "AND conversation_id=? AND role='assistant' AND status IN ('partial','failed')",
                (content, status, run_id, json.dumps(details or {}, ensure_ascii=False),
                 assistant_message_id, conversation_id)).rowcount
            if changed != 1:
                raise ValueError("RETRY_NOT_AVAILABLE")
            db.execute("UPDATE conversations SET updated_at=? WHERE id=? AND source_key=?",
                       (now, conversation_id, self.source_key))
        return assistant_message_id

    def append(self, identifier, role, content, *, status="completed", run_id=None, details=None):
        message_id, now = secrets.token_hex(16), _now()
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute("SELECT title FROM conversations WHERE id=? AND source_key=?",
                             (identifier, self.source_key)).fetchone()
            if row is None:
                raise ValueError("CONVERSATION_NOT_FOUND")
            db.execute("INSERT INTO messages(id,conversation_id,role,content,status,created_at,run_id,details) VALUES(?,?,?,?,?,?,?,?)",
                       (message_id, identifier, role, content, status, now, run_id, json.dumps(details or {}, ensure_ascii=False)))
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, identifier))
            if role == "user":
                title = projected_conversation_title(db, identifier, row[0], latest_question=content)
                if title != row[0]:
                    db.execute("UPDATE conversations SET title=? WHERE id=?", (title, identifier))
        return message_id

    def complete_user(self, run_id, status):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE messages SET status=? WHERE run_id=? AND role='user'", (status, run_id))

    def recover_interrupted(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE messages SET status='interrupted' WHERE status='pending' AND conversation_id IN (SELECT id FROM conversations WHERE source_key=?)", (self.source_key,))
