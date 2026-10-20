"""Actual answer delivery revises narrow evidence risks within its allowance."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[2]), str(Path(__file__).resolve().parents[1])]
from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from business_synthesis import assess_business_answer
from company_api import CompanyAPIConfig, TransportResponse
from draft_validation import revision_regression, validate_answer_draft
from repository_discovery import ensure_repository_search
from tools.business_onboarding_fixture import sources


class AnswerBoundaryDeliveryTests(unittest.TestCase):
    def ask(self, first, *, requests=3, citation_style="square"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "balance.cbl").write_text(sources()["balance.cbl"])
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", quiet=True)
            ensure_repository_search(database, source)
            sent = []
            good = ("BALANCEQ 按调用方提供的账号查询可用余额，余额表提供数据，余额参数承接输出。"
                "这段 SQL 读取余额，没有更新账户余额的语句。SQLCODE=0 时返回 00，否则返回 90；"
                "失败分支没有重新赋值余额，使用者须结合状态判断。锁行为及具体数据库事件的返回码约定未提供。")

            def transport(request):
                payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
                sent.append(payload)
                page = next(row for context in payload["source_context"] for row in context["pages"]
                            if row["relative_path"] == "balance.cbl")
                answer = (first if len(sent) == 1 and first is not None else good)
                answer += (f" （{page['evidence_id']}）" if citation_style == "round"
                           else f" [{page['evidence_id']}]")
                return TransportResponse(200, json.dumps({"choices": [{"message": {
                    "role": "assistant", "content": answer}, "finish_reason": "stop"}]}))

            result = run_business_chat("我是新人，讲讲 BALANCEQ。", database, source,
                CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only"),
                transport=transport, framework_reference_path="", policy=AgentPolicy(max_model_requests=requests))
            return sent, result["agent_result"]

    def test_supplied_sql_risk_reaches_revision_and_is_removed(self):
        first = ("BALANCEQ 按账号查询可用余额，账号是输入，余额表提供输出。"
                 "这是只读查询，不改表、不锁定、不写回。SQLCODE=0 返回00，否则90。")
        sent, result = self.ask(first)
        self.assertEqual(len(sent), 2)
        review = sent[1]["answer_review"]["sql_boundary_review"]
        self.assertTrue(review["review_required"])
        self.assertTrue(review["risks"])
        self.assertNotIn("不锁定", result["answer"])
        self.assertTrue(result["business_review"]["synthesis_review_attempted"])

    def test_supported_first_answer_does_not_spend_a_second_request(self):
        sent, result = self.ask(None)
        self.assertEqual(len(sent), 1)
        self.assertFalse(result["business_review"]["synthesis_review_attempted"])
        self.assertFalse(result["answer_completion"]["sql_boundary_review"]["review_required"])

    def test_qualified_database_boundaries_do_not_spend_a_second_request(self):
        for qualification in (
                "对于多行查询，是否走ELSE分支需要数据库约定，本段源码没有提供。",
                "对于数据库查询，查不到时走哪条分支取决于数据库返回的状态。",
                "这段SELECT未保证不会加锁。"):
            with self.subTest(qualification=qualification):
                sent, result = self.ask("BALANCEQ 按调用方账号查询可用余额，余额表提供输出；"
                    "SQLCODE=0时返回00，否则返回90，失败分支未赋值余额。" + qualification)
                self.assertEqual(len(sent), 1)
                self.assertFalse(result["business_review"]["synthesis_review_attempted"])

    def test_internal_rule_marker_is_repaired_using_real_supplied_references(self):
        sent, result = self.ask("BALANCEQ 按账号查询可用余额，账号输入，余额表提供输出。"
            "SQLCODE=0返回00，否则90。[rule_0123456789ab_0123456789abcdef01234567]")
        self.assertEqual(len(sent), 2)
        self.assertIn("unresolvable_internal_references", sent[1]["answer_review"]["missing_aspects"])
        self.assertNotIn("[rule_", result["answer"])

    def test_exact_supplied_round_reference_is_delivered_without_another_request(self):
        sent, result = self.ask(None, citation_style="round")
        self.assertEqual(len(sent), 1)
        page = next(row for context in sent[0]["source_context"] for row in context["pages"]
                    if row["relative_path"] == "balance.cbl")
        self.assertIn(f"[{page['evidence_id']}]", result["answer"])
        self.assertNotIn(f"（{page['evidence_id']}）", result["answer"])
        self.assertFalse(result["business_review"]["synthesis_review_attempted"])

    def test_unknown_round_reference_stays_incomplete_when_budget_is_exhausted(self):
        sent, result = self.ask("按账号查询余额，调用方输入账号，余额表提供结果。"
            "SQLCODE=0返回00，否则90，失败分支未赋值余额。（ev_unknown_page）", requests=1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("unresolvable_reference_markers", result["answer_completion"]["missing_aspects"])
        self.assertIn("（ev_unknown_page）", result["answer"])

    def test_manual_round_reference_is_assessed_against_actual_supplied_manual(self):
        reference = {"reference_id": "fw:record-contract", "text": "ADVANCE requests the following record."}
        assessment = assess_business_answer("解释约定", "约定只说明请求下一条记录。（fw:record-contract）",
            {}, [], framework_references=[reference])
        self.assertNotIn("unresolvable_reference_markers", assessment["missing_aspects"])
        absent = assess_business_answer("解释约定", "约定只说明请求下一条记录。（fw:record-contract）", {}, [])
        self.assertIn("unresolvable_reference_markers", absent["missing_aspects"])

    def test_literal_reference_examples_are_not_treated_as_citations(self):
        marker = "[rule_0123456789ab_0123456789abcdef01234567]"
        for example in (f"标识符示例 `{marker}`。", f"```text\n{marker}\n```", f"~~~text\n{marker}\n~~~"):
            with self.subTest(example=example):
                assessment = assess_business_answer("解释引用格式", example, {}, [])
                self.assertNotIn("unresolvable_internal_references", assessment["missing_aspects"])

    def test_unsupported_sql_effect_stays_incomplete_when_budget_is_exhausted(self):
        sent, result = self.ask("按账号查询余额，调用方输入账号，余额表提供结果，不会加锁。", requests=1)
        self.assertEqual(len(sent), 1)
        self.assertEqual(result["answer_completion"]["status"], "incomplete")
        self.assertEqual(result["status"], "PARTIAL")

    def test_revision_cannot_introduce_a_new_sql_runtime_claim(self):
        text = sources()["balance.cbl"]
        page = {"evidence_id": "ev_query", "relative_path": "balance.cbl",
            "source_text": text, "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "start_line": 1, "end_line": len(text.splitlines())}
        previous = validate_answer_draft("解释余额查询", "SQL 按账号查询余额，锁行为不能确认。[ev_query]", {}, [page])
        candidate = validate_answer_draft("解释余额查询", "SQL 按账号查询余额，不会加锁。[ev_query]", {}, [page])
        self.assertEqual(revision_regression(previous, candidate), "ANSWER_BOUNDARIES_REGRESSED")


if __name__ == "__main__":
    unittest.main()
