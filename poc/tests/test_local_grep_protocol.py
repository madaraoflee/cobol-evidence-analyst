"""Ordinary investigation budgets, literal tools, and conditional answer scope."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import (_action_reply_invalid, _actions, _literal_call_path_gaps,
                           _source_shortage_reply)


class LocalGrepProtocolTests(unittest.TestCase):
    def test_literal_actions_share_search_slots_and_reject_command_objects(self):
        self.assertEqual(_actions('{"grep":"RUN-ID"}')["grep"], ["RUN-ID"])
        action = _actions('{"search":["a"],"grep":["b","b","c"],"read":[{"evidence_id":"ev"}]}',
                          AgentPolicy(max_searches_per_turn=2))
        self.assertEqual(action, {"search": ["a"], "grep": ["b"], "read": [{"evidence_id": "ev"}]})
        for value in ({"command": "ls"}, {"text": "a", "path": "/tmp"}, ["one\ntwo"], [" "],
                      ["a" * 257], [5]):
            message = json.dumps({"grep": value})
            self.assertIsNone(_actions(message))
            self.assertTrue(_action_reply_invalid(message, None))
        self.assertTrue(_action_reply_invalid('{"grep":', None))

    def test_short_source_deferral_preserves_supported_answer_and_qualified_boundaries(self):
        for text in ("源码不足以分析。", "现有的源代码不完整。请提供更多原文。", "Insufficient source code."):
            self.assertTrue(_source_shortage_reply(text), text)
        for text in ("主程序先扣款。被调源码不足以说明运行结果。", "源码不足以证明运行结果，但仍能确认顺序。",
                     "源码不足以确认下游处理。[ev:line]"):
            self.assertFalse(_source_shortage_reply(text), text)

    def test_empty_batches_reach_last_file_in_one_tool_then_supply_its_original(self):
        from test_business_discovery_recovery import BusinessDiscoveryRecoveryTests
        case = BusinessDiscoveryRecoveryTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        events = []
        def reply(payload):
            if len(case.requests) == 1:
                return '{"grep":["COMPUTE FINAL-PREMIUM"]}'
            latest = next(row for row in payload["completed_actions"] if row.get("tool") == "grep")
            self.assertEqual(latest["matches"][0]["relative_path"], "z-calculation.cbl")
            self.assertTrue(latest["complete"])
            self.assertNotIn("next_cursor", latest)
            page = next(page for page in payload["source_context"][0]["pages"]
                        if "COMPUTE FINAL-PREMIUM" in page["source_text"])
            return f'基础金额大于零时，保费等于基础金额乘费率，否则保费为零。[{page["evidence_id"]}]'
        with patch("local_source_grep.MAX_FILES", 2):
            output = case.ask(reply, policy=AgentPolicy(max_model_requests=2), progress=events.append)
        results = [event["source_grep"] for event in events if event.get("source_grep")]
        self.assertGreater(len(results), 5)
        self.assertTrue(all(not row["matches"] and row["has_more"] for row in results[:-1]))
        self.assertEqual(results[-1]["matches"], 1)
        self.assertTrue(results[-1]["complete"])
        self.assertEqual(len(case.requests), 2)
        self.assertIn("乘费率", output["agent_result"]["answer"])

    def test_foreign_fields_missing_bindings_and_capped_batches_remain_open(self):
        source = {"relative_path": "entry.cbl", "source_sha256": "v", "start_line": 1, "end_line": 8}
        proof = {"paths": ["entry.cbl", "worker.cbl"], "inputs": ["AMOUNT"], "result": "DUE",
                 "calls": [{"path": "entry.cbl", "line": 8, "variable": "TARGET"}]}
        rows = [
            {"kind": "inputs", "reason": "input_source_not_located", "status": "PARTIAL",
             "evidence_ids": ["ev"], "fields": ["OTHER"], "missing_count": 0},
            {"kind": "dependencies", "reason": "runtime_target_unresolved", "status": "UNRESOLVED",
             "evidence_ids": ["ev"], "targets": ["TARGET", "OTHER"], "missing_count": 0,
             "unresolved_reasons": ["runtime_target_unresolved"]},
            {"kind": "business_steps", "reason": "business_steps_not_located", "status": "OPEN",
             "candidate_count": 1, "missing_count": 1, "evidence_ids": []}]
        gaps = [{k: row[k] for k in ("kind", "reason", "status")} for row in rows]
        self.assertEqual(_literal_call_path_gaps(gaps, {"required_items": rows}, {}, proof,
            {"ev": source}, [source]), (gaps, []))
        rows[1]["targets"] = ["TARGET"]
        self.assertEqual(_literal_call_path_gaps(gaps, {"required_items": rows}, {}, proof,
            {"ev": {**source, "end_line": 7}}, [source]), (gaps, []))
        self.assertEqual(_literal_call_path_gaps(gaps, {"required_items": rows}, {}, proof,
            {"ev": {**source, "source_sha256": "old"}}, [source]), (gaps, []))


if __name__ == "__main__":
    unittest.main()
