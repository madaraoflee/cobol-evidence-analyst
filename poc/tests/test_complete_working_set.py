"""Offline whole-source supply, request fitting and excerpt identity checks.

The transport captures the actual request. One mechanical response exercises
final metadata binding; the other captures stop without producing an answer.
These assertions measure source and metadata, never answer quality.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import APIClientError, CompanyAPIConfig, TransportResponse
import complete_working_set
from evidence_context import EvidenceContext, ReadTask
from repository_discovery import ensure_repository_search


def source_pages(payload):
    return [page for bundle in payload["source_context"] for page in bundle.get("pages", [])]


def program(name, body, data="01 RESULT-AMOUNT PIC 9(7)V99."):
    return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
            f"WORKING-STORAGE SECTION.\n{data}\nPROCEDURE DIVISION.\nMAIN.\n"
            f"{body}\nGOBACK.\n")


class CompleteWorkingSetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
            api_key="offline-neutral-credential", timeout_seconds=60.0, max_output_tokens=2048)
        self.files = {}
        self.network_attempts = []

        def forbidden(*args, **kwargs):
            self.network_attempts.append("connection")
            raise AssertionError("network forbidden in offline whole-source regression")

        for owner, name in ((socket.socket, "connect"), (socket.socket, "connect_ex"),
                            (socket, "create_connection")):
            patcher = mock.patch.object(owner, name, side_effect=forbidden)
            patcher.start()
            self.addCleanup(patcher.stop)

    def build(self, files):
        self.files = files
        for relative, text in files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free",
                             quiet=True, verify_content=True)
        ensure_repository_search(self.database, self.source)

    def capture(self, question, *, policy=None):
        policy = policy or AgentPolicy()
        requests, request_bytes = [], []

        def transport(request):
            envelope = json.loads(request.body)
            self.assertEqual(envelope["max_tokens"], self.config.max_output_tokens)
            self.assertLessEqual(len(request.body), policy.max_request_bytes)
            requests.append(json.loads(envelope["messages"][-1]["content"]))
            request_bytes.append(len(request.body))
            raise APIClientError("OFFLINE_CAPTURE_ONLY")

        output = run_business_chat(question, self.database, self.source, self.config,
            transport=transport, allow_network=False, framework_reference_path="", policy=policy)
        self.assertEqual(len(requests), 1)
        self.assertFalse(output["agent_result"]["model_answer_recorded"])
        self.assertEqual(self.network_attempts, [])
        self.assertLessEqual(output["agent_result"]["metrics"]["model_requests"], policy.max_model_requests)
        return requests[0], output, request_bytes[0]

    def assert_exact_complete_files(self, payload, paths):
        pages = [page for page in source_pages(payload)
                 if "complete_working_set" in page.get("selection_reasons", [])]
        self.assertEqual({page["relative_path"] for page in pages}, set(paths))
        self.assertEqual(len(pages), len(paths))
        metadata = payload["source_context"][0]["working_set"]
        self.assertEqual(metadata["status"], "supplied")
        self.assertTrue(metadata["physical_complete"])
        self.assertEqual(metadata["physical_completeness_scope"], "candidate_paths")
        manifest = {item["relative_path"]: item for item in metadata["source_manifest"]}
        for page in pages:
            relative = page["relative_path"]
            raw = (self.source / relative).read_bytes()
            lines = raw.decode("utf-8").splitlines()
            # This proves every physical line was sent, including comments,
            # non-anchor assignments and the final program lines.
            self.assertEqual(page["source_text"].split("\n"), lines)
            self.assertEqual(page["source_text"], "\n".join(lines))
            self.assertEqual((page["start_line"], page["end_line"]), (1, len(lines)))
            self.assertFalse(page["span_truncated"])
            self.assertEqual(page["source_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(manifest[relative]["sha256"], page["source_sha256"])
            self.assertEqual(manifest[relative]["line_count"], len(lines))
            self.assertEqual(manifest[relative]["evidence_id"], page["evidence_id"])
        actual_ids = {page["evidence_id"] for page in source_pages(payload)}
        for group in payload.get("evidence_groups", []):
            self.assertLessEqual(set(group.get("core_evidence_ids", [])), actual_ids)
            self.assertLessEqual(set(group.get("visible_evidence_ids", [])), actual_ids)
        for item in payload.get("business_analysis_brief", {}).get("supplied_material", []):
            self.assertLessEqual(set(item.get("supplied_reference_ids", [])), actual_ids)
        return metadata

    def test_actual_request_contains_every_outgoing_file_line_with_rounding_and_overwrites(self):
        self.build({
            "entry.cbl": program("ENTRY-RULE",
                "MOVE 8 TO BASIS.\nCOMPUTE PRELIMINARY = BASIS * FACTOR.\n"
                "CALL 'FINAL-RULE' USING PRELIMINARY RESULT-AMOUNT.\n"
                "MOVE 2 TO RESULT-AMOUNT.\n*> the later assignment remains business evidence",
                "COPY shared-values.\n01 RESULT-AMOUNT PIC 9(7)V99."),
            "shared-values.cpy": "01 BASIS PIC 9(5).\n01 FACTOR PIC 9V99 VALUE 1.25.\n"
                "01 PRELIMINARY PIC 9(7)V99.\n",
            "final.cbl": ("IDENTIFICATION DIVISION.\nPROGRAM-ID. FINAL-RULE.\nDATA DIVISION.\n"
                "LINKAGE SECTION.\n01 INPUT-AMOUNT PIC 9(7)V99.\n01 OUTPUT-AMOUNT PIC 9(7)V99.\n"
                "PROCEDURE DIVISION USING INPUT-AMOUNT OUTPUT-AMOUNT.\nMAIN.\n"
                "COMPUTE OUTPUT-AMOUNT ROUNDED = INPUT-AMOUNT / 3.\n"
                "IF OUTPUT-AMOUNT < 1 MOVE ZERO TO OUTPUT-AMOUNT END-IF.\nGOBACK.\n"),
            "caller.cbl": program("OUTER-RULE", "CALL 'ENTRY-RULE'."),
        })
        payload, _, _ = self.capture("请详细解释 entry.cbl 的业务流程、金额计算、条件与结果影响。")
        metadata = self.assert_exact_complete_files(payload,
            {"entry.cbl", "shared-values.cpy", "final.cbl"})
        self.assertTrue(metadata["closure_complete"])
        self.assertEqual(metadata["root_paths"], ["entry.cbl"])
        self.assertNotIn("caller.cbl", metadata["candidate_paths"])
        self.assertNotIn("caller.cbl", {item["relative_path"] for item in metadata["source_manifest"]})

    def test_whole_file_includes_a_thirteen_thousand_character_physical_line(self):
        long_line = "*> " + "x" * 13000
        self.build({"long-rule.cbl": program("LONG-RULE",
            long_line + "\nMOVE 7 TO RESULT-AMOUNT.")})
        payload, _, _ = self.capture("请详细解释 long-rule.cbl 的业务流程与结果。")
        metadata = self.assert_exact_complete_files(payload, {"long-rule.cbl"})
        self.assertLess(metadata["source_characters"], AgentPolicy().max_source_characters)
        self.assertIn(long_line, source_pages(payload)[0]["source_text"])

    def test_final_metadata_is_bound_to_the_actual_answer_request(self):
        self.build({"rule.cbl": program("BINDING-RULE", "MOVE 7 TO RESULT-AMOUNT.")})
        requests, bodies = [], []
        policy = AgentPolicy(max_model_requests=1, max_answer_revisions=0)

        def transport(request):
            envelope = json.loads(request.body)
            payload = json.loads(envelope["messages"][-1]["content"])
            requests.append(payload)
            self.assertLessEqual(len(request.body), policy.max_request_bytes)
            self.assertEqual(envelope["max_tokens"], self.config.max_output_tokens)
            page = next(page for page in source_pages(payload)
                        if "complete_working_set" in page.get("selection_reasons", []))
            body = f"已发送原文引用用于离线绑定检查。[{page['evidence_id']}]"
            bodies.append(body)
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": body}, "finish_reason": "stop"}]}, ensure_ascii=False))

        output = run_business_chat("请详细解释 rule.cbl 的业务流程与结果。",
            self.database, self.source, self.config, transport=transport, allow_network=False,
            framework_reference_path="", policy=policy)
        self.assertEqual(len(requests), 1)
        actual = self.assert_exact_complete_files(requests[0], {"rule.cbl"})
        result = output["agent_result"]
        self.assertTrue(result["model_answer_recorded"])
        self.assertEqual(result["answer"], bodies[0])
        self.assertEqual(output["investigation"]["working_set"], actual)
        quality = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text(encoding="utf-8"))
        self.assertEqual(quality["working_set"], actual)
        self.assertEqual(quality["final"]["final_answer_round_id"], "round-1")
        self.assertFalse(actual["semantic_execution_verified"])
        self.assertLessEqual(set(quality["final"]["final_visible_ids"]),
                             {page["evidence_id"] for page in source_pages(requests[0])})
        self.assertEqual(self.network_attempts, [])

    def test_over_source_budget_uses_bounded_fallback_without_a_complete_flag(self):
        self.build({"large-rule.cbl": program("LARGE-RULE",
            "*> " + "x" * 13000 + "\nMOVE 7 TO RESULT-AMOUNT.")})
        policy = AgentPolicy(max_source_characters=512, max_complete_source_characters=512, initial_source_characters=512,
                             search_source_characters=512, read_source_characters=512)
        payload, _, _ = self.capture("请详细解释 large-rule.cbl 的业务流程与结果。", policy=policy)
        metadata = payload["source_context"][0]["working_set"]
        self.assertEqual(metadata["status"], "fallback")
        self.assertFalse(metadata["physical_complete"])
        self.assertIn(metadata["reason"], {"source_characters", "source_byte_budget"})
        self.assertFalse(any("complete_working_set" in page.get("selection_reasons", [])
                             for page in source_pages(payload)))
        self.assertLessEqual(sum(len(page["source_text"]) for page in source_pages(payload)),
                             policy.max_source_characters)

    def test_search_resolution_supplies_complete_files_and_reuses_changed_identity(self):
        self.build({
            "first.cbl": program("FIRST-RULE", "MOVE 3 TO RESULT-AMOUNT.\n" +
                "*> neutral first context\n" * 500 + "MOVE 7 TO RESULT-AMOUNT."),
            "second.cbl": program("SECOND-RULE", "MOVE 5 TO RESULT-AMOUNT.\n" +
                "*> neutral second context\n" * 500 + "MOVE 9 TO RESULT-AMOUNT."),
        })
        actions = [["FIRST-RULE"], ["FIRST-RULE", "RESULT-AMOUNT"],
                   ["SECOND-RULE"], ["FIRST-RULE", "MAIN"]]
        requests = []
        policy = AgentPolicy(max_model_requests=5)

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            self.assertLessEqual(len(request.body), policy.max_request_bytes)
            if len(requests) > len(actions):
                raise APIClientError("OFFLINE_CAPTURE_ONLY")
            reply = json.dumps({"search": actions[len(requests) - 1]})
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": reply}, "finish_reason": "stop"}]}))

        with mock.patch.object(complete_working_set, "build_complete_working_set",
                wraps=complete_working_set.build_complete_working_set) as build:
            run_business_chat("这项处理有什么用途？", self.database, self.source, self.config,
                transport=transport, allow_network=False, framework_reference_path="", policy=policy)
        self.assertEqual(len(requests), 5)
        self.assertEqual(source_pages(requests[0]), [])
        for payload, path in zip(requests[1:], ("first.cbl", "first.cbl", "second.cbl", "first.cbl")):
            metadata = self.assert_exact_complete_files(payload, {path})
            self.assertEqual(metadata["root_paths"], [path])
            self.assertEqual(metadata["supplied_complete_paths"], [path])
            self.assertEqual(metadata["omitted_complete_paths"], [])
            self.assertLessEqual(sum(len(page["source_text"]) for page in source_pages(payload)),
                                 policy.max_source_characters)
        eligible_calls = [call for call in build.call_args_list
                          if call.args[2]["source_identity"]["status"] == "resolved"]
        self.assertEqual(len(eligible_calls), 2)
        self.assertEqual(self.network_attempts, [])

    def test_dependency_budget_keeps_root_and_smaller_complete_dependencies(self):
        self.build({
            "root.cbl": program("ROOT-RULE", "CALL 'LARGE-RULE'.\nCALL 'SMALL-RULE'.\n"
                "MOVE 7 TO RESULT-AMOUNT.\n" + "*> neutral root context\n" * 300),
            "a-large.cbl": program("LARGE-RULE", "*> neutral large context\n" * 1300 +
                "MOVE 8 TO RESULT-AMOUNT."),
            "z-small.cbl": program("SMALL-RULE", "MOVE 9 TO RESULT-AMOUNT."),
        })
        payload, _, _ = self.capture("请详细解释 root.cbl 的业务流程与结果。")
        pages = [page for page in source_pages(payload)
                 if "complete_working_set" in page.get("selection_reasons", [])]
        self.assertEqual({page["relative_path"] for page in pages}, {"root.cbl", "z-small.cbl"})
        for page in pages:
            self.assertEqual(page["source_text"], self.files[page["relative_path"]].rstrip("\n"))
        metadata = payload["source_context"][0]["working_set"]
        self.assertEqual(metadata["status"], "partial")
        self.assertEqual(metadata["transmission_status"], "partial")
        self.assertFalse(metadata["physical_complete"])
        self.assertFalse(metadata["closure_complete"])
        self.assertEqual(set(metadata["supplied_complete_paths"]), {"root.cbl", "z-small.cbl"})
        self.assertIn("a-large.cbl", metadata["omitted_candidate_paths"])
        self.assertEqual(metadata["source_characters"], sum(len(page["source_text"]) for page in pages))
        self.assertLessEqual(sum(len(page["source_text"]) for page in source_pages(payload)),
                             AgentPolicy().max_source_characters)
        self.assertTrue(all(len(text) < AgentPolicy().max_source_characters for text in self.files.values()))
        self.assertGreater(sum(map(len, self.files.values())), AgentPolicy().max_source_characters)

    def test_dependency_file_budget_keeps_root_and_reports_unavailable_closure(self):
        self.build({"root.cbl": program("ROOT-RULE", "CALL 'LEAF-RULE'.\nMOVE 7 TO RESULT-AMOUNT."),
                    "leaf.cbl": program("LEAF-RULE", "MOVE 9 TO RESULT-AMOUNT.")})
        payload, _, _ = self.capture("请详细解释 root.cbl 的业务流程与结果。",
                                    policy=AgentPolicy(max_semantic_files=1))
        pages = [page for page in source_pages(payload)
                 if "complete_working_set" in page.get("selection_reasons", [])]
        self.assertEqual([page["relative_path"] for page in pages], ["root.cbl"])
        self.assertEqual(pages[0]["source_text"], self.files["root.cbl"].rstrip("\n"))
        metadata = payload["source_context"][0]["working_set"]
        self.assertEqual(metadata["status"], "partial")
        self.assertFalse(metadata["physical_complete"])
        self.assertFalse(metadata["closure_complete"])
        self.assertIn("leaf.cbl", metadata["omitted_candidate_paths"])
        self.assertIn("dependency_file_budget", {gap["reason"] for gap in metadata["frontier"]})

    def test_dependency_byte_budget_keeps_verified_root_without_widening_budget(self):
        self.build({"root.cbl": program("ROOT-RULE", "CALL 'LEAF-RULE'.\nMOVE 7 TO RESULT-AMOUNT."),
                    "leaf.cbl": program("LEAF-RULE", "*> neutral context\n" * 200 +
                        "MOVE 9 TO RESULT-AMOUNT.")})
        policy = AgentPolicy(max_semantic_source_bytes=1024)
        payload, _, _ = self.capture("请详细解释 root.cbl 的业务流程与结果。", policy=policy)
        metadata = payload["source_context"][0]["working_set"]
        pages = [page for page in source_pages(payload)
                 if "complete_working_set" in page.get("selection_reasons", [])]
        self.assertEqual([page["relative_path"] for page in pages], ["root.cbl"])
        self.assertEqual(pages[0]["source_text"], self.files["root.cbl"].rstrip("\n"))
        self.assertEqual(metadata["status"], "partial")
        self.assertFalse(metadata["physical_complete"])
        self.assertEqual(metadata["omitted_candidate_paths"], ["leaf.cbl"])
        self.assertIn({"reason": "source_byte_budget", "relative_path": "leaf.cbl", "byte_limit": 1024},
                      metadata["frontier"])

    def test_depth_limit_keeps_bounded_complete_files_without_claiming_closure(self):
        files = {f"level-{index}.cbl": program(f"LEVEL-{index}",
            (f"CALL 'LEVEL-{index + 1}'.\n" if index < 5 else "") +
            f"MOVE {index} TO RESULT-AMOUNT.") for index in range(6)}
        self.build(files)
        payload, _, _ = self.capture("请详细解释 level-0.cbl 的业务流程与结果。")
        metadata = payload["source_context"][0]["working_set"]
        pages = [page for page in source_pages(payload)
                 if "complete_working_set" in page.get("selection_reasons", [])]
        self.assertEqual({page["relative_path"] for page in pages}, {f"level-{i}.cbl" for i in range(5)})
        for page in pages:
            self.assertEqual(page["source_text"], self.files[page["relative_path"]].rstrip("\n"))
        self.assertEqual(metadata["status"], "partial")
        self.assertFalse(metadata["physical_complete"])
        self.assertFalse(metadata["closure_complete"])
        self.assertEqual(metadata["omitted_candidate_paths"], ["level-5.cbl"])
        self.assertIn("dependency_depth_budget", {gap["reason"] for gap in metadata["frontier"]})

    def test_partial_working_set_metadata_still_reports_request_byte_trimming(self):
        self.build({
            "root.cbl": program("ROOT-RULE", "CALL 'LARGE-RULE'.\nMOVE 7 TO RESULT-AMOUNT.\n" +
                "*> 中性业务注释保留原始行\n" * 400),
            "large.cbl": program("LARGE-RULE", "*> neutral large context\n" * 2000 +
                "MOVE 9 TO RESULT-AMOUNT."),
        })
        prefix = "请详细解释 root.cbl 的业务流程与结果。"
        question = prefix + ("业务" * 3000)[:6000 - len(prefix)]
        payload, _, size = self.capture(question, policy=AgentPolicy(max_request_bytes=32768))
        metadata = payload["source_context"][0]["working_set"]
        self.assertLessEqual(size, 32768)
        self.assertEqual(metadata["status"], "partial")
        self.assertEqual(metadata["transmission_status"], "partial")
        self.assertFalse(metadata["physical_complete"])
        self.assertIn("large.cbl", metadata["omitted_candidate_paths"])
        self.assertIn("root.cbl", metadata["omitted_complete_paths"])
        self.assertNotIn("root.cbl", metadata["supplied_complete_paths"])
        self.assertFalse(any(page["relative_path"] == "root.cbl" and page["start_line"] == 1
            and page["end_line"] == len(self.files["root.cbl"].splitlines())
            for page in source_pages(payload)))

    def test_changing_whole_file_priority_preserves_prior_request_pages(self):
        context = EvidenceContext()
        first = {"evidence_id": "ev:first-neutral", "relative_path": "first.cbl",
            "start_line": 1, "end_line": 1, "source_text": "FIRST",
            "selection_reasons": ["complete_working_set"]}
        second = {**first, "evidence_id": "ev:second-neutral", "relative_path": "second.cbl",
                  "source_text": "SECOND"}
        context.accept({"pages": [first], "metadata": {"status": "supplied"}}, "complete_working_set")
        previous = context.bundle(context.selected_pages(512))[0]["pages"][0]
        context.accept({"pages": [second], "metadata": {"status": "partial"}}, "complete_working_set")
        self.assertIn(first["evidence_id"], context.pages)
        self.assertEqual(context.pages[first["evidence_id"]]["source_text"], "FIRST")
        self.assertNotIn("complete_working_set", context.pages[first["evidence_id"]]["selection_reasons"])
        self.assertIn("complete_working_set", previous["selection_reasons"])
        context.accept({"pages": [], "metadata": {"status": "not_applicable"}}, "complete_working_set")
        self.assertIsNone(context.working_set)
        self.assertEqual(set(context.pages), {first["evidence_id"], second["evidence_id"]})
        self.assertFalse(any("complete_working_set" in page["selection_reasons"]
                             for page in context.pages.values()))

    def test_encoded_request_trimming_recomputes_actual_complete_status(self):
        self.build({"rule.cbl": program("SHORT-RULE",
            "MOVE 7 TO RESULT-AMOUNT.\n" + "*> 中性业务注释保留原始行\n" * 400)})
        prefix = "请详细解释 rule.cbl 的业务流程与结果。"
        question = prefix + ("业务" * 3000)[:6000 - len(prefix)]
        policy = AgentPolicy(max_request_bytes=32768)
        payload, _, size = self.capture(question, policy=policy)
        metadata = payload["source_context"][0]["working_set"]
        self.assertLessEqual(size, policy.max_request_bytes)
        self.assertEqual(payload["question"], question)
        self.assertEqual(metadata["status"], "supplied")
        self.assertFalse(metadata["physical_complete"])
        self.assertEqual(metadata["transmission_status"], "partial")
        self.assertIn("rule.cbl", metadata["omitted_complete_paths"])
        self.assertFalse(any(page.get("relative_path") == "rule.cbl"
            and page.get("start_line") == 1
            and page.get("end_line") == len(self.files["rule.cbl"].splitlines())
            for page in source_pages(payload)))

    def test_ambiguous_or_missing_identity_never_supplies_a_substitute_whole_file(self):
        self.build({"one/rule.cbl": program("FIRST-RULE", "MOVE 1 TO RESULT-AMOUNT."),
                    "two/rule.cbl": program("SECOND-RULE", "MOVE 2 TO RESULT-AMOUNT.")})
        for requested, expected in (("rule.cbl", "ambiguous"), ("absent.cbl", "not_found")):
            with self.subTest(identity=expected):
                payload, _, _ = self.capture(f"请详细解释 {requested} 的业务流程与结果。")
                self.assertEqual(payload["business_map"]["source_identity"]["status"], expected)
                self.assertNotIn("working_set", payload["source_context"][0])
                self.assertFalse(any("complete_working_set" in page.get("selection_reasons", [])
                                     for page in source_pages(payload)))

    def test_external_dependency_keeps_physical_and_closure_completeness_distinct(self):
        self.build({"rule.cbl": program("EXTERNAL-RULE",
            "MOVE 8 TO RESULT-AMOUNT.\nCALL 'UNAVAILABLE-RULE' USING RESULT-AMOUNT.\n"
            "IF RESULT-AMOUNT < ZERO MOVE ZERO TO RESULT-AMOUNT END-IF.")})
        payload, _, _ = self.capture("请详细解释 rule.cbl 的业务流程与结果。")
        metadata = self.assert_exact_complete_files(payload, {"rule.cbl"})
        self.assertFalse(metadata["closure_complete"])
        self.assertIn("external_implementation_unavailable",
                      {item["reason"] for item in metadata["frontier"]})
        self.assertFalse(metadata["semantic_execution_verified"])

    def test_covering_excerpt_rebinding_requires_identical_hash_chain_and_text(self):
        complete = {"evidence_id": "ev:whole-neutral", "relative_path": "rule.cbl",
            "source_sha256": "a" * 64, "start_line": 1, "end_line": 4,
            "source_text": "FIRST\nSECOND\nTHIRD\nFOURTH", "span_truncated": False,
            "selection_reasons": ["complete_working_set"]}
        excerpt = {"evidence_id": "ev:part-neutral", "relative_path": "rule.cbl",
            "source_sha256": "a" * 64, "start_line": 2, "end_line": 3,
            "source_text": "SECOND\nTHIRD", "span_truncated": False}
        variants = [excerpt,
            {**excerpt, "evidence_id": "ev:hash-neutral", "source_sha256": "b" * 64},
            {**excerpt, "evidence_id": "ev:chain-neutral", "include_chain": ["different-copy-context"]},
            {**excerpt, "evidence_id": "ev:text-neutral", "source_text": "SECOND\nCHANGED"}]
        context = EvidenceContext()
        context.accept({"pages": variants}, "search")
        context.accept({"pages": [complete], "metadata": {"status": "supplied"}},
                       "complete_working_set")
        selected = context.selected_pages(512)
        identifiers = {page["evidence_id"] for page in selected}
        self.assertIn(complete["evidence_id"], identifiers)
        self.assertNotIn(excerpt["evidence_id"], identifiers)
        self.assertEqual(context.covering_id(excerpt["evidence_id"], selected), complete["evidence_id"])
        for variant in variants[1:]:
            self.assertIn(variant["evidence_id"], identifiers)
            self.assertEqual(context.covering_id(variant["evidence_id"], selected), variant["evidence_id"])

    def test_complete_set_only_advances_an_open_read_when_it_covers_the_cursor(self):
        complete = {"evidence_id": "ev:cursor-whole-neutral", "relative_path": "rule.cbl",
            "source_sha256": "a" * 64, "start_line": 1, "end_line": 4,
            "source_text": "FIRST\nSECOND\nTHIRD\nFOURTH", "span_truncated": False,
            "selection_reasons": ["complete_working_set"]}
        cases = (
            ("empty_fallback", {"pages": [], "metadata": {"status": "fallback"}}, "open", 3, 4),
            ("other_path", {"pages": [{**complete, "relative_path": "other.cbl"}],
                             "metadata": {"status": "supplied"}}, "open", 3, 4),
            ("head_before_cursor", {"pages": [{**complete, "end_line": 2,
                "source_text": "FIRST\nSECOND"}], "metadata": {"status": "supplied"}}, "open", 3, 4),
            ("same_path_complete", {"pages": [complete], "metadata": {"status": "supplied"}},
             "complete", None, 4),
            ("same_path_partial_set", {"pages": [complete], "metadata": {"status": "partial"}},
             "complete", None, 4),
            ("requested_beyond_eof", {"pages": [complete], "metadata": {"status": "supplied",
                "source_manifest": [{"relative_path": "rule.cbl", "sha256": "a" * 64,
                                     "line_count": 4, "evidence_id": complete["evidence_id"]}]}},
             "complete", None, 12),
        )
        for label, supplied, expected_state, expected_cursor, requested_end in cases:
            with self.subTest(supply=label):
                context = EvidenceContext()
                task = ReadTask("neutral-open-read", "rule.cbl", {"start_line": 1, "end_line": requested_end},
                                supplied_ranges=[(1, 2)], next_start_line=3)
                context.tasks[task.task_id] = task
                context.accept(supplied, "complete_working_set")
                self.assertEqual(task.state, expected_state)
                self.assertEqual(task.next_start_line, expected_cursor)
                if expected_state == "open":
                    self.assertEqual(task.supplied_ranges, [(1, 2)])


if __name__ == "__main__":
    unittest.main()
