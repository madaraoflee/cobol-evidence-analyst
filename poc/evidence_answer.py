"""Useful, bounded answers from captured source when model prose is unavailable.

This module reads no files, performs no search and sends no model request. Its
output describes source syntax, never a completed investigation or execution.
"""

from __future__ import annotations

import hashlib
import re

from claim_ledger import _render_fact
from claim_support import supplied_statement_facts
from question_planner import question_references
from response_language import localized_text, resolve_answer_language
from statement_facts import data_references


_EVIDENCE_ID = re.compile(r"ev[_:-][^\]\r\n]{1,157}\Z")
_HASH = re.compile(r"[a-fA-F0-9]{64}\Z")
_MAX_PAGES = 24
_MAX_CHARACTERS = 64000
_MAX_FACTS = 8


def _valid_page(page):
    if not isinstance(page, dict):
        return False
    text, first, last = page.get("source_text"), page.get("start_line"), page.get("end_line")
    return (isinstance(text, str) and bool(text.strip()) and not page.get("span_truncated")
        and isinstance(page.get("relative_path"), str) and bool(page["relative_path"])
        and _EVIDENCE_ID.fullmatch(str(page.get("evidence_id", ""))) is not None
        and _HASH.fullmatch(str(page.get("source_sha256", ""))) is not None
        and type(first) is int and type(last) is int and 0 < first <= last
        and len(text.splitlines()) == last - first + 1)


def _matches(reference, name):
    """Bare fields nominate candidates; qualified and indexed fields stay exact."""
    requested, supplied = data_references(reference), data_references(name)
    return any(left.name == right.name
        and (not left.qualifiers or left.qualifiers == right.qualifiers)
        and (not left.subscripts or left.subscripts == right.subscripts)
        and (left.reference_modification is None or left.reference_modification == right.reference_modification)
        for left in requested for right in supplied)


def _explicit_root_path(question, plan, investigation, ranges, pages):
    """An exact, unique file question can nominate its captured statements.

    A file name inside another path, a fuzzy basename, or a narrower unresolved
    subject does not authorize widening the answer to every operation in a file.
    """
    paths = set(plan.get("paths") or [])
    if (len(paths) != 1 or plan.get("entities") or ranges or plan.get("source_scopes")
            or plan.get("file_entities") or plan.get("unresolved")
            or investigation.get("state") in {"unresolved", "ambiguous"}
            or (investigation.get("source_identity") or {}).get("status") in {"ambiguous", "not_found"}):
        return None
    path = next(iter(paths))
    if not isinstance(path, str) or not path or len({page["source_sha256"] for page in pages
            if page["relative_path"] == path}) != 1:
        return None
    token = (r"(?<![A-Za-z0-9_$#@./\\-])" + re.escape(path)
             + r"(?![A-Za-z0-9_$#@/\\-]|\.[A-Za-z0-9_$#@/\\-])")
    return path if re.search(token, str(question)) else None


def _relationship_root(plan, investigation, ranges, pages):
    """Consume a locally revalidated relationship subject, never a fuzzy path."""
    root = plan.get("relation_root")
    if ("call_relationships" not in plan.get("intents", ())
            or plan.get("relation_direction") not in {"outgoing", "incoming", "related"}
            or not isinstance(root, dict)
            or not all(isinstance(root.get(key), str) and root[key]
                       for key in ("relative_path", "source_sha256", "program_name"))
            or _HASH.fullmatch(root["source_sha256"]) is None
            or set(plan.get("paths", ())) != {root["relative_path"]}
            or plan.get("unresolved") or investigation.get("state") in {"unresolved", "ambiguous"}
            or (investigation.get("source_identity") or {}).get("status") in {"ambiguous", "not_found"}):
        return None
    versions = {page["source_sha256"] for page in pages if page["relative_path"] == root["relative_path"]}
    if versions != {root["source_sha256"]}:
        return None
    scoped = [scope for scope in ranges if scope.get("relative_path") == root["relative_path"]
              and scope.get("exclusive") is not False]
    if any(scope.get("source_sha256", root["source_sha256"]) != root["source_sha256"]
           or type(scope.get("start_line")) is not int or type(scope.get("end_line")) is not int
           or not 0 < scope["start_line"] <= scope["end_line"] for scope in scoped):
        return None
    return {**root, "ranges": scoped}


