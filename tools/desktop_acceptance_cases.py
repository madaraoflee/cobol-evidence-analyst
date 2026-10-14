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

from tools.desktop_acceptance_runtime import LoopbackProvider, NativeWorkbench, supplied_pages, write_profile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "docs/eval/desktop-joint-v1/manifest.json"
PUBLIC_MANIFEST = ROOT / "docs/eval/public-business-v1/manifest.json"
PUBLIC_CASES = ROOT / "docs/eval/public-business-v1/cases.json"


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
        source_root = DEFAULT_MANIFEST.parent / "source"
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


def _quality_checks(case, agent, output, pages):
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
    return result


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
                    checks = _quality_checks(case, agent, output, pages)
                    if case["id"] == "forward_impact":
                        groups = [group for request in requests for group in request["payload"].get("evidence_groups", [])]
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
                        checks["completeness_reviewed_ranges_supplied"] = ranges_supplied(pages, _required_ranges(case, source))
                    row = {"id": case["id"], "turn_id": "1", "sample_index": number,
                        "run_id": trace["run_id"], "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(),
                        "elapsed_seconds": response["elapsed_seconds"], "index_reused": number > 1,
                        "question_index_reused": diagnosis.get("index_reused") is True,
                        "timings": {"cold_index_seconds": cold_seconds if number == 1 else None,
                            "first_provider_seconds": requests[0]["received_at"] - started,
                            "question_seconds": response["elapsed_seconds"]},
                        "checks": {label: checks.get(label, False) for label in case["assertions"]},
                        "corpus": corpus, "answer": answer, "agent_status": agent.get("status"),
                        "stop_reason": agent.get("stop_reason"), "claim_ledger": ledger, "review_identity": {
                            "run_id": trace["run_id"], "pipeline_version": trace.get("pipeline_version"),
                            "prompt_version": trace.get("prompt_version"), "configuration": trace.get("configuration"),
                            "request_sha256": [entry["request_sha256"] for entry in final_requests]},
                        "evidence": evidence, "source_evidence_sha256": hashlib.sha256(json.dumps(
                            evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                        "model_requests": len(requests), "transport": "loopback_https_fake",
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
