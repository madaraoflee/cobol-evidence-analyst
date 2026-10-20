"""A program category does not add an unasked launch investigation."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from question_planner import QuestionPlanner, runtime_activation_requested


class ActivationQuestionScopeTests(unittest.TestCase):
    def plan(self, question):
        return QuestionPlanner().plan(question, {
            "direct_paths": ["service.cbl"], "selected_paths": ["service.cbl"],
            "programs": [{"program_names": ["SERVICE"]}],
            "rule_leads": [{"reads": ["INPUT-AMOUNT"], "writes": ["OUTPUT-AMOUNT"]}]})

    def test_batch_noun_in_local_boundary_or_calculation_question_adds_no_launch_obligations(self):
        for question in (
            "这批处理遇到空输入会怎样？",
            "批处理的输出文件写失败时怎样处理？",
            "批处理中的 INPUT-AMOUNT 大于零是否等于非零？",
            "这个 batch 的 OUTPUT-AMOUNT 怎么计算？",
            "继续核对业务边界：这批处理遇空输入会怎样？SERVICE 任何异常都返回90吗？",
            "What happens when this batch receives empty input?",
            "Can this batch return an error after a failed file write?",
            "How does the batch calculate OUTPUT-AMOUNT?",
        ):
            with self.subTest(question=question):
                plan = self.plan(question)
                self.assertNotIn("activation", plan.intents)
                self.assertFalse({"entry_point", "caller_invocation", "activation_conditions",
                                  "scheduler_binding"}.intersection(plan.required_obligations))

    def test_overview_and_explicit_entry_still_request_static_activation_evidence(self):
        for question in (
            "请从业务目的解释 SERVICE 的整体流程，包括 Batch job、输入输出和异常。",
            "Explain the business purpose and workflow of this batch.",
            "批处理入口和 OUTPUT-AMOUNT 公式是什么？",
            "Explain the batch entry and OUTPUT-AMOUNT formula.",
            "批处理入口 COPY 的完整计算公式是什么？",
        ):
            with self.subTest(question=question):
                plan = self.plan(question)
                self.assertIn("activation", plan.intents)
                self.assertIn("caller_invocation", plan.required_obligations)
                self.assertIn("scheduler_binding", plan.required_obligations)

    def test_explicit_activation_survives_combined_local_questions(self):
        for question in (
            "SERVICE 批处理怎么trigger的，OUTPUT-AMOUNT怎么计算？",
            "SERVICE如何启动，空输入如何处理？",
            "谁启动这批处理，OUTPUT-AMOUNT 是什么？",
            "这个batch如何调度，写失败如何处理？",
            "How is the batch triggered and what happens on empty input?",
            "How is SERVICE triggered and how is OUTPUT-AMOUNT computed?",
            "Who launches this batch?",
            "At what time does this batch run?",
            "什么时候执行这项批处理？",
            "谁启动这项批处理？",
            "这批处理多久运行一次？",
            "How often does this batch run?",
            "Who launches SERVICE?",
            "什么时候执行 SERVICE？",
        ):
            with self.subTest(question=question):
                self.assertIn("activation", self.plan(question).intents)

    def test_rule_local_trigger_and_incoming_program_relation_keep_existing_scope(self):
        local = self.plan("SERVICE 出账触发条件和金额如何计算？")
        self.assertNotIn("activation", local.intents)
        self.assertIn("control_conditions", local.required_obligations)
        incoming = self.plan("哪些程序调用 SERVICE？")
        self.assertEqual(incoming.relation_direction, "incoming")
        self.assertIn("call_relationships", incoming.intents)
        self.assertNotIn("scheduler_binding", incoming.required_obligations)

    def test_explicit_initiator_question_is_shared_with_delivery_scope(self):
        for question in (
            "请介绍 SERVICE 这项批处理的业务用途、输入输出，并确认实际发起者是谁。",
            "Explain the business purpose of this batch and who initiates it.",
        ):
            with self.subTest(question=question):
                self.assertTrue(runtime_activation_requested(question))
                plan = self.plan(question)
                self.assertIn("activation", plan.intents)
                self.assertIn("scheduler_binding", plan.required_obligations)
        self.assertFalse(runtime_activation_requested("批处理输入记录里的发起者字段是什么意思？"))


if __name__ == "__main__":
    unittest.main()
