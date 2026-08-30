from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from investigation_tools import (  # noqa: E402
    InvestigationTools,
    tool_definitions,
)
from run_demo import build_demo_bundle  # noqa: E402
from structural_index import build_structural_index  # noqa: E402


FIXTURE_ROOT = POC_ROOT / "fixtures" / "synthetic-insurance-v1"


class InvestigationToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temporary.name) / "structural.sqlite"
        cls.build_report = build_structural_index(
            FIXTURE_ROOT, cls.database, quiet=True
        )
        cls.tools = InvestigationTools(cls.database)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def test_fixture_is_a_real_multi_program_snapshot(self) -> None:
        self.assertEqual(self.build_report["files"]["decoded"], 13)
        self.assertGreaterEqual(
            self.build_report["database_counts"]["code_units"], 250
        )
        self.assertGreaterEqual(
            self.build_report["database_counts"]["relations"], 400
        )
        self.assertFalse(self.build_report["privacy"]["network_calls"])

    def test_search_prioritizes_exact_symbols_without_leaking_source(self) -> None:
        result = self.tools.search_code(
            "OUT-INSTALMENT-PREMIUM WS-MODE-FACTOR", limit=8
        )

        self.assertIn(result["status"], {"OK", "PARTIAL"})
        self.assertEqual(result["hits"][0]["match_type"], "exact_symbol")
        self.assertEqual(
            result["hits"][0]["name"], "OUT-INSTALMENT-PREMIUM"
        )
        self.assertTrue(result["evidence_refs"])
        self.assertNotIn("source_text", str(result))

    def test_search_requires_a_code_anchor(self) -> None:
        result = self.tools.search_code("分期保费是怎样计算的？")

        self.assertEqual(result["status"], "NOT_FOUND")
        self.assertEqual(result["diagnostics"][0]["code"], "NO_CODE_ANCHOR")

    def test_inspect_field_finds_formula_and_error_reset_writers(self) -> None:
        result = self.tools.inspect_symbol("OUT-INSTALMENT-PREMIUM")

        self.assertEqual(result["status"], "OK")
        incoming = result["matches"][0]["incoming_relations"]
        writers = [
            relation
            for relation in incoming
            if relation["relation_type"] == "WRITES"
        ]
        programs = {relation["source"]["program_name"] for relation in writers}
        expressions = {
            relation["metadata"].get("expression") for relation in writers
        }
        self.assertEqual(programs, {"SYNP040", "SYNP090"})
        self.assertIn(
            "WS-ANNUAL-PREMIUM * WS-MODE-FACTOR", expressions
        )
        self.assertIn("ZERO", expressions)

    def test_inspect_program_exposes_performs_tables_and_rounding(self) -> None:
        result = self.tools.inspect_symbol(
            "SYNP040", symbol_type="Program", max_relations=80
        )

        self.assertEqual(result["status"], "OK")
        outgoing = result["matches"][0]["outgoing_relations"]
        relation_types = {relation["relation_type"] for relation in outgoing}
        table_names = {
            relation["target"]["name"]
            for relation in outgoing
            if relation["relation_type"] == "SELECTS_FROM"
        }
        rounded_expressions = {
            relation["metadata"].get("expression")
            for relation in outgoing
            if relation["metadata"].get("rounded")
        }
        self.assertIn("PERFORMS", relation_types)
        self.assertEqual(
            table_names,
            {
                "SYN_OCC_LOAD",
                "SYN_DISCOUNT",
                "SYN_MODE_FACTOR",
                "SYN_POLICY_FEE",
            },
        )
        self.assertIn(
            "WS-ANNUAL-PREMIUM * WS-MODE-FACTOR",
            rounded_expressions,
        )

    def test_ambiguous_symbol_requires_refinement(self) -> None:
        result = self.tools.inspect_symbol("SQLCODE")

        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertGreater(result["match_count"], 1)
        self.assertEqual(result["diagnostics"][0]["code"], "AMBIGUOUS_SYMBOL")

    def test_call_trace_is_bounded_and_keeps_dynamic_call_unresolved(self) -> None:
        result = self.tools.trace_relations(
            "SYNP000",
            symbol_type="Program",
            relation_types=["CALLS", "CALL_TARGET_FROM"],
            max_depth=3,
            max_edges=30,
        )

        self.assertEqual(result["status"], "OK")
        call_targets = {
            edge["target"]["name"]
            for edge in result["edges"]
            if edge["relation_type"] == "CALLS"
        }
        self.assertEqual(
            call_targets,
            {"SYNP010", "SYNP020", "SYNP030", "SYNP040", "SYNP090"},
        )
        dynamic = [
            edge
            for edge in result["edges"]
            if edge["relation_type"] == "CALL_TARGET_FROM"
        ]
        self.assertEqual(len(dynamic), 1)
        self.assertEqual(dynamic[0]["status"], "unresolved")
        self.assertEqual(
            dynamic[0]["target"]["name"], "LK-CALCULATOR-PROGRAM"
        )
        self.assertEqual(result["boundaries"][0]["status"], "unresolved")

    def test_table_trace_reports_missing_database_definitions_as_boundaries(self) -> None:
        result = self.tools.trace_relations(
            "SYNP040",
            symbol_type="Program",
            relation_types=["SELECTS_FROM"],
            max_depth=1,
        )

        self.assertEqual(result["edge_count"], 4)
        self.assertEqual(len(result["boundaries"]), 4)
        self.assertTrue(
            all(
                boundary["reason"] == "database_definition_not_indexed"
                for boundary in result["boundaries"]
            )
        )

    def test_trace_rejects_unapproved_relation_types(self) -> None:
        with self.assertRaises(ValueError):
            self.tools.trace_relations(
                "SYNP000", relation_types=["ARBITRARY_SQL_EDGE"]
            )

    def test_evidence_can_only_be_read_by_id_and_is_marked_untrusted(self) -> None:
        search = self.tools.search_code("OUT-INSTALMENT-PREMIUM", limit=6)
        formula_ref = next(
            hit["evidence_ref"]
            for hit in search["hits"]
            if hit["evidence_ref"]["relative_path"]
            == "programs/SYNP040.cbl"
        )
        result = self.tools.read_evidence([formula_ref["evidence_id"]])

        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["spans"][0]["integrity"], "VALID")
        self.assertEqual(
            result["spans"][0]["content_type"], "UNTRUSTED_SOURCE_TEXT"
        )
        self.assertIn(
            "OUT-INSTALMENT-PREMIUM", result["spans"][0]["source_text"]
        )
        with self.assertRaises(ValueError):
            self.tools.read_evidence([])

    def test_missing_or_over_budget_evidence_is_explicitly_partial(self) -> None:
        search = self.tools.search_code("SYNP040", limit=4)
        evidence_id = search["evidence_refs"][0]["evidence_id"]
        result = self.tools.read_evidence(
            [evidence_id, "ev_missing"], max_chars=200
        )

        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["missing_evidence_ids"], ["ev_missing"])

    def test_tool_schemas_expose_only_the_four_bounded_tools(self) -> None:
        definitions = tool_definitions()
        names = {
            definition["function"]["name"] for definition in definitions
        }

        self.assertEqual(
            names,
            {
                "search_code",
                "inspect_symbol",
                "trace_relations",
                "read_evidence",
            },
        )
        read_schema = next(
            definition["function"]["parameters"]
            for definition in definitions
            if definition["function"]["name"] == "read_evidence"
        )
        self.assertNotIn("path", read_schema["properties"])
        self.assertFalse(read_schema["additionalProperties"])

    def test_six_step_demo_builds_a_supported_evidence_package(self) -> None:
        demo_database = Path(self.temporary.name) / "demo.sqlite"
        bundle = build_demo_bundle(FIXTURE_ROOT, demo_database)

        self.assertEqual(bundle["tool_budget"]["calls_used"], 6)
        self.assertFalse(bundle["privacy"]["network_calls"])
        self.assertEqual(
            bundle["answer_preview"]["support_status"],
            "SUPPORTED_WITH_BOUNDARIES",
        )
        self.assertEqual(
            bundle["answer_preview"]["instalment_expression"],
            "WS-ANNUAL-PREMIUM * WS-MODE-FACTOR",
        )
        self.assertIn(
            "SYN_CALC_ROUTING",
            bundle["answer_preview"]["external_configuration_tables"],
        )
        self.assertTrue(bundle["answer_preview"]["dynamic_call_boundaries"])
        self.assertEqual(bundle["answer_preview"]["evidence_span_count"], 12)


if __name__ == "__main__":
    unittest.main()
