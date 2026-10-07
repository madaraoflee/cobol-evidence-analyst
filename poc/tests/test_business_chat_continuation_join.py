"""Exercise response joining through the real chat consumer, without a provider."""

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
from repository_discovery import ensure_repository_search


class ChatContinuationJoinTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        (self.source / "decision.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. DECISION.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 RESULT-CODE PIC 99.\n"
            "PROCEDURE DIVISION.\nMAIN.\nMOVE 12 TO RESULT-CODE.\nGOBACK.\n", encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
                                       api_key="offline-neutral-credential")

    def ask(self, *, second_finish="stop"):
        self.responses = []
        self.requests = []
        self.prefix = "结果来自入口赋值。\n\n1. 已说明的条件保留。\n\n"

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.requests.append(payload)
            page = payload["source_context"][0]["pages"][0]
            if len(self.requests) == 1:
                text = self.prefix + "2. 返回的状态字段是 `RESULT-CO"
                finish = "length"
            else:
                text = "2. 返回的状态字段是 `RESULT-CODE`，其值是12。[" + page["evidence_id"] + "]"
                finish = second_finish
            self.responses.append(text)
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": text}, "finish_reason": finish}]}, ensure_ascii=False))

        return run_business_chat("DECISION 返回什么？", self.database, self.source, self.config,
            transport=transport, framework_reference_path="",
            policy=AgentPolicy(max_model_requests=3, max_answer_revisions=0))["agent_result"]

    def test_restarted_item_is_joined_once_and_accounting_keeps_both_requests(self):
        result = self.ask()
        self.assertEqual(result["answer"], self.prefix + self.responses[1])
        self.assertEqual(result["narrative"]["text"], result["answer"])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["metrics"]["model_requests"], 2)
        self.assertTrue(result["continuation_attempted"])
        self.assertFalse(result["answer_truncated"])
        self.assertEqual(self.requests[1]["draft_answer"], self.responses[0])
        summary = result["diagnostic_summary"]
        self.assertEqual([row["parsed_answer_characters"] for row in summary["requests"]],
                         [len(text) for text in self.responses])
        self.assertEqual(summary["output"]["final_answer_characters"], len(result["answer"]))
        self.assertTrue(result["narrative"]["citations"])

    def test_second_length_keeps_partial_status_without_third_request(self):
        result = self.ask(second_finish="length")
        self.assertEqual(result["answer"], self.prefix + self.responses[1])
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(result["answer_truncated"])
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["stop_reason"], "MODEL_OUTPUT_TRUNCATED")


if __name__ == "__main__":
    unittest.main()
