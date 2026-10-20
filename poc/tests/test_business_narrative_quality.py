"""Offline checks of source-bound explanation gaps, not model-quality scores."""

import hashlib
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_orientation import build_business_orientation
from business_synthesis import (answer_requirements, assess_business_answer, build_analysis_brief,
    needs_synthesis_review, project_analysis_brief)
from answer_markdown import BUSINESS_ANSWER_POLICY, BUSINESS_RUNTIME_BOUNDARY_POLICY
from claim_ledger import review_answer_claims


QUESTION = "请详细解释这项业务的目的、整体流程、程序分工和输入输出文件的作用，让新人也能看懂。"
SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. INVOICE-BATCH.
*> Purpose: prepare invoice records for eligible requests.
ENVIRONMENT DIVISION.
INPUT-OUTPUT SECTION.
FILE-CONTROL.
SELECT REQUEST-FILE ASSIGN TO REQUESTS.
SELECT BILL-FILE ASSIGN TO BILLS.
DATA DIVISION.
FILE SECTION.
FD REQUEST-FILE.
01 REQUEST-REC.
  05 ELIGIBLE-FLAG PIC X.
  05 BASE-AMOUNT PIC 9(5)V99.
FD BILL-FILE.
01 BILL-REC.
  05 BILL-AMOUNT PIC 9(5)V99.
WORKING-STORAGE SECTION.
01 RESULT-STATE PIC X.
PROCEDURE DIVISION.
MAIN.
READ REQUEST-FILE.
IF ELIGIBLE-FLAG = 'Y'
    CALL "FEE-RULE" USING BASE-AMOUNT BILL-AMOUNT
    WRITE BILL-REC
ELSE
    MOVE 'R' TO RESULT-STATE
