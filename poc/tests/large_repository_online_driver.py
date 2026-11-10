"""Actual online tools and durable recovery driven from supplied source only.

The generated business gold is deliberately unavailable to this driver. Model
interpretation is synthetic; material reads, reviews, recovery, final source
validation, publication and export use production code.
"""
from __future__ import annotations

import json
from pathlib import Path
import re


def exercise_online_store(material, source, store_root, *, progress=None):
    import online_investigation as investigation
    from company_api import CompanyAPIConfig, OpenAICompatibleChatClient, TransportResponse
    from online_inventory_store import OnlineInventoryStore
    from online_material_format import canonical_json, digest
    from online_material_query import MaterialQuery
    from online_material_reader import DirectoryObjectStore, MaterialReader

    question = "List all premium calculation formulas across the entire repository."
    query_factory = lambda: MaterialQuery(MaterialReader(DirectoryObjectStore(material)))
    query = query_factory()
    store = OnlineInventoryStore(Path(store_root))
    ownership = dict(workspace_id="scale-workspace", conversation_id="scale-conversation",
                     user_message_id="scale-question", manifest=query.reader.manifest, question=question)
    session = store.session(**ownership)
    state = investigation._new_state(query.reader.manifest, question)
    state["answer_requirements"] = {"answer_language": "en", "answer_instructions": ""}
    session.capture(state)
    session.attach(state)
    read_calls, verified, restored, checkpoint_bytes, paths = [], set(), 0, [], set()
    inventory_paths, inventory_cursors, cursor = set(), set(), None
    first_publication, first_page = None, None

    def read(path, **options):
        read_calls.append(path)
        return (Path(source) / path).read_bytes()

    sources = investigation._Sources(query, read, None)

    def execute(action):
        return investigation._execute(query, sources, investigation._action(action), state, verified)

    def restore():
        nonlocal query, sources, session, state, verified, restored
        checkpoint = json.loads(canonical_json(investigation._checkpoint(state)))
        checkpoint_bytes.append(len(canonical_json(checkpoint)))
        query = query_factory()
        session = OnlineInventoryStore(Path(store_root)).session(**ownership,
            identifier=checkpoint["inventory_session"]["id"])
        state = investigation._restore(query.reader.manifest, question, checkpoint, inventory_session=session)
        session.attach(state)
        sources = investigation._Sources(query, read, None)
        verified = set()
        restored += 1

    while True:
        page = execute({"tool": "inventory", "limit": 100, "cursor": cursor})
        for row in page["files"]:
            path = row["relative_path"]
            if path in inventory_paths:
                raise AssertionError("Production inventory repeated a file")
            inventory_paths.add(path)
        if page["complete"]:
            break
        cursor = page["next_cursor"]
        marker = canonical_json(cursor)
        if marker in inventory_cursors:
            raise AssertionError("Production directory cursor failed to advance")
        inventory_cursors.add(marker)
        if len(inventory_cursors) % 20 == 0:
            restore()
    restore()

    while pending := session.pending(limit=1):
        task = pending[0]
        path = task.get("path")
        if not path or path in paths:
            raise AssertionError("Reviewed file unexpectedly remained pending: " + repr(task))
        metadata = query.reader.file(path)
        candidates = execute({"tool": "arithmetic_candidates", "path": path, "limit": 100})
        if candidates["truncated"]:
            raise AssertionError("Scale fixture unexpectedly needs another per-file candidate page")
        evidence = execute({"tool": "read", "path": path, "start_line": 1, "end_line": metadata["line_count"]})
        source_page = next(row for row in state["evidence"] if row["id"] == evidence["id"])
        lines = source_page["text"].splitlines()
        label = next((match.group(1) for line in lines
                      if (match := re.search(r"Business calculation:\s*(.+)", line))), None)
        if label is None:
            raise AssertionError("Reviewer lacks original-source business-role label")
        state["request_evidence"] = [evidence["id"]]
        for candidate in candidates["candidates"]:
            formula = "\n".join(lines[candidate["start_line"] - 1:candidate["end_line"]]).strip()
            execute({"tool": "review", "candidate_id": candidate["candidate_id"], "disposition": "included",
                "summary": label + " determines this business entry's charge. " + formula,
                "citations": [evidence["id"]], "start_line": candidate["start_line"], "end_line": candidate["end_line"]})
        execute({"tool": "review_file", "path": path, "start_line": 1, "end_line": metadata["line_count"],
            "summary": "Reviewed the entire supplied source and the calculation's business role; runtime inputs remain unverified.",
            "citations": [evidence["id"]]})
        paths.add(path)
        if first_publication is None:
            first_publication = session.publish(state, "First reviewed batch", False)
            first_page = first_publication.page()
        if len(paths) % 250 == 0:
            restore()
            if progress:
                progress({"reviewed_files": len(paths), "restores": restored,
                          "checkpoint_bytes": checkpoint_bytes[-1]})
    if paths != inventory_paths:
        raise AssertionError("Reviewed paths differ from the real complete inventory")
    source_reads_before_delivery = len(read_calls)
    summary = session.summary()
    if not summary["complete"]:
        raise AssertionError("Actual review tools did not complete their durable work: " + repr(summary))
    restore()
    requests, recall_requests = [], []

    def final_response(request):
        body = json.loads(request.body)
        context = json.loads(body["messages"][-1]["content"])
        requests.append(context)
        if context["work_ledger"]["pending"]:
            raise AssertionError("Final model request still has unfinished inventory work")
        if not context["source_context"]:
            identifier = context["retained_findings"][0]["citations"][0]
            recall_requests.append(identifier)
            value = {"actions": [{"tool": "recall", "evidence_ids": [identifier]}]}
        else:
            identifier = context["source_context"][0]["id"]
            if recall_requests and identifier != recall_requests[0]:
                raise AssertionError("Final answer did not use the exact recalled original source")
            value = {"answer": "The complete indexed snapshot calculation inventory is available with business roles and original-source positions [" + identifier + "].",
                     "citations": [identifier], "complete": True, "remaining": []}
        return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": json.dumps(value)}, "finish_reason": "stop"}]}))

    client = OpenAICompatibleChatClient(CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="test-secret"), transport=final_response)
    checkpoint = investigation._checkpoint(state)
    final_runs = 0
    while final_runs < 100:
        final_runs += 1
        query = query_factory()
        session = OnlineInventoryStore(Path(store_root)).session(**ownership, identifier=checkpoint["inventory_session"]["id"])
        result = investigation.run_investigation(query, read, question, checkpoint=checkpoint,
            inventory_session=session, allow_source_transfer=True, client=client, max_model_calls=1, max_actions=4, locale="en")
        checkpoint = result["checkpoint"]
        checkpoint_bytes.append(len(canonical_json(checkpoint)))
        if result["status"] == "answered":
            break
        if result["status"] != "needs_continuation":
            raise AssertionError("Final production delivery failed: " + repr((result["status"], result["error"])))
    if result["status"] != "answered":
        raise AssertionError("Final source verification stopped advancing")
    publication = store.publication(result["inventory_result"]["id"], conversation_id=ownership["conversation_id"],
        workspace_id=ownership["workspace_id"], revision=ownership["manifest"]["revision"])
    if first_publication.page() != first_page:
        raise AssertionError("Later source review mutated an older published result")
    with store.connection() as db:
        durable_actions = db.execute("SELECT count(*) FROM actions WHERE session=?", (session.id,)).fetchone()[0]
    initial_action_retained = session.action_done(digest(canonical_json(investigation._action(
        {"tool": "inventory", "limit": 100, "cursor": None}))))
    return {"publication": publication, "store": store, "result": result,
        "statistics": {"inventory_files": len(inventory_paths), "reviewed_files": len(paths),
            "restores": restored, "maximum_checkpoint_bytes": max(checkpoint_bytes),
            "source_reads_before_delivery": source_reads_before_delivery, "final_source_reads": len(read_calls) - source_reads_before_delivery,
            "final_model_requests": len(requests), "final_runs": final_runs, "older_handle_unchanged": True,
            "recall_requests": recall_requests, "first_final_request_recalled_source": bool(requests and not requests[0]["source_context"] and recall_requests),
            "final_answer_citations": [row["id"] for row in result["citations"]],
            "durable_completed_actions": durable_actions, "initial_completed_action_retained": initial_action_retained,
            "reviewed_source_roles_and_formulas": True, "real_model_semantics_verified": False}}
