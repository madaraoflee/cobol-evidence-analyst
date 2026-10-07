"""Offline regressions for automatic follow-ups with unchanged supplied evidence."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
import business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
import question_investigation
from repository_discovery import ensure_repository_search


class AutomaticMaterialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        (self.source / "rule.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. TOTAL-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 BASE-AMOUNT PIC 9(5) VALUE 10.\n"
            "01 TOTAL-AMOUNT PIC 9(6).\nPROCEDURE DIVISION.\nMAIN.\n"
            "COMPUTE TOTAL-AMOUNT = BASE-AMOUNT * 2.\nGOBACK.\n",
            encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free",
                             framework_reference_path="", quiet=True)
        ensure_repository_search(self.database, self.source)
        self.config = CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model",
                                       api_key="offline-only")
        self.policy = AgentPolicy(max_answer_revisions=0, max_evidence_groups=0,
                                  max_business_context_actions_per_turn=0)
        self.requests, self.answers = [], []
        for owner, attribute in ((socket, "create_connection"), (socket.socket, "connect"),
                                 (socket.socket, "connect_ex")):
            patcher = mock.patch.object(owner, attribute, side_effect=AssertionError("Network forbidden"))
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def investigation(*args, source_pages, completed_actions, **kwargs):
        # A bounded dependency read may be located after the program's complete
        # text is already selected; the planner has not reconciled that coverage.
        done = any("read" in action for action in completed_actions)
        return {"required_items": [], "planned_actions": [] if done else [{
            "tool": "read", "arguments": {"relative_path": "rule.cbl", "start_line": 5,
                                           "end_line": 9}, "reason": "dependency_source_not_supplied"}],
            "open_gaps": [] if done else [{"kind": "dependencies", "reason": "source_not_supplied"}],
            "state": "located" if done else "needs_evidence", "can_answer": done}

    def ask(self, *, answer_detail="brief", policy=None, finish_reason="stop"):
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            self.requests.append(payload)
            page = next(page for page in payload["source_context"][0]["pages"]
                        if "COMPUTE TOTAL-AMOUNT" in page["source_text"])
            answer = f"金额按基础金额乘以二得到。[{page['evidence_id']}]"
            self.answers.append(answer)
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": answer},
                "finish_reason": finish_reason}]}, ensure_ascii=False))

        return business_chat.run_business_chat("rule.cbl 的金额如何计算？", self.database,
            self.source, self.config, transport=transport, framework_reference_path="",
            answer_detail=answer_detail, policy=policy or self.policy)["agent_result"]

    def test_redundant_read_does_not_regenerate_a_complete_answer(self):
        with mock.patch.object(question_investigation, "build_question_investigation",
                               side_effect=self.investigation):
            result = self.ask()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["metrics"]["tool_calls"]["read"], 1)
        self.assertEqual(result["answer"], self.answers[0])
        self.assertIn("automatic_followup_without_new_material",
                      {row["reason"] for row in result["boundaries"]})
        # The retained answer keeps the evidence and gaps from its own request;
        # an unsent local read must not promote its evidence status.
        self.assertEqual(result["stop_reason"], "question_evidence_incomplete")
        first_ids = {page["evidence_id"] for page in self.requests[0]["source_context"][0]["pages"]}
        self.assertLessEqual({row["evidence_id"] for row in result["narrative"]["citations"]}, first_ids)
        trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text(encoding="utf-8"))
        self.assertEqual(trace["final"]["final_answer_round_ids"], ["round-1"])

    def test_chained_deterministic_reads_finish_before_first_answer(self):
        ranges = [(5, 6), (8, 9)]

        def planner(*args, completed_actions, **kwargs):
            completed = {action["read"]["start_line"] for action in completed_actions if "read" in action}
            pending = next(((start, end) for start, end in ranges if start not in completed), None)
            return {"required_items": [], "planned_actions": [] if pending is None else [{
                "tool": "read", "arguments": {"relative_path": "rule.cbl", "start_line": pending[0],
                                               "end_line": pending[1]}, "reason": "dependency_source_not_supplied"}],
                "open_gaps": [], "state": "located" if pending is None else "needs_evidence",
                "can_answer": pending is None}

        with mock.patch.object(question_investigation, "build_question_investigation", side_effect=planner):
            result = self.ask(answer_detail="detailed")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["metrics"]["tool_calls"]["read"], 2)
        self.assertEqual(sum("read" in action for action in self.requests[0]["completed_actions"]), 2)
        self.assertEqual(self.requests[0]["investigation_budget"]["reads_per_turn"], 1)
        self.assertFalse(self.requests[0]["question_investigation"]["planned_actions"])

    def test_newly_supplied_source_still_gets_an_answer_followup(self):
        (self.source / "detail.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. DETAIL-RULE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 LIMIT-AMOUNT PIC 9(5) VALUE 50.\nPROCEDURE DIVISION.\n"
            "MAIN.\nDISPLAY LIMIT-AMOUNT.\nGOBACK.\n", encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free",
                             framework_reference_path="", quiet=True)
        ensure_repository_search(self.database, self.source)

        def planner(*args, **kwargs):
            result = self.investigation(*args, **kwargs)
            for action in result["planned_actions"]:
                action["arguments"] = {"relative_path": "detail.cbl", "start_line": 1, "end_line": 9}
            return result

        with mock.patch.object(question_investigation, "build_question_investigation", side_effect=planner):
            result = self.ask()
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn("detail.cbl", {page["relative_path"] for page in self.requests[0]["source_context"][0]["pages"]})
        self.assertIn("detail.cbl", {page["relative_path"] for page in self.requests[1]["source_context"][0]["pages"]})
        self.assertEqual(result["metrics"]["tool_calls"]["read"], 1)
        self.assertNotIn("automatic_followup_without_new_material", {row["reason"] for row in result["boundaries"]})

    def test_truncated_draft_is_not_treated_as_a_complete_answer(self):
        with mock.patch.object(question_investigation, "build_question_investigation",
                               side_effect=self.investigation):
            result = self.ask(policy=replace(self.policy, max_model_requests=2), finish_reason="length")
        self.assertEqual(len(self.requests), 2)
        self.assertNotIn("automatic_followup_without_new_material", {row["reason"] for row in result["boundaries"]})
        self.assertEqual(result["stop_reason"], "MODEL_OUTPUT_TRUNCATED")

    def test_redundant_read_can_reveal_a_new_dependency_in_brief_mode(self):
        (self.source / "detail.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. DETAIL-RULE.\nPROCEDURE DIVISION.\n"
            "MAIN.\nDISPLAY 'DETAIL'.\nGOBACK.\n", encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free",
                             framework_reference_path="", quiet=True)
        ensure_repository_search(self.database, self.source)

        def planner(*args, completed_actions, **kwargs):
            done = {action["read"]["relative_path"] for action in completed_actions if "read" in action}
            path = "rule.cbl" if "rule.cbl" not in done else "detail.cbl" if "detail.cbl" not in done else None
            arguments = ({"relative_path": path, "start_line": 5, "end_line": 9} if path == "rule.cbl"
                         else {"relative_path": path, "start_line": 1, "end_line": 6})
            return {"required_items": [], "planned_actions": [] if path is None else [{
                "tool": "read", "arguments": arguments, "reason": "dependency_source_not_supplied"}],
                "open_gaps": [] if path is None else [{"kind": "dependencies", "reason": "source_not_supplied"}],
                "state": "located" if path is None else "needs_evidence", "can_answer": path is None}

        with mock.patch.object(question_investigation, "build_question_investigation", side_effect=planner):
            result = self.ask(answer_detail="brief")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["metrics"]["tool_calls"]["read"], 2)
        self.assertNotIn("detail.cbl", {page["relative_path"] for page in self.requests[0]["source_context"][0]["pages"]})
        self.assertIn("detail.cbl", {page["relative_path"] for page in self.requests[1]["source_context"][0]["pages"]})
        self.assertFalse(self.requests[1]["question_investigation"]["planned_actions"])

    def test_one_request_budget_never_runs_an_automatic_extra_request(self):
        with mock.patch.object(question_investigation, "build_question_investigation",
                               side_effect=self.investigation):
            result = self.ask(policy=replace(self.policy, max_model_requests=1))
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(result["metrics"]["tool_calls"]["read"], 0)
        self.assertEqual(result["stop_reason"], "request_budget")

    def test_zero_page_read_does_not_hide_a_later_dependency(self):
        (self.source / "detail.cbl").write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. DETAIL-RULE.\nPROCEDURE DIVISION.\n"
            "MAIN.\nDISPLAY 'DETAIL'.\nGOBACK.\n", encoding="utf-8")
        build_business_index(self.source, self.database, source_format="free",
                             framework_reference_path="", quiet=True)
        ensure_repository_search(self.database, self.source)
        chain = [("rule.cbl", 5, 9), ("rule.cbl", 1, 10), ("detail.cbl", 1, 6)]

        def planner(*args, completed_actions, **kwargs):
            done = {(action["read"]["relative_path"], action["read"]["start_line"], action["read"]["end_line"])
                    for action in completed_actions if "read" in action}
            pending = next((row for row in chain if row not in done), None)
            return {"required_items": [], "planned_actions": [] if pending is None else [{
                "tool": "read", "arguments": dict(zip(("relative_path", "start_line", "end_line"), pending)),
                "reason": "dependency_source_not_supplied"}],
                "open_gaps": [] if pending is None else [{"kind": "dependencies", "reason": "source_not_supplied"}],
                "state": "located" if pending is None else "needs_evidence", "can_answer": pending is None}

        with mock.patch.object(question_investigation, "build_question_investigation", side_effect=planner):
            result = self.ask(answer_detail="brief")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(result["metrics"]["tool_calls"]["read"], 3)
        self.assertIn("detail.cbl", {page["relative_path"] for page in self.requests[1]["source_context"][0]["pages"]})
        self.assertFalse(self.requests[1]["question_investigation"]["planned_actions"])
        self.assertEqual(self.requests[1]["investigation_budget"]["reads_per_turn"], 1)

    def test_material_comparison_ignores_navigation_but_preserves_versions_and_facts(self):
        payload = {"source_context": [{"pages": [{"evidence_id": "ev_first", "relative_path": "rule.cbl",
            "source_sha256": "version-1", "start_line": 1, "end_line": 1,
            "source_text": "MOVE 1 TO TOTAL.", "include_chain": []}]}],
            "framework_references": [{"reference_id": "fw:rule", "document_sha256": "manual-1",
                "start_line": 1, "end_line": 1, "text": "Returns the current record."}],
            "framework_facts": []}
        original = business_chat._supplied_answer_material(payload)
        bookkeeping = deepcopy(payload)
        bookkeeping["source_context"][0]["pages"][0].update(evidence_id="ev_alias", selection_reasons=["read"])
        bookkeeping["completed_actions"] = [{"read": {"relative_path": "rule.cbl"}}]
        self.assertEqual(business_chat._supplied_answer_material(bookkeeping), original)
        for section, field, value in (("source", "source_sha256", "version-2"),
                                      ("source", "source_text", "MOVE 2 TO TOTAL."),
                                      ("source", "include_chain", [{"path": "caller.cbl", "line": 2}]),
                                      ("framework", "document_sha256", "manual-2")):
            with self.subTest(section=section, field=field):
                changed = deepcopy(payload)
                row = (changed["source_context"][0]["pages"][0] if section == "source"
                       else changed["framework_references"][0])
                row[field] = value
                self.assertTrue(business_chat._supplied_answer_material(changed)[section] - original[section])
        facts = deepcopy(payload)
        facts["framework_facts"] = [{"fact_id": "new-binding", "operation": "read", "source_evidence_ids": ["ev_first"]}]
        self.assertTrue(business_chat._supplied_answer_material(facts)["facts"] - original["facts"])


if __name__ == "__main__":
    unittest.main()
