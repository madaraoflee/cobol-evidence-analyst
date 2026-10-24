"""Source-bound stage requests and containment; no model or runtime claims."""

from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import business_orientation
from business_orientation import build_business_orientation
from syntax_evidence import build_syntax_guide
from test_business_orientation import page


SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. FLOWJOB.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 APPROVAL PIC X.
PROCEDURE DIVISION.
MAIN.
PERFORM INPUT-STAGE.
IF APPROVAL = 'Y'
 PERFORM OUTPUT-STAGE
END-IF.
GOBACK.
INPUT-STAGE SECTION.
READ-RECORD.
CALL 'ROWSIO'.
OUTPUT-STAGE SECTION.
WRITE-RECORD.
CALL 'OUTPUTIO'.
"""


def material(source=SOURCE):
    supplied = page("flow.cbl", source)
    reference = {"reference_id": "fw:stages", "text":
        "INPUT-STAGE prepares the next input. OUTPUT-STAGE prepares delivery.", "text_truncated": False}
    facts = []
    for name, meaning in (("INPUT-STAGE", "Prepare the next input."),
                          ("OUTPUT-STAGE", "Prepare delivery.")):
        number = source.splitlines().index(name + " SECTION.") + 1
        facts.append({"fact_id": "stage:" + name, "kind": "framework_section",
            "relative_path": "flow.cbl", "source_sha256": supplied["source_sha256"],
            "program_name": "FLOWJOB", "target_name": name,
            "operation": {"value": name, "meaning": meaning, "caveat": "Execution depends on source control flow."},
            "source_ranges": [{"start_line": number, "end_line": number, "role": "section"}],
            "reference_ids": [reference["reference_id"]], "dependency_covered": False, "runtime_verified": False})
        facts[-1]["reason"] = "documented_section_observed"
    return supplied, facts, [reference]


class FrameworkStageOrientationTests(unittest.TestCase):
    def project(self, source=SOURCE, *, pages=None, references=None, facts=None):
        supplied, default_facts, default_references = material(source)
        return build_business_orientation("请说明业务流程", {}, pages if pages is not None else [supplied],
            framework_facts=default_facts if facts is None else facts,
            framework_references=default_references if references is None else references)

    @staticmethod
    def performs(result):
        return [row for row in result["relationships"] if row["kind"] == "procedure_perform"]

    def test_named_requests_keep_source_guards_and_exact_target_declarations(self):
        result = self.project()
        requests = self.performs(result)
        self.assertEqual([row["target_name"] for row in requests], ["INPUT-STAGE", "OUTPUT-STAGE"])
        for row in requests:
            self.assertEqual(row["perform_scope"], "named_target_request")
            self.assertEqual(row["framework_control_context"], "complete_supplied_syntax")
            definition, = row["source_target_definitions"]
            self.assertEqual((definition["kind"], definition["name"]), ("Section", row["target_name"]))
            self.assertFalse(row["execution_verified"])
        guard, = requests[1]["guards"]
        self.assertEqual((guard["condition"], guard["outcome"]), ("APPROVAL = 'Y'", True))
        conventions = [row for row in result["visible_rules"] if row.get("kind") == "framework_section"]
        self.assertEqual([row["section_locations"][0]["start_line"] for row in conventions], [13, 16])
        self.assertEqual(conventions[0]["operation"]["meaning"], "Prepare the next input.")

    def test_range_is_one_request_without_expanding_or_implying_execution(self):
        source = SOURCE.replace("PERFORM INPUT-STAGE.", "PERFORM INPUT-STAGE THROUGH OUTPUT-STAGE.")
        requests = self.performs(self.project(source))
        ranges = [row for row in requests if row["perform_scope"] == "range_request"]
        self.assertEqual(len(ranges), 1)
        self.assertEqual((ranges[0]["target_name"], ranges[0]["target_range_end"]),
                         ("INPUT-STAGE", "OUTPUT-STAGE"))
        self.assertEqual(len(requests), 2)

    def test_stage_call_ownership_covers_header_through_call(self):
        calls = [row for row in self.project()["relationships"] if row["kind"] == "program_call"]
        self.assertEqual(calls[0]["enclosing_section"], {"name": "INPUT-STAGE", "start_line": 13, "end_line": 13})
        self.assertTrue(any((span["start_line"], span["end_line"]) == (13, 15)
                            for span in calls[0]["source_locations"]))
        self.assertEqual(calls[0]["framework_control_context"], "complete_supplied_syntax")

    def test_unsupported_control_body_keeps_request_without_a_bound_stage_path(self):
        source = SOURCE.replace("MAIN.\n", "MAIN.\nINSPECT APPROVAL REPLACING ALL 'N' BY 'Y'.\n")
        requests = self.performs(self.project(source))
        self.assertTrue(requests)
        self.assertTrue(all(row["framework_control_context"] == "not_bound" for row in requests))

    def test_nested_evaluate_retains_selector_and_first_match_conditions(self):
        source = SOURCE.replace("IF APPROVAL = 'Y'\n PERFORM OUTPUT-STAGE\nEND-IF.",
            "IF APPROVAL NOT = 'N'\n EVALUATE APPROVAL\n WHEN 'Y'\n  CONTINUE\n"
            " WHEN OTHER\n  PERFORM OUTPUT-STAGE\n END-EVALUATE\nEND-IF.")
        row = self.performs(self.project(source))[1]
        self.assertEqual(row["framework_control_context"], "complete_supplied_syntax")
        guard = next(item for item in row["guards"] if item["kind"] == "EVALUATE")
        self.assertEqual(guard["selector"], "APPROVAL")
        self.assertTrue(guard["is_other"])
        self.assertEqual(guard["prior_branches_must_not_match"], [["'Y'"]])

    def test_gap_between_section_and_call_withdraws_ownership(self):
        lines = SOURCE.splitlines()
        pages = [page("flow.cbl", "\n".join(lines[:13]), first=1, full_text=SOURCE),
                 page("flow.cbl", "\n".join(lines[14:]), first=15, full_text=SOURCE)]
        result = self.project(pages=pages)
        calls = [row for row in result["relationships"] if row["kind"] == "program_call"]
        self.assertFalse(any(row.get("enclosing_section") for row in calls))

    def test_duplicate_section_or_paragraph_stays_visible_as_ambiguous_definitions(self):
        for duplicate in ("INPUT-STAGE SECTION.\n", "INPUT-STAGE.\n"):
            with self.subTest(duplicate=duplicate):
                source = SOURCE + duplicate + "CONTINUE.\n"
                result = self.project(source)
                request = self.performs(result)[0]
                self.assertEqual(len(request["source_target_definitions"]), 2)
                if "SECTION" in duplicate:
                    call = next(row for row in result["relationships"] if row.get("target_name") == "ROWSIO")
                    self.assertNotIn("enclosing_section", call)

    def test_missing_or_truncated_manual_does_not_expand_ordinary_requests(self):
        for references in ([], [{"reference_id": "fw:stages", "text": "partial", "text_truncated": True}]):
            with self.subTest(references=references):
                result = self.project(references=references)
                self.assertFalse(self.performs(result))
                self.assertFalse(any(row.get("enclosing_section") for row in result["relationships"]))

    def test_copy_instances_cannot_acquire_new_stage_relationships(self):
        supplied, _, _ = material()
        for pages in ([{**supplied, "include_chain": ["host-a"]}],
                      [supplied, {**supplied, "include_chain": ["host-b"]}]):
            with self.subTest(pages=pages):
                result = self.project(pages=pages)
                self.assertFalse(self.performs(result))
                self.assertFalse(any(row.get("enclosing_section") for row in result["relationships"]))

    def test_old_source_version_cannot_reuse_stage_bindings(self):
        _, facts, _ = material()
        stale = deepcopy(facts)
        for fact in stale:
            fact["source_sha256"] = "0" * 64
        result = self.project(facts=stale)
        self.assertFalse(self.performs(result))
        self.assertFalse(any(row.get("enclosing_section") for row in result["relationships"]))

    def test_ambiguous_program_scope_never_creates_new_stage_relationships(self):
        _, facts, _ = material()
        for fact in facts:
            fact["reason"] = "program_scope_ambiguous"
        result = self.project(facts=facts)
        self.assertFalse(self.performs(result))
        self.assertFalse(any(row.get("enclosing_section") for row in result["relationships"]))
        source = SOURCE + "END PROGRAM FLOWJOB.\n" + SOURCE
        duplicated = self.project(source)
        self.assertFalse(self.performs(duplicated))
        self.assertFalse(any(row.get("enclosing_section") for row in duplicated["relationships"]))

    def test_unexpanded_stage_scope_retains_an_explicit_boundary(self):
        _, facts, _ = material()
        for fact in facts:
            fact["reason"] = "framework_stage_scope_requires_expansion"
        result = self.project(facts=facts)
        self.assertFalse(self.performs(result))
        self.assertFalse(any(row.get("enclosing_section") for row in result["relationships"]))
        self.assertTrue(any(row["reason"] == "framework_stage_control_scope_not_established"
                            for row in result["unknowns"]))
        stages = [row for row in result["visible_rules"] if row.get("kind") == "framework_section"]
        self.assertEqual(len(stages), 2)
        self.assertTrue(all(row["reason"] == "framework_stage_scope_requires_expansion" for row in stages))


class SuppliedSyntaxFormatTests(unittest.TestCase):
    def test_explicit_free_format_preserves_eight_space_indented_perform(self):
        text = "MAIN.\n    IF APPROVAL = 'Y'\n        PERFORM INPUT-STAGE\n    END-IF.\n"
        supplied = page("flow.cbl", text)
        facts = build_syntax_guide([supplied])["facts"]
        self.assertEqual([row["kind"] for row in facts], ["PERFORM"])
        self.assertEqual(facts[0]["guards"][0]["condition"], "APPROVAL = 'Y'")
        unknown = {**supplied, "format_hint": "auto"}
        self.assertFalse(build_syntax_guide([unknown])["facts"])

    def test_explicit_fixed_format_retains_physical_statement_location(self):
        text = "000100 MAIN.\n000200     PERFORM INPUT-STAGE.\n"
        supplied = {**page("flow.cbl", text), "format_hint": "fixed"}
        fact, = build_syntax_guide([supplied])["facts"]
        self.assertEqual((fact["kind"], fact["start_line"], fact["end_line"]), ("PERFORM", 2, 2))

    def test_explicit_directive_must_agree_with_supplied_format(self):
        text = ">>SOURCE FORMAT FREE\nMAIN.\n        PERFORM INPUT-STAGE.\n"
        supplied = page("flow.cbl", text)
        self.assertEqual(len(build_syntax_guide([supplied])["facts"]), 1)
        conflict = page("flow.cbl", text.replace("FORMAT FREE", "FORMAT FIXED"))
        result = build_syntax_guide([conflict])
        self.assertFalse(result["facts"])
        self.assertEqual(result["boundaries"][0]["reason"], "source_format_configuration_conflict")
        fixed = "000100 >>SOURCE FORMAT FIXED\n000200 MAIN.\n000300     PERFORM INPUT-STAGE.\n"
        self.assertEqual(len(build_syntax_guide([{**page("flow.cbl", fixed), "format_hint": "fixed"}])["facts"]), 1)

    def test_mixed_page_configuration_does_not_upgrade_unknown_format(self):
        text = "MAIN.\nIF APPROVAL = 'Y'\n PERFORM INPUT-STAGE\nEND-IF.\n"
        lines = text.splitlines()
        first = page("flow.cbl", "\n".join(lines[:2]), eid="ev-first", full_text=text)
        last = page("flow.cbl", "\n".join(lines[2:]), eid="ev-last", first=3, full_text=text)
        for hint in ("fixed", "auto", "mixed"):
            with self.subTest(hint=hint):
                result = build_syntax_guide([first, {**last, "format_hint": hint}])
                self.assertFalse(result["facts"])
                self.assertEqual(result["boundaries"][0]["reason"], "source_format_configuration_conflict")
        self.assertFalse(build_syntax_guide([{**page("flow.cbl", text), "format_hint": "mixed"}])["facts"])

    def test_conflicting_format_on_same_evidence_id_is_not_last_writer_wins(self):
        supplied = page("flow.cbl", "MAIN.\nPERFORM INPUT-STAGE.\n")
        result = build_syntax_guide([supplied, {**supplied, "format_hint": "fixed"}])
        self.assertFalse(result["facts"])
        self.assertEqual(result["boundaries"][0]["reason"], "conflicting_reference_content")


class OrientationPreparationCostTests(unittest.TestCase):
    def test_dense_call_inventory_bounds_projection_work_and_keeps_fitting_facts(self):
        source = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. FLOWJOB.\nPROCEDURE DIVISION.\nMAIN.\n"
                  + "".join("CALL 'P%03d'.\n" % index for index in range(128)) + "GOBACK.\n")
        supplied = page("flow.cbl", source)
        for budget in (24000, 2400):
            with self.subTest(budget=budget), patch.object(business_orientation, "_metadata_bytes",
                    wraps=business_orientation._metadata_bytes) as measure:
                result = build_business_orientation("请解释业务流程", {}, [supplied],
                    max_items=128, max_metadata_bytes=budget)
                # Prevent one complete wire projection per discarded source
                # fact. This measures work, without a machine-speed threshold.
                self.assertLess(measure.call_count, 32)
            self.assertLessEqual(business_orientation._metadata_bytes(result, budget), budget)
            if budget == 24000:
                self.assertGreater(len(result["relationships"]), 4)
                self.assertTrue(all(row["supplied_reference_ids"] for row in result["relationships"]))


if __name__ == "__main__":
    unittest.main()
