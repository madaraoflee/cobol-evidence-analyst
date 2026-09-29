"""Local durable conversations; browser input never supplies trusted history."""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import sqlite3


MODEL_HISTORY_MESSAGES = 64
MODEL_HISTORY_CHARACTERS = 24_000
MAX_MODEL_HISTORY_CHARACTERS = 128_000
MODEL_HISTORY_DETAILS_BYTES = 64_000
MODEL_HISTORY_REFERENCE_MESSAGES = 8


def _now():
    return datetime.now(timezone.utc).isoformat()


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
            row = db.execute("SELECT * FROM conversations WHERE id=? AND source_key=?", (identifier, self.source_key)).fetchone()
            if row is None:
                raise ValueError("CONVERSATION_NOT_FOUND")
            result = {key: row[key] for key in ("id", "title", "snapshot_id", "created_at", "updated_at")}
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
                "SELECT id,title,snapshot_id,created_at,updated_at FROM conversations "
                "WHERE id=? AND source_key=?", (identifier, self.source_key)).fetchone()
            if row is None:
                raise ValueError("CONVERSATION_NOT_FOUND")
            return dict(row)

    def model_history(self, identifier, *, max_messages=MODEL_HISTORY_MESSAGES,
                      max_characters=MODEL_HISTORY_CHARACTERS, before_position=None):
        """Read a bounded provider context; the complete transcript stays on disk.

        Large response diagnostics and framework excerpts are display records,
        not conversation memory. Only recent source references are restored.
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
                "SELECT position,role,length(content) AS content_length,"
                "length(CAST(details AS BLOB)) AS details_bytes FROM messages "
                "WHERE conversation_id=? AND role IN ('user','assistant') AND status='completed' "
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
                messages.append(message)
        messages.reverse()
        return messages

    def list(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(
                "SELECT id,title,snapshot_id,updated_at FROM conversations WHERE source_key=? ORDER BY updated_at DESC LIMIT 100",
                (self.source_key,))]

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
                "AND role='user' AND status IN ('failed','cancelled','interrupted')",
                (user_message_id, conversation_id)).fetchone()
            if user is None:
                raise ValueError("RETRY_NOT_AVAILABLE")
            assistant = db.execute(
                "SELECT id FROM messages WHERE conversation_id=? AND role='assistant' "
                "AND run_id=? AND status='failed' ORDER BY position LIMIT 1",
                (conversation_id, user["run_id"])).fetchone()
            return {"question": user["content"], "position": user["position"],
                    "assistant_message_id": assistant["id"] if assistant else None}

    def claim_retry(self, conversation_id, user_message_id, new_run_id):
        with closing(sqlite3.connect(self.path)) as db, db:
            if db.execute("SELECT 1 FROM conversations WHERE id=? AND source_key=?",
                          (conversation_id, self.source_key)).fetchone() is None:
                raise ValueError("CONVERSATION_NOT_FOUND")
            user = db.execute(
                "SELECT run_id FROM messages WHERE id=? AND conversation_id=? AND role='user' "
                "AND status IN ('failed','cancelled','interrupted')",
                (user_message_id, conversation_id)).fetchone()
            if user is None:
                raise ValueError("RETRY_NOT_AVAILABLE")
            assistant = db.execute(
                "SELECT id FROM messages WHERE conversation_id=? AND role='assistant' "
                "AND run_id=? AND status='failed' ORDER BY position LIMIT 1",
                (conversation_id, user[0])).fetchone()
            changed = db.execute(
                "UPDATE messages SET status='pending',run_id=? WHERE id=? AND conversation_id=? "
                "AND role='user' AND status IN ('failed','cancelled','interrupted')",
                (new_run_id, user_message_id, conversation_id)).rowcount
            if changed != 1:
                raise ValueError("RETRY_NOT_AVAILABLE")
            if assistant is not None:
                db.execute("UPDATE messages SET run_id=? WHERE id=? AND conversation_id=? "
                           "AND role='assistant' AND status='failed'",
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
                "AND conversation_id=? AND role='assistant' AND status='failed'",
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
            if db.execute("SELECT 1 FROM conversations WHERE id=? AND source_key=?",
                          (identifier, self.source_key)).fetchone() is None:
                raise ValueError("CONVERSATION_NOT_FOUND")
            db.execute("INSERT INTO messages(id,conversation_id,role,content,status,created_at,run_id,details) VALUES(?,?,?,?,?,?,?,?)",
                       (message_id, identifier, role, content, status, now, run_id, json.dumps(details or {}, ensure_ascii=False)))
            db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, identifier))
            if role == "user":
                db.execute("UPDATE conversations SET title=? WHERE id=? AND title='新对话'", (content[:80], identifier))
        return message_id

    def complete_user(self, run_id, status):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE messages SET status=? WHERE run_id=? AND role='user'", (status, run_id))

    def recover_interrupted(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE messages SET status='interrupted' WHERE status='pending' AND conversation_id IN (SELECT id FROM conversations WHERE source_key=?)", (self.source_key,))
