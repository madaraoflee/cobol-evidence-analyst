#!/usr/bin/env python3
"""Reproduce the offline synthetic material benchmark; no network or model.

Run from the repository root:
    python3 docs/reports/2026-11-07-online-materials/benchmark.py \
        --output docs/reports/2026-11-07-online-materials/benchmark-results.json

All generated source, indexes, and material objects live in a temporary
directory. The JSON result is the only retained output.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from pathlib import Path
import resource
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "poc"))

from business_index import build_business_index
from online_material_export import export_material
from online_material_format import digest
from online_material_query import MaterialQuery
from online_material_reader import DirectoryObjectStore, MaterialReader


COMMIT = "a" * 40
REPOSITORY = "synthetic-workspace/business-source"
COMMENT = "*> Synthetic context: account input balance processing status output and exception review.\n"


def source_program(number, programs, copybooks):
    body = (
        "IDENTIFICATION DIVISION.\n"
        f"PROGRAM-ID. ENTRY-{number:04}.\n"
        "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
        f"COPY RECORD-{number % copybooks:03}.\n"
        "01 RESULT-AMOUNT PIC 9(7).\n"
        "PROCEDURE DIVISION.\nMAIN-PARA.\n"
        "IF INPUT-AMOUNT > 0\n"
        f"CALL 'ENTRY-{(number + 1) % programs:04}'\n"
        "END-IF.\nMOVE INPUT-AMOUNT TO RESULT-AMOUNT.\nGOBACK.\n"
        f"*> Unique search marker ledgeranchor{number:04}.\n"
    )
    filler_count = 7200 if number == 0 else 96
    return (body + COMMENT * filler_count).encode("utf-8")


def generate(source, programs, copybooks):
    source.mkdir()
    for number in range(programs):
        (source / f"entry-{number:04}.cbl").write_bytes(source_program(number, programs, copybooks))
    for number in range(copybooks):
        content = "01 INPUT-AMOUNT PIC 9(7).\n" + "".join(
            f"01 SHARED-FIELD-{number:03}-{field:03} PIC X(24).\n" for field in range(48))
        (source / f"RECORD-{number:03}.cpy").write_text(content, encoding="utf-8")
    paths = sorted(source.iterdir())
    fingerprint = hashlib.sha256()
    total = 0
    largest = 0
    for path in paths:
        data = path.read_bytes()
        fingerprint.update(path.name.encode() + b"\0" + hashlib.sha256(data).digest() + b"\n")
        total += len(data)
        largest = max(largest, len(data))
    return {"programs": programs, "copybooks": copybooks, "files": len(paths),
            "bytes": total, "largest_file_bytes": largest,
            "source_fingerprint": fingerprint.hexdigest(),
            "shape": "Free-format programs, static CALL ring, shared COPY fields, repeated comments; no execution."}


class CountingStore:
    def __init__(self, root):
        self.store = DirectoryObjectStore(root)
        self.requests, self.bytes = 0, 0
        self.paths = []

    def read(self, path, max_bytes):
        data = self.store.read(path, max_bytes)
        self.requests += 1
        self.bytes += len(data)
        self.paths.append(path)
        return data


def seconds(start):
    return round(time.perf_counter() - start, 6)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--programs", type=int, default=2000)
    parser.add_argument("--copybooks", type=int, default=200)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 2 <= args.programs <= 10000 or not 1 <= args.copybooks <= args.programs:
        parser.error("Require 2..10000 programs and 1..programs copybooks.")
    total_start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="online-material-benchmark-") as temporary:
        base = Path(temporary)
        source, database, material = base / "source", base / "index.sqlite", base / "material"
        generated = generate(source, args.programs, args.copybooks)
        target_number = min(199, args.programs - 1)
        target_path = f"entry-{target_number:04}.cbl"
        target_bytes = (source / target_path).read_bytes()
        started = time.perf_counter()
        build = build_business_index(source, database, source_format="free", verify_content=True,
                                     framework_reference_path="", quiet=True)
        build_seconds = seconds(started)
        database_bytes = sum(path.stat().st_size for path in base.glob("index.sqlite*"))
        started = time.perf_counter()
        manifest = export_material(database, source, material, repository_id=REPOSITORY,
            commit=COMMIT, build_report=build, commit_verification="caller_declared")
        export_seconds = seconds(started)
        objects = list((material / "objects").iterdir())
        object_bytes = sum(path.stat().st_size for path in objects)
        manifest_bytes = (material / "material.json").stat().st_size
        kinds = {}
        for path in objects:
            kind = json.loads(path.read_bytes())["kind"]
            record = kinds.setdefault(kind, {"objects": 0, "bytes": 0})
            record["objects"] += 1
            record["bytes"] += path.stat().st_size

        # Remove the producer inputs before opening the query reader.
        shutil.rmtree(source)
        for path in base.glob("index.sqlite*"):
            path.unlink()
        assert not source.exists() and not database.exists()
        store = CountingStore(material)
        started = time.perf_counter()
        reader = MaterialReader(store, expected_repository=REPOSITORY, expected_commit=COMMIT)
        query = MaterialQuery(reader)
        result = query.search(f"ENTRY-{target_number:04}", limit=3)
        query_seconds = seconds(started)
        returned_paths = [item["relative_path"] for item in result["matches"]]
        assert target_path in returned_paths
        cold = {"query": f"ENTRY-{target_number:04}", "seconds": query_seconds,
                "returned_paths": returned_paths, "target_rank": returned_paths.index(target_path) + 1,
                "total_matches": result["total_matches"], "truncated": result["truncated"],
                "fetched_objects_excluding_manifest": reader.fetched_objects,
                "fetched_requests_including_manifest": store.requests,
                "fetched_bytes_including_manifest": store.bytes,
                "material_bytes_fraction": round(store.bytes / (object_bytes + manifest_bytes), 8)}
        before_requests, before_bytes = store.requests, store.bytes
        started = time.perf_counter()
        repeated = query.search(f"ENTRY-{target_number:04}", limit=3)
        warm = {"seconds": seconds(started), "same_result": repeated == result,
                "additional_requests": store.requests - before_requests,
                "additional_bytes": store.bytes - before_bytes}
        incoming = query.incoming(f"ENTRY-{target_number:04}")
        predecessor = f"entry-{(target_number - 1) % args.programs:04}.cbl"
        assert [match["relative_path"] for match in incoming["matches"]] == [predecessor]
        assert incoming["matches"][0]["relation"]["status"] == "confirmed"
        copy_relations = query.relations(target_path, "INCLUDES_COPY")
        assert copy_relations["relations"][0]["status"] == "confirmed"
        supplied = []
        def fake_source(path, *, max_bytes):
            assert path == target_path and len(target_bytes) <= max_bytes
            supplied.append(path)
            return target_bytes
        evidence = query.evidence(target_path, 1, 2, fake_source)
        assert evidence["text"].endswith(f"PROGRAM-ID. ENTRY-{target_number:04}.")
        assert evidence["citation"]["commit"] == COMMIT
        assert evidence["citation"]["hash"] == digest(target_bytes)
        result = {
            "schema_version": "online-material-benchmark/v1", "kind": "synthetic_offline_only",
            "network_calls": 0, "model_calls": 0, "business_programs_executed": False,
            "producer_source_and_database_removed_before_queries": True,
            "environment": {"python": platform.python_version(), "platform": sys.platform,
                            "machine": platform.machine(), "sqlite": sqlite3.sqlite_version},
            "reproduction": {"programs": args.programs, "copybooks": args.copybooks,
                             "script_sha256": digest(Path(__file__).read_bytes()),
                             "module_sha256": {path.name: digest(path.read_bytes())
                                               for path in sorted((ROOT / "poc").glob("online_*.py"))}},
            "source": generated,
            "build": {"seconds": build_seconds, "database_bytes_with_sidecars": database_bytes,
                      "index_kind": build["index_kind"], "indexed_files": build["files"]["decoded"],
                      "coverage": {key: value for key, value in build["scope"].items()
                                   if key != "dependency_scan"},
                      "dependency_scan_entries": len(build["scope"]["dependency_scan"])},
            "export": {"seconds": export_seconds, "objects": len(objects), "object_bytes": object_bytes,
                       "manifest_bytes": manifest_bytes, "material_total_bytes": object_bytes + manifest_bytes,
                       "object_kinds": kinds, "revision": manifest["revision"]},
            "cold_program_search": cold, "warm_program_search": warm,
            "relationship_checks": {"incoming_call_path": predecessor, "copy_status": "confirmed"},
            "fake_source_evidence": {"requests": len(supplied), "bytes": len(target_bytes),
                                     "citation": evidence["citation"]},
            "total_seconds": seconds(total_start),
            "peak_process_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024),
            "limitations": [
                "Repeated synthetic comments make this unsuitable for extrapolating production compression or GB-scale time.",
                "Directory object reads measure lazy fetch size, not HTTP latency, service quotas, or cloud performance.",
                "The exact program query is any-term lexical retrieval, not business-language understanding.",
                "The named program and its caller both contain the identifier; lexical ranking need not place the declaration first.",
                "The offline caller declares the commit; no remote or Git commit correspondence was checked.",
                "Full semantic analysis, model answer quality, Windows behavior, and deployment remain untested.",
            ],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"source_bytes": generated["bytes"], "source_files": generated["files"],
                      "build_seconds": build_seconds, "export_seconds": export_seconds,
                      "material_bytes": result["export"]["material_total_bytes"],
                      "cold_fetch_bytes": cold["fetched_bytes_including_manifest"]}, indent=2))


if __name__ == "__main__":
    main()
