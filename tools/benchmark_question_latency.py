"""Measure complete offline questions, including journal and result persistence.

The selected source tree supplies application code. The current harness uses
the frozen desktop large-repository question and synthetic response. A worker
blocks every socket operation and uses explicit disposable configuration.
Measured samples and cProfile samples are separate; neither is a real-model
quality or Windows executable measurement.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
import cProfile
import hashlib
import json
from pathlib import Path
import platform
import pstats
import shutil
import sqlite3
import statistics
import subprocess
import sys
import threading
import time
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.benchmark_release_performance import block_network, digest_json, source_identity
from tools.desktop_acceptance_cases import (DEFAULT_MANIFEST, _provider_answer,
    _quality_checks, corpus_fingerprint, generate_large, ledger_versions_match, verify_pages)
from tools.desktop_acceptance_runtime import isolated_environment, supplied_pages


def clone_index(original, destination):
    """Read a consistent backup without opening the supplied index for writes."""
    original, destination = Path(original).resolve(), Path(destination).resolve()
    if destination.exists() or destination == original or destination.is_relative_to(original):
        raise ValueError("The disposable workspace must be new and outside the input index")
    required = ("structural-index.sqlite", "diagnosis.json", "programs.json")
    if not all((original / name).is_file() for name in required):
        raise ValueError("Index input needs structural-index.sqlite, diagnosis.json and programs.json")
    destination.mkdir(parents=True)
    database = original / required[0]
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination / required[0])) as dst:
            src.backup(dst)
    for name in required[1:]:
        shutil.copyfile(original / name, destination / name)


def prepare_source(source, files, lines, longest):
    source = Path(source)
    manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    specification = {"programs": files, "copybooks": min(files, 24),
        "lines_per_program": lines, "longest_program_lines": longest, "expected": {}}
    if (files, lines, longest) == (12000, 224, 25000):
        specification = manifest["large_corpus"]
    if not source.exists():
        return generate_large(source, specification)
    actual = corpus_fingerprint(source)
    if specification["expected"] and actual != specification["expected"]:
        raise ValueError("Existing source does not match the frozen desktop corpus")
    return actual


def source_payload_fingerprint(requests):
    material = sorted((page["relative_path"], page["start_line"], page["end_line"],
        page["source_sha256"], page["source_text"], bool(page.get("span_truncated")))
        for request in requests for page in supplied_pages(request["payload"]))
    return {"sha256": digest_json(material), "pages": len(material),
        "files": len({row[0] for row in material}), "characters": sum(len(row[4]) for row in material)}


def summarize(samples):
    timed = [row for row in samples if not row["profile_only"]]
    warm = [row for row in timed if row["sample_index"] > 1]
    keys = ("total_seconds", "business_chat_seconds", "first_provider_seconds",
            "after_provider_seconds", "outside_business_chat_seconds")
    return {"timed_samples": len(timed), "warm_samples": len(warm),
        "median_seconds": {key: statistics.median(row["timings"][key] for row in timed) for key in keys},
        "warm_median_seconds": {key: statistics.median(row["timings"][key] for row in warm) for key in keys} if warm else {},
        "source_content_identical": len({row["source_payload"]["sha256"] for row in samples}) == 1,
        "answer_identical": len({row["answer_sha256"] for row in samples}) == 1,
        "claim_ledger_identical": len({row["claim_ledger_sha256"] for row in samples}) == 1}


def case_policy(case):
    """Match desktop_acceptance_runtime.write_profile's explicit test policy."""
    settings = {"max_model_requests": 1, "max_answer_revisions": 0, "max_question_seconds": 120}
    settings.update(case.get("policy", {"max_model_requests": 3, "max_answer_revisions": 0}))
    return settings


