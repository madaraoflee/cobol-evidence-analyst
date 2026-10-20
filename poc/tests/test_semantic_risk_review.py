"""Bounded review candidates preserve prose and use existing request limits."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from business_synthesis import assess_business_answer, semantic_risk_candidates
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. LOOKUPQ.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 SQLCODE PIC S9(9).
LINKAGE SECTION.
01 SEARCH-KEY PIC 9(8).
01 FOUND-VALUE PIC 9(8).
01 RESULT-CODE PIC XX.
PROCEDURE DIVISION USING SEARCH-KEY FOUND-VALUE RESULT-CODE.
MAIN.
EXEC SQL
SELECT STORED_VALUE INTO :FOUND-VALUE
FROM LOOKUP_ROWS WHERE ROW_KEY = :SEARCH-KEY
END-EXEC.
IF SQLCODE = 0
MOVE '47' TO RESULT-CODE
ELSE MOVE '63' TO RESULT-CODE
END-IF.
GOBACK.
"""


def page(source=SOURCE, **changes):
    return {"relative_path": "lookup.cbl", "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "source_text": source, "start_line": 1, "end_line": len(source.splitlines()),
            "evidence_id": "ev_source", "format_hint": "free", **changes}


class SemanticRiskCandidateTests(unittest.TestCase):
    def test_summary_and_table_are_candidates_not_error_verdicts(self):
        answer = ("其他情况一律返回 '63'。[ev_source]\n\n"
                  "| 场景 | 结果 |\n|---|---|\n"
                  "|传入参数非法或格式不符|响应码 '63'，程序不预先校验输入|\n\n"
                  "异常不保证返回；必须正常返回并走到 IF。")
        rows = semantic_risk_candidates(answer, [page()])
        self.assertEqual([row["kind"] for row in rows],
                         ["unconditional_exception_result", "unvalidated_input_outcome"])
        for row in rows:
            self.assertEqual(row["status"], "review_candidate")
            self.assertFalse(row["semantic_verification"])
            self.assertIn("核对", row["feedback"])
            ref, = row["source_refs"]
            self.assertEqual(ref["supplied_reference_ids"], ["ev_source"])
            self.assertEqual(ref["source_sha256"], page()["source_sha256"])
            self.assertLessEqual(ref["start_line"], 12)
            self.assertGreaterEqual(ref["end_line"], 15)
        assessment = assess_business_answer("返回状态？", answer, {}, [page()], answer_detail="brief")
        self.assertEqual(assessment["reason"], "semantic_scope_review_needed")
        self.assertEqual(assessment["status"], "incomplete")
        self.assertEqual(assessment["semantic_risk_candidates"], rows)

    def test_normal_return_local_scope_negation_and_questions_are_not_guarantees(self):
        for answer in (
            "正常返回并且控制流到达 IF 时，所有错误都返回 '63'。[ev_source]",
            "仅描述静态分支：所有错误返回 '63'。[ev_source]",
            "不能保证任何异常都返回 '63'。[ev_source]",
            "非法输入不能保证返回 '63'。[ev_source]",
            "Does every error return '63'? [ev_source]",
            "任何异常都返回 '63'？[ev_source]",
            "When control reaches the IF after a normal return, all errors return '63'. [ev_source]",
            "Cannot guarantee that all errors return '63'. [ev_source]",
            "An invalid input may return '63'. [ev_source]",
            "SQLCODE 不等于零时，返回 '63'。[ev_source]",
            "## 四、LOOKUPQ 是否任何异常都返回 '63'",
            "是否任何异常都返回 '63'，需要区分。[ev_source]",
        ):
            with self.subTest(answer=answer):
                self.assertEqual(semantic_risk_candidates(answer, [page()]), [])

    def test_english_and_zero_input_have_same_source_bound_review(self):
        for answer, kind in (
            ("All exceptions return '63'. [ev_source]", "unconditional_exception_result"),
            ("An invalid input returns '63'. [ev_source]", "unvalidated_input_outcome"),
            ("零键输入一定查询失败。[ev_source]", "unvalidated_input_outcome"),
        ):
            with self.subTest(answer=answer):
                rows = semantic_risk_candidates(answer, [page()])
                self.assertEqual([row["kind"] for row in rows], [kind])

    def test_actual_named_numeric_rejection_and_direct_return_is_not_speculation(self):
        source = SOURCE.replace("EXEC SQL", "IF SEARCH-KEY IS NOT NUMERIC\n"
            "MOVE '29' TO RESULT-CODE\nGOBACK\nEND-IF\nEXEC SQL", 1)
        original = "SEARCH-KEY 非法输入时，响应码 RESULT-CODE 为 '29'。[ev_source]"
        self.assertEqual(semantic_risk_candidates(original, [page(source)]), [])
        # A different value or unidentified return slot still needs review.
        for answer in (original.replace("'29'", "'63'"), "非法输入返回 '29'。[ev_source]"):
            self.assertEqual(len(semantic_risk_candidates(answer, [page(source)])), 1)

    def test_other_or_ambiguous_input_cannot_borrow_a_rejection_branch(self):
        source = SOURCE.replace("01 SEARCH-KEY", "01 OTHER-KEY PIC 9(8).\n01 SEARCH-KEY").replace(
            "EXEC SQL", "IF SEARCH-KEY IS NOT NUMERIC\nMOVE '29' TO RESULT-CODE\nGOBACK\nEND-IF\nEXEC SQL", 1)
        for changed, answer in (
            (source, "OTHER-KEY 非法输入的响应码 RESULT-CODE 为 '29'。[ev_source]"),
            (source, "SEARCH-KEY 或 OTHER-KEY 非法输入的响应码 RESULT-CODE 为 '29'。[ev_source]"),
            (source.replace("ROW_KEY = :SEARCH-KEY", "ROW_KEY = :SEARCH-KEY AND EXTRA_KEY = :OTHER-KEY"),
             "非法输入返回 '29'。[ev_source]"),
            (source.replace("01 OTHER-KEY PIC 9(8).", "01 SEARCH-KEY PIC 9(8)."),
             "SEARCH-KEY 非法输入的响应码 RESULT-CODE 为 '29'。[ev_source]"),
        ):
            with self.subTest(answer=answer):
                self.assertEqual(len(semantic_risk_candidates(answer, [page(changed)])), 1)

    def test_audit_move_or_continuing_path_is_not_a_return_rule(self):
        guard = "IF SEARCH-KEY IS NOT NUMERIC\nMOVE '29' TO RESULT-CODE\nGOBACK\nEND-IF\n"
        source = SOURCE.replace("EXEC SQL", guard + "EXEC SQL", 1)
        answer = "SEARCH-KEY 非法输入时，响应码 RESULT-CODE 为 '29'。[ev_source]"
        for changed in (
            source.replace("GOBACK\nEND-IF", "CONTINUE\nEND-IF"),
            source.replace("GOBACK\nEND-IF", "MOVE '63' TO RESULT-CODE\nGOBACK\nEND-IF"),
            source.replace(guard, guard.replace("GOBACK\n", "")),
            source.replace("PROCEDURE DIVISION USING SEARCH-KEY FOUND-VALUE RESULT-CODE.",
                           "PROCEDURE DIVISION USING SEARCH-KEY FOUND-VALUE."),
            source.replace(guard, "IF FOUND-VALUE = 1\n" + guard + "END-IF\n"),
            source.replace(guard, guard.replace("END-IF\n", "")),
        ):
            with self.subTest(source=changed):
                self.assertEqual(len(semantic_risk_candidates(answer, [page(changed)])), 1)
        audit = SOURCE.replace("01 SQLCODE", "01 AUDIT-COUNT PIC 99.\n01 SQLCODE").replace(
            "EXEC SQL", "IF SEARCH-KEY IS NOT NUMERIC\nMOVE '29' TO AUDIT-COUNT\nEND-IF\nEXEC SQL", 1)
        self.assertEqual(len(semantic_risk_candidates("非法输入返回 '29'。[ev_source]", [page(audit)])), 1)
        self.assertEqual(len(semantic_risk_candidates(
            "SEARCH-KEY 非法输入时，响应码 AUDIT-COUNT 为 '29'。[ev_source]", [page(audit)])), 1)

    def test_uncited_table_uses_unique_boundary_cited_by_answer_not_unrelated_source(self):
        other = page(SOURCE.replace("LOOKUPQ", "OTHERQ"), relative_path="other.cbl",
                     evidence_id="ev_other")
        answer = "查询读取参数。[ev_source]\n\n|非法输入|响应码 '63'|\n"
        rows = semantic_risk_candidates(answer, [page(), other])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["binding_basis"], "unique_answer_cited_boundary")
        self.assertEqual(rows[0]["source_refs"][0]["relative_path"], "lookup.cbl")
        self.assertEqual(semantic_risk_candidates(answer.replace("[ev_source]", ""), [page(), other]), [])

    def test_fenced_examples_and_negative_quoted_claim_are_not_positive_assertions(self):
        answer = ("```text\n任何异常都返回 '63'。[ev_source]\n```\n"
                  "不能保证“任何异常都返回 '63'”。[ev_source]")
        self.assertEqual(semantic_risk_candidates(answer, [page()]), [])

    def test_no_current_bound_source_no_candidate_and_conflicts_do_not_join(self):
        answer = "任何异常都返回 '63'。[ev_source]"
        for pages in ([], [page(span_truncated=True)], [page(end_line=99)],
                      [page(source_sha256="not-a-source-version")],
                      [page(), page(source_sha256="0" * 64)],
                      [page(evidence_id="ev_other")],
                      [page(SOURCE.replace("EXEC SQL", "DISPLAY 'EXEC SQL'").replace("END-EXEC.", ""))]):
            with self.subTest(pages=pages):
                self.assertEqual(semantic_risk_candidates(answer, pages), [])

    def test_no_risky_assertion_does_not_parse_sources_and_candidate_count_is_bounded(self):
        with patch("answer_grounding.answer_grounding_risks", side_effect=AssertionError("no risk to inspect")):
            self.assertEqual(semantic_risk_candidates("源码依条件返回结果。[ev_source]", [page()]), [])
        answer = "\n".join("所有异常返回 '63'。[ev_source]" for _ in range(100))
        self.assertLessEqual(len(semantic_risk_candidates(answer, [page()])), 12)

    def test_later_negation_does_not_cancel_a_positive_summary(self):
        rows = semantic_risk_candidates(
            "任何异常都返回 '63'，但不能保证实际执行。[ev_source]", [page()])
        self.assertEqual(len(rows), 1)


