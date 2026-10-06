#!/usr/bin/env python3
"""Compare complete sparse facts on a synthetic corpus of long programs.

Example: python poc/benchmark_business_facts.py --directory /private/tmp/business-facts-long --files 20 --lines 25000 --profile
The optional baseline is loaded from Git without changing the checkout. Intake
timings exclude corpus generation, profiles, and the full equivalence check.
"""
from __future__ import annotations

import argparse
import cProfile
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import pstats
import sqlite3
import subprocess
import sys
import time

import business_index


def long_program(index, files, lines):
    rows = ["IDENTIFICATION DIVISION.", f"PROGRAM-ID. FLOW-{index:05d}.",
            "DATA DIVISION.", "WORKING-STORAGE SECTION."]
    rows += [f"01 VALUE-{field:04d} PIC 9(12)V99 VALUE {field % 97}." for field in range(lines // 10)]
    rows += ["01 TARGET-NAME PIC X(32).", 'COPY "shared-record.cpy".', "PROCEDURE DIVISION."]
    block = 0
    while len(rows) < lines - 25:
        left, right = block % (lines // 10), (block * 7 + 1) % (lines // 10)
        rows += [f"CHECK-{block:05d}.", f"IF VALUE-{left:04d} > {block % 971}",
                 f"COMPUTE VALUE-{right:04d} ROUNDED = VALUE-{left:04d} * {block % 103 + 1}",
                 "ELSE", f"MOVE VALUE-{right:04d} TO VALUE-{left:04d}", "END-IF.",
                 f"ADD {block % 61} TO VALUE-{right:04d}.",
                 f"SUBTRACT VALUE-{left:04d} FROM VALUE-{right:04d}.",
                 f"MULTIPLY VALUE-{left:04d} BY VALUE-{right:04d}.",
                 f"DIVIDE VALUE-{left:04d} INTO VALUE-{right:04d}.",
                 f"EVALUATE VALUE-{left:04d}", f"WHEN {block % 17}",
                 f"SET VALUE-{right:04d} TO VALUE-{left:04d}", "END-EVALUATE.",
                 f'CALL "FLOW-{(index + 1) % files:05d}" USING VALUE-{left:04d}.',
                 "CALL TARGET-NAME.", f"PERFORM CHECK-{max(0, block - 1):05d}.",
                 "EXEC SQL", f"SELECT ITEM_AMOUNT INTO :VALUE-{right:04d}",
                 f"FROM ITEM_LEDGER WHERE ITEM_KEY = :VALUE-{left:04d}", "END-EXEC.",
                 f'*> Description of calculation branch {block}; CALL "COMMENT-ONLY".',
                 'DISPLAY "Quoted CALL and COPY cannot create dependencies".']
        block += 1
    rows += ["MOVE VALUE-0000 TO VALUE-0001."] * (lines - len(rows) - 2)
    rows += [f"*> FINALMARK{index:05d}", "GOBACK."]
    return "\n".join(rows) + "\n"


def prepare_corpus(directory, files, lines):
    if files < 1 or lines < 100:
        raise ValueError("Use at least one program and 100 lines per program.")
    source = directory / "source"
    source.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        (source / f"flow-{index:05d}.cbl").write_text(long_program(index, files, lines), encoding="utf-8")
    (source / "shared-record.cpy").write_text("01 SHARED-RECORD.\n  05 SHARED-AMOUNT PIC 9(12)V99.\n", encoding="utf-8")
    return source


def load_baseline(directory, ref):
    contents = subprocess.run(["git", "show", f"{ref}:poc/business_index.py"], check=True,
                              capture_output=True).stdout
    path = directory / "baseline_business_index.py"
    path.write_bytes(contents)
    spec = importlib.util.spec_from_file_location("baseline_business_index", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The statement-kind vocabulary is shared with the strict parser. Load the
    # baseline implementation as well so its allocation cost is measured.
    structural = directory / "baseline_structural_index.py"
    structural.write_bytes(subprocess.run(["git", "show", f"{ref}:poc/structural_index.py"],
                                         check=True, capture_output=True).stdout)
    structural_spec = importlib.util.spec_from_file_location("baseline_structural_index", structural)
    structural_module = importlib.util.module_from_spec(structural_spec)
    sys.modules[structural_spec.name] = structural_module
    structural_spec.loader.exec_module(structural_module)
    module._statement_kind = structural_module._statement_kind
    return module


def fingerprint(database):
    tables = ("source_files", "code_units", "evidence_spans", "symbols", "relations",
              "business_rules", "business_rule_fields", "code_units_fts", "metadata")
    excluded = {"source_files": {"indexed_at_utc"}}
    result = {}
    with closing(sqlite3.connect(database)) as connection:
        for table in tables:
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")
                       if row[1] not in excluded.get(table, set())]
            selection = ",".join(columns)
            where = " WHERE key!='indexed_at_utc'" if table == "metadata" else ""
            order = ",".join(columns[:3]) if table == "business_rule_fields" else columns[0]
            if table == "code_units_fts":
                selection, order = "rowid," + selection, "rowid"
            digest, count = hashlib.sha256(), 0
            for row in connection.execute(f"SELECT {selection} FROM {table}{where} ORDER BY {order}"):
                digest.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode())
                digest.update(b"\n")
                count += 1
            result[table] = {"rows": count, "sha256": digest.hexdigest()}
        schema = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
        result["schema"] = {"rows": len(schema), "sha256": hashlib.sha256(
            json.dumps(schema, separators=(",", ":")).encode()).hexdigest()}
        result["fts_candidates"] = {
            query: connection.execute("SELECT unit_id FROM code_units_fts WHERE code_units_fts MATCH ? "
                "ORDER BY rank,unit_id LIMIT 200", (query,)).fetchall()
            for query in ('"VALUE-0001"', '"DEPENDENCY"', '"SHARED-AMOUNT"', '"CHECK-00010"')}
    return result


def intake(module, source, database):
    if database.exists():
        database.unlink()
    started = time.perf_counter()
    result = module.build_business_index(source, database, source_format="free", quiet=True,
                                         framework_reference_path=source.parent / "no-reference.md")
    return time.perf_counter() - started, result


def run(directory, files, lines, ref, profile=False):
    directory.mkdir(parents=True, exist_ok=True)
    source = prepare_corpus(directory, files, lines)
    baseline = load_baseline(directory, ref)
    if profile:
        for label, module in (("baseline", baseline), ("current", business_index)):
            profile_database = directory / f"profile-{label}.sqlite"
            if profile_database.exists():
                profile_database.unlink()
            profiler = cProfile.Profile()
            profiler.runcall(module.build_business_index, source, profile_database,
                            source_format="free", quiet=True,
                            include_paths=["flow-00000.cbl"],
                            framework_reference_path=directory / "no-reference.md")
            profiler.dump_stats(str(directory / f"{label}.prof"))
            with (directory / f"{label}-profile.txt").open("w") as handle:
                pstats.Stats(profiler, stream=handle).strip_dirs().sort_stats("cumulative").print_stats(40)
    old_seconds, old = intake(baseline, source, directory / "baseline.sqlite")
    new_seconds, new = intake(business_index, source, directory / "current.sqlite")
    old_facts, new_facts = fingerprint(directory / "baseline.sqlite"), fingerprint(directory / "current.sqlite")
    if old_facts != new_facts:
        different = [key for key in old_facts if old_facts[key] != new_facts[key]]
        raise RuntimeError(f"Sparse facts differ: {different}")
    if old["scope"] != new["scope"] or old["snapshot_id"] != new["snapshot_id"]:
        raise RuntimeError("Scope or snapshot differs")
    report = {"synthetic": True, "model_called": False, "baseline_ref": ref,
              "programs": files, "program_lines": lines,
              "source_bytes": sum(path.stat().st_size for path in source.iterdir()),
              "baseline_seconds": old_seconds, "current_seconds": new_seconds,
              "speedup": old_seconds / new_seconds, "facts_equal": True,
              "fingerprints": {key: value for key, value in new_facts.items() if key != "fts_candidates"}}
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--files", type=int, default=20)
    parser.add_argument("--lines", type=int, default=25000)
    parser.add_argument("--baseline-ref", default="b2acc40")
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args.directory, args.files, args.lines, args.baseline_ref, args.profile), indent=2))


if __name__ == "__main__":
    main()
