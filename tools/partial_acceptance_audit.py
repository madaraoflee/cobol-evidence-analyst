"""Explain partial answers from saved reports without changing their acceptance.

An absent diagnostic remains absent. A trace from a different run is never
attached by case name, answer text, or source path. Supplemental source replays
are separate observations and cannot close a native executable investigation.
This tool reads local files only; it never invokes a model or starts a process.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re


SCHEMA = "partial-acceptance-audit/v1"
IDENTITY_FIELDS = ("source_commit", "application_sha256", "archive_sha256", "binary_sha256",
                   "manifest_sha256", "execution_surface", "runtime_platform", "transport_kind")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def trace_index(root):
    """Only business traces; duplicate run ids with different data are invalid."""
    index = {}
    if root is None:
        return index
    for path in sorted(Path(root).rglob("*.json")):
        if "quality-traces" not in path.parts:
            continue
        data = read_json(path)
        if data.get("schema_version") != "business-quality-trace/v1":
            continue
        run_id = data.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("TRACE_RUN_ID_MISSING")
        if run_id in index and index[run_id]["sha256"] != digest(path):
            raise ValueError("TRACE_RUN_ID_CONFLICT:" + run_id)
        index[run_id] = {"data": data, "sha256": digest(path)}
    return index


def classify_gap(gap):
    """A failed input search does not establish a real missing input value."""
    reason = str(gap.get("reason", ""))
    if reason in {"external_implementation_unavailable", "runtime_target_unresolved"}:
        return "external_semantics_unresolved"
    if "budget" in reason or reason == "input_candidate_frontier":
        return "local_candidate_frontier_incomplete"
    if reason == "input_dependency_cycle":
        return "candidate_cycle_requires_order_and_flow_review"
    if reason == "input_source_not_located":
        return "input_origin_search_incomplete_or_value_unspecified"
    return "unclassified_obligation_requires_review"


def _claim_observations(ledger):
    observations = []
    for entry in ledger.get("entries", []):
        if entry.get("support_status") == "supported":
            continue
        revisions = entry.get("revised_claims", [])
        observations.append({key: entry.get(key) for key in
            ("claim_id", "claim_type", "target_entity", "reason_code", "support_status",
             "evidence_ids", "invalid_evidence_ids", "occurrence_ids", "source_versions", "text")})
        observations[-1]["supported_local_replacement_recorded"] = bool(revisions) and all(
            revision.get("support_status") == "supported" for revision in revisions)
        observations[-1]["final_visible_claim_is_unresolved"] = not observations[-1][
            "supported_local_replacement_recorded"]
    return observations


def _plan_summary(plan):
    if not isinstance(plan, dict):
        return plan
    result = dict(plan)
    paths = plan.get("paths")
    if isinstance(paths, list):
        result["paths"] = paths[:32]
        result["path_count"] = len(paths)
        result["paths_truncated"] = len(paths) > 32
        result["all_paths_sha256"] = hashlib.sha256(json.dumps(
            paths, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


def _answer_sha(row):
    answer = row.get("answer")
    recorded = row.get("answer_sha256")
    if isinstance(answer, str):
        actual = hashlib.sha256(answer.encode()).hexdigest()
        if recorded is not None and recorded != actual:
            raise ValueError("REPORT_ANSWER_HASH_MISMATCH")
        return actual
    return recorded


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _diagnostics(row, traces):
    """Accept an in-report capture or an exact-run, exact-answer local trace."""
    answer_sha = _answer_sha(row)
    captured = row.get("acceptance_diagnostics")
    if isinstance(captured, dict):
        final = captured.get("final")
        if (captured.get("schema_version") != "executable-answer-diagnostics/v1"
                or not isinstance(row.get("run_id"), str) or not row["run_id"].strip()
                or not re.fullmatch(r"[0-9a-f]{64}", answer_sha or "")
                or captured.get("run_id") != row["run_id"]
                or captured.get("answer_sha256") != answer_sha
                or captured.get("agent_status") != row.get("agent_status")
                or not re.fullmatch(r"[0-9a-f]{64}", captured.get("trace_sha256") or "")
                or not isinstance(final, dict)
                or captured.get("final_sha256") != _fingerprint(final)
                or row.get("stop_reason") is not None and captured.get("stop_reason") != row["stop_reason"]
                or final.get("stop_reason") is not None and captured.get("stop_reason") != final["stop_reason"]):
            raise ValueError("CAPTURED_DIAGNOSTIC_IDENTITY_MISMATCH")
        return {"run_id": captured["run_id"], "answer_sha256": answer_sha,
            "stop_reason": captured.get("stop_reason"), "answer_completion": final.get("answer_completion"),
            "question_investigation": final.get("question_investigation"),
            "open_tasks": final.get("open_tasks"), "missing_core_evidence_ids": final.get("missing_core_evidence_ids"),
            "claim_ledger": captured.get("claim_ledger")}, {
                "kind": "same_report_executable_capture", "sha256": captured["trace_sha256"]}
    embedded = row.get("partial_diagnostics")
    if isinstance(embedded, dict):
        if (not isinstance(row.get("run_id"), str) or not row["run_id"].strip()
                or not re.fullmatch(r"[0-9a-f]{64}", answer_sha or "")
                or embedded.get("run_id") != row.get("run_id")
                or embedded.get("answer_sha256") != answer_sha):
            raise ValueError("EMBEDDED_DIAGNOSTIC_IDENTITY_MISMATCH")
        return embedded, {"kind": "same_report_capture"}
    trace = traces.get(row.get("run_id"))
    if trace is None:
        return {}, {"kind": "not_archived", "matching_run_found": False}
    data = trace["data"]
    ledger = data.get("claim_ledger", {})
    if (not re.fullmatch(r"[0-9a-f]{64}", answer_sha or "")
            or ledger.get("answer_sha256") != answer_sha):
        raise ValueError("TRACE_ANSWER_IDENTITY_MISMATCH:" + str(row.get("run_id")))
    final = data.get("final", {})
    return {"run_id": data["run_id"], "answer_sha256": ledger["answer_sha256"],
            "stop_reason": final.get("stop_reason"),
            "answer_completion": final.get("answer_completion"),
            "question_investigation": final.get("question_investigation"),
            "open_tasks": final.get("open_tasks"),
            "missing_core_evidence_ids": final.get("missing_core_evidence_ids"),
            "claim_ledger": ledger}, {"kind": "exact_run_and_answer_trace", "sha256": trace["sha256"]}


def audit_report(report, *, traces=None, observation_kind="original"):
    """Return one record per partial sample, retaining nested conversation turns."""
    if not isinstance(report.get("cases"), list):
        raise ValueError("REPORT_CASES_MISSING")
    rows = []
    seen = set()
    for row in report["cases"]:
        identity = (row.get("id"), row.get("turn_id"), row.get("sample_index"))
        if identity in seen:
            raise ValueError("REPORT_SAMPLE_DUPLICATED")
        seen.add(identity)
        if row.get("agent_status") != "PARTIAL":
            continue
        diagnostic, origin = _diagnostics(row, traces or {})
        stop_reason = diagnostic.get("stop_reason") or row.get("stop_reason")
        completion = diagnostic.get("answer_completion")
        investigation = diagnostic.get("question_investigation")
        gaps = (investigation or {}).get("open_gaps", [])
        ledger = diagnostic.get("claim_ledger") or row.get("claim_ledger", {})
        reasons = []
        if stop_reason == "answer_incomplete":
            reasons.append("answer_coverage_incomplete")
        elif stop_reason in {"question_evidence_incomplete", "request_budget"}:
            reasons.append("investigation_obligations_open")
        elif stop_reason:
            reasons.append("other_recorded_partial_reason")
        else:
            reasons.append("partial_reason_not_archived")
        reasons.extend(classify_gap(gap) for gap in gaps)
        if completion and completion.get("missing_aspects"):
            reasons.append("supplied_aspects_not_explained_by_answer")
        missing = [name for name, value in (("answer_completion", completion),
            ("question_investigation", investigation), ("claim_ledger", ledger)) if not isinstance(value, dict) or not value]
        if not stop_reason:
            missing.append("stop_reason")
        if diagnostic.get("missing_core_evidence_ids"):
            reasons.append("critical_evidence_not_supplied")
        if diagnostic.get("open_tasks"):
            reasons.append("source_reads_not_finished")
        turn_rows = row.get("turns", [])
        turn_audit = audit_report({"cases": turn_rows}, traces=traces,
                                 observation_kind=observation_kind)["samples"] if turn_rows else []
        rows.append({"case_id": row.get("id"), "turn_id": row.get("turn_id"),
            "sample_index": row.get("sample_index"), "run_id": row.get("run_id"),
            "answer_sha256": _answer_sha(row), "observation_kind": observation_kind,
            "diagnostic_origin": origin, "recorded_stop_reason": stop_reason,
            "reason_categories": sorted(set(reasons)), "answer_completion": completion,
            "required_items": (investigation or {}).get("required_items"),
            "analysis_plan": _plan_summary((investigation or {}).get("analysis_plan")), "open_gaps": gaps,
            "claim_observations": _claim_observations(ledger), "missing_diagnostic_fields": missing,
            "mechanism_assertions_passed": bool(row.get("checks")) and all(
                value is True for value in row["checks"].values()),
            "semantic_support_scope": ledger.get("scope"), "turns": turn_audit,
            "root_cause_closed": False,
            "closure_requirement": "same_executable_real_answer_and_identity_bound_review",
            "independent_source_facts_remain_available": True})
    return {"identity": {key: report.get(key) for key in IDENTITY_FIELDS},
            "sample_count": len(report["cases"]), "partial_count": len(rows),
            "diagnostic_complete_count": sum(not row["missing_diagnostic_fields"] for row in rows),
            "recorded_stop_reasons": dict(Counter(row["recorded_stop_reason"] or "not_archived" for row in rows)),
            "samples": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--trace-root", type=Path)
    parser.add_argument("--supplemental-report", action="append", type=Path, default=[])
    parser.add_argument("--supplemental-trace-root", action="append", type=Path, default=[])
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    if len(args.supplemental_report) != len(args.supplemental_trace_root):
        parser.error("Each supplemental report needs its own trace root.")
    data = {"schema_version": SCHEMA, "read_only": True, "external_model_calls": 0,
        "original_report_sha256": digest(args.report),
        "original": audit_report(read_json(args.report), traces=trace_index(args.trace_root)),
        "supplemental_observations": [], "release_qualified": False,
        "interpretation": "diagnostic_audit_not_business_accuracy_or_release_acceptance"}
    for report_path, trace_root in zip(args.supplemental_report, args.supplemental_trace_root):
        supplemental = audit_report(read_json(report_path), traces=trace_index(trace_root),
                                    observation_kind="supplemental_separate_run")
        supplemental["report_sha256"] = digest(report_path)
        supplemental["may_close_original_samples"] = False
        data["supplemental_observations"].append(supplemental)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"partial_count": data["original"]["partial_count"],
        "diagnostic_complete_count": data["original"]["diagnostic_complete_count"],
        "root_causes_closed": 0, "release_qualified": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
