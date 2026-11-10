"""Snapshot-bound, cumulative review of an explicitly requested formula inventory.

The index nominates positions, not business relevance. Every source window is
also reviewed so parser omissions cannot silently become a completeness claim.
"""
import hashlib
import json
import sqlite3

_SCHEMA = "repository-formula-inventory/v1"
_ARITHMETIC = ("COMPUTE", "ADD", "SUBTRACT", "MULTIPLY", "DIVIDE")
_WINDOW = 96
PAGED_FILE_THRESHOLD = 256


def _identifier(snapshot, kind, path, start, end, extra=""):
    return "inventory_" + hashlib.sha256(json.dumps(
        [snapshot, kind, path, start, end, extra], ensure_ascii=False).encode()).hexdigest()[:24]


def prepare_inventory(database_path, snapshot_id, question, saved=None):
    """Create the finite directory once; a same-snapshot continuation reuses it."""
    key = hashlib.sha256(json.dumps([snapshot_id, question], ensure_ascii=False).encode()).hexdigest()
    if isinstance(saved, dict) and saved.get("schema") == _SCHEMA and saved.get("scope_key") == key:
        if saved.get("paged"):
            from repository_inventory_store import InventoryStore
            restored = InventoryStore(database_path).load(saved["store_handle"], scope_key=key)
            restored["_store_database"] = str(database_path)
            return restored
        return saved
    db = sqlite3.connect(database_path)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        files = [dict(row) for row in db.execute(
            "SELECT relative_path,sha256,line_count FROM source_files ORDER BY relative_path")]
        items = []
        for row in files:
            for start in range(1, row["line_count"] + 1, _WINDOW):
                end = min(row["line_count"], start + _WINDOW - 1)
                items.append({"candidate_id": _identifier(snapshot_id, "source_review", row["relative_path"], start, end),
                    "kind": "source_review", "relative_path": row["relative_path"],
                    "source_sha256": row["sha256"], "start_line": start, "end_line": end})
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if {"business_rules", "business_rule_occurrences"} <= tables:
            rows = db.execute("SELECT o.*,b.rule_kind "
                "FROM business_rule_occurrences o JOIN business_rules b USING(rule_id) "
                "WHERE b.rule_kind IN (" + ",".join("?" for _ in _ARITHMETIC) + ") "
                "ORDER BY o.relative_path,o.start_line,o.start_column,o.occurrence_id", _ARITHMETIC)
            for row in rows:
                items.append({"candidate_id": _identifier(snapshot_id, "formula", row["relative_path"],
                    row["start_line"], row["end_line"], row["occurrence_id"]),
                    "kind": "formula", "relative_path": row["relative_path"], "source_sha256": row["source_sha256"],
                    "start_line": row["start_line"], "end_line": row["end_line"],
                    "rule_id": row["rule_id"], "occurrence_id": row["occurrence_id"],
                    "operation": row["rule_kind"]})
        items.sort(key=lambda row: (row["relative_path"], row["start_line"], row["kind"], row["candidate_id"]))
        state = {"schema": _SCHEMA, "scope_key": key, "snapshot_id": snapshot_id,
            "file_count": len(files), "items": items, "findings": {}, "additional_findings": {},
            "catalog_builds": 1, "rejected_count": 0, "narratives": {}, "cursor": 0, "reviewed_count": 0}
        if len(files) > PAGED_FILE_THRESHOLD:
            from repository_inventory_store import InventoryStore
            state.update(paged=True, _store_database=str(database_path), review_pending=len(items))
            state["store_handle"] = InventoryStore(database_path).create(state)
        return state
    finally:
        db.close()


def _complete(row):
    return bool(row) and row.get("disposition") in {"included", "excluded", "reviewed"}


def inventory_batch(state):
    records = state["findings"]
    # Resolve unseen work first; an uncertain classification cannot stall the
    # rest of the repository, nor disappear from the final pending count.
    cursor = state.get("cursor", 0)
    while cursor < len(state["items"]) and state["items"][cursor]["candidate_id"] in records:
        cursor += 1
    state["cursor"] = cursor
    pending = (state["items"][index] for index in range(cursor, len(state["items"]))) if cursor < len(state["items"]) else (
        row for row in state["items"] if not _complete(records.get(row["candidate_id"]))
        or row["candidate_id"] in state.get("presentation_pending", []))
    chosen, paths = [], set()
    for row in pending:
        if cursor < len(state["items"]) and row["candidate_id"] in records:
            continue
        if row["relative_path"] not in paths and len(paths) == 8:
            break
        paths.add(row["relative_path"])
        chosen.append(row)
        if len(chosen) == 32:
            break
    return chosen


