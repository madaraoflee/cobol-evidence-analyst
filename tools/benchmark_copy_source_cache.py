"""Compare immutable COPY-token reuse through real offline semantic scopes.

Authored source fixtures measure local repeated work, not production answer
quality. Each variant gets an independent cold sidecar, then a warm pass in the
same question session. No model, compiler, application build or network is used.
"""
from __future__ import annotations

import argparse
from contextlib import closing, nullcontext
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import statistics
import sys
import tempfile
import time
from unittest import mock


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("repository", type=Path, nargs="?", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--lines", type=int, default=2000)
    parser.add_argument("--hosts", type=int, default=4)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.lines <= 10000 or not 2 <= args.hosts <= 4 or not 1 <= args.repeats <= 10:
        parser.error("lines must be 1..10000, hosts 2..4 and repeats 1..10")
    sys.path.insert(0, str(args.repository / "poc"))
    from agent_policy import AgentPolicy
    from business_index import build_business_index
    import procedure_expansion
    from repository_discovery import ensure_repository_search
    from semantic_scope import prepare_semantic_scope
    import source_session
    from source_session import QuestionSourceSession

    report = {"schema": "copy-source-cache-benchmark/v1",
        "scope": "authored synthetic shared-COPY workloads", "production_semantic_scope_executed": True,
        "final_answer_evaluated": False, "model_calls": 0, "compiler_invocations": 0,
        "physical_copy_lines": args.lines, "repeats": args.repeats,
        "cache_max_bytes": source_session.EXPANSION_CACHE_MAX_BYTES,
        "cache_max_entries": source_session.EXPANSION_CACHE_MAX_ENTRIES,
        "module_sha256": {name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
            for name, module in (("procedure_expansion", procedure_expansion), ("source_session", source_session))},
        "cases": []}
    with mock.patch.object(socket, "create_connection", side_effect=AssertionError("Offline benchmark")), \
         mock.patch.object(socket.socket, "connect", side_effect=AssertionError("Offline benchmark")), \
         tempfile.TemporaryDirectory(prefix="copy-source-cache-benchmark-") as temporary:
        root = Path(temporary)
        for hosts in (1, args.hosts):
            case_root = root / f"hosts-{hosts}"
            source = case_root / "source"
            source.mkdir(parents=True)
            for number in range(hosts):
                (source / f"entry-{number}.cbl").write_text(
                    f"IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY-{number}.\nDATA DIVISION.\n"
                    "WORKING-STORAGE SECTION.\n01 INPUT-VALUE PIC 9(8).\n01 RESULT-VALUE PIC 9(8).\n"
                    "PROCEDURE DIVISION.\nMAIN.\nCOPY SHARED.\nGOBACK.\n", encoding="utf-8")
            (source / "shared.cpy").write_text("COMPUTE RESULT-VALUE = INPUT-VALUE * 2.\n" * args.lines,
                                               encoding="utf-8")
            base = case_root / "base.sqlite"
            build_business_index(source, base, source_format="free", verify_content=True)
            ensure_repository_search(base, source)
            case = {"host_count": hosts, "source_file_count": hosts + 1, "variants": {}}
            expected = None
            for label, enabled in (("cache_disabled", False), ("cache_enabled", True)):
                runs = []
                for repeat in range(args.repeats):
                    run_root = case_root / f"{label}-{repeat}"
                    run_root.mkdir()
                    database = run_root / "index.sqlite"
                    with closing(sqlite3.connect(base)) as original, closing(sqlite3.connect(database)) as target:
                        original.backup(target)
                    with QuestionSourceSession(database, source) as session:
                        toggle = nullcontext() if enabled else mock.patch.object(session, "cached_expansion_source", return_value=None)
                        # Baseline also skips cache bookkeeping, reproducing the
                        # prior physical-token path without changing expansion.
                        save = nullcontext() if enabled else mock.patch.object(session, "cache_expansion_source", return_value=None)
                        with toggle, save:
                            run = {}
                            for phase in ("cold", "warm"):
                                calls = 0
                                original_lex = procedure_expansion._lex_line

                                def counted_lex(*arguments, **options):
                                    nonlocal calls
                                    calls += 1
                                    return original_lex(*arguments, **options)

                                started = time.perf_counter()
                                with mock.patch.object(procedure_expansion, "_lex_line", new=counted_lex):
                                    with prepare_semantic_scope(database, session,
                                            anchors=[{"relative_path": "entry-0.cbl", "line": 9}],
                                            requested_calls=[f"entry-{number}.cbl" for number in range(1, hosts)],
                                            policy=AgentPolicy()) as scope:
                                        with closing(sqlite3.connect(scope.database_path)) as db:
                                            ir = db.execute("SELECT relative_path,ir_fingerprint FROM semantic_ir_documents ORDER BY relative_path").fetchall()
                                        identity = {"scope_key": scope.scope_key,
                                            "input_manifest": scope.input_manifest, "derived_manifest": scope.derived_manifest,
                                            "source_map_sha256": digest(scope.source_map), "ir_fingerprints": ir,
                                            "frontier": scope.frontier}
                                        cache_hit = scope.cache_hit
                                elapsed = time.perf_counter() - started
                                if expected is None:
                                    expected = identity
                                if identity != expected:
                                    raise AssertionError("Caching changed source provenance, derived bytes, IR or frontier")
                                run[phase] = {"seconds": elapsed, "lexical_line_calls": calls,
                                    "sidecar_cache_hit": cache_hit,
                                    "retained_token_entries": len(session._expansion_sources),
                                    "retained_token_bytes": session._expansion_source_bytes}
                            runs.append(run)
                case["variants"][label] = {"runs": runs,
                    "median_seconds": {phase: statistics.median(run[phase]["seconds"] for run in runs)
                                       for phase in ("cold", "warm")}}
            case.update(outputs_identical=True, derived_manifest=expected["derived_manifest"],
                        source_map_sha256=expected["source_map_sha256"], ir_fingerprints=expected["ir_fingerprints"])
            report["cases"].append(case)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
