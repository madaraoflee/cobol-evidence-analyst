"""Offline intent/continuation checks, not natural-model answer quality scores."""

import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_synthesis import assess_answer_completion, build_analysis_brief, wants_business_overview
from question_planner import QuestionPlanner, business_concept_requested


SOURCES = {
    "illustration.cbl": """IDENTIFICATION DIVISION.
PROGRAM-ID. ILLUSTRATION.
*> BUSINESS PURPOSE: contract illustration for an active contract.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 CONTRACT-AMOUNT PIC 9(6) VALUE 1000.
01 PROJECTED-AMOUNT PIC 9(8)V99.
PROCEDURE DIVISION.
CALL 'PROJECTOR' USING CONTRACT-AMOUNT PROJECTED-AMOUNT.
DISPLAY PROJECTED-AMOUNT.
GOBACK.
""",
    "projector.cbl": """IDENTIFICATION DIVISION.
PROGRAM-ID. PROJECTOR.
DATA DIVISION.
LINKAGE SECTION.
01 INPUT-AMOUNT PIC 9(6).
01 OUTPUT-AMOUNT PIC 9(8)V99.
PROCEDURE DIVISION USING INPUT-AMOUNT OUTPUT-AMOUNT.
COMPUTE OUTPUT-AMOUNT = INPUT-AMOUNT * 1.05.
GOBACK.
""",
}
PAGES = [{"relative_path": path, "source_text": text, "evidence_id": "ev:" + path,
          "source_sha256": hashlib.sha256(text.encode()).hexdigest(), "start_line": 1,
          "end_line": len(text.splitlines()), "format_hint": "free"} for path, text in SOURCES.items()]
MAPPING = {"direct_paths": ["illustration.cbl"], "selected_paths": list(SOURCES),
           "programs": [{"relative_path": "illustration.cbl", "program_names": ["ILLUSTRATION"]}],
           "rule_leads": [{"reads": ["CONTRACT-AMOUNT"], "writes": ["PROJECTED-AMOUNT"]}]}