def _call_facts(page, source_format, compilation_context):
    """Use the structural front end for actual CALL/PERFORM occurrences.

    A literal call is still only a request. Neither resolution nor scheduler
    execution follows from finding its syntax in the captured caller.
    """
    from structural_index import SourceDocument, normalize_cobol_lines, parse_document

    text, first = page["source_text"], page["start_line"]
    lines, hint = normalize_cobol_lines(text, source_format)
    parsed = parse_document(SourceDocument(page["relative_path"], page["source_sha256"],
        "utf-8", False, hint, "COBOL", tuple(text.splitlines()), lines),
        compilation_context=compilation_context)
    units = {unit.unit_id: unit for unit in parsed.code_units}
    guards, conditional = {}, {}
    for relation in parsed.relations:
        if relation.relation_type == "CONTROL_DEPENDS_ON" and relation.status == "confirmed":
            condition = units.get(relation.target_entity_id)
            if condition is not None:
                guards.setdefault(relation.from_entity_id, []).append({
                    "condition": condition.normalized_text, "outcome": relation.metadata.get("outcome")})
        if relation.metadata.get("compilation_status") == "candidate":
            conditional[relation.from_entity_id] = relation.metadata.get("compilation_guards", [])
    facts = []
    for relation in parsed.relations:
        unit = units.get(relation.from_entity_id)
        if (relation.relation_type not in {"CALLS", "PERFORMS"} or unit is None
                or unit.unit_type != "Statement" or unit.parse_status not in {"complete", "candidate"}
                or not relation.target_name):
            continue
        facts.append({"predicate": "call_statement", "target": relation.target_name,
            "statement_kind": unit.name, "statement": unit.normalized_text,
            "program_name": unit.program_name, "occurrence_id": unit.unit_id,
            "start_line": first + unit.start_line - 1, "end_line": first + unit.end_line - 1,
            "guards": guards.get(unit.unit_id, []),
            "compilation_status": "candidate" if unit.unit_id in conditional else "effective_source",
            "compilation_guards": conditional.get(unit.unit_id, []),
            "call_form": relation.metadata.get("call_form"),
            "support_scope": "source_call_statement_syntax", "execution_verified": False})
    return facts


def _incoming_relation_matches(item, fact, page, root):
    """A located graph edge still needs its complete, same-version CALL text."""
    return (root is not None and item.get("kind") == "calls" and item.get("status") == "SATISFIED"
        and item.get("relation_direction") == "incoming" and item.get("target_path") == root["relative_path"]
        and item.get("relative_path") == page["relative_path"]
        and item.get("source_sha256") == page["source_sha256"]
        and item.get("target_name", "").casefold() == root["program_name"].casefold()
        and fact.get("target", "").casefold() == root["program_name"].casefold()
        and item.get("start_line") == fact["start_line"] and item.get("end_line") == fact["end_line"]
        and page["evidence_id"] in item.get("evidence_ids", []))


def _render_call(fact, *, answer_language="zh-CN"):
    guards = [((localized_text(answer_language, "条件不成立：", "false condition: ", "條件不成立：")
        if guard.get("outcome") in {False, "false"} else "")
        + guard["condition"]) for guard in fact.get("guards", [])]
    prefix = localized_text(answer_language,
        "在片段中可见的控制条件 `" + "；".join(guards) + "` 下，",
        "Under the control conditions visible in this excerpt, `" + "; ".join(guards) + "`, ",
        "在片段中可見的控制條件 `" + "；".join(guards) + "` 下，") if guards else ""
    if fact.get("conditions_status") == "not_fully_verified":
        prefix = localized_text(answer_language, "该调用的控制条件尚未完整核对；",
            "The control conditions of this call have not been fully checked; ",
            "該呼叫的控制條件尚未完整核對；") + prefix
    if fact.get("compilation_status") == "candidate":
        prefix = localized_text(answer_language, "编译条件尚未确认，以下仅为候选源码语句：",
            "Compilation conditions are unconfirmed; only a candidate source statement follows: ",
            "編譯條件尚未確認，以下僅為候選源碼語句：") + prefix
    statement = fact["statement"]
    location = f"{fact['relative_path']}:{fact['start_line']}"
    return (prefix + localized_text(answer_language,
        f"调用点写有 `{statement}`；这是程序内的调用请求，实际调用是否发生及返回效果仍需执行路径和被调用实现支持（{location}）",
        f"the call site contains `{statement}`, requesting a call within the program; whether the call runs and what it returns still require evidence from the execution path and called implementation ({location})",
        f"呼叫點寫有 `{statement}`；這是程式內的呼叫請求，實際呼叫是否發生及傳回效果仍需執行路徑和被呼叫實作支持（{location}）") + "".join(
            "[" + identifier + "]" for identifier in dict.fromkeys(
                [fact["evidence_id"], *fact.get("guard_evidence_ids", [])])))


