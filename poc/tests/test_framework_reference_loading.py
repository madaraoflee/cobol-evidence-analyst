from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import framework_knowledge as knowledge
from company_api import _read_local_env


class FrameworkReferenceLoadingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manual = self.root / "使用说明"
        self.manual.mkdir()
        knowledge._CACHE.clear()

    def write(self, name, text, encoding="utf-8"):
        path = self.manual / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding=encoding)
        return path

    def test_quoted_directory_loads_nested_text_files_with_individual_provenance(self):
        first = self.write("a.MD", "# Processing stages\n\nFLOW-OPEN prepares the request.\n")
        second = self.write("detail/b.markdown", "# Completion\n\nFLOW-CLOSE finishes the request.\n")
        self.write("ignored.csv", "not a framework document")
        result = knowledge.build_framework_context(reference_path=f'"{self.manual}"',
                                                  question="FLOW-OPEN FLOW-CLOSE")
        self.assertEqual(result["loaded_document_count"], 2)
        self.assertEqual({item["name"] for item in result["documents"]}, {"a.MD", "detail/b.markdown"})
        self.assertEqual(len(result["references"]), 2)
        expected = {"a.MD": first, "detail/b.markdown": second}
        for reference in result["references"]:
            path = expected[reference["document_name"]]
            self.assertEqual(reference["document_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(reference["text"], path.read_text().splitlines()[reference["start_line"] - 1])
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_windows_environment_and_copy_as_path_quotes_are_supported(self):
        self.write("guide.txt", "# Guide\n\nFLOW-OPEN begins a request.\n")
        with mock.patch.dict(os.environ, {"REFERENCE_HOME": str(self.manual)}):
            status = knowledge.framework_status('"%reference_home%"')
        self.assertEqual(status["status"], "LOADED")
        self.assertEqual(status["loaded_document_count"], 1)

    def test_windows_config_backslashes_are_literal_not_escape_sequences(self):
        config = self.root / "local.env"
        config.write_text('FRAMEWORK_REFERENCE_PATH="C:\\team\\notes\\reference.md"\n', encoding="utf-8")
        value = _read_local_env(config)["FRAMEWORK_REFERENCE_PATH"]
        self.assertEqual(value, r"C:\team\notes\reference.md")
        self.assertNotIn("\t", value)
        self.assertNotIn("\n", value)

    def test_windows_utf16_and_utf8_bom_text_are_loaded(self):
        for encoding in ("utf-16", "utf-8-sig"):
            path = self.write("guide.txt", "# 框架说明\n\nFLOW-OPEN 表示准备处理。\n", encoding)
            status = knowledge.framework_status(path)
            self.assertEqual(status["status"], "LOADED")
            self.assertEqual(status["document"]["title"], "框架说明")

    def test_one_bad_file_does_not_hide_other_manuals(self):
        self.write("good.md", "# Valid guide\n\nFLOW-OPEN prepares work.\n")
        (self.manual / "bad.txt").write_bytes(b"\xff")
        status = knowledge.framework_status(self.manual)
        self.assertEqual(status["status"], "LOADED")
        self.assertEqual(status["loaded_document_count"], 1)
        self.assertEqual(status["loading_warnings"], [{"name": "bad.txt", "reason_code": "FRAMEWORK_REFERENCE_INVALID"}])

    def test_empty_directory_and_unreadable_documents_have_actionable_diagnostics(self):
        status = knowledge.framework_status(self.manual)
        self.assertEqual(status["reason_code"], "FRAMEWORK_REFERENCE_DIRECTORY_EMPTY")
        self.assertIn(".md", status["loading_message"])
        (self.manual / "bad.md").write_bytes(b"\xff")
        status = knowledge.framework_status(self.manual)
        self.assertEqual(status["reason_code"], "FRAMEWORK_REFERENCE_DIRECTORY_NO_READABLE_DOCUMENTS")
        self.assertIn("编码", status["loading_message"])

    def test_manual_is_available_without_exact_source_or_question_terms(self):
        self.write("first.md", "# Request processing\n\nFLOW-OPEN prepares the request.\n")
        self.write("second.md", "# Persistent state\n\nFLOW-SAVE asks for persistence.\n")
        result = knowledge.build_framework_context(reference_path=self.manual, question="这项业务如何运作？", source_pages=[])
        self.assertEqual(result["status"], "NO_MATCH")
        self.assertFalse(result["source_matches"])
        self.assertEqual(len(result["references"]), 2)
        self.assertTrue(all(item["selection_reason"] == "document_overview" for item in result["references"]))
        self.assertFalse(result["runtime_verified"])

    def test_retrieved_pages_do_not_rescan_source_or_open_database(self):
        path = self.write("guide.md", "# Request guide\n\nFLOW-OPEN prepares a request.\n")
        page = {"evidence_id": "ev-page-1", "relative_path": "request.cbl", "start_line": 50,
                "end_line": 51, "source_sha256": "a" * 64, "program_name": "REQUEST",
                "text": 'MOVE "BEGIN" TO FLOW-OPEN.\nCALL "SERVICE" USING FLOW-OPEN.', "format_hint": "free"}
        with mock.patch.object(knowledge, "_source_candidates", side_effect=AssertionError("source rescan")), \
             mock.patch.object(knowledge.sqlite3, "connect", side_effect=AssertionError("database reopened")):
            result = knowledge.build_framework_context(self.root / "absent.sqlite", reference_path=path,
                                                      source_root=self.root, source_pages=[page], question="处理规则？")
        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(result["source_matches"][0]["evidence_id"], "ev-page-1")
        self.assertEqual(result["source_matches"][0]["start_line"], 50)
        self.assertEqual(result["coverage"]["source_verification"], "provided_retrieval_pages")
        self.assertFalse(result["coverage"]["source_hash_verified"])
        self.assertEqual(result["external_calls"], [])

    def test_retrieved_comment_does_not_claim_framework_use(self):
        path = self.write("guide.md", "# Guide\n\nFLOW-OPEN prepares a request.\n")
        result = knowledge.build_framework_context(reference_path=path, source_pages=[{
            "text": "*> FLOW-OPEN\nDISPLAY 'HELLO'.", "format_hint": "free", "evidence_id": "ev-page-1",
            "relative_path": "a.cbl", "start_line": 1, "end_line": 2, "source_sha256": "a" * 64}])
        self.assertFalse(result["source_matches"])
        self.assertEqual(result["references"][0]["selection_reason"], "document_overview")

    def test_retrieved_document_context_is_bounded_without_losing_other_files(self):
        for number in range(8):
            self.write(f"{number}.md", f"# Topic {number}\n\n" + "Background guidance. " * 100)
        result = knowledge.build_framework_context(reference_path=self.manual, source_pages=[])
        self.assertLessEqual(result["coverage"]["reference_chars"], 8000)
        self.assertGreater(len({item["document_name"] for item in result["references"]}), 1)
        self.assertTrue(result["coverage"]["references_truncated"])

    def test_collection_cache_refreshes_changes_and_removed_documents(self):
        first = self.write("a.md", "# Guide\n\nFLOW-OPEN starts work.\n")
        second = self.write("b.md", "# Guide\n\nFLOW-CLOSE finishes work.\n")
        initial = knowledge.framework_status(self.manual)
        first.write_text("# Guide\n\nFLOW-OPEN prepares state.\n", encoding="utf-8")
        changed = knowledge.framework_status(self.manual)
        self.assertNotEqual(initial["document"]["sha256"], changed["document"]["sha256"])
        second.unlink()
        remaining = knowledge.framework_status(self.manual)
        self.assertEqual(remaining["loaded_document_count"], 1)
        self.assertNotEqual(changed["document"]["sha256"], remaining["document"]["sha256"])

    def test_weak_question_matches_keep_basic_framework_guidance_without_claiming_source_use(self):
        path = self.write("guide.md", "# Processing guide\n\n## Architecture\n\n"
            "Shared record definitions and generated routines jointly describe the application behavior.\n\n"
            "## Overview\n\nThe visible caller supplies operation inputs and interprets the returned outcome.\n\n"
            "## System notes\n\nThe system has a release number and a document index.\n")
        result = knowledge.build_framework_context(reference_path=path,
            question="How does the system calculate an amount?", source_pages=[])
        background = [item for item in result["references"] if item["selection_reason"] == "document_overview"]
        self.assertEqual({item["heading"].rsplit(" / ", 1)[-1] for item in background}, {"Architecture", "Overview"})
        self.assertTrue(any(item["selection_reason"] == "question_only" for item in result["references"]))
        self.assertEqual(result["status"], "NO_MATCH")
        self.assertFalse(result["source_matches"])
        self.assertFalse(result["external_calls"])
        self.assertLessEqual(sum(len(item["text"]) for item in background), knowledge.MAX_BACKGROUND_CHARS)

    def test_background_keeps_relevant_operations_and_existing_request_budgets(self):
        path = self.write("guide.md", "# Processing guide\n\n## Architecture\n\n"
            "The application combines generated definitions with caller-owned decisions and status handling.\n\n"
            + "\n\n".join(f"## Operation {number}\n\nACTION-{number:02d} selects the corresponding operation and returns its status."
                          for number in range(14)))
        page = {"evidence_id": "ev-page-1", "relative_path": "request.cbl", "start_line": 1,
                "end_line": 1, "source_sha256": "a" * 64, "format_hint": "free",
                "text": 'MOVE "ACTION-13" TO REQUEST-ACTION.'}
        result = knowledge.build_framework_context(reference_path=path, question="Explain the decision", source_pages=[page])
        self.assertTrue(any(item["selection_reason"] == "document_overview" for item in result["references"]))
        matched = [item for item in result["references"] if item["selection_reason"] == "source_marker"]
        self.assertTrue(any("ACTION-13" in item["matched_terms"] for item in matched))
        self.assertEqual(result["status"], "MATCHED")
        self.assertEqual(len(result["source_matches"]), 1)
        self.assertLessEqual(len(result["references"]), knowledge.MAX_REFERENCES)
        self.assertLessEqual(result["coverage"]["reference_chars"], 8000)
        with mock.patch.object(knowledge, "MAX_REFERENCES", 1):
            small = knowledge.build_framework_context(reference_path=path, source_pages=[page])
        self.assertEqual(small["references"][0]["selection_reason"], "source_marker")
        self.assertEqual(small["status"], "MATCHED")

    def test_identical_documents_have_distinct_references(self):
        for name in ("a.md", "b.md"):
            self.write(name, "# Guide\n\nFLOW-OPEN prepares work.\n")
        result = knowledge.build_framework_context(reference_path=self.manual, question="FLOW-OPEN")
        self.assertEqual(len({item["reference_id"] for item in result["references"]}), 2)


if __name__ == "__main__":
    unittest.main()
