from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from framework_rules import compile_framework_rules


MANUAL = """# Processing guide R2.1

## Data access

CALL XXXXIO USING XXXX-PARAMS

## Database I/O

I/O operations use XXXX-FUNCTION.

### Reads

<!-- SOURCE_PAGE: 03 -->
| Function | Meaning | Caution |
| --- | --- | --- |
| SEEK | Return the first selected record. | The first record has already been read. |
| ADVANCE | Return the following record. | End status ends traversal. |

### Results

| Status | Meaning |
| --- | --- |
| OK / \\*\\*\\*\\* | The operation succeeded. |
| DONE | No further record is available. |

## Screens

| Function | Meaning |
| --- | --- |
| SHOW | Display the record. |

## Batch stages

1000-OPEN prepares input.

| Section | Meaning |
| --- | --- |
| 1000 | Open the input. |
| 2000 | Unspecified stage. |
"""


class FrameworkRulesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / "reference.md"

    def compile(self, text=MANUAL):
        self.path.write_text(text, encoding="utf-8")
        return compile_framework_rules(self.path)

    def test_compiles_citable_rules_and_scoped_interface_without_model(self):
        result = self.compile()
        self.assertEqual(result["status"], "COMPILED")
        self.assertEqual(result["document"]["target_version"], "R2.1")
        self.assertEqual(result["document"]["sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        rules = {row["symbol"]: row for row in result["rules"]}
        self.assertIn("1000-OPEN", rules)
        self.assertNotIn("1000", rules)
        self.assertNotIn("2000", rules)
        self.assertIn("****", rules)
        self.assertEqual(rules["SEEK"]["caveat"], "The first record has already been read.")
        interface, = result["interfaces"]
        self.assertEqual(interface["target_template"], "XXXXIO")
        self.assertEqual(interface["argument_template"], "XXXX-PARAMS")
        self.assertEqual(interface["function_template"], "XXXX-FUNCTION")
        self.assertEqual(set(interface["operation_rule_ids"]), {rules[key]["rule_id"] for key in ("SEEK", "ADVANCE")})
        self.assertNotIn(rules["SHOW"]["rule_id"], interface["operation_rule_ids"])
        references = {row["reference_id"]: row for row in result["references"]}
        for row in result["rules"] + result["interfaces"]:
            self.assertTrue(set(row["reference_ids"]) <= references.keys())
            self.assertFalse(row["runtime_verified"])
        self.assertEqual(references[rules["SEEK"]["reference_ids"][0]]["page"], 3)

    def test_changed_document_changes_rules_and_interface_identity(self):
        first = self.compile()
        second = self.compile(MANUAL.replace("R2.1", "R2.2"))
        self.assertNotEqual(first["document"]["sha256"], second["document"]["sha256"])
        self.assertTrue({r["rule_id"] for r in first["rules"]}.isdisjoint(r["rule_id"] for r in second["rules"]))
        self.assertNotEqual(first["interfaces"][0]["interface_id"], second["interfaces"][0]["interface_id"])

    def test_numbered_stage_retains_full_name_and_role_sources(self):
        result = self.compile()
        stage = next(row for row in result["rules"] if row["kind"] == "section")
        references = {row["reference_id"]: row for row in result["references"]}
        supporting = [references[identifier] for identifier in stage["reference_ids"]]
        self.assertEqual(len(supporting), 2)
        self.assertTrue(any("1000-OPEN" in row["text"] for row in supporting))
        self.assertTrue(any("| 1000 | Open the input. |" in row["text"] for row in supporting))
        self.assertTrue(all(row["heading"] == stage["scope"] for row in supporting))
        from framework_binding import bind_framework_source
        from framework_semantics import visible_framework_facts
        text = "IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRYJOB.\nPROCEDURE DIVISION.\n1000-OPEN SECTION.\nGOBACK.\n"
        page = {"evidence_id": "ev-stage", "relative_path": "entry.cbl", "source_sha256": "a" * 64,
                "start_line": 1, "end_line": 5, "source_text": text}
        facts = bind_framework_source(enumerate(text.splitlines(), 1), result,
            relative_path="entry.cbl", source_sha256=page["source_sha256"], source_format="free")
        self.assertEqual(len(visible_framework_facts(facts, [page], supporting)), 1)
        role_only = [row for row in supporting if "1000-OPEN" not in row["text"]]
        self.assertFalse(visible_framework_facts(facts, [page], role_only))

    def test_stage_name_source_cannot_cross_document_or_heading(self):
        text = MANUAL.replace("1000-OPEN prepares input.", "")
        result = self.compile(text + "\n## Other stages\n\n1000-OPEN prepares input.\n")
        self.assertFalse(any(row["kind"] == "section" for row in result["rules"]))
        self.path.write_text(text, encoding="utf-8")
        (self.root / "other.md").write_text("# Processing guide R2.1\n\n## Batch stages\n\n1000-OPEN prepares input.\n", encoding="utf-8")
        result = compile_framework_rules(self.root)
        self.assertFalse(any(row["kind"] == "section" for row in result["rules"]))

    def test_function_scope_ambiguity_does_not_compile_interface(self):
        result = self.compile(MANUAL + "\n## Other I/O\n\nI/O operations use XXXX-FUNCTION.\n")
        self.assertFalse(result["interfaces"])
        self.assertIn("interface_function_scope_ambiguous", {b["reason"] for b in result["boundaries"]})

    def test_multi_argument_example_does_not_compile_interface(self):
        result = self.compile(MANUAL.replace("USING XXXX-PARAMS", "USING XXXX-PARAMS OTHER-AREA"))
        self.assertFalse(result["interfaces"])
        self.assertIn("interface_call_form_unsupported", {b["reason"] for b in result["boundaries"]})

    def test_template_mismatch_does_not_compile_interface(self):
        result = self.compile(MANUAL.replace("USING XXXX-PARAMS", "USING XXXXX-PARAMS"))
        self.assertFalse(result["interfaces"])

    def test_conflicting_rows_do_not_bind_an_operation(self):
        text = MANUAL.replace("| ADVANCE |", "| SEEK |")
        result = self.compile(text)
        self.assertNotIn("SEEK", {row["symbol"] for row in result["rules"]})
        self.assertFalse(result["interfaces"])
        self.assertIn("conflicting_documented_rule", {b["reason"] for b in result["boundaries"]})

    def test_different_document_cannot_supply_interface_function(self):
        self.path.write_text("# Calls\n\nCALL XXXXIO USING XXXX-PARAMS\n", encoding="utf-8")
        (self.root / "operations.md").write_text(MANUAL.replace("CALL XXXXIO USING XXXX-PARAMS", ""), encoding="utf-8")
        result = compile_framework_rules(self.root)
        self.assertFalse(result["interfaces"])
        self.assertTrue(result["rules"])

    def test_numbered_stage_must_resolve_unambiguously(self):
        result = self.compile(MANUAL.replace("1000-OPEN prepares input.", "1000-OPEN or 1000-BEGIN prepares input."))
        self.assertFalse(any(row["kind"] == "section" for row in result["rules"]))

    def test_truncated_manual_keeps_citable_rows_without_covering_interfaces(self):
        result = self.compile(MANUAL + "\n" + "unfinished-detail " * 200 + "\n")
        self.assertTrue(result["rules"])
        self.assertFalse(result["interfaces"])
        self.assertIn("framework_document_truncated", {b["reason"] for b in result["boundaries"]})

    def test_truncated_table_caveat_never_becomes_a_known_rule(self):
        result = self.compile(MANUAL.replace("The first record has already been read.",
            "Condition " * 400 + "Only when explicitly enabled."))
        self.assertNotIn("SEEK", {row["symbol"] for row in result["rules"]})
        self.assertFalse(result["interfaces"])
        self.assertIn("framework_rule_text_truncated", {b["reason"] for b in result["boundaries"]})

    def test_conflicting_child_scopes_do_not_cover_the_same_operation(self):
        text = MANUAL.replace("### Results", "### Other reads\n\n| Operation | Meaning |\n| --- | --- |\n| SEEK | Only position the cursor. |\n\n### Results")
        result = self.compile(text)
        self.assertFalse(result["interfaces"])
        self.assertIn("interface_rule_scope_ambiguous", {b["reason"] for b in result["boundaries"]})

    def test_missing_disabled_and_no_rules_are_distinct(self):
        self.assertEqual(compile_framework_rules("")["status"], "NOT_CONFIGURED")
        self.assertEqual(compile_framework_rules(self.root / "absent.md")["status"], "UNAVAILABLE")
        self.assertEqual(self.compile("# Notes\n\nOnly explanatory prose.\n")["status"], "NO_RULES")


if __name__ == "__main__":
    unittest.main()
