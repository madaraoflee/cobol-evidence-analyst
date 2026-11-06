"""Real request orchestration for cumulative analysis, without network access."""

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from agent_policy import AgentPolicy
from prior_source_memory import PriorSourceMemory
from quality_trace import QualityTrace
from run_store import RunStore
import test_completion_driven_chat as fixtures


class AnalysisCompactionChatTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CompletionDrivenChatTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        for number in range(1, 5):
            self.fixture.write(f"helper-{number}.cbl", f"HELPER-{number}",
                f"IF DUE-AMOUNT > 0\nSUBTRACT {number} FROM DUE-AMOUNT\nEND-IF.\n" +
                "\n".join(f"*> descriptive context {line}" for line in range(45)),
                "01 DUE-AMOUNT PIC 9(6).")
        self.policy = AgentPolicy(max_source_characters=2000, max_complete_source_characters=2000,
            max_answer_revisions=0)
        self.ordinary = 0
        self.compactions = []
        self.early_id = None

    def compact_reply(self, payload):
        task = payload["analysis_compaction"]
        self.compactions.append(task)
        entries = [{"kind": row["kind"], "text": row["text"],
                    "evidence_ids": row["evidence_ids"], "covers_entry_ids": [row["entry_id"]]}
                   for row in task["previous_entries"]]
        entries.append({"kind": "conditions", "text": "仅在尚有应收余额时执行该项抵扣，之后仍需检查后续处理。",
            "evidence_ids": task["source_ids"], "covers_entry_ids": []})
        return json.dumps({"schema": "analysis-ledger-result/v1", "task_id": task["task_id"],
            "entries": entries, "deferred_source_ids": []}, ensure_ascii=False)

    def respond(self, payload, number):
        if payload.get("schema") == "business-analysis-compaction/v1":
            return self.compact_reply(payload)
        self.ordinary += 1
        for page in self.fixture.pages(payload):
            if page["relative_path"] == "helper-1.cbl":
                self.early_id = page["evidence_id"]
        if self.ordinary <= 4:
            return self.fixture.read_helper(self.ordinary)
        self.assertNotIn(self.early_id, {p["evidence_id"] for p in self.fixture.pages(payload)})
        self.assertIn(self.early_id, payload["analysis_ledger"]["historical_source_references"])
        return self.fixture.cited_answer(payload) + (
            f"早先的抵扣仅在尚有应收余额时执行。[{self.early_id}]")

    def saved(self, result):
        return RunStore(self.fixture.root / "investigation-runs.sqlite").load(result["run_id"])

    def test_two_generations_preserve_early_condition_and_original_citation(self):
        result = self.fixture.ask(self.respond, policy=self.policy, request_guard=7)
        self.assertTrue(result["answer_available"], result.get("errors"))
        self.assertEqual(len(self.compactions), 2)
        self.assertEqual(self.ordinary, 5)
        self.assertIn(self.early_id, {r["evidence_id"] for r in result["narrative"]["citations"]})
        saved = self.saved(result)
        ledger = saved["checkpoint"]["prior_source_memory"]["analysis_ledger"]
        self.assertEqual(len(ledger["generations"]), 2)
        self.assertEqual(len(saved["requests"]), 7)
        states = [event["analysis_state"] for event in self.fixture.events if event.get("analysis_state")]
        self.assertTrue(any(state.get("compaction", {}).get("status") == "running" for state in states))
        self.assertTrue(any(state.get("compaction", {}).get("status") == "completed" for state in states))
        self.assertTrue(any(state["findings"] for state in states))
        self.assertFalse(any("analysis-ledger-result" in event.get("answer_preview", {}).get("text", "")
                             for event in self.fixture.events))
        self.assertEqual(saved["checkpoint"]["analysis_state_revision"], states[-1]["revision"])

    def test_invalid_second_compression_retains_first_generation_and_all_source(self):
        def respond(payload, number):
            if payload.get("schema") == "business-analysis-compaction/v1" and self.compactions:
                task = payload["analysis_compaction"]
                return json.dumps({"schema": "analysis-ledger-result/v1", "task_id": task["task_id"],
                    "entries": [], "deferred_source_ids": task["source_ids"]})
            return self.respond(payload, number)
        result = self.fixture.ask(respond, policy=self.policy, request_guard=6)
        self.assertEqual(result["stop_reason"], "CONTEXT_COMPACTION_INVALID")
        saved = self.saved(result)
        memory = saved["checkpoint"]["prior_source_memory"]
        self.assertEqual(len(memory["analysis_ledger"]["generations"]), 1)
        self.assertEqual(memory["analysis_ledger"]["attempts"][-1]["status"], "failed")
        self.assertIn(self.early_id, {page["evidence_id"] for page in memory["pages"]})
        self.assertTrue(any(event.get("analysis_state", {}).get("compaction", {}).get("status") == "failed"
                            for event in self.fixture.events))

    def test_received_compression_replays_once_after_interrupt_without_resending(self):
        class Interrupted(RuntimeError):
            pass
        with patch.object(PriorSourceMemory, "accept_analysis_compaction", side_effect=Interrupted):
            with self.assertRaises(Interrupted):
                self.fixture.ask(self.respond, policy=self.policy, request_guard=3)
        store = RunStore(self.fixture.root / "investigation-runs.sqlite")
        with store.connect() as connection:
            run_id = connection.execute("SELECT run_id FROM investigation_runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
        before = store.load(run_id)
        self.assertIsNotNone(before["checkpoint"]["prior_source_memory"]["analysis_ledger"]["pending_task"])
        self.assertEqual(len(self.compactions), 1)
        result = self.fixture.ask(self.respond, policy=self.policy, request_guard=7, resume_run_id=run_id)
        self.assertTrue(result["answer_available"], result.get("errors"))
        self.assertEqual(len(self.compactions), 2, "The received first compression must not be sent twice")
        saved = self.saved(result)
        self.assertEqual(len(saved["requests"]), 7)
        self.assertEqual(len(saved["checkpoint"]["prior_source_memory"]["analysis_ledger"]["generations"]), 2)

    def test_prepared_but_unsent_compression_resumes_its_saved_task(self):
        class Interrupted(RuntimeError):
            pass
        prepare = QualityTrace.prepare
        def interrupt(trace, **kwargs):
            if kwargs["stage"] == "context_compact":
                raise Interrupted
            return prepare(trace, **kwargs)
        with patch.object(QualityTrace, "prepare", interrupt):
            with self.assertRaises(Interrupted):
                self.fixture.ask(self.respond, policy=self.policy, request_guard=2)
        store = RunStore(self.fixture.root / "investigation-runs.sqlite")
        with store.connect() as connection:
            run_id = connection.execute("SELECT run_id FROM investigation_runs").fetchone()[0]
        pending = store.load(run_id)["checkpoint"]["prior_source_memory"]["analysis_ledger"]["pending_task"]
        self.assertIsNotNone(pending)
        result = self.fixture.ask(self.respond, policy=self.policy, request_guard=7, resume_run_id=run_id)
        self.assertTrue(result["answer_available"], result.get("errors"))
        self.assertEqual(self.compactions[0]["task_id"], pending["task_id"])
        self.assertEqual(len(self.compactions), 2)


if __name__ == "__main__":
    unittest.main()
