"""Grounded AST evidence must never outlive its supplied source or scope."""
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from business_synthesis import build_analysis_brief, _supplied_source_spans
from syntax_evidence import build_syntax_guide, build_inline_syntax_guide


def page(text, reference="ev:source", start=1, digest="snapshot", chain=None):
    return {"evidence_id": reference, "relative_path": "rule.cbl", "start_line": start,
            "end_line": start + len(text.splitlines()) - 1, "source_text": text,
            "source_sha256": digest, **({"include_chain": chain} if chain else {})}


class SyntaxEvidenceTests(unittest.TestCase):
    def test_same_line_alternatives_keep_opposite_guards_and_columns(self):
        source = "MAIN.\nIF FLAG = 'Y' MOVE 1 TO RESULT-VALUE ELSE MOVE 2 TO RESULT-VALUE END-IF."
        facts = build_syntax_guide([page(source)])["facts"]
        self.assertEqual([f["statement"] for f in facts], ["MOVE 1 TO RESULT-VALUE", "MOVE 2 TO RESULT-VALUE"])
        self.assertEqual([f["guards"][0]["outcome"] for f in facts], [True, False])
        self.assertLess(facts[0]["start_column"], facts[1]["start_column"])
        self.assertTrue(all(f["start_line"] == 2 for f in facts))

    def test_evaluate_records_first_match_and_all_scope_evidence(self):
        source = "MAIN.\nEVALUATE TRUE\nWHEN LEFT-CODE NOT = ZERO\nMOVE LEFT-CODE TO STATUS-CODE\nWHEN RIGHT-CODE NOT = ZERO\nMOVE RIGHT-CODE TO STATUS-CODE\nWHEN OTHER\nMOVE ZERO TO STATUS-CODE\nEND-EVALUATE."
        lines = source.splitlines()
        pages = [page("\n".join(lines[:4]), "ev:first"), page("\n".join(lines[4:]), "ev:last", 5)]
        facts = build_syntax_guide(pages)["facts"]
        self.assertEqual(len(facts), 3)
        self.assertEqual(facts[1]["guards"][0]["prior_branches_must_not_match"], [["LEFT-CODE NOT = ZERO"]])
        self.assertTrue(facts[2]["guards"][0]["is_other"])
        self.assertTrue(all(set(f["supplied_reference_ids"]) == {"ev:first", "ev:last"} for f in facts))
        self.assertEqual(build_syntax_guide(pages[:1])["facts"], [])
        self.assertEqual(build_syntax_guide(pages[1:])["facts"], [])

    def test_fragment_without_visible_unit_start_is_not_unconditional_fact(self):
        self.assertEqual(build_syntax_guide([page("MOVE 1 TO RESULT-VALUE.", start=20)])["facts"], [])

    def test_clipped_tail_is_not_completed_unit(self):
        self.assertEqual(build_syntax_guide([page("MAIN.\nMOVE 1 TO RESULT-VALUE")])["facts"], [])

    def test_missing_physical_line_or_different_snapshot_cannot_close_scope(self):
        first = page("MAIN.\nIF FLAG = 'Y'\nMOVE 1 TO RESULT-VALUE", "ev:first")
        for end in (page("END-IF.", "ev:last", 5), page("END-IF.", "ev:last", 4, "other")):
            self.assertEqual(build_syntax_guide([first, end])["facts"], [])

    def test_include_instances_cannot_share_scope(self):
        first = page("MAIN.\nIF FLAG = 'Y'\nMOVE 1 TO RESULT-VALUE", "ev:first", chain=["caller-a"])
        last = page("END-IF.", "ev:last", 4, chain=["caller-b"])
        self.assertEqual(len(_supplied_source_spans([first, last], {"ev:first", "ev:last"})), 2)
        self.assertEqual(build_syntax_guide([first, last])["facts"], [])

    def test_overlapping_pages_choose_small_number_of_complete_references(self):
        text = "MAIN.\nMOVE 1 TO RESULT-VALUE."
        pages = [page(text, "ev:whole")] + [page("MOVE 1 TO RESULT-VALUE.", f"ev:{i}", 2) for i in range(30)]
        fact = build_syntax_guide(pages)["facts"][0]
        self.assertEqual(fact["supplied_reference_ids"], ["ev:whole"])

    def test_cache_rebinds_references_and_does_not_keep_trimmed_facts(self):
        source = "MAIN.\nMOVE 1 TO RESULT-VALUE."
        self.assertEqual(build_syntax_guide([page(source)])["facts"][0]["supplied_reference_ids"], ["ev:source"])
        self.assertEqual(build_syntax_guide([page(source, "ev:new")])["facts"][0]["supplied_reference_ids"], ["ev:new"])
        self.assertEqual(build_syntax_guide([])["facts"], [])

    def test_unknown_effects_preserve_boundary_without_promoting_handler_writes(self):
        source = "MAIN.\nCOMPUTE RESULT-VALUE = BASIS * 2 ON SIZE ERROR MOVE ZERO TO RESULT-VALUE END-COMPUTE\nMOVE 7 TO RESULT-VALUE."
        facts = build_syntax_guide([page(source)])["facts"]
        self.assertEqual(len(facts), 2)
        self.assertTrue(facts[0]["effects_unknown"])
        self.assertEqual(facts[0]["writes"], [])
        self.assertEqual(facts[1]["statement"], "MOVE 7 TO RESULT-VALUE")

    def test_literal_and_comment_cannot_create_paragraph(self):
        text = "MAIN.\nDISPLAY 'unfinished\nFAKE.\nMOVE 1 TO RESULT-VALUE."
        self.assertEqual(build_syntax_guide([page(text)])["facts"], [])
        text = "*> FAKE.\nMOVE 1 TO RESULT-VALUE."
        self.assertEqual(build_syntax_guide([page(text)])["facts"], [])

    def test_data_division_never_becomes_procedure(self):
        text = "DATA DIVISION.\nWORKING-STORAGE SECTION.\nMAIN.\nMOVE 1 TO RESULT-VALUE."
        self.assertEqual(build_syntax_guide([page(text)])["facts"], [])

    def test_fixed_format_preserves_original_line_and_column(self):
        text = "000100 MAIN.\n000200     MOVE 1 TO RESULT-VALUE.\n000300*    MOVE 2 TO RESULT-VALUE.\n000400     GOBACK.\n000500*padding\n000600*padding\n000700*padding"
        facts = build_syntax_guide([page(text, start=30)])["facts"]
        self.assertEqual(facts[0]["start_line"], 31)
        self.assertEqual(facts[0]["start_column"], 12)
        self.assertEqual(facts[0]["statement"], "MOVE 1 TO RESULT-VALUE")

    def test_brief_contains_bound_facts_and_removes_them_after_trim(self):
        p = page("MAIN.\nIF FLAG = 'Y' MOVE 1 TO RESULT-VALUE END-IF.")
        brief = build_analysis_brief("结果如何决定？", {}, [p], [], 8192)
        self.assertIn("syntax_guide", brief)
        self.assertFalse(brief["syntax_guide"]["semantic_execution_verified"])
        self.assertNotIn("syntax_guide", build_analysis_brief("结果如何决定？", {}, [], [], 8192))

    def test_default_guide_only_promotes_units_with_inline_structure(self):
        ordinary = page("MAIN.\nIF FLAG = 'Y'\nMOVE 1 TO RESULT-VALUE\nEND-IF.")
        self.assertTrue(build_syntax_guide([ordinary])["facts"])
        self.assertEqual(build_inline_syntax_guide([ordinary])["facts"], [])
        self.assertNotIn("syntax_guide", build_analysis_brief("规则如何执行？", {}, [ordinary], [], 8192))
        inline = page("MAIN.\nIF FLAG = 'Y' MOVE 1 TO RESULT-VALUE END-IF.")
        self.assertTrue(build_inline_syntax_guide([inline])["facts"])
        multiple = page("MAIN.\nMOVE 1 TO RESULT-VALUE MOVE 2 TO STATUS-CODE.")
        self.assertEqual(len(build_inline_syntax_guide([multiple])["facts"]), 2)

    def test_budget_exhaustion_is_explicit(self):
        text = "MAIN.\n" + "MOVE 1 TO RESULT-VALUE.\n" * 100
        result = build_syntax_guide([page(text)])
        self.assertLessEqual(len(result["facts"]), 24)
        self.assertGreater(result["omitted_facts"], 0)


    def test_short_numbered_fixed_excerpt_ignores_identification_area(self):
        source = "000100 MAIN.\n" + "000200     MOVE 1 TO RESULT-VALUE".ljust(72) + "FAKE\n000300     GOBACK."
        facts = build_syntax_guide([page(source)])["facts"]
        self.assertEqual(facts[0]["writes"], ["RESULT-VALUE"])
        self.assertNotIn("FAKE", facts[0]["statement"])

    def test_unnumbered_overflow_requires_unambiguous_source_format(self):
        source = "       MAIN.\n" + "           MOVE 1 TO RESULT-VALUE".ljust(72) + "FAKE\n           GOBACK."
        result = build_syntax_guide([page(source)])
        self.assertEqual(result["facts"], [])
        self.assertEqual(result["boundaries"][0]["reason"], "source_format_ambiguous_column_73")
        explicit = build_syntax_guide([page(">>SOURCE FORMAT FREE\n" + source)])
        self.assertEqual(explicit["facts"][0]["writes"], ["RESULT-VALUE", "FAKE"])

    def test_fixed_debug_or_continuation_unit_header_cannot_bypass_parser(self):
        for indicator in ("D", "d", "-"):
            with self.subTest(indicator=indicator):
                source = f"000100{indicator}MAIN.\n000200     MOVE 1 TO RESULT-VALUE."
                result = build_syntax_guide([page(source)])
                self.assertEqual(result["facts"], [])
                self.assertEqual(result["boundaries"][0]["reason"], "fixed_continuation_or_debug_line_not_supported")

    def test_same_reference_rebinds_within_each_include_instance(self):
        source = "MAIN.\nMOVE 1 TO RESULT-VALUE."
        facts = build_syntax_guide([page(source, chain=["caller-a"]), page(source, chain=["caller-b"])])["facts"]
        self.assertEqual([f["include_chain"] for f in facts], [["caller-a"], ["caller-b"]])

    def test_conflicting_same_reference_content_is_not_bound(self):
        result = build_syntax_guide([page("MAIN.\nMOVE 1 TO RESULT-VALUE."),
                                     page("MAIN.\nMOVE 2 TO RESULT-VALUE.")])
        self.assertEqual(result["facts"], [])
        self.assertEqual(result["boundaries"][0]["reason"], "conflicting_reference_content")

    def test_nested_variable_loop_never_promotes_inner_assignment(self):
        source = ("MAIN.\nPERFORM UNTIL DONE = 1 PERFORM LIMIT TIMES MOVE 1 TO RESULT-VALUE "
                  "END-PERFORM MOVE 2 TO RESULT-VALUE END-PERFORM MOVE 3 TO RESULT-VALUE.")
        facts = build_syntax_guide([page(source)])["facts"]
        self.assertEqual([f["statement"] for f in facts if f["writes"]], ["MOVE 3 TO RESULT-VALUE"])


    def test_statement_keywords_and_terminators_cannot_create_units(self):
        for word in ("DISPLAY", "STOP", "CALL", "END-REWRITE", "END-START", "ZERO", "WHEN"):
            with self.subTest(word=word):
                result = build_syntax_guide([page(f"{word}.\nMOVE 1 TO RESULT-VALUE.")])
                self.assertEqual(result["facts"], [])

    def test_embedded_blocks_cannot_create_cobol_units(self):
        source = "EXEC SQL\nFAKE.\nMOVE 1 TO RESULT-VALUE.\nEND-EXEC."
        self.assertEqual(build_syntax_guide([page(source)])["facts"], [])
        source = "MAIN.\nMOVE 0 TO RESULT-VALUE.\n" + source
        result = build_syntax_guide([page(source)])
        self.assertEqual([f["statement"] for f in result["facts"]], ["MOVE 0 TO RESULT-VALUE"])
        self.assertEqual(result["boundaries"][0]["reason"], "statement_not_supported:EXEC")

    def test_fragment_beginning_inside_embedded_block_cannot_create_unit(self):
        source = "FAKE.\nMOVE 1 TO RESULT-VALUE.\nEND-EXEC.\nREAL-UNIT.\nMOVE 2 TO RESULT-VALUE."
        result = build_syntax_guide([page(source, start=20)])
        self.assertEqual([f["statement"] for f in result["facts"]], ["MOVE 2 TO RESULT-VALUE"])

    def test_embedded_comments_and_literals_cannot_end_embedded_navigation(self):
        for decoy in ("-- END-EXEC", "/* END-EXEC */", "'END-EXEC'"):
            with self.subTest(decoy=decoy):
                source = f"EXEC SQL\n{decoy}\nFAKE.\nMOVE 1 TO RESULT-VALUE.\nEND-EXEC."
                self.assertEqual(build_syntax_guide([page(source)])["facts"], [])


if __name__ == "__main__":
    unittest.main()
