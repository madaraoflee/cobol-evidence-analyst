"""Short relationship follow-ups retain source identity and CALL direction."""

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_chat import run_business_chat
from business_index import build_business_index
from business_map import build_business_map
from company_api import CompanyAPIConfig, TransportResponse
from conversation_focus import build_session_subject
from question_investigation import build_question_investigation
from question_planner import QuestionPlanner
from repository_discovery import ensure_repository_search, read_repository_context
from repository_identity import resolve_source_identity


class RelatedProgramFollowupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        self.write("entry.cbl", "ENTRYPOINT", "*> source context\n" * 350 + "CALL 'HELPER01'.")
        self.write("detail.cbl", "HELPER01", "DISPLAY 'DONE'.")
        self.write("driver.cbl", "DRIVER01", "CALL 'ENTRYPOINT'.")
        self.write("other.cbl", "OTHERPOINT", "CALL 'OTHER-WORKER'.")
        build_business_index(self.source, self.database, source_format="free", verify_content=True, quiet=True)
        ensure_repository_search(self.database, self.source)
        with closing(sqlite3.connect(self.database)) as db:
            subject = build_session_subject(db, "ENTRYPOINT 的流程", resolve_source_identity(db, "ENTRYPOINT 的流程"))
        self.history = [{"role": "user", "content": "ENTRYPOINT 的流程"},
            {"role": "assistant", "content": "已解释入口流程。",
             "investigation_state": {"session_subject": subject}}]

    def write(self, path, name, body):
        (self.source / path).write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. " + name +
            ".\nPROCEDURE DIVISION.\nMAIN.\n" + body + "\nGOBACK.\n", encoding="utf-8")

    def mapping(self, question):
        return build_business_map(self.database, self.source, question, history=self.history,
                                  prior_paths=["entry.cbl", "detail.cbl"])

    def test_outgoing_followup_retains_root_and_related_callee(self):
        mapped = self.mapping("用到的相关程序呢？")
        self.assertEqual(mapped["intent"], "explain")
        self.assertEqual(mapped["source_identity"]["direct_paths"], ["entry.cbl"])
        self.assertIn("detail.cbl", mapped["selected_paths"])
        self.assertNotIn("other.cbl", mapped["selected_paths"])
        plan = QuestionPlanner().plan("用到的相关程序呢？", mapped)
        self.assertIn("call_relationships", plan.intents)
        self.assertIn("calls", plan.required_obligations)
        self.assertEqual(plan.relation_direction, "outgoing")
        self.assertEqual(plan.relation_root, {"relative_path": "entry.cbl", "program_name": "ENTRYPOINT",
                         "source_sha256": mapped["source_versions"]["entry.cbl"]})

    def test_incoming_followup_retains_callers_without_switching_to_callees(self):
        mapped = self.mapping("哪些程序用到它？")
        self.assertEqual(mapped["intent"], "impact")
        self.assertEqual(set(mapped["selected_paths"]), {"entry.cbl", "driver.cbl"})
        plan = QuestionPlanner().plan("哪些程序用到它？", mapped)
        self.assertEqual(plan.relation_direction, "incoming")
        investigation = build_question_investigation("哪些程序用到它？", mapped, database_path=self.database)
        reads = investigation["planned_actions"]
        self.assertEqual([item["arguments"]["relative_path"] for item in reads], ["driver.cbl"])
        self.assertEqual(investigation["required_items"][0]["target_name"], "ENTRYPOINT")
        self.assertEqual(investigation["required_items"][0]["relation_direction"], "incoming")
        self.assertEqual(investigation["required_items"][0]["target_path"], "entry.cbl")

    def test_known_new_name_and_mixed_followup_bind_the_explicit_program(self):
        for question in ("HELPER01呢？", "HELPER01程序呢？", "用到的相关程序呢？HELPER01呢？"):
            with self.subTest(question=question):
                mapped = self.mapping(question)
                self.assertEqual(mapped["source_identity"]["direct_paths"], ["detail.cbl"])
                self.assertEqual(mapped["session_subject"]["root_paths"], ["detail.cbl"])
                self.assertIn("entry.cbl", mapped["selected_paths"])

    def test_explicit_unknown_program_cannot_resolve_as_old_root(self):
        for question in ("UNKNOWN01程序呢？", "UNKNOWN01这个程序呢？", "相关程序呢？UNKNOWN01程序呢？"):
            with self.subTest(question=question):
                mapped = self.mapping(question)
                identity = mapped["source_identity"]
                self.assertEqual(identity["status"], "not_found")
                self.assertEqual(identity["requested"], ["UNKNOWN01"])
                self.assertEqual(identity["direct_paths"], [])
                self.assertEqual(mapped["session_subject"]["status"], "unresolved")
                plan = QuestionPlanner().plan(question, mapped)
                self.assertIsNone(plan.relation_root)
                # Saved source paths can remain investigation context without
                # becoming the definition of the newly requested program.
                self.assertIn("entry.cbl", mapped["selected_paths"])

    def test_call_obligation_requires_actual_same_version_occurrence(self):
        question = "用到的相关程序呢？"
        mapped = self.mapping(question)
        missing = build_question_investigation(question, mapped, database_path=self.database)
        self.assertEqual(len(missing["planned_actions"]), 1)
        arguments = missing["planned_actions"][0]["arguments"]
        self.assertEqual(arguments["relative_path"], "entry.cbl")
        self.assertEqual(arguments["start_line"], 355)
        pages = read_repository_context(self.database, self.source, **arguments)["pages"]
        good = build_question_investigation(question, mapped, source_pages=pages, database_path=self.database)
        self.assertTrue(good["can_answer"])
        self.assertEqual(good["required_items"][0]["status"], "SATISFIED")
        self.assertEqual(good["planned_actions"], [])
        for changes in ({"source_sha256": "0" * 64}, {"relative_path": "detail.cbl"},
                        {"source_text": "CALL 'OTHER-WORKER'."}, {"span_truncated": True},
                        {"end_line": pages[0]["end_line"] + 1}):
            with self.subTest(changes=changes):
                bad = build_question_investigation(question, mapped,
                    source_pages=[{**pages[0], **changes}], database_path=self.database)
                self.assertFalse(bad["can_answer"])
                self.assertEqual(bad["required_items"][0]["status"], "OPEN")
                self.assertEqual(len(bad["planned_actions"]), 1)

    def test_relation_root_requires_current_unique_revalidated_subject(self):
        mapped = self.mapping("相关程序呢？")
        for mutate in (lambda m: m["session_subject"].update(revalidated=False),
                       lambda m: m["source_identity"].update(status="ambiguous"),
                       lambda m: m["source_versions"].update({"entry.cbl": "0" * 64}),
                       lambda m: m["session_subject"]["programs"].append(dict(m["session_subject"]["programs"][0]))):
            candidate = deepcopy(mapped)
            mutate(candidate)
            self.assertIsNone(QuestionPlanner().plan("相关程序呢？", candidate).relation_root)

    def test_call_reads_respect_existing_allowance_and_stale_root_is_not_supported(self):
        question = "用到的相关程序呢？"
        mapped = self.mapping(question)
        result = build_question_investigation(question, mapped, database_path=self.database, max_actions=0)
        self.assertEqual(result["planned_actions"], [])
        self.assertFalse(result["can_answer"])
        self.assertEqual(result["required_items"][0]["status"], "OPEN")
        mapped["source_versions"]["entry.cbl"] = "0" * 64
        mapped["session_subject"]["programs"][0]["source_sha256"] = "0" * 64
        result = build_question_investigation(question, mapped, database_path=self.database)
        self.assertFalse(result["can_answer"])
        self.assertEqual(result["open_gaps"][0]["reason"], "call_root_version_changed")

    def test_first_request_supplies_correct_direction_call_and_related_program(self):
        config = CompanyAPIConfig("https://gateway.example.invalid/v1", "neutral-model", api_key="test-credential")
        for question, direction, path, target in (("用到的相关程序呢？", "outgoing", "entry.cbl", "HELPER01"),
                                                 ("哪些程序用到它？", "incoming", "driver.cbl", "ENTRYPOINT")):
            with self.subTest(direction=direction):
                requests = []
                def transport(request):
                    payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
                    requests.append(payload)
                    pages = [page for context in payload["source_context"] for page in context.get("pages", [])]
                    captured = next(page for page in pages if page["relative_path"] == path
                                    and "CALL '" + target + "'" in page["source_text"])
                    self.assertEqual(payload["question_investigation"]["analysis_plan"]["relation_direction"], direction)
                    self.assertEqual(payload["question_investigation"]["required_items"][0]["status"], "SATISFIED")
                    if direction == "outgoing":
                        self.assertIn("detail.cbl", {page["relative_path"] for page in pages})
                    answer = "源码中存在 CALL '" + target + "'。[" + captured["evidence_id"] + "]"
                    return TransportResponse(200, json.dumps({"choices": [{"message": {"content": answer},
                        "finish_reason": "stop"}]}, ensure_ascii=False))
                with patch("socket.create_connection", side_effect=AssertionError("network forbidden")):
                    result = run_business_chat(question, self.database, self.source, config, history=self.history,
                        framework_reference_path="", transport=transport)["agent_result"]
                self.assertEqual(len(requests), 1)
                self.assertEqual(result["status"], "ANALYZED")


if __name__ == "__main__":
    unittest.main()
