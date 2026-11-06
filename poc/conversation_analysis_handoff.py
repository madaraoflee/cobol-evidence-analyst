"""Carry a locally verified analysis into a new question on the same object.

The transcript supplies only a navigation handle. Ownership, current source,
scope and original request proofs are checked against local records before any
candidate interpretation is reused. A new question always owns its own answer
and request budget.
"""

from contextlib import closing
from copy import deepcopy
import hashlib
from pathlib import Path
import re
import sqlite3

from conversation_focus import (_metadata, _question_identity, _RESET, build_session_subject,
                                resolve_conversation_subject, resolve_question_field_subject)
from prior_source_memory import PriorSourceMemory
from run_store import fingerprint


SCHEMA = "conversation-analysis-handoff/v1"
_FOLLOWUP = re.compile(r"继续|繼續|再(?:详细|詳細|说|說|解释|解釋|看|核对|核對)|刚才|剛才|"
    r"前面|上面|上述|这(?:个|段|项|条|些)|這(?:個|段|項|條|些)|那(?:个|段|项|条|些)|该(?:流程|程序|处理)|"
    r"\b(?:this|that|these|those|it|same|continue|further|earlier|previous)\b", re.I)
_NEW_TOPIC = re.compile(r"换(?:个|一个)话题|換(?:個|一個)話題|另(?:一)?个问题|另(?:一)?個問題|"
    r"\b(?:new\s+question|another\s+topic|unrelated\s+question)\b", re.I)


def conversation_analysis_context(conversation_id, source_root, framework_reference_path):
    return {"schema": SCHEMA, "conversation_id": conversation_id,
        "source_root_sha256": hashlib.sha256(str(Path(source_root).resolve()).encode()).hexdigest(),
        "framework_reference_path": str(Path(framework_reference_path).resolve()) if framework_reference_path else None}


def _latest_owned_handle(database_path, source_root, conversation_id):
    path = Path(database_path).resolve().parent / "conversations.sqlite"
    if not path.is_file() or path.is_symlink():
        return None
    from conversation_store import _MESSAGE_TURN_POSITION_SQL
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        owner = db.execute("SELECT 1 FROM conversations WHERE id=? AND source_key=?", (
            conversation_id, hashlib.sha256(str(Path(source_root).resolve()).encode()).hexdigest())).fetchone()
        if owner is None:
            return None
        row = db.execute("SELECT status,CASE WHEN json_valid(details) THEN "
            "CASE WHEN json_type(details,'$.investigation_run_id')='text' "
            "AND length(json_extract(details,'$.investigation_run_id'))=24 "
            "THEN json_extract(details,'$.investigation_run_id') END END "
            "FROM messages WHERE conversation_id=? AND role='assistant' "
            f"ORDER BY {_MESSAGE_TURN_POSITION_SQL} DESC,position DESC LIMIT 1", (conversation_id,)).fetchone()
    return row[1] if row and row[0] in {"completed", "partial"} else None


