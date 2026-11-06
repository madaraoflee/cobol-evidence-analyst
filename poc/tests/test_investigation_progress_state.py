"""Public investigation snapshots preserve state without exposing private envelopes."""

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web_app import WorkbenchState, _analysis_state


def snapshot(revision=3):
    return {"schema_version": "business-investigation-progress/v1", "revision": revision,
        "round": 4, "phase": "investigate", "summary": "Checking the remaining balance condition.",
        "counts": {"candidate": 2, "open": 1, "conflicts": 0, "total_sources": 8},
        "findings": [{"id": "entry-1", "kind": "conditions", "text": "The balance condition needs review.",
                      "evidence_count": 2},
                     {"id": "entry-2", "kind": "open_questions", "text": "Check the later override.",
                      "evidence_count": 0}], "omitted_findings": 0,
        "compaction": {"status": "completed", "cycle": 1, "before_bytes": 8000, "after_bytes": 5000,
                       "retained_entries": 3, "deferred_sources": 2, "structural_preservation_verified": True}}


class InvestigationProgressProjectionTests(unittest.TestCase):
    def test_projection_copies_only_the_contract_without_promoting_candidates(self):
        value = snapshot()
        value.update(reasoning_content="private-reasoning", provider_response={"secret": "private-envelope"})
        value["counts"]["confirmed"] = 2
        value["findings"][0].update(status="confirmed", source_text="private-source", reasoning_content="private-reasoning")
        value["compaction"]["semantics_lossless"] = True
        self.assertEqual(_analysis_state(value), snapshot())
        projected = _analysis_state(value)
        value["findings"][0]["text"] = "Changed by caller"
        self.assertEqual(projected, snapshot())
        self.assertNotIn("private-", json.dumps(projected))
        self.assertNotIn("confirmed", json.dumps(projected))

    def test_bounded_projection_reports_every_undisplayed_record(self):
        value = snapshot()
        value["summary"] = "s" * 3000
        value["findings"] = [{"id": f"entry-{number}", "kind": "rules", "text": "t" * 2500,
                              "evidence_count": 1} for number in range(75)]
        value["omitted_findings"] = 7
        projected = _analysis_state(value)
        self.assertEqual(len(projected["summary"]), 2000)
        self.assertTrue(projected["summary"].endswith("…"))
        self.assertEqual(len(projected["findings"]), 64)
        self.assertEqual(projected["omitted_findings"], 18)
        self.assertTrue(all(len(row["text"]) == 2000 for row in projected["findings"]))
        self.assertTrue(all(row["text"].endswith("…") for row in projected["findings"]))
        self.assertEqual(len(value["findings"]), 75)

    def test_malformed_counts_stages_and_protocol_text_are_rejected(self):
        invalid = []
        for key, bad in (("revision", True), ("revision", 2 ** 53), ("round", 0),
                         ("phase", []), ("phase", "reasoning_content"), ("findings", {}),
                         ("omitted_findings", -1), ("compaction", None)):
            invalid.append({**snapshot(), key: bad})
        for field in ("candidate", "open", "conflicts", "total_sources"):
            value = snapshot()
            value["counts"][field] = True
            invalid.append(value)
        for key, bad in (("status", "confirmed"), ("before_bytes", -1),
                         ("structural_preservation_verified", "true")):
            value = snapshot()
            value["compaction"][key] = bad
            invalid.append(value)
        for text in ('{"reasoning_content":"private-text"}', '<think>private-text</think>',
                     '{"read":[{"relative_path":"rule.cbl"}]}', '{"backward_slice":[]}'):
            invalid.append({**snapshot(), "summary": text})
            value = snapshot()
            value["findings"][0]["text"] = text
            invalid.append(value)
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(_analysis_state(value))

    def test_no_compaction_does_not_invent_compression_or_preservation(self):
        value = snapshot()
        value.pop("compaction")
        self.assertEqual(_analysis_state(value), value)

    def test_global_pending_records_are_strict_and_distinct_from_summary_open_questions(self):
        value = snapshot()
        value["counts"].update(open=0, global_pending=17)
        value["has_pending_work"] = True
        self.assertEqual(_analysis_state(value), value)
        for bad_count, bad_flag in ((True, True), (-1, True), (2 ** 53, True),
                                   (17, False), (17, "true")):
            invalid = snapshot()
            invalid["counts"]["global_pending"] = bad_count
            invalid["has_pending_work"] = bad_flag
            self.assertIsNone(_analysis_state(invalid))
        invalid = snapshot()
        invalid["has_pending_work"] = True
        self.assertIsNone(_analysis_state(invalid))
        value["counts"]["global_pending"] = 0
        self.assertEqual(_analysis_state(value), value)


class InvestigationProgressSnapshotTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.app = WorkbenchState(state_path=Path(directory.name) / "settings.json")
        self.job_id = "neutral-job"
        self.app.job = {"job_id": self.job_id, "status": "RUNNING", "result": None,
                        "progress": {"phase": "preparing", "completed": 0}}
        self.app.started_at = self.app.updated_at = self.app.phase_started_at = time.monotonic()

    def publish(self, **fields):
        self.app._progress(self.job_id, {"phase": "answering", **fields})

    def test_latest_snapshot_survives_phase_changes_and_stale_or_invalid_events(self):
        self.publish(analysis_state=snapshot())
        self.publish(phase="waiting_for_model")
        stale = snapshot(2)
        stale["summary"] = "Old state"
        self.publish(analysis_state=stale)
        self.publish(analysis_state={**snapshot(4), "counts": {}})
        current = self.app.get_job(self.job_id)
        self.assertEqual(current["progress"]["analysis_state"], snapshot())
        self.assertEqual(current["progress_revision"], 4)

    def test_running_completed_and_failure_snapshots_are_self_contained(self):
        running = snapshot(4)
        running["phase"] = "compacting"
        running["compaction"].update(status="running", structural_preservation_verified=False)
        self.publish(analysis_state=running)
        self.assertEqual(self.app.get_job(self.job_id)["progress"]["analysis_state"], running)
        completed = snapshot(5)
        self.publish(analysis_state=completed)
        reconnect = self.app.wait_job(self.job_id, after=0, wait_ms=0)
        self.assertEqual(reconnect["progress"]["analysis_state"], completed)
        failed = snapshot(6)
        failed["compaction"].update(status="failed", structural_preservation_verified=False)
        self.publish(analysis_state=failed)
        self.assertEqual(self.app.get_job(self.job_id)["progress"]["analysis_state"], failed)

    def test_new_job_does_not_inherit_previous_state_or_revision(self):
        self.publish(analysis_state=snapshot(20))
        self.app.job = {"job_id": "next-job", "status": "RUNNING", "result": None,
                        "progress": {"phase": "preparing", "completed": 0}}
        self.app._progress("next-job", {"phase": "answering", "analysis_state": snapshot(1)})
        self.assertEqual(self.app.get_job("next-job")["progress"]["analysis_state"], snapshot(1))

    def test_global_pending_snapshot_survives_reconnection_and_stage_only_events(self):
        current = snapshot(9)
        current["counts"].update(open=0, global_pending=17)
        current["has_pending_work"] = True
        self.publish(analysis_state=current)
        self.publish(phase="compacting")
        reconnect = self.app.wait_job(self.job_id, after=0, wait_ms=0)
        self.assertEqual(reconnect["progress"]["analysis_state"], current)
        self.assertEqual(reconnect["progress"]["analysis_state"]["counts"]["global_pending"], 17)


if __name__ == "__main__":
    unittest.main()