class SemanticRiskAssertionScopeTests(unittest.TestCase):
    def test_same_assertion_denial_is_not_a_guarantee(self):
        for answer in (
            "'63' 不等于“任何异常都返回”。[ev_source]",
            "'63' 只覆盖控制流回到判断处的非零路径，不代表任何异常都能返回。[ev_source]",
            "这个条件不意味着所有异常都返回 '63'。[ev_source]",
            "This does not imply that all exceptions return '63'. [ev_source]",
        ):
            with self.subTest(answer=answer):
                self.assertEqual(semantic_risk_candidates(answer, [page()]), [])

    def test_unrelated_negation_or_earlier_scope_cannot_excuse_a_later_guarantee(self):
        for answer in (
            "不是初始化结果，但所有异常都返回 '63'。[ev_source]",
            "不代表任何异常都返回，但所有错误都返回 '63'。[ev_source]",
            "正常返回后按状态判断，但任何异常都会返回 '63'。[ev_source]",
            "所有异常都返回 '63'，但实际执行需要正常返回。[ev_source]",
        ):
            with self.subTest(answer=answer):
                rows = semantic_risk_candidates(answer, [page()])
                self.assertEqual([row["kind"] for row in rows], ["unconditional_exception_result"])

    def test_generic_return_wording_and_table_outcome_column_need_review(self):
        for answer in (
            "查询成功给 '47'，其余情况统一给 '63'。[ev_source]",
            "查询成功回47，其餘情況統一給63。[ev_source]",
            "| 输入 | 返回码 |\n|---|---|\n|传入00000000无效占位账号|63|[ev_source]",
            "| Input | Response code |\n|---|---|\n|Invalid input|63|[ev_source]",
        ):
            with self.subTest(answer=answer):
                self.assertEqual(len(semantic_risk_candidates(answer, [page()])), 1)
        for answer in (
            "| 输入 | 库存数量 |\n|---|---|\n|非法输入|63|[ev_source]",
            "| 输入 | 返回码 |\n|---|---|\n|非法输入|无法确定|[ev_source]",
            "| 输入 | 返回码 |\n|---|---|\n|正常输入|47|\n\n|非法输入|63|[ev_source]",
            "正常返回并到达 IF 判断时，其他情况统一给63。[ev_source]",
        ):
            with self.subTest(answer=answer):
                self.assertEqual(semantic_risk_candidates(answer, [page()]), [])

    def cl_page(self, **changes):
        source = "PGM\nCALL PGM(PROCESSX)\nDLTOVR FILE(INPUTDATA)\nENDPGM\n"
        return page(source, relative_path="job.clle", evidence_id="ev_job", **changes)

    def test_post_call_guarantee_binds_actual_cl_call_and_following_command(self):
        answer = "即使 PROCESSX 内部出问题，CL 层也不会因此改变流程。[ev_job]"
        rows = semantic_risk_candidates(answer, [self.cl_page(), page()])
        self.assertEqual([row["kind"] for row in rows], ["unconditional_post_call_continuation"])
        self.assertFalse(rows[0]["semantic_verification"])
        self.assertIn("控制流正常返回", rows[0]["feedback"])
        self.assertEqual(rows[0]["source_refs"], [{"relative_path": "job.clle",
            "source_sha256": self.cl_page()["source_sha256"], "start_line": 2, "end_line": 3,
            "supplied_reference_ids": ["ev_job"]}])
        for text in (
            "即使 PROCESSX 内部失败，只有正常返回并回到调用方时，才会继续执行清理。[ev_job]",
            "PROCESSX 正常返回后，CL 执行后续 DLTOVR。[ev_job]",
            "不能保证即使 PROCESSX 内部失败仍无条件继续。[ev_job]",
        ):
            with self.subTest(answer=text):
                self.assertEqual(semantic_risk_candidates(text, [self.cl_page()]), [])

    def test_post_call_risk_cannot_borrow_unrelated_or_partial_source(self):
        answer = "即使 PROCESSX 内部出问题，CL 层也不会因此改变流程。[ev_job]"
        for pages in ([page()], [self.cl_page(span_truncated=True)],
                      [self.cl_page(), self.cl_page(source_sha256="0" * 64)],
                      [self.cl_page(source_text="PGM\nCALL PGM(PROCESSX)\n", end_line=2)],
                      [self.cl_page(source_text="PGM\nCALL PGM(OTHERX)\nDLTOVR FILE(INPUTDATA)\nENDPGM\n")]):
            with self.subTest(pages=pages):
                self.assertEqual(semantic_risk_candidates(answer, pages), [])
        self.assertEqual(semantic_risk_candidates(answer.replace("ev_job", "ev_source"),
                                                  [self.cl_page(), page()]), [])

    def test_frozen_phase2_raw_answers_keep_exact_text_and_risk_scope(self):
        from tools.business_onboarding_fixture import sources
        frozen = json.loads((Path(__file__).parent / "fixtures" / "semantic_boundary_phase2_answers.json").read_text())
        contents = sources()
        self.assertEqual({name: hashlib.sha256(text.encode()).hexdigest() for name, text in contents.items()},
                         frozen["source_sha256"])
        expected = {"batch-round-1": ["unconditional_post_call_continuation"],
                    "online-round-1": ["unconditional_exception_result", "unvalidated_input_outcome"],
                    "boundary-followup-round-1": [], "boundary-followup-round-2": []}
        for case in frozen["cases"]:
            with self.subTest(case=case["id"]):
                original = case["raw_answer"]
                self.assertEqual(hashlib.sha256(original.encode()).hexdigest(), case["raw_answer_sha256"])
                pages = []
                for ref in case["source_refs"]:
                    text = "\n".join(contents[ref["relative_path"]].splitlines()[ref["start_line"]-1:ref["end_line"]])
                    self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), ref["excerpt_sha256"])
                    pages.append({**ref, "source_text": text})
                rows = semantic_risk_candidates(original, pages)
                self.assertEqual([row["kind"] for row in rows], expected[case["id"]])
                self.assertEqual(hashlib.sha256(original.encode()).hexdigest(), case["raw_answer_sha256"])
                self.assertTrue(all(row["status"] == "review_candidate" and not row["semantic_verification"] for row in rows))


