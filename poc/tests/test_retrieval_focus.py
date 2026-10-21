"""Search-specific evidence must survive cropping into the model's budget."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
import repository_discovery as retrieval
from retrieval_quality_eval import production_loader


QUESTION = "approval status adjustment threshold verification processing transaction eligibility calculation"


class RetrievalFocusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"

    def build(self, marker="LANTERN-CAP-VALUE", *, near_prefix=False):
        text = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. TARGET-ENTRY.\nPROCEDURE DIVISION.\n"
                f"*> {marker}.\nCOMPUTE FINAL-VALUE = BASE-VALUE + 37.\n" +
                "CONTINUE.\n" * 250 +
                (f"*> PREFIX-{marker}-SUFFIX " if near_prefix else "*> ") + QUESTION + "\nGOBACK.\n")
        (self.source / "target.cbl").write_text(text, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", verify_content=True,
                             quiet=True, framework_reference_path="")
        retrieval.ensure_repository_search(self.database, self.source)
        return text

    def retrieve(self, marker):
        return retrieval.retrieve_repository_context(self.database, self.source, QUESTION,
            search_terms=[marker], max_pages=1, max_chars=512)

    def assert_target(self, result, original):
        self.assertFalse(result["orientation_only"])
        page, = result["pages"]
        self.assertLessEqual(page["start_line"], 4)
        self.assertGreaterEqual(page["end_line"], 5)
        self.assertEqual(page["source_text"], "\n".join(original.splitlines()[page["start_line"]-1:page["end_line"]]))
        self.assertLessEqual(len(page["source_text"]), 512)
        self.assertEqual(page["evidence_id"], retrieval._identify_page(page))
        # Check the actual serialized request material, not just the retriever.
        loaded = production_loader(QUESTION)(result["pages"])["pages"]
        self.assertTrue(any("BASE-VALUE + 37" in item["source_text"] for item in loaded))

    def test_exact_clue_is_not_cropped_out_by_old_question_words(self):
        original = self.build()
        self.assert_target(self.retrieve("LANTERN-CAP-VALUE"), original)

    def test_plain_new_clue_keeps_its_own_crop_priority(self):
        original = self.build("orchidallocation")
        self.assert_target(self.retrieve("orchidallocation"), original)

    def test_complete_identifier_beats_substring_even_when_old_words_match_more(self):
        original = self.build(near_prefix=True)
        self.assert_target(self.retrieve("LANTERN-CAP-VALUE"), original)

    def test_explicit_read_range_keeps_requested_location(self):
        original = self.build()
        result = retrieval.read_repository_context(self.database, self.source,
            relative_path="target.cbl", start_line=250, end_line=257, max_chars=512)
        page, = result["pages"]
        self.assertEqual(page["start_line"], 250)
        self.assertEqual(page["end_line"], 257)
        self.assertEqual(page["source_text"], "\n".join(original.splitlines()[249:257]))
        self.assertTrue(result["range_complete"])

    def test_focused_crop_still_rejects_changed_source(self):
        self.build()
        with (self.source / "target.cbl").open("a", encoding="utf-8") as handle:
            handle.write("*> changed after indexing\n")
        result = self.retrieve("LANTERN-CAP-VALUE")
        self.assertEqual(result["pages"], [])
        self.assertTrue(result["needs_refresh"])
        self.assertTrue(any(item["reason"] == "source_changed_since_index" for item in result["boundaries"]))

    def test_literal_predicate_keeps_boundaries_case_and_sentence_period(self):
        terms = {"lantern-cap-value", "reserve_limit"}
        for value in ("DISPLAY LANTERN-CAP-VALUE.", "*> reserve_LIMIT.", "LANTERN-CAP-VALUE"):
            self.assertTrue(retrieval._contains_identifier(value, terms), value)
        for value in ("LANTERN CAP VALUE", "PREFIX-LANTERN-CAP-VALUE", "RESERVE_LIMIT_SUFFIX", "", None):
            self.assertFalse(retrieval._contains_identifier(value, terms), value)


if __name__ == "__main__":
    unittest.main()
