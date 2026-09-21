from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analyze_source import analyze_source
from company_api import CompanyAPIConfig, TransportResponse


def program(name, body="GOBACK.\n"):
    return f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nPROCEDURE DIVISION.\n{body}"


class RepositoryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        self.output = self.base / "output"
        self.requests = []

    def write(self, relative, text):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def analyze(self, question=None, *, planner="DONE", **options):
        def transport(request):
            payload = json.loads(request.body)
            prompt = json.loads(payload["messages"][-1]["content"])
            self.requests.append(prompt)
            text = planner if prompt.get("stage") == "repository_search" else "SIMULATED TEST RESPONSE: supplied source text was received; no production result is asserted."
            return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}))
        return analyze_source(self.source, self.output, question=question, source_format="free",
                              analysis_mode=options.pop("analysis_mode", "business"), index_mode=options.pop("index_mode", "catalog"),
                              reading_strategy="full_chain", allow_network=question is not None,
                              config=CompanyAPIConfig("https://service.example/v1", "test-text-model", api_key="test-only-key"),
                              transport=transport, framework_reference_path=self.base / "absent.md", **options)

    def result(self):
        return json.loads((self.output / "agent-result.json").read_text(encoding="utf-8"))

    def source_paths(self):
        return {page["relative_path"] for request in self.requests for page in request.get("source_pages", [])}

    def test_repository_ingestion_builds_full_search_without_entry_or_model_call(self):
        self.write("first.cbl", program("FIRST-ENTRY"))
        self.write("second.cbl", program("SECOND-ENTRY"))
        report = self.analyze()
        self.assertNotEqual(report["runner_status"], "BLOCKED", report["messages"])
        self.assertEqual(report["scope"]["mode"], "repository_index")
        self.assertEqual(report["scope"]["repository_file_count"], 2)
        self.assertEqual(report["repository_search"]["indexed_files"], 2)
        self.assertEqual(self.requests, [])
        self.assertIsNone(report["selected_entry"])

    def test_question_without_entry_reaches_model_with_relevant_callers_and_definitions(self):
        self.write("entry.cbl", program("START-ENTRY", 'CALL "RULE-ENTRY".\n'))
        self.write("rule.cbl", program("RULE-ENTRY", '*> 星辉额度\nCOPY SHARED.\nCALL "UNAVAILABLE-SERVICE".\n'))
        self.write("shared.cpy", "01 SHARED-STATE PIC X.\n")
        self.write("separate.cbl", program("SEPARATE-ENTRY", "COPY SHARED.\n"))
        report = self.analyze("星辉额度")
        self.assertNotEqual(report["runner_status"], "BLOCKED", report["messages"])
        self.assertEqual(report["scope"]["mode"], "repository_question")
        self.assertTrue(any(request.get("stage") == "repository_search" for request in self.requests))
        self.assertEqual(self.source_paths(), {"entry.cbl", "rule.cbl", "shared.cpy"})
        result = self.result()
        self.assertEqual(result["runner_status"], "COMPLETED")
        self.assertIn("SIMULATED TEST RESPONSE", result["agent_result"]["answer"])
        self.assertEqual(result["agent_result"]["reading_coverage"]["repository_total_files"], 4)

    def test_unmatched_question_falls_back_to_all_programs(self):
        self.write("first.cbl", program("FIRST-ENTRY"))
        self.write("second.cbl", program("SECOND-ENTRY"))
        report = self.analyze("UnrecordedHypothesis")
        self.assertNotEqual(report["runner_status"], "BLOCKED", report["messages"])
        self.assertEqual(self.source_paths(), {"first.cbl", "second.cbl"})
        self.assertTrue(self.result()["investigation"]["fallback_all"])

    def test_model_can_request_general_repository_browse_without_a_topic_dictionary(self):
        self.write("first.cbl", program("FIRST-ENTRY", "*> MINT-HANDLING\n"))
        self.write("second.cbl", program("SECOND-ENTRY", "*> COPPER-HANDLING\n"))
        self.analyze("列出业务功能", planner="*")
        self.assertEqual(self.source_paths(), {"first.cbl", "second.cbl"})
        self.assertEqual(self.result()["investigation"]["search_stop_reason"], "repository_overview_requested")

    def test_explicit_entry_preserves_the_existing_scope_and_request_protocol(self):
        self.write("first.cbl", program("FIRST-ENTRY"))
        self.write("second.cbl", program("SECOND-ENTRY"))
        report = self.analyze("Explain the outcome", entry="FIRST-ENTRY")
        self.assertEqual(report["scope"]["mode"], "entry_static_closure")
        self.assertEqual(self.source_paths(), {"first.cbl"})
        self.assertFalse(any(request.get("stage") == "repository_search" for request in self.requests))

    def test_strict_catalog_question_still_requires_an_entry(self):
        self.write("first.cbl", program("FIRST-ENTRY"))
        self.write("second.cbl", program("SECOND-ENTRY"))
        report = self.analyze("Explain the outcome", analysis_mode="strict")
        self.assertEqual(report["reason_code"], "ENTRY_REQUIRED")
        self.assertEqual(self.requests, [])

    def test_repository_without_program_id_can_still_read_available_business_material(self):
        self.write("state.cpy", "01 REQUEST-STATE PIC X(12).\n88 READY-STATE VALUE 'READY'.\n")
        report = self.analyze("REQUEST-STATE")
        self.assertNotEqual(report["runner_status"], "BLOCKED", report["messages"])
        self.assertEqual(self.source_paths(), {"state.cpy"})

    def test_saved_framework_context_is_the_final_context_used_for_the_question(self):
        self.write("first.cbl", program("FIRST-ENTRY"))
        final_context = {"status": "MATCHED", "references": [], "source_paths": ["first.cbl"]}
        investigation = {"mode": "repository", "repository_file_count": 1, "selected_file_count": 1,
                         "matched_file_count": 1, "selected_paths": ["first.cbl"]}
        simulated = {"runner_status": "COMPLETED", "agent_result": {"status": "ANALYZED",
                     "answer": "SIMULATED TEST RESPONSE", "framework_context": final_context,
                     "investigation": investigation}}
        with mock.patch("analyze_source.run_investigation", return_value=simulated):
            report = self.analyze("Explain the outcome")
        self.assertEqual(report["framework_context"], final_context)
        self.assertEqual(json.loads((self.output / "framework-context.json").read_text(encoding="utf-8")), final_context)
        self.assertEqual(report["scope"]["mode"], "repository_question")
        self.assertEqual(report["investigation"]["selected_file_count"], 1)
        self.assertNotIn("selected_paths", report["investigation"])


if __name__ == "__main__":
    unittest.main()
