"""Compaction shares request limits and preserves unfinished analysis on failure."""

from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

import test_analysis_compaction_chat as fixtures


class AnalysisCompactionLimitTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.AnalysisCompactionChatTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def test_compaction_and_answer_share_the_same_finite_request_budget(self):
        case = self.case
        def respond(payload, number):
            if payload.get("schema") == "business-analysis-compaction/v1":
                return case.compact_reply(payload)
            case.ordinary += 1
            if case.ordinary <= 2:
                return case.fixture.read_helper(case.ordinary)
            self.assertEqual(payload["investigation_budget"]["remaining_model_requests"], 1)
            self.assertEqual(payload["analysis_ledger"]["generation"], 1)
            return case.fixture.cited_answer(payload)
        result = case.fixture.ask(respond, policy=replace(case.policy, max_model_requests=4), request_guard=4)
        self.assertTrue(result["answer_available"])
        self.assertEqual(len(case.compactions), 1)
        self.assertEqual(case.ordinary, 3)
        saved = case.saved(result)
        self.assertEqual(len(saved["requests"]), 4)
        self.assertEqual(sum(row["stage"] == "context_compact"
                             for row in saved["checkpoint"]["quality"]["rounds"]), 1)

    def test_all_deferred_source_is_preserved_without_endless_same_input_compression(self):
        case = self.case
        deferred_tasks = []
        def respond(payload, number):
            if payload.get("schema") == "business-analysis-compaction/v1":
                task = payload["analysis_compaction"]
                deferred_tasks.append(task)
                return json.dumps({"schema": "analysis-ledger-result/v1", "task_id": task["task_id"],
                    "entries": [], "deferred_source_ids": task["source_ids"]})
            return case.respond(payload, number)
        result = case.fixture.ask(respond, policy=case.policy, request_guard=3)
        self.assertEqual(result["stop_reason"], "CONTEXT_COMPACTION_UNRESOLVED")
        self.assertNotEqual(result["status"], "ANALYZED")
        self.assertEqual(len(deferred_tasks), 1)
        saved = case.saved(result)
        memory = saved["checkpoint"]["prior_source_memory"]
        self.assertEqual(len(memory["analysis_ledger"]["generations"]), 1)
        self.assertEqual({row["status"] for row in memory["analysis_ledger"]["source_coverage"].values()},
                         {"deferred"})
        self.assertTrue(set(deferred_tasks[0]["source_ids"]) <= {page["evidence_id"] for page in memory["pages"]})

    def test_small_deferred_source_cannot_disappear_from_final_completion_status(self):
        case = self.case
        deferred_ids = []
        def respond(payload, number):
            if payload.get("schema") == "business-analysis-compaction/v1":
                task = payload["analysis_compaction"]
                pages = case.fixture.pages(payload)
                deferred = min(pages, key=lambda page: len(page["source_text"]))["evidence_id"]
                deferred_ids.append(deferred)
                represented = [identifier for identifier in task["source_ids"] if identifier != deferred]
                self.assertTrue(represented)
                return json.dumps({"schema": "analysis-ledger-result/v1", "task_id": task["task_id"],
                    "entries": [{"kind": "conditions", "text": "尚有应收余额才执行抵扣。",
                        "evidence_ids": represented, "covers_entry_ids": []}],
                    "deferred_source_ids": [deferred]})
            case.ordinary += 1
            return case.fixture.read_helper(case.ordinary) if case.ordinary <= 2 else case.fixture.cited_answer(payload)
        result = case.fixture.ask(respond, policy=case.policy, request_guard=4)
        self.assertEqual(len(deferred_ids), 1)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertTrue(any(boundary.get("reason") == "analysis_deferred_sources"
                            for boundary in result["boundaries"]))

    def test_explicit_continue_revisits_deferred_source_without_automatic_retry(self):
        case = self.case
        def defer(payload, number):
            if payload.get("schema") == "business-analysis-compaction/v1":
                task = payload["analysis_compaction"]
                return json.dumps({"schema": "analysis-ledger-result/v1", "task_id": task["task_id"],
                    "entries": [], "deferred_source_ids": task["source_ids"]})
            return case.respond(payload, number)
        prior = case.fixture.ask(defer, policy=case.policy, request_guard=3)
        self.assertEqual(prior["stop_reason"], "CONTEXT_COMPACTION_UNRESOLVED")
        def revisit(payload, number):
            return (case.compact_reply(payload) if payload.get("schema") == "business-analysis-compaction/v1"
                    else case.fixture.cited_answer(payload))
        result = case.fixture.ask(revisit, policy=case.policy, request_guard=5,
            resume_run_id=prior["run_id"], continue_investigation=True)
        self.assertTrue(result["answer_available"])
        self.assertEqual(len(case.compactions), 1)
        ledger = case.saved(result)["checkpoint"]["prior_source_memory"]["analysis_ledger"]
        self.assertEqual(len(ledger["generations"]), 2)
        self.assertEqual(len(ledger["attempts"]), 2)
        self.assertTrue(all(row["status"] == "represented_in_candidate" for row in ledger["source_coverage"].values()))

    def test_explicit_continue_retries_failed_compaction_and_keeps_original_source_proof(self):
        case = self.case
        def invalid(payload, number):
            if payload.get("schema") == "business-analysis-compaction/v1":
                return '{"schema":"analysis-ledger-result/v1","entries":'
            return case.respond(payload, number)
        failed = case.fixture.ask(invalid, policy=case.policy, request_guard=3)
        self.assertEqual(failed["stop_reason"], "CONTEXT_COMPACTION_INVALID")
        old_memory = case.saved(failed)["checkpoint"]["prior_source_memory"]
        self.assertEqual(old_memory["analysis_ledger"]["attempts"][-1]["status"], "failed")
        result = case.fixture.ask(case.respond, policy=case.policy, request_guard=8,
            resume_run_id=failed["run_id"], continue_investigation=True)
        self.assertTrue(result["answer_available"], result.get("errors"))
        self.assertNotEqual(result["run_id"], failed["run_id"])
        self.assertEqual(case.compactions[0]["retry_token"], result["run_id"])
        memory = case.saved(result)["checkpoint"]["prior_source_memory"]
        for identifier, origin in old_memory["origins"].items():
            self.assertEqual(memory["origins"][identifier], origin)
        self.assertEqual(len(memory["analysis_ledger"]["generations"]), 2)
        self.assertEqual(len(memory["analysis_ledger"]["attempts"]), 3)


if __name__ == "__main__":
    unittest.main()
