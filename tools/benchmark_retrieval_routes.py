"""Offline real-retrieval timing and evidence-identity comparisons.

Cold means cleared Python caches/new SQLite connections, not a cold OS disk.
Synthetic sources and frozen module copies use a disposable benchmark directory.
Source generation and verification of returned text are outside timed retrieval.
"""
from __future__ import annotations
import argparse
from contextlib import ExitStack
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import statistics
import subprocess
import tempfile
import sys
import time
from unittest import mock

LONG = ("请从入口请求开始追踪处理过程，解释记录读取之后怎样校验请求状态、计算最终结果、决定是否继续写回，"
        "并把默认值、参数来源、缺失数据、异常返回、失败分支、重复请求、批次边界、时间窗口、上下限条件、"
        "撤销操作、重新执行、调用关系、共享数据以及最终输出串成一条可以根据源码核对的流程。"
        "对于每一步都需要给出准确的条件和对应的代码位置，区分已经明确实现的判断和目前仍然缺少证据的情况。")

def digest(value):
    data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(data).hexdigest()

def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

def prepare(directory, files, lines, build, retrieval):
    source, database = directory / "source", directory / "index.sqlite"
    source.mkdir(parents=True, exist_ok=True)
    if not database.exists() and any(source.iterdir()):
        raise RuntimeError("BENCHMARK_SOURCE_DIRECTORY_NOT_EMPTY")
    config = {"files": files, "lines": lines, "version": 1}
    if not database.exists():
        for file in range(files):
            body = ["IDENTIFICATION DIVISION.", f"PROGRAM-ID. MEMBER-{file:05d}.",
                    "DATA DIVISION.", "WORKING-STORAGE SECTION.", "01 COMMON-VALUE PIC 9(9).",
                    "01 REQUEST-STATE PIC 9.", "PROCEDURE DIVISION."]
            for line in range(lines):
                body.append(f"*> COMMON-VALUE record adjustment status REQUEST-STATE lantern cap value sample{line % 71} group{file % 13}")
            if file == files - 1:
                body += ["*> LANTERN-CAP-VALUE exact adjustment decision.", "MOVE 7 TO COMMON-VALUE."]
            body += ["GOBACK."]
            (source / f"source-{file:05d}.cbl").write_text("\n".join(body) + "\n", encoding="utf-8")
        started = time.perf_counter()
        build(source, database, source_format="free", quiet=True, framework_reference_path="")
        overview = retrieval.ensure_repository_search(database, source)
        (directory / "manifest.json").write_text(json.dumps({**config, "build_seconds": time.perf_counter()-started,
            "indexed_pages": overview["indexed_pages"], "source_bytes": sum(p.stat().st_size for p in source.iterdir())}))
    manifest = json.loads((directory / "manifest.json").read_text())
    if any(manifest[key] != value for key, value in config.items()):
        raise RuntimeError("BENCHMARK_CORPUS_PARAMETERS_CHANGED")
    return source, database, manifest

