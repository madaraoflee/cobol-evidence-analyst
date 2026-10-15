"""A failed model request must not erase independently supported source facts."""

import hashlib
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claim_ledger import review_answer_claims
from evidence_answer import build_evidence_answer


def page(text, identifier="ev_local", path="rule.cbl", first=1):
    return {"evidence_id": identifier, "relative_path": path,
        "source_text": text, "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "start_line": first, "end_line": first + len(text.splitlines()) - 1, "span_truncated": False}


class EvidenceAnswerTests(unittest.TestCase):
    def build(self, question, pages, investigation=None, **options):
        return build_evidence_answer(question, pages, investigation or {}, source_format="free", **options)

    def test_formula_survives_without_model_success_or_final_value_claim(self):
        source = page("COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.")
        result = self.build("OUT-AMOUNT 怎么计算？", [source])
        self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", result["answer"])
        self.assertIn("[ev_local]", result["answer"])
        self.assertEqual(result["answer_origin"], "local_source_facts")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertFalse(result["model_answer_recorded"])
        self.assertFalse(result["execution_verified"])
        self.assertFalse(result["claims_semantically_verified"])
        self.assertEqual(result["manifest"]["source_ids"], ["ev_local"])
        self.assertEqual(result["evidence_refs"][0]["source_sha256"], source["source_sha256"])
        self.assertIn("最终结果尚未确认", result["answer"])
        answer, ledger = review_answer_claims(result["answer"], [source], {"ev_local": source}, source_format="free")
        self.assertIn("OUT-AMOUNT = IN-AMOUNT * 2", answer)
        self.assertGreater(ledger.to_dict()["counts"]["supported"], 0)

    def test_branch_guards_and_later_overwrite_remain_visible(self):
        text = "IF IN-AMOUNT > 0\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2\nELSE\nMOVE 0 TO OUT-AMOUNT\nEND-IF.\nMOVE 7 TO OUT-AMOUNT."
        result = self.build("OUT-AMOUNT 为什么最后变成零？", [page(text)])
        for expected in ("IF IN-AMOUNT > 0", "条件不成立", "OUT-AMOUNT = 0", "OUT-AMOUNT = 7"):
            self.assertIn(expected, result["answer"])
        self.assertEqual(len(result["facts"]), 3)
        self.assertNotIn("最终等于", result["answer"])

    def test_only_matching_field_is_selected_not_nearby_formula(self):
        result = self.build("OUT-AMOUNT 怎么计算？", [page("COMPUTE OTHER-AMOUNT ROUNDED = OTHER-IN * 3.\nMOVE 9 TO OUT-AMOUNT.")])
        self.assertIn("OUT-AMOUNT = 9", result["answer"])
        self.assertNotIn("OTHER-AMOUNT", result["answer"])
        self.assertNotIn("ROUNDED", result["answer"])

    def test_giving_receiver_and_actual_input_output_identity(self):
        result = self.build("C 怎么计算？", [page("ADD A TO B GIVING C.")])
        self.assertIn("C = A + B", result["answer"])
        self.assertEqual([fact["target"] for fact in result["facts"]], ["C"])
        same = self.build("B 怎么计算？", [page("ADD A TO B GIVING B.")])
        self.assertIn("B = A + B", same["answer"])

    def test_qualified_fields_are_distinct_and_in_alias_is_equivalent(self):
        source = page("MOVE 1 TO AMOUNT OF INPUT-REC.\nMOVE 2 TO AMOUNT OF OUTPUT-REC.")
        for question in ("AMOUNT OF OUTPUT-REC 的来源？", "AMOUNT IN OUTPUT-REC 的来源？"):
            result = self.build(question, [source])
            self.assertIn("AMOUNT OF OUTPUT-REC = 2", result["answer"])
            self.assertNotIn("AMOUNT OF INPUT-REC", result["answer"])
        both = self.build("AMOUNT 的来源？", [source])
        self.assertEqual(len(both["facts"]), 2)

    def test_indexed_and_reference_modified_targets_remain_exact(self):
        source = page("MOVE 1 TO VALUES-TABLE(1).\nMOVE 2 TO VALUES-TABLE(2).\nMOVE 'X' TO LABEL-TEXT(1:2).\nMOVE 'Y' TO LABEL-TEXT(3:2).")
        for target, selected, excluded in (("VALUES-TABLE(2)", "VALUES-TABLE(2)", "VALUES-TABLE(1)"),
                ("LABEL-TEXT(3:2)", "LABEL-TEXT(3:2)", "LABEL-TEXT(1:2)")):
            result = self.build(target + " 的来源？", [source])
            self.assertIsNotNone(result)
            self.assertIn(selected, result["answer"])
            self.assertNotIn(excluded, result["answer"])

    def test_unknown_question_never_uses_random_source_candidates(self):
        source = page("COMPUTE OUT-AMOUNT = IN-AMOUNT * 2.")
        self.assertIsNone(self.build("解释未定位的业务事项", [source]))
        self.assertIsNone(self.build("UNKNOWN-AMOUNT 怎么计算？", [source], {"analysis_plan": {"paths": ["rule.cbl"]}}))

    def test_validated_conversation_entity_restores_pronoun_focus(self):
        result = self.build("刚才第二种情况呢？", [page("MOVE 0 TO OUT-AMOUNT.\nMOVE 9 TO OTHER-AMOUNT.")],
            {"analysis_plan": {"entities": ["OUT-AMOUNT"], "focus_basis": "conversation_focus_and_source"}})
        self.assertIn("OUT-AMOUNT = 0", result["answer"])
        self.assertNotIn("OTHER-AMOUNT", result["answer"])

    def test_validated_scope_excludes_unrelated_procedure_occurrences(self):
        source = page("MOVE 1 TO STATUS-CODE.\nMOVE 2 TO STATUS-CODE.")
        result = self.build("这个段落呢？", [source], {"source_scope": {"ranges": [
            {"relative_path": "rule.cbl", "start_line": 2, "end_line": 2, "exclusive": True}]}})
        self.assertIn("STATUS-CODE = 2", result["answer"])
        self.assertNotIn("STATUS-CODE = 1", result["answer"])

    def test_identified_program_can_explain_its_visible_operations(self):
        source = page("IDENTIFICATION DIVISION.\nPROGRAM-ID. VALUE-WORKER.\nPROCEDURE DIVISION.\nMAIN.\nMOVE 8 TO OUT-AMOUNT.\nGOBACK.")
        result = self.build("VALUE-WORKER 有什么操作？", [source])
        self.assertIn("OUT-AMOUNT = 8", result["answer"])

    def test_real_call_preserves_guard_without_inventing_scheduler(self):
        source = page('PROCEDURE DIVISION.\nMAIN.\nIF RUN-FLAG = 1\nCALL "BATCH-WORKER" USING INPUT-REC\nEND-IF.\nGOBACK.')
        result = self.build("BATCH-WORKER 怎样触发？", [source])
        self.assertIn('CALL "BATCH-WORKER" USING INPUT-REC', result["answer"])
        self.assertIn("IF RUN-FLAG = 1", result["answer"])
        self.assertIn("不能证明外部调度配置", result["answer"])
        self.assertEqual(result["facts"][0]["support_scope"], "source_call_statement_syntax")
        self.assertFalse(result["facts"][0]["execution_verified"])

    def test_activation_fallback_uses_bound_upstream_call_not_unrelated_root_formula(self):
        root = page("IDENTIFICATION DIVISION.\nPROGRAM-ID. BATCH-WORKER.\nPROCEDURE DIVISION.\nMAIN.\n"
                    "COMPUTE OUT-AMOUNT = IN-AMOUNT + 1.\nGOBACK.", path="worker.cbl")
        caller = page('PROCEDURE DIVISION.\nMAIN.\nIF RUN-FLAG = 1\nCALL "BATCH-WORKER"\nEND-IF.\nGOBACK.',
                      identifier="ev_caller", path="launcher.cbl")
        plan = {"analysis_plan": {"intents": ["activation"], "paths": ["worker.cbl"], "entities": ["BATCH-WORKER"]},
                "activation_evidence": {"callers": [{"relative_path": "launcher.cbl", "evidence_ids": ["ev_caller"],
                    "source_sha256": caller["source_sha256"], "target_name": "BATCH-WORKER", "line": 4,
                    "occurrence_id": "bound-call-four", "binding_confirmed": True, "compilation_status": "active",
                    "scope_known": True, "conditions_supplied": True, "condition_candidate_count": 1,
                    "conditions": [{"kind": "IF", "condition": "RUN-FLAG = 1", "outcome": True,
                        "span": {"start_line": 3, "end_line": 3}, "evidence_ids": ["ev_caller"]}]}]}}
        result = self.build("BATCH-WORKER 的batch怎么trigger的？", [root, caller], plan)
        self.assertIn('CALL "BATCH-WORKER"', result["answer"])
        self.assertIn("IF RUN-FLAG = 1", result["answer"])
        self.assertNotIn("OUT-AMOUNT", result["answer"])
        self.assertEqual(result["manifest"]["source_ids"], ["ev_caller"])
        self.assertIn("不能证明外部调度配置", result["answer"])
        plan["activation_evidence"]["callers"][0]["source_sha256"] = "0" * 64
        self.assertIsNone(self.build("BATCH-WORKER 的batch怎么trigger的？", [root, caller], plan))

    def activation_fragments(self):
        text = 'PROCEDURE DIVISION.\nMAIN.\nIF RUN-FLAG = 1\nCALL "BATCH-WORKER"\nEND-IF.\nGOBACK.'
        sha = hashlib.sha256(text.encode()).hexdigest()
        guard = {**page('IF RUN-FLAG = 1', identifier="ev_guard", path="launcher.cbl", first=3), "source_sha256": sha}
        call = {**page('CALL "BATCH-WORKER"', identifier="ev_call", path="launcher.cbl", first=4), "source_sha256": sha}
        plan = {"analysis_plan": {"intents": ["activation"], "paths": ["worker.cbl"], "entities": ["BATCH-WORKER"]},
            "activation_evidence": {"callers": [{"relative_path": "launcher.cbl", "source_sha256": sha,
                "evidence_ids": ["ev_call"], "target_name": "BATCH-WORKER", "line": 4,
                "occurrence_id": "bound-call-four", "binding_confirmed": True, "compilation_status": "active",
                "scope_known": True, "conditions_supplied": True, "condition_candidate_count": 1,
                "conditions": [{"kind": "IF", "condition": "RUN-FLAG = 1", "outcome": True,
                    "span": {"start_line": 3, "end_line": 3}, "evidence_ids": ["ev_guard"]}]}]}}
        return guard, call, plan

    def test_bound_fragmented_caller_preserves_actual_guard_and_both_evidence_ids(self):
        guard, call, plan = self.activation_fragments()
        result = self.build("BATCH-WORKER怎样trigger？", [guard, call], plan)
        self.assertIn("IF RUN-FLAG = 1", result["answer"])
        self.assertIn("[ev_guard]", result["answer"])
        self.assertIn("[ev_call]", result["answer"])
        self.assertEqual(set(result["manifest"]["source_ids"]), {"ev_guard", "ev_call"})
        self.assertEqual(result["facts"][0]["conditions_status"], "supplied_source_scope")
        self.assertEqual(result["facts"][0]["bound_occurrence_id"], "bound-call-four")
        self.assertFalse(result["model_answer_recorded"])
        self.assertFalse(result["claims_semantically_verified"])
        # Exercise the real answer outlet as well as the fallback renderer. An
        # IF-only page remains a syntax witness even without its END-IF page.
        reviewed, ledger = review_answer_claims(result["answer"], [guard, call],
            {item["evidence_id"]: item for item in [guard, call]}, source_format="free")
        self.assertEqual(reviewed, result["answer"])
        self.assertIn("IF RUN-FLAG = 1", reviewed)
        self.assertIn("[ev_guard]", reviewed)
        self.assertIn("[ev_call]", reviewed)
        conditions = [entry for entry in ledger.entries if entry.claim_type == "condition_statement"]
        self.assertEqual(len(conditions), 1)
        condition = conditions[0]
        self.assertEqual(condition.condition, ["RUN-FLAG", "=", "1"])
        self.assertEqual(condition.support_status, "supported")
        self.assertEqual(condition.support_scope, "source_statement_syntax")
        self.assertEqual(set(condition.evidence_ids), {"ev_guard", "ev_call"})
        self.assertEqual({item["source_sha256"] for item in condition.source_versions}, {call["source_sha256"]})
        self.assertEqual({item["start_line"] for item in condition.source_versions}, {3, 4})
        self.assertEqual(ledger.to_dict()["revised_claim_count"], 0)
        self.assertFalse(ledger.to_dict()["prose_semantics_verified"])

    def test_caller_guard_metadata_is_not_substitute_for_actual_versioned_source(self):
        from copy import deepcopy
        guard, call, original = self.activation_fragments()
        changes = [lambda g, c, p: p["activation_evidence"]["callers"][0]["conditions"][0].update(evidence_ids=["ev_fake"]),
            lambda g, c, p: p["activation_evidence"]["callers"][0]["conditions"][0].update(outcome=False),
            lambda g, c, p: p["activation_evidence"]["callers"][0]["conditions"][0].update(condition="RUN-FLAG = 7"),
            lambda g, c, p: g.update(source_sha256="0" * 64),
            lambda g, c, p: g.update(source_text="IF RUN-FLAG = 7"),
            lambda g, c, p: g.update(copy_instance="another-copy"),
            lambda g, c, p: p["activation_evidence"]["callers"][0].update(scope_known=False)]
        for index, change in enumerate(changes):
            with self.subTest(change=index):
                current_guard, current_call, plan = deepcopy(guard), deepcopy(call), deepcopy(original)
                change(current_guard, current_call, plan)
                result = self.build("BATCH-WORKER怎样trigger？", [current_guard, current_call], plan)
                self.assertIn('CALL "BATCH-WORKER"', result["answer"])
                self.assertIn("控制条件尚未完整核对", result["answer"])
                self.assertNotEqual(result["facts"][0]["conditions_status"], "supplied_source_scope")
                self.assertNotIn("RUN-FLAG = 7", result["answer"])
                self.assertEqual(result["manifest"]["source_ids"], ["ev_call"])

    def test_stale_scope_cannot_select_another_call_occurrence_in_a_bound_caller(self):
        guard, call, plan = self.activation_fragments()
        text = ('PROCEDURE DIVISION.\nMAIN.\nIF RUN-FLAG = 1\nCALL "BATCH-WORKER"\nEND-IF.\n'
                'IF RUN-FLAG = 2\nCALL "BATCH-WORKER"\nEND-IF.\nGOBACK.')
        caller = page(text, identifier="ev_call", path="launcher.cbl")
        bound = plan["activation_evidence"]["callers"][0]
        bound["source_sha256"] = caller["source_sha256"]
        bound["conditions"][0]["evidence_ids"] = ["ev_call"]
        plan["source_scope"] = {"ranges": [{"relative_path": "launcher.cbl", "source_sha256": "0" * 64,
            "start_line": 7, "end_line": 7, "exclusive": True}]}
        result = self.build("BATCH-WORKER怎样trigger？", [caller], plan)
        self.assertEqual([fact["start_line"] for fact in result["facts"]], [4])
        self.assertIn("IF RUN-FLAG = 1", result["answer"])
        self.assertNotIn("IF RUN-FLAG = 2", result["answer"])

    def test_bound_caller_admission_cannot_supply_unrelated_formulas_or_calls(self):
        guard, call, plan = self.activation_fragments()
        caller = page('PROCEDURE DIVISION.\nMAIN.\nIF RUN-FLAG = 1\nCALL "BATCH-WORKER"\nEND-IF.\n'
                      'COMPUTE OUT-AMOUNT = IN-AMOUNT * 999.\nCALL "OTHER-WORKER".\nGOBACK.',
                      identifier="ev_call", path="launcher.cbl")
        bound = plan["activation_evidence"]["callers"][0]
        bound["source_sha256"] = caller["source_sha256"]
        bound["conditions"][0]["evidence_ids"] = ["ev_call"]
        plan["analysis_plan"]["intents"].append("calculation")
        plan["analysis_plan"]["entities"].append("OUT-AMOUNT")
        result = self.build("BATCH-WORKER怎样trigger，OUT-AMOUNT如何计算？", [caller], plan)
        self.assertEqual([fact["predicate"] for fact in result["facts"]], ["call_statement"])
        self.assertNotIn("999", result["answer"])
        self.assertNotIn("OTHER-WORKER", result["answer"])

    def test_scope_flags_do_not_hide_unsupplied_gap_or_else_branch(self):
        guard, call, plan = self.activation_fragments()
        call.update(start_line=6, end_line=6)
        plan["activation_evidence"]["callers"][0]["line"] = 6
        result = self.build("BATCH-WORKER怎样trigger？", [guard, call], plan)
        self.assertIn("控制条件尚未完整核对", result["answer"])
        self.assertNotIn("在片段中可见的控制条件", result["answer"])

    def test_fragmented_else_condition_keeps_its_false_outcome(self):
        guard, call, plan = self.activation_fragments()
        text = 'IF RUN-FLAG = 1\nCONTINUE\nELSE\nCALL "BATCH-WORKER"'
        source = {**page(text, identifier="ev_branch", path="launcher.cbl", first=3), "source_sha256": call["source_sha256"]}
        bound = plan["activation_evidence"]["callers"][0]
        bound.update(line=6, evidence_ids=["ev_branch"])
        bound["conditions"][0].update(outcome=False, evidence_ids=["ev_branch"])
        result = self.build("BATCH-WORKER怎样trigger？", [source], plan)
        self.assertIn("条件不成立：IF RUN-FLAG = 1", result["answer"])
        self.assertEqual(result["facts"][0]["conditions_status"], "supplied_source_scope")

    def test_perform_section_and_literal_words_do_not_become_calls(self):
        source = page('PROCEDURE DIVISION.\nMAIN.\nDISPLAY "CALL FAKE-TARGET PERFORM FAKE-SECTION"\nPERFORM ACTIVE-SECTION.\nGOBACK.\nACTIVE-SECTION SECTION.\nCONTINUE.')
        result = self.build("ACTIVE-SECTION 怎样调用？", [source])
        self.assertIn("PERFORM ACTIVE-SECTION", result["answer"])
        self.assertIsNone(self.build("FAKE-TARGET 怎样调用？", [source]))
        self.assertIsNone(self.build("FAKE-SECTION 怎样调用？", [source]))

    def test_literal_comment_marker_and_terminators_use_shared_facts(self):
        result = self.build("LABEL-TEXT 怎么赋值？", [page('MOVE "A *> B" TO LABEL-TEXT.\nCOMPUTE OUT-AMOUNT = 1 END-COMPUTE.')])
        self.assertIn('LABEL-TEXT = "A *> B"', result["answer"])
        self.assertNotIn("END-COMPUTE", result["answer"])

    def test_same_physical_line_has_distinct_occurrences(self):
        result = self.build("OUT-AMOUNT 怎样赋值？", [page("PROCEDURE DIVISION.\nMAIN.\nMOVE 1 TO OUT-AMOUNT MOVE 2 TO OUT-AMOUNT.\nGOBACK.")])
        self.assertEqual(len(result["facts"]), 2)
        self.assertEqual(len({fact["occurrence_id"] for fact in result["facts"]}), 2)

    def test_unknown_compilation_conditions_remain_candidates(self):
        text = "PROCEDURE DIVISION.\nMAIN.\n>>IF FEATURE-A\nMOVE 1 TO OUT-AMOUNT.\n>>ELSE\nMOVE 2 TO OUT-AMOUNT.\n>>END-IF\nGOBACK."
        result = self.build("OUT-AMOUNT 怎样赋值？", [page(text)])
        self.assertIn("编译条件尚未确认", result["answer"])
        self.assertEqual(len(result["facts"]), 2)
        known = self.build("OUT-AMOUNT 怎样赋值？", [page(text)], compilation_context={"compile_conditions": {"FEATURE-A": True}})
        self.assertIn("OUT-AMOUNT = 1", known["answer"])
        self.assertNotIn("OUT-AMOUNT = 2", known["answer"])

    def test_original_file_hash_is_retained_not_replaced_with_excerpt_hash(self):
        source = {**page("MOVE 1 TO OUT-AMOUNT.", first=80), "source_sha256": "a" * 64}
        result = self.build("OUT-AMOUNT 的赋值？", [source])
        self.assertEqual(result["citations"][0]["source_sha256"], "a" * 64)
        self.assertNotEqual(result["source_versions"][0]["excerpt_sha256"], "a" * 64)
        self.assertIn("rule.cbl:80", result["answer"])

    def test_conflicting_evidence_ids_and_incomplete_pages_cannot_support_answer(self):
        source = page("MOVE 1 TO OUT-AMOUNT.")
        conflict = page("MOVE 2 TO OUT-AMOUNT.")
        self.assertIsNone(self.build("OUT-AMOUNT 的赋值？", [source, conflict]))
        for invalid in ({**source, "source_sha256": "bad"}, {**source, "span_truncated": True},
                {**source, "end_line": 9}, {**source, "evidence_id": "unprovided"}):
            self.assertIsNone(self.build("OUT-AMOUNT 的赋值？", [invalid]))

    def test_outside_plan_paths_never_supply_fallback_answer(self):
        self.assertIsNone(self.build("OUT-AMOUNT 的赋值？", [page("MOVE 1 TO OUT-AMOUNT.")],
            {"analysis_plan": {"paths": ["selected.cbl"]}}))

    def test_no_files_or_network_are_read_when_rendering_captured_evidence(self):
        source = page("MOVE 1 TO OUT-AMOUNT.")
        with mock.patch("builtins.open", side_effect=AssertionError("Unexpected file read")), \
                mock.patch("socket.create_connection", side_effect=AssertionError("Unexpected network")):
            result = self.build("OUT-AMOUNT 的赋值？", [source])
        self.assertIn("OUT-AMOUNT = 1", result["answer"])


if __name__ == "__main__":
    unittest.main()
