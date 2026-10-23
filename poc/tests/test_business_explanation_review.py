import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "poc"))
sys.path.insert(0, str(ROOT / "tools"))

from business_explanation_review import review_business_explanation
from business_onboarding_fixture import BATCH_QUESTION, ONLINE_QUESTION, sources
from business_orientation import build_business_orientation
from business_synthesis import assess_business_answer, wants_business_overview


def orientation(kind="batch"):
    selected = {path: text for path, text in sources().items()
                if (path == "balance.cbl") == (kind == "online")}
    pages = [{"relative_path": path, "source_text": text, "start_line": 1,
              "end_line": len(text.splitlines()), "evidence_id": "ev-" + path,
              "source_sha256": hashlib.sha256(text.encode()).hexdigest(), "format_hint": "free"}
             for path, text in selected.items()]
    question = ONLINE_QUESTION if kind == "online" else BATCH_QUESTION
    return build_business_orientation(question, {"state": "located"}, pages)


class BusinessExplanationReviewTests(unittest.TestCase):
    def test_purpose_sentence_does_not_cover_supplied_program_or_data_handoffs(self):
        for kind, answer in (("batch", "SETTLE 整理符合受理要求的订单。"),
                             ("online", "BALANCEQ 帮操作人员查看账户余额。"),
                             ("online", "BALANCEQ helps operators check account balances.")):
            with self.subTest(kind=kind, answer=answer):
                result = review_business_explanation(answer, orientation(kind))
                self.assertEqual(set(result["obvious_missing_aspects"]), {"program_handoffs", "data_flow"})
                self.assertEqual(result["aspects"]["business_purpose"]["status"], "mentioned_unverified")
                self.assertTrue(result["revision_recommended"])
                self.assertFalse(result["semantic_verification"])

    def test_names_in_one_sentence_do_not_establish_relationships(self):
        result = review_business_explanation("程序：SETTLE；文件：ORDER-IN、SETTLEMENT-OUT。", orientation())
        self.assertTrue(result["recognized_names_only"])
        self.assertEqual(set(result["obvious_missing_aspects"]),
                         {"business_purpose", "program_handoffs", "data_flow"})

    def test_supported_citation_formats_do_not_hide_purpose_only_or_inventory_answers(self):
        for citation in ("[ev:balance]", "[ev_balance]", "[ev-balance]", "[fw:query-contract]",
                         "[ref:balance]", "[ref_balance]", "[ref-balance]"):
            with self.subTest(citation=citation):
                purpose = review_business_explanation(
                    "BALANCEQ 帮操作人员查看账户余额。" + citation, orientation("online"))
                self.assertEqual(set(purpose["obvious_missing_aspects"]), {"program_handoffs", "data_flow"})
                inventory = review_business_explanation(
                    "程序：SETTLE；文件：ORDER-IN、SETTLEMENT-OUT。" + citation, orientation())
                self.assertTrue(inventory["recognized_names_only"])
                self.assertTrue(inventory["revision_recommended"])

    def test_focused_cross_role_question_does_not_require_a_business_overview(self):
        questions = (
            "批处理程序和文件的关系中，哪个文件是输出？",
            "只解释批处理程序和文件之间的输入关系。",
            "批处理程序和文件的关系是否只限于读取？",
            "Explain only the relationship between this batch program and its input file.",
            "Which output file participates in the relationship with this batch program?",
        )
        text = sources()["settle.cbl"]
        pages = [{"relative_path": "settle.cbl", "source_text": text, "start_line": 1,
                  "end_line": len(text.splitlines()), "evidence_id": "ev:settle",
                  "source_sha256": hashlib.sha256(text.encode()).hexdigest(), "format_hint": "free"}]
        for question in questions:
            with self.subTest(question=question):
                self.assertFalse(wants_business_overview(question))
                review = assess_business_answer(question, "SETTLEMENT-OUT 承接符合受理条件的订单记录。[ev:settle]",
                    {"state": "located", "required_items": []}, pages)
                self.assertNotIn("business_explanation_review", review)
                self.assertFalse({"business_purpose", "program_roles", "data_roles"}
                                 & set(review["required_aspects"]))
        self.assertTrue(wants_business_overview("请讲清batch程序、online程序和表、文件的关系。"))
        self.assertTrue(wants_business_overview("解释整体流程，以及批处理程序和文件之间哪个环节交接数据。"))

    def test_explicit_purpose_only_question_does_not_request_unrelated_handoffs(self):
        for question in ("只说明 BALANCEQ 的业务目的。", "仅解释 BALANCEQ 的业务用途。",
                         "Only explain the business purpose of BALANCEQ."):
            with self.subTest(question=question):
                self.assertFalse(wants_business_overview(question))
        self.assertTrue(wants_business_overview("不要只说明业务目的，还要解释整体流程和交接。"))

    def test_only_obvious_omission_is_a_retry_hint(self):
        for answer in (
                "这一步形成应收凭据，尚未过账。",
                "Eligible requests become bill records; rejected requests bypass fee calculation.",
                "请求通过审核后进入出账处理；否则留下拒绝状态，不进入金额计算分支。",
                "Aquí se preparan los comprobantes para la siguiente etapa."):
            with self.subTest(answer=answer):
                result = review_business_explanation(answer, orientation())
                self.assertFalse(result["revision_recommended"])
                self.assertFalse(result["obvious_missing_aspects"])
                self.assertIn("not_assessable", [item["status"] for item in result["aspects"].values()])

    def test_actual_historical_answers_are_reviewed_without_claiming_new_generation(self):
        report = ROOT / "docs/reports/2026-10-20-business-boundaries/real-model-results-final.json"
        before = report.read_bytes()
        cases = json.loads(before)["cases"]
        for kind in ("batch", "online"):
            case = next(case for case in cases if case["id"] == kind)
            with self.subTest(kind=kind):
                self.assertEqual(hashlib.sha256(case["answer"].encode()).hexdigest(), case["answer_sha256"])
                result = review_business_explanation(case["answer"], orientation(kind))
                self.assertFalse(result["revision_recommended"])
                self.assertFalse(result["semantic_verification"])
                self.assertNotIn("pass", result)
        self.assertEqual(report.read_bytes(), before)

    def test_prose_needs_no_fixed_headings_or_language_template(self):
        answers = (
            ("batch", "nightjob.clle requests SETTLE after declaring the input override. "
             "SETTLE reads ORDER-IN and writes qualifying order identifiers and amounts to SETTLEMENT-OUT."),
            ("online", "调用方传入账户号给 BALANCEQ；它查询 ACCOUNT_BALANCES，余额和状态交给调用方。"),
            ("online", "The caller passes an account key to BALANCEQ. "
             "The program queries ACCOUNT_BALANCES and returns the stored balance and a status to its caller."),
        )
        for kind, answer in answers:
            with self.subTest(kind=kind, answer=answer):
                result = review_business_explanation(answer, orientation(kind))
                self.assertFalse(result["revision_recommended"])
                self.assertTrue(any(item["status"] == "mentioned_unverified"
                                    for item in result["aspects"]["data_flow"]["items"]))
                if kind == "online":
                    self.assertEqual(result["aspects"]["program_handoffs"]["status"], "mentioned_unverified")

    def test_mentions_never_certify_correct_direction_or_claims(self):
        result = review_business_explanation(
            "BALANCEQ calls its caller. BALANCEQ writes ACCOUNT_BALANCES to settle payments.", orientation("online"))
        self.assertEqual(result["aspects"]["data_flow"]["status"], "mentioned_unverified")
        self.assertFalse(result["semantic_verification"])
        self.assertIn("correctness", result["limitation"])

    def test_purpose_uses_its_own_supplied_basis(self):
        context = orientation("online")
        context["purpose_candidates"] = []
        result = review_business_explanation("BALANCEQ 帮操作人员查看账户余额。", context)
        self.assertEqual(result["aspects"]["business_purpose"]["status"], "no_supplied_basis")
        self.assertEqual(set(result["obvious_missing_aspects"]), {"program_handoffs", "data_flow"})

    def test_declarations_alone_do_not_invent_an_io_relationship(self):
        context = orientation()
        context["data_roles"] = [row for row in context["data_roles"] if row.get("kind") == "file_declaration"]
        context["handoffs"] = []
        result = review_business_explanation("SETTLE 整理符合受理要求的订单。", context)
        self.assertEqual(result["aspects"]["data_flow"]["status"], "no_supplied_basis")

    def test_edges_require_retained_origins_and_keep_local_relationship_references(self):
        context = orientation("online")
        for group in ("programs", "entries", "relationships", "data_roles", "visible_rules", "purpose_candidates"):
            for number, row in enumerate(context[group]):
                row["orientation_id"] = group + str(number)
        context["handoffs"] = [
            {"handoff_id": "current", "relation": "reads_sql_table", "from": {"name": "BALANCEQ"},
             "to": {"name": "ACCOUNT_BALANCES"}, "source_fact_ids": ["data_roles0"],
             "supplied_reference_ids": ["ev-balance.cbl"]},
            {"handoff_id": "removed", "relation": "reads_sql_table", "from": {"name": "BALANCEQ"},
             "to": {"name": "OTHER_TABLE"}, "source_fact_ids": ["absent"],
             "supplied_reference_ids": ["ev-removed"]},
        ]
        result = review_business_explanation("BALANCEQ 帮操作人员查看账户余额。", context)
        items = result["aspects"]["data_flow"]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["handoff_id"], "current")
        self.assertEqual(items[0]["supplied_reference_ids"], ["ev-balance.cbl"])

    def test_projection_or_answer_truncation_cannot_prove_omission(self):
        context = orientation("online")
        context["truncated"] = True
        self.assertFalse(review_business_explanation("BALANCEQ 帮操作人员查看账户余额。", context)["revision_recommended"])
        result = review_business_explanation(" " * 66000 + "调用方传入账户号。", orientation("online"))
        self.assertTrue(result["answer_review_truncated"])
        self.assertFalse(result["revision_recommended"])

    def test_no_evidence_no_obligations_and_no_io_or_mutation(self):
        self.assertEqual(review_business_explanation("SETTLE 帮操作人员整理订单。", {})["obvious_missing_aspects"], [])
        context = orientation("online")
        before = copy.deepcopy(context)
        with patch("builtins.open", side_effect=AssertionError("file access")), \
                patch("socket.socket.connect", side_effect=AssertionError("network access")):
            result = review_business_explanation("BALANCEQ 帮操作人员查看账户余额。", context)
        self.assertTrue(result["revision_recommended"])
        self.assertEqual(context, before)

    def test_empty_answer_omits_available_relationships(self):
        result = review_business_explanation("", orientation("online"))
        self.assertEqual(set(result["obvious_missing_aspects"]),
                         {"business_purpose", "program_handoffs", "data_flow"})


if __name__ == "__main__":
    unittest.main()
