"""Exact selection equivalence while avoiding repeated old-page comparisons."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evidence_context import EvidenceContext


class ReferenceContext(EvidenceContext):
    def _minimal_page_union(self, values, rank, source_instance, *, previous=()):
        # The original full-union algorithm is the equivalence oracle.
        retained, by_instance = [], {}
        for page in sorted(values, key=lambda value: (-len(value.get("source_text", "")), rank(value))):
            covering = by_instance.setdefault(source_instance(page), [])
            if self.covering_id(page["evidence_id"], covering) != page["evidence_id"]:
                continue
            if any(item["evidence_id"] == page["evidence_id"] for item in covering):
                continue
            retained.append(page)
            covering.append(page)
        return retained


def page(identifier, start, end, **options):
    return {"evidence_id": identifier, "relative_path": "entry.cbl",
        "source_sha256": "a" * 64, "start_line": start, "end_line": end,
        "source_text": "\n".join(f"MOVE INPUT-{line} TO RESULT-{line}." for line in range(start, end + 1)),
        **options}


class EvidenceSelectionReuseTests(unittest.TestCase):
    def compare(self, context, maximum, **options):
        expected = ReferenceContext()
        expected.__dict__ = deepcopy(context.__dict__)
        actual = deepcopy(context)
        self.assertEqual(actual.selected_pages(maximum, **options), expected.selected_pages(maximum, **options))
        self.assertEqual(actual.selection_trim_events, expected.selection_trim_events)
        self.assertEqual(actual.selection_frontier, expected.selection_frontier)
        self.assertEqual(actual._pending_continuation_ids, expected._pending_continuation_ids)
        self.assertEqual(actual.pages, expected.pages)
        return actual

    def test_varied_groups_versions_and_copy_instances_preserve_exact_output(self):
        rng = random.Random(7319)
        context = EvidenceContext()
        for index in range(100):
            first = rng.randrange(1, 80)
            options = {"relative_path": f"member-{index % 3}.cbl",
                "source_sha256": ("a" if index % 7 else "b") * 64,
                "span_truncated": index % 13 == 0,
                "include_chain": [{"relative_path": "entry.cbl", "line": index % 3}] if index % 4 == 0 else [],
                "replacement_trace": [{"before": "INPUT", "after": "OUTPUT"}] if index % 9 == 0 else [],
                "semantic_roles": ["condition" if index % 2 else "result"] if index % 4 else [],
                "group_ids": [f"group-{index % 16}"] if index % 4 else [],
                "selection_reasons": ["question_match"] if index % 3 else []}
            context.accept({"pages": [page(f"p-{index}", first, first + rng.randrange(1, 10), **options)]},
                           "read" if index % 11 == 0 else "search")
        for maximum in (300, 1200, 5000, 30000):
            with self.subTest(maximum=maximum):
                self.compare(context, maximum, priority_targets=[{"relative_path": "member-1.cbl", "line": 25}])

    def test_later_union_span_retains_roles_and_recovers_earlier_omissions(self):
        context = EvidenceContext()
        context.accept({"pages": [page("condition", 10, 11, semantic_roles=["condition"], group_ids=["rule"]),
            page("result", 12, 12, semantic_roles=["result"], group_ids=["rule"]),
            page("other", 70, 73, selection_reasons=["question_match"])]}, "search")
        context.accept({"pages": [page("union", 10, 13, selection_reasons=["agent_requested_read"])]}, "read")
        for maximum in (90, 150, 300):
            with self.subTest(maximum=maximum):
                self.compare(context, maximum)

    def test_complete_pages_and_subsequent_reads_do_not_reuse_stale_ranks(self):
        context = EvidenceContext()
        context.accept({"pages": [page("whole", 1, 20, selection_reasons=["complete_working_set"]),
            page("fragment", 6, 9, semantic_roles=["condition"], group_ids=["rule"])]}, "complete_working_set")
        for include_complete in (True, False):
            self.compare(context, 1000, include_complete=include_complete)
        context.selected_pages(200)
        context.accept({"pages": [page("late", 40, 42, semantic_roles=["result"])]}, "read")
        self.compare(context, 200, priority_targets=["late"])

    def test_each_new_page_avoids_rechecking_all_previous_pairs(self):
        counts, results = [], []
        for cls in (ReferenceContext, EvidenceContext):
            context = cls()
            pages = [page(f"p-{index}", index + 1, index + 1, source_text="CONTINUE.") for index in range(80)]
            context.accept({"pages": pages}, "search")
            selected = []
            with patch.object(context, "covering_id", wraps=context.covering_id) as cover:
                for current in pages:
                    selected = context._minimal_page_union([*selected, current],
                        lambda value: value["start_line"], lambda value: (value["relative_path"], value["source_sha256"]),
                        previous=selected)
                counts.append(cover.call_count)
            results.append(selected)
        self.assertEqual(results[0], results[1])
        self.assertLess(counts[1] * 10, counts[0])

    def test_new_covering_span_replaces_previous_pages_and_duplicate_ids_once(self):
        results = []
        for cls in (ReferenceContext, EvidenceContext):
            context = cls()
            first, second, whole = page("first", 3, 4), page("second", 5, 6), page("whole", 1, 8)
            context.accept({"pages": [first, second, whole]}, "search")
            rank = lambda value: value["start_line"]
            instance = lambda value: (value["relative_path"], value["source_sha256"])
            previous = context._minimal_page_union([first, second], rank, instance)
            results.append(context._minimal_page_union([*previous, whole, first, whole], rank, instance,
                                                        previous=previous))
        self.assertEqual(results[0], results[1])
        self.assertEqual([item["evidence_id"] for item in results[1]], ["whole"])


if __name__ == "__main__":
    unittest.main()
