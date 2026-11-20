"""Literal leads anywhere in an imported snapshot remain available to normal questions."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import closing

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
import repository_discovery as discovery
from source_reading import _identify_page
from source_session import QuestionSourceSession


class CrossFileLiteralRetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(temporary.cleanup)
        cls.source = Path(temporary.name) / "source"
        cls.source.mkdir()
        cls.database = cls.source.parent / "index.sqlite"

        def program(path, name, *lines):
            (cls.source / path).write_text("\n".join([
                "       IDENTIFICATION DIVISION.", f"       PROGRAM-ID. {name}.",
                "       PROCEDURE DIVISION.", *lines, "           GOBACK.", "",
            ]), encoding="utf-8")

        program("a-entry.cbl", "BILLING", "      * Premium collection",
                "           CALL 'APPLYNET' USING DUE-AMT.")
        (cls.source / "z-worker.cbl").write_text("\n".join([
            "       IDENTIFICATION DIVISION.", "       PROGRAM-ID. APPLYNET.", "       DATA DIVISION.",
            "       WORKING-STORAGE SECTION.", "       01 PLAN-CODE PIC X(8).",
            "       01 CREDIT-AMT PIC 9(6)V99.", "       LINKAGE SECTION.",
            "       01 LK-BALANCE PIC 9(6)V99.", "       PROCEDURE DIVISION USING LK-BALANCE.",
            "           IF PLAN-CODE = 'ACTIVE'", "               COMPUTE LK-BALANCE = LK-BALANCE - CREDIT-AMT",
            "           END-IF.", "           GOBACK.", "",
        ]), encoding="utf-8")
        program("a-screen.cbl", "SCREENOUT", "      * Only a PLANKEY caption",
                "           DISPLAY 'PLANKEY'.")
        for index in range(130):
            program(f"mid-{index:03}.cbl", f"ROUTINE{index:03}", "           CONTINUE.")
        program("z-rule.cbl", "CHECKCODE", "           IF PLAN-CODE = 'PM/XADJKEYENDORSED'",
                "               PERFORM APPLY-RATE", "           END-IF.",
                *["      * irrelevant separator for independent crops" for _ in range(45)],
                "           IF PLAN-CODE = 'L-PLANKEYEXT'", "               PERFORM APPLY-RATE",
                "           END-IF.", "       APPLY-RATE.", "           COMPUTE NET-DUE = DUE-AMT - CREDIT-AMT.")
        build_business_index(cls.source, cls.database, source_format="fixed", verify_content=True,
                             framework_reference_path="")
        discovery.ensure_repository_search(cls.database, cls.source)

    def search(self, question, **kwargs):
        return discovery.retrieve_repository_context(self.database, self.source, question, **kwargs)

    def test_ordinary_questions_receive_late_embedded_literal_in_first_model_request(self):
        for question in ("Where is XADJKEY applied?", "xadjkey 在什么条件下使用？", "Where is `xadjkey` applied?"):
            requests = []

            def respond(request):
                wire = json.loads(json.loads(request.body)["messages"][-1]["content"])
                requests.append(wire)
                return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant",
                    "content": "Unable to confirm."}, "finish_reason": "stop"}]}))

            with self.subTest(question=question):
                run_business_chat(question, self.database, self.source,
                    CompanyAPIConfig("https://neutral.example.invalid/v1", "offline", api_key="offline"),
                    framework_reference_path="", transport=respond)
                pages = [page for part in requests[0]["source_context"] for page in part.get("pages", [])]
                hit, = [page for page in pages if "PM/XADJKEYENDORSED" in page["source_text"]]
                self.assertEqual(hit["relative_path"], "z-rule.cbl")
                self.assertEqual(hit["evidence_id"], _identify_page(hit))
                self.assertEqual(requests[0]["response_mode"], "business_analysis")
                self.assertEqual(requests[0]["completed_searches"][0]["literal_source_files"], 1)
                self.assertGreater(requests[0]["completed_searches"][0]["literal_source_pages"], 0)

    def test_weak_exact_screen_and_comment_do_not_hide_embedded_branch(self):
        result = self.search("Explain PLANKEY", max_pages=1)
        self.assertEqual(result["matched_file_count"], 1)
        self.assertEqual(result["selected_paths"], ["z-rule.cbl"])
        self.assertIn("IF PLAN-CODE = 'L-PLANKEYEXT'", result["pages"][0]["source_text"])
        both = self.search("check both", search_terms=["xadjkey", "plankey"], max_pages=2, max_chars=1024)
        original = [p["source_text"] for p in both["pages"] if p["relative_path"] == "z-rule.cbl"]
        self.assertTrue(any("PM/XADJKEYENDORSED" in text for text in original))
        self.assertTrue(any("L-PLANKEYEXT" in text for text in original))

    def test_literal_search_respects_exclusive_scope_and_checks_only_selected_sources(self):
        scoped = self.search("Only analyze a-screen.cbl", search_terms=["XADJKEY"])
        self.assertNotIn("z-rule.cbl", scoped["selected_paths"])
        with QuestionSourceSession(self.database, self.source) as session:
            first = self.search("XADJKEY", source_session=session)
            repeated = self.search("XADJKEY", source_session=session)
        self.assertTrue(repeated["literal_search"]["cache_reused"])
        self.assertEqual(first["cache"]["content_verified_files"], 1)
        self.assertEqual(first["pages"][0]["source_verification"], "content_hash")
        self.assertEqual(first["pages"][0]["evidence_id"], repeated["pages"][0]["evidence_id"])

    def test_literal_lookup_keeps_callee_without_topic_words_and_its_formula(self):
        result = self.search("premium collection")
        pages = {page["relative_path"]: page["source_text"] for page in result["pages"]}
        self.assertIn("CALL 'APPLYNET'", pages["a-entry.cbl"])
        self.assertIn("IF PLAN-CODE = 'ACTIVE'", pages["z-worker.cbl"])
        self.assertIn("PROCEDURE DIVISION USING LK-BALANCE", pages["z-worker.cbl"])
        self.assertIn("COMPUTE LK-BALANCE = LK-BALANCE - CREDIT-AMT", pages["z-worker.cbl"])

    def test_changed_source_cannot_be_cited_from_literal_search(self):
        path = self.source / "z-rule.cbl"
        saved = path.read_bytes()
        try:
            path.write_bytes(saved.replace(b"XADJKEY", b"NEWRULE"))
            with QuestionSourceSession(self.database, self.source) as session:
                result = self.search("XADJKEY", source_session=session)
            self.assertFalse(result["pages"])
            self.assertEqual(result["literal_search"]["selected_pages"], 0)
            self.assertTrue(result["needs_refresh"])
        finally:
            path.write_bytes(saved)

    def test_unrelated_prose_avoids_scan_and_long_literal_scan_can_be_cancelled(self):
        with mock.patch.object(discovery, "_literal_source_matches",
                               side_effect=AssertionError("unexpected literal scan")):
            self.search("How are business decisions made?")
        ticks = 0
        def stop_scan():
            nonlocal ticks
            ticks += 1
            if ticks == 15:
                raise InterruptedError("Cancelled")
        with closing(discovery._connect(self.database)) as connection, self.assertRaises(InterruptedError):
            discovery._literal_source_matches(connection, ["xadjkey"], [], "", (), 1, None, stop_scan)
        self.assertEqual(ticks, 15)
