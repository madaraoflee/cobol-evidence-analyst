"""Compaction observes actual source text and never repairs a partial task."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from agent_policy import AgentPolicy
from analysis_compaction import analysis_progress, compaction_input_ids, compaction_request, encoded_request_size
from company_api import CompanyAPIConfig
import test_prior_source_memory as memory_fixtures


class AnalysisCompactionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = memory_fixtures.PriorSourceMemoryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory
        self.page = self.fixture.make_page(start_line=2, end_line=4,
            source_text="\n".join("*> neutral business condition " + str(number) + "x" * 160
                                  for number in range(3)))
        self.record = self.fixture.round_record([self.page])
        self.memory.remember_supplied_sources(payload={"source_context": [{"pages": [self.page]}]},
                                             round_record=self.record)
        self.policy = AgentPolicy(max_source_characters=512)
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model", api_key="offline-only")

    def selected_ids(self, pages):
        return compaction_input_ids(self.memory, pages, self.policy)

    def test_only_actual_original_slice_with_matching_provenance_prevents_eviction(self):
        self.assertEqual(self.selected_ids([self.page]), [])
        expanded = {**self.page, "start_line": 1, "end_line": 5,
                    "source_text": "*> preceding line\n" + self.page["source_text"] + "\n*> following line"}
        self.assertEqual(self.selected_ids([expanded]), [])
        for mutation in ("text", "missing_text", "short_text", "truncated", "version", "copy"):
            changed = deepcopy(self.page)
            if mutation == "text":
                changed["source_text"] = changed["source_text"].replace("condition", "different")
            elif mutation == "missing_text":
                changed.pop("source_text")
            elif mutation == "short_text":
                changed["source_text"] = changed["source_text"].split("\n")[0]
            elif mutation == "truncated":
                changed["span_truncated"] = True
            elif mutation == "version":
                changed["source_sha256"] = "f" * 64
            else:
                changed["copy_instance"] = "another-instance"
            with self.subTest(mutation=mutation):
                self.assertEqual(self.selected_ids([changed]), [self.page["evidence_id"]])

    def test_truncated_tail_does_not_hide_an_earlier_complete_slice(self):
        expanded = {**self.page, "start_line": 1, "end_line": 5, "span_truncated": True,
                    "source_text": "*> preceding line\n" + self.page["source_text"] + "\n*> cut"}
        self.assertEqual(self.selected_ids([expanded]), [])

    def test_pending_task_and_small_context_do_not_start_another_compaction(self):
        self.assertEqual(compaction_input_ids(self.memory, [], AgentPolicy()), [])
        self.assertEqual(self.selected_ids([]), [self.page["evidence_id"]])
        task = self.memory.prepare_analysis_compaction()
        self.assertIsNotNone(task)
        self.assertEqual(self.selected_ids([]), [])

    def test_request_requires_exact_complete_task_source_set_before_encoding(self):
        task = self.memory.prepare_analysis_compaction()
        extra = self.fixture.make_page(relative_path="another-rule.cbl")
        for pages in ([], [self.page, self.page], [self.page, extra], [extra], [None],
                      [{**self.page, "evidence_id": []}]):
            with self.subTest(pages=pages), self.assertRaisesRegex(ValueError, "SOURCE_SET_INVALID"):
                compaction_request(self.fixture.question, task, pages, self.config, self.policy)
        payload, messages, size = compaction_request(self.fixture.question, task, [self.page],
                                                   self.config, self.policy)
        self.assertEqual(payload["analysis_compaction"], task)
        self.assertEqual(payload["source_context"][0]["pages"], [self.page])
        self.assertEqual(json.loads(messages[-1]["content"]), payload)
        self.assertEqual(size, encoded_request_size(self.config, messages))
        self.assertLessEqual(size, self.policy.max_request_bytes)

    def test_oversized_request_is_rejected_without_trimming_task_or_source(self):
        task = self.memory.prepare_analysis_compaction()
        before = deepcopy(task)
        with self.assertRaisesRegex(ValueError, "REQUEST_TOO_LARGE"):
            compaction_request("问题" * 20000, task, [self.page], self.config,
                               AgentPolicy(max_request_bytes=32768))
        self.assertEqual(task, before)
        self.assertEqual(self.memory.analysis_ledger.pending_task, before)

    def test_progress_failure_preserves_candidates_and_does_not_emit_raw_reasoning(self):
        task = self.memory.prepare_analysis_compaction()
        running = analysis_progress(self.memory, revision=1, round_number=2,
                                    phase="compacting", summary="正在保留已读业务规则。")
        self.assertEqual(running["compaction"]["status"], "running")
        self.memory.mark_analysis_compaction_failed(task["task_id"], "provider_failure")
        failed = analysis_progress(self.memory, revision=2, round_number=2,
                                   phase="investigate", summary="压缩未完成，原文仍保留。")
        self.assertEqual(failed["compaction"]["status"], "failed")
        self.assertFalse(failed["compaction"]["structural_preservation_verified"])
        self.assertEqual(failed["counts"]["total_sources"], 1)
        self.assertEqual(failed["findings"], [])
        self.assertNotIn("reasoning", json.dumps(failed))

    def test_global_pending_work_is_visible_before_any_summary_exists(self):
        state = analysis_progress(self.memory, revision=1, round_number=1,
            phase="investigate", summary="Following the remaining chain.", investigation={
                "pending_work": {"source_path_count": 5, "source_expansion_count": 2,
                    "rule_path_count": 3, "unknown_count": 999, "source_window_count": True},
                "unprojected_open_gaps": [{"reason": "unresolved_target"}],
                "investigation_complete": False})
        self.assertEqual(state["counts"]["open"], 0)
        self.assertEqual(state["counts"]["global_pending"], 11)
        self.assertIs(state["has_pending_work"], True)
        self.assertEqual(state["findings"], [])
        unknown = analysis_progress(self.memory, revision=2, round_number=1,
            phase="investigate", summary="Checking scope.",
            investigation={"investigation_complete": False})
        self.assertIs(unknown["has_pending_work"], True)
        self.assertEqual(unknown["counts"]["global_pending"], 0)
        missing = analysis_progress(self.memory, revision=3, round_number=1,
            phase="investigate", summary="Restoring scope.")
        self.assertNotIn("has_pending_work", missing)
        self.assertNotIn("global_pending", missing["counts"])


if __name__ == "__main__":
    unittest.main()
