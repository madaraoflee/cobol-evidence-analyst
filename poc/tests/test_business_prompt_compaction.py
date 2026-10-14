"""Lossless navigation encoding must not change source authority or visibility."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
import business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search


def decoded_navigation(payload):
    """Independent consumer of the documented positional encoding."""
    result = deepcopy(payload)
    result.pop("navigation_encoding", None)

    def expand(container, name):
        columns = container.pop(name + "_columns", None)
        if columns is None:
            return
        for row in container[name]:
            values = row.pop("_values")
            if len(columns) != len(values) or set(columns) & set(row):
                raise AssertionError("Invalid navigation record")
            row.update(zip(columns, values))

    for context in result.get("source_context", []):
        expand(context.get("call_chain", {}), "links")
        for outline in context.get("outline", []):
            expand(outline, "units")
    expand(result.get("business_map", {}), "relations")
    for group in result.get("evidence_groups", []):
        analysis = group.get("semantic_analysis", {})
        expand(analysis, "slice_statements")
        for tool_result in analysis.get("tool_results", []):
            expand(tool_result, "statements")
    return result


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class BusinessPromptCompactionTests(unittest.TestCase):
    @staticmethod
    def payload():
        text = "IF INPUT-AMOUNT > ZERO\nMOVE 7 TO RESULT-AMOUNT\nEND-IF."
        pages = [{"evidence_id": f"ev:source-{index}", "relative_path": "rule.cbl",
            "source_sha256": hashlib.sha256((text + str(index % 2)).encode()).hexdigest(),
            "start_line": 1, "end_line": 3, "source_text": text,
            "include_chain": [{"relative_path": "entry.cbl", "line": index + 1}],
            "selection_reasons": ["business_context"], "semantic_roles": ["condition"]}
            for index in range(4)]
        links = [{"relation_id": f"rel:{index}", "caller_path": "rule.cbl",
            "caller_start_line": 1, "caller_end_line": 3,
            "relation_type": "CALLS_STATIC", "target_name": f"WORKER-{index}",
            "target_path": f"worker-{index}.cbl", "target_start_line": 1,
            "target_end_line": 40, "target_source_status": "indexed",
            "resolution": "confirmed", "runtime_verified": False,
            "parameter_binding_verified": False, "caller_evidence_ids": [],
            "target_evidence_ids": [], "requires_source_read": True}
            for index in range(40)]
        units = [{"unit_type": "Paragraph", "name": f"STEP-{index}",
            "program_name": "REQUEST-HANDLER", "start_line": index + 1,
            "end_line": index + 2, "context_role": "source_structure",
            "complete_text_supplied": False} for index in range(16)]
        return {"question": "请解释请求的处理条件。", "repository": {},
            "source_context": [{"pages": pages,
                "call_chain": {"links": links, "omitted_links": 0},
                "outline": [{"relative_path": "rule.cbl", "units": units}], "notices": []}],
            "business_map": {"relations": [{"caller_path": "rule.cbl", "caller_program": "REQUEST-HANDLER",
                "relation_type": "CALLS_STATIC", "target_name": f"WORKER-{index}",
                "target_path": f"worker-{index}.cbl", "resolution": "confirmed",
                "caller_line": index + 1} for index in range(40)]},
            "framework_references": [], "question_investigation": {"required_items": []}}

    def test_every_navigation_value_round_trips_without_mutating_authority(self):
        payload = self.payload()
        links = payload["source_context"][0]["call_chain"]["links"]
        links[0]["optional"] = None
        links[1]["optional"] = False
        links[2]["optional"] = 0
        links[3]["optional"] = {"unicode": "例外", "values": [None, False, 0]}
        original = deepcopy(payload)
        compact = business_chat._compact_prompt_payload(payload)
        self.assertIn("navigation_encoding", compact)
        self.assertEqual(encoded(decoded_navigation(compact)), encoded(original))
        self.assertEqual(encoded(payload), encoded(original))
        self.assertLess(len(encoded(compact)), len(encoded(original)))
        self.assertEqual(compact["source_context"][0]["pages"], original["source_context"][0]["pages"])
        for original_link, link in zip(links, compact["source_context"][0]["call_chain"]["links"]):
            for key in ("relation_id", "target_name", "target_source_status", "caller_evidence_ids",
                        "target_evidence_ids", "requires_source_read"):
                self.assertEqual(link[key], original_link[key])

    def test_colliding_values_and_preexisting_encoding_are_not_overwritten(self):
        payload = self.payload()
        payload["source_context"][0]["call_chain"]["links"][0]["_values"] = ["original-field"]
        compact = business_chat._compact_prompt_payload(payload)
        self.assertEqual(compact["source_context"][0]["call_chain"], payload["source_context"][0]["call_chain"])
        self.assertEqual(encoded(decoded_navigation(compact)), encoded(payload))
        payload["navigation_encoding"] = {"format": "existing"}
        self.assertEqual(business_chat._compact_prompt_payload(payload), payload)

    def test_semantic_occurrences_guards_and_unknowns_round_trip(self):
        payload = self.payload()
        statements = [{"occurrence_id": f"write:{index}", "kind": "MOVE",
            "reads": ["INPUT-AMOUNT"], "writes": ["RESULT-AMOUNT"],
            "guards": [{"expression": "INPUT-AMOUNT > ZERO", "branch": index % 2}],
            "source_ref": {"source_sha256": "a" * 64, "start_line": index + 1},
            "evidence_ids": [f"ev:source-{index % 4}"], "effects_unknown": False}
            for index in range(8)]
        statements[1]["optional"] = None
        payload["evidence_groups"] = [{"semantic_analysis": {
            "slice_statements": statements,
            "tool_results": [{"statements": deepcopy(statements), "unknowns": ["runtime_input"]}],
            "query_count": 19}}]
        original = deepcopy(payload)
        compact = business_chat._compact_prompt_payload(payload)
        self.assertEqual(decoded_navigation(compact), original)
        self.assertEqual(payload, original)
        analysis = compact["evidence_groups"][0]["semantic_analysis"]
        for key, owner in (("slice_statements", analysis),
                           ("statements", analysis["tool_results"][0])):
            self.assertIn(key + "_columns", owner)
            self.assertEqual([row["occurrence_id"] for row in owner[key]],
                             [row["occurrence_id"] for row in statements])
        self.assertEqual(analysis["query_count"], 19)
        self.assertEqual(analysis["tool_results"][0]["unknowns"], ["runtime_input"])

    def test_small_payload_is_not_expanded_with_encoding_overhead(self):
        payload = self.payload()
        payload["source_context"][0]["call_chain"]["links"] = []
        payload["source_context"][0]["outline"] = []
        payload["business_map"]["relations"] = []
        self.assertIs(business_chat._compact_prompt_payload(payload), payload)

    def test_budget_compaction_preserves_referent_and_manifest_keeps_bindings(self):
        payload = self.payload()
        payload["budget_wire_compact"] = True
        payload["session_subject"] = {"schema": "conversation-subject/v1", "status": "resolved",
            "root_paths": ["rule.cbl"], "focus_entities": ["ACCOUNT-SUM"],
            "ordinal_subjects": [{"ordinal": 1, "entities": ["PAGE-SUM"]},
                                 {"ordinal": 2, "entities": ["ACCOUNT-SUM"]}],
            "entity_bindings": [{"qualified_name": "REPORT::ACCOUNT-SUM", "source_sha256": "a" * 64}]}
        payload["question_investigation"]["analysis_plan"] = {"entities": ["ACCOUNT-SUM"]}
        original = deepcopy(payload["session_subject"])
        wire = business_chat._prompt_payload(payload)
        self.assertTrue(wire["question_investigation"]["analysis_plan_omitted"])
        self.assertEqual(wire["session_subject"]["focus_entities"], ["ACCOUNT-SUM"])
        self.assertNotIn("entity_bindings", wire["session_subject"])
        self.assertEqual(payload["session_subject"], original)
        manifest = business_chat._request_manifest(payload)
        payload["session_subject"]["focus_entities"][:] = ["PAGE-SUM"]
        self.assertEqual(manifest["session_subject"], original)

    def test_budget_projection_preserves_full_local_origin_proof_and_wire_evidence(self):
        payload = self.payload()
        payload["budget_wire_compact"] = True
        payload["question_investigation"]["required_items"] = [{
            "kind": "inputs", "status": "SATISFIED", "evidence_ids": ["ev:source-0"],
            "source_flow_origins": [{"relative_path": "rule.cbl", "field": "INPUT-AMOUNT",
                "status": "closed", "basis": "supplied_source_flow", "unknown_reasons": [],
                "execution_verified": False, "runtime_value_known": False,
                "activation_semantics": ["first_activation_or_initial_or_cancelled_program"],
                "reader_occurrences": [{"occurrence_id": "read-1",
                    "source_refs": [{"source_sha256": "a" * 64, "start_line": 4,
                                     "end_line": 4}], "definitions": ["write-1"]}]}]}]
        original = deepcopy(payload)
        wire = business_chat._prompt_payload(payload)
        item = wire["question_investigation"]["required_items"][0]
        self.assertEqual(item["evidence_ids"], ["ev:source-0"])
        self.assertEqual(item["source_flow_origin_occurrences_omitted"], 1)
        self.assertNotIn("reader_occurrences", item["source_flow_origins"][0])
        self.assertIs(item["source_flow_origins"][0]["execution_verified"], False)
        self.assertIs(item["source_flow_origins"][0]["runtime_value_known"], False)
        self.assertEqual(item["source_flow_origins"][0]["activation_semantics"],
                         ["first_activation_or_initial_or_cancelled_program"])
        self.assertEqual(wire["source_context"][0]["pages"], original["source_context"][0]["pages"])
        self.assertEqual(payload, original)
        self.assertEqual(payload["question_investigation"]["required_items"][0]
                         ["source_flow_origins"][0]["reader_occurrences"][0]["definitions"], ["write-1"])

    def test_fit_rebuilds_tables_and_brief_after_source_removal(self):
        payload = self.payload()
        config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only")
        policy = AgentPolicy(max_request_bytes=230000)

        def investigation(pages):
            ids = [page["evidence_id"] for page in pages]
            return {"required_items": [{"kind": "business_steps", "status": "SATISFIED" if ids else "OPEN",
                "evidence_ids": ids, "candidate_count": len(ids)}], "open_gaps": []}

        first, first_size = business_chat._fit_request(config, payload, [], policy,
                                                      investigation_builder=investigation)
        wire = json.loads(first[-1]["content"])
        self.assertIn("navigation_encoding", wire)
        restored = decoded_navigation(wire)
        restored.pop("response_contract")
        restored.pop("response_mode")
        restored.pop("source_selection")
        self.assertTrue(wire["source_selection"]["source_supplied"])
        self.assertEqual(wire["source_selection"]["page_count"], len(payload["source_context"][0]["pages"]))
        self.assertEqual(encoded(restored), encoded(payload))
        self.assertEqual(first_size, len(json.dumps({"model": config.chat_model, "messages": first,
            "max_tokens": config.max_output_tokens}, ensure_ascii=False, separators=(",", ":")).encode()))
        del payload["source_context"][0]["pages"][:]
        second, _ = business_chat._fit_request(config, payload, [], policy,
                                             investigation_builder=investigation)
        actual = json.loads(second[-1]["content"])
        restored = decoded_navigation(actual)
        restored.pop("response_contract")
        restored.pop("response_mode")
        restored.pop("source_selection")
        self.assertFalse(actual["source_selection"]["source_supplied"])
        self.assertEqual(actual["source_selection"]["page_count"], 0)
        self.assertEqual(encoded(restored), encoded(payload))
        self.assertEqual(restored["question_investigation"]["required_items"][0]["status"], "OPEN")
        self.assertTrue(all(link["requires_source_read"] for link in restored["source_context"][0]["call_chain"]["links"]))
        self.assertTrue(all(not unit["complete_text_supplied"] for unit in restored["source_context"][0]["outline"][0]["units"]))
        material = restored["business_analysis_brief"]["supplied_material"]
        self.assertTrue(all(not item["supplied_reference_ids"] for item in material))
        self.assertTrue(all(item["status"] != "SATISFIED" for item in material))

    def test_real_fixture_request_shrinks_with_identical_source_citations_and_trace(self):
        source = Path(__file__).resolve().parents[1] / "fixtures/framework-workbench/source"
        config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only")
        question = "When does the service request move to review, and what happens before saving it?"
        requests, traces = [], []
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "index.sqlite"
            build_business_index(source, database, verify_content=True)
            ensure_repository_search(database, source)

            def transport(request):
                body = json.loads(request.body)
                wire = json.loads(body["messages"][-1]["content"])
                requests.append((request.body, body, wire))
                page = next(p for p in wire["source_context"][0]["pages"]
                            if p["relative_path"] == "programs/service-entry.cbl" and p["start_line"] == 1)
                answer = ("金额大于5000时进入复核，不超过5000时获批。更新前先恢复并检查状态，"
                          "之后请求保存并检查状态；调用本身不能证明实际持久化成功。"
                          f"[{page['evidence_id']}]")
                return TransportResponse(200, json.dumps({"choices": [{"message": {
                    "role": "assistant", "content": answer}, "finish_reason": "stop"}]}))

            original_project = business_chat._compact_prompt_payload
            for compact in (False, True):
                def project(value, compact=compact):
                    canonical = deepcopy(value)
                    projected = original_project(value) if compact else value
                    # Compare the same input, including its live SQL counters.
                    # Encoding must preserve every value and leave authority intact.
                    self.assertEqual(encoded(decoded_navigation(projected)), encoded(canonical))
                    self.assertEqual(encoded(value), encoded(canonical))
                    return projected
                with mock.patch.object(business_chat, "_compact_prompt_payload", side_effect=project):
                    output = business_chat.run_business_chat(question, database, source, config,
                        framework_reference_path="", transport=transport, capture_context=True,
                        policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))
                self.assertEqual(output["agent_result"]["metrics"]["model_requests"], 1)
                trace = json.loads(Path(output["agent_result"]["metrics"]["quality_trace_path"]).read_text())
                traces.append(trace["rounds"][0])
            self.assertEqual(len(requests), 2)
            self.assertIn("navigation_encoding", requests[1][2])
            for (actual_bytes, body, wire), trace in zip(requests, traces):
                # The local deadline can produce different material in cold
                # and warm investigations. Compare both encodings of THIS
                # actual request, including its counters and unknowns.
                canonical = decoded_navigation(wire)
                compacted = original_project(canonical)
                self.assertEqual(encoded(decoded_navigation(compacted)), encoded(canonical))
                if "navigation_encoding" in wire:
                    self.assertEqual(compacted, wire)

                def request_bytes(payload):
                    envelope = deepcopy(body)
                    envelope["messages"][-1]["content"] = json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":"))
                    return json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode()

                before_bytes = len(request_bytes(canonical))
                after_bytes = len(request_bytes(compacted))
                self.assertGreater(before_bytes - after_bytes, before_bytes * 0.10,
                                   f"same-request bytes: {before_bytes} -> {after_bytes}")
                self.assertEqual(len(actual_bytes), trace["request_bytes"])
                self.assertEqual(hashlib.sha256(actual_bytes).hexdigest(), trace["request_body_sha256"])
                pages = canonical["source_context"][0]["pages"]
                self.assertEqual(len(pages), len(trace["sources"]))
                self.assertEqual(len(pages), len(trace["context"]["sources"]))
                for page, captured, context in zip(pages, trace["sources"], trace["context"]["sources"]):
                    self.assertEqual(page["evidence_id"], captured["evidence_id"])
                    self.assertEqual(page["relative_path"], captured["path"])
                    self.assertEqual(page["source_sha256"], captured["source_sha256"])
                    self.assertEqual(page["start_line"], captured["start_line"])
                    self.assertEqual(page["end_line"], captured["end_line"])
                    self.assertEqual(page.get("include_chain", []), captured["include_chain"])
                    self.assertEqual(page["source_text"], context["excerpt"])
                    self.assertEqual(hashlib.sha256(page["source_text"].encode()).hexdigest(),
                                     captured["excerpt_sha256"])


if __name__ == "__main__":
    unittest.main()
