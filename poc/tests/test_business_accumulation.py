"""Explicit accumulation coverage uses supplied statements, never answer length."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from agent_policy import AgentPolicy
import business_chat
from business_index import build_business_index
from business_synthesis import answer_requirements, assess_business_answer
from claim_ledger import review_answer_claims
from company_api import CompanyAPIConfig, TransportResponse
from question_planner import QuestionPlanner
from repository_discovery import ensure_repository_search
from tools.desktop_acceptance_runtime import decoded_navigation


class AccumulationCoverageTests(unittest.TestCase):
    def material(self, text="COMPUTE PERIOD-AMOUNT = BASIS * FACTOR.\nADD PERIOD-AMOUNT TO TOTAL-AMOUNT."):
        page = {"relative_path": "accrual.cbl", "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "start_line": 1, "end_line": len(text.splitlines()), "source_text": text,
            "evidence_id": "ev:accrual", "span_truncated": False}
        investigation = {"state": "located", "analysis_plan": {"paths": ["accrual.cbl"]},
            "required_items": [{"kind": kind, "status": "SATISFIED", "candidate_count": 1,
                "evidence_ids": [page["evidence_id"]]} for kind in ("formula", "accumulation_step")]}
        return investigation, [page]

    def assessment(self, question, answer, text=None):
        investigation, pages = self.material() if text is None else self.material(text)
        return assess_business_answer(question, answer, investigation, pages)

    def test_explicit_multilingual_operation_requires_actual_accumulation(self):
        for question in ("PERIOD-AMOUNT 如何计算并累加？", "PERIOD-AMOUNT 如何計算並累計？",
                "How is PERIOD-AMOUNT computed and accumulated?", "How does PERIOD-AMOUNT enter the running total?",
                "How do we add PERIOD-AMOUNT into TOTAL-AMOUNT?"):
            with self.subTest(question=question):
                review = self.assessment(question, "PERIOD-AMOUNT = BASIS * FACTOR。[ev:accrual]")
                self.assertIn("accumulation", review["required_aspects"])
                self.assertIn("accumulation", review["missing_aspects"])
                self.assertEqual(review["status"], "incomplete")
                self.assertEqual(review["semantic_verification"], "unverified")

    def test_short_actual_add_and_equivalent_formulas_cover_the_operation(self):
        for answer in ("ADD PERIOD-AMOUNT TO TOTAL-AMOUNT。", "TOTAL-AMOUNT = TOTAL-AMOUNT + PERIOD-AMOUNT。",
                "TOTAL-AMOUNT = PERIOD-AMOUNT + TOTAL-AMOUNT。", "将 PERIOD-AMOUNT 累加到 TOTAL-AMOUNT。",
                "把本月金额加进累计金额。[ev:accrual]", "Add PERIOD-AMOUNT to TOTAL-AMOUNT."):
            with self.subTest(answer=answer):
                review = self.assessment("TOTAL-AMOUNT 如何累加？", answer)
                self.assertIn("accumulation", review["required_aspects"])
                self.assertNotIn("accumulation", review["missing_aspects"])

    def test_compute_self_update_is_an_accumulator_but_self_multiply_is_not(self):
        review = self.assessment("TOTAL-AMOUNT 如何累加？", "TOTAL-AMOUNT = TOTAL-AMOUNT + PERIOD-AMOUNT。",
                                "COMPUTE TOTAL-AMOUNT = TOTAL-AMOUNT + PERIOD-AMOUNT.")
        self.assertNotIn("accumulation", review["missing_aspects"])
        review = self.assessment("TOTAL-AMOUNT 如何累加？", "TOTAL-AMOUNT = TOTAL-AMOUNT * 2。",
                                "COMPUTE TOTAL-AMOUNT = TOTAL-AMOUNT * 2.")
        self.assertNotIn("accumulation", review["required_aspects"])

    def test_table_segment_metadata_preserves_actual_accumulation_coverage(self):
        for expression, covered in (("TOTAL-AMOUNT + PERIOD-AMOUNT", True), ("TOTAL-AMOUNT + 999", False)):
            with self.subTest(expression=expression):
                answer = "| Result | Operation |\n| --- | --- |\n| Total | TOTAL-AMOUNT = " + expression + ". [ev:accrual] |"
                review = self.assessment("TOTAL-AMOUNT 如何累加？", answer)
                self.assertEqual("accumulation" not in review["missing_aspects"], covered)

    def test_constant_reset_and_wrong_increment_do_not_masquerade_as_accumulation(self):
        for answer in ("TOTAL-AMOUNT = 0。", "TOTAL-AMOUNT = 99。", "TOTAL-AMOUNT = PERIOD-AMOUNT。",
                       "TOTAL-AMOUNT = TOTAL-AMOUNT + 999。", "ADD 999 TO TOTAL-AMOUNT。",
                       "累计值还需要按文件写入状态核对。[ev:accrual]"):
            with self.subTest(answer=answer):
                self.assertIn("accumulation", self.assessment("TOTAL-AMOUNT 如何累加？", answer)["missing_aspects"])
        investigation, pages = self.material()
        answer, ledger = review_answer_claims("TOTAL-AMOUNT = TOTAL-AMOUNT + 999。[ev:accrual]",
            pages, {page["evidence_id"]: page for page in pages})
        self.assertNotIn("999", answer)
        self.assertEqual(ledger.to_dict()["counts"]["contradicted"], 1)
        self.assertTrue(ledger.entries[0].revised_claims)

    def test_natural_literal_increment_must_match_without_conflicting_constants(self):
        for answer in ("TOTAL-AMOUNT 每次加上999。[ev:accrual]",
                       "TOTAL-AMOUNT 加上1然后改为999。[ev:accrual]", "TOTAL-AMOUNT 加上一百。[ev:accrual]",
                       "TOTAL-AMOUNT 加上二十一。[ev:accrual]", "TOTAL-AMOUNT increases by twenty one.[ev:accrual]",
                       "TOTAL-AMOUNT increases by one hundred.[ev:accrual]"):
            with self.subTest(answer=answer):
                self.assertIn("accumulation", self.assessment("TOTAL-AMOUNT 如何累加？", answer,
                    "ADD 1 TO TOTAL-AMOUNT.")["missing_aspects"])
        for answer in ("TOTAL-AMOUNT 每次加上1。", "TOTAL-AMOUNT 每次加上一。",
                       "TOTAL-AMOUNT increases by one.", "TOTAL-AMOUNT increases by 1."):
            with self.subTest(answer=answer):
                self.assertNotIn("accumulation", self.assessment("TOTAL-AMOUNT 如何累加？", answer,
                    "ADD 1 TO TOTAL-AMOUNT.")["missing_aspects"])

    def test_explicit_denial_or_unmatched_scaling_cannot_cover_a_known_add(self):
        for answer in ("TOTAL-AMOUNT 不会累加 PERIOD-AMOUNT。[ev:accrual]",
                       "不会把 PERIOD-AMOUNT 加入 TOTAL-AMOUNT。[ev:accrual]",
                       "TOTAL-AMOUNT 只累加 PERIOD-AMOUNT 的一半。[ev:accrual]",
                       "TOTAL-AMOUNT does not accumulate PERIOD-AMOUNT.[ev:accrual]",
                       "Do not ADD PERIOD-AMOUNT TO TOTAL-AMOUNT.[ev:accrual]"):
            with self.subTest(answer=answer):
                self.assertIn("accumulation", self.assessment("TOTAL-AMOUNT 如何累加？", answer)["missing_aspects"])

    def test_not_only_affirms_an_operation_but_does_not_hide_a_real_negation(self):
        for qualifier in ("not only", "not just", "not merely"):
            answer = "TOTAL-AMOUNT " + qualifier + " accumulates PERIOD-AMOUNT but also updates a report.[ev:accrual]"
            self.assertNotIn("accumulation", self.assessment("TOTAL-AMOUNT 如何累加？", answer)["missing_aspects"])
        for answer in ("TOTAL-AMOUNT not only never accumulates PERIOD-AMOUNT.[ev:accrual]",
                       "TOTAL-AMOUNT not only accumulates PERIOD-AMOUNT but TOTAL-AMOUNT never accumulates PERIOD-AMOUNT.[ev:accrual]"):
            self.assertIn("accumulation", self.assessment("TOTAL-AMOUNT 如何累加？", answer)["missing_aspects"])

    def test_why_a_step_did_not_run_requires_conditions_without_an_affirmative_accumulation(self):
        mapping = {"direct_paths": ["accrual.cbl"], "rule_leads": [{"reads": ["PERIOD-AMOUNT", "TOTAL-AMOUNT"],
            "writes": ["TOTAL-AMOUNT"]}]}
        for question in ("为什么没有累加到 TOTAL-AMOUNT？", "Why does TOTAL-AMOUNT not accumulate?"):
            plan = QuestionPlanner().plan(question, mapping)
            self.assertNotIn("accumulation", plan.intents)
            self.assertIn("condition", plan.intents)
            self.assertIn("backward_slice", plan.tools)
            self.assertNotIn("accumulation", self.assessment(question,
                "IF RUN-FLAG = 1 不成立，跳过这条处理。")["required_aspects"])
        for question in ("为什么没累加、应该怎么累加到 TOTAL-AMOUNT？",
                         "Why did it not accumulate, and how is TOTAL-AMOUNT accumulated?"):
            self.assertIn("accumulation", QuestionPlanner().plan(question, mapping).intents)

    def test_unrelated_write_uncertainty_does_not_erase_known_add(self):
        answer = "ADD PERIOD-AMOUNT TO TOTAL-AMOUNT，文件是否写入成功无法确认。[ev:accrual]"
        self.assertNotIn("accumulation", self.assessment("TOTAL-AMOUNT 如何累加？", answer)["missing_aspects"])

    def test_valid_reference_cannot_turn_an_explicit_different_operation_into_a_business_alias(self):
        for answer in ("ADD FEE TO OTHER-TOTAL。[ev:accrual]", "将 FEE 累加到 OTHER-TOTAL。[ev:accrual]",
                       "OTHER-TOTAL = OTHER-TOTAL + FEE。[ev:accrual]"):
            with self.subTest(answer=answer):
                self.assertIn("accumulation", self.assessment("TOTAL-AMOUNT 如何累加？", answer)["missing_aspects"])

    def test_equivalent_sequence_containers_deduplicate_only_the_same_occurrence(self):
        investigation, pages = self.material()
        pages[0].update(include_chain=[], replacement_trace=[])
        same = {**deepcopy(pages[0]), "include_chain": (), "replacement_trace": ()}
        requirements = answer_requirements("TOTAL-AMOUNT 如何累加？", investigation, [*pages, same])
        hints = next(item for item in requirements if item["kind"] == "accumulation")["coverage_hints"]
        self.assertEqual(len(hints), 1)
        distinct = {**deepcopy(same), "copy_instance": "second-copy"}
        requirements = answer_requirements("TOTAL-AMOUNT 如何累加？", investigation, [*pages, distinct])
        self.assertEqual(len(next(item for item in requirements if item["kind"] == "accumulation")["coverage_hints"]), 2)

    def test_unique_cited_business_short_answer_needs_a_real_reference(self):
        for suffix in ("", "[ev:fake]"):
            with self.subTest(suffix=suffix):
                self.assertIn("accumulation", self.assessment("TOTAL-AMOUNT 如何累加？",
                    "把本月金额加进累计金额。" + suffix)["missing_aspects"])

    def test_ambiguous_multiple_accumulators_cannot_share_a_generic_short_answer(self):
        investigation, pages = self.material("ADD PART-AMOUNT TO LEFT-TOTAL RIGHT-TOTAL.")
        review = assess_business_answer("PART-AMOUNT 如何累加？", "把本月金额加进累计金额。[ev:accrual]",
                                        investigation, pages)
        self.assertIn("accumulation", review["missing_aspects"])

    def test_unrelated_add_and_giving_non_receiver_do_not_acquire_an_aspect(self):
        for text in ("COMPUTE PERIOD-AMOUNT = BASIS * FACTOR.\nADD 1 TO RUN-COUNT.",
                     "ADD BASIS TO FACTOR GIVING PERIOD-AMOUNT."):
            with self.subTest(text=text):
                review = self.assessment("PERIOD-AMOUNT 如何计算并累加？", "PERIOD-AMOUNT = BASIS * FACTOR。", text)
                self.assertNotIn("accumulation", review["required_aspects"])

    def test_an_explicit_accumulator_target_does_not_require_its_downstream_consumer(self):
        investigation, pages = self.material("ADD PERIOD-AMOUNT TO TOTAL-AMOUNT.\nADD TOTAL-AMOUNT TO REPORT-TOTAL.")
        requirements = answer_requirements("TOTAL-AMOUNT 如何累加？", investigation, pages)
        hint = next(item for item in requirements if item["kind"] == "accumulation")
        self.assertEqual([row["target_field"] for row in hint["coverage_hints"]], ["TOTAL-AMOUNT"])
        self.assertNotIn("accumulation", assess_business_answer("TOTAL-AMOUNT 如何累加？",
            "ADD PERIOD-AMOUNT TO TOTAL-AMOUNT。", investigation, pages)["missing_aspects"])

    def test_literals_unsupplied_pages_and_stale_scope_cannot_nominate_an_add(self):
        investigation, pages = self.material("DISPLAY 'ADD PERIOD-AMOUNT TO TOTAL-AMOUNT'.")
        self.assertNotIn("accumulation", [item["kind"] for item in answer_requirements(
            "TOTAL-AMOUNT 如何累加？", investigation, pages)])
        investigation, pages = self.material()
        investigation["required_items"][1]["evidence_ids"] = ["ev:unsupplied"]
        self.assertNotIn("accumulation", [item["kind"] for item in answer_requirements(
            "TOTAL-AMOUNT 如何累加？", investigation, pages)])
        investigation, pages = self.material()
        investigation["source_scope"] = {"ranges": [{"relative_path": "accrual.cbl", "start_line": 1,
            "end_line": 2, "source_sha256": "0" * 64}]}
        self.assertNotIn("accumulation", [item["kind"] for item in answer_requirements(
            "TOTAL-AMOUNT 如何累加？", investigation, pages)])

    def test_no_accumulation_intent_adds_no_new_parsing_work(self):
        investigation, pages = self.material()
        with mock.patch("claim_support.supplied_statement_facts", side_effect=AssertionError("Unneeded fact parsing")):
            review = assess_business_answer("PERIOD-AMOUNT 怎么计算？", "PERIOD-AMOUNT = BASIS * FACTOR。",
                                            investigation, pages)
        self.assertNotIn("accumulation", review["required_aspects"])

    def test_question_planner_consumes_actual_operation_not_identifier_text(self):
        mapping = {"direct_paths": ["accrual.cbl"], "rule_leads": [{"reads": ["PERIOD-AMOUNT", "TOTAL-AMOUNT"],
            "writes": ["TOTAL-AMOUNT"]}]}
        for question in ("TOTAL-AMOUNT 如何累加？", "How is TOTAL-AMOUNT accumulated?"):
            plan = QuestionPlanner().plan(question, mapping)
            self.assertIn("accumulation", plan.intents)
            self.assertIn("accumulation_step", plan.required_obligations)
            self.assertIn("explain_assignment", plan.tools)
        plan = QuestionPlanner().plan("ACCUMULATE-PROGRAM 的字段类型是什么？", mapping)
        self.assertNotIn("accumulation", plan.intents)
        for question in ("累计利息的PIC是什么？", "累计利息的类型是什么？", "累计利息的长度是什么？",
                         "What is the data type of ACCUMULATED-INTEREST?"):
            with self.subTest(question=question):
                plan = QuestionPlanner().plan(question, mapping)
                self.assertNotIn("accumulation", plan.intents)
                self.assertNotIn("accumulation", self.assessment(question, "PIC 9(9)。")["required_aspects"])

    def test_pic_letters_inside_a_field_do_not_make_accumulation_a_declaration_question(self):
        mapping = {"direct_paths": ["accrual.cbl"], "rule_leads": [{"reads": ["PERIOD-AMOUNT", "TOPIC-TOTAL"],
            "writes": ["TOPIC-TOTAL"]}]}
        question = "解释 TOPIC-TOTAL 的累加过程"
        self.assertIn("accumulation", QuestionPlanner().plan(question, mapping).intents)
        review = self.assessment(question, "TOPIC-TOTAL = PERIOD-AMOUNT + TOPIC-TOTAL。",
                                 "ADD PERIOD-AMOUNT TO TOPIC-TOTAL.")
        self.assertIn("accumulation", review["required_aspects"])
        self.assertNotIn("accumulation", review["missing_aspects"])


class DefaultAccumulationTests(unittest.TestCase):
    def test_default_one_request_supplies_operation_and_detects_only_its_actual_omission(self):
        source_text = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. ACCRUAL-MAIN.\nDATA DIVISION.\n"
            "WORKING-STORAGE SECTION.\n01 BASIS PIC 9(6) VALUE 4.\n01 FACTOR PIC 9 VALUE 2.\n"
            "01 PERIOD-AMOUNT PIC 9(8).\n01 TOTAL-AMOUNT PIC 9(9) VALUE 0.\nPROCEDURE DIVISION.\nMAIN.\n"
            "COMPUTE PERIOD-AMOUNT = BASIS * FACTOR.\nADD PERIOD-AMOUNT TO TOTAL-AMOUNT.\nGOBACK.\n")
        config = CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root / "source"; source.mkdir()
            (source / "accrual.cbl").write_bytes(source_text.encode("utf-8"))
            database = root / "index.sqlite"
            build_business_index(source, database, verify_content=True)
            ensure_repository_search(database, source)
            for variant in ("missing", "correct", "denied"):
                correct = variant == "correct"
                with self.subTest(variant=variant):
                    sent = []
                    def transport(request):
                        payload = decoded_navigation(json.loads(json.loads(request.body)["messages"][-1]["content"]))
                        sent.append(payload)
                        investigation = payload["question_investigation"]
                        self.assertIn("accumulation", investigation["analysis_plan"]["intents"])
                        self.assertIn("accumulation_step", investigation["analysis_plan"]["required_obligations"])
                        item = next(item for item in investigation["required_items"] if item["kind"] == "accumulation_step")
                        self.assertEqual(item["status"], "SATISFIED")
                        pages = [page for bundle in payload["source_context"] for page in bundle["pages"]]
                        witness = next(page for page in pages if page["evidence_id"] in item["evidence_ids"])
                        self.assertIn("ADD PERIOD-AMOUNT TO TOTAL-AMOUNT", witness["source_text"])
                        self.assertEqual(witness["source_sha256"], hashlib.sha256(source_text.encode()).hexdigest())
                        self.assertIn("accumulation", [item["kind"] for item in payload["business_analysis_brief"]["required_answer_aspects"]])
                        answer = "PERIOD-AMOUNT = BASIS * FACTOR。"
                        if correct:
                            answer += "把本期金额加进累计金额。"
                        elif variant == "denied":
                            answer += "TOTAL-AMOUNT 不会累加 PERIOD-AMOUNT。"
                        answer += "[" + witness["evidence_id"] + "]"
                        return TransportResponse(200, json.dumps({"choices": [{"message": {"content": answer}, "finish_reason": "stop"}]}, ensure_ascii=False))
                    result = business_chat.run_business_chat("ACCRUAL-MAIN 的 PERIOD-AMOUNT 如何计算并累加？", database,
                        source, config, transport=transport, framework_reference_path="", conversation_id=variant,
                        policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))["agent_result"]
                    self.assertEqual(len(sent), 1)
                    self.assertEqual(result["metrics"]["model_requests"], 1)
                    trace = json.loads(Path(result["metrics"]["quality_trace_path"]).read_text())
                    completion = trace["final"]["answer_completion"]
                    self.assertIn("accumulation", completion["required_aspects"])
                    self.assertEqual("accumulation" in completion["missing_aspects"], not correct)
                    self.assertEqual(completion["semantic_verification"], "unverified")
                    if not correct:
                        self.assertEqual(result["status"], "PARTIAL")
                        self.assertEqual(completion["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
