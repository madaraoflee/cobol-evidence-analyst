"""End-to-end memory under natural source-window eviction, without model/network IO."""

import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from agent_policy import AgentPolicy
from company_api import APIClientError
import evidence_answer
from prior_source_memory import PriorSourceMemory
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

    def test_rewritten_draft_recovers_evicted_source_on_the_next_existing_turn(self):
        def respond(payload, number):
            self.capture(payload)
            if number == 1:
                return self.fixture.read_helper(1)
            if number == 2:
                return (self.fixture.cited_answer(payload, "金额按基础金额乘以二得到。")
                        + "\n\n" + self.helper_claim(1))
            if number == 3:
                return self.fixture.read_helper(2)
            if number == 4:
                # A revised paragraph omits the earlier helper interpretation.
                return (self.fixture.cited_answer(payload, "金额按基础金额乘以二得到。")
                        + "\n\n" + self.helper_claim(2))
            return self.final_answer(payload)

        result = self.fixture.ask(respond, policy=self.policy, request_guard=5)
        self.assertEqual(len(self.fixture.requests), 5)
        self.assertEqual({page["relative_path"] for page in self.fixture.pages(self.fixture.requests[3])},
                         {"root.cbl", "helper-2.cbl"})
        final_payload = self.fixture.requests[-1]
        self.assertIn("helper-1.cbl", {page["relative_path"] for page in self.fixture.pages(final_payload)})
        old_id = self.identifiers["helper-1.cbl"]
        self.assertTrue(any(block["kind"] == "source" and old_id in block["evidence_ids"]
                            for block in final_payload["prior_source_memory"]["blocks"]))
        self.assertNotIn(self.helper_claim(1), final_payload["draft_answer"])
        for number in (1, 2):
            self.assertIn(self.helper_claim(number), result["answer"])
        checkpoint = RunStore(self.fixture.root / "investigation-runs.sqlite").load(result["run_id"])["checkpoint"]
        self.assertNotIn(old_id, checkpoint["prior_source_memory"]["pending_reread_ids"])

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

    def test_explicit_finite_policy_keeps_adaptive_memory_and_requires_declared_evidence(self):
        def respond(payload, number):
            self.capture(payload)
            return self.fixture.read_helper(number) if number <= 2 else self.final_answer(payload)

        result = self.fixture.ask(respond, policy=AgentPolicy(max_model_requests=3,
            max_source_characters=2000, max_complete_source_characters=2000), request_guard=3)
        self.assert_eviction(last_round=3)
        self.assertTrue(all("prior_source_memory" in payload for payload in self.fixture.requests))
        self.assertTrue(result["metrics"]["completion_driven"])
        self.assert_unremembered_source_rejected(result)

    def local_failure(self, *, remember=True, corrupt_archive=False):
        original_builder = evidence_answer.build_evidence_answer
        original_archive = PriorSourceMemory._archived_page
        supplied_to_fallback = []

        def respond(payload, number):
            self.capture(payload)
            if number == 1:
                return self.fixture.read_helper(1)
            if number == 2:
                action = json.loads(self.fixture.read_helper(2))
                if remember:
                    action["findings"] = [{"text": self.helper_claim(1),
                        "evidence_ids": [self.identifiers["helper-1.cbl"]]}]
                return json.dumps(action, ensure_ascii=False)
            raise APIClientError("REQUEST_TIMEOUT")

        def archived(memory, identifier):
            if (corrupt_archive and len(self.fixture.requests) >= 3
                    and identifier == self.identifiers.get("helper-1.cbl")):
                raise ValueError("PRIOR_SOURCE_MEMORY_INVALID")
            return original_archive(memory, identifier)

        def render(question, pages, investigation, **options):
            supplied_to_fallback.extend(pages)
            # Isolate evidence delivery from the renderer's question relevance
            # policy: select a real statement from the evicted, declared page.
            retained = [page for page in pages if page["relative_path"] == "helper-1.cbl"]
            if retained:
                return original_builder("helper-1.cbl 的 HELPER-RESULT 怎样赋值？", retained,
                    {"analysis_plan": {"paths": ["helper-1.cbl"], "entities": ["HELPER-RESULT"]}}, **options)
            return original_builder(question, pages, investigation, **options)

        with mock.patch.object(PriorSourceMemory, "_archived_page", archived), \
                mock.patch.object(evidence_answer, "build_evidence_answer", side_effect=render):
            result = self.fixture.ask(respond, policy=self.policy, request_guard=3)
        self.assert_eviction(last_round=3)
        return result, supplied_to_fallback

    def test_local_failure_retains_verified_memory_with_original_request_binding(self):
        result, supplied = self.local_failure()
        old_id = self.identifiers["helper-1.cbl"]
        self.assertIn(old_id, {page["evidence_id"] for page in supplied})
        self.assertEqual(result["answer_origin"], "local_source_facts")
        self.assertFalse(result["model_answer_recorded"])
        self.assertIn(f"[{old_id}]", result["answer"])
        self.assertIn("HELPER-RESULT = 1", result["answer"])
        self.assertEqual(result["metrics"]["current_source_pages"], 0)
        self.assertEqual(result["metrics"]["prior_memory_source_pages"], 1)
        self.assertEqual(result["stop_reason"], "REQUEST_TIMEOUT")
        trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text())
        self.assertIn(old_id, trace["final"]["final_visible_ids"])
        self.assertEqual(trace["final"]["unsupported_citation_ids"], [])

    def test_invalid_archived_memory_is_not_used_for_local_failure_answer(self):
        result, supplied = self.local_failure(corrupt_archive=True)
        old_id = self.identifiers["helper-1.cbl"]
        self.assertNotIn(old_id, {page["evidence_id"] for page in supplied})
        self.assertNotIn(f"[{old_id}]", result["answer"])
        self.assertTrue(any(item.get("reason") == "prior_source_memory_unavailable"
                            and item.get("evidence_id") == old_id for item in result["boundaries"]))

    def test_seen_but_undeclared_memory_is_not_used_for_local_failure_answer(self):
        result, supplied = self.local_failure(remember=False)
        old_id = self.identifiers["helper-1.cbl"]
        self.assertNotIn(old_id, {page["evidence_id"] for page in supplied})
        self.assertNotIn(f"[{old_id}]", result["answer"])


if __name__ == "__main__":
    unittest.main()
