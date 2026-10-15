"""Frozen semantic and scale cases against the delivered desktop HTTP process.

This runner never imports application modules. The executable creates every
index, semantic cache and ledger. SQLite is opened read-only for assertions.
Synthetic provider answers exercise evidence and checking, not model accuracy.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import time

from tools.desktop_acceptance_runtime import LoopbackProvider, NativeWorkbench, decoded_navigation, supplied_pages, write_profile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs/eval/desktop-joint-v1/manifest.json"
PUBLIC_MANIFEST = ROOT / "docs/eval/public-business-v1/manifest.json"
PUBLIC_CASES = ROOT / "docs/eval/public-business-v1/cases.json"
FROZEN_SOURCE_ROOT = None


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def read_database(path, query, parameters=()):
    require(Path(path).is_file(), "Executable did not create database: " + Path(path).name)
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(query, parameters)]


def verify_pages(source, pages):
    """Check actual wire text, physical ranges and complete-file versions."""
    root = Path(source).resolve()
    details = []
    cached = {}
    for page in pages:
        path = (root / page["relative_path"]).resolve(strict=True)
        require(path.is_relative_to(root), "Supplied page escaped source root")
        if path not in cached:
            raw = path.read_bytes()
            cached[path] = hashlib.sha256(raw).hexdigest(), raw.decode("utf-8").splitlines()
        version, lines = cached[path]
        start, end = page["start_line"], page["end_line"]
        require(type(start) is int and type(end) is int and 1 <= start <= end <= len(lines),
                "Supplied physical line range invalid")
        text = "\n".join(lines[start - 1:end])
        require(version == page["source_sha256"], "Supplied source version differs")
        require(page["source_text"] == text or (page.get("span_truncated") is True
                and text.startswith(page["source_text"]) and bool(page["source_text"])), "Supplied source text differs")
        details.append({"evidence_id": page["evidence_id"], "relative_path": page["relative_path"],
            "source_sha256": version, "start_line": start, "end_line": end,
            "excerpt_sha256": hashlib.sha256(page["source_text"].encode()).hexdigest(),
            "span_truncated": bool(page.get("span_truncated"))})
    require(bool(details), "No source was supplied to provider")
    return details


def ranges_supplied(pages, required):
    """A path hit cannot substitute for the union of reviewed raw line ranges."""
    if not required:
        return False
    for location in required:
        covered = set()
        for page in pages:
            if (page["relative_path"] == location["path"] and not page.get("span_truncated")
                    and page["source_sha256"] == location["sha256"]):
                covered.update(range(page["start_line"], page["end_line"] + 1))
        if not set(range(location["start_line"], location["end_line"] + 1)) <= covered:
            return False
    return True


def page_for(pages, relative, needle):
    matches = [page for page in pages if page["relative_path"] == relative and needle in page["source_text"]]
    require(matches, "Critical source not supplied: " + relative + ": " + needle)
    return min(matches, key=lambda page: page["end_line"] - page["start_line"])


def line_number(source, relative, needle):
    lines = (Path(source) / relative).read_text(encoding="utf-8").splitlines()
    positions = [number for number, line in enumerate(lines, 1) if needle in line]
    require(len(positions) == 1, "Semantic anchor must be unique: " + needle)
    return positions[0]


def corpus_fingerprint(source):
    result = hashlib.sha256()
    files = lines = size = 0
    for path in sorted(Path(source).rglob("*")):
        if not path.is_file():
            continue
        raw = path.read_bytes()
        relative = path.relative_to(source).as_posix()
        result.update(relative.encode() + b"\0" + raw + b"\n")
        files += 1
        lines += len(raw.splitlines())
        size += len(raw)
    return {"files": files, "lines": lines, "source_bytes": size, "source_fingerprint": result.hexdigest()}


def _large_program(index, files, lines, copybooks):
    """Frozen neutral shape used in the pre-release 12,024-file measurement."""
    rows = ["IDENTIFICATION DIVISION.", f"PROGRAM-ID. MEMBER-{index:05d}.",
            "DATA DIVISION.", "WORKING-STORAGE SECTION."]
    rows += [f'COPY "shared-{(index + offset) % copybooks:03d}.cpy".' for offset in range(4)]
    rows += [f"01 INPUT-{field:03d} PIC 9(9)." for field in range(24)]
    rows += ["01 BILL-AMOUNT PIC 9(9).", "01 BILL-DATE PIC 9(8).", "PROCEDURE DIVISION.", "MAIN-WORK."]
    rows += [f"PERFORM RULE-{paragraph:03d}." for paragraph in range(8)]
    rows += [f'CALL "MEMBER-{(index + 1) % files:05d}".', "GOBACK."]
    blocks = (lines - len(rows) - 10) // 4
    for paragraph in range(8):
        rows.append(f"RULE-{paragraph:03d}.")
        for block in range(blocks // 8 + (paragraph < blocks % 8)):
            field = (paragraph * 5 + block) % 24
            rows += [f"IF INPUT-{field:03d} > {block % 97}",
                     f"COMPUTE BILL-AMOUNT = INPUT-{field:03d} * {block % 97 + 1}",
                     "END-IF.", f"MOVE INPUT-{field:03d} TO BILL-DATE."]
    while len(rows) < lines - 2:
        rows.append("ADD INPUT-000 TO BILL-AMOUNT.")
    rows += [f"*> TAILMARK{index:05d}", "GOBACK."]
    return "\n".join(rows) + "\n"


def generate_large(source, specification):
    source = Path(source)
    source.mkdir(parents=True, exist_ok=False)
    count, copies = specification["programs"], specification["copybooks"]
    for index in range(copies):
        text = f"01 SHARED-RECORD-{index:03d}.\n" + "".join(
            f"  05 SHARED-FIELD-{index:03d}-{field:02d} PIC 9(9).\n" for field in range(8))
        (source / f"shared-{index:03d}.cpy").write_bytes(text.encode())
    for index in range(count):
        lines = specification["longest_program_lines"] if index == count - 1 else specification["lines_per_program"]
        (source / f"member-{index:05d}.cbl").write_bytes(_large_program(index, count, lines, copies).encode())
    actual = corpus_fingerprint(source)
    for key, expected in specification["expected"].items():
        require(actual[key] == expected, "Generated corpus identity changed: " + key)
    return actual


def prepare_source(case, manifest, destination, public_source):
    destination = Path(destination)
    if case["corpus"] == "large":
        return generate_large(destination, manifest["large_corpus"])
    destination.mkdir(parents=True, exist_ok=False)
    if case["corpus"] == "public":
        require(public_source is not None, "Required fixed public source was not provided; skipping is forbidden")
        public = json.loads(PUBLIC_MANIFEST.read_text(encoding="utf-8"))
        require(digest(PUBLIC_MANIFEST) == manifest["public_corpus"]["manifest_sha256"], "Public manifest changed")
        require(digest(PUBLIC_CASES) == manifest["public_corpus"]["cases_sha256"], "Public gold changed")
        source_root = Path(public_source).resolve(strict=True)
        files = public["files"]
    else:
        source_root = Path(FROZEN_SOURCE_ROOT) if FROZEN_SOURCE_ROOT else DEFAULT_MANIFEST.parent / "source"
        files = [manifest["source_files"][name] for name in case["files"]]
    for item in files:
        path = (source_root / item["path"]).resolve(strict=True)
        require(path.is_relative_to(source_root.resolve()), "Corpus path escaped its root")
        require(digest(path) == item["sha256"], "Required source version changed: " + item["path"])
        target = destination / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    return corpus_fingerprint(destination)


def _rules(output):
    return read_database(Path(output) / "structural-index.sqlite", "SELECT * FROM business_rules")


def frontend_checks(output):
    """Query facts produced by the executable, never a runner-side parser."""
    database = Path(output) / "structural-index.sqlite"
    rules = _rules(output)
    def exact(text):
        return [row for row in rules if row["normalized_text"].rstrip(".") == text]
    def access(text, reads, writes):
        rows = exact(text)
        return bool(rows) and all(json.loads(row["reads_json"]) == reads and json.loads(row["writes_json"]) == writes for row in rows)
    result = {"quality_add_giving_roles": access("ADD A TO B GIVING C END-ADD", ["A", "B"], ["C"])
              and access("ADD A TO B GIVING A END-ADD", ["A", "B"], ["A"]),
        "quality_scope_words_not_operands": all(not any(value.startswith("END-") or value == "ROUNDED"
            for value in json.loads(row[key])) for row in rules for key in ("reads_json", "writes_json")),
        "quality_qualified_fields_distinct": access("COMPUTE AMOUNT OF OUTPUT-REC = AMOUNT OF INPUT-REC * 2 END-COMPUTE",
            ["AMOUNT OF INPUT-REC"], ["AMOUNT OF OUTPUT-REC"]),
        "quality_literal_comment_marker": access("MOVE 'CALL *> PERFORM' TO TEXT-VALUE", [], ["TEXT-VALUE"])}
    dependencies = read_database(database, "SELECT relation_type,target_name FROM relations WHERE relation_type IN ('CALLS','CALL_TARGET_FROM','PERFORMS','PERFORMS_THRU')")
    result["quality_literal_dependencies_absent"] = dependencies == [
        {"relation_type": "PERFORMS", "target_name": "CALCULATE-SECTION"}]
    occurrences = read_database(database, "SELECT o.*,b.rule_kind,b.reads_json,b.writes_json FROM business_rule_occurrences o JOIN business_rules b ON b.rule_id=o.rule_id WHERE o.start_line=18 ORDER BY o.start_column")
    result["quality_same_line_occurrences"] = (len(occurrences) == 3 and len({row["occurrence_id"] for row in occurrences}) == 3
        and [row["rule_kind"] for row in occurrences] == ["ADD", "MOVE", "COMPUTE"]
        and [(json.loads(row["reads_json"]), json.loads(row["writes_json"])) for row in occurrences]
            == [(["A", "B"], ["C"]), (["C"], ["A"]), (["A"], ["B"])]
        and len({row["start_column"] for row in occurrences}) == 3)
    sections = read_database(database, "SELECT r.status,s.symbol_type,s.name FROM relations r LEFT JOIN symbols s ON s.symbol_id=r.target_entity_id WHERE r.relation_type='PERFORMS'")
    result["quality_section_binding"] = sections == [{"status": "confirmed", "symbol_type": "Section", "name": "CALCULATE-SECTION"}]
    documents = []
    for path in (Path(output) / "semantic-scopes").glob("*.sqlite"):
        documents.extend(json.loads(row["ir_json"]) for row in read_database(path,
            "SELECT ir_json FROM semantic_ir_documents WHERE relative_path='frontend.cbl'"))
    all_occurrences = read_database(database, "SELECT o.start_line,o.start_column,b.reads_json,b.writes_json FROM business_rule_occurrences o JOIN business_rules b ON b.rule_id=o.rule_id")
    def identity(reference):
        return reference["name"] + "".join(" OF " + name for name in reference["qualifiers"])
    def expression_safe(expression):
        return (not str(expression.get("value", "")).startswith("END-")
            and expression.get("value") != "ROUNDED"
            and all(expression_safe(child) for child in expression.get("operands", [])))
    agreement = bool(documents)
    for document in documents:
        agreement &= document.get("parse_complete") is True
        for statement in document["statements"]:
            if statement["statement_type"] not in {"ADD", "MOVE", "COMPUTE"}:
                continue
            ref = statement["source_refs"][0]
            expected = ([identity(item) for item in statement["reads"]], [identity(item) for item in statement["writes"]])
            agreement &= any(row["start_line"] == ref["start_line"] and row["start_column"] == ref["start_column"]
                and (json.loads(row["reads_json"]), json.loads(row["writes_json"])) == expected for row in all_occurrences)
            result["quality_scope_words_not_operands"] &= all(expression_safe(expression) for _, expression in statement["expressions"])
    result["quality_semantic_ir_agrees"] = bool(agreement)
    return result


def _flow_graphs(output):
    return [json.loads(row["value_json"]) for row in read_database(Path(output) / "investigation-runs.sqlite",
        "SELECT value_json FROM derivation_cache WHERE layer='flow'")]


def flow_checks(output, case_id):
    graphs = _flow_graphs(output)
    if case_id == "frontend_semantics":
        return {"quality_section_flow": any(
            any(node["statement"].rstrip(".") == "MOVE A TO C" for node in graph["nodes"].values())
            and not any(node["statement"].rstrip(".") == "MOVE 9 TO C" for node in graph["nodes"].values())
            and graph.get("flow_complete") is True for graph in graphs)}
    if case_id == "overwrite_condition":
        found = False
        for graph in graphs:
            if graph.get("flow_complete") is not True or graph.get("frontier"):
                continue
            nodes = graph["nodes"]
            reads = [key for key, node in nodes.items() if node["statement"].rstrip(".") == "MOVE OUT-AMOUNT TO FINAL-AMOUNT"]
            for key in reads:
                reaching = graph["reaching_in"].get(key, {}).get("OUT-AMOUNT", [])
                texts = {nodes[item]["statement"].rstrip(".") for item in reaching if item in nodes}
                zero = [nodes[item] for item in reaching if item in nodes and nodes[item]["statement"].rstrip(".") == "MOVE 0 TO OUT-AMOUNT"]
                found |= texts == {"COMPUTE OUT-AMOUNT = IN-AMOUNT * 2", "MOVE 0 TO OUT-AMOUNT"} and bool(zero) and any(
                    guard.get("condition") == "FLAG = 1" and guard.get("outcome") is True for guard in zero[0].get("guards", []))
        return {"quality_conditional_reaching_definitions": found}
    if case_id == "static_call_chain":
        bound = False
        for graph in graphs:
            if graph.get("flow_complete") is not True or graph.get("frontier"):
                continue
            nodes = graph["nodes"]
            if not {"MIDDLE", "LEAF"} <= set(graph.get("dependencies", [])):
                continue
            for key, node in nodes.items():
                if node["statement"].rstrip(".") != "MOVE OUT-AMOUNT TO FINAL-AMOUNT":
                    continue
                definitions = graph["reaching_in"].get(key, {}).get("OUT-AMOUNT", [])
                bound |= len(definitions) == 1 and definitions[0] in nodes and nodes[definitions[0]]["statement"].rstrip(".") == "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2"
        return {"quality_static_chain_return_binding": bound}
    return {}


def _supported(ledger, target=None, expression=None, condition=None, predicate=None, effect=None):
    return any(entry.get("support_status") == "supported"
        and (predicate is None or entry.get("claim_type") == predicate)
        and (effect is None or entry.get("effect") == effect)
        and (target is None or entry.get("target_entity") == target)
        and (expression is None or entry.get("expression") == expression)
        and (condition is None or entry.get("condition") == condition)
        for entry in ledger.get("entries", []))


def ledger_versions_match(ledger, pages):
    """Verified claims must refer to supplied IDs and the corresponding versions."""
    supplied = {page["evidence_id"]: page for page in pages}
    if not supplied or not ledger.get("entries"):
        return False
    for entry in ledger["entries"]:
        if entry.get("support_status") == "supported":
            identifiers = entry.get("evidence_ids", [])
            versions = entry.get("source_versions", [])
            if not identifiers or not versions or entry.get("invalid_evidence_ids") or any(key not in supplied for key in identifiers):
                return False
            if not all(any(supplied[key]["relative_path"] == ref.get("relative_path")
                and supplied[key]["source_sha256"] == ref.get("source_sha256")
                and supplied[key]["start_line"] <= ref.get("start_line", 0) <= ref.get("end_line", 0) <= supplied[key]["end_line"]
                for key in identifiers) for ref in versions):
                return False
        for revised in entry.get("revised_claims", []):
            if revised.get("support_status") != "supported":
                continue
            page = supplied.get(revised.get("evidence_id"))
            if not page or page["source_sha256"] != revised.get("source_sha256"):
                return False
    return True


def replacement_supported(ledger, expected):
    return any(revised.get("support_status") == "supported" and revised.get("assertion") == expected
        for entry in ledger.get("entries", []) for revised in entry.get("revised_claims", []))


def _public_case(case):
    cases = json.loads(PUBLIC_CASES.read_text(encoding="utf-8"))["cases"]
    return next(item for item in cases if item["id"] == case["public_case_id"])


def _provider_answer(case, source, payload, first_request):
    pages = supplied_pages(payload)
    verify_pages(source, pages)
    if first_request and case.get("semantic_tool"):
        anchor = case["semantic_tool"]
        page_for(pages, anchor["path"], anchor["contains"])
        return json.dumps({anchor["tool"]: {"relative_path": anchor["path"],
            "line": line_number(source, anchor["path"], anchor["contains"])}})
    if case["corpus"] == "public":
        item = _public_case(case)
        location = item["mechanism"]["evidence_range"]
        matching = [page for page in pages if page["relative_path"] == location["path"]
            and page["start_line"] <= location["start_line"] and page["end_line"] >= location["end_line"]
            and not page.get("span_truncated")]
        require(matching, "Public critical review range not fully supplied")
        page = min(matching, key=lambda value: value["end_line"] - value["start_line"])
        key = "fake_incorrect_answer" if case.get("inject_error") else "fake_correct_answer"
        return item["mechanism"][key].replace("[EVIDENCE]", "[" + page["evidence_id"] + "]")
    if case["id"] == "frontend_semantics":
        page = page_for(pages, "frontend.cbl", "ADD A TO B GIVING C")
        qualified = page_for(pages, "frontend.cbl", "COMPUTE AMOUNT OF OUTPUT-REC")
        return (f"C = A + B。[{page['evidence_id']}] "
            f"AMOUNT OF OUTPUT-REC = AMOUNT OF INPUT-REC * 2。[{qualified['evidence_id']}] "
            "ADD 的输入为 A 和 B；GIVING C 的接收字段是 C。同一实际字段可同时承担输入和输出。"
            "字符串中的 CALL、PERFORM 和 *> 属于文字，限定字段分属两个记录。")
    if case["id"] in {"overwrite_condition", "forward_impact"}:
        formula = page_for(pages, "overwrite.cbl", "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2")
        condition = page_for(pages, "overwrite.cbl", "IF FLAG = 1")
        overwrite = page_for(pages, "overwrite.cbl", "MOVE 0 TO OUT-AMOUNT")
        return (f"OUT-AMOUNT = IN-AMOUNT * 2。[{formula['evidence_id']}] "
            f"若 FLAG = 1，后续 MOVE 0 TO OUT-AMOUNT 会覆盖前面的计算。[{condition['evidence_id']}][{overwrite['evidence_id']}] "
            "条件未成立时保留前面的计算；最终读取需要区分这两个分支。")
    if case["id"] == "static_call_chain":
        entry = page_for(pages, "entry.cbl", "CALL 'MIDDLE' USING IN-AMOUNT OUT-AMOUNT")
        middle = page_for(pages, "middle.cbl", "CALL 'LEAF' USING IN-AMOUNT OUT-AMOUNT")
        leaf = page_for(pages, "leaf.cbl", "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2")
        return (f"ENTRYPOINT 通过 MIDDLE 调用 LEAF，实参与形参按顺序传递。[{entry['evidence_id']}][{middle['evidence_id']}] "
            f"OUT-AMOUNT = IN-AMOUNT * 2。[{leaf['evidence_id']}] "
            "正常返回后的读取由叶程序这条赋值到达；这里只说明源码中的静态路径。")
    if case["id"] == "large_repository":
        for page in pages:
            match = re.search(r"IF (INPUT-\d+ > \d+)\s+COMPUTE BILL-AMOUNT = (INPUT-\d+ \* \d+)", page["source_text"])
            if match:
                return (f"在 {match[1]} 条件下，BILL-AMOUNT = {match[2]}。[{page['evidence_id']}] "
                    "这是一项局部计算事实；它不足以证明所有分支的最终结果或外部调用的数据。")
        raise AssertionError("No formula with its controlling condition supplied")
    raise ValueError("Unknown desktop acceptance case: " + case["id"])


def _required_ranges(case, source):
    if case["corpus"] == "public":
        return _public_case(case)["reviewed_context"]["source_ranges"]
    if case["corpus"] == "large":
        return []
    return [{"path": name, "sha256": digest(Path(source) / name), "start_line": 1,
             "end_line": len((Path(source) / name).read_text(encoding="utf-8").splitlines())} for name in case["files"]]


def _investigation(trace):
    return (trace.get("final") or {}).get("question_investigation") or {}


def _obligation(trace, kind):
    return next((item for item in _investigation(trace).get("required_items", [])
                 if item.get("kind") == kind), {})


def _quality_checks(case, agent, output, pages, trace=None):
    ledger, answer = agent.get("claim_ledger", {}), agent.get("answer", "")
    result = {}
    if case["id"] == "frontend_semantics":
        result.update(frontend_checks(output), **flow_checks(output, case["id"]))
        result["quality_explicit_formulas_supported"] = _supported(ledger, "C", ["A", "+", "B"]) and _supported(
            ledger, "AMOUNT OF OUTPUT-REC", ["AMOUNT", "OF", "INPUT-REC", "*", "2"])
    elif case["id"] in {"overwrite_condition", "forward_impact"}:
        result.update(flow_checks(output, "overwrite_condition"))
        result["quality_formula_condition_overwrite_supported"] = (_supported(ledger, "OUT-AMOUNT", ["IN-AMOUNT", "*", "2"])
            and _supported(ledger, "OUT-AMOUNT", ["0"]) and _supported(ledger, condition=["FLAG", "=", "1"]))
        result["completeness_both_branches_preserved"] = all(text in answer for text in ("FLAG", "覆盖", "条件未成立", "计算"))
    elif case["id"] == "static_call_chain":
        result.update(flow_checks(output, case["id"]))
        result["quality_leaf_formula_supported"] = _supported(ledger, "OUT-AMOUNT", ["IN-AMOUNT", "*", "2"])
        result["completeness_three_program_path"] = all(name in answer for name in ("ENTRYPOINT", "MIDDLE", "LEAF"))
        inputs = _obligation(trace or {}, "inputs")
        witnesses = inputs.get("source_flow_origins") or []
        supplied_ids = {page["evidence_id"] for page in pages}
        result["completeness_static_origin_closed"] = (inputs.get("status") == "SATISFIED"
            and inputs.get("reason") == "source_flow_origins_supplied" and bool(witnesses)
            and all(witness.get("status") == "source_origin_supplied"
                and witness.get("execution_verified") is False and witness.get("evidence_ids")
                and set(witness["evidence_ids"]) <= supplied_ids for witness in witnesses)
            and not _investigation(trace or {}).get("open_gaps"))
    elif case["id"] == "large_repository":
        result["quality_formula_and_condition_supported"] = (_supported(ledger, "BILL-AMOUNT")
            and any(entry.get("claim_type") == "condition_statement" and entry.get("support_status") == "supported" for entry in ledger.get("entries", [])))
        result["quality_unknown_not_verified"] = (ledger.get("counts", {}).get("unsupported", 0) > 0
            and agent.get("claims_semantically_verified") is False and "不足以证明" in answer)
        result["completeness_formula_control_supplied"] = any(re.search(
            r"IF INPUT-\d+ > \d+\s+COMPUTE BILL-AMOUNT = INPUT-\d+ \* \d+", page["source_text"]) for page in pages)
    elif case["corpus"] == "public":
        item = _public_case(case)
        result["quality_frozen_source_assertions"] = all(_supported(ledger,
            assertion.get("target"), assertion.get("expression"), assertion.get("condition"),
            assertion.get("predicate"), assertion.get("effect"))
            for assertion in item["mechanism"]["source_assertions"])
        if case.get("inject_error"):
            result["quality_injected_error_revised"] = ledger.get("revised_claim_count", 0) > 0
            result["quality_invalid_claim_absent"] = all(text not in answer for text in case["forbidden_answer_text"])
            # The ledger intentionally retains the rejected original claim.
            expected = item["mechanism"]["source_assertions"][0] if item["mechanism"]["source_assertions"] else {
                "predicate": "noop_procedure", "target": "1400-COMPUTE-FEES"}
            result["quality_frozen_source_assertions"] = (all(text in answer for text in case["required_answer_text"])
                and replacement_supported(ledger, expected))
        else:
            result["quality_critical_answer_preserved"] = all(text in answer for text in case["required_answer_text"])
        if case.get("unverified_prose"):
            result["quality_prose_not_falsely_verified"] = ledger.get("prose_semantics_verified") is False and agent.get("claims_semantically_verified") is False
        if case["public_case_id"] == "unimplemented-procedure":
            investigation = _investigation(trace or {})
            body = _obligation(trace or {}, "procedure_body")
            body_ids = set(body.get("evidence_ids") or [])
            body_scope_supplied = any(scope.get("procedure_name") == "1400-COMPUTE-FEES"
                and any(page["evidence_id"] in body_ids and page["relative_path"] == scope.get("relative_path")
                    and page["source_sha256"] == scope.get("source_sha256") and not page.get("span_truncated")
                    and page["start_line"] <= scope.get("start_line", 0) <= scope.get("end_line", 0) <= page["end_line"]
                    for page in pages) for scope in (investigation.get("source_scope") or {}).get("ranges", []))
            result["quality_noop_scope_not_monthly_interest"] = (body.get("status") == "SATISFIED"
                and body.get("candidate_count") == 1 and body_scope_supplied
                and _obligation(trace or {}, "formula").get("candidate_count") == 0
                and _obligation(trace or {}, "conditions").get("candidate_count") == 0
                and _obligation(trace or {}, "dependencies").get("status") in {"SATISFIED", "NOT_APPLICABLE"}
                and not _obligation(trace or {}, "dependencies").get("targets")
                and not investigation.get("open_gaps"))
        if case["public_case_id"] == "file-record-mapping":
            investigation = _investigation(trace or {})
            entities = investigation.get("analysis_plan", {}).get("file_entities", [])
            file_entities = [entity for entity in entities if entity.get("file_name") == "ACCOUNT-FILE"]
            supplied_ids = {page["evidence_id"] for page in pages}
            obligations = [_obligation(trace or {}, kind) for kind in
                           ("file_definitions", "file_layout", "copy_layout", "file_io")]
            bound = bool(file_entities) and all(entity.get("entity_type") == "FileRef"
                and entity.get("binding_status") == "bound" and entity.get("external_identity_verified") is False
                and not entity.get("diagnostics") and ranges_supplied(pages, [{
                    "path": ref["relative_path"], "sha256": ref["source_sha256"],
                    "start_line": ref["start_line"], "end_line": ref["end_line"]}
                    for ref in entity.get("source_versions", [])]) for entity in file_entities)
            result["completeness_file_entity_bound"] = bool(bound and all(item.get("status") == "SATISFIED"
                and item.get("evidence_ids") and set(item["evidence_ids"]) <= supplied_ids for item in obligations)
                and not any(gap.get("reason") in {"requested_field_not_bound", "business_steps_not_located"}
                            for gap in investigation.get("open_gaps", [])))
    return result


def _probe_answer(probe, source, payload):
    pages = supplied_pages(payload)
    verify_pages(source, pages)
    if probe["id"] == "bare_group_field":
        page = page_for(pages, "record.cbl", "COMPUTE VALUE-00000 = VALUE-00001 * 2")
        return f"VALUE-00000 = VALUE-00001 * 2。[{page['evidence_id']}] 两个字段都属于 SOURCE-RECORD。输入声明是 PIC 9(9)，源码没有说明本次运行的输入值。"
    if probe["id"] in {"qualified_output_scope", "ambiguous_group_field"}:
        page = page_for(pages, "frontend.cbl", "COMPUTE AMOUNT OF OUTPUT-REC")
        answer = f"AMOUNT OF OUTPUT-REC = AMOUNT OF INPUT-REC * 2。[{page['evidence_id']}] 两个字段分别属于 OUTPUT-REC 与 INPUT-REC，输入运行值尚未给出。"
        return "AMOUNT 的限定记录存在歧义，需保留两个候选。" + answer if probe["id"] == "ambiguous_group_field" else answer
    if probe["id"] == "large_two_if_pic":
        first = page_for(pages, "member-11999.cbl", "IF INPUT-000 > 0")
        second = page_for(pages, "member-11999.cbl", "IF INPUT-001 > 1")
        return (f"INPUT-000 > 0 时，BILL-AMOUNT = INPUT-000 * 1。[{first['evidence_id']}] "
            f"随后 INPUT-001 > 1 时，BILL-AMOUNT = INPUT-001 * 2，会覆盖先前值。[{second['evidence_id']}] "
            "两个输入和输出均声明为 PIC 9(9)。第二个条件未成立时不执行第二次赋值。"
            "这只解释提问范围中的前两条 IF；真实输入值和后续代码的最终影响尚未确认。")
    raise ValueError("Unknown frozen scope probe: " + probe["id"])


def _bare_group_binding(output, pages):
    """Read the executable's bound IR; never infer binding from display text."""
    if output is None:
        return False
    versions = {page["source_sha256"] for page in pages if page["relative_path"] == "record.cbl"}
    for database in (Path(output) / "semantic-scopes").glob("*.sqlite"):
        for row in read_database(database, "SELECT ir_json FROM semantic_ir_documents WHERE relative_path='record.cbl'"):
            document = json.loads(row["ir_json"])
            items = {item["item_id"]: item for item in document.get("data_items", [])}
            for statement in document.get("statements", []):
                if statement.get("statement_type") != "COMPUTE" or statement.get("binding_status") != "bound":
                    continue
                if not statement.get("source_refs") or not all(
                        ref.get("source_sha256") in versions for ref in statement["source_refs"]):
                    continue
                matches = True
                for role, name in (("reads", "VALUE-00001"), ("writes", "VALUE-00000")):
                    references = statement.get(role) or []
                    matches &= len(references) == 1 and references[0].get("binding_status") == "bound"
                    candidates = references[0].get("candidates", []) if len(references) == 1 else []
                    item = items.get(candidates[0], {}) if len(candidates) == 1 else {}
                    matches &= item.get("name") == name and item.get("ancestors") == ["SOURCE-RECORD"]
                if matches:
                    return True
    return False


