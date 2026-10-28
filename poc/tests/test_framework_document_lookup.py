from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import framework_knowledge as knowledge


class FrameworkDocumentLookupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        knowledge._CACHE.clear()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def navigation(self, name="Billing_Layer1_Business_Navigation.md"):
        return self.write(name, "# Billing Business Navigation Index\n\n"
            "## Overview\n\nUse this index to locate the relevant business process before examining its implementation.\n\n"
            "## Collection\n\nThe collection entry is COLLECT01 in collection.cbl; inspect returned status before explaining outcomes.\n")

    def test_filename_search_reads_original_content_without_filename_in_body(self):
        target = self.navigation()
        result = knowledge.search_framework_context(target.name, reference_path=self.root)
        self.assertEqual(result["coverage"]["document_lookup"], [target.name])
        self.assertEqual({item["document_name"] for item in result["references"]}, {target.name})
        self.assertTrue(any("collection.cbl" in item["text"] for item in result["references"]))
        lines = target.read_text().splitlines()
        for item in result["references"]:
            self.assertEqual(item["document_sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
            self.assertEqual(item["text"], "\n".join(lines[item["start_line"] - 1:item["end_line"]]))
        self.assertFalse(result["source_matches"])

    def test_filename_in_followup_beats_noisy_manual_and_source_markers(self):
        target = self.navigation()
        self.write("a-runtime.md", "# Runtime guide\n\n## Architecture\n\n"
            "The runtime coordinates generated source with the visible caller and status interpretation.\n\n"
            + "\n\n".join(f"## Entry {number}\n\nACTION-{number:02d} implements a runtime operation."
                              for number in range(20)))
        with mock.patch.object(knowledge, "MAX_REFERENCES", 2):
            result = knowledge.build_framework_context(reference_path=self.root,
                question=f"为什么没有读到 {target.name}？", source_pages=[{
                    "text": "MOVE 'ACTION-01' TO REQUEST-ACTION.", "format_hint": "free",
                    "relative_path": "request.cbl", "start_line": 1, "end_line": 1,
                    "evidence_id": "ev-request", "source_sha256": "a" * 64}])
        self.assertEqual({item["document_name"] for item in result["references"]}, {target.name})
        self.assertTrue(all(item["selection_reason"] == "question_only" for item in result["references"]))
        self.assertFalse(result["source_matches"])
        self.assertFalse(result["runtime_verified"])

    def test_single_file_configuration_supports_filename_search(self):
        target = self.navigation()
        result = knowledge.search_framework_context(target.name, reference_path=target)
        self.assertEqual(result["coverage"]["document_lookup"], [target.name])
        self.assertEqual(len(result["references"]), 2)

    def test_relative_and_windows_paths_disambiguate_duplicate_basenames(self):
        self.navigation("current/billing-navigation.md")
        self.write("legacy/billing-navigation.md", "# Old navigation\n\nRetired processing notes.\n")
        for query in ("current/billing-navigation.md", r"C:\reference\current\billing-navigation.md",
                      "/reference/current/billing-navigation.md"):
            with self.subTest(query=query):
                result = knowledge.search_framework_context(query, reference_path=self.root)
                self.assertEqual(result["coverage"]["document_lookup"], ["current/billing-navigation.md"])
                self.assertEqual({item["document_name"] for item in result["references"]},
                                 {"current/billing-navigation.md"})

    def test_missing_directory_does_not_fall_back_to_existing_basenames(self):
        self.navigation("current/billing-navigation.md")
        self.navigation("legacy/billing-navigation.md")
        for query in ("missing/billing-navigation.md", "missing/current/billing-navigation.md"):
            with self.subTest(query=query):
                result = knowledge.search_framework_context(query, reference_path=self.root)
                self.assertEqual(result["coverage"]["document_lookup"], [])
                self.assertEqual(result["references"], [])

    def test_single_file_accepts_absolute_paths_without_exposing_them(self):
        target = self.navigation()
        for query in (f"C:/reference notes/{target.name}", f"/reference notes/{target.name}"):
            with self.subTest(query=query):
                result = knowledge.search_framework_context(query, reference_path=target)
                self.assertEqual(result["coverage"]["document_lookup"], [target.name])
                self.assertEqual(len(result["references"]), 2)
                self.assertNotIn(query, str(result))

    def test_exact_document_title_and_basename_stem_read_all_bounded_sections(self):
        target = self.navigation()
        self.write("unrelated.md", "# Other business guide\n\nBusiness navigation is a generic topic.\n")
        for query in ("Billing Business Navigation Index", target.stem,
                      "billing layer1 business navigation"):
            with self.subTest(query=query):
                result = knowledge.search_framework_context(query, reference_path=self.root)
                self.assertEqual(result["coverage"]["document_lookup"], [target.name])
                self.assertEqual(len(result["references"]), 2)
                self.assertEqual({item["document_name"] for item in result["references"]}, {target.name})

    def test_business_question_can_match_filename_vocabulary(self):
        target = self.write("收款业务索引.md", "# Entries\n\nCOLLECT01 is the entry to collection.cbl.\n")
        self.write("other.md", "# Other\n\nUnrelated guidance.\n")
        result = knowledge.build_framework_context(reference_path=self.root,
            question="收款业务如何处理？", source_pages=[])
        self.assertEqual(result["references"][0]["document_name"], target.name)
        self.assertEqual(result["references"][0]["selection_reason"], "question_only")
        self.assertFalse(result["source_matches"])

    def test_business_topic_in_filename_survives_unrelated_source_marker_matches(self):
        target = self.navigation("Payment_Layer1_Business_Navigation.md")
        for number in range(14):
            self.write(f"runtime-{number}.md", f"# Runtime {number}\n\n"
                "REQUEST-STATE is a shared state marker; inspect the returned state.\n")
        result = knowledge.build_framework_context(reference_path=self.root,
            question="分析 payment 的业务信息、改动注意事项与测试范围。", source_pages=[{
                "text": "IF REQUEST-STATE = 'A' CONTINUE END-IF.", "format_hint": "free",
                "relative_path": "request.cbl", "start_line": 1, "end_line": 1,
                "evidence_id": "ev-request", "source_sha256": "a" * 64}])
        self.assertEqual(result["references"][0]["document_name"], target.name)
        self.assertEqual(result["references"][0]["selection_reason"], "question_only")
        self.assertTrue(any(item["selection_reason"] == "source_marker" for item in result["references"]))
        self.assertLessEqual(len(result["references"]), knowledge.MAX_REFERENCES)

    def test_explicit_document_read_preserves_limits_and_reports_omissions(self):
        target = self.write("billing-navigation.md", "# Billing Navigation\n\n" + "\n\n".join(
            f"## Process {number}\n\nProcess {number} uses a distinct entry and requires checking its outcome."
            for number in range(20)))
        result = knowledge.search_framework_context(target.name, reference_path=self.root,
                                                    max_references=3, max_chars=200)
        self.assertLessEqual(len(result["references"]), 3)
        self.assertLessEqual(sum(len(item["text"]) for item in result["references"]), 200)
        self.assertTrue(result["coverage"]["references_truncated"])
        self.assertEqual(result["coverage"]["matched_sections"], 20)

    def test_missing_filename_does_not_match_similarly_named_document(self):
        self.navigation()
        result = knowledge.search_framework_context("Old_Billing_Layer1_Business_Navigation.md",
                                                     reference_path=self.root)
        self.assertEqual(result["coverage"]["document_lookup"], [])
        self.assertEqual(result["references"], [])


if __name__ == "__main__":
    unittest.main()
