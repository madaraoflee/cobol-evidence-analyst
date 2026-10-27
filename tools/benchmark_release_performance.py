"""Compare repository versions serially on one machine without a model call.

Example: python tools/benchmark_release_performance.py --variant old=/tmp/old \
    --variant new=/tmp/new --output /tmp/release-comparison --repeats 3 --profile

Each worker imports only its selected source tree and uses a new disposable
source/output directory. Timings exclude corpus creation and fingerprinting.
Request preparation is measured up to a locally captured first request, not
model latency or answer quality. Profile runs are separate from timed samples.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import cProfile
import hashlib
import json
from pathlib import Path
import platform
import pstats
import sqlite3
import statistics
import subprocess
import sys
import time
import uuid


FACT_TABLES = (
    "source_files", "code_units", "evidence_spans", "symbols", "relations",
    "business_rules", "business_rule_fields", "business_rule_occurrences",
    "copy_expansions", "copy_scope_boundaries",
)
TIMING_KEYS = ("cold_import_seconds", "warm_import_seconds", "incremental_import_seconds")


def digest_json(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def variant_order(labels, round_index):
    """Rotate positions: AB/BA for two versions, ABC/BCA/CAB for three."""
    offset = round_index % len(labels)
    return list(labels[offset:]) + list(labels[:offset])


def block_network(event, arguments):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.sendto"}:
        raise RuntimeError("Offline benchmark forbids network access")


def corpus_program(index, count, lines, copybooks):
    rows = ["IDENTIFICATION DIVISION.", f"PROGRAM-ID. MEMBER-{index:05d}.",
            "DATA DIVISION.", "WORKING-STORAGE SECTION."]
    rows += [f'COPY "shared-{(index + offset) % copybooks:03d}.cpy".' for offset in range(4)]
    rows += [f"01 INPUT-{field:03d} PIC 9(9)." for field in range(24)]
    rows += ["01 BILL-AMOUNT PIC 9(9).", "01 BILL-DATE PIC 9(8).", "PROCEDURE DIVISION.", "MAIN-WORK."]
    rows += [f"PERFORM RULE-{paragraph:03d}." for paragraph in range(8)]
    rows += [f'CALL "MEMBER-{(index + 1) % count:05d}".', "GOBACK."]
    blocks = max(0, (lines - len(rows) - 10) // 4)
    for paragraph in range(8):
        rows.append(f"RULE-{paragraph:03d}.")
        for block in range(blocks // 8 + (paragraph < blocks % 8)):
            field = (paragraph * 5 + block) % 24
            rows += [f"IF INPUT-{field:03d} > {block % 97}",
                     f"COMPUTE BILL-AMOUNT = INPUT-{field:03d} * {block % 97 + 1}",
                     "END-IF.", f"MOVE INPUT-{field:03d} TO BILL-DATE."]
    while len(rows) < lines - 2:
        rows.append("ADD INPUT-000 TO BILL-AMOUNT.")
    return "\n".join([*rows, f"*> TAILMARK{index:05d}", "GOBACK."]) + "\n"


def prepare_corpus(source, files, lines, longest):
    source.mkdir(parents=True, exist_ok=False)
    copies = min(files, 24)
    for number in range(copies):
        (source / f"shared-{number:03d}.cpy").write_text(
            f"01 SHARED-RECORD-{number:03d}.\n" + "".join(
                f"  05 SHARED-FIELD-{number:03d}-{field:02d} PIC 9(9).\n" for field in range(8)),
            encoding="utf-8")
    for number in range(files):
        (source / f"member-{number:05d}.cbl").write_text(
            corpus_program(number, files, longest if number == files - 1 else lines, copies),
            encoding="utf-8")
    entries = [(path.name, hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
               for path in sorted(source.iterdir())]
    return {"sha256": digest_json(entries), "files": len(entries),
            "bytes": sum(item[2] for item in entries), "programs": files,
            "lines_per_program": lines, "longest_program_lines": longest}


def source_identity(root):
    entries = [(path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
               for path in sorted((root / "poc").rglob("*.py"))
               if "tests" not in path.relative_to(root).parts]
    return {"production_python_files": len(entries), "sha256": digest_json(entries)}


def facts_fingerprint(database):
    result = {}
    with closing(sqlite3.connect(database)) as db:
        existing = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # Include all persisted call/CL/framework facts in addition to the base
        # business index; exclude caches, timestamps and absolute workspace paths.
        tables = set(FACT_TABLES) | {name for name in existing
            if name.startswith(("call_", "cl_", "framework_")) and not name.endswith("cache")}
        for table in sorted(tables & existing):
            columns = [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')
                       if row[1] not in {"indexed_at_utc", "created_at_utc", "updated_at_utc"}]
            selection = ",".join('"' + name.replace('"', '""') + '"' for name in columns)
            digest, count = hashlib.sha256(), 0
            for row in db.execute(f'SELECT {selection} FROM "{table}" ORDER BY {selection}'):
                digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                count += 1
            result[table] = {"columns": columns, "rows": count, "sha256": digest.hexdigest()}
    return result


class FirstRequestCaptured(BaseException):
    pass


def capture_first_request(database, source, question, reference):
    from business_chat import run_business_chat
    from company_api import CompanyAPIConfig
    captured, phases = {}, []
    started = time.perf_counter()

    def progress(event):
        phase = event.get("phase")
        if not phases or phases[-1]["phase"] != phase:
            phases.append({"phase": phase, "seconds": time.perf_counter() - started})

    def transport(request):
        captured["seconds"] = time.perf_counter() - started
        payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
        pages = [page for bundle in payload.get("source_context", []) for page in bundle.get("pages", [])]
        source_content = sorted((page["relative_path"], page["start_line"], page["end_line"],
                                 page.get("source_sha256"), page["source_text"],
                                 bool(page.get("span_truncated"))) for page in pages)
        captured.update(source_page_count=len(pages), source_file_count=len({page[0] for page in source_content}),
                        source_characters=sum(len(page[4]) for page in source_content),
                        source_content_sha256=digest_json(source_content),
                        source_paths=sorted({page[0] for page in source_content}),
                        request_bytes=len(request.body),
                        retrieval_state=payload.get("retrieval_status", {}).get("state"))
        raise FirstRequestCaptured()

    try:
        run_business_chat(question, database, source,
            CompanyAPIConfig("https://offline.example.invalid/v1", "offline-evaluation", api_key="offline-placeholder"),
            allow_network=False, transport=transport, progress=progress,
            conversation_id="offline-performance-" + uuid.uuid4().hex,
            framework_reference_path=reference)
    except FirstRequestCaptured:
        pass
    if not captured:
        raise RuntimeError("Question failed to reach the locally intercepted first request")
    return {**captured, "phases": phases, "model_calls": 0, "answer_quality_measured": False}


def write_profile(profile, destination):
    profile.dump_stats(str(destination.with_suffix(".prof")))
    with destination.with_suffix(".txt").open("w", encoding="utf-8") as stream:
        pstats.Stats(profile, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(70)


def worker(spec):
    sys.addaudithook(block_network)
    root, run = Path(spec["source_tree"]), Path(spec["run_directory"])
    identity = source_identity(root)
    if spec.get("expected_source_identity", identity) != identity:
        raise RuntimeError("Source tree changed before the measurement")
    sys.path.insert(0, str(root / "poc"))
    from analyze_source import analyze_source
    from repository_discovery import retrieve_repository_context

    source, output = run / "source", run / "output"
    corpus = prepare_corpus(source, spec["files"], spec["lines"], spec["longest"])
    database = output / "structural-index.sqlite"
    reference = str(run / "no-framework-reference.md")
    result = {"variant": spec["label"], "round": spec["round"], "profile_only": spec["profile"],
              "source_identity": identity, "corpus": corpus, "timings": {},
              "real_model_calls": 0, "network_policy": "all_socket_connections_blocked"}

    def intake():
        report = analyze_source(source, output, index_mode="catalog", analysis_mode="business",
            reading_strategy="retrieval", source_format="free", verify_content=True,
            framework_reference_path=reference)
        if report.get("runner_status") == "FAILED" or not report.get("source_manifest_verified"):
            raise RuntimeError("Repository intake failed: " + str(report.get("reason_code")))
        return {"files": report.get("build_report", {}).get("files"),
                "full_text_complete": report.get("repository_search", {}).get("full_text_complete")}

    for key in TIMING_KEYS:
        if key == "incremental_import_seconds":
            with (source / "member-00000.cbl").open("a", encoding="utf-8") as stream:
                stream.write("*> UPDATEDBUSINESSMARK\n")
        profiler = cProfile.Profile() if spec["profile"] and key == "cold_import_seconds" else None
        started = time.perf_counter()
        intake_result = profiler.runcall(intake) if profiler else intake()
        result["timings"][key] = time.perf_counter() - started
        result[key.removesuffix("_seconds") + "_result"] = intake_result
        if profiler:
            write_profile(profiler, run / "cold-import")
        if key == "cold_import_seconds":
            result["cold_facts"] = facts_fingerprint(database)
    result["incremental_facts"] = facts_fingerprint(database)
    tail = f"TAILMARK{spec['files'] - 1:05d}"
    context = retrieve_repository_context(database, source, tail)
    result["tail_retrieved"] = any(tail in page["source_text"] and
        page["relative_path"] == f"member-{spec['files'] - 1:05d}.cbl" for page in context["pages"])
    updated = retrieve_repository_context(database, source, "UPDATEDBUSINESSMARK")
    result["incremental_marker_retrieved"] = any("UPDATEDBUSINESSMARK" in page["source_text"] for page in updated["pages"])
    if not result["tail_retrieved"] or not result["incremental_marker_retrieved"]:
        raise RuntimeError("Complete source tail or incremental update was not retrievable")
    result["questions"] = []
    for number, question in enumerate(spec["questions"]):
        profiler = cProfile.Profile() if spec["profile"] else None
        arguments = (database, source, question, reference)
        observation = profiler.runcall(capture_first_request, *arguments) if profiler else capture_first_request(*arguments)
        result["questions"].append({"question": question, **observation})
        if profiler:
            write_profile(profiler, run / f"question-{number}")
    if source_identity(root) != identity:
        raise RuntimeError("Source tree changed during the measurement")
    (run / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def summarize(results, labels):
    output = {}
    for label in labels:
        rows = [row for row in results if row["variant"] == label and not row["profile_only"]]
        output[label] = {"sample_count": len(rows),
            "median_seconds": {key: statistics.median(row["timings"][key] for row in rows) for key in TIMING_KEYS},
            "questions": [{"question": rows[0]["questions"][index]["question"],
                           "median_first_request_seconds": statistics.median(row["questions"][index]["seconds"] for row in rows)}
                          for index in range(len(rows[0]["questions"]))]}
    return output


def run_comparison(args):
    variants = {}
    for value in args.variant:
        label, separator, raw_path = value.partition("=")
        if not separator or not label or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in label):
            raise ValueError("Variants must be unique label=/absolute/source/tree entries")
        path = Path(raw_path).resolve(strict=True)
        if label in variants or not (path / "poc/business_chat.py").is_file():
            raise ValueError("Duplicate label or incomplete source tree")
        variants[label] = path
    if len(variants) < 2 or not 1 <= args.repeats <= 20 or args.files < 1 or args.lines < 100 or args.longest < args.lines:
        raise ValueError("Use at least two variants, 1-20 repeats, one file, 100 lines and longest >= lines")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    identities = {label: source_identity(path) for label, path in variants.items()}
    questions = args.question or ["BILL-AMOUNT 怎么计算？有哪些触发条件？",
        f"MEMBER-{args.files - 1:05d} 的 RULE-000 第一条 IF 如何计算 BILL-AMOUNT，输入在哪里声明，紧接下一条 IF 怎样覆盖？"]
    report = {"schema_version": "same-machine-performance/v1", "environment": {
        "platform": platform.platform(), "machine": platform.machine(), "python": sys.version,
        "executable": sys.executable, "sqlite": sqlite3.sqlite_version},
        "measurement": "serial_source_python_first_request_no_model", "real_model_calls": 0,
        "answer_quality_measured": False, "runs": [], "profile_runs": []}
    sequence = [(round_index, label, False) for round_index in range(args.repeats)
                for label in variant_order(list(variants), round_index)]
    if args.profile:
        sequence += [(0, label, True) for label in variants]
    for round_index, label, profiling in sequence:
        directory = output / (f"profile-{label}" if profiling else f"round-{round_index + 1}-{label}")
        directory.mkdir()
        spec = {"label": label, "source_tree": str(variants[label]), "run_directory": str(directory),
                "round": round_index + 1, "profile": profiling, "files": args.files,
                "lines": args.lines, "longest": args.longest, "questions": questions,
                "expected_source_identity": identities[label]}
        spec_path = directory / "worker-spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        print(json.dumps({"stage": "profile" if profiling else "timed", "variant": label, "round": round_index + 1}), flush=True)
        with (directory / "worker.log").open("w", encoding="utf-8") as stream:
            subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker-spec", str(spec_path)],
                           cwd=directory, stdout=stream, stderr=subprocess.STDOUT, check=True)
        result = json.loads((directory / "result.json").read_text(encoding="utf-8"))
        report["profile_runs" if profiling else "runs"].append(result)
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report["summary"] = summarize(report["runs"], list(variants))
    first = report["runs"][0]
    report["equivalence"] = {key: all(row[key] == first[key] for row in report["runs"])
                             for key in ("corpus", "cold_facts", "incremental_facts")}
    report["equivalence"]["request_source_content"] = all(
        [question["source_content_sha256"] for question in row["questions"]] ==
        [question["source_content_sha256"] for question in first["questions"]] for row in report["runs"])
    report["pairwise_equivalence"] = []
    labels = list(variants)
    for index, left in enumerate(labels):
        for right in labels[index + 1:]:
            selected = [row for row in report["runs"] if row["variant"] in {left, right}]
            baseline = selected[0]
            report["pairwise_equivalence"].append({"variants": [left, right],
                **{key: all(row[key] == baseline[key] for row in selected)
                   for key in ("corpus", "cold_facts", "incremental_facts")},
                "request_source_content": all(
                    [question["source_content_sha256"] for question in row["questions"]] ==
                    [question["source_content_sha256"] for question in baseline["questions"]]
                    for row in selected)})
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(output / "report.json"), "summary": report["summary"],
                      "equivalence": report["equivalence"]}, ensure_ascii=False, indent=2))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--lines", type=int, default=250)
    parser.add_argument("--longest", type=int, default=25000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--question", action="append")
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--worker-spec", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_spec:
        worker(json.loads(args.worker_spec.read_text(encoding="utf-8")))
    else:
        if not args.output:
            parser.error("--output is required")
        run_comparison(args)


if __name__ == "__main__":
    main()
