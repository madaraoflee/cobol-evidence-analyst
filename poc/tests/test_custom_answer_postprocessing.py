"""Saved writing requirements also apply after a model returns its answer."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from answer_fact_completion import complete_supplied_answer
import business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from draft_validation import validate_answer_draft
from repository_discovery import ensure_repository_search
from test_answer_fact_completion import SOURCE, investigation, page


class CustomAnswerPostprocessingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "flow.cbl").write_text(SOURCE, encoding="utf-8")
        self.database = self.root / "index.sqlite"
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only")
        self.instructions = "Always answer in English. Use exactly one short paragraph, no appended technical notes."
        self.requests, self.answers = [], []

    def ask(self, *, request_bytes=230000, default=False, revise=False):
        def transport(request):
            envelope = json.loads(request.body)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.requests.append(envelope)
            source = max((row for bundle in payload["source_context"] for row in bundle["pages"]
                          if row["relative_path"] == "flow.cbl"), key=lambda row: row["end_line"] - row["start_line"])
            text = "The output amount is calculated as OUT-AMOUNT = RAW-AMOUNT * RATE / 1200."
            if revise and len(self.requests) > 1:
                text = ("When RATE is nonzero, the calculation runs. OUT-AMOUNT = RAW-AMOUNT * RATE / 1200. "
                        "The accumulation is TOTAL-AMOUNT = OUT-AMOUNT + TOTAL-AMOUNT. The program then requests PERFORM WRITE-RESULT. "
                        "RAW-AMOUNT, RATE and the running total have no specified initial values here, so the numeric result "
                        "depends on their incoming values. OUT-AMOUNT has PIC S9(7)V99 with two decimal places and no ROUNDED; "
                        "storage scale and overflow handling depend on the compilation environment.")
            text += f" [{source['evidence_id']}]"
            self.answers.append(text)
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}))
        options = dict(transport=transport, framework_reference_path="",
            answer_instructions="" if default else self.instructions,
            conversation_id=f"format-{request_bytes}-{default}-{revise}",
            policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0, max_request_bytes=request_bytes))
        result = business_chat.run_business_chat("CALCFLOW 的 CALCULATE-AMOUNT 计算规则", self.database,
            self.source, self.config, **options)["agent_result"]
        if revise:
            result = business_chat.run_business_chat("CALCFLOW 的 CALCULATE-AMOUNT 计算规则", self.database,
                self.source, self.config, continue_investigation=True, resume_run_id=result["run_id"],
                **options)["agent_result"]
        trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text())
        return result, trace

    def test_custom_single_paragraph_survives_full_and_compact_requests(self):
        for request_bytes in (230000, 32768):
            with self.subTest(request_bytes=request_bytes):
                self.requests.clear()
                self.answers.clear()
                if request_bytes == 32768:
                    (self.source / "flow.cbl").write_text(SOURCE
                        + "".join(f"*> Business context note {index}.\n" for index in range(600)), encoding="utf-8")
                    build_business_index(self.source, self.database, source_format="free", verify_content=True)
                    ensure_repository_search(self.database, self.source)
                result, trace = self.ask(request_bytes=request_bytes)
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(result["answer"], self.answers[0])
                self.assertNotIn("\n\n", result["answer"])
                self.assertFalse(any("\u4e00" <= character <= "\u9fff" for character in result["answer"]))
                report = trace["source_fact_completion"]
                self.assertEqual(report["added_facts"], [])
                self.assertTrue(report["suggested_facts"])
                self.assertEqual(report["status"], "source_facts_deferred_to_model")
                if request_bytes == 32768:
                    self.assertIn("inspect_business_context, backward_slice", self.requests[0]["messages"][0]["content"])

    def test_default_answer_still_receives_local_source_facts(self):
        result, trace = self.ask(default=True)
        self.assertTrue(result["answer"].startswith(self.answers[0]))
        self.assertIn("TOTAL-AMOUNT = OUT-AMOUNT + TOTAL-AMOUNT", result["answer"])
        self.assertTrue(trace["source_fact_completion"]["added_facts"])
        self.assertEqual(trace["source_fact_completion"]["suggested_facts"], [])

    def test_existing_continuation_receives_source_candidates_and_composes_requested_format(self):
        result, _ = self.ask(revise=True)
        self.assertEqual(len(self.requests), 2)
        payload = json.loads(self.requests[1]["messages"][-1]["content"])
        suggestions = payload["answer_review"]["source_fact_suggestions"]
        self.assertTrue(suggestions)
        self.assertTrue(any(row["kind"] == "assignment" for row in suggestions))
        self.assertEqual(result["answer"], self.answers[-1])
        self.assertNotIn("\n\n", result["answer"])
        self.assertIn(self.instructions, self.requests[1]["messages"][0]["content"])

    def test_suggestions_rebind_only_to_supplied_source_version_and_instance(self):
        original = "OUT-AMOUNT = RAW-AMOUNT * RATE / 1200."
        _, report = complete_supplied_answer("计算规则", original, investigation(), [page()],
            source_format="free", append_source_facts=False)
        valid = next(row for row in report["suggested_facts"] if row["kind"] == "assignment")
        supplied = page(identifier="ev_current_source")
        payload = {"question": "计算规则", "answer_instructions": self.instructions,
            "source_context": [{"pages": [supplied]}], "business_map": {}, "framework_references": [],
            "draft_answer": original, "answer_review": {}, "source_fact_suggestions": [valid,
                {**valid, "source_sha256": "f" * 64}, {**valid, "copy_instance": "another-instance"},
                {**valid, "start_line": 999, "end_line": 999}]}
        messages, _ = business_chat._fit_request(self.config, deepcopy(payload), [], AgentPolicy())
        wire = json.loads(messages[-1]["content"])
        self.assertNotIn("source_fact_suggestions", wire)
        self.assertEqual(wire["answer_review"]["source_fact_suggestions"],
                         [{**valid, "evidence_id": "ev_current_source"}])
        # A formula excerpt alone cannot carry a suggestion whose guard or
        # declaration was established using the wider supplied source page.
        payload["source_context"][0]["pages"] = [{**supplied, "start_line": 16, "end_line": 20,
            "source_text": "\n".join(SOURCE.splitlines()[15:20])}]
        messages, _ = business_chat._fit_request(self.config, deepcopy(payload), [], AgentPolicy())
        self.assertNotIn("source_fact_suggestions", json.loads(messages[-1]["content"])["answer_review"])
        payload["source_context"][0]["pages"] = []
        messages, _ = business_chat._fit_request(self.config, payload, [], AgentPolicy())
        self.assertNotIn("source_fact_suggestions", json.loads(messages[-1]["content"])["answer_review"])

    def test_suppressing_appendices_still_repairs_a_demonstrably_wrong_formula(self):
        original = "OUT-AMOUNT = RAW-AMOUNT * RATE / 1000. [ev_test_source]"
        review = validate_answer_draft("计算规则", original, investigation(), [page()],
            source_format="free", answer_language="en", append_source_facts=False)
        self.assertIn("RAW-AMOUNT * RATE / 1200", review["answer"])
        self.assertNotIn("RAW-AMOUNT * RATE / 1000", review["answer"])
        self.assertGreater(review["ledger"]["revised_claim_count"], 0)
        self.assertEqual(review["fact_completion"]["added_facts"], [])
        self.assertTrue(review["fact_completion"]["suggested_facts"])


if __name__ == "__main__":
    unittest.main()
