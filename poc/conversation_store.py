"""Local durable conversations; browser input never supplies trusted history."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
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
            result["messages"] = []
            for item in db.execute("SELECT * FROM messages WHERE conversation_id=? ORDER BY position", (identifier,)):
                message = {key: item[key] for key in ("id", "role", "content", "status", "created_at", "run_id")}
                details = json.loads(item["details"])
                message.update(details)
                result["messages"].append(message)
            return result

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
                      max_characters=MODEL_HISTORY_CHARACTERS, before_position=None):
        """Read a bounded provider context; the complete transcript stays on disk.

        Large response diagnostics and framework excerpts are display records,
        not conversation memory. Recent source references and a separately
        bounded local session subject are restored.
        Length checks happen in SQLite before text is returned to Python.
        """
        if not 1 <= max_messages <= MODEL_HISTORY_MESSAGES or not 0 <= max_characters <= MAX_MODEL_HISTORY_CHARACTERS:
            raise ValueError("CONVERSATION_HISTORY_BUDGET_INVALID")
        if before_position is not None and (type(before_position) is not int or before_position < 1):
            raise ValueError("CONVERSATION_HISTORY_POSITION_INVALID")
        self.get_metadata(identifier)
        if not max_characters:
            return []
        remaining = max_characters
        details_remaining = MODEL_HISTORY_DETAILS_BYTES
        messages = []
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            position_filter = "AND position<? " if before_position is not None else ""
            parameters = ((identifier, before_position, max_messages) if before_position is not None
                          else (identifier, max_messages))
            rows = list(db.execute(
                "SELECT position,role,status,length(content) AS content_length,"
                "length(CAST(details AS BLOB)) AS details_bytes FROM messages "
                "WHERE conversation_id=? AND role IN ('user','assistant') AND (status='completed' OR "
                "(status IN ('partial','failed','cancelled','interrupted') AND EXISTS (SELECT 1 FROM messages a "
                "WHERE a.conversation_id=messages.conversation_id AND a.run_id=messages.run_id "
                "AND a.role='assistant' AND a.status='partial' AND CASE WHEN json_valid(a.details) THEN "
                "json_extract(a.details,'$.answer_available')=1 AND "
                "(json_extract(a.details,'$.answer_origin') IN ('model','local_source_facts') OR "
                "json_extract(a.details,'$.model_answer_recorded')=1 OR "
                "(json_extract(a.details,'$.answer_origin') IS NULL AND "
                "json_extract(a.details,'$.model_answer_recorded') IS NULL)) ELSE 0 END))) "
                + position_filter + "ORDER BY position DESC LIMIT ?", parameters))
            latest_user = next((row for row in rows if row["role"] == "user"), None)
            reserved_user = min(latest_user["content_length"], max(1, max_characters // 4)) if latest_user else 0
            for offset, row in enumerate(rows):
                if remaining <= 0:
                    break
                if latest_user is not None and row["position"] == latest_user["position"]:
                    reserved_user = 0
                available = remaining - reserved_user
                if available <= 0:
                    continue
                content = db.execute("SELECT substr(content,1,?) FROM messages WHERE position=?",
                                     (available, row["position"])).fetchone()[0]
                message = {"role": row["role"], "content": content}
                if row["status"] == "partial":
                    message["analysis_status"] = "PARTIAL"
                remaining -= len(content)
                if row["content_length"] > len(content):
                    message["context_truncated"] = True
                if (offset < MODEL_HISTORY_REFERENCE_MESSAGES and row["role"] == "assistant"
                        and row["details_bytes"] <= details_remaining):
                    text = db.execute("SELECT details FROM messages WHERE position=?",
                                      (row["position"],)).fetchone()[0]
                    details_remaining -= row["details_bytes"]
                    details = json.loads(text)
                    for key in ("evidence_refs", "cited_evidence_ids", "investigation_state"):
                        if isinstance(details.get(key), (list, dict)):
                            message[key] = details[key]
                    if row["status"] == "partial":
                        for key in ("answer_origin", "model_answer_recorded", "answer_available"):
                            if isinstance(details.get(key), (str, bool)):
                                message[key] = details[key]
                    if isinstance(message.get("investigation_state"), dict):
                        # The subject has its own byte limit even when the
                        # complete details document fits the reference budget.
                        message["investigation_state"].pop("session_subject", None)
                if offset < MODEL_HISTORY_REFERENCE_MESSAGES and row["role"] == "assistant":
                    subject = _history_subject(db, row["position"])
                    if subject is not None:
                        state = message.setdefault("investigation_state", {})
                        state["session_subject"] = subject
                messages.append(message)
        messages.reverse()
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
                          status="completed", run_id=None, details=None):
        if assistant_message_id is None:
            return self.append(conversation_id, "assistant", content, status=status,
                               run_id=run_id, details=details)
        now = _now()
        with closing(sqlite3.connect(self.path)) as db, db:
            if db.execute("SELECT 1 FROM conversations WHERE id=? AND source_key=?",
                          (conversation_id, self.source_key)).fetchone() is None:
                raise ValueError("CONVERSATION_NOT_FOUND")
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