def model_config():
    """Use the desktop profile with explicit synthetic settings and no dotenv."""
    from company_api import CompanyAPIConfig
    from model_profiles import WORKBENCH_MAX_OUTPUT_TOKENS, WORKBENCH_TIMEOUT_SECONDS
    return CompanyAPIConfig.from_env(environ={
        "COMPANY_API_BASE_URL": "https://offline.example.invalid/v1",
        "COMPANY_CHAT_MODEL": "synthetic-acceptance",
        "COMPANY_API_KEY": "offline-placeholder"},
        default_max_output_tokens=WORKBENCH_MAX_OUTPUT_TOKENS,
        timeout_seconds=WORKBENCH_TIMEOUT_SECONDS, profile_name="workbench")


def workbench_job(app, payload, *, profiler=None, timeout=300, started=None):
    """Run the real worker and await its terminal browser-visible snapshot.

    Profiling belongs to the worker thread. The measured completion includes
    the terminal get_job copy and JSON encoding, but not a subsequent state
    refresh, just like the native desktop acceptance client's main timer.
    """
    original = app._run
    done = threading.Event()
    record = {}

    def observed(job_id, options):
        record["thread"] = threading.current_thread()
        record["started"] = time.perf_counter()
        try:
            return profiler.runcall(original, job_id, options) if profiler else original(job_id, options)
        finally:
            record["finished"] = time.perf_counter()
            done.set()

    app._run = observed
    began = time.perf_counter() if started is None else started
    snapshots = []
    try:
        job_id = app.start(payload)["job_id"]
        # CPython 3.14's cProfile monitoring can observe another thread's
        # polling calls and corrupt cumulative parent timings. Timed samples
        # retain normal polling; the separate profile sample leaves the main
        # thread idle until the real worker disables its profiler.
        if profiler and not done.wait(timeout):
            app.cancel(job_id)
            raise TimeoutError("Profiled workbench worker did not finish")
        while True:
            read_started = time.perf_counter()
            job = app.get_job(job_id)
            copied = time.perf_counter()
            encoded = json.dumps(job, ensure_ascii=False).encode()
            completed = time.perf_counter()
            snapshots.append({"status": job["status"], "copy_seconds": copied - read_started,
                              "json_seconds": completed - copied, "bytes": len(encoded)})
            if job["status"] != "RUNNING":
                break
            if completed - began > timeout:
                app.cancel(job_id)
                raise TimeoutError("Offline workbench benchmark did not finish")
            time.sleep(0.05)
        if not done.wait(max(0, timeout - (time.perf_counter() - began))):
            raise TimeoutError("Workbench reached terminal state but its worker did not exit")
        record["thread"].join(timeout=1)
        if record["thread"].is_alive():
            raise TimeoutError("Workbench worker did not join")
        if job["status"] != "COMPLETED":
            raise AssertionError("Workbench failed: " + str(job.get("error")))
        return job, {"total_seconds": completed - began,
            "worker_seconds": record["finished"] - record["started"],
            "worker_started_seconds": record["started"] - began,
            "worker_finished_seconds": record["finished"] - began,
            "snapshot_count": len(snapshots),
            "snapshot_copy_seconds": sum(row["copy_seconds"] for row in snapshots),
            "snapshot_json_seconds": sum(row["json_seconds"] for row in snapshots),
            "terminal_snapshot": snapshots[-1]}
    finally:
        app._run = original


def make_workbench(source, workspace, reference, case):
    from agent_policy import AgentPolicy
    from web_app import WorkbenchState
    config = model_config()
    policy = AgentPolicy(**case_policy(case))
    app = WorkbenchState(config_provider=lambda: config, policy_provider=lambda: policy,
                         state_path=workspace.parent / "isolated-profile/workbench-state.json")
    payload = {"source": str(source), "output": str(workspace), "source_format": "free",
        "index_mode": "catalog", "reading_strategy": "retrieval", "answer_detail": "detailed",
        "framework_reference_path": reference, "allow_network": False}
    workbench_job(app, payload)
    return app


