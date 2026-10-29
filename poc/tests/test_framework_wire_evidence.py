"""Only actual transmitted page IDs are exposed as framework source citations."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from framework_semantics import visible_framework_facts


class FrameworkWireEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.digest = hashlib.sha256(b"FIRST.\nSECOND.").hexdigest()
        self.page = {"evidence_id": "ev_page_current", "relative_path": "layout.cpy",
            "source_sha256": self.digest, "start_line": 1, "end_line": 2,
            "source_text": "FIRST.\nSECOND."}
        self.span = {"relative_path": "layout.cpy", "source_sha256": self.digest,
            "start_line": 1, "end_line": 2}
        self.fact = {"relative_path": "layout.cpy", "source_sha256": self.digest,
            "reference_ids": ["fw:rule"], "source_ranges": [self.span],
            "layout_evidence_refs": [{**self.span, "evidence_id": "ev_indexed_statement"}]}
        self.references = [{"reference_id": "fw:rule", "text": "The documented request."}]

    def visible(self, pages):
        return visible_framework_facts([self.fact], pages, self.references)

    def test_nested_statement_reference_uses_actual_covering_page(self):
        original = deepcopy(self.fact)
        row, = self.visible([self.page])
        self.assertEqual(row["layout_evidence_refs"][0]["evidence_id"], "ev_page_current")
        self.assertEqual(row["layout_evidence_refs"][0]["source_evidence_ids"], ["ev_page_current"])
        self.assertEqual(self.fact, original)

    def test_multi_page_reference_never_picks_one_incomplete_page(self):
        first = {**self.page, "end_line": 1, "source_text": "FIRST.", "evidence_id": "ev_page_first"}
        second = {**self.page, "start_line": 2, "source_text": "SECOND.", "evidence_id": "ev_page_second"}
        row, = self.visible([first, second])
        ref = row["layout_evidence_refs"][0]
        self.assertNotIn("evidence_id", ref)
        self.assertEqual(ref["source_evidence_ids"], ["ev_page_first", "ev_page_second"])

    def test_stale_source_or_conflicting_text_does_not_create_a_citation(self):
        self.assertEqual(self.visible([{**self.page, "source_sha256": "0" * 64}]), [])
        self.assertEqual(self.visible([self.page, {**self.page, "source_text": "CONFLICT.\nSECOND."}]), [])

    def test_uncovered_nested_reference_does_not_keep_an_internal_id(self):
        self.fact["layout_evidence_refs"].append({**self.span, "start_line": 3, "end_line": 3,
                                                 "evidence_id": "ev_hidden_statement"})
        row, = self.visible([self.page])
        ref = row["layout_evidence_refs"][1]
        self.assertNotIn("evidence_id", ref)
        self.assertEqual(ref["source_evidence_ids"], [])


if __name__ == "__main__":
    unittest.main()
