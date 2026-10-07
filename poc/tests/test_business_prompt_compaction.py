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

    def test_small_payload_is_not_expanded_with_encoding_overhead(self):
        payload = self.payload()
        payload["source_context"][0]["call_chain"]["links"] = []
        payload["source_context"][0]["outline"] = []
        payload["business_map"]["relations"] = []
        self.assertIs(business_chat._compact_prompt_payload(payload), payload)

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
                requests.append((len(request.body), wire))
                page = next(p for p in wire["source_context"][0]["pages"]
                            if p["relative_path"] == "programs/service-entry.cbl" and p["start_line"] == 1)
                answer = ("金额大于5000时进入复核，不超过5000时获批。更新前先恢复并检查状态，"
                          "之后请求保存并检查状态；调用本身不能证明实际持久化成功。"
                          f"[{page['evidence_id']}]")
                return TransportResponse(200, json.dumps({"choices": [{"message": {
                    "role": "assistant", "content": answer}, "finish_reason": "stop"}]}))

            for compact in (False, True):
                project = business_chat._compact_prompt_payload if compact else lambda value: value
                with mock.patch.object(business_chat, "_compact_prompt_payload", side_effect=project):
                    output = business_chat.run_business_chat(question, database, source, config,
                        framework_reference_path="", transport=transport, capture_context=True,
                        policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))
                self.assertEqual(output["agent_result"]["metrics"]["model_requests"], 1)
                trace = json.loads(Path(output["agent_result"]["metrics"]["quality_trace_path"]).read_text())
                traces.append(trace["rounds"][0])
            before_bytes, before = requests[0]
            after_bytes, after = requests[1]
            self.assertGreater(before_bytes - after_bytes, before_bytes * 0.10)
            self.assertEqual(encoded(decoded_navigation(after)), encoded(before))
            self.assertEqual(traces[0]["sources"], traces[1]["sources"])
            self.assertEqual(traces[0]["context"]["sources"], traces[1]["context"]["sources"])
            self.assertEqual(after_bytes, traces[1]["request_bytes"])


if __name__ == "__main__":
    unittest.main()
