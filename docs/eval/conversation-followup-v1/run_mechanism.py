"""Repeat the formerly reserved public cases as development regressions, offline."""

from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
from unittest import mock


REFERENCE_RUNNER = Path(__file__).resolve().parents[1] / "public-reserved-v1/run_mechanism.py"


def _implementation_hash():
    root = Path(__file__).resolve().parents[3] / "poc"
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        if "tests" not in path.relative_to(root).parts:
            digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\n")
    return digest.hexdigest()


def _baseline_report(path):
    path = Path(path).resolve(strict=True)
    return json.loads((path / "mechanism-results.json" if path.is_dir() else path).read_text(encoding="utf-8"))


def run_development(source_root, *, baseline=None):
    """Reuse the existing evaluator, frozen-source checks, fake and range scorer."""
    before_import = _implementation_hash()
    spec = importlib.util.spec_from_file_location("public_development_reference", REFERENCE_RUNNER)
    reference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)
    evaluate = reference.evaluate
    wire = []
    def instrumented_evaluate(*args, **kwargs):
        transport = kwargs.get("transport")
        if transport is not None:
            def observe(request):
                envelope = json.loads(request.body)
                payload = json.loads(envelope["messages"][-1]["content"])
                mapping = payload.get("business_map") or {}
                investigation = payload.get("question_investigation") or {}
                bundles = payload.get("source_context", [])
                wire.append({"question": payload.get("question"),
                    "request_bytes": len(request.body),
                    "request_body_sha256": hashlib.sha256(request.body).hexdigest(),
                    "source_characters": sum(len(page.get("source_text", "")) for bundle in bundles
                                             for page in bundle.get("pages", [])),
                    "prior_user_turns": sum(row["role"] == "user" for row in envelope["messages"][1:-1]),
                    "source_identity": mapping.get("source_identity"),
                    "session_subject": payload.get("session_subject") or mapping.get("session_subject"),
                    "analysis_plan": investigation.get("analysis_plan"),
                    "analysis_plan_omitted": investigation.get("analysis_plan_omitted", False),
                    "investigation_state": investigation.get("state"),
                    "open_gaps": investigation.get("open_gaps", []),
                    "investigation_budget": payload.get("investigation_budget"),
                    "working_sets": [bundle.get("working_set") for bundle in bundles],
                    "source_pages": [{key: page.get(key) for key in
                        ("evidence_id", "relative_path", "source_sha256", "start_line", "end_line", "span_truncated")}
                        for bundle in bundles for page in bundle.get("pages", [])]})
                # The original fake only observes this real request; gold stays
                # in the original evaluator's post-response scoring path.
                return transport(request)
            kwargs["transport"] = observe
        return evaluate(*args, **kwargs)
    reference.evaluate = instrumented_evaluate
    with tempfile.TemporaryDirectory(prefix="followup-development-") as temporary, ExitStack() as stack:
        staging = Path(temporary) / "mechanism-results.json"
        for owner, method in ((socket, "create_connection"), (socket.socket, "connect"),
                              (socket.socket, "connect_ex")):
            stack.enter_context(mock.patch.object(owner, method, side_effect=AssertionError("Network forbidden")))
        stack.enter_context(mock.patch.object(sys, "argv", [str(REFERENCE_RUNNER), "--source-root",
            str(Path(source_root).resolve(strict=True)), "--output", str(staging),
            "--code-freeze-id", "development-regression-not-a-new-holdout"]))
        with redirect_stdout(io.StringIO()):
            reference.main()
        artifact = json.loads(staging.read_text(encoding="utf-8"))
    original_freeze = artifact.pop("freeze")
    after_run = _implementation_hash()
    artifact["implementation_unchanged_during_run"] = bool(artifact["implementation_unchanged_during_run"]
        and artifact["implementation_sha256"] == before_import == after_run)
    artifact.update(schema_version="conversation-followup-development/v1",
        source_corpus_role="public-development-regression", formerly_reserved=True,
        old_gold_now_used_for_development=True, independent_holdout=False,
        original_frozen_artifacts_modified=False, code_freeze_declared=False,
        original_frozen_identity={key: original_freeze[key] for key in
            ("cases_sha256", "manifest_sha256", "frozen_at_utc", "source_commit")},
        real_model_accuracy=None, business_answer_correctness=None,
        network_requests=0, paid_model_requests=0, network_forbidden_by_local_guard=True,
        wire_requests=wire, implementation_sha256_before_import=before_import,
        implementation_sha256_after_run=after_run,
        development_runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    artifact.pop("code_freeze_id", None)
    artifact.pop("code_changed_after_reserve_results", None)
    if baseline is not None:
        previous = _baseline_report(baseline)
        previous_identity = previous.get("original_frozen_identity") or previous.get("freeze") or {}
        if (previous.get("source_commit") != artifact["source_commit"]
                or previous_identity.get("cases_sha256") != original_freeze["cases_sha256"]):
            raise ValueError("DEVELOPMENT_BASELINE_IDENTITY_MISMATCH")
        old_rows = {(row["id"], row["turn_id"]): row
                    for row in previous["automatic_eval_report"]["cases"]}
        comparisons = []
        for row in artifact["automatic_eval_report"]["cases"]:
            old = old_rows[(row["id"], row["turn_id"])]
            comparisons.append({"case_id": row["id"], "turn_id": row["turn_id"],
                "before_request_bytes": old["request_bytes"], "after_request_bytes": row["request_bytes"],
                "before_model_requests": old["model_requests"], "after_model_requests": row["model_requests"],
                "before_elapsed_seconds": old["elapsed_seconds"], "after_elapsed_seconds": row["elapsed_seconds"]})
        artifact["development_comparison"] = {"before_counts": previous["counts"], "after_counts": artifact["counts"],
            "baseline_implementation_sha256": previous["implementation_sha256"], "rows": comparisons,
            "interpretation": "one_local_fake_run_not_real_model_accuracy_or_a_stable_performance_benchmark"}
    return artifact


def main():
    parser = argparse.ArgumentParser(description="Offline conversation evidence development regressions; no paid model calls.")
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args()
    report = run_development(args.source_root, baseline=args.baseline)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    diagnostics = args.output.with_name("request-diagnostics.json")
    diagnostics.write_text(json.dumps(report["wire_requests"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(args.output.resolve()), "role": report["source_corpus_role"],
        "counts": report["counts"], "implementation_unchanged_during_run": report["implementation_unchanged_during_run"],
        "real_model_accuracy": None, "network_requests": 0}, ensure_ascii=False))
    return 0 if report["implementation_unchanged_during_run"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
