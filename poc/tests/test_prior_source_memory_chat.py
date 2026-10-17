"""End-to-end memory under natural source-window eviction, without model/network IO."""

import json
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from agent_policy import AgentPolicy
from run_store import RunStore
import test_completion_driven_chat as chat_fixtures


class PriorSourceMemoryEvictionChatTests(unittest.TestCase):
    def setUp(self):
        self.fixture = chat_fixtures.CompletionDrivenChatTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.question = "root.cbl 的金额如何计算，输入初值来自哪里？"
        # Each helper fits alongside the root. Both helpers cannot fit together;
        # use real source selection, rather than mocking its output or manifest.
        for number in (1, 2):
            self.fixture.write(f"helper-{number}.cbl", f"HELPER-{number}",
                f"MOVE {number} TO HELPER-RESULT.\n" + "\n".join(
                    f"*> neutral padding line {line}" for line in range(45)),
                "01 HELPER-RESULT PIC 9.")
        self.policy = AgentPolicy(max_source_characters=2000, max_complete_source_characters=2000)
        self.identifiers = {}

    def capture(self, payload):
        for page in self.fixture.pages(payload):
            self.identifiers[page["relative_path"]] = page["evidence_id"]

    def helper_claim(self, number):
        return (f"helper-{number}.cbl 将{number}写入 HELPER-RESULT。"
                f"[{self.identifiers[f'helper-{number}.cbl']}]")

    def final_answer(self, payload):
        return self.fixture.cited_answer(payload) + "\n\n" + "\n\n".join(
            self.helper_claim(number) for number in (1, 2))

    def assert_eviction(self, *, last_round):
        requests = self.fixture.requests
        self.assertEqual(len(requests), last_round)
        self.assertEqual({page["relative_path"] for page in self.fixture.pages(requests[1])},
                         {"root.cbl", "helper-1.cbl"})
        self.assertEqual({page["relative_path"] for page in self.fixture.pages(requests[-1])},
                         {"root.cbl", "helper-2.cbl"})
        for payload in requests:
            self.assertEqual(payload["question"], self.fixture.question)
            self.assertLessEqual(sum(len(page["source_text"]) for page in self.fixture.pages(payload)), 2000)

    def assert_inherited_answer(self, result):
        old_id = self.identifiers["helper-1.cbl"]
        blocks = self.fixture.requests[-1]["prior_source_memory"]["blocks"]
        self.assertIn(old_id, {identifier for block in blocks for identifier in block["evidence_ids"]})
        self.assertTrue(all(block["verification"] == "unverified" for block in blocks))
        for number in (1, 2):
            self.assertIn(self.helper_claim(number), result["answer"])
        self.assertIn("初值为10", result["answer"])
        self.assertIn("乘以二得到20", result["answer"])
        self.assertIn(old_id, {item["evidence_id"] for item in result["narrative"]["citations"]})
        self.assertFalse(any(item["reason"] == "invalid_citation_claim_review" for item in result["boundaries"]))
        self.assertEqual(result["metrics"]["current_source_pages"], 2)
        self.assertEqual(result["metrics"]["prior_memory_source_pages"], 1)
        self.assertFalse(result["claims_semantically_verified"])

    def assert_unremembered_source_rejected(self, result):
        old_id = self.identifiers["helper-1.cbl"]
        self.assertNotIn(old_id, {identifier for block in self.fixture.requests[-1].get(
            "prior_source_memory", {}).get("blocks", []) for identifier in block["evidence_ids"]})
        self.assertNotIn(old_id, {item["evidence_id"] for item in result["narrative"]["citations"]})
        self.assertNotIn(f"[{old_id}]", result["answer"])
        self.assertTrue(any(old_id in item.get("unsupported_citation_ids", []) for item in result["boundaries"]))

    def test_draft_keeps_evicted_source_and_later_answer_can_correct_its_interpretation(self):
        draft = []

        def respond(payload, number):
            self.capture(payload)
            if number == 1:
                return self.fixture.read_helper(1)
            if number == 2:
                draft.append(self.fixture.cited_answer(payload, "金额按基础金额乘以二得到。") +
                    "\n\n" + self.helper_claim(1) + " 初步认为所有辅助结果都只能为1。")
                return draft[0]
            if number == 3:
                return self.fixture.read_helper(2)
            return self.final_answer(payload)

        result = self.fixture.ask(respond, policy=self.policy, request_guard=4)
        self.assert_eviction(last_round=4)
        self.assertEqual(self.fixture.requests[2]["draft_answer"], draft[0])
        self.assertEqual(self.fixture.requests[-1]["draft_answer"], draft[0])
        self.assert_inherited_answer(result)
        self.assertNotIn("所有辅助结果都只能为1", result["answer"])

    def test_tool_only_findings_keep_evicted_source_before_any_draft_exists(self):
        def respond(payload, number):
            self.capture(payload)
            if number == 1:
                return self.fixture.read_helper(1)
            if number == 2:
                action = json.loads(self.fixture.read_helper(2))
                action["findings"] = [{"text": self.helper_claim(1),
                    "evidence_ids": [self.identifiers["helper-1.cbl"]]}]
                return json.dumps(action, ensure_ascii=False)
            return self.final_answer(payload)

        result = self.fixture.ask(respond, policy=self.policy, request_guard=3)
        self.assert_eviction(last_round=3)
        self.assertTrue(all("draft_answer" not in payload for payload in self.fixture.requests))
        self.assert_inherited_answer(result)
        trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text())
        block = next(block for block in self.fixture.requests[-1]["prior_source_memory"]["blocks"]
                     if self.identifiers["helper-1.cbl"] in block["evidence_ids"])
        self.assertEqual(block["kind"], "finding")
        self.assertEqual(block["origins"][0]["round_id"], "round-2")
        supplying_round = next(item for item in trace["rounds"] if item["round_id"] == "round-2")
        self.assertEqual(block["origins"][0]["request_body_sha256"], supplying_round["request_body_sha256"])
        checkpoint = RunStore(self.fixture.root / "investigation-runs.sqlite").load(result["run_id"])["checkpoint"]
        self.assertTrue(any(item["kind"] == "finding" for item in checkpoint["prior_source_memory"]["blocks"]))

    def test_seen_source_without_findings_is_not_implicitly_authorized(self):
        def respond(payload, number):
            self.capture(payload)
            return self.fixture.read_helper(number) if number <= 2 else self.final_answer(payload)

        result = self.fixture.ask(respond, policy=self.policy, request_guard=3)
        self.assert_eviction(last_round=3)
        self.assertEqual(self.fixture.requests[-1]["prior_source_memory"]["blocks"], [])
        self.assert_unremembered_source_rejected(result)

    def test_mixed_valid_and_unsupplied_finding_does_not_authorize_either(self):
        def respond(payload, number):
            self.capture(payload)
            if number == 1:
                return self.fixture.read_helper(1)
            if number == 2:
                action = json.loads(self.fixture.read_helper(2))
                action["findings"] = [{"text": self.helper_claim(1),
                    "evidence_ids": [self.identifiers["helper-1.cbl"], "ev_page_unsupplied"]}]
                return json.dumps(action, ensure_ascii=False)
            return self.final_answer(payload)

        result = self.fixture.ask(respond, policy=self.policy, request_guard=3)
        self.assert_eviction(last_round=3)
        self.assertEqual(self.fixture.requests[-1]["prior_source_memory"]["blocks"], [])
        self.assert_unremembered_source_rejected(result)

    def test_explicit_finite_policy_keeps_current_request_citation_contract(self):
        def respond(payload, number):
            self.capture(payload)
            return self.fixture.read_helper(number) if number <= 2 else self.final_answer(payload)

        result = self.fixture.ask(respond, policy=AgentPolicy(max_model_requests=3,
            max_source_characters=2000, max_complete_source_characters=2000), request_guard=3)
        self.assert_eviction(last_round=3)
        self.assertTrue(all("prior_source_memory" not in payload for payload in self.fixture.requests))
        self.assertFalse(result["metrics"]["completion_driven"])
        self.assert_unremembered_source_rejected(result)


if __name__ == "__main__":
    unittest.main()