def _cover(item, pages):
    rows = sorted((p for p in pages if p.get("relative_path") == item["relative_path"]
        and p.get("source_sha256") == item["source_sha256"] and p.get("source_text")
        and not p.get("span_truncated") and isinstance(p.get("start_line"), int)
        and isinstance(p.get("end_line"), int)), key=lambda p: (p["start_line"], -p["end_line"]))
    cursor, used = item["start_line"], []
    for page in rows:
        if page["start_line"] > cursor:
            break
        if page["end_line"] >= cursor:
            used.append(page["evidence_id"])
            cursor = page["end_line"] + 1
        if cursor > item["end_line"]:
            return used
    return []


def inventory_investigation(state, pages, max_actions=8):
    batch = inventory_batch(state)
    actions, required = [], []
    for item in batch:
        ids = _cover(item, pages)
        required.append({"id": item["candidate_id"], "kind": "formula" if item["kind"] == "formula" else "source",
            "status": "SATISFIED" if ids else "OPEN", "evidence_ids": ids,
            "relative_path": item["relative_path"], "start_line": item["start_line"], "end_line": item["end_line"],
            "reason": "inventory_classification_pending" if ids else "inventory_source_not_supplied"})
        if not ids:
            args = {key: item[key] for key in ("relative_path", "start_line", "end_line")}
            if not any(row["arguments"] == args or state.get("paged") and
                    row["arguments"]["relative_path"] == args["relative_path"]
                    and row["arguments"]["start_line"] <= args["start_line"]
                    and row["arguments"]["end_line"] >= args["end_line"] for row in actions):
                actions.append({"tool": "read", "arguments": args, "reason": "fixed_repository_inventory"})
    pending = len(state["items"]) - state["reviewed_count"] + len(state.get("presentation_pending", []))
    return {"scope": "fixed_repository_inventory", "state": "needs_evidence" if actions else "located",
        "can_answer": pending == 0, "can_answer_locally": bool(state["findings"]),
        "investigation_complete": pending == 0, "analysis_plan": {"intents": ["calculation_rules"]},
        "required_items": required, "planned_actions": actions[:max_actions],
        "candidate_paths": list(dict.fromkeys(row["relative_path"] for row in batch)),
        "pending_work": {"repository_inventory_count": pending}, "open_gaps": [],
        "global_coverage": {"scope": "fixed_snapshot_all_source_windows_and_arithmetic_occurrences",
            "candidate_count": len(state["items"]), "reviewed_count": len(state["items"]) - pending,
            "pending_count": pending, "semantic_execution_verified": False}}


def inventory_prompt(state):
    pending = len(state["items"]) - state["reviewed_count"] + len(state.get("presentation_pending", []))
    return {"schema": _SCHEMA, "snapshot_id": state["snapshot_id"],
        "file_count": state["file_count"], "candidate_count": len(state["items"]),
        "reviewed_count": state["reviewed_count"], "pending_count": pending,
        "items": [{**row, **({"previous_finding": state["findings"][row["candidate_id"]],
            "presentation_revision_required": True} if row["candidate_id"] in state.get("presentation_pending", []) else {})}
            for row in inventory_batch(state)],
        "response_schema": {"answer": "Business explanation for this batch in the requested language",
            "inventory_findings": [{"candidate_id": "one supplied item ID",
                "disposition": "included/excluded/uncertain for formula; reviewed/uncertain for source_review",
                "summary": "source-based business meaning or reason for exclusion, not merely an instruction translation",
                "formula": "business formula for included locations", "conditions": "applicability and exceptions for included locations",
                "evidence_ids": ["IDs of supplied source pages covering the entire item range"]}],
            "additional_findings": [{"relative_path": "a reviewed source path", "start_line": 1, "end_line": 1,
                "summary": "additional business formula missing from the arithmetic index", "formula": "formula",
                "conditions": "applicability", "evidence_ids": ["source page IDs"]}]},
        **({"durable_archive": {"handle":state["store_handle"], "all_previous_findings_preserved":True,
            "lookup_response": {"inventory_lookup":{"query":"business term, field or source path","cursor":0}},
            "instruction":"All earlier formulas, conditions, evidence and business explanations remain in the durable inventory. Use inventory_lookup to recall a bounded page of earlier analysis and its original source when interpreting cross-file relationships; do not infer that an absent working-window item is absent globally."},
            "lookup_result":state.get("lookup_result")} if state.get("paged") else {}),
        "instructions": "Review every supplied item against the original question. The arithmetic index does not establish premium relevance. "
            "Do not exclude abbreviations solely because they lack the question keyword. Follow source definitions/calls and preserve uncertainty. "
            "Source_review means the entire source window has been checked for relevant formulas, definitions, aliases and calls; "
            "report relevant formulas omitted by the index as additional_findings. Missing evidence or classification must remain uncertain. "
            "Return the JSON response_schema; the application accumulates findings across batches and renders the complete inventory. "
            "Previously accepted business findings remain in source analysis memory; do not rewrite or drop them."}