END-IF.
GOBACK.
"""
JOB = """PGM
SBMJOB CMD(CALL PGM(INVOICE-BATCH)) JOB(INVOICE-RUN)
ENDPGM
"""
BUSINESS_ANSWER = (
    "入口注释说明，这项处理把符合条件的请求整理为待出账记录。[ev_entry]\n\n"
    "提交脚本把处理交给 INVOICE-RUN 批作业，由 INVOICE-BATCH 承担入口处理；"
    "这说明存在提交批作业的指令，尚不能确认每天何时由谁发起。[ev_job]\n\n"
    "入口先从 REQUEST-FILE 读入一条待处理请求。审核标志为 Y 时，"
    "把基础金额和账单金额交给 FEE-RULE，再请求向 BILL-FILE 写出账单记录；"
    "不满足条件时标记拒绝状态 R，因此不会进入这条计算及写出分支。[ev_entry]\n\n"
    "请求文件提供本次审核标志与金额，账单文件承接计算分支的输出。"
    "计算程序的实现没有随本段材料提供，因此费率和返回的账单金额仍未知；"
    "这一缺口不影响上述调用条件和输出请求的说明，也不能据此确认生产数据已写入。[ev_entry]"
)
TECHNICAL_INVENTORY = (
    "## 程序清单\n\n"
    "- `SBMJOB CMD(CALL PGM(INVOICE-BATCH)) JOB(INVOICE-RUN)`。[ev_job]\n"
    "- `READ REQUEST-FILE`。[ev_entry]\n"
    "- `IF ELIGIBLE-FLAG = 'Y' CALL \"FEE-RULE\" USING BASE-AMOUNT BILL-AMOUNT`。[ev_entry]\n"
    "- `WRITE BILL-REC`；`MOVE 'R' TO RESULT-STATE`。[ev_entry]"
)


def page(text=SOURCE, *, identifier="ev_entry", path="entry.cbl"):
    return {"relative_path": path, "evidence_id": identifier, "source_text": text,
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "start_line": 1, "end_line": len(text.splitlines()), "format_hint": "free"}


def investigation(pages):
    return {"state": "located", "analysis_plan": {"intents": ["flow"], "paths": ["entry.cbl"]},
        "required_items": [{"kind": "business_steps", "status": "SATISFIED",
            "candidate_count": 1, "evidence_ids": [source["evidence_id"] for source in pages]}]}


class BusinessNarrativeQualityTests(unittest.TestCase):
    def setUp(self):
        self.pages = [page(), page(JOB, identifier="ev_job", path="submit.cl")]
        self.investigation = investigation(self.pages)

    def assess(self, answer, *, question=QUESTION, pages=None, detail="detailed"):
        pages = self.pages if pages is None else pages
        return assess_business_answer(question, answer, investigation(pages), pages, answer_detail=detail)

    def test_real_source_inventory_nominates_specific_missing_explanations(self):
        orientation = build_business_orientation(QUESTION, self.investigation, self.pages)
        self.assertIn("business_purpose", orientation["available_facets"])
        self.assertIn("program_roles", orientation["available_facets"])
        self.assertIn("data_roles", orientation["available_facets"])
        self.assertTrue(any(row.get("command") == "SBMJOB" for row in orientation["entries"]))
        self.assertTrue(any(row.get("target_name") == "FEE-RULE" for row in orientation["relationships"]))
        assessment = self.assess(TECHNICAL_INVENTORY)
        self.assertEqual(assessment["status"], "incomplete")
        self.assertEqual(set(assessment["business_explanation_review"]["missing_aspects"]),
                         {"business_purpose", "program_roles", "data_roles"})
        self.assertFalse(assessment["business_explanation_review"]["semantic_verification"])
        self.assertTrue(needs_synthesis_review(QUESTION, TECHNICAL_INVENTORY, self.investigation,
                                             source_pages=self.pages, assessment=assessment))
        self.assertIn("CALL", TECHNICAL_INVENTORY)  # The review never replaces the received answer.

    def test_explained_purpose_program_roles_data_and_specific_gap_are_retained(self):
        assessment = self.assess(BUSINESS_ANSWER)
        self.assertNotEqual(assessment["status"], "incomplete")
        self.assertEqual(assessment["missing_aspects"], [])
        self.assertFalse(assessment["business_explanation_review"]["technical_inventory_only"])
        self.assertFalse(assessment["business_explanation_review"]["semantic_verification"])
        self.assertFalse(needs_synthesis_review(QUESTION, BUSINESS_ANSWER, self.investigation,
                                              source_pages=self.pages, assessment=assessment))

    def test_natural_code_inventory_and_generic_boilerplate_are_not_business_explanations(self):
        for answer in (
                "The program calls FEE-RULE, reads REQUEST-FILE and writes BILL-REC. [ev_entry]",
                "调用 FEE-RULE，把 Y 赋值给 RESULT-STATE，读取 REQUEST-FILE。[ev_entry]",
                "程序先校验输入，然后按规则处理数据，最后更新结果。[ev_entry]",
                "| 程序 | 语句 |\n|---|---|\n| INVOICE-BATCH | CALL FEE-RULE |"):
            with self.subTest(answer=answer):
                review = self.assess(answer)["business_explanation_review"]
                self.assertEqual(set(review["missing_aspects"]),
                                 {"business_purpose", "program_roles", "data_roles"})

    def test_unfamiliar_business_terms_and_short_useful_explanations_are_not_rejected(self):
        for answer in (
                "这一步形成应收凭据，尚未过账。[ev_entry]",
                "Eligible requests become bill records; rejected requests bypass fee calculation. [ev_entry]",
                "请求通过审核后进入出账处理；否则只留下拒绝状态，不进入金额计算分支。[ev_entry]"):
            with self.subTest(answer=answer):
                review = self.assess(answer)["business_explanation_review"]
                self.assertEqual(review["missing_aspects"], [])
                self.assertFalse(review["semantic_verification"])

    def test_specific_questions_and_explicit_brevity_do_not_require_an_overview(self):
        for question in ("简短解释业务流程。", "它调用哪些相关程序？", "FEE-RULE 的输入参数是什么？",
                         "BILL-AMOUNT 的公式是什么？", "Explain the fee formula.",
                         "Which programs call it?"):
            with self.subTest(question=question):
                assessment = self.assess(TECHNICAL_INVENTORY, question=question)
                self.assertNotIn("business_explanation_review", assessment)
                self.assertFalse(set(assessment["required_aspects"]) &
                                 {"business_purpose", "program_roles", "data_roles"})
        self.assertNotIn("business_explanation_review", self.assess(TECHNICAL_INVENTORY, detail="brief"))

    def test_direct_questions_never_run_orientation_projection(self):
        with mock.patch("business_orientation.build_business_orientation",
                        side_effect=AssertionError("A narrow question must not scan orientation")):
            for question in ("它调用哪些相关程序？", "BILL-AMOUNT 的公式是什么？", "简短解释业务流程。"):
                with self.subTest(question=question):
                    assessment = self.assess(TECHNICAL_INVENTORY, question=question)
                    self.assertNotIn("business_explanation_review", assessment)
                    brief = build_analysis_brief(question, self.investigation, self.pages, [], 2048)
                    self.assertNotIn("business_orientation", brief)
                    self.assertEqual(brief["business_explanation_contract"]["mode"], "direct_answer")
            brief = build_analysis_brief(QUESTION, self.investigation, self.pages, [], 2048, answer_detail="brief")
            self.assertNotIn("business_orientation", brief)

    def test_overview_that_also_requests_a_formula_keeps_its_explicit_business_scope(self):
        assessment = self.assess(TECHNICAL_INVENTORY,
            question="请解释业务目的和整体流程，也包括金额计算公式。")
        self.assertIn("business_explanation_review", assessment)
        self.assertIn("business_purpose", assessment["missing_aspects"])

    def test_business_use_and_newcomer_function_questions_request_business_explanation(self):
        for question in ("我是新人，请解释 LOOKUP-RULE 这个在线查询功能的业务用途、输入输出和 tables，哪些条件会返回失败？",
                         "我是新手，想了解这个功能。", "这项功能的业务用途是什么？",
                         "這項功能的業務用途是甚麼？"):
            with self.subTest(question=question):
                assessment = self.assess(TECHNICAL_INVENTORY, question=question)
                self.assertIn("business_explanation_review", assessment)
                self.assertIn("business_purpose", assessment["missing_aspects"])

    def test_no_purpose_is_inferred_from_program_name_or_ordinary_comments(self):
        for comment in ("", "*> TODO: update this section.", "*> neutral padding line 12"):
            with self.subTest(comment=comment):
                source = SOURCE.replace("*> Purpose: prepare invoice records for eligible requests.", comment)
                pages = [page(source)]
                requirements = answer_requirements(QUESTION, investigation(pages), pages)
                self.assertNotIn("business_purpose", {row["kind"] for row in requirements})
                self.assertIn("program_roles", {row["kind"] for row in requirements})
                self.assertIn("data_roles", {row["kind"] for row in requirements})
        header = page("IDENTIFICATION DIVISION.\nPROGRAM-ID. INVOICE-BATCH.\nPROCEDURE DIVISION.")
        requirements = answer_requirements(QUESTION, investigation([header]), [header])
        self.assertFalse({row["kind"] for row in requirements} &
                         {"business_purpose", "program_roles", "data_roles"})

    def test_unavailable_trimmed_or_conflicting_source_does_not_create_overview_obligations(self):
        for pages in ([], [{**page(), "span_truncated": True}],
                      [page(), {**page(SOURCE.replace("REQUESTS", "OTHER-REQUESTS")), "evidence_id": "ev_other"}]):
            with self.subTest(pages=len(pages)):
                assessment = self.assess(TECHNICAL_INVENTORY, pages=pages)
                self.assertFalse(set(assessment["required_aspects"]) &
                                 {"business_purpose", "program_roles", "data_roles"})
        for state in ("unresolved", "ambiguous"):
            with self.subTest(state=state):
                assessment = assess_business_answer(QUESTION, TECHNICAL_INVENTORY,
                    {**self.investigation, "state": state}, self.pages)
                self.assertFalse(set(assessment["required_aspects"]) &
                                 {"business_purpose", "program_roles", "data_roles"})

    def test_brief_and_wire_projection_keep_roles_bound_to_actual_supplied_material(self):
        brief = build_analysis_brief(QUESTION, self.investigation, self.pages, [], 2048)
        self.assertEqual(brief["business_explanation_contract"]["mode"], "business_overview")
        self.assertFalse(brief["business_explanation_contract"]["semantic_verification"])
        self.assertEqual(project_analysis_brief(brief)["business_orientation"], brief["business_orientation"])
        self.assertEqual(project_analysis_brief(brief)["business_explanation_contract"],
                         brief["business_explanation_contract"])
        trimmed = build_analysis_brief(QUESTION, self.investigation, [self.pages[1]], [], 2048)
        self.assertNotIn("business_purpose", trimmed["business_orientation"]["available_facets"])
        self.assertNotIn("data_roles", trimmed["business_orientation"]["available_facets"])
        self.assertFalse(any(row["kind"] == "business_purpose" for row in trimmed["required_answer_aspects"]))
        direct = build_analysis_brief("它调用哪些程序？", self.investigation, self.pages, [], 2048)
        self.assertEqual(direct["business_explanation_contract"]["mode"], "direct_answer")

    def test_query_input_alone_cannot_establish_a_missing_row(self):
        import sqlite3
        from contextlib import closing

        # An independent relational counterexample, not an IBM i execution
        # oracle: exactly the same unvalidated zero key may find a stored row.
        with closing(sqlite3.connect(":memory:")) as database:
            database.execute("CREATE TABLE CUSTOMER_TOTALS (CUSTOMER_ID INTEGER, TOTAL INTEGER)")
            self.assertEqual(database.execute(
                "SELECT TOTAL FROM CUSTOMER_TOTALS WHERE CUSTOMER_ID = ?", (0,)).fetchall(), [])
            database.execute("INSERT INTO CUSTOMER_TOTALS VALUES (0, 12)")
            self.assertEqual(database.execute(
                "SELECT TOTAL FROM CUSTOMER_TOTALS WHERE CUSTOMER_ID = ?", (0,)).fetchall(), [(12,)])
        source = """IDENTIFICATION DIVISION.
