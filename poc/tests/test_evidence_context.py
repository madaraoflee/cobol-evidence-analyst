from __future__ import annotations

from pathlib import Path
import sys
import unittest
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evidence_context import EvidenceContext
from agent_policy import AgentPolicy
from business_chat import _fit_request
from company_api import CompanyAPIConfig


def page(identifier, start, text, **extra):
    return {"evidence_id": identifier, "relative_path": "entry.cbl", "start_line": start,
            "end_line": start, "source_sha256": "a" * 64, "source_text": text, **extra}


class EvidenceContextTests(unittest.TestCase):
    def test_late_irrelevant_page_does_not_evict_result_condition_or_input(self):
        store = EvidenceContext()
        store.accept({"pages": [page("result", 90, "COMPUTE RESULT = INPUT * 2", semantic_roles=["result"]),
                                page("condition", 20, "IF INPUT > 4", semantic_roles=["condition"]),
                                page("input", 10, "MOVE REQUEST TO INPUT", semantic_roles=["input"])]}, "semantic")
        for index in range(20):
            store.accept({"pages": [page(f"noise{index}", 100 + index, "CONTINUE " * 20)]}, "search")
        selected = store.selected_pages(90)
        self.assertEqual({p["evidence_id"] for p in selected}, {"result", "condition", "input"})
        self.assertEqual(len(store.retrieved_ids), 23)

    def test_read_cursor_advances_only_by_supplied_range_and_stops_on_no_progress(self):
        store = EvidenceContext()
        request = {"relative_path": "entry.cbl", "start_line": 1, "end_line": 500}
        store.accept({"requested_range": request, "pages": [page("first", 1, "A")],
                      "next_start_line": 2, "range_complete": False}, "read")
        task = next(iter(store.tasks.values()))
        self.assertEqual(task.next_start_line, 2)
        store.accept({"requested_range": {**request, "start_line": 2}, "pages": [],
                      "next_start_line": 300, "range_complete": True}, "read")
        self.assertEqual(task.state, "stalled")

    def test_two_outlines_and_visible_callsite_are_recomputed(self):
        store = EvidenceContext()
        link = {"relation_id": "r1", "caller_path": "entry.cbl", "caller_start_line": 5,
                "caller_end_line": 5, "target_path": "result.cbl", "target_start_line": 1,
                "caller_evidence_ids": ["old"]}
        store.accept({"pages": [page("one", 5, "CALL 'RESULT'", semantic_roles=["callsite"])],
                      "call_chain": {"links": [link]},
                      "outline": [{"relative_path": "entry.cbl", "anchor": 5}]}, "search")
        store.accept({"pages": [page("two", 90, "MOVE RESULT TO OUTPUT")],
                      "outline": [{"relative_path": "entry.cbl", "anchor": 90}]}, "read")
        bundle = store.bundle([store.pages["two"]])[0]
        self.assertEqual(len(bundle["outline"]), 2)
        self.assertEqual(bundle["call_chain"]["links"][0]["caller_evidence_ids"], [])
        self.assertTrue(bundle["call_chain"]["links"][0]["requires_source_read"])

    def test_final_byte_trim_marks_partly_supplied_group_in_actual_request(self):
        critical = page("critical", 1, "COMPUTE RESULT = INPUT * 2", semantic_roles=["result"])
        noise = page("noise", 2, "UNRELATED " * 5000)
        payload = {"question": "Explain RESULT", "repository": {}, "business_map": {},
            "source_context": [{"pages": [critical, noise],
                "call_chain": {"links": [], "omitted_links": 0}, "outline": [], "notices": []}],
            "framework_references": [], "evidence_groups": [{"group_id": "group-1",
                "required_evidence_ids": ["critical", "noise"], "open_frontier": []}]}
        config = CompanyAPIConfig("https://gateway.example.invalid/v1", "offline-model", api_key="offline-only")
        messages, size = _fit_request(config, payload, [], AgentPolicy(max_request_bytes=32768))
        actual = json.loads(messages[-1]["content"])
        self.assertLessEqual(size, 32768)
        self.assertEqual([row["evidence_id"] for row in actual["source_context"][0]["pages"]], ["critical"])
        self.assertFalse(actual["evidence_groups"][0]["complete_text_supplied"])
        self.assertEqual(actual["evidence_groups"][0]["visible_evidence_ids"], ["critical"])

    def test_adjacent_visible_ranges_complete_an_outline_unit(self):
        payload = {"source_context": [{"pages": [page("first", 5, "IF INPUT"), page("second", 6, "MOVE RESULT")],
            "call_chain": {"links": [], "omitted_links": 0},
            "outline": [{"relative_path": "entry.cbl", "units": [{"start_line": 5, "end_line": 6,
                "complete_text_supplied": False}]}]}], "framework_references": []}
        EvidenceContext.reconcile_payload(payload)
        self.assertTrue(payload["source_context"][0]["outline"][0]["units"][0]["complete_text_supplied"])


if __name__ == "__main__":
    unittest.main()
