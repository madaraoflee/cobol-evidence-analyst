"""Bounded final source checks and deterministic failed-action repetition."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[1])]
import online_investigation as investigation
import online_inventory_store as inventory_storage
from online_material_format import canonical_json, digest
import test_online_inventory_store as storage_fixture
import test_online_investigation as investigation_fixture
from test_online_investigation import answer, read


class OnlineInvestigationTerminationTests(unittest.TestCase):
    def setUp(self):
        self.case = storage_fixture.OnlineInventoryStoreTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def complete_checkpoint(self):
        self.case.complete_work()
        self.case.state["status"] = "answered"
        return investigation._checkpoint(self.case.state)

    def recover(self, checkpoint, source, client):
        return investigation.run_investigation(self.case.fixture.fixture.query, source,
            self.case.options["question"], checkpoint=checkpoint, inventory_session=self.case.session,
            allow_source_transfer=True, client=client, max_actions=1, max_model_calls=1)

    def test_final_verification_file_slice_preserves_cursor_and_finishes_without_model_calls(self):
        checkpoint = self.complete_checkpoint()
        original = self.case.fixture.fixture.source
        reads, client = [], mock.Mock()
        def source(path, **options):
            reads.append(path)
            return original(path, **options)
        with mock.patch.object(inventory_storage, "_MAX_VERIFICATION_FILES_PER_RUN", 2):
            first = self.recover(checkpoint, source, client)
            self.assertEqual(first["status"], "needs_continuation")
            self.assertEqual(first["error"], "ONLINE_SOURCE_BUDGET_EXCEEDED")
            self.assertEqual(len(reads), 2)
            self.assertEqual(self.case.session.verification_progress(), {"checked": 2, "total": 4, "pending": 2})
            self.assertIn("final source verification is unfinished", first["answer"])
            self.assertIn("2/4", first["answer"])
            self.assertFalse(first["inventory_result"]["complete"])
            second = self.recover(first["checkpoint"], source, client)
        self.assertEqual(second["status"], "answered")
        self.assertEqual(reads, sorted(self.case.fixture.fixture.source_bytes))
        self.assertEqual(second["model_calls"], 0)
        self.assertEqual(second["actions"], 0)
        self.assertTrue(second["inventory_result"]["complete"])
        self.assertIsNone(self.case.session.verification_progress())
        client.complete.assert_not_called()

    def test_final_verification_slow_source_stops_at_time_slice_and_resumes_without_replay(self):
        checkpoint = self.complete_checkpoint()
        original, clock, reads, client = self.case.fixture.fixture.source, [0.0], [], mock.Mock()
        def source(path, **options):
            reads.append(path)
            clock[0] += 40.0
            return original(path, **options)
        results = []
        with mock.patch.object(inventory_storage.time, "monotonic", side_effect=lambda: clock[0]):
            for _ in range(4):
                result = self.recover(checkpoint, source, client)
                results.append(result)
                checkpoint = result["checkpoint"]
        self.assertEqual([r["status"] for r in results], ["needs_continuation"] * 3 + ["answered"])
        self.assertEqual(reads, sorted(self.case.fixture.fixture.source_bytes))
        self.assertTrue(all(r["model_calls"] == 0 for r in results))
        client.complete.assert_not_called()

    def test_changed_source_in_later_verification_slice_is_rejected_without_advancing_cursor(self):
        checkpoint = self.complete_checkpoint()
        original, reads, client = self.case.fixture.fixture.source, [], mock.Mock()
        def source(path, **options):
            reads.append(path)
            return original(path, **options)
        with mock.patch.object(inventory_storage, "_MAX_VERIFICATION_FILES_PER_RUN", 2):
            first = self.recover(checkpoint, source, client)
            before = deepcopy(self.case.session.meta()["verification"])
            def changed(path, **options):
                raw = source(path, **options)
                return bytes([raw[0] ^ 1]) + raw[1:]
            second = self.recover(first["checkpoint"], changed, client)
        self.assertEqual(second["status"], "failed")
        self.assertEqual(second["error"], "MATERIAL_SOURCE_HASH_MISMATCH")
        self.assertEqual(self.case.session.meta()["verification"], before)
        self.assertFalse(second["inventory_result"]["complete"])
        client.complete.assert_not_called()

    def test_repeated_known_failure_stops_across_checkpoint_without_becoming_completed_work(self):
        case = investigation_fixture.OnlineInvestigationTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        bad = {"actions": [read("main.cbl", 999, 1000)]}
        checkpoint, outcomes = None, []
        for _ in range(4):
            result = case.run_question([bad], max_model_calls=1, checkpoint=checkpoint)
            outcomes.append(result)
            checkpoint = result["checkpoint"]
        self.assertEqual([r["status"] for r in outcomes], ["needs_continuation"] * 3 + ["failed"])
        self.assertEqual(outcomes[-1]["error"], "ONLINE_NO_PROGRESS")
        self.assertEqual(checkpoint["completed_actions"], [])
        self.assertEqual(checkpoint["pending_actions"], [])
        self.assertEqual(checkpoint["no_progress_rounds"], 3)
        self.assertEqual(len([v for v in checkpoint["observations"]
                              if v["result"].get("error") == "MATERIAL_RANGE_OUT_OF_BOUNDS"]), 3)
        again = case.run_question([bad], max_model_calls=1, checkpoint=checkpoint)
        self.assertEqual(again["error"], "ONLINE_NO_PROGRESS")
        self.assertEqual(again["model_calls"], 1)
        self.assertEqual(again["actions"], 0)

    def test_partitioned_inventory_repeated_failure_stops_without_claiming_checked_coverage(self):
        fixture = self.case.fixture.fixture
        action = read("main.cbl", 999, 1000)
        result = investigation.run_investigation(fixture.query, fixture.source, self.case.options["question"],
            inventory_session=self.case.session, allow_source_transfer=True,
            client=fixture.client([{"actions": [action]}] * 6))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "ONLINE_NO_PROGRESS")
        self.assertEqual(result["model_calls"], 4)
        self.assertFalse(result["coverage_ledger"]["complete"])
        self.assertFalse(self.case.session.action_done(digest(canonical_json(investigation._action(action)))))
        pending = deepcopy(self.case.session.pending())
        again = investigation.run_investigation(fixture.query, fixture.source, self.case.options["question"],
            checkpoint=result["checkpoint"], inventory_session=self.case.session, allow_source_transfer=True,
            client=fixture.client([{"actions": [action]}]))
        self.assertEqual(again["error"], "ONLINE_NO_PROGRESS")
        self.assertEqual(again["actions"], 0)
        self.assertEqual(self.case.session.pending(), pending)
        self.assertGreater(again["coverage_ledger"]["pending_count"], 0)

    def test_failure_then_valid_new_work_can_still_complete_normally(self):
        case = investigation_fixture.OnlineInvestigationTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        first = case.run_question([{"actions": [read("main.cbl", 999, 1000)]}] * 6)
        self.assertEqual(first["error"], "ONLINE_NO_PROGRESS")
        result = case.run_question([{"actions": [read()]}, answer], checkpoint=first["checkpoint"])
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["checkpoint"]["no_progress_rounds"], 0)

    def test_unknown_model_outcome_after_failed_action_still_requires_confirmation_before_retry(self):
        fixture = self.case.fixture.fixture
        action = read("main.cbl", 999, 1000)
        first = investigation.run_investigation(fixture.query, fixture.source, self.case.options["question"],
            inventory_session=self.case.session, allow_source_transfer=True,
            client=fixture.client([{"actions": [action]}]), max_model_calls=1)
        checkpoint = first["checkpoint"]
        checkpoint["request_status"] = "unknown"
        source, client = mock.Mock(), mock.Mock()
        second = investigation.run_investigation(fixture.query, source, self.case.options["question"],
            checkpoint=checkpoint, inventory_session=self.case.session, allow_source_transfer=True, client=client)
        self.assertEqual(second["status"], "retry_confirmation_required")
        self.assertEqual(second["checkpoint"]["no_progress_rounds"], checkpoint["no_progress_rounds"])
        source.assert_not_called()
        client.complete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