PROGRAM-ID. TOTAL-LOOKUP.
*> Purpose: query a customer's total.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 SQLCODE PIC S9(9).
LINKAGE SECTION.
01 CUSTOMER-KEY PIC 9(8).
01 FOUND-TOTAL PIC 9(5).
PROCEDURE DIVISION USING CUSTOMER-KEY FOUND-TOTAL.
EXEC SQL
SELECT TOTAL INTO :FOUND-TOTAL
FROM CUSTOMER_TOTALS WHERE CUSTOMER_ID = :CUSTOMER-KEY
END-EXEC.
GOBACK.
"""
        pages = [page(source)]
        question = "请解释查询功能的业务用途、输入输出与失败条件。"
        brief = build_analysis_brief(question, investigation(pages), pages, [], 2048)
        self.assertIn(BUSINESS_RUNTIME_BOUNDARY_POLICY, brief["business_explanation_contract"]["task"])
        self.assertIn(BUSINESS_RUNTIME_BOUNDARY_POLICY, BUSINESS_ANSWER_POLICY)
        self.assertTrue(any(row.get("table_name") == "CUSTOMER_TOTALS"
                            for row in brief["business_orientation"]["data_roles"]))
        for answer in ("输入没有校验，所以全零账号必然查不到。[ev_entry]",
                       "若表中没有该账号的行，则本查询没有匹配行；全零账号是否存在仍取决于实际数据。[ev_entry]"):
            reviewed, ledger = review_answer_claims(answer, pages, {"ev_entry": pages[0]}, source_format="free")
            self.assertEqual(reviewed, answer)
            self.assertFalse(ledger.to_dict()["all_claims_supported"])
            self.assertFalse(ledger.to_dict()["prose_semantics_verified"])

    def test_unchecked_call_does_not_certify_error_absence_or_normal_return(self):
        pages = [page("PGM\nCALL PGM(WORKER)\nDLTOVR FILE(INPUTS)\nENDPGM",
                      path="launcher.cl", identifier="ev_job")]
        question = "请解释批处理作业的业务用途和异常处理。"
        brief = build_analysis_brief(question, investigation(pages), pages, [], 2048)
        self.assertTrue(any(row.get("command") == "CALL"
                            for row in brief["business_orientation"]["relationships"]))
        self.assertIn(BUSINESS_RUNTIME_BOUNDARY_POLICY, brief["business_explanation_contract"]["task"])
        for answer in ("调用后没有状态检查，因此作业不会报错。[ev_job]",
                       "源码没有显式检查返回状态；调用正常返回后才会继续撤销覆盖，不能据此排除异常上抛。[ev_job]"):
            reviewed, ledger = review_answer_claims(answer, pages, {"ev_job": pages[0]}, source_format="free")
            self.assertEqual(reviewed, answer)
            self.assertFalse(ledger.to_dict()["all_claims_supported"])
            self.assertFalse(ledger.to_dict()["prose_semantics_verified"])
        direct = build_analysis_brief("它调用哪个程序？", investigation(pages), pages, [], 2048)
        self.assertNotIn(BUSINESS_RUNTIME_BOUNDARY_POLICY, direct["business_explanation_contract"]["task"])


if __name__ == "__main__":
    unittest.main()
