"""The question benchmark must preserve its input and separate timing surfaces."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.benchmark_question_latency import case_policy, clone_index, source_payload_fingerprint, summarize
from tools.desktop_acceptance_runtime import write_profile


class QuestionLatencyBenchmarkTests(unittest.TestCase):
    def test_policy_matches_native_acceptance_profile_defaults_and_case_override(self):
        with tempfile.TemporaryDirectory() as folder:
            provider = SimpleNamespace(base_url="https://127.0.0.1:9443/v1")
            for index, case in enumerate(({}, {"policy": {"max_model_requests": 2, "max_question_seconds": 90}})):
                destination = Path(folder) / str(index)
                write_profile(destination, provider,
                    case.get("policy", {"max_model_requests": 3, "max_answer_revisions": 0}))
                settings = json.loads((destination / "agent-settings.json").read_text())["agent"]
                self.assertEqual(case_policy(case), settings)

    def test_index_backup_is_independent_and_keeps_input_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            source, destination = Path(folder) / "source", Path(folder) / "copy"
            source.mkdir()
            for name in ("diagnosis.json", "programs.json"):
                (source / name).write_text(json.dumps({"original": True}))
            with closing(sqlite3.connect(source / "structural-index.sqlite")) as db, db:
                db.execute("CREATE TABLE facts(value TEXT)")
                db.execute("INSERT INTO facts VALUES ('complete-source')")
            clone_index(source, destination)
            with closing(sqlite3.connect(destination / "structural-index.sqlite")) as db, db:
                db.execute("DELETE FROM facts")
            with closing(sqlite3.connect(source / "structural-index.sqlite")) as db:
                self.assertEqual(db.execute("SELECT value FROM facts").fetchall(), [("complete-source",)])
            self.assertEqual((destination / "diagnosis.json").read_bytes(), (source / "diagnosis.json").read_bytes())
            with self.assertRaises(ValueError):
                clone_index(source, source / "nested")

    def test_timed_summary_excludes_profile_and_keeps_warm_sample_separate(self):
        def row(number, seconds, profile=False):
            return {"sample_index": number, "profile_only": profile,
                "timings": {key: seconds for key in ("total_seconds", "business_chat_seconds",
                    "first_provider_seconds", "after_provider_seconds", "outside_business_chat_seconds")},
                "source_payload": {"sha256": "source"}, "answer_sha256": "answer", "claim_ledger_sha256": "ledger"}
        result = summarize([row(1, 20), row(2, 10), row(3, 1000, True)])
        self.assertEqual(result["timed_samples"], 2)
        self.assertEqual(result["warm_samples"], 1)
        self.assertEqual(result["median_seconds"]["total_seconds"], 15)
        self.assertEqual(result["warm_median_seconds"]["total_seconds"], 10)

    def test_source_digest_detects_late_code_and_truncation_changes(self):
        page = {"relative_path": "main.cbl", "start_line": 1, "end_line": 2,
                "source_sha256": "version", "source_text": "MAIN.\nGOBACK.", "span_truncated": False}
        def fingerprint(value):
            return source_payload_fingerprint([{"payload": {"source_context": [{"pages": [value]}]}}])
        original = fingerprint(page)
        self.assertNotEqual(original["sha256"], fingerprint({**page, "source_text": "MAIN.\nSTOP RUN."})["sha256"])
        self.assertNotEqual(original["sha256"], fingerprint({**page, "span_truncated": True})["sha256"])
        self.assertEqual(original["pages"], 1)

    def test_worker_guard_rejects_even_loopback_before_network(self):
        script = ("import sys,socket;sys.path.insert(0,sys.argv[1]);"
            "from tools.benchmark_question_latency import block_network;sys.addaudithook(block_network);"
            "socket.create_connection(('127.0.0.1',9))")
        result = subprocess.run([sys.executable, "-c", script, str(ROOT)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Offline benchmark forbids network access", result.stderr)

    def test_workbench_surface_saves_and_recovers_the_real_history_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            result = subprocess.run([sys.executable, str(ROOT / "tools/benchmark_question_latency.py"),
                "--source", str(root / "source"), "--output", str(root / "measurement"),
                "--files", "2", "--lines", "100", "--longest", "100", "--repeats", "2",
                "--surface", "workbench"], capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = json.loads((root / "measurement/report.json").read_text())
            self.assertEqual(report["measurement_surface"], "workbench_start_to_terminal_snapshot")
            self.assertTrue(report["history_recovered_after_restart"])
            self.assertEqual(report["real_model_calls"], 0)
            self.assertEqual(report["model_configuration"]["profile_name"], "workbench")
            self.assertEqual(report["model_configuration"]["max_output_tokens"], 8192)
            self.assertEqual(report["model_configuration"]["timeout_seconds"], 60)
            self.assertTrue(report["summary"]["source_content_identical"])
            self.assertTrue(report["summary"]["answer_identical"])
            for sample in report["samples"]:
                self.assertTrue(sample["checks"]["persisted_history_matches"])
                self.assertEqual(sample["workbench"]["terminal_snapshot"]["status"], "COMPLETED")


if __name__ == "__main__":
    unittest.main()
