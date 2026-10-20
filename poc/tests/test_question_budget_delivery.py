"""Question deadlines bound work and retain consumed time across recovery."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy, resolve_agent_policy
from question_budget import QuestionBudget, QuestionDeadlineExceeded


class QuestionBudgetDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0

    def budget(self, seconds=120, **options):
        return QuestionBudget(seconds, clock=lambda: self.now, **options)

    def test_standard_default_reserves_time_and_enforces_hard_deadline(self):
        policy = AgentPolicy()
        budget = self.budget(policy.max_question_seconds, hard_seconds=policy.hard_question_seconds)
        self.now += 135
        self.assertTrue(budget.should_answer(300))
        budget.check()
        self.now += 45
        with self.assertRaises(QuestionDeadlineExceeded):
            budget.check()
        self.assertTrue(budget.target_reached())

    def test_explicit_hard_limit_works_without_completion_target(self):
        budget = self.budget(None, hard_seconds=90)
        budget.observe_request(30)
        self.now += 70
        self.assertIsNone(budget.remaining())
        self.assertFalse(budget.target_reached())
        self.assertTrue(budget.should_answer(60))
        self.assertEqual(budget.request_timeout(60), 20)
        self.now += 20
        with self.assertRaises(QuestionDeadlineExceeded):
            budget.check()

    def test_cancellation_still_works_without_question_timer(self):
        cancelled = RuntimeError("User cancelled the investigation")

        def cancel():
            raise cancelled

        budget = self.budget(None, check_cancel=cancel)
        with self.assertRaises(RuntimeError) as caught:
            budget.check()
        self.assertIs(caught.exception, cancelled)

    def test_first_answer_can_arrive_after_completion_target(self):
        budget = self.budget()
        self.now += 121
        budget.check()
        self.assertEqual(budget.remaining(), 0)
        self.assertTrue(budget.target_reached())
        self.assertIsNone(budget.hard_remaining())
        self.assertTrue(budget.should_answer(60))
        self.assertEqual(budget.request_timeout(60), 60)

    def test_target_remaining_does_not_shorten_provider_allowance(self):
        budget = self.budget()
        self.now += 115
        self.assertEqual(budget.remaining(), 5)
        self.assertEqual(budget.request_timeout(60), 60)

    def test_provider_allowance_is_not_increased(self):
        budget = self.budget()
        self.assertEqual(budget.request_timeout(7), 7)
        self.now += 121
        self.assertEqual(budget.request_timeout(7), 7)

    def test_first_discovery_is_allowed_before_target(self):
        budget = self.budget()
        self.assertFalse(budget.should_answer(300))
        self.now += 74
        self.assertFalse(budget.should_answer(300))
        self.now += 1
        self.assertTrue(budget.should_answer(300))

    def test_observed_request_wait_reserves_answer_before_target(self):
        budget = self.budget()
        budget.observe_request(55)
        self.now += 70
        self.assertFalse(budget.target_reached())
        self.assertTrue(budget.should_answer(60))
        budget.check()
        self.assertEqual(budget.request_timeout(60), 60)

    def test_next_planning_wait_and_final_answer_are_reserved_separately_from_local_reads(self):
        budget = self.budget(180, hard_seconds=180)
        budget.observe_request(60)
        self.now += 60
        self.assertTrue(budget.should_answer(300))
        self.assertFalse(budget.should_stop_local_investigation())
        self.assertEqual(budget.request_timeout(300), 120)
        self.now += 60
        self.assertTrue(budget.should_stop_local_investigation())

    def test_explicit_hard_limit_bounds_request_and_active_wait(self):
        budget = self.budget(hard_seconds=30)
        self.now += 23
        self.assertEqual(budget.hard_remaining(), 7)
        self.assertEqual(budget.request_timeout(60), 7)
        self.now += 7
        self.assertFalse(budget.target_reached())
        with self.assertRaises(QuestionDeadlineExceeded):
            budget.check()
        with self.assertRaises(QuestionDeadlineExceeded):
            budget.request_timeout(60)

    def test_longer_hard_limit_does_not_turn_target_into_cancellation(self):
        budget = self.budget(hard_seconds=180)
        self.now += 121
        budget.check()
        self.assertTrue(budget.should_answer(60))
        self.assertEqual(budget.request_timeout(60), 59)

    def test_explicit_hard_limit_does_not_expand_transport_timeout(self):
        budget = self.budget(hard_seconds=180)
        self.assertEqual(budget.request_timeout(20), 20)

    def test_cancel_only_callback_does_not_consume_hard_limit(self):
        calls = []
        budget = self.budget(hard_seconds=1, check_cancel=lambda: calls.append(True))
        self.now += 2
        budget.check_cancel()
        self.assertEqual(calls, [True])
        with self.assertRaises(QuestionDeadlineExceeded):
            budget.check()

    def test_user_cancellation_wins_over_both_time_limits(self):
        cancelled = RuntimeError("User cancelled the investigation")

        def cancel():
            raise cancelled

        budget = self.budget(hard_seconds=1, check_cancel=cancel)
        self.now += 121
        for check in (budget.check_cancel, budget.check, lambda: budget.request_timeout(60)):
            with self.assertRaises(RuntimeError) as caught:
                check()
            self.assertIs(caught.exception, cancelled)

    def test_legacy_policy_preserves_stricter_time_limit(self):
        policy = resolve_agent_policy({"max_question_seconds": 7})
        self.assertEqual(policy.max_question_seconds, 7)
        self.assertEqual(policy.hard_question_seconds, 7)
        self.assertEqual(policy.to_dict()["hard_question_seconds"], 7)

    def test_policy_soft_target_does_not_cancel_request_before_finite_hard_deadline(self):
        policy = AgentPolicy(max_question_seconds=120, delivery_target_seconds=1)
        budget = self.budget(policy.effective_delivery_target_seconds, hard_seconds=policy.hard_question_seconds)
        self.now += 1.25
        self.assertTrue(budget.target_reached())
        budget.check()
        self.assertEqual(budget.request_timeout(60), 60)
        self.now += 118.75
        with self.assertRaises(QuestionDeadlineExceeded):
            budget.check()

    def test_soft_target_checkpoint_restore_cannot_extend_target_or_hard_deadline(self):
        wall = [1000.0]
        policy = AgentPolicy(max_question_seconds=120, delivery_target_seconds=1)
        budget = self.budget(policy.effective_delivery_target_seconds,
                             hard_seconds=policy.hard_question_seconds, wall_clock=lambda: wall[0])
        self.now += 1.25
        wall[0] += 1.25
        saved = budget.checkpoint()
        self.assertEqual((saved["limit_seconds"], saved["hard_limit_seconds"]), (1, 120))
        wall[0] += 30
        self.now = 0
        wider = AgentPolicy(max_question_seconds=180, delivery_target_seconds=60)
        restored = self.budget(wider.effective_delivery_target_seconds,
            hard_seconds=wider.hard_question_seconds, wall_clock=lambda: wall[0])
        restored.restore(saved)
        self.assertEqual(restored.limit, 1)
        self.assertTrue(restored.target_reached())
        self.assertEqual(restored.hard_limit, 120)
        self.assertEqual(restored.hard_remaining(), 88.75)
        self.now += 88.75
        with self.assertRaises(QuestionDeadlineExceeded):
            restored.check()

    def test_explicit_hard_policy_round_trips_independently_of_target(self):
        policy = AgentPolicy(max_question_seconds=120, hard_question_seconds=7)
        self.assertEqual(resolve_agent_policy(policy.to_dict()), policy)

    def test_hard_policy_rejects_invalid_runtime_limits(self):
        for value in (0, -1, 1801, True, 1.5, "120"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                AgentPolicy(hard_question_seconds=value)

    def test_restart_retains_elapsed_time_including_disconnection(self):
        wall = [1000.0]
        budget = self.budget(180, hard_seconds=180, wall_clock=lambda: wall[0])
        self.now += 60
        wall[0] += 60
        budget.observe_request(55)
        checkpoint = budget.checkpoint()
        self.now = 5.0  # A restarted process has a different monotonic origin.
        wall[0] += 30
        restored = self.budget(180, hard_seconds=180, wall_clock=lambda: wall[0])
        restored.restore(checkpoint)
        self.assertEqual(restored.elapsed(), 90)
        self.assertEqual(restored.hard_remaining(), 90)
        self.assertEqual(restored.request_durations, [55])
        self.now += 90
        wall[0] += 90
        with self.assertRaises(QuestionDeadlineExceeded):
            restored.check()

    def test_repeated_restore_and_clock_rollback_cannot_extend_deadline(self):
        wall = [1000.0]
        budget = self.budget(180, hard_seconds=180, wall_clock=lambda: wall[0])
        self.now += 100
        wall[0] += 100
        checkpoint = budget.checkpoint()
        wall[0] -= 50
        restored = self.budget(180, hard_seconds=180, wall_clock=lambda: wall[0])
        restored.restore(checkpoint)
        self.assertEqual(restored.hard_remaining(), 80)
        self.now += 20
        restored.restore(checkpoint)
        self.assertEqual(restored.hard_remaining(), 60)

    def test_restore_preserves_stricter_old_and_current_limits(self):
        old = self.budget(30, hard_seconds=30)
        self.now += 20
        checkpoint = old.checkpoint()
        restored = self.budget(180, hard_seconds=180)
        restored.restore(checkpoint)
        self.assertLessEqual(restored.hard_remaining(), 10)
        strict = self.budget(5, hard_seconds=5)
        strict.restore(checkpoint)
        self.assertEqual(strict.hard_remaining(), 0)

    def test_legacy_journal_uses_creation_time_fallback(self):
        budget = self.budget(180, hard_seconds=180)
        budget.restore(None, fallback_elapsed=181)
        with self.assertRaises(QuestionDeadlineExceeded):
            budget.check()


if __name__ == "__main__":
    unittest.main()
