"""Real offline chat requests retain source provenance under byte pressure."""

import json
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from agent_policy import AgentPolicy
from prior_source_memory import PriorSourceMemory
from run_store import RunStore
import test_prior_source_memory_chat as memory_fixtures


class PriorMemoryRequestBudgetTests(unittest.TestCase):
    def setUp(self):
        self.case = memory_fixtures.PriorSourceMemoryEvictionChatTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.policy = AgentPolicy(max_source_characters=2000,
            max_complete_source_characters=2000, max_request_bytes=32768,
            max_model_requests=4)

    def test_long_findings_keep_previous_subprograms_and_new_source(self):
        case = self.case

        def respond(payload, number):
            case.capture(payload)
            if number == 1:
                return case.fixture.read_helper(1)
            if number in (2, 3):
                action = json.loads(case.fixture.read_helper(number))
                action["findings"] = [{"text": case.helper_claim(number - 1)
                    + "该业务处理辅助计算，保持已取得的源码流程。" * 170,
                    "evidence_ids": [case.identifiers[f"helper-{number - 1}.cbl"]]}]
                return json.dumps(action, ensure_ascii=False)
            return case.final_answer(payload)

        result = case.fixture.ask(respond, policy=self.policy, request_guard=4)
        final = case.fixture.requests[-1]
        # Previously both the latest source and the oldest finding were lost.
        self.assertIn("helper-3.cbl", {page["relative_path"] for page in case.fixture.pages(final)})
        blocks = final["prior_source_memory"]["blocks"]
        for number in (1, 2):
            identifier = case.identifiers[f"helper-{number}.cbl"]
            self.assertIn(identifier, {value for block in blocks for value in block["evidence_ids"]})
            self.assertIn(case.helper_claim(number), result["answer"])
            # The model must receive the old business operation itself, not
            # only a citation ID that a scripted responder already knows.
            visible_text = "\n".join(block["text"] for block in blocks)
            self.assertTrue(f"MOVE {number} TO HELPER-RESULT." in visible_text
                            or case.helper_claim(number) in visible_text)
        self.assertTrue(any(block["kind"] == "source" for block in blocks))
        self.assertTrue(all(block["verification"] == "unverified" for block in blocks))
        self.assertEqual(final["prior_source_memory"]["confirmed_facts"], [])
        self.assertFalse(result["claims_semantically_verified"])
        self.assertFalse(any(item["reason"] == "invalid_citation_claim_review" for item in result["boundaries"]))

        trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text())
        saved = RunStore(case.fixture.root / "investigation-runs.sqlite").load(result["run_id"])
        checkpoint = saved["checkpoint"]["prior_source_memory"]
        self.assertFalse(checkpoint["pending_reread_ids"])
        self.assertEqual(len([block for block in checkpoint["blocks"] if block["kind"] == "finding"]), 2)
        for block in blocks:
            for origin in block["origins"]:
                supplying = next(row for row in trace["rounds"] if row["round_id"] == origin["round_id"])
                self.assertEqual(origin["request_body_sha256"], supplying["request_body_sha256"])
        memory = PriorSourceMemory(case.fixture.database, question=case.fixture.question, run_id=result["run_id"])
        memory.scope = checkpoint["scope"]
        memory.restore(checkpoint, trace["rounds"])
        self.assertTrue(memory.effective_pages(final, verify_archive=True))

    def test_many_reworded_findings_do_not_evict_new_source_or_create_extra_rounds(self):
        case = self.case

        def respond(payload, number):
            case.capture(payload)
            if number == 1:
                return case.fixture.read_helper(1)
            if number == 2:
                action = json.loads(case.fixture.read_helper(2))
                action["findings"] = [{"text": f"记录 {index}：" + "辅助计算的说明。" * 400,
                    "evidence_ids": [case.identifiers["helper-1.cbl"]]} for index in range(8)]
                return json.dumps(action, ensure_ascii=False)
            return case.final_answer(payload)

        result = case.fixture.ask(respond, policy=self.policy, request_guard=3)
        final = case.fixture.requests[-1]
        self.assertIn("helper-2.cbl", {page["relative_path"] for page in case.fixture.pages(final)})
        self.assertLessEqual(len(final["prior_source_memory"]["blocks"]), 2)
        self.assertIn("MOVE 1 TO HELPER-RESULT.", "\n".join(
            block["text"] for block in final["prior_source_memory"]["blocks"]))
        self.assertIn(case.helper_claim(1), result["answer"])
        self.assertIn(case.helper_claim(2), result["answer"])
        self.assertEqual(len(case.fixture.requests), 3)

    def test_replaced_draft_still_resupplies_source_once_under_request_limit(self):
        # This scenario needs five existing investigation requests; neither
        # compaction nor the pending source bookmark may add a sixth request.
        self.case.policy = AgentPolicy(max_source_characters=2000,
            max_complete_source_characters=2000, max_request_bytes=32768,
            max_model_requests=5)
        self.case.test_rewritten_draft_recovers_evicted_source_on_the_next_existing_turn()


if __name__ == "__main__":
    unittest.main()
