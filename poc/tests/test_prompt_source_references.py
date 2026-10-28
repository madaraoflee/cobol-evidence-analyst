"""Wire citations follow the exact source pages delivered to the model."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from prompt_projection import bind_prompt_source_references
from repository_discovery import ensure_repository_search


class PromptSourceReferenceTests(unittest.TestCase):
    def payload(self):
        text = "PROCEDURE DIVISION.\nMOVE 1 TO RESULT-CODE.\nGOBACK."
        page = {"relative_path": "rule.cbl", "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "start_line": 1, "end_line": 3, "source_text": text, "evidence_id": "full-page"}
        ref = {key: page[key] for key in ("relative_path", "source_sha256")}
        ref.update(start_line=2, end_line=2, evidence_id="old-excerpt", include_chain=[], replacement_trace=[])
        return {"source_context": [{"pages": [page]}], "evidence_groups": [{"semantic_analysis": {
            "slice_statements": [{"source_refs": [ref]}]}}]}

    @staticmethod
    def reference(payload):
        return payload["evidence_groups"][0]["semantic_analysis"]["slice_statements"][0]["source_refs"][0]

    def test_covering_page_changes_only_wire_reference_id(self):
        payload = self.payload()
        saved = deepcopy(payload)
        wire = bind_prompt_source_references(payload)
        self.assertEqual(self.reference(wire), {**self.reference(payload), "evidence_id": "full-page"})
        self.assertEqual(wire["source_context"], payload["source_context"])
        self.assertEqual(payload, saved)
        self.assertEqual(bind_prompt_source_references(wire), wire)
        # Every projection is rebuilt after fitting: a previously visible page
        # does not authorize an old citation once that page leaves the request.
        payload["source_context"][0]["pages"] = []
        self.assertEqual(self.reference(bind_prompt_source_references(payload))["evidence_id"], "old-excerpt")

    def test_uncovered_or_different_source_instance_cannot_borrow_a_page(self):
        changes = ({"relative_path": "other.cbl"}, {"source_sha256": "b" * 64},
                   {"source_sha256": ""}, {"start_line": 4, "end_line": 4},
                   {"start_line": 0}, {"end_line": 0}, {"span_truncated": True},
                   {"include_chain": [{"relative_path": "caller.cbl", "line": 8}]},
                   {"replacement_trace": ["BASE BY TARGET"]}, {"copy_instance": "different"},
                   {"excerpt_sha256": "c" * 64})
        for change in changes:
            with self.subTest(change=change):
                payload = self.payload()
                self.reference(payload).update(change)
                self.assertEqual(self.reference(bind_prompt_source_references(payload))["evidence_id"], "old-excerpt")
        for change in ({"span_truncated": True}, {"source_text": "only one line"}, {"source_text": ""}):
            with self.subTest(page=change):
                payload = self.payload()
                payload["source_context"][0]["pages"][0].update(change)
                self.assertEqual(self.reference(bind_prompt_source_references(payload))["evidence_id"], "old-excerpt")
        payload = self.payload()
        self.reference(payload)["excerpt_sha256"] = hashlib.sha256(b"MOVE 1 TO RESULT-CODE.").hexdigest()
        self.assertEqual(self.reference(bind_prompt_source_references(payload))["evidence_id"], "full-page")

    def test_same_preprocessing_context_survives_json_container_conversion(self):
        payload = self.payload()
        page = payload["source_context"][0]["pages"][0]
        ref = self.reference(payload)
        page.update(include_chain=[{"relative_path": "caller.cbl", "line": 8}],
                    replacement_trace=["BASE BY TARGET"], copy_instance="copy-1")
        ref.update(include_chain=tuple(page["include_chain"]),
                   replacement_trace=tuple(page["replacement_trace"]), copy_instance="copy-1")
        self.assertEqual(self.reference(bind_prompt_source_references(payload))["evidence_id"], "full-page")
        ref["replacement_trace"] = ("BASE BY OTHER",)
        self.assertEqual(self.reference(bind_prompt_source_references(payload))["evidence_id"], "old-excerpt")

    def test_actual_request_semantic_reference_is_a_valid_answer_citation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            content = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. CHECKORDER.\n"
                "*> Order eligibility check\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n"
                "01 ORDER-AMOUNT PIC 9(5).\n01 RESULT-CODE PIC XX.\nPROCEDURE DIVISION.\n"
                "IF ORDER-AMOUNT > ZERO MOVE '00' TO RESULT-CODE ELSE MOVE '90' TO RESULT-CODE END-IF.\n"
                "GOBACK.\n")
            (source / "a-policy.cbl").write_text(content, encoding="utf-8")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", quiet=True)
            ensure_repository_search(database, source)
            requests, cited = [], []

            def transport(request):
                payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
                requests.append(payload)
                if payload["retrieval_status"]["state"] == "needs_discovery":
                    reply = '{"search":["ORDER-AMOUNT"]}'
                else:
                    # Follow the reference exposed by semantic navigation,
                    # including its shared-fields representation on the wire.
                    facts = payload.get("shared_navigation_facts", {})
                    refs = []
                    def visit(value):
                        if isinstance(value, list):
                            for item in value: visit(item)
                        elif isinstance(value, dict):
                            if "navigation_fact_ref" in value:
                                visit(facts[value["navigation_fact_ref"]])
                            if value.get("relative_path") == "a-policy.cbl" and value.get("start_line") == 9 and value.get("evidence_id"):
                                refs.append(value)
                            for key, item in value.items():
                                if key != "navigation_fact_ref": visit(item)
                    visit(payload["evidence_groups"])
                    self.assertTrue(refs)
                    pages = [page for bundle in payload["source_context"] for page in bundle["pages"]]
                    page = next(page for page in pages if page["relative_path"] == "a-policy.cbl")
                    self.assertEqual(page["source_text"].strip(), content.strip())
                    self.assertTrue(all(ref["evidence_id"] == page["evidence_id"] for ref in refs))
                    cited.append(refs[0]["evidence_id"])
                    reply = f"订单金额大于零时结果码为00，否则为90；这段源码没有给出金额初值。[{cited[-1]}]"
                return TransportResponse(200, json.dumps({"choices": [{"message": {
                    "role": "assistant", "content": reply}, "finish_reason": "stop"}]}))

            result = run_business_chat("这份源码里订单金额是怎样检查并决定结果的？请面向业务新人解释。",
                database, source, CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="test-secret"),
                transport=transport, framework_reference_path="", policy=AgentPolicy(max_model_requests=2))["agent_result"]
            self.assertTrue(cited)
            self.assertIn(f"[{cited[0]}]", result["answer"])
            self.assertFalse(any(row.get("unsupported_citation_ids") for row in result["boundaries"]))
            self.assertNotIn("未供应的引用", result["answer"])


if __name__ == "__main__":
    unittest.main()
