"""Check the offline error matrix used by computer-use acceptance runs."""
import unittest
from unittest import mock

import workbench_ui_fixture as fixture


class ReviewFixtureTests(unittest.TestCase):
    def tearDown(self):
        fixture.REVIEW_EVENTS.clear()

    def test_safe_error_categories_are_distinct(self):
        for marker, category in (("429-quota", "quota_exhausted"),
                                 ("429-rate", "rate_limit"),
                                 ("429-unknown", "http_429_unknown"),
                                 ("500", "http_5xx_unknown"),
                                 ("network", "connection")):
            with self.subTest(marker=marker):
                scenario, failure = fixture.synthetic_failure("[" + marker + "] synthetic")
                self.assertEqual(scenario, marker)
                self.assertEqual(failure.diagnostic["category"], category)

    def test_model_enabled_path_only_raises_the_synthetic_failure(self):
        with mock.patch.object(fixture, "analyze_source", side_effect=AssertionError("must stay offline")), \
                mock.patch.object(fixture.time, "sleep"), \
                self.assertRaises(fixture.APIClientError) as raised:
            fixture.offline_analyzer(None, None, allow_network=True,
                question="[500] synthetic", check_cancel=lambda: None)
        self.assertEqual(raised.exception.http_status, 500)
        self.assertEqual(fixture.REVIEW_EVENTS, [{"sequence": 1, "scenario": "500"}])

    def test_cancellation_is_checked_before_the_error(self):
        check = mock.Mock(side_effect=RuntimeError("synthetic cancellation"))
        with self.assertRaisesRegex(RuntimeError, "synthetic cancellation"):
            fixture.offline_analyzer(None, None, allow_network=True,
                question="停止", check_cancel=check)
        self.assertEqual(check.call_count, 1)
        self.assertEqual(fixture.REVIEW_EVENTS[-1]["scenario"], "cancel")


if __name__ == "__main__":
    unittest.main()