class BusinessConceptQuestionTests(unittest.TestCase):
    def test_natural_concepts_select_business_investigation_without_domain_dictionary(self):
        for question in ("什么是inforce illustration", "解释一下contract illustration",
                         "解释一下 premium refund 以及 inforce illustration",
                         "請解釋一下保單試算", "什麼是保單試算？", "保单试算是什么？",
                         "请介绍预约分配", "What is contract illustration?",
                         "Please explain contract illustration.", "Tell me about order settlement."):
            with self.subTest(question=question):
                self.assertTrue(business_concept_requested(question))
                self.assertTrue(wants_business_overview(question))
                plan = QuestionPlanner().plan(question, MAPPING, source_pages=PAGES)
                self.assertIn("business_overview", plan.intents)
                self.assertIn("trace_parameter", plan.tools)
                self.assertIn("inspect_business_context", plan.tools)
                self.assertTrue({"calls", "control_conditions", "external_effects"}
                                <= set(plan.required_obligations))
                self.assertNotIn("scheduler_binding", plan.required_obligations)

    def test_program_name_is_explained_but_bound_field_stays_narrow(self):
        program = QuestionPlanner().plan("解释一下 ILLUSTRATION", MAPPING)
        self.assertIn("business_overview", program.intents)
        question = "什么是 PROJECTED-AMOUNT？"
        plan = QuestionPlanner().plan(question, MAPPING)
        self.assertEqual(plan.entities, ("PROJECTED-AMOUNT",))
        self.assertEqual(plan.intents, ("origin",))
        investigation = {"state": "located", "analysis_plan": plan.to_dict()}
        brief = build_analysis_brief(question, investigation, PAGES, [], 2048)
        self.assertEqual(brief["business_explanation_contract"]["mode"], "direct_answer")
        self.assertNotIn("business_orientation", brief)

    def test_bound_procedure_stays_focused_while_nonexclusive_scope_can_explain_program(self):
        question = "解释一下 DISPATCH-STEP"
        scope = {"procedure_name": "DISPATCH-STEP", "relative_path": "illustration.cbl",
                 "exclusive": True}
        plan = QuestionPlanner().plan(question, MAPPING, source_scopes=[scope])
        self.assertEqual(plan.intents, ("explanation",))
        brief = build_analysis_brief(question, {"state": "located", "analysis_plan": plan.to_dict()},
                                     PAGES, [], 2048, business_map=MAPPING)
        self.assertEqual(brief["business_explanation_contract"]["mode"], "direct_answer")
        self.assertNotIn("business_orientation", brief)
        whole_program = QuestionPlanner().plan("解释一下整个 ILLUSTRATION，包括 DISPATCH-STEP", MAPPING,
            source_scopes=[{**scope, "exclusive": False}])
        self.assertIn("business_overview", whole_program.intents)

    def test_narrow_questions_do_not_expand_to_business_overviews(self):
        for question in ("什么是 CONTRACT-AMOUNT 字段？", "解释一下 PROJECTED-AMOUNT 的类型。",
                         "解释一下 contract illustration 的返回值。", "只解释订单的输出。",
                         "解释一下 contract illustration 应该翻译成什么。", "什么是 PIC？",
                         "What is the data type of PROJECTED-AMOUNT?", "Explain the input to PROJECTOR.",
                         "Explain which file this batch program reads.", "What is the error code?",
                         "请解释批处理程序读文件失败会怎样。", "出 bill 的逻辑是什么？",
                         "NATURALFLOW 的金额规则是什么？", "EOFFLOW 的最终金额计算规则是什么？",
                         "解释一下这一行代码。", "Explain this statement.", "What is the value of AMOUNT?",
                         "Translate contract illustration into Chinese.", "What is COMP-3?",
                         "解释一下 CALL", "What is EVALUATE?", "What is EXEC SQL?"):
            with self.subTest(question=question):
                self.assertFalse(business_concept_requested(question))
                self.assertFalse(wants_business_overview(question))
                self.assertNotIn("business_overview", QuestionPlanner().plan(question, MAPPING).intents)

    def test_program_kind_does_not_require_actual_scheduler(self):
        for question in ("什么是批处理结算", "解释一下批处理结算程序", "What is a batch settlement job?"):
            with self.subTest(question=question):
                plan = QuestionPlanner().plan(question, MAPPING)
                self.assertIn("business_overview", plan.intents)
                self.assertNotIn("activation", plan.intents)
                self.assertNotIn("scheduler_binding", plan.required_obligations)
        plan = QuestionPlanner().plan("批处理结算程序何时启动？", MAPPING)
        self.assertIn("activation", plan.intents)

    def test_manual_definition_does_not_require_program_implementation(self):
        for question in ("框架手册中 DEFERRED-SETTLEMENT 的含义是什么？",
                         "手冊中延遲結算的定義是什麼？",
                         "What is deferred settlement in the framework manual?",
                         "Explain the meaning of deferred settlement according to the manual."):
            with self.subTest(question=question):
                self.assertFalse(business_concept_requested(question))
                self.assertFalse(wants_business_overview(question))
                self.assertEqual(QuestionPlanner().plan(question, {}).intents, ("explanation",))
        self.assertTrue(wants_business_overview("请结合手册解释延迟结算在源码中的业务流程。"))

    def test_concept_brief_only_adds_supplied_relationships(self):
        question = "什么是contract illustration"
        plan = QuestionPlanner().plan(question, MAPPING, source_pages=PAGES)
        brief = build_analysis_brief(question, {"state": "located", "analysis_plan": plan.to_dict()},
                                     PAGES, [], 2048, business_map=MAPPING)
        self.assertEqual(brief["business_explanation_contract"]["mode"], "business_overview")
        requirements = {row["kind"] for row in brief["required_answer_aspects"]}
        self.assertIn("program_roles", requirements)
        self.assertNotIn("data_roles", requirements)
        self.assertFalse(brief["semantic_execution_verified"])
        self.assertFalse(wants_business_overview(question, answer_detail="brief"))
        empty = build_analysis_brief(question, {"state": "unresolved"}, [], [], 2048)
        self.assertEqual(empty["required_answer_aspects"], [])


