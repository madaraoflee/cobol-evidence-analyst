"""Processing order needs business evidence, not only assignment details."""

import hashlib
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from business_synthesis import (assess_business_answer, build_analysis_brief,
                                business_explanation_excluded_facets, business_explanation_focus,
                                wants_business_overview)
from company_api import APIClientError, CompanyAPIConfig, TransportResponse
from question_intent import business_sequence_requested
from question_planner import QuestionPlanner
from repository_discovery import ensure_repository_search
from test_business_explanation_intent import EXPLANATION, INVENTORY, SOURCE, page


QUESTION = ("关于 COLLECT-FLOW，他有个收取的顺序，多种余额是怎么样的 sequency "
            "来扣减应收款的？怎么定义怎么写的")


class BusinessSequenceIntentTests(unittest.TestCase):
    def test_processing_order_is_not_limited_to_overview_keywords(self):
        for question in (
                QUESTION,
                "预存款、待分配款和退款按什么顺序抵扣？每项适用条件如何定义？",
                "預存款、待分配款和退款按什麼順序抵扣？每項適用條件如何定義？",
                "How are the different balances applied and in what order?",
                "What order are the different balances applied in?",
                "Describe the priority of applying different balances."):
            with self.subTest(question=question):
                self.assertTrue(wants_business_overview(question))
                plan = QuestionPlanner().plan(question, {"direct_paths": ["collect.cbl"],
                    "source_identity": {"status": "resolved", "kind": "program",
                                        "direct_paths": ["collect.cbl"]}})
                self.assertIn("flow", plan.intents)
                self.assertIn("business_overview", plan.intents)
                self.assertIn("inspect_business_context", plan.tools)
                self.assertTrue({"control_conditions", "calls", "external_effects"}
                                <= set(plan.required_obligations))

    def test_identifiers_sorting_layout_and_single_formula_remain_focused(self):
        for question in (
                "SEQUENCE-NO 字段取值是什么？", "如何使用优先级字段？",
                "SEQUENCE 函数如何使用？", "How do I use a sequence generator?",
                "Which field controls the sort order?", "按哪个排序键升序处理记录？",
                "如何使用 ORDER BY 处理行排序？", "字段在输出中的排列顺序如何定义？",
                "DUE-AMOUNT 的公式按什么顺序扣减余额？",
                "What is the value of SEQUENCE-NO when a payment is processed?"):
            with self.subTest(question=question):
                self.assertFalse(business_sequence_requested(question))
                self.assertFalse(wants_business_overview(question))
        self.assertFalse(business_sequence_requested("How is SEQUENCE used?", field_entities=["SEQUENCE"]))

    def test_current_scope_and_presentation_limits_still_apply(self):
        self.assertFalse(wants_business_overview("请简短回答各项余额的扣减顺序。"))
        self.assertFalse(wants_business_overview("逐行解释程序按什么顺序处理数据。"))
        self.assertFalse(wants_business_overview(QUESTION, source_scopes=[{
            "relative_path": "collect.cbl", "start_line": 1, "end_line": 10, "exclusive": True}]))
        # A matched business amount does not make an otherwise broad sequence
        # question a declaration lookup.
        self.assertTrue(wants_business_overview(QUESTION, field_entities=["DUE-AMOUNT"]))

    def test_negated_or_translated_order_text_does_not_expand_investigation(self):
        for question in (
                "无需说明处理顺序，只告诉我结果是否为零。",
                "Do not explain the processing sequence; where is the input file?",
                "请翻译这句话：What order are the balances applied in?",
                'Translate "What order are the balances applied in?" into Chinese.'):
            with self.subTest(question=question):
                self.assertFalse(business_sequence_requested(question))
                self.assertFalse(wants_business_overview(question))
        self.assertTrue(wants_business_overview("不要只讲赋值，请按处理顺序解释各步骤。"))
        self.assertTrue(wants_business_overview("请按处理顺序解释各步骤，不要翻译代码。"))

    def test_sequence_with_multiple_step_formulas_keeps_business_scope(self):
        question = "各项余额按什么顺序抵扣？请列出每步的公式和剩余应收款。"
        self.assertTrue(wants_business_overview(question))
        plan = QuestionPlanner().plan(question, {"direct_paths": ["collect.cbl"]})
        self.assertTrue({"flow", "calculation", "business_overview"} <= set(plan.intents))

    def test_excluding_field_details_does_not_exclude_the_requested_business_sequence(self):
        for question in (
                "请按处理顺序解释各项抵扣，不需要说明字段初值和类型。",
                "請按處理順序解釋各項抵扣，不需要說明欄位初值和類型。",
                "What order are offsets applied? Do not explain the value of individual fields.",
                "Do not explain field types. What order are offsets applied?"):
            with self.subTest(question=question):
                self.assertTrue(business_sequence_requested(question))
                self.assertTrue(wants_business_overview(question))
                self.assertEqual(business_explanation_focus(question), "processing_sequence")
        self.assertFalse(business_sequence_requested("What order is this field processed in? Explain its type and value."))

    def test_priority_nouns_are_not_processing_precedence_requests(self):
        for question in ("优先股分配业务是如何处理的？", "優先股分配業務是如何處理的？",
                         "优先权相关业务如何处理？"):
            with self.subTest(question=question):
                self.assertFalse(business_sequence_requested(question))
                self.assertIsNone(business_explanation_focus(question))
        self.assertTrue(business_sequence_requested("各项余额优先扣减哪一项？"))
        self.assertTrue(business_sequence_requested("各項餘額按什麼優先級抵扣？"))

    def test_explicit_overview_and_sequence_requests_retain_both_source_bound_aspects(self):
        for question in (
                "请详细说明整体业务用途、批处理和数据库关系，并说明各项余额扣减顺序。",
                "請詳細說明整體業務用途、批次和資料庫關係，並說明各項餘額扣減順序。",
                "Explain the business purpose, batch and database relationships, and the order of applying balances.",
                "不要逐行解释代码；请说明业务用途和各项抵扣顺序。",
                "Do not explain code line by line; explain the business purpose and the order of applying balances."):
            with self.subTest(question=question):
                self.assertEqual(business_explanation_focus(question), "processing_sequence_with_overview")
                brief = build_analysis_brief(question, {}, [page()], [], 4096)
                contract = brief["business_explanation_contract"]
                self.assertEqual(contract["focus"], "processing_sequence_with_overview")
                self.assertIn("两部分都必须覆盖", contract["task"])
                self.assertNotIn("只围绕用户所问的处理顺序", contract["task"])
                requirements = brief["required_answer_aspects"]
                self.assertTrue({"processing_sequence", "business_purpose", "program_roles", "data_roles"}
                                <= {row["kind"] for row in requirements})
                self.assertTrue(all(row["supplied_reference_ids"] == ["ev_balance"] for row in requirements))
        self.assertEqual(business_explanation_focus(
            "请解释各项余额扣减顺序，不需要说明批处理和数据库关系。"), "processing_sequence")

    def test_declining_unrelated_overview_retains_explicit_sequence_and_current_limits(self):
        question = "请解释各项余额扣减顺序，不需要说明整体业务用途、批处理和数据库关系。"
        for text in (question,
                     "請解釋各項餘額扣減順序，不需要說明整體業務用途。",
                     "What order are offsets applied? Do not explain the overall business purpose."):
            with self.subTest(question=text):
                self.assertTrue(wants_business_overview(text))
                self.assertEqual(business_explanation_focus(text), "processing_sequence")
                brief = build_analysis_brief(text, {}, [page()], [], 4096)
                self.assertEqual([row["kind"] for row in brief["required_answer_aspects"]], ["processing_sequence"])
        self.assertFalse(wants_business_overview("请简短回答。" + question))
        self.assertFalse(wants_business_overview("请逐行解释。" + question))
        self.assertFalse(wants_business_overview("只做语法检查。" + question))
        self.assertFalse(wants_business_overview(question, source_scopes=[{
            "relative_path": "collect.cbl", "start_line": 1, "end_line": 10, "exclusive": True}]))

    def test_combined_request_does_not_require_explicitly_excluded_data_roles(self):
        for question in (
                "请说明各项抵扣顺序和业务用途，不要讲数据库或文件。",
                "請說明各項抵扣順序和業務用途，不要講資料庫或檔案。",
                "Explain the business purpose and order of applying offsets. Do not explain databases or files."):
            with self.subTest(question=question):
                self.assertEqual(business_explanation_excluded_facets(question), ["data_roles"])
                brief = build_analysis_brief(question, {}, [page()], [], 4096)
                contract = brief["business_explanation_contract"]
                self.assertEqual(contract["excluded_facets"], ["data_roles"])
                self.assertNotIn("表文件角色", contract["task"])
                self.assertEqual({row["kind"] for row in brief["required_answer_aspects"]},
                                 {"processing_sequence", "business_purpose", "program_roles"})
                review = assess_business_answer(question, INVENTORY, {}, [page()])
                self.assertNotIn("data_roles", review["required_aspects"])
                self.assertNotIn("data_roles", review["missing_aspects"])

    def test_combined_request_does_not_require_explicitly_excluded_purpose(self):
        for question in (
                "不要讲业务用途；请说明批处理和数据库关系及各项抵扣顺序。",
                "不要講業務用途；請說明批次和資料庫關係及各項抵扣順序。",
                "Do not explain business purpose; explain batch and database relationships and the order of applying offsets."):
            with self.subTest(question=question):
                self.assertEqual(business_explanation_excluded_facets(question), ["business_purpose"])
                brief = build_analysis_brief(question, {}, [page()], [], 4096)
                contract = brief["business_explanation_contract"]
                self.assertEqual(contract["excluded_facets"], ["business_purpose"])
                self.assertNotIn("业务用途", contract["task"])
                self.assertEqual({row["kind"] for row in brief["required_answer_aspects"]},
                                 {"processing_sequence", "program_roles", "data_roles"})
        self.assertEqual(business_explanation_excluded_facets(
            "请说明业务用途和各项抵扣顺序，但不需要重复说明业务用途。"), [])

    def test_sequence_question_rejects_an_assignment_only_synopsis(self):
        answer = "程序把 RESULT-AMOUNT 设置为 0，然后返回调用方。"
        result = assess_business_answer("BALANCE-QUERY 的各个步骤按什么顺序执行？",
                                        answer, {}, [page()])
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["missing_aspects"], ["processing_sequence"])
        self.assertEqual(result["required_aspects"], ["processing_sequence"])
        narrow = assess_business_answer("RESULT-AMOUNT 字段取值是什么？", answer, {}, [page()])
        self.assertNotIn("business_purpose", narrow["missing_aspects"])

    def test_sequence_review_does_not_require_unasked_orientation_categories(self):
        brief = build_analysis_brief("BALANCE-QUERY 的各个步骤按什么顺序执行？", {}, [page()], [], 4096)
        contract = brief["business_explanation_contract"]
        self.assertEqual(contract["focus"], "processing_sequence")
        self.assertFalse(contract["semantic_verification"])
        requirements = brief["required_answer_aspects"]
        self.assertEqual([row["kind"] for row in requirements], ["processing_sequence"])
        self.assertEqual(requirements[0]["supplied_reference_ids"], ["ev_balance"])
        self.assertIn("初始化、配置装载和内部表定义只是线索", contract["task"])

    def test_general_overview_does_not_require_unasked_processing_sequence(self):
        brief = build_analysis_brief("请详细说明 BALANCE-QUERY 的整体业务用途。", {}, [page()], [], 4096)
        self.assertNotIn("focus", brief["business_explanation_contract"])
        self.assertTrue(brief["required_answer_aspects"])
        self.assertNotIn("processing_sequence", [row["kind"] for row in brief["required_answer_aspects"]])

    def test_actual_request_reads_dynamic_candidate_and_late_business_condition(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            database = Path(temporary) / "index.sqlite"

            def program(name, data, body):
                return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
                        f"WORKING-STORAGE SECTION.\n{data}\nPROCEDURE DIVISION.\nMAIN.\n{body}\nGOBACK.\n")

            (source / "collect.cbl").write_text(program("COLLECT-FLOW",
                '01 SERVICE-NAME PIC X(16) VALUE "CREDIT-STEP".\n01 DUE-AMOUNT PIC 9(6).',
                "CALL SERVICE-NAME USING DUE-AMOUNT."), encoding="utf-8")
            (source / "credit.cbl").write_text(program("CREDIT-STEP",
                "01 DUE-AMOUNT PIC 9(6).\n01 SAVED-AMOUNT PIC 9(6).",
                "\n".join(["CONTINUE."] * 500) + "\nIF SAVED-AMOUNT > 0\n"
                " SUBTRACT SAVED-AMOUNT FROM DUE-AMOUNT\n"
                ' DISPLAY "reserve is applied before bank debit"\nEND-IF.'), encoding="utf-8")
            build_business_index(source, database, source_format="free", quiet=True,
                                 verify_content=True, framework_reference_path="")
            ensure_repository_search(database, source)

            def capture(question):
                requests = []

                def transport(request):
                    requests.append(json.loads(json.loads(request.body)["messages"][-1]["content"]))
                    raise APIClientError("OFFLINE_CAPTURE_ONLY")

                with patch.object(socket.socket, "connect", side_effect=AssertionError("Offline test")):
                    run_business_chat(question, database, source,
                        CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only"),
                        transport=transport, allow_network=False, framework_reference_path="",
                        policy=AgentPolicy(max_model_requests=1))
                self.assertEqual(len(requests), 1)
                return requests[0]

            payload = capture(QUESTION)
            pages = [row for bundle in payload["source_context"] for row in bundle.get("pages", [])]
            supplied = next(row for row in pages if row["relative_path"] == "credit.cbl")
            self.assertIn("reserve is applied before bank debit", supplied["source_text"])
            self.assertEqual(supplied["source_sha256"], hashlib.sha256((source / "credit.cbl").read_bytes()).hexdigest())
            self.assertEqual(supplied["end_line"], len((source / "credit.cbl").read_text().splitlines()))
            working = payload["source_context"][0]["working_set"]
            self.assertEqual(set(working["candidate_paths"]), {"collect.cbl", "credit.cbl"})
            gap = next(row for row in working["frontier"] if row["reason"] == "runtime_target_unresolved")
            self.assertEqual(gap["candidate_paths"], ["credit.cbl"])
            self.assertFalse(gap["runtime_target_resolved"])
            self.assertFalse(working["closure_complete"])
            narrow = capture("COLLECT-FLOW 中 SERVICE-NAME 字段取值是什么？")
            narrow_paths = {row["relative_path"] for bundle in narrow["source_context"]
                            for row in bundle.get("pages", [])}
            self.assertNotIn("credit.cbl", narrow_paths)

    def test_actual_chat_revises_sequence_synopsis_using_supplied_business_aspects(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            (source / "balance.cbl").write_bytes(SOURCE.encode())
            database = Path(temporary) / "index.sqlite"
            build_business_index(source, database, source_format="free", quiet=True,
                                 verify_content=True, framework_reference_path="")
            ensure_repository_search(database, source)
            requests = []

            def transport(request):
                payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
                requests.append(payload)
                supplied = next(row for bundle in payload["source_context"] for row in bundle["pages"]
                                if row["relative_path"] == "balance.cbl")
                answer = INVENTORY if len(requests) == 1 else EXPLANATION
                answer += f"[{supplied['evidence_id']}]"
                return TransportResponse(200, json.dumps({"choices": [{"message": {
                    "role": "assistant", "content": answer}, "finish_reason": "stop"}]}))

            with patch.object(socket.socket, "connect", side_effect=AssertionError("Offline test")):
                output = run_business_chat("BALANCE-QUERY 的各个步骤按什么顺序执行？请说明职责和数据交接。",
                    database, source,
                    CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only"),
                    transport=transport, allow_network=False, framework_reference_path="",
                    policy=AgentPolicy(max_model_requests=3, max_answer_revisions=1))
            self.assertEqual(len(requests), 2)
            self.assertIn("business_explanation", requests[0]["response_contract"])
            self.assertIn("processing_sequence", requests[1]["answer_review"]["missing_aspects"])
            self.assertFalse({"business_purpose", "program_roles", "data_roles"}
                             & set(requests[1]["answer_review"]["missing_aspects"]))
            self.assertIn("按给定账号查询可用余额", output["agent_result"]["answer"])
            self.assertTrue(output["agent_result"]["business_review"]["synthesis_review_attempted"])

    def test_concept_query_reads_execution_candidate_beyond_initialization_roots(self):
        question = ("关于 payment collection，他有个收取的顺序，比如 deposit, suspense, payment refund, "
                    "benefit 等等诸多因素，他是怎么样的 sequency 来扣减应收款的？怎么定义怎么写的")
        self.assertNotIn("COLLECT-FLOW", question)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            source.mkdir()
            database = Path(temporary) / "index.sqlite"

            def program(name, comment, data, body):
                return (f"*> {comment}\nIDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
                        f"WORKING-STORAGE SECTION.\n{data}\nPROCEDURE DIVISION.\nMAIN.\n{body}\nGOBACK.\n")

            for number in range(7):
                (source / f"a-init-{number}.cbl").write_text(program(f"INIT-{number}",
                    "Payment collection initializes deposit suspense payment refund benefit configuration.",
                    "01 CONFIG-FLAG PIC 9.", "MOVE 0 TO CONFIG-FLAG."), encoding="utf-8")
            (source / "z-collect.cbl").write_text(program("COLLECT-FLOW",
                "Payment collection applies deposit suspense payment refund benefit in business sequence.",
                '01 SERVICE-NAME PIC X(16) VALUE "CREDIT-STEP".\n01 DUE-AMOUNT PIC 9(6).',
                "CALL SERVICE-NAME USING DUE-AMOUNT."), encoding="utf-8")
            (source / "credit.cbl").write_text(program("CREDIT-STEP",
                "Determine eligible reserve before remaining debit.",
                "01 DUE-AMOUNT PIC 9(6).\n01 SAVED-AMOUNT PIC 9(6).",
                "\n".join(["CONTINUE."] * 500) + "\nIF SAVED-AMOUNT > 0\n"
                " SUBTRACT SAVED-AMOUNT FROM DUE-AMOUNT\n"
                ' DISPLAY "reserve is applied before bank debit"\nEND-IF.'), encoding="utf-8")
            build_business_index(source, database, source_format="free", quiet=True,
                                 verify_content=True, framework_reference_path="")
            ensure_repository_search(database, source)
            requests = []

            def transport(request):
                requests.append(json.loads(json.loads(request.body)["messages"][-1]["content"]))
                raise APIClientError("OFFLINE_CAPTURE_ONLY")

            with patch.object(socket.socket, "connect", side_effect=AssertionError("Offline test")):
                run_business_chat(question, database, source,
                    CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only"),
                    transport=transport, allow_network=False, framework_reference_path="",
                    policy=AgentPolicy(max_model_requests=1))
            self.assertEqual(len(requests), 1)
            payload = requests[0]
            self.assertEqual(payload["business_map"]["source_identity"]["status"], "none")
            pages = [row for bundle in payload["source_context"] for row in bundle.get("pages", [])]
            implementations = [row for row in pages if row["relative_path"] == "credit.cbl"]
            self.assertTrue(implementations, "Initializers must not displace all execution candidates")
            self.assertTrue(any("reserve is applied before bank debit" in row["source_text"]
                                for row in implementations))
            self.assertTrue(all(row["source_sha256"] == hashlib.sha256((source / "credit.cbl").read_bytes()).hexdigest()
                                for row in implementations))
            working = payload["source_context"][0]["working_set"]
            self.assertEqual(working["root_candidate_count"], 8)
            self.assertEqual(working["root_candidate_budget"], AgentPolicy().initial_pages)
            self.assertEqual(len(working["root_paths"]), AgentPolicy().initial_pages)
            self.assertIn("z-collect.cbl", working["root_paths"])
            self.assertEqual(working["omitted_candidate_path_count"], 2)
            self.assertEqual(set(working["omitted_candidate_paths"]),
                {row["relative_path"] for row in working["frontier"] if row["reason"] == "concept_root_budget"})
            self.assertLessEqual(len(working["candidate_paths"]), AgentPolicy().max_semantic_files)
            self.assertFalse(working["semantic_execution_verified"])
            self.assertFalse(working["closure_complete"])
            dynamic = next(row for row in working["frontier"] if row["reason"] == "runtime_target_unresolved")
            self.assertFalse(dynamic["runtime_target_resolved"])


if __name__ == "__main__":
    unittest.main()
