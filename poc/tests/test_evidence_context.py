from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evidence_context import EvidenceContext
from agent_policy import AgentPolicy
from business_chat import _fit_request, run_business_chat
from company_api import CompanyAPIConfig


def page(identifier, start, text, **extra):
    return {"evidence_id": identifier, "relative_path": "entry.cbl", "start_line": start,
            "end_line": start, "source_sha256": "a" * 64, "source_text": text, **extra}


class EvidenceContextTests(unittest.TestCase):
    def test_equivalent_include_chain_sequences_merge_only_observation_metadata(self):
        from source_reading import _identify_page
        chain = [{"relative_path": "entry.cbl", "line": 12}]
        for first_chain, second_chain in (([], ()), ((), []), (chain, tuple(chain)), (tuple(chain), chain)):
            with self.subTest(first=type(first_chain).__name__, empty=not first_chain):
                store = EvidenceContext()
                first = page("", 8, "MOVE 0 TO TOTAL-AMOUNT", include_chain=first_chain,
                    semantic_roles=["result"], selection_reasons=["business_rule"], group_id="group-one")
                first["evidence_id"] = _identify_page(first)
                second = {**first, "include_chain": second_chain, "semantic_roles": ["input"],
                    "selection_reasons": ["business_context"], "group_id": "group-two"}
                self.assertEqual(_identify_page(second), first["evidence_id"])
                self.assertEqual(store.accept({"pages": [first]}, "search"), [first])
                self.assertEqual(store.accept({"pages": [second]}, "business_context"), [])
                self.assertEqual(len(store.pages), 1)
                retained = store.pages[first["evidence_id"]]
                self.assertEqual(retained["include_chain"], first_chain)
                self.assertEqual(retained["source_text"], first["source_text"])
                self.assertEqual(retained["semantic_roles"], ["result", "input"])
                self.assertEqual(retained["selection_reasons"], ["business_rule", "business_context"])
                self.assertEqual(retained["group_ids"], ["group-one", "group-two"])

    def test_same_evidence_id_still_rejects_authority_and_copy_instance_changes(self):
        original = page("one-page", 8, "MOVE 0 TO TOTAL-AMOUNT",
            include_chain=[{"relative_path": "entry.cbl", "line": 12}])
        changes = (
            {"relative_path": "other.cbl"}, {"start_line": 9}, {"end_line": 9},
            {"source_sha256": "b" * 64}, {"source_text": "MOVE 9 TO TOTAL-AMOUNT"},
            {"include_chain": [{"relative_path": "other.cbl", "line": 12}]},
            {"include_chain": [{"relative_path": "entry.cbl", "line": 24}]},
            {"include_chain": ()}, {"include_chain": None}, {"include_chain": ""},
        )
        for changed in changes:
            with self.subTest(changed=changed):
                store = EvidenceContext()
                store.accept({"pages": [original]}, "search")
                with self.assertRaisesRegex(ValueError, "EVIDENCE_ID_CONTENT_MISMATCH"):
                    store.accept({"pages": [{**original, **changed}]}, "business_context")
                self.assertEqual(store.pages[original["evidence_id"]]["source_text"], original["source_text"])
        for empty in ([], ()):
            for invalid in (None, ""):
                with self.subTest(empty=empty, invalid=invalid):
                    store = EvidenceContext()
                    first = {**original, "include_chain": empty}
                    store.accept({"pages": [first]}, "search")
                    with self.assertRaisesRegex(ValueError, "EVIDENCE_ID_CONTENT_MISMATCH"):
                        store.accept({"pages": [{**first, "include_chain": invalid}]}, "business_context")

    def test_default_conversation_initialization_accepts_equivalent_provenance_without_network(self):
        from business_index import build_business_index
        from repository_discovery import ensure_repository_search
        from tools.desktop_acceptance_scenarios import CONVERSATION_QUESTIONS, _conversation_source

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "report.cbl").write_bytes(_conversation_source().encode("utf-8"))
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True, quiet=True)
            ensure_repository_search(database, source)
            config = CompanyAPIConfig("https://gateway.example.invalid/v1", "offline-model", api_key="unused")
            result = run_business_chat(CONVERSATION_QUESTIONS[0], database, source, config,
                allow_network=False, framework_reference_path="",
                policy=AgentPolicy(max_initial_context_seconds=15, max_local_analysis_seconds=30))

        self.assertEqual(result["reason_code"], "NETWORK_DISABLED")
        answer = result["agent_result"]
        self.assertEqual(answer["metrics"]["model_requests"], 0)
        self.assertNotIn("semantic_scope_unavailable", {item["reason"] for item in answer["boundaries"]})
        self.assertEqual(sum(row["tool"] == "business_context" for row in answer["tool_trace"]), 4)

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