def accept_inventory_response(state, text, pages, *, requested_items):
    try:
        value = json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
    except (ValueError, TypeError):
        return 0
    if not isinstance(value, dict) or not isinstance(value.get("inventory_findings"), list):
        return 0
    allowed = {row["candidate_id"]: row for row in requested_items}
    supplied = {p.get("evidence_id"): p for p in pages}
    accepted = 0
    for row in value["inventory_findings"]:
        if not isinstance(row, dict) or row.get("candidate_id") not in allowed:
            state["rejected_count"] += 1
            continue
        item = allowed[row["candidate_id"]]
        ids = row.get("evidence_ids")
        disposition = row.get("disposition")
        valid = {"reviewed", "uncertain"} if item["kind"] == "source_review" else {"included", "excluded", "uncertain"}
        if (disposition not in valid or not isinstance(ids, list) or not ids or
                any(not isinstance(i, str) or i not in supplied for i in ids) or
                not _cover(item, [supplied[i] for i in ids]) or
                not isinstance(row.get("summary"), str) or not row["summary"].strip() or
                disposition == "included" and any(not isinstance(row.get(k), str) or not row[k].strip()
                                                   for k in ("formula", "conditions"))):
            state["rejected_count"] += 1
            continue
        record = {**{key: item[key] for key in ("candidate_id", "kind", "relative_path", "source_sha256", "start_line", "end_line")},
            "disposition": disposition, "summary": row["summary"].strip(),
            "formula": str(row.get("formula", "")), "conditions": str(row.get("conditions", "")),
            "evidence_ids": list(dict.fromkeys(ids))}
        if state.get("paged"):
            record["presentation_key"] = state.get("presentation_key")
        old = state["findings"].get(item["candidate_id"])
        presentation_revision = item["candidate_id"] in state.get("presentation_pending", [])
        if _complete(old) and not presentation_revision:
            continue
        if presentation_revision:
            state["presentation_pending"].remove(item["candidate_id"])
        state["findings"][item["candidate_id"]] = record
        state["reviewed_count"] = state.get("reviewed_count", 0) + int(_complete(record)) - int(_complete(old))
        accepted += int(record != old)
    for row in value.get("additional_findings", []) if isinstance(value.get("additional_findings"), list) else []:
        if not isinstance(row, dict) or type(row.get("start_line")) is not int or type(row.get("end_line")) is not int:
            continue
        container = next((item for item in allowed.values() if item["kind"] == "source_review"
            and item["relative_path"] == row.get("relative_path") and item["start_line"] <= row["start_line"]
            <= row["end_line"] <= item["end_line"] and _complete(state["findings"].get(item["candidate_id"]))), None)
        if container is None or any(candidate["kind"] == "formula" and
                candidate["relative_path"] == row["relative_path"] and candidate["start_line"] == row["start_line"]
                and candidate["end_line"] == row["end_line"] for candidate in state.get("items", [])):
            continue
        item = {**container, "kind": "formula", "start_line": row["start_line"], "end_line": row["end_line"]}
        item["candidate_id"] = _identifier(state["snapshot_id"], "additional", item["relative_path"], item["start_line"], item["end_line"])
        temporary = {"findings": {}, "additional_findings": {}, "rejected_count": 0, "reviewed_count": 0}
        count = accept_inventory_response(temporary, json.dumps({"inventory_findings": [
            {**row, "candidate_id": item["candidate_id"], "disposition": "included"}]}), pages, requested_items=[item])
        if count:
            if state.get("paged"):
                for record in temporary["findings"].values():
                    record["presentation_key"] = state.get("presentation_key")
            state["items"].append(item)
            state["findings"].update(temporary["findings"])
            state["additional_findings"].update(temporary["findings"])
            state["reviewed_count"] += 1
            accepted += 1
    if accepted and isinstance(value.get("answer"), str) and value["answer"].strip():
        state["last_narrative"] = value["answer"].strip()
        batch_key = hashlib.sha256(json.dumps(sorted(allowed)).encode()).hexdigest()
        state.setdefault("narratives", {})[batch_key] = value["answer"].strip()
    if state.get("paged"):
        state["review_pending"] = max(0,inventory_review_total(state)-state.get("review_cursor",0))
    return accepted


