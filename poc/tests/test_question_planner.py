from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from question_planner import QuestionPlanner


class QuestionPlannerTests(unittest.TestCase):
    def setUp(self):
        self.map = {"direct_paths": ["entry.cbl"], "selected_paths": ["entry.cbl"],
                    "rule_leads": [{"reads": ["IN-AMOUNT"], "writes": ["OUT-AMOUNT"]}]}

    def test_natural_questions_have_explicit_tools_and_obligations(self):
        cases = (("这个字段从哪里来的？", "origin", "backward_slice"),
                 ("为什么最后变成零？", "overwrite", "explain_assignment"),
                 ("前面的计算为什么没有生效？", "overwrite", "backward_slice"),
                 ("修改这里会影响哪些结果？", "impact", "forward_slice"),
                 ("刚才第二种情况呢？", "condition", "backward_slice"))
        history = [{"investigation_state": {"analysis_plan": {"entities": ["OUT-AMOUNT"]}}}]
        for question, intent, tool in cases:
            with self.subTest(question=question):
                plan = QuestionPlanner().plan(question, self.map, history=history)
                self.assertIn(intent, plan.intents)
                self.assertIn(tool, plan.tools)
                self.assertIn("OUT-AMOUNT", plan.entities)
                self.assertTrue(plan.required_obligations)

    def test_source_fact_selects_depth_without_trigger_word(self):
        plan = QuestionPlanner().plan("OUT-AMOUNT？", self.map)
        self.assertEqual(plan.intents, ("origin",))
        self.assertIn("backward_slice", plan.tools)

    def test_chinese_question_keeps_adjacent_field_identity(self):
        plan = QuestionPlanner().plan("修改OUT-AMOUNT会影响哪些结果？", self.map)
        self.assertEqual(plan.entities, ("OUT-AMOUNT",))
        self.assertIn("forward_slice", plan.tools)

    def test_model_cannot_expand_scope_or_invent_entities(self):
        plan = QuestionPlanner().plan("说明", self.map,
            candidate={"entities": ["MADE-UP"], "paths": ["../outside"]})
        self.assertNotIn("MADE-UP", plan.entities)
        self.assertNotIn("../outside", plan.paths)
        self.assertTrue(plan.required_obligations)

    def test_unresolved_reference_stays_explicit(self):
        plan = QuestionPlanner().plan("刚才第二种情况呢？", {})
        self.assertIn("conversation_entity_not_resolved", plan.unresolved)

    def test_assistant_prose_cannot_establish_field_referent(self):
        plan = QuestionPlanner().plan("这个字段从哪里来的？", self.map,
            history=[{"role": "assistant", "content": "OUT-AMOUNT 是第二种情况。"}])
        self.assertEqual(plan.entities, ())
        self.assertIn("conversation_entity_not_resolved", plan.unresolved)

    def test_local_subject_survives_missing_history_and_selects_exact_ordinal(self):
        mapped = {**self.map, "session_subject": {"status": "resolved", "focus_entities": ["OUT-AMOUNT"]}}
        plan = QuestionPlanner().plan("刚才第二种情况呢？也会归零吗？", mapped)
        self.assertEqual(plan.entities, ("OUT-AMOUNT",))
        self.assertIn("later_writes", plan.required_obligations)

    def test_explicit_source_switch_cannot_reuse_old_plan_entity(self):
        mapped = {**self.map, "source_identity": {"status": "resolved", "direct_paths": ["other.cbl"]}}
        plan = QuestionPlanner().plan("other.cbl 的这个字段呢？", mapped,
            history=[{"investigation_state": {"analysis_plan": {"entities": ["OUT-AMOUNT"]}}}])
        self.assertEqual(plan.entities, ())
