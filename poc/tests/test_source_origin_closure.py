"""Call-return origins close only when each actual witness is really supplied."""
from __future__ import annotations

import copy
from collections import Counter
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_policy import AgentPolicy
from analyze_source import analyze_source
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from question_investigation import _flow_input_origins
from repository_discovery import ensure_repository_search
from semantic_scope import build_business_evidence, prepare_semantic_scope
from semantic_service import SemanticService, FactStore, SERVICE_VERSION
from source_session import QuestionSourceSession
from run_store import RunStore, fingerprint


QUESTION = "ENTRYPOINT 的 FINAL-AMOUNT 从哪里来的？请追踪 MIDDLE、LEAF 的调用参数和正常返回。"


class SourceOriginClosureTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "workspace"
        self.database = self.root / "facts.sqlite"
        self.policy = AgentPolicy(max_model_requests=1, max_answer_revisions=0)
        self.requests = []
        self.write("entry.cbl", "ENTRYPOINT", "01 IN-AMOUNT PIC 9(6)V99.\n01 OUT-AMOUNT PIC 9(6)V99.\n"
            "01 FINAL-AMOUNT PIC 9(6)V99.", "MOVE 4 TO IN-AMOUNT.\nCALL 'MIDDLE' USING IN-AMOUNT OUT-AMOUNT.\n"
            "MOVE OUT-AMOUNT TO FINAL-AMOUNT.\nGOBACK.")
        self.write("middle.cbl", "MIDDLE", "01 MID-IN PIC 9(6)V99.\n01 MID-OUT PIC 9(6)V99.",
            "CALL 'LEAF' USING MID-IN MID-OUT.\nGOBACK.", using="MID-IN MID-OUT")
        self.write("leaf.cbl", "LEAF", "01 PARAM-IN PIC 9(6)V99.\n01 PARAM-OUT PIC 9(6)V99.",
            "COMPUTE PARAM-OUT = PARAM-IN * 2.\nGOBACK.", using="PARAM-IN PARAM-OUT")

    def write(self, path, name, declarations, body, using=None):
        text = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. " + name + ".\nDATA DIVISION.\n" +
            ("LINKAGE SECTION.\n" if using else "WORKING-STORAGE SECTION.\n") + declarations +
            "\nPROCEDURE DIVISION" + (" USING " + using if using else "") + ".\nMAIN.\n" + body + "\n")
        (self.source / path).write_text(text, encoding="utf-8")

    def page(self, path):
        raw = (self.source / path).read_bytes()
        lines = raw.decode().splitlines()
        return {"relative_path": path, "source_sha256": hashlib.sha256(raw).hexdigest(),
                "start_line": 1, "end_line": len(lines), "source_text": "\n".join(lines),
                "evidence_id": "source-" + path, "span_truncated": False}

    def evidence(self):
        build_business_index(self.source, self.database, source_format="free")
        ensure_repository_search(self.database, self.source)
        with QuestionSourceSession(self.database, self.source) as session:
            line = next(number for number, text in enumerate(self.page("entry.cbl")["source_text"].splitlines(), 1)
                        if text.startswith("MOVE OUT-AMOUNT TO FINAL-AMOUNT"))
            anchor = {"relative_path": "entry.cbl", "line": line, "fields": ["OUT-AMOUNT", "FINAL-AMOUNT"]}
            with prepare_semantic_scope(self.database, session, anchors=[anchor], policy=self.policy) as scope:
                group = build_business_evidence(scope, session, anchor=anchor, policy=self.policy)
                service = next(iter(scope._semantic_services.values()))
                occurrence = next((key for key, node in service.nodes.items()
                                   if node.statement == "MOVE OUT-AMOUNT TO FINAL-AMOUNT"), None)
                candidate = {"kind": "business_steps", "relative_path": "entry.cbl", "source_sha256": self.page("entry.cbl")["source_sha256"],
                    "start_line": line, "end_line": line, "statement": "MOVE OUT-AMOUNT TO FINAL-AMOUNT.",
                    "occurrence_count": 1, "reads": ["OUT-AMOUNT"], "writes": ["FINAL-AMOUNT"]}
                return asdict(group), candidate, service.read_origins(occurrence) if occurrence else []

    def test_confirmed_different_formal_names_and_normal_returns_close_the_supplied_origin(self):
        group, candidate, proofs = self.evidence()
        self.assertEqual(proofs[0]["status"], "resolved_source_origin")
        self.assertEqual(len(proofs[0]["call_occurrences"]), 2)
        self.assertFalse(proofs[0]["execution_verified"])
        refs = proofs[0]["source_refs"]
        self.assertEqual({ref["relative_path"] for ref in refs}, {"entry.cbl", "middle.cbl", "leaf.cbl"})
        closed, invalidated, witnesses = _flow_input_origins([candidate],
            [self.page(path) for path in ("entry.cbl", "middle.cbl", "leaf.cbl")], [group])
        self.assertEqual(closed, {("entry.cbl", "OUT-AMOUNT")})
        self.assertEqual(invalidated, set())
        self.assertEqual(witnesses[0]["status"], "source_origin_supplied")
        self.assertEqual(set(witnesses[0]["evidence_ids"]), {"source-entry.cbl", "source-middle.cbl", "source-leaf.cbl"})

    def test_missing_layout_signature_return_wrong_version_or_text_cannot_close(self):
        group, candidate, _ = self.evidence()
        original = [self.page(path) for path in ("entry.cbl", "middle.cbl", "leaf.cbl")]
        mutations = []
        for needle in ("01 PARAM-OUT", "PROCEDURE DIVISION", "GOBACK", "PROGRAM-ID"):
            pages = copy.deepcopy(original)
            page = pages[-1]
            lines = page["source_text"].splitlines()
            line = next(index for index, text in enumerate(lines) if needle in text)
            page["source_text"] = "\n".join([*lines[:line], " " * len(lines[line]), *lines[line + 1:]])
            mutations.append((needle, pages))
        pages = copy.deepcopy(original)
        pages[-1]["source_sha256"] = "0" * 64
        mutations.append(("wrong_version", pages))
        mutations.append(("missing_file", original[:-1]))
        for label, pages in mutations:
            with self.subTest(label=label):
                closed, _, _ = _flow_input_origins([candidate], pages, [group])
                self.assertEqual(closed, set())

    def test_repeated_same_statement_needs_each_occurrence_and_context(self):
        group, candidate, _ = self.evidence()
        repeat = {**candidate, "occurrence_count": 2, "occurrences": [
            {"start_line": candidate["start_line"], "end_line": candidate["end_line"]},
            {"start_line": candidate["start_line"] + 1, "end_line": candidate["end_line"] + 1}]}
        closed, _, _ = _flow_input_origins([repeat],
            [self.page(path) for path in ("entry.cbl", "middle.cbl", "leaf.cbl")], [group])
        self.assertEqual(closed, set())

    def test_large_physical_program_and_many_witness_refs_decode_each_capture_once(self):
        path = self.source / "entry.cbl"
        path.write_text(path.read_text().replace("PROCEDURE DIVISION.",
            "\n".join("*> neutral source separation " + "x" * 80 for _ in range(6000)) + "\nPROCEDURE DIVISION."))
        build_business_index(self.source, self.database, source_format="free")
        ensure_repository_search(self.database, self.source)
        line = next(number for number, text in enumerate(self.page("entry.cbl")["source_text"].splitlines(), 1)
                    if text.startswith("MOVE OUT-AMOUNT"))
        anchor = {"relative_path": "entry.cbl", "line": line}
        reads, explanations = [], []
        original_read, original_explain = Path.read_text, SemanticService.explain_assignment
        with QuestionSourceSession(self.database, self.source) as session:
            captures = {session.capture(name).path for name in ("entry.cbl", "middle.cbl", "leaf.cbl")}
            def counted_read(path, *args, **kwargs):
                if path in captures:
                    reads.append(path)
                return original_read(path, *args, **kwargs)
            def counted_explain(service, occurrence):
                explanations.append(occurrence)
                return original_explain(service, occurrence)
            with prepare_semantic_scope(self.database, session, anchors=[anchor], policy=self.policy) as scope:
                with mock.patch.object(Path, "read_text", new=counted_read), \
                     mock.patch.object(SemanticService, "explain_assignment", new=counted_explain):
                    groups = [build_business_evidence(scope, session, anchor=anchor, policy=self.policy) for _ in range(2)]
                self.assertEqual(Counter(reads), {path: 1 for path in captures})
                self.assertEqual(len(explanations), sum(len(group.semantic_analysis["anchors"]) for group in groups))
                self.assertGreaterEqual(len(groups[0].semantic_analysis["assignments"][0]["read_origins"][0]["source_refs"]), 20)
                self.assertGreater(len(self.page("entry.cbl")["source_text"].splitlines()), 6000)

    def test_v8_flow_cache_does_not_swallow_new_call_origin_or_force_ir_rebuild(self):
        build_business_index(self.source, self.database, source_format="free")
        ensure_repository_search(self.database, self.source)
        line = next(number for number, text in enumerate(self.page("entry.cbl")["source_text"].splitlines(), 1)
                    if text.startswith("MOVE OUT-AMOUNT"))
        anchor = {"relative_path": "entry.cbl", "line": line}
        with QuestionSourceSession(self.database, self.source) as session:
            with prepare_semantic_scope(self.database, session, anchors=[anchor], policy=self.policy) as scope:
                journal = RunStore(self.database.parent / "investigation-runs.sqlite")
                old_key = fingerprint({"scope": scope.scope_key, "path": "entry.cbl",
                    "version": "bounded-semantic-service/v8", "cache_schema": "ordered-flow-nodes-v2"})
                journal.cache("flow", old_key, {"node_order": [], "nodes": {}, "edges": [], "frontier": [],
                    "dependencies": [], "entry": None, "exits": [], "reaching_in": {}, "reaching_out": {},
                    "flow_complete": True})
                group = build_business_evidence(scope, session, anchor=anchor, policy=self.policy)
                proof = group.semantic_analysis["assignments"][0]["read_origins"][0]
                self.assertEqual(proof["status"], "resolved_source_origin")
                new_key = fingerprint({"scope": scope.scope_key, "path": "entry.cbl",
                    "version": SERVICE_VERSION, "cache_schema": "ordered-flow-nodes-v2"})
                self.assertNotEqual(old_key, new_key)
                cached = journal.cached("flow", new_key)
                self.assertTrue(any(node["call_contract"] for node in cached["nodes"].values()))
                self.assertEqual(journal.cached("summary", new_key)["version"], SERVICE_VERSION)
                sidecar_path = scope.database_path
            with prepare_semantic_scope(self.database, session, anchors=[anchor], policy=self.policy) as warm:
                self.assertTrue(warm.cache_hit)
                self.assertEqual(warm.database_path, sidecar_path)

    def test_unknown_leaf_effect_keeps_origin_open_and_local_assignment_fact_available(self):
        path = self.source / "leaf.cbl"
        path.write_text(path.read_text().replace("COMPUTE PARAM-OUT = PARAM-IN * 2.",
            "COMPUTE PARAM-OUT = PARAM-IN * 2.\nCALL 'UNKNOWN-SERVICE' USING PARAM-OUT."))
        group, candidate, proofs = self.evidence()
        self.assertEqual(proofs[0]["status"], "unresolved_source_origin")
        self.assertIn("reaching_write_effect_not_bounded", proofs[0]["unknown_reasons"])
        closed, invalidated, witnesses = _flow_input_origins([candidate],
            [self.page(path) for path in ("entry.cbl", "middle.cbl", "leaf.cbl")], [group])
        self.assertEqual(closed, set())
        self.assertIn(("entry.cbl", "OUT-AMOUNT"), invalidated)
        self.assertEqual(witnesses[0]["status"], "source_origin_unresolved")
        self.assertTrue(any("COMPUTE PARAM-OUT" in node["statement"]
                            for node in group["semantic_analysis"]["slice_statements"]))

    def test_confirmed_label_without_its_actual_abi_evidence_does_not_close(self):
        original = FactStore.rows
        def incomplete(facts, sql, arguments=()):
            rows = original(facts, sql, arguments)
            if sql.startswith("SELECT b.*,cs.qualified_name"):
                for row in rows:
                    row["supporting_evidence_ids_json"] = json.dumps([
                        *json.loads(row["supporting_evidence_ids_json"]), "absent-abi-evidence"])
            return rows
        with mock.patch.object(FactStore, "rows", new=incomplete):
            group, candidate, proofs = self.evidence()
        self.assertFalse(any(proof["status"] == "resolved_source_origin" for proof in proofs))
        closed, _, _ = _flow_input_origins([candidate],
            [self.page(path) for path in ("entry.cbl", "middle.cbl", "leaf.cbl")], [group])
        self.assertFalse(closed)
        self.assertTrue(any(item["reason"] == "parameter_binding_evidence_incomplete"
                            for item in group["semantic_analysis"]["unknown_effects"]))

    def test_wrong_abi_or_no_normal_return_never_supplies_a_returned_origin(self):
        original = (self.source / "leaf.cbl").read_text()
        for label, replacement in (("different_layout", original.replace("PIC 9(6)V99", "PIC 9(8)V99")),
                                   ("stop_without_return", original.replace("GOBACK.", "STOP RUN."))):
            with self.subTest(label=label):
                (self.source / "leaf.cbl").write_text(replacement)
                group, candidate, proofs = self.evidence()
                self.assertFalse(any(proof["status"] == "resolved_source_origin" for proof in proofs))
                closed, _, _ = _flow_input_origins([candidate],
                    [self.page(path) for path in ("entry.cbl", "middle.cbl", "leaf.cbl")], [group])
                self.assertFalse(closed)
        (self.source / "leaf.cbl").write_text(original)

    def transport(self, request):
        payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
        self.requests.append(payload)
        pages = [page for bundle in payload["source_context"] for page in bundle.get("pages", [])]
        leaf = next(page for page in pages if "COMPUTE PARAM-OUT = PARAM-IN * 2" in page["source_text"])
        entry = next(page for page in pages if "MOVE OUT-AMOUNT TO FINAL-AMOUNT" in page["source_text"])
        answer = ("ENTRYPOINT 经 MIDDLE 调用 LEAF，参数按位置传递并正常返回。PARAM-OUT = PARAM-IN * 2。[" +
            leaf["evidence_id"] + "] 返回后 MOVE OUT-AMOUNT TO FINAL-AMOUNT。[" + entry["evidence_id"] + "]")
        if any("UNKNOWN-SERVICE" in page["source_text"] for page in pages):
            answer = ("叶程序存在直接公式 PARAM-OUT = PARAM-IN * 2。[" + leaf["evidence_id"] +
                "] 随后发出 UNKNOWN-SERVICE 调用请求；该实现未提供，返回后的 OUT-AMOUNT 仍可能被修改。[" +
                entry["evidence_id"] + "]")
        return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": answer},
            "finish_reason": "stop"}]}, ensure_ascii=False))

    def indexed_question(self):
        config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="local-test-secret")
        analyze_source(self.source, self.output, source_format="free", analysis_mode="business",
                       reading_strategy="retrieval", allow_network=False, framework_reference_path="")
        result = analyze_source(self.source, self.output, question=QUESTION, config=config, transport=self.transport,
            source_format="free", analysis_mode="business", reading_strategy="retrieval", allow_network=True,
            framework_reference_path="", agent_policy=self.policy)
        self.assertTrue(result["index_reused"])
        return json.loads((self.output / "agent-result.json").read_text())["agent_result"]

    def test_default_indexed_chat_closes_static_chain_without_extra_model_round_or_repository_rebuild(self):
        result = self.indexed_question()
        self.assertEqual(result["status"], "ANALYZED")
        self.assertEqual(result["stop_reason"], "sufficient_material")
        self.assertEqual(len(self.requests), 1)
        investigation = result["investigation_state"]["question_investigation"]
        self.assertEqual(investigation["open_gaps"], [])
        inputs = next(item for item in investigation["required_items"] if item["kind"] == "inputs")
        self.assertEqual(inputs["status"], "SATISFIED")
        self.assertEqual(inputs["reason"], "source_flow_origins_supplied")
        self.assertFalse(result["claims_semantically_verified"])
        self.assertFalse(result["metrics"]["repository_rebuilt"])

    def test_default_indexed_chat_preserves_known_formula_but_does_not_close_unknown_return(self):
        path = self.source / "leaf.cbl"
        path.write_text(path.read_text().replace("GOBACK.", "CALL 'UNKNOWN-SERVICE' USING PARAM-OUT.\nGOBACK."))
        result = self.indexed_question()
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("PARAM-OUT = PARAM-IN * 2", result["answer"])
        self.assertIn("可能被修改", result["answer"])
        self.assertFalse(result["claims_semantically_verified"])
        inputs = next(item for item in result["investigation_state"]["question_investigation"]["required_items"]
                      if item["kind"] == "inputs")
        self.assertNotEqual(inputs["status"], "SATISFIED")
        self.assertIn("OUT-AMOUNT", inputs["fields"])
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