class StructuredSemanticRiskTests(unittest.TestCase):
    LOOP_SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. SCANQ.
ENVIRONMENT DIVISION.
INPUT-OUTPUT SECTION.
FILE-CONTROL.
SELECT RAW-IN ASSIGN TO RAWDATA.
DATA DIVISION.
FILE SECTION.
FD RAW-IN.
01 RAW-ROW PIC X(20).
WORKING-STORAGE SECTION.
01 DONE-SW PIC 9 VALUE 2.
PROCEDURE DIVISION.
MAIN.
OPEN INPUT RAW-IN.
PERFORM UNTIL DONE-SW = 9
READ RAW-IN
AT END MOVE 9 TO DONE-SW
NOT AT END DISPLAY 'FOUND'
END-READ
END-PERFORM.
CLOSE RAW-IN.
GOBACK.
"""

    def test_empty_file_loop_claim_requires_actual_read_inside_complete_loop(self):
        answer = "空输入且第一条读到文件尾时，循环体一次也不进。[ev_source]"
        rows = semantic_risk_candidates(answer, [page(self.LOOP_SOURCE)])
        self.assertEqual([row["kind"] for row in rows], ["empty_input_zero_loop"])
        self.assertEqual(rows[0]["source_refs"][0]["start_line"], 12)
        self.assertEqual(rows[0]["source_refs"][0]["end_line"], 21)
        self.assertIn("只要已到达该 READ/AT END", rows[0]["feedback"])
        self.assertIn("不把 VALUE 当每次调用重置", rows[0]["feedback"])
        self.assertFalse(rows[0]["semantic_verification"])
        for changed in (
            self.LOOP_SOURCE.replace("VALUE 2", "VALUE 9"),
            self.LOOP_SOURCE.replace("PERFORM UNTIL", "MOVE 9 TO DONE-SW.\nPERFORM UNTIL"),
            self.LOOP_SOURCE.replace("MAIN.\n", "PREPARE.\nMOVE 9 TO DONE-SW.\nMAIN.\n"),
            self.LOOP_SOURCE.replace("PERFORM UNTIL", "IF RAW-ROW = SPACES\nPERFORM UNTIL")
                            .replace("END-PERFORM.", "END-PERFORM\nEND-IF."),
            self.LOOP_SOURCE.replace("DONE-SW = 9", "DONE-SW > 1"),
            self.LOOP_SOURCE.replace("END-READ\n", ""),
            self.LOOP_SOURCE.replace("END-PERFORM.", ""),
            self.LOOP_SOURCE.replace("PERFORM UNTIL DONE-SW = 9\n", "")
                            .replace("END-READ\n", "END-READ.\nPERFORM UNTIL DONE-SW = 9\nCONTINUE\n"),
        ):
            with self.subTest(source=changed):
                self.assertEqual(semantic_risk_candidates(answer, [page(changed)]), [])

    def test_empty_file_business_branch_and_uncertain_loop_are_not_zero_loop_guarantees(self):
        for answer in (
            "空输入时，NOT AT END 业务分支一次也不进。[ev_source]",
            "空输入时，循环体内的 NOT AT END 业务处理不执行。[ev_source]",
            "空输入先进入循环 READ，再执行 AT END，不进入业务分支。[ev_source]",
            "空输入不意味着循环体一次也不进。[ev_source]",
            "空输入可能导致循环体一次也不进，需核对保留状态。[ev_source]",
            "是否空输入时循环体一次也不进？[ev_source]",
            "```text\n空输入时循环体一次也不进。[ev_source]\n```",
        ):
            with self.subTest(answer=answer):
                self.assertEqual(semantic_risk_candidates(answer, [page(self.LOOP_SOURCE)]), [])
        for pages in (
            [page(self.LOOP_SOURCE, span_truncated=True)],
            [page(self.LOOP_SOURCE, end_line=99)],
            [page(self.LOOP_SOURCE, start_line=0, end_line=22)],
            [page(self.LOOP_SOURCE, source_sha256="invalid")],
            [page(self.LOOP_SOURCE), page(self.LOOP_SOURCE, source_sha256="0" * 64)],
            [page(self.LOOP_SOURCE, include_chain=[{"copy": "LOCAL"}])],
        ):
            with self.subTest(pages=pages):
                self.assertEqual(semantic_risk_candidates("空输入时循环体一次也不进。[ev_source]", pages), [])

    def test_named_run_state_is_not_replaced_with_the_declaration_initial_value(self):
        for answer in (
            "若调用前 DONE-SW 已是 9，空输入时循环体一次也不进。[ev_source]",
            "空输入且 DONE-SW 已经等于 9 时，循环体一次也不进。[ev_source]",
            "If DONE-SW is already 9, empty input has zero loop iterations. [ev_source]",
        ):
            with self.subTest(answer=answer):
                self.assertEqual(semantic_risk_candidates(answer, [page(self.LOOP_SOURCE)]), [])
        # A first-read precondition does not imply a pre-set exit flag.
        rows = semantic_risk_candidates(
            "若空输入且第一条 READ 到文件尾，循环体一次也不进。[ev_source]",
            [page(self.LOOP_SOURCE)])
        self.assertEqual([row["kind"] for row in rows], ["empty_input_zero_loop"])

    def test_same_target_in_sql_both_arms_requires_review_of_success_only_assignment(self):
        answer = "RESULT-CODE 只在查询成功后才被赋值。[ev_source]"
        rows = semantic_risk_candidates(answer, [page()])
        self.assertEqual([row["kind"] for row in rows], ["success_only_status_assignment"])
        self.assertEqual(rows[0]["source_refs"][0]["start_line"], 12)
        self.assertEqual(rows[0]["source_refs"][0]["end_line"], 19)
        self.assertFalse(rows[0]["semantic_verification"])
        spaced = SOURCE.replace("MOVE '47'", "*> Actual comment\n\nMOVE '47'")
        spaced_rows = semantic_risk_candidates(answer, [page(spaced)])
        self.assertEqual(spaced_rows[0]["source_refs"][0]["end_line"], 21)
        for changed in (
            SOURCE.replace("ELSE MOVE '63' TO RESULT-CODE\n", ""),
            SOURCE.replace("ELSE MOVE '63' TO RESULT-CODE", "ELSE MOVE '63' TO FOUND-VALUE"),
            SOURCE.replace("END-IF.", ""),
            SOURCE.replace("END-EXEC.", ""),
        ):
            with self.subTest(source=changed):
                self.assertEqual(semantic_risk_candidates(answer, [page(changed)]), [])
        for text in (
            "FOUND-VALUE 只在查询成功后才被赋值。[ev_source]",
            "不能说 RESULT-CODE 只在查询成功后才被赋值。[ev_source]",
            "RESULT-CODE 在成功和非成功两支均赋值；仅查询数据在成功时取得。[ev_source]",
            "RESULT-CODE 是否只在查询成功后才被赋值？[ev_source]",
        ):
            with self.subTest(answer=text):
                self.assertEqual(semantic_risk_candidates(text, [page()]), [])

    def test_status_role_requires_unique_explicit_parameter_mapping_and_current_source(self):
        table = "| 参数 | 含义 |\n|---|---|\n| RESULT-CODE | 响应码 |\n"
        claim = "数值和响应码都只在查出结果后才被赋值。[ev_source]"
        rows = semantic_risk_candidates(table + claim, [page()])
        self.assertEqual([row["kind"] for row in rows], ["success_only_status_assignment"])
        for answer in (claim, table.replace("RESULT-CODE", "OTHER-CODE") + claim,
                       table + "| OTHER-CODE | 响应码 |\n" + claim,
                       table + claim.replace("ev_source", "ev_missing")):
            with self.subTest(answer=answer):
                self.assertEqual(semantic_risk_candidates(answer, [page()]), [])
        for pages in ([page(span_truncated=True)], [page(), page(source_sha256="0" * 64)],
                      [page(source_text=SOURCE[:-20])]):
            with self.subTest(pages=pages):
                self.assertEqual(semantic_risk_candidates(table + claim, pages), [])

    def test_earlier_status_clause_cannot_lend_target_to_a_query_data_claim(self):
        table = "| 参数 | 含义 |\n|---|---|\n| RESULT-CODE | 响应码 |\n"
        for answer in (
            "RESULT-CODE 两个分支都赋值，FOUND-VALUE 只在查询成功后才被赋值。[ev_source]",
            "响应码 RESULT-CODE 依状态设置，但 FOUND-VALUE 只在查询成功后才被赋值。[ev_source]",
            table + "响应码依状态设置，查询数据只在查出结果后才被赋值。[ev_source]",
        ):
            with self.subTest(answer=answer):
                self.assertEqual(semantic_risk_candidates(answer, [page()]), [])
        rows = semantic_risk_candidates(
            table + "查询正常返回后，数值和响应码都只在查出结果后才被赋值。[ev_source]", [page()])
        self.assertEqual([row["kind"] for row in rows], ["success_only_status_assignment"])

    def test_value_limited_assignment_is_not_whole_target_write_exclusivity(self):
        for answer in (
            "RESULT-CODE 只在查询成功后才被赋值为 '47'。[ev_source]",
            "RESULT-CODE 只在查询成功后才设置为 '47'。[ev_source]",
            "RESULT-CODE 只在查询成功后才被赋值 '47'。[ev_source]",
            "RESULT-CODE is only on query success assigned to '47'. [ev_source]",
            # Value claims remain outside this risk category even when wrong;
            # skipping this candidate does not validate a literal or formula.
            "RESULT-CODE 只在查询成功后才被赋值为 '88'。[ev_source]",
            "不能说 RESULT-CODE 只在查询成功后才被赋值。[ev_source]",
        ):
            with self.subTest(answer=answer):
                self.assertEqual(semantic_risk_candidates(answer, [page()]), [])
        for answer in (
            "RESULT-CODE 只在查询成功后才被赋值。[ev_source]",
            "正常返回后，RESULT-CODE 只在查询成功后才被赋值。[ev_source]",
            "RESULT-CODE 只在查询成功后才被赋值，但仅指正常返回路径。[ev_source]",
            "RESULT-CODE is only on query success assigned. [ev_source]",
        ):
            with self.subTest(answer=answer):
                rows = semantic_risk_candidates(answer, [page()])
                self.assertEqual([row["kind"] for row in rows], ["success_only_status_assignment"])

    def test_frozen_phase3_raw_answers_bind_both_new_structures_without_rewriting(self):
        from tools.business_onboarding_fixture import sources
        frozen = json.loads((Path(__file__).parent / "fixtures" / "semantic_boundary_phase3_answers.json").read_text())
        contents = sources()
        self.assertEqual({name: hashlib.sha256(text.encode()).hexdigest() for name, text in contents.items()},
                         frozen["source_sha256"])
        expected = {"batch-round-1": "empty_input_zero_loop",
                    "online-round-1": "success_only_status_assignment",
                    "boundary-followup-round-1": "empty_input_zero_loop"}
        for case in frozen["cases"]:
            with self.subTest(case=case["id"]):
                original = case["raw_answer"]
                self.assertEqual(hashlib.sha256(original.encode()).hexdigest(), case["raw_answer_sha256"])
                pages = []
                for ref in case["source_refs"]:
                    text = "\n".join(contents[ref["relative_path"]].splitlines()[ref["start_line"]-1:ref["end_line"]])
                    self.assertEqual(hashlib.sha256(text.encode()).hexdigest(), ref["excerpt_sha256"])
                    pages.append({**ref, "source_text": text})
                rows = semantic_risk_candidates(original, pages)
                self.assertEqual([row["kind"] for row in rows], [expected[case["id"]]])
                self.assertFalse(rows[0]["semantic_verification"])
                self.assertEqual(hashlib.sha256(original.encode()).hexdigest(), case["raw_answer_sha256"])
                assessment = assess_business_answer(case["question"], original, {}, pages, answer_detail="brief")
                self.assertEqual(assessment["reason"], "semantic_scope_review_needed")


class SemanticRiskPipelineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.source = root / "source"
        self.source.mkdir()
        (self.source / "lookup.cbl").write_bytes(SOURCE.encode("utf-8"))
        self.database = root / "index.sqlite"
        build_business_index(self.source, self.database, source_format="free",
                             framework_reference_path="", quiet=True)
        ensure_repository_search(self.database, self.source)
        self.requests = []
        self.responses = []
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline", api_key="unused")

    def ask(self, callback, policy):
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.requests.append(payload)
            pages = [page for context in payload["source_context"] for page in context.get("pages", [])]
            identifier = next(page["evidence_id"] for page in pages if "END-EXEC" in page["source_text"])
            text = callback(len(self.requests), identifier)
            self.responses.append(text)
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": text}, "finish_reason": "stop"}]}, ensure_ascii=False))
        return run_business_chat("LOOKUPQ 的查询怎样返回结果码？", self.database, self.source,
            self.config, transport=transport, policy=policy, answer_detail="brief",
            framework_reference_path="")["agent_result"]

    @staticmethod
    def bad(identifier):
        return f"任何异常都一定返回 '63'。[{identifier}]"

    @staticmethod
    def good(identifier):
        return ("查询正常返回并且控制流到达 IF 时，SQLCODE 为零则设置 RESULT-CODE 为 '47'，"
                f"否则为 '63'。未到达判断的异常及非法输入的查询结果不能据此保证。[{identifier}]")

    def test_concrete_risk_feedback_requests_one_bounded_model_revision(self):
        result = self.ask(lambda turn, ref: self.bad(ref) if turn == 1 else self.good(ref),
                          AgentPolicy(max_model_requests=2, max_answer_revisions=1))
        self.assertEqual(len(self.requests), 2)
        review = self.requests[1]["answer_review"]
        self.assertEqual(review["semantic_risk_candidates"][0]["status"], "review_candidate")
        self.assertIn("不是已判定错误", self.requests[1]["task"])
        self.assertEqual(result["answer"], self.responses[1])
        self.assertFalse(result["answer_completion"]["semantic_risk_candidates"])
        self.assertFalse(result["claims_semantically_verified"])
        self.assertEqual(result["status"], "ANALYZED")

    def test_explicit_zero_revisions_preserves_answer_and_marks_risk(self):
        result = self.ask(lambda turn, ref: self.bad(ref),
                          AgentPolicy(max_model_requests=6, max_answer_revisions=0))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["answer"], self.responses[0])
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["answer_completion"]["reason"], "semantic_scope_review_needed")
        boundary = next(row for row in result["boundaries"] if row["reason"] == "semantic_scope_review_needed")
        self.assertEqual(boundary["revision_limit"], 0)
        self.assertFalse(boundary["semantic_verification"])
        self.assertTrue(result["evidence_refs"])

    def test_provider_request_cap_still_wins_over_revision_allowance(self):
        result = self.ask(lambda turn, ref: self.bad(ref), AgentPolicy(max_model_requests=1))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["answer"], self.responses[0])

    def test_unchanged_risk_stops_without_using_all_default_requests(self):
        self.assertEqual(AgentPolicy().answer_revision_limit, 2)
        result = self.ask(lambda turn, ref: self.bad(ref), AgentPolicy())
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertTrue(result["answer_completion"]["semantic_risk_candidates"])


if __name__ == "__main__":
    unittest.main()
