"""Convergence counts facts and satisfied obligations, not generated IDs."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis_continuation import AnalysisContinuation


class AnalysisContinuationReviewTests(unittest.TestCase):
    def test_generated_evidence_ids_and_order_do_not_reset_idle_rounds(self):
        continuation = AnalysisContinuation("Explain the amount")
        page = {"relative_path": "root.cbl", "source_sha256": "fixed-source-version",
                "start_line": 1, "end_line": 1, "source_text": "MOVE 1 TO AMOUNT.", "evidence_id": "old"}

        def investigation(ids):
            return {"required_items": [{"id": "amount-formula", "kind": "formula",
                                        "status": "SATISFIED", "evidence_ids": ids}]}

        self.assertTrue(continuation.observe([page], [], investigation(["old", "secondary"])))
        for ids in (["secondary", "old"], ["regenerated", "other"]):
            continuation.awaiting_tools = True
            self.assertFalse(continuation.observe([{**page, "evidence_id": ids[0]}], [], investigation(ids)))
        self.assertEqual(continuation.idle_tool_rounds, 2)
        self.assertEqual(continuation.stop_reason, "no_evidence_progress")

    def test_new_satisfied_obligation_or_actual_material_still_counts(self):
        continuation = AnalysisContinuation("Explain the amount")
        page = {"relative_path": "root.cbl", "source_sha256": "fixed-source-version",
                "start_line": 1, "end_line": 1, "source_text": "MOVE 1 TO AMOUNT."}
        formula = {"id": "formula", "kind": "formula", "status": "SATISFIED"}
        self.assertTrue(continuation.observe([page], [], {"required_items": [formula]}))
        continuation.awaiting_tools = True
        inputs = {"id": "inputs", "kind": "inputs", "status": "SATISFIED"}
        self.assertTrue(continuation.observe([page], [], {"required_items": [formula, inputs]}))
        continuation.awaiting_tools = True
        self.assertTrue(continuation.observe([page, {**page, "start_line": 2, "end_line": 2,
            "source_text": "ADD 1 TO AMOUNT."}], [], {"required_items": [formula, inputs]}))
        self.assertEqual(continuation.idle_tool_rounds, 0)


if __name__ == "__main__":
    unittest.main()
