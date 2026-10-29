"""Long natural-language questions must not erase source search leads."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import (discover_repository, ensure_repository_search,
                                  retrieve_repository_context)


QUESTION = ("请说明客户按申请渠道选择付款方式以后，订单进入审核等待阶段，"
    "然后资金处理人员依据到账记录对照审批申请，结算团队会先匹配账户凭证，"
    "再进行余额复核和付款登记，最终确认这个流程究竟采用什么业务条件决定"
    "是否允许付款完成以及费用怎么扣除和排序，退费具体怎样计算？")
CONDITION = "IF PAYMENT-ELIGIBLE = 1"
FORMULA = "COMPUTE REFUND-AMOUNT = PAID-AMOUNT - CHARGE-AMOUNT"


class RetrievalQueryBudgetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"

    def build(self, *, comment="", overview=""):
        self.text = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRYPOINT.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 PAYMENT-ELIGIBLE PIC 9 VALUE 1.\n"
            "01 PAID-AMOUNT PIC 9(5) VALUE 100.\n"
            "01 CHARGE-AMOUNT PIC 9(5) VALUE 7.\n"
            "01 REFUND-AMOUNT PIC 9(5).\nPROCEDURE DIVISION.\nMAIN.\n" + overview
            + "*> unrelated neutral source context\n" * 160
            + "REFUND-ELIGIBILITY.\n" + comment + CONDITION + "\n" + FORMULA + "\nEND-IF.\nGOBACK.\n")
        (self.source / "entry.cbl").write_text(self.text, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free")
        ensure_repository_search(self.database, self.source)

    def assert_original_lines(self, pages):
        digest = hashlib.sha256((self.source / "entry.cbl").read_bytes()).hexdigest()
        for page in pages:
            self.assertEqual(page["source_sha256"], digest)
            self.assertEqual(page["source_text"].splitlines(),
                self.text.splitlines()[page["start_line"] - 1:page["end_line"]])

    def test_explicit_expansion_survives_long_question_in_discovery_and_crop(self):
        self.build()
        context = retrieve_repository_context(self.database, self.source, QUESTION,
            search_terms=["REFUND-ELIGIBILITY"], max_pages=1, max_chars=512)
        discovery = discover_repository(self.database, QUESTION,
            search_terms=["REFUND-ELIGIBILITY"], fallback_to_repository=False)
        supplied = "\n".join(page["source_text"] for page in context["pages"])
        self.assertIn(CONDITION, supplied)
        self.assertIn(FORMULA, supplied)
        self.assertGreater(context["matched_page_count"], 0)
        self.assertFalse(context["orientation_only"])
        self.assertEqual(discovery["selected_paths"], ["entry.cbl"])
        self.assertGreater(discovery["matched_page_count"], 0)
        self.assertLessEqual(len(context["search_terms"]), 128)
        self.assertLessEqual(len(supplied), 512)
        self.assert_original_lines(context["pages"])

    def test_new_search_crops_around_new_hit_instead_of_old_dense_comment(self):
        question = ("Explain customer receipt allocation outstanding settlement charge "
                    "balance payment account eligibility approval amount status result order")
        self.build(overview="*> " + question + "\n")
        context = retrieve_repository_context(self.database, self.source, question,
            search_terms=["REFUND-ELIGIBILITY"], max_pages=1, max_chars=512)
        supplied = "\n".join(page["source_text"] for page in context["pages"])
        self.assertIn("REFUND-ELIGIBILITY.", supplied)
        self.assertIn(CONDITION, supplied)
        self.assertIn(FORMULA, supplied)
        self.assert_original_lines(context["pages"])

    def test_short_chinese_source_term_at_end_of_long_question_is_searchable(self):
        self.build(comment="*> 退费\n")
        context = retrieve_repository_context(self.database, self.source, QUESTION,
            max_pages=1, max_chars=512)
        supplied = "\n".join(page["source_text"] for page in context["pages"])
        self.assertIn("退费", supplied)
        self.assertIn(FORMULA, supplied)
        self.assertGreater(context["matched_page_count"], 0)
        self.assertFalse(context["orientation_only"])
        self.assert_original_lines(context["pages"])

    def test_expanded_search_reaches_actual_model_request(self):
        self.build()
        requests = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            pages = [page for bundle in payload["source_context"] for page in bundle.get("pages", [])]
            if len(requests) == 1:
                text = json.dumps({"search": ["REFUND-ELIGIBILITY"]})
            else:
                page = next((page for page in pages if FORMULA in page["source_text"]), None)
                # The response reports literal visibility only. It does not
                # stand in for a model's business interpretation.
                text = (f"Visible source: {CONDITION}; {FORMULA}.[{page['evidence_id']}]"
                        if page else "The requested formula was not visible in this request.")
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text},
                "finish_reason": "stop"}]}))

        run_business_chat(QUESTION, self.database, self.source,
            CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model", api_key="offline-only"),
            transport=transport, allow_network=False, framework_reference_path="",
            policy=AgentPolicy(max_model_requests=2, initial_source_characters=512,
                search_source_characters=512, max_complete_source_characters=512))
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["response_mode"], "source_discovery")
        for payload in requests:
            self.assert_original_lines([page for bundle in payload["source_context"]
                for page in bundle.get("pages", [])])
        supplied = "\n".join(page["source_text"] for bundle in requests[1]["source_context"]
            for page in bundle.get("pages", []))
        self.assertIn(CONDITION, supplied)
        self.assertIn(FORMULA, supplied)


if __name__ == "__main__":
    unittest.main()
