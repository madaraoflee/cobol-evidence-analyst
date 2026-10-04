"""Request trimming rechecks obligations only when supporting material changes."""

import hashlib
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
import business_chat
from company_api import CompanyAPIConfig


class RequestFitLatencyTests(unittest.TestCase):
    def setUp(self):
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
                                       api_key="offline-only")
        self.policy = AgentPolicy(max_request_bytes=32768)

    @staticmethod
    def page(identifier, path, text, *, priority=False):
        return {"evidence_id": identifier, "relative_path": path,
            "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "start_line": 1, "end_line": len(text.splitlines()), "source_text": text,
            "semantic_roles": ["condition"] if priority else [], "selection_reasons": []}

    def payload(self, pages):
        return {"question": "金额如何计算？", "repository": {}, "business_map": {},
            "source_context": [{"pages": pages, "call_chain": {"links": [], "omitted_links": 0},
                                "outline": [], "notices": []}], "framework_references": []}

    @staticmethod
    def investigation(satisfied, identifiers, *, kind="formula"):
        return {"can_answer": satisfied, "required_items": [{"kind": kind,
            "status": "SATISFIED" if satisfied else "UNRESOLVED",
            "evidence_ids": identifiers if satisfied else [],
            "reason": "visible_support" if satisfied else "support_not_visible"}],
            "open_gaps": [] if satisfied else [{"kind": kind, "reason": "support_not_visible"}]}

    def fit(self, payload, builder, **kwargs):
        trims = []
        messages, size = business_chat._fit_request(self.config, payload, [], self.policy,
            trim_events=trims, investigation_builder=builder, **kwargs)
        actual = json.loads(messages[-1]["content"])
        encoded = json.dumps({"model": self.config.chat_model, "messages": messages,
            "max_tokens": self.config.max_output_tokens}, ensure_ascii=False,
            separators=(",", ":")).encode("utf-8")
        self.assertEqual(size, len(encoded))
        self.assertLessEqual(len(encoded), self.policy.max_request_bytes)
        return actual, trims

    def test_many_navigation_trims_do_not_repeat_source_investigation(self):
        page = self.page("ev:formula", "rule.cbl", "COMPUTE RESULT-AMOUNT = INPUT-AMOUNT * 2.")
        payload = self.payload([page])
        payload["source_context"][0]["open_frontier"] = [
            {"reason": "neutral_navigation", "detail": "unselected catalog entry " * 180}
            for _ in range(20)]
        calls = []

        def builder(pages):
            calls.append([item["evidence_id"] for item in pages])
            return self.investigation("ev:formula" in calls[-1], ["ev:formula"])

        actual, trims = self.fit(payload, builder)
        self.assertGreaterEqual(sum(item["reason"] == "source_selection_metadata" for item in trims), 10)
        self.assertEqual(calls, [["ev:formula"]])
        self.assertEqual(actual["source_context"][0]["pages"], [page])
        self.assertTrue(actual["question_investigation"]["can_answer"])

    def test_removed_source_rechecks_formula_and_physical_completeness(self):
        formula = self.page("ev:formula", "rule.cbl", "COMPUTE RESULT-AMOUNT = INPUT-AMOUNT * 2.\n"
            + "*> neutral source filler\n" * 2500)
        condition = self.page("ev:condition", "condition.cbl", "IF INPUT-AMOUNT > 0 CONTINUE END-IF.", priority=True)
        payload = self.payload([formula, condition])
        payload["source_context"][0]["working_set"] = {"status": "supplied", "root_paths": [],
            "source_manifest": [{"relative_path": page["relative_path"], "sha256": page["source_sha256"],
                "evidence_id": page["evidence_id"], "line_count": page["end_line"],
                "source_characters": len(page["source_text"])} for page in (formula, condition)]}
        calls = []

        def builder(pages):
            calls.append([item["evidence_id"] for item in pages])
            return self.investigation("ev:formula" in calls[-1], ["ev:formula"])

        actual, trims = self.fit(payload, builder)
        self.assertEqual(calls, [["ev:formula", "ev:condition"], ["ev:condition"]])
        self.assertEqual(actual["question_investigation"]["required_items"][0]["status"], "UNRESOLVED")
        working_set = actual["source_context"][0]["working_set"]
        self.assertFalse(working_set["physical_complete"])
        self.assertEqual(working_set["omitted_complete_paths"], ["rule.cbl"])
        self.assertEqual(working_set["supplied_complete_paths"], ["condition.cbl"])
        self.assertEqual(working_set["transmission_status"], "partial")
        self.assertTrue(any(item["item_id"] == "ev:formula" and item["role"] == "source" for item in trims))

    def test_removed_framework_fact_rechecks_its_dependency_support(self):
        page = self.page("ev:call", "rule.cbl", "CALL 'RECORD-SERVICE' USING INPUT-AMOUNT.")
        payload = self.payload([page])
        payload["framework_references"] = [{"reference_id": "fw:rule", "text": "Read the requested record."}]
        payload["framework_facts"] = [{"fact_id": "fact:read", "relative_path": "rule.cbl",
            "source_sha256": page["source_sha256"], "reference_ids": ["fw:rule"],
            "source_ranges": [{"start_line": 1, "end_line": 1}],
            "operation": {"value": "FETCH", "caveat": "Returned values require interpretation. " * 1800}}]
        calls = []

        def builder(pages, *, framework_facts):
            identifiers = [fact["fact_id"] for fact in framework_facts]
            calls.append(identifiers)
            return self.investigation("fact:read" in identifiers, ["ev:call"], kind="dependencies")

        actual, trims = self.fit(payload, builder)
        self.assertEqual(calls, [["fact:read"], []])
        self.assertEqual(actual["framework_facts"], [])
        self.assertTrue(actual["source_context"][0]["pages"])
        self.assertEqual(actual["question_investigation"]["required_items"][0]["status"], "UNRESOLVED")
        self.assertTrue(any(item["reason"] == "framework_fact_request_bytes" for item in trims))

    def test_removed_framework_reference_invalidates_cached_material(self):
        page = self.page("ev:call", "rule.cbl", "CALL 'RECORD-SERVICE' USING INPUT-AMOUNT.")
        payload = self.payload([page])
        payload["framework_references"] = [{"reference_id": "fw:rule",
            "text": "Read the requested record. " * 2000}]
        calls = []

        def builder(pages):
            identifiers = [ref["reference_id"] for ref in payload["framework_references"]]
            calls.append(identifiers)
            return self.investigation("fw:rule" in identifiers, ["ev:call", "fw:rule"], kind="dependencies")

        actual, trims = self.fit(payload, builder)
        self.assertEqual(calls, [["fw:rule"], []])
        self.assertEqual(actual["framework_references"], [])
        self.assertEqual(actual["question_investigation"]["required_items"][0]["status"], "UNRESOLVED")
        self.assertTrue(any(item["role"] == "framework" and item["item_id"] == "fw:rule" for item in trims))

    def test_truncated_draft_is_reviewed_again_against_the_retained_text(self):
        page = self.page("ev:formula", "rule.cbl", "COMPUTE RESULT-AMOUNT = INPUT-AMOUNT * 2.")
        payload = self.payload([page])
        payload["draft_answer"] = "本金满足条件时参与金额计算。" * 2000 + "最终金额还要加费用。"
        payload["answer_review"] = {}
        reviewed = []

        def review(question, draft, investigation, pages, **kwargs):
            reviewed.append(draft)
            return {"explains_final_amount": "最终金额还要加费用。" in draft}

        with mock.patch.object(business_chat, "assess_business_answer", side_effect=review):
            actual, trims = self.fit(payload, lambda pages: self.investigation(True, ["ev:formula"]))
        self.assertEqual(len(reviewed), 2)
        self.assertTrue(reviewed[0].endswith("最终金额还要加费用。"))
        self.assertEqual(reviewed[-1], actual["draft_answer"])
        self.assertNotIn("最终金额还要加费用。", reviewed[-1])
        self.assertTrue(actual["draft_answer_truncated"])
        self.assertFalse(actual["answer_review"]["explains_final_amount"])
        self.assertTrue(any(item["reason"] == "draft_answer_request_bytes" for item in trims))


if __name__ == "__main__":
    unittest.main()
