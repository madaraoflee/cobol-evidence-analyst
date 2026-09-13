"""Incomplete preprocessing must not masquerade as entry-bound procedure facts."""

from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework_audit import audit_framework


def program(body: str, name: str = "ENTRYPG") -> str:
    return f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nPROCEDURE DIVISION.\n{body}\n"


class FrameworkExpansionBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, name: str, source: str) -> None:
        (self.root / name).write_text(source, encoding="utf-8")

    def assert_suppressed(self, result: dict, reason: str) -> None:
        self.assertFalse(result["scope"]["source_expansion_complete"])
        reasons = {item["reason"] for item in result["boundaries"]}
        self.assertIn(reason, reasons)
        self.assertIn("procedure_observations_suppressed", reasons)
        for key in ("procedure_definitions", "performs", "calls", "native_io"):
            self.assertEqual(result["observations"][key], [], key)

    def test_multiple_programs_do_not_attribute_second_program_calls_to_entry(self) -> None:
        self.write("entry.cbl", program("ENTRY-PATH.\nGOBACK.\nEND PROGRAM ENTRYPG.")
                   + program("OTHER-PATH.\nPERFORM 4000-HANDLE.\nCALL 'OTHERWORKER'.\n"
                             "4000-HANDLE SECTION.\nREAD OTHER-FILE.\nGOBACK.", "OTHERPG"))
        result = audit_framework(self.root, "ENTRYPG")
        self.assert_suppressed(result, "multiple_programs_in_source_not_supported")

    def test_replace_state_does_not_emit_old_or_replacement_call_as_actual_target(self) -> None:
        self.write("entry.cbl", program("REPLACE ==OLDWORKER== BY ==NEWWORKER==.\n"
                                        "ENTRY-PATH.\nPERFORM 4000-HANDLE.\nCALL 'OLDWORKER'.\n"
                                        "4000-HANDLE SECTION.\nREAD SOURCE-FILE.\nGOBACK."))
        self.assert_suppressed(audit_framework(self.root, "ENTRYPG"), "replace_statement_not_supported")

    def test_copy_replacement_pseudotext_cannot_create_a_call_observation(self) -> None:
        self.write("entry.cbl", program("COPY CONTROL REPLACING ==TOKEN== BY ==CALL 'PHANTOM'==.\nGOBACK."))
        self.write("control.cpy", "TOKEN.\n")
        result = audit_framework(self.root, "ENTRYPG")
        self.assert_suppressed(result, "copy_form_not_supported")
        self.assertTrue(result["observations"]["includes"])
        self.assertEqual(result["observations"]["includes"][0]["status"], "bounded")

    def test_inline_copy_does_not_leave_following_tokens_as_analyzed_procedure(self) -> None:
        self.write("entry.cbl", program("CONTINUE. COPY CONTROL. CALL 'WORKER'.\nGOBACK."))
        self.write("control.cpy", "PERFORM 4000-HANDLE.\n")
        self.assert_suppressed(audit_framework(self.root, "ENTRYPG"), "copy_form_not_supported")

    def test_missing_copy_does_not_claim_uniquely_resolved_perform(self) -> None:
        self.write("entry.cbl", program("COPY CONTROL.\nPERFORM 4000-HANDLE.\nCALL 'WORKER'.\n"
                                        "4000-HANDLE SECTION.\nREAD SOURCE-FILE.\nGOBACK."))
        result = audit_framework(self.root, "ENTRYPG")
        self.assert_suppressed(result, "copy_target_not_found")
        self.assertEqual(result["observations"]["includes"][0]["copy_name"], "CONTROL")

    def test_conditional_source_directive_cannot_promote_disabled_call(self) -> None:
        self.write("entry.cbl", program(
            ">>IF FEATURE\nCALL 'CONDITIONALWORKER'.\n>>END-IF\nGOBACK."))
        self.assert_suppressed(audit_framework(self.root, "ENTRYPG"), "source_directive_not_supported")

    def test_nested_boundary_suppresses_host_and_included_procedure_observations(self) -> None:
        self.write("entry.cbl", program("COPY CONTROL.\nHOST-PATH.\nCALL 'HOSTWORKER'.\nGOBACK."))
        self.write("control.cpy", "COPY ABSENT.\nINCLUDED-PATH.\nCALL 'INCLUDEDWORKER'.\n")
        result = audit_framework(self.root, "ENTRYPG")
        self.assert_suppressed(result, "copy_target_not_found")
        self.assertEqual(len(result["observations"]["includes"]), 2)
        self.assertEqual(result["observations"]["includes"][0]["resolved_path"], "control.cpy")

    def test_incomplete_source_does_not_confirm_declared_section_presence(self) -> None:
        self.write("entry.cbl", program("COPY CONTROL.\nPERFORM 4000-HANDLE.\n4000-HANDLE SECTION.\nGOBACK."))
        profile = {"schema_version": "1.0", "profile_id": "test-contract", "profile_version": "1",
                   "provenance": {"kind": "synthetic", "reference": "local-test", "target_version": "test"},
                   "entries": [{"program": "ENTRYPG", "mode": "batch", "control_copy": "CONTROL",
                                "sections": [{"name": "4000-HANDLE", "role": "Declared record handling"}]}]}
        result = audit_framework(self.root, "ENTRYPG", profile)
        self.assert_suppressed(result, "copy_target_not_found")
        section = result["declared_contracts"]["entry"]["sections"][0]
        self.assertFalse(section["definition_observed"])
        self.assertFalse(section["perform_observed"])

    def test_complete_source_still_observes_real_call_but_not_literal_keywords(self) -> None:
        self.write("entry.cbl", program("DISPLAY 'COPY LOST. CALL PHANTOM. PERFORM LOST.'.\n"
                                        "CALL 'REALWORKER'.\nGOBACK."))
        result = audit_framework(self.root, "ENTRYPG")
        self.assertTrue(result["scope"]["source_expansion_complete"])
        self.assertEqual([call["target"] for call in result["observations"]["calls"]], ["REALWORKER"])
        self.assertEqual(result["observations"]["performs"], [])


if __name__ == "__main__":
    unittest.main()
