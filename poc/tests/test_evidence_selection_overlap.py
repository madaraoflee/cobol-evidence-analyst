"""Exact evidence coverage must not charge nested physical excerpts twice."""

from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evidence_context import EvidenceContext, EvidenceGroup, Observation, SourceRef
from source_reading import _identify_page


def page(path, lines, first, last, *, roles=(), group=None, reasons=()):
    text = "\n".join(lines)
    value = {"relative_path": path, "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "start_line": first, "end_line": last, "source_text": "\n".join(lines[first - 1:last]),
        "span_truncated": False, "include_chain": [], "selection_reasons": list(reasons),
        "semantic_roles": list(roles)}
    if group:
        value["group_ids"] = [group]
    value["evidence_id"] = _identify_page(value)
    return value


class EvidenceSelectionOverlapTests(unittest.TestCase):
    def test_covered_declarations_and_nested_core_keep_formula_within_budget(self):
        lines = ["PROGRAM-ID. AMOUNT-RULE.", "DATA DIVISION.", "WORKING-STORAGE SECTION.",
                 "01 BASIS PIC 9(5).", "01 FACTOR PIC 9V99 VALUE 1.25.",
                 "01 FINAL-AMOUNT PIC 9(7)V99.", "PROCEDURE DIVISION.", "MAIN."]
        lines += ["DISPLAY 'UNCHANGED'."] * 700
        lines += ["MOVE 10 TO BASIS.", "IF BASIS > ZERO", "COMPUTE FINAL-AMOUNT = BASIS * FACTOR",
                  "ELSE", "MOVE ZERO TO FINAL-AMOUNT", "END-IF.", "GOBACK."]
        pool = EvidenceContext()
        group_id = "amount-core"
        core = [page("rule.cbl", lines, 709, 709, roles=["input"], group=group_id),
                page("rule.cbl", lines, 710, 714, roles=["condition"], group=group_id),
                page("rule.cbl", lines, 711, 711, roles=["result"], group=group_id),
                page("rule.cbl", lines, 710, 710, roles=["condition"], group=group_id),
                page("rule.cbl", lines, 712, 712, roles=["condition"], group=group_id)]
        declarations = [page("rule.cbl", lines, line, line, roles=["declaration"])
                        for line in (4, 5, 6)]
        pool.accept({"pages": core + declarations}, "business_context")
        header = page("rule.cbl", lines, 1, 13, reasons=["agent_requested_read"])
        pool.accept({"pages": [header]}, "read")
        observations = [Observation(value["evidence_id"], value["semantic_roles"][0], [SourceRef(
            "source", "rule.cbl", value["source_sha256"], value["start_line"], value["end_line"],
            hashlib.sha256(value["source_text"].encode()).hexdigest(), value["evidence_id"])]) for value in core]
        group = EvidenceGroup(group_id, {"relative_path": "rule.cbl", "line": 711}, "explain", observations)
        selected = pool.selected_pages(512, evidence_groups=[group],
            priority_targets=[{"relative_path": "rule.cbl", "start_line": 1, "end_line": 13}])
        self.assertTrue(any("COMPUTE FINAL-AMOUNT" in value["source_text"] for value in selected))
        self.assertTrue(any("VALUE 1.25" in value["source_text"] for value in selected))
        self.assertLessEqual(sum(len(value["source_text"]) for value in selected), 512)
        visible = {value["evidence_id"] for value in selected}
        for value in core + declarations:
            self.assertIn(pool.covering_id(value["evidence_id"], selected), visible)
        self.assertNotIn(core[2]["evidence_id"], visible)
        self.assertFalse(pool.selection_frontier)

    def test_superset_preserves_exact_fragment_annotations_without_expanding_pool_obligations(self):
        lines = ["IF ENABLED", "MOVE 10 TO TOTAL-AMOUNT", "END-IF."]
        fragment = page("rule.cbl", lines, 2, 2, roles=["result"], group="result-group")
        whole = page("rule.cbl", lines, 1, 3)
        other_instance = {**deepcopy(fragment), "evidence_id": "ev_other_annotation_instance",
            "semantic_roles": ["unrelated-instance"], "group_ids": ["other-group"],
            "replacement_trace": [{"from": "INPUT-AMOUNT", "to": "TOTAL-AMOUNT"}]}
        pool = EvidenceContext()
        pool.accept({"pages": [fragment, other_instance]}, "business_context")
        pool.accept({"pages": [whole]}, "read")
        selected = pool.selected_pages(len(whole["source_text"]), priority_targets=[whole["evidence_id"]])
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["evidence_id"], whole["evidence_id"])
        self.assertEqual(selected[0]["source_text"], whole["source_text"])
        self.assertIn("result", selected[0]["semantic_roles"])
        self.assertIn("result-group", selected[0]["group_ids"])
        self.assertNotIn("unrelated-instance", selected[0]["semantic_roles"])
        self.assertNotIn("other-group", selected[0]["group_ids"])
        self.assertEqual(pool.pages[whole["evidence_id"]]["semantic_roles"], [])
        self.assertEqual(pool.pages[whole["evidence_id"]]["group_ids"], [])
        self.assertIn(fragment["evidence_id"], {item["evidence_id"] for item in
            pool.selected_pages(len(fragment["source_text"]), priority_targets=[fragment["evidence_id"]])})

    def test_later_union_clears_frontier_for_a_previously_rejected_core_span(self):
        lines = ["A" * 10, "B" * 10, "C" * 100, "D" * 10, "E" * 10]
        first = page("rule.cbl", lines, 1, 3)
        second = page("rule.cbl", lines, 3, 5)
        core = page("rule.cbl", lines, 2, 4, roles=["condition"], group="core")
        union = page("rule.cbl", lines, 1, 5)
        pool = EvidenceContext()
        pool.accept({"pages": [first, second, core, union]}, "search")
        selected = pool.selected_pages(250, priority_targets=[item["evidence_id"]
            for item in (first, second, core, union)])
        self.assertEqual([item["evidence_id"] for item in selected], [union["evidence_id"]])
        self.assertEqual(pool.covering_id(core["evidence_id"], selected), union["evidence_id"])
        self.assertEqual(pool.selection_trim_events, [])
        self.assertEqual(pool.selection_frontier, [])

    def test_rebinding_requires_exact_text_version_and_expansion_instance(self):
        lines = ["IF ENABLED", "MOVE 10 TO TOTAL-AMOUNT", "END-IF."]
        original = page("rule.cbl", lines, 2, 2)
        covering = page("rule.cbl", lines, 1, 3)
        pool = EvidenceContext()
        pool.accept({"pages": [original]}, "search")
        self.assertEqual(pool.covering_id(original["evidence_id"], [covering]), covering["evidence_id"])
        variants = {
            "text": {"source_text": "IF ENABLED\nMOVE 11 TO TOTAL-AMOUNT\nEND-IF."},
            "hash": {"source_sha256": "b" * 64},
            "path": {"relative_path": "other.cbl"},
            "include": {"include_chain": [{"relative_path": "caller.cbl", "line": 12}]},
            "replacement": {"replacement_trace": [{"from": "TOTAL-AMOUNT", "to": "OTHER-AMOUNT"}]},
            "truncated": {"span_truncated": True},
        }
        for name, changes in variants.items():
            with self.subTest(name=name):
                altered = {**deepcopy(covering), **changes}
                self.assertEqual(pool.covering_id(original["evidence_id"], [altered]), original["evidence_id"])

    def test_unversioned_excerpts_cannot_rebind_across_evidence_ids(self):
        lines = ["IF ENABLED", "MOVE 10 TO TOTAL-AMOUNT", "END-IF."]
        original = page("rule.cbl", lines, 2, 2)
        covering = page("rule.cbl", lines, 1, 3)
        original.pop("source_sha256")
        covering.pop("source_sha256")
        pool = EvidenceContext()
        pool.accept({"pages": [original]}, "search")
        self.assertEqual(pool.covering_id(original["evidence_id"], [covering]), original["evidence_id"])
        self.assertEqual(pool.covering_id(original["evidence_id"], [original]), original["evidence_id"])

    def test_distinct_replacement_instances_are_charged_separately(self):
        lines = ["MOVE 10 TO TOTAL-AMOUNT"]
        first = page("rule.cbl", lines, 1, 1, roles=["result"])
        first["replacement_trace"] = [{"from": "SOURCE-AMOUNT", "to": "TOTAL-AMOUNT"}]
        second = {**deepcopy(first), "evidence_id": "ev_other_instance",
                  "replacement_trace": [{"from": "INPUT-AMOUNT", "to": "TOTAL-AMOUNT"}]}
        pool = EvidenceContext()
        pool.accept({"pages": [first, second]}, "business_context")
        self.assertEqual(len(pool.selected_pages(len(first["source_text"]))), 1)
        self.assertEqual(len(pool.selected_pages(2 * len(first["source_text"]))), 2)


if __name__ == "__main__":
    unittest.main()
