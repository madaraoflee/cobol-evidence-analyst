"""Language changes and journal upgrades preserve uncertain remote requests."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig
from repository_discovery import ensure_repository_search
from run_store import RunStore, fingerprint


class LanguageResumeSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "amount.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. AMOUNT-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 OUT-AMOUNT PIC 9(5).\n"
            "PROCEDURE DIVISION.\nMAIN.\nMOVE 4 TO OUT-AMOUNT.\nGOBACK.\n")
        self.database = self.root / "facts.sqlite"
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model", api_key="offline-only")
        self.question = "OUT-AMOUNT"
        self.question_id = "sha256:" + hashlib.sha256(self.question.encode()).hexdigest()
        self.model_fingerprint = hashlib.sha256(self.config.chat_model.encode()).hexdigest()
        self.requests = []

    def interrupted(self, locale="zh-CN"):
        def transport(request):
            self.requests.append(request)
            raise KeyboardInterrupt("simulated_process_loss")
        with self.assertRaises(KeyboardInterrupt):
            run_business_chat(self.question, self.database, self.source, self.config,
                              locale=locale, transport=transport)
        self.journal = RunStore(self.root / "investigation-runs.sqlite")
        return self.journal.unfinished(self.question_id, self.model_fingerprint)

    def retain_draft(self, saved):
        checkpoint = {**saved["checkpoint"], "result": {"runner_status": "COMPLETED", "agent_result": {
            "answer": "旧语言正文保持原样。", "answer_language": "zh-CN", "answer_available": True,
            "answer_origin": "model", "model_answer_recorded": True}}}
        self.journal.checkpoint(saved["run_id"], checkpoint, status="attention")
        return checkpoint

    def assert_pending_blocked(self, saved, **options):
        forbidden = mock.Mock(side_effect=AssertionError("Uncertain request must not be resent"))
        result = run_business_chat(self.question, self.database, self.source, self.config,
                                   locale="en", transport=forbidden, **options)
        forbidden.assert_not_called()
        self.assertEqual(result["reason_code"], "REMOTE_REQUEST_OUTCOME_UNKNOWN")
        answer = result["agent_result"]
        self.assertEqual(answer["run_id"], saved["run_id"])
        self.assertEqual(answer["answer_language"], "en")
        self.assertEqual(answer["answer_origin"], "diagnostic")
        self.assertFalse(answer["answer_available"])
        self.assertFalse(answer["resume_state"]["automatic_resend"])
        self.assertNotRegex(answer["answer"], r"[\u3400-\u9fff]")
        self.assertTrue(answer["resume_state"]["uncertain_requests"])
        self.assertEqual(len(self.requests), 1)

    def test_language_switch_blocks_automatic_and_explicit_resume_without_replaying_old_draft(self):
        saved = self.interrupted()
        checkpoint = self.retain_draft(saved)
        for options in ({}, {"resume_run_id": saved["run_id"]}):
            with self.subTest(options=options):
                self.assert_pending_blocked(saved, **options)
                self.assertEqual(self.journal.load(saved["run_id"])["checkpoint"], checkpoint)

    def test_legacy_identity_pending_request_blocks_after_upgrade(self):
        saved = self.interrupted()
        checkpoint = self.retain_draft(saved)
        legacy_identity = fingerprint({
            "database_path": str(self.database.resolve()), "source_root": str(self.source.resolve()),
            "entry_program": None, "history": [], "framework_reference_path": None,
            "answer_detail": "detailed", "conversation_id": None})
        metadata = {**saved["metadata"], "analysis_identity": legacy_identity, "prompt_version": "business-chat-v26"}
        with self.journal.connect() as db:
            db.execute("UPDATE investigation_runs SET metadata_json=? WHERE run_id=?",
                       (json.dumps(metadata), saved["run_id"]))
        for options in ({}, {"resume_run_id": saved["run_id"]}):
            with self.subTest(options=options):
                self.assert_pending_blocked(saved, **options)
                self.assertEqual(self.journal.load(saved["run_id"])["checkpoint"], checkpoint)

    def test_newer_safe_run_cannot_hide_an_older_pending_request(self):
        saved = self.interrupted(locale="zh-HK")
        self.journal.create("newer-safe-run", saved["metadata"])
        self.assert_pending_blocked(saved)

    def test_newer_same_language_run_cannot_hide_an_older_pending_request(self):
        saved = self.interrupted(locale="en")
        self.journal.create("newer-safe-run", saved["metadata"])
        self.assert_pending_blocked(saved)

    def test_confirmed_old_request_does_not_replay_another_language_answer(self):
        saved = self.interrupted()
        self.retain_draft(saved)
        for request in saved["uncertain_requests"]:
            self.journal.finish_request(saved["run_id"], request["request_id"], {"choices": []})
        with mock.patch("business_chat._run_business_chat", return_value={"fresh_answer": True}) as execute:
            result = run_business_chat(self.question, self.database, self.source, self.config, locale="en")
        self.assertEqual(result, {"fresh_answer": True})
        self.assertIsNone(execute.call_args.kwargs["resume_state"])
        self.assertEqual(execute.call_args.kwargs["answer_language"], "en")
        self.assertEqual(self.journal.load(saved["run_id"])["checkpoint"]["result"]["agent_result"]["answer"],
                         "旧语言正文保持原样。")

    def test_other_entry_keeps_its_own_investigation_scope(self):
        saved = self.interrupted()
        with mock.patch("business_chat._run_business_chat", return_value={"fresh_scope": True}) as execute:
            result = run_business_chat(self.question, self.database, self.source, self.config,
                                       locale="en", entry_program="OTHER-RULE")
        self.assertEqual(result, {"fresh_scope": True})
        self.assertIsNone(execute.call_args.kwargs["resume_state"])
        self.assertTrue(self.journal.load(saved["run_id"])["uncertain_requests"])


if __name__ == "__main__":
    unittest.main()
