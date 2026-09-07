from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_loop import (  # noqa: E402
    BoundedAgentLoop,
    ToolResultPolicyError,
    _project_copy_binding,
    _project_relation_metadata,
)
from test_agent_loop import FakeClient, FakeTools, json_action  # noqa: E402


def inclusion_ref() -> dict[str, object]:
    return {
        "evidence_id": "ev_INCLUDE",
        "relative_path": "programs/TEST.cbl",
        "start_line": 10,
        "end_line": 12,
    }


def copy_binding() -> dict[str, object]:
    return {
        "kind": "copybook_expansion",
        "inclusion_evidence_refs": [inclusion_ref()],
    }


class CopyInspectionTools(FakeTools):
    def __init__(self, binding: dict[str, object] | None = None) -> None:
        super().__init__()
        self.binding = copy.deepcopy(binding if binding is not None else copy_binding())

    def inspect_symbol(
        self,
        name: str,
        *,
        program_name: str | None = None,
        symbol_type: str | None = None,
        max_relations: int = 40,
    ) -> dict[str, object]:
        result = super().inspect_symbol(
            name, program_name=program_name, symbol_type=symbol_type,
            max_relations=max_relations,
        )
        result.update({
            "status": "OK",
            "match_count": 1,
            "matches": [{
                "symbol": {
                    "symbol_id": "symbol_AMOUNT",
                    "symbol_type": "Field",
                    "name": "OUT-AMOUNT",
                    "program_name": "TESTPROG",
                    "qualified_name": "TESTPROG::OUT-AMOUNT",
                },
                "definition": {
                    "unit_id": "unit_AMOUNT",
                    "unit_type": "DataItem",
                    "evidence_ref": {"evidence_id": "ev_DEFINITION"},
                    "copy_binding": self.binding,
                },
                "scope_unit_count": 1,
                "outgoing_relations": [],
                "incoming_relations": [],
            }],
        })
        return result


class AgentSourceBindingContractTests(unittest.TestCase):
    def test_copy_inclusion_evidence_enters_the_normal_discovery_and_read_scope(self) -> None:
        client = FakeClient([
            json_action("inspect_symbol", {"name": "OUT-AMOUNT", "program_name": "TESTPROG"}),
            json_action("read_evidence", {"evidence_ids": ["ev_INCLUDE"]}),
            json_action("abstain", {
                "claims": [],
                "evidence_ids": ["ev_INCLUDE"],
                "boundaries": ["尚未检查完整数据来源。"],
            }),
        ])
        result = BoundedAgentLoop(client, CopyInspectionTools()).run("字段定义来自哪里？")
        self.assertEqual(result["stop_reason"], "model_abstained")
        self.assertEqual(result["tool_calls_used"], 2)
        self.assertEqual(result["discovered_evidence_ids"], ["ev_DEFINITION", "ev_INCLUDE"])
        self.assertEqual(result["verified_evidence_ids"], ["ev_INCLUDE"])
        self.assertEqual(result["evidence_ids"], ["ev_INCLUDE"])
        projected = result["tool_trace"][0]["result"]["matches"][0]["definition"]
        self.assertEqual(projected["copy_binding"], copy_binding())

    def test_copy_binding_is_a_bounded_exact_projection(self) -> None:
        self.assertEqual(_project_copy_binding(copy_binding()), copy_binding())
        invalid = [
            {**copy_binding(), "source_text": "SOURCE-CANARY"},
            {**copy_binding(), "kind": "free_form_binding"},
            {"kind": "copybook_expansion"},
            {**copy_binding(), "inclusion_evidence_refs": []},
            {**copy_binding(), "inclusion_evidence_refs": [inclusion_ref()] * 9},
            {**copy_binding(), "inclusion_evidence_refs": [
                {**inclusion_ref(), "source_text": "SOURCE-CANARY"},
            ]},
            {**copy_binding(), "inclusion_evidence_refs": [
                {**inclusion_ref(), "relative_path": "../private.cbl"},
            ]},
        ]
        for binding in invalid:
            with self.subTest(binding=binding):
                with self.assertRaises(ToolResultPolicyError):
                    _project_copy_binding(binding)

    def test_invalid_copy_binding_stops_before_releasing_tool_result(self) -> None:
        client = FakeClient([
            json_action("inspect_symbol", {"name": "OUT-AMOUNT"}),
        ])
        tools = CopyInspectionTools({**copy_binding(), "source_text": "SOURCE-CANARY"})
        result = BoundedAgentLoop(client, tools).run("字段来源是什么？")
        self.assertEqual(result["stop_reason"], "tool_contract_mismatch")
        self.assertEqual(result["discovered_evidence_ids"], [])
        self.assertEqual(len(client.requests), 1)
        self.assertNotIn("SOURCE-CANARY", json.dumps(result))

    def test_sql_host_direction_metadata_retains_its_lineage_boundary(self) -> None:
        metadata = {
            "operation": "EXEC_SQL",
            "boundary": "sql_column_lineage_not_resolved",
        }
        self.assertEqual(_project_relation_metadata(metadata), metadata)
        with self.assertRaises(ToolResultPolicyError):
            _project_relation_metadata({**metadata, "sql_text": "SOURCE-CANARY"})

    def test_conservative_copy_and_condition_reasons_are_allowed(self) -> None:
        for reason in (
            "condition_syntax_not_supported",
            "copy_context_not_supported",
            "copy_form_not_supported",
            "copy_scope_incomplete",
        ):
            with self.subTest(reason=reason):
                self.assertEqual(
                    _project_relation_metadata({"boundary": reason}),
                    {"boundary": reason},
                )
                self.assertEqual(
                    _project_relation_metadata({"resolution_reason": reason}),
                    {"resolution_reason": reason},
                )
        with self.assertRaises(ToolResultPolicyError):
            _project_relation_metadata({"boundary": "UNTRUSTED-CANARY"})

    def test_condition_text_is_still_removed_from_structural_projection(self) -> None:
        projected = _project_relation_metadata({
            "condition": "INPUT-STATUS = 'A' OR INPUT-STATUS = 'B'",
            "outcome": "true",
            "control_kind": "IF",
            "boundary": "condition_syntax_not_supported",
        })
        self.assertEqual(projected, {
            "control_kind": "IF",
            "boundary": "condition_syntax_not_supported",
        })


if __name__ == "__main__":
    unittest.main()
