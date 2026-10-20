"""Optional question deadline, answer reserve, and restart-safe elapsed time."""

from collections.abc import Mapping
import math
import time

from company_api import APIClientError


class QuestionDeadlineExceeded(APIClientError):
    def __init__(self):
        super().__init__("QUESTION_TIME_BUDGET_EXHAUSTED")


class QuestionBudget:
    def __init__(self, seconds, *, clock, check_cancel=None, hard_seconds=None,
                 finalization_reserve_seconds=45, wall_clock=time.time):
        self.limit = seconds
        self.hard_limit = hard_seconds
        self.clock = clock
        self.started = clock()
        self.wall_clock = wall_clock
        self.hard_deadline_epoch = (wall_clock() + hard_seconds
                                    if hard_seconds is not None else None)
        self.finalization_reserve_seconds = finalization_reserve_seconds
        self.cancel = check_cancel
        self.phase = "locating_sources"
        self.recovery = None
        self.request_durations = []

    def elapsed(self):
        return max(0.0, self.clock() - self.started)

    def remaining(self):
        if self.limit is None:
            return None
        return max(0.0, self.limit - self.elapsed())

    def target_reached(self):
        remaining = self.remaining()
        return remaining is not None and remaining <= 0

    def hard_remaining(self):
        if self.hard_limit is None:
            return None
        remaining = self.hard_limit - self.elapsed()
        if self.hard_deadline_epoch is not None:
            remaining = min(remaining, self.hard_deadline_epoch - self.wall_clock())
        return max(0.0, remaining)

    def check_cancel(self):
        """Check only a user's cancellation, including during provider waits."""
        if self.cancel:
            self.cancel()

    def check(self):
        """Check cancellation and any explicit hard deadline, including SSE waits."""
        self.check_cancel()
        if self.hard_remaining() == 0:
            raise QuestionDeadlineExceeded()

    def request_timeout(self, configured):
        self.check()
        remaining = self.hard_remaining()
        return float(configured) if remaining is None else min(float(configured), remaining)

    def _delivery_remaining(self):
        remaining = self.remaining()
        hard_remaining = self.hard_remaining()
        if hard_remaining is not None:
            remaining = hard_remaining if remaining is None else min(remaining, hard_remaining)
        return remaining

    def _answer_reserve(self):
        reserve = float(self.finalization_reserve_seconds)
        if self.request_durations:
            reserve = max(reserve, min(60.0, max(self.request_durations)))
        return reserve

    def should_stop_local_investigation(self):
        """Keep cheap necessary source reads until the actual answer reserve."""
        remaining = self._delivery_remaining()
        return remaining is not None and remaining <= self._answer_reserve()

    def should_answer(self, configured):
        # A configured timeout is an upper bound, not a latency prediction.
        # Reserve the observed cost of another planning response as well as
        # synthesis, without imposing that planning allowance on local reads.
        remaining = self._delivery_remaining()
        if remaining is None:
            # The caller still enforces request/progress limits. Elapsed time
            # alone does not force an answer when no target was configured.
            return False
        # Even before a first response, reaching the target means the next
        # request must answer with available evidence rather than plan more work.
        if remaining <= 0:
            return True
        next_investigation = max(self.request_durations, default=0.0)
        return remaining <= self._answer_reserve() + next_investigation

    def observe_request(self, seconds):
        self.request_durations.append(max(0.0, seconds))

    def checkpoint(self):
        """Persist wall time as well as elapsed time; reconnects earn no time."""
        return {"schema": "question-budget/v1", "elapsed_seconds": self.elapsed(),
                "saved_at_epoch_seconds": self.wall_clock(),
                "hard_deadline_epoch_seconds": self.hard_deadline_epoch,
                "limit_seconds": self.limit, "hard_limit_seconds": self.hard_limit,
                "request_durations": list(self.request_durations)}

    def restore(self, state, *, fallback_elapsed=0):
        """Restore consumed time without widening this run's earlier deadline.

        Older journals have no budget checkpoint. Their creation time gives a
        conservative fallback elapsed duration, including time disconnected.
        The existing request journal remains the authority for call counts.
        """
        def number(value):
            return (float(value) if type(value) in (int, float)
                    and math.isfinite(value) and value >= 0 else None)

        state = state if isinstance(state, Mapping) else {}
        elapsed = number(state.get("elapsed_seconds")) or 0.0
        saved_at = number(state.get("saved_at_epoch_seconds"))
        if saved_at is not None:
            elapsed += max(0.0, self.wall_clock() - saved_at)
        elapsed = max(elapsed, number(fallback_elapsed) or 0.0, self.elapsed())
        self.started = self.clock() - elapsed
        for attribute, key in (("limit", "limit_seconds"), ("hard_limit", "hard_limit_seconds")):
            saved_limit = number(state.get(key))
            current = getattr(self, attribute)
            if saved_limit is not None:
                setattr(self, attribute, saved_limit if current is None else min(current, saved_limit))
        saved_deadline = number(state.get("hard_deadline_epoch_seconds"))
        if self.hard_limit is not None:
            deadline = self.wall_clock() + max(0.0, self.hard_limit - elapsed)
            deadlines = [value for value in (deadline, saved_deadline, self.hard_deadline_epoch)
                         if value is not None]
            self.hard_deadline_epoch = min(deadlines)
        durations = state.get("request_durations")
        if isinstance(durations, list):
            self.request_durations = [value for item in durations
                                      if (value := number(item)) is not None][-32:]
