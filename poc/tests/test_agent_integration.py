from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from agent_loop import BoundedAgentLoop  # noqa: E402
from investigation_tools import InvestigationTools  # noqa: E402
from structural_index import build_structural_index  # noqa: E402


FIXTURE_ROOT = POC_ROOT / "fixtures" / "synthetic-insurance-v1"


def json_action(action: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {"action": action, "arguments": arguments},
                        ensure_ascii=False,
                    ),
                }
            }
        ]
    }


def relation_evidence(
    relations: list[dict[str, Any]], relation_type: str, target_name: str
) -> str:
    for relation in relations:
        if (
            relation.get("relation_type") == relation_type
            and relation.get("target", {}).get("name") == target_name
        ):
            return str(relation["evidence_ref"]["evidence_id"])
    raise AssertionError(f"Missing {relation_type} relation to {target_name}")


class DynamicCalcPlanner:
    """Offline planner that derives every later argument from real tool output."""

    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []
        self.formula_evidence_id: str | None = None
        self.requested_evidence_ids: list[str] = []

    def complete(self, **kwargs: object) -> dict[str, object]:
        messages = kwargs["messages"]
        last = messages[-1]
        if last["role"] == "user":
            try:
                envelope = json.loads(last["content"])
            except (TypeError, json.JSONDecodeError):
                envelope = None
            if isinstance(envelope, dict) and envelope.get("type") == "TOOL_RESULT":
                result = envelope["result"]
                if not self.results or self.results[-1] is not result:
                    self.results.append(result)

        stage = len(self.results)
        if stage == 0:
            return json_action(
                "search_code",
                {"query": "OUT-INSTALMENT-PREMIUM", "limit": 6},
            )

        if stage == 1:
            search = self.results[0]
            program_name = next(
                hit["program_name"]
                for hit in search["hits"]
                if hit.get("name") == "COMPUTE" and hit.get("program_name")
            )
            return json_action(
                "inspect_symbol",
                {
                    "name": program_name,
                    "symbol_type": "Program",
                    "max_relations": 80,
                },
            )

        if stage == 2:
            inspection = self.results[1]["matches"][0]
            caller = next(
                relation["source"]["program_name"]
                for relation in inspection["incoming_relations"]
                if relation["relation_type"] == "CALLS"
            )
            return json_action(
                "trace_relations",
                {
                    "start_name": caller,
                    "symbol_type": "Program",
                    "relation_types": [
                        "CALLS",
                        "CALL_TARGET_FROM",
                        "SELECTS_FROM",
                    ],
                    "direction": "outgoing",
                    "max_depth": 2,
                    "max_edges": 30,
                },
            )

        if stage == 3:
            inspection = self.results[1]["matches"][0]
            outgoing = inspection["outgoing_relations"]
            incoming = inspection["incoming_relations"]
            trace_edges = self.results[2]["edges"]
            evidence_ids = [
                relation_evidence(outgoing, "WRITES", "WS-ANNUAL-PREMIUM"),
                relation_evidence(
                    outgoing, "WRITES", "OUT-INSTALMENT-PREMIUM"
                ),
                relation_evidence(outgoing, "SELECTS_FROM", "SYN_MODE_FACTOR"),
                relation_evidence(incoming, "CALLS", "SYNP040"),
                relation_evidence(
                    trace_edges, "CALL_TARGET_FROM", "LK-CALCULATOR-PROGRAM"
                ),
                relation_evidence(trace_edges, "SELECTS_FROM", "SYN_CALC_ROUTING"),
            ]
            self.formula_evidence_id = evidence_ids[1]
            self.requested_evidence_ids = list(dict.fromkeys(evidence_ids))
            return json_action(
                "read_evidence",
                {
                    "evidence_ids": self.requested_evidence_ids,
                    "max_chars": 8_000,
                },
            )

        if stage == 4:
            assert self.formula_evidence_id is not None
            return json_action(
                "final_answer",
                {
                    "claims": [
                        {
                            "claim": (
                                "OUT-INSTALMENT-PREMIUM is computed from "
                                "WS-ANNUAL-PREMIUM and WS-MODE-FACTOR."
                            ),
                            "kind": "code_fact",
                            "code_anchors": [
                                "OUT-INSTALMENT-PREMIUM",
                                "WS-ANNUAL-PREMIUM",
                                "WS-MODE-FACTOR",
                            ],
                            "evidence_ids": [self.formula_evidence_id],
                            "support_status": "supported",
                        }
                    ],
                    "evidence_ids": [self.formula_evidence_id],
                    "boundaries": [],
                },
            )
        raise AssertionError("Planner received an unexpected model turn")


class RefusalPlanner:
    def complete(self, **_: object) -> dict[str, object]:
        return json_action(
            "abstain",
            {
                "claims": [],
                "evidence_ids": [],
                "boundaries": [
                    {
                        "reason": "Production table values and runtime logs are not indexed.",
                        "required_artifact": "approved production data snapshot",
                    }
                ],
            },
        )


class RealAgentIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temporary.name) / "agent.sqlite"
        cls.build_report = build_structural_index(
            FIXTURE_ROOT, cls.database, quiet=True
        )
        cls.tools = InvestigationTools(cls.database)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_calc_01_closes_with_four_real_tool_calls_and_dynamic_evidence(self) -> None:
        planner = DynamicCalcPlanner()
        result = BoundedAgentLoop(
            planner,
            self.tools,
            native_tool_calling=False,
            strict_json=True,
        ).run("分期保费最终是怎样计算出来的？")

        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["stop_reason"], "completed")
        self.assertEqual(result["tool_calls_used"], 4)
        self.assertEqual(
            [item["tool"] for item in result["tool_trace"]],
            ["search_code", "inspect_symbol", "trace_relations", "read_evidence"],
        )
        self.assertEqual(len(planner.requested_evidence_ids), 6)
        self.assertTrue(
            set(planner.requested_evidence_ids)
            <= set(result["verified_evidence_ids"])
        )
        self.assertEqual(result["snapshot_id"], self.build_report["snapshot_id"])
        self.assertIn("OUT-INSTALMENT-PREMIUM", result["answer"])
        self.assertIn("ROUNDED", result["answer"])
        self.assertIn("control-table values", result["answer"])
        self.assertFalse(
            result["verification"]["semantic_claim_support_checked"]
        )

    def test_business_reason_and_production_value_questions_abstain(self) -> None:
        questions = (
            "为什么公司把月缴 MODE_FACTOR 定成现在这个数值？",
            (
                "产品 ABCD 在 2026-08-31 的生产运行中会由 SYNP100 还是 "
                "SYNP200 处理，当时实际 MODE_FACTOR 是多少？"
            ),
        )
        for question in questions:
            with self.subTest(question=question):
                result = BoundedAgentLoop(
                    RefusalPlanner(),
                    self.tools,
                    native_tool_calling=False,
                    strict_json=True,
                ).run(question)
                self.assertEqual(result["status"], "ABSTAINED")
                self.assertEqual(result["stop_reason"], "model_abstained")
                self.assertEqual(result["tool_calls_used"], 0)
                self.assertIn("Production table values", result["answer"])


if __name__ == "__main__":
    unittest.main()
