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
    return _latest_owned_analysis(database_path, source_root, conversation_id)[0]


def _latest_owned_analysis(database_path, source_root, conversation_id, *, first_failure_followup=None):
    path = Path(database_path).resolve().parent / "conversations.sqlite"
    if not path.is_file() or path.is_symlink():
        return None, [], [], None
    from conversation_store import MODEL_HISTORY_MESSAGES, _MESSAGE_TURN_POSITION_SQL, _model_history_rows
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        owner = db.execute("SELECT 1 FROM conversations WHERE id=? AND source_key=?", (
            conversation_id, hashlib.sha256(str(Path(source_root).resolve()).encode()).hexdigest())).fetchone()
        if owner is None:
            return None, [], [], None
        # A diagnostic-only failed answer is not model history. Select the same
        # latest eligible assistant as the transcript, including valid partial
        # answers and logical retry order. Never skip a missing/invalid handle
        # on that selected answer to borrow an older investigation instead.
        latest = next((row for row in _model_history_rows(db, conversation_id,
            MODEL_HISTORY_MESSAGES, None, legacy=False) if row["role"] == "assistant"), None)
        if latest is None and first_failure_followup is not None:
            failed_handle, failed_question = _first_failed_turn(db, conversation_id, first_failure_followup)
            return failed_handle, [], [], failed_question
        if latest is None or latest["status"] not in {"completed", "partial"}:
            return None, [], [], None
        row = db.execute("SELECT CASE WHEN json_valid(details) THEN "
            "CASE WHEN json_type(details,'$.investigation_run_id')='text' "
            "AND length(json_extract(details,'$.investigation_run_id'))=24 "
            "THEN json_extract(details,'$.investigation_run_id') END END,"
            "CASE WHEN json_valid(details) THEN json_extract(details,'$.retry_blocked_reason') END "
            "FROM messages WHERE conversation_id=? AND position=? AND role='assistant'",
            (conversation_id, latest["position"])).fetchone()
        if row is None or row[1] == "REMOTE_REQUEST_OUTCOME_UNKNOWN":
            return None, [], [], None
        # Only diagnostic turns after this logical turn may be skipped. A
        # retried older question can have a newer physical row position.
        skipped = db.execute("SELECT role,status,run_id AS job_id,"
            f"{_MESSAGE_TURN_POSITION_SQL} AS turn_position,"
            "CASE WHEN role='user' THEN substr(content,1,32769) END AS question,"
            "CASE WHEN json_valid(details) THEN json_extract(details,'$.retry_blocked_reason') END AS blocked,"
            "CASE WHEN json_valid(details) THEN json_extract(details,'$.investigation_run_id') END AS handle "
            "FROM messages WHERE conversation_id=? "
            f"AND ({_MESSAGE_TURN_POSITION_SQL})>? ORDER BY {_MESSAGE_TURN_POSITION_SQL} DESC,position DESC LIMIT ?",
            (conversation_id, latest["turn_position"], MODEL_HISTORY_MESSAGES + 1)).fetchall()
        if len(skipped) > MODEL_HISTORY_MESSAGES or any(item["blocked"] == "REMOTE_REQUEST_OUTCOME_UNKNOWN" for item in skipped):
            return None, [], [], None
        questions = [{"question": item["question"], "handles": [answer["handle"] for answer in skipped
            if answer["role"] == "assistant" and item["job_id"] and answer["job_id"] == item["job_id"]
            and answer["turn_position"] == item["turn_position"] and isinstance(answer["handle"], str)
            and re.fullmatch(r"[0-9a-f]{24}", answer["handle"])]}
            for item in skipped if item["role"] == "user"
            and item["status"] in {"failed", "partial", "cancelled", "interrupted"}]
        handles = [item["handle"] for item in skipped if item["role"] == "assistant"
                   and isinstance(item["handle"], str) and re.fullmatch(r"[0-9a-f]{24}", item["handle"])]
    return row[0], questions, handles, None


