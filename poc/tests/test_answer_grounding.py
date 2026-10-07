"""Source-bound call observations, without a model."""

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from answer_grounding import answer_grounding_risks


def page(name, body, *, identifier=None):
    text = (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nPROCEDURE DIVISION.\n"
            f"MAIN.\n{body}\nGOBACK.\n")
    return {"evidence_id": identifier or f"ev_{name}", "relative_path": f"{name}.cbl",
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(), "start_line": 1,
            "end_line": len(text.splitlines()), "source_text": text, "include_chain": []}


class GroundingRiskTests(unittest.TestCase):
    @staticmethod
    def kinds(result):
        return {row["kind"] for row in result["reasons"]}

    def test_literal_call_with_supplied_implementation_is_not_external_risk(self):
        pages = [page("ROOT", "CALL 'LEAF' END-CALL."), page("LEAF", "MOVE 1 TO OUTPUT-AMOUNT.")]
        self.assertFalse(answer_grounding_risks(pages)["required"])
        result = answer_grounding_risks(pages[:1])
        self.assertEqual(self.kinds(result), {"call_implementation_not_supplied"})
        self.assertEqual(result["reasons"][0]["supplied_reference_ids"], ["ev_ROOT"])

    def test_value_and_content_boundaries_are_detected_without_named_program_rules(self):
        for mode in ("VALUE", "CONTENT"):
            with self.subTest(mode=mode):
                pages = [page("ROOT", f"CALL 'LEAF' USING BY {mode} STATUS-FIELD END-CALL."),
                         page("LEAF", "MOVE 1 TO STATUS-FIELD.")]
                result = answer_grounding_risks(pages)
                self.assertEqual(self.kinds(result), {"parameter_copy_boundary"})
                self.assertEqual(result["reasons"][0]["copy_modes"], [mode])

    def test_dynamic_target_and_distinct_exception_sites_remain_distinct(self):
        source = page("ROOT", "CALL 'KNOWN' ON EXCEPTION MOVE 1 TO STATUS-FIELD END-CALL.\n"
                      "CALL TARGET-NAME ON EXCEPTION MOVE 2 TO STATUS-FIELD END-CALL.")
        result = answer_grounding_risks([source, page("KNOWN", "CONTINUE.")])
        self.assertIn("runtime_call_target", self.kinds(result))
        handlers = [row for row in result["reasons"] if row["kind"] == "multiple_call_exception_sites"]
        self.assertEqual(len(handlers), 2)
        self.assertEqual({row["start_line"] for row in handlers}, {5, 6})

    def test_comments_and_literal_instruction_text_do_not_become_calls(self):
        source = page("ROOT", "*> CALL 'REMOTE' USING BY VALUE STATUS-FIELD.\n"
                      "MOVE 'CALL REMOTE BY CONTENT' TO MESSAGE-TEXT.")
        self.assertFalse(answer_grounding_risks([source])["required"])

    def test_navigation_fallback_requires_the_actual_callsite_to_be_supplied(self):
        source = page("ROOT", "EXEC SQL SELECT COL INTO :HOST END-EXEC.\nCALL TARGET-NAME END-CALL.")
        source.update(start_line=6, end_line=6, source_text=source["source_text"].splitlines()[5])
        relation = {"relation_type": "CALL_TARGET_FROM", "caller_path": "ROOT.cbl",
                    "caller_line": 6, "target_name": "TARGET-NAME"}
        result = answer_grounding_risks([source], business_map={"relations": [relation]})
        self.assertEqual(self.kinds(result), {"runtime_call_target"})
        outside = {**relation, "caller_line": 1000}
        self.assertFalse(answer_grounding_risks([source], business_map={"relations": [outside]})["required"])

    def test_later_and_nested_calls_survive_unsupported_statement_ast_syntax(self):
        source = page("ROOT", "INITIALIZE OUTPUT-AREA.\n"
                      "PERFORM UNTIL DONE-FLAG = 1\n"
                      "CALL 'FIRST-STORE' END-CALL\n"
                      "MOVE 1 TO DONE-FLAG\nEND-PERFORM.\n"
                      "PERFORM 9000-CHECK.\n"
                      "CALL 'SECOND-STORE' ON EXCEPTION\n"
                      "CALL 'RECOVERY-STORE' USING BY CONTENT REQUEST-AREA END-CALL\n"
                      "END-CALL.\n"
                      "CALL 'FINAL-STORE' END-CALL.")
        result = answer_grounding_risks([source])
        self.assertEqual({row["target"] for row in result["reasons"]
                          if row["kind"] == "call_implementation_not_supplied"},
                         {"FIRST-STORE", "SECOND-STORE", "RECOVERY-STORE", "FINAL-STORE"})
        self.assertEqual({row["target"] for row in result["reasons"] if row["kind"] == "parameter_copy_boundary"},
                         {"RECOVERY-STORE"})

    def test_supplied_callee_fragment_is_not_reported_missing_when_navigation_resolves_it(self):
        caller = page("ROOT", "CALL 'LEAF' END-CALL.")
        callee = page("LEAF", "MOVE 1 TO OUTPUT-AMOUNT.")
        callee.update(start_line=5, end_line=6, source_text="\n".join(callee["source_text"].splitlines()[4:]))
        relation = {"relation_type": "CALLS", "caller_path": "ROOT.cbl", "caller_line": 5,
                    "target_name": "LEAF", "target_path": "LEAF.cbl"}
        result = answer_grounding_risks([caller, callee], business_map={"relations": [relation]})
        self.assertFalse(result["required"])

    def test_selected_callee_path_cannot_be_replaced_by_a_supplied_namesake(self):
        caller = page("ROOT", "CALL 'LEAF' END-CALL.")
        namesake = {**page("LEAF", "MOVE 1 TO OUTPUT-AMOUNT."), "relative_path": "other/LEAF.cbl"}
        relation = {"relation_type": "CALLS", "caller_path": "ROOT.cbl", "caller_line": 5,
                    "target_name": "LEAF", "target_path": "selected/LEAF.cbl", "resolution": "confirmed"}
        result = answer_grounding_risks([caller, namesake], business_map={"relations": [relation]})
        self.assertEqual(self.kinds(result), {"call_implementation_not_supplied"})
        self.assertEqual(result["reasons"][0]["source_status"], "not_supplied")
        selected = {**namesake, "relative_path": "selected/LEAF.cbl", "evidence_id": "ev_selected"}
        self.assertFalse(answer_grounding_risks([caller, namesake, selected], business_map={"relations": [relation]})["required"])

    def test_name_fallback_preserves_multiple_supplied_identities_as_ambiguous(self):
        caller = page("ROOT", "CALL 'LEAF' END-CALL.")
        first = {**page("LEAF", "MOVE 1 TO OUTPUT-AMOUNT."), "relative_path": "first/LEAF.cbl"}
        second = {**page("LEAF", "MOVE 2 TO OUTPUT-AMOUNT."), "relative_path": "second/LEAF.cbl", "evidence_id": "ev_second"}
        result = answer_grounding_risks([caller, first, second])
        self.assertEqual(result["reasons"][0]["source_status"], "ambiguous")
        relation = {"relation_type": "CALLS", "caller_path": "ROOT.cbl", "caller_line": 5,
                    "target_name": "LEAF", "target_path": None, "resolution": "ambiguous"}
        self.assertEqual(answer_grounding_risks([caller, first], business_map={"relations": [relation]})["reasons"][0]["source_status"], "ambiguous")

    def test_framework_coverage_requires_the_same_supplied_call_and_manual(self):
        source = page("ROOT", "CALL 'REMOTE' END-CALL.")
        reference = {"reference_id": "fw:operation", "document_sha256": "manual-version", "text": "Operation contract."}
        fact = {"fact_id": "binding", "relative_path": source["relative_path"],
                "source_sha256": source["source_sha256"], "start_line": 5, "end_line": 5,
                "source_ranges": [{"start_line": 5, "end_line": 5}], "reference_ids": ["fw:operation"],
                "target_name": "REMOTE", "dependency_covered": True}
        self.assertFalse(answer_grounding_risks([source], framework_references=[reference], framework_facts=[fact])["required"])
        for changed in ({**fact, "source_sha256": "another-version"}, {**fact, "target_name": "OTHER"}):
            self.assertTrue(answer_grounding_risks([source], framework_references=[reference], framework_facts=[changed])["required"])
        self.assertTrue(answer_grounding_risks([source], framework_facts=[fact])["required"])

    def test_external_call_parameters_preserve_position_and_passing_mode(self):
        source = page("ROOT", "CALL 'RECORD-ACCESS' USING CONTROL-AREA\n"
                      "BY CONTENT REQUEST-KEY REQUEST-DATE BY VALUE RETRY-LIMIT\n"
                      "BY REFERENCE RESULT-AREA STATUS-CODE\n"
                      "ON EXCEPTION MOVE 1 TO STATUS-CODE END-CALL.")
        row = answer_grounding_risks([source])["reasons"][0]
        self.assertEqual(row["parameter_parse_status"], "parsed")
        self.assertEqual(row["actual_parameters"], [
            {"position": 1, "name": "CONTROL-AREA", "mode": "REFERENCE"},
            {"position": 2, "name": "REQUEST-KEY", "mode": "CONTENT"},
            {"position": 3, "name": "REQUEST-DATE", "mode": "CONTENT"},
            {"position": 4, "name": "RETRY-LIMIT", "mode": "VALUE"},
            {"position": 5, "name": "RESULT-AREA", "mode": "REFERENCE"},
            {"position": 6, "name": "STATUS-CODE", "mode": "REFERENCE"}])
        self.assertEqual(row["callee_source_status"], "not_supplied")
        self.assertEqual(row["mutable_outputs"], ["CONTROL-AREA", "RESULT-AREA", "STATUS-CODE"])
        self.assertEqual(row["mutable_output_scope"], "argument_storage_including_subordinate_fields")
        self.assertFalse(row["unchanged_value_guaranteed"])
        self.assertEqual((row["start_line"], row["end_line"]), (5, 7))
        self.assertEqual(row["supplied_reference_ids"], ["ev_ROOT"])

    def test_content_and_value_are_not_caller_mutable_outputs(self):
        source = page("ROOT", "CALL 'RECORD-ACCESS' USING BY CONTENT REQUEST-AREA\n"
                      "BY VALUE RETRY-LIMIT END-CALL.")
        result = answer_grounding_risks([source])
        self.assertTrue(result["required"])
        self.assertTrue(all(row["mutable_outputs"] == [] for row in result["reasons"]))
        self.assertTrue(all(row["parameter_parse_status"] == "parsed" for row in result["reasons"]))

    def test_operation_contract_does_not_hide_reference_parameter_boundary(self):
        source = page("ROOT", "CALL 'REMOTE' USING CONTROL-AREA END-CALL.")
        reference = {"reference_id": "fw:operation", "document_sha256": "manual-version",
                     "text": "The operation requests a record."}
        fact = {"fact_id": "binding", "relative_path": source["relative_path"],
                "source_sha256": source["source_sha256"], "start_line": 5, "end_line": 5,
                "source_ranges": [{"start_line": 5, "end_line": 5}], "reference_ids": ["fw:operation"],
                "target_name": "REMOTE", "dependency_covered": True}
        result = answer_grounding_risks([source], framework_references=[reference], framework_facts=[fact])
        self.assertEqual(self.kinds(result), {"call_reference_effect_boundary"})
        self.assertEqual(result["reasons"][0]["mutable_outputs"], ["CONTROL-AREA"])

    def test_unsupported_parameter_forms_remain_unknown_not_empty(self):
        for parameters in ("RESULT-AREA(1)", "RESULT-FIELD OF RESULT-AREA", "RESULT-AREA RETURNING STATUS-CODE",
                           "RESULT-AREA, STATUS-CODE", "ADDRESS OF RESULT-AREA", "9000-FIELD"):
            with self.subTest(parameters=parameters):
                row = answer_grounding_risks([page("ROOT", f"CALL 'REMOTE' USING {parameters} END-CALL.")])["reasons"][0]
                self.assertEqual(row["parameter_parse_status"], "unknown")
                self.assertIsNone(row["actual_parameters"])
                self.assertIsNone(row["mutable_outputs"])
                self.assertFalse(row["unchanged_value_guaranteed"])

    def test_numeric_perform_does_not_truncate_multiline_call_parameters(self):
        for fixed in (False, True):
            with self.subTest(fixed=fixed):
                source = page("ROOT", "PERFORM 9000-CHECK.\n"
                              "CALL 'RECORD-ACCESS' USING CONTROL-AREA\n"
                              "    RECORD-AREA\nPERFORM 9000-CHECK.")
                if fixed:
                    source["source_text"] = "\n".join("       " + line
                        for line in source["source_text"].splitlines()) + "\n"
                    source["source_sha256"] = hashlib.sha256(source["source_text"].encode()).hexdigest()
                row = answer_grounding_risks([source])["reasons"][0]
                self.assertEqual(row["parameter_parse_status"], "parsed")
                self.assertEqual(row["mutable_outputs"], ["CONTROL-AREA", "RECORD-AREA"])
                self.assertEqual((row["start_line"], row["end_line"]), (6, 7))

    def test_legacy_first_line_fallback_is_not_a_complete_parameter_list(self):
        source = page("ROOT", "EXEC SQL SELECT AMOUNT INTO :OUTPUT-AMOUNT FROM RECORD_CONFIG END-EXEC.\n"
                      "CALL 'RECORD-ACCESS' USING CONTROL-AREA\n"
                      "    RECORD-AREA\nPERFORM CHECK-STATUS.")
        rows = answer_grounding_risks([source])["reasons"]
        call = next(row for row in rows if row["kind"] == "call_implementation_not_supplied")
        self.assertEqual(call["parameter_parse_status"], "unknown")
        self.assertIsNone(call["actual_parameters"])
        self.assertIsNone(call["mutable_outputs"])

    def test_ambiguous_target_does_not_hide_reference_mutability(self):
        caller = page("ROOT", "CALL 'LEAF' USING CONTROL-AREA END-CALL.")
        first = page("LEAF", "CONTINUE.")
        second = {**first, "relative_path": "other/LEAF.cbl", "evidence_id": "ev_other"}
        row = answer_grounding_risks([caller, first, second])["reasons"][0]
        self.assertEqual(row["callee_source_status"], "ambiguous")
        self.assertEqual(row["mutable_outputs"], ["CONTROL-AREA"])

    def test_select_into_names_outputs_without_interpreting_sqlcode_or_final_values(self):
        source = page("ROOT", "EXEC SQL\n"
                      "SELECT AMOUNT, STATE INTO :OUTPUT-AMOUNT, :OUTPUT-STATE :STATE-INDICATOR\n"
                      "FROM RECORD_CONFIG WHERE RECORD_KEY = :INPUT-KEY\nEND-EXEC.\n"
                      "IF SQLCODE NOT = ZERO MOVE 1 TO STATUS-CODE END-IF.\n"
                      "MOVE ZERO TO OUTPUT-AMOUNT.")
        rows = answer_grounding_risks([source])["reasons"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["kind"], "sql_select_into_effect_boundary")
        self.assertEqual(row["host_outputs"], ["OUTPUT-AMOUNT", "OUTPUT-STATE", "STATE-INDICATOR"])
        self.assertFalse(row["sqlcode_semantics_interpreted"])
        self.assertFalse(row["absence_of_local_assignment_proves_unchanged"])
        self.assertFalse(row["unchanged_value_guaranteed"])
        self.assertEqual((row["start_line"], row["end_line"]), (5, 8))
        self.assertEqual(row["supplied_reference_ids"], ["ev_ROOT"])
        self.assertNotIn("final_value", row)

    def test_sql_and_calls_survive_numeric_perform_and_ignore_sql_comment_markers(self):
        source = page("ROOT", "PERFORM 1000-CHECK.\n"
                      "EXEC SQL SELECT MESSAGE_TEXT || 'END-EXEC INTO :FAKE'\n"
                      "-- END-EXEC INTO :COMMENT-OUTPUT\n"
                      "INTO :OUTPUT-TEXT FROM RECORD_CONFIG\n"
                      "WHERE RECORD_KEY = :INPUT-KEY END-EXEC.\n"
                      "CALL 'RECORD-ACCESS' USING CONTROL-AREA END-CALL.")
        rows = answer_grounding_risks([source])["reasons"]
        sql = next(row for row in rows if row["kind"] == "sql_select_into_effect_boundary")
        call = next(row for row in rows if row["kind"] == "call_implementation_not_supplied")
        self.assertEqual(sql["host_outputs"], ["OUTPUT-TEXT"])
        self.assertEqual((sql["start_line"], sql["end_line"]), (6, 9))
        self.assertEqual(call["mutable_outputs"], ["CONTROL-AREA"])
        self.assertEqual(call["start_line"], 10)

    def test_unparsed_or_non_select_sql_never_publishes_host_outputs(self):
        for statement in (
            "EXEC SQL SELECT AMOUNT INTO :OUTPUT-AREA.AMOUNT FROM RECORD_CONFIG END-EXEC.",
            "EXEC SQL SELECT AMOUNT INTO :OUTPUT-TABLE(2) FROM RECORD_CONFIG END-EXEC.",
            "EXEC SQL SELECT AMOUNT INTO :OUTPUT-AMOUNT FROM RECORD_CONFIG",
            "EXEC SQL SELECT AMOUNT INTO :OUTPUT-AMOUNT FROM RECORD_CONFIG /* END-EXEC.",
            "EXEC SQL FETCH RECORD-CURSOR INTO :OUTPUT-AMOUNT END-EXEC.",
            "EXEC SQL EXECUTE QUERY-NAME INTO :OUTPUT-AMOUNT END-EXEC.",
            "EXEC SQL SELECT AMOUNT FROM RECORD_CONFIG END-EXEC.",
            "MOVE 'EXEC SQL SELECT AMOUNT INTO :FAKE FROM RECORD_CONFIG END-EXEC' TO MESSAGE-TEXT.",
        ):
            with self.subTest(statement=statement):
                result = answer_grounding_risks([page("ROOT", "PERFORM CHECK-INPUT.\n" + statement)])
                self.assertNotIn("sql_select_into_effect_boundary", self.kinds(result))

    def test_complete_sql_envelope_does_not_need_a_cobol_paragraph_header(self):
        text = "EXEC SQL\n    SELECT AMOUNT\n        INTO :OUTPUT-AMOUNT\n    FROM RECORD_CONFIG\nEND-EXEC."
        source = {"evidence_id": "ev_sql", "relative_path": "lookup.cbl", "include_chain": [],
                  "source_text": text, "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                  "start_line": 35, "end_line": 39}
        row = answer_grounding_risks([source])["reasons"][0]
        self.assertEqual(row["kind"], "sql_select_into_effect_boundary")
        self.assertEqual(row["host_outputs"], ["OUTPUT-AMOUNT"])
        self.assertEqual((row["start_line"], row["end_line"]), (35, 39))
        self.assertEqual(row["supplied_reference_ids"], ["ev_sql"])

    def test_sql_comment_end_marker_does_not_expose_a_fake_call(self):
        for comment in ("-- END-EXEC CALL 'FAKE' USING RESULT-AREA",
                        "/* END-EXEC CALL 'FAKE' USING RESULT-AREA */"):
            with self.subTest(comment=comment):
                source = page("ROOT", "EXEC SQL\n" + comment + "\n"
                              "SELECT 1 FROM RECORD_CONFIG\nEND-EXEC.\n"
                              "CALL 'REAL-ACCESS' USING CONTROL-AREA END-CALL.")
                rows = answer_grounding_risks([source])["reasons"]
                self.assertEqual({row["target"] for row in rows}, {"REAL-ACCESS"})
                self.assertEqual(rows[0]["mutable_outputs"], ["CONTROL-AREA"])
                self.assertEqual(rows[0]["start_line"], 9)


class MutableGroupScopeTests(unittest.TestCase):
    @staticmethod
    def source(path, text, *, chain=None, start=1, digest=None):
        return {"evidence_id": "ev_" + path + "_" + str(start), "relative_path": path,
                "source_text": text, "source_sha256": digest or hashlib.sha256(text.encode()).hexdigest(),
                "start_line": start, "end_line": start + len(text.splitlines()) - 1,
                "include_chain": chain or []}

    def caller(self, declarations, *, body="CALL 'REMOTE' USING CONTROL-AREA END-CALL."):
        return self.source("caller.cbl", "IDENTIFICATION DIVISION.\nPROGRAM-ID. CALLER.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n" + declarations +
            "\nPROCEDURE DIVISION.\nMAIN.\n" + body + "\nGOBACK.\n")

    @staticmethod
    def navigation(path="copy/control.cpy", *, line=5):
        return {"relations": [{"relation_type": "INCLUDES_COPY", "caller_path": "caller.cbl",
            "caller_line": line, "target_name": "CONTROL.CPY", "target_path": path,
            "resolution": "unbound_copy_candidate"}]}

    @staticmethod
    def groups(pages, navigation=None):
        rows = answer_grounding_risks(pages, business_map=navigation)["reasons"]
        return next(row for row in rows if row.get("target") == "REMOTE")["mutable_output_groups"]

    def test_local_group_children_have_physical_provenance_and_exclude_other_storage(self):
        caller = self.caller("01 CONTROL-AREA.\n05 OUTPUT-AMOUNT PIC 9(5).\n"
            "05 DETAIL-AREA.\n10 OUTPUT-STATE PIC X.\n88 ACCEPTED VALUE 'A'.\n"
            "01 UNRELATED-AREA.\n05 OTHER-STATE PIC X.")
        groups = self.groups([caller])
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual([field["name"] for field in group["observed_fields"]],
                         ["OUTPUT-AMOUNT", "DETAIL-AREA", "OUTPUT-STATE"])
        self.assertEqual((group["relative_path"], group["source_sha256"]),
                         (caller["relative_path"], caller["source_sha256"]))
        self.assertEqual((group["start_line"], group["end_line"]), (5, 8))
        self.assertEqual(group["supplied_reference_ids"], [caller["evidence_id"]])
        self.assertFalse(group["complete_layout_verified"])
        self.assertFalse(group["return_values_verified"])

    def test_direct_copy_uses_selected_path_not_a_same_named_other_program_or_copy(self):
        caller = self.caller('COPY "control.cpy".')
        selected = self.source("copy/control.cpy", "01 CONTROL-AREA.\n05 OUTPUT-STATE PIC X.\n")
        namesake = self.source("other/control.cpy", "01 CONTROL-AREA.\n05 WRONG-FIELD PIC X.\n")
        other = self.caller("01 CONTROL-AREA.\n05 FOREIGN-FIELD PIC X.")
        other.update(relative_path="other.cbl", evidence_id="ev_other")
        group = self.groups([caller, selected, namesake, other], self.navigation())[0]
        self.assertEqual([field["name"] for field in group["observed_fields"]], ["OUTPUT-STATE"])
        self.assertEqual(group["binding"], "direct_copy")
        self.assertEqual(group["include_site"]["source_sha256"], caller["source_sha256"])
        self.assertEqual(group["relative_path"], selected["relative_path"])
        self.assertEqual(group["source_sha256"], selected["source_sha256"])
        self.assertEqual(self.groups([caller, selected]), [])

    def test_copy_chain_requires_exact_caller_version_site_and_context(self):
        caller = self.caller('COPY "control.cpy".')
        site = {"relative_path": "caller.cbl", "source_hash": caller["source_sha256"],
                "line": 5, "copy_name": "CONTROL.CPY"}
        copy = self.source("copy/control.cpy", "01 CONTROL-AREA.\n05 OUTPUT-STATE PIC X.\n", chain=[site])
        self.assertEqual(self.groups([caller, copy])[0]["include_chain"], [site])
        for changed in ({**site, "source_hash": "old-version"}, {**site, "line": 6},
                        {**site, "relative_path": "other.cbl"}):
            with self.subTest(site=changed):
                self.assertEqual(self.groups([caller, {**copy, "include_chain": [changed]}]), [])

    def test_copy_replacing_repeated_inclusions_and_multiple_versions_stay_unbound(self):
        copy = self.source("copy/control.cpy", "01 CONTROL-AREA.\n05 OUTPUT-STATE PIC X.\n")
        caller = self.caller('COPY "control.cpy".')
        changed = {**copy, "source_sha256": "another-version", "evidence_id": "ev_other_version"}
        self.assertEqual(self.groups([caller, copy, changed], self.navigation()), [])
        partial = {**changed, "source_text": "05 NEW-FIELD PIC X.", "start_line": 2, "end_line": 2}
        self.assertEqual(self.groups([caller, copy, partial], self.navigation()), [])
        replaced = self.caller('COPY "control.cpy" REPLACING ==OUTPUT-STATE== BY ==OTHER-STATE==.')
        self.assertEqual(self.groups([replaced, copy], self.navigation()), [])
        repeated = self.caller('COPY "control.cpy".\nCOPY "control.cpy".')
        navigation = self.navigation()
        navigation["relations"] += self.navigation(line=6)["relations"]
        self.assertEqual(self.groups([repeated, copy], navigation), [])
        nested = self.source("copy/control.cpy", 'COPY INNER.\n01 CONTROL-AREA.\n05 OUTPUT-STATE PIC X.\n')
        self.assertEqual(self.groups([caller, nested], self.navigation()), [])

    def test_missing_copy_or_program_context_cannot_supply_same_named_children(self):
        caller = self.caller("01 CONTROL-AREA.\n05 OUTPUT-STATE PIC X.\nCOPY UNKNOWN.")
        self.assertEqual(self.groups([caller]), [])
        complete = self.caller("01 CONTROL-AREA.\n05 OUTPUT-STATE PIC X.")
        lines = complete["source_text"].splitlines()
        data = {**complete, "source_text": "\n".join(lines[:6]), "end_line": 6}
        procedure = {**complete, "source_text": "\n".join(lines[7:]), "start_line": 8,
                     "evidence_id": "ev_procedure"}
        self.assertEqual(self.groups([data, procedure]), [])
        compound = self.source("caller.cbl", complete["source_text"] +
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. OTHER.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 CONTROL-AREA.\n05 FOREIGN-FIELD PIC X.\n")
        self.assertEqual(self.groups([compound]), [])

    def test_copy_mode_and_observation_limits_do_not_expand_mutable_storage(self):
        caller = self.caller("01 CONTROL-AREA.\n05 OUTPUT-STATE PIC X.",
                             body="CALL 'REMOTE' USING BY CONTENT CONTROL-AREA END-CALL.")
        self.assertEqual(self.groups([caller]), [])
        caller = self.caller("01 CONTROL-AREA.\n05 FIRST-FIELD PIC X.\n05 SECOND-FIELD PIC X.")
        with mock.patch("answer_grounding._MAX_GROUP_FIELDS", 1):
            self.assertEqual(self.groups([caller]), [])

    def test_copy_instances_do_not_share_an_evidence_id_lookup_and_conflicts_do_not_bind(self):
        caller = self.caller('COPY "control.cpy".')
        site = {"relative_path": "caller.cbl", "source_hash": caller["source_sha256"],
                "line": 5, "copy_name": "CONTROL.CPY"}
        copy = self.source("copy/control.cpy", "01 CONTROL-AREA.\n05 OUTPUT-STATE PIC X.\n", chain=[site])
        other = {**copy, "include_chain": [{**site, "relative_path": "other.cbl"}]}
        group = self.groups([caller, other, copy])[0]
        self.assertEqual(group["include_chain"], [site])
        conflict = {**copy, "source_text": "05 DIFFERENT-STATE PIC X.", "start_line": 2,
                    "end_line": 2, "evidence_id": "ev_conflict"}
        self.assertEqual(self.groups([caller, copy, conflict]), [])

    def test_global_group_budget_limits_repeated_calls_with_explicit_omission(self):
        import json

        caller = self.caller("01 CONTROL-AREA.\n05 FIRST-FIELD PIC X.\n05 SECOND-FIELD PIC X.",
            body="\n".join("CALL 'REMOTE' USING CONTROL-AREA END-CALL." for _ in range(8)))
        with mock.patch("answer_grounding._MAX_MUTABLE_FIELD_OBSERVATIONS", 3):
            rows = answer_grounding_risks([caller])["reasons"]
        self.assertEqual(sum(len(group["observed_fields"]) for row in rows
                            for group in row["mutable_output_groups"]), 3)
        self.assertEqual(sum(row["mutable_output_fields_omitted"] for row in rows), 13)
        self.assertTrue(rows[-1]["mutable_output_observation_truncated"])
        with mock.patch("answer_grounding._MAX_MUTABLE_GROUP_BYTES", 1000):
            rows = answer_grounding_risks([caller])["reasons"]
        size = sum(len(json.dumps(group, ensure_ascii=False, separators=(",", ":")).encode()) + 1
                   for row in rows for group in row["mutable_output_groups"])
        self.assertLessEqual(size, 1000)
        self.assertTrue(any(row["mutable_output_fields_omitted"] for row in rows))

    def test_real_neutral_source_binds_reference_record_and_io_children(self):
        from business_map import build_business_map
        from business_index import build_business_index
        from repository_discovery import ensure_repository_search

        root = Path(__file__).resolve().parents[1] / "fixtures/framework-workbench/source"
        paths = ["programs/service-entry.cbl", "copybooks/shared-context.cpy", "copybooks/request-record.cpy",
                 "copybooks/io-parameters.cpy", "copybooks/screen-fields.cpy", "copybooks/online-flow.cpy"]
        pages = []
        for path in paths:
            raw = (root / path).read_bytes()
            pages.append(self.source(path, raw.decode("utf-8"), digest=hashlib.sha256(raw).hexdigest()))
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "index.sqlite3"
            build_business_index(root, database, verify_content=True)
            ensure_repository_search(database, root)
            navigation = build_business_map(database, root, "SERVICEENTRY")
            rows = answer_grounding_risks(pages, business_map=navigation)["reasons"]
        store_calls = [row for row in rows if row.get("target") == "REQUESTSTORE"]
        self.assertEqual({row["start_line"] for row in store_calls}, {19, 25, 57, 67})
        for row in store_calls:
            fields = {group["argument"]: {field["name"] for field in group["observed_fields"]}
                      for group in row["mutable_output_groups"]}
            self.assertTrue({"REQUEST-STATE", "REQUEST-AMOUNT"} <= fields["REQUEST-RECORD"])
            self.assertTrue({"IO-KEY", "IO-FORMAT", "IO-VIEW"} <= fields["IO-PARAMETERS"])



if __name__ == "__main__":
    unittest.main()
