#!/usr/bin/env python3
"""Measure the same complete repository intake used by the business workbench.

Example: python poc/benchmark_intake.py --files 12000 --lines-per-file 250 --workload business --longest-program-lines 25000
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


def _business_program(index, files, lines, copybooks):
    rows = ["IDENTIFICATION DIVISION.", f"PROGRAM-ID. MEMBER-{index:05d}.",
            "DATA DIVISION.", "WORKING-STORAGE SECTION."]
    rows += [f'COPY "shared-{(index + offset) % copybooks:03d}.cpy".' for offset in range(4)]
    rows += [f"01 INPUT-{field:03d} PIC 9(9)." for field in range(24)]
    rows += ["01 BILL-AMOUNT PIC 9(9).", "01 BILL-DATE PIC 9(8).", "PROCEDURE DIVISION.", "MAIN-WORK."]
    rows += [f"PERFORM RULE-{paragraph:03d}." for paragraph in range(8)]
    rows += [f'CALL "MEMBER-{(index + 1) % files:05d}".', "GOBACK."]
    blocks = (lines - len(rows) - 10) // 4
    for paragraph in range(8):
        rows.append(f"RULE-{paragraph:03d}.")
        for block in range(blocks // 8 + (paragraph < blocks % 8)):
            field = (paragraph * 5 + block) % 24
            rows += [f"IF INPUT-{field:03d} > {block % 97}",
                     f"COMPUTE BILL-AMOUNT = INPUT-{field:03d} * {block % 97 + 1}",
                     "END-IF.", f"MOVE INPUT-{field:03d} TO BILL-DATE."]
    while len(rows) < lines - 2:
        rows.append("ADD INPUT-000 TO BILL-AMOUNT.")
    rows += [f"*> TAILMARK{index:05d}", "GOBACK."]
    return "\n".join(rows) + "\n"


def run_benchmark(files: int, lines_per_file: int, rule_every: int = 5, progress=None,
                  *, workload="comments", longest_program_lines=None) -> dict:
    if files < 1 or lines_per_file < 10 or rule_every < 1:
        raise ValueError("Use at least one file, ten lines per file and rule_every >= 1.")
    if workload not in {"comments", "business"} or (workload == "business" and lines_per_file < 100):
        raise ValueError("Business workload requires at least 100 lines per program.")
    if longest_program_lines is not None and longest_program_lines < lines_per_file:
        raise ValueError("The longest program must be at least lines_per_file lines.")
    with tempfile.TemporaryDirectory(prefix="source-intake-benchmark-") as temporary:
        root = Path(temporary)
        source, output = root / "source", root / "output"
        source.mkdir()
        copybooks = min(files, 24) if workload == "business" else 0
        for index in range(copybooks):
            (source / f"shared-{index:03d}.cpy").write_text(
                f"01 SHARED-RECORD-{index:03d}.\n" + "".join(
                    f"  05 SHARED-FIELD-{index:03d}-{field:02d} PIC 9(9).\n" for field in range(8)),
                encoding="utf-8")
        body = "".join(
            f"COMPUTE AMOUNT-{line % 193:03d} = INPUT-{line % 71:03d} * {line % 17 + 1}.\n"
            if line % rule_every == 0 else f"*> record description item {line % 271} coverage tier {line % 13}\n"
            for line in range(lines_per_file - 10))
        for index in range(files):
            text = (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. MEMBER-{index:05d}.\n"
                    "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 RESULT-VALUE PIC 9(9).\n"
                    "PROCEDURE DIVISION.\nMAIN-WORK.\n" + body +
                    f'CALL "MEMBER-{(index + 1) % files:05d}".\n*> TAILMARK{index:05d}\nGOBACK.\n')
            if workload == "business":
                text = _business_program(index, files,
                    longest_program_lines if index == files - 1 and longest_program_lines else lines_per_file, copybooks)
            elif index == files - 1 and longest_program_lines:
                marker = f"*> TAILMARK{index:05d}\n"
                text = text.replace(marker, "MOVE INPUT-000 TO RESULT-VALUE.\n" *
                                    (longest_program_lines - lines_per_file) + marker)
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
            rule_count = connection.execute("SELECT COUNT(*) FROM business_rules").fetchone()[0]
            relation_count = connection.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        return {
            "synthetic": True, "llm_called": False, "files": files,
            "lines_per_file": lines_per_file, "source_bytes": source_bytes,
            "workload": {"kind": workload, "rule_every": rule_every if workload == "comments" else None,
                         "copybooks": copybooks, "longest_program_lines": longest_program_lines or lines_per_file,
                         "other_lines": "descriptive_comments" if workload == "comments" else "declarations_conditions_calculations_dependencies"},
            "cold_seconds": cold_seconds, "warm_refresh_seconds": warm_seconds,
            "one_changed_file_seconds": incremental_seconds, "retrieval_seconds": retrieval_seconds,
            "cold_build": cold["build_report"]["files"], "warm_build": warm["build_report"]["files"],
            "incremental_build": incremental["build_report"]["files"],
            "warm_search": {key: warm["repository_search"].get(key) for key in
                            ("indexed_files", "updated_files", "cached_files", "metadata_cache_reused", "content_hash_verified")},
            "full_text_complete": cold["repository_search"]["full_text_complete"],
            "deep_tail_retrieved": tail_found, "indexed_lines_after_edit": database_lines,
            "confirmed_call_edges": resolved_calls,
            "business_rule_count": rule_count, "relation_count": relation_count,
            "database_bytes": (output / "structural-index.sqlite").stat().st_size,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=1000)
    parser.add_argument("--lines-per-file", type=int, default=1000)
    parser.add_argument("--rule-every", type=int, default=5)
    parser.add_argument("--workload", choices=("comments", "business"), default="comments")
    parser.add_argument("--longest-program-lines", type=int)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    last_update = [0.0]
    def progress(event):
        now = time.monotonic()
        if now - last_update[0] >= 10:
            print(f"{event.get('phase')}: {event.get('completed', 0)}/{event.get('total', '?')} "
                  f"{event.get('current_file') or ''}", file=sys.stderr, flush=True)
            last_update[0] = now
    report = run_benchmark(args.files, args.lines_per_file, args.rule_every, progress,
                           workload=args.workload, longest_program_lines=args.longest_program_lines)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        args.report.write_text(text, encoding="utf-8")
    print(text, end="")


if __name__ == "__main__":
    main()
