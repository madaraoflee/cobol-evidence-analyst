"""Broad automatic reads must not evict known business prerequisites."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evidence_context import EvidenceContext
from source_reading import _identify_page


def excerpt(start, text, **metadata):
    page = {"relative_path": "entry.cbl", "source_sha256": "a" * 64,
            "start_line": start, "end_line": start + len(text.split("\n")) - 1,
            "source_text": text, "span_truncated": False,
            "selection_reasons": ["question_match"], **metadata}
    page["evidence_id"] = _identify_page(page)
    return page


class ContinuationPriorityTests(unittest.TestCase):
    def setUp(self):
        self.pool = EvidenceContext()
        self.head = excerpt(1, "01 RATE PIC 9 VALUE 3.\nIF ENABLED = 1")
        self.formula = excerpt(100, "COMPUTE TOTAL = BASE * RATE + 7")
        self.tail = excerpt(101, "\n".join(["ADD 1 TO AUX"] * 8))
        self.pool.accept({"pages": [self.head, self.formula]}, "search")
        self.budget = sum(len(p["source_text"]) for p in (self.head, self.formula, self.tail)) - len(self.head["source_text"])

    def test_redundant_cursor_tail_does_not_displace_declaration_or_guard(self):
        self.pool.accept({"pages": [self.tail]}, "read_continuation")
        pages = self.pool.selected_pages(self.budget,
            priority_targets=[{"relative_path": "entry.cbl", "start_line": 100, "end_line": 100000}])
        self.assertEqual({p["evidence_id"] for p in pages},
                         {self.head["evidence_id"], self.formula["evidence_id"]})
        self.assertLessEqual(sum(len(p["source_text"]) for p in pages), self.budget)
        self.assertEqual([p["evidence_id"] for p in self.pool.selection_frontier], [self.tail["evidence_id"]])
        self.assertIn(self.tail["evidence_id"], self.pool.pages)

    def test_explicit_read_still_has_priority_over_older_search(self):
        self.pool.accept({"pages": [self.tail]}, "read_continuation")
        self.pool.accept({"pages": [self.tail]}, "read")
        selected = self.pool.selected_pages(self.budget, priority_targets=[self.tail["evidence_id"]])
        self.assertIn(self.tail["evidence_id"], {p["evidence_id"] for p in selected})

    def test_newly_identified_condition_in_continuation_keeps_core_priority(self):
        condition = excerpt(101, "IF STATUS = 'OK'", semantic_roles=["condition"])
        self.pool.accept({"pages": [condition]}, "read_continuation")
        selected = self.pool.selected_pages(len(condition["source_text"]),
            priority_targets=[condition["evidence_id"]])
        self.assertEqual([p["evidence_id"] for p in selected], [condition["evidence_id"]])

    def test_later_continuation_does_not_erase_a_previous_targeted_read(self):
        target = excerpt(101, "IF STATUS = 'OK'")
        self.pool.accept({"pages": [target]}, "read")
        self.pool.accept({"pages": [target]}, "read_continuation")
        selected = self.pool.selected_pages(len(target["source_text"]),
            priority_targets=[target["evidence_id"]])
        self.assertEqual([p["evidence_id"] for p in selected], [target["evidence_id"]])

    def cursor_case(self, *, complete=False, material=True):
        pool = EvidenceContext()
        lines = [f"DISPLAY 'ROW-{index:02d}'." for index in range(1, 15)]
        old = excerpt(1, "\n".join(lines[:12]))
        initial = excerpt(1, "\n".join(lines[:2]))
        following = excerpt(13, "\n".join(lines[12:]))
        request = {"relative_path": "entry.cbl", "start_line": 1,
                   "end_line": 14 if complete else 30}
        pool.accept({"pages": [old]}, "search")
        pool.sent_any_round_ids.add(old["evidence_id"])
        pool.accept({"pages": [initial], "requested_range": request}, "read")
        task = next(iter(pool.tasks.values()))
        self.assertEqual(task.next_start_line, 13)
        task.material_to_question = material
        pool.accept({"pages": [following], "requested_range": {**request, "start_line": 13}},
                    "read_continuation")
        return pool, old, initial, following, task

    def test_real_cursor_progress_displaces_a_large_old_search_within_same_budget(self):
        pool, old, initial, following, task = self.cursor_case()
        budget = len(old["source_text"])
        selected = pool.selected_pages(budget)
        self.assertEqual({p["evidence_id"] for p in selected},
                         {initial["evidence_id"], following["evidence_id"]})
        self.assertEqual(task.next_start_line, 15)
        self.assertLessEqual(sum(len(p["source_text"]) for p in selected), budget)
        self.assertEqual([p["evidence_id"] for p in pool.selection_frontier], [old["evidence_id"]])

    def test_cursor_page_stays_pending_after_budget_omission_until_actually_sent(self):
        pool, old, initial, following, _ = self.cursor_case()
        selected = pool.selected_pages(len(initial["source_text"]))
        self.assertNotIn(following["evidence_id"], {p["evidence_id"] for p in selected})
        for _ in range(2):
            selected = pool.selected_pages(len(old["source_text"]))
            self.assertIn(following["evidence_id"], {p["evidence_id"] for p in selected})
        pool.sent_any_round_ids.update(p["evidence_id"] for p in selected)
        selected = pool.selected_pages(len(old["source_text"]))
        self.assertEqual([p["evidence_id"] for p in selected], [old["evidence_id"]])

    def test_last_cursor_page_is_still_pending_after_task_becomes_complete(self):
        pool, old, initial, following, task = self.cursor_case(complete=True)
        self.assertEqual(task.state, "complete")
        self.assertIsNone(task.next_start_line)
        selected = pool.selected_pages(len(old["source_text"]))
        self.assertEqual({p["evidence_id"] for p in selected},
                         {initial["evidence_id"], following["evidence_id"]})

    def test_real_cursor_cannot_displace_core_targeted_or_located_dependency(self):
        pool, _, initial, following, _ = self.cursor_case()
        dependency = excerpt(40, "01 RATE PIC 9.\nIF ACTIVE = 1",
                             selection_reasons=["dependency_context"])
        condition = excerpt(70, "IF RETURN-STATUS = 9", semantic_roles=["condition"])
        pool.accept({"pages": [dependency, condition]}, "search")
        protected = [initial, dependency, condition]
        budget = sum(len(page["source_text"]) for page in protected)
        selected = pool.selected_pages(budget, priority_targets=[following["evidence_id"]])
        self.assertEqual({p["evidence_id"] for p in selected}, {p["evidence_id"] for p in protected})
        self.assertTrue(any(p["evidence_id"] == following["evidence_id"]
                            for p in pool.selection_frontier))

    def test_nonmaterial_or_unrelated_cursor_does_not_gain_pending_priority(self):
        pool, old, _, following, _ = self.cursor_case(material=False)
        self.assertEqual([p["evidence_id"] for p in pool.selected_pages(len(old["source_text"]))],
                         [old["evidence_id"]])
        # Calling an operation a continuation does not create a prior read task.
        pool = EvidenceContext()
        pool.accept({"pages": [old]}, "search")
        pool.accept({"pages": [following], "requested_range": {
            "relative_path": "entry.cbl", "start_line": 13, "end_line": 14}}, "read_continuation")
        self.assertEqual([p["evidence_id"] for p in pool.selected_pages(len(old["source_text"]))],
                         [old["evidence_id"]])

    def test_larger_cursor_page_already_supplied_as_exact_fragments_is_not_new(self):
        pool, old, _, following, _ = self.cursor_case()
        for index, line in enumerate(following["source_text"].split("\n"), following["start_line"]):
            fragment = excerpt(index, line)
            pool.accept({"pages": [fragment]}, "read_continuation")
            pool.sent_any_round_ids.add(fragment["evidence_id"])
        self.assertNotIn(following["evidence_id"], pool.sent_any_round_ids)
        selected = pool.selected_pages(len(old["source_text"]))
        self.assertEqual([p["evidence_id"] for p in selected], [old["evidence_id"]])

    def test_prior_fragments_with_gap_wrong_version_instance_or_text_do_not_consume_pending(self):
        for invalid in ("gap", "version", "instance", "replacement", "text", "truncated"):
            with self.subTest(invalid=invalid):
                pool, old, _, following, _ = self.cursor_case()
                lines = following["source_text"].split("\n")
                first = excerpt(13, lines[0])
                second = excerpt(14, lines[1])
                if invalid == "gap":
                    fragments = [second]
                else:
                    changes = {"version": {"source_sha256": "b" * 64},
                        "instance": {"include_chain": [{"relative_path": "other.cbl", "line": 8}]},
                        "replacement": {"replacement_trace": [{"from": "OLD", "to": "NEW"}]},
                        "text": {"source_text": "DISPLAY 'DIFFERENT'."},
                        "truncated": {"span_truncated": True}}[invalid]
                    first.update(changes)
                    first["evidence_id"] = _identify_page(first)
                    fragments = [first, second]
                pool.accept({"pages": fragments}, "read_continuation")
                pool.sent_any_round_ids.update(p["evidence_id"] for p in fragments)
                selected = pool.selected_pages(len(old["source_text"]))
                self.assertIn(following["evidence_id"], {p["evidence_id"] for p in selected})


if __name__ == "__main__":
    unittest.main()
