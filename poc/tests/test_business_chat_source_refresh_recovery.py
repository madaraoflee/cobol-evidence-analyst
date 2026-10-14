"""Exercise question-local refreshes with real indexes and no network."""

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
from analyze_source import AnalysisCancelled
from business_chat import run_business_chat
from business_index import build_business_index
from business_map import build_business_map
from company_api import CompanyAPIConfig, TransportResponse
from complete_working_set import build_complete_working_set
from repository_discovery import ensure_repository_search
from source_session import QuestionSourceSession, refresh_selected_sources


def program(name, value, tail=""):
    return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 RESULT-AMOUNT PIC 9(5).\n"
            f"PROCEDURE DIVISION.\nMAIN.\nMOVE {value} TO RESULT-AMOUNT.\n{tail}GOBACK.\n")


class SourceRefreshRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
                                       api_key="offline-only")
        for owner, name in ((socket, "create_connection"), (socket.socket, "connect"),
                            (socket.socket, "connect_ex")):
            patcher = mock.patch.object(owner, name, side_effect=AssertionError("Network forbidden"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self, files):
        for relative, text in files.items():
            (self.source / relative).write_text(text, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free",
                             framework_reference_path="", quiet=True)
        ensure_repository_search(self.database, self.source)

    def change_value(self, relative, before, after):
        path = self.source / relative
        stat = path.stat()
        text = path.read_text().replace(f"MOVE {before} TO", f"MOVE {after} TO")
        path.write_text(text, encoding="utf-8")
        # Stat-only checks must not hide an equal-length change.
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def ask(self, callback, *, question="说明 entry.cbl 的业务流程。", check_cancel=None):
        requests = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            answer = callback(payload, len(requests))
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": answer},
                "finish_reason": "stop"}]}, ensure_ascii=False))

        result = run_business_chat(question, self.database, self.source,
            self.config, transport=transport, framework_reference_path="", check_cancel=check_cancel)["agent_result"]
        return result, requests

    def test_complete_working_set_exposes_dependency_hash_mismatch_for_recovery(self):
        self.build({"entry.cbl": program("ENTRY", 1, "CALL 'DETAIL'.\n"),
                    "detail.cbl": program("DETAIL", 2)})
        digest = self.change_value("detail.cbl", 2, 7)
        mapping = build_business_map(self.database, self.source, "说明 entry.cbl 的流程。")
        with QuestionSourceSession(self.database, self.source) as session:
            context = build_complete_working_set(self.database, session, mapping, AgentPolicy())
        self.assertTrue(context["needs_refresh"], (mapping.get("source_identity"), context))
        self.assertEqual(context["pages"], [])
        self.assertEqual(context["boundaries"][0]["relative_path"], "detail.cbl")
        self.assertEqual(context["boundaries"][0]["reason_code"], "SOURCE_HASH_MISMATCH")
        self.assertEqual(context["boundaries"][0]["captured_sha256"], digest)

    def test_complete_chain_refreshes_more_than_one_batch_before_answering(self):
        files = {"entry.cbl": program("ENTRY", 1, "CALL 'STEP-A'.\n")}
        for index, name in enumerate(("STEP-A", "STEP-B", "STEP-C", "STEP-D")):
            tail = f"CALL 'STEP-{chr(ord('B') + index)}'.\n" if index < 3 else ""
            files[name.lower() + ".cbl"] = program(name, 1, tail)
        self.build(files)
        digests = {path: self.change_value(path, 1, 7) for path in files}

        def reply(payload, turn):
            pages = payload["source_context"][0]["pages"]
            self.assertEqual({p["relative_path"] for p in pages}, set(files))
            for page in pages:
                self.assertEqual(page["source_sha256"], digests[page["relative_path"]])
                self.assertIn("MOVE 7 TO", page["source_text"])
                self.assertNotIn("MOVE 1 TO", page["source_text"])
            self.assertTrue(payload["source_context"][0]["working_set"]["physical_complete"])
            return "各程序把结果金额设为 7。" + "".join(f"[{p['evidence_id']}]" for p in pages)

        with mock.patch("source_session.refresh_selected_sources", wraps=refresh_selected_sources) as refresh:
            result, requests = self.ask(reply)
        self.assertGreaterEqual(refresh.call_count, 2)
        self.assertTrue(requests)
        self.assertNotEqual(result["status"], "ABSTAINED")
        with sqlite3.connect(self.database) as database:
            indexed = dict(database.execute("SELECT relative_path,sha256 FROM source_files"))
        self.assertEqual(indexed, digests)

    def test_later_explicit_read_refreshes_after_the_root_already_refreshed(self):
        self.build({"entry.cbl": program("ENTRY", 1), "detail.cbl": program("DETAIL", 2)})
        root_digest = self.change_value("entry.cbl", 1, 7)
        detail_digest = self.change_value("detail.cbl", 2, 8)

        def reply(payload, turn):
            if turn == 1:
                return json.dumps({"read": [{"relative_path": "detail.cbl", "start_line": 1, "end_line": 10}]})
            pages = payload["source_context"][0]["pages"]
            detail = next(p for p in pages if p["relative_path"] == "detail.cbl")
            self.assertEqual(detail["source_sha256"], detail_digest)
            self.assertIn("MOVE 8 TO", detail["source_text"])
            entry = next(p for p in pages if p["relative_path"] == "entry.cbl")
            self.assertEqual(entry["source_sha256"], root_digest)
            return f"入口把结果金额设为 7。[{entry['evidence_id']}]"

        with mock.patch("source_session.refresh_selected_sources", wraps=refresh_selected_sources) as refresh:
            result, requests = self.ask(reply)
        self.assertGreaterEqual(refresh.call_count, 2)
        self.assertGreaterEqual(len(requests), 2)
        self.assertNotEqual(result["status"], "ABSTAINED")

    def test_failed_refresh_is_attempted_once_and_preserves_the_gap(self):
        self.build({"entry.cbl": program("ENTRY", 1)})
        self.change_value("entry.cbl", 1, 7)
        with mock.patch("source_session.refresh_selected_sources", side_effect=ValueError("SNAPSHOT_CHANGED")) as refresh:
            result, requests = self.ask(lambda payload, turn: "当前索引版本尚未更新，无法确认这次修改。")
        self.assertEqual(refresh.call_count, 1)
        self.assertTrue(any(item.get("reason") == "source_refresh_unavailable" for item in result["boundaries"]))
        for payload in requests:
            self.assertFalse(payload["source_context"][0]["pages"])

    def test_only_changed_dependency_refreshes_without_changing_root_version(self):
        self.build({"entry.cbl": program("ENTRY", 1, "CALL 'DETAIL'.\n"),
                    "detail.cbl": program("DETAIL", 2)})
        root_digest = hashlib.sha256((self.source / "entry.cbl").read_bytes()).hexdigest()
        detail_digest = self.change_value("detail.cbl", 2, 8)

        def reply(payload, turn):
            pages = {page["relative_path"]: page for page in payload["source_context"][0]["pages"]}
            self.assertEqual(pages["entry.cbl"]["source_sha256"], root_digest)
            self.assertEqual(pages["detail.cbl"]["source_sha256"], detail_digest)
            self.assertIn("MOVE 8 TO", pages["detail.cbl"]["source_text"])
            return f"入口把结果金额设为 1。[{pages['entry.cbl']['evidence_id']}]"

        with mock.patch("source_session.refresh_selected_sources", wraps=refresh_selected_sources) as refresh:
            self.ask(reply)
        self.assertEqual(refresh.call_count, 1)
        self.assertEqual([capture.relative_path for capture in refresh.call_args.args[1]], ["detail.cbl"])

    def test_exclusive_scope_rejects_outside_citation_before_capture_or_refresh(self):
        self.build({"entry.cbl": program("ENTRY", 1), "detail.cbl": program("DETAIL", 2)})
        with sqlite3.connect(self.database) as database:
            identifier = database.execute("SELECT evidence_id FROM evidence_spans WHERE relative_path=? "
                "AND evidence_id LIKE 'ev_page_%' LIMIT 1", ("detail.cbl",)).fetchone()[0]
            original_digest = database.execute("SELECT sha256 FROM source_files WHERE relative_path=?",
                                                ("detail.cbl",)).fetchone()[0]
        self.change_value("detail.cbl", 2, 8)
        captured = []
        original_capture = QuestionSourceSession.capture

        def capture(session, relative_path):
            captured.append(relative_path)
            return original_capture(session, relative_path)

        def reply(payload, turn):
            self.assertTrue(payload["business_map"]["source_identity"]["exclusive_source_scope"])
            self.assertEqual({page["relative_path"] for page in payload["source_context"][0]["pages"]}, {"entry.cbl"})
            if turn == 1:
                return json.dumps({"read": [{"relative_path": "entry.cbl", "evidence_id": identifier}]})
            page = payload["source_context"][0]["pages"][0]
            return f"入口把结果金额设为 1。[{page['evidence_id']}]"

        with mock.patch.object(QuestionSourceSession, "capture", capture), mock.patch(
                "source_session.refresh_selected_sources", wraps=refresh_selected_sources) as refresh:
            result, _ = self.ask(reply, question="仅分析 entry.cbl，不读取其他文件。")
        self.assertNotIn("detail.cbl", captured)
        self.assertEqual(refresh.call_count, 0)
        self.assertTrue(any(action.get("outcome") == "unavailable" for action in
                            result["investigation_state"]["completed_actions"]))
        with sqlite3.connect(self.database) as database:
            self.assertEqual(database.execute("SELECT sha256 FROM source_files WHERE relative_path=?",
                                             ("detail.cbl",)).fetchone()[0], original_digest)

    def test_exclusive_scope_uses_citation_target_and_unknown_id_fails_normally(self):
        self.build({"entry.cbl": program("ENTRY", 1), "detail.cbl": program("DETAIL", 2)})
        with sqlite3.connect(self.database) as database:
            root_id = database.execute("SELECT evidence_id FROM evidence_spans WHERE relative_path=? "
                "AND evidence_id LIKE 'ev_page_%' LIMIT 1", ("entry.cbl",)).fetchone()[0]
        for identifier, unavailable in ((root_id, False), ("ev_page_unknown", True)):
            with self.subTest(identifier=identifier):
                def reply(payload, turn):
                    if turn == 1:
                        return json.dumps({"read": [{"relative_path": "detail.cbl", "evidence_id": identifier}]})
                    self.assertEqual({page["relative_path"] for page in payload["source_context"][0]["pages"]}, {"entry.cbl"})
                    page = payload["source_context"][0]["pages"][0]
                    return f"入口把结果金额设为 1。[{page['evidence_id']}]"

                result, _ = self.ask(reply, question="仅分析 entry.cbl，不读取其他文件。")
                reads = [action for action in result["investigation_state"]["completed_actions"] if "read" in action]
                self.assertEqual(any(action.get("outcome") == "unavailable" for action in reads), unavailable)

    def test_cancellation_before_refresh_propagates_without_index_mutation(self):
        self.build({"entry.cbl": program("ENTRY", 1)})
        with sqlite3.connect(self.database) as database:
            original_digest = database.execute("SELECT sha256 FROM source_files WHERE relative_path=?",
                                                ("entry.cbl",)).fetchone()[0]
        self.change_value("entry.cbl", 1, 7)
        cancelled = False
        original_capture = QuestionSourceSession.capture

        def capture(session, relative_path):
            nonlocal cancelled
            result = original_capture(session, relative_path)
            cancelled = True
            return result

        def check_cancel():
            if cancelled:
                raise AnalysisCancelled("Cancelled before source refresh")

        with mock.patch.object(QuestionSourceSession, "capture", capture), mock.patch(
                "source_session.refresh_selected_sources", wraps=refresh_selected_sources) as refresh:
            with self.assertRaises(AnalysisCancelled):
                self.ask(lambda payload, turn: self.fail("Model must not be called after cancellation"),
                         check_cancel=check_cancel)
        self.assertEqual(refresh.call_count, 0)
        with sqlite3.connect(self.database) as database:
            self.assertEqual(database.execute("SELECT sha256 FROM source_files WHERE relative_path=?",
                                             ("entry.cbl",)).fetchone()[0], original_digest)


if __name__ == "__main__":
    unittest.main()
