"""Real-index regressions for analysis identity and supporting-source discovery."""

from contextlib import closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from analyze_source import analyze_source
from business_chat import run_business_chat
from business_index import build_business_index
from business_map import build_business_map
from company_api import CompanyAPIConfig, TransportResponse
from complete_working_set import build_complete_working_set
from evidence_context import EvidenceContext
from indexed_chat import try_indexed_question
from repo_inventory import DEFAULT_EXTENSIONS
from repository_discovery import discover_repository, ensure_repository_search, retrieve_repository_context
from repository_identity import identity_blocks_analysis, resolve_source_identity
from source_session import QuestionSourceSession


def program(name, statements="CONTINUE."):
    return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 ROUTE-NAME PIC X(20) VALUE 'HELPER'.\n"
            "PROCEDURE DIVISION.\nMAIN.\n" + statements + "\nGOBACK.\n")


class AuxiliarySearchScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="auxiliary-source-regression-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "output"
        self.database = self.output / "structural-index.sqlite"
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model",
                                       api_key="offline-only")
        environment = mock.patch.dict(os.environ, {"FRAMEWORK_REFERENCE_PATH": ""})
        environment.start()
        self.addCleanup(environment.stop)
        for owner, name in ((socket, "create_connection"), (socket.socket, "connect"),
                            (socket.socket, "connect_ex")):
            patcher = mock.patch.object(owner, name, side_effect=AssertionError("Offline test forbids network"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, path, text):
        destination = self.source / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")

    def build(self):
        self.output.mkdir(exist_ok=True)
        build_business_index(self.source, self.database, source_format="free", quiet=True,
                             framework_reference_path="", verify_content=True)
        ensure_repository_search(self.database, self.source)

    def fixture(self, *, static=False):
        self.write("app/root.cbl", program("ROOT", "CALL 'HELPER'." if static else "CALL ROUTE-NAME."))
        self.write("lib/helper.cbl", program("HELPER", "DISPLAY 'UNIQUE-PROOF'."))
        self.build()

    def identity(self, question, *, entry=None, search=None):
        with closing(sqlite3.connect(self.database)) as connection:
            return resolve_source_identity(connection, question, search, entry_program=entry)

    def test_auxiliary_search_finds_other_file_without_replacing_explicit_root(self):
        self.fixture()
        question = "Explain app/root.cbl workflow"
        context = retrieve_repository_context(self.database, self.source, question,
                                              search_terms=["UNIQUE-PROOF"])
        self.assertIn("lib/helper.cbl", {page["relative_path"] for page in context["pages"]})
        self.assertEqual(context["source_identity"]["direct_paths"], ["app/root.cbl"])
        self.assertTrue(context["source_identity"]["hard_constraint"])
        mapping = build_business_map(self.database, self.source, question, search_terms=["HELPER"])
        self.assertEqual(mapping["direct_paths"], ["app/root.cbl"])
        self.assertEqual(mapping["source_identity"]["selection_source"], "question")

    def test_supplementary_search_also_keeps_resolved_program_name_as_root(self):
        self.fixture()
        self.assertEqual(self.identity("Explain ROOT", search=["HELPER"])["direct_paths"], ["app/root.cbl"])

    def test_other_version_remains_a_separate_candidate_with_its_own_source_hash(self):
        self.write("variants/current/handler.cbl", program("HANDLER", "DISPLAY 'CURRENT-RULE'."))
        self.write("variants/archive/handler.cbl", program("HANDLER", "DISPLAY 'ARCHIVE-RULE'."))
        self.build()
        current = "variants/current/handler.cbl"
        archive = "variants/archive/handler.cbl"
        question = f"Explain {current}"
        context = retrieve_repository_context(self.database, self.source, question, search_terms=[archive])
        pages = {page["relative_path"]: page for page in context["pages"]}
        self.assertEqual(set(pages), {current, archive})
        self.assertEqual(context["source_identity"]["direct_paths"], [current])
        self.assertNotEqual(pages[current]["source_sha256"], pages[archive]["source_sha256"])
        self.assertNotEqual(pages[current]["evidence_id"], pages[archive]["evidence_id"])
        self.assertIn("CURRENT-RULE", pages[current]["source_text"])
        self.assertNotIn("ARCHIVE-RULE", pages[current]["source_text"])
        self.assertIn("ARCHIVE-RULE", pages[archive]["source_text"])
        self.assertNotIn("CURRENT-RULE", pages[archive]["source_text"])
        mapping = build_business_map(self.database, self.source, question, search_terms=[archive])
        self.assertEqual(mapping["direct_paths"], [current])
        self.assertEqual(mapping["source_identity"]["direct_paths"], [current])
        with QuestionSourceSession(self.database, self.source) as session:
            whole = build_complete_working_set(self.database, session, mapping, AgentPolicy())
        evidence = EvidenceContext()
        evidence.accept(context, "search")
        evidence.accept(whole, "complete_working_set")
        payload = {"source_context": evidence.bundle(evidence.selected_pages(36000,
                    max_complete_source_characters=128000))}
        EvidenceContext.reconcile_payload(payload)
        working_set = payload["source_context"][0]["working_set"]
        self.assertEqual(working_set["root_paths"], [current])
        self.assertEqual(working_set["supplied_complete_paths"], [current])
        self.assertNotIn(archive, working_set["candidate_paths"])

    def test_explicit_only_file_scope_blocks_other_sources_and_neighbours(self):
        self.fixture(static=True)
        for question in ("Only analyze app/root.cbl", "只分析 app/root.cbl 的业务流程", "Only analyze ROOT"):
            with self.subTest(question=question):
                context = retrieve_repository_context(self.database, self.source, question,
                                                      search_terms=["UNIQUE-PROOF"])
                self.assertEqual({page["relative_path"] for page in context["pages"]}, {"app/root.cbl"})
                self.assertTrue(context["source_identity"]["exclusive_source_scope"])
                discovery = discover_repository(self.database, question, search_terms=["HELPER"])
                mapping = build_business_map(self.database, self.source, question, search_terms=["HELPER"])
                self.assertEqual(discovery["selected_paths"], ["app/root.cbl"])
                self.assertEqual(mapping["selected_paths"], ["app/root.cbl"])

    def test_limiting_answer_topic_does_not_exclude_supporting_source_files(self):
        self.fixture()
        for question in ("Explain app/root.cbl; only analyze conditions", "说明 app/root.cbl，只分析拒绝条件"):
            with self.subTest(question=question):
                context = retrieve_repository_context(self.database, self.source, question,
                                                      search_terms=["UNIQUE-PROOF"])
                self.assertFalse(context["source_identity"]["exclusive_source_scope"])
                self.assertIn("lib/helper.cbl", {page["relative_path"] for page in context["pages"]})

    def test_auxiliary_search_cannot_bypass_missing_or_ambiguous_explicit_file(self):
        self.fixture()
        self.write("other/root.cbl", program("OTHER"))
        self.build()
        for question in ("Explain missing/root.cbl", "Explain root.cbl"):
            with self.subTest(question=question):
                context = retrieve_repository_context(self.database, self.source, question,
                                                      search_terms=["HELPER"], prior_paths=["lib/helper.cbl"])
                self.assertEqual(context["pages"], [])
                self.assertTrue(identity_blocks_analysis(context["source_identity"]))

    def test_selected_entry_forms_resolve_the_same_indexed_definition(self):
        self.fixture()
        for entry in ("ROOT", "root.cbl", "app/root.cbl", "./app/root.cbl", "app\\root.cbl",
                      "app/root.cbl::ROOT::2", "APP/ROOT.CBL::root::2"):
            with self.subTest(entry=entry):
                identity = self.identity("完整业务流程是什么？", entry=entry, search=["HELPER"])
                self.assertEqual(identity["status"], "resolved")
                self.assertEqual(identity["direct_paths"], ["app/root.cbl"])
                self.assertEqual(identity["selection_source"], "entry_program")
                mapping = build_business_map(self.database, self.source, "完整业务流程是什么？", entry_program=entry)
                self.assertEqual(mapping["direct_paths"], ["app/root.cbl"])

    def test_explicit_question_identity_takes_precedence_over_selected_entry(self):
        self.fixture()
        self.assertEqual(self.identity("Explain lib/helper.cbl", entry="ROOT")["direct_paths"], ["lib/helper.cbl"])
        self.assertEqual(self.identity("Explain lib/helper.cbl", entry="missing.cbl")["status"], "resolved")

    def test_filename_identity_survives_common_sentence_punctuation(self):
        self.fixture()
        for question in ("说明 root.cbl。", "说明 root.cbl！", "说明（root.cbl）。",
                         "说明【root.cbl】。", "说明 [root.cbl].", "Explain (root.cbl).",
                         "说明“root.cbl”。", "说明‘root.cbl’。", "说明 app/root.cbl。",
                         "说明（app/root.cbl）。", "说明“app/root.cbl”。"):
            with self.subTest(question=question):
                identity = self.identity(question)
                self.assertEqual(identity["status"], "resolved")
                self.assertEqual(identity["direct_paths"], ["app/root.cbl"])
                mapping = build_business_map(self.database, self.source, question)
                self.assertEqual(mapping["direct_paths"], ["app/root.cbl"])

    def test_invalid_entry_and_stale_entry_key_cannot_fall_back_to_another_program(self):
        self.fixture()
        for entry in ("missing.cbl", "missing/root.cbl", "app/root.cbl::OTHER::2",
                      "app/root.cbl::ROOT::3", "app/root.cbl::ROOT::0", "app/root.cbl::ROOT::invalid"):
            with self.subTest(entry=entry):
                identity = self.identity("完整业务流程是什么？", entry=entry)
                self.assertTrue(identity_blocks_analysis(identity))
                context = retrieve_repository_context(self.database, self.source, "完整业务流程是什么？",
                                                      entry_program=entry, search_terms=["HELPER"])
                self.assertEqual(context["pages"], [])

    def test_model_search_receives_callee_text_while_analysis_root_stays_fixed(self):
        self.fixture()
        requests = []
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            if len(requests) == 1:
                content = json.dumps({"search": ["UNIQUE-PROOF"]})
            else:
                helper = next(page for page in payload["source_context"][0]["pages"]
                              if page["relative_path"] == "lib/helper.cbl")
                content = f"被调程序显示固定文本。[{helper['evidence_id']}]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": content},
                                                                   "finish_reason": "stop"}]}))
        run_business_chat("Explain app/root.cbl workflow", self.database, self.source, self.config,
                          transport=transport, allow_network=False, framework_reference_path="",
                          policy=AgentPolicy(max_model_requests=2, max_answer_revisions=0))
        self.assertEqual(len(requests), 2)
        self.assertIn("lib/helper.cbl", {page["relative_path"] for page in requests[-1]["source_context"][0]["pages"]})
        self.assertTrue(all(payload["business_map"]["direct_paths"] == ["app/root.cbl"] for payload in requests))

    def index_selected(self, entry="first.cbl"):
        return analyze_source(self.source, self.output, index_mode="catalog", entry=entry,
                              analysis_mode="business", reading_strategy="retrieval", source_format="free",
                              framework_reference_path="", allow_network=False)

    def try_reuse(self, entry):
        return try_indexed_question(self.source.resolve(), self.output.resolve(), question="完整业务流程是什么？", entry=entry,
                                    extensions=DEFAULT_EXTENSIONS, include_extensionless=True,
                                    encoding="auto", source_format="free", config=self.config, api_options=None,
                                    transport=mock.Mock(side_effect=AssertionError("Wrong index must not call model")),
                                    framework_reference_path="", capture_api_responses=False, history=None,
                                    progress=None, check_cancel=None)

    def test_local_index_reuse_rejects_every_form_of_an_unindexed_selected_entry(self):
        self.write("first.cbl", program("FIRST"))
        self.write("sub/second.cbl", program("SECOND"))
        first = self.index_selected()
        self.assertEqual(first["scope"]["catalog_file_count"], 2)
        self.assertEqual(first["scope"]["selected_file_count"], 1)
        for entry in ("SECOND", "second.cbl", "sub/second.cbl", "./sub/second.cbl",
                      "sub\\second.cbl", "sub/second.cbl::SECOND::2"):
            with self.subTest(entry=entry):
                self.assertIsNone(self.try_reuse(entry))

    def test_local_index_does_not_hide_basename_ambiguity_in_catalog(self):
        self.write("one/common.cbl", program("FIRST"))
        self.write("two/common.cbl", program("SECOND"))
        self.index_selected("one/common.cbl")
        self.assertIsNone(self.try_reuse("common.cbl"))

    def test_covered_selected_entry_still_reuses_its_index(self):
        self.write("first.cbl", program("FIRST"))
        self.write("sub/second.cbl", program("SECOND"))
        self.index_selected()
        agent = {"runner_status": "COMPLETED", "agent_result": {"status": "ANSWERED", "answer": "已有索引。"}}
        for entry in ("FIRST", "first.cbl", "./first.cbl", "first.cbl::FIRST::2"):
            with self.subTest(entry=entry), mock.patch("business_chat.run_business_chat", return_value=agent) as run:
                result = self.try_reuse(entry)
                self.assertTrue(result["index_reused"])
                self.assertEqual(run.call_args.kwargs["entry_program"], entry)

    def test_switching_catalog_entry_rebuilds_needed_index_before_answering(self):
        self.write("first.cbl", program("FIRST", "*> billing process\nDISPLAY 'FIRST-RESULT'."))
        self.write("sub/second.cbl", program("SECOND", "*> billing process\nDISPLAY 'SECOND-RESULT'."))
        self.index_selected()
        requests = []
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            second = next(page for page in payload["source_context"][0]["pages"]
                          if page["relative_path"] == "sub/second.cbl")
            answer = f"所选程序显示固定文本。[{second['evidence_id']}]"
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": answer},
                                                                   "finish_reason": "stop"}]}))
        result = analyze_source(self.source, self.output, index_mode="catalog", entry="sub/second.cbl::SECOND::2",
                                question="billing 的完整业务流程是什么？", analysis_mode="business",
                                reading_strategy="retrieval", source_format="free", framework_reference_path="",
                                allow_network=True, config=self.config, transport=transport,
                                agent_policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))
        self.assertFalse(result.get("index_reused", False))
        self.assertEqual(result["selected_entry"]["relative_path"], "sub/second.cbl")
        self.assertEqual(len(requests), 1)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertIsNotNone(connection.execute("SELECT 1 FROM source_files WHERE relative_path='sub/second.cbl'").fetchone())
        self.assertEqual(requests[0]["business_map"]["direct_paths"], ["sub/second.cbl"])


if __name__ == "__main__":
    unittest.main()