def _first_failed_turn(db, conversation_id, followup):
    """Pair the only closed diagnostic turn with its original user question."""
    from conversation_store import _MESSAGE_TURN_POSITION_SQL

    rows = db.execute(
        "SELECT role,status,run_id,"
        f"{_MESSAGE_TURN_POSITION_SQL} AS turn_position,"
        "CASE WHEN role='user' THEN substr(content,1,32769) END AS question,"
        "CASE WHEN json_valid(details) THEN json_extract(details,'$.investigation_run_id') END AS handle,"
        "CASE WHEN json_valid(details) THEN json_extract(details,'$.retry_blocked_reason') END AS blocked,"
        "CASE WHEN json_valid(details) THEN json_extract(details,'$.answer_origin') END AS origin,"
        "CASE WHEN json_valid(details) THEN json_extract(details,'$.answer_available') END AS available "
        "FROM messages WHERE conversation_id=? AND role IN ('user','assistant') ORDER BY position LIMIT 4",
        (conversation_id,)).fetchall()
    if len(rows) == 3:
        pending = rows[-1]
        if (pending["role"] != "user" or pending["status"] != "pending" or pending["question"] != followup
                or not pending["run_id"] or pending["run_id"] == rows[0]["run_id"]):
            return None, None
        rows = rows[:2]
    if len(rows) != 2:
        return None, None
    user, assistant = rows
    question, handle = user["question"], assistant["handle"]
    if (user["role"] != "user" or assistant["role"] != "assistant"
            or user["status"] != "failed" or assistant["status"] != "failed"
            or not user["run_id"] or user["run_id"] != assistant["run_id"]
            or user["turn_position"] != assistant["turn_position"]
            or assistant["blocked"] or assistant["origin"] != "diagnostic" or assistant["available"] != 0
            or not question or len(question) > 32768
            or not isinstance(handle, str) or re.fullmatch(r"[0-9a-f]{24}", handle) is None):
        return None, None
    return handle, question


