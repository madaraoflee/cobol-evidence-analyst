from __future__ import annotations

from contextlib import closing
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from structural_index import build_structural_index


FIXTURE_ROOT = POC_ROOT / "fixtures" / "call-context-v3"
MAIN_ROOT = FIXTURE_ROOT / "main"


class RepeatedCallContextFixtureTests(unittest.TestCase):
    """Independent fixture checks; this test suite does not execute COBOL."""

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

    def source(self, name: str) -> str:
        return (MAIN_ROOT / "programs" / f"{name}.cbl").read_text(encoding="utf-8")

    def bindings(self) -> list[sqlite3.Row]:
        return self.rows(
            "SELECT b.*, caller.name AS caller_field, callee.name AS callee_field "
            "FROM call_bindings b JOIN symbols caller ON caller.symbol_id = b.caller_symbol_id "
            "JOIN symbols callee ON callee.symbol_id = b.callee_symbol_id")

    def test_main_is_seven_programs_with_separate_negative_cases(self) -> None:
        names = {row[0] for row in self.rows("SELECT name FROM symbols WHERE symbol_type = 'Program'")}
        self.assertEqual(names, {contract["program_name"] for contract in self.profile["contracts"]})
        self.assertEqual(len(names), 7)
        self.assertEqual(self.report["files"]["candidate"], 7)
        self.assertEqual(self.profile["source_root"], "main")
        self.assertNotIn("CONTEXTREC", names)
        self.assertEqual(len(list((FIXTURE_ROOT / "boundaries").glob("*.cbl"))), 5)

    def test_static_calls_and_expected_context_counts_are_distinct_measures(self) -> None:
        calls = self.rows(
            "SELECT u.program_name, r.target_name FROM relations r "
            "JOIN code_units u ON u.unit_id = r.from_entity_id "
            "WHERE r.relation_type = 'CALLS' AND r.status = 'confirmed'")
        self.assertEqual(len(calls), self.profile["expected_totals"]["source_callsites"])
        counts = self.profile["expected_context_counts"]
        self.assertEqual(sum(counts.values()), 12)
        self.assertEqual(counts["CALCWRAP"], 2)
        self.assertEqual(counts["SHAREDWK"], 2)
        self.assertEqual(counts["INPUTCHK"], 2)
        expanded = sum(counts[row["program_name"]] for row in calls)
        self.assertEqual(expanded, self.profile["expected_totals"]["expanded_call_occurrences"])
        self.assertEqual(expanded + 1, sum(counts.values()))

    def test_entry_reuses_one_wrapper_definition_at_two_source_callsites(self) -> None:
        bindings = [row for row in self.bindings() if row["caller_program"] == "PAIRMAIN"
                    and row["callee_program"] == "CALCWRAP" and row["parameter_position"] == 1
                    and row["group_member_index"] == 0]
        self.assertEqual({row["caller_field"] for row in bindings}, {"REQUEST-A", "REQUEST-B"})
        self.assertEqual(len({row["callsite_id"] for row in bindings}), 2)
        self.assertEqual(len({row["callee_symbol_id"] for row in bindings}), 1)
        self.assertEqual({row["passing_mode"] for row in bindings}, {"CONTENT"})

    def test_nested_shared_callsite_requires_ancestor_context_to_separate_branches(self) -> None:
        rows = [row for row in self.bindings() if row["caller_program"] == "CALCWRAP"]
        self.assertEqual({row["callee_program"] for row in rows}, {"SHAREDWK"})
        self.assertEqual(len({row["callsite_id"] for row in rows}), 1)
        for branch in self.profile["expected_nested_branches"]:
            self.assertEqual(branch["program_chain"], ["PAIRMAIN", "CALCWRAP", "SHAREDWK"])
            self.assertEqual(branch["program_path"], branch["program_chain"])
            self.assertEqual(branch["leaf_status_field"], "WORK-STATUS")
            self.assertNotEqual(branch["branch_id"], branch["must_not_share_context_with"])
            actual = {(row["caller_field"], row["callee_field"], row["passing_mode"],
                       row["parameter_position"], row["group_member_index"]) for row in rows}
            expected = {(item["caller_field"], item["callee_field"], item["passing_mode"],
                         item["position"], item["member_index"])
                        for item in branch["shared_worker_parameters"]}
            self.assertEqual(actual, expected)

    def test_profile_entry_endpoints_match_real_positional_and_group_bindings(self) -> None:
        actual = {(row["callee_program"], row["caller_field"], row["callee_field"], row["passing_mode"])
                  for row in self.bindings() if row["caller_program"] == "PAIRMAIN"}
        for item in self.profile["expected_entry_mappings"]:
            self.assertIn((item["callee_program"], item["caller_field"], item["callee_field"],
                           item["passing_mode"]), actual)
        self.assertEqual(self.report["call_bindings"]["confirmed_bindings"], 43)
        self.assertEqual(self.report["call_bindings"]["boundary_counts"], {})
        self.assertEqual(self.report["call_bindings"]["writeback_candidates"], 19)

    def test_content_copy_is_not_a_reference_writeback_to_entry_request(self) -> None:
        relations = self.rows(
            "SELECT r.*, caller.program_name AS caller_program, caller.name AS caller_field "
            "FROM relations r JOIN symbols caller ON caller.symbol_id = r.target_entity_id "
            "WHERE r.relation_type = 'MAY_WRITE_BACK'")
        for row in relations:
            self.assertEqual(row["status"], "candidate")
            metadata = json.loads(row["metadata_json"])
            self.assertEqual(metadata["passing_mode"], "REFERENCE")
            if row["caller_program"] == "PAIRMAIN":
                self.assertNotIn(row["caller_field"], {
                    "REQUEST-A", "AMOUNT-A", "COUNT-A", "RATE-A",
                    "REQUEST-B", "AMOUNT-B", "COUNT-B", "RATE-B"})
        self.assertTrue(any(row["caller_program"] == "CALCWRAP"
                            and row["caller_field"] == "WRAP-REQUEST" for row in relations))

    def test_leaf_renaming_and_binary_by_value_remain_explicit(self) -> None:
        actual = {(row["caller_program"], row["callee_program"], row["caller_field"],
                   row["callee_field"], row["passing_mode"]) for row in self.bindings()}
        for item in self.profile["expected_leaf_mappings"]:
            self.assertIn((item["caller_program"], item["callee_program"], item["caller_field"],
                           item["callee_field"], item["passing_mode"]), actual)
        value = [row for row in self.bindings() if row["passing_mode"] == "VALUE"]
        self.assertEqual(len(value), 1)
        self.assertEqual(value[0]["caller_field"], "INPUT-COUNT")
        self.assertEqual(value[0]["callee_field"], "CHECK-COUNT")
        self.assertIn("PIC 9(4) COMP-5", self.source("INPUTCHK"))

    def test_every_main_call_has_exception_clause_and_finalizer_fallback(self) -> None:
        calls = self.rows("SELECT * FROM code_units WHERE unit_type = 'Statement' AND name = 'CALL'")
        self.assertEqual(len(calls), 8)
        self.assertTrue(all("ON EXCEPTION" in row["normalized_text"] for row in calls))
        entry = self.source("PAIRMAIN")
        for branch in ("A", "B"):
            self.assertIn(f"MOVE 93 TO CODE-{branch}", entry)
            self.assertIn(f"MOVE ZERO TO OUTPUT-{branch}", entry)
        self.assertIn("MOVE 94 TO SUMMARY-STATUS", entry)
        self.assertIn("MOVE ZERO TO TOTAL-AMOUNT", entry)

    def test_internal_subroutines_and_error_guard_remain_source_backed(self) -> None:
        ranges = self.rows(
            "SELECT r.* FROM relations r JOIN code_units u ON u.unit_id = r.from_entity_id "
            "WHERE u.program_name = 'CALCWRAP' AND r.relation_type = 'PERFORMS_THRU'")
        self.assertEqual(len(ranges), 1)
        self.assertEqual(ranges[0]["target_name"], "RUN-WORKER")
        self.assertEqual(json.loads(ranges[0]["metadata_json"])["range_end"], "WORKER-END")
        worker = self.source("SHAREDWK")
        self.assertIn("IF WORK-STATUS = ZERO\n        PERFORM CALCULATE-RESULT", worker)
        self.assertIn("IF WORK-STATUS NOT = ZERO\n        MOVE ZERO TO WORK-RESULT", worker)
        join = self.source("PAIRJOIN")
        self.assertLess(join.index("WHEN LEFT-CODE NOT = ZERO"), join.index("WHEN RIGHT-CODE NOT = ZERO"))

    def test_fixed_and_free_source_forms_have_unambiguous_declarations(self) -> None:
        fixed = self.source("INPUTCHK").splitlines()
        self.assertTrue(all(line[:6].isdigit() for line in fixed))
        self.assertTrue(all(len(line) <= 72 for line in fixed))
        self.assertIn(">>SOURCE FORMAT FREE", self.source("CALCWRAP"))
        self.assertEqual(len(self.rows(
            "SELECT * FROM symbols WHERE program_name = 'INPUTCHK' AND symbol_type = 'Field'")), 3)

    def test_hand_expected_pair_arithmetic_is_transparent_and_unexecuted(self) -> None:
        baseline = self.profile["baseline"]
        cents = lambda value: value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        amounts = [cents(Decimal(baseline[key]["amount"]) * baseline[key]["count"]
                         * Decimal(baseline[key]["rate"])) for key in ("request_a", "request_b")]
        normal = self.profile["scenarios"][0]["expected"]
        self.assertEqual(amounts, [Decimal(normal["left_amount"]), Decimal(normal["right_amount"])])
        self.assertEqual(sum(amounts), Decimal(normal["total"]))
        self.assertEqual(len(self.profile["scenarios"]), 12)
        self.assertTrue(all(item["execution_status"] == "NOT_EXECUTED"
                            for item in self.profile["scenarios"]))
        for scenario in self.profile["scenarios"][1:]:
            self.assertEqual(scenario["expected"]["total"], "0.00")
            self.assertNotEqual(scenario["expected"]["summary_status"], 0)

    def test_negative_cases_preserve_recursion_dynamic_alias_and_loop_facts(self) -> None:
        database = Path(self.temporary.name) / "boundaries.sqlite"
        build_structural_index(FIXTURE_ROOT / "boundaries", database, quiet=True)
        with closing(sqlite3.connect(database)) as connection:
            connection.row_factory = sqlite3.Row
            rows = list(connection.execute("SELECT * FROM call_bindings"))
            recursive = [row for row in rows if row["caller_program"] == "CONTEXTREC"]
            self.assertEqual(len(recursive), 1)
            self.assertEqual(recursive[0]["callee_program"], "CONTEXTREC")
            self.assertEqual(recursive[0]["status"], "confirmed")
            dynamic = [row for row in rows if row["caller_program"] == "CONTEXTDYN"]
            self.assertEqual(len(dynamic), 1)
            self.assertEqual(dynamic[0]["reason"], "dynamic_target_not_resolved")
            alias = [row for row in rows if row["caller_program"] == "ALIASCALL"]
            self.assertEqual(len(alias), 2)
            self.assertEqual(len({row["caller_symbol_id"] for row in alias}), 1)
            self.assertEqual(len({row["callee_symbol_id"] for row in alias}), 2)
            self.assertTrue(all(row["status"] == "confirmed" for row in alias))
            loop = list(connection.execute(
                "SELECT * FROM code_units WHERE program_name = 'LOOPCALL' AND name = 'CALL'"))
            self.assertEqual(len(loop), 1)
        loop_source = (FIXTURE_ROOT / "boundaries" / "LOOPCALL.cbl").read_text(encoding="utf-8")
        self.assertIn("PERFORM VARYING LOOP-INDEX FROM 1 BY 1", loop_source)
        self.assertEqual({item["root_program"] for item in self.profile["boundary_cases"]},
                         {"CONTEXTREC", "CONTEXTDYN", "ALIASCALL", "LOOPCALL"})


if __name__ == "__main__":
    unittest.main()
