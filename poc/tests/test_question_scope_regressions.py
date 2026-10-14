from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from business_map import build_business_map
from business_synthesis import assess_business_answer, answer_requirements
from company_api import CompanyAPIConfig, TransportResponse
from question_investigation import build_question_investigation, _indexed_candidates, _question_scopes, _calculation_paths
from question_planner import question_references
from repository_discovery import ensure_repository_search


class QuestionScopeRegressions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"

    def write(self, body, data="01 IN-AMOUNT PIC 9(6)V99.\n01 OUT-AMOUNT PIC 9(6)V99."):
        (self.source / "entry.cbl").write_bytes(("IDENTIFICATION DIVISION.\nPROGRAM-ID. VALUECASE.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n" + data + "\nPROCEDURE DIVISION.\n" + body + "\n").encode())
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        ensure_repository_search(self.database, self.source)

    def page(self, start=1, end=None):
        raw = (self.source / "entry.cbl").read_bytes()
        lines = raw.decode().splitlines()
        end = len(lines) if end is None else end
        return {"relative_path": "entry.cbl", "source_sha256": hashlib.sha256(raw).hexdigest(),
            "start_line": start, "end_line": end, "source_text": "\n".join(lines[start-1:end]),
            "evidence_id": f"page-{start}-{end}", "selection_reasons": ["question_match"]}

    def investigate(self, question, pages=(), **options):
        return build_question_investigation(question, build_business_map(self.database, self.source, question),
                                            database_path=self.database, source_pages=pages, **options)

    def item(self, result, kind):
        return next(item for item in result["required_items"] if item["kind"] == kind)

    def test_qualified_dataref_uses_frontend_identity_not_extra_tokens(self):
        self.assertEqual(question_references("AMOUNT IN INPUT-REC是什么？"), ("AMOUNT OF INPUT-REC",))
        self.write("MAIN.\nCOMPUTE AMOUNT OF OUTPUT-REC = AMOUNT OF INPUT-REC * 2.\nGOBACK.\n"
                   "UNRELATED.\nADD A TO B GIVING A.\nMOVE A TO B.",
                   "01 INPUT-REC.\n 05 AMOUNT PIC 9(6)V99.\n01 OUTPUT-REC.\n 05 AMOUNT PIC 9(6)V99.\n01 A PIC 9.\n01 B PIC 9.")
        result = self.investigate("VALUECASE AMOUNT OF OUTPUT-REC 从哪里来？它和 AMOUNT IN INPUT-REC 一样吗？", [self.page()])
        self.assertEqual(set(result["analysis_plan"]["entities"]), {"AMOUNT OF OUTPUT-REC", "AMOUNT OF INPUT-REC"})
        self.assertEqual(self.item(result, "business_steps")["candidate_count"], 1)
        self.assertFalse(any(gap.get("field") in {"A", "B"} for gap in result["open_gaps"]))
        self.assertEqual(self.item(result, "declarations")["candidate_count"], 4)

    def test_noop_procedure_has_complete_body_obligation_not_peer_formula(self):
        self.write("MAIN.\nGOBACK.\n1400-COMPUTE-FEES.\nEXIT.\nOTHER-RULE.\n"
                   "IF IN-AMOUNT > 0\nCOMPUTE OUT-AMOUNT = IN-AMOUNT / 1200\nEND-IF.\nCALL 'UNAVAILABLE' USING OUT-AMOUNT.")
        question = "VALUECASE 的1400-COMPUTE-FEES实现了哪些手续费规则？"
        result = self.investigate(question, [self.page()])
        self.assertEqual(result["analysis_plan"]["intents"], ("explanation",))
        self.assertEqual(result["analysis_plan"]["source_scopes"][0]["procedure_name"], "1400-COMPUTE-FEES")
        self.assertEqual(self.item(result, "procedure_body")["status"], "SATISFIED")
        self.assertEqual(self.item(result, "formula")["status"], "NOT_APPLICABLE")
        self.assertEqual(self.item(result, "dependencies")["candidate_count"], 0)
        self.assertEqual(result["planned_actions"], [])
        self.assertEqual(result["open_gaps"], [])
        self.assertTrue(result["can_answer"])
        answer = "该完整段落只有EXIT，没有手续费算法。"
        self.assertEqual(answer_requirements(question, result, [self.page()]), [])
        self.assertNotEqual(assess_business_answer(question, answer, result, [self.page()])["status"], "incomplete")

    def test_unknown_effect_within_target_routine_is_preserved(self):
        self.write("MAIN.\nGOBACK.\n1400-COMPUTE-FEES.\nCALL 'UNAVAILABLE' USING OUT-AMOUNT.\nEXIT.\nOTHER-RULE.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT / 1200.")
        result = self.investigate("VALUECASE 1400-COMPUTE-FEES 的计算规则？", [self.page()])
        self.assertEqual(self.item(result, "dependencies")["status"], "UNRESOLVED")
        self.assertIn("UNAVAILABLE", self.item(result, "dependencies")["targets"])
        self.assertNotEqual(result["open_gaps"], [])

    def test_compound_field_and_section_question_keeps_independent_formulas(self):
        self.write("MAIN.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nPERFORM SELECT-SECTION.\nGOBACK.\n"
                   "SELECT-SECTION SECTION.\nFIRST-RULE.\nMOVE 1 TO OTHER-AMOUNT.\nNEXT-SECTION SECTION.\nEXIT.",
                   "01 IN-AMOUNT PIC 9(6)V99.\n01 OUT-AMOUNT PIC 9(6)V99.\n01 OTHER-AMOUNT PIC 9(6)V99.")
        question = "VALUECASE 的OUT-AMOUNT如何计算，同时SELECT-SECTION执行范围是什么？"
        result = self.investigate(question, [self.page()])
        self.assertFalse(result["analysis_plan"]["source_scopes"][0]["exclusive"])
        self.assertEqual(self.item(result, "formula")["candidate_count"], 1)
        self.assertEqual(self.item(result, "procedure_body")["candidate_count"], 1)

    def test_explicit_whole_program_keeps_named_procedure_as_nonexclusive_evidence(self):
        self.write("MAIN.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nGOBACK.\nFOCUS-RULE.\n"
                   "COMPUTE OUT-AMOUNT = IN-AMOUNT * 3.")
        question = "VALUECASE 整个程序包括 FOCUS-RULE 的计算规则是什么？"
        scopes = _question_scopes(self.database, ["entry.cbl"], question)
        self.assertEqual(len(scopes), 1)
        self.assertIs(scopes[0]["exclusive"], False)
        result = self.investigate(question, [self.page()])
        self.assertEqual(self.item(result, "formula")["candidate_count"], 2)

    def test_procedure_lookup_keeps_legacy_index_fallback(self):
        self.write("MAIN.\nGOBACK.\nFOCUS-RULE.\nEXIT.")
        expected = _question_scopes(self.database, ["entry.cbl"], "VALUECASE FOCUS-RULE 是什么？")
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("DROP INDEX idx_symbols_path")
        actual = _question_scopes(self.database, ["entry.cbl"], "VALUECASE FOCUS-RULE 是什么？")
        self.assertEqual(actual, expected)

    def test_unbound_or_declared_unused_target_does_not_select_peer_formulas(self):
        self.write("MAIN.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nGOBACK.",
                   "01 IN-AMOUNT PIC 9.\n01 OUT-AMOUNT PIC 9.\n01 UNUSED-AMOUNT PIC 9.")
        missing = self.investigate("VALUECASE MISSING-AMOUNT 怎样计算？", [self.page()])
        self.assertEqual(self.item(missing, "formula")["candidate_count"], 0)
        self.assertTrue(any(gap.get("reason") == "requested_field_not_bound" for gap in missing["open_gaps"]))
        unused = self.investigate("VALUECASE UNUSED-AMOUNT 怎样计算？", [self.page()])
        self.assertEqual(self.item(unused, "formula")["candidate_count"], 0)
        self.assertEqual(self.item(unused, "declarations")["candidate_count"], 1)

    def test_unique_group_item_bare_spelling_keeps_its_actual_formula(self):
        self.write("MAIN.\nCOMPUTE VALUE-00000 = VALUE-00001 * 2.\nGOBACK.",
                   "01 SOURCE-RECORD.\n05 VALUE-00000 PIC 9(9).\n05 VALUE-00001 PIC 9(9).")
        result = self.investigate("VALUECASE VALUE-00000 是怎么计算的？", [self.page()])
        self.assertEqual(self.item(result, "formula")["candidate_count"], 1)
        self.assertEqual(self.item(result, "formula")["status"], "SATISFIED")
        self.assertFalse(any(gap.get("reason") == "requested_field_not_bound" for gap in result["open_gaps"]))
        qualified = self.investigate("VALUECASE VALUE-00000 OF SOURCE-RECORD 是怎么计算的？", [self.page()])
        self.assertEqual(self.item(qualified, "formula")["candidate_count"], 1)
        self.assertEqual(self.item(qualified, "formula")["status"], "SATISFIED")

    def test_condition_field_selects_parser_guarded_effects_not_peer_procedure(self):
        self.write("MAIN.\nIF MODE-FLAG OF INPUT-REC = 1\nMOVE 17 TO OUT-AMOUNT\n"
                   "ELSE\nMOVE ZERO TO OUT-AMOUNT\nEND-IF.\nGOBACK.\nOTHER-RULE.\n"
                   "IF MODE-FLAG OF OTHER-REC = 1\nMOVE 99 TO OUT-AMOUNT\nEND-IF.",
                   "01 INPUT-REC.\n05 MODE-FLAG PIC 9 VALUE 1.\n01 OTHER-REC.\n"
                   "05 MODE-FLAG PIC 9 VALUE 0.\n01 OUT-AMOUNT PIC 9(6)V99.")
        candidates, _, _ = _indexed_candidates(self.database, ["entry.cbl"],
            "VALUECASE MODE-FLAG OF INPUT-REC 的业务规则？", business_steps=True)
        steps = [row["statement"] for row in candidates if row["kind"] == "business_steps"]
        self.assertEqual(len(steps), 3)
        self.assertTrue(any("MODE-FLAG OF INPUT-REC" in text for text in steps))
        self.assertTrue(any("MOVE 17 TO OUT-AMOUNT" in text for text in steps))
        self.assertTrue(any("MOVE ZERO TO OUT-AMOUNT" in text for text in steps))
        self.assertFalse(any("OTHER-REC" in text or "MOVE 99" in text for text in steps))

    def test_business_effect_read_precedes_still_required_numeric_layout(self):
        self.write("MAIN.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nGOBACK.")
        question = "VALUECASE OUT-AMOUNT 怎样计算？"
        first = self.investigate(question, max_actions=1)
        self.assertEqual(first["planned_actions"][0]["reason"], "formula_source_not_supplied")
        self.assertEqual(self.item(first, "declarations")["status"], "OPEN")
        formula_line = self.page()["source_text"].splitlines().index("COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.") + 1
        after = self.investigate(question, [self.page(formula_line, formula_line)], max_actions=1)
        self.assertEqual(self.item(after, "formula")["status"], "SATISFIED")
        self.assertEqual(self.item(after, "declarations")["status"], "OPEN")
        self.assertEqual(after["planned_actions"][0]["tool"], "read")
        self.assertEqual(after["planned_actions"][0]["reason"], "declarations_source_not_supplied")

    def test_auto_fixed_controlled_effects_use_the_shared_preprocessor(self):
        self.write("MAIN.\nIF IN-AMOUNT > 0\nMOVE 17 TO OUT-AMOUNT\nELSE\nMOVE ZERO TO OUT-AMOUNT\nEND-IF.\nGOBACK.")
        path = self.source / "entry.cbl"
        path.write_text("\n".join(f"{number:06d} {line}" for number, line in
                                  enumerate(path.read_text().splitlines(), 1)) + "\n")
        build_business_index(self.source, self.database, source_format="auto", verify_content=True)
        ensure_repository_search(self.database, self.source)
        candidates, frontier, _ = _indexed_candidates(self.database, ["entry.cbl"],
            "VALUECASE IN-AMOUNT 的业务规则？", business_steps=True)
        self.assertEqual(sum(row["kind"] == "business_steps" for row in candidates), 3)
        self.assertFalse(any(gap["reason"] in {"control_scope_incomplete", "control_scope_source_not_indexed"}
                             for gap in frontier))

    def test_controlled_duplicate_shape_uses_requested_procedure_occurrence(self):
        self.write("MAIN.\nGOBACK.\nFIRST-RULE.\nIF IN-AMOUNT > 0\nMOVE 17 TO OUT-AMOUNT\nEND-IF.\n"
                   "FOCUS-RULE.\nIF IN-AMOUNT > 0\nMOVE 17 TO OUT-AMOUNT\nEND-IF.")
        with closing(sqlite3.connect(self.database)) as db, db:
            for kind in ("IF", "MOVE"):
                first = db.execute("SELECT rule_id FROM business_rules WHERE paragraph_name='FIRST-RULE' AND rule_kind=?",
                                   (kind,)).fetchone()[0]
                focused = db.execute("SELECT rule_id FROM business_rules WHERE paragraph_name='FOCUS-RULE' AND rule_kind=?",
                                     (kind,)).fetchone()[0]
                db.execute("UPDATE business_rule_occurrences SET rule_id=? WHERE rule_id=?", (first, focused))
        question = "VALUECASE 只看 FOCUS-RULE：IN-AMOUNT 控制哪些业务结果？"
        scopes = _question_scopes(self.database, ["entry.cbl"], question)
        candidates, _, _ = _indexed_candidates(self.database, ["entry.cbl"], question,
                                               business_steps=True, source_scopes=scopes)
        effects = [row for row in candidates if row["kind"] == "business_steps" and row["rule_kind"] == "MOVE"]
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0]["paragraph_name"], "FOCUS-RULE")
        self.assertEqual(effects[0]["start_line"], scopes[0]["start_line"] + 2)

    def test_manual_label_discovery_requires_local_source_match_without_field_binding(self):
        self.write("MAIN.\nDISPLAY 'PERIOD-LABEL'.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nGOBACK.")
        question = "SPECIAL-PERIOD 的源码公式？"
        absent, _, _ = _indexed_candidates(self.database, ["entry.cbl"], question,
                                          discovery_terms=["ABSENT-LABEL"])
        self.assertFalse(any(row["kind"] == "formula" for row in absent))
        found, frontier, _ = _indexed_candidates(self.database, ["entry.cbl"], question,
                                                discovery_terms=["PERIOD-LABEL"])
        self.assertEqual(sum(row["kind"] == "formula" for row in found), 1)
        self.assertTrue(any(gap["reason"] == "requested_field_not_bound" for gap in frontier))

    def test_unqualified_duplicate_records_keep_candidates_and_ambiguity(self):
        self.write("MAIN.\nCOMPUTE AMOUNT OF OUTPUT-REC = AMOUNT OF INPUT-REC * 2.\nGOBACK.",
                   "01 INPUT-REC.\n05 AMOUNT PIC 9(6)V99.\n01 OUTPUT-REC.\n05 AMOUNT PIC 9(6)V99.")
        result = self.investigate("VALUECASE AMOUNT 怎样计算？", [self.page()])
        gap = next(gap for gap in result["open_gaps"] if gap.get("reason") == "field_reference_ambiguous")
        self.assertEqual(set(gap["candidates"]), {"AMOUNT OF INPUT-REC", "AMOUNT OF OUTPUT-REC"})
        self.assertEqual(self.item(result, "formula")["candidate_count"], 1)
        self.assertNotEqual(result["open_gaps"], [])

    def repeated_if_program(self):
        self.write("MAIN.\nGOBACK.\nFOCUS-RULE.\n" + "\n".join(
            f"IF INPUT-{index:03d} > {index}\nCOMPUTE OUT-AMOUNT = INPUT-{index:03d} * {index+1}\nEND-IF."
            for index in range(45)), data="\n".join(f"01 INPUT-{index:03d} PIC 9(9)." for index in range(45)) + "\n01 OUT-AMOUNT PIC 9(9).")

    def test_two_occurrences_are_selected_before_budget_and_pic_reads_are_real(self):
        self.repeated_if_program()
        question = "VALUECASE FOCUS-RULE 第一条 IF 如何计算 OUT-AMOUNT，输入在哪里声明，紧接下一条 IF 怎样覆盖？"
        scope = _question_scopes(self.database, ["entry.cbl"], question)[0]
        result = self.investigate(question, [self.page(scope["start_line"], scope["end_line"])])
        self.assertEqual(self.item(result, "formula")["candidate_count"], 2)
        self.assertEqual(self.item(result, "conditions")["candidate_count"], 2)
        self.assertEqual(self.item(result, "declarations")["candidate_count"], 3)
        self.assertFalse(any("budget" in gap.get("reason", "") for gap in result["open_gaps"]))
        actions = [action for action in result["planned_actions"] if action["tool"] == "read"]
        self.assertTrue(actions)
        acquired = [self.page(action["arguments"]["start_line"], action["arguments"]["end_line"]) for action in actions]
        after = self.investigate(question, [self.page(scope["start_line"], scope["end_line"]), *acquired])
        self.assertEqual(self.item(after, "declarations")["status"], "SATISFIED")
        self.assertEqual(after["planned_actions"], [])
        self.assertEqual(set(self.item(after, "inputs")["fields"]), {"INPUT-000", "INPUT-001"})
        # No VALUE or assignment establishes a runtime input. Supplying PIC
        # does not turn this explicit unknown into an invented value origin.
        self.assertEqual(self.item(after, "inputs")["status"], "UNRESOLVED")

    def test_occurrence_window_and_pic_fit_three_actual_source_reads(self):
        self.repeated_if_program()
        question = "VALUECASE FOCUS-RULE 前两个 IF 如何计算 OUT-AMOUNT，输入在哪里声明？"
        before = self.investigate(question, max_actions=3)
        actions = before["planned_actions"]
        self.assertEqual(len(actions), 3)
        self.assertTrue(all(action["tool"] == "read" for action in actions))
        acquired = [self.page(action["arguments"]["start_line"], action["arguments"]["end_line"])
                    for action in actions]
        text = "\n".join(page["source_text"] for page in acquired)
        for literal in ("IF INPUT-000 > 0", "COMPUTE OUT-AMOUNT = INPUT-000 * 1",
                        "IF INPUT-001 > 1", "COMPUTE OUT-AMOUNT = INPUT-001 * 2",
                        "01 INPUT-000 PIC 9(9).", "01 INPUT-001 PIC 9(9).", "01 OUT-AMOUNT PIC 9(9)."):
            self.assertIn(literal, text)
        after = self.investigate(question, acquired, max_actions=3)
        for kind in ("formula", "conditions", "declarations"):
            self.assertEqual(self.item(after, kind)["status"], "SATISFIED")
        self.assertFalse(after["planned_actions"])

    def test_occurrence_window_cannot_promote_truncated_nested_if_to_closed_scope(self):
        self.write("MAIN.\nGOBACK.\nFOCUS-RULE.\nIF IN-AMOUNT > 0\n IF IN-AMOUNT > 1\n"
                   " COMPUTE OUT-AMOUNT = IN-AMOUNT * 2\n END-IF\n IF IN-AMOUNT > 2\n"
                   " COMPUTE OUT-AMOUNT = IN-AMOUNT * 3\n END-IF\nEND-IF.")
        result = self.investigate("VALUECASE FOCUS-RULE 前两个 IF 如何计算 OUT-AMOUNT？", [self.page()])
        self.assertEqual(result["source_scope"]["ranges"][0]["control_scope_status"], "incomplete_or_unsupported")
        self.assertTrue(any(gap.get("reason") == "statement_scope_incomplete" for gap in result["open_gaps"]))

    def test_same_shape_in_other_procedure_keeps_target_occurrence_and_evidence(self):
        self.write("MAIN.\nGOBACK.\nFIRST-RULE.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\n"
                   "FOCUS-RULE.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2.\nLAST-RULE.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 7.")
        # Emulate a parser adapter which shares retrieval shape across two
        # occurrences; their physical/procedure positions remain independent.
        with closing(sqlite3.connect(self.database)) as db, db:
            first = db.execute("SELECT rule_id FROM business_rules WHERE paragraph_name='FIRST-RULE'").fetchone()[0]
            focused = db.execute("SELECT rule_id FROM business_rules WHERE paragraph_name='FOCUS-RULE'").fetchone()[0]
            db.execute("UPDATE business_rule_occurrences SET rule_id=? WHERE rule_id=?", (first, focused))
        question = "VALUECASE FOCUS-RULE 的 OUT-AMOUNT 怎样计算？"
        scopes = _question_scopes(self.database, ["entry.cbl"], question)
        candidates, _, _ = _indexed_candidates(self.database, ["entry.cbl"], question, source_scopes=scopes)
        formula = next(row for row in candidates if row["kind"] == "formula")
        target = scopes[0]["start_line"] + 1
        self.assertEqual(formula["start_line"], target)
        self.assertEqual(formula["paragraph_name"], "FOCUS-RULE")
        self.assertEqual(len(formula["occurrences"]), 1)
        before = self.investigate(question, [self.page(scopes[0]["start_line"] - 2, scopes[0]["start_line"] - 1)])
        self.assertEqual(self.item(before, "formula")["missing_count"], 1)
        after = self.investigate(question, [self.page(target, target), self.page(5, 6)])
        self.assertEqual(self.item(after, "formula")["status"], "SATISFIED")

    def test_scope_survives_actual_saved_history_not_assistant_prose(self):
        self.repeated_if_program()
        question = "VALUECASE FOCUS-RULE 前两个 IF 如何计算 OUT-AMOUNT？"
        previous = self.investigate(question, [self.page()])
        current = self.investigate("刚才这两条计算的OUT-AMOUNT从哪里来？", [self.page()], history=[
            {"role": "assistant", "investigation_state": {"question_investigation": previous}}])
        self.assertEqual(current["analysis_plan"]["source_scopes"], previous["analysis_plan"]["source_scopes"])
        self.assertEqual(sum(item["candidate_count"] for item in current["required_items"]
                             if item["kind"] in {"business_steps", "formula"}), 2)
        outside = self.investigate("其他那些 IF 如何计算 OUT-AMOUNT？", [self.page()], history=[
            {"role": "assistant", "content": "继续只看前两个IF"}])
        self.assertEqual(outside["analysis_plan"]["source_scopes"], ())

    def test_default_chat_noop_short_answer_does_not_enter_peer_calculation_review(self):
        self.write("MAIN.\nGOBACK.\n1400-COMPUTE-FEES.\nEXIT.\nOTHER-RULE.\nCOMPUTE OUT-AMOUNT = IN-AMOUNT / 1200.")
        requests = []
        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            requests.append(payload)
            page = next(page for context in payload["source_context"] for page in context["pages"]
                        if "1400-COMPUTE-FEES." in page["source_text"] and "EXIT." in page["source_text"])
            return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant",
                "content": "完整段落只有EXIT，没有手续费算法。[" + page["evidence_id"] + "]"}, "finish_reason": "stop"}]}))
        result = run_business_chat("VALUECASE 的1400-COMPUTE-FEES实现了哪些手续费规则？", self.database, self.source,
            CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model", api_key="offline-only"),
            transport=transport, allow_network=False, framework_reference_path=self.root / "absent.md",
            policy=AgentPolicy(max_model_requests=3, max_answer_revisions=1))
        self.assertEqual(len(requests), 1)
        investigation = result["agent_result"]["investigation_state"]["question_investigation"]
        self.assertEqual(self.item(investigation, "formula")["status"], "NOT_APPLICABLE")
        self.assertEqual(investigation["open_gaps"], [])
        self.assertNotEqual(result["agent_result"].get("stop_reason"), "answer_incomplete")

    def test_outside_call_does_not_expand_occurrence_scope_into_large_closure(self):
        paths = [f"member-{index:05d}.cbl" for index in range(12000)]
        edges = [{"caller_path": paths[index], "target_path": paths[(index+1) % len(paths)],
                  "relation_type": "CALLS", "resolution": "confirmed", "caller_line": 40}
                 for index in range(len(paths))]
        mapping = {"selected_paths": paths, "direct_paths": [paths[0]], "relations": edges}
        self.assertEqual(_calculation_paths(mapping, [], source_scopes=[{
            "relative_path": paths[0], "start_line": 48, "end_line": 55}]), [paths[0]])


if __name__ == "__main__":
    unittest.main()
