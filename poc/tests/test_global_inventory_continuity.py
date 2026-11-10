"""Independent controls for the supplemental final-EXE inventory checker."""
from copy import deepcopy
from pathlib import Path
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.check_global_inventory_continuity import (FILE_COUNT, InventoryResponder,
    QUESTION, create_fixture, run_checks, run_lookup_checks, validate_rows)


class GlobalInventoryContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.expected = create_fixture(self.source)

    def delivered(self):
        return [{"relative_path": name, "formula": value["formula"], "start_line": value["line"],
            "end_line": value["line"], "source_sha256": value["sha256"], "summary": "Business charge.",
            "conditions": "The shown path applies.", "evidence_ids": ["original-" + name]}
            for name, value in self.expected.items()]

    def test_fixture_exceeds_production_paging_threshold_with_independent_roots(self):
        self.assertEqual(len(self.expected), 257)
        self.assertEqual(FILE_COUNT, 257)
        for name in ("charge-000.cbl", "charge-128.cbl", "charge-256.cbl"):
            lines = (self.source / name).read_text().splitlines()
            self.assertEqual(lines[9], self.expected[name]["formula"])
            self.assertNotIn("CALL ", "\n".join(lines))
        validate_rows(self.delivered(), self.expected)

    def test_equal_counts_do_not_hide_missing_middle_and_duplicate_neighbor(self):
        rows = self.delivered()
        rows[128] = deepcopy(rows[129])
        with self.assertRaisesRegex(AssertionError, "duplicates"):
            validate_rows(rows, self.expected)

    def test_formula_source_identity_and_business_content_are_required(self):
        for field, value in (("formula", "wrong formula"), ("source_sha256", "0" * 64),
                             ("conditions", ""), ("summary", ""), ("evidence_ids", [])):
            rows = self.delivered()
            rows[128][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(AssertionError, "lost a business"):
                validate_rows(rows, self.expected)

    def test_controlled_provider_rejects_completed_batch_replay(self):
        fixture = InventoryResponder(self.source)
        name = "charge-000.cbl"
        text = (self.source / name).read_text()
        item = {"candidate_id": "candidate-first", "relative_path": name, "start_line": 10,
                "end_line": 10, "kind": "formula"}
        page = {"relative_path": name, "start_line": 1, "end_line": 11, "source_text": text,
                "source_sha256": self.expected[name]["sha256"], "evidence_id": "original-first"}
        payload = {"question": QUESTION, "repository_inventory": {"items": [item]},
                   "source_context": [{"pages": [page]}]}
        response, reason = fixture.respond({}, payload, 0)
        self.assertEqual(reason, "stop")
        self.assertEqual(len(json.loads(response)["inventory_findings"]), 1)
        with self.assertRaisesRegex(AssertionError, "repeated"):
            fixture.respond({}, payload, 2)

    def test_source_http_preview_exercises_failure_restore_pages_export_and_history(self):
        result = run_checks([sys.executable, str(ROOT / "poc/desktop_app.py")], self.root / "http-preview")
        self.assertTrue(result["checks"])
        self.assertTrue(all(result["checks"].values()), result["checks"])
        self.assertEqual(result["formula_positions"], 257)
        self.assertEqual(result["source_files"], 257)
        self.assertGreater(len(result["observations"]), 1)
        self.assertGreater(result["strict_review_parts"], 1)
        self.assertLess(result["synthetic_model_requests"], 8 * 48 + 2)

    def test_source_http_invalid_and_empty_lookups_stop_without_request_or_time_limits(self):
        result = run_lookup_checks([sys.executable, str(ROOT / "poc/desktop_app.py")], self.root / "lookup-http")
        self.assertTrue(all(result["checks"].values()), result["checks"])
        self.assertEqual(len(result["observations"]), 2)
        for row in result["observations"]:
            self.assertEqual(row["status"], "PARTIAL")
            self.assertEqual(row["convergence_stop_reason"], "no_evidence_progress")
            self.assertEqual(row["model_requests"], 2)
            self.assertEqual(row["counts"]["pending"], 514)


if __name__ == "__main__":
    unittest.main()