def _scope_probe_checks(probe, trace, agent, pages, output=None):
    """Inspect executable obligations and transmitted evidence, not runner parsing."""
    investigation = _investigation(trace)
    formula = _obligation(trace, probe.get("expected_primary_kind", "formula"))
    supplied = {page["evidence_id"] for page in pages}
    basic = (bool(investigation.get("required_items")) and formula.get("status") == "SATISFIED"
        and formula.get("candidate_count") == probe["expected_candidate_count"]
        and bool(formula.get("evidence_ids")) and set(formula["evidence_ids"]) <= supplied)
    ledger = agent.get("claim_ledger") or {}
    if probe["id"] == "bare_group_field":
        require_ledger = _supported(ledger, "VALUE-00000", ["VALUE-00001", "*", "2"])
        full_source = any(page["relative_path"] == "record.cbl" and page["start_line"] == 1
            and page["end_line"] >= 11 and not page.get("span_truncated") for page in pages)
        return {"quality_bare_group_field_formula_supplied": basic and require_ledger and full_source
            and _bare_group_binding(output, pages)}
    if probe["id"] == "qualified_output_scope":
        steps = _obligation(trace, "business_steps")
        return {"quality_qualified_output_scope": basic and steps.get("candidate_count") == 1
            and _supported(ledger, "AMOUNT OF OUTPUT-REC", ["AMOUNT", "OF", "INPUT-REC", "*", "2"])}
    if probe["id"] == "ambiguous_group_field":
        ambiguity = [gap for gap in investigation.get("open_gaps", [])
                     if gap.get("reason") == "field_reference_ambiguous"]
        completion = (trace.get("final") or {}).get("answer_completion") or {}
        return {"quality_ambiguous_group_field_candidates": basic and any(
            set(gap.get("candidates", [])) == {"AMOUNT OF INPUT-REC", "AMOUNT OF OUTPUT-REC"}
            for gap in ambiguity) and agent.get("status") == "PARTIAL",
            "quality_ambiguous_completion_relevant": (completion.get("semantic_verification") == "unverified"
                and "formula" in completion.get("required_aspects", [])
                and "result_adjustments" not in completion.get("required_aspects", [])
                and "rounding" not in completion.get("missing_aspects", ["rounding"]))}
    if probe["id"] == "large_two_if_pic":
        conditions, declarations, inputs = [_obligation(trace, kind) for kind in ("conditions", "declarations", "inputs")]
        expected = {9: "01 INPUT-000 PIC 9(9).", 10: "01 INPUT-001 PIC 9(9).", 33: "01 BILL-AMOUNT PIC 9(9)."}
        declared = all(any(page["relative_path"] == "member-11999.cbl"
            and page["start_line"] <= line <= page["end_line"]
            and statement in page["source_text"] and not page.get("span_truncated") for page in pages)
            for line, statement in expected.items())
        return {"completeness_large_two_if_pic_supplied": basic and declared
            and conditions.get("status") == "SATISFIED" and conditions.get("candidate_count") == 2
            and declarations.get("status") == "SATISFIED" and declarations.get("candidate_count") == 3
            and _supported(ledger, "BILL-AMOUNT", ["INPUT-000", "*", "1"])
            and _supported(ledger, "BILL-AMOUNT", ["INPUT-001", "*", "2"]),
            "quality_large_unknown_runtime_input_preserved": inputs.get("status") in {"PARTIAL", "UNRESOLVED"}
                and set(inputs.get("fields", [])) == {"INPUT-000", "INPUT-001"}
                and inputs.get("reason") == "input_source_not_located"
                and not any("budget" in gap.get("reason", "") for gap in investigation.get("open_gaps", []))}
    raise ValueError("Unknown frozen scope probe: " + probe["id"])


