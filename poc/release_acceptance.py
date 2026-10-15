#!/usr/bin/env python3
"""Verify a frozen desktop package and its jointly bound acceptance evidence.

This command reads local artifacts only. Candidate acceptance proves synthetic
mechanisms; publishing additionally requires native remote-model answers and
human/office reviews of that exact artifact. It never makes model requests.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import zipfile

from live_business_eval import (_cases, _evidence_metadata, _fingerprint,
                                _safe_write, _turns, apply_reviews)


SCHEMA = "release-acceptance/v1"
POLICY_SCHEMA = "release-acceptance-policy/v1"
COMPILED_SCHEMA = "compiled-workbench-acceptance/v1"
MANIFEST_SCHEMA = "desktop-joint-acceptance/v1"
BINARY_MEMBER = "COBOLWorkbench/COBOLWorkbench/COBOLWorkbench.exe"
INCOMPLETE_ANSWER_CODES = {"QUESTION_TIME_BUDGET_EXHAUSTED", "REMOTE_REQUEST_OUTCOME_UNKNOWN",
                           "MODEL_OUTPUT_TRUNCATED"}
OFFICE_BASELINE_CHECKS = ("desktop_start", "source_import", "conversation_recovery",
                          "cancellation_recovery", "historical_evidence", "company_api_ca")
OFFICE_REGRESSION_BASELINE = ("add_giving", "end_words", "same_line_occurrences",
    "display_literal_relations", "literal_comment_marker", "qualified_identity", "perform_section",
    "bare_group_field", "qualified_in_alias", "ambiguous_group_field",
    "activation_trigger", "caller_condition", "external_scheduler_unknown",
    "ibmi_cl_batch", "cl_library_list", "cl_file_override", "cl_late_target",
    "wrong_formula_999", "input_origin", "value_initialization_source", "page_account_multiturn",
    "later_overwrite", "three_turn_focus", "forward_slice",
    "static_a_b_c", "parameter_binding", "size_error", "decimal_scale", "unknown_effect",
    "independent_direct_fact", "legal_continuation", "evaluate_merge", "public_limit_rejection",
    "public_account_update", "public_monthly_interest", "public_file_record_mapping",
    "public_unimplemented_procedure")
PARTIAL_CLASSIFICATIONS = {"scope_mismatch", "answer_omission", "evidence_gap",
                           "explicit_unknown", "unsupported_semantics"}


def file_sha256(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _sha(value, length=64):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None


def _number(value):
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _read(path):
    result = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError("ACCEPTANCE_DOCUMENT_MUST_BE_OBJECT")
    return result


def _check(errors, condition, code, context=None):
    if not condition:
        errors.append({"code": code, **({"case": context} if context else {})})
    return condition


def _valid_answer_diagnostics(row):
    diagnostics = row.get("acceptance_diagnostics")
    if not isinstance(diagnostics, dict):
        return False
    answer_sha = row.get("answer_sha256") or hashlib.sha256(row.get("answer", "").encode()).hexdigest()
    final = diagnostics.get("final")
    return (diagnostics.get("schema_version") == "executable-answer-diagnostics/v1"
        and _text(row.get("answer")) and hashlib.sha256(row["answer"].encode()).hexdigest() == answer_sha
        and diagnostics.get("run_id") == row.get("run_id")
        and diagnostics.get("answer_sha256") == answer_sha
        and row.get("agent_status") in {"ANALYZED", "PARTIAL"}
        and diagnostics.get("agent_status") == row.get("agent_status")
        and _sha(diagnostics.get("trace_sha256")) and isinstance(final, dict)
        and isinstance(final.get("question_investigation"), dict)
        and isinstance(final.get("answer_completion"), dict)
        and isinstance(final["question_investigation"].get("required_items"), list)
        and isinstance(final["question_investigation"].get("open_gaps"), list)
        and _text(final["answer_completion"].get("status"))
        and isinstance(final["answer_completion"].get("missing_aspects"), list)
        and _text(final.get("stop_reason"))
        and final["stop_reason"] == row.get("stop_reason") == diagnostics.get("stop_reason")
        and diagnostics.get("final_sha256") == _fingerprint(final))


def _package_identity(package, compiled, policy, errors):
    digest = file_sha256(package)
    binary_path = compiled.get("binary_path")
    identity = {"archive_sha256": digest, "source_commit": compiled.get("source_commit"),
                "binary_path": binary_path, "binary_sha256": None, "application_sha256": None}
    _check(errors, _sha(policy.get("source_commit"), 40)
           and compiled.get("source_commit") == policy.get("source_commit"), "SOURCE_COMMIT_MISMATCH")
    _check(errors, compiled.get("source_dirty") is False, "SOURCE_NOT_FROZEN")
    _check(errors, compiled.get("archive_sha256") == digest, "ARCHIVE_HASH_MISMATCH")
    safe_path = binary_path == BINARY_MEMBER
    _check(errors, safe_path, "BINARY_PATH_INVALID")
    with zipfile.ZipFile(package) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if not _check(errors, len(names) <= 4000 and len(names) == len({name.casefold() for name in names}), "ARCHIVE_DUPLICATE_MEMBERS"):
            return identity
        if not _check(errors, sum(entry.file_size for entry in entries) <= 1024 ** 3, "ARCHIVE_EXPANDED_SIZE_INVALID"):
            return identity
        for entry in entries:
            path = PurePosixPath(entry.orig_filename)
            if not _check(errors, not path.is_absolute() and ".." not in path.parts
                          and "\\" not in entry.filename and ":" not in entry.filename
                          and entry.orig_filename == entry.filename
                          and not stat.S_ISLNK(entry.external_attr >> 16), "ARCHIVE_UNSAFE_MEMBER"):
                return identity
        _check(errors, archive.testzip() is None, "ARCHIVE_CRC_FAILED")
        metadata_name = "COBOLWorkbench/BUILD-INFO.json"
        if not _check(errors, metadata_name in names, "BUILD_INFO_MISSING"):
            return identity
        metadata = json.loads(archive.read(metadata_name))
        if not _check(errors, isinstance(metadata, dict), "BUILD_INFO_INVALID"):
            return identity
        _check(errors, metadata.get("target") == "windows-x64", "PACKAGE_PLATFORM_MISMATCH")
        checks = metadata.get("self_test") or {}
        _check(errors, isinstance(checks, dict) and checks.get("ok") is True
               and checks.get("model_calls") == 0 and checks.get("desktop_dependencies") is True
               and checks.get("json_accelerated") is True, "COMPILED_SELF_TEST_MISSING")
        fingerprint = metadata.get("source_fingerprint")
        identity["application_sha256"] = fingerprint
        _check(errors, _sha(fingerprint) and compiled.get("application_sha256") == fingerprint,
               "APPLICATION_HASH_MISMATCH")
        _check(errors, metadata.get("source_commit") == policy.get("source_commit")
               and metadata.get("source_dirty") is False, "BUILD_COMMIT_MISMATCH")
        if safe_path and _check(errors, binary_path in names, "BINARY_MISSING"):
            binary = archive.read(binary_path)
            identity["binary_sha256"] = hashlib.sha256(binary).hexdigest()
            _check(errors, compiled.get("binary_sha256") == identity["binary_sha256"], "BINARY_HASH_MISMATCH")
            valid_pe = len(binary) > 64 and binary[:2] == b"MZ"
            if valid_pe:
                offset = struct.unpack_from("<I", binary, 0x3C)[0]
                valid_pe = (offset + 6 <= len(binary) and binary[offset:offset + 4] == b"PE\0\0"
                            and struct.unpack_from("<H", binary, offset + 4)[0] == 0x8664)
            _check(errors, valid_pe, "BINARY_NOT_WINDOWS_X64")
    return identity


def _compiled_acceptance(compiled, manifest, manifest_sha256, errors):
    _check(errors, compiled.get("schema_version") == COMPILED_SCHEMA, "COMPILED_SCHEMA_INVALID")
    _check(errors, manifest.get("schema_version") == MANIFEST_SCHEMA, "MANIFEST_SCHEMA_INVALID")
    _check(errors, compiled.get("manifest_sha256") == manifest_sha256, "COMPILED_MANIFEST_MISMATCH")
    _check(errors, compiled.get("execution_surface") == "native_windows_http"
           and compiled.get("runtime_platform") == "Windows", "COMPILED_EXECUTION_SURFACE_INVALID")
    _check(errors, compiled.get("transport_kind") == "synthetic", "COMPILED_TRANSPORT_INVALID")
    _check(errors, _text(compiled.get("report_execution_id")), "COMPILED_RUN_ID_MISSING")
    _check(errors, compiled.get("selection_complete") is True and compiled.get("errors") == []
           and compiled.get("status") == "passed", "COMPILED_RUN_INCOMPLETE")
    specs = manifest.get("cases")
    rows = compiled.get("cases")
    if not _check(errors, isinstance(specs, list) and bool(specs)
                  and isinstance(rows, list) and all(isinstance(row, dict) for row in rows),
                  "COMPILED_CASES_INVALID"):
        return []
    expected, seen_ids = {}, set()
    for spec in specs:
        if not isinstance(spec, dict):
            _check(errors, False, "MANIFEST_CASE_INVALID")
            continue
        case_id, repeats = spec.get("id"), spec.get("repeats")
        if not _check(errors, _text(case_id) and case_id not in seen_ids
                      and type(repeats) is int and 1 <= repeats <= 100, "MANIFEST_CASE_INVALID"):
            continue
        seen_ids.add(case_id)
        assertions = spec.get("assertions")
        groups = spec.get("assertion_groups") or {}
        valid_assertions = (isinstance(assertions, list) and bool(assertions)
                            and all(_text(label) for label in assertions)
                            and len(assertions) == len(set(assertions)))
        _check(errors, valid_assertions, "MANIFEST_ASSERTIONS_INVALID", case_id)
        kind = spec.get("kind")
        required_groups = ("quality", "completeness", "evidence") if kind == "answer" else ("lifecycle",)
        valid_groups = kind in {"answer", "lifecycle"} and isinstance(groups, dict) and valid_assertions
        if valid_groups:
            valid_groups = all(isinstance(groups.get(group), list) and bool(groups[group])
                and all(label in assertions for label in groups[group]) for group in required_groups)
        _check(errors, valid_groups, "MANIFEST_ASSERTION_GROUPS_MISSING", case_id)
        limits = spec.get("limits") or {}
        _check(errors, isinstance(limits, dict) and all(_number(limits.get(key)) and limits[key] > 0
               for key in ("cold_index_seconds", "first_provider_seconds", "question_seconds")),
               "MANIFEST_LIMITS_INVALID", case_id)
        for sample in range(1, repeats + 1):
            expected[(case_id, str(spec.get("turn_id", "1")), sample)] = spec
    actual, duplicates = {}, False
    for row in rows:
        key = (row.get("id"), row.get("turn_id"), row.get("sample_index"))
        if not all(isinstance(value, (str, int)) for value in key) or type(key[2]) is not int:
            _check(errors, False, "COMPILED_CASE_IDENTITY_INVALID")
            continue
        if key in actual:
            duplicates = True
        actual[key] = row
    _check(errors, not duplicates and set(actual) == set(expected), "COMPILED_CASE_COVERAGE_MISMATCH")
    run_ids = [row.get("run_id") for row in rows]
    _check(errors, all(_text(run_id) for run_id in run_ids) and len(set(run_ids)) == len(run_ids),
           "COMPILED_SAMPLE_RUNS_NOT_DISTINCT")
    results = []
    for key, spec in expected.items():
        if key not in actual:
            continue
        row = actual[key]
        context = f"{key[0]}/{key[1]}/{key[2]}"
        before = len(errors)
        _check(errors, _text(row.get("run_id")), "COMPILED_CASE_RUN_ID_MISSING", context)
        _check(errors, not row.get("error"), "COMPILED_CASE_ERROR", context)
        answer_hash = row.get("answer_sha256")
        _check(errors, _sha(answer_hash) or (spec.get("kind") == "lifecycle" and answer_hash is None),
               "COMPILED_ANSWER_IDENTITY_MISSING", context)
        checks = row.get("checks") or {}
        assertions = spec.get("assertions") or []
        _check(errors, isinstance(checks, dict) and set(checks) == set(assertions)
               and all(checks.get(label) is True for label in assertions), "COMPILED_ASSERTION_FAILED", context)
        timings, limits = row.get("timings") or {}, spec.get("limits") or {}
        if not isinstance(timings, dict) or not isinstance(limits, dict):
            _check(errors, False, "COMPILED_TIMING_INVALID", context)
            continue
        reused = row.get("index_reused")
        _check(errors, type(reused) is bool and (key[2] != 1 or reused is False), "COMPILED_INDEX_TIMING_MISSING", context)
        _check(errors, row.get("question_index_reused") is True, "COMPILED_QUESTION_REBUILT_INDEX", context)
        for name in ("cold_index_seconds", "first_provider_seconds", "question_seconds"):
            value = timings.get(name)
            if name == "cold_index_seconds" and reused is True and value is None:
                continue
            valid = _number(value) and _number(limits.get(name))
            _check(errors, valid, "COMPILED_TIMING_INVALID", context + ":" + name)
            if valid:
                _check(errors, value <= limits[name], "COMPILED_LATENCY_EXCEEDED", context + ":" + name)
        _check(errors, _number(row.get("elapsed_seconds")) and _number(timings.get("question_seconds"))
               and abs(row["elapsed_seconds"] - timings["question_seconds"]) <= .05,
               "COMPILED_QUESTION_TIMING_MISMATCH", context)
        results.append({"id": key[0], "turn_id": key[1], "sample_index": key[2],
                        "passed": len(errors) == before})
    return results


def _required_ranges(turn):
    """Resolve frozen gold ranges to hashes without consulting live source."""
    frozen = (turn.get("reviewed_context") or {}).get("source_ranges", [])
    result = []
    findings = (turn.get("gold") or {}).get("required_findings", [])
    for finding in findings:
        ranges = finding.get("supporting_ranges") or []
        if not ranges:
            return None
        for item in ranges:
            item = dict(item)
            if not item.get("source_sha256"):
                hashes = {ref.get("sha256") for ref in frozen if ref.get("path") == item.get("path")
                          and ref.get("start_line", 0) <= item.get("start_line", -1)
                          and ref.get("end_line", -1) >= item.get("end_line", 0)}
                if len(hashes) == 1:
                    item["source_sha256"] = hashes.pop()
            if (not _text(item.get("path")) or not _sha(item.get("source_sha256"))
                    or type(item.get("start_line")) is not int or type(item.get("end_line")) is not int
                    or item["start_line"] < 1 or item["end_line"] < item["start_line"]):
                return None
            result.append(item)
    return result or None


def _range_covered(required, supplied):
    spans = sorted((row["start_line"], row["end_line"]) for row in supplied
        if row.get("path") == required["path"] and row.get("source_sha256") == required["source_sha256"]
        and type(row.get("start_line")) is int and type(row.get("end_line")) is int)
    cursor = required["start_line"]
    for start, end in spans:
        if start > cursor:
            break
        cursor = max(cursor, end + 1)
        if cursor > required["end_line"]:
            return True
    return False


def _live_case_scope(items, errors):
    """Check the minimum frozen suite before native evaluation incurs any cost."""
    before = len(errors)
    valid = isinstance(items, list) and all(isinstance(item, dict) for item in items)
    def three_turns(item):
        turns = item.get("turns")
        return (isinstance(turns, list) and len(turns) >= 3
                and all(isinstance(turn, dict) and _text(turn.get("id")) and _text(turn.get("question"))
                        for turn in turns)
                and len({turn["id"] for turn in turns}) == len(turns))
    _check(errors, valid and any(three_turns(item) for item in items), "LIVE_THREE_TURN_CASE_REQUIRED")
    _check(errors, valid and any(item.get("split") == "holdout" for item in items), "LIVE_HOLDOUT_CASE_REQUIRED")
    _check(errors, valid and any(item.get("split") in ("regression", "public-development") for item in items),
           "LIVE_REGRESSION_CASE_REQUIRED")
    return len(errors) == before


def _live_acceptance(report, items, reviews, cases_sha256, policy, artifact, errors):
    _live_case_scope(items, errors)
    coverage = {tag for item in items for tag in (item.get("coverage") or []) if isinstance(tag, str)}
    _check(errors, set(OFFICE_REGRESSION_BASELINE) <= coverage, "LIVE_PRIOR_REGRESSIONS_MISSING")
    _check(errors, report.get("schema_version") == "live-business-eval/v3", "LIVE_SCHEMA_INVALID")
    _check(errors, _text(report.get("evaluation_run_id")), "LIVE_RUN_ID_MISSING")
    _check(errors, report.get("errors") == [] and report.get("status") == "completed_not_reviewed",
           "LIVE_RUN_INCOMPLETE")
    _check(errors, report.get("cases_sha256") == cases_sha256 == policy.get("live_cases_sha256"),
           "LIVE_CASES_NOT_FROZEN")
    runtime = report.get("runtime_provenance") or {}
    _check(errors, isinstance(runtime, dict) and runtime.get("execution_surface") == "native_windows_http"
           and runtime.get("runtime_platform") == "Windows" and runtime.get("transport_kind") == "remote_http"
           and runtime.get("source_dirty") is False, "LIVE_EXECUTION_SURFACE_INVALID")
    if not isinstance(runtime, dict):
        return []
    _check(errors, all(runtime.get(key) == value for key, value in artifact.items()), "LIVE_ARTIFACT_MISMATCH")
    _check(errors, (report.get("plan") or {}).get("mode") == "automatic", "LIVE_DEFAULT_PATH_REQUIRED")
    expected = {(item["id"], turn["id"]): {**item, **turn}
                for item in items for turn in _turns(item)}
    followup_turns = {(item["id"], turn["id"])
                     for item in items for turn in _turns(item)[1:]}
    _check(errors, len(expected) == sum(len(_turns(item)) for item in items), "LIVE_GOLD_CASE_IDS_INVALID")
    rows = report.get("cases")
    if not _check(errors, isinstance(rows, list) and all(isinstance(row, dict) for row in rows), "LIVE_CASES_INVALID"):
        return []
    actual = {(row.get("id"), row.get("turn_id")): row for row in rows}
    _check(errors, len(actual) == len(rows) and set(actual) == set(expected), "LIVE_CASE_COVERAGE_MISMATCH")
    review_rows = reviews.get("reviews")
    if not _check(errors, isinstance(review_rows, list) and all(isinstance(row, dict) for row in review_rows),
                  "LIVE_REVIEWS_INVALID"):
        return []
    reviewed = apply_reviews(copy.deepcopy(report), items, review_rows)
    accepted = {(row["id"], row["turn_id"]): row for row in reviewed["cases"]}
    limits = policy.get("live_limits") or {}
    valid_limits = (isinstance(limits, dict) and all(_number(limits.get(name)) and limits[name] > 0
                    for name in ("question_seconds", "p95_question_seconds", "min_evidence_recall"))
                    and limits["min_evidence_recall"] <= 1)
    _check(errors, valid_limits, "LIVE_LIMITS_MISSING")
    results, durations = [], []
    for key, turn in expected.items():
        if key not in actual:
            continue
        row, graded = actual[key], accepted[key]
        context, before = "/".join(key), len(errors)
        _check(errors, not row.get("branches") and not row.get("error") and _text(row.get("answer"))
               and row.get("truncated") is not True, "LIVE_ANSWER_UNAVAILABLE", context)
        stop_codes = [str(row.get(name) or "").upper() for name in ("reason_code", "stop_reason")]
        _check(errors, row.get("remote_result_unknown") is False
               and not any(code in INCOMPLETE_ANSWER_CODES or "CANCELLED" in code or "CANCELED" in code
                           for code in stop_codes), "LIVE_ANSWER_INTERRUPTED", context)
        _check(errors, type(row.get("model_requests")) is int and row["model_requests"] > 0,
               "LIVE_MODEL_REQUESTS_UNAVAILABLE", context)
        identity = row.get("review_identity") or {}
        measurement = row.get("measurement") or {}
        evidence = row.get("supplied_evidence") or {}
        if not all(isinstance(value, dict) for value in (identity, measurement, evidence)):
            _check(errors, False, "LIVE_IDENTITY_INVALID", context)
            continue
        diagnostics = row.get("acceptance_diagnostics") or {}
        valid_diagnostics = (_valid_answer_diagnostics(row)
            and identity.get("acceptance_diagnostics_sha256") == _fingerprint(diagnostics))
        _check(errors, valid_diagnostics, "LIVE_DIAGNOSTICS_MISSING_OR_STALE", context)
        policy_value = row.get("policy")
        history = row.get("history")
        _check(errors, isinstance(policy_value, dict)
            and type(policy_value.get("max_model_requests")) is int and policy_value["max_model_requests"] > 0
            and type(row.get("model_requests")) is int
            and 0 < row["model_requests"] <= policy_value["max_model_requests"],
            "LIVE_REQUEST_BUDGET_EXCEEDED", context)
        execution = policy.get("live_execution") or {}
        if execution:
            _check(errors, isinstance(execution, dict) and isinstance(policy_value, dict)
                and policy_value.get("max_model_requests") == execution.get("max_model_requests")
                and policy_value.get("max_question_seconds") == execution.get("max_question_seconds"),
                "LIVE_EXECUTION_POLICY_MISMATCH", context)
        if key in followup_turns:
            valid_history = (isinstance(history, list) and bool(history)
                and all(isinstance(entry, dict) and entry.get("role") in ("user", "assistant")
                        and _text(entry.get("content")) for entry in history)
                and {entry["role"] for entry in history} == {"user", "assistant"})
            _check(errors, valid_history, "LIVE_FOLLOWUP_HISTORY_MISSING", context)
        prompt = identity.get("prompt") or {}
        valid_prompt = (isinstance(prompt, dict) and _text(prompt.get("version"))
                        and prompt.get("version") != "unknown" and _sha(prompt.get("system_sha256")))
        request_hashes = prompt.get("request_sha256") if isinstance(prompt, dict) else None
        if isinstance(request_hashes, str):
            request_hashes = [request_hashes]
        valid_prompt = valid_prompt and isinstance(request_hashes, list) and bool(request_hashes) and all(_sha(value) for value in request_hashes)
        _check(errors, graded.get("review_binding_status") == "matched"
               and identity.get("run_id") == row.get("run_id") and _text(row.get("run_id"))
               and identity.get("runtime_sha256") == _fingerprint(runtime)
               and identity.get("measurement_sha256") == _fingerprint(measurement)
               and isinstance(policy_value, dict) and bool(policy_value)
               and identity.get("policy_sha256") == _fingerprint(policy_value)
               and isinstance(history, list) and identity.get("history_sha256") == _fingerprint(history)
               and _text(identity.get("model")) and valid_prompt, "LIVE_IDENTITY_INVALID", context)
        expected_measurement = {"scope": "full_question", "case_id": key[0], "turn_id": key[1],
            "branch": "automatic", "run_id": row.get("run_id"),
            "answer_sha256": hashlib.sha256(row.get("answer", "").encode()).hexdigest(),
            "elapsed_seconds": row.get("elapsed_seconds"), "cases_sha256": cases_sha256,
            "runtime_sha256": _fingerprint(runtime)}
        _check(errors, measurement == expected_measurement, "LIVE_TIMING_IDENTITY_MISMATCH", context)
        supplied = evidence.get("sources")
        valid_evidence = (isinstance(supplied, list) and bool(supplied)
                          and all(isinstance(entry, dict) and _sha(entry.get("source_sha256"))
                                  and _sha(entry.get("excerpt_sha256")) and _text(entry.get("evidence_id"))
                                  and type(entry.get("start_line")) is int and type(entry.get("end_line")) is int
                                  and 0 < entry["start_line"] <= entry["end_line"] for entry in supplied))
        _check(errors, valid_evidence and identity.get("evidence_fingerprint_status") == "supplied"
               and identity.get("evidence_sha256") == _fingerprint(_evidence_metadata(evidence))
               and identity.get("source_versions") == sorted({entry["source_sha256"] for entry in supplied})
               and _text(identity.get("source_snapshot_id")),
               "LIVE_SUPPLIED_EVIDENCE_INVALID", context)
        ranges = _required_ranges(turn)
        _check(errors, bool(ranges), "LIVE_GOLD_EVIDENCE_MISSING", context)
        recall = (sum(_range_covered(required, supplied) for required in ranges) / len(ranges)
                  if ranges and valid_evidence else None)
        if valid_limits:
            _check(errors, recall is not None and recall >= limits["min_evidence_recall"],
                   "LIVE_SUPPLIED_EVIDENCE_INCOMPLETE", context)
        review = graded.get("human_review") or {}
        matches = [entry for entry in review_rows if entry.get("identity") == identity]
        _check(errors, len(matches) == 1 and _text(review.get("reviewer")) and _text(review.get("reviewed_at")),
               "LIVE_REVIEW_ATTESTATION_MISSING", context)
        findings = (turn.get("gold") or {}).get("required_findings", [])
        forbidden = (turn.get("gold") or {}).get("forbidden_claims", [])
        finding_ids = [entry.get("id") for entry in findings]
        forbidden_ids = [entry.get("id", str(index)) if isinstance(entry, dict) else str(index)
                         for index, entry in enumerate(forbidden, 1)]
        reviewed_findings = review.get("required_findings", [])
        reviewed_forbidden = review.get("forbidden_claims", [])
        if row.get("agent_status") == "PARTIAL":
            partial = review.get("partial_review") or {}
            _check(errors, isinstance(partial, dict)
                and partial.get("classification") in PARTIAL_CLASSIFICATIONS
                and _text(partial.get("reason")) and partial.get("resolved") is True
                and isinstance(partial.get("affected_findings"), list)
                and bool(partial["affected_findings"])
                and set(partial["affected_findings"]) <= set(finding_ids),
                "LIVE_PARTIAL_NOT_EXPLAINED", context)
            _check(errors, partial.get("classification") != "scope_mismatch",
                   "LIVE_ANALYSIS_SCOPE_INVALID", context)
        _check(errors, bool(findings) and len(set(finding_ids)) == len(finding_ids)
               and sorted(entry.get("id", "") for entry in reviewed_findings) == sorted(finding_ids)
               and all(entry.get("result") == "met" for entry in reviewed_findings)
               and sorted(entry.get("id", "") for entry in reviewed_forbidden) == sorted(forbidden_ids)
               and all(entry.get("present") is False for entry in reviewed_forbidden)
               and graded.get("correctness_status") == "correct", "LIVE_QUALITY_OR_COMPLETENESS_FAILED", context)
        followup = bool(history)
        _check(errors, review.get("usefulness") == "usable"
               and review.get("followup_continuity") in ({"preserved"} if followup else {"preserved", "not_applicable"}),
               "LIVE_USEFULNESS_OR_CONTINUITY_FAILED", context)
        elapsed = row.get("elapsed_seconds")
        _check(errors, _number(elapsed), "LIVE_LATENCY_MISSING", context)
        if _number(elapsed):
            durations.append(elapsed)
            if valid_limits:
                _check(errors, elapsed <= limits["question_seconds"], "LIVE_LATENCY_EXCEEDED", context)
        results.append({"id": key[0], "turn_id": key[1], "answer_sha256": expected_measurement["answer_sha256"],
                        "elapsed_seconds": elapsed, "supplied_evidence_recall": recall,
                        "passed": before == len(errors)})
    p95 = sorted(durations)[max(0, math.ceil(len(durations) * .95) - 1)] if durations else None
    if valid_limits:
        _check(errors, p95 is not None and p95 <= limits["p95_question_seconds"], "LIVE_P95_EXCEEDED")
    return results


def evaluate_release(*, package, compiled_report, manifest, policy, live_report=None, live_cases=None, reviews=None,
                     office_report=None):
    """Return detailed reasons, never infer permission from a report's green summary."""
    result = {"schema_version": SCHEMA, "candidate_allowed": False, "publish_allowed": False,
              "candidate_errors": [], "publish_errors": [], "compiled_cases": [], "live_cases": []}
    candidate_errors, publish_errors = result["candidate_errors"], result["publish_errors"]
    try:
        config, compiled, specs = _read(policy), _read(compiled_report), _read(manifest)
        manifest_hash = file_sha256(manifest)
        result["input_sha256"] = {"policy": file_sha256(policy), "manifest": manifest_hash,
                                  "compiled_report": file_sha256(compiled_report)}
        _check(candidate_errors, config.get("schema_version") == POLICY_SCHEMA, "POLICY_SCHEMA_INVALID")
        _check(candidate_errors, config.get("manifest_sha256") == manifest_hash, "MANIFEST_NOT_FROZEN")
        artifact = _package_identity(package, compiled, config, candidate_errors)
        result["artifact"] = artifact
        result["compiled_cases"] = _compiled_acceptance(compiled, specs, manifest_hash, candidate_errors)
        result["candidate_allowed"] = not candidate_errors
    except (OSError, ValueError, TypeError, KeyError, AttributeError, zipfile.BadZipFile) as exc:
        candidate_errors.append({"code": "CANDIDATE_INPUT_INVALID", "error_type": type(exc).__name__})
        return result
    if not result["candidate_allowed"]:
        publish_errors.append({"code": "CANDIDATE_GATE_FAILED"})
    if not all((live_report, live_cases, reviews)):
        publish_errors.append({"code": "REAL_MODEL_AND_OFFICE_ACCEPTANCE_REQUIRED"})
        return result
    try:
        live, review_document = _read(live_report), _read(reviews)
        items = _cases(Path(live_cases))
        live_hash, cases_hash = file_sha256(live_report), file_sha256(live_cases)
        result["input_sha256"].update(live_report=live_hash, live_cases=cases_hash, reviews=file_sha256(reviews))
        result["live_cases"] = _live_acceptance(live, items, review_document, cases_hash, config, artifact, publish_errors)
        if office_report:
            native_office = _read(office_report)
            result["input_sha256"]["office_report"] = file_sha256(office_report)
            _check(publish_errors, all(native_office.get(key) == compiled.get(key) for key in
                ("archive_sha256", "binary_sha256", "application_sha256", "source_commit", "source_dirty")),
                "OFFICE_NATIVE_ARTIFACT_MISMATCH")
            _check(publish_errors, native_office.get("report_execution_id") != compiled.get("report_execution_id"),
                "OFFICE_NATIVE_REPORT_REUSED")
            prior_ids = {row.get("run_id") for row in compiled.get("cases", [])}
            office_rows = native_office.get("cases") or []
            _check(publish_errors, isinstance(office_rows, list)
                and all(isinstance(row, dict) and row.get("run_id") not in prior_ids for row in office_rows),
                "OFFICE_NATIVE_SAMPLE_REUSED")
            if isinstance(office_rows, list):
                for row in office_rows:
                    if isinstance(row, dict) and row.get("kind", "answer") == "answer":
                        _check(publish_errors, _valid_answer_diagnostics(row), "OFFICE_NATIVE_DIAGNOSTICS_MISSING_OR_STALE",
                               str(row.get("id", "")) + "/" + str(row.get("sample_index", "")))
            native_environment, live_environment = native_office.get("office_environment"), live.get("office_environment")
            _check(publish_errors, isinstance(native_environment, dict) and isinstance(live_environment, dict)
                and native_environment == live_environment
                and native_environment.get("schema_version") == "office-execution-environment/v1"
                and _sha(native_environment.get("machine_sha256"))
                and native_environment.get("system") == "Windows", "OFFICE_ENVIRONMENT_MISMATCH")
            result["office_cases"] = _compiled_acceptance(native_office, specs, manifest_hash, publish_errors)
        else:
            publish_errors.append({"code": "OFFICE_NATIVE_REPORT_REQUIRED"})
        office = review_document.get("office_acceptance") or {}
        required = config.get("office_required_checks")
        _check(publish_errors, isinstance(required, list) and bool(required) and all(_text(item) for item in required)
               and len(required) == len(set(required)) and set(OFFICE_BASELINE_CHECKS) <= set(required),
               "OFFICE_POLICY_MISSING")
        office_checks = office.get("checks") or {}
        _check(publish_errors, all(office.get(key) == artifact.get(key) for key in ("source_commit", "archive_sha256", "binary_sha256"))
               and office.get("evaluation_run_id") == live.get("evaluation_run_id")
               and office.get("live_report_sha256") == live_hash
               and office.get("office_report_sha256") == result["input_sha256"].get("office_report")
               and office_report is not None
               and _text(office.get("reviewer")) and _text(office.get("reviewed_at"))
               and isinstance(required, list) and isinstance(office_checks, dict)
               and all(office_checks.get(key) is True for key in required), "OFFICE_ACCEPTANCE_MISSING_OR_STALE")
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        publish_errors.append({"code": "LIVE_INPUT_INVALID", "error_type": type(exc).__name__})
    result["publish_allowed"] = result["candidate_allowed"] and not publish_errors
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("package", "compiled-report", "manifest", "policy", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    for name in ("live-report", "live-cases", "reviews", "office-report"):
        parser.add_argument("--" + name, type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--candidate", action="store_true", help="Only allow an offline candidate, never formal publication.")
    mode.add_argument("--require-publish", action="store_true", help="Fail unless the exact package has complete joint acceptance.")
    args = parser.parse_args(argv)
    result = evaluate_release(package=args.package, compiled_report=args.compiled_report, manifest=args.manifest,
        policy=args.policy, live_report=args.live_report, live_cases=args.live_cases, reviews=args.reviews,
        office_report=args.office_report)
    result["requested_mode"] = "candidate" if args.candidate else "publish"
    _safe_write(args.output, result)
    allowed = result["candidate_allowed"] if args.candidate else result["publish_allowed"]
    print(json.dumps({"candidate_allowed": result["candidate_allowed"], "publish_allowed": result["publish_allowed"],
                      "requested_mode": result["requested_mode"]}, ensure_ascii=False))
    return 0 if allowed else 1


if __name__ == "__main__":
    sys.exit(main())
