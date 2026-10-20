"""Source HTTP rehearsal of the native semantic-review gate, not model quality."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.business_onboarding_fixture import fixture_identity, sources
from tools.desktop_acceptance_business import semantic_revision_supply_checks
from tools.desktop_acceptance_scenarios import RUNTIME_CASES, run_scenarios


class SemanticReviewNativeAcceptanceTests(unittest.TestCase):
    def case(self):
        manifest = json.loads((ROOT / "docs/eval/desktop-joint-v1/manifest.json").read_text())
        return next(case for case in manifest["cases"] if case["id"] == "business_semantic_revision")

    def payload(self):
        page = {"relative_path": "balance.cbl", "source_sha256": fixture_identity()["balance.cbl"],
                "start_line": 1, "end_line": 21, "source_text": sources()["balance.cbl"].rstrip("\n"),
                "evidence_id": "ev-neutral-query", "span_truncated": False}
        draft = "所有异常都返回 '90'。[ev-neutral-query]"
        return {"source_context": [{"pages": [page]}], "draft_answer": draft,
                "answer_review": {"semantic_risk_candidates": [{
                    "kind": "unconditional_exception_result", "status": "review_candidate",
                    "semantic_verification": False, "assertion": "所有异常都返回 '90'。",
                    "feedback": "核对实际返回路径。",
                    "source_refs": [{"relative_path": "balance.cbl", "source_sha256": page["source_sha256"],
                        "start_line": 13, "end_line": 16, "supplied_reference_ids": [page["evidence_id"]]}]}]}}, draft, [page]

    def test_new_gate_requires_fixed_source_budget_and_four_assertion_groups(self):
        case = self.case()
        self.assertIn(case["id"], RUNTIME_CASES)
        self.assertEqual(case["fixture_sha256"], fixture_identity())
        self.assertEqual(case["repeats"], 3)
        self.assertEqual(case["limits"], {"cold_index_seconds": 60, "first_provider_seconds": 15, "question_seconds": 30})
        self.assertEqual(case["policy"], {"max_model_requests": 2, "max_answer_revisions": 1,
                                         "max_question_seconds": None, "hard_question_seconds": None})
        self.assertEqual(set(case["assertion_groups"]), {"quality", "completeness", "evidence", "lifecycle"})
        self.assertTrue(all(case["assertion_groups"].values()))
        self.assertEqual(case["assertions"], [check for group in case["assertion_groups"].values() for check in group])
        payload, draft, pages = self.payload()
        self.assertTrue(all(semantic_revision_supply_checks(payload, draft, pages).values()))

    def test_review_cannot_replace_draft_or_lose_specific_source_bound_candidate(self):
        payload, draft, pages = self.payload()
        changes = [
            lambda p: p.update(draft_answer="Different answer"),
            lambda p: p.update(draft_answer_truncated=True),
            lambda p: p["answer_review"].update(semantic_risk_candidates=[]),
            lambda p: p["answer_review"]["semantic_risk_candidates"][0].update(semantic_verification=True),
            lambda p: p["answer_review"]["semantic_risk_candidates"][0].update(assertion="Invented claim"),
            lambda p: p["answer_review"]["semantic_risk_candidates"][0].update(feedback=""),
            lambda p: p["answer_review"]["semantic_risk_candidates"][0]["source_refs"][0].update(source_sha256="0" * 64),
            lambda p: p["answer_review"]["semantic_risk_candidates"][0]["source_refs"][0].update(supplied_reference_ids=["ev-absent"]),
            lambda p: p["answer_review"]["semantic_risk_candidates"][0]["source_refs"][0].update(start_line=17),
            lambda p: p["source_context"][0].update(pages=[]),
            lambda p: p["source_context"][0]["pages"][0].update(span_truncated=True),
        ]
        for index, change in enumerate(changes):
            with self.subTest(index=index):
                altered = deepcopy(payload)
                change(altered)
                self.assertFalse(all(semantic_revision_supply_checks(altered, draft, pages).values()))

    def test_source_http_reviews_once_preserves_complete_answer_and_closes_both_requests(self):
        case = self.case()
        with tempfile.TemporaryDirectory() as directory:
            rows = run_scenarios([sys.executable, str(ROOT / "poc/desktop_app.py")], directory,
                                 {"cases": [{**case, "repeats": 1}]})
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["status"], "passed", {"error": row.get("error"), "checks": row.get("checks")})
            self.assertEqual(set(row["checks"]), set(case["assertions"]))
            self.assertTrue(all(row["checks"].values()))
            self.assertEqual(row["model_requests"], 2)
            self.assertEqual(row["agent_status"], "ANALYZED")
            self.assertIsNone(row["real_model_accuracy"])
            self.assertIsNone(row["business_answer_correctness"])
            self.assertTrue(row["run_id"])
            self.assertTrue(row["acceptance_diagnostics"])
            for name, limit in case["limits"].items():
                self.assertLessEqual(row["timings"][name], limit)


if __name__ == "__main__":
    unittest.main()
