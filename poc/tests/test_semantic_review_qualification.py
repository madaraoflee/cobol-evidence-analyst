"""Review nominations distinguish pending questions from source contradictions."""
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_synthesis import assess_business_answer, semantic_risk_candidates
from test_semantic_risk_review import SOURCE, page


class SemanticReviewQualificationTests(unittest.TestCase):
    def test_captured_answer_keeps_text_and_nominates_actual_missing_check_claim(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" /
                              "semantic_boundary_final_answer.json").read_text())
        answer = fixture["raw_answer"]
        self.assertEqual(hashlib.sha256(answer.encode()).hexdigest(), fixture["raw_answer_sha256"])
        for item in fixture["source_pages"]:
            self.assertEqual(hashlib.sha256(item["source_text"].encode()).hexdigest(),
                             item["supplied_excerpt_sha256"])
            self.assertEqual(item["source_sha256"],
                             fixture["source_sha256"][item["relative_path"]])
        rows = semantic_risk_candidates(answer, fixture["source_pages"])
        self.assertEqual([row["kind"] for row in rows], ["database_status_check_denial"])
        self.assertIn("没有文件/数据库状态检查", rows[0]["assertion"])
        self.assertFalse(rows[0]["semantic_verification"])
        self.assertEqual(rows[0]["status"], "review_candidate")
        ref, = rows[0]["source_refs"]
        self.assertEqual((ref["relative_path"], ref["start_line"], ref["end_line"]),
                         ("balance.cbl", 13, 20))
        assessment = assess_business_answer(fixture["question"], answer, {},
                                            fixture["source_pages"], answer_detail="brief")
        self.assertEqual(assessment["reason"], "semantic_scope_review_needed")
        self.assertEqual(assessment["semantic_risk_candidates"], rows)
        self.assertEqual(answer, fixture["raw_answer"])  # No prose rewrite.

    def test_same_quoted_pending_proposition_is_not_an_asserted_guarantee(self):
        for answer in (
            "若要知道“任何异常都返回 '63'”，还需确认程序加载/入口调用成功、"
            "SQL 语句确实被执行并回填 SQLCODE。[ev_source]",
            "如果要验证“LOOKUPQ 所有错误都返回 '63'”，仍需核对入口处理。[ev_source]",
            "若要確認「所有異常都返回 '63'」，還需驗證控制流。[ev_source]",
        ):
            with self.subTest(answer=answer):
                self.assertEqual(semantic_risk_candidates(answer, [page()]), [])

    def test_pending_frame_never_cancels_a_separate_positive_guarantee(self):
        pending = "若要知道“OTHERQ 任何异常都返回 '63'”，还需确认其入口"
        for answer in (
            "LOOKUPQ 任何异常都返回 '63'，" + pending + "。[ev_source]",
            pending + "，但 LOOKUPQ 任何异常都返回 '63'。[ev_source]",
            pending + "。LOOKUPQ 任何异常都返回 '63'。[ev_source]",
            "LOOKUPQ 任何异常都返回 '63'。还需确认入口是否成功。[ev_source]",
            "若要知道“任何异常都返回 '63'”，无需确认其他条件。[ev_source]",
        ):
            with self.subTest(answer=answer):
                self.assertEqual([row["kind"] for row in semantic_risk_candidates(answer, [page()])],
                                 ["unconditional_exception_result"])

    def test_status_check_denial_requires_actual_complete_sql_and_both_branches(self):
        answer = "LOOKUPQ 没有数据库状态检查。[ev_source]"
        rows = semantic_risk_candidates(answer, [page()])
        self.assertEqual([row["kind"] for row in rows], ["database_status_check_denial"])
        self.assertEqual(rows[0]["source_refs"][0]["source_sha256"], page()["source_sha256"])
        no_check = SOURCE.replace("IF SQLCODE = 0\nMOVE '47' TO RESULT-CODE\n"
                                  "ELSE MOVE '63' TO RESULT-CODE\nEND-IF.\n", "")
        for sources in (
            [], [page(no_check)], [page(SOURCE.replace("END-IF.", ""))],
            [page(SOURCE, span_truncated=True)],
            [page(SOURCE, end_line=3)],
            [page(SOURCE), page(SOURCE, source_sha256="0" * 64, evidence_id="ev_other")],
            [page(SOURCE.replace("PROGRAM-ID. LOOKUPQ.", "PROGRAM-ID. ."))],
        ):
            with self.subTest(sources=sources):
                self.assertEqual(semantic_risk_candidates(answer, sources), [])

    def test_status_check_denial_does_not_borrow_another_program_or_false_reference(self):
        other = page(SOURCE.replace("LOOKUPQ", "OTHERQ"), relative_path="other.cbl",
                     evidence_id="ev_other")
        for answer, pages in (
            ("OTHERQ 没有数据库状态检查。[ev_source]", [page()]),
            ("Otherq 没有数据库状态检查。[ev_source]", [page()]),
            ("otherq 没有数据库状态检查。[ev_source]", [page()]),
            ("LOOKUPQ 没有数据库状态检查。[ev_other]", [page(), other]),
            ("没有数据库状态检查。[ev_missing]", [page()]),
            ("没有数据库状态检查。", [page(), other]),
        ):
            with self.subTest(answer=answer):
                self.assertEqual(semantic_risk_candidates(answer, pages), [])
        self.assertEqual(len(semantic_risk_candidates(
            "LOOKUPQ 没有数据库状态检查。[ev_source]", [page(), other])), 1)
        self.assertEqual(len(semantic_risk_candidates(
            "lookupq 没有数据库状态检查。[ev_source]", [page(), other])), 1)

    def test_absent_extra_recovery_hypothesis_or_negated_denial_is_not_absent_check(self):
        for answer in (
            "不能说没有数据库状态检查。[ev_source]",
            "若没有数据库状态检查，需要补查。[ev_source]",
            "没有数据库状态检查吗？[ev_source]",
            "没有数据库状态检查可以保证任何异常都返回。[ev_source]",
            "没有额外数据库状态检查。[ev_source]",
            "没有覆盖所有异常的数据库状态检查。[ev_source]",
            "未见数据库异常恢复处理。[ev_source]",
            "```text\n没有数据库状态检查。[ev_source]\n```",
        ):
            with self.subTest(answer=answer):
                # Only the new source-check absence candidate is in scope;
                # other conservative review kinds retain their old behavior.
                self.assertNotIn("database_status_check_denial",
                                 [row["kind"] for row in semantic_risk_candidates(answer, [page()])])
        self.assertEqual([row["kind"] for row in semantic_risk_candidates(
            "No database status checks. [ev_source]", [page()])],
            ["database_status_check_denial"])


if __name__ == "__main__":
    unittest.main()
