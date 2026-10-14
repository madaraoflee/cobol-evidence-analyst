"""An interactive question must not silently become a repository rebuild."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_source import analyze_source
from api_error_details import build_diagnostic, sanitize_diagnostic
from business_index import PARSER_VERSION
from company_api import CompanyAPIConfig, TransportResponse


SOURCE = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. CALCULATION.\n"
          "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
          "01 IN-AMOUNT PIC 9(4).\n01 OUT-AMOUNT PIC 9(6).\n"
          "PROCEDURE DIVISION.\nMAIN.\n"
          "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2 END-COMPUTE.\nGOBACK.\n")


class IndexUpgradeQuestionGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.output = self.root / "source", self.root / "output"
        self.source.mkdir()
        (self.source / "calculation.cbl").write_text(SOURCE, encoding="utf-8")
        self.database = self.output / "structural-index.sqlite"
        self.options = {"analysis_mode": "business", "reading_strategy": "retrieval",
                        "source_format": "free", "framework_reference_path": ""}
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model",
                                       api_key="offline-only")

    def index(self, **options):
        return analyze_source(self.source, self.output, **(self.options | options))

    def change_metadata(self, key, value):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE metadata SET value=? WHERE key=?", (value, key))

    def ask_without_rebuild(self, **options):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        transport = mock.Mock(side_effect=AssertionError("An incompatible index must not call a model"))
        with mock.patch("analyze_source.build_business_index", side_effect=AssertionError("Unexpected full rebuild")), \
             mock.patch("analyze_source.build_structural_index", side_effect=AssertionError("Unexpected full parse")), \
             mock.patch("analyze_source.refresh_source_catalog", side_effect=AssertionError("Unexpected source scan")):
            report = self.index(question="CALCULATION 的 OUT-AMOUNT 怎么计算？", allow_network=True,
                                config=self.config, transport=transport, **options)
        self.assertEqual(report["reason_code"], "SOURCE_INDEX_UPDATE_REQUIRED")
        self.assertEqual(report["question_status"], "NOT_READY")
        self.assertFalse(report["index_reused"])
        self.assertFalse(report["source_manifest_verified"])
        self.assertEqual(report["index_update"]["model_requests"], 0)
        self.assertFalse(report["index_update"]["source_directory_scanned"])
        self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)
        agent = json.loads((self.output / "agent-result.json").read_text(encoding="utf-8"))
        self.assertIsNone(agent["agent_result"])
        self.assertEqual(agent["reason_code"], report["reason_code"])
        self.assertNotIn("OBSOLETE ANSWER", (self.output / "agent-result.md").read_text(encoding="utf-8"))
        self.assertIn("更新源码", (self.output / "agent-result.md").read_text(encoding="utf-8"))
        transport.assert_not_called()
        return report

    def test_old_parser_blocks_question_without_rebuild_even_on_repeat(self):
        self.index()
        self.change_metadata("parser_version", "business-sparse-previous")
        (self.output / "agent-result.md").write_text("OBSOLETE ANSWER", encoding="utf-8")
        for _ in range(2):
            report = self.ask_without_rebuild()
            self.assertIn("parser_version_changed", report["index_update"]["reasons"])
            self.assertEqual(report["index_update"]["expected_parser_version"], PARSER_VERSION)

    def test_changed_input_options_do_not_reparse_during_question(self):
        self.index()
        for options in ({"source_format": "fixed"}, {"encoding": "utf-8"},
                        {"extensions": frozenset({".cob"})}, {"include_extensionless": False}):
            with self.subTest(options=options):
                report = self.ask_without_rebuild(**options)
                self.assertIn("source_options_changed", report["index_update"]["reasons"])

    def test_search_schema_change_does_not_trigger_full_repository_import(self):
        self.index()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE repo_metadata SET value='older-search' WHERE key='version'")
        report = self.ask_without_rebuild()
        self.assertEqual(report["index_update"]["reasons"], ["search_index_version_changed"])

    def test_compilation_changes_and_removal_require_explicit_update(self):
        context = self.root / "compilation.json"
        original = {"copy_libraries": ["copybooks", "shared"], "copy_library_order_known": True,
                    "compile_conditions": {"FEATURE": True}, "framework_version": "1"}
        context.write_text(json.dumps(original), encoding="utf-8")
        self.index(compilation_context_path=context)
        for change in ({"compile_conditions": {"FEATURE": False}},
                       {"copy_libraries": ["shared", "copybooks"]}, {"framework_version": "2"}):
            with self.subTest(change=change):
                context.write_text(json.dumps(original | change), encoding="utf-8")
                report = self.ask_without_rebuild(compilation_context_path=context)
                self.assertIn("compilation_context_changed", report["index_update"]["reasons"])
        report = self.ask_without_rebuild()
        self.assertIn("compilation_context_changed", report["index_update"]["reasons"])

    def test_explicit_update_migrates_index_then_question_reuses_it(self):
        self.index()
        self.change_metadata("parser_version", "business-sparse-previous")
        self.ask_without_rebuild()
        refreshed = self.index()
        self.assertEqual(refreshed["build_report"]["parser_version"], PARSER_VERSION)
        answer = {"runner_status": "COMPLETED", "agent_result": {"status": "ANSWERED", "answer": "当前源码。"}}
        with mock.patch("business_chat.run_business_chat", return_value=answer) as run:
            report = self.index(question="CALCULATION 说明", allow_network=True, config=self.config)
        self.assertTrue(report["index_reused"])
        run.assert_called_once()

    def test_first_question_can_still_import_when_index_does_not_exist(self):
        from business_index import build_business_index
        calls = []

        def transport(request):
            calls.append(request)
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "content": "当前程序计算输出金额。"}, "finish_reason": "stop"}]}))

        from agent_policy import AgentPolicy
        with mock.patch("analyze_source.build_business_index", wraps=build_business_index) as build:
            report = self.index(question="CALCULATION 说明", allow_network=True,
                config=self.config, transport=transport,
                agent_policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))
        build.assert_called_once()
        self.assertTrue(report["source_manifest_verified"])
        self.assertEqual(len(calls), 1)

    def test_operational_diagnostics_are_safe_and_actionable(self):
        for code, category, expected in (
            ("SOURCE_INDEX_UPDATE_REQUIRED", "index_update_required", "更新源码"),
            ("TRANSPORT_BUSY", "transport_busy", "未发送"),
            ("QUESTION_TIME_BUDGET_EXHAUSTED", "question_time_budget", "先确认请求状态"),
        ):
            with self.subTest(code=code):
                diagnostic = build_diagnostic(code)
                self.assertEqual(diagnostic["category"], category)
                self.assertEqual(sanitize_diagnostic(diagnostic), diagnostic)
                self.assertIn(expected, diagnostic["reason"] + diagnostic["next_step"])


if __name__ == "__main__":
    unittest.main()
