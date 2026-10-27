"""Integrity checks for the offline, same-machine measurement harness."""
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.benchmark_release_performance import (
    facts_fingerprint, prepare_corpus, source_identity, summarize, variant_order,
)


class ReleasePerformanceBenchmarkTests(unittest.TestCase):
    def test_each_variant_rotates_through_every_execution_position(self):
        labels = ["old", "current", "optimized"]
        order = [variant_order(labels, index) for index in range(3)]
        for position in range(3):
            self.assertEqual({row[position] for row in order}, set(labels))
        self.assertEqual(variant_order(["old", "new"], 1), ["new", "old"])
        self.assertEqual(labels, ["old", "current", "optimized"])

    def test_independent_corpora_have_identical_bytes_and_complete_long_tail(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first = prepare_corpus(base / "first", 2, 100, 151)
            second = prepare_corpus(base / "second", 2, 100, 151)
            self.assertEqual(first, second)
            lines = (base / "first/member-00001.cbl").read_text().splitlines()
            self.assertEqual(len(lines), 151)
            self.assertEqual(lines[-2:], ["*> TAILMARK00001", "GOBACK."])
            self.assertIn('COPY "shared-000.cpy".', lines)

    def test_fingerprints_ignore_only_timestamps_and_detect_late_source_facts(self):
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "index.sqlite"
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("CREATE TABLE source_files(relative_path TEXT,sha256 TEXT,indexed_at_utc TEXT)")
                db.execute("INSERT INTO source_files VALUES('main.cbl','source-version','first')")
                db.execute("CREATE TABLE business_rule_occurrences(occurrence_id TEXT,statement_order INTEGER)")
                db.executemany("INSERT INTO business_rule_occurrences VALUES(?,?)", [("start", 1), ("tail", 9000)])
                db.execute("CREATE TABLE call_bindings(binding_id TEXT,target TEXT)")
                db.execute("INSERT INTO call_bindings VALUES('call','HELPER')")
            original = facts_fingerprint(database)
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("UPDATE source_files SET indexed_at_utc='second'")
            self.assertEqual(original, facts_fingerprint(database))
            with closing(sqlite3.connect(database)) as db, db:
                db.execute("DELETE FROM business_rule_occurrences WHERE occurrence_id='tail'")
            changed = facts_fingerprint(database)
            self.assertNotEqual(original["business_rule_occurrences"], changed["business_rule_occurrences"])
            self.assertEqual(original["call_bindings"]["rows"], 1)

    def test_worker_network_guard_stops_loopback_before_connecting(self):
        script = (
            "import sys,socket; sys.path.insert(0,sys.argv[1]); "
            "from tools.benchmark_release_performance import block_network; "
            "sys.addaudithook(block_network); socket.create_connection(('127.0.0.1',9))")
        result = subprocess.run([sys.executable, "-c", script, str(ROOT)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Offline benchmark forbids network access", result.stderr)

    def test_source_identity_detects_changes_without_git_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "poc/tests").mkdir(parents=True)
            source = root / "poc/main.py"
            source.write_text("VALUE = 1\n")
            first = source_identity(root)
            (root / "poc/tests/test_main.py").write_text("# Test-only note.\n")
            self.assertEqual(first, source_identity(root))
            source.write_text("VALUE = 2\n")
            self.assertNotEqual(first, source_identity(root))

    def test_summary_excludes_profiler_runs_from_medians(self):
        def sample(seconds, profile):
            return {"variant": "candidate", "profile_only": profile,
                "timings": {key: seconds for key in
                    ("cold_import_seconds", "warm_import_seconds", "incremental_import_seconds")},
                "questions": [{"question": "business rule", "seconds": seconds}]}
        report = summarize([sample(1, False), sample(3, False), sample(100, True)], ["candidate"])
        self.assertEqual(report["candidate"]["sample_count"], 2)
        self.assertEqual(report["candidate"]["median_seconds"]["cold_import_seconds"], 2)
        self.assertEqual(report["candidate"]["questions"][0]["median_first_request_seconds"], 2)


if __name__ == "__main__":
    unittest.main()
