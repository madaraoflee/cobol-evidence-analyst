"""A saved provider diagnostic must not hide earlier valid analysis memory."""

from dataclasses import replace
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from business_chat import run_business_chat
from company_api import TransportResponse
from conversation_analysis_handoff import _latest_owned_handle, followup_analysis_seed
from run_store import RunStore
from source_session import QuestionSourceSession
from web_app import WorkbenchState
import test_conversation_analysis_handoff as fixtures


class WorkbenchFailedHandoffTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.ConversationAnalysisHandoffTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.fixture = self.case.fixture
        self.first = self.case.prior_analysis()
        self.app = WorkbenchState(state_path=self.fixture.root / "settings.json",
                                  config_provider=lambda: self.fixture.config)
        self.app.conversation_store = self.case.store
        self.app.conversation = self.case.store.get(self.case.conversation["id"])
        self.jobs = 0

    def history(self, question):
        return self.case.store.model_history(self.case.conversation["id"], max_characters=80,
            question=question, include_analysis_handoff=True)

    def begin(self, question, history=None):
        self.jobs += 1
        job = f"business-chat-{self.jobs}"
        options = {"conversation_id": self.case.conversation["id"], "question": question,
                   "conversation_history": self.history(question) if history is None else history}
        self.case.store.append(self.case.conversation["id"], "user", question, run_id=job, status="pending")
        self.app.conversation = self.case.store.get(self.case.conversation["id"])
        return job, options

    def save_http_failure(self, question="root.cbl 前面抵扣还有什么适用条件？"):
        job, options = self.begin(question)
        calls = []

        def transport(request):
            calls.append(request)
            return TransportResponse(500, "{}")

        result = run_business_chat(question, self.fixture.database, self.fixture.source, self.fixture.config,
            framework_reference_path="", transport=transport, history=options["conversation_history"],
            conversation_id=self.case.conversation["id"],
            policy=replace(self.case.case.policy, max_model_requests=1))
        self.assertEqual(len(calls), 1)
        self.assertFalse(result["agent_result"]["answer_available"])
        self.assertEqual(result["agent_result"]["answer_origin"], "diagnostic")
        self.app._record_answer(options, job, {"agent": result})
        messages = self.case.store.get(self.case.conversation["id"])["messages"]
        self.assertEqual([row["status"] for row in messages[-2:]], ["failed", "failed"])
        return result["agent_result"]

    def save_diagnostic(self, question, *, reason="MODEL_HTTP_ERROR", investigation_run_id=None):
        job, options = self.begin(question)
        self.app._record_answer(options, job, {"agent": {"runner_status": "NOT_READY", "reason_code": reason,
            "agent_result": {"status": "PARTIAL", "answer_available": False, "answer_origin": "diagnostic",
                             "answer": "本次没有取得可用回答。", "run_id": investigation_run_id}}})
        self.assertEqual(self.case.store.get(self.case.conversation["id"])["messages"][-1]["status"], "failed")

    def ordinary_question(self, question, *, history=None, finish_reason="stop"):
        job, options = self.begin(question, history)
        self.fixture.question = question
        payloads = []

        def respond(payload, _):
            payloads.append(payload)
            historical = payload.get("analysis_ledger", {}).get("historical_source_references", {})
            answer = self.fixture.cited_answer(payload)
            if self.case.case.early_id in historical:
                answer += f"早先抵扣仍须存在应收余额。[{self.case.case.early_id}]"
            return answer, finish_reason

        result = self.fixture.ask(respond, policy=replace(self.case.case.policy, max_model_requests=1),
            request_guard=len(self.fixture.requests) + 1, history=options["conversation_history"],
            conversation_id=self.case.conversation["id"])
        self.assertTrue(result["answer_available"], result.get("errors"))
        self.assertEqual(len(payloads), 1)
        self.app._record_answer(options, job, {"agent": {"runner_status": "COMPLETED", "agent_result": result}})
        return result, payloads[0], self.case.case.saved(result)

    def test_actual_http500_diagnostic_does_not_hide_two_generations_from_next_question(self):
        before = self.case.old
        self.save_http_failure()
        question = "root.cbl 前面的条件如何共同影响最终金额？"
        history = self.history(question)
        latest = next(row for row in reversed(history) if row["role"] == "assistant")
        self.assertEqual(latest["investigation_run_id"], self.first["run_id"])
        result, payload, saved = self.ordinary_question(question)
        self.assertEqual(payload.get("analysis_ledger", {}).get("generation"), 2)
        self.assertTrue(any("尚有应收余额" in row["text"] for row in payload["analysis_ledger"]["entries"]))
        self.assertIn(self.case.case.early_id, payload["analysis_ledger"]["historical_source_references"])
        self.assertEqual(saved["checkpoint"]["conversation_analysis_handoff"]["from_run_id"], self.first["run_id"])
        self.assertIn(self.case.case.early_id, {row["evidence_id"] for row in result["narrative"]["citations"]})
        self.assertEqual(self.case.case.saved(self.first), before)

    def test_latest_available_partial_remains_authoritative_and_older_forged_handle_is_rejected(self):
        second, _, _ = self.ordinary_question("root.cbl 抵扣条件还要注意哪些限制？", finish_reason="length")
        latest = self.case.store.get(self.case.conversation["id"])["messages"][-1]
        self.assertEqual(latest["status"], "partial")
        self.assertTrue(latest["answer_available"])
        self.save_http_failure()
        question = "root.cbl 这些限制怎样影响后续处理？"
        history = self.history(question)
        latest = next(row for row in reversed(history) if row["role"] == "assistant")
        self.assertEqual(latest["investigation_run_id"], second["run_id"])
        third, payload, saved = self.ordinary_question(question)
        self.assertEqual(payload.get("analysis_ledger", {}).get("generation"), 2)
        self.assertEqual(saved["checkpoint"]["conversation_analysis_handoff"]["from_run_id"], second["run_id"])
        self.assertEqual(_latest_owned_handle(self.fixture.database, self.fixture.source,
                         self.case.conversation["id"]), third["run_id"])
        forged_question = "root.cbl 的抵扣需要什么前提？"
        forged = self.history(forged_question)
        next(row for row in reversed(forged) if row["role"] == "assistant")["investigation_run_id"] = self.first["run_id"]
        _, fresh_payload, fresh_saved = self.ordinary_question(forged_question, history=forged)
        self.assertFalse(fresh_payload.get("analysis_ledger", {}).get("generation"))
        self.assertIsNone(fresh_saved["checkpoint"].get("conversation_analysis_handoff"))

    def test_failed_reset_cannot_carry_previous_topic_into_a_deictic_question(self):
        self.save_diagnostic("换个问题，root.cbl 怎样处理？")
        _, payload, saved = self.ordinary_question("这个流程的适用条件还有什么？")
        self.assertFalse(payload.get("analysis_ledger", {}).get("generation"))
        self.assertIsNone(saved["checkpoint"].get("conversation_analysis_handoff"))

    def test_failed_deictic_question_inherits_only_with_its_verified_same_object_lineage(self):
        failed = self.save_http_failure("这个流程的抵扣还需要满足哪些条件？")
        failed_checkpoint = self.case.case.saved(failed)["checkpoint"]
        self.assertEqual(failed_checkpoint["conversation_analysis_handoff"]["from_run_id"], self.first["run_id"])
        _, payload, saved = self.ordinary_question("继续解释这个流程的前提条件。")
        self.assertEqual(payload.get("analysis_ledger", {}).get("generation"), 2)
        self.assertIn(self.case.case.early_id, payload["analysis_ledger"]["historical_source_references"])
        self.assertEqual(saved["checkpoint"]["conversation_analysis_handoff"]["from_run_id"], self.first["run_id"])

    def test_failed_deictic_question_without_lineage_does_not_guess_an_old_subject(self):
        self.save_diagnostic("这个流程的适用条件还有什么？")
        _, payload, saved = self.ordinary_question("继续解释这个流程的前提条件。")
        self.assertFalse(payload.get("analysis_ledger", {}).get("generation"))
        self.assertIsNone(saved["checkpoint"].get("conversation_analysis_handoff"))

    def test_missing_or_malformed_failed_lineage_is_rejected_without_an_exception(self):
        failed = self.save_http_failure("这个流程的抵扣还需要满足哪些条件？")
        saved = self.case.case.saved(failed)
        store = RunStore(self.fixture.root / "investigation-runs.sqlite")
        question = "继续解释这个流程的前提条件。"
        history = self.history(question)
        mutations = [("lineage", None), ("lineage", "unbound"), ("lineage", []),
                     ("subject", ["invalid"]), ("scope", None), ("scope", "invalid")]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                checkpoint = deepcopy(saved["checkpoint"])
                if field == "lineage":
                    checkpoint["conversation_analysis_handoff"] = value
                elif field == "subject":
                    checkpoint["session_subject"] = value
                else:
                    checkpoint["session_subject"]["scope"] = value
                store.checkpoint(failed["run_id"], checkpoint, status=saved["state"])
                with QuestionSourceSession(self.fixture.database, self.fixture.source) as session:
                    seed = followup_analysis_seed(question, database_path=self.fixture.database,
                        source_root=self.fixture.source, session=session, history=history,
                        conversation_id=self.case.conversation["id"], store=store, framework_reference_path="")
                self.assertIsNone(seed)
        checkpoint = deepcopy(saved["checkpoint"])
        checkpoint["conversation_analysis_handoff"] = None
        store.checkpoint(failed["run_id"], checkpoint, status=saved["state"])
        _, payload, fresh = self.ordinary_question(question)
        self.assertFalse(payload.get("analysis_ledger", {}).get("generation"))
        self.assertIsNone(fresh["checkpoint"].get("conversation_analysis_handoff"))

    def test_failed_question_cannot_borrow_a_different_failed_turns_lineage(self):
        failed = self.save_http_failure("这个流程的抵扣还需要满足哪些条件？")
        self.save_diagnostic("继续解释这些条件如何影响后续处理。", investigation_run_id=failed["run_id"])
        _, payload, saved = self.ordinary_question("继续解释这个流程的前提条件。")
        self.assertFalse(payload.get("analysis_ledger", {}).get("generation"))
        self.assertIsNone(saved["checkpoint"].get("conversation_analysis_handoff"))

    def test_failed_other_object_cannot_carry_previous_topic_into_a_deictic_question(self):
        self.save_diagnostic("helper-3.cbl 的处理规则是什么？")
        _, payload, saved = self.ordinary_question("这个流程的适用条件还有什么？")
        self.assertFalse(payload.get("analysis_ledger", {}).get("generation"))
        self.assertIsNone(saved["checkpoint"].get("conversation_analysis_handoff"))

    def test_skipped_diagnostic_with_unknown_remote_outcome_blocks_inheritance(self):
        self.save_diagnostic("root.cbl 的处理规则是什么？", reason="REMOTE_REQUEST_OUTCOME_UNKNOWN")
        _, payload, saved = self.ordinary_question("root.cbl 前面条件还需要注意什么？")
        self.assertFalse(payload.get("analysis_ledger", {}).get("generation"))
        self.assertIsNone(saved["checkpoint"].get("conversation_analysis_handoff"))

    def test_unknown_outcome_on_retained_partial_answer_blocks_its_old_handle(self):
        second, _, _ = self.ordinary_question("root.cbl 抵扣条件还有哪些限制？", finish_reason="length")
        user, answer = self.case.store.get(self.case.conversation["id"])["messages"][-2:]
        job = "uncertain-continuation"
        self.case.store.claim_retry(self.case.conversation["id"], user["id"], job)
        self.app.conversation = self.case.store.get(self.case.conversation["id"])
        self.app._record_answer({"conversation_id": self.case.conversation["id"], "question": user["content"],
            "retry_message_id": user["id"], "continue_message_id": user["id"],
            "retry_assistant_id": answer["id"], "resume_run_id": second["run_id"]}, job,
            {"agent": {"runner_status": "NOT_READY", "reason_code": "REMOTE_REQUEST_OUTCOME_UNKNOWN",
                       "agent_result": {"status": "PARTIAL", "answer_available": False, "answer_origin": "diagnostic"}}})
        retained = self.case.store.get(self.case.conversation["id"])["messages"][-1]
        self.assertEqual(retained["status"], "partial")
        self.assertEqual(retained["investigation_run_id"], second["run_id"])
        self.assertEqual(retained["retry_blocked_reason"], "REMOTE_REQUEST_OUTCOME_UNKNOWN")
        self.assertFalse(self.case.case.saved(second)["uncertain_requests"], "The UI marker must independently block the retained old run")
        _, payload, saved = self.ordinary_question("root.cbl 前面条件怎样影响后续处理？")
        self.assertFalse(payload.get("analysis_ledger", {}).get("generation"))
        self.assertIsNone(saved["checkpoint"].get("conversation_analysis_handoff"))

    def test_latest_eligible_answer_without_handle_does_not_fall_back_to_older_memory(self):
        job, options = self.begin("root.cbl 有哪些适用前提？")
        self.app._record_answer(options, job, {"agent": {"runner_status": "COMPLETED",
            "agent_result": {**self.first, "run_id": None}}})
        latest = self.case.store.get(self.case.conversation["id"])["messages"][-1]
        self.assertEqual(latest["status"], "completed")
        self.assertIsNone(latest.get("investigation_run_id"))
        self.assertIsNone(_latest_owned_handle(self.fixture.database, self.fixture.source, self.case.conversation["id"]))
        _, payload, saved = self.ordinary_question("root.cbl 这些前提还需要注意什么？")
        self.assertFalse(payload.get("analysis_ledger", {}).get("generation"))
        self.assertIsNone(saved["checkpoint"].get("conversation_analysis_handoff"))


if __name__ == "__main__":
    unittest.main()
