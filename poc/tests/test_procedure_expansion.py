from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from procedure_expansion import expand_program


def program(body: str, working: str = "") -> str:
    return ("IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRYPG.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n" + working + "\nPROCEDURE DIVISION.\n" + body + "\n")


class ProcedureExpansionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write(self, name: str, text: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def expand(self, **options: object) -> dict:
        return expand_program(self.root, "ENTRYPG", **options)

    def reasons(self, result: dict) -> set[str]:
        return {item["reason"] for item in result["boundaries"]}

    def test_data_and_procedure_copy_preserve_nested_provenance(self) -> None:
        self.write("entry.cbl", program("COPY BATCHCTL.\n2000-HANDLE.\nCONTINUE.", "COPY AREA."))
        self.write("area.cpy", "01 ACCESS-STATUS PIC XX.\n")
        self.write("batchctl.cpy", "PERFORM 2000-HANDLE.\nCOPY ENDCTL.\n")
        self.write("endctl.cpy", "GOBACK.\n")
        result = self.expand()
        self.assertTrue(result["source_expansion_complete"])
        self.assertEqual(result["status"], "expanded")
        self.assertFalse(result["compiler_equivalent"])
        nested = next(line for line in result["lines"] if line["text"] == "GOBACK.")
        self.assertEqual(nested["origin"]["relative_path"], "endctl.cpy")
        self.assertEqual(nested["origin"]["line"], 1)
        self.assertEqual([item["copy_name"] for item in nested["include_chain"]], ["BATCHCTL", "ENDCTL"])
        self.assertEqual(nested["include_chain"][1]["line"], 2)
        self.assertEqual(nested["origin"]["source_hash"], hashlib.sha256(b"GOBACK.\n").hexdigest())
        self.assertEqual(len(result["source_files"]), 4)
        json.dumps(result)

    def test_repeated_inclusion_has_distinct_inclusion_sites(self) -> None:
        self.write("entry.cbl", program("COPY STEPCTL.\nCOPY STEPCTL."))
        self.write("stepctl.cpy", "PERFORM 2000-HANDLE.\n")
        result = self.expand()
        copies = [line for line in result["lines"] if line["include_chain"]]
        self.assertEqual(len(copies), 2)
        self.assertEqual(copies[0]["origin"], copies[1]["origin"])
        self.assertNotEqual(copies[0]["include_chain"], copies[1]["include_chain"])
        self.assertEqual([item["status"] for item in result["includes"]], ["expanded", "expanded"])

    def test_empty_copy_is_explicit_and_manifest_covers_discovery_scope(self) -> None:
        self.write("entry.cbl", program("COPY EMPTYCTL."))
        self.write("emptyctl.cpy", "")
        self.write("other.cbl", "PROGRAM-ID. OTHERPG.\nGOBACK.")
        result = self.expand()
        self.assertTrue(result["source_expansion_complete"])
        self.assertEqual(len(result["source_files"]), 3)
        self.assertEqual(result["includes"][0]["resolved_path"], "emptyctl.cpy")
        self.assertEqual(result["includes"][0]["status"], "expanded")

    def test_nested_boundary_makes_parent_include_bounded(self) -> None:
        self.write("entry.cbl", program("COPY FIRSTCTL."))
        self.write("firstctl.cpy", "COPY ABSENT.")
        result = self.expand()
        self.assertEqual([item["status"] for item in result["includes"]], ["bounded", "bounded"])
        self.assertEqual(result["includes"][0]["resolved_path"], "firstctl.cpy")

    def test_missing_member_keeps_original_copy_visible(self) -> None:
        self.write("entry.cbl", program("COPY ABSENT."))
        result = self.expand()
        self.assertEqual(self.reasons(result), {"copy_target_not_found"})
        self.assertFalse(result["source_expansion_complete"])
        self.assertIn("COPY ABSENT.", [item["text"] for item in result["lines"]])

    def test_duplicate_member_never_guesses_search_order(self) -> None:
        self.write("entry.cbl", program("COPY STEPCTL."))
        self.write("a/stepctl.cpy", "GOBACK.")
        self.write("b/stepctl.cpy", "CONTINUE.")
        result = self.expand()
        self.assertIn("copy_target_ambiguous", self.reasons(result))
        self.assertEqual(result["boundaries"][0]["candidates"], ["a/stepctl.cpy", "b/stepctl.cpy"])

    def test_recursive_include_reports_full_arrival_chain(self) -> None:
        self.write("entry.cbl", program("COPY FIRSTCTL."))
        self.write("firstctl.cpy", "COPY SECONDCTL.")
        self.write("secondctl.cpy", "COPY FIRSTCTL.")
        result = self.expand()
        self.assertIn("copy_cycle", self.reasons(result))
        issue = result["boundaries"][0]
        self.assertEqual(issue["origin"]["relative_path"], "secondctl.cpy")
        self.assertEqual([site["copy_name"] for site in issue["include_chain"]], ["FIRSTCTL", "SECONDCTL"])

    def test_replacing_and_inline_and_split_copy_are_unsupported(self) -> None:
        for body in ["COPY STEPCTL REPLACING ==A== BY ==B==.",
                     "CONTINUE. COPY STEPCTL.", "COPY\nSTEPCTL.", "COPY STEPCTL IN LIBRARY."]:
            with self.subTest(body=body):
                self.write("entry.cbl", program(body))
                self.write("stepctl.cpy", "GOBACK.")
                result = self.expand()
                self.assertIn("copy_form_not_supported", self.reasons(result))
                self.assertFalse(any(line["include_chain"] for line in result["lines"]))

    def test_replace_state_and_directives_disable_file_expansion(self) -> None:
        for modifier, reason in [("REPLACE ==A== BY ==B==.", "replace_statement_not_supported"),
                                 (">>IF FEATURE", "source_directive_not_supported"),
                                 ("$SET SOURCEFORMAT FREE", "source_directive_not_supported")]:
            with self.subTest(modifier=modifier):
                self.write("entry.cbl", program(modifier + "\nCOPY STEPCTL."))
                self.write("stepctl.cpy", "GOBACK.")
                result = self.expand()
                self.assertIn(reason, self.reasons(result))
                self.assertIn("COPY STEPCTL.", [line["text"] for line in result["lines"]])

    def test_quoted_words_and_comments_do_not_create_copy_or_program_ids(self) -> None:
        self.write("entry.cbl", program(
            "DISPLAY 'COPY LOST. *> still literal'. *> COPY ALSO-LOST.\n"
            'DISPLAY "REPLACE COPY PROGRAM-ID. ENTRYPG.".\n'
            "DISPLAY 'it''s COPY'.\n*> COPY LOST.\n      *COPY LOST.\nGOBACK."))
        result = self.expand()
        self.assertTrue(result["source_expansion_complete"])
        self.assertEqual(result["boundaries"], [])
        self.assertTrue(any("*> still literal" in line["code"] for line in result["lines"]))

    def test_fixed_source_columns_and_quoted_member(self) -> None:
        self.write("entry.cbl", "000100 IDENTIFICATION DIVISION.\n000200 PROGRAM-ID. ENTRYPG.\n"
                   "000300 PROCEDURE DIVISION.\n000400 COPY 'STEPCTL'.\n")
        self.write("stepctl.cpy", "000100 PERFORM 2000-HANDLE.\n")
        result = self.expand()
        self.assertTrue(result["source_expansion_complete"])
        self.assertEqual(result["lines"][-1]["text"], "000100 PERFORM 2000-HANDLE.")
        self.assertEqual(result["lines"][-1]["code"], "PERFORM 2000-HANDLE.")

    def test_indented_free_format_data_level_is_not_a_sequence_number(self) -> None:
        self.write("entry.cbl", program("GOBACK.", "COPY AREA."))
        self.write("area.cpy", "01 ACCESS-AREA.\n    05 ACCESS-STATUS PIC X(4).\n")
        result = self.expand()
        self.assertTrue(result["source_expansion_complete"])
        field = next(line for line in result["lines"] if "ACCESS-STATUS" in line["text"])
        self.assertEqual(field["code"].strip(), "05 ACCESS-STATUS PIC X(4).")

    def test_debug_and_continuation_and_unclosed_literal_are_bounded(self) -> None:
        for source, reason in [("000900D COPY STEPCTL.", "debug_source_not_supported"),
                               ("000900- COPY STEPCTL.", "source_continuation_not_supported"),
                               ("DISPLAY 'COPY", "source_continuation_not_supported")]:
            with self.subTest(source=source):
                self.write("entry.cbl", program(source))
                self.assertIn(reason, self.reasons(self.expand()))

    def test_depth_and_line_budgets_are_explicit(self) -> None:
        self.write("entry.cbl", program("COPY FIRSTCTL."))
        self.write("firstctl.cpy", "COPY SECONDCTL.")
        self.write("secondctl.cpy", "GOBACK.")
        self.assertIn("copy_depth_limit", self.reasons(self.expand(max_depth=1)))
        limited = self.expand(max_lines=2)
        self.assertEqual(len(limited["lines"]), 2)
        self.assertIn("copy_line_limit", self.reasons(limited))
        self.assertIn("copy_depth_limit", self.reasons(self.expand(max_depth=0)))

    def test_exact_line_budget_can_complete(self) -> None:
        self.write("entry.cbl", program("GOBACK."))
        expected = len((self.root / "entry.cbl").read_text().splitlines())
        self.assertTrue(self.expand(max_lines=expected)["source_expansion_complete"])

    def test_empty_members_cannot_evade_inclusion_budget(self) -> None:
        self.write("entry.cbl", program("\n".join(["COPY EMPTYCTL."] * 20)))
        self.write("emptyctl.cpy", "")
        result = self.expand(max_lines=10)
        self.assertIn("copy_inclusion_limit", self.reasons(result))
        self.assertEqual(len(result["includes"]), 10)
        self.assertLessEqual(len(result["lines"]), 10)

    def test_unknown_fixed_indicator_is_not_silently_discarded(self) -> None:
        self.write("entry.cbl", program("000900X COPY STEPCTL."))
        self.assertIn("fixed_indicator_not_supported", self.reasons(self.expand()))

    def test_non_text_source_reports_discovery_boundary(self) -> None:
        self.write("entry.cbl", program("GOBACK."))
        (self.root / "unknown.cpy").write_bytes(b"\x00" * 20)
        self.assertIn("source_not_text", self.reasons(self.expand()))

    def test_source_files_unchanged_and_symlinks_not_followed(self) -> None:
        self.write("entry.cbl", program("COPY STEPCTL."))
        self.write("stepctl.cpy", "GOBACK.")
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        (self.root / "duplicate.cbl").symlink_to(self.root / "entry.cbl")
        result = self.expand()
        self.assertTrue(result["source_expansion_complete"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.root.iterdir() if not path.is_symlink()})

    def test_extensionless_and_configured_extensions_are_supported(self) -> None:
        self.write("entry.member", program('COPY "STEPCTL".'))
        self.write("stepctl", "GOBACK.")
        result = self.expand(extensions="member,cpy")
        self.assertTrue(result["source_expansion_complete"])
        self.assertEqual(result["program_source"], "entry.member")

    def test_missing_and_duplicate_and_multiple_programs_are_bounded(self) -> None:
        self.assertIn("entry_program_not_found", self.reasons(self.expand()))
        self.write("entry.cbl", program("GOBACK."))
        self.write("other.cbl", program("GOBACK."))
        self.assertIn("entry_program_ambiguous", self.reasons(self.expand()))
        self.write("other.cbl", "")
        self.write("entry.cbl", program("GOBACK.") + "PROGRAM-ID. ANOTHERPG.\nGOBACK.")
        self.assertIn("multiple_programs_in_source_not_supported", self.reasons(self.expand()))

    def test_program_file_is_not_silently_included_as_copybook(self) -> None:
        self.write("entry.cbl", program("COPY OTHER."))
        self.write("other.cbl", "PROGRAM-ID. OTHERPG.\nGOBACK.")
        self.assertIn("copy_target_contains_program", self.reasons(self.expand()))

    def test_bad_invocation_rejected(self) -> None:
        for options in [{"max_depth": -1}, {"max_depth": True}, {"max_depth": 65}, {"max_lines": 0},
                        {"max_lines": 1.5}, {"extensions": []}, {"extensions": "../cpy"},
                        {"extensions": ["cpy", ""]}, {"extensions": [3]}, {"extensions": 3}]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.expand(**options)
        with self.assertRaises(ValueError):
            expand_program(self.root / "missing", "ENTRYPG")
        with self.assertRaises(ValueError):
            expand_program(self.root, "ENTRYPG OR OTHERPG")


if __name__ == "__main__":
    unittest.main()