class DeferredSourceExplanationTests(unittest.TestCase):
    def test_actual_external_boundary_does_not_hide_other_unfinished_source_work(self):
        working_set = {"frontier": [{"reason": "external_implementation_unavailable",
                                     "target_name": "EXTERNAL-ENGINE"}]}
        local_limit = ("主程序把合同金额交给 PROJECTOR，得到投影金额后展示结果。"
                       "若要确认外部引擎内部算法，还需要查看未提供的外部源码。")
        result = assess_answer_completion(local_limit, source_pages=PAGES, working_set=working_set)
        self.assertFalse(result["pending_investigation"])
        self.assertTrue(assess_answer_completion(local_limit)["pending_investigation"])
        for unfinished in (
            "还需要查看主流程实现。",
            "还需要查看未提供的外部 PROJECTOR 实现。",
            "还需要查看未提供的外部 `PROJECTOR` 实现。",
            "还需要查看未提供的外部 'PROJECTOR' 实现。",
            "还需要查看未提供的外部 other-engine 实现。",
        ):
            with self.subTest(unfinished=unfinished):
                result = assess_answer_completion(local_limit + unfinished,
                                                   source_pages=PAGES, working_set=working_set)
                self.assertTrue(result["pending_source_explanation"])

    def test_external_limit_alone_still_requires_explanation_of_supplied_source(self):
        working_set = {"frontier": [{"reason": "external_implementation_unavailable",
                                     "target_name": "EXTERNAL-ENGINE"}]}
        for answer in (
            "若要解释这项业务，还需要查看未提供的外部源码。",
            "目前无法回答。若要解释这项业务，还需要查看未提供的外部源码。",
            "若要解釋這項業務，還需要查看未提供的外部源碼。",
            "To explain this process, we need to inspect unavailable external source.",
        ):
            with self.subTest(answer=answer):
                result = assess_answer_completion(answer, source_pages=PAGES, working_set=working_set)
                self.assertEqual(result["status"], "incomplete")
                self.assertTrue(result["pending_source_explanation"])

    def test_located_comment_followed_by_required_reading_is_unfinished(self):
        answer = ("源码注释把 ILLUSTRATION 定位为 contract illustration，相关文件是 illustration.cbl。"
                  "若进一步说明具体演示什么、输入输出如何串起来，还需要查看主流程与投影子程序实现。")
        result = assess_answer_completion(answer)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["reason"], "source_explanation_deferred")
        self.assertTrue(result["pending_investigation"])
        self.assertTrue(result["pending_source_explanation"])

    def test_source_work_is_recognized_across_supported_languages(self):
        for answer in ("还需要查看主流程与子程序实现。", "仍需閱讀主程式與子程式實現。",
                       "若進一步說明輸入輸出，還需要查看主程式實現。",
                       "To explain the inputs and outputs, we still need to inspect the main flow and callee implementation.",
                       "The comment names a contract illustration. I still need to read the source implementation."):
            with self.subTest(answer=answer):
                result = assess_answer_completion(answer)
                self.assertTrue(result["pending_source_explanation"])
                self.assertTrue(result["pending_investigation"])

    def test_optional_offers_runtime_boundaries_and_program_behavior_do_not_request_retry(self):
        for answer in ("如需更详细的代码说明，还可以查看子程序实现。",
                       "如果你还需要了解更多，我可以查看子程序实现。",
                       "已从实现确认金额计算并返回结果，如果需要更多细节，还需要查看主程序实现。",
                       "要解释实际执行时点，还需要查看运行日志。",
                       "金额已按条件计算；需要查看生产配置才能确认实际调度。",
                       "To confirm the production run, we need to inspect runtime logs.",
                       "If you need more detail, I can inspect the source implementation.",
                       "不需要查看主流程就能从已供应的完整实现说明金额计算。",
                       "程序需要读取当前订单数据才能继续。",
                       "程序输出“还需要查看子程序实现”并返回。",
                       "说明文本：\n```text\n还需要查看子程序实现。\n```",
                       "字段保存提示信息。\n> 还需要查看子程序实现。",
                       "按输入金额乘以 1.05 得到试算金额并显示；实际投保金额不由本程序决定。"):
            with self.subTest(answer=answer):
                result = assess_answer_completion(answer)
                self.assertFalse(result["pending_source_explanation"])
                self.assertFalse(result["pending_investigation"])


if __name__ == "__main__":
    unittest.main()
