from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from call_bindings import MAX_PARAMETERS, parse_call, parse_signature
from structural_index import build_structural_index, normalize_cobol_lines


def program(name: str, declarations: str, body: str, *, parameters: str = "", linkage: str = "") -> str:
    return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
            f"WORKING-STORAGE SECTION.\n{declarations}\n"
            + (f"LINKAGE SECTION.\n{linkage}\n" if linkage else "")
            + f"PROCEDURE DIVISION{(' USING ' + parameters) if parameters else ''}.\n"
            f"MAIN-ENTRY.\n{body}\nGOBACK.\n")


class CallFormTests(unittest.TestCase):
    def test_modes_persist_and_callee_content_is_reference(self) -> None:
        call = parse_call("CALL 'WORKER' USING A BY CONTENT B C BY VALUE D BY REFERENCE E END-CALL.")
        self.assertFalse(call.dynamic)
        self.assertEqual([(item.name, item.mode) for item in call.parameters], [
            ("A", "REFERENCE"), ("B", "CONTENT"), ("C", "CONTENT"), ("D", "VALUE"), ("E", "REFERENCE")])
        self.assertEqual([item.mode for item in parse_signature("PROCEDURE DIVISION USING A BY VALUE B BY REFERENCE C.")], ["REFERENCE", "VALUE", "REFERENCE"])

    def test_complex_or_malformed_calls_fail_closed(self) -> None:
        cases = [
            "CALL 'WORKER' USING BY.", "CALL 'WORKER' USING BY CONTENT.",
            "CALL 'WORKER' USING LENGTH OF A.", "CALL 'WORKER' USING A OF B.",
            "CALL 'WORKER' USING A(1).", "CALL 'WORKER' USING A(1:2).",
            "CALL 'WORKER' USING OMITTED.", "CALL 'WORKER' USING 'X'.",
            "CALL 'WORKER' USING 12.", "CALL 'WORKER' USING A RETURNING B.",
            "CALL 'WORKER' USING A ON EXCEPTION MOVE 1 TO B END-CALL.",
            "CALL 'WORKER' USING A ELSE.", "CALL 'WORKER' USING BY UNKNOWN A.",
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                parse_call(case)
        for case in ("PROCEDURE DIVISION USING A A.", "PROCEDURE DIVISION USING BY CONTENT A.",
                     "PROCEDURE DIVISION USING A RETURNING B.", "PROCEDURE DIVISION USING A"):
            with self.subTest(case=case), self.assertRaises(ValueError):
                parse_signature(case)

    def test_parameter_budget_is_explicit(self) -> None:
        with self.assertRaisesRegex(ValueError, "call_parameter_limit"):
            parse_call("CALL 'WORKER' USING " + " ".join(f"ARG-{index}" for index in range(MAX_PARAMETERS + 1)))

    def test_free_indentation_is_not_a_sequence_number(self) -> None:
        lines, _ = normalize_cobol_lines("01 GROUP-A.\n   05 FIELD-A PIC X.\n000100 01 FIXED-A PIC X.")
        self.assertEqual([line.text.strip() for line in lines], ["01 GROUP-A.", "05 FIELD-A PIC X.", "01 FIXED-A PIC X."])


class CallBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        self.database = Path(self.temporary.name) / "facts.sqlite"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, name: str, text: str) -> None:
        (self.root / name).write_text(text, encoding="utf-8")

    def build(self) -> dict:
        return build_structural_index(self.root, self.database, quiet=True)

    def rows(self, sql: str, parameters: tuple = ()) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(sql, parameters).fetchall()

    def pair(self, call: str = "CALL 'WORKER' USING AMOUNT STATUS.", *,
             caller_fields: str = "01 AMOUNT PIC 9(8) COMP-5.\n01 STATUS PIC 9(4) COMP-5.",
             params: str = "IN-AMOUNT OUT-STATUS",
             callee_fields: str = "01 IN-AMOUNT PIC 9(8) COMP-5.\n01 OUT-STATUS PIC 9(4) COMP-5.",
             linkage: bool = True) -> None:
        self.write("ENTRY.cbl", program("ENTRYPG", caller_fields, call))
        self.write("WORKER.cbl", program("WORKER", "" if linkage else callee_fields,
                                        "MOVE 8 TO OUT-STATUS.", parameters=params,
                                        linkage=callee_fields if linkage else ""))

    def test_different_names_map_by_position_with_complete_provenance(self) -> None:
        self.pair()
        report = self.build()
        self.assertEqual(report["call_bindings"]["confirmed_bindings"], 2)
        rows = self.rows("SELECT * FROM relations WHERE relation_type = 'PASSES_AS' ORDER BY target_name")
        self.assertEqual([row["target_name"] for row in rows], ["IN-AMOUNT", "OUT-STATUS"])
        for row in rows:
            metadata = json.loads(row["metadata_json"])
            self.assertEqual(row["status"], "confirmed")
            self.assertEqual(metadata["passing_mode"], "REFERENCE")
            self.assertEqual(len(metadata["supporting_evidence_refs"]), 4)
            self.assertEqual(metadata["supporting_evidence_refs"][0]["evidence_id"], row["evidence_id"])
        writeback = self.rows("SELECT * FROM relations WHERE relation_type = 'MAY_WRITE_BACK'")
        self.assertEqual(len(writeback), 2)
        self.assertTrue(all(row["status"] == "candidate" for row in writeback))
        self.assertTrue(all(json.loads(row["metadata_json"])["boundary"] == "runtime_writeback_not_proven" for row in writeback))

    def test_content_and_value_never_create_writeback(self) -> None:
        self.pair("CALL 'WORKER' USING BY VALUE AMOUNT BY CONTENT STATUS.", params="BY VALUE IN-AMOUNT BY REFERENCE OUT-STATUS")
        self.build()
        self.assertEqual(len(self.rows("SELECT * FROM relations WHERE relation_type = 'PASSES_AS'")), 2)
        self.assertEqual(self.rows("SELECT * FROM relations WHERE relation_type = 'MAY_WRITE_BACK'"), [])

    def test_signature_mode_mismatch_is_not_confirmed(self) -> None:
        self.pair("CALL 'WORKER' USING BY VALUE AMOUNT BY REFERENCE STATUS.")
        self.build()
        rows = self.rows("SELECT * FROM call_bindings ORDER BY parameter_position")
        self.assertEqual(rows[0]["reason"], "parameter_mode_mismatch")
        self.assertEqual(rows[1]["status"], "confirmed")

    def test_value_abi_subset_rejects_display_decimal_group_and_oversize(self) -> None:
        for declaration in ("PIC 9(8)", "PIC 9(5)V99 COMP-3", "PIC 9(19) COMP-5"):
            with self.subTest(declaration=declaration):
                self.pair("CALL 'WORKER' USING BY VALUE AMOUNT.", caller_fields=f"01 AMOUNT {declaration}.",
                          params="BY VALUE IN-AMOUNT", callee_fields=f"01 IN-AMOUNT {declaration}.")
                self.build()
                self.assertEqual(self.rows("SELECT reason FROM call_bindings")[0][0], "by_value_layout_not_supported")
                self.assertEqual(self.rows("SELECT * FROM relations WHERE relation_type='PASSES_AS'"), [])

    def test_arity_mismatch_has_no_partial_position_guess(self) -> None:
        self.pair("CALL 'WORKER' USING AMOUNT.")
        self.build()
        self.assertEqual(self.rows("SELECT reason FROM call_bindings")[0][0], "parameter_arity_mismatch")
        self.assertEqual(self.rows("SELECT * FROM relations WHERE relation_type = 'PASSES_AS'"), [])
        metadata = json.loads(self.rows("SELECT metadata_json FROM relations WHERE relation_type = 'CALLS'")[0][0])
        self.assertEqual(metadata["boundary"], "call_parameter_binding_incomplete")

    def test_signature_edit_rebuilds_unchanged_caller_and_clears_stale_boundary(self) -> None:
        self.pair(params="OUT-STATUS IN-AMOUNT")
        self.build()
        self.assertEqual(len(self.rows("SELECT * FROM call_bindings WHERE status = 'unresolved'")), 2)
        self.write("WORKER.cbl", program("WORKER", "", "MOVE 8 TO OUT-STATUS.", parameters="IN-AMOUNT OUT-STATUS",
                                         linkage="01 IN-AMOUNT PIC 9(8) COMP-5.\n01 OUT-STATUS PIC 9(4) COMP-5."))
        report = self.build()
        self.assertEqual(report["files"]["skipped_unchanged"], 1)
        self.assertEqual(report["call_bindings"]["confirmed_bindings"], 2)
        metadata = json.loads(self.rows("SELECT metadata_json FROM relations WHERE relation_type = 'CALLS'")[0][0])
        self.assertNotIn("boundary", metadata)

    def test_removed_callee_removes_all_generated_parameter_edges(self) -> None:
        self.pair()
        self.build()
        (self.root / "WORKER.cbl").unlink()
        self.build()
        self.assertEqual(self.rows("SELECT * FROM relations WHERE relation_type IN ('PASSES_AS','MAY_WRITE_BACK')"), [])
        self.assertEqual(self.rows("SELECT reason FROM call_bindings")[0][0], "call_target_not_found")

    def test_unchanged_rebuild_keeps_stable_ids_without_duplicates(self) -> None:
        self.pair()
        self.build()
        before = [tuple(row) for row in self.rows("SELECT binding_id,callsite_id,caller_symbol_id,callee_symbol_id FROM call_bindings ORDER BY binding_id")]
        report = self.build()
        after = [tuple(row) for row in self.rows("SELECT binding_id,callsite_id,caller_symbol_id,callee_symbol_id FROM call_bindings ORDER BY binding_id")]
        self.assertEqual(before, after)
        self.assertEqual(report["files"]["skipped_unchanged"], 2)

    def test_multiline_signature_preserves_every_physical_line(self) -> None:
        self.pair(params="\n BY VALUE IN-AMOUNT\n BY REFERENCE OUT-STATUS", call="CALL 'WORKER' USING BY VALUE AMOUNT\n BY REFERENCE STATUS\n END-CALL.")
        self.build()
        signature = self.rows("SELECT u.start_line,u.end_line,e.text FROM code_units u JOIN evidence_spans e ON e.evidence_id=u.evidence_id WHERE unit_type='ProcedureSignature' AND program_name='WORKER'")[0]
        self.assertEqual(signature["end_line"] - signature["start_line"], 2)
        self.assertIn("BY VALUE IN-AMOUNT", signature["text"])
        self.assertIn("BY REFERENCE OUT-STATUS", signature["text"])
        self.assertEqual(len(self.rows("SELECT * FROM call_bindings WHERE status='confirmed'")), 2)

    def test_not_linkage_or_duplicate_definitions_are_boundaries(self) -> None:
        self.pair(linkage=False)
        self.build()
        self.assertEqual({row[0] for row in self.rows("SELECT reason FROM call_bindings")}, {"callee_not_linkage_parameter"})
        self.pair(caller_fields="01 AMOUNT PIC 9(8) COMP-5.\n01 AMOUNT PIC 9(8) COMP-5.\n01 STATUS PIC 9(4) COMP-5.")
        self.build()
        self.assertEqual(self.rows("SELECT reason FROM call_bindings WHERE parameter_position=1")[0][0], "caller_field_ambiguous")

    def test_ambiguous_program_target_is_not_resolved(self) -> None:
        self.pair()
        self.write("OTHER.cbl", (self.root / "WORKER.cbl").read_text())
        self.build()
        self.assertEqual(self.rows("SELECT reason FROM call_bindings")[0][0], "call_target_ambiguous")

    def test_compound_program_files_do_not_assume_nested_scope(self) -> None:
        self.pair()
        worker = (self.root / "WORKER.cbl").read_text()
        self.write("WORKER.cbl", worker + program("NESTEDPG", "", "CONTINUE."))
        self.build()
        self.assertEqual(self.rows("SELECT reason FROM call_bindings")[0][0], "program_scope_not_supported")
        self.assertEqual(self.rows("SELECT * FROM relations WHERE relation_type='PASSES_AS'"), [])

    def test_dynamic_call_keeps_target_unknown_despite_value_literal(self) -> None:
        self.pair("CALL ROUTE-NAME USING AMOUNT STATUS.", caller_fields="01 AMOUNT PIC 9(8) COMP-5.\n01 STATUS PIC 9(4) COMP-5.\n01 ROUTE-NAME PIC X(8) VALUE 'WORKER'.")
        self.build()
        binding = self.rows("SELECT * FROM call_bindings")[0]
        self.assertIsNone(binding["callee_program"])
        self.assertEqual(binding["reason"], "dynamic_target_not_resolved")
        self.assertEqual(self.rows("SELECT * FROM relations WHERE relation_type='PASSES_AS'"), [])

    def test_callsite_contexts_do_not_collapse_same_argument_pair(self) -> None:
        self.pair("CALL 'WORKER' USING AMOUNT STATUS.\nCALL 'WORKER' USING BY CONTENT AMOUNT STATUS.")
        self.build()
        rows = self.rows("SELECT * FROM call_bindings")
        self.assertEqual(len(rows), 4)
        self.assertEqual(len({row["callsite_id"] for row in rows}), 2)
        self.assertEqual(len(self.rows("SELECT * FROM relations WHERE relation_type='MAY_WRITE_BACK'")), 2)

    def test_compatible_renamed_groups_map_every_member(self) -> None:
        self.pair("CALL 'WORKER' USING REQUEST.", caller_fields="01 REQUEST.\n   05 AMOUNT PIC 9(8) COMP-5.\n   05 DETAIL.\n      10 FLAG PIC X.",
                  params="IN-REQUEST", callee_fields="01 IN-REQUEST.\n   05 IN-AMOUNT PIC 9(8) COMP-5.\n   05 IN-DETAIL.\n      10 IN-FLAG PIC X.")
        self.build()
        rows = self.rows("SELECT * FROM call_bindings ORDER BY group_member_index")
        self.assertEqual([row["group_member_index"] for row in rows], [0, 1, 2, 3])
        self.assertTrue(all(row["status"] == "confirmed" for row in rows))
        refs = json.loads(rows[-1]["supporting_evidence_ids_json"])
        source = self.rows("SELECT text FROM evidence_spans WHERE evidence_id=?", (refs[2],))[0][0]
        self.assertIn("01 REQUEST", source)
        self.assertIn("10 FLAG", source)

    def test_compatible_copy_groups_use_inclusion_section_and_source_evidence(self) -> None:
        self.write("AREA.cpy", "01 SHARED-AREA.\n   05 PAYLOAD PIC 9(8) COMP-5.\n   05 STATE-CODE PIC 9(4) COMP-5.\n")
        self.pair("CALL 'WORKER' USING SHARED-AREA.", caller_fields="COPY AREA.", params="SHARED-AREA", callee_fields="COPY AREA.")
        self.build()
        rows = self.rows("SELECT * FROM call_bindings ORDER BY group_member_index")
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row["status"] == "confirmed" for row in rows))
        self.assertNotEqual(rows[1]["caller_symbol_id"], rows[1]["callee_symbol_id"])
        refs = json.loads(rows[1]["supporting_evidence_ids_json"])
        self.assertEqual(len(refs), 5)

    def test_copy_definitions_alone_never_become_parameter_flow(self) -> None:
        self.write("AREA.cpy", "01 SHARED-AREA.\n   05 PAYLOAD PIC X.\n")
        self.write("ENTRY.cbl", program("ENTRYPG", "COPY AREA.", "CONTINUE."))
        self.write("WORKER.cbl", program("WORKER", "COPY AREA.", "CONTINUE."))
        self.build()
        self.assertEqual(self.rows("SELECT * FROM call_bindings"), [])

    def test_subordinate_copy_cannot_hide_consumer_occurs_or_redefines(self) -> None:
        for ancestor in ("05 ROW-AREA OCCURS 3 TIMES.", "05 ROW-AREA REDEFINES ORIGINAL-AREA."):
            with self.subTest(ancestor=ancestor):
                self.write("DETAIL.cpy", "10 AMOUNT PIC 9(4).\n")
                self.pair("CALL 'WORKER' USING AMOUNT.", caller_fields=f"01 REQUEST.\n{ancestor}\nCOPY DETAIL.",
                          params="IN-AMOUNT", callee_fields="01 IN-AMOUNT PIC 9(4).")
                self.build()
                self.assertEqual(self.rows("SELECT reason FROM call_bindings")[0][0], "parameter_layout_not_supported")
                self.assertEqual(self.rows("SELECT * FROM relations WHERE relation_type='PASSES_AS'"), [])

    def test_incomplete_copy_scope_suppresses_parameter_mapping(self) -> None:
        self.pair(caller_fields="01 AMOUNT PIC 9(8) COMP-5.\n01 STATUS PIC 9(4) COMP-5.\nCOPY MISSING.")
        self.build()
        self.assertEqual(self.rows("SELECT reason FROM call_bindings")[0][0], "copy_scope_incomplete")

    def test_unsupported_group_layout_or_leaf_ancestor_is_never_guessed(self) -> None:
        for argument in ("REQUEST", "AMOUNT"):
            with self.subTest(argument=argument):
                self.pair(f"CALL 'WORKER' USING {argument}.", caller_fields="01 REQUEST.\n   05 ROW-AREA OCCURS 3 TIMES.\n      10 AMOUNT PIC 9(8) COMP-5.",
                          params="IN-AMOUNT", callee_fields="01 IN-AMOUNT PIC 9(8) COMP-5.")
                self.build()
                self.assertEqual(self.rows("SELECT reason FROM call_bindings")[0][0], "parameter_layout_not_supported")

    def test_group_mismatch_does_not_partially_bind_members(self) -> None:
        self.pair("CALL 'WORKER' USING REQUEST.", caller_fields="01 REQUEST.\n05 AMOUNT PIC 9(8) COMP-5.\n05 FLAG PIC X.",
                  params="IN-REQUEST", callee_fields="01 IN-REQUEST.\n05 IN-AMOUNT PIC 9(8) COMP-5.\n05 IN-FLAG PIC X(2).")
        self.build()
        self.assertEqual(self.rows("SELECT reason FROM call_bindings")[0][0], "parameter_layout_mismatch")
        self.assertEqual(self.rows("SELECT * FROM relations WHERE relation_type='PASSES_AS'"), [])

    def test_hidden_unparsed_declaration_inside_group_prevents_layout_proof(self) -> None:
        self.pair("CALL 'WORKER' USING REQUEST.", caller_fields="01 REQUEST.\n05 AMOUNT PIC 9(8) COMP-5.\n>>DEFINE EXTRA 1\n05 FLAG PIC X.",
                  params="IN-REQUEST", callee_fields="01 IN-REQUEST.\n05 IN-AMOUNT PIC 9(8) COMP-5.\n05 IN-FLAG PIC X.")
        self.build()
        self.assertEqual(self.rows("SELECT reason FROM call_bindings")[0][0], "parameter_layout_not_supported")

    def test_multiline_scalar_declaration_keeps_complete_layout(self) -> None:
        self.pair(caller_fields="01 AMOUNT\n PIC 9(8)\n USAGE COMP-5.\n01 STATUS PIC 9(4) COMP-5.")
        report = self.build()
        self.assertEqual(report["call_bindings"]["confirmed_bindings"], 2)

    def test_old_parser_version_forces_full_refresh(self) -> None:
        self.pair()
        self.build()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE metadata SET value='old-parser' WHERE key='parser_version'")
        report = self.build()
        self.assertTrue(report["parser_rebuild_required"])
        self.assertEqual(report["files"]["indexed_or_updated"], 2)


if __name__ == "__main__":
    unittest.main()
