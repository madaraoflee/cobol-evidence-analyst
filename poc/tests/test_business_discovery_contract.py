"""Discovery requests must not also require an unsupported business answer."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_chat import _prompt_payload, run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


class BusinessDiscoveryContractTests(unittest.TestCase):
    @staticmethod
    def payload():
        return {"question": "什么情况需要人工复核？", "answer_detail": "detailed",
                "task": "这一轮只做检索规划，返回 JSON search 数组。",
                "source_context": [{"pages": []}],
                "business_map": {"source_identity": {"status": "none"}},
                "retrieval_status": {"state": "needs_discovery"},
                "investigation_budget": {"remaining_model_requests": 5, "searches_per_turn": 3}}

    def test_empty_source_discovery_has_action_contract_without_mutating_payload(self):
        payload = self.payload()
        original = deepcopy(payload)
        wire = _prompt_payload(payload)
        self.assertEqual(payload, original)
        self.assertEqual(wire["response_mode"], "source_discovery")
        self.assertIn("JSON", wire["response_contract"]["format"])
        self.assertNotIn("detail", wire["response_contract"])
        self.assertNotIn("organization", wire["response_contract"])
        self.assertEqual(wire["investigation_budget"], payload["investigation_budget"])
        self.assertFalse(wire["source_selection"]["source_supplied"])

    def test_supplied_source_keeps_detailed_answer_contract_during_additional_search(self):
        payload = self.payload()
        page = {"relative_path": "decision.cbl", "source_text": "MOVE 'REVIEW' TO REQUEST-STATE."}
        payload["source_context"][0]["pages"] = [page]
        wire = _prompt_payload(payload)
        self.assertEqual(wire["response_mode"], "business_analysis")
        self.assertIn("thoroughly", wire["response_contract"]["detail"])
        self.assertTrue(wire["source_selection"]["source_supplied"])
        self.assertEqual(wire["source_selection"]["relative_paths"], ["decision.cbl"])
        self.assertEqual(wire["source_context"][0]["pages"], [page])

    def test_exhausted_search_budget_does_not_request_discovery_actions(self):
        payload = self.payload()
        payload["investigation_budget"].update(remaining_model_requests=1, searches_per_turn=0)
        wire = _prompt_payload(payload)
        self.assertEqual(wire["response_mode"], "business_analysis")
        self.assertNotIn("format", wire["response_contract"])

    def test_unresolved_identity_is_explicit_without_becoming_lexical_discovery(self):
        for identity in ("ambiguous", "not_found"):
            with self.subTest(identity=identity):
                payload = self.payload()
                payload["retrieval_status"]["state"] = "unresolved"
                payload["business_map"]["source_identity"]["status"] = identity
                wire = _prompt_payload(payload)
                self.assertEqual(wire["response_mode"], "business_analysis")
                self.assertEqual(wire["source_selection"]["identity_status"], identity)
                self.assertTrue(wire["source_selection"]["identity_unresolved"])
                self.assertFalse(wire["source_selection"]["source_supplied"])

    def test_unmatched_business_question_searches_then_receives_source_and_answer_contract(self):
        config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only")
        requests = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "decision.cbl").write_text(
                "IDENTIFICATION DIVISION.\nPROGRAM-ID. REQUEST-GATE.\nDATA DIVISION.\n"
                "WORKING-STORAGE SECTION.\n01 REQUEST-AMOUNT PIC 9(6).\n"
                "01 REQUEST-STATE PIC X(8).\nPROCEDURE DIVISION.\nMAIN.\n"
                "IF REQUEST-AMOUNT > 5000\nMOVE 'REVIEW' TO REQUEST-STATE\nEND-IF.\nGOBACK.\n",
                encoding="utf-8")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            ensure_repository_search(database, source)

            def transport(request):
                body = json.loads(request.body)
                wire = json.loads(body["messages"][-1]["content"])
                requests.append(wire)
                if len(requests) == 1:
                    self.assertEqual(wire["response_mode"], "source_discovery")
                    self.assertFalse(wire["source_selection"]["source_supplied"])
                    self.assertGreater(wire["repository"]["indexed_files"], 0)
                    answer = '{"search":["REQUEST-AMOUNT"]}'
                else:
                    self.assertEqual(wire["response_mode"], "business_analysis")
                    self.assertTrue(wire["source_selection"]["source_supplied"])
                    self.assertIn("thoroughly", wire["response_contract"]["detail"])
                    page = next(page for bundle in wire["source_context"] for page in bundle["pages"]
                                if "MOVE 'REVIEW'" in page["source_text"])
                    answer = f"请求金额大于5000时进入REVIEW。[{page['evidence_id']}]"
                return TransportResponse(200, json.dumps({"choices": [{"message": {
                    "role": "assistant", "content": answer}, "finish_reason": "stop"}]}, ensure_ascii=False))

            output = run_business_chat("什么情况需要人工复核？", database, source, config,
                                       framework_reference_path="", transport=transport)
        self.assertEqual(len(requests), 2)
        self.assertEqual(output["reason_code"], "BUSINESS_CHAT_COMPLETED")
        self.assertEqual(output["agent_result"]["metrics"]["tool_calls"]["search"], 1)
        self.assertIn("大于5000", output["agent_result"]["answer"])


if __name__ == "__main__":
    unittest.main()
