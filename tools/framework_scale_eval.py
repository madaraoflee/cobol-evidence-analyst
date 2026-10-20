"""Offline pressure baseline: actual wire evidence, separate deterministic read probes.

No external provider is contacted. Oracle-guided reads are explicitly a tool
chain test, not evidence that a real model discovered the required material.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.framework_scale_fixture import audit_wire_memory, build_fixture, evidence_coverage


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def provenance(code_root):
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((code_root / "poc").glob("*.py"))}


def tool_provenance():
    return {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (Path(__file__), ROOT / "tools/framework_scale_fixture.py")}


def run_worker(args):
    sys.path.insert(0, str(args.code_root / "poc"))
    from business_index import build_business_index
    from repository_discovery import ensure_repository_search, discover_repository, read_repository_context
    source = args.directory / "source"
    oracle = json.loads((args.directory / "oracle.json").read_text(encoding="utf-8"))
    output = args.directory / args.label
    output.mkdir(exist_ok=True)
    database = output / "index.sqlite"
    manual = args.directory / "framework-reference.md"
    report = {"stage": args.stage, "synthetic": True, "external_model_calls": 0,
              "model_answer_accuracy_measured": False, "product_source_hashes": provenance(args.code_root),
              "evaluation_tool_hashes": tool_provenance(), "platform": platform.platform(),
              "python_version": platform.python_version()}
    started = time.perf_counter()
    if args.stage == "index":
        tick = time.perf_counter()
        indexed = build_business_index(source, database, source_format="free", quiet=True,
                                      framework_reference_path=manual)
        report["cold_index_seconds"] = time.perf_counter() - tick
        tick = time.perf_counter()
        searched = ensure_repository_search(database, source)
        report["cold_search_index_seconds"] = time.perf_counter() - tick
        tick = time.perf_counter()
        ensure_repository_search(database, source)
        report["reuse_search_index_seconds"] = time.perf_counter() - tick
        report["index_snapshot"] = indexed.get("snapshot_id")
        report["index_scope"] = indexed.get("scope")
        report["repository_search"] = searched
        report["database_bytes"] = sum(path.stat().st_size for path in output.glob("index.sqlite*"))
    elif args.stage == "continuity":
        from framework_semantics import build_framework_facts
        report["scope"] = "oracle_directed_contiguous_physical_page_probe_not_model_accuracy"
        report["cases"] = []
        for case in oracle["cases"]:
            span = next(span for span in case["required_ranges"] if 'CALL "LEDGERIO"' in span["text"])
            support = []
            for need in case["required_ranges"]:
                if need is not span:
                    support.extend(read_repository_context(database, source, relative_path=need["path"],
                        start_line=need["start_line"], end_line=need["end_line"], max_chars=20000)["pages"])
            # Split the CALL from USING using actual reader output, not invented pages.
            split = span["start_line"] + 3
            def read(first, last):
                return read_repository_context(database, source, relative_path=span["path"],
                    start_line=first, end_line=last, max_chars=20000)["pages"]
            variants = {"whole": read(span["start_line"], span["end_line"]),
                "contiguous_split": read(span["start_line"], split) + read(split + 1, span["end_line"]),
                "missing_using_line": read(span["start_line"], split) + read(split + 2, span["end_line"])}
            for variant, pages in variants.items():
                bound = build_framework_facts(database, support + pages, reference_path=manual)
                report["cases"].append({"id": case["id"], "variant": variant,
                    "coverage": evidence_coverage(source, pages, [span]),
                    "facts": [{key: fact.get(key) for key in ("target_name", "operation", "reason",
                        "dependency_covered", "runtime_verified", "source_evidence_ids")} for fact in bound["facts"]],
                    "runtime_verified": bound["runtime_verified"]})
                save(output / "probe-pages" / f"{case['id']}-{variant}.json", pages)
    elif args.stage == "probe":
        report["scope"] = "oracle_directed_tool_chain_not_autonomous_model_discovery"
        report["cases"] = []
        for case in oracle["cases"]:
            tick = time.perf_counter()
            retrieval = discover_repository(database, case["question"], entry_program=case["entry_program"])
            retrieved_at = time.perf_counter()
            pages, reads = [], []
            for span in case["required_ranges"]:
                result = read_repository_context(database, source, relative_path=span["path"],
                    start_line=span["start_line"], end_line=span["end_line"], max_chars=20000)
                pages.extend(result.get("pages", []))
                reads.append({"path": span["path"], "range_complete": result.get("range_complete"),
                              "next_start_line": result.get("next_start_line")})
            report["cases"].append({"id": case["id"], "retrieval_seconds": retrieved_at - tick,
                "read_seconds": time.perf_counter() - retrieved_at,
                "retrieval_state": retrieval.get("state"), "reads": reads,
                "coverage": evidence_coverage(source, pages, case["required_ranges"])})
            save(output / "probe-pages" / f"{case['id']}.json", pages)
        required = oracle["tool_chain_probes"]["longline"]
        report["long_physical_line"] = []
        for cap in (512, 20000):
            result = read_repository_context(database, source, relative_path=required[0]["path"],
                start_line=4, end_line=7, max_chars=cap)
            report["long_physical_line"].append({"max_chars": cap,
                "range_complete": result.get("range_complete"), "next_start_line": result.get("next_start_line"),
                "coverage": evidence_coverage(source, result.get("pages", []), required)})
    else:
        from business_chat import run_business_chat
        from company_api import CompanyAPIConfig, TransportResponse
        from tools.desktop_acceptance_runtime import supplied_pages
        case = next(case for case in oracle["cases"] if case["id"] == args.case)
        wires, all_pages, prior_wires = [], [], []

        def transport(request):
            received = time.perf_counter()
            envelope = json.loads(request.body)
            payload = json.loads(envelope["messages"][-1]["content"])
            pages = supplied_pages(payload)
            all_pages.extend(pages)
            memory = audit_wire_memory(payload, prior_wires)
            current = evidence_coverage(source, pages, case["required_ranges"])
            wire = {"sequence": len(wires) + 1, "ready_seconds": received - started,
                    "request_bytes": len(request.body.encode() if isinstance(request.body, str) else request.body),
                    "request_body_sha256": hashlib.sha256(request.body.encode() if isinstance(request.body, str) else request.body).hexdigest(),
                    "timeout_seconds": request.timeout_seconds, "coverage": current,
                    "effective_current_and_retransmitted_memory_coverage": evidence_coverage(
                        source, pages + memory["retransmitted_pages"], case["required_ranges"]),
                    "memory_audit": memory["blocks"],
                    "cumulative_coverage": evidence_coverage(source, all_pages, case["required_ranges"])}
            prior_wires.append({"sequence": len(wires) + 1, "request_body_sha256": wire["request_body_sha256"], "pages": pages})
            save(output / "wire" / f"{case['id']}-{args.repeat_index}-{len(wires) + 1}.json", payload)
            response_started = time.perf_counter()
            if not wires or args.stage == "closure-question":
                missing = [span for span, observed in zip(case["required_ranges"], current["ranges"])
                           if not observed["complete"]][:3]
                response = json.dumps({"read": [{"relative_path": span["path"],
                    "start_line": span["start_line"], "end_line": span["end_line"]} for span in missing]}) if missing else "当前供证已记录。封闭运行库源码缺失，运行状态、实际筛选与提交结果不能由此确认。"
                wire["response_kind"] = "oracle_directed_read" if missing else "synthetic_summary"
                wire["oracle_requested_ranges"] = [{key: span[key] for key in ("path", "start_line", "end_line")} for span in missing]
            else:
                response = "当前供证已记录。封闭运行库源码缺失，运行状态、实际筛选与提交结果不能由此确认。"
                if pages:
                    response += f"[{pages[0]['evidence_id']}]"
                wire["response_kind"] = "synthetic_summary"
            wire["simulated_model_seconds"] = time.perf_counter() - response_started
            wire["evaluation_instrumentation_seconds"] = response_started - received
            wires.append(wire)
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": response}, "finish_reason": "stop"}]}, ensure_ascii=False))

        result = run_business_chat(case["question"], database, source,
            CompanyAPIConfig("https://offline.example.invalid/v1", "offline-evaluation", api_key="offline-placeholder"),
            transport=transport, allow_network=False, framework_reference_path=manual,
            entry_program=case["entry_program"], conversation_id="framework-scale-" + uuid.uuid4().hex)
        agent = result.get("agent_result", {})
        report.update(case_id=case["id"], repeat_index=args.repeat_index, wires=wires,
            scope=("oracle_directed_reads_until_current_wire_complete_or_default_budget_exhausted"
                   if args.stage == "closure-question" else "default_first_request_then_three_oracle_reads_then_summary"),
            first_request_seconds=wires[0]["ready_seconds"] if wires else None,
            simulated_model_seconds=sum(row["simulated_model_seconds"] for row in wires),
            stop_reason=agent.get("stop_reason"), status=agent.get("status"),
            budget=agent.get("metrics", {}).get("policy"),
            answer=agent.get("answer"), answer_quality_measured=False,
            final_wire_coverage=wires[-1]["coverage"] if wires else None)
        if args.stage == "closure-question":
            signatures = [json.dumps(wire.get("oracle_requested_ranges"), sort_keys=True)
                          for wire in wires if wire.get("oracle_requested_ranges")]
            report["repeated_read_request_sets"] = len(signatures) - len(set(signatures))
    report["elapsed_seconds"] = time.perf_counter() - started
    if os.name == "posix":
        import resource
        report["peak_worker_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    save(args.result, report)
    print(json.dumps({"stage": args.stage, "case": args.case, "elapsed_seconds": report["elapsed_seconds"]}))


def orchestrate(args):
    if not (args.directory / "oracle.json").exists():
        build_fixture(args.directory, args.scale)
    oracle = json.loads((args.directory / "oracle.json").read_text())
    output = args.directory / args.label
    output.mkdir(exist_ok=False)
    report = {"schema": "framework-scale-evaluation/v1", "synthetic": True,
        "model_answer_accuracy_measured": False, "scale": oracle["scale"],
        "generation": oracle["generation"], "product_source_hashes": provenance(args.code_root), "stages": []}
    report["oracle_sha256"] = hashlib.sha256((args.directory / "oracle.json").read_bytes()).hexdigest()
    report["evaluation_tool_hashes"] = tool_provenance()
    stages = [("index", "", 0), ("probe", "", 0), ("continuity", "", 0)]
    stages += [("question", case["id"], repeat) for case in oracle["cases"] for repeat in range(args.repeat)]
    if args.closure_probe:
        stages += [("closure-question", case["id"], 0) for case in oracle["cases"]]
    for stage, case, repeat in stages:
        target = output / f"{stage}-{case or 'all'}-{repeat}.json"
        command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--directory", str(args.directory),
            "--code-root", str(args.code_root), "--label", args.label, "--stage", stage,
            "--case", case, "--repeat-index", str(repeat), "--result", str(target)]
        tick = time.perf_counter()
        with (output / f"{stage}-{case or 'all'}-{repeat}.log").open("wb") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=os.name == "posix")
            try:
                status = process.wait(timeout=args.max_seconds)
                outcome = "completed" if status == 0 else "failed"
            except subprocess.TimeoutExpired:
                if os.name == "posix":
                    import signal
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait()
                outcome, status = "timeout", None
        report["stages"].append({"stage": stage, "case": case, "repeat": repeat, "status": outcome,
            "exit_code": status, "elapsed_seconds": time.perf_counter() - tick, "report": target.name})
        save(output / "REPORT.json", report)
        print(json.dumps(report["stages"][-1]), flush=True)
        if outcome != "completed" and stage == "index":
            break
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--scale", choices=("focused", "stress"), default="focused")
    parser.add_argument("--label", default="baseline")
    parser.add_argument("--code-root", type=Path, default=ROOT)
    parser.add_argument("--repeat", type=int, default=2)
    parser.add_argument("--max-seconds", type=int, default=900)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--closure-probe", action="store_true", help="Also run oracle-directed reads to completion within default model/time budgets")
    parser.add_argument("--stage", choices=("index", "probe", "continuity", "question", "closure-question"), default="index")
    parser.add_argument("--case", default="")
    parser.add_argument("--repeat-index", type=int, default=0)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 3 or not 30 <= args.max_seconds <= 1800:
        parser.error("repeat must be 1..3 and per-stage seconds 30..1800")
    if args.worker:
        run_worker(args)
    else:
        orchestrate(args)


if __name__ == "__main__":
    main()
