"""Explicit language changes revise saved drafts within their original scope."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
import business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search
from run_store import RunStore


class ContinuationLanguageChangeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "decision.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. DECISION.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 RESULT-CODE PIC 99.\n"
            "PROCEDURE DIVISION.\nMAIN.\nMOVE 12 TO RESULT-CODE.\nGOBACK.\n"
            + "".join(f"*> Result context note {index}.\n" for index in range(400)), encoding="utf-8")
        self.database = self.root / "index.sqlite"
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.store = RunStore(self.root / "investigation-runs.sqlite")
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only")
        self.question = "DECISION 返回什么？"
        self.chinese = "请用中文回答。用一个段落说明结果。"
        self.english = "Always answer in English. Use one paragraph to explain the result."
        self.requests = []

    @staticmethod
    def reply(text, finish="stop"):
        return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text},
                                                               "finish_reason": finish}]}))

    @staticmethod
    def answer(payload, language):
        page = next(row for bundle in payload["source_context"] for row in bundle["pages"]
                    if row["relative_path"] == "decision.cbl")
        prose = ("The program sets the result code to 12 and returns to the caller." if language == "en" else
                 "程序把结果码设置为12，然后返回调用方。")
        return prose + "[" + page["evidence_id"] + "]"

    def invoke(self, instructions, *, finish="stop", fail=False, request_bytes=230000,
               conversation="language-change", question=None, source=None, **options):
        def transport(request):
            envelope = json.loads(request.body)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.requests.append((envelope, payload))
            if fail:
                return TransportResponse(401, json.dumps({"error": {"code": "invalid_api_key"}}))
            return self.reply(self.answer(payload, payload["answer_language"]), finish)
        return business_chat.run_business_chat(question or self.question, self.database, source or self.source,
            self.config, framework_reference_path="", answer_instructions=instructions, transport=transport,
            conversation_id=conversation,
            policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0, max_request_bytes=request_bytes),
            **options)

    def test_changed_language_revises_complete_or_truncated_draft_without_mutating_old_run(self):
        for finish in ("stop", "length"):
            for without_result in (False, True):
                for request_bytes in (230000, 32768):
                    with self.subTest(finish=finish, without_result=without_result, request_bytes=request_bytes):
                        conversation = f"change-{finish}-{without_result}-{request_bytes}"
                        prior = self.invoke(self.chinese, finish=finish, request_bytes=request_bytes, conversation=conversation)
                        old_id = prior["agent_result"]["run_id"]
                        if without_result:
                            checkpoint = deepcopy(self.store.load(old_id)["checkpoint"])
                            checkpoint.pop("result", None)
                            self.store.checkpoint(old_id, checkpoint, status="attention")
                        saved = deepcopy(self.store.load(old_id))
                        before = len(self.requests)
                        result = self.invoke(self.english, continue_investigation=True, resume_run_id=old_id,
                            request_bytes=request_bytes, conversation=conversation)["agent_result"]
                        self.assertEqual(len(self.requests) - before, 1)
                        envelope, payload = self.requests[-1]
                        self.assertEqual(payload["answer_language"], "en")
                        self.assertFalse(payload.get("draft_continuation"))
                        self.assertEqual(payload["draft_answer"], prior["agent_result"]["answer"])
                        self.assertIn("完整修订", payload["task"])
                        self.assertEqual(json.dumps(envelope, ensure_ascii=False).count(payload["draft_answer"]), 1)
                        self.assertTrue(any(bundle.get("pages") for bundle in payload["source_context"]))
                        self.assertTrue(result["answer_available"])
                        self.assertEqual(result["answer_language"], "en")
                        self.assertNotRegex(result["answer"], r"[\u3400-\u9fff]")
                        self.assertNotEqual(result["run_id"], old_id)
                        self.assertEqual(self.store.load(old_id), saved)
                        # The next continuation must retain the correctly
                        # bound source memory after the presentation change.
                        again = self.invoke(self.english, continue_investigation=True, resume_run_id=result["run_id"],
                            request_bytes=request_bytes, conversation=conversation)["agent_result"]
                        self.assertTrue(again["answer_available"])
                        self.assertEqual(again["answer_language"], "en")
                        self.assertNotRegex(again["answer"], r"[\u3400-\u9fff]")

    def test_changed_language_failure_does_not_present_old_draft_as_current_answer(self):
        for changed in (False, True):
            with self.subTest(changed=changed):
                conversation = f"failure-{changed}"
                prior = self.invoke(self.chinese, conversation=conversation)
                old_id = prior["agent_result"]["run_id"]
                saved = deepcopy(self.store.load(old_id))
                result = self.invoke(self.english if changed else self.chinese,
                    continue_investigation=True, resume_run_id=old_id, fail=True, conversation=conversation)["agent_result"]
                self.assertEqual(self.store.load(old_id), saved)
                if changed:
                    self.assertFalse(result["answer_available"])
                    self.assertFalse(result["model_answer_recorded"])
                    self.assertEqual(result["answer_origin"], "diagnostic")
                    self.assertEqual(result["answer_language"], "en")
                    self.assertNotRegex(result["answer"], r"[\u3400-\u9fff]")
                else:
                    self.assertTrue(result["answer_available"])
                    self.assertEqual(result["answer"], prior["agent_result"]["answer"])

    def test_unknown_remote_outcome_never_resends_or_replays_another_language(self):
        prior = self.invoke(self.chinese)
        old_id = prior["agent_result"]["run_id"]
        self.store.start_request(old_id, "pending-review", "request-digest")
        saved = deepcopy(self.store.load(old_id))
        before = len(self.requests)
        result = self.invoke(self.english, continue_investigation=True, resume_run_id=old_id)
        self.assertEqual(result["reason_code"], "REMOTE_REQUEST_OUTCOME_UNKNOWN")
        self.assertFalse(result["agent_result"]["answer_available"])
        self.assertFalse(result["agent_result"]["resume_state"]["automatic_resend"])
        self.assertEqual(len(self.requests), before)
        self.assertEqual(self.store.load(old_id), saved)

    def test_explicit_question_language_keeps_same_language_length_continuation(self):
        question = "请用中文回答：DECISION 返回什么？"
        prior = self.invoke(self.chinese, question=question, finish="length")
        old_id = prior["agent_result"]["run_id"]
        result = self.invoke(self.english, question=question,
            continue_investigation=True, resume_run_id=old_id)["agent_result"]
        payload = self.requests[-1][1]
        self.assertTrue(payload["draft_continuation"])
        self.assertEqual(payload["answer_language"], "zh-CN")
        self.assertTrue(result["answer_available"])
        self.assertEqual(result["answer_language"], "zh-CN")

    def test_entry_or_source_root_change_is_still_rejected(self):
        prior = self.invoke(self.chinese)
        old_id = prior["agent_result"]["run_id"]
        for options in ({"entry_program": "DECISION"}, {"source": self.root / "other-source"}):
            with self.subTest(options=options):
                before = len(self.requests)
                result = self.invoke(self.english, continue_investigation=True, resume_run_id=old_id, **options)
                self.assertEqual(result["reason_code"], "RESUME_IDENTITY_MISMATCH")
                self.assertEqual(len(self.requests), before)


if __name__ == "__main__":
    unittest.main()