def _strictly_extends_analysis_memory(previous, current):
    """Select a monotonic checkpoint, never merge unrelated failed attempts."""
    old_context, context = previous.get("source_memory_context"), current.get("source_memory_context")
    if (not isinstance(old_context, dict) or not isinstance(context, dict)
            or any(not old_context.get(key) or context.get(key) != old_context[key]
                   for key in ("run_id", "question", "analysis_identity"))):
        return False
    old_memory, memory = previous.get("prior_source_memory"), current.get("prior_source_memory")
    if not isinstance(old_memory, dict) or not isinstance(memory, dict):
        return False
    old_ledger, ledger = old_memory.get("analysis_ledger"), memory.get("analysis_ledger")
    if not isinstance(old_ledger, dict) or not isinstance(ledger, dict):
        return False
    old_generations, generations = old_ledger.get("generations"), ledger.get("generations")
    if (not isinstance(old_generations, list) or not isinstance(generations, list)
            or generations[:len(old_generations)] != old_generations):
        return False
    page_sets = []
    for value in (old_memory, memory):
        pages = value.get("pages")
        if (not isinstance(pages, list) or any(not isinstance(page, dict)
                or not isinstance(page.get("evidence_id"), str) for page in pages)):
            return False
        by_id = {page["evidence_id"]: page for page in pages}
        if len(by_id) != len(pages):
            return False
        page_sets.append(by_id)
    old_pages, pages = page_sets
    if any(pages.get(identifier) != page for identifier, page in old_pages.items()):
        return False
    old_origins, origins = old_memory.get("origins"), memory.get("origins")
    if (not isinstance(old_origins, dict) or not isinstance(origins, dict)
            or any(origins.get(identifier) != origin for identifier, origin in old_origins.items())):
        return False
    return len(generations) > len(old_generations) or len(pages) > len(old_pages)


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
                   if isinstance(item, dict) and item.get("role") == "assistant"), None)
    run_id = latest.get("investigation_run_id") if latest is not None else None
    first_failure_followup = None
    if latest is None and _FOLLOWUP.search(str(question)):
        from repository_scope import repository_inventory_requested
        if not repository_inventory_requested(question):
            first_failure_followup = question
    if first_failure_followup is None and (
            not isinstance(run_id, str) or re.fullmatch(r"[0-9a-f]{24}", run_id) is None):
        return None
    try:
        owned_handle, skipped_questions, skipped_handles, failed_question = _latest_owned_analysis(
            database_path, source_root, conversation_id, first_failure_followup=first_failure_followup)
        if failed_question is not None:
            run_id = owned_handle
        if owned_handle != run_id or run_id is None:
            return None
        skipped_runs = {handle: store.load(handle) for handle in skipped_handles}
        if any(saved.get("uncertain_requests") for saved in skipped_runs.values()):
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
        if failed_question is not None:
            memory_context = checkpoint.get("source_memory_context")
            if (saved["metadata"].get("question_id") != "sha256:" + hashlib.sha256(failed_question.encode()).hexdigest()
                    or not isinstance(memory_context, dict) or memory_context.get("run_id") != run_id):
                return None
        prior_subject = checkpoint.get("session_subject")
        if (not isinstance(prior_subject, dict) or prior_subject.get("status") != "resolved"
                or not isinstance(prior_subject.get("root_paths"), list) or len(prior_subject["root_paths"]) != 1
                or not isinstance(prior_subject.get("scope"), dict) or not checkpoint.get("prior_source_memory")):
            return None
        prior_scope = prior_subject["scope"]
        # Transcript eligibility and reusable source work are separate. A
        # diagnostic-only turn may still have durably completed new analysis.
        handoff_saved, handoff_run_id = saved, run_id
        lineage_handles = {run_id}
        with closing(sqlite3.connect(database_path)) as connection:
            metadata = _metadata(connection)
            if any(prior_subject.get(key) != metadata.get(key) for key in
                   ("snapshot_id", "source_root_hash", "compilation_context_fingerprint")):
                return None
            for skipped_turn in reversed(skipped_questions):
                skipped_question = skipped_turn["question"]
                if (not isinstance(skipped_question, str) or len(skipped_question) > 32768
                        or _RESET.search(skipped_question) or _NEW_TOPIC.search(skipped_question)):
                    return None
                skipped_identity = _question_identity(connection, skipped_question)
                if skipped_identity.get("status") == "resolved":
                    skipped_subject = build_session_subject(connection, skipped_question, skipped_identity)
                elif (skipped_identity.get("status") == "none" and _FOLLOWUP.search(skipped_question)
                        and len(skipped_turn["handles"]) == 1):
                    # A failed deictic question can retain its locally verified
                    # lineage. Reuse that binding, never guess a topic from the
                    # diagnostic text or borrow an unrelated prior handle.
                    failed_run = skipped_runs[skipped_turn["handles"][0]]
                    failed_checkpoint = failed_run["checkpoint"]
                    failed_lineage = failed_checkpoint.get("conversation_analysis_handoff")
                    if (not isinstance(failed_lineage, dict)
                            or failed_run.get("state") not in {"completed", "attention"}
                            or failed_run["metadata"].get("question_id") !=
                                "sha256:" + hashlib.sha256(skipped_question.encode()).hexdigest()
                            or failed_lineage.get("from_run_id") not in lineage_handles
                            or failed_checkpoint.get("conversation_analysis_context") != checkpoint["conversation_analysis_context"]
                            or failed_checkpoint.get("source_index_configuration") != session.index_configuration
                            or fingerprint(failed_checkpoint.get("compilation_context")) !=
                                fingerprint(session.options.get("compilation_context"))):
                        return None
                    skipped_subject = failed_checkpoint.get("session_subject") or {}
                    if (not isinstance(skipped_subject, dict)
                            or any(skipped_subject.get(key) != metadata.get(key) for key in
                                   ("snapshot_id", "source_root_hash", "compilation_context_fingerprint"))):
                        return None
                else:
                    return None
                skipped_scope = skipped_subject.get("scope")
                if (not isinstance(skipped_scope, dict)
                        or skipped_subject.get("status") != "resolved"
                        or skipped_subject.get("root_paths") != prior_subject["root_paths"]
                        or skipped_scope.get("exclusive_source_scope") != prior_scope.get("exclusive_source_scope")):
                    return None
                if len(skipped_turn["handles"]) == 1:
                    failed_handle = skipped_turn["handles"][0]
                    failed_run = skipped_runs[failed_handle]
                    failed_checkpoint = failed_run["checkpoint"]
                    failed_lineage = failed_checkpoint.get("conversation_analysis_handoff")
                    failed_subject = failed_checkpoint.get("session_subject")
                    if (not isinstance(failed_lineage, dict) or not isinstance(failed_subject, dict)
                            or failed_run.get("state") not in {"completed", "attention"}
                            or failed_run["metadata"].get("question_id") !=
                                "sha256:" + hashlib.sha256(skipped_question.encode()).hexdigest()
                            or failed_lineage.get("from_run_id") not in lineage_handles
                            or failed_checkpoint.get("conversation_analysis_context") != checkpoint["conversation_analysis_context"]
                            or failed_checkpoint.get("source_index_configuration") != session.index_configuration
                            or fingerprint(failed_checkpoint.get("compilation_context")) !=
                                fingerprint(session.options.get("compilation_context"))
                            or failed_subject.get("status") != "resolved"
                            or failed_subject.get("root_paths") != prior_subject["root_paths"]
                            or not isinstance(failed_subject.get("scope"), dict)
                            or failed_subject["scope"].get("exclusive_source_scope") != prior_scope.get("exclusive_source_scope")
                            or any(failed_subject.get(key) != metadata.get(key) for key in
                                   ("snapshot_id", "source_root_hash", "compilation_context_fingerprint"))):
                        return None
                    lineage_handles.add(failed_handle)
                    if _strictly_extends_analysis_memory(handoff_saved["checkpoint"], failed_checkpoint):
                        handoff_saved, handoff_run_id = failed_run, failed_handle
            navigation_history = history
            if failed_question is not None:
                failed_identity = _question_identity(connection, failed_question)
                failed_subject = build_session_subject(connection, failed_question, failed_identity)
                if (failed_subject.get("status") != "resolved"
                        or failed_subject.get("root_paths") != prior_subject["root_paths"]
                        or failed_subject.get("scope", {}).get("exclusive_source_scope") !=
                            prior_scope.get("exclusive_source_scope")):
                    return None
                navigation_history = [{"role": "user", "content": failed_question}]
            current = _question_identity(connection, question)
            if current.get("status") == "none":
                current = (resolve_question_field_subject(connection, question)
                    or (resolve_conversation_subject(connection, question, navigation_history, current_identity=current)
                        if _FOLLOWUP.search(str(question)) else None))
            if current is None or current.get("status") != "resolved":
                return None
            subject = build_session_subject(connection, question, current, previous=prior_subject)
            if (subject.get("status") != "resolved" or subject.get("version_changed")
                    or subject.get("root_paths") != prior_subject["root_paths"]
                    or subject.get("scope", {}).get("exclusive_source_scope") !=
                        prior_scope.get("exclusive_source_scope")):
                return None
            if entry_program:
                entry = _question_identity(connection, entry_program)
                if entry.get("status") != "resolved" or entry.get("direct_paths") != subject["root_paths"]:
                    return None
        saved, run_id = handoff_saved, handoff_run_id
        checkpoint = saved["checkpoint"]
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