def _same_instance(left, right):
    return (all(left.get(key, []) == right.get(key, []) for key in ("include_chain", "replacement_trace"))
            and left.get("copy_instance") == right.get("copy_instance"))


def _bound_caller_matches(caller, fact, page):
    return (caller.get("relative_path") == page["relative_path"]
        and caller.get("source_sha256") == page["source_sha256"]
        and type(caller.get("line")) is int and caller["line"] == fact["start_line"]
        and caller.get("occurrence_id") and caller.get("target_name") == fact.get("target")
        and caller.get("binding_confirmed") is True
        and page["evidence_id"] in caller.get("evidence_ids", []))


def _bound_call_conditions(fact, caller, page, pages, source_format, compilation_context):
    """Reparse the supplied physical guard-to-call span, never trust flags alone.

    A fragmented CALL has no independent empty-scope guarantee. Every claimed
    guard needs real, same-instance evidence and contiguous source through this
    call; missing gaps or conflicting guard metadata remain explicit unknowns.
    """
    if caller.get("compilation_status") != "active":
        fact = {**fact, "compilation_status": "candidate"}
    expected = caller.get("conditions", [])
    if not expected:
        return {**fact, "bound_occurrence_id": caller["occurrence_id"],
                "conditions_status": "not_fully_verified" if caller.get("condition_candidate_count", 0)
                    or caller.get("scope_known") is not True else "source_statement_only"}
    if not caller.get("conditions_supplied") or caller.get("scope_known") is not True:
        return {**fact, "conditions_status": "not_fully_verified"}
    starts, guard_ids = [], []
    for guard in expected:
        if not isinstance(guard, dict):
            return {**fact, "conditions_status": "not_fully_verified"}
        span = guard.get("span") or {"start_line": guard.get("source_line"), "end_line": guard.get("source_line")}
        first, last = span.get("start_line"), span.get("end_line")
        if type(first) is not int or type(last) is not int or not 0 < first <= last <= fact["end_line"]:
            return {**fact, "conditions_status": "not_fully_verified"}
        selector = guard.get("selector_span") or {}
        first = min(first, selector.get("start_line", first))
        starts.append(first)
        identifiers = guard.get("evidence_ids", [])
        needed = set(range(first, last + 1))
        for identifier in identifiers:
            supplied = pages.get(identifier)
            if (supplied is None or supplied["relative_path"] != page["relative_path"]
                    or supplied["source_sha256"] != page["source_sha256"] or not _same_instance(supplied, page)):
                return {**fact, "conditions_status": "not_fully_verified"}
            needed -= set(range(supplied["start_line"], supplied["end_line"] + 1))
            guard_ids.append(identifier)
        if needed:
            return {**fact, "conditions_status": "not_fully_verified"}
    first, last = min(starts), fact["end_line"]
    physical, witnesses = {}, []
    for identifier, supplied in pages.items():
        if (supplied["relative_path"] != page["relative_path"] or supplied["source_sha256"] != page["source_sha256"]
                or not _same_instance(supplied, page)):
            continue
        contributed = False
        for line, text in enumerate(supplied["source_text"].splitlines(), supplied["start_line"]):
            if first <= line <= last:
                if line in physical and physical[line] != text:
                    return {**fact, "conditions_status": "not_fully_verified"}
                physical[line], contributed = text, True
        if contributed:
            witnesses.append(identifier)
    if len(physical) != last - first + 1:
        return {**fact, "conditions_status": "not_fully_verified"}
    span_page = {**page, "start_line": first, "end_line": last,
                 "source_text": "\n".join(physical[line] for line in range(first, last + 1))}
    parsed = [candidate for candidate in _call_facts(span_page, source_format, compilation_context)
              if candidate["start_line"] == fact["start_line"] and candidate["target"] == fact["target"]]
    if len(parsed) != 1:
        return {**fact, "conditions_status": "not_fully_verified"}
    from statement_facts import lex_cobol
    def condition(text):
        values = tuple(token.value for token in lex_cobol(str(text)))
        return values[1:] if values[:1] == ("IF",) else values
    def outcome(value):
        return True if value is True or value == "true" else False if value is False or value == "false" else None
    actual_guards = parsed[0].get("guards", [])
    if not all(any(condition(guard.get("condition")) == condition(actual.get("condition"))
                   and outcome(guard.get("outcome")) is not None
                   and outcome(guard.get("outcome")) is outcome(actual.get("outcome"))
                   for actual in actual_guards) for guard in expected):
        return {**fact, "conditions_status": "not_fully_verified"}
    return {**fact, "guards": actual_guards, "guard_evidence_ids": list(dict.fromkeys([*guard_ids, *witnesses])),
            "bound_occurrence_id": caller["occurrence_id"], "conditions_status": "supplied_source_scope"}


