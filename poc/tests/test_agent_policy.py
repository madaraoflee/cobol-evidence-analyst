from dataclasses import FrozenInstanceError
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy, resolve_agent_policy
from business_chat import _actions, _fit_request, _framework_prompt_references, _history_messages, _usage_report
from business_analysis import _framework_for_prompt
from company_api import CompanyAPIConfig


class AgentPolicyTests(unittest.TestCase):
    def test_defaults_preserve_request_cost_and_transport_ceiling(self):
        policy = resolve_agent_policy()
        self.assertEqual(policy.max_model_requests, 5)
        self.assertEqual(policy.max_request_bytes, 230000)
        self.assertEqual(policy.max_source_characters, 36000)
        self.assertIs(resolve_agent_policy(policy), policy)
        self.assertEqual(resolve_agent_policy({"max_model_requests": 2}).max_model_requests, 2)
        with self.assertRaises(FrozenInstanceError):
            policy.max_model_requests = 9

    def test_invalid_configuration_cannot_expand_transport_hard_limit(self):
        for value in ({"max_request_bytes": 256001}, {"max_model_requests": True},
                      {"max_model_requests": 0}, {"max_source_characters": 12},
                      {"unsupported": 1}, "wrong"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                resolve_agent_policy(value)

    def test_action_budgets_and_disabled_tools_remain_actions(self):
        command = '{"search":["alpha","beta"],"read":[{"start_line":1},{"start_line":2}],"framework_search":["first","second"]}'
        result = _actions(command, AgentPolicy(max_searches_per_turn=1, max_reads_per_turn=1,
                                              max_framework_searches_per_turn=1))
        self.assertEqual(result, {"search": ["alpha"], "read": [{"start_line": 1}],
                                  "framework_search": ["first"]})
        disabled = _actions('{"framework_search":["concept"]}',
                            AgentPolicy(max_framework_searches_per_turn=0))
        self.assertIsNotNone(disabled)
        self.assertEqual(disabled["framework_search"], [])

    def test_history_budget_includes_older_question_summary(self):
        history = [{"role": "user", "content": "older topic " * 100},
                   {"role": "assistant", "content": "x" * 1000},
                   {"role": "user", "content": "current follow-up"}]
        retained = _history_messages(history, AgentPolicy(max_history_characters=200))
        self.assertLessEqual(sum(len(message["content"]) for message in retained), 200)
        self.assertEqual(retained[-1]["content"], "current follow-up")
        self.assertEqual(_history_messages(history, AgentPolicy(max_history_characters=0)), [])

    def test_usage_missing_and_partial_counts_are_not_reported_as_zero(self):
        missing = _usage_report([None], 1)
        self.assertFalse(missing["available"])
        self.assertIsNone(missing["total_tokens"])
        partial = _usage_report([{"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
                                 {"prompt_tokens": 7, "completion_tokens": True}], 2)
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["prompt_tokens"], 19)
        self.assertEqual(partial["total_tokens"], 16)
        self.assertEqual(partial["field_reported_requests"]["total_tokens"], 1)

    def test_oversized_latest_answer_retains_recent_question_and_business_context(self):
        question = "Explain the exception rules."
        answer = "Business rule detail. " * 2000
        retained = _history_messages([{"role": "user", "content": question},
                                      {"role": "assistant", "content": answer}])
        self.assertEqual(retained[0], {"role": "user", "content": question})
        self.assertEqual(retained[1]["role"], "assistant")
        self.assertTrue(retained[1]["content"].startswith("Business rule detail."))
        self.assertEqual(sum(len(message["content"]) for message in retained), 18000)
        self.assertFalse(any("Earlier conversation questions" in message["content"] for message in retained))

    def test_oversized_context_is_trimmed_to_configured_envelope_without_losing_question(self):
        config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="test-key")
        payload = {"question": "Explain the current rule.", "source_context": [{
            "pages": [{"source_text": "字段" * 16000}, {"source_text": "规则" * 16000}],
            "call_chain": {"links": [], "omitted_links": 0}, "outline": [], "notices": []}],
            "business_map": {}, "repository": {"program_samples": []},
            "framework_references": [{"text": "说明" * 1200} for _ in range(10)],
            "completed_actions": [{"read": "x" * 4000}], "completed_searches": []}
        _, encoded_bytes = _fit_request(config, payload, [], AgentPolicy(max_request_bytes=32768))
        self.assertLessEqual(encoded_bytes, 32768)
        self.assertTrue(payload["context_reduced"])
        self.assertEqual(payload["question"], "Explain the current rule.")

    def test_new_framework_lookup_and_current_source_match_survive_long_lookup_history(self):
        searched = {f"fw:{index}": {"reference_id": f"fw:{index}", "text": f"Old concept {index}.",
                                   "selection_reason": "framework_search"} for index in range(20)}
        searched["fw:new"] = {"reference_id": "fw:new", "text": "The current requested concept.",
                              "selection_reason": "framework_search"}
        automatic = {"references": [{"reference_id": "fw:source", "text": "The convention used by this source.",
                                       "selection_reason": "source_marker"}]}
        references, _ = _framework_prompt_references(automatic, searched, ["fw:new"],
            AgentPolicy(max_framework_references=2), _framework_for_prompt)
        self.assertEqual([item["reference_id"] for item in references], ["fw:new", "fw:source"])


if __name__ == "__main__":
    unittest.main()