def _run_scope_probe(workbench, provider, probe, source, output, case):
    start = len(provider.requests)
    started = time.monotonic()
    response = workbench.analyze(source, output, probe["question"], source_format=case.get("source_format", "free"),
        answer_detail="detailed", timeout=probe["limits"]["question_seconds"] + 10)
    require(response["job"]["status"] == "COMPLETED", "Default scope probe did not complete")
    require(not provider.errors, "Synthetic scope provider errors: " + str(provider.errors))
    body = response["result"]
    agent = body.get("agent_result", body)
    trace_path = agent.get("metrics", {}).get("quality_trace_path")
    require(trace_path and Path(trace_path).is_file(), "Scope probe trace missing")
    trace = json.loads(Path(trace_path).read_text(encoding="utf-8"))
    requests = provider.requests[start:]
    require(requests, "Scope probe did not transmit source to provider")
    for request in requests:
        verify_pages(source, request["source_pages"])
    final_ids = set(trace.get("final", {}).get("final_answer_round_ids", []))
    final_ids.add(trace.get("final", {}).get("final_answer_round_id"))
    final_hashes = {entry["request_body_sha256"] for entry in trace["rounds"] if entry["round_id"] in final_ids}
    final_requests = [entry for entry in requests if entry["request_sha256"] in final_hashes]
    pages = [page for entry in final_requests for page in entry["source_pages"]]
    evidence = verify_pages(source, pages)
    checks = _scope_probe_checks(probe, trace, agent, pages, output)
    checks["evidence_scope_probe_versions_match"] = ledger_versions_match(agent.get("claim_ledger", {}), pages)
    visible = (response["state"].get("conversation") or {}).get("messages", [])
    answer = agent.get("answer", "")
    checks["evidence_scope_probe_answers_bound"] = (bool(answer) and bool(visible)
        and visible[-1].get("content") == answer
        and hashlib.sha256(answer.encode()).hexdigest() == (agent.get("claim_ledger") or {}).get("answer_sha256")
        and trace.get("run_id") and agent.get("model_answer_recorded") is True
        and agent.get("stop_reason") not in {"QUESTION_TIME_BUDGET_EXHAUSTED", "cancelled"}
        and agent.get("reason_code") not in {"QUESTION_TIME_BUDGET_EXHAUSTED", "REMOTE_REQUEST_OUTCOME_UNKNOWN"})
    checks["lifecycle_scope_probe_index_reused"] = json.loads((output / "diagnosis.json").read_text()).get("index_reused") is True
    timings = {"first_provider_seconds": requests[0]["received_at"] - started,
               "question_seconds": response["elapsed_seconds"]}
    checks["performance_scope_probe_bounded"] = (len(requests) <= probe["limits"]["max_model_requests"]
        and all(timings[name] <= probe["limits"][name] for name in timings))
    from tools.acceptance_diagnostics import capture_diagnostics
    return {"id": probe["id"], "question": probe["question"], "checks": checks, "timings": timings,
        "run_id": trace["run_id"], "answer": agent.get("answer"), "agent_status": agent.get("status"),
        "model_requests": len(requests), "request_sha256": [entry["request_sha256"] for entry in requests],
        "evidence": evidence, "limits": probe["limits"], "transport": "loopback_https_fake",
        "acceptance_diagnostics": capture_diagnostics(trace, agent, trace_sha256=digest(trace_path))}


