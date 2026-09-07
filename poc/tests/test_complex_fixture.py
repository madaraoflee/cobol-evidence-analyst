from __future__ import annotations

from contextlib import closing
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from investigation_tools import InvestigationTools
from structural_index import build_structural_index


FIXTURE_ROOT = POC_ROOT / "fixtures" / "complex-business-v2"
MAIN_ROOT = FIXTURE_ROOT / "main"


class ComplexBusinessFixtureTests(unittest.TestCase):
    """Fixture and indexed-source checks; these do not execute COBOL."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temporary.name) / "main.sqlite"
        cls.report = build_structural_index(MAIN_ROOT, cls.database, quiet=True)
        cls.profile = json.loads((FIXTURE_ROOT / "profile.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def rows(self, sql: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return list(connection.execute(sql, parameters))

    def program_text(self, name: str) -> str:
        return (MAIN_ROOT / "programs" / f"{name}.cbl").read_text(encoding="utf-8")

    def test_main_corpus_is_large_diverse_and_isolated_from_negative_cases(self) -> None:
        names = {row[0] for row in self.rows("SELECT name FROM symbols WHERE symbol_type = 'Program'")}
        self.assertEqual(len(names), 14)
        self.assertEqual(names, {item["program_name"] for item in self.profile["contracts"]})
        self.assertEqual(self.report["files"]["candidate"], 18)
        self.assertGreater(self.report["database_counts"]["relations"], 700)
        self.assertEqual(self.report["copy_expansion"]["incomplete_scopes"], 0)
        self.assertTrue((FIXTURE_ROOT / "boundaries" / "BRECURS.cbl").is_file())
        self.assertNotIn("BRECURS", names)
        fixed = [path for path in (MAIN_ROOT / "programs").glob("*.cbl")
                 if path.read_text(encoding="utf-8").startswith("000100 ")]
        self.assertEqual(len(fixed), 4)
        for path in fixed:
            self.assertTrue(all(len(line) <= 72 for line in path.read_text(encoding="utf-8").splitlines()))

    def test_five_program_static_spine_is_backed_by_resolved_source_calls(self) -> None:
        calls = {(row["program_name"], row["target_name"])
                 for row in self.rows(
                     "SELECT u.program_name, r.target_name FROM relations r "
                     "JOIN code_units u ON u.unit_id = r.from_entity_id "
                     "WHERE r.relation_type = 'CALLS' AND r.status = 'confirmed'")}
        spine = self.profile["static_spine"]
        self.assertEqual(len(spine), 5)
        for caller, callee in zip(spine, spine[1:]):
            self.assertIn((caller, callee), calls)
        self.assertIn(("ITEMSUM", "ITEMCALC"), calls)
        self.assertIn(("ADJUST", "FACTORLK"), calls)
        self.assertIn(("ADJUST", "FEELOOK"), calls)

    def test_working_storage_is_not_mistaken_for_unbound_callee_local_state(self) -> None:
        entry = self.program_text("TXNENTRY")
        self.assertLess(entry.index("WORKING-STORAGE SECTION"), entry.index("COPY CALCAREA"))
        self.assertLess(entry.index("COPY CALCAREA"), entry.index("LINKAGE SECTION"))
        core = self.program_text("TXNCORE")
        self.assertLess(core.index("LINKAGE SECTION"), core.index("COPY CALCAREA"))
        tools = InvestigationTools(self.database)
        first = tools.inspect_symbol("PROCESS-STATUS", program_name="TXNENTRY")["matches"][0]
        second = tools.inspect_symbol("PROCESS-STATUS", program_name="TXNCORE")["matches"][0]
        self.assertNotEqual(first["symbol"]["symbol_id"], second["symbol"]["symbol_id"])
        self.assertEqual(first["definition"]["evidence_ref"]["relative_path"], "copybooks/ERRAREA.cpy")
        self.assertEqual(second["definition"]["evidence_ref"]["relative_path"], "copybooks/ERRAREA.cpy")
        for copybook in (MAIN_ROOT / "copybooks").glob("*.cpy"):
            self.assertNotIn("OCCURS", copybook.read_text(encoding="utf-8"))

    def test_renamed_elementary_arguments_and_three_passing_modes_are_real_source(self) -> None:
        base = self.program_text("BASECALC")
        rate = self.program_text("RATELOOK")
        date = self.program_text("DATECHK")
        self.assertIn("INPUT-PRODUCT INPUT-DATE", base)
        self.assertIn("BY REFERENCE CALC-BASE-RATE ERROR-AREA", base)
        self.assertIn("PROCEDURE DIVISION USING RATE-PRODUCT RATE-DATE", rate)
        self.assertIn("RATE-OUTPUT ERROR-AREA", rate)
        self.assertIn("CALL 'DATECHK' USING BY VALUE WS-DATE-VALUE", rate)
        self.assertIn("01 WS-DATE-VALUE PIC 9(8) COMP-5", rate)
        self.assertIn("01 DATE-INPUT PIC 9(8) COMP-5", date)
        self.assertIn("PROCEDURE DIVISION USING BY VALUE DATE-INPUT", date)
        self.assertIn("BY CONTENT REQUEST-AREA", self.program_text("TXNENTRY"))

    def test_dynamic_configuration_writer_does_not_fabricate_a_static_target(self) -> None:
        dynamic = self.rows(
            "SELECT r.*, u.program_name FROM relations r "
            "JOIN code_units u ON u.unit_id = r.from_entity_id "
            "WHERE r.relation_type = 'CALL_TARGET_FROM'")
        self.assertEqual(len(dynamic), 1)
        call = dynamic[0]
        self.assertEqual(call["program_name"], "TXNCORE")
        self.assertEqual(call["target_name"], "CALC-ROUTE-PROGRAM")
        self.assertEqual(json.loads(call["metadata_json"])["boundary"], "runtime_target_requires_value_flow")
        self.assertEqual(self.rows(
            "SELECT * FROM relations WHERE from_entity_id = ? AND relation_type = 'CALLS'",
            (call["from_entity_id"],)), [])
        route_write = self.rows(
            "SELECT r.*, u.name, u.normalized_text FROM relations r "
            "JOIN code_units u ON u.unit_id = r.from_entity_id "
            "WHERE u.program_name = 'ROUTESEL' AND r.relation_type = 'WRITES' "
            "AND r.target_name = 'ROUTE-OUTPUT' AND u.name = 'EXEC_SQL'")
        self.assertEqual(len(route_write), 1)
        self.assertIn("FROM ROUTE_CONFIG", route_write[0]["normalized_text"])
        self.assertIn("ON EXCEPTION", self.program_text("TXNCORE"))
        self.assertIn("MOVE 92 TO PROCESS-STATUS", self.program_text("TXNCORE"))

    def test_each_configuration_lookup_indexes_host_inputs_output_and_failure_branch(self) -> None:
        cases = (
            ("RATELOOK", "RATE-OUTPUT", "RATE-PRODUCT", "RATE-DATE"),
            ("FACTORLK", "FACTOR-OUTPUT", "FACTOR-MODE", "FACTOR-DATE"),
            ("FEELOOK", "FEE-OUTPUT", "FEE-PRODUCT", "FEE-DATE"),
            ("ROUTESEL", "ROUTE-OUTPUT", "ROUTE-PRODUCT", "ROUTE-DATE"),
        )
        for program, output, key, date in cases:
            with self.subTest(program=program):
                relations = self.rows(
                    "SELECT r.relation_type, r.target_name, r.status FROM relations r "
                    "JOIN code_units u ON u.unit_id = r.from_entity_id "
                    "WHERE u.program_name = ? AND u.name = 'EXEC_SQL'", (program,))
                actual = {(row["relation_type"], row["target_name"], row["status"]) for row in relations}
                self.assertIn(("WRITES", output, "confirmed"), actual)
                self.assertIn(("READS", key, "confirmed"), actual)
                self.assertIn(("READS", date, "confirmed"), actual)
                text = self.program_text(program)
                self.assertIn("IF SQLCODE NOT = ZERO", text)
                for branch in ("WHEN 100", "WHEN -811", "WHEN OTHER"):
                    self.assertIn(branch, text)
                self.assertIn("EFFECTIVE_FROM <=", text)
                self.assertIn("EFFECTIVE_TO >=", text)

    def test_subroutine_range_and_bounded_active_item_loop_remain_visible(self) -> None:
        ranges = self.rows(
            "SELECT r.* FROM relations r JOIN code_units u ON u.unit_id = r.from_entity_id "
            "WHERE u.program_name = 'TXNCORE' AND r.relation_type = 'PERFORMS_THRU'")
        self.assertTrue(ranges)
        self.assertEqual(ranges[0]["target_name"], "VALIDATE-INPUT")
        self.assertEqual(json.loads(ranges[0]["metadata_json"])["range_end"], "VALIDATE-END")
        item = self.program_text("ITEMSUM")
        self.assertIn("PERFORM VARYING WS-ITEM-INDEX FROM 1 BY 1", item)
        self.assertIn("UNTIL WS-ITEM-INDEX > INPUT-ITEM-COUNT", item)
        self.assertIn("OR PROCESS-STATUS NOT = ZERO", item)
        self.assertIn("IF WS-ITEM-STATUS(WS-ITEM-INDEX) = 'A'", item)
        self.assertIn("ADD WS-ITEM-PREMIUM TO CALC-ITEM-TOTAL", item)
        self.assertIn("ON SIZE ERROR", item)
        self.assertTrue(any("element-sensitive" in boundary for boundary in self.profile["boundaries"]))

    def test_final_outputs_have_zero_writes_on_actual_nonzero_status_condition(self) -> None:
        rows = self.rows(
            "SELECT w.target_name, w.metadata_json, c.normalized_text, d.metadata_json AS control_json "
            "FROM relations w JOIN code_units s ON s.unit_id = w.from_entity_id "
            "JOIN relations d ON d.from_entity_id = s.unit_id AND d.relation_type = 'CONTROL_DEPENDS_ON' "
            "JOIN code_units c ON c.unit_id = d.target_entity_id "
            "WHERE s.program_name = 'RESULTMP' AND w.relation_type = 'WRITES'")
        cleared = set()
        for row in rows:
            if json.loads(row["metadata_json"]).get("expression") != "ZERO":
                continue
            self.assertIn("PROCESS-STATUS NOT = ZERO", row["normalized_text"])
            self.assertEqual(json.loads(row["control_json"])["outcome"], "true")
            cleared.add(row["target_name"])
        self.assertEqual(cleared, {"RESULT-AMOUNT", "RESULT-ANNUAL"})
        self.assertIn("MOVE PROCESS-STATUS TO RESULT-STATUS", self.program_text("RESULTMP"))
        self.assertIn("CALL 'RESULTMP' USING", self.program_text("TXNENTRY"))

    def test_source_derived_error_scenarios_have_real_status_and_error_literals(self) -> None:
        all_source = "\n".join(self.program_text(name) for name in (
            item["program_name"] for item in self.profile["contracts"]))
        scenarios = [item for item in self.profile["scenarios"] if item["expected"]["status"] != 0]
        self.assertGreaterEqual(len(scenarios), 20)
        for scenario in scenarios:
            with self.subTest(scenario=scenario["scenario_id"]):
                expected = scenario["expected"]
                self.assertIn(f"MOVE {expected['status']} TO ", all_source)
                self.assertIn(f"'{expected['error_code']}'", all_source)
                self.assertIn(f"'{expected['error_stage']}'", all_source)
                self.assertEqual(expected["amount"], "0.00")
                self.assertEqual(expected["annual"], "0.00")
                self.assertEqual(scenario["execution_status"], "not_executed")

    def test_every_main_call_has_exception_handler_and_mapper_has_independent_fallback(self) -> None:
        calls = self.rows("SELECT * FROM code_units WHERE unit_type = 'Statement' AND name = 'CALL'")
        self.assertEqual(len(calls), 12)
        for call in calls:
            with self.subTest(program=call["program_name"], line=call["start_line"]):
                self.assertIn("ON EXCEPTION", call["normalized_text"])
        entry = self.program_text("TXNENTRY")
        mapper_call = entry[entry.index("CALL 'RESULTMP'"):]
        self.assertIn("MOVE 93 TO PROCESS-STATUS RESULT-STATUS", mapper_call)
        self.assertIn("MOVE ZERO TO RESULT-AMOUNT RESULT-ANNUAL", mapper_call)
        self.assertIn("'MAPPER-UNAVAILABLE'", mapper_call)
        self.assertEqual(len([item for item in self.profile["scenarios"]
                              if "literal program cannot be loaded" in item["trigger"]]), 11)

    def test_negative_fixture_rejects_arity_subscripts_replacing_and_content_writeback(self) -> None:
        database = Path(self.temporary.name) / "boundaries.sqlite"
        report = build_structural_index(FIXTURE_ROOT / "boundaries", database, quiet=True)
        self.assertEqual(report["files"]["candidate"], 8)
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            bindings = {row["caller_program"]: row for row in connection.execute("SELECT * FROM call_bindings")}
            for program, reason in (("BARGCALL", "parameter_arity_mismatch"),
                                    ("BSUBCALL", "call_form_not_supported"),
                                    ("BREPL", "copy_scope_incomplete")):
                self.assertEqual(bindings[program]["status"], "unresolved")
                self.assertEqual(bindings[program]["reason"], reason)
            content = bindings["BCONTENT"]
            self.assertEqual(content["status"], "confirmed")
            self.assertEqual(content["passing_mode"], "CONTENT")
            self.assertEqual(list(connection.execute(
                "SELECT * FROM relations WHERE relation_type = 'MAY_WRITE_BACK' "
                "AND from_entity_id = (SELECT definition_unit_id FROM symbols WHERE symbol_id = ?)",
                (content["callee_symbol_id"],))), [])
            recursive = bindings["BRECURS"]
            self.assertEqual(recursive["status"], "confirmed")
            self.assertEqual(recursive["caller_program"], recursive["callee_program"])
            self.assertEqual(len(bindings), 5)

    def test_main_binding_proves_correspondence_not_successful_writeback(self) -> None:
        bindings = self.rows("SELECT * FROM call_bindings")
        self.assertGreater(len([row for row in bindings if row["status"] == "confirmed"]), 150)
        unresolved = [row for row in bindings if row["status"] == "unresolved"]
        self.assertEqual({row["reason"] for row in unresolved}, {"dynamic_target_not_resolved"})
        self.assertEqual(len(unresolved), 1)
        writebacks = self.rows("SELECT * FROM relations WHERE relation_type = 'MAY_WRITE_BACK'")
        self.assertTrue(writebacks)
        self.assertEqual({row["status"] for row in writebacks}, {"candidate"})

    def test_hand_calculated_examples_are_consistent_but_not_runtime_verification(self) -> None:
        baseline = self.profile["baseline"]
        request, values = baseline["request"], baseline["lookup_values"]
        cents = lambda value: value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        base = cents(Decimal(request["sum"]) / 1000 * Decimal(values["base_rate"]))
        loading = cents(base * Decimal(request["load_percent"]) / 100)
        discount = cents(base * Decimal(request["discount_percent"]) / 100)
        items = sum((cents(Decimal(item["amount"]) * Decimal("1.25"))
                     for item in request["items"] if item["status"] == "A"), Decimal(0))
        annual = base + loading + items - discount + Decimal(values["annual_fee"])
        instalment = cents(annual * Decimal(values["mode_factor"]))
        for name, actual in (("base", base), ("loading", loading), ("discount", discount),
                             ("active_item_total", items), ("annual", annual), ("instalment", instalment)):
            self.assertEqual(actual, Decimal(baseline["component_expectations"][name]))
        alternate = cents((annual - Decimal(values["annual_fee"])) * Decimal("1.02")
                          + Decimal(values["annual_fee"]))
        self.assertEqual(alternate, Decimal("304.74"))
        self.assertEqual(cents(alternate * Decimal(values["mode_factor"])), Decimal("27.43"))
        self.assertEqual(self.profile["execution_status"], "not_compiled_or_executed")
        self.assertNotIn("full_business_analysis_verified", self.profile)

    def test_selected_index_evidence_matches_original_physical_lines_and_hash(self) -> None:
        evidence = self.rows(
            "SELECT DISTINCT e.* FROM evidence_spans e JOIN code_units u ON u.evidence_id = e.evidence_id "
            "WHERE u.program_name IN ('BASECALC', 'RATELOOK', 'RESULTMP') "
            "AND u.unit_type = 'Statement'")
        self.assertTrue(evidence)
        for row in evidence:
            raw = (MAIN_ROOT / row["relative_path"]).read_bytes()
            self.assertEqual(row["source_sha256"], hashlib.sha256(raw).hexdigest())
            span = "\n".join(raw.decode("utf-8").splitlines()[row["start_line"] - 1:row["end_line"]])
            self.assertEqual(row["text"], span)


if __name__ == "__main__":
    unittest.main()