def run_sample(source, workspace, sample_root, case, *, sample_index, profile_only, reference, workbench=None):
    from analyze_source import analyze_source
    import business_chat
    from agent_policy import AgentPolicy
    from company_api import TransportResponse

    sample_root.mkdir(parents=True)
    requests, phases, chat_spans, measured_calls = [], [], [], {}
    clock = time.perf_counter
    started = clock()

    def progress(event):
        phase = event.get("phase")
        if not phases or phases[-1]["phase"] != phase:
            phases.append({"phase": phase, "seconds": clock() - started})

    def transport(request):
        entered = clock()
        envelope = json.loads(request.body)
        payload = json.loads(envelope["messages"][-1]["content"])
        if payload.get("question") != case["question"]:
            raise AssertionError("Unexpected question on the offline transport")
        answer = _provider_answer(case, source, payload, not requests)
        response = TransportResponse(200, json.dumps({"choices": [{"message": {
            "role": "assistant", "content": answer}, "finish_reason": "stop"}]}))
        requests.append({"received_seconds": entered - started, "returned_seconds": clock() - started,
            "request_bytes": len(request.body), "payload": payload,
            "request_sha256": hashlib.sha256(request.body).hexdigest()})
        return response

    original_chat = business_chat.run_business_chat

    def measure_chat(*args, **kwargs):
        begin = clock()
        try:
            return original_chat(*args, **kwargs)
        finally:
            chat_spans.append({"start_seconds": begin - started, "end_seconds": clock() - started})

    def timed_call(function, name):
        def measured(*args, **kwargs):
            begin = clock()
            try:
                return function(*args, **kwargs)
            finally:
                item = measured_calls.setdefault(name, {"calls": 0, "seconds": 0.0})
                item["calls"] += 1
                item["seconds"] += clock() - begin
        return measured

    arguments = dict(question=case["question"], analysis_mode="business", reading_strategy="retrieval",
        index_mode="catalog", source_format="free", answer_detail="detailed",
        allow_network=True, transport=transport, progress=progress,
        config=model_config(),
        agent_policy=AgentPolicy(**case_policy(case)),
        conversation_id="offline-question-" + uuid.uuid4().hex, framework_reference_path=reference)
    profiler = cProfile.Profile() if profile_only else None
    started = clock()
    with ExitStack() as patches:
        patches.enter_context(patch.object(business_chat, "run_business_chat", measure_chat))
        import run_store
        for name in ("checkpoint", "load"):
            patches.enter_context(patch.object(run_store.RunStore, name,
                timed_call(getattr(run_store.RunStore, name), "run_store_" + name)))
        if workbench is not None:
            import web_app
            for owner, attribute, name in ((web_app, "_relationships", "relationships"),
                    (web_app, "_read_json", "web_read_json"),
                    (workbench, "_record_answer", "record_answer"),
                    (workbench, "_save_workspace", "save_workspace")):
                patches.enter_context(patch.object(owner, attribute,
                    timed_call(getattr(owner, attribute), name)))
            def analyzer(src, out, **options):
                callback = options.get("progress")

                def observe(event):
                    progress(event)
                    if callback:
                        callback(event)
                options["progress"] = observe
                return analyze_source(src, out, transport=transport, **options)

            workbench.analyzer = analyzer
            payload = {"source": str(source), "output": str(workspace), "source_format": "free",
                "index_mode": "catalog", "reading_strategy": "retrieval", "answer_detail": "detailed",
                "framework_reference_path": reference, "allow_network": True, "question": case["question"]}
            started = clock()
            job, desktop = workbench_job(workbench, payload, profiler=profiler, started=started)
            elapsed = desktop["total_seconds"]
            diagnosis = json.loads((workspace / "diagnosis.json").read_text(encoding="utf-8"))
        elif profiler:
            diagnosis = profiler.runcall(analyze_source, source, workspace, **arguments)
        else:
            diagnosis = analyze_source(source, workspace, **arguments)
    if workbench is None:
        elapsed = clock() - started
    if profiler:
        profiler.dump_stats(str(sample_root / "question.prof"))
        with (sample_root / "question-profile.txt").open("w", encoding="utf-8") as stream:
            pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(100)

    if not diagnosis.get("index_reused") or len(chat_spans) != 1 or not requests:
        raise AssertionError("Question did not reuse the index and complete one business-chat path")
    output = json.loads((workspace / "agent-result.json").read_text(encoding="utf-8"))
    agent = output.get("agent_result") or {}
    trace_path = Path(agent.get("metrics", {}).get("quality_trace_path", ""))
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    pages = [page for request in requests for page in supplied_pages(request["payload"])]
    checks = _quality_checks(case, agent, workspace, pages, trace)
    checks.update(source_versions_match=ledger_versions_match(agent.get("claim_ledger", {}), pages),
        answer_recorded=agent.get("model_answer_recorded") is True,
        answer_ledger_hash_matches=agent.get("claim_ledger", {}).get("answer_sha256") ==
            hashlib.sha256(agent.get("answer", "").encode()).hexdigest(),
        index_reused=diagnosis.get("index_reused") is True)
    if workbench is not None:
        conversation = job["result"]["conversation"]
        persisted = workbench.get_conversation(conversation["id"])["conversation"]
        checks["visible_answer_matches"] = conversation["messages"][-1]["content"] == agent.get("answer")
        checks["persisted_history_matches"] = persisted["messages"] == conversation["messages"]
    verify_pages(source, pages)
    if not all(checks.values()):
        raise AssertionError("Offline answer checks failed: " + str(checks))
    for number, request in enumerate(requests, 1):
        (sample_root / f"request-{number:02d}.json").write_text(
            json.dumps(request["payload"], ensure_ascii=False, indent=2), encoding="utf-8")
    for name, value in (("answer.json", agent), ("quality-trace.json", trace)):
        (sample_root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    chat = chat_spans[0]["end_seconds"] - chat_spans[0]["start_seconds"]
    row = {"sample_index": sample_index, "profile_only": profile_only, "question": case["question"],
        "timings": {"total_seconds": elapsed, "business_chat_seconds": chat,
            "first_provider_seconds": requests[0]["received_seconds"],
            "after_provider_seconds": elapsed - requests[-1]["returned_seconds"],
            "outside_business_chat_seconds": elapsed - chat,
            "synthetic_transport_seconds": sum(item["returned_seconds"] - item["received_seconds"] for item in requests)},
        "phases": phases, "chat_spans": chat_spans, "internal_metrics": agent.get("metrics", {}),
        "measured_calls": measured_calls,
        "source_payload": source_payload_fingerprint(requests), "checks": checks,
        "answer_sha256": hashlib.sha256(agent["answer"].encode()).hexdigest(),
        "claim_ledger_sha256": digest_json(agent["claim_ledger"]), "synthetic_requests": len(requests),
        "real_model_calls": 0, "answer_status": agent.get("status"), "stop_reason": agent.get("stop_reason")}
    if workbench is not None:
        row["workbench"] = desktop
        row["conversation_id"] = conversation["id"]
    (sample_root / "sample.json").write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding="utf-8")
    return row


