from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework_binding import bind_framework_source


def knowledge() -> dict:
    return {
        "rules": [
            {"rule_id": "next", "kind": "operation", "symbol": "NX", "meaning": "Read the next record.",
             "caveat": "Ordering depends on the configured access path.", "reference_ids": ["ref-next"]},
            {"rule_id": "status", "kind": "status", "symbol": "OK", "meaning": "Successful completion.",
             "caveat": "Must inspect the returned status.", "reference_ids": ["ref-status"]},
            {"rule_id": "section", "kind": "section", "symbol": "INPUT-STAGE", "meaning": "Prepare an input record.",
             "caveat": "Execution order is not proven.", "reference_ids": ["ref-section"]},
        ],
        "interfaces": [{"interface_id": "record-interface", "target_template": "XXXXIO", "argument_template": "XXXX-PARAMS",
                        "function_template": "XXXX-FUNCTION", "reference_ids": ["ref-interface"],
                        "operation_rule_ids": ["next"], "status_rule_ids": ["status"]}],
        "references": [{"reference_id": value, "document_sha256": "a" * 64, "document_name": "Local reference",
                        "heading": "Record access", "text": value, "start_line": index, "end_line": index}
                       for index, value in enumerate(["ref-interface", "ref-next", "ref-status", "ref-section"], 1)],
    }


def source(body: str, declarations: str = "", name: str = "ENTRYPG") -> str:
    return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
            f"WORKING-STORAGE SECTION.\n{declarations}\nPROCEDURE DIVISION.\nMAIN-ENTRY.\n{body}\n")


def bind(text: str, rules: dict | None = None, source_format: str = "auto") -> list[dict]:
    return bind_framework_source(enumerate(text.splitlines(), 1), knowledge() if rules is None else rules,
                                 relative_path="entry.cbl", source_sha256=hashlib.sha256(text.encode()).hexdigest(),
                                 source_format=source_format)


CALL = "CALL 'ITEMIO' USING ITEM-PARAMS."
MOVE = "MOVE 'NX' TO ITEM-FUNCTION."


