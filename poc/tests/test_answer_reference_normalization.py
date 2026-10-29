"""Citation spelling repairs never guess the source supporting prose."""

from pathlib import Path
import json
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from answer_reference_normalization import normalize_answer_references


HASH = "1234567890abcdef12345678"
PAGE = "ev_page_" + HASH
ALIAS = "ev_" + HASH
MANUAL = "fw:0123456789abcdef:7-9"
ALLOWED = {PAGE: {"evidence_id": PAGE, "relative_path": "lookup.cbl",
                  "source_sha256": "source-version", "start_line": 1, "end_line": 12},
           MANUAL: {"reference_id": MANUAL, "document_sha256": "manual-version",
                    "start_line": 7, "end_line": 9}}


class ReferenceNormalizationTests(unittest.TestCase):
    def test_exact_parenthesized_source_and_manual_ids_become_links(self):
        text = f"Source ({PAGE}); manual（{MANUAL}）。"
        result = normalize_answer_references(text, ALLOWED)
        self.assertEqual(result["answer"], f"Source [{PAGE}]; manual[{MANUAL}]。")
        self.assertEqual(len(result["repairs"]), 2)
        self.assertFalse(result["semantic_verification"])
        for repair in result["repairs"]:
            self.assertEqual(text[repair["start"]:repair["end"]], repair["original"])
            self.assertIn(repair["canonical_id"], ALLOWED)
            self.assertFalse(repair["semantic_verification"])

    def test_page_prefix_alias_requires_the_entire_exact_hash(self):
        result = normalize_answer_references(f"COPY（{ALIAS}）; more [{ALIAS}].", ALLOWED)
        self.assertEqual(result["answer"], f"COPY[{PAGE}]; more [{PAGE}].")
        self.assertTrue(all(row["reason"] == "EXACT_PAGE_IDENTIFIER_DIGEST_ALIAS"
                            for row in result["repairs"]))
        self.assertEqual(result["unresolved"], [])

    def test_already_valid_ids_and_bare_text_do_not_change(self):
        text = f"[{PAGE}] [{MANUAL}] {ALIAS} {PAGE}"
        result = normalize_answer_references(text, ALLOWED)
        self.assertEqual(result["answer"], text)
        self.assertEqual(result["repairs"], [])

    def test_unknown_stale_partial_and_case_changed_ids_remain_unresolved(self):
        for identifier in ("ev_abcdefabcdefabcdefabcdef", "ev_" + HASH[:-1],
                           "ev_" + HASH + "0", ALIAS.upper(), MANUAL[:-1] + "8"):
            text = f"Unsupported ({identifier})."
            result = normalize_answer_references(text, ALLOWED)
            self.assertEqual(result["answer"], text)
            self.assertEqual(result["repairs"], [])
        stale = normalize_answer_references(f"[{ALIAS}]", {})
        self.assertEqual(stale["unresolved"][0]["reason"], "REFERENCE_NOT_SUPPLIED")

    def test_conflicting_supplied_identity_cannot_authorize_alias_or_format(self):
        first = ALLOWED[PAGE]
        other = {**first, "source_sha256": "different-version"}
        for identifier in (PAGE, ALIAS):
            text = f"Result ({identifier})."
            result = normalize_answer_references(text, [first, other])
            self.assertEqual(result["answer"], text)
            self.assertEqual(result["unresolved"][0]["reason"], "AMBIGUOUS_SUPPLIED_ID")
        self.assertTrue(normalize_answer_references(f"({PAGE})", [first, dict(first)])["repairs"])

    def test_exact_supplied_short_id_wins_over_possible_alias(self):
        result = normalize_answer_references(f"({ALIAS})", {**ALLOWED, ALIAS: {"evidence_id": ALIAS}})
        self.assertEqual(result["answer"], f"[{ALIAS}]")
        self.assertEqual(result["repairs"][0]["reason"], "EXACT_SUPPLIED_REFERENCE_FORMAT")

    def test_conflicting_copy_instance_or_replacement_cannot_authorize_reference(self):
        first = {**ALLOWED[PAGE], "copy_instance": "copy-1",
                 "replacement_trace": [{"from": "INPUT", "to": "FIRST-INPUT"}]}
        for change in ({"copy_instance": "copy-2"},
                       {"replacement_trace": [{"from": "INPUT", "to": "SECOND-INPUT"}]}):
            with self.subTest(change=change):
                result = normalize_answer_references(f"({ALIAS})", [first, {**first, **change}])
                self.assertEqual(result["answer"], f"({ALIAS})")
                self.assertEqual(result["unresolved"][0]["reason"], "AMBIGUOUS_SUPPLIED_ID")

    def test_mismatched_mapping_identity_is_unresolved(self):
        result = normalize_answer_references(f"({PAGE})", {PAGE: {"evidence_id": "ev_other"}})
        self.assertEqual(result["answer"], f"({PAGE})")
        self.assertEqual(result["unresolved"][0]["reason"], "AMBIGUOUS_SUPPLIED_ID")

    def test_code_spans_fences_and_indented_code_are_untouched(self):
        examples = (
            f"`({PAGE})` and ``({ALIAS}) with ` mark``",
            f"```markdown\n({PAGE})\n({ALIAS})\n```\n",
            f"~~~~\n({MANUAL})\n~~~\n({PAGE})\n~~~~\n",
            f"```text\n({ALIAS})\n",
            f"    ({PAGE})\n\t({ALIAS})\n",
            f"``multi\n({PAGE})\nline``",
            f"> ~~~text\n> ({PAGE})\n> ~~~\n",
            f"- ~~~~text\n  ({PAGE})\n  ~~~~\n",
        )
        for text in examples:
            with self.subTest(text=text):
                result = normalize_answer_references(text, ALLOWED)
                self.assertEqual(result["answer"], text)
                self.assertEqual(result["repairs"], [])
        mixed = f"`({PAGE})` then ({PAGE})"
        self.assertEqual(normalize_answer_references(mixed, ALLOWED)["answer"],
                         f"`({PAGE})` then [{PAGE}]")

    def test_links_escapes_and_malformed_or_multiple_id_groups_are_untouched(self):
        for text in (f"[label]({PAGE})", f"![image]({ALIAS})",
                     f"[label](https://example.invalid/({PAGE}))", f"<https://example.invalid/({PAGE})>",
                     f"\\({PAGE})", f"({PAGE}）", f"（{PAGE})",
                     f"({PAGE}, {MANUAL})", f"({PAGE}; {MANUAL})"):
            with self.subTest(text=text):
                self.assertEqual(normalize_answer_references(text, ALLOWED)["answer"], text)

    def test_repaired_reference_set_is_a_subset_of_actual_supplied_ids(self):
        text = (f"Parameter block（{ALIAS}）。Operation（{MANUAL}）。"
                f"Caller（{PAGE}）。Unknown（ev_not_supplied）。")
        result = normalize_answer_references(text, ALLOWED)
        linked = set(re.findall(r"\[((?:ev[_:-]|fw:)[^\]]+)\]", result["answer"]))
        self.assertEqual(linked, set(ALLOWED))
        self.assertEqual(len(result["repairs"]), 3)
        self.assertEqual(len(result["unresolved"]), 1)
        self.assertIn("（ev_not_supplied）", result["answer"])

    def test_normalization_is_idempotent_and_accepts_explicit_identifier_sets(self):
        once = normalize_answer_references(f"({ALIAS}) ({MANUAL})", set(ALLOWED))
        twice = normalize_answer_references(once["answer"], set(ALLOWED))
        self.assertEqual(twice["answer"], once["answer"])
        self.assertEqual(twice["repairs"], [])

    def test_audit_is_bounded_without_limiting_prose_repairs_or_unresolved_counts(self):
        text = (f"({PAGE}) (ev_unknown) " * 1000).strip()
        result = normalize_answer_references(text, ALLOWED)
        self.assertEqual(result["answer"].count(f"[{PAGE}]"), 1000)
        self.assertEqual(result["answer"].count("(ev_unknown)"), 1000)
        for field in ("repairs", "unresolved"):
            self.assertLessEqual(len(result[field]), 16)
            self.assertEqual(result[field + "_total_count"], 1000)
            self.assertEqual(result[field + "_omitted_count"], 1000 - len(result[field]))
            self.assertLessEqual(len(json.dumps(result[field], ensure_ascii=False).encode()), 3500)
        audit = {key: value for key, value in result.items() if key != "answer"}
        self.assertLess(len(json.dumps(audit, ensure_ascii=False).encode()), 8000)

    def test_large_marker_whitespace_is_only_copied_as_a_bounded_audit_excerpt(self):
        marker = "(" + " " * 10000 + PAGE + ")"
        result = normalize_answer_references(marker, ALLOWED)
        self.assertEqual(result["answer"], f"[{PAGE}]")
        self.assertTrue(result["repairs"][0]["original_truncated"])
        self.assertLessEqual(len(result["repairs"][0]["before"]), 256)
        self.assertEqual(result["repairs"][0]["end"], len(marker))
        self.assertEqual(len(result["repairs"][0]["original_sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