def measure(module, source, database, question, hints, cache_mode, *, max_pages=8, max_chars=32000, target_literal=None):
    if cache_mode == "python_cache_cold":
        module._GRAPH_CACHE.clear()
        module._identifier_tokens.cache_clear()
    metrics = {"sql_statements": 0, "fts_statements": 0, "vm_steps_approx": 0, "routes": []}
    connect = module._connect
    route = module._search_route_rows

    def trace(sql):
        metrics["sql_statements"] += 1
        metrics["fts_statements"] += int("repo_fts MATCH" in sql)

    def progress():
        metrics["vm_steps_approx"] += 1000
        return 0

    def traced_connect(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(trace)
        connection.set_progress_handler(progress, 1000)
        return connection

    def timed_route(connection, active, *args):
        started = time.perf_counter()
        rows = route(connection, active, *args)
        metrics["routes"].append({"exact": bool(active[1]), "query_sha256": digest(active),
                                   "rows": len(rows), "seconds": time.perf_counter() - started})
        return rows

    with mock.patch.object(module, "_connect", side_effect=traced_connect), \
         mock.patch.object(module, "_search_route_rows", side_effect=timed_route):
        started = time.perf_counter()
        result = module.retrieve_repository_context(database, source, question, search_terms=hints,
                                                     max_pages=max_pages, max_chars=max_chars)
        elapsed = time.perf_counter() - started
    pages = result["pages"]
    identity = []
    for page in pages:
        original = (source / page["relative_path"]).read_bytes()
        expected = "\n".join(original.decode().splitlines()[page["start_line"]-1:page["end_line"]])
        if page["source_text"] != expected or page["source_sha256"] != hashlib.sha256(original).hexdigest():
            raise RuntimeError("BENCHMARK_OUTPUT_NOT_PHYSICAL_SOURCE")
        identity.append({key: page.get(key) for key in ("relative_path", "source_sha256", "start_line", "end_line", "evidence_id", "source_text", "selection_reasons")})
    return {"seconds": elapsed, "cache_mode": cache_mode, **metrics,
            "evidence_identity_sha256": digest(identity), "full_result_sha256": digest(result),
            "page_count": len(pages), "returned_paths": [p["relative_path"] for p in pages],
            "matched_page_count": result["matched_page_count"], "matched_file_count": result["matched_file_count"],
            "orientation_only": result["orientation_only"], "physical_source_verified": True,
            "target_literal_visible": (any(target_literal in page["source_text"] for page in pages) if target_literal else None)}


def collect_case(modules, source, database, question, hints, repeat, *, label, corpus,
                 max_pages=8, max_chars=32000, target_literal=None):
    case = {**label, "corpus": corpus, "max_pages": max_pages, "max_chars": max_chars,
            "runs": {name: [] for name in modules}}
    for iteration in range(repeat + 1):
        order = list(modules.items())
        if iteration % 2:
            order.reverse()
        for name, module in order:
            case["runs"][name].append(measure(module, source, database, question, hints,
                "python_cache_cold" if iteration == 0 else "warm", max_pages=max_pages,
                max_chars=max_chars, target_literal=target_literal))
    identities = {run["evidence_identity_sha256"] for values in case["runs"].values() for run in values}
    full = {run["full_result_sha256"] for values in case["runs"].values() for run in values}
    case["evidence_identity_equal"] = len(identities) == 1
    case["full_result_equal"] = len(full) == 1
    case["warm_median_seconds"] = {name: statistics.median(run["seconds"] for run in values[1:])
                                   for name, values in case["runs"].items()}
    case["included_in_speed_comparison"] = label["kind"] == "output_equivalent_speed" and len(full) == 1
    if case["included_in_speed_comparison"]:
        case["speedup"] = case["warm_median_seconds"]["baseline"] / case["warm_median_seconds"]["candidate"]
    print(json.dumps({**label, "warm_median_seconds": case["warm_median_seconds"],
                      "evidence_equal": len(identities) == 1, "full_equal": len(full) == 1}), flush=True)
    return case


def quality_case(directory, modules, build, repeat):
    source, database = directory / "source", directory / "index.sqlite"
    source.mkdir(parents=True, exist_ok=True)
    question = "approval status adjustment threshold verification processing transaction eligibility calculation"
    text = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. TARGET-ENTRY.\nPROCEDURE DIVISION.\n"
            "*> LANTERN-CAP-VALUE.\nCOMPUTE FINAL-VALUE = BASE-VALUE + 37.\n" + "CONTINUE.\n" * 250 +
            "*> PREFIX-LANTERN-CAP-VALUE-SUFFIX " + question + "\nGOBACK.\n")
    if not database.exists():
        if any(source.iterdir()):
            raise RuntimeError("BENCHMARK_SOURCE_DIRECTORY_NOT_EMPTY")
        (source / "target.cbl").write_text(text, encoding="utf-8")
        build(source, database, source_format="free", quiet=True, framework_reference_path="")
        modules["baseline"].ensure_repository_search(database, source)
    return collect_case(modules, source, database, question, ["LANTERN-CAP-VALUE"], repeat,
        label={"kind": "crop_quality", "size": "single_page", "scenario": "exact_clue_survives_budget_crop"},
        corpus={"files": 1, "source_bytes": len(text.encode()), "source_sha256": digest(text.encode())},
        max_pages=1, max_chars=512, target_literal="COMPUTE FINAL-VALUE = BASE-VALUE + 37.")


