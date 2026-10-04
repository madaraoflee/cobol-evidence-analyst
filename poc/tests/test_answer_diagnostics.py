from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from answer_diagnostics import build_answer_diagnostics, response_character_counts
from business_analysis import _extract_text, run_business_analysis
from company_api import CompanyAPIConfig, TransportResponse


class AnswerDiagnosticsTests(unittest.TestCase):
    def config(self):
        return CompanyAPIConfig(base_url="https://service.example/v1", chat_model="private-model-canary",
                                api_key="private-key-canary", profile_name="workbench", output_limit_source="environment")

    def test_shareable_latency_distinguishes_local_preparation_from_provider_wait(self):
        result = {"metrics": {"timing_seconds": {"first_model_request_seconds": 1.125,
            "local_processing_seconds": 2.5, "provider_wait_seconds": 17.75, "total_seconds": 20.25,
            "private_path": "private-timing-canary"}},
            "boundaries": [{"reason": "local_analysis_budget_reached"}]}
        quality = {"configuration": {"policy": {
            "max_initial_context_seconds": 3, "max_local_analysis_seconds": 15}}}
        summary = build_answer_diagnostics(config=self.config(), quality=quality, result=result)
        self.assertEqual(summary["timing_seconds"], {"first_model_request_seconds": 1.125,
            "local_processing_seconds": 2.5, "provider_wait_seconds": 17.75, "total_seconds": 20.25})
        self.assertEqual(summary["configured"]["max_initial_context_seconds"], 3)
        self.assertIn("local_analysis_budget_reached", summary["source_coverage"]["limitation_codes"])
        self.assertNotIn("private-timing-canary", json.dumps(summary))
        result["metrics"]["timing_seconds"] = {"first_model_request_seconds": float("nan"),
            "local_processing_seconds": True, "provider_wait_seconds": float("inf"), "total_seconds": -1}
        self.assertTrue(all(value is None for value in build_answer_diagnostics(
            config=self.config(), quality=quality, result=result)["timing_seconds"].values()))

    def test_normal_long_selected_response_is_preserved_with_matching_counts(self):
        text = "結論。\n\n" + "相关步骤与条件🙂。" * 10000 + "\n\n最后一条例外。"
        raw = {"choices": [{"message": {"content": ""}, "finish_reason": "length"},
                           {"message": {"content": text}, "finish_reason": "stop"}]}
        reply = _extract_text(raw)
        self.assertEqual(reply.text, text)
        self.assertFalse(reply.truncated)
        self.assertEqual(response_character_counts(raw, reply), {
            "raw_content_characters": len(text), "parsed_answer_characters": len(text), "choice_index": 1})

    def test_length_and_json_wrapper_are_distinguished_from_body_loss(self):
        text = "第一段规则。" * 10000
        wrapped = json.dumps({"answer": text}, ensure_ascii=False)
        raw = {"choices": [{"message": {"content": wrapped}, "finish_reason": "length"}]}
        reply = _extract_text(raw)
        self.assertEqual(reply.text, text)
        self.assertTrue(reply.truncated)
        counts = response_character_counts(raw, reply)
        self.assertEqual(counts["raw_content_characters"], len(wrapped))
        self.assertEqual(counts["parsed_answer_characters"], len(text))

    def test_complete_long_legacy_answer_keeps_tail(self):
        text = "规则🙂。" * 15001 + "最后条件。"
        def transport(request):
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}, ensure_ascii=False))
        page = {"evidence_id": "ev_page_000000000000000000000001", "relative_path": "entry.cbl",
                "start_line": 1, "end_line": 1, "source_sha256": "a" * 64,
                "source_text": "MOVE BASE-VALUE TO NET-VALUE.", "span_truncated": False}
        plan = {"snapshot_id": "snapshot-test", "pages": [page], "evidence_refs": [],
                "outline": [{"relative_path": "entry.cbl", "program_name": "FLOWPLAN"}],
                "boundaries": [], "coverage": {"total_pages": 1, "selected_pages": 1,
                    "total_lines": 1, "total_files": 1, "selected_files": 1, "complete": True}}
        with mock.patch("business_analysis.prepare_source_reading", return_value=plan):
            result = run_business_analysis("如何处理", "unused.sqlite", "unused", self.config(),
                                           entry_program="FLOWPLAN", framework_context={}, transport=transport)
        self.assertEqual(result["agent_result"]["answer"], text)
        self.assertNotIn("ANSWER_PREVIEW_TRUNCATED", json.dumps(result))

    def test_export_projects_only_counts_and_fixed_codes_from_poisoned_trace(self):
        poison = "private-source-body-secret-canary"
        quality = {"configuration": {"policy": {"max_model_requests": 5}, "private": poison},
            "rounds": [{"stage": "continue", "request_bytes": 1000,
                "sources": [{"path": poison, "characters": 10, "start_line": 2, "end_line": 4,
                             "excerpt": poison}], "history_summary": {"count": 2, "characters": 8, "text": poison},
                "trim_events": [{"path": poison, "role": "source"}], "context": poison,
                "response": {"finish_reason": poison, "usage": {poison: poison},
                             "raw_content_characters": 90, "parsed_answer_characters": 80, "choice_index": 1}}],
            "final": {"open_tasks": [{"path": poison, "state": "failed"}],
                      "question_investigation": {"open_gaps": [{"reason": "external_implementation_unavailable", "target": poison}]}}}
        result = {"answer": poison, "answer_detail": "detailed", "finish_reason": poison,
            "answer_truncated": True, "continuation_attempted": True, "stop_reason": poison,
            "investigation": {"repository_file_count": 3, "selected_file_count": 1,
                "retrieval_status": "source_candidates", "business_map": {"source_identity": {"status": "resolved"}}},
            "reading_coverage": {"sent_files": 1, "sent_pages": 2}}
        with mock.patch("answer_diagnostics._commit", return_value="d" * 40):
            summary = build_answer_diagnostics(config=self.config(), quality=quality, result=result)
        dumped = json.dumps(summary)
        self.assertNotIn(poison, dumped)
        self.assertNotIn("private-model-canary", dumped)
        self.assertNotIn("private-key-canary", dumped)
        self.assertNotIn("https://", dumped)
        self.assertEqual(summary["runtime"]["profile"], "workbench")
        self.assertEqual(summary["runtime"]["output_limit_source"], "environment")
        self.assertEqual(summary["requests"][0]["stage"], "continuation")
        self.assertEqual(summary["requests"][0]["source_line_count"], 3)
        self.assertEqual(summary["stop_reason"], "other")
        self.assertEqual(summary["source_coverage"]["limitation_codes"], [
            "external_implementation_missing", "located_source_not_read", "output_limit_reached",
            "source_budget_omitted", "source_read_failed"])

    def test_not_found_and_business_word_mismatch_do_not_claim_file_absence(self):
        result = {"answer": "", "investigation": {"repository_file_count": 5,
            "retrieval_status": "unresolved", "business_map": {"source_identity": {"status": "none"}}}}
        summary = build_answer_diagnostics(config=self.config(), quality={}, result=result)
        self.assertEqual(summary["source_coverage"]["limitation_codes"], ["business_terms_unresolved", "search_no_match"])
        self.assertEqual(summary["output"]["finish_reason"], "unknown")

    def test_history_trim_is_not_reported_as_missing_source_and_read_failure_is_distinct(self):
        quality = {"rounds": [{"stage": "answer", "trim_events": [{"role": "history"}]}]}
        result = {"answer": "", "investigation_state": {"completed_actions": [{"read": {}, "outcome": "unavailable"}]}}
        summary = build_answer_diagnostics(config=self.config(), quality=quality, result=result)
        self.assertEqual(summary["source_coverage"]["limitation_codes"], ["source_read_failed"])

    def test_supplied_sources_are_counted_when_response_has_no_accepted_answer(self):
        quality = {"rounds": [{"stage": "answer", "sources": [
            {"path": "entry.cbl", "characters": 12, "start_line": 1, "end_line": 2}]}]}
        result = {"answer": "", "finish_reason": "length", "diagnostics": [{"code": "MODEL_TEXT_EMPTY"}],
                  "investigation": {"repository_file_count": 1, "selected_file_count": 0,
                    "current_question_match_observed": True, "retrieval_status": "unresolved"},
                  "reading_coverage": {"sent_files": 0, "sent_pages": 0}}
        summary = build_answer_diagnostics(config=self.config(), quality=quality, result=result)
        self.assertEqual(summary["source_coverage"]["retrieval_status"], "source_candidates")
        self.assertEqual(summary["source_coverage"]["provided_files"], 1)
        self.assertEqual(summary["source_coverage"]["pages"], 1)
        self.assertEqual(summary["source_coverage"]["limitation_codes"], ["output_limit_reached", "parser_failed"])

    def test_legacy_brief_preference_reaches_direct_prompt(self):
        captured = []
        def transport(request):
            captured.append(json.loads(json.loads(request.body)["messages"][-1]["content"]))
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": "简短结论。"}, "finish_reason": "stop"}]}))
        page = {"evidence_id": "ev_page_000000000000000000000001", "relative_path": "entry.cbl",
                "start_line": 1, "end_line": 1, "source_sha256": "a" * 64, "source_text": "MOVE 1 TO RESULT."}
        plan = {"snapshot_id": "snapshot-test", "pages": [page], "coverage": {"complete": True}, "boundaries": []}
        with mock.patch("business_analysis.prepare_source_reading", return_value=plan):
            run_business_analysis("如何处理", "unused", "unused", self.config(), entry_program="FLOWPLAN",
                                  answer_detail="brief", framework_context={}, transport=transport)
        self.assertEqual(captured[0]["answer_detail"], "brief")
        self.assertIn("简要说明结论", captured[0]["task"])

    def test_complete_source_rejected_before_request_is_reported_as_budget_omission(self):
        policy = {"max_model_requests": 5, "max_source_characters": 36000,
                  "max_complete_source_characters": 128000, "max_request_bytes": 230000}
        for reason in ("source_characters", "complete_source_characters", "source_byte_budget"):
            with self.subTest(reason=reason):
                result = {"investigation": {"working_set": {"status": "fallback",
                    "reason": reason, "source_manifest": [], "physical_complete": False,
                    "closure_complete": False, "omitted_candidate_paths": ["private-source.cbl"],
                    "omitted_candidate_path_count": 1}}}
                summary = build_answer_diagnostics(config=self.config(),
                    quality={"configuration": {"policy": policy}}, result=result)
                coverage = summary["source_coverage"]
                self.assertEqual(coverage["omitted_complete_files"], 1)
                self.assertIn("source_budget_omitted", coverage["limitation_codes"])
                self.assertNotIn("external_implementation_missing", coverage["limitation_codes"])
                self.assertEqual(coverage["working_set_reason"], reason)
                self.assertEqual(summary["configured"]["max_complete_source_characters"], 128000)
                self.assertNotIn("private-source.cbl", json.dumps(summary))

    def test_working_set_projection_keeps_unknown_distinct_and_deduplicates_omissions(self):
        poison = "private-context-marker"
        summary = build_answer_diagnostics(config=self.config(), quality={}, result={
            "investigation": {"working_set": {"status": poison, "reason": poison,
                "physical_complete": poison, "closure_complete": poison,
                "complete_root_budget_applied": poison, "transmission_fallback_reason": poison,
                "omitted_candidate_paths": [poison], "omitted_candidate_path_count": 1,
                "omitted_complete_paths": [poison]}}})
        coverage = summary["source_coverage"]
        self.assertEqual(coverage["omitted_complete_files"], 1)
        self.assertEqual(coverage["working_set_status"], "unknown")
        self.assertIsNone(coverage["physical_complete"])
        self.assertIsNone(coverage["closure_complete"])
        self.assertIsNone(coverage["complete_root_budget_applied"])
        self.assertNotIn(poison, json.dumps(summary))


if __name__ == "__main__":
    unittest.main()
