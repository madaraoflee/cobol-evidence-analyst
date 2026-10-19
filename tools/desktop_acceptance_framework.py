"""One native HTTP probe for physical-page framework evidence continuity.

The executable supplies and binds every fact. This module only creates neutral
input and checks the observed wire protocol; it imports no product analyzers.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools.desktop_acceptance_cases import ranges_supplied, verify_pages
from tools.desktop_acceptance_runtime import LoopbackProvider, NativeWorkbench, require, supplied_pages, write_profile


QUESTION = "FRAME-ENTRY 如何调用 ROWSIO，以及何时停止？封闭实现未提供时哪些可见逻辑仍可解释？"
REFERENCE = """# Record processing R1.0

## Database I/O

CALL XXXXIO USING XXXX-PARAMS

The operation is selected by XXXX-FUNCTION.

### Operations

| Function | Meaning | Caution |
| --- | --- | --- |
| ADVANCE | Request the following record. | Inspect the returned status; runtime data, cursor and commit effects remain unknown. |
"""


def framework_sources():
    # Physical comments separate CALL from USING without adding executable
    # syntax. Their total length exceeds a source-read page, within 64 lines.
    entry = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. FRAME-ENTRY.\nDATA DIVISION.\n"
        "WORKING-STORAGE SECTION.\nCOPY ROW-LAYOUT.\nPROCEDURE DIVISION.\nREAD-STAGE.\n"
        "MOVE 'ADVANCE' TO ROWS-FUNCTION.\nCALL 'ROWSIO'\n"
        + "".join("*> neutral interface padding " + "x" * 300 + "\n" for _ in range(48))
        + "USING ROWS-PARAMS.\nIF ROWS-STATUS = 'DONE'\nMOVE 1 TO STOP-FLAG\nEND-IF.\nGOBACK.\n")
    return {"entry.cbl": entry,
        "ROW-LAYOUT.cpy": "01 ROWS-PARAMS.\n 05 ROWS-FUNCTION PIC X(8).\n"
                          " 05 ROWS-STATUS PIC X(4).\n01 STOP-FLAG PIC 9.\n"}


def fixture_identity():
    return {name: hashlib.sha256(text.encode()).hexdigest()
            for name, text in {**framework_sources(), "reference.md": REFERENCE}.items()}


def fixed_reads():
    total = len(framework_sources()["entry.cbl"].splitlines())
    return [{"relative_path": "entry.cbl", "start_line": start, "end_line": min(start + 19, total)}
            for start in range(1, total + 1, 20)] + [
                {"relative_path": "ROW-LAYOUT.cpy", "start_line": 1, "end_line": 4}]


def framework_supply_checks(payload):
    pages = supplied_pages(payload)
    versions = fixture_identity()
    entry = framework_sources()["entry.cbl"].splitlines()
    first, last = entry.index("CALL 'ROWSIO'") + 1, entry.index("USING ROWS-PARAMS.") + 1
    required = [{"path": "entry.cbl", "sha256": versions["entry.cbl"], "start_line": 5, "end_line": len(entry)},
                {"path": "ROW-LAYOUT.cpy", "sha256": versions["ROW-LAYOUT.cpy"], "start_line": 1, "end_line": 4}]
    # The oracle owns these obligations. A fact cannot narrow its own required
    # support by dropping the CALL, operation assignment or parameter layout.
    mandatory_support = [
        {**required[0], "start_line": first, "end_line": last},
        {**required[0], "start_line": entry.index("MOVE 'ADVANCE' TO ROWS-FUNCTION.") + 1,
         "end_line": entry.index("MOVE 'ADVANCE' TO ROWS-FUNCTION.") + 1},
        {**required[1], "end_line": 3},
    ]
    facts = [fact for fact in payload.get("framework_facts", []) if fact.get("target_name") == "ROWSIO"
        and fact.get("relative_path") == "entry.cbl" and fact.get("source_sha256") == versions["entry.cbl"]
        and fact.get("start_line") == first and fact.get("end_line") == last]
    fact = facts[0] if len(facts) == 1 else {}
    manual_lines = set(REFERENCE.splitlines())
    references = {ref.get("reference_id"): ref for ref in payload.get("framework_references", [])
                  if ref.get("text") and all(line in manual_lines for line in ref["text"].splitlines())
                  and not ref.get("text_truncated")
                  and ref.get("document_sha256") == versions["reference.md"]}
    cited_manual_lines = {line for identifier in fact.get("reference_ids", []) if identifier in references
                          for line in references[identifier]["text"].splitlines()}
    mandatory_manual_lines = {"CALL XXXXIO USING XXXX-PARAMS",
        "The operation is selected by XXXX-FUNCTION.",
        next(line for line in REFERENCE.splitlines() if line.startswith("| ADVANCE |"))}
    support_ids = fact.get("source_evidence_ids", [])
    supporting = [page for page in pages if page.get("evidence_id") in support_ids]
    support_ranges = [{"path": span.get("relative_path", fact.get("relative_path")),
                       "sha256": span.get("source_sha256", fact.get("source_sha256")),
                       "start_line": span["start_line"], "end_line": span["end_line"]}
                      for span in fact.get("source_ranges", [])]
    call_pages = [page for page in pages if page.get("relative_path") == "entry.cbl"
                  and page.get("source_sha256") == versions["entry.cbl"] and not page.get("span_truncated")]
    return {
        "completeness_framework_fact_supplied": bool(fact and fact.get("dependency_covered") is True
            and (fact.get("operation") or {}).get("value") == "ADVANCE" and fact.get("argument") == "ROWS-PARAMS"
            and fact.get("function_field") == "ROWS-FUNCTION" and ranges_supplied(pages, required)),
        "evidence_call_requires_multiple_pages": (ranges_supplied(call_pages, [{**required[0], "start_line": first, "end_line": last}])
            and not any(page["start_line"] <= first and page["end_line"] >= last for page in call_pages)),
        "evidence_framework_all_supports_supplied": bool(support_ids and support_ranges
            and set(support_ids) <= {page.get("evidence_id") for page in pages}
            and ranges_supplied(supporting, support_ranges)
            and ranges_supplied(supporting, mandatory_support)
            and fact.get("reference_ids") and set(fact["reference_ids"]) <= references.keys()
            and mandatory_manual_lines <= cited_manual_lines),
        "quality_closed_runtime_not_proven": bool(fact and fact.get("target_source_available") is False
            and fact.get("runtime_verified") is False and fact.get("return_status_binding") == "not_bound"
            and fact.get("effect_contract", {}).get("runtime_verified") is False
            and {"cursor", "transaction_result", "file_table_contents"}
                <= set(fact.get("effect_contract", {}).get("unknown_effects", []))),
    }


def run_framework_page_chain(command, root, case, sample_index):
    from tools.desktop_acceptance_scenarios import _row
    source = root / "source"
    source.mkdir()
    require(fixture_identity() == case["fixture_sha256"], "Frozen framework fixture changed")
    for name, text in framework_sources().items():
        (source / name).write_bytes(text.encode())
    manual = root / "reference.md"
    manual.write_bytes(REFERENCE.encode())
    output = root / "workspace"
    observed = {}
    expected_answer = []

    def respond(_envelope, payload, index):
        require(index < 2 and payload["question"] == QUESTION, "Unexpected framework request")
        verify_pages(source, supplied_pages(payload))
        if index == 0:
            return json.dumps({"read": fixed_reads()})
        observed.update(framework_supply_checks(payload))
        require(all(observed.values()), "Framework supply checks failed: " + str(observed))
        completed = payload.get("completed_actions", [])
        observed["completeness_fixed_reads_completed"] = all(any(
            action.get("read") == wanted and action.get("range_complete") is True for action in completed)
            for wanted in fixed_reads())
        require(observed["completeness_fixed_reads_completed"], "Fixed source reads did not complete")
        fact = next(fact for fact in payload["framework_facts"] if fact.get("target_name") == "ROWSIO")
        citations = "".join("[" + identifier + "]" for identifier in fact["source_evidence_ids"] + fact["reference_ids"])
        answer = ("可见源码将 ADVANCE 放入 ROWS-FUNCTION，调用 ROWSIO 并传入 ROWS-PARAMS；"
                "ROWS-STATUS 为 DONE 时将 STOP-FLAG 设为 1。" + citations
                + "封闭实现未提供，实际返回值、读取记录、游标与提交结果未知。")
        expected_answer.append(answer)
        return answer

    with LoopbackProvider(root / "provider", respond) as provider:
        environment = write_profile(root / "profile", provider, case["policy"])
        with NativeWorkbench(command, root / "profile", environment, root) as workbench:
            indexed = workbench.analyze(source, output, framework_reference_path=str(manual))
            require(indexed["job"]["status"] == "COMPLETED" and not provider.requests, "Framework indexing failed or called provider")
            observation = workbench.analyze(source, output, QUESTION, framework_reference_path=str(manual))
            require(observation["job"]["status"] == "COMPLETED" and not provider.errors,
                    "Framework question failed: " + str(provider.errors))
            agent = (observation.get("result") or {}).get("agent_result") or {}
            require(agent.get("model_answer_recorded") is True and agent.get("metrics", {}).get("model_requests") == 2,
                    "Framework question did not record the two-request exchange")
            answer = agent.get("answer", "")
            observed["quality_closed_runtime_not_proven"] &= agent.get("claims_semantically_verified") is False
            observed.update({
                "quality_visible_caller_logic_preserved": expected_answer == [answer],
                "evidence_framework_pages_exact": bool(verify_pages(source, provider.requests[-1]["source_pages"])),
                "evidence_visible_answer_matches": observation["state"]["conversation"]["messages"][-1]["content"] == answer,
            })
            row = _row(case, sample_index, observation, provider, cold=indexed["elapsed_seconds"], checks=observed)
            row["timings"]["first_provider_seconds"] = provider.requests[0]["received_at"] - observation["started_at"]
            row["model_requests"] = agent["metrics"]["model_requests"]
            row["fixture_sha256"] = fixture_identity()
            return [row]
