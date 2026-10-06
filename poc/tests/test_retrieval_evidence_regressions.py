"""Offline regressions for source identity and transmitted evidence.

Fake replies describe literal visibility only. They do not evaluate a model,
prove runtime value flow, or promote conservative source candidates to facts.
"""

from __future__ import annotations

import copy
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
from analyze_source import analyze_source
from business_chat import run_business_chat
from business_map import build_business_map
from company_api import CompanyAPIConfig, TransportResponse
from evidence_context import EvidenceContext
from repository_discovery import retrieve_repository_context
from source_reading import _identify_page


FORMULA = "COMPUTE FINAL-VALUE = BASE-VALUE * RATE-VALUE + 37"
CONDITION = "IF ACTIVE-FLAG = 'Y'"
INPUT = "MOVE 123 TO BASE-VALUE"
TARGET = "z-result.cbl"


def source_pages(payload):
    return [page for bundle in payload["source_context"] for page in bundle.get("pages", [])]


def response(text):
    return TransportResponse(200, json.dumps({"choices": [{"message": {
        "role": "assistant", "content": text}, "finish_reason": "stop"}]}, ensure_ascii=False))


def deep_source(name="COUNTPLAN", *, early_inputs=False):
    lines = ["IDENTIFICATION DIVISION.", f"PROGRAM-ID. {name}.",
             "DATA DIVISION.", "WORKING-STORAGE SECTION.",
             "01 FINAL-VALUE PIC 9(9)V99.", "01 BASE-VALUE PIC 9(9).",
             "01 RATE-VALUE PIC 9V99 VALUE 2.", "01 ACTIVE-FLAG PIC X VALUE 'Y'.",
             "01 WORK-COUNT PIC 9(9).", "PROCEDURE DIVISION.", "MAIN."]
    if early_inputs:
        lines += [f"MOVE {number} TO BASE-VALUE." for number in range(15)]
    else:
        lines += [f"COMPUTE WORK-COUNT = {number} + 1." for number in range(12)]
    lines += [f"*> ordinary neutral record description category value {number}" for number in range(1200)]
    lines += ["DEEP-RULE.", INPUT]
    if early_inputs:
        # A late writer is outside the fixed nearby window and beyond the
        # earliest twelve symbol links; it remains a source candidate only.
        lines += [f"*> neutral input separation {number}" for number in range(32)]
    lines += [CONDITION]
    lines += [f"*> intervening neutral explanation {number}" for number in range(380)]
    lines += [FORMULA, "END-IF.", "GOBACK."]
    return "\n".join(lines) + "\n"


def small_source(name, statement="CONTINUE."):
    return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\n"
            "PROCEDURE DIVISION.\nMAIN.\n" + statement + "\nGOBACK.\n")


class RetrievalEvidenceRegressionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="retrieval-evidence-regression-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "intake"
        self.database = self.output / "structural-index.sqlite"
        self.reference = self.root / "absent-reference.md"
        self.sources = {}
        self.requests = []
        self.config = CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model",
                                       api_key="offline-only")
        for target in ((socket, "create_connection"), (socket.socket, "connect")):
            patcher = mock.patch.object(*target, side_effect=AssertionError("Offline regression forbids network"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, path, text):
        destination = self.source / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
        self.sources[path] = text

    def build(self):
        result = analyze_source(self.source, self.output, index_mode="catalog", analysis_mode="business",
            reading_strategy="retrieval", source_format="free", allow_network=False,
            framework_reference_path=self.reference)
        self.assertTrue(result["source_manifest_verified"])

    def assert_physical_pages(self, pages):
        for page in pages:
            text = self.sources[page["relative_path"]]
            self.assertEqual(page["source_text"], "\n".join(
                text.splitlines()[page["start_line"] - 1:page["end_line"]]))
            self.assertEqual(page["source_sha256"], hashlib.sha256((self.source / page["relative_path"]).read_bytes()).hexdigest())
            self.assertEqual(page["evidence_id"], _identify_page(page))

    def literals_visible(self, payload, path=TARGET, literals=None):
        pages = [page for page in source_pages(payload) if page["relative_path"] == path]
        literals = literals or {"formula": FORMULA, "condition": CONDITION, "input": INPUT}
        return {key: any(literal in page["source_text"] for page in pages)
                for key, literal in literals.items()}

    def visible_reply(self, payload, literals=None):
        literals = literals or {"formula": FORMULA, "condition": CONDITION, "input": INPUT}
        observed = self.literals_visible(payload, literals=literals)
        citations = sorted({page["evidence_id"] for page in source_pages(payload)
                            if any(literal in page["source_text"] for literal in literals.values())})
        text = "Offline source observation: " + json.dumps(observed, sort_keys=True)
        text += "\n" + "\n".join(literal for kind, literal in literals.items() if observed[kind])
        return response(text + " " + " ".join(f"[{identifier}]" for identifier in citations))

    def ask(self, question, *, policy=None, first_action=None, history=None, literals=None):
        self.requests.clear()

        def transport(request):
            envelope = json.loads(request.body)
            payload = json.loads(envelope["messages"][-1]["content"])
            self.assert_physical_pages(source_pages(payload))
            self.requests.append({"body": request.body, "payload": payload})
            if first_action is not None and len(self.requests) == 1:
                return response(json.dumps(first_action))
            return self.visible_reply(payload, literals=literals)

        return run_business_chat(question, self.database, self.source, self.config,
            transport=transport, allow_network=False, capture_context=True,
            framework_reference_path=self.reference, policy=policy, history=history)

    def assert_all_literals(self, payload, literals=None):
        self.assertEqual(self.literals_visible(payload, literals=literals),
                         {"formula": True, "condition": True, "input": True})

    def assert_visible_citations_and_budgets(self, result, policy):
        agent = result["agent_result"]
        self.assertLessEqual(len(self.requests), policy.max_model_requests)
        for request in self.requests:
            self.assertLessEqual(len(request["body"]), policy.max_request_bytes)
            supplied = source_pages(request["payload"])
            working_set = request["payload"]["source_context"][0].get("working_set", {})
            extended_roots = [page for page in supplied
                if working_set.get("complete_root_budget_applied")
                and page["relative_path"] in working_set.get("root_paths", [])
                and "complete_working_set" in page.get("selection_reasons", [])]
            self.assertLessEqual(len(extended_roots), 1)
            if extended_roots:
                self.assertEqual(request["payload"]["business_map"]["source_identity"]["status"], "resolved")
                self.assertLessEqual(len(extended_roots[0]["source_text"]), policy.max_complete_source_characters)
            self.assertLessEqual(sum(len(page["source_text"]) for page in supplied if page not in extended_roots),
                                 policy.max_source_characters)
            ids = {page["evidence_id"] for page in source_pages(request["payload"])}
            for group in request["payload"].get("evidence_groups", []):
                self.assertLessEqual(set(group.get("visible_evidence_ids", [])), ids)
                if group.get("complete_text_supplied"):
                    self.assertLessEqual(set(group["required_evidence_ids"]), ids)
                    self.assertFalse(group.get("open_frontier"))
        trace = json.loads(Path(agent["metrics"]["quality_trace_path"]).read_text())
        self.assertEqual(len(trace["rounds"]), len(self.requests))
        for request, round_trace in zip(self.requests, trace["rounds"]):
            self.assertEqual(round_trace["request_body_sha256"], hashlib.sha256(request["body"]).hexdigest())
            self.assertEqual(round_trace["request_bytes"], len(request["body"]))
        final_round = trace["final"].get("final_answer_round_id")
        if final_round:
            final_index = next(index for index, row in enumerate(trace["rounds"])
                               if row["round_id"] == final_round)
            final_ids = {page["evidence_id"] for page in source_pages(self.requests[final_index]["payload"])}
            for citation in agent["narrative"]["citations"]:
                self.assertIn(citation["evidence_id"], final_ids)
        self.assertFalse(agent["claims_semantically_verified"])
        return trace

    def test_alphabetic_and_numeric_program_identifiers_keep_deep_result_evidence(self):
        policy = AgentPolicy(max_model_requests=2)
        for name in ("COUNTPLAN", "COUNT123"):
            with self.subTest(name=name):
                self.write(TARGET, deep_source(name))
                self.build()
                result = self.ask(f"Explain {name} calculation and conditions", policy=policy)
                self.assertEqual(len(self.requests), 1)
                self.assert_all_literals(self.requests[0]["payload"])
                self.assertEqual(self.requests[0]["payload"]["business_map"]["direct_paths"], [TARGET])
                self.assert_visible_citations_and_budgets(result, policy)

    def test_qualified_path_excludes_same_basename_and_comment_reference(self):
        target = "one/z-result.cbl"
        self.write(target, deep_source())
        self.write("two/z-result.cbl", small_source("OTHERPLAN", "COMPUTE LOCAL-VALUE = 2 + 1."))
        self.write("comment.cbl", small_source("NOTEONLY", "*> reference one/z-result.cbl"))
        self.build()
        question = f"Explain {target} calculation"
        mapping = build_business_map(self.database, self.source, question)
        context = retrieve_repository_context(self.database, self.source, question)
        self.assertEqual(mapping["direct_paths"], [target])
        self.assertEqual(mapping["source_identity"]["status"], "resolved")
        self.assertEqual({page["relative_path"] for page in context["pages"]}, {target})
        prior = retrieve_repository_context(self.database, self.source, "OTHERPLAN")["pages"][0]
        history = [{"role": "assistant", "content": "Prior source observation", "evidence_refs": [prior],
                    "investigation_state": {"focus_candidates": [{"relative_path": "two/z-result.cbl", "line": 5}]}}]
        result = self.ask(question, history=history)
        self.assertEqual({page["relative_path"] for request in self.requests
                          for page in source_pages(request["payload"])}, {target})
        self.assert_visible_citations_and_budgets(result, AgentPolicy())

    def test_ambiguous_basename_keeps_candidates_unselected_and_reports_gap(self):
        for path, name in (("one/z-result.cbl", "FIRSTPLAN"), ("two/z-result.cbl", "SECONDPLAN")):
            self.write(path, small_source(name))
        self.build()
        question = "Explain z-result.cbl calculation"
        mapping = build_business_map(self.database, self.source, question)
        context = retrieve_repository_context(self.database, self.source, question)
        self.assertEqual(mapping["source_identity"]["status"], "ambiguous")
        self.assertEqual(mapping["direct_paths"], [])
        self.assertEqual(context["pages"], [])
        prior = retrieve_repository_context(self.database, self.source, "FIRSTPLAN")["pages"][0]
        history = [{"role": "assistant", "content": "Prior source observation", "evidence_refs": [prior],
                    "investigation_state": {"focus_candidates": [{"relative_path": "one/z-result.cbl", "line": 5}]}}]
        result = self.ask(question, history=history)
        self.assertTrue(all(not source_pages(request["payload"]) for request in self.requests))
        self.assertIn("source_identity_ambiguous", {row["reason"] for row in result["agent_result"]["boundaries"]})
        self.assertNotEqual(result["agent_result"]["metrics"]["quality_stop_reason"], "sufficient_material")

    def test_duplicate_program_declarations_do_not_promote_comment_or_pick_one(self):
        self.write("one.cbl", small_source("COUNTPLAN"))
        self.write("two.cbl", small_source("COUNTPLAN"))
        self.write("comment.cbl", small_source("NOTEONLY", "*> COUNTPLAN"))
        self.build()
        mapping = build_business_map(self.database, self.source, "Explain COUNTPLAN calculation")
        self.assertEqual(mapping["source_identity"]["status"], "ambiguous")
        self.assertEqual(mapping["direct_paths"], [])
        candidates = {path for row in mapping["source_identity"]["candidates"] for path in row["relative_paths"]}
        self.assertEqual(candidates, {"one.cbl", "two.cbl"})

    def test_unresolved_identity_cannot_be_bypassed_by_read_or_inspect_actions(self):
        self.write("one/z-result.cbl", small_source("FIRSTPLAN", "COMPUTE LOCAL-VALUE = 2 + 1."))
        self.write("two/z-result.cbl", small_source("SECONDPLAN"))
        self.build()
        actions = ({"read": [{"relative_path": "one/z-result.cbl", "start_line": 1, "end_line": 6}]},
                   {"inspect_business_context": [{"relative_path": "one/z-result.cbl", "line": 5}]})
        for question in ("Explain z-result.cbl calculation", "Explain missing/z-result.cbl calculation"):
            for action in actions:
                with self.subTest(question=question, action=action):
                    result = self.ask(question, first_action=action)
                    self.assertTrue(all(not source_pages(request["payload"]) for request in self.requests))
                    self.assertEqual(result["agent_result"]["status"], "PARTIAL")
                    self.assertIn("source_identity_unresolved",
                                  {row["reason"] for row in result["agent_result"]["boundaries"]})

    def test_missing_qualified_path_does_not_restore_prior_source_or_basename(self):
        self.write(TARGET, deep_source())
        self.build()
        prior = retrieve_repository_context(self.database, self.source, "FINAL-VALUE")["pages"][0]
        history = [{"role": "assistant", "content": "Prior source observation",
                    "cited_evidence_ids": [prior["evidence_id"]], "evidence_refs": [prior],
                    "investigation_state": {"focus_candidates": [{"relative_path": TARGET,
                        "line": self.sources[TARGET].splitlines().index(FORMULA) + 1}]}}]
        question = "Explain missing/z-result.cbl calculation"
        mapping = build_business_map(self.database, self.source, question, prior_paths=[TARGET])
        context = retrieve_repository_context(self.database, self.source, question, prior_paths=[TARGET])
        self.assertEqual(mapping["source_identity"]["status"], "not_found")
        self.assertEqual(mapping["direct_paths"], [])
        self.assertEqual(context["pages"], [])
        result = self.ask(question, history=history)
        self.assertTrue(all(not source_pages(request["payload"]) for request in self.requests))
        self.assertIn("source_identity_not_found", {row["reason"] for row in result["agent_result"]["boundaries"]})
        self.assertNotEqual(result["agent_result"]["metrics"]["quality_stop_reason"], "sufficient_material")

    def test_followup_search_supplies_result_condition_and_input_source(self):
        self.write(TARGET, deep_source())
        self.build()
        policy = AgentPolicy(max_model_requests=3)
        result = self.ask("Explain COUNTPLAN calculation", policy=policy,
                          first_action={"search": ["FINAL-VALUE"]})
        self.assertEqual(len(self.requests), 2)
        self.assert_all_literals(self.requests[1]["payload"])
        self.assertEqual(result["agent_result"]["metrics"]["tool_calls"]["search"], 1)
        self.assert_visible_citations_and_budgets(result, policy)

    def test_late_input_candidate_is_not_lost_after_fifteen_early_writers(self):
        # A single-token result name prevents lexical VALUE matches from
        # incidentally retrieving the input page and hiding the link limit.
        self.write(TARGET, deep_source(early_inputs=True).replace("FINAL-VALUE", "FINALRESULT"))
        self.build()
        policy = AgentPolicy()
        literals = {"formula": FORMULA.replace("FINAL-VALUE", "FINALRESULT"),
                    "condition": CONDITION, "input": INPUT}
        result = self.ask("Explain FINALRESULT calculation", policy=policy, literals=literals)
        self.assert_all_literals(self.requests[-1]["payload"], literals=literals)
        self.assert_visible_citations_and_budgets(result, policy)

    def test_source_budget_omissions_are_visible_in_payload_and_quality_trace(self):
        self.write(TARGET, deep_source())
        self.build()
        policy = AgentPolicy(max_source_characters=512, initial_source_characters=512,
                             max_request_bytes=32768, max_model_requests=2)
        result = self.ask("Explain FINAL-VALUE calculation", policy=policy)
        trace = self.assert_visible_citations_and_budgets(result, policy)
        self.assertTrue(any(row.get("reason") == "source_characters"
                            for round_trace in trace["rounds"] for row in round_trace["trim_events"]))
        bundles = [bundle for request in self.requests for bundle in request["payload"]["source_context"]]
        self.assertTrue(any(bundle.get("source_selection_trim_events") for bundle in bundles))
        self.assertTrue(any(bundle.get("open_frontier") for bundle in bundles))

    def test_request_byte_cap_preserves_critical_source_and_real_visible_citations(self):
        self.write(TARGET, deep_source())
        self.build()
        policy = AgentPolicy(max_request_bytes=32768, max_model_requests=2)
        result = self.ask("Explain FINAL-VALUE calculation", policy=policy)
        self.assert_all_literals(self.requests[-1]["payload"])
        self.assert_visible_citations_and_budgets(result, policy)
        self.assertTrue(result["agent_result"]["narrative"]["citations"])

    def test_explicit_identity_gap_with_manual_context_does_not_enter_discovery(self):
        self.write(TARGET, small_source("COUNTPLAN"))
        self.reference.write_text(
            "# Deferred processing\n\nDEFERRED-SETTLEMENT retains requests until the next working day.\n",
            encoding="utf-8")
        self.build()
        result = self.ask("Explain missing/z-result.cbl and DEFERRED-SETTLEMENT")
        self.assertEqual(len(self.requests), 1)
        payload = self.requests[0]["payload"]
        self.assertTrue(payload["framework_references"])
        self.assertEqual(payload["business_map"]["source_identity"]["status"], "not_found")
        self.assertEqual(payload["retrieval_status"]["state"], "unresolved")
        self.assertFalse(payload["retrieval_status"]["query_expansion_attempted"])
        self.assertEqual(result["agent_result"]["status"], "PARTIAL")
        self.assertEqual(result["agent_result"]["stop_reason"], "source_identity_not_found")
        self.assert_visible_citations_and_budgets(result, AgentPolicy())

    def test_transport_budget_omitting_known_source_does_not_restart_discovery(self):
        self.write(TARGET, deep_source())
        self.build()
        import business_chat
        original_fit = business_chat._fit_request

        def drop_source_before_fit(config, payload, history, policy=None, *, trim_events=None,
                                   investigation_builder=None, source_fallback_builder=None):
            # Exercise the flow contract after transport fitting omits every
            # already located excerpt; selector behavior is covered separately.
            self.assertEqual(payload["business_map"]["source_identity"]["status"], "none")
            pages = source_pages(payload)
            self.assertTrue(pages)
            for page in pages:
                trim_events.append({"item_id": page["evidence_id"], "role": "source",
                    "reason": "request_bytes", "old_range": {
                        "start_line": page["start_line"], "end_line": page["end_line"]},
                    "new_range": None, "before_characters": len(page["source_text"]),
                    "after_characters": 0})
            payload["source_context"][0]["pages"] = []
            return original_fit(config, payload, history, policy, trim_events=trim_events,
                                investigation_builder=investigation_builder,
                                source_fallback_builder=source_fallback_builder)

        with mock.patch.object(business_chat, "_fit_request", side_effect=drop_source_before_fit):
            result = self.ask("Explain FINAL-VALUE calculation")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(source_pages(self.requests[0]["payload"]), [])
        self.assertEqual(result["agent_result"]["status"], "PARTIAL")
        self.assertEqual(result["agent_result"]["stop_reason"], "evidence_incomplete")
        self.assertFalse(result["agent_result"]["investigation"]["query_expansion_attempted"])
        self.assert_visible_citations_and_budgets(result, AgentPolicy())

    def test_read_revision_accounts_for_all_sent_ids_and_refreshes_prompt_budget(self):
        text = deep_source()
        self.write(TARGET, text)
        self.build()
        lines = text.splitlines()
        policy = AgentPolicy(max_model_requests=5, max_complete_source_characters=36000)
        result = self.ask("Explain COUNTPLAN calculation", policy=policy, first_action={"read": [{
            "relative_path": TARGET, "start_line": lines.index(INPUT) + 1,
            "end_line": lines.index(FORMULA) + 2}]})
        trace = self.assert_visible_citations_and_budgets(result, policy)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(trace["rounds"][-1]["stage"], "revise")
        sent_ids = {page["evidence_id"] for request in self.requests for page in source_pages(request["payload"])}
        self.assertEqual(result["agent_result"]["metrics"]["sent_any_round_ids"], len(sent_ids))
        self.assertEqual(set(trace["final"]["sent_any_round_ids"]), sent_ids)
        revision = self.requests[-1]["payload"]
        self.assertEqual(revision["investigation_budget"]["remaining_model_requests"], 3)
        self.assertEqual(revision["investigation_budget"]["searches_per_turn"], 0)
        self.assertEqual(revision["investigation_budget"]["reads_per_turn"], 0)
        self.assert_all_literals(revision)

    def test_duplicate_evidence_ids_merge_roles_without_changing_physical_provenance(self):
        text = FORMULA
        page = {"relative_path": TARGET, "start_line": 1, "end_line": 1,
                "source_text": text, "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "selection_reasons": ["question_match"]}
        page["evidence_id"] = _identify_page(page)
        original = copy.deepcopy(page)
        pool = EvidenceContext()
        pool.accept({"pages": [page]}, "search")
        update = {**copy.deepcopy(original), "semantic_roles": ["result", "input"],
                  "selection_reasons": ["business_context"], "group_id": "selected-group"}
        pool.accept({"pages": [update]}, "business_context")
        merged = pool.pages[page["evidence_id"]]
        self.assertEqual(set(merged["semantic_roles"]), {"result", "input"})
        self.assertEqual(set(merged["selection_reasons"]), {"question_match", "business_context"})
        for key in ("relative_path", "start_line", "end_line", "source_text", "source_sha256", "evidence_id"):
            self.assertEqual(merged[key], original[key])

    def test_priority_result_page_survives_the_2449_vs_2414_source_budget_boundary(self):
        noise = "*> " + "n" * 33583
        result_text = FORMULA + "\n*> " + "n" * (2449 - len(FORMULA) - 4)
        whole_text = noise + "\n" + result_text
        source_hash = hashlib.sha256(whole_text.encode()).hexdigest()
        pages = [{"relative_path": TARGET, "start_line": 1, "end_line": 1,
                  "source_sha256": source_hash, "source_text": noise, "selection_reasons": ["question_match"]},
                 {"relative_path": TARGET, "start_line": 2, "end_line": 3,
                  "source_sha256": source_hash, "source_text": result_text, "selection_reasons": ["question_match"]}]
        for page in pages:
            page["evidence_id"] = _identify_page(page)
        self.assertEqual(len(noise), 33586)
        self.assertEqual(len(result_text), 2449)
        pool = EvidenceContext()
        pool.accept({"pages": pages}, "search")
        selected = pool.selected_pages(36000, priority_targets=[{"relative_path": TARGET, "line": 2}])
        self.assertIn(pages[1]["evidence_id"], {page["evidence_id"] for page in selected})
        self.assertLessEqual(sum(len(page["source_text"]) for page in selected), 36000)
        self.assertEqual(pool.pages[pages[1]["evidence_id"]]["source_text"], result_text)
        self.assertTrue(pool.selection_trim_events)
        self.assertTrue(pool.selection_frontier)


if __name__ == "__main__":
    unittest.main()
