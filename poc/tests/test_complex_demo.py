from __future__ import annotations

import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import unittest


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from agent_loop import _canonical_tool_result
from complex_demo import FIXTURE_ROOT, _call_paths, _writer_candidates, build_complex_demo, render_markdown
from error_paths import ErrorContract
from investigation_tools import InvestigationTools


class ComplexDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temporary.name) / "complex.sqlite"
        profile = json.loads((FIXTURE_ROOT / "profile.json").read_text(encoding="utf-8"))
        contracts = [ErrorContract(item["program_name"], tuple(item["status_fields"]), tuple(item.get("output_fields", ()))) for item in profile["contracts"]]
        cls.bundle = build_complex_demo(FIXTURE_ROOT / "main", cls.database, profile["root_program"], contracts)
        cls.tools = InvestigationTools(cls.database)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_report_contains_five_program_chain_and_stays_offline(self) -> None:
        self.assertEqual(len(self.bundle["programs"]), 14)
        self.assertIn(["TXNENTRY", "TXNCORE", "BASECALC", "RATELOOK", "DATECHK"], [path["programs"] for path in self.bundle["call_paths"]["paths"]])
        for flag in ("full_business_analysis_verified", "runtime_execution_tested", "model_called", "network_calls"):
            self.assertFalse(self.bundle[flag])
        self.assertEqual(self.bundle["error_audit"]["snapshot_id"], self.bundle["snapshot_id"])

    def test_dynamic_program_field_reaches_config_sql_without_fabricating_target(self) -> None:
        calls = self.bundle["dynamic_calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["target_field"], "CALC-ROUTE-PROGRAM")
        sources = calls[0]["source_candidates"]
        self.assertFalse(sources["actual_runtime_target_verified"])
        writers = [row for row in sources["writers"] if row["program_name"] == "ROUTESEL" and row["statement_name"] == "EXEC_SQL"]
        self.assertEqual(len(writers), 1)
        self.assertEqual(writers[0]["table_names"], ["ROUTE_CONFIG"])
        self.assertEqual(writers[0]["input_fields"], ["ROUTE-DATE", "ROUTE-PRODUCT"])
        self.assertFalse(any(edge["callee"] in {"CALCSTD", "CALCALT"} for edge in self.bundle["static_calls"]))

    def test_actual_parameter_tools_pass_strict_projection_with_supporting_evidence(self) -> None:
        raw = self.tools.inspect_symbol("CALC-ROUTE-PROGRAM", program_name="TXNCORE", max_relations=100)
        projected = _canonical_tool_result("inspect_symbol", raw)
        edges = projected["matches"][0]["incoming_relations"]
        mappings = [edge for edge in edges if edge["relation_type"] == "MAY_WRITE_BACK"]
        self.assertTrue(mappings)
        self.assertTrue(any(edge["source"]["program_name"] == "ROUTESEL" for edge in mappings))
        for edge in mappings:
            self.assertEqual(edge["status"], "candidate")
            self.assertEqual(edge["metadata"]["passing_mode"], "REFERENCE")
            self.assertIn(edge["evidence_ref"], edge["metadata"]["supporting_evidence_refs"])
        encoded = json.dumps(projected)
        self.assertNotIn("normalized_text", encoded)
        self.assertNotIn("source_text", encoded)

    def test_incoming_parameter_trace_keeps_field_granularity(self) -> None:
        raw = self.tools.trace_relations("CALC-ROUTE-PROGRAM", program_name="TXNCORE", direction="incoming", relation_types=["MAY_WRITE_BACK"], max_depth=2, max_edges=50)
        result = _canonical_tool_result("trace_relations", raw)
        entities = result["visited_entities"]
        self.assertTrue(any(item["name"] == "ROUTE-OUTPUT" and item["program_name"] == "ROUTESEL" for item in entities))
        self.assertTrue(all(item["entity_type"] == "Field" for item in entities))

    def test_read_only_parameter_modes_do_not_expose_writeback_edges(self) -> None:
        raw = self.tools.inspect_symbol("RATE-PRODUCT", program_name="RATELOOK", max_relations=100)
        result = _canonical_tool_result("inspect_symbol", raw)
        match = result["matches"][0]
        self.assertTrue(any(edge["relation_type"] == "PASSES_AS" and edge["metadata"]["passing_mode"] == "CONTENT" for edge in match["incoming_relations"]))
        self.assertFalse(any(edge["relation_type"] == "MAY_WRITE_BACK" for edge in match["outgoing_relations"]))

    def test_graph_cycles_and_breadth_have_explicit_budgets(self) -> None:
        def edge(a: str, b: str) -> dict[str, object]:
            return {"caller": a, "callee": b, "evidence_ref": {"evidence_id": f"ev_{a}_{b}"}}
        cycle = _call_paths([edge("ROOT", "CHILD"), edge("CHILD", "ROOT")], "ROOT")
        self.assertEqual(cycle["cycles"][0]["programs"], ["ROOT", "CHILD", "ROOT"])
        large = _call_paths([edge("ROOT", f"LEAF{i}") for i in range(1100)], "ROOT")
        self.assertTrue(large["truncated"])
        self.assertLessEqual(len(large["paths"]), 100)
        many_cycles = _call_paths([edge("ROOT", "ROOT") for _ in range(1100)], "ROOT")
        self.assertLessEqual(len(many_cycles["cycles"]), 100)
        self.assertTrue(many_cycles["truncated"])

    def test_single_field_many_writers_respects_result_and_work_budgets(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript("""
                CREATE TABLE code_units(unit_id TEXT, program_name TEXT, name TEXT);
                CREATE TABLE evidence_spans(evidence_id TEXT, relative_path TEXT, start_line INTEGER, end_line INTEGER);
                CREATE TABLE relations(relation_id TEXT, from_entity_id TEXT, target_entity_id TEXT, relation_type TEXT, target_name TEXT, status TEXT, evidence_id TEXT);
                INSERT INTO code_units VALUES ('unit_WRITE', 'WRITER', 'MOVE');
                INSERT INTO evidence_spans VALUES ('ev_WRITE', 'programs/WRITER.cbl', 1, 1);
            """)
            connection.executemany("INSERT INTO relations VALUES (?, 'unit_WRITE', 'sym_FIELD', 'WRITES', 'OUT-AMOUNT', 'confirmed', 'ev_WRITE')", [(f"rel_{number}",) for number in range(2500)])
            result = _writer_candidates(connection, "sym_FIELD")
            self.assertLessEqual(len(result["writers"]), 200)
            self.assertTrue(result["truncated"])
        finally:
            connection.close()

    def test_rendered_result_contains_actual_examples_and_scope(self) -> None:
        output = render_markdown(self.bundle)
        self.assertIn("TXNENTRY → TXNCORE → BASECALC → RATELOOK → DATECHK", output)
        self.assertIn("ROUTE_CONFIG", output)
        self.assertIn("可能", output)
        self.assertIn("不代表已经运行", output)
        self.assertIn("VALUE", output)


if __name__ == "__main__":
    unittest.main()
