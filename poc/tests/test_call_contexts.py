from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from call_contexts import audit_call_contexts
from structural_index import build_structural_index


def program(name: str, body: str, *, working: str = "", linkage: str = "", using: str = "") -> str:
    return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
            + (f"WORKING-STORAGE SECTION.\n{working}\n" if working else "")
            + (f"LINKAGE SECTION.\n{linkage}\n" if linkage else "")
            + f"PROCEDURE DIVISION{(' USING ' + using) if using else ''}.\nMAIN-ENTRY.\n{body}\nGOBACK.\n")


class CallContextTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "source"
        self.root.mkdir()
        self.database = Path(self.temporary.name) / "facts.sqlite"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, filename: str, text: str) -> None:
        (self.root / filename).write_text(text, encoding="utf-8")

    def build(self) -> dict:
        return build_structural_index(self.root, self.database, quiet=True)

    def audit(self, **budgets: int) -> dict:
        return audit_call_contexts(self.database, "ROOTPG", **budgets)

    def mutate(self, sql: str, params: tuple = ()) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(sql, params)
            connection.commit()

    def pair(self, calls: str = "CALL 'WORKER' USING INPUT-A STATUS-A.") -> None:
        self.write("root.cbl", program("ROOTPG", calls, working=(
            "01 INPUT-A PIC 9(4) COMP-5.\n01 STATUS-A PIC 9(4) COMP-5.\n"
            "01 INPUT-B PIC 9(4) COMP-5.\n01 STATUS-B PIC 9(4) COMP-5.\n01 TARGET-NAME PIC X(16).")))
        self.write("worker.cbl", program("WORKER", "MOVE 8 TO OUT-STATUS.",
            linkage="01 IN-VALUE PIC 9(4) COMP-5.\n01 OUT-STATUS PIC 9(4) COMP-5.", using="IN-VALUE OUT-STATUS"))

    def test_two_calls_keep_error_and_value_parameter_identities_separate(self) -> None:
        self.pair("CALL 'WORKER' USING INPUT-A STATUS-A.\nCALL 'WORKER' USING INPUT-B STATUS-B.")
        build = self.build()
        result = self.audit()
        self.assertEqual(result["snapshot_id"], build["snapshot_id"])
        children = result["contexts"][1:]
        self.assertEqual(len(children), 2)
        self.assertEqual(result["summary"]["parameter_mappings"], 4)
        self.assertEqual(result["summary"]["programs_with_multiple_contexts"], {"WORKER": 2})
        first, second = [child["parameter_mappings"][1] for child in children]
        self.assertEqual(first["callee_field"]["symbol_id"], second["callee_field"]["symbol_id"])
        self.assertNotEqual(first["callee_field"]["field_instance_id"], second["callee_field"]["field_instance_id"])
        self.assertEqual(first["caller_field"]["name"], "STATUS-A")
        self.assertEqual(second["caller_field"]["name"], "STATUS-B")
        self.assertEqual(first["callee_field"]["storage_section"], "LINKAGE")
        self.assertEqual(first["caller_field"]["storage_section"], "WORKING-STORAGE")

    def test_nested_shared_callsite_keeps_entire_ancestor_chain(self) -> None:
        self.pair("CALL 'WORKER' USING INPUT-A STATUS-A.\nCALL 'WORKER' USING INPUT-B STATUS-B.")
        self.write("worker.cbl", program("WORKER", "CALL 'LEAFPG' USING IN-VALUE OUT-STATUS.",
            linkage="01 IN-VALUE PIC 9(4) COMP-5.\n01 OUT-STATUS PIC 9(4) COMP-5.", using="IN-VALUE OUT-STATUS"))
        self.write("leaf.cbl", program("LEAFPG", "MOVE 2 TO LEAF-STATUS.",
            linkage="01 LEAF-VALUE PIC 9(4) COMP-5.\n01 LEAF-STATUS PIC 9(4) COMP-5.", using="LEAF-VALUE LEAF-STATUS"))
        self.build()
        result = self.audit()
        leaves = [context for context in result["contexts"] if context["program_name"] == "LEAFPG"]
        self.assertEqual(len(leaves), 2)
        self.assertEqual(leaves[0]["via_callsite_id"], leaves[1]["via_callsite_id"])
        self.assertNotEqual(leaves[0]["context_id"], leaves[1]["context_id"])
        self.assertNotEqual(leaves[0]["callsite_chain"][0], leaves[1]["callsite_chain"][0])
        self.assertEqual([leaf["depth"] for leaf in leaves], [2, 2])
        self.assertNotEqual(leaves[0]["parameter_mappings"][1]["caller_field"]["field_instance_id"],
                            leaves[1]["parameter_mappings"][1]["caller_field"]["field_instance_id"])

    def test_content_and_value_are_copy_in_without_writeback(self) -> None:
        self.pair("CALL 'WORKER' USING BY VALUE INPUT-A BY CONTENT STATUS-A.")
        self.write("worker.cbl", program("WORKER", "MOVE 8 TO OUT-STATUS.",
            linkage="01 IN-VALUE PIC 9(4) COMP-5.\n01 OUT-STATUS PIC 9(4) COMP-5.",
            using="BY VALUE IN-VALUE BY REFERENCE OUT-STATUS"))
        self.build()
        result = self.audit()
        mappings = result["contexts"][1]["parameter_mappings"]
        self.assertEqual([row["passing_mode"] for row in mappings], ["VALUE", "CONTENT"])
        self.assertTrue(all(row["transfer"] == "copy_in" and not row["possible_writeback"] for row in mappings))

    def test_repeated_reference_actual_has_alias_boundary(self) -> None:
        self.pair("CALL 'WORKER' USING INPUT-A INPUT-A.")
        self.build()
        result = self.audit()
        alias = [item for item in result["boundaries"] if item["reason"] == "duplicate_reference_actual_alias_possible"]
        self.assertEqual(len(alias), 1)
        self.assertEqual(alias[0]["parameter_positions"], [1, 2])

    def test_group_member_and_elementary_actual_overlap_is_reported(self) -> None:
        self.write("root.cbl", program("ROOTPG", "CALL 'WORKER' USING GROUP-A FIELD-A.",
            working="01 GROUP-A.\n05 FIELD-A PIC 9(4) COMP-5."))
        self.write("worker.cbl", program("WORKER", "CONTINUE.", using="GROUP-B FIELD-C",
            linkage="01 GROUP-B.\n05 FIELD-B PIC 9(4) COMP-5.\n01 FIELD-C PIC 9(4) COMP-5."))
        self.build()
        result = self.audit()
        self.assertEqual(result["summary"]["parameter_mappings"], 3)
        self.assertEqual(result["summary"]["boundary_counts"], {"duplicate_reference_actual_alias_possible": 1})

    def test_copy_field_storage_comes_from_inclusion_site(self) -> None:
        self.write("area.cpy", "01 VALUE-AREA.\n05 AREA-FIELD PIC 9(4) COMP-5.\n")
        self.write("root.cbl", program("ROOTPG", "CALL 'WORKER' USING VALUE-AREA.", working="COPY AREA."))
        self.write("worker.cbl", program("WORKER", "CONTINUE.", linkage="COPY AREA.", using="VALUE-AREA"))
        self.build()
        result = self.audit()
        mappings = result["contexts"][1]["parameter_mappings"]
        self.assertEqual(len(mappings), 2)
        self.assertTrue(all(item["caller_field"]["storage_section"] == "WORKING-STORAGE" and
                            item["callee_field"]["storage_section"] == "LINKAGE" for item in mappings))

    def test_no_arguments_still_creates_confirmed_static_context(self) -> None:
        self.write("root.cbl", program("ROOTPG", "CALL 'WORKER'."))
        self.write("worker.cbl", program("WORKER", "CONTINUE."))
        self.build()
        child = self.audit()["contexts"][1]
        self.assertTrue(child["parameter_mapping_complete"])
        self.assertEqual(child["parameter_mappings"], [])

    def test_partial_mapping_preserves_only_confirmed_parameters(self) -> None:
        self.pair("CALL 'WORKER' USING BY VALUE INPUT-A BY REFERENCE STATUS-A.")
        self.build()
        result = self.audit()
        child = result["contexts"][1]
        self.assertFalse(child["parameter_mapping_complete"])
        self.assertEqual([row["parameter_position"] for row in child["parameter_mappings"]], [2])
        self.assertEqual(result["boundaries"][0]["binding_reason"], "parameter_mode_mismatch")

    def test_arity_mismatch_does_not_invent_mapping(self) -> None:
        self.pair("CALL 'WORKER' USING INPUT-A.")
        self.build()
        child = self.audit()["contexts"][1]
        self.assertFalse(child["parameter_mapping_complete"])
        self.assertEqual(child["parameter_mappings"], [])

    def test_dynamic_call_and_recursion_stop_at_explicit_boundaries(self) -> None:
        self.pair("CALL TARGET-NAME USING INPUT-A STATUS-A.\nCALL 'ROOTPG'.")
        self.build()
        result = self.audit()
        self.assertEqual(len(result["contexts"]), 1)
        self.assertEqual(result["summary"]["boundary_counts"], {
            "dynamic_target_not_resolved": 1, "recursive_call_chain_not_expanded": 1})

    def test_duplicate_program_names_and_compound_scope_fail_closed(self) -> None:
        for compound in (False, True):
            with self.subTest(compound=compound):
                self.pair()
                if compound:
                    self.write("root.cbl", program("ROOTPG", "CALL 'WORKER'.") + program("INNERPG", "CONTINUE."))
                else:
                    self.write("duplicate.cbl", program("ROOTPG", "CONTINUE."))
                self.build()
                result = self.audit()
                self.assertEqual(result["contexts"], [])
                self.assertIn(result["boundaries"][0]["reason"], {"program_scope_ambiguous", "compound_program_scope_not_supported"})
                if not compound:
                    (self.root / "duplicate.cbl").unlink()

    def test_ambiguous_callee_never_gets_a_child_context(self) -> None:
        self.pair()
        self.write("duplicate.cbl", program("WORKER", "CONTINUE."))
        self.build()
        result = self.audit()
        self.assertEqual(len(result["contexts"]), 1)
        self.assertEqual(result["boundaries"][0]["reason"], "program_scope_ambiguous")

    def test_context_depth_and_binding_budgets_are_explicit(self) -> None:
        self.pair("CALL 'WORKER' USING INPUT-A STATUS-A.\nCALL 'WORKER' USING INPUT-B STATUS-B.")
        self.build()
        cases = [("context_budget_exhausted", {"max_contexts": 1}),
                 ("call_depth_budget_exhausted", {"max_depth": 0}),
                 ("binding_budget_exhausted", {"max_bindings": 1})]
        for reason, kwargs in cases:
            with self.subTest(reason=reason):
                result = self.audit(**kwargs)
                self.assertTrue(result["summary"]["truncated"])
                self.assertIn(reason, result["summary"]["boundary_counts"])
                self.assertLessEqual(result["summary"]["parameter_mappings"], kwargs.get("max_bindings", 5000))
        self.assertTrue(all(not context["parameter_mapping_complete"] for context in self.audit(max_bindings=0)["contexts"][1:]))

    def test_call_expansion_budget_caps_append_loop(self) -> None:
        self.pair("\n".join("CALL 'WORKER' USING INPUT-A STATUS-A." for _ in range(6)))
        self.build()
        with patch("call_contexts.MAX_CALL_EXPANSIONS", 2):
            result = self.audit()
        self.assertEqual(result["summary"]["callsites_considered"], 2)
        self.assertEqual(result["summary"]["contexts"], 3)
        self.assertTrue(result["summary"]["truncated"])

    def test_boundary_output_and_fact_text_have_independent_limits(self) -> None:
        self.pair("\n".join("CALL TARGET-NAME USING INPUT-A STATUS-A." for _ in range(6)))
        self.build()
        with patch("call_contexts.MAX_BOUNDARIES", 2):
            result = self.audit()
        self.assertEqual(len(result["boundaries"]), 2)
        self.assertEqual(result["summary"]["boundaries"], 6)
        self.assertTrue(result["summary"]["boundaries_truncated"])
        with patch("call_contexts.MAX_TEXT_BYTES", 8), self.assertRaisesRegex(ValueError, "text budget"):
            self.audit()

    def test_boolean_or_invalid_budgets_are_rejected(self) -> None:
        self.pair()
        self.build()
        for kwargs in ({"max_bindings": True}, {"max_depth": False}, {"max_contexts": 0},
                       {"max_depth": -1}, {"max_bindings": 50001}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.audit(**kwargs)

    def test_static_contexts_do_not_claim_conditional_or_post_exit_execution(self) -> None:
        self.pair("IF INPUT-A = ZERO\nCALL 'WORKER' USING INPUT-A STATUS-A\nEND-IF.\n"
                  "GOBACK.\nDORMANT-PATH.\nCALL 'WORKER' USING INPUT-B STATUS-B.")
        self.build()
        result = self.audit()
        self.assertEqual(len(result["contexts"]), 3)
        self.assertTrue(result["contexts"][1]["callsite_controls"])
        self.assertFalse(result["runtime_execution_tested"])
        self.assertFalse(result["complete"])
        self.assertIn("unreachable", result["reachability_scope"])
        self.assertTrue(any("Repeated PERFORM" in item for item in result["limitations"]))
        self.assertTrue(any("WORKING-STORAGE" in item for item in result["limitations"]))

    def test_missing_binding_rows_are_not_fresh_local_parameters(self) -> None:
        self.pair()
        self.build()
        self.mutate("DELETE FROM call_bindings")
        result = self.audit()
        self.assertFalse(result["contexts"][1]["parameter_mapping_complete"])
        self.assertEqual(result["boundaries"][0]["reason"], "parameter_bindings_missing")

    def test_evaluate_when_call_controls_preserve_source_arm(self) -> None:
        self.pair("EVALUATE INPUT-A\nWHEN ZERO\nCALL 'WORKER' USING INPUT-A STATUS-A\n"
                  "WHEN OTHER\nCONTINUE\nEND-EVALUATE.")
        self.build()
        child = self.audit()["contexts"][1]
        self.assertEqual(child["callsite_controls"][0]["outcome"], "WHEN ZERO")

    def test_snapshot_manifest_mismatch_is_rejected_even_with_valid_hash_shape(self) -> None:
        self.pair()
        self.build()
        self.mutate("UPDATE metadata SET value = ? WHERE key = 'snapshot_id'", ("sha256:" + "a" * 64,))
        with self.assertRaisesRegex(ValueError, "manifest"):
            self.audit()

    def test_corrupt_evidence_hash_range_text_and_missing_rows_are_rejected(self) -> None:
        changes = ["UPDATE evidence_spans SET source_sha256 = 'broken'",
                   "UPDATE evidence_spans SET start_line = 0",
                   "UPDATE evidence_spans SET text = 'CORRUPTED' WHERE evidence_id IN (SELECT evidence_id FROM code_units WHERE name = 'CALL')",
                   "DELETE FROM evidence_spans WHERE evidence_id IN (SELECT evidence_id FROM code_units WHERE name = 'CALL')"]
        for sql in changes:
            with self.subTest(sql=sql):
                self.pair()
                # A new database is used because the builder deliberately treats
                # unchanged source files as a cache hit, not an index repair.
                if self.database.exists():
                    self.database.unlink()
                self.build()
                self.mutate(sql)
                with self.assertRaises(ValueError):
                    self.audit()

    def test_corrupt_parameter_scope_or_mode_is_rejected(self) -> None:
        self.pair()
        self.build()
        self.mutate("UPDATE call_bindings SET caller_program = 'OTHERPG'")
        with self.assertRaisesRegex(ValueError, "scope"):
            self.audit()

    def test_audit_is_deterministic_and_does_not_modify_database(self) -> None:
        self.pair("CALL 'WORKER' USING INPUT-A STATUS-A.\nCALL 'WORKER' USING INPUT-B STATUS-B.")
        self.build()
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        first, second = self.audit(), self.audit()
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())
        refs = first["contexts"][1]["parameter_mappings"][0]["evidence_refs"]
        self.assertTrue(all(len(ref["span_sha256"]) == 64 and len(ref["source_sha256"]) == 64 for ref in refs))


if __name__ == "__main__":
    unittest.main()
