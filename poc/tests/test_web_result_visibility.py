from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from analyze_source import analyze_source
from web_app import RequestError, WorkbenchState, _validate_options
from report_view import write_report_view


class WebResultVisibilityTests(unittest.TestCase):
    def run_case(self, change, *, capture=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "source"
        source.mkdir()
        (source / "entry.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. REQUEST-ENTRY.\n"
            "PROCEDURE DIVISION.\nDISPLAY 'REQUEST ACCEPTED'.\nGOBACK.\n",
            encoding="utf-8",
        )

        def analyzer(source, output, **options):
            report = analyze_source(source, output, **{**options, "allow_network": False})
            snapshot = report["build_report"]["snapshot_id"]
            agent = {"runner_status": "COMPLETED", "agent_result": {
                "status": "PARTIAL", "analysis_mode": "source_reading", "snapshot_id": snapshot,
                "answer": "Available business explanation.", "model_answer_recorded": True,
                "narrative": {"text": "Available business explanation. [ev_foreign]", "verification": "unverified"},
                "evidence_refs": [{"evidence_id": "ev_foreign"}], "claims": [],
            }}
            if capture:
                agent["api_diagnostics"] = {"captured": True, "exchanges": [{
                    "phase": "investigation", "http_status": 200, "body_text": "received response",
                }]}
            programs = json.loads((output / "programs.json").read_text(encoding="utf-8"))
            change(report, agent, programs)
            (output / "programs.json").write_text(json.dumps(programs), encoding="utf-8")
            (output / "agent-result.json").write_text(json.dumps(agent), encoding="utf-8")
            write_report_view(output / "agent-result.json", agent)
            return report

        app = WorkbenchState(analyzer=analyzer)
        job_id = app.start({"source": str(source), "output": str(root / "output"),
                            "entry": "REQUEST-ENTRY", "question": "Explain request eligibility.",
                            "allow_network": False})["job_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = app.get_job(job_id)
            if job["status"] != "RUNNING":
                self.assertEqual(job["status"], "COMPLETED", job.get("error"))
                return app, job["result"]
            time.sleep(0.01)
        self.fail("web job did not finish")

    def test_web_analysis_defaults_to_full_chain_without_changing_business_mode(self):
        _, project = self.run_case(lambda report, agent, programs: None)
        options = project["diagnosis"]["source_options"]
        self.assertEqual(options["analysis_mode"], "business")
        self.assertEqual(options["reading_strategy"], "full_chain")
        self.assertEqual(options["max_source_pages"], 12)

    def test_reading_strategy_and_batch_size_validate_independently(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            payload = {"source": str(source), "output": str(root / "output")}
            for strategy in ("full_chain", "focused"):
                with self.subTest(strategy=strategy):
                    options = _validate_options({**payload, "reading_strategy": strategy, "max_source_pages": 48})
                    self.assertEqual(options["reading_strategy"], strategy)
                    self.assertEqual(options["max_source_pages"], 48)
            for invalid in ("unknown", 3, True, [], {}):
                with self.subTest(invalid=invalid):
                    with self.assertRaises(RequestError):
                        _validate_options({**payload, "reading_strategy": invalid})
            for invalid in (True, 0, 129, "12"):
                with self.subTest(batch=invalid):
                    with self.assertRaises(RequestError):
                        _validate_options({**payload, "reading_strategy": "full_chain", "max_source_pages": invalid})

    def test_verified_partial_explanation_survives_missing_dependencies_and_scope_limits(self):
        def change(report, agent, programs):
            report["unresolved_dependencies"] = [{"target_name": "REQUESTIO"}]
            report["scope"] = {"truncated": True}
            report["question_status"] = "PARTIAL"

        _, project = self.run_case(change)
        self.assertEqual(project["agent"]["agent_result"]["status"], "PARTIAL")
        self.assertIn("Available business", project["agent"]["agent_result"]["narrative"]["text"])
        self.assertTrue(project["snapshot_id"])

    def test_large_browser_result_keeps_final_answer_and_source_authority(self):
        def change(report, agent, programs):
            agent["agent_result"]["page_summaries"] = [{"text": "Repeated page explanation " * 12000}] * 65
        _, project = self.run_case(change, capture=True)
        self.assertIn("Available business explanation", project["agent"]["agent_result"]["narrative"]["text"])
        self.assertEqual(project["agent"]["agent_result"]["snapshot_id"], project["snapshot_id"])
        self.assertTrue(project["agent"]["display_projection"]["complete_report_on_disk"])
        self.assertEqual(project["agent"]["api_diagnostics"]["exchanges"][0]["body_text"], "received response")

    def test_wrong_answer_snapshot_retains_text_without_accepting_its_citations(self):
        def change(report, agent, programs):
            agent["agent_result"]["snapshot_id"] = "sha256:older-answer"

        app, project = self.run_case(change)
        agent = project["agent"]
        self.assertIsNone(agent["agent_result"])
        self.assertEqual(agent["unaccepted_response"], {
            "reason_code": "ANSWER_SNAPSHOT_MISMATCH", "text": "Available business explanation. [ev_foreign]",
        })
        self.assertNotIn("api_diagnostics", agent)
        self.assertTrue(project["snapshot_id"])
        self.assertTrue(project["programs"])
        with self.assertRaises(RequestError) as caught:
            app.evidence("ev_foreign")
        self.assertEqual(caught.exception.code, "EVIDENCE_NOT_FOUND")

    def test_program_snapshot_mismatch_keeps_api_and_text_but_no_source_authority(self):
        def change(report, agent, programs):
            programs["snapshot_id"] = "sha256:older-index"

        app, project = self.run_case(change, capture=True)
        self.assertIsNone(project["snapshot_id"])
        self.assertEqual(project["programs"], [])
        self.assertFalse(project["diagnosis"]["source_manifest_verified"])
        self.assertIsNone(project["agent"]["agent_result"])
        self.assertEqual(project["agent"]["unaccepted_response"]["reason_code"], "SOURCE_SNAPSHOT_MISMATCH")
        self.assertEqual(project["agent"]["api_diagnostics"]["exchanges"][0]["body_text"], "received response")
        with self.assertRaises(RequestError) as caught:
            app.evidence("ev_foreign")
        self.assertEqual(caught.exception.code, "NO_CURRENT_INDEX")

    def test_catalog_readiness_does_not_accept_an_unverified_business_answer(self):
        def change(report, agent, programs):
            report.update(source_manifest_verified=False, catalog_ready=True)

        _, project = self.run_case(change)
        self.assertTrue(project["diagnosis"]["catalog_ready"])
        self.assertTrue(project["programs"])
        self.assertIsNone(project["snapshot_id"])
        self.assertIsNone(project["agent"]["agent_result"])
        self.assertEqual(project["agent"]["unaccepted_response"]["reason_code"], "SOURCE_SNAPSHOT_UNVERIFIED")

    def test_core_rejection_text_is_retained_even_without_api_capture_or_a_catalog(self):
        def change(report, agent, programs):
            report.update(source_manifest_verified=False, catalog_ready=False, reason_code="SOURCE_ANALYSIS_FAILED")
            agent.update(agent_result=None, unaccepted_response={
                "reason_code": "SOURCE_ANALYSIS_FAILED", "text": "Previously received business explanation.",
            })

        _, project = self.run_case(change)
        self.assertIsNone(project["agent"]["agent_result"])
        self.assertEqual(project["agent"]["unaccepted_response"]["text"], "Previously received business explanation.")
        self.assertNotIn("api_diagnostics", project["agent"])


if __name__ == "__main__":
    unittest.main()
