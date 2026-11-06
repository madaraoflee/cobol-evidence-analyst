"""Real multi-message memory handoff with synthetic, in-process model replies."""

from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

import test_conversation_analysis_handoff as fixtures
from analyze_source import AnalysisCancelled
from source_session import QuestionSourceSession


class ConversationAnalysisHandoffMultiturnTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.ConversationAnalysisHandoffTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.fixture = self.case.fixture
        # All files exist before the first snapshot. Later turns discover more
        # of the same source version rather than silently changing its scope.
        for number in range(5, 9):
            self.fixture.write(f"helper-{number}.cbl", f"HELPER-{number}",
                f"IF DUE-AMOUNT > {number}\nSUBTRACT {number} FROM DUE-AMOUNT\nEND-IF.\n" +
                "\n".join(f"*> further adjustment context {line}" for line in range(45)),
                "01 DUE-AMOUNT PIC 9(6).")

    def ordinary_turn(self, number, *, helpers=()):
        question = f"root.cbl 的第 {number} 项抵扣条件和前面条件怎样共同生效？"
        self.fixture.question = question
        history = self.case.store.model_history(self.case.conversation["id"], max_characters=80,
            question=question, include_analysis_handoff=True)
        job = f"ordinary-chat-{number}"
        self.case.store.append(self.case.conversation["id"], "user", question, run_id=job)
        start = len(self.fixture.requests)
        ordinary = []
        compactions = []
        marker = f"第{number}轮新增条件仍以应收余额为适用前提。"

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
            return self.fixture.cited_answer(payload) + (
                f"早先抵扣的适用前提仍是尚有应收余额。[{self.case.case.early_id}]")

        result = self.fixture.ask(respond, policy=replace(self.case.case.policy, max_model_requests=8),
            request_guard=start + 8, history=history, conversation_id=self.case.conversation["id"])
        self.assertTrue(result["answer_available"], result.get("errors"))
        self.case.store.append(self.case.conversation["id"], "assistant", result["answer"], run_id=job,
            details={"investigation_run_id": result["run_id"], "investigation_state": result["investigation_state"],
                     "evidence_refs": result["evidence_refs"], "cited_evidence_ids": [self.case.case.early_id]})
        return result, self.case.case.saved(result), ordinary, compactions, marker

    def test_three_messages_accumulate_new_generations_and_retain_original_source_proofs(self):
        first = self.case.prior_analysis()
        first_saved = self.case.old
        second, second_saved, second_payloads, second_compactions, second_marker = self.ordinary_turn(2, helpers=(5, 6))
        third, third_saved, third_payloads, third_compactions, third_marker = self.ordinary_turn(3, helpers=(7, 8))
        self.assertEqual(len({first["run_id"], second["run_id"], third["run_id"]}), 3)
        self.assertTrue(second_compactions)
        self.assertTrue(third_compactions)
        snapshots = [item["checkpoint"]["prior_source_memory"] for item in (first_saved, second_saved, third_saved)]
        self.assertLess(len(snapshots[0]["analysis_ledger"]["generations"]),
                        len(snapshots[1]["analysis_ledger"]["generations"]))
        self.assertLess(len(snapshots[1]["analysis_ledger"]["generations"]),
                        len(snapshots[2]["analysis_ledger"]["generations"]))
        for current, previous in ((second_saved, first_saved), (third_saved, second_saved)):
            checkpoint = current["checkpoint"]
            self.assertEqual(checkpoint["conversation_analysis_handoff"]["from_run_id"], previous["run_id"])
            self.assertEqual(checkpoint["source_memory_context"]["run_id"], first["run_id"])
            self.assertEqual(checkpoint["source_memory_context"]["question"],
                             first_saved["checkpoint"]["source_memory_context"]["question"])
            for identifier, origin in previous["checkpoint"]["prior_source_memory"]["origins"].items():
                self.assertEqual(checkpoint["prior_source_memory"]["origins"][identifier], origin)
        for payload in [*second_payloads, *third_payloads]:
            self.assertIn(self.case.case.early_id, payload["analysis_ledger"]["historical_source_references"])
            self.assertTrue(any("尚有应收余额" in row["text"] for row in payload["analysis_ledger"]["entries"]))
            self.assertNotIn("draft_answer", payload)
        for payload in third_payloads:
            self.assertIn(second_marker, {row["text"] for row in payload["analysis_ledger"]["entries"]})
        self.assertIn(third_marker, {row["text"] for row in third_payloads[-1]["analysis_ledger"]["entries"]})
        earliest = next(page for page in snapshots[0]["pages"] if page["evidence_id"] == self.case.case.early_id)
        self.assertEqual(next(page for page in snapshots[2]["pages"] if page["evidence_id"] == earliest["evidence_id"]), earliest)
        self.assertIn(earliest["evidence_id"], {row["evidence_id"] for row in third["narrative"]["citations"]})

    def test_cancelled_third_message_retains_previous_memory_for_next_ordinary_message(self):
        self.case.prior_analysis()
        second, second_saved, _, _, second_marker = self.ordinary_turn(2, helpers=(5, 6))
        question = "root.cbl 前面这些抵扣条件还有哪些未确认？"
        self.fixture.question = question
        history = self.case.store.model_history(self.case.conversation["id"], max_characters=80,
            question=question, include_analysis_handoff=True)
        self.case.store.append(self.case.conversation["id"], "user", question,
                              run_id="cancelled-third-chat", status="pending")
        before = len(self.fixture.requests)
        capture = QuestionSourceSession.capture
        validating = []
        cancellation = []

        def checking_capture(session, relative_path):
            validating.append(relative_path)
            if relative_path == "helper-1.cbl":
                cancellation.append(True)
            return capture(session, relative_path)

        def check_cancel():
            if cancellation:
                raise AnalysisCancelled("Cancelled during saved-source validation")

        with patch.object(QuestionSourceSession, "capture", checking_capture):
            with self.assertRaises(AnalysisCancelled):
                self.fixture.ask(lambda *_: self.fail("A cancelled handoff must not send a model request"),
                    history=history, conversation_id=self.case.conversation["id"],
                    policy=self.case.case.policy, check_cancel=check_cancel, request_guard=before)
        self.assertIn("helper-1.cbl", validating)
        self.assertEqual(len(self.fixture.requests), before)
        # This is the same persistence operation used by WorkbenchState after
        # AnalysisCancelled; the cancelled question has no assistant answer.
        self.case.store.complete_user("cancelled-third-chat", "cancelled")
        self.assertEqual(self.case.case.saved(second), second_saved)
        fourth, fourth_saved, payloads, _, _ = self.ordinary_turn(4)
        self.assertTrue(fourth["answer_available"])
        self.assertEqual(fourth_saved["checkpoint"]["conversation_analysis_handoff"]["from_run_id"], second["run_id"])
        self.assertIn(second_marker, {row["text"] for row in payloads[0]["analysis_ledger"]["entries"]})
        self.assertIn(self.case.case.early_id, payloads[0]["analysis_ledger"]["historical_source_references"])

    def test_changed_early_source_after_two_messages_invalidates_all_inherited_candidates(self):
        self.case.prior_analysis()
        second, second_saved, _, _, _ = self.ordinary_turn(2, helpers=(5, 6))
        old_page = next(page for page in second_saved["checkpoint"]["prior_source_memory"]["pages"]
                        if page["evidence_id"] == self.case.case.early_id)
        path = self.fixture.source / old_page["relative_path"]
        path.write_text(path.read_text().replace("SUBTRACT 1", "SUBTRACT 9"))
        self.fixture.question = "root.cbl 现在的抵扣前提是什么？"
        history = self.case.store.model_history(self.case.conversation["id"], max_characters=80,
            question=self.fixture.question, include_analysis_handoff=True)
        self.case.store.append(self.case.conversation["id"], "user", self.fixture.question,
                              run_id="changed-source-third-chat")
        observed = []

        def respond(payload, _):
            observed.append(payload)
            return self.fixture.cited_answer(payload)

        result = self.fixture.ask(respond, policy=replace(self.case.case.policy, max_model_requests=1),
            request_guard=len(self.fixture.requests) + 1, history=history,
            conversation_id=self.case.conversation["id"])
        self.assertTrue(result["answer_available"])
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].get("analysis_ledger", {}).get("generation"))
        self.assertNotIn(self.case.case.early_id,
                         observed[0].get("analysis_ledger", {}).get("historical_source_references", {}))
        self.assertIsNone(self.case.case.saved(result)["checkpoint"].get("conversation_analysis_handoff"))
        self.assertEqual(self.case.case.saved(second), second_saved, "A fresh investigation must not rewrite the old proof")


if __name__ == "__main__":
    unittest.main()
