"""Review the same source structures without corrupting formula or location."""

import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from claim_ledger import review_answer_claims
from evidence_answer import build_evidence_answer


def page(body, *, name="modules/rules.v2/amount.cbl", first=1):
    return {"evidence_id": "ev_guarded", "relative_path": name, "source_text": body,
        "source_sha256": hashlib.sha256(body.encode()).hexdigest(), "span_truncated": False,
        "start_line": first, "end_line": first + len(body.splitlines()) - 1}


class ClaimLedgerBoundaryTests(unittest.TestCase):
    body = ("PROCEDURE DIVISION.\nMAIN.\nIF BILL-DATE <= TODAY-DATE AND PRINCIPAL > ZERO\n"
            "COMPUTE BILL-AMOUNT = PRINCIPAL + FEE\nEND-IF.\nGOBACK.")

    def review(self, text, body=None, **options):
        supplied = page(self.body if body is None else body, **options)
        answer, ledger = review_answer_claims(text, [supplied], {"ev_guarded": supplied}, source_format="free")
        return answer, ledger.to_dict()

    def test_generated_source_answer_retains_compound_guard_formula_and_location(self):
        supplied = page(self.body)
        original = build_evidence_answer("BILL-AMOUNT 怎样计算？", [supplied], {}, source_format="free")
        answer, ledger = review_answer_claims(original["answer"], [supplied], {"ev_guarded": supplied}, source_format="free")
        self.assertEqual(answer, original["answer"])
        self.assertIn("BILL-AMOUNT = PRINCIPAL + FEE", answer)
        self.assertIn("modules/rules.v2/amount.cbl:4", answer)
        self.assertEqual(ledger.to_dict()["revised_claim_count"], 0)
        self.assertTrue(any(entry.target_entity == "BILL-AMOUNT" and entry.support_status == "supported"
                            for entry in ledger.entries))
        self.assertFalse(ledger.to_dict()["prose_semantics_verified"])

    def test_identical_model_notation_is_checked_and_kept_intact(self):
        text = ("在 `IF BILL-DATE <= TODAY-DATE AND PRINCIPAL > ZERO` 下，"
                "`BILL-AMOUNT = PRINCIPAL + FEE`（modules/rules.v2/amount.cbl:4）[ev_guarded]。")
        answer, ledger = self.review(text)
        self.assertEqual(answer, text)
        self.assertEqual(ledger["counts"]["contradicted"], 0)
        self.assertEqual(ledger["revised_claim_count"], 0)
        conditions = [entry for entry in ledger["entries"] if entry["claim_type"] == "condition_statement"]
        self.assertEqual(len(conditions), 1)
        self.assertEqual(conditions[0]["condition"], ["BILL-DATE", "<=", "TODAY-DATE", "AND", "PRINCIPAL", ">", "ZERO"])
        self.assertEqual(conditions[0]["support_status"], "supported")

    def test_wrong_formula_with_same_guard_and_real_id_is_still_repaired(self):
        text = ("在 `IF BILL-DATE <= TODAY-DATE AND PRINCIPAL > ZERO` 下，"
                "`BILL-AMOUNT = PRINCIPAL * 999`（modules/rules.v2/amount.cbl:4）[ev_guarded]。"
                "输入来源另待核对。[ev_guarded]")
        answer, ledger = self.review(text)
        self.assertNotIn("999", answer)
        self.assertIn("BILL-AMOUNT = PRINCIPAL + FEE", answer)
        self.assertIn("BILL-DATE <= TODAY-DATE AND PRINCIPAL > ZERO", answer)
        self.assertIn("输入来源另待核对", answer)
        self.assertNotIn("。cbl:", answer)
        self.assertGreater(ledger["revised_claim_count"], 0)
        self.assertGreater(ledger["counts"]["contradicted"], 0)

    def test_wrong_compound_guard_does_not_erase_independent_supported_formula(self):
        text = ("在 `IF BILL-DATE <= TODAY-DATE AND PRINCIPAL > 999` 下，"
                "`BILL-AMOUNT = PRINCIPAL + FEE`（modules/rules.v2/amount.cbl:4）[ev_guarded]。")
        answer, ledger = self.review(text)
        self.assertNotIn("999", answer)
        self.assertIn("BILL-DATE <= TODAY-DATE AND PRINCIPAL > ZERO", answer)
        self.assertIn("BILL-AMOUNT = PRINCIPAL + FEE", answer)
        self.assertGreater(ledger["revised_claim_count"], 0)

    def test_compound_equality_is_not_mistaken_for_an_assignment(self):
        body = "IF RUN-FLAG = 1 AND STATUS-CODE = 0\nMOVE 2 TO OUT-AMOUNT\nEND-IF."
        text = "`IF RUN-FLAG = 1 AND STATUS-CODE = 0`。[ev_guarded]"
        answer, ledger = self.review(text, body)
        self.assertEqual(answer, text)
        self.assertEqual(len(ledger["entries"]), 1)
        self.assertEqual(ledger["entries"][0]["claim_type"], "condition_statement")
        self.assertEqual(ledger["entries"][0]["support_status"], "supported")

    def test_parenthesized_or_condition_stays_one_supported_structure(self):
        body = "IF (RUN-FLAG = 1 OR RUN-FLAG = 2) AND STATUS-CODE = 0\nMOVE 2 TO OUT-AMOUNT\nEND-IF."
        text = "`IF (RUN-FLAG = 1 OR RUN-FLAG = 2) AND STATUS-CODE = 0`。[ev_guarded]"
        answer, ledger = self.review(text, body)
        self.assertEqual(answer, text)
        self.assertEqual(len(ledger["entries"]), 1)
        self.assertEqual(ledger["entries"][0]["support_status"], "supported")

    def test_literal_punctuation_and_string_move_remain_supported(self):
        body = 'MOVE "A. B *> STILL TEXT" TO MESSAGE-TEXT.'
        text = '`MESSAGE-TEXT = "A. B *> STILL TEXT"`（modules/rules.v2/amount.cbl:1）[ev_guarded]。'
        answer, ledger = self.review(text, body)
        self.assertEqual(answer, text)
        self.assertEqual(ledger["entries"][0]["claim_type"], "assignment_statement")
        self.assertEqual(ledger["entries"][0]["support_status"], "supported")

    def test_wrong_string_move_is_repaired_without_orphaned_location(self):
        body = 'MOVE "A. B" TO MESSAGE-TEXT.'
        text = '`MESSAGE-TEXT = "WRONG. VALUE"`（modules/rules.v2/amount.cbl:1）[ev_guarded]。'
        answer, ledger = self.review(text, body)
        self.assertIn('MESSAGE-TEXT = "A. B"', answer)
        self.assertNotIn("WRONG", answer)
        self.assertNotIn("。cbl:", answer)
        self.assertGreater(ledger["counts"]["contradicted"], 0)

    def test_decimal_and_dotted_source_path_do_not_merge_next_sentence(self):
        text = ("OUT-AMOUNT = IN-AMOUNT * 999。[ev_guarded] "
                "Input is supplied by the user.[ev_guarded]")
        answer, ledger = self.review(text, "COMPUTE OUT-AMOUNT = IN-AMOUNT * 1.25.")
        self.assertIn("IN-AMOUNT * 1.25", answer)
        self.assertNotIn("999", answer)
        self.assertIn("Input is supplied by the user", answer)
        self.assertNotIn("。v2/", answer)
        self.assertGreater(ledger["revised_claim_count"], 0)

    def test_unbound_english_if_prose_remains_explicitly_unverified(self):
        text = "If no record is found, the result flag is R and the program reports a missing record."
        answer, ledger = self.review(text)
        self.assertEqual(answer, text)
        self.assertFalse(ledger["all_claims_supported"])
        self.assertEqual(ledger["counts"]["supported"], 0)

    def test_natural_guard_equality_is_checked_as_condition_and_keeps_call_prose(self):
        body = 'PROCEDURE DIVISION.\nMAIN.\nIF READY-FLAG = 1\nCALL "JOB-STEP"\nEND-IF.\nGOBACK.'
        text = ("JOB-STEP由LAUNCHER在READY-FLAG = 1的源码条件下通过CALL调用。"
                "源码未证明外部调度绑定，也未证明实际执行。[ev_guarded]")
        answer, ledger = self.review(text, body, name="caller.cbl")
        self.assertEqual(answer, text)
        conditions = [entry for entry in ledger["entries"] if entry["claim_type"] == "condition_statement"]
        self.assertEqual(len(conditions), 1)
        self.assertEqual(conditions[0]["condition"], ["READY-FLAG", "=", "1"])
        self.assertEqual(conditions[0]["support_status"], "supported")
        self.assertFalse(ledger["prose_semantics_verified"])

    def test_natural_guard_with_wrong_constant_is_repaired_under_same_real_id(self):
        body = 'PROCEDURE DIVISION.\nMAIN.\nIF READY-FLAG = 1\nCALL "JOB-STEP"\nEND-IF.\nGOBACK.'
        text = ("JOB-STEP由LAUNCHER在READY-FLAG = 999的源码条件下通过CALL调用。"
                "源码未证明外部调度绑定，也未证明实际执行。[ev_guarded]")
        answer, ledger = self.review(text, body, name="caller.cbl")
        self.assertNotIn("999", answer)
        self.assertIn("IF READY-FLAG = 1", answer)
        self.assertIn("源码未证明外部调度绑定", answer)
        self.assertGreater(ledger["counts"]["contradicted"], 0)

    def test_assignment_wording_cannot_borrow_an_if_as_its_support(self):
        body = 'PROCEDURE DIVISION.\nMAIN.\nIF READY-FLAG = 1\nCALL "JOB-STEP"\nEND-IF.\nGOBACK.'
        answer, ledger = self.review("程序执行赋值 READY-FLAG = 1。[ev_guarded]", body)
        self.assertNotIn("程序执行赋值 READY-FLAG = 1", answer)
        self.assertFalse(ledger["all_claims_supported"])
        self.assertEqual(ledger["counts"]["supported"], 0)

    def test_natural_wrong_guard_without_source_condition_is_not_marked_supported(self):
        answer, ledger = self.review("在 READY-FLAG = 1 的条件下调用。[ev_guarded]", 'CALL "JOB-STEP".')
        self.assertNotIn("在 READY-FLAG = 1 的条件下调用", answer)
        self.assertEqual(ledger["counts"]["supported"], 0)

    def test_default_activation_answer_keeps_real_caller_guard_and_unknown_scheduler(self):
        from agent_policy import AgentPolicy
        from business_chat import run_business_chat
        from company_api import CompanyAPIConfig, TransportResponse
        from test_activation_evidence import ActivationEvidenceTests, QUESTION
        fixture = ActivationEvidenceTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.build()
        answers = []

        def transport(request):
            payload = json.loads(json.loads(request.body)["messages"][-1]["content"])
            caller = next(page for bundle in payload["source_context"] for page in bundle["pages"]
                          if page["relative_path"] == "caller.cbl" and 'CALL "JOB-STEP"' in page["source_text"])
            self.assertEqual(caller["source_sha256"], hashlib.sha256((fixture.source / "caller.cbl").read_bytes()).hexdigest())
            text = ("JOB-STEP由LAUNCHER在READY-FLAG = 1的源码条件下通过CALL调用。"
                    "源码未证明外部调度绑定，也未证明实际执行。[" + caller["evidence_id"] + "]")
            answers.append(text)
            return TransportResponse(200, json.dumps({"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}))

        output = run_business_chat(QUESTION, fixture.database, fixture.source,
            CompanyAPIConfig("https://neutral.example.invalid/v1", "offline-model", api_key="offline-only"),
            transport=transport, allow_network=False, framework_reference_path="",
            policy=AgentPolicy(max_model_requests=1, max_answer_revisions=0))
        result = output["agent_result"]
        self.assertEqual(len(answers), 1)
        self.assertEqual(result["answer"], answers[0])
        self.assertEqual(result["answer_origin"], "model")
        self.assertIs(result["claims_semantically_verified"], False)
        self.assertTrue(any(entry["claim_type"] == "condition_statement" and entry["support_status"] == "supported"
                            for entry in result["claim_ledger"]["entries"]))


if __name__ == "__main__":
    unittest.main()