def inventory_report(state):
    if state.get("paged"):
        return inventory_store(state).page(state["store_handle"])
    pending = [{key: row[key] for key in ("candidate_id", "kind", "relative_path", "start_line", "end_line")}
        for row in state["items"] if not _complete(state["findings"].get(row["candidate_id"]))
        or row["candidate_id"] in state.get("presentation_pending", [])]
    findings = [state["findings"][row["candidate_id"]] for row in state["items"] if row["candidate_id"] in state["findings"]]
    findings.extend(row for key, row in state["additional_findings"].items() if key not in state["findings"])
    return {"schema": _SCHEMA, "snapshot_id": state["snapshot_id"], "complete": not pending,
        "scope": "fixed_snapshot_all_source_windows_and_arithmetic_occurrences",
        "file_count": state["file_count"], "candidate_count": len(state["items"]),
        "reviewed_count": len(state["items"]) - len(pending), "pending_count": len(pending),
        "remaining": pending, "findings": findings, "semantic_execution_verified": False}


def render_inventory(state, *, language="zh-CN"):
    if state.get("paged"):
        from response_language import localized_text
        count = len(state["items"]) - state["reviewed_count"] + len(state.get("presentation_pending", []))
        result = inventory_store(state).page(state["store_handle"])
        notice = localized_text(language,
            f"固定索引快照包含 {state['file_count']} 个源码文件；已核对 {state['reviewed_count']} 项，仍有 {count} 项待处理。已确认的 {result['total']} 处业务公式、适用条件和引用保存在完整分析清单中，可逐页查看或完整导出。",
            f"The fixed index snapshot contains {state['file_count']} source files. {state['reviewed_count']} review items are recorded and {count} remain. All {result['total']} confirmed business formulas, conditions and citations are retained in the complete inventory, available by page or full export.",
            f"固定索引快照包含 {state['file_count']} 個原始碼檔案；已核對 {state['reviewed_count']} 項，仍有 {count} 項待處理。已確認的 {result['total']} 處業務公式、適用條件和引用保存在完整分析清單中，可逐頁查看或完整匯出。")
        return "\n\n".join(filter(None, [notice, state.get("last_narrative", "")]))
    from response_language import localized_text
    def local(cn, en, hk):
        return localized_text(language, cn, en, hk)
    report = inventory_report(state)
    completed, total = report["reviewed_count"], report["candidate_count"]
    title = local(f"已核对固定索引快照中已纳入源码的 {completed}/{total} 项原文窗口与公式位置。",
        f"Reviewed {completed}/{total} source windows and formula positions included in the fixed index snapshot.",
        f"已核對固定索引快照中已納入原始碼的 {completed}/{total} 項原文窗口與公式位置。")
    chunks = [title]
    narratives = list(dict.fromkeys(state.get("narratives", {}).values()))
    chunks.extend(narratives)
    chunks.append(local("### 累计公式位置与业务规则", "### Cumulative formula locations and business rules", "### 累計公式位置與業務規則"))
    for row in report["findings"]:
        if row["disposition"] != "included" or row["candidate_id"] in state.get("presentation_pending", []):
            continue
        refs = "".join("[" + i + "]" for i in row["evidence_ids"])
        chunks.append(f"**{row['relative_path']}:{row['start_line']}-{row['end_line']}** — "
            f"{row['summary']} {row['formula']} {row['conditions']} {refs}")
    if report["pending_count"]:
        count = report["pending_count"]
        chunks.append(local(f"仍有 {count} 项待核对或分类；以上是已确认内容，尚不能作为全库完整清单。",
            f"{count} review items remain; this is an incomplete inventory.",
            f"仍有 {count} 項待核對或分類；以上是已確認內容，尚不能作為全庫完整清單。"))
        chunks.append("\n".join(f"- {row['relative_path']}:{row['start_line']}-{row['end_line']} ({row['kind']})"
                      for row in report["remaining"]))
    elif not any(row["disposition"] == "included" for row in report["findings"]):
        chunks.append(local("所选快照核对完成，未确认与本题相关的公式。",
            "No relevant formulas were confirmed in the reviewed snapshot.", "所選快照核對完成，未確認與本題相關的公式。"))
    return "\n\n".join(chunks)


