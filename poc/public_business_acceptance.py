"""Offline public-source business acceptance through the default chat pipeline.

Inputs are an external fixed source checkout and a source-reviewed manifest.
Every response comes from an in-process fake transport. This checks mechanisms
and golden source assertions, not real-model accuracy or compiled execution.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
import tempfile
import time

from agent_policy import AgentPolicy
from business_chat import run_business_chat, _SYSTEM
from business_index import build_business_index
from claim_support import check_statement_support, supplied_statement_facts
from company_api import CompanyAPIConfig, TransportResponse
from compilation_context import CompilationContext
from live_business_eval import _review_identity
from repository_discovery import ensure_repository_search
from source_reading import _identify_page, _safe_file


DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "docs/eval/public-business-v1/manifest.json"


def _answer_coverage(answer, checks):
    """Reviewed lexical expectations are completeness signals, not model scores."""
    rows = [{"id": check["id"], "covered": bool(check.get("all_of"))
             and all(re.search(pattern, answer, re.I | re.S) is not None for pattern in check["all_of"])
             and all(re.search(pattern, answer, re.I | re.S) is None for pattern in check.get("none_of", ()))}
            for check in checks]
    return {"method": "source_reviewed_answer_patterns", "evaluated": bool(rows), "checks": rows,
            "passed": bool(rows) and all(row["covered"] for row in rows)}


def _source_expectations(page, facts, checks):
    """Require positive source witnesses even for non-arithmetic questions."""
    from structural_index import normalize_cobol_lines
    from statement_facts import CobolLexError, lex_cobol
    lines, _ = normalize_cobol_lines(page["source_text"], "fixed")
    try:
        values = tuple(token.value for token in lex_cobol("\n".join(line.text for line in lines)))
    except CobolLexError:
        values = ()
    rows = []
    for check in checks:
        if check.get("kind") == "source_tokens":
            wanted = tuple(check.get("tokens", ()))
            supported = bool(wanted) and any(values[index:index + len(wanted)] == wanted for index in range(len(values)))
        elif check.get("kind") == "source_fact":
            wanted = check.get("fact") or {}
            supported = bool(wanted) and any(all(fact.get(key) == value for key, value in wanted.items()) for fact in facts)
        else:
            supported = False
        rows.append({"id": check["id"], "kind": check.get("kind"), "supported": supported})
    return rows


def run_acceptance(source_root, manifest_path=DEFAULT_MANIFEST, *, output_path=None):
    source_root = Path(source_root).resolve(strict=True)
    manifest_path = Path(manifest_path).resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = json.loads((manifest_path.parent / manifest["cases_file"]).read_text(encoding="utf-8"))["cases"]
    for row in manifest["files"]:
        if hashlib.sha256(_safe_file(source_root, row["path"]).read_bytes()).hexdigest() != row["sha256"]:
            raise ValueError("PUBLIC_CORPUS_SOURCE_VERSION_MISMATCH")
    context = CompilationContext(encoding="utf-8", source_format="fixed", copy_libraries=("app/cpy",),
        copy_library_order_known=True)
    report = {"schema_version": "public-business-acceptance/v1", "source_commit": manifest["commit"],
        "source_files": manifest["files"], "transport": "in_process_fake", "model_accuracy_measured": False,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "cases_sha256": hashlib.sha256((manifest_path.parent / manifest["cases_file"]).read_bytes()).hexdigest(),
        "external_download_archive_sha256": manifest.get("download_archive_sha256"),
        "compilation_context": context.to_dict(), "compilation_context_sha256": context.fingerprint,
        "compiled_execution_validated": False, "enterprise_target_dialect_validated": False,
        "cases": [], "parser_trials": {}}
    config = CompanyAPIConfig("https://gateway.example.invalid/v1", "offline-test-model", api_key="local-test-only")
    policy = AgentPolicy(max_model_requests=1, max_answer_revisions=0)
    with tempfile.TemporaryDirectory(prefix="public-business-acceptance-") as directory:
        database = Path(directory) / "index.sqlite"
        index = build_business_index(source_root, database, include_paths=[row["path"] for row in manifest["files"]],
                             encoding="utf-8", source_format="fixed", verify_content=True, compilation_context=context)
        report["index"] = {key: index[key] for key in ("snapshot_id", "parser_version", "source_options")}
        ensure_repository_search(database, source_root)
        for case in cases:
            mechanism = case["mechanism"]
            location = mechanism["evidence_range"]
            path = _safe_file(source_root, location["path"])
            lines = path.read_text(encoding="utf-8").splitlines()
            page = {"relative_path": location["path"], "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "start_line": location["start_line"], "end_line": location["end_line"],
                "source_text": "\n".join(lines[location["start_line"] - 1:location["end_line"]]), "span_truncated": False}
            page["evidence_id"] = _identify_page(page)
            # Full compilation syntax supplies the procedure context; only
            # occurrences inside the independently reviewed range are scored.
            full_page = {**page, "start_line": 1, "end_line": len(lines), "source_text": "\n".join(lines)}
            full_page["evidence_id"] = _identify_page(full_page)
            all_facts = supplied_statement_facts(full_page, source_format="fixed")
            facts = [fact for fact in all_facts
                     if location["start_line"] <= fact["start_line"] <= fact["end_line"] <= location["end_line"]]
            static_checks = []
            for assertion in mechanism["source_assertions"]:
                verdicts = [check_statement_support(assertion, fact, full_page) for fact in facts]
                static_checks.append({"assertion": assertion,
                    "supported": any(verdict["support_status"] == "supported" for verdict in verdicts)})
            source_checks = _source_expectations(full_page, all_facts, mechanism.get("source_checks", ()))
            positive_source = bool(static_checks or source_checks) and all(
                check["supported"] for check in [*static_checks, *source_checks])
            for branch, answer in (("correct", mechanism["fake_correct_answer"]),
                                   ("incorrect", mechanism.get("fake_incorrect_answer"))):
                if answer is None:
                    continue
                requests, supplied_ranges = [], []
                def transport(request):
                    payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
                    requests.append(payload)
                    pages = [entry for bundle in payload["source_context"] for entry in bundle.get("pages", [])]
                    relevant = [entry for entry in pages if entry["relative_path"] == location["path"]
                        and entry["start_line"] <= location["start_line"] and entry["end_line"] >= location["end_line"]]
                    if not relevant:
                        raise AssertionError("PUBLIC_BUSINESS_CORE_EVIDENCE_NOT_SUPPLIED")
                    evidence = min(relevant, key=lambda entry: entry["end_line"] - entry["start_line"])
                    supplied_ranges.extend({key: entry[key] for key in
                        ("evidence_id", "relative_path", "source_sha256", "start_line", "end_line")} for entry in pages)
                    text = answer.replace("[EVIDENCE]", "[" + evidence["evidence_id"] + "]")
                    return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": text},
                        "finish_reason": "stop"}]}, ensure_ascii=False))
                started = time.monotonic()
                output = run_business_chat(case["question"], database, source_root, config,
                    framework_reference_path="", transport=transport, policy=policy)
                result = output["agent_result"]
                trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text(encoding="utf-8"))
                final_round_ids = set(trace.get("final", {}).get("final_answer_round_ids", []))
                final_round_ids.add(trace.get("final", {}).get("final_answer_round_id"))
                final_rounds = [entry for entry in trace["rounds"] if entry["round_id"] in final_round_ids]
                actual_context = {kind: [entry for round_entry in final_rounds for entry in round_entry.get(kind, [])]
                                  for kind in ("sources", "framework")}
                ledger = result.get("claim_ledger", {})
                row = {"case_id": case["id"], "branch": branch, "question": case["question"],
                    "pipeline_version": trace["pipeline_version"],
                    "review_identity": _review_identity(run_id=trace["run_id"], case_id=case["id"], turn_id="1",
                        branch=branch, answer=result["answer"], model=config.chat_model,
                        prompt={"version": trace["prompt_version"], "system_sha256": hashlib.sha256(_SYSTEM.encode()).hexdigest(),
                                "request_sha256": [entry.get("request_body_sha256") for entry in final_rounds]},
                        policy=policy.to_dict(), gold=case.get("gold") or {}, context=actual_context,
                        source_snapshot_id=trace.get("analysis_revision", index["snapshot_id"]),
                        evidence_status="supplied" if final_rounds else "unavailable", question=case["question"]),
                    "static_rule_checks": static_checks,
                    "static_rules_evaluated": bool(static_checks),
                    "static_rules_supported": bool(static_checks) and all(check["supported"] for check in static_checks),
                    "positive_source_checks": source_checks, "positive_source_supported": positive_source,
                    "answer_coverage": _answer_coverage(result["answer"], mechanism.get("answer_checks", ())),
                    "source_fact_completion": trace.get("source_fact_completion", {}),
                    "default_answer": result["answer"], "ledger": ledger,
                    "supplied_evidence": supplied_ranges, "source_evidence_fingerprint": hashlib.sha256(
                        json.dumps(supplied_ranges, sort_keys=True).encode()).hexdigest(),
                    "model_requests": len(requests), "elapsed_seconds": round(time.monotonic() - started, 4),
                    "stop_reason": result.get("stop_reason"), "status": result.get("status")}
                if branch == "incorrect":
                    row["incorrect_claim_revised"] = ledger.get("revised_claim_count", 0) > 0
                    row["mechanism_passed"] = (row["incorrect_claim_revised"] and positive_source
                                               and row["answer_coverage"]["passed"])
                else:
                    row["correct_claims_preserved"] = ledger.get("counts", {}).get("contradicted", 0) == 0
                    row["mechanism_passed"] = (positive_source and bool(supplied_ranges)
                        and row["correct_claims_preserved"] and row["answer_coverage"]["passed"])
                report["cases"].append(row)
        from parser_adapter import IsolatedParserAdapter, PROLEAP, CHE4Z
        for candidate in (PROLEAP, CHE4Z):
            trial = IsolatedParserAdapter(candidate).parse("MOVE INPUT-VALUE TO RESULT-VALUE.", relative_path="trial.cbl")
            report["parser_trials"][candidate.name] = {"version": candidate.version,
                "availability": "not_configured_for_workbench" if trial.diagnostics else "available", "diagnostics": trial.diagnostics,
                "native_parser_executed": False}
    report["mechanisms_passed"] = all(row["mechanism_passed"] for row in report["cases"])
    if output_path:
        output = Path(output_path).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run source-hash-bound public business checks without model network requests.")
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args(argv)
    report = run_acceptance(arguments.source_root, arguments.manifest, output_path=arguments.output)
    print(json.dumps({"report": str(arguments.output.resolve()), "mechanisms_passed": report["mechanisms_passed"],
                      "cases": len(report["cases"]), "model_accuracy_measured": False}))
    return 0 if report["mechanisms_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
