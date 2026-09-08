from __future__ import annotations

from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from structural_index import build_structural_index


FIXTURE_ROOT = POC_ROOT / "fixtures" / "exception-flow-v4"
MAIN_ROOT = FIXTURE_ROOT / "main"


class ExceptionFlowFixtureTests(unittest.TestCase):
    """Source-oracle checks only; no execution or full CFG claim is made here."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temporary.name) / "main.sqlite"
        cls.report = build_structural_index(MAIN_ROOT, cls.database, quiet=True)
        cls.profile = json.loads((FIXTURE_ROOT / "profile.json").read_text(encoding="utf-8"))
        cls.regression = json.loads((FIXTURE_ROOT / "regressions" / "profile.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def rows(self, query: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return list(connection.execute(query, parameters))

    def source(self, program: str) -> str:
        return (MAIN_ROOT / "programs" / f"{program}.cbl").read_text(encoding="utf-8")

    def test_positive_main_is_separate_from_regression_and_boundary_sources(self) -> None:
        names = {row[0] for row in self.rows("SELECT name FROM symbols WHERE symbol_type = 'Program'")}
        self.assertEqual(names, {item["program_name"] for item in self.profile["contracts"]})
        self.assertEqual(len(names), 5)
        self.assertEqual(self.report["files"]["candidate"], 5)
        self.assertNotIn("EXBAD", names)
        self.assertNotIn("EXUNKNOWN", names)
        self.assertEqual(self.profile["source_root"], "main")
        self.assertTrue((FIXTURE_ROOT / "regressions" / "programs" / "EXBAD.cbl").is_file())

    def test_fixed_scalar_parameter_correspondences_are_supported(self) -> None:
        self.assertEqual(self.report["call_bindings"]["callsites"], 6)
        self.assertEqual(self.report["call_bindings"]["confirmed_bindings"], 23)
        self.assertEqual(self.report["call_bindings"]["writeback_candidates"], 12)
        self.assertEqual(self.report["call_bindings"]["boundary_counts"], {})
        for row in self.rows("SELECT status, passing_mode FROM call_bindings"):
            self.assertEqual(row["status"], "confirmed")
            self.assertIn(row["passing_mode"], {"CONTENT", "REFERENCE"})

    def test_repeated_wrapper_calls_keep_different_root_fields_and_one_nested_callsite(self) -> None:
        bindings = self.rows(
            "SELECT b.*, caller.name AS caller_field, callee.name AS callee_field "
            "FROM call_bindings b JOIN symbols caller ON caller.symbol_id = b.caller_symbol_id "
            "JOIN symbols callee ON callee.symbol_id = b.callee_symbol_id")
        for branch in self.profile["expected_branch_mappings"]:
            subset = [row for row in bindings if row["caller_program"] == branch["caller_program"]
                      and row["callee_program"] == branch["callee_program"]]
            for role, callee_field in (("input_field", "WRAP-INPUT"), ("output_field", "WRAP-OUTPUT"),
                                       ("status_field", "WRAP-STATUS")):
                self.assertEqual(len([row for row in subset if row["caller_field"] == branch[role]
                                      and row["callee_field"] == callee_field]), 1)
        nested = [row for row in bindings if row["caller_program"] == "EXWRAP"]
        self.assertEqual({row["callee_program"] for row in nested}, {"EXLEAF"})
        self.assertEqual(len({row["callsite_id"] for row in nested}), 1)
        self.assertEqual(self.profile["expected_context_counts"]["EXWRAP"], 2)
        self.assertEqual(self.profile["expected_context_counts"]["EXLEAF"], 2)
        self.assertEqual(sum(self.profile["expected_context_counts"].values()), 8)

    def test_failure_event_anchors_select_unique_indexed_source_statements(self) -> None:
        expectations = self.profile["local_exceptional_expectations"]
        self.assertEqual(len(expectations), 3)
        self.assertEqual({item["event_kind"] for item in expectations}, {"exception", "size_error"})
        for item in expectations:
            with self.subTest(expectation=item["expectation_id"]):
                anchor = item["anchor"]
                candidates = self.rows(
                    "SELECT * FROM code_units WHERE program_name = ? AND name = ? "
                    "AND relative_path = ? AND unit_type = 'Statement'",
                    (item["program_name"], anchor["statement_name"], anchor["relative_path"]))
                selected = [row for row in candidates if anchor["source_text_contains"] in row["normalized_text"]]
                self.assertEqual(len(selected), 1)
                self.assertEqual(item["expected"], "zero_on_all_modeled_exits")
                text = self.source(item["program_name"])
                self.assertIn(f"MOVE {item['status_value']} TO {item['status_field']}", text)
                self.assertIn(f"MOVE ZERO TO {item['output_field']}", text)

    def test_wrapper_has_mutually_exclusive_call_and_arithmetic_source_clauses(self) -> None:
        source = self.source("EXWRAP")
        positions = [source.index(token) for token in (
            "CALL 'EXLEAF'", "ON EXCEPTION", "MOVE 91 TO WRAP-STATUS",
            "NOT ON EXCEPTION", "IF LEAF-STATUS = ZERO", "COMPUTE WRAP-OUTPUT",
            "ON SIZE ERROR", "MOVE 24 TO WRAP-STATUS", "NOT ON SIZE ERROR",
            "END-COMPUTE", "ELSE", "MOVE LEAF-STATUS TO WRAP-STATUS", "END-IF", "END-CALL")]
        self.assertEqual(positions, sorted(positions))
        success = source[source.index("NOT ON SIZE ERROR"):source.index("END-COMPUTE")]
        self.assertIn("MOVE ZERO TO WRAP-STATUS", success)
        self.assertNotIn("MOVE 24", success)
        self.assertIn("MOVE ZERO TO WRAP-OUTPUT", source[source.index("ON SIZE ERROR"):source.index("NOT ON SIZE ERROR")])

    def test_perform_thru_contains_exceptional_step_without_falling_through_after_goback(self) -> None:
        source = self.source("EXWRAP")
        self.assertLess(source.index("PERFORM RUN-STEP THRU STEP-END"), source.index("GOBACK"))
        self.assertLess(source.index("GOBACK"), source.index("RUN-STEP."))
        ranges = self.rows(
            "SELECT r.* FROM relations r JOIN code_units u ON u.unit_id = r.from_entity_id "
            "WHERE r.relation_type = 'PERFORMS_THRU' AND u.program_name = 'EXWRAP'")
        self.assertEqual(len(ranges), 1)
        self.assertEqual(ranges[0]["target_name"], "RUN-STEP")
        self.assertEqual(json.loads(ranges[0]["metadata_json"])["range_end"], "STEP-END")

    def test_each_main_call_has_exception_handler_and_result_fallback_is_local(self) -> None:
        calls = self.rows("SELECT * FROM code_units WHERE unit_type = 'Statement' AND name = 'CALL'")
        self.assertEqual(len(calls), 6)
        self.assertTrue(all("ON EXCEPTION" in row["normalized_text"] for row in calls))
        entry = self.source("EXENTRY")
        for branch in ("A", "B"):
            finalizer = entry[entry.index(f"CALL 'EXFINAL' USING BY CONTENT WORK-{branch}"):]
            handler = finalizer[:finalizer.index("END-CALL")]
            self.assertIn(f"MOVE 93 TO CODE-{branch}", handler)
            self.assertIn(f"MOVE ZERO TO OUTPUT-{branch}", handler)
        join = entry[entry.index("CALL 'EXJOIN'"):]
        self.assertIn("MOVE 94 TO SUMMARY-STATUS", join)
        self.assertIn("MOVE ZERO TO TOTAL-AMOUNT", join)

    def test_safe_finalizer_and_join_check_returned_business_status(self) -> None:
        finalizer = self.source("EXFINAL")
        self.assertIn("IF FINAL-STATUS NOT = ZERO\n        MOVE ZERO TO FINAL-OUTPUT", finalizer)
        self.assertIn("MOVE FINAL-STATUS TO FINAL-CODE", finalizer)
        join = self.source("EXJOIN")
        self.assertLess(join.index("IF LEFT-STATUS NOT = ZERO"), join.index("IF RIGHT-STATUS NOT = ZERO"))
        self.assertLess(join.index("IF RIGHT-STATUS NOT = ZERO"), join.index("COMPUTE JOIN-OUTPUT"))
        self.assertIn("NOT ON SIZE ERROR", join)

    def test_overwrite_regression_is_selected_separately_and_expectation_is_nonzero(self) -> None:
        path = FIXTURE_ROOT / "regressions" / "programs" / "EXBAD.cbl"
        source = path.read_text(encoding="utf-8")
        item = self.regression["local_exceptional_expectations"][0]
        self.assertEqual(self.regression["root_program"], "EXBAD")
        self.assertEqual(self.regression["source_root"], "programs")
        self.assertEqual(item["expected"], "nonzero_exit_possible")
        self.assertEqual(item["event_kind"], "size_error")
        self.assertLess(source.index("MOVE ZERO TO RESULT-AMOUNT\n"), source.index("END-COMPUTE"))
        self.assertLess(source.index("END-COMPUTE"), source.index("MOVE 7 TO RESULT-AMOUNT"))
        self.assertLess(source.index("MOVE 7 TO RESULT-AMOUNT"), source.index("GOBACK"))
        self.assertEqual(self.regression["scenarios"][0]["execution_status"], "NOT_EXECUTED")
        self.assertEqual(self.regression["scenarios"][0]["expected"]["output"], "7.00")

    def test_hand_arithmetic_explains_distinct_wrapper_and_join_size_boundaries(self) -> None:
        baseline = self.profile["baseline"]
        left = Decimal(baseline["input_a"]) * 2
        right = Decimal(baseline["input_b"]) * 2
        self.assertEqual(left, Decimal(baseline["expected_output_a"]))
        self.assertEqual(right, Decimal(baseline["expected_output_b"]))
        self.assertEqual(left + right, Decimal(baseline["expected_total"]))
        receiving_capacity = Decimal("99999.99")
        self.assertGreater(Decimal("60000.00") * 2, receiving_capacity)
        individual = Decimal("30000.00") * 2
        self.assertLessEqual(individual, receiving_capacity)
        self.assertGreater(individual + individual, receiving_capacity)
        self.assertTrue(all(case["execution_status"] == "NOT_EXECUTED" for case in self.profile["scenarios"]))
        self.assertEqual(len(self.profile["scenarios"]), 8)

    def test_unknown_write_dynamic_call_and_loop_are_outside_positive_slice(self) -> None:
        boundary_root = FIXTURE_ROOT / "boundaries" / "programs"
        unknown = (boundary_root / "EXUNKNOWN.cbl").read_text(encoding="utf-8")
        self.assertLess(unknown.index("MOVE ZERO TO UNKNOWN-OUTPUT"), unknown.index("ACCEPT UNKNOWN-OUTPUT"))
        dynamic = (boundary_root / "EXDYNAMIC.cbl").read_text(encoding="utf-8")
        self.assertIn("CALL PROGRAM-CHOICE", dynamic)
        loop = (boundary_root / "EXLOOP.cbl").read_text(encoding="utf-8")
        self.assertIn("PERFORM VARYING LOOP-INDEX FROM 1 BY 1", loop)
        self.assertIn("ON SIZE ERROR", loop)
        for path in (MAIN_ROOT / "programs").glob("*.cbl"):
            source = path.read_text(encoding="utf-8")
            for unsupported in ("EXEC SQL", "EVALUATE", "PERFORM VARYING", "ACCEPT ", "OCCURS"):
                self.assertNotIn(unsupported, source)


if __name__ == "__main__":
    unittest.main()
