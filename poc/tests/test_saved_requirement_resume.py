"""Interrupted provider requests cannot replay answers from old requirements."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_store import RunStore
import test_answer_instructions as fixtures


class SavedRequirementResumeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.AnswerInstructionsTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def pending(self):
        f = self.fixture
        original = f.ask(lambda payload, _: f.reply(f.answer(payload)))
        run_id = original["agent_result"]["run_id"]
        journal = RunStore(f.root / "investigation-runs.sqlite")
        saved = journal.load(run_id)
        journal.checkpoint(run_id, saved["checkpoint"], status="attention")
        journal.start_request(run_id, "pending-review", "request-digest")
        f.requests.clear()
        f.sent.clear()
        return original, journal, run_id

    def test_changed_or_cleared_requirements_do_not_present_the_old_answer(self):
        original, journal, run_id = self.pending()
        saved = deepcopy(journal.load(run_id))
        for instructions in ("只用一个业务结果表格，解释接收方如何使用结果。", ""):
            self.fixture.instructions = instructions
            for options in ({}, {"resume_run_id": run_id}):
                with self.subTest(instructions=instructions, options=options):
                    result = self.fixture.ask(lambda *_: self.fail("Automatic resend"), **options)
                    self.assertEqual(result["reason_code"], "REMOTE_REQUEST_OUTCOME_UNKNOWN")
                    self.assertFalse(result["agent_result"]["answer_available"])
                    self.assertFalse(result["agent_result"]["model_answer_recorded"])
                    self.assertTrue(result["agent_result"]["resume_state"]["answer_requirements_changed"])
                    self.assertNotEqual(result["agent_result"]["answer"], original["agent_result"]["answer"])
                    self.assertEqual(journal.load(run_id), saved)
        self.assertEqual(self.fixture.requests, [])

    def test_unchanged_requirements_preserve_the_received_business_answer(self):
        original, journal, run_id = self.pending()
        saved = deepcopy(journal.load(run_id))
        result = self.fixture.ask(lambda *_: self.fail("Automatic resend"))
        self.assertEqual(result["reason_code"], "REMOTE_REQUEST_OUTCOME_UNKNOWN")
        self.assertTrue(result["agent_result"]["answer_available"])
        self.assertEqual(result["agent_result"]["answer"], original["agent_result"]["answer"])
        self.assertTrue(result["agent_result"]["resume_state"]["retained_answer_from_saved_run"])
        self.assertEqual(journal.load(run_id), saved)
        self.assertEqual(self.fixture.requests, [])


if __name__ == "__main__":
    unittest.main()
