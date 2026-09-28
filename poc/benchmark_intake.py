#!/usr/bin/env python3
"""Measure the same complete repository intake used by the business workbench.

Example: python poc/benchmark_intake.py --files 1000 --lines-per-file 12000
The generated repository and indexes are temporary. No model API is called.
Timings describe this machine and synthetic corpus, not a production SLA.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

from analyze_source import analyze_source
from repository_discovery import retrieve_repository_context


def run_benchmark(files: int, lines_per_file: int, rule_every: int = 5, progress=None) -> dict:
    if files < 1 or lines_per_file < 10 or rule_every < 1:
        raise ValueError("Use at least one file, ten lines per file and rule_every >= 1.")
    with tempfile.TemporaryDirectory(prefix="source-intake-benchmark-") as temporary:
        root = Path(temporary)
        source, output = root / "source", root / "output"
        source.mkdir()
        body = "".join(
            f"COMPUTE AMOUNT-{line % 193:03d} = INPUT-{line % 71:03d} * {line % 17 + 1}.\n"
            if line % rule_every == 0 else f"*> record description item {line % 271} coverage tier {line % 13}\n"
            for line in range(lines_per_file - 10))
        for index in range(files):
            text = (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. MEMBER-{index:05d}.\n"
                    "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 RESULT-VALUE PIC 9(9).\n"
                    "PROCEDURE DIVISION.\nMAIN-WORK.\n" + body +
                    f'CALL "MEMBER-{(index + 1) % files:05d}".\n*> TAILMARK{index:05d}\nGOBACK.\n')
            (source / f"member-{index:05d}.cbl").write_text(text, encoding="utf-8")
        source_bytes = sum(path.stat().st_size for path in source.iterdir())

        def intake():
            started = time.perf_counter()
            report = analyze_source(source, output, index_mode="catalog", analysis_mode="business",
                                    reading_strategy="retrieval", source_format="free", progress=progress)
            elapsed = time.perf_counter() - started
            if not report.get("source_manifest_verified") or report.get("runner_status") == "FAILED":
                raise RuntimeError(f"Intake did not complete: {report.get('reason_code')}")
            return elapsed, report

        cold_seconds, cold = intake()
        warm_seconds, warm = intake()
        started = time.perf_counter()
        context = retrieve_repository_context(output / "structural-index.sqlite", source, f"TAILMARK{files - 1:05d}")
        retrieval_seconds = time.perf_counter() - started
        expected_path = f"member-{files - 1:05d}.cbl"
        tail_found = any(page["relative_path"] == expected_path and f"TAILMARK{files - 1:05d}" in page["source_text"]
                         for page in context["pages"])
        if not tail_found or context["matched_file_count"] != 1:
            raise RuntimeError("The last program's final business marker was not retrieved correctly.")

        changed = source / "member-00000.cbl"
        with changed.open("a", encoding="utf-8") as handle:
            handle.write("*> UPDATEDBUSINESSMARK\n")
        incremental_seconds, incremental = intake()
        refreshed = retrieve_repository_context(output / "structural-index.sqlite", source, "UPDATEDBUSINESSMARK")
        if refreshed["matched_file_count"] != 1:
            raise RuntimeError("Incremental refresh did not retrieve the changed file.")
        with closing(sqlite3.connect(output / "structural-index.sqlite")) as connection:
            database_lines = connection.execute("SELECT SUM(line_count) FROM source_files").fetchone()[0]
            resolved_calls = connection.execute("SELECT COUNT(*) FROM relations WHERE relation_type='CALLS' AND status='confirmed'").fetchone()[0]
        return {
            "synthetic": True, "llm_called": False, "files": files,
            "lines_per_file": lines_per_file, "source_bytes": source_bytes,
            "workload": {"rule_every": rule_every, "other_lines": "descriptive_comments"},
            "cold_seconds": cold_seconds, "warm_refresh_seconds": warm_seconds,
            "one_changed_file_seconds": incremental_seconds, "retrieval_seconds": retrieval_seconds,
            "cold_build": cold["build_report"]["files"], "warm_build": warm["build_report"]["files"],
            "incremental_build": incremental["build_report"]["files"],
            "warm_search": {key: warm["repository_search"].get(key) for key in
                            ("indexed_files", "updated_files", "cached_files", "metadata_cache_reused", "content_hash_verified")},
            "full_text_complete": cold["repository_search"]["full_text_complete"],
            "deep_tail_retrieved": tail_found, "indexed_lines_after_edit": database_lines,
            "confirmed_call_edges": resolved_calls,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--lines-per-file", type=int, default=1000)
    parser.add_argument("--rule-every", type=int, default=5)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    last_update = [0.0]
    def progress(event):
        now = time.monotonic()
        if now - last_update[0] >= 10:
            print(f"{event.get('phase')}: {event.get('completed', 0)}/{event.get('total', '?')} "
                  f"{event.get('current_file') or ''}", file=sys.stderr, flush=True)
            last_update[0] = now
    report = run_benchmark(args.files, args.lines_per_file, args.rule_every, progress)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