def missing_deliveries(state, answer):
    """Require the accumulated business content, not a directory of citations."""
    if state.get("paged"):
        # Delivery is the immutable, fully accessible inventory; the short
        # answer explicitly identifies it as paginated, never as a full list.
        stored = inventory_store(state).page(state["store_handle"])
        return [] if stored["counts"]["reviewed"] == state["reviewed_count"] else ["inventory_store_not_committed"]
    def normalized(text):
        return "".join(str(text).split()).replace("**", "").replace("`", "")
    text = normalized(answer)
    return [row["candidate_id"] for row in inventory_records(state)
        if row["disposition"] == "included" and (
            f"{row['relative_path']}:{row['start_line']}-{row['end_line']}" not in answer or
            not any("[" + identifier + "]" in answer for identifier in row["evidence_ids"]) or
            any(normalized(row[key]) not in text for key in ("summary", "formula", "conditions")))]


def restore_inventory_deliveries(state, answer, *, language="zh-CN"):
    """Keep editorial paraphrases and append any lost canonical business rows.

    The revised answer still has to pass the ordinary independent source review;
    restoring a row does not establish that the original model claim was true.
    """
    missing = set(missing_deliveries(state, answer))
    if not missing:
        return answer
    from response_language import localized_text
    heading = localized_text(language, "### 累计公式位置与业务规则", "### Cumulative formula locations and business rules", "### 累計公式位置與業務規則")
    rows = [answer, heading]
    for row in inventory_records(state):
        if row["candidate_id"] in missing:
            citations = "".join("[" + identifier + "]" for identifier in row["evidence_ids"])
            rows.append(f"**{row['relative_path']}:{row['start_line']}-{row['end_line']}** — "
                f"{row['summary']} {row['formula']} {row['conditions']} {citations}")
    return "\n\n".join(rows)


def inventory_review_parts(answer):
    """Partition every output character for the existing strict source reviewer."""
    parts, current, separators = [], "", 0
    for line in answer.splitlines(keepends=True):
        while line:
            room = 8000 - len(current)
            current += line[:room]
            line = line[room:]
            if len(current) == 8000:
                parts.append(current)
                current, separators = "", 0
        if current and not current.splitlines()[-1].strip():
            separators += 1
            if separators == 16:
                parts.append(current)
                current, separators = "", 0
    if current:
        parts.append(current)
    return parts


def set_inventory_requirements(state, answer_language, answer_instructions):
    """Rephrase accepted findings without discarding the source investigation."""
    key = hashlib.sha256(json.dumps([answer_language, answer_instructions], ensure_ascii=False).encode()).hexdigest()
    previous = state.get("presentation_key")
    state["presentation_key"] = key
    if previous is None or previous == key:
        return
    state["presentation_pending"] = [identifier for identifier, row in state["findings"].items()
        if row["kind"] == "formula" and _complete(row)]
    state["previous_narratives"] = state.get("narratives", {})
    state["narratives"] = {}
    state["semantic_reviews"] = {}
    if state.get("paged"):
        state.update(review_cursor=0, review_part_offset=0, reviewed_batch_count=0,
            review_pending=inventory_review_total(state))


