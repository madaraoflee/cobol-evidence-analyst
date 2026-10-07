"""Source-bound call observations, without a model."""

import hashlib
from pathlib import Path
import sys
import unittest

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



if __name__ == "__main__":
    unittest.main()
