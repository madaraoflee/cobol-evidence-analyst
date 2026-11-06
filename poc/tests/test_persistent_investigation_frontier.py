"""Batch limits preserve resumable, source-bound work outside the prompt."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_index import build_business_index
from business_map import build_business_map
from complete_working_set import build_complete_working_set
from question_investigation import _business_source_reads, build_question_investigation
from repository_discovery import ensure_repository_search
from source_session import QuestionSourceSession
from business_chat import _prompt_payload, run_business_chat
from company_api import CompanyAPIConfig, TransportResponse
from run_store import RunStore


def program(name, body):
    return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 TOTAL-AMOUNT PIC 9(6) VALUE 0.\n"
            f"PROCEDURE DIVISION.\nMAIN.\n{body}\nGOBACK.\n")


class PersistentInvestigationFrontierTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "source"
        self.root.mkdir()
        self.database = Path(temporary.name) / "index.sqlite"

    def build(self, files):
        for name, text in files.items():
            (self.root / name).write_text(text, encoding="utf-8")
        build_business_index(self.root, self.database, source_format="free", quiet=True,
                             verify_content=True, framework_reference_path="")
        ensure_repository_search(self.database, self.root)

    def chain(self, count=8):
        self.build({"entry.cbl": program("ENTRY-CHAIN", "\n".join(
            f'CALL "STEP{number:02d}".' for number in range(count))),
            **{f"step-{number:02d}.cbl": program(f"STEP{number:02d}", f"ADD {number + 1} TO TOTAL-AMOUNT.")
               for number in range(count)}})
        self.question = "请详细解释 ENTRY-CHAIN 的整体业务流程和各个步骤的处理顺序。"
        return build_business_map(self.database, self.root, self.question)

    def pages(self):
        return [{"relative_path": path.name, "source_text": path.read_text(), "start_line": 1,
                 "end_line": len(path.read_text().splitlines()), "evidence_id": "ev_" + path.stem,
                 "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "span_truncated": False}
                for path in self.root.glob("*.cbl")]

    def test_static_fanout_advances_after_the_batch_and_sql_limits_without_rereading(self):
        mapping = self.chain()
        saved, supplied = None, []
        with QuestionSourceSession(self.database, self.root) as session:
            for _ in range(5):
                result = build_complete_working_set(self.database, session, mapping,
                    AgentPolicy(max_semantic_files=4), business_overview=True, frontier_state=saved)
                metadata = result["metadata"]
                self.assertLessEqual(len(metadata["candidate_paths"]), 4)
                self.assertFalse(set(supplied) & set(metadata["candidate_paths"]))
                supplied.extend(metadata["candidate_paths"])
                saved = json.loads(json.dumps(result["source_frontier_state"]))
                if saved["pending_paths"] or saved["pending_expansions"]:
                    self.assertFalse(metadata["closure_complete"])
                    self.assertFalse(metadata["traversal_complete"])
                else:
                    break
            self.assertEqual(set(supplied), set(mapping["selected_paths"]))
            self.assertEqual(len(supplied), 9)
            self.assertTrue(metadata["traversal_complete"])
            self.assertTrue(metadata["closure_complete"])
            self.assertFalse(metadata["semantic_execution_verified"])
            exhausted = build_complete_working_set(self.database, session, mapping,
                AgentPolicy(max_semantic_files=4), business_overview=True, frontier_state=saved)
            self.assertEqual(exhausted["metadata"]["candidate_paths"], [])
            self.assertEqual(exhausted["metadata"]["global_candidate_count"], 9)

    def test_unresolved_relation_cursor_retains_every_boundary_without_claiming_closure(self):
        self.build({"entry.cbl": program("ENTRY-CHAIN", "\n".join(
            f'CALL "UNAVAILABLE{number:02d}".' for number in range(70)))})
        mapping = build_business_map(self.database, self.root, "请说明 ENTRY-CHAIN 的业务流程。")
        saved = None
        with QuestionSourceSession(self.database, self.root) as session:
            for _ in range(4):
                result = build_complete_working_set(self.database, session, mapping, AgentPolicy(),
                    business_overview=True, frontier_state=saved)
                saved = json.loads(json.dumps(result["source_frontier_state"]))
                if not saved["pending_expansions"]:
                    break
        boundaries = [row for row in saved["unresolved_boundaries"]
                      if row["reason"] == "external_implementation_unavailable"]
        self.assertEqual(len(boundaries), 70)
        self.assertEqual(len({row["target_name"] for row in boundaries}), 70)
        self.assertTrue(result["metadata"]["traversal_complete"])
        self.assertFalse(result["metadata"]["closure_complete"])
        self.assertEqual(result["metadata"]["global_unresolved_count"], 70)

    def test_changing_the_question_root_resets_a_saved_source_cursor(self):
        mapping = self.chain()
        changed = build_business_map(self.database, self.root, "请解释 STEP07 的整体业务流程。")
        with QuestionSourceSession(self.database, self.root) as session:
            first = build_complete_working_set(self.database, session, mapping,
                AgentPolicy(max_semantic_files=4), business_overview=True)
            result = build_complete_working_set(self.database, session, changed,
                AgentPolicy(max_semantic_files=4), business_overview=True,
                frontier_state=first["source_frontier_state"])
        self.assertEqual(result["source_frontier_state"]["reset_reason"], "source_frontier_scope_changed")
        self.assertEqual(result["metadata"]["candidate_paths"], ["step-07.cbl"])

    def test_rule_path_batches_remain_open_until_the_last_related_path_is_processed(self):
        mapping = self.chain()
        first = build_question_investigation(self.question, mapping, database_path=self.database,
            source_pages=self.pages(), advance_frontier=True)
        self.assertTrue(first["investigation_frontier_state"]["batch_complete"])
        self.assertEqual(first["pending_work"]["rule_path_count"], 1)
        self.assertFalse(first["investigation_complete"])
        self.assertFalse(first["can_answer"])
        self.assertTrue(first["can_answer_locally"])
        same = build_question_investigation(self.question, mapping, database_path=self.database,
            source_pages=self.pages(), investigation_frontier_state=first["investigation_frontier_state"])
        self.assertEqual(same["obligation_batch_key"], first["obligation_batch_key"])
        second = build_question_investigation(self.question, mapping, database_path=self.database,
            source_pages=self.pages(), investigation_frontier_state=json.loads(json.dumps(
                first["investigation_frontier_state"])), advance_frontier=True)
        self.assertNotEqual(second["obligation_batch_key"], first["obligation_batch_key"])
        self.assertEqual(second["candidate_paths"], ["step-07.cbl"])
        self.assertTrue(second["investigation_frontier_state"]["batch_complete"])
        self.assertTrue(second["investigation_complete"])
        self.assertTrue(second["can_answer"])
        self.assertEqual(second["open_gaps"], [])

    def test_fallback_reads_after_the_first_sixty_four_handoffs_until_source_is_covered(self):
        body = "\n".join("\n".join(["CONTINUE."] * 20 + [f'CALL "STEP{number:02d}".'])
                         for number in range(70))
        self.build({"entry.cbl": program("ENTRY-CHAIN", body)})
        mapping = build_business_map(self.database, self.root, "请说明 ENTRY-CHAIN 的整体业务流程。")
        working = {"snapshot_id": mapping["snapshot_id"], "candidate_paths": ["entry.cbl"]}
        source = self.pages()[0]
        lines = source["source_text"].splitlines()
        _, missing = _business_source_reads(self.database, mapping, working, [], [], [], max_files=32, max_actions=4)
        pages = [{**source, **row, "evidence_id": f"ev_{number}",
                  "source_text": "\n".join(lines[row["start_line"] - 1:row["end_line"]])}
                 for number, row in enumerate(missing) if row.get("coverage_role") != "remaining_physical_source"]
        late_call = max(number + 1 for number, line in enumerate(lines) if line.startswith("CALL"))
        self.assertFalse(any(page["start_line"] <= late_call <= page["end_line"] for page in pages))
        for _ in range(100):
            actions, pending = _business_source_reads(self.database, mapping, working, pages, [], [],
                                                      max_files=32, max_actions=4)
            if not pending:
                break
            self.assertTrue(actions, "A later physical gap must stay actionable")
            for action in actions:
                row = action["arguments"]
                self.assertLessEqual(row["end_line"] - row["start_line"] + 1, 96)
                pages.append({**source, **row, "evidence_id": f"ev_{len(pages)}",
                    "source_text": "\n".join(lines[row["start_line"] - 1:row["end_line"]])})
        self.assertEqual(pending, [])
        self.assertTrue(any(page["start_line"] <= late_call <= page["end_line"] for page in pages))

    def test_more_than_thirty_two_primary_rules_resume_within_the_same_file(self):
        body = "\n".join(f"STEP-{number:02d}.\nADD {number + 1} TO TOTAL-AMOUNT." for number in range(37))
        self.build({"entry.cbl": program("ENTRY-CHAIN", body)})
        question = "请详细说明 ENTRY-CHAIN 的整体业务流程及各个步骤处理顺序。"
        mapping = build_business_map(self.database, self.root, question)
        first = build_question_investigation(question, mapping, database_path=self.database,
            source_pages=self.pages(), advance_frontier=True)
        state = first["investigation_frontier_state"]
        self.assertEqual(len(state["active_rule_ids"]), 32)
        self.assertTrue(state["primary_rules_remaining"])
        self.assertTrue(state["batch_complete"])
        self.assertFalse(first["investigation_complete"])
        self.assertEqual(first["pending_work"]["primary_rule_batch_count"], 1)
        second = build_question_investigation(question, mapping, database_path=self.database,
            source_pages=self.pages(), investigation_frontier_state=json.loads(json.dumps(state)),
            advance_frontier=True)
        following = second["investigation_frontier_state"]
        self.assertEqual(following["active_paths"], state["active_paths"])
        self.assertEqual(len(following["active_rule_ids"]), 5)
        self.assertFalse(following["primary_rules_remaining"])
        self.assertEqual(second["pending_work"]["primary_rule_batch_count"], 0)
        self.assertFalse(set(following["active_rule_ids"]) & set(state["active_rule_ids"]))
        self.assertEqual(len(set(following["active_rule_ids"]) | set(state["active_rule_ids"])), 37)
        self.assertNotEqual(second["obligation_batch_key"], first["obligation_batch_key"])

    def test_new_snapshot_discards_old_navigation_and_rule_progress(self):
        mapping = self.chain()
        with QuestionSourceSession(self.database, self.root) as session:
            previous = build_complete_working_set(self.database, session, mapping,
                AgentPolicy(max_semantic_files=4), business_overview=True)
        rules = build_question_investigation(self.question, mapping, database_path=self.database,
            source_pages=self.pages(), advance_frontier=True)["investigation_frontier_state"]
        path = self.root / "entry.cbl"
        path.write_text(path.read_text() + "*> source version changed\n", encoding="utf-8")
        build_business_index(self.root, self.database, source_format="free", quiet=True,
                             verify_content=True, framework_reference_path="")
        ensure_repository_search(self.database, self.root)
        current = build_business_map(self.database, self.root, self.question)
        self.assertNotEqual(mapping["snapshot_id"], current["snapshot_id"])
        with QuestionSourceSession(self.database, self.root) as session:
            result = build_complete_working_set(self.database, session, current,
                AgentPolicy(max_semantic_files=4), business_overview=True,
                frontier_state=previous["source_frontier_state"])
        self.assertIn("entry.cbl", result["metadata"]["candidate_paths"])
        self.assertEqual(result["source_frontier_state"]["reset_reason"], "source_frontier_scope_changed")
        investigation = build_question_investigation(self.question, current, database_path=self.database,
            source_pages=self.pages(), investigation_frontier_state=rules, advance_frontier=True,
            source_frontier_state=previous["source_frontier_state"])
        self.assertEqual(investigation["investigation_frontier_state"]["reset_reason"], "investigation_frontier_scope_changed")
        self.assertIn("entry.cbl", investigation["investigation_frontier_state"]["active_paths"])
        self.assertEqual(investigation["source_frontier_reset_reason"], "source_frontier_scope_changed")

    def chat(self, *, requests, request_limit=5, **options):
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            pages = [page for bundle in payload["source_context"] for page in bundle.get("pages", [])]
            text = "已读取当前流程中的处理步骤。" + "".join(f"[{page['evidence_id']}]" for page in pages)
            return TransportResponse(200, json.dumps({"choices": [{"message": {
                "role": "assistant", "content": text}, "finish_reason": "stop"}]}, ensure_ascii=False))
        config = CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model", api_key="offline-key")
        return run_business_chat(self.question, self.database, self.root, config, transport=transport,
            framework_reference_path="", conversation_id="frontier-conversation",
            policy=AgentPolicy(max_model_requests=request_limit, max_answer_revisions=0,
                               max_semantic_files=4, context_compaction_enabled=False), **options)["agent_result"]

    def test_chat_advances_all_source_and_rule_batches_before_completing(self):
        self.chain(11)
        requests = []
        result = self.chat(requests=requests)
        self.assertGreaterEqual(len(requests), 3)
        store = RunStore(self.database.parent / "investigation-runs.sqlite")
        checkpoint = store.load(result["run_id"])["checkpoint"]
        state = checkpoint["source_frontier_state"]
        self.assertEqual(len(state["read_candidate_paths"]), 12)
        self.assertEqual(state["pending_paths"], [])
        self.assertEqual(state["pending_expansions"], [])
        self.assertGreaterEqual(len(checkpoint["investigation_obligation_ledger"]), 2)
        for payload in requests:
            investigation = payload["question_investigation"]
            self.assertNotIn("investigation_frontier_state", investigation)
            self.assertNotIn("unprojected_open_gaps", investigation)
        self.assertTrue(any(request["question_investigation"].get("pending_work", {}).get("source_path_count")
                            for request in requests))

    def test_finite_chat_preserves_pending_work_and_explicit_continuation_advances_it(self):
        self.chain(11)
        requests, updates = [], []
        first = self.chat(requests=requests, request_limit=1, progress=updates.append)
        self.assertEqual(first["status"], "PARTIAL")
        self.assertEqual(first["material_gap"], "investigation_frontier_pending")
        store = RunStore(self.database.parent / "investigation-runs.sqlite")
        saved = store.load(first["run_id"])
        states = [update["analysis_state"] for update in updates if update.get("analysis_state")]
        self.assertTrue(any(state["has_pending_work"] and state["counts"]["global_pending"] > 0
                            for state in states))
        self.assertTrue(saved["checkpoint"]["analysis_state"]["has_pending_work"])
        self.assertGreater(saved["checkpoint"]["analysis_state"]["counts"]["global_pending"], 0)
        before = len(saved["checkpoint"]["source_frontier_state"]["read_candidate_paths"])
        second = self.chat(requests=requests, request_limit=1, continue_investigation=True,
                           resume_run_id=first["run_id"])
        after = store.load(second["run_id"])["checkpoint"]
        self.assertGreater(len(after["source_frontier_state"]["read_candidate_paths"]), before)
        self.assertEqual(store.load(first["run_id"]), saved)
        self.assertEqual(len(requests), 2)

    def test_actual_chat_advances_rule_batches_once_even_when_source_is_unchanged(self):
        body = "\n".join(f"STEP-{number:02d}.\nADD {number + 1} TO TOTAL-AMOUNT." for number in range(37))
        self.build({"entry.cbl": program("ENTRY-CHAIN", body)})
        self.question = "请详细说明 ENTRY-CHAIN 的整体业务流程及各个步骤处理顺序。"
        requests = []
        result = self.chat(requests=requests)
        batches = [payload["question_investigation"]["obligation_batch_key"] for payload in requests]
        self.assertEqual(len(batches), 2)
        self.assertEqual(len(set(batches)), 2)
        checkpoint = RunStore(self.database.parent / "investigation-runs.sqlite").load(result["run_id"])["checkpoint"]
        state = checkpoint["investigation_frontier_state"]
        self.assertEqual(len(set(state["processed_rule_ids"]) | set(state["active_rule_ids"])), 37)
        self.assertEqual(len(checkpoint["investigation_obligation_ledger"]), 2)

    def test_earlier_unresolved_callee_is_retained_after_later_batches_are_read(self):
        self.chain(11)
        path = self.root / "entry.cbl"
        path.write_text(path.read_text().replace("GOBACK.", 'CALL "ABSENT-WORK".\nGOBACK.'), encoding="utf-8")
        build_business_index(self.root, self.database, source_format="free", quiet=True,
                             verify_content=True, framework_reference_path="")
        ensure_repository_search(self.database, self.root)
        requests = []
        result = self.chat(requests=requests)
        saved = RunStore(self.database.parent / "investigation-runs.sqlite").load(result["run_id"])["checkpoint"]
        ledger = saved["investigation_obligation_ledger"]
        self.assertGreaterEqual(len(ledger), 2)
        final = result["investigation_state"]["question_investigation"]
        earlier = [gap for gap in final["open_gaps"] if gap.get("obligation_batch_key")
                   and gap["obligation_batch_key"] != final.get("obligation_batch_key")]
        self.assertTrue(any(gap.get("reason") == "external_implementation_unavailable" for gap in earlier))
        self.assertFalse(final["investigation_complete"])
        self.assertEqual(result["status"], "PARTIAL")

    def test_prompt_projection_never_serializes_local_frontier_or_all_gap_records(self):
        payload = {"question": "Explain the process", "source_context": [{"pages": []}],
            "question_investigation": {"can_answer": False, "investigation_complete": False,
                "pending_work": {"rule_path_count": 20}, "required_items": [],
                "investigation_frontier_state": {"known_paths": ["hidden-local.cbl"]},
                "unprojected_open_gaps": [{"local": "not-for-wire"}]}}
        projected = _prompt_payload(payload)["question_investigation"]
        self.assertFalse(projected["can_answer"])
        self.assertEqual(projected["pending_work"], {"rule_path_count": 20})
        self.assertNotIn("investigation_frontier_state", projected)
        self.assertNotIn("unprojected_open_gaps", projected)

    def test_more_than_sixteen_logical_view_objects_remain_in_the_saved_cursor(self):
        selects = "\n".join(f"SELECT FILE-{number:02d} ASSIGN TO 'VIEW{number:02d}'." for number in range(21))
        self.build({"entry.cbl": "IDENTIFICATION DIVISION.\nPROGRAM-ID. ENTRY-VIEWS.\n"
            "ENVIRONMENT DIVISION.\nINPUT-OUTPUT SECTION.\nFILE-CONTROL.\n" + selects +
            "\nDATA DIVISION.\nFILE SECTION.\nPROCEDURE DIVISION.\nGOBACK.\n",
            **{f"VIEW{number:02d}.dds": f"     A          R RECORD{number:02d} PFILE(BASE{number:02d})\n"
               for number in range(21)},
            **{f"BASE{number:02d}.pf": f"     A          R RECORD{number:02d}\n" for number in range(21)}})
        mapping = build_business_map(self.database, self.root, "请完整分析 ENTRY-VIEWS 的业务流程和文件链路。")
        state, paths, definition_cursor = None, [], False
        with QuestionSourceSession(self.database, self.root) as session:
            for _ in range(20):
                result = build_complete_working_set(self.database, session, mapping,
                    AgentPolicy(max_semantic_files=4), business_overview=True, frontier_state=state)
                state = json.loads(json.dumps(result["source_frontier_state"]))
                definition_cursor |= any(row["relation_kind"] == "file_definitions"
                                         for row in state["pending_expansions"])
                paths.extend(result["metadata"]["candidate_paths"])
                if not state["pending_paths"] and not state["pending_expansions"]:
                    break
        self.assertTrue(definition_cursor)
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(set(paths), {path.name for path in self.root.iterdir()})
        self.assertEqual(len(paths), 43)
        self.assertTrue(result["metadata"]["traversal_complete"])
        self.assertFalse(result["metadata"]["closure_complete"], "source names do not prove runtime library binding")

    def test_global_dependency_depth_is_not_capped_by_the_per_batch_file_allowance(self):
        self.build({f"step-{number:02d}.cbl": program(f"STEP{number:02d}",
            f'CALL "STEP{number + 1:02d}".' if number < 11 else "ADD 9 TO TOTAL-AMOUNT.")
            for number in range(12)})
        mapping = build_business_map(self.database, self.root, "请完整分析 STEP00 的业务流程及全部后续链路。")
        state, paths = None, []
        with QuestionSourceSession(self.database, self.root) as session:
            for _ in range(8):
                result = build_complete_working_set(self.database, session, mapping,
                    AgentPolicy(max_semantic_files=3), business_overview=True, frontier_state=state)
                state = json.loads(json.dumps(result["source_frontier_state"]))
                self.assertLessEqual(len(result["metadata"]["candidate_paths"]), 3)
                paths.extend(result["metadata"]["candidate_paths"])
                if not state["pending_paths"] and not state["pending_expansions"]:
                    break
        self.assertEqual(len(paths), 12)
        self.assertEqual(len(set(paths)), 12)
        self.assertTrue(result["metadata"]["closure_complete"])
        self.assertIsNone(result["metadata"]["depth_limit"])
        self.assertNotIn("dependency_depth_budget", {row["reason"] for row in state["unresolved_boundaries"]})


if __name__ == "__main__":
    unittest.main()
