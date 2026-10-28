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

    def test_new_source_resets_only_the_no_evidence_revision_counter(self):
        continuation = AnalysisContinuation("Explain the amount")
        page = {"relative_path": "root.cbl", "source_sha256": "fixed-source-version",
                "start_line": 1, "end_line": 1, "source_text": "MOVE 1 TO AMOUNT."}
        continuation.observe([page], [], {})
        for gap in ("inputs", "formula"):
            self.assertTrue(continuation.needs_followup("Candidate answer", {
                "missing_aspects": [gap], "status": "incomplete"}, [page]))
        self.assertEqual(continuation.revision_requests, 2)
        expanded = {**page, "end_line": 2, "source_text": "MOVE 1 TO AMOUNT.\nADD 1 TO AMOUNT."}
        self.assertTrue(continuation.observe([expanded], [], {}))
        self.assertEqual(continuation.revision_requests, 0)
        self.assertTrue(continuation.needs_followup("Candidate using the added source", {
            "missing_aspects": ["formula"], "status": "incomplete"}, [expanded]))
        restored = AnalysisContinuation("Explain the amount", continuation.checkpoint())
        self.assertEqual(restored.revision_requests, 1)
        self.assertEqual(restored.total_revision_requests, 3)

    def test_explicit_total_revision_cap_survives_source_progress(self):
        continuation = AnalysisContinuation("Explain the amount")
        page = {"relative_path": "root.cbl", "source_sha256": "fixed-source-version",
                "start_line": 1, "end_line": 1, "source_text": "MOVE 1 TO AMOUNT."}
        review = {"missing_aspects": ["inputs"], "status": "incomplete"}
        continuation.observe([page], [], {})
        self.assertTrue(continuation.needs_followup("First candidate", review, [page], max_total_revisions=1))
        expanded = {**page, "end_line": 2, "source_text": "MOVE 1 TO AMOUNT.\nADD 1 TO AMOUNT."}
        continuation.observe([expanded], [], {})
        self.assertFalse(continuation.needs_followup("Second candidate", review, [expanded],
                                                    max_total_revisions=1))
        self.assertEqual(continuation.stop_reason, "answer_revision_budget")

    def test_new_obligation_or_semantic_material_reopens_a_still_relevant_gap(self):
        page = {"relative_path": "root.cbl", "source_sha256": "fixed-source-version",
                "start_line": 1, "end_line": 2,
                "source_text": "MOVE 1 TO AMOUNT.\nADD 1 TO AMOUNT."}
        review = {"missing_aspects": ["formula"], "status": "incomplete"}
        group = {"semantic_analysis": {"slice_statements": [{"occurrence_id": "amount-update",
            "kind": "ADD", "statement": "ADD 1 TO AMOUNT.", "reads": ["AMOUNT"], "writes": ["AMOUNT"],
            "source_refs": [{key: page[key] for key in ("relative_path", "source_sha256", "start_line", "end_line")}]}]}}
        for investigation, groups in (({"required_items": [{"id": "inputs", "kind": "inputs",
                                                           "status": "SATISFIED"}]}, []), ({}, [group])):
            with self.subTest(investigation=investigation, semantic=bool(groups)):
                continuation = AnalysisContinuation("Explain the amount")
                continuation.observe([page], [], {})
                self.assertTrue(continuation.needs_followup("First candidate", review, [page]))
                self.assertTrue(continuation.observe([page], [], investigation, semantic_groups=groups))
                self.assertEqual(continuation.revision_requests, 0)
                self.assertTrue(continuation.needs_followup("Candidate after source-bound progress", review, [page]))

    def test_changing_gaps_without_new_evidence_still_obeys_revision_allowance(self):
        continuation = AnalysisContinuation("Explain the amount")
        page = {"relative_path": "root.cbl", "source_sha256": "fixed-source-version",
                "start_line": 1, "end_line": 1, "source_text": "MOVE 1 TO AMOUNT."}
        continuation.observe([page], [], {})
        for number, gap in enumerate(("inputs", "formula", "outputs")):
            self.assertFalse(continuation.observe([{**page, "evidence_id": f"regenerated-{number}"}], [], {}))
            self.assertEqual(continuation.needs_followup("Changed wording", {
                "missing_aspects": [gap], "status": "incomplete"}, [page]), number < 2)
        self.assertEqual(continuation.stop_reason, "answer_revision_budget")


if __name__ == "__main__":
    unittest.main()
