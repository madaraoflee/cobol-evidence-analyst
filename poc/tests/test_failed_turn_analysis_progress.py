"""Known failed requests must not discard completed, source-bound analysis work."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from company_api import TransportResponse
from conversation_analysis_handoff import followup_analysis_seed
from run_store import RunStore
from source_session import QuestionSourceSession
from web_app import WorkbenchState
import test_completion_driven_chat as transport_fixture
import test_conversation_analysis_handoff_multiturn as fixtures


class FailedTurnAnalysisProgressTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.ConversationAnalysisHandoffMultiturnTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.fixture = self.case.fixture
        self.conversation_case = self.case.case
        self.first = self.conversation_case.prior_analysis()
        self.original = self.conversation_case.old
        self.store = RunStore(self.fixture.root / "investigation-runs.sqlite")
        self.conversations = self.conversation_case.store
        self.conversation_id = self.conversation_case.conversation["id"]
        self.app = WorkbenchState(state_path=self.fixture.root / "settings.json",
                                  config_provider=lambda: self.fixture.config)
        self.app.conversation_store = self.conversations
        self.failed_turns = 0

    @staticmethod
    def memory(saved):
        return saved["checkpoint"]["prior_source_memory"]

    @classmethod
    def generations(cls, saved):
        return len(cls.memory(saved)["analysis_ledger"]["generations"])

    @classmethod
    def page_ids(cls, saved):
        return {page["evidence_id"] for page in cls.memory(saved)["pages"]}

    def history(self, question):
        return self.conversations.model_history(self.conversation_id, max_characters=80,
            question=question, include_analysis_handoff=True)

    def fail_after_progress(self, *, question, helpers=(5, 6)):
        self.failed_turns += 1
        job = f"failed-progress-{self.failed_turns}"
        marker = f"第{self.failed_turns}次失败前新增的抵扣条件已经纳入累计分析。"
        history = self.history(question)
        self.fixture.question = question
        self.conversations.append(self.conversation_id, "user", question, run_id=job, status="pending")
        ordinary, compactions = [], []

        def respond(payload, _):
            if payload.get("schema") == "business-analysis-compaction/v1":
                task = payload["analysis_compaction"]
                compactions.append(task)
                entries = [{"kind": row["kind"], "text": row["text"],
                    "evidence_ids": row["evidence_ids"], "covers_entry_ids": [row["entry_id"]]}
                    for row in task["previous_entries"]]
                entries.append({"kind": "conditions", "text": marker,
                    "evidence_ids": task["source_ids"], "covers_entry_ids": []})
                return json.dumps({"schema": "analysis-ledger-result/v1", "task_id": task["task_id"],
                    "entries": entries, "deferred_source_ids": []}, ensure_ascii=False)
            ordinary.append(payload)
            if len(ordinary) <= len(helpers):
                return self.fixture.read_helper(helpers[len(ordinary) - 1])
            return "SYNTHETIC_HTTP_FAILURE"

        def response(status, body):
            if status == 200 and json.loads(body)["choices"][0]["message"]["content"] == "SYNTHETIC_HTTP_FAILURE":
                return TransportResponse(500, json.dumps({"error": {"code": "internal_error"}}))
            return TransportResponse(status, body)

        with patch.object(transport_fixture, "TransportResponse", side_effect=response):
            result = self.fixture.ask(respond,
                policy=replace(self.conversation_case.case.policy, max_model_requests=8),
                request_guard=len(self.fixture.requests) + 8, history=history,
                conversation_id=self.conversation_id)
        self.assertFalse(result["answer_available"])
        self.assertEqual(result["answer_origin"], "diagnostic")
        self.assertEqual(result["stop_reason"], "HTTP_ERROR")
        saved = self.store.load(result["run_id"])
        self.assertFalse(saved["uncertain_requests"])
        self.app.conversation = self.conversations.get(self.conversation_id)
        self.app._record_answer({"conversation_id": self.conversation_id, "question": question,
            "conversation_history": history}, job,
            {"agent": {"runner_status": "COMPLETED", "agent_result": result}})
        message = self.conversations.get(self.conversation_id)["messages"][-1]
        self.assertEqual(message["status"], "failed")
        return result, saved, marker, compactions

    def seed(self, *, store=None, history=None):
        question = "root.cbl 前面这些抵扣条件还需要什么前提？"
        with QuestionSourceSession(self.fixture.database, self.fixture.source) as session:
            return followup_analysis_seed(question, database_path=self.fixture.database,
                source_root=self.fixture.source, session=session,
                history=self.history(question) if history is None else history,
                conversation_id=self.conversation_id, store=store or self.store,
                framework_reference_path="")

    def test_completed_compaction_before_http500_is_inherited_by_next_question(self):
        failed, saved, marker, compactions = self.fail_after_progress(
            question="root.cbl 新的第五及第六项抵扣条件如何影响前面规则？")
        self.assertTrue(compactions)
        self.assertEqual([self.generations(self.original), self.generations(saved)], [2, 3])
        _, following, payloads, _, _ = self.case.ordinary_turn(3)
        self.assertEqual(self.generations(following), 3)
        self.assertEqual(following["checkpoint"]["conversation_analysis_handoff"]["from_run_id"], failed["run_id"])
        self.assertIn(marker, {entry["text"] for entry in payloads[0]["analysis_ledger"]["entries"]})
        self.assertLess(self.page_ids(self.original), self.page_ids(saved))
        self.assertLessEqual(self.page_ids(saved), self.page_ids(following))
        self.assertEqual(self.store.load(self.first["run_id"]), self.original)
        self.assertEqual(self.store.load(failed["run_id"]), saved)

    def test_two_failed_turns_preserve_monotonic_analysis_and_latest_owned_handle(self):
        first, saved_first, first_marker, _ = self.fail_after_progress(
            question="root.cbl 第五及第六项抵扣如何生效？")
        second, saved_second, second_marker, _ = self.fail_after_progress(
            question="root.cbl 第七及第八项抵扣如何衔接？", helpers=(7, 8))
        _, following, payloads, _, _ = self.case.ordinary_turn(4)
        self.assertEqual([self.generations(saved) for saved in
                          (self.original, saved_first, saved_second, following)], [2, 3, 4, 4])
        self.assertEqual(saved_second["checkpoint"]["conversation_analysis_handoff"]["from_run_id"], first["run_id"])
        self.assertEqual(following["checkpoint"]["conversation_analysis_handoff"]["from_run_id"], second["run_id"])
        text = {row["text"] for row in payloads[0]["analysis_ledger"]["entries"]}
        self.assertTrue({first_marker, second_marker}.issubset(text))
        self.assertEqual(self.store.load(first["run_id"]), saved_first)
        self.assertEqual(self.store.load(second["run_id"]), saved_second)

    def test_older_unknown_request_blocks_even_a_newer_known_failure(self):
        first, _, _, _ = self.fail_after_progress(question="root.cbl 第五及第六项抵扣如何生效？")
        _, latest, _, _ = self.fail_after_progress(
            question="root.cbl 第七及第八项抵扣如何衔接？", helpers=(7, 8))
        self.assertFalse(latest["uncertain_requests"])
        self.store.start_request(first["run_id"], "unresolved-later-request", "f" * 64)
        requests = len(self.fixture.requests)
        self.assertIsNone(self.seed())
        self.assertEqual(len(self.fixture.requests), requests)
        self.assertTrue(self.store.load(first["run_id"])["uncertain_requests"])

    def test_changed_new_source_and_compilation_identity_reject_failed_donor(self):
        failed, saved, _, _ = self.fail_after_progress(question="root.cbl 新的第五及第六项抵扣条件如何影响前面规则？")
        added = self.page_ids(saved) - self.page_ids(self.original)
        path = next(page["relative_path"] for page in self.memory(saved)["pages"] if page["evidence_id"] in added)
        original_bytes = (self.fixture.source / path).read_bytes()
        captures = []
        capture = QuestionSourceSession.capture

        def observed(session, relative_path):
            captures.append(relative_path)
            return capture(session, relative_path)

        (self.fixture.source / path).write_bytes(original_bytes + b"\n*> changed after failed answer\n")
        with patch.object(QuestionSourceSession, "capture", observed):
            self.assertIsNone(self.seed())
        self.assertIn(path, captures)
        (self.fixture.source / path).write_bytes(original_bytes)
        for key, value in (("source_index_configuration", "f" * 64),
                           ("compilation_context", {"dialect": "changed-neutral-dialect"})):
            with self.subTest(key=key):
                checkpoint = deepcopy(saved["checkpoint"])
                checkpoint[key] = value
                self.store.checkpoint(failed["run_id"], checkpoint, status=saved["state"])
                self.assertIsNone(self.seed())
        self.store.checkpoint(failed["run_id"], saved["checkpoint"], status=saved["state"])

    def test_no_progress_failure_keeps_original_anchor(self):
        _, saved, _, compactions = self.fail_after_progress(
            question="root.cbl 新的第五及第六项抵扣条件如何影响前面规则？", helpers=())
        self.assertEqual(compactions, [])
        self.assertEqual(self.generations(saved), 2)
        seed = self.seed()
        self.assertEqual(seed["conversation_analysis_handoff"]["from_run_id"], self.first["run_id"])
        self.assertEqual(seed["prior_source_memory"]["analysis_ledger"]["generations"],
                         self.memory(self.original)["analysis_ledger"]["generations"])

    def test_deictic_failed_chain_preserves_verified_lineage(self):
        first, _, _, _ = self.fail_after_progress(question="这个流程的新抵扣还需要什么条件？")
        second, latest, marker, _ = self.fail_after_progress(
            question="继续解释这个流程后面的抵扣条件。", helpers=(7, 8))
        seed = self.seed()
        self.assertEqual(seed["conversation_analysis_handoff"]["from_run_id"], second["run_id"])
        self.assertEqual(seed["conversation_analysis_handoff"]["generation"], 4)
        self.assertIn(marker, json.dumps(seed["prior_source_memory"], ensure_ascii=False))
        self.assertEqual(latest["checkpoint"]["conversation_analysis_handoff"]["from_run_id"], first["run_id"])
        load = self.store.load

        def foreign_question(run_id):
            saved = load(run_id)
            if run_id == second["run_id"]:
                saved = deepcopy(saved)
                saved["metadata"]["question_id"] = "sha256:" + "f" * 64
            return saved

        with patch.object(self.store, "load", side_effect=foreign_question):
            self.assertIsNone(self.seed())
        forged_history = self.history("root.cbl 前面的抵扣条件是什么？")
        next(row for row in reversed(forged_history) if row["role"] == "assistant")["investigation_run_id"] = second["run_id"]
        self.assertIsNone(self.seed(history=forged_history))

    def test_forked_or_retracted_failed_checkpoint_is_not_merged(self):
        failed, saved, _, _ = self.fail_after_progress(question="root.cbl 新的第五及第六项抵扣条件如何影响前面规则？")
        mutations = []
        missing_page = deepcopy(saved["checkpoint"])
        old_id = next(iter(self.page_ids(self.original)))
        missing_page["prior_source_memory"]["pages"] = [page for page in
            missing_page["prior_source_memory"]["pages"] if page["evidence_id"] != old_id]
        mutations.append(missing_page)
        changed_generation = deepcopy(saved["checkpoint"])
        changed_generation["prior_source_memory"]["analysis_ledger"]["generations"] = []
        mutations.append(changed_generation)
        changed_origin = deepcopy(saved["checkpoint"])
        origin_id = next(iter(self.memory(self.original)["origins"]))
        changed_origin["prior_source_memory"]["origins"][origin_id] = {}
        mutations.append(changed_origin)
        for checkpoint in mutations:
            with self.subTest(mutation=mutations.index(checkpoint)):
                self.store.checkpoint(failed["run_id"], checkpoint, status=saved["state"])
                seed = self.seed()
                self.assertEqual(seed["conversation_analysis_handoff"]["from_run_id"], self.first["run_id"])
                self.assertEqual(seed["prior_source_memory"]["analysis_ledger"]["generations"],
                                 self.memory(self.original)["analysis_ledger"]["generations"])
        self.assertEqual(self.store.load(self.first["run_id"]), self.original)


if __name__ == "__main__":
    unittest.main()