def followup_analysis_seed(question, *, database_path, source_root, session, history, conversation_id,
                           store, framework_reference_path=None, entry_program=None):
    """Return a validated same-object seed, or no inheritance at all.

    Missing legacy ownership metadata, ambiguity, changed source/configuration
    and forged handles fall back to an ordinary fresh investigation. This does
    not reinterpret a failure or resend an unresolved remote request.
    """
    if (not isinstance(conversation_id, str) or not conversation_id
            or _RESET.search(str(question)) or _NEW_TOPIC.search(str(question))):
        return None
    latest = next((item for item in reversed(history or ())
                   if isinstance(item, dict) and item.get("role") == "assistant"), {})
    run_id = latest.get("investigation_run_id")
    if not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{24}", run_id) is None:
        return None
    try:
        if _latest_owned_handle(database_path, source_root, conversation_id) != run_id:
            return None
        saved = store.load(run_id)
        checkpoint = saved["checkpoint"]
        if (saved.get("uncertain_requests") or saved.get("state") not in {"completed", "attention"}
                or checkpoint.get("conversation_analysis_context") !=
                    conversation_analysis_context(conversation_id, source_root, framework_reference_path)
                or checkpoint.get("source_index_configuration") != session.index_configuration
                or fingerprint(checkpoint.get("compilation_context")) !=
                    fingerprint(session.options.get("compilation_context"))):
            return None
        prior_subject = checkpoint.get("session_subject") or {}
        if (prior_subject.get("status") != "resolved" or len(prior_subject.get("root_paths", [])) != 1
                or not checkpoint.get("prior_source_memory")):
            return None
        with closing(sqlite3.connect(database_path)) as connection:
            metadata = _metadata(connection)
            if any(prior_subject.get(key) != metadata.get(key) for key in
                   ("snapshot_id", "source_root_hash", "compilation_context_fingerprint")):
                return None
            current = _question_identity(connection, question)
            if current.get("status") == "none":
                current = (resolve_question_field_subject(connection, question)
                    or (resolve_conversation_subject(connection, question, history, current_identity=current)
                        if _FOLLOWUP.search(str(question)) else None))
            if current is None or current.get("status") != "resolved":
                return None
            subject = build_session_subject(connection, question, current, previous=prior_subject)
            if (subject.get("status") != "resolved" or subject.get("version_changed")
                    or subject.get("root_paths") != prior_subject["root_paths"]
                    or subject.get("scope", {}).get("exclusive_source_scope") !=
                        prior_subject.get("scope", {}).get("exclusive_source_scope")):
                return None
            if entry_program:
                entry = _question_identity(connection, entry_program)
                if entry.get("status") != "resolved" or entry.get("direct_paths") != subject["root_paths"]:
                    return None
        memory_checkpoint = deepcopy(checkpoint["prior_source_memory"])
        context = deepcopy(checkpoint.get("source_memory_context") or {})
        original_question = context.get("question")
        if not isinstance(original_question, str) or not original_question:
            return None
        original_run = store.load(context.get("run_id"))
        original_metadata = original_run["metadata"]
        if (original_metadata.get("question_id") != "sha256:" + hashlib.sha256(original_question.encode()).hexdigest()
                or original_metadata.get("analysis_identity") != context.get("analysis_identity")
                or original_run["checkpoint"].get("conversation_analysis_context") !=
                    checkpoint["conversation_analysis_context"]):
            return None
        pages = [*checkpoint.get("source_pages", []), *memory_checkpoint.get("pages", [])]
        for page in pages:
            capture = session.capture(page["relative_path"])
            if capture.sha256 != page["source_sha256"]:
                return None
            for origin in page.get("include_chain", []):
                if (isinstance(origin, dict) and origin.get("relative_path") and origin.get("source_hash")
                        and session.capture(origin["relative_path"]).sha256 != origin["source_hash"]):
                    return None
        records = [{key: row[key] for key in ("round_id", "request_body_sha256", "sources") if key in row}
                   for row in (checkpoint.get("quality") or {}).get("rounds", [])]
        context["rounds"] = [*context.get("rounds", []), *records]
        memory = PriorSourceMemory(database_path, question=original_question, run_id=context["run_id"],
            context={"analysis_identity": context["analysis_identity"],
                     "compilation_context": session.options.get("compilation_context")})
        memory.restore(memory_checkpoint, context["rounds"], check_cancel=session.check_cancel)
        if memory.analysis_ledger.pending_task is not None:
            return None
        # The old delivered draft belongs to its own question. Source-bound
        # findings and cumulative generations remain available for rechecking.
        drafts = {row["memory_id"] for row in memory_checkpoint.get("declared_blocks", [])
                  if row.get("kind") == "draft"}
        for key in ("blocks", "declared_blocks"):
            memory_checkpoint[key] = [row for row in memory_checkpoint.get(key, []) if row["memory_id"] not in drafts]
        memory_checkpoint["retracted_blocks"] = {key: value for key, value in
            memory_checkpoint.get("retracted_blocks", {}).items() if key not in drafts}
        seed = {"prior_source_memory": memory_checkpoint, "source_memory_context": context,
            "source_pages": deepcopy(checkpoint.get("source_pages", [])), "session_subject": subject,
            "analysis_compaction_pending_ids": deepcopy(checkpoint.get("analysis_compaction_pending_ids", [])),
            "conversation_analysis_handoff": {"schema": SCHEMA, "from_run_id": run_id,
                "root_paths": subject["root_paths"], "generation": memory.analysis_ledger.generation,
                "source_count": len(memory.pages), "candidate_only": True}}
        source_frontier = checkpoint.get("source_frontier_state")
        if (isinstance(source_frontier, dict) and source_frontier.get("snapshot_id") == metadata["snapshot_id"]
                and source_frontier.get("root_paths") == subject["root_paths"]):
            seed["source_frontier_state"] = deepcopy(source_frontier)
            # A completed cursor has no new pages to emit. Retain the exact
            # last supplied context so it cannot become an empty working set.
            if isinstance(checkpoint.get("source_frontier_context"), dict):
                seed["source_frontier_context"] = deepcopy(checkpoint["source_frontier_context"])
        return seed
    except (KeyError, TypeError, ValueError, OSError, sqlite3.Error):
        return None
