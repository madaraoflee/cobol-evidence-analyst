"""One answer cannot earn unlimited output by making tiny changes or replaying."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from answer_continuation_budget import AnswerContinuationBudget


class AnswerContinuationBudgetTests(unittest.TestCase):
    def budget(self, **options):
        return AnswerContinuationBudget(**{
            "source_characters": 12000, "framework_characters": 2000,
            "max_output_tokens": 1024, "max_request_bytes": 32768, **options})

    def test_parameters_reject_noninteger_negative_or_empty_limits(self):
        for name in ("source_characters", "framework_characters", "max_output_tokens", "max_request_bytes"):
            invalid = (-1, True, 1.5, "100", None)
            if name in {"max_output_tokens", "max_request_bytes"}:
                invalid = (*invalid, 0)
            for value in invalid:
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    self.budget(**{name: value})
        empty = self.budget(source_characters=0, framework_characters=0)
        self.assertTrue(empty.can_continue())

    def test_allowance_scales_with_material_and_output_window_without_fixed_round_count(self):
        small = self.budget(source_characters=1000, framework_characters=0)
        large = self.budget(source_characters=30000, framework_characters=2000)
        wide = self.budget(source_characters=30000, framework_characters=2000, max_output_tokens=8192)
        self.assertGreater(large.max_output_characters, small.max_output_characters)
        self.assertGreater(large.max_output_requests, small.max_output_requests)
        self.assertLess(wide.max_output_requests, large.max_output_requests)
        capped = self.budget(source_characters=1000000, framework_characters=1000000)
        at_wire_limit = self.budget(source_characters=32768, framework_characters=0)
        self.assertEqual(capped.max_output_characters, at_wire_limit.max_output_characters)
        self.assertEqual(capped.max_output_requests, at_wire_limit.max_output_requests)

    def test_normal_chinese_and_english_answers_do_not_exhaust_allowance(self):
        for answer in ("先读取余额，再按业务条件计算本次扣减金额，最后写入处理结果。" * 30,
                       "Read the balance, apply the deduction rule, and return the result. " * 30):
            with self.subTest(answer=answer[:12]):
                budget = self.budget()
                budget.record_output(answer, output_id="round-1")
                self.assertTrue(budget.can_continue())
                self.assertIsNone(budget.stop_reason)

    def test_chinese_length_limited_segments_have_room_to_finish(self):
        budget = self.budget(source_characters=1000, framework_characters=0)
        for number in range(4):
            self.assertTrue(budget.can_continue())
            budget.record_output("解释实际业务规则及其结果。" * 70, output_id=f"round-{number}")
        self.assertTrue(budget.can_continue())

    def test_cumulative_original_output_counts_overlap_and_keeps_received_text(self):
        budget = self.budget()
        draft = "The rule reads the current balance. "
        budget.record_output(draft, output_id="round-1")
        repeated = (draft * budget.max_output_characters)[:budget.max_output_characters]
        budget.record_output(repeated, output_id="round-2", made_progress=True)
        self.assertEqual(budget.output_characters, len(draft) + len(repeated))
        self.assertFalse(budget.can_continue())
        self.assertEqual(budget.stop_reason, "answer_output_characters_exhausted")
        # The budget neither slices the returned text nor claims it is complete.
        self.assertEqual(len(repeated), budget.max_output_characters)
        self.assertNotIn("completed", budget.checkpoint())

    def test_tiny_unique_progress_exhausts_derived_request_allowance(self):
        budget = self.budget()
        for number in range(budget.max_output_requests):
            self.assertTrue(budget.can_continue())
            budget.record_output(str(number), output_id=f"round-{number}")
        self.assertLess(budget.output_characters, budget.max_output_characters)
        self.assertFalse(budget.can_continue())
        self.assertEqual(budget.stop_reason, "answer_output_requests_exhausted")

    def test_no_progress_is_terminal_even_when_raw_response_is_nonempty(self):
        budget = self.budget()
        budget.record_output("The business result is known.", output_id="round-1")
        budget.record_output("The business result is known.", output_id="round-2", made_progress=False)
        self.assertFalse(budget.can_continue())
        self.assertEqual(budget.stop_reason, "answer_output_no_progress")
        budget.record_output("A later change.", output_id="round-3")
        self.assertEqual(budget.stop_reason, "answer_output_no_progress")
        empty = self.budget()
        empty.record_output(" \n\t")
        self.assertEqual(empty.stop_reason, "answer_output_no_progress")

    def test_checkpoint_round_trip_and_replay_do_not_charge_same_output_twice(self):
        budget = self.budget()
        budget.record_output("业务结论和已确认的处理规则。", output_id="round-1")
        state = json.loads(json.dumps(budget.checkpoint()))
        restored = self.budget().restore(state)
        self.assertEqual(restored.checkpoint(), state)
        self.assertFalse(restored.record_output("业务结论和已确认的处理规则。",
                                               output_id="round-1", made_progress=False))
        self.assertEqual(restored.checkpoint(), state)
        restored.record_output("继续解释实际读写的业务影响。", output_id="round-2")
        self.assertEqual(restored.output_requests, 2)
        self.assertEqual(restored.output_characters, budget.output_characters + len("继续解释实际读写的业务影响。"))

    def test_restore_with_more_evidence_or_larger_settings_does_not_enlarge_allowance(self):
        budget = self.budget()
        budget.record_output("The first partial answer.", output_id="round-1")
        state = budget.checkpoint()
        larger = self.budget(source_characters=200000, framework_characters=20000,
                             max_output_tokens=8192, max_request_bytes=230000).restore(state)
        self.assertLessEqual(larger.max_output_characters, budget.max_output_characters)
        self.assertLessEqual(larger.max_output_requests, budget.max_output_requests)
        self.assertEqual(larger.output_characters, budget.output_characters)
        self.assertEqual(larger.output_ids, {"round-1"})
        # Reapplying an older checkpoint cannot undo local consumption either.
        larger.record_output("More output.", output_id="round-2")
        larger.restore(state)
        self.assertEqual(larger.output_requests, 2)
        self.assertIn("round-2", larger.output_ids)

    def test_old_journal_or_user_continuation_accounts_for_existing_draft(self):
        for state in (None, {}, {"schema": "old-budget/v0"}):
            with self.subTest(state=state):
                budget = self.budget()
                budget.restore(state, fallback_output_characters=budget.max_output_characters,
                               fallback_output_requests=1)
                self.assertFalse(budget.can_continue())
                self.assertEqual(budget.stop_reason, "answer_output_characters_exhausted")
        restored = self.budget().restore(None, fallback_output_characters=100, fallback_output_requests=1)
        self.assertEqual((restored.output_characters, restored.output_requests), (100, 1))

    def test_restore_ignores_malformed_consumption_and_preserves_no_progress(self):
        budget = self.budget()
        budget.record_output("Repeated text.", output_id="round-1", made_progress=False)
        state = deepcopy(budget.checkpoint())
        state.update(output_characters=-1, output_requests=True, output_ids=[None, "", "round-2"])
        restored = self.budget().restore(state, fallback_output_characters=20, fallback_output_requests=1)
        self.assertEqual(restored.output_characters, 20)
        self.assertEqual(restored.output_requests, 1)
        self.assertEqual(restored.output_ids, {"round-2"})
        self.assertFalse(restored.can_continue())
        self.assertEqual(restored.stop_reason, "answer_output_no_progress")


if __name__ == "__main__":
    unittest.main()
