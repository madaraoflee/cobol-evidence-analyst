"""Separate answer scheduling from cancellation of an active investigation.

The completion target stops optional discovery in favour of an answer. It is
not a deadline for a provider response already being generated. Only explicit
user cancellation or an optional hard runtime limit interrupts active work;
provider requests retain their separately configured transport timeout.
"""

from company_api import APIClientError


class QuestionDeadlineExceeded(APIClientError):
    def __init__(self):
        super().__init__("QUESTION_TIME_BUDGET_EXHAUSTED")


class QuestionBudget:
    def __init__(self, seconds, *, clock, check_cancel=None, hard_seconds=None):
        self.limit = seconds
        self.hard_limit = hard_seconds
        self.clock = clock
        self.started = clock()
        self.cancel = check_cancel
        self.phase = "locating_sources"
        self.recovery = None
        self.request_durations = []

    def remaining(self):
        return max(0.0, self.limit - (self.clock() - self.started))

    def target_reached(self):
        return self.remaining() <= 0

    def hard_remaining(self):
        if self.hard_limit is None:
            return None
        return max(0.0, self.hard_limit - (self.clock() - self.started))

    def check_cancel(self):
        """Check only a user's cancellation, including during provider waits."""
        if self.cancel:
            self.cancel()

    def check(self):
        """Check cancellation and an explicitly configured hard runtime limit."""
        self.check_cancel()
        if self.hard_remaining() == 0:
            raise QuestionDeadlineExceeded()

    def request_timeout(self, configured):
        self.check()
        remaining = self.hard_remaining()
        return float(configured) if remaining is None else min(float(configured), remaining)

    def should_answer(self, configured):
        # A configured timeout is an upper bound, not a latency prediction.
        # In particular a large provider timeout must not disable first-round
        # discovery. Use observed waits with a bounded synthesis allowance.
        remaining = self.remaining()
        hard_remaining = self.hard_remaining()
        if hard_remaining is not None:
            remaining = min(remaining, hard_remaining)
        # Even before a first response, reaching the target means the next
        # request must answer with available evidence rather than plan more work.
        if remaining <= 0:
            return True
        if not self.request_durations:
            return False
        reserve = min(60.0, max(min(30.0, float(configured)), max(self.request_durations)))
        return remaining <= reserve

    def observe_request(self, seconds):
        self.request_durations.append(max(0.0, seconds))
