"""Excerpt reshaping cannot keep a source investigation running."""

import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analysis_continuation import AnalysisContinuation, material_keys


class ContinuationMaterialCoverageTests(unittest.TestCase):
    def setUp(self):
        self.lines = ["MOVE 1 TO AMOUNT.", "ADD 1 TO AMOUNT.", "DISPLAY AMOUNT.", "GOBACK."]
        self.page = {"relative_path": "root.cbl", "source_sha256": "fixed-source-version",
                     "start_line": 1, "end_line": 4, "source_text": "\n".join(self.lines)}

    def excerpt(self, start, end):
        return {**self.page, "start_line": start, "end_line": end,
                "source_text": "\n".join(self.lines[start - 1:end])}

    def test_read_subranges_of_supplied_source_reaches_no_progress_stop(self):
        tracker = AnalysisContinuation("Explain the amount")
        self.assertTrue(tracker.observe([self.page], [], {}))
        for start, end in ((1, 3), (2, 4)):
            tracker.awaiting_tools = True
            self.assertFalse(tracker.observe([self.excerpt(start, end)], [], {}))
        self.assertEqual(tracker.stop_reason, "no_evidence_progress")
        self.assertEqual(tracker.idle_tool_rounds, 2)

    def test_merging_and_reordering_supplied_fragments_adds_no_material(self):
        fragments = [self.excerpt(1, 2), self.excerpt(3, 4)]
        self.assertEqual(material_keys(fragments), material_keys([self.page]))
        tracker = AnalysisContinuation("Explain the amount")
        tracker.observe(fragments, [], {})
        self.assertFalse(tracker.observe([{**self.page, "evidence_id": "rebuilt-handle"}], [], {}))

    def test_new_line_and_changed_source_content_still_count(self):
        tracker = AnalysisContinuation("Explain the amount")
        tracker.observe([self.excerpt(1, 2)], [], {})
        self.assertTrue(tracker.observe([self.page], [], {}))
        changed = {**self.page, "source_text": self.page["source_text"].replace("ADD 1", "ADD 2")}
        self.assertTrue(tracker.observe([changed], [], {}))

    def test_source_version_and_expansion_instances_keep_distinct_coverage(self):
        for field, value in (("relative_path", "other.cbl"),
                             ("source_sha256", "changed-source-version"),
                             ("include_chain", ["root.cbl", "amount.cpy"]),
                             ("replacement_trace", [{"from": "AMOUNT", "to": "NET-AMOUNT"}]),
                             ("copy_instance", "second-instance")):
            with self.subTest(field=field):
                tracker = AnalysisContinuation("Explain the amount")
                tracker.observe([self.page], [], {})
                self.assertTrue(tracker.observe([{**self.page, field: value}], [], {}))

    def test_empty_expansion_metadata_does_not_manufacture_progress(self):
        tracker = AnalysisContinuation("Explain the amount")
        tracker.observe([self.page], [], {})
        self.assertFalse(tracker.observe([{**self.page, "include_chain": [], "replacement_trace": []}], [], {}))

    def test_regenerated_framework_reference_id_does_not_count(self):
        reference = {"reference_id": "first-handle", "document_sha256": "guide-version",
                     "document_name": "Business guide", "start_line": 1, "end_line": 1,
                     "text": "The result is returned to the caller."}
        tracker = AnalysisContinuation("Explain the amount")
        tracker.observe([], [reference], {})
        for identifier in ("second-handle", "third-handle"):
            tracker.awaiting_tools = True
            self.assertFalse(tracker.observe([], [{**reference, "reference_id": identifier,
                                                   "text_offset_chars": 0}], {}))
        self.assertEqual(tracker.stop_reason, "no_evidence_progress")
        self.assertTrue(tracker.observe([], [{**reference, "document_sha256": "new-guide-version"}], {}))

    def test_checkpoint_retains_union_coverage(self):
        tracker = AnalysisContinuation("Explain the amount")
        tracker.observe([self.excerpt(1, 2)], [], {})
        restored = AnalysisContinuation("Explain the amount", tracker.checkpoint())
        self.assertTrue(restored.observe([self.excerpt(3, 4)], [], {}))
        again = AnalysisContinuation("Explain the amount", restored.checkpoint())
        self.assertFalse(again.observe([self.page], [], {}))

    def test_legacy_checkpoint_migrates_matching_excerpt_without_progress(self):
        legacy_value = ("source", self.page["relative_path"], self.page["source_sha256"],
                        1, 4, self.page["source_text"], [])
        legacy = hashlib.sha256(json.dumps(legacy_value, sort_keys=True, ensure_ascii=False,
                                           separators=(",", ":")).encode()).hexdigest()
        tracker = AnalysisContinuation("Explain the amount", {"known_material": [legacy],
            "revision_requests": 1, "awaiting_tools": True})
        self.assertFalse(tracker.observe([self.page], [], {}))
        self.assertEqual(tracker.revision_requests, 1)
        self.assertEqual(tracker.idle_tool_rounds, 1)
        self.assertFalse(tracker.observe([self.excerpt(2, 3)], [], {}))
        self.assertEqual(AnalysisContinuation("Explain the amount", tracker.checkpoint()).revision_requests, 1)


if __name__ == "__main__":
    unittest.main()
