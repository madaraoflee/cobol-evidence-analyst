"""Inherited-source diagnostics preserve provenance without default prose capture."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from company_api import CompanyAPIConfig
from quality_trace import QualityTrace


class QualityTraceMemoryTests(unittest.TestCase):
    def observe(self, capture_context=False, with_memory=True, pages=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        trace = QualityTrace(Path(temporary.name) / "index.sqlite", question="Explain the amount.",
            config=CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="test-secret"),
            policy=AgentPolicy(), capture_context=capture_context)
        memory = {"schema": "prior-source-memory/v1", "draft_is_evidence": False, "blocks": [{
            "memory_id": "memory-1", "text": "Private candidate explanation, still unverified.",
            "evidence_ids": ["ev_page_source"], "kind": "finding", "round_id": "round-1",
            "verification": "unverified", "origins": [{"round_id": "round-1",
                "request_body_sha256": "a" * 64, "excerpt_sha256": "b" * 64,
                "binding": {"relative_path": "root.cbl", "source_sha256": "c" * 64,
                    "start_line": 4, "end_line": 9,
                    "replacement_trace": [{"replacement": "Private replacement metadata."}]}}]}]}
        payload = {"question": "Explain the amount.", "source_context": [{"pages": pages or []}]}
        if with_memory:
            payload["prior_source_memory"] = memory
        messages = [{"role": "system", "content": "Use declared sources."},
                    {"role": "user", "content": json.dumps(payload)}]
        body = json.dumps({"messages": messages}).encode()
        trace.prepare(stage="investigate", payload=payload, messages=messages)
        trace.observe(body)
        return trace.data["rounds"][0], memory, body

    def test_default_trace_records_memory_provenance_without_candidate_text(self):
        row, memory, body = self.observe()
        serialized = json.dumps(row)
        self.assertNotIn("Private candidate explanation", serialized)
        self.assertNotIn("Private replacement metadata", serialized)
        self.assertNotIn("context", row)
        self.assertEqual(row["sources"], [])
        self.assertEqual(row["request_body_sha256"], hashlib.sha256(body).hexdigest())
        inherited = row["prior_source_memory"]
        block = inherited["blocks"][0]
        self.assertEqual(block["evidence_ids"], ["ev_page_source"])
        self.assertEqual(block["verification"], "unverified")
        self.assertEqual(block["text_sha256"], hashlib.sha256(memory["blocks"][0]["text"].encode()).hexdigest())
        self.assertEqual(block["origins"][0]["request_body_sha256"], "a" * 64)
        self.assertEqual(block["origins"][0]["relative_path"], "root.cbl")
        self.assertEqual(len(block["origins"][0]["binding_sha256"]), 64)

    def test_capture_opt_in_retains_exact_memory_snapshot_separately_from_sources(self):
        row, memory, _ = self.observe(capture_context=True)
        self.assertEqual(row["context"]["prior_source_memory"], memory)
        self.assertEqual(row["context"]["sources"], [])
        memory["blocks"][0]["text"] = "Changed after request."
        self.assertNotEqual(row["context"]["prior_source_memory"], memory)

    def test_legacy_requests_do_not_gain_memory_diagnostics(self):
        row, _, _ = self.observe(capture_context=True, with_memory=False)
        self.assertNotIn("prior_source_memory", row)
        self.assertNotIn("prior_source_memory", row["context"])

    def test_source_binding_fingerprint_distinguishes_copy_replacements(self):
        page = {"evidence_id": "ev_page_source", "relative_path": "fields.cpy",
                "source_sha256": "c" * 64, "start_line": 1, "end_line": 1,
                "source_text": "01 INPUT-AMOUNT PIC 9(5).",
                "include_chain": [{"relative_path": "root.cbl", "line": 8, "source_hash": "d" * 64}],
                "replacement_trace": [{"from": "INPUT-AMOUNT", "to": "FIRST-AMOUNT"}]}
        first, _, _ = self.observe(with_memory=False, pages=[page])
        changed = {**page, "replacement_trace": [{"from": "INPUT-AMOUNT", "to": "SECOND-AMOUNT"}]}
        second, _, _ = self.observe(with_memory=False, pages=[changed])
        self.assertEqual(first["sources"][0]["excerpt_sha256"], second["sources"][0]["excerpt_sha256"])
        self.assertNotEqual(first["sources"][0]["binding_sha256"], second["sources"][0]["binding_sha256"])
        self.assertNotIn("FIRST-AMOUNT", json.dumps(first))

    def test_source_diagnostics_retain_incomplete_excerpt_boundary(self):
        page = {"evidence_id": "ev_page_source", "relative_path": "entry.cbl",
                "source_sha256": "c" * 64, "start_line": 4, "end_line": 4,
                "source_text": "CALL 'SERVICE' USING", "span_truncated": True}
        row, _, _ = self.observe(capture_context=True, with_memory=False, pages=[page])
        self.assertIs(row["sources"][0].get("span_truncated"), True)
        self.assertIs(row["context"]["sources"][0].get("span_truncated"), True)

    def test_recorded_round_is_a_snapshot_of_mutable_request_and_response_data(self):
        with tempfile.TemporaryDirectory() as folder:
            trace = QualityTrace(Path(folder) / "index.sqlite", question="Explain ENTRY.",
                config=CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="test-secret"),
                policy=AgentPolicy(), capture_context=True)
            page = {"evidence_id": "ev_page_source", "relative_path": "fields.cpy",
                "source_sha256": "c" * 64, "start_line": 1, "end_line": 1,
                "source_text": "01 INPUT-AMOUNT PIC 9(5).",
                "include_chain": [{"relative_path": "entry.cbl", "line": 8}],
                "semantic_roles": ["input"]}
            history = {"role": "assistant", "content": "The input is an amount."}
            trims = [{"reason": "request_budget", "removed_ids": ["ev_page_old"]}]
            payload = {"question": "Explain ENTRY.", "source_context": [{"pages": [page]}]}
            messages = [{"role": "system", "content": "Use declared sources."}, history,
                        {"role": "user", "content": json.dumps(payload)}]
            body = json.dumps({"messages": messages}).encode()
            trace.prepare(stage="investigate", payload=payload, messages=messages, trim_events=trims)
            trace.observe(body)
            usage = {"total_tokens": 20, "completion_tokens_details": {"reasoning_tokens": 4}}
            trace.finish_round(finish_reason="stop", usage=usage)
            action = {"read": [{"relative_path": "entry.cbl", "start_line": 1}]}
            cursor = {"next_start_line": 12, "remaining_ranges": [[12, 20]]}
            trace.add_tool_result(action=action, actual_result_ids=["ev_page_next"], open_read_cursor=cursor)
            expected = deepcopy(trace.data["rounds"][0])

            page["include_chain"][0]["line"] = 80
            page["semantic_roles"].append("output")
            history["content"] = "A revised history message."
            trims[0]["removed_ids"].clear()
            usage["completion_tokens_details"]["reasoning_tokens"] = 40
            action["read"][0]["start_line"] = 10
            cursor["remaining_ranges"][0][0] = 15

            saved = json.loads(Path(trace.save(final={"stop_reason": "sufficient_material"})).read_text())
            self.assertEqual(saved["rounds"][0], expected)
            self.assertEqual(saved["rounds"][0]["request_body_sha256"], hashlib.sha256(body).hexdigest())


if __name__ == "__main__":
    unittest.main()