def inventory_revision_prompt(state, review):
    quotes = "\n".join(str(row.get("quote", "")) for row in review.get("findings", []))
    candidates = [row for row in inventory_records(state) if row["disposition"] == "included" and
        (f"{row['relative_path']}:{row['start_line']}-{row['end_line']}" in quotes or
         any(row[key] and row[key] in quotes for key in ("summary", "formula", "conditions")))]
    narrative_items = []
    if state.get("paged"):
        for identifier,text in state.get("narratives",{}).items():
            if identifier in quotes or any(quote and (quote in text or text in quote)
                    for quote in (str(row.get("quote","")) for row in review.get("findings",[]))):
                rows=inventory_store(state).narrative_page(state["store_handle"],narrative_id=identifier)["rows"]
                if rows:
                    narrative_items.append({"narrative_id":identifier,"text":text,
                        "evidence_ids":[page["evidence_id"] for page in rows[0]["pages"]]})
            if len(narrative_items)==4:
                break
    return {"items": candidates[:32], "narrative_items":narrative_items, "remaining_count": max(0, len(candidates) - 32),
        "response_schema": {"answer": "revised full business explanation", "inventory_corrections": [{
            "candidate_id": "an item ID above", "disposition": "included/excluded/uncertain",
            "summary": "corrected source-grounded business meaning", "formula": "correct formula",
            "conditions": "correct applicability", "evidence_ids": ["actual supplied source IDs"]}],
            "inventory_narrative_corrections":[{"narrative_id":"an ID from narrative_items",
                "text":"complete corrected business explanation retaining supported sections",
                "evidence_ids":["the original source IDs supplied for that explanation"]}]},
        "instruction": "Correct any erroneous inventory finding using actual source. Return JSON with answer and inventory_corrections. "
            "Correct a flagged earlier business explanation with inventory_narrative_corrections and keep its other supported content. "
            "Do not delete correct findings or merely preserve wrong formulas to satisfy coverage. "
            "Corrections replace the current finding but retain its earlier version in the local history; corrected text must be independently reviewed again."}


def apply_inventory_corrections(state, text, pages, requested_items, requested_narratives=()):
    try:
        document = json.loads(text)
    except (ValueError, TypeError):
        return state, text, []
    if (not isinstance(document, dict) or not isinstance(document.get("answer"), str)
            or not isinstance(document.get("inventory_corrections"), list)):
        return state, text, []
    updated = json.loads(json.dumps(state))
    allowed = {row["candidate_id"] for row in requested_items}
    old = {identifier: dict(state["findings"][identifier]) for identifier in allowed if identifier in state["findings"]}
    original_pending = set(updated.get("presentation_pending", []))
    updated["presentation_pending"] = list(original_pending | set(old))
    accept_inventory_response(updated, json.dumps({"inventory_findings": document["inventory_corrections"]}),
        pages, requested_items=requested_items)
    corrected = [identifier for identifier in old if updated["findings"][identifier] != old[identifier]]
    # Unchanged or rejected corrections must not create new presentation work.
    updated["presentation_pending"] = [identifier for identifier in updated.get("presentation_pending", [])
        if identifier in original_pending]
    for identifier in corrected:
        updated.setdefault("finding_history", {}).setdefault(identifier, []).append(old[identifier])
    if corrected:
        updated.setdefault("narrative_history", []).append(updated.get("narratives", {}))
        if not state.get("paged"):
            updated["narratives"] = {"semantic_revision": document["answer"]}
        updated["last_narrative"] = document["answer"]
    narrative_allowed={row["narrative_id"]:row for row in requested_narratives}
    supplied={page["evidence_id"] for page in pages}
    changed_narratives=[]
    for row in document.get("inventory_narrative_corrections",[]) if isinstance(document.get("inventory_narrative_corrections"),list) else []:
        if not isinstance(row,dict) or row.get("narrative_id") not in narrative_allowed:
            continue
        original=narrative_allowed[row["narrative_id"]]
        if (not isinstance(row.get("text"),str) or not row["text"].strip() or
            not isinstance(row.get("evidence_ids"),list) or not all(isinstance(i,str) for i in row["evidence_ids"]) or
            not set(original["evidence_ids"])<=set(row["evidence_ids"])<=supplied):
            continue
        if updated["narratives"].get(row["narrative_id"])!=row["text"].strip():
            updated["narratives"][row["narrative_id"]]=row["text"].strip()
            changed_narratives.append(row["narrative_id"])
    if changed_narratives:
        updated["_narratives_corrected"]=changed_narratives
        updated["last_narrative"]=document["answer"]
    return updated, document["answer"], corrected


