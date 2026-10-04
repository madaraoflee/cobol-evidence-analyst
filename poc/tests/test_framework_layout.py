from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework_layout import validate_framework_layouts


def fact() -> dict:
    return {"fact_id": "operation-1", "kind": "framework_operation", "relative_path": "entry.cbl",
            "source_sha256": "a" * 64, "program_name": "ENTRYPG", "function_field": "ROWS-FUNCTION",
            "argument": "ROWS-PARAMS", "operation": {"value": "ADVANCE", "meaning": "Read the next record."},
            "observed_function_value": "ADVANCE", "dependency_covered": True,
            "reason": "documented_operation_bound", "source_ranges": [{"start_line": 20, "end_line": 20, "role": "callsite"}]}


class FrameworkLayoutTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE code_units(unit_id TEXT,relative_path TEXT,unit_type TEXT,name TEXT,
                program_name TEXT,parent_unit_id TEXT,start_line INTEGER,end_line INTEGER,
                normalized_text TEXT,evidence_id TEXT,parse_status TEXT);
            CREATE TABLE evidence_spans(evidence_id TEXT PRIMARY KEY,source_sha256 TEXT);
            CREATE TABLE relations(relation_id TEXT,relative_path TEXT,from_entity_id TEXT,
                relation_type TEXT,target_name TEXT,target_entity_id TEXT,status TEXT,metadata_json TEXT);
            CREATE TABLE symbols(symbol_id TEXT,relative_path TEXT,symbol_type TEXT);
        """)
        self.unit("storage", "Section", "WORKING-STORAGE", "WORKING-STORAGE SECTION.", 4, parent=None)

    def unit(self, identity, kind, name, text, line, *, path="entry.cbl", program="ENTRYPG", parent="storage", sha=None, complete=True):
        evidence = "ev-" + identity
        self.db.execute("INSERT INTO code_units VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (identity, path, kind, name, program, parent, line, line, text, evidence, "complete" if complete else "partial"))
        self.db.execute("INSERT OR REPLACE INTO evidence_spans VALUES(?,?)", (evidence, sha or ("a" if path == "entry.cbl" else "b") * 64))

    def declarations(self, function="05 ROWS-FUNCTION PIC X(8).", group="01 ROWS-PARAMS.", **kwargs):
        self.unit("argument", "DataItem", "ROWS-PARAMS", group, 5, **kwargs)
        self.unit("function", "DataItem", "ROWS-FUNCTION", function, 6, **kwargs)

    def result(self, original=None):
        result, = validate_framework_layouts(self.db, [fact() if original is None else original])
        return result

    def copy(self, *, status="confirmed", metadata=None, statement="COPY ROW-LAYOUT.", nested=False):
        self.unit("copy", "Statement", "DEPENDENCY", statement, 5)
        self.db.execute("INSERT INTO relations VALUES(?,?,?,?,?,?,?,?)",
                        ("include", "entry.cbl", "copy", "INCLUDES_COPY", "ROW-LAYOUT", "copy-symbol" if status == "confirmed" else None,
                         status, json.dumps(metadata or {})))
        self.db.execute("INSERT INTO symbols VALUES(?,?,?)", ("copy-symbol", "row-layout.cpy", "Copybook"))
        if nested:
            self.db.execute("INSERT INTO relations VALUES(?,?,?,?,?,?,?,?)",
                            ("nested", "row-layout.cpy", "nested-unit", "INCLUDES_COPY", "INNER-LAYOUT", None, "unresolved", "{}"))

    def test_simple_group_confirms_only_local_storage_and_adds_evidence(self):
        self.declarations()
        original = fact()
        untouched = deepcopy(original)
        result = self.result(original)
        self.assertTrue(result["dependency_covered"])
        self.assertEqual(result["layout_validation"]["status"], "confirmed")
        self.assertEqual(result["layout_validation"]["function_length"], 8)
        self.assertFalse(result["layout_validation"]["callee_abi_verified"])
        self.assertEqual({ref["evidence_id"] for ref in result["layout_evidence_refs"]}, {"ev-function", "ev-argument"})
        self.assertEqual(original, untouched)

    def test_truncation_withdraws_known_operation(self):
        self.declarations("05 ROWS-FUNCTION PIC X(3).")
        result = self.result()
        self.assertFalse(result["dependency_covered"])
        self.assertEqual(result["reason"], "framework_function_literal_truncated")
        self.assertIsNone(result["operation"])
        self.assertEqual(result["documented_operation_candidate"]["value"], "ADVANCE")

    def test_long_form_picture_and_explicit_display_are_supported(self):
        self.declarations("05 ROWS-FUNCTION PICTURE IS XXXXXXXX USAGE IS DISPLAY VALUE SPACES.")
        self.assertTrue(self.result()["dependency_covered"])

    def test_numeric_national_justified_and_binary_fields_are_not_alphanumeric_proof(self):
        for tail in ("PIC 9(8)", "PIC N(8)", "PIC X(8) COMP", "PIC X(8) JUSTIFIED RIGHT", "PIC X(8) USAGE NATIONAL"):
            with self.subTest(tail=tail):
                self.db.execute("DELETE FROM code_units WHERE unit_type='DataItem'")
                self.declarations("05 ROWS-FUNCTION " + tail + ".")
                result = self.result()
                self.assertFalse(result["dependency_covered"])
                self.assertEqual(result["reason"], "framework_function_storage_not_supported")

    def test_function_must_be_member_of_actual_argument_group(self):
        self.declarations("01 ROWS-FUNCTION PIC X(8).")
        result = self.result()
        self.assertEqual(result["reason"], "framework_function_outside_argument")
        self.assertFalse(result["dependency_covered"])

    def test_nested_plain_group_retains_all_ancestor_evidence(self):
        self.unit("argument", "DataItem", "ROWS-PARAMS", "01 ROWS-PARAMS.", 5)
        self.unit("nested", "DataItem", "CONTROL-GROUP", "05 CONTROL-GROUP.", 6)
        self.unit("function", "DataItem", "ROWS-FUNCTION", "10 ROWS-FUNCTION PIC X(8).", 7)
        result = self.result()
        self.assertTrue(result["dependency_covered"])
        self.assertEqual({ref["evidence_id"] for ref in result["layout_evidence_refs"]}, {"ev-argument", "ev-function", "ev-nested"})

    def test_alias_and_occurs_on_group_or_descendant_are_boundaries(self):
        self.declarations(group="01 ROWS-PARAMS OCCURS 2 TIMES.")
        self.assertEqual(self.result()["reason"], "framework_layout_alias_or_occurs_not_supported")
        self.db.execute("UPDATE code_units SET normalized_text='01 ROWS-PARAMS.' WHERE unit_id='argument'")
        self.unit("alias", "DataItem", "ALTERNATE", "05 ALTERNATE REDEFINES ROWS-FUNCTION PIC X(8).", 7)
        self.assertEqual(self.result()["reason"], "framework_layout_alias_or_occurs_not_supported")
        self.db.execute("UPDATE code_units SET normalized_text='01 ALTERNATE REDEFINES ROWS-PARAMS.' WHERE unit_id='alias'")
        self.assertEqual(self.result()["reason"], "framework_layout_alias_or_occurs_not_supported")

    def test_duplicate_or_missing_field_never_confirms(self):
        self.assertEqual(self.result()["reason"], "framework_layout_declaration_missing")
        self.declarations()
        self.unit("duplicate", "DataItem", "ROWS-FUNCTION", "05 ROWS-FUNCTION PIC X(8).", 7)
        self.assertEqual(self.result()["reason"], "framework_layout_field_ambiguous")

    def test_incomplete_or_stale_declaration_withdraws_coverage(self):
        self.declarations(complete=False)
        self.assertEqual(self.result()["reason"], "framework_layout_declaration_incomplete")
        self.db.execute("UPDATE code_units SET parse_status='complete'")
        self.db.execute("UPDATE evidence_spans SET source_sha256=? WHERE evidence_id='ev-function'", ("c" * 64,))
        self.assertEqual(self.result()["reason"], "framework_layout_source_mismatch")

    def test_sections_and_source_precedence_remain_untouched(self):
        original = fact()
        original.update(kind="framework_section")
        self.assertEqual(self.result(original), original)
        original = fact()
        original.update(dependency_covered=False, target_source_available=True, reason="source_implementation_takes_precedence")
        self.assertEqual(self.result(original), original)

    def test_missing_value_is_not_upgraded_by_valid_storage(self):
        self.declarations()
        original = fact()
        original.update(dependency_covered=False, operation=None, observed_function_value=None, reason="function_value_not_bound")
        result = self.result(original)
        self.assertFalse(result["dependency_covered"])
        self.assertEqual(result["reason"], "function_value_not_bound")
        self.assertEqual(result["layout_validation"]["status"], "confirmed")

    def test_plain_confirmed_copy_binds_and_preserves_both_source_files(self):
        self.copy()
        self.declarations(path="row-layout.cpy", program="ROW-LAYOUT", parent=None)
        result = self.result()
        self.assertTrue(result["dependency_covered"])
        self.assertEqual({ref["relative_path"] for ref in result["layout_evidence_refs"]}, {"entry.cbl", "row-layout.cpy"})
        self.assertIn("copy_inclusion", {ref["role"] for ref in result["source_ranges"]})

    def test_subordinate_copy_inherits_visible_enclosing_group(self):
        self.unit("argument", "DataItem", "ROWS-PARAMS", "01 ROWS-PARAMS.", 4)
        self.copy()
        self.unit("function", "DataItem", "ROWS-FUNCTION", "05 ROWS-FUNCTION PIC X(8).", 1,
                  path="row-layout.cpy", program="ROW-LAYOUT", parent=None)
        self.assertTrue(self.result()["dependency_covered"])

    def test_missing_ambiguous_replacing_and_nested_copy_stay_unresolved(self):
        self.copy(status="unresolved")
        self.assertEqual(self.result()["reason"], "framework_layout_copy_unresolved")
        self.db.execute("UPDATE relations SET status='candidate'")
        self.assertEqual(self.result()["reason"], "framework_layout_copy_unresolved")
        self.db.execute("UPDATE code_units SET normalized_text='COPY ROW-LAYOUT REPLACING ==A== BY ==B==.' WHERE unit_id='copy'")
        self.assertEqual(self.result()["reason"], "framework_layout_copy_form_not_supported")
        self.db.execute("UPDATE code_units SET normalized_text='COPY ROW-LAYOUT.' WHERE unit_id='copy'")
        self.db.execute("UPDATE relations SET status='confirmed',target_entity_id='copy-symbol'")
        self.db.execute("INSERT INTO relations VALUES('nested','row-layout.cpy','nested','INCLUDES_COPY','INNER',NULL,'unresolved','{}')")
        self.assertEqual(self.result()["reason"], "framework_layout_nested_copy_not_supported")

    def test_duplicate_copy_inclusion_is_ambiguous(self):
        self.copy()
        self.declarations(path="row-layout.cpy", program="ROW-LAYOUT", parent=None)
        self.unit("copy-2", "Statement", "DEPENDENCY", "COPY ROW-LAYOUT.", 7)
        self.db.execute("INSERT INTO relations VALUES('include-2','entry.cbl','copy-2','INCLUDES_COPY','ROW-LAYOUT','copy-symbol','confirmed','{}')")
        self.assertEqual(self.result()["reason"], "framework_layout_field_ambiguous")

    def test_other_programs_and_storage_sections_do_not_supply_membership(self):
        self.declarations(program="OTHERPG")
        self.assertEqual(self.result()["reason"], "framework_layout_declaration_missing")
        self.db.execute("UPDATE code_units SET program_name='ENTRYPG'")
        self.unit("linkage", "Section", "LINKAGE", "LINKAGE SECTION.", 6, parent=None)
        self.db.execute("UPDATE code_units SET parent_unit_id='linkage',start_line=7 WHERE unit_id='function'")
        self.assertEqual(self.result()["reason"], "framework_function_outside_argument")

    def test_directive_before_program_header_with_null_owner_withdraws_coverage(self):
        self.declarations()
        self.unit("directive", "PreprocessorDirective", "REPLACE", "REPLACE =='ADVANCE'== BY =='STORE'==.",
                  1, program=None, parent=None)
        result = self.result()
        self.assertFalse(result["dependency_covered"])
        self.assertEqual(result["reason"], "framework_preprocessing_not_resolved")
        self.assertIsNone(result["operation"])
        self.assertIn("ev-directive", {ref["evidence_id"] for ref in result["layout_evidence_refs"]})

    def test_data_copy_directive_withdraws_literal_and_layout_proof(self):
        self.copy()
        self.declarations(path="row-layout.cpy", program="ROW-LAYOUT", parent=None)
        self.unit("directive", "PreprocessorDirective", "REPLACE", "REPLACE =='ADVANCE'== BY =='STORE'==.",
                  1, path="row-layout.cpy", program="ROW-LAYOUT", parent=None)
        result = self.result()
        self.assertFalse(result["dependency_covered"])
        self.assertEqual(result["reason"], "framework_copy_preprocessing_not_resolved")
        self.assertEqual({ref["relative_path"] for ref in result["layout_evidence_refs"]}, {"entry.cbl", "row-layout.cpy"})

    def test_procedure_copy_is_checked_for_directives_and_nested_copies(self):
        self.declarations()
        self.copy()
        self.db.execute("UPDATE code_units SET parent_unit_id=NULL,start_line=15 WHERE unit_id='copy'")
        self.assertTrue(self.result()["dependency_covered"])
        self.unit("directive", "PreprocessorDirective", ">>", ">>IF OPTION-ENABLED",
                  1, path="row-layout.cpy", program="ROW-LAYOUT", parent=None)
        result = self.result()
        self.assertFalse(result["dependency_covered"])
        self.assertEqual(result["reason"], "framework_copy_preprocessing_not_resolved")
        self.db.execute("DELETE FROM code_units WHERE unit_type='PreprocessorDirective'")
        self.db.execute("INSERT INTO relations VALUES('nested','row-layout.cpy','nested','INCLUDES_COPY','INNER',NULL,'unresolved','{}')")
        self.assertEqual(self.result()["reason"], "framework_layout_nested_copy_not_supported")

    def test_copy_before_program_header_cannot_hide_preprocessing(self):
        self.declarations()
        self.copy()
        self.db.execute("UPDATE code_units SET parent_unit_id=NULL,program_name=NULL,start_line=1 WHERE unit_id='copy'")
        self.unit("directive", "PreprocessorDirective", "PROCESS", "PROCESS OPTIONS",
                  1, path="row-layout.cpy", program="ROW-LAYOUT", parent=None)
        result = self.result()
        self.assertFalse(result["dependency_covered"])
        self.assertEqual(result["reason"], "framework_copy_preprocessing_not_resolved")


if __name__ == "__main__":
    unittest.main()
