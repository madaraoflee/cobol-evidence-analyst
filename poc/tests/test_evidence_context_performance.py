"""Preserve request-visible coverage while avoiding repeated source scans."""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evidence_context import EvidenceContext


def source_page(identifier, first, last, *, path="rule.cbl", **extra):
    return {"evidence_id": identifier, "relative_path": path, "start_line": first,
            "end_line": last, "source_text": "CONTINUE", "source_sha256": "a" * 64,
            **extra}


def source_payload(pages, *, first=3, last=8):
    return {"source_context": [{"pages": pages,
        "call_chain": {"links": [{"caller_path": "rule.cbl", "caller_start_line": first,
            "caller_end_line": last, "target_path": "rule.cbl", "target_start_line": last}]},
        "outline": [{"relative_path": "rule.cbl", "units": [
            {"start_line": first, "end_line": last}]}]}],
        "evidence_groups": [{"group_id": "required-rule", "required_evidence_ids": ["first", "second"]}]}


class EvidenceContextPerformanceTests(unittest.TestCase):
    def test_adjacent_ranges_keep_physical_citation_order_and_skip_unrelated_paths(self):
        payload = source_payload([
            source_page("second", 6, 10), source_page("outside", 1, 100, path="other.cbl"),
            source_page("first", 1, 5), source_page("nested", 2, 3)])
        EvidenceContext.reconcile_payload(payload)
        bundle = payload["source_context"][0]
        link = bundle["call_chain"]["links"][0]
        self.assertEqual(link["caller_evidence_ids"], ["first", "second"])
        self.assertEqual(link["target_evidence_ids"], ["second"])
        self.assertFalse(link["requires_source_read"])
        self.assertTrue(bundle["outline"][0]["units"][0]["complete_text_supplied"])
        self.assertEqual(bundle["outline"][0]["retrieved_ranges"], [
            {"start_line": 6, "end_line": 10}, {"start_line": 1, "end_line": 5},
            {"start_line": 2, "end_line": 3}])
        self.assertTrue(payload["evidence_groups"][0]["complete_text_supplied"])

    def test_trimmed_page_invalidates_coverage_and_restoration_clears_visibility_gap(self):
        first, second = source_page("first", 1, 5), source_page("second", 6, 10)
        payload = source_payload([first, second])
        bundle = payload["source_context"][0]
        EvidenceContext.reconcile_payload(payload)
        bundle["pages"].pop(0)
        EvidenceContext.reconcile_payload(payload)
        self.assertEqual(bundle["call_chain"]["links"][0]["caller_evidence_ids"], [])
        self.assertTrue(bundle["call_chain"]["links"][0]["requires_source_read"])
        self.assertFalse(bundle["outline"][0]["units"][0]["complete_text_supplied"])
        group = payload["evidence_groups"][0]
        self.assertEqual(group["missing_evidence_count"], 1)
        self.assertEqual(group["visible_evidence_ids"], ["second"])
        self.assertEqual(group["open_frontier"][0]["evidence_id"], "first")
        bundle["pages"].insert(0, first)
        EvidenceContext.reconcile_payload(payload)
        self.assertEqual(bundle["call_chain"]["links"][0]["caller_evidence_ids"], ["first", "second"])
        self.assertTrue(group["complete_text_supplied"])
        self.assertEqual(group["open_frontier"], [])

    def test_in_place_range_path_and_truncation_changes_cannot_reuse_prior_coverage(self):
        for changed in ({"span_truncated": True}, {"start_line": 7},
                        {"end_line": 7}, {"relative_path": "other.cbl"},
                        {"start_line": True}, {"end_line": "10"}):
            with self.subTest(changed=changed):
                second = source_page("second", 6, 10)
                payload = source_payload([source_page("first", 1, 5), second])
                EvidenceContext.reconcile_payload(payload)
                self.assertFalse(payload["source_context"][0]["call_chain"]["links"][0]["requires_source_read"])
                second.update(changed)
                EvidenceContext.reconcile_payload(payload)
                bundle = payload["source_context"][0]
                self.assertTrue(bundle["call_chain"]["links"][0]["requires_source_read"])
                self.assertFalse(bundle["outline"][0]["units"][0]["complete_text_supplied"])

    def test_gaps_remain_uncovered_and_reconciliation_is_idempotent(self):
        payload = source_payload([source_page("first", 1, 4), source_page("second", 6, 10),
            source_page("truncated-gap", 5, 5, span_truncated=True)])
        EvidenceContext.reconcile_payload(payload)
        bundle = payload["source_context"][0]
        self.assertEqual(bundle["call_chain"]["links"][0]["caller_evidence_ids"], [])
        self.assertEqual(bundle["call_chain"]["links"][0]["target_evidence_ids"], ["second"])
        self.assertFalse(bundle["outline"][0]["units"][0]["complete_text_supplied"])
        expected = copy.deepcopy(payload)
        EvidenceContext.reconcile_payload(payload)
        self.assertEqual(payload, expected)

    def test_many_navigation_queries_inspect_each_page_only_once_per_reconciliation(self):
        class CountedPage(dict):
            lookups = 0

            def get(self, *args):
                self.lookups += 1
                return super().get(*args)

        pages = [CountedPage(source_page(f"page-{index}", index + 1, index + 1))
                 for index in range(200)]
        payload = source_payload(pages)
        bundle = payload["source_context"][0]
        bundle["call_chain"]["links"] = [{"caller_path": "rule.cbl",
            "caller_start_line": first, "caller_end_line": first + 1,
            "target_path": "rule.cbl", "target_start_line": first + 1}
            for first in range(1, 200)]
        bundle["outline"][0]["units"] = [{"start_line": first, "end_line": first + 1}
                                         for first in range(1, 200)]
        EvidenceContext.reconcile_payload(payload)
        self.assertTrue(all(not item["requires_source_read"] for item in bundle["call_chain"]["links"]))
        self.assertTrue(all(item["complete_text_supplied"] for item in bundle["outline"][0]["units"]))
        # Count work rather than using a machine-dependent time threshold.
        self.assertLessEqual(sum(page.lookups for page in pages), len(pages) * 8)


if __name__ == "__main__":
    unittest.main()
