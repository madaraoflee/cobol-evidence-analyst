"""One wall-clock allowance shared by local work and provider requests.

The deadline bounds cooperative investigation, not remote server execution.
Evidence verification and checkpoint cleanup still run after it is reached.
"""

from company_api import APIClientError


class QuestionDeadlineExceeded(APIClientError):
    def __init__(self):
        super().__init__("QUESTION_TIME_BUDGET_EXHAUSTED")


class QuestionBudget:
    def __init__(self, seconds, *, clock, check_cancel=None):
        self.limit = seconds
        self.clock = clock
        self.started = clock()
        self.cancel = check_cancel
        self.phase = "locating_sources"
        self.recovery = None
        self.request_durations = []

    def remaining(self):
        return max(0.0, self.limit - (self.clock() - self.started))

    def check(self):
        if self.cancel:
            self.cancel()
        if self.remaining() <= 0:
            raise QuestionDeadlineExceeded()

    def request_timeout(self, configured):
        self.check()
        return min(float(configured), self.remaining())

    def should_answer(self, configured):
        # A configured timeout is an upper bound, not a latency prediction.
        # In particular a large provider timeout must not disable first-round
        # discovery. Use observed waits with a bounded synthesis allowance.
        if not self.request_durations:
            return False
        reserve = min(60.0, max(min(30.0, float(configured)), max(self.request_durations)))
        return self.remaining() <= reserve

    def observe_request(self, seconds):
        self.request_durations.append(max(0.0, seconds))
