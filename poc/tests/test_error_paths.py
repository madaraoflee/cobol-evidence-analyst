from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from error_paths import ErrorContract, audit_error_paths
from structural_index import build_structural_index


SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. ENTRYPGM.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 PROCESS-STATUS PIC S9(4) COMP.
01 RESULT-AMOUNT PIC 9(9)V99.
01 INPUT-KEY PIC X(8).
01 RATE-VALUE PIC 9V9999.
01 SQLCODE PIC S9(9) COMP.
PROCEDURE DIVISION.
MAIN-PARA.
    MOVE ZERO TO PROCESS-STATUS
    PERFORM QUERY-PARA
    IF PROCESS-STATUS = ZERO
        PERFORM CALC-PARA
    END-IF
    PERFORM FINAL-PARA
    GOBACK.
QUERY-PARA.
    EXEC SQL
        SELECT RATE_VALUE INTO :RATE-VALUE FROM RATE_TABLE
        WHERE INPUT_KEY = :INPUT-KEY
    END-EXEC
    IF SQLCODE NOT = ZERO
        EVALUATE SQLCODE
            WHEN 100
                MOVE 12 TO PROCESS-STATUS
            WHEN -811
                MOVE 16 TO PROCESS-STATUS
            WHEN OTHER
                MOVE 20 TO PROCESS-STATUS
        END-EVALUATE
    END-IF.
CALC-PARA.
    COMPUTE RESULT-AMOUNT = RATE-VALUE * 100.
FINAL-PARA.
    IF PROCESS-STATUS NOT = ZERO
        MOVE ZERO TO RESULT-AMOUNT
    ELSE
        MOVE RESULT-AMOUNT TO RESULT-AMOUNT
    END-IF.
"""

FINALIZER = """IDENTIFICATION DIVISION.
PROGRAM-ID. FINALIZE.
DATA DIVISION.
LINKAGE SECTION.
01 LK-STATUS PIC S9(4) COMP.
01 LK-AMOUNT PIC 9(9)V99.
PROCEDURE DIVISION USING LK-STATUS LK-AMOUNT.
MAIN-PARA.
    IF LK-STATUS NOT = ZERO
        MOVE ZERO TO LK-AMOUNT
    END-IF.
    GOBACK.