def run(args, directory):
    sys.path.insert(0, str(args.repository / "poc"))
    from business_index import build_business_index
    directory.mkdir(parents=True, exist_ok=True)
    baseline = args.baseline_module
    if args.baseline_ref:
        frozen = subprocess.run(["git", "show", f"{args.baseline_ref}:poc/repository_discovery.py"],
                                cwd=args.repository, capture_output=True, check=True).stdout
        baseline = directory / "baseline_repository_discovery.py"
        baseline.write_bytes(frozen)
    candidate = args.candidate_module or args.repository / "poc/repository_discovery.py"
    modules = {"baseline": load_module(baseline, "retrieval_benchmark_baseline"),
               "candidate": load_module(candidate, "retrieval_benchmark_candidate")}
    initial_hashes = {name: digest(Path(module.__file__).read_bytes()) for name, module in modules.items()}
    report = {"schema": "retrieval-route-benchmark/v2", "synthetic": True, "model_calls": 0,
        "answer_quality_evaluated": False, "instrumented_timings": True, "os_disk_cache_flushed": False,
        "cold_definition": "Python graph/token caches cleared; new SQLite connection; OS page cache uncontrolled",
        "baseline_ref": args.baseline_ref, "module_sha256": initial_hashes, "warm_repeats": args.repeat,
        "speed_cases": [], "quality_cases": []}
    scenarios = [("exact", "请核对 LANTERN-CAP-VALUE 对输出的影响", None),
                 ("broad", "record adjustment status", None),
                 ("long_multi", LONG, ["COMMON-VALUE", "REQUEST-STATE", "record adjustment", "UNRECORDEDTERM"]),
                 ("no_hit", LONG, ["UNRECORDEDTERM"]),
                 ("exclusive", "Only analyze source-00000.cbl", ["LANTERN-CAP-VALUE"])]
    with ExitStack() as stack:
        for owner, name in ((socket, "create_connection"), (socket.socket, "connect"), (socket.socket, "connect_ex")):
            stack.enter_context(mock.patch.object(owner, name, side_effect=AssertionError("Offline benchmark forbids network")))
        for size in args.sizes:
            files, lines = {"small": (24, 400), "medium": (160, 1200)}[size]
            source, database, manifest = prepare(directory / size, files, lines, build_business_index, modules["baseline"])
            for scenario, question, hints in scenarios:
                report["speed_cases"].append(collect_case(modules, source, database, question, hints, args.repeat,
                    label={"kind": "output_equivalent_speed", "size": size, "scenario": scenario}, corpus=manifest))
        report["quality_cases"].append(quality_case(directory / "crop-quality", modules, build_business_index, args.repeat))
    report["modules_unchanged_during_benchmark"] = initial_hashes == {
        name: digest(Path(module.__file__).read_bytes()) for name, module in modules.items()}
    report["all_speed_outputs_identical"] = all(case["full_result_equal"] for case in report["speed_cases"])
    report["candidate_preserves_quality_target"] = all(run["target_literal_visible"]
        for case in report["quality_cases"] for run in case["runs"]["candidate"])
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if not (report["modules_unchanged_during_benchmark"] and report["all_speed_outputs_identical"]
            and report["candidate_preserves_quality_target"]):
        raise RuntimeError("BENCHMARK_IDENTITY_OR_QUALITY_CHECK_FAILED")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--directory", type=Path, help="Disposable synthetic benchmark directory; omit for temporary storage")
    parser.add_argument("--sizes", nargs="+", choices=["small", "medium"], default=["small", "medium"])
    parser.add_argument("--repeat", type=int, default=3)
    baseline = parser.add_mutually_exclusive_group(required=True)
    baseline.add_argument("--baseline-module", type=Path)
    baseline.add_argument("--baseline-ref", help="Local Git revision whose retrieval implementation is the baseline")
    parser.add_argument("--candidate-module", type=Path, help="Defaults to the current repository implementation")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.repeat <= 10:
        parser.error("repeat must be between 1 and 10")
    if args.directory:
        report = run(args, args.directory)
    else:
        with tempfile.TemporaryDirectory(prefix="retrieval-route-benchmark-") as temporary:
            report = run(args, Path(temporary))
    if not args.output:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
