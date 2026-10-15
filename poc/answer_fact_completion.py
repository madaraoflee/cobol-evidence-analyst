"""Add omitted, question-scoped source facts without another model request.

This is a bounded rendering service, not an answer gate or an execution engine.
It uses only the source versions actually supplied to the answer. A missing or
unsupported fact leaves the existing narrative available and is recorded locally.
"""

from __future__ import annotations

import hashlib
import re

from claim_ledger import _assertions
from claim_support import supplied_statement_facts
from statement_facts import CobolLexError, data_references, io_clause_guards, lex_cobol

VERSION = "source-fact-completion/v2"
_MAX_ADDITIONS = 8
_MAX_SCAN = 160000


def _covered(assertion, answer):
    """Recognize exact supported notation and leave other prose to the model."""
    for actual in _assertions(answer):
        if all(actual.get(key) == value for key, value in assertion.items()
               if key in {"predicate", "target", "expression", "condition", "effect"}):
            return True
    return False


def _parsed(page, source_format, context):
    from structural_index import SourceDocument, normalize_cobol_lines, parse_document
    source = page.get("source_text", "")
    if (not isinstance(source, str) or not source or page.get("span_truncated")
            or not isinstance(page.get("start_line"), int) or not isinstance(page.get("end_line"), int)
            or len(source.splitlines()) != page["end_line"] - page["start_line"] + 1
            or not re.fullmatch(r"[a-fA-F0-9]{64}", str(page.get("source_sha256", "")))):
        return None
    lines, hint = normalize_cobol_lines(source, source_format)
    document = SourceDocument(page["relative_path"], page["source_sha256"], "utf-8", False,
                              hint, "COBOL", tuple(source.splitlines()), lines)
    return parse_document(document, compilation_context=context)


def _guard_description(guard):
    """Render guard roles without turning non-boolean branch labels into false."""
    condition, outcome = str(guard.get("condition", "")), guard.get("outcome")
    kind = str(guard.get("kind", "")).upper()
    if kind == "IF":
        if outcome is True or outcome == "true":
            return "`" + condition + "` 成立"
        if outcome is False or outcome == "false":
            return "`" + condition + "` 不成立"
    if kind in {"WHEN", "EVALUATE_WHEN"} or condition.upper().startswith("WHEN "):
        selector = guard.get("selector")
        evaluate = guard.get("evaluate_source") or ("EVALUATE " + str(selector) if selector else None)
        prefix = "`" + evaluate + "` 的 " if evaluate else ""
        prior = guard.get("prior_when_sources") or guard.get("prior_alternatives")
        return prefix + "`" + condition + "` 分支" + ("（此前 WHEN 均未命中）" if prior else "")
    return "`" + condition + "` 源码分支"


def _unit_guards(parsed, by_id, unit_id):
    guards = []
    for relation in parsed.relations:
        if relation.relation_type != "CONTROL_DEPENDS_ON" or relation.from_entity_id != unit_id:
            continue
        condition = by_id.get(relation.target_entity_id)
        if relation.status != "confirmed" or condition is None or condition.parse_status != "complete":
            return None
        guards.append({"kind": condition.name, "condition": condition.normalized_text,
                       **relation.metadata})
    return guards


def _condition_descriptions(guards, io_guards, line):
    clauses = [guard for guard in io_guards if guard["start_line"] <= line <= guard["end_line"]]
    descriptions = [f"`{guard['operation_text'] or guard['operation']}` 的 {guard['condition']} 分支"
                    for guard in sorted(clauses, key=lambda guard: guard["operation_line"])]
    descriptions.extend(_guard_description(guard) for guard in guards)
    return descriptions


def _io_conditions_complete(io_facts, line, end_line=None):
    end_line = line if end_line is None else end_line
    for scope in io_facts:
        if (scope["kind"] not in {"io_operation_scope", "io_scope_unknown"}
                or scope["end_line"] < line or scope["start_line"] > end_line):
            continue
        if scope["kind"] == "io_scope_unknown" or not scope.get("closed"):
            return False
        if any(line <= boundary <= end_line for boundary in scope.get("ambiguous_lines", ())):
            return False
        matching = [guard for guard in io_facts if guard["kind"] == "io_clause"
                    and guard["operation_line"] == scope["operation_line"]
                    and guard["start_line"] <= line <= end_line <= guard["end_line"]]
        if len(matching) != 1:
            return False
    return True


