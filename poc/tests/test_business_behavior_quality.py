"""Offline behavior anchors: varied source, visible evidence, and strict bounds.

These tests inspect grounded reading guidance. They do not claim that lexical
observations verify paths or that a real model writes a good business answer.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_behavior import build_behavior_guide, wants_behavior_explanation


QUESTION = "账单处理的业务逻辑是什么，为什么有些记录会跳过？"
FLOW = ("IF ELIGIBLE-FLAG = 'N'\n"
        "MOVE 'S' TO RESULT-STATUS\n"
        "GOBACK\n"
        "END-IF.\n"
        "WRITE BILL-RECORD.\n"
        "MOVE 'C' TO RESULT-STATUS.\n"
        "GOBACK.")


def page(text, *, reference="ev:flow", path="billing.cbl", start=1, digest=None):
    return {"evidence_id": reference, "relative_path": path, "start_line": start,
            "end_line": start + len(text.splitlines()) - 1, "source_text": text,
            "source_sha256": digest or hashlib.sha256(text.encode()).hexdigest()}


def investigation(*references, kind="business_steps", status="SATISFIED"):
    return {"required_items": [{"kind": kind, "status": status,
            "candidate_count": len(references), "evidence_ids": list(references)}]}


def kinds(guide):
    return {item["kind"] for item in guide["observations"]}


class BusinessBehaviorQualityTests(unittest.TestCase):
    def guide(self, text=FLOW, question=QUESTION):
        return build_behavior_guide(question, investigation("ev:flow"), [page(text)])

    def test_visible_branch_exit_write_and_status_are_separate_reading_anchors(self):
        guide = self.guide()
        self.assertEqual(kinds(guide), {"conditions", "early_exit", "file_io", "state_assignment"})
        self.assertEqual([item["source_statement"] for item in guide["observations"]
                          if item["kind"] == "early_exit"], ["GOBACK"])
        self.assertTrue(any(item["source_statement"] == "WRITE BILL-RECORD."
                            for item in guide["observations"]))
        self.assertFalse(guide["semantic_execution_verified"])
        self.assertEqual(guide["interpretation"], "visible_syntax_not_execution")
        self.assertTrue(all(item["supplied_reference_ids"] == ["ev:flow"]
                            for item in guide["observations"]))
        self.assertTrue(all("outcome" not in item and "execution_order" not in item
                            for item in guide["observations"]))

    def test_replacing_conditional_return_with_continue_removes_early_exit_anchor(self):
        changed = FLOW.replace("GOBACK\nEND-IF", "CONTINUE\nEND-IF")
        self.assertIn("early_exit", kinds(self.guide()))
        self.assertNotIn("early_exit", kinds(self.guide(changed)))
        self.assertIn("file_io", kinds(self.guide(changed)))

    def test_terminal_return_is_not_a_skip_or_error(self):
        guide = self.guide("WRITE BILL-RECORD.\nGOBACK.")
        self.assertEqual(kinds(guide), {"file_io"})

    def test_period_closes_branch_and_paragraph_starts_an_independent_scope(self):
        for text in ("IF ELIGIBLE-FLAG = 'N' CONTINUE.\nGOBACK.",
                     "IF ELIGIBLE-FLAG = 'N'\nNEXT-PARAGRAPH.\nGOBACK.",
                     "IF ELIGIBLE-FLAG = 'N'\nEND PROGRAM FIRST-RULE.\n"
                     "PROGRAM-ID. SECOND-RULE.\nPROCEDURE DIVISION.\nGOBACK."):
            with self.subTest(text=text):
                self.assertNotIn("early_exit", kinds(self.guide(text)))

    def test_conditional_sentence_ending_goback_keeps_local_branch(self):
        guide = self.guide("IF ELIGIBLE-FLAG = 'N'\nGOBACK.")
        self.assertIn("early_exit", kinds(guide))

    def test_quoted_display_comments_and_embedded_language_are_not_cobol_behavior(self):
        text = ("DISPLAY 'IF FLAG = Y WRITE SECRET-RECORD GOBACK'.\n"
                'DISPLAY "MOVE N TO STATUS CALL HELPER".\n'
                "*> IF OTHER-FLAG = 'N' GOBACK WRITE OTHER-RECORD\n"
                "EXEC SQL SELECT WRITE FROM RULES END-EXEC.\n"
                "WRITE BILL-RECORD.")
        guide = self.guide(text)
        self.assertEqual(len(guide["observations"]), 1)
        self.assertEqual(guide["observations"][0]["source_statement"], "WRITE BILL-RECORD.")

    def test_fixed_format_comments_do_not_supply_branches(self):
        text = ("000100*IF ELIGIBLE-FLAG = 'N'\n"
                "000200*    GOBACK\n"
                "000300     WRITE BILL-RECORD.\n")
        self.assertEqual(kinds(self.guide(text)), {"file_io"})

    def test_unfinished_quoted_literal_does_not_become_executable_syntax(self):
        text = "DISPLAY 'IF ELIGIBLE-FLAG = N WRITE BILL-RECORD GOBACK"
        self.assertEqual(self.guide(text)["observations"], [])

    def test_inline_comment_marker_inside_literal_preserves_assignment_and_next_write(self):
        text = ('MOVE "text *> retained" TO OUTPUT-TEXT.\n'
                'WRITE BILL-RECORD. *> A real comment contains \'WRITE OTHER-RECORD\n')
        guide = self.guide(text)
        self.assertEqual([row["source_statement"] for row in guide["observations"]],
                         ['MOVE "text *> retained" TO OUTPUT-TEXT.', "WRITE BILL-RECORD."])

    def test_literal_spaces_are_never_rewritten_by_behavior_guidance(self):
        text = "IF ROUTE-CODE = 'A  B'\nMOVE 'X  Y' TO OUTPUT-TEXT\nEND-IF."
        guide = self.guide(text)
        self.assertEqual([row["source_statement"] for row in guide["observations"]],
                         ["IF ROUTE-CODE = 'A  B'", "MOVE 'X  Y' TO OUTPUT-TEXT"])

    def test_source_limit_omits_partial_first_line_instead_of_emitting_a_prefix(self):
        guide = self.guide("MOVE '" + "X" * 80000 + "' TO OUTPUT-TEXT.")
        self.assertTrue(guide["source_scan_truncated"])
        self.assertEqual(guide["observations"], [])

    def test_source_limit_preserves_complete_lines_but_omits_partial_tail_line(self):
        guide = self.guide("WRITE BILL-RECORD.\nMOVE '" + "X" * 80000 + "' TO OUTPUT-TEXT.")
        self.assertTrue(guide["source_scan_truncated"])
        self.assertEqual([row["source_statement"] for row in guide["observations"]],
                         ["WRITE BILL-RECORD."])

    def test_long_quoted_condition_is_omitted_instead_of_becoming_a_changed_literal(self):
        guide = self.guide("IF ROUTE-CODE = '" + "A  B " * 60 + "'\nCONTINUE\nEND-IF.")
        self.assertEqual(guide["observations"], [])
        self.assertEqual(guide["omitted_observations"], 1)
        self.assertFalse(guide["source_scan_truncated"])

    def test_unreferenced_or_unsupplied_source_cannot_enter_guide(self):
        visible = page("WRITE BILL-RECORD.")
        hidden = page(FLOW, reference="ev:hidden", path="other.cbl")
        guide = build_behavior_guide(QUESTION, investigation("ev:flow"), [visible, hidden])
        self.assertEqual(kinds(guide), {"file_io"})
        self.assertEqual(build_behavior_guide(QUESTION, investigation("ev:hidden"), [visible])["observations"], [])

    def test_controls_without_located_workflow_or_open_workflow_do_not_create_guide(self):
        for plan in (investigation("ev:flow", kind="conditions"),
                     investigation("ev:flow", status="OPEN")):
            with self.subTest(plan=plan):
                self.assertEqual(build_behavior_guide(QUESTION, plan, [page(FLOW)])["observations"], [])
        self.assertIn("early_exit", kinds(build_behavior_guide(QUESTION,
            investigation("ev:flow", status="PARTIAL"), [page(FLOW)])))

    def test_physical_gap_or_different_version_does_not_extend_branch_scope(self):
        first = page("IF ELIGIBLE-FLAG = 'N'", reference="ev:first", digest="same-version")
        plan = investigation("ev:first", "ev:second")
        adjacent = page("GOBACK\nEND-IF.", reference="ev:second", start=2, digest="same-version")
        self.assertIn("early_exit", kinds(build_behavior_guide(QUESTION, plan, [first, adjacent])))
        for second in (page("GOBACK\nEND-IF.", reference="ev:second", start=20, digest="same-version"),
                       page("GOBACK\nEND-IF.", reference="ev:second", start=2, digest="other-version")):
            with self.subTest(second=second):
                self.assertNotIn("early_exit", kinds(build_behavior_guide(QUESTION, plan, [first, second])))

    def test_referenced_conditions_page_can_add_controls_only_after_steps_are_visible(self):
        steps = page("WRITE BILL-RECORD.")
        condition = page("IF ELIGIBLE-FLAG = 'Y'\nCONTINUE\nEND-IF.",
                         reference="ev:condition", path="condition.cbl")
        plan = investigation("ev:flow")
        plan["required_items"] += investigation("ev:condition", kind="conditions")["required_items"]
        guide = build_behavior_guide(QUESTION, plan, [steps, condition])
        self.assertEqual(kinds(guide), {"conditions", "file_io"})
        self.assertEqual(build_behavior_guide(QUESTION, plan, [condition])["observations"], [])

    def test_late_statement_in_more_than_eight_adjacent_pages_cites_its_actual_page(self):
        lines = [f"MOVE {number} TO WORK-VALUE-{number}." for number in range(10)]
        lines.append("WRITE BILL-RECORD.")
        pages = [page(line, reference=f"ev:part-{number}", start=number + 1, digest="one-version")
                 for number, line in enumerate(lines)]
        plan = investigation(*(part["evidence_id"] for part in pages))
        guide = build_behavior_guide(QUESTION, plan, pages)
        write = next(row for row in guide["observations"] if row["kind"] == "file_io")
        self.assertEqual(write["supplied_reference_ids"], ["ev:part-10"])
        for row in guide["observations"]:
            cited = [part["source_text"] for part in pages
                     if part["evidence_id"] in row["supplied_reference_ids"]]
            self.assertIn(row["source_statement"], " ".join(cited))

    def test_multiline_statement_across_adjacent_pages_cites_both_parts(self):
        pages = [page("MOVE 'S'", reference="ev:first", digest="one-version"),
                 page("TO RESULT-STATUS.", reference="ev:second", start=2, digest="one-version")]
        guide = build_behavior_guide(QUESTION, investigation("ev:first", "ev:second"), pages)
        self.assertEqual(len(guide["observations"]), 1)
        assignment = guide["observations"][0]
        self.assertEqual(assignment["source_statement"], "MOVE 'S'\nTO RESULT-STATUS.")
        self.assertEqual(assignment["supplied_reference_ids"], ["ev:first", "ev:second"])

    def test_conflicting_overlapping_versions_do_not_share_provenance(self):
        pages = [page("MOVE 'S' TO RESULT-STATUS.", reference="ev:first", digest="same-declared-version"),
                 page("WRITE BILL-RECORD.", reference="ev:second", digest="same-declared-version")]
        guide = build_behavior_guide(QUESTION, investigation("ev:first", "ev:second"), pages)
        write = next(row for row in guide["observations"] if row["kind"] == "file_io")
        self.assertEqual(write["supplied_reference_ids"], ["ev:second"])

    def test_small_guide_retains_each_available_behavior_kind_without_full_source_duplication(self):
        text = "\n".join([f"MOVE {number} TO WORK-VALUE-{number}." for number in range(40)])
        text += ("\nIF ELIGIBLE-FLAG = 'N'\nGOBACK\nEND-IF.\nWRITE BILL-RECORD.\n"
                 "CALL 'RULE-HELPER'.\nPERFORM UNTIL END-FLAG = 'Y'\nREAD INPUT-FILE\nEND-PERFORM.")
        guide = self.guide(text)
        self.assertEqual(len(guide["observations"]), 12)
        self.assertGreater(guide["omitted_observations"], 0)
        self.assertEqual(kinds(guide), {"conditions", "early_exit", "file_io", "state_assignment", "call", "iteration"})
        self.assertTrue(all(len(row["source_statement"]) <= 240 for row in guide["observations"]))

    def test_natural_behavior_questions_and_narrow_field_facts_remain_distinct(self):
        for question in (QUESTION, "出 bill 的逻辑是什么？", "出 bill 的邏輯是什麼？",
                         "为什么没有生成账单？", "状态如何变化？", "What is the bill logic?",
                         "Why was this record skipped?", "What is the processing workflow?",
                         "默认值如何改变处理流程？"):
            with self.subTest(question=question):
                self.assertTrue(wants_behavior_explanation(question))
        for question in ("BILL-LOGIC 的字段类型是什么？", "BILL-LOGIC 这个逻辑字段的长度是多少？",
                         "BILL-LOGIC 的初值是什么？", "BILL-AMOUNT 在哪里定义？", "输入来源是什么？",
                         "What is the data type of BILL-LOGIC?", "Where is BILL-AMOUNT defined?",
                         "What is the length of the return flag?"):
            with self.subTest(question=question):
                self.assertFalse(wants_behavior_explanation(question))
                self.assertEqual(self.guide(question=question)["observations"], [])


if __name__ == "__main__":
    unittest.main()
