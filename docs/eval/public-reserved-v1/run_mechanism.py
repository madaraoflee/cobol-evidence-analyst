"""Run frozen public reserve retrieval/supply checks without a model service."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "poc"))

from agent_policy import AgentPolicy
from company_api import CompanyAPIConfig, TransportResponse
from compilation_context import resolve_compilation_context
from live_business_eval import evaluate, _turns


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--code-freeze-id", required=True)
    args = parser.parse_args()
    directory = Path(__file__).resolve().parent
    cases_path = directory / "cases.json"
    manifest_path = directory / "manifest.json"
    freeze = json.loads((directory / "freeze.json").read_text())
    for path, key in ((cases_path, "cases_sha256"), (manifest_path, "manifest_sha256")):
        if hashlib.sha256(path.read_bytes()).hexdigest() != freeze[key]:
            raise ValueError("RESERVED_CASE_IDENTITY_CHANGED")
    manifest = json.loads(manifest_path.read_text())
    root = args.source_root.resolve(strict=True)
    for entry in manifest["files"]:
        if hashlib.sha256((root / entry["path"]).read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("RESERVED_SOURCE_VERSION_CHANGED")
    items = json.loads(cases_path.read_text())["cases"]
    erratum = json.loads((directory / "annotation-erratum.json").read_text())
    product_root = directory.parents[2] / "poc"
    def implementation_fingerprint():
        digest = hashlib.sha256()
        for path in sorted(product_root.rglob("*.py")):
            if "tests" in path.relative_to(product_root).parts:
                continue
            digest.update(str(path.relative_to(product_root)).encode() + b"\0" + path.read_bytes() + b"\n")
        return digest.hexdigest()
    implementation_before = implementation_fingerprint()
    requests = []
    def transport(request):
        # No cases, gold, reviewed ranges or expected targets enter this branch.
        messages = json.loads(request.body)["messages"]
        payload = json.loads(messages[-1]["content"])
        pages = [page for bundle in payload["source_context"] for page in bundle.get("pages", [])]
        requests.append({"question": payload.get("question"),
            "history": [{"role": row["role"], "content": row["content"]} for row in messages[1:-1]],
            "pages": [{key: page[key] for key in ("evidence_id", "relative_path", "source_sha256", "start_line", "end_line")}
                      for page in pages]})
        if pages:
            page = pages[0]
            answer = (f"已定位并供应 {page['relative_path']} 的原文；这份材料可用于继续解释本轮问题。"
                      f"[{page['evidence_id']}] 本轮是供证机制试验，尚未进行真实模型业务答案评审。")
        else:
            answer = "本轮尚未供应可引用的源码，供证机制记录此缺口；未进行业务答案准确率评审。"
        return TransportResponse(200, json.dumps({"choices": [{"message": {
            "role": "assistant", "content": answer}, "finish_reason": "stop"}]}, ensure_ascii=False))

    config = CompanyAPIConfig("https://gateway.example.invalid/v1", "offline-reserve-mechanism", api_key="local-test-only")
    policy = AgentPolicy(max_model_requests=1, max_answer_revisions=0)
    with tempfile.TemporaryDirectory(prefix="public-reserved-eval-") as temporary:
        evaluation_dir = Path(temporary)
        plan = evaluate(root, evaluation_dir, cases_path, plan=True, config=config,
                        policy=policy, framework="", source_format="fixed", encoding="utf-8", preflight=False)
        report = evaluate(root, evaluation_dir, cases_path, allow_network=True, config=config,
            transport=transport, policy=policy, framework="", source_format="fixed", encoding="utf-8", preflight=False)
        with sqlite3.connect(evaluation_dir / "structural-index.sqlite") as connection:
            metadata = dict(connection.execute("SELECT key,value FROM metadata WHERE key IN ('source_options','parser_version','snapshot_id')"))
        metadata["source_options"] = json.loads(metadata["source_options"])
        for result in report["cases"]:
            trace_path = result["metrics"].pop("quality_trace_path", None)
            trace = json.loads(Path(trace_path).read_text()) if trace_path else {}
            result["pipeline_version"] = trace.get("pipeline_version")
            result["source_options"] = metadata["source_options"]
    turns = {(item["id"], turn["id"]): {**item, **turn} for item in items for turn in _turns(item)}
    scored = []
    for index, result in enumerate(report["cases"]):
        turn = turns[(result["id"], result["turn_id"])]
        actual = requests[index] if index < len(requests) else {"pages": [], "history": []}
        ranges = []
        for expected in turn["reviewed_context"]["source_ranges"]:
            relevant = [page for page in actual["pages"] if page["relative_path"] == expected["path"]
                        and page["source_sha256"] == expected["sha256"]]
            actual_lines = len((root / expected["path"]).read_text(encoding="utf-8").splitlines())
            expected_lines = set(range(expected["start_line"], expected["end_line"] + 1))
            supplied_lines = {line for page in relevant for line in range(page["start_line"], page["end_line"] + 1)}
            corrected_range = {key: expected[key] for key in ("start_line", "end_line")}
            if (result["id"] == erratum["case_id"] and expected["path"] == erratum["path"]
                    and expected["sha256"] == erratum["source_sha256"]
                    and corrected_range == erratum["original_range"]):
                corrected_range = erratum["valid_range_if_corrected"]
            corrected_lines = set(range(corrected_range["start_line"], corrected_range["end_line"] + 1))
            ranges.append({key: expected[key] for key in ("path", "sha256", "start_line", "end_line")} | {
                "supplied": expected_lines <= supplied_lines,
                "range_after_documented_annotation_correction": corrected_range,
                "supplied_after_documented_annotation_correction": corrected_lines <= supplied_lines,
                "source_line_count": actual_lines,
                "annotation_valid": 1 <= expected["start_line"] <= expected["end_line"] <= actual_lines,
                "expected_raw_lines": len(expected_lines), "supplied_raw_lines": len(expected_lines & supplied_lines),
                "coverage_metric": "union_of_actual_supplied_raw_physical_lines_not_semantic_correctness"})
        scored.append({"case_id": result["id"], "turn_id": result["turn_id"],
            "expected_paths_navigated": result["expected_paths_navigated"],
            "expected_paths_retrieved": result["expected_paths_retrieved"],
            "expected_paths_supplied": result["expected_paths_supplied"],
            "reviewed_ranges": ranges, "all_reviewed_ranges_supplied": all(entry["supplied"] for entry in ranges),
            "all_reviewed_ranges_supplied_after_documented_annotation_correction": all(
                entry["supplied_after_documented_annotation_correction"] for entry in ranges),
            "prior_user_turns": sum(entry["role"] == "user" for entry in actual["history"]),
            "actual_request": actual, "review_identity": result["review_identity"],
            "pipeline_version": result["pipeline_version"]})
    artifact = {"schema_version": "public-reserved-mechanism/v1", "code_freeze_declared": True,
        "code_freeze_id": args.code_freeze_id, "implementation_sha256": implementation_before,
        "implementation_unchanged_during_run": implementation_before == implementation_fingerprint(),
        "code_changed_after_reserve_results": False, "freeze": freeze,
        "mechanism_runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "index": metadata, "compilation_context_sha256": resolve_compilation_context(
            metadata["source_options"].get("compilation_context"), source_options=metadata["source_options"]).fingerprint,
        "transport": "in_process_fake_without_gold", "network_requests": 0,
        "paid_model_requests": 0, "real_model_accuracy": None, "business_answer_correctness": None,
        "source_commit": manifest["commit"], "source_files": manifest["files"],
        "annotation_erratum": erratum,
        "dry_run": plan, "mechanism_results": scored, "automatic_eval_report": report,
        "counts": {"turns": len(scored), "expected_paths_supplied": sum(bool(row["expected_paths_supplied"]) for row in scored),
                   "all_reviewed_ranges_supplied": sum(row["all_reviewed_ranges_supplied"] for row in scored)}}
    artifact["gold_annotation_diagnostics"] = [{"case_id": row["case_id"], "turn_id": row["turn_id"],
        "range": location, "reason": "frozen_reviewed_range_exceeds_original_file_eof"}
        for row in scored for location in row["reviewed_ranges"] if not location["annotation_valid"]]
    artifact["counts"].update(reviewed_ranges=sum(len(row["reviewed_ranges"]) for row in scored),
        reviewed_ranges_supplied=sum(location["supplied"] for row in scored for location in row["reviewed_ranges"]),
        invalid_frozen_ranges=len(artifact["gold_annotation_diagnostics"]))
    artifact["counts"]["all_reviewed_ranges_supplied_after_documented_annotation_correction"] = sum(
        row["all_reviewed_ranges_supplied_after_documented_annotation_correction"] for row in scored)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"counts": artifact["counts"], "real_model_accuracy": None, "report": str(args.output)}))


if __name__ == "__main__":
    main()