def complete_supplied_answer(question, answer, investigation, source_pages, *, source_format="auto",
                             compilation_context=None, answer_detail="detailed", source_fact_cache=None):
    """Return the preserved answer plus narrowly selected omitted source facts.

    Selection requires locally bound procedure/occurrence scopes. Whole-program
    source supply does not turn every statement into an answer obligation.
    """
    report = {"version": VERSION, "mode": "non_blocking_local_source_rendering",
              "model_requests": 0, "answer_preserved": True, "added_facts": [],
              "preservation_scope": "source_completion_before_claim_review",
              "input_answer_sha256": hashlib.sha256(str(answer).encode()).hexdigest(),
              "scanned_characters": 0, "omitted_candidate_count": 0,
              "execution_verified": False, "status": "unchanged"}
    if not str(answer).strip():
        return answer, report
    plan = investigation.get("analysis_plan") or {}
    intents = set(plan.get("intents", ()))
    scopes = [scope for scope in (investigation.get("source_scope") or {}).get("ranges", ())
              if scope.get("exclusive") is not False and scope.get("source_sha256")]
    relevant = intents.intersection({"calculation", "accumulation", "overwrite", "condition", "exception", "flow", "explanation"})
    from business_synthesis import wants_business_detail
    detailed = wants_business_detail(question, answer_detail=answer_detail)
    necessary_short = intents.intersection({"accumulation", "overwrite", "exception"})
    if not scopes or not relevant or not (detailed or necessary_short):
        report["status"] = "scope_not_applicable"
        return answer, report
    from compilation_context import resolve_compilation_context
    context = resolve_compilation_context(compilation_context, source_options={"source_format": source_format})
    candidates, seen = [], set()
    pages = sorted(source_pages, key=lambda page: (page.get("relative_path", ""), page.get("start_line", 0)))
    for page in pages:
        limits = [scope for scope in scopes if scope.get("relative_path") == page.get("relative_path")
                  and scope["source_sha256"] == page.get("source_sha256")
                  and page.get("start_line", 0) <= scope["start_line"]
                  and page.get("end_line", 0) >= scope["end_line"]]
        if not limits:
            continue
        size = len(page.get("source_text", ""))
        if report["scanned_characters"] + size > _MAX_SCAN:
            report["omitted_candidate_count"] += 1
            continue
        report["scanned_characters"] += size
        try:
            parsed = _parsed(page, source_format, context)
        except (ValueError, RecursionError):
            report["omitted_candidate_count"] += 1
            continue
        if parsed is None:
            continue
        base = page["start_line"] - 1
        by_id = {unit.unit_id: unit for unit in parsed.code_units}
        def in_scope(first, last):
            return any(scope["start_line"] <= base + first <= base + last <= scope["end_line"] for scope in limits)
        facts = supplied_statement_facts(page, source_format=source_format, compilation_context=context,
                                        parsed_document=parsed)
        if isinstance(source_fact_cache, dict):
            source_fact_cache[(page.get("relative_path"), page.get("source_sha256"), page.get("start_line"),
                page.get("end_line"), page.get("source_text"), context.fingerprint)] = facts
        selected = [fact for fact in facts if fact.get("predicate") == "assignment_statement"
                    and any(scope["start_line"] <= fact["start_line"] <= fact["end_line"] <= scope["end_line"] for scope in limits)
                    and fact.get("compilation_status") != "candidate"]
        arithmetic = [fact for fact in selected if fact["statement_kind"] != "MOVE"]
        # Rejection/overwrite questions need the other constants written to the
        # same result field, including the preceding I/O error branch.
        named_targets = {item.get("target") for item in _assertions(answer)}
        named_targets.update(plan.get("entities", ()))
        selected = [fact for fact in selected if fact in arithmetic and intents.intersection({"calculation", "accumulation"})
                    or (fact["target"] in named_targets or not named_targets and intents.intersection({"exception", "flow"}))
                    and intents.intersection({"overwrite", "condition", "exception", "flow", "explanation"})]
        io_facts = io_clause_guards(parsed.document.lines, include_operation_scopes=True)
        io_guards = [fact for fact in io_facts if fact["kind"] == "io_clause"]
        input_fields = {ref.identity for fact in arithmetic for ref in data_references(" ".join(fact["expression"]))}
        targets = {fact["target"] for fact in arithmetic if fact["statement_kind"] == "COMPUTE"}
        for fact in selected:
            guards = _unit_guards(parsed, by_id, fact["occurrence_id"])
            if guards is None or not _io_conditions_complete(io_facts, fact["start_line"] - base, fact["end_line"] - base):
                targets.discard(fact["target"])
                report["omitted_candidate_count"] += 1
                continue
            assertion = {key: fact[key] for key in ("predicate", "target", "expression", "rounded")}
            if _covered(assertion, answer):
                continue
            # The claim ledger will locally repair a wrong explicit formula.
            # Supply its missing downstream facts without duplicating the fix.
            if (fact["statement_kind"] == "COMPUTE"
                    and sum(row["target"] == fact["target"] for row in arithmetic) == 1
                    and any(row.get("predicate") == "assignment_statement" and row.get("target") == fact["target"]
                            for row in _assertions(answer))):
                continue
            formula = fact["target"] + " = " + " ".join(fact["expression"])
            prefix = "原文还给出 "
            conditions = _condition_descriptions(guards, io_guards, fact["start_line"] - base)
            if conditions:
                prefix = "在 " + " 且 ".join(conditions) + "时，"
            if fact.get("normal_result_condition") == "no_size_error":
                prefix += "无 SIZE ERROR 时的算式为 "
            candidates.append((page, fact["start_line"], fact["end_line"], "assignment", formula,
                               prefix + "`" + formula + "`"))
        # A calculation's directly following local invocation is a visible
        # operation request; it does not prove that the operation succeeds.
        if arithmetic and intents.intersection({"calculation", "accumulation"}):
            last_arithmetic = max(fact["end_line"] for fact in arithmetic)
            for unit in parsed.code_units:
                if (unit.unit_type == "Statement" and unit.name in {"PERFORM", "CALL"}
                        and unit.parse_status == "complete" and in_scope(unit.start_line, unit.end_line)
                        and base + unit.start_line > last_arithmetic):
                    try:
                        tokens = lex_cobol(unit.normalized_text)
                    except CobolLexError:
                        continue
                    if len(tokens) > 1 and tokens[1].value not in {"UNTIL", "VARYING", "WITH"}:
                        target = tokens[1].value.strip("'\"")
                        if target.upper() not in answer.upper():
                            guards = _unit_guards(parsed, by_id, unit.unit_id)
                            if guards is None or not _io_conditions_complete(io_facts, unit.start_line, unit.end_line):
                                report["omitted_candidate_count"] += 1
                                continue
                            conditions = _condition_descriptions(guards, io_guards, unit.start_line)
                            prefix = "在 " + " 且 ".join(conditions) + "时，" if conditions else "随后"
                            candidates.append((page, base + unit.start_line, base + unit.end_line,
                                "following_invocation", unit.normalized_text,
                                prefix + "发起 `" + unit.normalized_text.rstrip(".") + "` 调用"))
            # Incoming conditions are linked to the exact local procedure and
            # formula inputs, avoiding unrelated outer end-of-file checks.
            names = {scope.get("procedure_name") for scope in limits}
            declarations = [unit for unit in parsed.code_units if unit.unit_type in {"Paragraph", "Section"}
                            and unit.name in names and unit.parse_status == "complete"]
            incoming = {relation.from_entity_id for relation in parsed.relations
                        if relation.relation_type == "PERFORMS" and relation.target_name in names
                        and relation.status != "candidate" and relation.from_entity_id in by_id
                        and sum(unit.name == relation.target_name and unit.program_name ==
                                by_id[relation.from_entity_id].program_name for unit in declarations) == 1}
            for relation in parsed.relations:
                if relation.relation_type != "CONTROL_DEPENDS_ON" or relation.status != "confirmed" or relation.from_entity_id not in incoming:
                    continue
                guard, call = by_id.get(relation.target_entity_id), by_id.get(relation.from_entity_id)
                if guard is None or call is None or guard.parse_status != "complete":
                    continue
                refs = {ref.identity for ref in data_references(guard.normalized_text)}
                if input_fields.intersection(refs) and guard.normalized_text.upper() not in answer.upper():
                    condition = guard.normalized_text
                    description = _guard_description({"kind": guard.name, "condition": condition,
                                                      **relation.metadata})
                    candidates.append((page, base + guard.start_line, base + call.end_line,
                        "invocation_condition", condition,
                        "此处调用受 " + description + "控制"))
            for unit in parsed.code_units:
                if unit.unit_type != "DataItem" or unit.name not in targets or unit.parse_status != "complete":
                    continue
                picture = re.search(r"\bPIC(?:TURE)?\s+(?:IS\s+)?([^\s.]+)", unit.normalized_text, re.I)
                if picture and picture[1].upper() not in answer.upper():
                    from numeric_semantics import numeric_layout
                    layout = numeric_layout(unit.normalized_text)
                    scale = f"（{layout.scale} 位小数）" if layout.scale is not None else ""
                    rounded_modes = {fact.get("rounded") for fact in arithmetic
                                     if fact["target"] == unit.name and fact["statement_kind"] == "COMPUTE"}
                    rounding = ("本次计算" + ("使用 ROUNDED" if True in rounded_modes else "没有 ROUNDED")
                                if len(rounded_modes) == 1 else "各条计算的 ROUNDED 设置不同")
                    candidates.append((page, base + unit.start_line, base + unit.end_line,
                        "numeric_layout", unit.normalized_text,
                        f"`{unit.name}` 声明为 `PIC {picture[1]}`{scale}，{rounding}；存储尺度与溢出处理须按目标编译环境解释"))
    additions = []
    for page, first, last, kind, statement, text in candidates:
        key = (page["relative_path"], page["source_sha256"], first, last, kind, statement)
        if key in seen:
            continue
        seen.add(key)
        if len(additions) >= _MAX_ADDITIONS:
            report["omitted_candidate_count"] += 1
            continue
        identifier = page["evidence_id"]
        additions.append(text + f"。[{identifier}]")
        report["added_facts"].append({"kind": kind, "statement": statement, "evidence_id": identifier,
            "relative_path": page["relative_path"], "source_sha256": page["source_sha256"],
            "start_line": first, "end_line": last, "support_scope": "supplied_source_syntax"})
    if additions:
        answer = answer.rstrip() + "\n\n" + " ".join(additions)
        report["status"] = "source_facts_added"
    report["rendered_answer_sha256"] = hashlib.sha256(answer.encode()).hexdigest()
    return answer, report
