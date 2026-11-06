"""Completed rule batches stay complete across actual compaction requests."""

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from agent_policy import AgentPolicy
import question_investigation
from run_store import RunStore
import test_completion_driven_chat as fixtures


class CompactionRuleBatchContinuityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CompletionDrivenChatTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.write("root.cbl", "TOTAL-RULE", "\n".join(
            f"STEP-{number:02d}.\nADD {number + 1} TO TOTAL-AMOUNT." for number in range(37)),
            "01 TOTAL-AMOUNT PIC 9(6) VALUE 0.")
        for number in range(1, 5):
            self.fixture.write(f"helper-{number}.cbl", f"HELPER-{number}",
                f"IF DUE-AMOUNT > 0\nSUBTRACT {number} FROM DUE-AMOUNT\nEND-IF.\n" +
                "\n".join(f"*> neutral context {line} " + "detail " * 7 for line in range(45)),
                "01 DUE-AMOUNT PIC 9(6).")
        self.fixture.question = "请详细说明 TOTAL-RULE 的整体业务流程及各个步骤处理顺序。"

    def test_completed_rules_are_not_reopened_after_compaction_and_later_reads(self):
        ordinary, compacted, investigation_states, root_batches = [], [], [], []
        early = {}
        original = question_investigation.build_question_investigation

        def observe(*args, **kwargs):
            result = original(*args, **kwargs)
            state = result.get("investigation_frontier_state")
            if state:
                investigation_states.append(deepcopy(state))
            return result

        def respond(payload, number):
            if payload.get("schema") == "business-analysis-compaction/v1":
                task = payload["analysis_compaction"]
                compacted.append(deepcopy(task))
                entries = [{"kind": row["kind"], "text": row["text"],
                    "evidence_ids": row["evidence_ids"], "covers_entry_ids": [row["entry_id"]]}
                    for row in task["previous_entries"]]
                entries.append({"kind": "conditions", "text": "辅助抵扣只在仍有应收余额时执行，后续步骤仍须分别核对。",
                    "evidence_ids": task["source_ids"], "covers_entry_ids": []})
                return json.dumps({"schema": "analysis-ledger-result/v1", "task_id": task["task_id"],
                    "entries": entries, "deferred_source_ids": []}, ensure_ascii=False)
            ordinary.append(deepcopy(payload))
            state = investigation_states[-1]
            root_batches.append(deepcopy(state))
            for page in self.fixture.pages(payload):
                if page["relative_path"] == "helper-1.cbl" and "IF DUE-AMOUNT > 0" in page["source_text"]:
                    early.update(page)
            if len(ordinary) <= 4:
                return self.fixture.read_helper(len(ordinary))
            self.assertTrue(compacted)
            self.assertTrue(early)
            self.assertIn(early["evidence_id"], payload["analysis_ledger"]["historical_source_references"])
            return ("主程序分步骤累计金额；已读的辅助抵扣仅在应收余额为正时执行。"
                f"独立辅助片段不能单独证明主程序的完整运行调用链。[{early['evidence_id']}]")

        with patch.object(question_investigation, "build_question_investigation", side_effect=observe):
            result = self.fixture.ask(respond, request_guard=10, answer_detail="brief",
                policy=AgentPolicy(max_model_requests=10, max_source_characters=6000,
                    max_complete_source_characters=6000, max_answer_revisions=0))
        self.assertTrue(result["answer_available"], result.get("diagnostics"))
        self.assertGreaterEqual(len(compacted), 1)
        self.assertEqual(len(root_batches[0]["active_rule_ids"]), 32)
        first_ids = set(root_batches[0]["active_rule_ids"])
        remaining = root_batches[1]
        self.assertEqual(len(remaining["active_rule_ids"]), 5)
        self.assertEqual(set(remaining["processed_rule_ids"]), first_ids)
        self.assertFalse(first_ids & set(remaining["active_rule_ids"]))
        self.assertTrue(all(row["batch_key"] == remaining["batch_key"] for row in root_batches[1:]),
                        "Compaction or later reads must not reopen a completed first batch")
        self.assertEqual(len(ordinary), 5)
        completed_position = next(index for index, state in enumerate(investigation_states)
            if len(state.get("processed_rule_ids", [])) == 32 and len(state["active_rule_ids"]) == 5)
        self.assertTrue(all(set(state.get("processed_rule_ids", [])) >= first_ids
                            for state in investigation_states[completed_position:]))
        self.assertTrue(all(not (first_ids & set(state["active_rule_ids"]))
                            for state in investigation_states[completed_position:]))
        saved = RunStore(self.fixture.root / "investigation-runs.sqlite").load(result["run_id"])
        checkpoint = saved["checkpoint"]
        state = checkpoint["investigation_frontier_state"]
        self.assertEqual(len(set(state["processed_rule_ids"]) | set(state["active_rule_ids"])), 37)
        self.assertEqual(len(checkpoint["investigation_obligation_ledger"]), 2)
        memory = checkpoint["prior_source_memory"]
        self.assertGreaterEqual(len(memory["analysis_ledger"]["generations"]), 1)
        self.assertTrue({f"helper-{number}.cbl" for number in range(1, 5)} <=
                        {page["relative_path"] for page in memory["pages"]})
        self.assertIn(early["evidence_id"], {ref["evidence_id"] for ref in result["narrative"]["citations"]})
        with sqlite3.connect(self.fixture.root / "versioned-evidence.sqlite") as connection:
            archived = connection.execute("SELECT text,source_sha256 FROM excerpts WHERE evidence_id=?",
                (early["evidence_id"],)).fetchone()
        self.assertEqual(archived, (early["source_text"], early["source_sha256"]))
        self.assertEqual(len(saved["requests"]), len(ordinary) + len(compacted))


if __name__ == "__main__":
    unittest.main()
