"""Ordinary same-object chat messages retain source-bound cumulative analysis."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from conversation_store import ConversationStore
from agent_policy import AgentPolicy
from company_api import TransportResponse
from run_store import RunStore
import test_analysis_compaction_chat as fixtures
import test_conversation_workbench as workbench_fixtures


class ConversationAnalysisHandoffTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.AnalysisCompactionChatTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.fixture = self.case.fixture
        self.store = ConversationStore(self.fixture.root, self.fixture.source)
        self.conversation = self.store.create()

    def prior_analysis(self, *, respond=None, policy=None):
        question = self.fixture.question
        self.store.append(self.conversation["id"], "user", question, run_id="first-chat-job")
        result = self.fixture.ask(respond or self.case.respond, policy=policy or self.case.policy, request_guard=7,
                                  conversation_id=self.conversation["id"])
        self.store.append(self.conversation["id"], "assistant", result["answer"], run_id="first-chat-job",
            details={"investigation_run_id": result["run_id"], "investigation_state": result["investigation_state"],
                     "evidence_refs": result["evidence_refs"], "cited_evidence_ids": [self.case.early_id]})
        self.old = self.case.saved(result)
        return result

    def followup(self, question, *, inherited, history=None, conversation_id=None, require_evicted=True):
        self.fixture.question = question
        conversation_id = conversation_id or self.conversation["id"]
        if history is None:
            history = self.store.model_history(conversation_id, max_characters=80, question=question,
                                               include_analysis_handoff=True)
        self.store.append(conversation_id, "user", question, run_id="next-chat-job")
        def respond(payload, number):
            ledger = payload.get("analysis_ledger", {})
            self.assertEqual(ledger.get("generation", 0), 2 if inherited else 0)
            self.assertNotIn("draft_answer", payload)
            if inherited:
                self.assertIn(self.case.early_id, ledger["historical_source_references"])
                self.assertTrue(any("尚有应收余额" in row["text"] for row in ledger["entries"]))
                if require_evicted:
                    self.assertNotIn(self.case.early_id, {page["evidence_id"] for page in self.fixture.pages(payload)})
                return self.fixture.cited_answer(payload) + f"早先抵扣仍要求尚有应收余额。[{self.case.early_id}]"
            return self.fixture.cited_answer(payload)
        return self.fixture.ask(respond, policy=replace(self.case.policy, max_model_requests=1), request_guard=8,
            history=history, conversation_id=conversation_id)

    def test_ordinary_same_object_followup_keeps_early_analysis_after_history_text_is_evicted(self):
        prior = self.prior_analysis()
        result = self.followup("root.cbl 的抵扣还需要满足哪些条件？", inherited=True)
        self.assertTrue(result["answer_available"], result.get("errors"))
        self.assertEqual(result["metrics"]["model_requests"], 1)
        saved = self.case.saved(result)
        self.assertNotEqual(saved["run_id"], prior["run_id"])
        self.assertEqual(len(saved["requests"]), 1)
        self.assertEqual(saved["checkpoint"]["source_memory_context"]["question"],
                         self.old["checkpoint"]["source_memory_context"]["question"])
        self.assertEqual(saved["checkpoint"]["conversation_analysis_handoff"]["from_run_id"], prior["run_id"])
        old_memory = self.old["checkpoint"]["prior_source_memory"]
        new_memory = saved["checkpoint"]["prior_source_memory"]
        self.assertEqual(new_memory["scope"], old_memory["scope"])
        self.assertEqual(new_memory["analysis_ledger"]["generations"], old_memory["analysis_ledger"]["generations"])
        for identifier, origin in old_memory["origins"].items():
            self.assertEqual(new_memory["origins"][identifier], origin)

    def test_ordinary_new_object_does_not_inherit_old_analysis(self):
        self.prior_analysis()
        result = self.followup("helper-3.cbl 的计算条件是什么？", inherited=False)
        self.assertTrue(result["answer_available"])
        self.assertIsNone(self.case.saved(result)["checkpoint"].get("conversation_analysis_handoff"))

    def test_deictic_followup_inherits_same_locally_revalidated_program(self):
        self.prior_analysis()
        result = self.followup("这个流程的抵扣还需要满足哪些条件？", inherited=True, require_evicted=False)
        self.assertTrue(result["answer_available"])
        self.assertIsNotNone(self.case.saved(result)["checkpoint"].get("conversation_analysis_handoff"))

    def test_changed_source_reading_options_invalidate_handoff(self):
        self.prior_analysis()
        checkpoint = deepcopy(self.old["checkpoint"])
        checkpoint["source_index_configuration"] = "f" * 64
        RunStore(self.fixture.root / "investigation-runs.sqlite").checkpoint(self.old["run_id"], checkpoint,
                                                                           status="completed")
        result = self.followup("root.cbl 的抵扣还需要满足哪些条件？", inherited=False)
        self.assertTrue(result["answer_available"])

    def test_one_request_followup_delivers_supported_answer_and_preserves_uncompressed_global_work(self):
        def initial(payload, number):
            return self.fixture.read_helper(number) if number <= 4 else self.fixture.cited_answer(payload)
        self.prior_analysis(respond=initial, policy=replace(self.case.policy, context_compaction_enabled=False))
        self.fixture.question = "root.cbl 当前可以确认哪些处理条件？"
        history = self.store.model_history(self.conversation["id"], question=self.fixture.question,
                                           include_analysis_handoff=True)
        self.store.append(self.conversation["id"], "user", self.fixture.question, run_id="limited-chat-job")
        def deliver(payload, number):
            self.assertNotIn("analysis_compaction", payload)
            self.assertGreater(payload["analysis_compaction_pending"]["source_count"], 0)
            self.assertFalse(payload["analysis_compaction_pending"]["global_analysis_complete"])
            self.assertFalse(payload["question_investigation"]["investigation_complete"])
            self.assertTrue(payload["analysis_compaction_pending"]["original_source_preserved"])
            return self.fixture.cited_answer(payload)
        result = self.fixture.ask(deliver, policy=replace(self.case.policy, max_model_requests=1), request_guard=6,
            history=history, conversation_id=self.conversation["id"])
        self.assertTrue(result["answer_available"])
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["metrics"]["model_requests"], 1)
        saved = self.case.saved(result)
        self.assertTrue(saved["checkpoint"]["analysis_compaction_pending_ids"])
        self.assertEqual(saved["checkpoint"]["prior_source_memory"]["analysis_ledger"]["attempts"], [])
        self.assertTrue({page["evidence_id"] for page in self.old["checkpoint"]["prior_source_memory"]["pages"]}
            <= {page["evidence_id"] for page in saved["checkpoint"]["prior_source_memory"]["pages"]})

    def test_reset_question_does_not_inherit_even_when_it_names_same_program(self):
        self.prior_analysis()
        result = self.followup("换个问题，root.cbl 做了什么？", inherited=False)
        self.assertTrue(result["answer_available"])

    def test_forged_handle_cannot_borrow_another_local_run(self):
        self.prior_analysis()
        question = "root.cbl 的抵扣还需要满足哪些条件？"
        history = self.store.model_history(self.conversation["id"], question=question, include_analysis_handoff=True)
        next(row for row in history if row["role"] == "assistant")["investigation_run_id"] = "f" * 24
        result = self.followup(question, inherited=False, history=history)
        self.assertTrue(result["answer_available"])

    def test_changed_archived_source_invalidates_handoff(self):
        self.prior_analysis()
        path = self.fixture.source / "helper-1.cbl"
        path.write_text(path.read_text().replace("SUBTRACT 1", "SUBTRACT 9"))
        result = self.followup("root.cbl 的抵扣还需要满足哪些条件？", inherited=False)
        self.assertTrue(result["answer_available"])

    def test_changed_compilation_context_invalidates_handoff(self):
        self.prior_analysis()
        checkpoint = deepcopy(self.old["checkpoint"])
        checkpoint["compilation_context"] = {"dialect": "different-neutral-dialect"}
        RunStore(self.fixture.root / "investigation-runs.sqlite").checkpoint(self.old["run_id"], checkpoint,
                                                                           status="completed")
        result = self.followup("root.cbl 的抵扣还需要满足哪些条件？", inherited=False)
        self.assertTrue(result["answer_available"])

    def test_same_handle_from_another_conversation_is_rejected(self):
        self.prior_analysis()
        question = "root.cbl 的抵扣还需要满足哪些条件？"
        history = self.store.model_history(self.conversation["id"], question=question, include_analysis_handoff=True)
        other = self.store.create()
        result = self.followup(question, inherited=False, history=history, conversation_id=other["id"])
        self.assertTrue(result["answer_available"])


class ConversationAnalysisHandoffWorkbenchTests(unittest.TestCase):
    def test_continue_ordinary_followup_retains_the_same_history_handle_identity(self):
        case = workbench_fixtures.ConversationWorkbenchTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        case.app.policy_provider = lambda: AgentPolicy(max_model_requests=1, max_answer_revisions=0)
        first = case.run_job(question="QUOTA 的容量如何检查？", allow_network=True)
        conversation_id = first["conversation"]["id"]
        working = case.transport
        case.transport = lambda request: TransportResponse(401, "{}")
        question = "QUOTA 超出容量之后呢？"
        partial = case.run_job(question=question, allow_network=True, conversation_id=conversation_id)
        user, answer = partial["conversation"]["messages"][-2:]
        self.assertEqual(answer["status"], "partial")
        previous_run = answer["investigation_run_id"]
        case.transport = working
        continued = case.run_job(question=question, allow_network=True, conversation_id=conversation_id,
                                 continue_message_id=user["id"])
        result = continued["agent"]["agent_result"]
        self.assertTrue(result["model_answer_recorded"], continued["agent"].get("reason_code"))
        self.assertNotEqual(result["run_id"], previous_run)
        self.assertNotEqual(continued["agent"].get("reason_code"), "RESUME_IDENTITY_MISMATCH")


if __name__ == "__main__":
    unittest.main()
