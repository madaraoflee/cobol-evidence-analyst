from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from business_map import build_business_map
from question_investigation import build_question_investigation
from repository_discovery import discover_repository, ensure_repository_search, retrieve_repository_context


class IdentityConsumerScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "source"
        self.source.mkdir()
        self.database = Path(temporary.name) / "index.sqlite"
        for relative, name, multiplier in (
                ("first.cbl", "FIRSTPLAN", 2),
                ("second.cbl", "SECONDPLAN", 3),
                ("history.cbl", "HISTORYPLAN", 4)):
            (self.source / relative).write_text(
                f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\n"
                "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
                "01 INPUT-AMOUNT PIC 9(6).\n01 RESULT-AMOUNT PIC 9(6).\n"
                "PROCEDURE DIVISION.\nMAIN.\n"
                f"COMPUTE RESULT-AMOUNT = INPUT-AMOUNT * {multiplier}.\nGOBACK.\n",
                encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free", quiet=True,
                             verify_content=True)
        ensure_repository_search(self.database, self.source)

    @staticmethod
    def identity(status, paths, *, hard=False):
        return {"status": status, "direct_paths": paths, "hard_constraint": hard,
                "requested": ["UNLOCATED"], "candidates": [], "selection_source": "question"}

    def test_soft_candidates_supply_distinct_source_pages_and_investigation(self):
        identity = self.identity("ambiguous", ["first.cbl", "second.cbl"])
        with mock.patch("repository_discovery.resolve_source_identity", return_value=identity):
            mapping = build_business_map(self.database, self.source, "未知候选")
            context = retrieve_repository_context(self.database, self.source, "未知候选")
        self.assertEqual(mapping["direct_paths"], ["first.cbl", "second.cbl"])
        pages = {page["relative_path"]: page for page in context["pages"]}
        self.assertEqual(set(pages), {"first.cbl", "second.cbl"})
        self.assertIn("* 2", pages["first.cbl"]["source_text"])
        self.assertIn("* 3", pages["second.cbl"]["source_text"])
        self.assertTrue(all("source_candidate" in page["selection_reasons"] for page in pages.values()))
        investigation = build_question_investigation("未知候选", mapping, database_path=self.database)
        self.assertEqual(investigation["state"], "located")

    def test_empty_soft_candidates_preserve_lexical_and_history_evidence(self):
        identity = self.identity("not_found", [])
        with mock.patch("repository_discovery.resolve_source_identity", return_value=identity):
            lexical = build_business_map(self.database, self.source, "SECONDPLAN 的处理")
            historical = build_business_map(self.database, self.source, "接着解释", prior_paths=["history.cbl"])
            context = retrieve_repository_context(self.database, self.source, "接着解释", prior_paths=["history.cbl"])
        self.assertEqual(lexical["direct_paths"], ["second.cbl"])
        self.assertEqual(historical["selected_paths"], ["history.cbl"])
        self.assertEqual(context["pages"][0]["relative_path"], "history.cbl")
        self.assertIn("conversation_context", context["pages"][0]["selection_reasons"])

    def test_soft_initial_focus_precedes_incidental_matches_but_search_can_expand(self):
        identity = self.identity("resolved", ["first.cbl"])
        with mock.patch("repository_discovery.resolve_source_identity", return_value=identity):
            initial = retrieve_repository_context(self.database, self.source, "SECONDPLAN")
            matched = retrieve_repository_context(self.database, self.source, "FIRSTPLAN")
            searched = retrieve_repository_context(self.database, self.source, "SECONDPLAN",
                                                   search_terms=["SECONDPLAN"], prior_paths=["first.cbl"])
        self.assertEqual(initial["pages"][0]["relative_path"], "first.cbl")
        self.assertIn("second.cbl", {page["relative_path"] for page in initial["pages"]})
        self.assertIn("question_match", matched["pages"][0]["selection_reasons"])
        self.assertEqual(matched["omitted_matched_pages"], 0)
        self.assertEqual(searched["pages"][0]["relative_path"], "second.cbl")
        self.assertIn("first.cbl", {page["relative_path"] for page in searched["pages"]})

    def test_hard_unresolved_constraint_still_blocks_all_fallbacks(self):
        for hard in (True, None):
            with self.subTest(hard=hard):
                identity = self.identity("not_found", [], hard=True)
                if hard is None:
                    del identity["hard_constraint"]
                with mock.patch("repository_discovery.resolve_source_identity", return_value=identity):
                    mapping = build_business_map(self.database, self.source, "SECONDPLAN", prior_paths=["history.cbl"])
                    context = retrieve_repository_context(self.database, self.source, "SECONDPLAN", prior_paths=["history.cbl"])
                self.assertEqual(mapping["direct_paths"], [])
                self.assertEqual(context["pages"], [])
                investigation = build_question_investigation("SECONDPLAN", mapping, database_path=self.database)
                self.assertFalse(investigation["can_answer"])

    def test_validated_focus_is_forwarded_by_all_retrieval_entry_points(self):
        for operation in (
                lambda: discover_repository(self.database, "FIRSTPLAN 的处理", focus_paths=["second.cbl"]),
                lambda: retrieve_repository_context(self.database, self.source, "FIRSTPLAN 的处理", focus_paths=["second.cbl"]),
                lambda: build_business_map(self.database, self.source, "FIRSTPLAN 的处理", focus_paths=["second.cbl"])):
            result = operation()
            self.assertEqual(result["source_identity"]["selection_source"], "focus")
            self.assertEqual(result["source_identity"]["direct_paths"], ["second.cbl"])


if __name__ == "__main__":
    unittest.main()
