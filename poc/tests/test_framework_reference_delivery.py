"""Manual interpretations must travel with their applicable limitations."""

from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_analysis import _framework_for_prompt
from business_chat import _complete_framework_reference_groups, _framework_prompt_references, _fit_request
from company_api import CompanyAPIConfig


def reference(identifier, text, **changes):
    return {"reference_id": identifier, "text": text, "heading": "Record access",
        "document_name": "manual.md", "document_sha256": "a" * 64,
        "start_line": 1, "end_line": 2, "selection_reason": "source_marker", **changes}


class FrameworkReferenceDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.rule = reference("fw:rule", "ADVANCE requests the next eligible record.",
                              required_reference_ids=["fw:limit"])
        self.limit = reference("fw:limit", "A successful commit or lock is not implied.",
                               selection_reason="document_constraint")

    def select(self, rows, **changes):
        return _framework_prompt_references({"references": rows}, {}, [],
            replace(AgentPolicy(), **changes), _framework_for_prompt,
            priority_ids=["fw:rule"])[0]

    def test_lookup_priority_retains_both_convention_and_constraints(self):
        distractors = [reference(f"fw:extra-{n}", "Unrelated context " * 40) for n in range(12)]
        rows = self.select([*distractors, self.rule, self.limit])
        self.assertEqual([row["reference_id"] for row in rows[:2]], ["fw:rule", "fw:limit"])
        self.assertEqual(rows[0]["required_reference_ids"], ["fw:limit"])
        self.assertEqual(rows[1]["text"], self.limit["text"])
        self.assertLessEqual(len(rows), 10)
        self.assertLessEqual(sum(len(row["text"]) for row in rows), 8000)

    def test_count_and_character_budgets_cannot_keep_only_the_convention(self):
        for changes in ({"max_framework_references": 1}, {"max_framework_characters": 512}):
            limit = {**self.limit, "text": "Limitation requiring complete source. " * 20}
            rows = self.select([self.rule, limit], **changes)
            self.assertNotIn("fw:rule", {row["reference_id"] for row in rows})

    def test_missing_wrong_version_cycles_and_truncated_constraints_withdraw_rule(self):
        variants = ([], [{**self.limit, "document_sha256": "b" * 64}],
                    [{**self.limit, "document_name": "other.md"}],
                    [{**self.limit, "text_truncated": True}],
                    [{**self.limit, "required_reference_ids": ["fw:rule"]}])
        for variant in variants:
            with self.subTest(variant=variant):
                rows = self.select([self.rule, *variant])
                self.assertNotIn("fw:rule", {row["reference_id"] for row in rows})

    def test_request_trimming_removes_dependent_rule_but_keeps_unrelated_context(self):
        other = reference("fw:other", "Another source-bound reference.")
        rows = self.select([self.rule, self.limit, other])
        trimmed = [row for row in rows if row["reference_id"] != "fw:limit"]
        self.assertEqual([row["reference_id"] for row in _complete_framework_reference_groups(trimmed)],
                         ["fw:other"])

    def test_existing_source_truncation_cannot_be_relabelled_complete(self):
        row, = _framework_for_prompt({"references": [{**self.limit, "text_truncated": True}]})
        self.assertTrue(row["text_truncated"])

    def test_legacy_unlinked_background_without_a_citation_id_remains_compatible(self):
        row = {"text": "Unlinked background remains non-citable context."}
        self.assertEqual(_complete_framework_reference_groups([row]), [row])
        self.assertEqual(_complete_framework_reference_groups([{**row, "required_reference_ids": ["fw:missing"]}]), [])

    def test_unlinked_reference_still_uses_bounded_excerpt(self):
        row, = self.select([reference("fw:other", "readable " * 300)], max_framework_characters=512)
        self.assertEqual(len(row["text"]), 512)
        self.assertTrue(row["text_truncated"])

    def test_actual_request_budget_cannot_orphan_constraints_or_lose_user_audience(self):
        payload = {"question": "帮我分析这个程序", "task": "T" * 20000,
            "source_context": [{"pages": [], "notices": [], "call_chain": {"links": []}, "outline": []}],
            "framework_references": [{**self.rule, "text": "R" * 2300}, {**self.limit, "text": "L" * 2300}],
            "business_map": {}, "evidence_groups": [], "repository": {},
            "business_analysis_brief_omitted": True, "budget_wire_compact": True}
        config = CompanyAPIConfig("https://example.invalid/v1", "test-model", api_key="offline")
        events = []
        messages, size = _fit_request(config, payload, [], AgentPolicy(max_request_bytes=32768), trim_events=events)
        wire = json.loads(messages[-1]["content"])
        self.assertLessEqual(size, 32768)
        self.assertEqual(wire["framework_references"], [])
        self.assertIn("business_explanation", wire["response_contract"])
        self.assertTrue(any(row["reason"] == "framework_constraint_not_supplied" for row in events))


if __name__ == "__main__":
    unittest.main()
