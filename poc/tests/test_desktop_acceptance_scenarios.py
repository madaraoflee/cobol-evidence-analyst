"""Source HTTP rehearsal of passive recovery and explicit retry acceptance."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.desktop_acceptance_scenarios import run_scenarios


class UnknownRequestAcceptanceTests(unittest.TestCase):
    def manifest(self):
        return json.loads((ROOT / "docs/eval/desktop-joint-v1/manifest.json").read_text(encoding="utf-8"))

    def test_contract_retains_original_lifecycle_checks_samples_and_limits(self):
        manifest = self.manifest()
        self.assertEqual(len(manifest["cases"]), 25)
        self.assertEqual(sum(case["repeats"] for case in manifest["cases"]), 79)
        cases = {case["id"]: case for case in manifest["cases"]}
        required = {"lifecycle_remote_unknown_preserved", "lifecycle_no_automatic_resend",
                    "lifecycle_restart_restored", "lifecycle_unknown_isolated_between_conversations",
                    "lifecycle_explicit_retry_once_preserves_unknown"}
        for name, trigger_check, limits in (
            ("cancel_unknown", "lifecycle_cancel_prompt", {"cold_index_seconds": 60,
                "first_provider_seconds": 15, "question_seconds": 20, "cancel_seconds": 2}),
            ("deadline_unknown", "lifecycle_total_deadline_enforced", {"cold_index_seconds": 60,
                "first_provider_seconds": 2.5, "question_seconds": 8}),
        ):
            with self.subTest(case=name):
                case = cases[name]
                self.assertEqual(case["repeats"], 3)
                self.assertEqual(case["limits"], limits)
                self.assertEqual(set(case["assertions"]), required | {trigger_check})
                self.assertEqual(case["assertion_groups"]["lifecycle"], case["assertions"])

    def assert_source_http_recovery(self, name):
        case = next(case for case in self.manifest()["cases"] if case["id"] == name)
        with tempfile.TemporaryDirectory() as directory:
            rows = run_scenarios([sys.executable, str(ROOT / "poc/desktop_app.py")], directory,
                                 {"cases": [{**case, "repeats": 1}]})
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["status"], "passed", {"error": row.get("error"), "checks": row.get("checks")})
            self.assertEqual(set(row["checks"]), set(case["assertions"]))
            self.assertTrue(all(row["checks"].values()))
            self.assertEqual(row["request_counts"], {"before_restart": 1, "after_restart": 1,
                "after_history_open": 1, "after_independent_question": 2, "after_explicit_retry": 3})
            self.assertTrue(all(row["explicit_retry_checks"].values()))
            self.assertNotEqual(row["run_id"], row["explicit_retry_run_id"])
            self.assertEqual(row["explicit_independent_conversation_requests"], 1)
            self.assertEqual(row["explicit_retry_requests"], 1)
            self.assertEqual(row["remote_request_state"], "started")
            self.assertEqual(row["remote_outcome"], "unknown")
            self.assertIsNone(row["real_model_accuracy"])
            for field, limit in case["limits"].items():
                self.assertLessEqual(row["timings"][field], limit)

    def test_cancelled_request_restart_and_history_do_not_resend_but_explicit_retry_does(self):
        self.assert_source_http_recovery("cancel_unknown")

    def test_deadline_request_restart_and_history_do_not_resend_but_explicit_retry_does(self):
        self.assert_source_http_recovery("deadline_unknown")


if __name__ == "__main__":
    unittest.main()
