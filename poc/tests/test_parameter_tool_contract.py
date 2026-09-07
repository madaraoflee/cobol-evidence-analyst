from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_loop import BoundedAgentLoop, ToolResultPolicyError, _project_relation
from test_agent_loop import FakeClient, FakeTools, json_action


def ref(name: str, path: str = "programs/CALLER.cbl") -> dict[str, object]:
    return {"evidence_id": name, "relative_path": path, "start_line": 10, "end_line": 12}


def parameter_relation() -> dict[str, object]:
    return {
        "relation_id": "rel_PARAMETER",
        "relation_type": "PASSES_AS",
        "status": "confirmed",
        "source": {"entity_id": "unit_ACTUAL", "unit_type": "DataItem", "name": "WS-AMOUNT", "program_name": "CALLER"},
        "target": {"entity_id": "sym_FORMAL", "name": "LK-INPUT", "scope": "CALLEE"},
        "metadata": {
            "parameter_position": 2,
            "passing_mode": "REFERENCE",
            "callsite_id": "unit_CALL",
            "supporting_evidence_refs": [ref("ev_CALL"), ref("ev_SIGNATURE", "programs/CALLEE.cbl")],
        },
        "evidence_ref": ref("ev_CALL"),
    }


class ParameterInspectionTools(FakeTools):
    def inspect_symbol(self, name: str, **kwargs) -> dict[str, object]:
        result = super().inspect_symbol(name, **kwargs)
        result.update({
            "status": "OK", "match_count": 1,
            "matches": [{
                "symbol": {"symbol_id": "sym_ACTUAL", "symbol_type": "Field", "name": "WS-AMOUNT", "program_name": "CALLER", "qualified_name": "CALLER::WS-AMOUNT"},
                "definition": {"unit_id": "unit_ACTUAL", "unit_type": "DataItem", "evidence_ref": ref("ev_ACTUAL")},
                "scope_unit_count": 1,
                "incoming_relations": [], "outgoing_relations": [parameter_relation()],
            }],
        })
        return result


class ParameterToolContractTests(unittest.TestCase):
    def test_reference_content_and_value_mapping_metadata_survive_projection(self) -> None:
        for mode in ("REFERENCE", "CONTENT", "VALUE"):
            with self.subTest(mode=mode):
                edge = parameter_relation()
                edge["metadata"]["passing_mode"] = mode
                self.assertEqual(_project_relation(edge, allow_depth=False), edge)

    def test_writeback_is_always_reference_candidate_with_runtime_boundary(self) -> None:
        edge = parameter_relation()
        edge.update(relation_type="MAY_WRITE_BACK", status="candidate")
        edge["metadata"]["boundary"] = "runtime_writeback_not_proven"
        self.assertEqual(_project_relation(edge, allow_depth=False), edge)
        for mode, status, boundary in (
            ("CONTENT", "candidate", "runtime_writeback_not_proven"),
            ("VALUE", "candidate", "runtime_writeback_not_proven"),
            ("REFERENCE", "confirmed", "runtime_writeback_not_proven"),
            ("REFERENCE", "candidate", None),
        ):
            with self.subTest(mode=mode, status=status, boundary=boundary):
                bad = copy.deepcopy(edge)
                bad["status"] = status
                bad["metadata"]["passing_mode"] = mode
                bad["metadata"]["boundary"] = boundary
                with self.assertRaises(ToolResultPolicyError):
                    _project_relation(bad, allow_depth=False)

    def test_parameter_position_and_member_index_are_strictly_bounded(self) -> None:
        for key, values in (
            ("parameter_position", [True, 0, 65, "2", 1.5]),
            ("group_member_index", [True, 0, 257, "2"]),
        ):
            for value in values:
                with self.subTest(key=key, value=value):
                    edge = parameter_relation()
                    edge["metadata"][key] = value
                    with self.assertRaises(ToolResultPolicyError):
                        _project_relation(edge, allow_depth=False)

    def test_missing_or_fabricated_supporting_reference_shape_is_rejected(self) -> None:
        for refs in (
            [], [ref("ev_SIGNATURE")], [ref("ev_CALL")] * 9,
            [ref("ev_CALL"), ref("ev_CALL")],
            [ref("ev_CALL", "../outside.cbl")],
            [{**ref("ev_CALL"), "source_text": "SOURCE-CANARY"}],
        ):
            with self.subTest(refs=refs):
                edge = parameter_relation()
                edge["metadata"]["supporting_evidence_refs"] = refs
                with self.assertRaises(ToolResultPolicyError):
                    _project_relation(edge, allow_depth=False)

    def test_mapping_requires_field_endpoints_and_callsite_provenance(self) -> None:
        variants = []
        edge = parameter_relation()
        del edge["metadata"]["callsite_id"]
        variants.append(edge)
        edge = parameter_relation()
        edge["source"]["unit_type"] = "Statement"
        variants.append(edge)
        edge = parameter_relation()
        edge["target"]["entity_id"] = None
        variants.append(edge)
        edge = parameter_relation()
        edge["status"] = "candidate"
        variants.append(edge)
        for edge in variants:
            with self.subTest(edge=edge):
                with self.assertRaises(ToolResultPolicyError):
                    _project_relation(edge, allow_depth=False)

    def test_signature_evidence_enters_bounded_agent_discovery_before_read(self) -> None:
        client = FakeClient([
            json_action("inspect_symbol", {"name": "WS-AMOUNT", "program_name": "CALLER"}),
            json_action("read_evidence", {"evidence_ids": ["ev_SIGNATURE"]}),
            json_action("abstain", {"claims": [], "evidence_ids": ["ev_SIGNATURE"], "boundaries": ["参数位置不代表已证明完整执行路径。"]}),
        ])
        result = BoundedAgentLoop(client, ParameterInspectionTools()).run("参数传给了哪个字段？")
        self.assertEqual(result["stop_reason"], "model_abstained")
        self.assertIn("ev_SIGNATURE", result["discovered_evidence_ids"])
        self.assertEqual(result["verified_evidence_ids"], ["ev_SIGNATURE"])
        self.assertEqual(result["question_coverage"]["status"], "not_assessed")


if __name__ == "__main__":
    unittest.main()
