"""Tight requests retain incomplete read cursors without completed-read history."""

from copy import deepcopy
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).resolve().parent),
               str(Path(__file__).resolve().parents[1])]

from agent_policy import AgentPolicy
import business_chat
import semantic_scope
import test_complete_working_set as fixtures


class CompletedReadRequestFitTests(unittest.TestCase):
    def test_local_context_timeout_does_not_overflow_request_with_completed_read_history(self):
        fixture = fixtures.CompleteWorkingSetTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.build({
            "root.cbl": fixtures.program("ROOT-RULE", "CALL 'LARGE-RULE'.\n"
                "MOVE 7 TO RESULT-AMOUNT.\n" + "*> 中性业务注释保留原始行\n" * 400),
            "large.cbl": fixtures.program("LARGE-RULE", "*> neutral large context\n" * 2000
                + "MOVE 9 TO RESULT-AMOUNT."),
        })
        prefix = "请详细解释 root.cbl 的业务流程与结果。"
        question = prefix + ("业务" * 3000)[:6000 - len(prefix)]
        clock = time.monotonic
        prepare = semantic_scope.prepare_semantic_scope
        offset = 0

        def expire_initial_context(*args, **kwargs):
            nonlocal offset
            offset = 4
            return prepare(*args, **kwargs)

        with mock.patch.object(semantic_scope, "prepare_semantic_scope", side_effect=expire_initial_context), \
                mock.patch.object(business_chat.time, "monotonic", side_effect=lambda: clock() + offset):
            payload, output, size = fixture.capture(question, policy=AgentPolicy(
                max_request_bytes=32768, max_initial_context_seconds=3))

        self.assertLessEqual(size, 32768)
        self.assertEqual(payload["question"], question)
        bundle = payload["source_context"][0]
        self.assertEqual(bundle["omitted_completed_read_count"], 1)
        self.assertEqual(bundle["working_set"]["status"], "partial")
        self.assertEqual(bundle["working_set"]["transmission_status"], "partial")
        self.assertFalse(bundle["working_set"]["physical_complete"])
        self.assertIn("large.cbl", bundle["working_set"]["omitted_candidate_paths"])
        self.assertIn("root.cbl", bundle["working_set"]["omitted_complete_paths"])
        self.assertTrue(any(item.get("reason") == "local_analysis_budget_reached"
            for item in output["agent_result"]["boundaries"]))

    def test_compact_projection_retains_pending_reads_and_canonical_history(self):
        reads = [{"relative_path": "complete.cbl", "state": "complete", "range_complete": True,
                  "supplied_ranges": [[1, 12]], "next_start_line": None},
                 {"relative_path": "pending.cbl", "state": "open", "range_complete": False,
                  "supplied_ranges": [[1, 5]], "next_start_line": 6},
                 {"relative_path": "unavailable.cbl", "state": "unavailable", "range_complete": False,
                  "supplied_ranges": [], "next_start_line": 1}]
        payload = {"question": "请解释这些程序。", "source_context": [{"pages": [], "open_reads": reads}],
                   "business_map": {}}
        full = business_chat._prompt_payload(payload)
        self.assertEqual(full["source_context"][0]["open_reads"], reads)
        self.assertNotIn("omitted_completed_read_count", full["source_context"][0])
        payload["budget_wire_compact"] = True
        original = deepcopy(payload)
        wire = business_chat._prompt_payload(payload)
        self.assertEqual(wire["source_context"][0]["open_reads"], reads[1:])
        self.assertEqual(wire["source_context"][0]["omitted_completed_read_count"], 1)
        self.assertEqual(payload, original)


if __name__ == "__main__":
    unittest.main()
