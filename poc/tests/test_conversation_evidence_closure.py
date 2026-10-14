"""Conversation evidence closure through the saved workbench index, offline."""

from contextlib import closing
import hashlib
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
from company_api import CompanyAPIConfig, TransportResponse
from conversation_store import ConversationStore, MODEL_HISTORY_DETAILS_BYTES
from source_reading import _identify_page
from source_session import read_archived_evidence


def report_source(name, reset=0):
    # A root larger than the excerpt allowance still fits the complete allowance.
    padding = "\n".join("*> neutral source padding " + "x" * 108 for _ in range(360))
    return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\n"
        "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
        "01 TRAN-AMT PIC S9(7)V99 VALUE 25.00.\n"
        "01 PAGE-SUM PIC S9(7)V99 VALUE 0.\n"
        "01 ACCOUNT-SUM PIC S9(7)V99 VALUE 0.\n"
        "01 GRAND-SUM PIC S9(7)V99 VALUE 0.\n"
        "01 REPORT-PAGE-SUM PIC S9(7)V99.\n"
        "01 REPORT-ACCOUNT-SUM PIC S9(7)V99.\n"
        "01 LINE-COUNT PIC 9(3) VALUE 20.\n"
        "01 PAGE-SIZE PIC 9(3) VALUE 20.\n"
        "01 CURR-CARD PIC 9(4) VALUE 1.\n"
        "01 TRAN-CARD PIC 9(4) VALUE 2.\n"
        "01 FIRST-ITEM PIC X VALUE 'N'.\n"
        "PROCEDURE DIVISION.\nMAIN.\n"
        "    PERFORM ADD-TRANSACTION.\n"
        "    IF CURR-CARD NOT = TRAN-CARD\n"
        "        IF FIRST-ITEM = 'N'\n"
        "            PERFORM ACCOUNT-TOTALS\n"
        "        END-IF\n"
        "        MOVE TRAN-CARD TO CURR-CARD\n"
        "    END-IF.\n    GOBACK.\n" + padding + "\n"
        "ADD-TRANSACTION.\n"
        "    ADD TRAN-AMT TO PAGE-SUM ACCOUNT-SUM.\n"
        "    IF LINE-COUNT >= PAGE-SIZE\n"
        "        PERFORM PAGE-TOTALS\n"
        "    END-IF.\n    EXIT.\n"
        "PAGE-TOTALS.\n"
        "    MOVE PAGE-SUM TO REPORT-PAGE-SUM.\n"
        "    ADD PAGE-SUM TO GRAND-SUM.\n"
        "    MOVE 0 TO PAGE-SUM.\n    EXIT.\n"
        "ACCOUNT-TOTALS.\n"
        "    MOVE ACCOUNT-SUM TO REPORT-ACCOUNT-SUM.\n"
        f"    MOVE {reset} TO ACCOUNT-SUM.\n    EXIT.\n")


QUESTIONS = (
    "REPORTPG 里第一种页汇总 PAGE-SUM 和第二种账户汇总 ACCOUNT-SUM，各自从哪里累加，什么时候清零？",
    "刚才第二种情况呢？换页也会让它归零吗？",
    "前面的金额明明累加过，为什么后来 ACCOUNT-SUM 变成零？这会把 GRAND-SUM 一起抹掉吗？",
)
CORE_SOURCE = (
    "01 PAGE-SIZE PIC 9(3) VALUE 20.",
    "IF CURR-CARD NOT = TRAN-CARD",
    "IF FIRST-ITEM = 'N'",
    "PERFORM ACCOUNT-TOTALS",
    "ADD TRAN-AMT TO PAGE-SUM ACCOUNT-SUM.",
    "IF LINE-COUNT >= PAGE-SIZE",
    "PERFORM PAGE-TOTALS",
    "MOVE PAGE-SUM TO REPORT-PAGE-SUM.",
    "ADD PAGE-SUM TO GRAND-SUM.",
    "MOVE 0 TO PAGE-SUM.",
    "MOVE ACCOUNT-SUM TO REPORT-ACCOUNT-SUM.",
    "MOVE 0 TO ACCOUNT-SUM.",
)


def pages_from(payload):
    return [page for bundle in payload["source_context"] for page in bundle.get("pages", [])]


class ConversationEvidenceClosureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source, self.output = self.root / "source", self.root / "workspace"
        self.source.mkdir()
        self.source.joinpath("report.cbl").write_text(report_source("REPORTPG"), encoding="utf-8")
        self.source.joinpath("other.cbl").write_text(report_source("OTHERPG", reset=9), encoding="utf-8")
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-conversation-model",
                                      api_key="offline-only")
        self.policy = AgentPolicy(max_model_requests=1, max_answer_revisions=0)
        for owner, method in ((socket, "create_connection"), (socket.socket, "connect"),
                              (socket.socket, "connect_ex")):
            patcher = mock.patch.object(owner, method, side_effect=AssertionError("Network forbidden"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.assertGreater(len(self.source.joinpath("report.cbl").read_text()), self.policy.max_source_characters)
        self.assertLess(len(self.source.joinpath("report.cbl").read_text()), self.policy.max_complete_source_characters)
        analyze_source(self.source, self.output, index_mode="catalog", analysis_mode="business",
                       source_format="free", verify_content=True, framework_reference_path="")

    def ask(self, question, history=(), *, policy=None, entry=None):
        captured = []
        def transport(request):
            envelope = json.loads(request.body)
            payload = json.loads(envelope["messages"][-1]["content"])
            captured.append((payload, envelope))
            self.assertNotIn("tools", envelope)
            self.assertNotIn("response_format", envelope)
            pages = pages_from(payload)
            direct = payload.get("business_map", {}).get("source_identity", {}).get("direct_paths", [])
            candidates = [page for page in pages if page["relative_path"] in direct] or pages
            if candidates:
                label = "当前字段是 ONLY-SUM" if any("01 ONLY-SUM" in page["source_text"] for page in candidates) \
                    else "第一种是 PAGE-SUM，第二种是 ACCOUNT-SUM"
                answer = (label + "；本轮原文已供应，"
                          f"按当前程序的控制条件继续解释。[{candidates[0]['evidence_id']}]")
            else:
                answer = "本轮尚未供应足够原文，保留具体证据缺口。"
            return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": answer},
                "finish_reason": "stop"}]}, ensure_ascii=False))
        report = analyze_source(self.source, self.output, index_mode="catalog", analysis_mode="business",
            reading_strategy="retrieval", source_format="free", framework_reference_path="",
            question=question, entry=entry, conversation_history=list(history), config=self.config,
            transport=transport, allow_network=True, agent_policy=policy or self.policy)
        self.assertTrue(report["index_reused"], report.get("reason_code"))
        self.last_agent_output = json.loads(self.output.joinpath("agent-result.json").read_text())
        result = self.last_agent_output["agent_result"]
        self.assertEqual(len(captured), 1)
        self.assertEqual(result["metrics"]["model_requests"], 1)
        return result, captured[0][0], captured[0][1]

    @staticmethod
    def append_turn(history, question, result):
        history.extend(({"role": "user", "content": question}, {"role": "assistant", "content": result["answer"],
            "evidence_refs": result["evidence_refs"],
            "cited_evidence_ids": [ref["evidence_id"] for ref in result["narrative"]["citations"]
                                   if ref.get("evidence_id")],
            "investigation_state": result["investigation_state"]}))

    @staticmethod
    def plan(payload, result):
        return ((payload.get("question_investigation") or {}).get("analysis_plan") or
                (result["investigation_state"].get("question_investigation") or {}).get("analysis_plan") or {})

    def assert_current_pages(self, payload, relative):
        pages = [page for page in pages_from(payload) if page["relative_path"] == relative]
        self.assertTrue(pages, relative)
        digest = hashlib.sha256(self.source.joinpath(relative).read_bytes()).hexdigest()
        lines = self.source.joinpath(relative).read_text().splitlines()
        for page in pages:
            self.assertEqual(page["source_sha256"], digest)
            self.assertEqual(page["evidence_id"], _identify_page(page))
            if not page.get("span_truncated"):
                self.assertEqual(page["source_text"], "\n".join(lines[page["start_line"] - 1:page["end_line"]]))
        return pages

    def assert_complete_root(self, payload, relative):
        roots = [bundle.get("working_set") for bundle in payload["source_context"] if bundle.get("working_set")]
        self.assertTrue(roots, "Resolved conversation root lost its complete-source eligibility")
        working = roots[0]
        self.assertEqual(working["root_paths"], [relative])
        self.assertIn(relative, working["supplied_complete_paths"])
        self.assertTrue(working["physical_complete"])
        pages = self.assert_current_pages(payload, relative)
        return pages

    def test_three_turn_source_closure_survives_omitted_program_and_cold_warm_reuse(self):
        cold_ids = None
        for cache in ("cold", "warm"):
            history, turns = [], []
            for question in QUESTIONS:
                result, payload, envelope = self.ask(question, history)
                turns.append((result, payload, envelope))
                self.append_turn(history, question, result)
            ids = []
            for index, (result, payload, envelope) in enumerate(turns):
                with self.subTest(cache=cache, turn=index + 1):
                    pages = self.assert_complete_root(payload, "report.cbl")
                    text = "\n".join(page["source_text"] for page in pages)
                    for statement in CORE_SOURCE:
                        self.assertIn(statement, text)
                    self.assertEqual(sum(message["role"] == "user" for message in envelope["messages"][1:-1]), index)
                    if index == 1:
                        entities = self.plan(payload, result).get("entities", [])
                        self.assertTrue(entities, "The ordinal reference has no locally bound field")
                        self.assertEqual(entities[0], "ACCOUNT-SUM")
                    ids.append(tuple(sorted(page["evidence_id"] for page in pages)))
            if cache == "cold":
                cold_ids = ids
            else:
                self.assertEqual(ids, cold_ids, "Cache reuse changed the versioned evidence occurrences")

    def test_explicit_program_switch_replaces_conversation_root(self):
        history = []
        first, _, _ = self.ask(QUESTIONS[0])
        self.append_turn(history, QUESTIONS[0], first)
        question = "改问 OTHERPG 的 ACCOUNT-SUM，它在哪个条件下被设为9？"
        second, payload, _ = self.ask(question, history)
        pages = self.assert_complete_root(payload, "other.cbl")
        self.assertIn("MOVE 9 TO ACCOUNT-SUM.", "\n".join(page["source_text"] for page in pages))
        self.append_turn(history, question, second)
        third, payload, _ = self.ask("刚才这个字段呢？换页会再覆盖它吗？", history)
        self.assert_complete_root(payload, "other.cbl")
        self.assertIn("other.cbl", self.plan(payload, third).get("paths", []))

    def test_persistent_selected_entry_preserves_three_turn_subject_and_ordinal(self):
        history = []
        for index, question in enumerate(QUESTIONS):
            result, payload, _ = self.ask(question, history, entry="REPORTPG")
            with self.subTest(turn=index + 1):
                self.assert_complete_root(payload, "report.cbl")
                if index == 1:
                    entities = self.plan(payload, result).get("entities", [])
                    self.assertTrue(entities)
                    self.assertEqual(entities[0], "ACCOUNT-SUM")
            self.append_turn(history, question, result)

    def test_large_stored_details_restore_subject_into_default_indexed_followup(self):
        first, _, _ = self.ask(QUESTIONS[0])
        subject = first["investigation_state"].get("session_subject")
        self.assertIsInstance(subject, dict)
        store = ConversationStore(self.output, self.source)
        identifier = store.create(first["snapshot_id"])["id"]
        store.append(identifier, "user", QUESTIONS[0])
        details = {"evidence_refs": first["evidence_refs"],
            "cited_evidence_ids": [ref["evidence_id"] for ref in first["narrative"]["citations"]],
            "investigation_state": first["investigation_state"],
            "large_diagnostic": "x" * (MODEL_HISTORY_DETAILS_BYTES * 3)}
        store.append(identifier, "assistant", first["answer"], details=details)
        history = store.model_history(identifier)
        self.assertEqual([message["role"] for message in history], ["user", "assistant"])
        self.assertNotIn("evidence_refs", history[-1], "Large diagnostics exceeded the ordinary detail allowance")
        self.assertEqual(history[-1]["investigation_state"]["session_subject"], subject)
        second, payload, _ = self.ask(QUESTIONS[1], history)
        self.assert_complete_root(payload, "report.cbl")
        entities = self.plan(payload, second).get("entities", [])
        self.assertTrue(entities)
        self.assertEqual(entities[0], "ACCOUNT-SUM")

    def test_unique_field_first_question_keeps_semantic_sidecar_then_resolves_implicit_followup(self):
        self.source.joinpath("unique.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. UNIQUEPG.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 BASE-AMOUNT PIC 9(5) VALUE 3.\n01 ONLY-SUM PIC 9(6).\n"
            "01 RESET-FLAG PIC X VALUE 'Y'.\nPROCEDURE DIVISION.\nMAIN.\n"
            "COMPUTE ONLY-SUM = BASE-AMOUNT * 2.\n"
            "IF RESET-FLAG = 'Y'\nMOVE 0 TO ONLY-SUM\nEND-IF.\nGOBACK.\n", encoding="utf-8")
        analyze_source(self.source, self.output, index_mode="catalog", analysis_mode="business",
                       source_format="free", verify_content=True, framework_reference_path="")
        question = "ONLY-SUM 怎么计算？"
        first, payload, _ = self.ask(question)
        scope = self.last_agent_output.get("semantic_scope")
        self.assertIsInstance(scope, dict, "A unique field question must retain targeted semantic analysis")
        self.assertIn("unique.cbl", [item["relative_path"] for item in scope["input_manifest"]])
        pages = self.assert_current_pages(payload, "unique.cbl")
        text = "\n".join(page["source_text"] for page in pages)
        self.assertIn("COMPUTE ONLY-SUM = BASE-AMOUNT * 2.", text)
        self.assertIn("MOVE 0 TO ONLY-SUM", text)
        history = []
        self.append_turn(history, question, first)
        second, payload, _ = self.ask("为什么最后变成零？", history)
        self.assert_complete_root(payload, "unique.cbl")
        self.assertIsInstance(self.last_agent_output.get("semantic_scope"), dict,
                              "Complete source on a field followup must retain semantic analysis")
        self.assertIn("ONLY-SUM", self.plan(payload, second).get("entities", []))

    def test_equal_length_source_change_refreshes_followup_and_preserves_archived_evidence(self):
        first, payload, _ = self.ask(QUESTIONS[0])
        old_page = next(page for page in self.assert_current_pages(payload, "report.cbl")
                        if "MOVE 0 TO ACCOUNT-SUM." in page["source_text"])
        history = []
        self.append_turn(history, QUESTIONS[0], first)
        path = self.source / "report.cbl"
        stat = path.stat()
        path.write_text(path.read_text().replace("MOVE 0 TO ACCOUNT-SUM.", "MOVE 7 TO ACCOUNT-SUM."), encoding="utf-8")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        _, payload, _ = self.ask(QUESTIONS[1], history)
        current = self.assert_complete_root(payload, "report.cbl")
        self.assertIn("MOVE 7 TO ACCOUNT-SUM.", "\n".join(page["source_text"] for page in current))
        self.assertNotIn(old_page["evidence_id"], {page["evidence_id"] for page in current})
        with closing(sqlite3.connect(self.output / "structural-index.sqlite")) as connection:
            indexed = connection.execute("SELECT sha256 FROM source_files WHERE relative_path='report.cbl'").fetchone()[0]
        self.assertEqual(indexed, hashlib.sha256(path.read_bytes()).hexdigest())
        archived = read_archived_evidence(self.output / "structural-index.sqlite", old_page["evidence_id"])
        self.assertIsNotNone(archived)
        self.assertEqual((archived["source_sha256"], archived["source_text"]),
                         (old_page["source_sha256"], old_page["source_text"]))

    def test_conversation_closure_keeps_declared_budget_and_reports_incomplete_supply(self):
        policy = AgentPolicy(max_model_requests=1, max_answer_revisions=0,
                             max_source_characters=1024, max_complete_source_characters=1024)
        history = []
        first, _, _ = self.ask(QUESTIONS[0], policy=policy)
        self.append_turn(history, QUESTIONS[0], first)
        _, payload, _ = self.ask(QUESTIONS[1], history, policy=policy)
        workings = [bundle["working_set"] for bundle in payload["source_context"] if bundle.get("working_set")]
        self.assertTrue(workings, "A known root budget gap must remain explicit on a followup")
        self.assertFalse(workings[0]["physical_complete"])
        self.assertNotIn("report.cbl", workings[0].get("supplied_complete_paths", []))
        self.assertIn("budget", json.dumps(workings[0]))
        self.assertLessEqual(sum(len(page["source_text"]) for page in pages_from(payload)), policy.max_source_characters)


if __name__ == "__main__":
    unittest.main()
