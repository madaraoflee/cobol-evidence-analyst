"""Source-bound semantic results can advance an otherwise unchanged source window."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from agent_policy import AgentPolicy
import analysis_continuation
from analysis_continuation import AnalysisContinuation, semantic_material_keys
import test_completion_driven_chat as fixtures


class SemanticContinuationProgressTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CompletionDrivenChatTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.question = "TOTAL-RULE 的金额如何计算？"
        self.observed = []

    def run_semantic(self, *, chain):
        f = self.fixture
        if chain:
            f.write("root.cbl", "TOTAL-RULE",
                "COMPUTE TOTAL-AMOUNT = BASE-AMOUNT * 2.\n"
                "COMPUTE FINAL-AMOUNT = TOTAL-AMOUNT + 5.\nDISPLAY FINAL-AMOUNT.",
                "01 BASE-AMOUNT PIC 9(5) VALUE 10.\n01 TOTAL-AMOUNT PIC 9(6).\n01 FINAL-AMOUNT PIC 9(6).")

        def respond(payload, number):
            if number <= 3:
                tool = ("backward_slice", "forward_slice", "explain_assignment")[number - 1]
                return json.dumps({tool: [{"relative_path": "root.cbl", "line": 10 if chain else 9,
                                           "fields": ["TOTAL-AMOUNT"]}]})
            return f.cited_answer(payload)

        original = analysis_continuation.semantic_material_keys

        def observe(pages, groups):
            self.observed.append((deepcopy(pages), deepcopy(groups)))
            return original(pages, groups)

        with patch.object(analysis_continuation, "semantic_material_keys", side_effect=observe):
            return f.ask(respond, policy=AgentPolicy(max_model_requests=6), request_guard=4)

    def test_distinct_source_flow_survives_same_complete_file_window(self):
        result = self.run_semantic(chain=True)
        self.assertEqual(len(self.fixture.requests), 4)
        self.assertGreater(self.fixture.requests[2]["investigation_budget"]["business_context_actions_per_turn"], 0)
        self.assertFalse(any(item["reason"] == "no_evidence_progress" for item in result["boundaries"]))
        self.assertIn("金额按基础金额乘以二得到20", result["answer"])
        before = semantic_material_keys(*self.observed[1])
        after = semantic_material_keys(*self.observed[2])
        self.assertTrue(after - before)
        self.assertTrue(any(key.startswith("semantic-link:") for key in after - before))

    def test_repeated_single_node_graph_still_stops_with_finite_requests(self):
        result = self.run_semantic(chain=False)
        self.assertEqual(len(self.fixture.requests), 4)
        self.assertEqual(self.fixture.requests[-1]["investigation_budget"]["business_context_actions_per_turn"], 0)
        self.assertTrue(any(item["reason"] == "no_evidence_progress" for item in result["boundaries"]))
        self.assertIn("金额按基础金额乘以二得到20", result["answer"])

    def test_changed_temporary_ids_or_tool_names_do_not_create_progress(self):
        self.run_semantic(chain=True)
        pages, groups = self.observed[2]
        expected = semantic_material_keys(pages, groups)
        identifiers = {node["occurrence_id"] for group in groups
                       for node in group.get("semantic_analysis", {}).get("slice_statements", [])}
        changed = json.dumps(groups)
        for number, identifier in enumerate(sorted(identifiers)):
            changed = changed.replace(identifier, f"temporary-node-{number}")
        changed = changed.replace("backward_slice", "different_tool").replace("forward_slice", "renamed_tool")
        self.assertEqual(semantic_material_keys(pages, json.loads(changed)), expected)
        tracker = AnalysisContinuation("same source")
        self.assertTrue(tracker.observe(pages, [], {}, semantic_groups=groups))
        restored = AnalysisContinuation("same source", tracker.checkpoint())
        for _ in range(2):
            restored.awaiting_tools = True
            self.assertFalse(restored.observe(pages, [], {}, semantic_groups=json.loads(changed)))
        self.assertEqual(restored.stop_reason, "no_evidence_progress")
        # No bound source, or semantic groups removed by fitting, contributes
        # no semantic progress even if the local tool ran successfully.
        self.assertFalse(semantic_material_keys([], groups))
        self.assertFalse(semantic_material_keys(pages, []))
        changed_pages = [{**page, "source_sha256": "unmatched"} for page in pages]
        self.assertFalse(semantic_material_keys(changed_pages, groups))


if __name__ == "__main__":
    unittest.main()
