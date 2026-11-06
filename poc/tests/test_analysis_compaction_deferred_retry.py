"""Explicit continuation can finish small deferred source without automatic loops."""

import json
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from agent_policy import AgentPolicy
from analysis_compaction import compaction_input_ids
import test_analysis_compaction as selection_fixtures
import test_analysis_compaction_chat as chat_fixtures


class DeferredCompactionSelectionTests(unittest.TestCase):
    def test_visible_small_deferred_page_requires_explicit_retry(self):
        case = selection_fixtures.AnalysisCompactionTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        task = case.memory.prepare_analysis_compaction()
        result = {"schema": "analysis-ledger-result/v1", "task_id": task["task_id"],
                  "entries": [], "deferred_source_ids": task["source_ids"]}
        payload = {"source_context": [{"pages": [case.page]}], "analysis_compaction": task}
        self.assertTrue(case.memory.accept_analysis_compaction(result, task_id=task["task_id"],
            payload=payload, round_record=case.fixture.round_record([case.page], 2)))
        policy = AgentPolicy()
        self.assertLess(len(case.page["source_text"]), policy.max_source_characters * 0.75)
        self.assertEqual(compaction_input_ids(case.memory, [case.page], policy), [])
        self.assertEqual(compaction_input_ids(case.memory, [], policy), [])
        self.assertEqual(compaction_input_ids(case.memory, [case.page], policy, retry_deferred=True),
                         [case.page["evidence_id"]])


class DeferredCompactionChatTests(unittest.TestCase):
    def setUp(self):
        self.case = chat_fixtures.AnalysisCompactionChatTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.deferred = None
        self.initial_compactions = 0

    def defer_small(self, payload, number):
        case = self.case
        if payload.get("schema") == "business-analysis-compaction/v1":
            self.initial_compactions += 1
            task = payload["analysis_compaction"]
            page = min(case.fixture.pages(payload), key=lambda item: len(item["source_text"]))
            self.deferred = page["evidence_id"]
            self.assertLess(len(page["source_text"]), case.policy.max_source_characters * 0.75)
            represented = [identifier for identifier in task["source_ids"] if identifier != self.deferred]
            self.assertTrue(represented)
            return json.dumps({"schema": "analysis-ledger-result/v1", "task_id": task["task_id"],
                "entries": [{"kind": "conditions", "text": "Remaining balance gates collection.",
                    "evidence_ids": represented, "covers_entry_ids": []}],
                "deferred_source_ids": [self.deferred]})
        case.ordinary += 1
        return (case.fixture.read_helper(case.ordinary) if case.ordinary <= 2
                else case.fixture.cited_answer(payload))

    def first_partial(self):
        result = self.case.fixture.ask(self.defer_small, policy=self.case.policy, request_guard=4)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(self.initial_compactions, 1, "Ordinary rounds must not repeatedly compress deferred source")
        saved = self.case.saved(result)["checkpoint"]["prior_source_memory"]
        self.assertEqual(saved["analysis_ledger"]["source_coverage"][self.deferred]["status"], "deferred")
        return result, saved

    def test_explicit_continue_represents_small_deferred_source_and_preserves_originals(self):
        case = self.case
        first, before = self.first_partial()
        def revisit(payload, number):
            return (case.compact_reply(payload) if payload.get("schema") == "business-analysis-compaction/v1"
                    else case.fixture.cited_answer(payload))
        result = case.fixture.ask(revisit, policy=case.policy, request_guard=6,
            resume_run_id=first["run_id"], continue_investigation=True)
        self.assertEqual(len(case.compactions), 1)
        self.assertEqual(case.compactions[0]["source_ids"][0], self.deferred)
        self.assertEqual(case.compactions[0]["retry_token"], result["run_id"])
        after = case.saved(result)["checkpoint"]["prior_source_memory"]
        self.assertEqual(len(after["analysis_ledger"]["generations"]), 2)
        self.assertEqual(after["analysis_ledger"]["source_coverage"][self.deferred]["status"],
                         "represented_in_candidate")
        self.assertFalse(any(row.get("reason") == "analysis_deferred_sources" for row in result["boundaries"]))
        after_pages = {page["evidence_id"]: page for page in after["pages"]}
        for page in before["pages"]:
            self.assertEqual(after_pages[page["evidence_id"]], page)
            self.assertEqual(after["origins"][page["evidence_id"]], before["origins"][page["evidence_id"]])

    def test_still_deferred_explicit_retry_does_not_loop(self):
        case = self.case
        first, _ = self.first_partial()
        retries = []
        def defer_again(payload, number):
            if payload.get("schema") == "business-analysis-compaction/v1":
                task = payload["analysis_compaction"]
                retries.append(task)
                entries = [{"kind": row["kind"], "text": row["text"],
                    "evidence_ids": row["evidence_ids"], "covers_entry_ids": [row["entry_id"]]}
                    for row in task["previous_entries"]]
                return json.dumps({"schema": "analysis-ledger-result/v1", "task_id": task["task_id"],
                    "entries": entries, "deferred_source_ids": task["source_ids"]})
            return case.fixture.cited_answer(payload)
        result = case.fixture.ask(defer_again, policy=case.policy, request_guard=6,
            resume_run_id=first["run_id"], continue_investigation=True)
        self.assertEqual(len(retries), 1)
        self.assertEqual(result["stop_reason"], "CONTEXT_COMPACTION_UNRESOLVED")
        self.assertEqual(result["status"], "PARTIAL")
        ledger = case.saved(result)["checkpoint"]["prior_source_memory"]["analysis_ledger"]
        self.assertEqual(len(ledger["generations"]), 2)
        self.assertEqual(ledger["source_coverage"][self.deferred]["status"], "deferred")


if __name__ == "__main__":
    unittest.main()