class FrameworkBindingTests(unittest.TestCase):
    def test_literal_binding_preserves_call_assignment_hash_and_document_evidence(self) -> None:
        text = source(MOVE + "\n" + CALL, "COPY RECORD-LAYOUT.")
        fact, = bind(text)
        self.assertTrue(fact["dependency_covered"])
        self.assertEqual(fact["operation"]["value"], "NX")
        self.assertEqual(fact["function_field"], "ITEM-FUNCTION")
        self.assertEqual(fact["argument"], "ITEM-PARAMS")
        self.assertEqual(fact["program_name"], "ENTRYPG")
        self.assertEqual(fact["source_sha256"], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(fact["source_ranges"], [{"start_line": 9, "end_line": 9, "role": "callsite"},
                                                 {"start_line": 8, "end_line": 8, "role": "function_assignment"}])
        self.assertEqual(fact["reference_ids"], ["ref-interface", "ref-next"])
        self.assertEqual(fact["return_status_binding"], "not_bound")
        self.assertFalse(fact["runtime_verified"])
        self.assertEqual(bind(text), bind(text))

    def test_multiline_move_and_call_include_physical_ranges(self) -> None:
        text = source("MOVE\n 'NX' TO\n ITEM-FUNCTION\nCALL 'ITEMIO'\n USING\n ITEM-PARAMS\n END-CALL.")
        fact, = bind(text)
        self.assertTrue(fact["dependency_covered"])
        self.assertEqual((fact["start_line"], fact["end_line"]), (11, 14))
        self.assertEqual(fact["source_ranges"][1], {"start_line": 8, "end_line": 10, "role": "function_assignment"})

    def test_fixed_format_comments_and_continuation(self) -> None:
        text = "\n".join(f"{number:06} {line}" for number, line in enumerate(source("MOVE 'NX'\nTO ITEM-FUNCTION\n" + CALL).splitlines(), 1))
        text = text.replace("000009 TO", "000009-TO")
        text = text.replace("000010 CALL", "000010*CALL 'OTHERIO' USING OTHER-PARAMS.\n000011 CALL")
        fact, = bind(text, source_format="fixed")
        self.assertTrue(fact["dependency_covered"])

    def test_comment_and_string_contents_never_create_calls(self) -> None:
        text = source("*> MOVE 'NX' TO ITEM-FUNCTION.\nDISPLAY \"CALL 'ITEMIO' USING ITEM-PARAMS.\"\n"
                      + MOVE + " *> CALL 'OTHERIO' USING OTHER-PARAMS.\n" + CALL)
        fact, = bind(text)
        self.assertTrue(fact["dependency_covered"])
        self.assertEqual(fact["target_name"], "ITEMIO")

    def test_literal_comment_marker_is_not_stripped(self) -> None:
        rules = knowledge()
        rules["rules"][0]["symbol"] = "N*>X"
        fact, = bind(source("MOVE 'N*>X' TO ITEM-FUNCTION.\n" + CALL), rules)
        self.assertTrue(fact["dependency_covered"])

    def test_dynamic_target_never_resolves_from_assignment(self) -> None:
        self.assertEqual(bind(source("MOVE 'ITEMIO' TO ROUTE-NAME.\n" + MOVE + "\nCALL ROUTE-NAME USING ITEM-PARAMS.")), [])

    def test_same_stem_and_single_reference_argument_are_required(self) -> None:
        for call in ("CALL 'ITEMIO' USING ELSE-PARAMS.", "CALL 'ITEMIO' USING ITEM-PARAMS EXTRA.",
                     "CALL 'ITEMIO' USING BY CONTENT ITEM-PARAMS.", "CALL 'ITEMIO'."):
            with self.subTest(call=call):
                fact, = bind(source(MOVE + "\n" + call))
                self.assertFalse(fact["dependency_covered"])

    def test_exact_templates_do_not_match_substrings_or_different_lengths(self) -> None:
        for target in ("PREITEMIO", "ITEMIOPOST", "ITEIO", "ITEMMIO"):
            with self.subTest(target=target):
                self.assertEqual(bind(source(MOVE + f"\nCALL '{target}' USING ITEM-PARAMS.")), [])

    def test_barriers_invalidate_previous_function_assignment(self) -> None:
        for barrier in ("IF FLAG = 1 CONTINUE END-IF", "EVALUATE FLAG WHEN 1 CONTINUE END-EVALUATE",
                        "PERFORM OTHER-PARAGRAPH", "GO TO OTHER-PARAGRAPH", "CALL 'HELPER'", "UNKNOWN ACTION",
                        "INITIALIZE ITEM-PARAMS", "MOVE 'OTHER' TO ITEM-PARAMS", "COPY PROCEDURE-FRAGMENT.",
                        "NEXT-PARAGRAPH.", "NEXT-STAGE SECTION.", "2000-NEXT-PARAGRAPH.", "2000-NEXT-STAGE SECTION."):
            with self.subTest(barrier=barrier):
                fact, = bind(source(MOVE + "\n" + barrier + "\n" + CALL))
                self.assertFalse(fact["dependency_covered"])

    def test_branch_merge_and_sentence_closure_do_not_leak_literals(self) -> None:
        for body in ("IF FLAG = 1\nMOVE 'NX' TO ITEM-FUNCTION\nEND-IF\n" + CALL,
                     "IF FLAG = 1\n" + MOVE + "\n" + CALL,
                     "IF FLAG = 1\nIF OTHER = 1 CONTINUE END-IF\n" + MOVE + "\n" + CALL,
                     "EVALUATE FLAG\nWHEN 1 MOVE 'NX' TO ITEM-FUNCTION\nWHEN OTHER\n" + CALL):
            with self.subTest(body=body):
                fact, = bind(source(body))
                self.assertFalse(fact["dependency_covered"])

    def test_assignment_inside_same_branch_describes_conditional_intent(self) -> None:
        fact, = bind(source("IF FLAG = 1\nMOVE 'NX' TO ITEM-FUNCTION\n" + CALL + "\nEND-IF."))
        self.assertTrue(fact["dependency_covered"])
        self.assertFalse(fact["runtime_verified"])

    def test_new_assignment_after_branch_or_copy_restores_local_evidence(self) -> None:
        for prefix in ("COPY PROCEDURE-FRAGMENT.", "IF FLAG = 1 CONTINUE END-IF", "PERFORM HELPER."):
            with self.subTest(prefix=prefix):
                fact, = bind(source(prefix + "\n" + MOVE + "\n" + CALL))
                self.assertTrue(fact["dependency_covered"])

    def test_replacement_scope_cannot_prove_original_tokens(self) -> None:
        fact, = bind(source("REPLACE ==NX== BY ==OTHER==.\n" + MOVE + "\n" + CALL))
        self.assertFalse(fact["dependency_covered"])
        self.assertEqual(fact["reason"], "replacement_scope_not_resolved")
        fact, = bind(source("REPLACE ==NX== BY ==OTHER==.\nREPLACE OFF.\n" + MOVE + "\n" + CALL))
        self.assertTrue(fact["dependency_covered"])

    def test_embedded_sql_and_unclosed_literals_do_not_create_framework_calls(self) -> None:
        self.assertEqual(bind(source("EXEC SQL\n" + MOVE + "\n" + CALL + "\nEND-EXEC.")), [])
        self.assertEqual(bind(source("DISPLAY 'unclosed\n" + MOVE + "\n" + CALL)), [])

    def test_program_boundary_never_reuses_assignment(self) -> None:
        text = source(MOVE) + source(CALL, name="SECONDPG")
        fact, = bind(text)
        self.assertFalse(fact["dependency_covered"])
        self.assertEqual(fact["program_name"], "SECONDPG")

    def test_duplicate_program_or_field_is_ambiguous(self) -> None:
        text = source(MOVE + "\n" + CALL) + source("GOBACK.")
        self.assertFalse(bind(text)[0]["dependency_covered"])
        text = source(MOVE + "\n" + CALL, "01 ITEM-FUNCTION PIC XX.\n01 ITEM-FUNCTION PIC XX.")
        fact, = bind(text)
        self.assertEqual(fact["reason"], "source_field_ambiguous")

    def test_rule_and_interface_ambiguity_never_select_first_match(self) -> None:
        for kind in ("rule", "interface"):
            rules = knowledge()
            if kind == "rule":
                rules["rules"].append(dict(rules["rules"][0], rule_id="conflict", meaning="Delete a record."))
                rules["interfaces"][0]["operation_rule_ids"].append("conflict")
            else:
                rules["interfaces"].append(copy.deepcopy(rules["interfaces"][0]))
            with self.subTest(kind=kind):
                fact, = bind(source(MOVE + "\n" + CALL), rules)
                self.assertFalse(fact["dependency_covered"])

    def test_unknown_and_case_different_operation_remain_uncovered(self) -> None:
        for value in ("OTHER", "nx"):
            with self.subTest(value=value):
                fact, = bind(source(f"MOVE '{value}' TO ITEM-FUNCTION.\n" + CALL))
                self.assertFalse(fact["dependency_covered"])
                self.assertEqual(fact["reason"], "function_operation_not_documented")

    def test_named_operand_can_offer_documented_candidate_without_coverage(self) -> None:
        rules = knowledge()
        rules["rules"][0]["symbol"] = "NEXT-RECORD"
        fact, = bind(source("MOVE NEXT-RECORD TO ITEM-FUNCTION.\n" + CALL), rules)
        self.assertFalse(fact["dependency_covered"])
        self.assertIsNone(fact["operation"])
        self.assertEqual(fact["reason"], "function_operand_not_resolved")
        self.assertEqual(fact["documented_operation_candidate"]["value"], "NEXT-RECORD")

    def test_missing_manual_evidence_never_covers_dependency(self) -> None:
        rules = knowledge()
        rules["references"] = [ref for ref in rules["references"] if ref["reference_id"] != "ref-next"]
        fact, = bind(source(MOVE + "\n" + CALL), rules)
        self.assertFalse(fact["dependency_covered"])
        self.assertEqual(fact["reason"], "rule_evidence_incomplete")

    def test_section_matching_is_exact_and_does_not_claim_dependency_coverage(self) -> None:
        facts = bind(source("INPUT-STAGE SECTION.\nINPUT-STAGE-EXTRA SECTION.\nGOBACK."))
        fact, = facts
        self.assertEqual(fact["kind"], "framework_section")
        self.assertEqual(fact["target_name"], "INPUT-STAGE")
        self.assertFalse(fact["dependency_covered"])

    def test_numbered_section_names_bind_without_treating_numbers_as_labels(self) -> None:
        rules = knowledge()
        rules["rules"][2]["symbol"] = "1000-INPUT-STAGE"
        fact, = bind(source("1000-INPUT-STAGE SECTION.\nGOBACK."), rules)
        self.assertEqual(fact["kind"], "framework_section")
        self.assertEqual(fact["target_name"], "1000-INPUT-STAGE")
        rules["rules"][2]["symbol"] = "1000"
        self.assertEqual(bind(source("1000 SECTION.\nGOBACK."), rules), [])

    def test_debug_lines_and_oversized_physical_lines_are_boundaries(self) -> None:
        text = "\n".join(f"{number:06} {line}" for number, line in enumerate(source(MOVE + "\nCONTINUE\n" + CALL).splitlines(), 1))
        text = text.replace("000009 CONTINUE", "000009DCONTINUE")
        fact, = bind(text, source_format="fixed")
        self.assertFalse(fact["dependency_covered"])
        lines = list(enumerate(source(MOVE + "\nCONTINUE\n" + CALL).splitlines(), 1))
        lines[8] = (9, None)
        fact, = bind_framework_source(iter(lines), knowledge(), relative_path="entry.cbl", source_sha256="b" * 64)
        self.assertFalse(fact["dependency_covered"])

    def test_output_budget_still_consumes_source_iterator(self) -> None:
        consumed = []
        text = source((MOVE + "\n" + CALL + "\n") * 2050)

        def lines():
            for number, line in enumerate(text.splitlines(), 1):
                yield number, line
            consumed.append(True)

        facts = bind_framework_source(lines(), knowledge(), relative_path="entry.cbl", source_sha256="c" * 64)
        self.assertEqual(len(facts), 2048)
        self.assertEqual(consumed, [True])

    def test_checked_partial_program_starts_with_no_inherited_assignment(self) -> None:
        kwargs = {"relative_path": "entry.cbl", "source_sha256": "d" * 64, "initial_program": "ENTRYPG"}
        fact, = bind_framework_source([(50, CALL)], knowledge(), **kwargs)
        self.assertFalse(fact["dependency_covered"])
        fact, = bind_framework_source([(49, MOVE), (50, CALL)], knowledge(), **kwargs)
        self.assertTrue(fact["dependency_covered"])
        self.assertEqual(fact["source_ranges"][1]["start_line"], 49)
        fact, = bind_framework_source([(40, MOVE), (50, CALL)], knowledge(), **kwargs)
        self.assertFalse(fact["dependency_covered"])

    def test_focus_filters_output_without_hiding_prior_replacement_state(self) -> None:
        text = source("REPLACE ==NX== BY ==OTHER==.\n" + (MOVE + "\n" + CALL + "\n") * 2050)
        all_lines = list(enumerate(text.splitlines(), 1))
        final_call_line = max(number for number, line in all_lines if line == CALL)
        facts = bind_framework_source(iter(all_lines), knowledge(), relative_path="entry.cbl", source_sha256="e" * 64,
                                      focus_ranges=[(final_call_line, final_call_line)])
        fact, = facts
        self.assertEqual(fact["start_line"], final_call_line)
        self.assertEqual(fact["reason"], "replacement_scope_not_resolved")
        self.assertFalse(fact["dependency_covered"])


if __name__ == "__main__":
    unittest.main()