def build_evidence_answer(question, source_pages, investigation, *, source_format="auto", compilation_context=None,
                          answer_language=None):
    """Return a PARTIAL source answer, or None when no relevant fact is bound.

    The caller must pass captured pages, retain their versions and run its usual
    claim ledger. A source hash identifies the original file, not the UTF-8
    re-encoding of an excerpt. Duplicate evidence IDs with conflicting content
    are rejected rather than selecting one version.
    """
    answer_language = answer_language or resolve_answer_language(question)
    investigation = investigation if isinstance(investigation, dict) else {}
    plan = investigation.get("analysis_plan") or {}
    ranges = (investigation.get("source_scope") or {}).get("ranges") or plan.get("source_scopes") or []
    references = list(dict.fromkeys([*question_references(question), *plan.get("entities", [])]))
    root_paths = set(plan.get("paths") or [])
    paths = set(root_paths)
    intents = set(plan.get("intents") or [])
    activation_only = "activation" in intents and not intents & {"calculation", "origin", "overwrite", "impact", "exception"}
    bound_callers = (investigation.get("activation_evidence") or {}).get("callers", [])
    # These callers were bound by the local investigation, rather than merely
    # retrieved near the root. A supplied ID and version must still match.
    valid, conflicts = {}, set()
    for page in source_pages:
        if not _valid_page(page):
            continue
        identifier = page["evidence_id"]
        if identifier in valid and any(valid[identifier].get(key) != page.get(key) for key in
                ("relative_path", "source_sha256", "start_line", "end_line", "source_text", "include_chain",
                 "replacement_trace", "copy_instance")):
            conflicts.add(identifier)
        else:
            valid[identifier] = page
    valid = {identifier: page for identifier, page in valid.items() if identifier not in conflicts}
    explicit_root_path = _explicit_root_path(question, plan, investigation, ranges, valid.values())
    relationship_requested = "call_relationships" in intents
    relationship_root = _relationship_root(plan, investigation, ranges, valid.values())
    relationship_direction = plan.get("relation_direction")
    relationship_only = relationship_requested and not intents.intersection({
        "calculation", "accumulation", "origin", "overwrite", "condition", "exception", "file_mapping"})
    relation_items = [item for item in investigation.get("required_items", [])
        if isinstance(item, dict) and relationship_root is not None and item.get("kind") == "calls"
        and item.get("status") == "SATISFIED" and item.get("relation_direction") == "incoming"
        and item.get("target_path") == relationship_root["relative_path"]]
    paths.update(item["relative_path"] for item in relation_items if isinstance(item.get("relative_path"), str))
    for caller in bound_callers:
        if (caller.get("binding_confirmed") is True and caller.get("occurrence_id")
                and type(caller.get("line")) is int and any(page["relative_path"] == caller.get("relative_path")
                    and page["source_sha256"] == caller.get("source_sha256")
                    and page["evidence_id"] in caller.get("evidence_ids", [])
                    and page["start_line"] <= caller["line"] <= page["end_line"] for page in valid.values())):
            paths.add(caller["relative_path"])
    pages = [page for identifier, page in valid.items() if identifier not in conflicts
             and (not paths or page["relative_path"] in paths)]
    candidates, characters, examined = [], 0, 0
    for page in pages:
        if examined >= _MAX_PAGES or characters + len(page["source_text"]) > _MAX_CHARACTERS:
            continue
        characters += len(page["source_text"])
        examined += 1
        try:
            facts = supplied_statement_facts(page, source_format=source_format, compilation_context=compilation_context)
            facts.extend(_call_facts(page, source_format, compilation_context))
        except (ValueError, TypeError, KeyError):
            # Unsupported or malformed source cannot become a guessed answer.
            continue
        for fact in facts:
            if relationship_only and fact.get("predicate") != "call_statement":
                continue
            if activation_only and fact.get("predicate") != "call_statement":
                continue
            if root_paths and page["relative_path"] not in root_paths and fact.get("predicate") != "call_statement":
                continue
            fact = {**fact, "evidence_id": page["evidence_id"], "relative_path": page["relative_path"],
                    "source_sha256": page["source_sha256"], "execution_verified": False}
            if relationship_requested:
                exclusive = [scope for scope in ranges if scope.get("relative_path") == page["relative_path"]
                             and scope.get("exclusive") is not False]
                if any(scope.get("source_sha256", page["source_sha256"]) != page["source_sha256"]
                       or type(scope.get("start_line")) is not int or type(scope.get("end_line")) is not int
                       or not 0 < scope["start_line"] <= scope["end_line"] for scope in exclusive):
                    continue
                if exclusive and not any(scope["start_line"] <= fact["start_line"]
                                         and fact["end_line"] <= scope["end_line"] for scope in exclusive):
                    continue
            path_ranges = [scope for scope in ranges if scope.get("relative_path") == page["relative_path"]
                           and scope.get("source_sha256", page["source_sha256"]) == page["source_sha256"]
                           and scope.get("exclusive") is not False]
            if path_ranges and not any(type(scope.get("start_line")) is int and type(scope.get("end_line")) is int
                    and scope["start_line"] <= fact["start_line"] <= scope["end_line"] for scope in path_ranges):
                continue
            relationship_match = False
            if fact.get("predicate") == "call_statement":
                matched = [caller for caller in bound_callers if _bound_caller_matches(caller, fact, page)]
                relation_matched = any(_incoming_relation_matches(item, fact, page, relationship_root)
                                       for item in relation_items)
                if root_paths and page["relative_path"] not in root_paths and not (matched or relation_matched):
                    continue
                if len(matched) == 1:
                    fact["_bound_caller"] = matched[0]
                if relationship_requested:
                    if relationship_root is None or fact.get("statement_kind") != "CALL":
                        continue
                    root = relationship_root
                    outgoing = (page["relative_path"] == root["relative_path"]
                        and page["source_sha256"] == root["source_sha256"]
                        and not any(page.get(key) for key in ("include_chain", "replacement_trace", "copy_instance"))
                        and (not fact.get("program_name") or fact["program_name"].casefold() == root["program_name"].casefold())
                        and (not root["ranges"] or any(scope["start_line"] <= fact["start_line"]
                            and fact["end_line"] <= scope["end_line"] for scope in root["ranges"])))
                    incoming = ((len(matched) == 1 or relation_matched)
                        and fact.get("target", "").casefold() == root["program_name"].casefold())
                    relationship_match = ((relationship_direction in {"outgoing", "related"} and outgoing)
                        or (relationship_direction in {"incoming", "related"} and incoming))
                    if not relationship_match:
                        continue
                    fact["relationship_direction"] = "outgoing" if outgoing and relationship_direction != "incoming" else "incoming"
                    if not fact.get("program_name") and not matched:
                        fact["conditions_status"] = "not_fully_verified"
            target = fact.get("target") or ""
            inputs = " ".join(fact.get("expression") or fact.get("condition") or [])
            target_match = any(_matches(reference, target) for reference in references)
            input_match = any(_matches(reference, inputs) for reference in references)
            program_match = fact.get("program_name") and fact["program_name"] in references
            scoped = bool(path_ranges) or page["relative_path"] == explicit_root_path
            if activation_only and not (target_match or relationship_match):
                continue
            if not (target_match or input_match or program_match or scoped or relationship_match):
                continue
            score = 3 if target_match else 2 if input_match else 1
            candidates.append((score, fact))
    if not candidates:
        return None
    # Retain physical occurrences and both sides of a qualified-name ambiguity.
    # Do not collapse equal formulas in different branches or COPY instances.
    selected, seen = [], set()
    for _, fact in sorted(candidates, key=lambda row: (-row[0], row[1]["relative_path"], row[1]["start_line"], row[1]["occurrence_id"])):
        page = valid[fact["evidence_id"]]
        key = (fact["relative_path"], fact["source_sha256"], fact["occurrence_id"], fact["start_line"],
               fact.get("target"), repr(page.get("include_chain")), repr(page.get("copy_instance")))
        if key not in seen:
            selected.append(fact)
            seen.add(key)
        if len(selected) == _MAX_FACTS:
            break
    selected.sort(key=lambda fact: (fact["relative_path"], fact["start_line"], fact["occurrence_id"]))
    # Reparse only the bounded selected occurrences, rather than reparsing a
    # growing guard-to-call prefix for every retrieved call in a large source.
    for index, fact in enumerate(selected):
        caller = fact.pop("_bound_caller", None)
        if caller is not None:
            try:
                selected[index] = _bound_call_conditions(fact, caller, valid[fact["evidence_id"]],
                                                         valid, source_format, compilation_context)
            except (ValueError, TypeError, KeyError):
                selected[index] = {**fact, "conditions_status": "not_fully_verified",
                    "compilation_status": "candidate" if caller.get("compilation_status") != "active"
                        else fact.get("compilation_status")}
    identifiers = list(dict.fromkeys(identifier for fact in selected
                                    for identifier in [fact["evidence_id"], *fact.get("guard_evidence_ids", [])]))
    citations = [{"kind": "source_page", **{key: valid[identifier][key] for key in
        ("evidence_id", "relative_path", "start_line", "end_line", "source_sha256", "include_chain", "copy_instance")
        if key in valid[identifier]}} for identifier in identifiers]
    unknowns = [localized_text(answer_language,
        "以上说明仅覆盖已读取版本中的直接语句；这些语句本身不能证明实际执行路径，最终结果尚未确认。",
        "These observations cover only direct statements in the captured source versions; the statements alone do not establish actual execution paths, and final results remain unconfirmed.",
        "以上說明僅涵蓋已讀取版本中的直接語句；這些語句本身不能證明實際執行路徑，最終結果尚未確認。")]
    if any(fact["predicate"] == "call_statement" for fact in selected):
        unknowns.append(localized_text(answer_language,
            "这些调用点不能证明外部调度配置、实际运行时间或真实调用效果，仍需对应调度入口和实现证据。",
            "These call sites do not establish external scheduling configuration, actual run times, or call outcomes; the corresponding scheduling entry points and implementations still need evidence.",
            "這些呼叫點不能證明外部排程設定、實際執行時間或真實呼叫效果，仍需對應排程入口和實作證據。"))
    if relationship_requested:
        unknowns.append(localized_text(answer_language,
            "已列出的是本次原文可见的调用点，不代表完整的上下游程序清单。",
            "These are call sites visible in the captured source, not a complete inventory of upstream and downstream programs.",
            "已列出的是本次原文可見的呼叫點，不代表完整的上下游程式清單。"))
    if len(candidates) > len(selected):
        unknowns.append(localized_text(answer_language,
            "当前仅展示与问题最相关的部分语句，完整来源和影响链仍待继续核对。",
            "Only the statements most relevant to the question are shown; the complete source and impact chains still need to be checked.",
            "目前僅展示與問題最相關的部分語句，完整來源和影響鏈仍待繼續核對。"))
    lines = [_render_call(fact, answer_language=answer_language) if fact["predicate"] == "call_statement"
             else _render_fact(fact, answer_language=answer_language) for fact in selected]
    answer = (localized_text(answer_language,
        "模型说明尚未完成，已读取的源码仍可直接确认以下内容：",
        "The model explanation is incomplete, but the captured source directly supports the following:",
        "模型說明尚未完成，已讀取的源碼仍可直接確認以下內容：") + "\n\n"
        + "\n\n".join("- " + line + localized_text(answer_language, "。", ".") for line in lines)
        + "\n\n" + " ".join(unknowns))
    return {"answer": answer, "answer_origin": "local_source_facts", "answer_available": True,
        "model_answer_recorded": False, "status": "PARTIAL", "semantic_scope": "supplied_source_statement_syntax",
        "claims_semantically_verified": False, "execution_verified": False, "facts": selected, "unknowns": unknowns,
        "source_ids": identifiers, "evidence_ids": identifiers, "citations": citations, "evidence_refs": citations,
        "manifest": {"source_ids": identifiers, "framework_ids": [], "framework_facts": []},
        "source_versions": [{"relative_path": page["relative_path"], "source_sha256": page["source_sha256"],
            "evidence_id": identifier, "excerpt_sha256": hashlib.sha256(page["source_text"].encode()).hexdigest()}
            for identifier in identifiers for page in [valid[identifier]]],
        "selection": {"examined_pages": examined, "examined_characters": characters,
                      "selected_facts": len(selected), "maximum_facts": _MAX_FACTS}}
