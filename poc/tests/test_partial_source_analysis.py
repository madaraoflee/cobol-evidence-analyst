from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from company_api import CompanyAPIConfig, TransportRequest, TransportResponse  # noqa: E402
from run_agent import run_investigation  # noqa: E402
from structural_index import build_structural_index  # noqa: E402
from test_run_agent import AgentReadyTransport, response  # noqa: E402


SOURCE = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. ORDER-CALC.
       DATA DIVISION.
       WORKING-STORAGE SECTION.
       COPY SHARED-AREA.
       01 INPUT-AMOUNT PIC 9(7)V99.
       01 FACTOR PIC 9V99.
       01 RESULT-AMOUNT PIC 9(7)V99.
       01 OPTION-FLAG PIC X.
       PROCEDURE DIVISION.
       MAIN-STEP.
           IF OPTION-FLAG = 'Y'
              COMPUTE RESULT-AMOUNT = INPUT-AMOUNT * FACTOR
              END-COMPUTE
           ELSE
              MOVE INPUT-AMOUNT TO RESULT-AMOUNT
           END-IF.
           CALL 'CLOSED-SERVICE' USING RESULT-AMOUNT.
           GOBACK.
"""


class PartialSourceTransport(AgentReadyTransport):
    """Mock the API, deriving all evidence IDs from actual local tool results."""

    def __init__(self) -> None:
        super().__init__()
        self.agent_payloads: list[dict] = []

    def __call__(self, request: TransportRequest) -> TransportResponse:
        payload = json.loads(request.body or b"{}")
        messages = payload.get("messages", [])
        if request.endpoint != "chat/completions" or not any(
            item.get("role") == "system" for item in messages
        ):
            return super().__call__(request)
        self.requests.append(request)
        self.agent_payloads.append(payload)
        results = [json.loads(item["content"]) for item in messages if item.get("role") == "tool"]
        stage = len(results)
        if stage == 0:
            action = "inspect_symbol"
            arguments = {"name": "ORDER-CALC", "symbol_type": "Program", "max_relations": 80}
        elif stage == 1:
            action = "inspect_symbol"
            arguments = {"name": "CLOSED-SERVICE", "symbol_type": "Program"}
        elif stage == 2:
            action = "inspect_symbol"
            arguments = {"name": "SHARED-AREA", "symbol_type": "Copybook"}
        else:
            relations = results[0]["matches"][0]["outgoing_relations"]
            refs = list(dict.fromkeys(edge["evidence_ref"]["evidence_id"] for edge in relations))
            if stage == 3:
                action = "read_evidence"
                arguments = {"evidence_ids": refs}
            elif stage == 4:
                formula = next(edge["evidence_ref"]["evidence_id"] for edge in relations
                               if edge["relation_type"] == "WRITES" and edge["target"]["name"] == "RESULT-AMOUNT")
                condition = next(edge["evidence_ref"]["evidence_id"] for edge in relations
                                 if edge["relation_type"] == "READS" and edge["target"]["name"] == "OPTION-FLAG")
                call = next(edge["evidence_ref"]["evidence_id"] for edge in relations
                            if edge["relation_type"] == "CALLS")
                action = "final_answer"
                arguments = {
                    "claims": [
                        {"kind": "code_fact", "assertion": {
                            "predicate": "compute_statement", "target": "RESULT-AMOUNT",
                            "expression": ["INPUT-AMOUNT", "*", "FACTOR"], "rounded": False,
                        }, "evidence_ids": [formula]},
                        {"kind": "code_fact", "claim": "OPTION-FLAG 为 Y 时进入计算分支。",
                         "code_anchors": ["OPTION-FLAG"], "evidence_ids": [condition, formula],
                         "support_status": "partial"},
                        {"kind": "code_fact", "claim": "CLOSED-SERVICE 是此处调用的目标，其内部逻辑不可见。",
                         "code_anchors": ["CLOSED-SERVICE"], "evidence_ids": [call],
                         "support_status": "partial"},
                    ],
                    "evidence_ids": refs,
                    "boundaries": [
                        "SHARED-AREA 未随源码提供，字段布局不完整。",
                        "CLOSED-SERVICE 的实现不可见，不能确认其返回值或副作用。",
                    ],
                }
            else:
                raise AssertionError("Unexpected model turn")
        return response(200, {"choices": [{"message": {"role": "assistant", "content": json.dumps({
            "action": action, "arguments": arguments,
        }, ensure_ascii=False)}}]})


class PartialSourceAnalysisTests(unittest.TestCase):
    def test_single_program_with_missing_copy_and_closed_call_retains_visible_logic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            root.mkdir()
            (root / "order-calc.cbl").write_text(SOURCE, encoding="utf-8")
            database = Path(temporary) / "index.sqlite"
            build_structural_index(root, database, quiet=True)
            transport = PartialSourceTransport()
            output = run_investigation(
                "解释当前程序的计算、判断和外部调用", database,
                CompanyAPIConfig(base_url="https://company.example/v1", chat_model="company-chat-model", api_key="local-test-key"),
                entry_program="ORDER-CALC", transport=transport,
            )

        self.assertEqual(output["runner_status"], "COMPLETED")
        result = output["agent_result"]
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["stop_reason"], "completed")
        self.assertEqual(result["tool_calls_used"], 4)
        self.assertEqual(len(result["claims"]), 3)
        self.assertEqual(result["claims"][0]["support_status"], "supported")
        self.assertEqual(result["claims"][1]["support_status"], "citation_verified_only")
        self.assertEqual(result["analysis_scope"]["unresolved_dependency_count"], 2)
        self.assertEqual(result["analysis_scope"]["source_scope"], "visible_source_only")
        self.assertFalse(result["analysis_scope"]["runtime_state_verified"])
        self.assertEqual(result["question_coverage"]["status"], "not_assessed")
        self.assertTrue(all(ref["relative_path"] == "order-calc.cbl" for ref in result["evidence_refs"]))
        self.assertIn("RESULT-AMOUNT", result["answer"])
        self.assertIn("OPTION-FLAG", result["answer"])
        self.assertIn("CLOSED-SERVICE", result["answer"])
        self.assertTrue(any(item.get("reason") == "dependency_discovery_exhausted"
                            for item in result["boundaries"] if isinstance(item, dict)))
        self.assertIn("Read the already discovered evidence now",
                      transport.agent_payloads[3]["messages"][0]["content"])
        self.assertNotIn("SHARED-AREA", json.dumps(result["claims"]))


if __name__ == "__main__":
    unittest.main()
