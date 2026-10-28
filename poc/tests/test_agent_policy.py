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
    def test_bounded_defaults_preserve_context_and_adaptive_mode(self):
        policy = resolve_agent_policy()
        self.assertEqual(policy.max_model_requests, 8)
        self.assertIsNone(policy.max_question_seconds)
        self.assertIsNone(policy.hard_question_seconds)
        self.assertIsNone(policy.delivery_target_seconds)
        self.assertIsNone(policy.effective_delivery_target_seconds)
        self.assertIsNone(policy.max_initial_context_seconds)
        self.assertIsNone(policy.max_local_analysis_seconds)
        self.assertEqual((policy.max_semantic_files, policy.max_semantic_source_bytes,
                          policy.max_semantic_expansions, policy.max_evidence_groups,
                          policy.max_business_context_actions_per_turn),
                         (32, 33554432, 4, 12, 2))
        self.assertEqual(policy.finalization_reserve_seconds, 45)
        self.assertEqual(policy.max_investigation_rounds, 6)
        self.assertEqual(policy.investigation_mode, "adaptive")
        self.assertTrue(policy.source_memory_enabled)
        self.assertEqual(policy.max_request_bytes, 230000)
        self.assertEqual(policy.max_source_characters, 36000)
        self.assertIs(resolve_agent_policy(policy), policy)
        self.assertEqual(resolve_agent_policy({"max_model_requests": 2}).max_model_requests, 2)
        with self.assertRaises(FrozenInstanceError):
            policy.max_model_requests = 9

    def test_explicit_null_request_limit_allows_unbounded_requests(self):
        policy = AgentPolicy(max_model_requests=None)
        for used in (0, 5, 32, 1000):
            with self.subTest(used=used):
                self.assertIsNone(policy.remaining_model_requests(used))
                self.assertTrue(policy.can_request(used))
                self.assertFalse(policy.is_last_request(used))
        self.assertEqual(resolve_agent_policy(policy.to_dict()), policy)

    def test_legacy_null_limits_migrate_without_disabling_memory_or_mode(self):
        policy = resolve_agent_policy({"max_model_requests": None, "max_question_seconds": None,
                                       "hard_question_seconds": None})
        self.assertEqual((policy.max_model_requests, policy.max_question_seconds,
                          policy.hard_question_seconds), (None, None, None))
        self.assertEqual(policy.investigation_mode, "adaptive")
        self.assertTrue(policy.source_memory_enabled)
        strict = resolve_agent_policy({"max_model_requests": 2, "max_question_seconds": 7})
        self.assertEqual((strict.max_model_requests, strict.max_question_seconds,
                          strict.hard_question_seconds), (2, 7, 7))
        self.assertTrue(strict.source_memory_enabled)

    def test_mode_and_memory_are_independent_of_budget(self):
        policy = AgentPolicy(investigation_mode="single_pass", source_memory_enabled=False,
                             max_model_requests=2)
        self.assertEqual(policy.investigation_mode, "single_pass")
        self.assertFalse(policy.source_memory_enabled)
        self.assertEqual(policy.max_model_requests, 2)
        for options in ({"investigation_mode": "unknown"}, {"source_memory_enabled": 1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                AgentPolicy(**options)

    def test_counted_revision_allowance_preserves_legacy_zero_and_one(self):
        self.assertEqual(AgentPolicy().answer_revision_limit, 2)
        self.assertEqual(AgentPolicy(max_answer_revisions=0).answer_revision_limit, 0)
        self.assertEqual(AgentPolicy(max_answer_revisions=1).answer_revision_limit, 1)
        self.assertEqual(AgentPolicy(max_no_evidence_revisions=0).answer_revision_limit, 0)
        self.assertEqual(AgentPolicy(max_answer_revisions=1,
                                    max_no_evidence_revisions=0).answer_revision_limit, 0)
        for options in ({"max_answer_revisions": 2}, {"max_no_evidence_revisions": 3},
                        {"max_investigation_rounds": 33}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                AgentPolicy(**options)

    def test_optional_investigation_round_limit_preserves_explicit_values(self):
        for limit in (None, 0, 1, 5, 8, 32):
            with self.subTest(limit=limit):
                policy = AgentPolicy(max_investigation_rounds=limit)
                self.assertEqual(policy.max_investigation_rounds, limit)
                self.assertEqual(resolve_agent_policy(policy.to_dict()), policy)
        for value in (-1, True, 1.5, "5", 33):
            with self.subTest(value=value), self.assertRaises(ValueError):
                AgentPolicy(max_investigation_rounds=value)

    def test_explicit_deep_deadline_is_bounded_at_five_minutes(self):
        self.assertEqual(AgentPolicy(max_question_seconds=300,
                                    hard_question_seconds=300).hard_question_seconds, 300)
        self.assertEqual(AgentPolicy(max_question_seconds=1800,
                                    hard_question_seconds=1800).hard_question_seconds, 300)

    def test_explicit_deadlines_are_preserved_without_a_default_timer(self):
        for options, expected in (({"max_question_seconds": 180}, 180),
                                  ({"hard_question_seconds": 120}, 120),
                                  ({"max_question_seconds": 300}, 300),
                                  ({"hard_question_seconds": 300}, 300),
                                  ({"max_question_seconds": 90, "hard_question_seconds": 120}, 90),
                                  ({"max_question_seconds": 120, "hard_question_seconds": 7}, 7)):
            with self.subTest(options=options):
                policy = resolve_agent_policy(options)
                self.assertEqual((policy.max_question_seconds, policy.hard_question_seconds), (expected, expected))
                self.assertEqual(policy.effective_delivery_target_seconds, expected)
                self.assertEqual(resolve_agent_policy(policy.to_dict()), policy)

    def test_standalone_delivery_target_does_not_create_a_hard_deadline(self):
        policy = AgentPolicy(delivery_target_seconds=120)
        self.assertEqual(policy.effective_delivery_target_seconds, 120)
        self.assertIsNone(policy.max_question_seconds)
        self.assertIsNone(policy.hard_question_seconds)
        self.assertEqual(resolve_agent_policy(policy.to_dict()), policy)

    def test_explicit_null_target_disables_the_default_without_removing_hard_deadlines(self):
        for options, expected in (({"delivery_target_seconds": None}, None),
                                  ({"delivery_target_seconds": None, "hard_question_seconds": 120}, 120)):
            with self.subTest(options=options):
                policy = AgentPolicy(**options)
                self.assertEqual(policy.effective_delivery_target_seconds, expected)
                self.assertEqual(resolve_agent_policy(options), policy)
                self.assertEqual(resolve_agent_policy(policy.to_dict()), policy)

    def test_explicit_finite_request_override_is_preserved(self):
        policy = AgentPolicy(max_model_requests=8)
        self.assertEqual(policy.max_model_requests, 8)
        self.assertTrue(policy.is_last_request(7))
        self.assertFalse(policy.can_request(8))

    def test_explicit_request_limit_reserves_final_request(self):
        policy = AgentPolicy(max_model_requests=2)
        self.assertEqual(policy.remaining_model_requests(0), 2)
        self.assertTrue(policy.can_request(0))
        self.assertFalse(policy.is_last_request(0))
        self.assertEqual(policy.remaining_model_requests(1), 1)
        self.assertTrue(policy.is_last_request(1))
        for used in (2, 3):
            with self.subTest(used=used):
                self.assertEqual(policy.remaining_model_requests(used), 0)
                self.assertFalse(policy.can_request(used))
                self.assertFalse(policy.is_last_request(used))

    def test_optional_limits_reject_invalid_values(self):
        for field, maximum in (("max_model_requests", 32), ("max_question_seconds", 1800),
                               ("delivery_target_seconds", 1800),
                               ("max_initial_context_seconds", 120),
                               ("max_local_analysis_seconds", 300)):
            for value in (0, -1, maximum + 1, True, 1.5, "5"):
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    resolve_agent_policy({field: value})

    def test_explicit_local_timers_round_trip_independently(self):
        for initial, local in ((None, None), (3, 15), (7, None), (None, 9), (120, 300)):
            with self.subTest(initial=initial, local=local):
                policy = AgentPolicy(max_initial_context_seconds=initial,
                                     max_local_analysis_seconds=local)
                self.assertEqual(resolve_agent_policy(policy.to_dict()), policy)
                self.assertEqual((policy.max_initial_context_seconds,
                                  policy.max_local_analysis_seconds), (initial, local))
                self.assertIsNone(policy.hard_question_seconds)

    def test_explicit_delivery_target_is_separate_and_cannot_extend_total_deadline(self):
        policy = AgentPolicy(max_question_seconds=120, delivery_target_seconds=1)
        self.assertEqual(policy.effective_delivery_target_seconds, 1)
        self.assertEqual((policy.max_question_seconds, policy.hard_question_seconds), (120, 120))
        self.assertEqual(resolve_agent_policy(policy.to_dict()), policy)
        self.assertEqual(AgentPolicy(max_question_seconds=120, delivery_target_seconds=1800)
                         .effective_delivery_target_seconds, 120)
        strict = AgentPolicy(max_question_seconds=1, delivery_target_seconds=120)
        self.assertEqual((strict.effective_delivery_target_seconds, strict.hard_question_seconds), (1, 1))
        legacy = AgentPolicy(max_question_seconds=1)
        self.assertEqual((legacy.effective_delivery_target_seconds, legacy.hard_question_seconds), (1, 1))

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