"""


class ErrorPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.database = self.root / "index.sqlite"
        self.contract = ErrorContract("ENTRYPGM", ("PROCESS-STATUS",), ("RESULT-AMOUNT",))

    def audit(self, source=SOURCE, **kwargs):
        (self.sources / "entry.cbl").write_text(source, encoding="utf-8")
        build_structural_index(self.sources, self.database, quiet=True)
        return audit_error_paths(self.database, "ENTRYPGM", (self.contract,), **kwargs)

    @staticmethod
    def observations(report, kind):
        return [item for item in report["observations"] if item["kind"] == kind]

    def test_subroutine_query_guards_finalizer_and_snapshot(self):
        report = self.audit()
        self.assertFalse(report["complete"])
        self.assertFalse(report["full_control_flow_proven"])
        self.assertNotEqual(report["snapshot_id"], "unknown")
        checks = self.observations(report, "sql_error_check")
        self.assertEqual(checks[0]["status"], "source_shape_observed")
        self.assertEqual(len(self.observations(report, "error_status_origin")), 3)
        self.assertEqual(len(self.observations(report, "error_output_clear")), 1)
        self.assertEqual(self.observations(report, "possible_output_overwrite_after_error_clear"), [])
        self.assertEqual(self.observations(report, "local_error_output_clear_not_observed"), [])
        self.assertTrue(all(len(ref["source_sha256"]) == 64 and len(ref["span_sha256"]) == 64
                            for item in report["observations"] for ref in item["evidence_refs"]))
        self.assertTrue(any(item["statement"] == "PERFORM CALC-PARA" and item["status"] == "source_shape_observed"
                            for item in self.observations(report, "step_status_gate")))

    def test_remove_sqlcode_check_is_detected(self):
        start = SOURCE.index("    IF SQLCODE")
        end = SOURCE.index("CALC-PARA.", start)
        report = self.audit(SOURCE[:start] + "    CONTINUE.\n" + SOURCE[end:])
        check = self.observations(report, "sql_error_check")[0]
        self.assertEqual(check["status"], "missing")
        self.assertEqual(check["reason"], "no_local_sqlcode_guard_before_next_effect")

    def test_drop_one_failure_arm_cannot_be_hidden_by_when_other(self):
        report = self.audit(SOURCE.replace("MOVE 12 TO PROCESS-STATUS", "CONTINUE"))
        check = self.observations(report, "sql_error_check")[0]
        self.assertEqual(check["status"], "missing")
        self.assertIn("WHEN 100", check["missing_failure_arms"])

    def test_drop_other_failure_arm_is_detected(self):
        report = self.audit(SOURCE.replace("            WHEN OTHER\n                MOVE 20 TO PROCESS-STATUS\n", ""))
        self.assertEqual(self.observations(report, "sql_error_check")[0]["status"], "missing")

    def test_sqlcode_overwritten_before_guard_is_not_accepted(self):
        report = self.audit(SOURCE.replace("    IF SQLCODE NOT = ZERO", "    MOVE ZERO TO SQLCODE\n    IF SQLCODE NOT = ZERO"))
        self.assertEqual(self.observations(report, "sql_error_check")[0]["status"], "missing")

    def test_drop_final_clear_and_bypass_guard(self):
        report = self.audit(SOURCE.replace("        MOVE ZERO TO RESULT-AMOUNT", "        CONTINUE"))
        self.assertEqual(self.observations(report, "local_error_output_clear_not_observed")[0]["field"], "RESULT-AMOUNT")
        report = self.audit(SOURCE.replace("    IF PROCESS-STATUS NOT = ZERO\n        MOVE ZERO TO RESULT-AMOUNT\n    ELSE\n        MOVE RESULT-AMOUNT TO RESULT-AMOUNT\n    END-IF.", "    MOVE ZERO TO RESULT-AMOUNT."))
        self.assertEqual(len(self.observations(report, "local_error_output_clear_not_observed")), 1)

    def test_later_overwrite_and_status_reset_are_reported(self):
        report = self.audit(SOURCE + "    MOVE 99 TO RESULT-AMOUNT.\n    MOVE ZERO TO PROCESS-STATUS.\n")
        self.assertTrue(self.observations(report, "possible_output_overwrite_after_error_clear"))
        self.assertTrue(self.observations(report, "possible_error_status_reset"))

    def test_success_gate_removed_is_visible(self):
        report = self.audit(SOURCE.replace("    IF PROCESS-STATUS = ZERO\n        PERFORM CALC-PARA\n    END-IF", "    PERFORM CALC-PARA"))
        gates = self.observations(report, "step_status_gate")
        self.assertTrue(any(item["statement"] == "PERFORM CALC-PARA" and item["status"] == "not_observed" for item in gates))

    def test_recursive_perform_is_bounded(self):
        report = self.audit(SOURCE.replace("CALC-PARA.\n    COMPUTE", "CALC-PARA.\n    PERFORM CALC-PARA\n    COMPUTE"))
        self.assertIn("recursive_perform", {b["reason"] for b in report["boundaries"]})
        self.assertLess(report["programs"][0]["bounded_event_count"], 100)

    def test_goto_and_event_budget_do_not_claim_final_clear(self):
        report = self.audit(SOURCE.replace("    PERFORM FINAL-PARA", "    GO TO UNKNOWN-PARA"))
        self.assertIn("goto_control_flow_not_expanded", {b["reason"] for b in report["boundaries"]})
        self.assertEqual(len(self.observations(report, "local_error_output_clear_not_observed")), 1)
        report = self.audit(max_events=3)
        self.assertIn("event_budget_exhausted", {b["reason"] for b in report["boundaries"]})

    def test_literal_recursion_and_dynamic_call_remain_boundaries(self):
        source = SOURCE.replace("    PERFORM QUERY-PARA", "    CALL 'ENTRYPGM'\n    CALL INPUT-KEY\n    PERFORM QUERY-PARA")
        report = self.audit(source)
        reasons = {b["reason"] for b in report["boundaries"]}
        self.assertIn("recursive_call_chain", reasons)
        self.assertIn("dynamic_target_and_exception_outcome_not_proven", reasons)
        self.assertEqual(report["call_paths"][0]["program_path"], ["ENTRYPGM", "ENTRYPGM"])

    def test_simple_if_error_and_else_error_shapes(self):
        start = SOURCE.index("    IF SQLCODE")
        end = SOURCE.index("CALC-PARA.", start)
        for conditional in ("IF SQLCODE NOT = ZERO\n        MOVE 12 TO PROCESS-STATUS\n    END-IF.",
                            "IF SQLCODE = ZERO\n        CONTINUE\n    ELSE\n        MOVE 12 TO PROCESS-STATUS\n    END-IF."):
            with self.subTest(conditional=conditional):
                report = self.audit(SOURCE[:start] + "    " + conditional + "\n" + SOURCE[end:])
                self.assertEqual(self.observations(report, "sql_error_check")[0]["status"], "source_shape_observed")

    def test_snapshot_readonly_and_contract_validation(self):
        self.audit()
        before = self.database.read_bytes()
        audit_error_paths(self.database, "ENTRYPGM", (self.contract,))
        self.assertEqual(before, self.database.read_bytes())
        for args in (("ENTRYPGM", ()), ("ENTRYPGM", (self.contract, self.contract)),
                     ("entrypgm", (self.contract,))):
            with self.assertRaises(ValueError):
                audit_error_paths(self.database, *args)
        missing = self.root / "absent.sqlite"
        with self.assertRaises(sqlite3.OperationalError):
            audit_error_paths(missing, "ENTRYPGM", (self.contract,))
        self.assertFalse(missing.exists())

    def test_hash_mismatch_fails_closed(self):
        self.audit()
        connection = sqlite3.connect(self.database)
        try:
            with connection:
                connection.execute("UPDATE evidence_spans SET source_sha256 = ?", ("0" * 64,))
        finally:
            connection.close()
        with self.assertRaises(ValueError):
            audit_error_paths(self.database, "ENTRYPGM", (self.contract,))

    def test_delegated_clear_requires_reference_output_and_no_later_write(self):
        final_start = SOURCE.rindex("FINAL-PARA.\n")
        call = "    CALL 'FINALIZE' USING BY CONTENT PROCESS-STATUS\n        BY REFERENCE RESULT-AMOUNT\n    END-CALL.\n"
        caller = SOURCE[:final_start] + "FINAL-PARA.\n" + call
        contract = ErrorContract("FINALIZE", ("LK-STATUS",), ("LK-AMOUNT",))
        variants = (
            (caller, FINALIZER, True),
            (caller.replace("BY REFERENCE RESULT-AMOUNT", "BY CONTENT RESULT-AMOUNT"), FINALIZER, False),
            (caller + "    MOVE 99 TO RESULT-AMOUNT.\n", FINALIZER, False),
            (caller, FINALIZER.replace("    GOBACK.", "    MOVE 99 TO LK-AMOUNT.\n    GOBACK."), False),
            (caller.replace(call, "    IF PROCESS-STATUS = ZERO\n" + call.replace("END-CALL.", "END-CALL") + "    END-IF.\n"), FINALIZER, False),
        )
        for caller_source, callee_source, expected in variants:
            with self.subTest(expected=expected, caller=caller_source[-150:], callee=callee_source[-100:]):
                (self.sources / "entry.cbl").write_text(caller_source, encoding="utf-8")
                (self.sources / "finalize.cbl").write_text(callee_source, encoding="utf-8")
                build_structural_index(self.sources, self.database, quiet=True)
                report = audit_error_paths(self.database, "ENTRYPGM", (self.contract, contract))
                self.assertEqual(bool(self.observations(report, "delegated_error_output_clear")), expected)

    def test_call_exception_origin_is_labeled_as_clause_shape(self):
        source = SOURCE.replace("    PERFORM QUERY-PARA", "    CALL INPUT-KEY\n        ON EXCEPTION\n            MOVE 92 TO PROCESS-STATUS\n            MOVE ZERO TO RESULT-AMOUNT\n    END-CALL\n    PERFORM QUERY-PARA")
        report = self.audit(source)
        origins = self.observations(report, "error_status_origin")
        self.assertTrue(any(item["value"] == "92" and item["origin_context"] == "call_exception_clause" for item in origins))
        self.assertTrue(self.observations(report, "call_exception_output_clear"))

    def test_complex_fixture_is_not_name_specific(self):
        fixture = POC_ROOT / "fixtures" / "complex-business-v2" / "main"
        if not fixture.exists():
            self.skipTest("Complex business fixture is not available.")
        build_structural_index(fixture, self.database, quiet=True)
        contracts = (
            ErrorContract("TXNENTRY", ("PROCESS-STATUS",), ("RESULT-AMOUNT", "RESULT-ANNUAL")),
            ErrorContract("TXNCORE", ("PROCESS-STATUS",)),
            ErrorContract("RATELOOK", ("PROCESS-STATUS", "WS-DATE-STATUS")),
            ErrorContract("DATECHK", ("DATE-STATUS",)),
            ErrorContract("FACTORLK", ("PROCESS-STATUS",)),
            ErrorContract("FEELOOK", ("PROCESS-STATUS",)),
            ErrorContract("ROUTESEL", ("PROCESS-STATUS",)),
            ErrorContract("RESULTMP", ("PROCESS-STATUS",), ("RESULT-AMOUNT", "RESULT-ANNUAL")),
        )
        report = audit_error_paths(self.database, "TXNENTRY", contracts)
        checks = self.observations(report, "sql_error_check")
        self.assertGreaterEqual(len(checks), 4)
        self.assertTrue(all(item["status"] == "source_shape_observed" for item in checks), checks)
        self.assertEqual({item["field"] for item in self.observations(report, "error_output_clear")},
                         {"RESULT-AMOUNT", "RESULT-ANNUAL"})
        self.assertTrue(any(len(item["program_path"]) >= 5 for item in report["call_paths"]))
        self.assertTrue(any(item["source_field"] == "WS-DATE-STATUS" and item["target_field"] == "PROCESS-STATUS"
                            for item in self.observations(report, "error_status_forwarding")))
        self.assertTrue(any(item["value"] == "92"
                            for item in self.observations(report, "call_exception_status_assignment")))
        self.assertTrue(any(item["callee_program"] == "DATECHK" and item["callee_field"] == "DATE-STATUS"
                            for item in report["parameter_transfers"]))
        self.assertTrue(all(not item["runtime_writeback_proven"] for item in report["parameter_transfers"]))
        delegated = self.observations(report, "delegated_error_output_clear")
        self.assertEqual({item["field"] for item in delegated}, {"RESULT-AMOUNT", "RESULT-ANNUAL"})
        self.assertTrue(all(item["callee_program"] == "RESULTMP" and not item["runtime_path_proven"] for item in delegated))
        json.dumps(report)


if __name__ == "__main__":
    unittest.main()