def _run_cases(command, root, manifest, public_source, *, case_ids=None):
    """Return flat samples; the release gate independently checks every label."""
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(Path(manifest).read_text(encoding="utf-8")) if not isinstance(manifest, dict) else manifest
    samples = []
    for case in manifest["cases"]:
        if case["owner"] != "cases" or case_ids is not None and case["id"] not in case_ids:
            continue
        directory = root / case["id"]
        directory.mkdir(exist_ok=False)
        source, output, profile = [directory / name for name in ("source", "workspace", "profile")]
        corpus = prepare_source(case, manifest, source, public_source)
        request_start = [0]
        def respond(envelope, payload, index):
            probe = next((probe for probe in case.get("scope_probes", [])
                          if payload.get("question") == probe["question"]), None)
            if probe is not None:
                return _probe_answer(probe, source, payload)
            require(payload.get("question") == case["question"], "Unexpected case question on wire")
            return _provider_answer(case, source, payload, index == request_start[0])
        with LoopbackProvider(directory / "provider", respond) as provider:
            environment = write_profile(profile, provider, case.get("policy", {"max_model_requests": 3, "max_answer_revisions": 0}))
            with NativeWorkbench(command, profile, environment, directory) as workbench:
                index = workbench.analyze(source, output, source_format=case.get("source_format", "free"),
                    timeout=case["limits"]["cold_index_seconds"])
                require(index["job"]["status"] == "COMPLETED" and not provider.requests, "Cold index failed or called provider")
                cold_seconds = index["elapsed_seconds"]
                indexed_result = json.loads((output / "agent-result.json").read_text(encoding="utf-8"))
                require(indexed_result.get("agent_result") is None, "Cold index unexpectedly answered a question")
                for number in range(1, case["repeats"] + 1):
                    request_start[0] = len(provider.requests)
                    started = time.monotonic()
                    response = workbench.analyze(source, output, case["question"],
                        source_format=case.get("source_format", "free"),
                        answer_detail="detailed", timeout=case["limits"]["question_seconds"] + 10)
                    require(response["job"]["status"] == "COMPLETED", "Default desktop question did not complete")
                    require(not provider.errors, "Synthetic provider errors: " + str(provider.errors))
                    body = response["result"]
                    agent = body.get("agent_result", body)
                    trace_path = agent.get("metrics", {}).get("quality_trace_path")
                    require(trace_path and Path(trace_path).is_file(), "Missing executable quality trace")
                    trace = json.loads(Path(trace_path).read_text(encoding="utf-8"))
                    requests = provider.requests[request_start[0]:]
                    require(requests, "No request reached synthetic provider")
                    final_ids = set(trace.get("final", {}).get("final_answer_round_ids", []))
                    final_ids.add(trace.get("final", {}).get("final_answer_round_id"))
                    final_hashes = {entry["request_body_sha256"] for entry in trace["rounds"] if entry["round_id"] in final_ids}
                    final_requests = [entry for entry in requests if entry["request_sha256"] in final_hashes]
                    pages = [page for entry in final_requests for page in entry["source_pages"]]
                    evidence = verify_pages(source, pages)
                    for request in requests:
                        verify_pages(source, request["source_pages"])
                    answer = agent.get("answer", "")
                    ledger = agent.get("claim_ledger", {})
                    diagnosis = json.loads((output / "diagnosis.json").read_text(encoding="utf-8"))
                    visible = (response["state"].get("conversation") or {}).get("messages", [])
                    checks = _quality_checks(case, agent, output, pages, trace)
                    if case["id"] == "forward_impact":
                        groups = [group for request in requests for group in
                                  decoded_navigation(request["payload"]).get("evidence_groups", [])]
                        tool_results = [item for group in groups for item in group.get("semantic_analysis", {}).get("tool_results", [])
                            if item.get("tool") == "forward_slice"]
                        checks["quality_forward_slice_reaches_output"] = any(
                            {"COMPUTE OUT-AMOUNT = IN-AMOUNT * 2", "MOVE OUT-AMOUNT TO FINAL-AMOUNT"} <= {
                                statement.get("statement", "").rstrip(".") for statement in result.get("statements", [])}
                            and result.get("flow_status") == "possible_source_flow"
                            and result.get("execution_verified") is False for result in tool_results)
                    checks.update(evidence_supplied_hashes_match=ledger_versions_match(ledger, pages),
                        evidence_visible_answer_matches=bool(visible) and visible[-1].get("content") == answer,
                        evidence_answer_ledger_hash=bool(answer) and hashlib.sha256(answer.encode()).hexdigest() == ledger.get("answer_sha256"),
                        completeness_answer_recorded=agent.get("model_answer_recorded") is True
                            and agent.get("stop_reason") not in {"QUESTION_TIME_BUDGET_EXHAUSTED", "cancelled"}
                            and agent.get("reason_code") not in {"QUESTION_TIME_BUDGET_EXHAUSTED", "REMOTE_REQUEST_OUTCOME_UNKNOWN"},
                        lifecycle_index_reused=diagnosis.get("index_reused") is True)
                    if case["corpus"] != "large":
                        checks["completeness_reviewed_ranges_supplied"] = ranges_supplied(pages, _required_ranges(
                            {**case, "files": case.get("reviewed_files", case.get("files", []))}, source))
                    probes = [_run_scope_probe(workbench, provider, probe, source, output, case)
                              for probe in case.get("scope_probes", [])]
                    for probe in probes:
                        checks.update({label: value for label, value in probe["checks"].items()
                                       if label in case["assertions"]})
                    if probes:
                        checks["evidence_scope_probe_versions_match"] = all(
                            probe["checks"]["evidence_scope_probe_versions_match"] for probe in probes)
                        checks["evidence_scope_probe_answers_bound"] = all(
                            probe["checks"]["evidence_scope_probe_answers_bound"] for probe in probes)
                        checks["lifecycle_scope_probe_index_reused"] = all(
                            probe["checks"]["lifecycle_scope_probe_index_reused"] for probe in probes)
                        checks["performance_scope_probe_bounded"] = all(
                            probe["checks"]["performance_scope_probe_bounded"] for probe in probes)
                    measured_question_seconds = response["elapsed_seconds"] + sum(
                        probe["timings"]["question_seconds"] for probe in probes)
                    from tools.acceptance_diagnostics import capture_diagnostics
                    row = {"id": case["id"], "turn_id": "1", "sample_index": number,
                        "run_id": trace["run_id"], "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(),
                        "elapsed_seconds": measured_question_seconds, "index_reused": number > 1,
                        "question_index_reused": diagnosis.get("index_reused") is True,
                        "timings": {"cold_index_seconds": cold_seconds if number == 1 else None,
                            "first_provider_seconds": requests[0]["received_at"] - started,
                            "question_seconds": measured_question_seconds,
                            "primary_question_seconds": response["elapsed_seconds"],
                            "scope_probe_seconds": sum(probe["timings"]["question_seconds"] for probe in probes)},
                        "checks": {label: checks.get(label, False) for label in case["assertions"]},
                        "corpus": corpus, "answer": answer, "agent_status": agent.get("status"),
                        "stop_reason": agent.get("stop_reason"), "claim_ledger": ledger,
                        "acceptance_diagnostics": capture_diagnostics(trace, agent, trace_sha256=digest(trace_path)),
                        "review_identity": {
                            "run_id": trace["run_id"], "pipeline_version": trace.get("pipeline_version"),
                            "prompt_version": trace.get("prompt_version"), "configuration": trace.get("configuration"),
                            "request_sha256": [entry["request_sha256"] for entry in final_requests]},
                        "evidence": evidence, "source_evidence_sha256": hashlib.sha256(json.dumps(
                            evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                        "model_requests": len(requests) + sum(probe["model_requests"] for probe in probes),
                        "primary_model_requests": len(requests), "scope_probes": probes,
                        "transport": "loopback_https_fake",
                        "phase_timings_seconds": agent.get("metrics", {}).get("timing_seconds", {}),
                        "real_model_accuracy": None, "business_answer_correctness": None,
                        "quality_scope": "frozen_critical_mechanism_assertions_not_general_business_accuracy",
                        "status": "passed" if all(checks.get(label) is True for label in case["assertions"]) else "failed",
                        "kind": case["kind"]}
                    samples.append(row)
                    (directory / f"sample-{number}.json").write_text(json.dumps(row, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    print(json.dumps({"case_id": case["id"], "sample": number, "checks": row["checks"],
                        "timings": row["timings"]}, ensure_ascii=False), flush=True)
    return samples


def run_cases(command, root, manifest, public_source, *, case_ids=None):
    """Preserve failed/missing samples as failures and continue independent cases."""
    manifest = json.loads(Path(manifest).read_text(encoding="utf-8")) if not isinstance(manifest, dict) else manifest
    results = []
    for case in manifest["cases"]:
        if case["owner"] != "cases" or case_ids is not None and case["id"] not in case_ids:
            continue
        require(not (Path(root) / case["id"]).exists(), "Acceptance case directory already exists; previous samples cannot be reused")
        try:
            results.extend(_run_cases(command, root, manifest, public_source, case_ids=[case["id"]]))
        except (AssertionError, OSError, ValueError, KeyError, StopIteration, sqlite3.Error) as error:
            directory = Path(root) / case["id"]
            for number in range(1, case["repeats"] + 1):
                saved = directory / f"sample-{number}.json"
                if saved.is_file():
                    results.append(json.loads(saved.read_text(encoding="utf-8")))
                else:
                    results.append({"id": case["id"], "turn_id": "1", "sample_index": number,
                        "run_id": None, "answer_sha256": None, "elapsed_seconds": None,
                        "timings": {}, "checks": {label: False for label in case["assertions"]},
                        "status": "failed", "kind": case["kind"], "error": type(error).__name__ + ": " + str(error),
                        "real_model_accuracy": None, "business_answer_correctness": None})
    return results
