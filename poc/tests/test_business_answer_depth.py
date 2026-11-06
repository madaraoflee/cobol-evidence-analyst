"""Business requests keep their depth through review, fitting and rendering.

These offline checks validate delivery contracts and known weak answers. They
do not score free-form model understanding or assume every short answer is bad.
"""

from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_policy import AgentPolicy
from answer_fact_completion import complete_supplied_answer
from business_chat import _fit_request, _prompt_payload
from business_synthesis import assess_business_answer
from company_api import CompanyAPIConfig
from test_answer_fact_completion import investigation, page
from test_business_explanation_intent import SOURCE, page as balance_page


class BusinessAnswerDepthTests(unittest.TestCase):
    def test_business_flow_keeps_syntax_candidates_out_of_final_prose(self):
        question = "详细解释 CALCULATE-AMOUNT 的整体业务流程，不要逐行翻译代码。"
        original = "处理金额后交给后续输出。"
        plan = investigation(intents=("flow", "explanation", "calculation", "accumulation"))
        result, report = complete_supplied_answer(question, original, plan, [page()], source_format="free")
        self.assertEqual(result, original)
        self.assertEqual(report["added_facts"], [])
        self.assertTrue(any(row["kind"] == "assignment" for row in report["suggested_facts"]))
        self.assertTrue(all(row.get("source_context_range") for row in report["suggested_facts"]))
        # A calculation-specific request still receives the precise missing
        # accumulation; the overview change must not suppress that behavior.
        formula, report = complete_supplied_answer("CALCULATE-AMOUNT 的计算规则", original,
            plan, [page()], source_format="free")
        self.assertIn("TOTAL-AMOUNT = OUT-AMOUNT + TOTAL-AMOUNT", formula)
        self.assertTrue(report["added_facts"])

    def test_direct_overview_and_saved_overview_review_the_same_weak_answer(self):
        answer = "程序把 RESULT-AMOUNT 设置为 0，然后返回调用方。"
        for question, instructions in (("详细解释 balance.cbl 的业务流程", ""),
                                       ("balance.cbl", "请解释业务目的、数据流和程序分工。")):
            with self.subTest(question=question):
                review = assess_business_answer(question, answer, {}, [balance_page()],
                                                answer_instructions=instructions)
                self.assertEqual(review["status"], "incomplete")
                self.assertIn("business_purpose", review["missing_aspects"])
                self.assertIn("program_roles", review["missing_aspects"])
        narrow = assess_business_answer("RESULT-AMOUNT 设置为什么值？", answer, {}, [balance_page()])
        self.assertNotIn("business_purpose", narrow["missing_aspects"])
        unknown = assess_business_answer("详细解释 balance.cbl 的业务流程",
            "按账户查询可用余额并交回调用方，供后续账户服务采用。", {}, [balance_page()])
        self.assertFalse(unknown["business_explanation_review"]["technical_inventory_only"])

    def test_request_compaction_preserves_detailed_and_brief_contracts(self):
        payload = {"question": "RESULT-AMOUNT 怎么计算？", "answer_detail": "detailed",
            "source_context": [{"pages": [balance_page()], "call_chain": {"links": []},
                                "outline": [], "notices": []}],
            "business_map": {}, "framework_references": [], "repository": {}}
        compact = {**payload, "budget_wire_compact": True}
        self.assertIn("thoroughly", _prompt_payload(compact)["response_contract"]["detail"])
        self.assertIn("concise", _prompt_payload({**compact, "answer_detail": "brief"})
                      ["response_contract"]["detail"])
        # Force actual byte fitting to choose the compact contract while the
        # physical source still fits. No external provider is involved.
        original = balance_page(SOURCE + "*> Input context.\n" * 700)
        payload["question"] = "详细解释 balance.cbl 的业务流程"
        payload["source_context"][0]["pages"] = [original]
        config = CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model", api_key="offline")
        canonical = deepcopy(payload)
        messages, size = _fit_request(config, canonical, [], AgentPolicy(max_request_bytes=32768))
        wire = json.loads(messages[-1]["content"])
        self.assertLessEqual(size, 32768)
        self.assertTrue(canonical["budget_wire_compact"])
        self.assertEqual(wire["source_context"][0]["pages"], [original])
        self.assertIn("thoroughly", wire["response_contract"]["detail"])
        self.assertIn("subroutine", wire["response_contract"]["detail"])

    def test_sequence_contract_survives_compaction_and_saved_presentation(self):
        question = "各项余额按什么顺序抵扣？请说明适用条件和每步剩余金额。"
        payload = {"question": question, "answer_detail": "detailed",
            "source_context": [{"pages": [balance_page()], "call_chain": {"links": []},
                                "outline": [], "notices": []}],
            "business_map": {}, "framework_references": [], "repository": {}}
        for compact in (False, True):
            for instructions in ("", "请用连贯中文详细解释，并保留源码引用。"):
                with self.subTest(compact=compact, instructions=instructions):
                    wire = _prompt_payload({**payload, "budget_wire_compact": compact,
                                            "answer_instructions": instructions})
                    contract = wire["response_contract"]["business_explanation"]
                    self.assertEqual(contract["focus"], "processing_sequence")
                    self.assertIn("what remains", contract["task"])
                    self.assertIn("initialization", contract["source_walk"])
                    self.assertNotIn("Batch job", json.dumps(contract))
                    self.assertNotIn("Tables", json.dumps(contract))
                    self.assertEqual(wire["source_context"][0]["pages"], [balance_page()])

        # Exercise the actual byte fitter, not only the projection switch.
        original = balance_page(SOURCE + "*> Input context.\n" * 700)
        payload["source_context"][0]["pages"] = [original]
        config = CompanyAPIConfig("https://offline.example.invalid/v1", "offline-model", api_key="offline")
        messages, size = _fit_request(config, payload, [], AgentPolicy(max_request_bytes=32768))
        wire = json.loads(messages[-1]["content"])
        self.assertLessEqual(size, 32768)
        self.assertTrue(payload["budget_wire_compact"])
        self.assertEqual(wire["source_context"][0]["pages"], [original])
        self.assertEqual(wire["response_contract"]["business_explanation"]["focus"], "processing_sequence")
        self.assertIn("thoroughly", wire["response_contract"]["detail"])

    def test_combined_overview_and_sequence_contract_preserves_both_requests(self):
        question = "请详细说明整体业务用途、批处理和数据库关系，并说明各项余额扣减顺序。"
        for compact in (False, True):
            for instructions in ("", "请用连贯中文详细解释，并保留源码引用。"):
                with self.subTest(compact=compact, instructions=instructions):
                    payload = {"question": question, "answer_detail": "detailed",
                        "source_context": [{"pages": [balance_page()], "call_chain": {"links": []},
                                            "outline": [], "notices": []}],
                        "business_map": {}, "framework_references": [], "repository": {},
                        "budget_wire_compact": compact, "answer_instructions": instructions}
                    contract = _prompt_payload(payload)["response_contract"]["business_explanation"]
                    self.assertEqual(contract["focus"], "processing_sequence_with_overview")
                    self.assertIn("business overview", contract["task"])
                    self.assertIn("processing sequence", contract["task"])
                    self.assertNotIn("unasked deployment", contract["task"])
                    self.assertIn("explicitly excluded topics", contract["task"])
                    self.assertNotIn("order", contract)
                    self.assertNotIn("categories", contract)

    def test_combined_prompt_respects_explicitly_excluded_business_facets(self):
        for question, excluded in (
                ("请说明各项抵扣顺序和业务用途，不要讲数据库或文件。", "data_roles"),
                ("不要讲业务用途；请说明批处理和数据库关系及各项抵扣顺序。", "business_purpose")):
            for compact in (False, True):
                payload = {"question": question, "answer_detail": "detailed", "business_map": {},
                    "source_context": [{"pages": [balance_page()]}], "budget_wire_compact": compact}
                contract = _prompt_payload(payload)["response_contract"]["business_explanation"]
                self.assertIn(excluded, contract["excluded_facets"])
                self.assertNotIn("categories", contract)


if __name__ == "__main__":
    unittest.main()
