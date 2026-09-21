from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_analysis import run_business_analysis
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from framework_knowledge import build_framework_context
from source_reading import prepare_source_reading


def response(text):
    return TransportResponse(200, json.dumps({"choices": [{"message": {
        "role": "assistant", "content": text}, "finish_reason": "stop"}]}, ensure_ascii=False))


class QuestionContextRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="local-test-key")
        self.reference = self.root / "reference.md"
        self.reference.write_text("# Record behavior\n\nFETCH-LOCK requests a record and its update lock.\n", encoding="utf-8")

    def write(self, path, name, body):
        (self.source / path).write_text(
            f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nPROCEDURE DIVISION.\n{body}\nGOBACK.\n",
            encoding="utf-8")

    def build(self):
        build_business_index(self.source, self.database, source_format="free", quiet=True)

    def run_analysis(self, transport, **kwargs):
        return run_business_analysis("请说明处理规则与后续业务影响。", self.database, self.source,
            self.config, transport=transport, reading_strategy="full_chain",
            framework_reference_path=self.reference, **kwargs)

    def test_call_after_first_eighty_links_is_supplied_to_its_actual_source_page(self):
        for index in range(83):
            body = f"CALL 'FLOW{index + 1:03d}'." if index < 82 else "CONTINUE."
            self.write(f"flow{index:03d}.cbl", f"FLOW{index:03d}", body)
        self.build()
        plan = prepare_source_reading(self.database, self.source, entry_program="FLOW000",
            question="请说明处理规则。", reading_strategy="full_chain")
        self.assertEqual(len(plan["all_call_chain_links"]), 82)
        self.assertNotIn("FLOW082", {item["target_name"] for item in plan["call_chain"]["links"]})
        late_requests = []

        def transport(request):
            supplied = json.loads(json.loads(request.body)["messages"][-1]["content"])
            pages = supplied.get("source_pages", [])
            for page in pages:
                if page["relative_path"] == "flow081.cbl":
                    late_requests.append(supplied)
                    self.assertIn("CALL 'FLOW082'", page["source_text"])
                    links = supplied["call_chain"]["links"]
                    late_call = next(item for item in links if item["target_name"] == "FLOW082")
                    self.assertEqual(late_call["caller_path"], "flow081.cbl")
                    self.assertEqual(late_call["target_path"], "flow082.cbl")
                    self.assertEqual(late_call["resolution"], "confirmed")
                    self.assertIn(page["evidence_id"], late_call["caller_evidence_ids"])
            citations = " ".join(f"[{page['evidence_id']}]" for page in pages)
            return response("当前处理完成后继续调用下一处理程序。" + citations)

        result = self.run_analysis(transport, entry_program="FLOW000")
        self.assertEqual(result["runner_status"], "COMPLETED")
        self.assertEqual(len(late_requests), 1)
        self.assertEqual(result["agent_result"]["reading_coverage"]["sent_files"], 83)

    def test_stale_framework_call_details_are_removed_before_any_model_request(self):
        self.write("entry.cbl", "ENTRYONE", "MOVE 'FETCH-LOCK' TO ACCESS-FUNCTION.\n"
                   "CALL 'RECORDSERVICE' USING ACCESS-FUNCTION.")
        self.build()
        context = build_framework_context(self.database, entry_program="ENTRYONE",
            reference_path=self.reference, source_root=self.source)
        self.assertEqual(context["status"], "MATCHED")
        self.assertTrue(context["external_calls"])
        stale = deepcopy(context)
        stale["coverage"]["snapshot_id"] = "previous-snapshot"
        stale["external_calls"][0]["source_text"] = "OBSOLETE-CALL-TEXT"
        supplied_requests = []

        def transport(request):
            supplied = json.loads(json.loads(request.body)["messages"][-1]["content"])
            supplied_requests.append(supplied)
            self.assertEqual(supplied["framework_references"], [])
            self.assertEqual(supplied["external_calls"], [])
            self.assertNotIn("OBSOLETE-CALL-TEXT", json.dumps(supplied))
            self.assertIn("FETCH-LOCK", supplied["source_pages"][0]["source_text"])
            return response("程序请求读取记录；当前源码仍可解释。")

        result = self.run_analysis(transport, entry_program="ENTRYONE", framework_context=stale)
        self.assertEqual(result["runner_status"], "COMPLETED")
        self.assertTrue(supplied_requests)
        self.assertEqual(result["framework_context"]["reason_code"], "FRAMEWORK_SOURCE_CONTEXT_STALE")
        self.assertFalse(result["framework_context"]["external_calls"])

    def test_ordinary_structured_business_reply_is_preserved_without_contract_rejection(self):
        self.write("entry.cbl", "ENTRYONE", "IF WAIT-SLOTS > ZERO\nMOVE 'QUEUED' TO ITEM-STATE\nEND-IF.")
        self.build()
        requests = []

        def transport(request):
            envelope = json.loads(request.body)
            self.assertNotIn("tools", envelope)
            self.assertNotIn("response_format", envelope)
            supplied = json.loads(envelope["messages"][-1]["content"])
            requests.append(supplied)
            page = supplied["source_pages"][0]
            return response(json.dumps({"业务规则": "可等待名额大于零时设置排队状态。",
                                        "依据": f"[{page['evidence_id']}]"}, ensure_ascii=False))

        result = self.run_analysis(transport, entry_program="ENTRYONE")
        self.assertEqual(result["runner_status"], "COMPLETED")
        self.assertEqual(len(requests), 1)
        answer = result["agent_result"]
        self.assertIn("可等待名额大于零时设置排队状态", answer["answer"])
        self.assertTrue(answer["model_answer_recorded"])
        self.assertNotIn("【未确认来源引用】", answer["answer"])
        self.assertIn("MODEL_STRUCTURED_TEXT_PRESERVED", {item["code"] for item in answer["diagnostics"]})
        self.assertFalse({"MODEL_ACTION_RESPONSE", "MODEL_ERROR_RESPONSE", "MODEL_TEXT_EMPTY"}
                         & {item["code"] for item in answer["diagnostics"]})


if __name__ == "__main__":
    unittest.main()