def inventory_store(state):
    from repository_inventory_store import InventoryStore
    return InventoryStore(state["_store_database"])


def inventory_checkpoint(state):
    if not state.get("paged"):
        return state
    return {key: state[key] for key in ("schema", "scope_key", "snapshot_id", "paged", "store_handle", "catalog_builds")}


def persist_inventory(state, identifiers=(), pages=(), *, run_id=None, update_key=None, navigation=None):
    if state.get("paged"):
        state["store_handle"] = inventory_store(state).commit(state, identifiers, pages,
            run_id=run_id, update_key=update_key, navigation=navigation)


def inventory_records(state):
    return list(state["findings"].values())


def inventory_pending(state):
    return len(state["items"]) - state["reviewed_count"] + len(state.get("presentation_pending", []))


def render_inventory_rows(rows):
    return "\n\n".join(f"{row['candidate_id']} — **{row['relative_path']}:{row['start_line']}-{row['end_line']}** "
        + " ".join(str(row.get(key,"")) for key in ("disposition","summary","formula","conditions"))
        + " " + "".join("[" + identifier + "]" for identifier in row.get("evidence_ids",[])) for row in rows)


def inventory_review_total(state):
    return len(state["items"]) + len(state.get("narratives", {}))


def advance_inventory_review(state, batch):
    state.update(review_cursor=batch["next_cursor"] if batch["part_index"]+1 == batch["part_count"] else batch["cursor"],
        review_part_offset=0 if batch["part_index"]+1 == batch["part_count"] else batch["part_index"]+1)
    state["review_pending"] = max(0,inventory_review_total(state)-state["review_cursor"])


def inventory_review_batches(state):
    """Yield only unreviewed durable records and their exact archived source."""
    if not state.get("paged"):
        return
    store=inventory_store(state);cursor=state.get("review_cursor",0)
    while cursor < len(state["items"]):
        page=store.page(state["store_handle"],cursor=cursor,page_size=16,audit=True)
        records=[row for row in page["rows"] if row.get("disposition") in {"included","excluded","reviewed"}]
        if records:
            parts=inventory_review_parts(render_inventory_rows(records))
            for part_index,part in enumerate(parts):
                if cursor==state.get("review_cursor",0) and part_index<state.get("review_part_offset",0):
                    continue
                pages={}
                for row in records:
                    for index,identifier in enumerate(row.get("evidence_ids",[])):
                        pages[identifier]=store.evidence(state["store_handle"],row["candidate_id"],index)["spans"][0]
                yield {"text":part,"pages":list(pages.values()),"cursor":cursor,"part_index":part_index,
                    "part_count":len(parts),"next_cursor":page["next_cursor"] if page["next_cursor"] is not None else len(state["items"])}
        if page["next_cursor"] is None:
            break
        cursor=page["next_cursor"]

    cursor=max(0,state.get("review_cursor",0)-len(state["items"]))
    while cursor < len(state.get("narratives",{})):
        page=store.narrative_page(state["store_handle"],cursor=cursor,page_size=4)
        records=page["rows"]
        if not records:
            break
        text="\n\n".join("Business explanation "+row["narrative_id"]+"\n"+row["text"] for row in records)
        parts=inventory_review_parts(text)
        pages={source["evidence_id"]:source for row in records for source in row["pages"]}
        for part_index,part in enumerate(parts):
            logical_cursor=len(state["items"])+cursor
            if logical_cursor==state.get("review_cursor",0) and part_index<state.get("review_part_offset",0):
                continue
            yield {"text":part,"pages":list(pages.values()),"cursor":logical_cursor,"part_index":part_index,
                "part_count":len(parts),"narrative_ids":[row["narrative_id"] for row in records],
                "next_cursor":len(state["items"])+(page["next_cursor"] if page["next_cursor"] is not None else len(state["narratives"]))}
        if page["next_cursor"] is None:
            break
        cursor=page["next_cursor"]
