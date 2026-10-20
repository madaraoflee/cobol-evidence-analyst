"""A selected convention must retain its scoped documentary qualifications."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import framework_knowledge as knowledge
from framework_binding import bind_framework_source
from framework_rules import compile_framework_rules
from framework_semantics import visible_framework_facts


MANUAL = """# Processing R1.0

## Record access

CALL XXXXIO USING XXXX-PARAMS

The operation is selected by XXXX-FUNCTION.

### Operations

| Function | Meaning |
| --- | --- |
| ADVANCE | Request the following record. |

### Results

| Status | Meaning |
| --- | --- |
| DONE | End of input. |

### Limitations

The operation describes a request; no returned row is guaranteed.

## Evidence boundary

The runtime implementation is absent. This reference does not establish
successful persistence, cursor position, locks, or transaction completion.

## Screen access

SCREEN-OPEN prepares a screen.

### Limitations

Screen dimensions require separate configuration.
"""


class FrameworkReferenceConstraintTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "reference.md"
        self.path.write_text(MANUAL, encoding="utf-8")

    def assert_qualified(self, result):
        references = {row["reference_id"]: row for row in result["references"]}
        convention = next(row for row in references.values() if "| ADVANCE |" in row["text"])
        required = convention["required_reference_ids"]
        self.assertEqual(len(required), 2)
        self.assertTrue(set(required) <= references.keys())
        self.assertTrue(any("no returned row" in references[key]["text"] for key in required))
        self.assertTrue(any("runtime implementation" in references[key]["text"] for key in required))
        self.assertFalse(any("Screen dimensions" in references[key]["text"] for key in required))
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        lines = self.path.read_text().splitlines()
        for key in required:
            reference = references[key]
            self.assertEqual(reference["document_sha256"], digest)
            self.assertEqual(reference["document_name"], self.path.name)
            self.assertEqual(reference["text"], "\n".join(
                lines[reference["start_line"] - 1:reference["end_line"]]))
        return convention

    def test_targeted_search_keeps_local_and_document_boundaries(self):
        self.assert_qualified(knowledge.search_framework_context("ADVANCE", reference_path=self.path))

    def test_automatic_selection_keeps_qualifications_without_keyword_overlap(self):
        result = knowledge.build_framework_context(reference_path=self.path,
            question="ADVANCE means what?", source_pages=[])
        self.assert_qualified(result)
        self.assertFalse(result["runtime_verified"])
        self.assertFalse(result["source_matches"])

    def test_constraint_count_and_character_budgets_do_not_send_definition_alone(self):
        for options in ({"max_references": 2}, {"max_chars": 80}):
            with self.subTest(options=options):
                result = knowledge.search_framework_context("ADVANCE", reference_path=self.path, **options)
                self.assertFalse(result["references"])
                self.assertEqual(result["coverage"]["constraint_groups_omitted"], 1)
                self.assertTrue(result["coverage"]["references_truncated"])
        with patch.object(knowledge, "MAX_REFERENCES", 2):
            result = knowledge.build_framework_context(reference_path=self.path,
                question="ADVANCE", source_pages=[])
        self.assertFalse(any("| ADVANCE |" in row["text"] for row in result["references"]))
        self.assertGreater(result["coverage"]["constraint_groups_omitted"], 0)

    def test_multiple_paragraphs_under_one_boundary_are_all_required(self):
        self.path.write_text(MANUAL.replace(
            "## Screen access", "Second independent constraint applies to this document.\n\n## Screen access"))
        result = knowledge.search_framework_context("ADVANCE", reference_path=self.path)
        row = next(row for row in result["references"] if "| ADVANCE |" in row["text"])
        self.assertEqual(len(row["required_reference_ids"]), 3)
        self.assertTrue(any("Second independent" in row["text"] for row in result["references"]))

    def test_same_title_in_another_file_cannot_supply_or_pollute_constraints(self):
        other = self.root / "other.md"
        other.write_text("# Processing R1.0\n\n## Evidence boundary\n\nOther-only exclusion.\n")
        result = knowledge.search_framework_context("ADVANCE", reference_path=self.root)
        definition = next(row for row in result["references"] if "| ADVANCE |" in row["text"])
        references = {row["reference_id"]: row for row in result["references"]}
        self.assertTrue(all(references[key]["document_name"] == "reference.md"
                            for key in definition["required_reference_ids"]))
        self.assertFalse(any("Other-only" in row["text"] for row in result["references"]))

    def test_full_heading_path_retains_scope_beyond_display_heading_limit(self):
        self.path.write_text("# Guide\n\n## One\n\n### Detail\n\n#### More\n\n"
            "##### Operations\n\nADVANCE initiates processing.\n\n"
            "##### Constraints\n\nOnly within the declared context.\n\n"
            "## Two\n\n### Detail\n\n#### More\n\n##### Constraints\n\nUnrelated limitation.\n")
        result = knowledge.search_framework_context("ADVANCE", reference_path=self.path)
        self.assertEqual(len(result["references"]), 2)
        self.assertTrue(any("Only within" in row["text"] for row in result["references"]))
        self.assertFalse(any("Unrelated" in row["text"] for row in result["references"]))

    def test_chinese_scope_headings_keep_qualifications_as_original_text(self):
        self.path.write_text("# 处理说明\n\n## 操作\n\nADVANCE 发起请求。\n\n"
            "## 适用范围\n\n本说明仅描述接口约定，实际返回记录需另行核对。\n")
        result = knowledge.search_framework_context("ADVANCE", reference_path=self.path)
        self.assertEqual(len(result["references"]), 2)
        self.assertEqual(result["references"][1]["selection_reason"], "document_constraint")

    def test_truncated_constraint_does_not_license_an_unqualified_definition(self):
        self.path.write_text(MANUAL.replace(
            "The operation describes a request; no returned row is guaranteed.", "detail " * 1000))
        result = knowledge.search_framework_context("ADVANCE", reference_path=self.path)
        self.assertFalse(result["references"])
        self.assertGreater(result["coverage"]["constraint_groups_omitted"], 0)

    def test_rule_and_interface_citations_require_the_same_boundary_evidence(self):
        compiled = compile_framework_rules(self.path)
        references = {row["reference_id"]: row for row in compiled["references"]}
        rule = next(row for row in compiled["rules"] if row["symbol"] == "ADVANCE")
        self.assertEqual(len(rule["reference_ids"]), 3)
        self.assertTrue(any("runtime implementation" in references[key]["text"] for key in rule["reference_ids"]))
        interface, = compiled["interfaces"]
        self.assertTrue(any("runtime implementation" in references[key]["text"] for key in interface["reference_ids"]))
        self.assertEqual({row["symbol"] for row in compiled["rules"]}, {"ADVANCE", "DONE"})
        source = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRYJOB.\nPROCEDURE DIVISION.\n"
                  "MOVE 'ADVANCE' TO ROWS-FUNCTION.\nCALL 'ROWSIO' USING ROWS-PARAMS.\n")
        page = {"evidence_id": "ev-source", "relative_path": "entry.cbl", "source_sha256": "a" * 64,
                "start_line": 1, "end_line": 5, "source_text": source}
        facts = bind_framework_source(enumerate(source.splitlines(), 1), compiled,
            relative_path="entry.cbl", source_sha256=page["source_sha256"], source_format="free")
        self.assertEqual(len(visible_framework_facts(facts, [page], list(references.values()))), 1)
        without_boundary = [row for row in references.values() if "runtime implementation" not in row["text"]]
        self.assertFalse(visible_framework_facts(facts, [page], without_boundary))

    def test_constraint_scan_obeys_cancellation(self):
        def cancel():
            raise RuntimeError("cancelled")
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            knowledge.build_framework_context(reference_path=self.path, question="ADVANCE",
                                              source_pages=[], check_cancel=cancel)


if __name__ == "__main__":
    unittest.main()