def worker(spec):
    sys.addaudithook(block_network)
    tree, source, output = (Path(spec[key]).resolve() for key in ("source_tree", "source", "output"))
    identity = source_identity(tree)
    sys.path.insert(0, str(tree / "poc"))
    from analyze_source import analyze_source
    corpus = prepare_source(source, spec["files"], spec["lines"], spec["longest"])
    workspace = output / "workspace"
    reference = str(output / "no-framework-reference.md")
    before = time.perf_counter()
    if spec.get("index_from"):
        clone_index(spec["index_from"], workspace)
        preparation = "read_only_index_backup"
    else:
        report = analyze_source(source, workspace, analysis_mode="business", index_mode="catalog",
            source_format="free", reading_strategy="retrieval", verify_content=True,
            framework_reference_path=reference)
        if not report.get("source_manifest_verified"):
            raise AssertionError("Offline index preparation failed")
        preparation = "new_disposable_index"
    prepare_seconds = time.perf_counter() - before
    manifest = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    case = next(row for row in manifest["cases"] if row["id"] == "large_repository")
    workbench = make_workbench(source, workspace, reference, case) if spec.get("surface") == "workbench" else None
    samples = []
    for index in range(1, spec["repeats"] + 1):
        row = run_sample(source, workspace, output / f"sample-{index:02d}", case,
            sample_index=index, profile_only=False, reference=reference, workbench=workbench)
        samples.append(row)
        print(json.dumps({"sample": index, "timings": row["timings"]}), flush=True)
    if spec["profile"]:
        samples.append(run_sample(source, workspace, output / "profile", case,
            sample_index=spec["repeats"] + 1, profile_only=True, reference=reference, workbench=workbench))
    history_recovered = None
    if workbench is not None:
        from web_app import WorkbenchState
        original_history = workbench.state()["conversation"]
        restored = WorkbenchState(analyzer=workbench.analyzer, config_provider=workbench.config_provider,
            policy_provider=workbench.policy_provider, state_path=workbench.state_path)
        restored_history = restored.state()["conversation"]
        history_recovered = bool(restored.job is None and restored_history
            and restored_history["id"] == original_history["id"]
            and restored_history["messages"] == original_history["messages"])
        if not history_recovered:
            raise AssertionError("Saved history did not recover after rebuilding WorkbenchState")
    if source_identity(tree) != identity:
        raise RuntimeError("Application source changed during measurement")
    if corpus_fingerprint(source) != corpus:
        raise RuntimeError("Input source changed during measurement")
    report = {"schema_version": "offline-question-latency/v1", "source_tree": str(tree),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_identity": identity, "corpus": corpus, "index_preparation": preparation,
        "index_preparation_seconds": prepare_seconds, "samples": samples, "summary": summarize(samples),
        "environment": {"platform": platform.platform(), "python": platform.python_version(),
                        "executable": sys.executable},
        "model_configuration": model_config().safe_summary(),
        "real_model_calls": 0, "network_policy": "all_socket_connections_blocked",
        "measurement_surface": "workbench_start_to_terminal_snapshot" if workbench else "analyze_source_with_in_memory_synthetic_transport",
        "history_recovered_after_restart": history_recovered,
        "excludes": ["HTTP socket transfer", "real TLS and model generation"] if workbench else
                    ["desktop HTTP polling", "conversation-store append after analysis", "real TLS and model generation"],
        "profile_samples_excluded_from_timing_summary": True,
        "profile_workbench_polling": "main_thread_waits_until_profile_disabled; timed_samples_poll_normally"}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"summary": report["summary"]}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-tree", type=Path, default=ROOT)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--index-from", type=Path)
    parser.add_argument("--files", type=int, default=12000)
    parser.add_argument("--lines", type=int, default=224)
    parser.add_argument("--longest", type=int, default=25000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--surface", choices=("analyze", "workbench"), default="analyze")
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker(json.loads(args.worker.read_text(encoding="utf-8")))
        return 0
    if not args.source or not args.output:
        parser.error("--source and --output are required")
    if args.files < 1 or args.lines < 100 or args.longest < args.lines or args.repeats < 1:
        parser.error("Use positive files/repeats, at least100 lines, and longest >= lines")
    source, output = args.source.resolve(), args.output.resolve()
    if output.exists() or output == source or output.is_relative_to(source) or source.is_relative_to(output):
        parser.error("The new output directory must be separate from the source tree")
    output.mkdir(parents=True)
    spec = {key: str(value.resolve()) if isinstance(value, Path) else value
            for key, value in vars(args).items() if key != "worker"}
    spec_path = output / "measurement.json"
    spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    environment = isolated_environment({"WORKBENCH_DATA_DIR": str(output / "isolated-profile")})
    return subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", str(spec_path)],
                          env=environment, cwd=output).returncode


if __name__ == "__main__":
    raise SystemExit(main())
