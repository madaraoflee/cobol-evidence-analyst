"""Local source explanations follow the answer language without changing evidence."""

import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from answer_fact_completion import complete_supplied_answer
from claim_ledger import review_answer_claims
from evidence_answer import build_evidence_answer


SOURCE = """IDENTIFICATION DIVISION.
PROGRAM-ID. AMOUNT-WORKER.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 IN-AMOUNT PIC S9(7)V99.
01 RATE PIC 9(3).
01 OUT-AMOUNT PIC S9(7)V99.
01 TOTAL-AMOUNT PIC S9(9)V99.
PROCEDURE DIVISION.
MAIN.
IF RATE NOT = 0
  PERFORM CALCULATE-AMOUNT
END-IF.
GOBACK.
CALCULATE-AMOUNT.
COMPUTE OUT-AMOUNT = IN-AMOUNT * RATE / 1200
ADD OUT-AMOUNT TO TOTAL-AMOUNT
PERFORM WRITE-RESULT.
EXIT.
WRITE-RESULT.
CONTINUE.
"""


def page(text):
    return {"relative_path": "amount.cbl", "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "start_line": 1, "end_line": len(text.splitlines()), "source_text": text,
            "evidence_id": "ev_amount_source"}


def completion_state(source):
    lines = source.splitlines()
    return {"analysis_plan": {"intents": ["calculation", "accumulation"], "entities": []},
            "source_scope": {"ranges": [{"relative_path": "amount.cbl",
                "source_sha256": page(source)["source_sha256"], "procedure_name": "CALCULATE-AMOUNT",
                "start_line": lines.index("CALCULATE-AMOUNT.") + 1,
                "end_line": lines.index("EXIT.") + 1}]}}


class SuppliedAnswerLanguageTests(unittest.TestCase):
    def complete(self, question, source=SOURCE, **options):
        original = "The supplied formula is `OUT-AMOUNT = IN-AMOUNT * RATE / 1200`. [ev_amount_source]"
        answer, report = complete_supplied_answer(question, original, completion_state(source), [page(source)],
            source_format="free", **options)
        self.assertTrue(answer.startswith(original))
        return answer, report

    def build(self, question, source, **options):
        return build_evidence_answer(question, [page(source)], {}, source_format="free", **options)

    def test_english_completion_preserves_formula_and_versioned_citations(self):
        answer, report = self.complete("Explain how CALCULATE-AMOUNT calculates and accumulates the amount.")
        self.assertNotRegex(answer, r"[\u3400-\u9fff]")
        for expected in ("TOTAL-AMOUNT = OUT-AMOUNT + TOTAL-AMOUNT", "PERFORM WRITE-RESULT",
                         "IF RATE NOT = 0", "PIC S9(7)V99", "2 decimal places", "no ROUNDED clause"):
            self.assertIn(expected, answer)
        self.assertEqual({fact["kind"] for fact in report["added_facts"]},
                         {"assignment", "following_invocation", "invocation_condition", "numeric_layout"})
        self.assertTrue(all(fact["source_sha256"] == page(SOURCE)["source_sha256"]
                            and fact["evidence_id"] == "ev_amount_source" for fact in report["added_facts"]))
        self.assertEqual(report["model_requests"], 0)

    def test_english_completion_translates_control_and_numeric_qualifiers(self):
        bodies = [
            "EVALUATE RATE\nWHEN 1\nCOMPUTE OUT-AMOUNT ROUNDED = IN-AMOUNT * 2\n"
            "WHEN OTHER\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 3\nEND-EVALUATE.",
            "READ INPUT-FILE\nINVALID KEY\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2\n"
            "NOT INVALID KEY\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 3\nEND-READ.",
            "COMPUTE OUT-AMOUNT = IN-AMOUNT * 2\nON SIZE ERROR\nCONTINUE\nEND-COMPUTE.",
            "IF RATE = 1\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2\nELSE\n"
            "COMPUTE OUT-AMOUNT = IN-AMOUNT * 3\nEND-IF\nIF RATE = 2\nPERFORM WRITE-RESULT\nEND-IF.",
        ]
        expectations = [("the `WHEN OTHER` branch", "no earlier WHEN branch matched", "different ROUNDED settings"),
                        ("INVALID KEY branch", "NOT INVALID KEY branch"),
                        ("when no SIZE ERROR occurs",), ("`IF RATE = 1` is false", "`IF RATE = 2` is true")]
        for body, expected in zip(bodies, expectations):
            with self.subTest(body=body):
                source = SOURCE.replace("COMPUTE OUT-AMOUNT = IN-AMOUNT * RATE / 1200\n"
                    "ADD OUT-AMOUNT TO TOTAL-AMOUNT\nPERFORM WRITE-RESULT.", body)
                answer, _ = complete_supplied_answer("Explain the calculation and its conditions.",
                    "The source contains a calculation.", completion_state(source), [page(source)], source_format="free")
                self.assertNotRegex(answer, r"[\u3400-\u9fff]")
                for text in expected:
                    self.assertIn(text, answer)
                self.assertIn("[ev_amount_source]", answer)

    def test_explicit_language_controls_completion(self):
        english, _ = self.complete("解释计算和累加规则。", answer_language="en")
        self.assertNotRegex(english, r"[\u3400-\u9fff]")
        chinese, _ = self.complete("Explain the calculation.", answer_language="zh-CN")
        self.assertIn("原文还给出", chinese)
        traditional, _ = self.complete("Explain the calculation.", answer_language="zh-HK")
        self.assertIn("原文還給出", traditional)
        self.assertIn("位小數", traditional)

    def test_english_fallback_keeps_branch_scope_and_evidence(self):
        source = "IF IN-AMOUNT > 0\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2\nELSE\nMOVE 0 TO OUT-AMOUNT\nEND-IF."
        result = self.build("How is OUT-AMOUNT calculated?", source)
        self.assertIsNotNone(result)
        self.assertNotRegex(result["answer"], r"[\u3400-\u9fff]")
        for expected in ("OUT-AMOUNT = IN-AMOUNT * 2", "OUT-AMOUNT = 0", "IF IN-AMOUNT > 0",
                         "[ev_amount_source]", "final results remain unconfirmed"):
            self.assertIn(expected, result["answer"])
        self.assertEqual(result["status"], "PARTIAL")
        self.assertFalse(result["execution_verified"])
        self.assertEqual(result["source_versions"][0]["source_sha256"], page(source)["source_sha256"])

    def test_english_fallback_preserves_chinese_source_literal(self):
        result = self.build("What value is assigned to LABEL-TEXT?", 'MOVE "原始文字" TO LABEL-TEXT.')
        self.assertIn('LABEL-TEXT = "原始文字"', result["answer"])
        self.assertNotRegex(result["answer"].replace("原始文字", ""), r"[\u3400-\u9fff]")
        self.assertIn("[ev_amount_source]", result["answer"])

    def test_english_call_fallback_translates_false_guard_and_scheduling_limits(self):
        source = ('PROCEDURE DIVISION.\nMAIN.\nIF RUN-FLAG = 1\nCONTINUE\nELSE\n'
                  'CALL "BATCH-WORKER"\nEND-IF.\nGOBACK.')
        result = self.build("How is BATCH-WORKER called?", source)
        self.assertNotRegex(result["answer"], r"[\u3400-\u9fff]")
        for expected in ('CALL "BATCH-WORKER"', "false condition: IF RUN-FLAG = 1",
                         "do not establish external scheduling"):
            self.assertIn(expected, result["answer"])

    def test_english_call_fallback_translates_unconfirmed_compilation_notice(self):
        source = ('PROCEDURE DIVISION.\nMAIN.\n>>IF FEATURE-A\n'
                  'CALL "BATCH-WORKER".\n>>END-IF\nGOBACK.')
        result = self.build("How is BATCH-WORKER called?", source)
        self.assertNotRegex(result["answer"], r"[\u3400-\u9fff]")
        self.assertIn("Compilation conditions are unconfirmed", result["answer"])
        self.assertEqual(result["facts"][0]["compilation_status"], "candidate")

    def test_english_bound_call_fallback_retains_incomplete_control_notice(self):
        source = 'CALL "BATCH-WORKER".'
        supplied = page(source)
        state = {"analysis_plan": {"intents": ["activation"], "entities": ["BATCH-WORKER"]},
            "activation_evidence": {"callers": [{"relative_path": "amount.cbl",
                "source_sha256": supplied["source_sha256"], "evidence_ids": ["ev_amount_source"],
                "target_name": "BATCH-WORKER", "line": 1, "occurrence_id": "bound-call-one",
                "binding_confirmed": True, "compilation_status": "active", "scope_known": False}]}}
        result = build_evidence_answer("How is BATCH-WORKER called?", [supplied], state, source_format="free")
        self.assertNotRegex(result["answer"], r"[\u3400-\u9fff]")
        self.assertIn("control conditions of this call have not been fully checked", result["answer"])
        self.assertIn('CALL "BATCH-WORKER"', result["answer"])
        self.assertIn("[ev_amount_source]", result["answer"])

    def test_english_call_condition_and_citation_survive_claim_review(self):
        for branch in ("", "CONTINUE\nELSE\n"):
            with self.subTest(branch=branch):
                source = 'IF RUN-FLAG = 1\n' + branch + 'CALL "BATCH-WORKER"\nEND-IF.'
                supplied = page(source)
                result = self.build("How is BATCH-WORKER called?", source)
                reviewed, ledger = review_answer_claims(result["answer"], [supplied],
                    {"ev_amount_source": supplied}, source_format="free", answer_language="en")
                self.assertEqual(reviewed, result["answer"])
                self.assertEqual(ledger.to_dict()["revised_claim_count"], 0)
                self.assertIn("IF RUN-FLAG = 1", reviewed)
                self.assertIn("[ev_amount_source]", reviewed)

    def test_fallback_selection_limit_notice_uses_english(self):
        result = self.build("How is OUT-AMOUNT assigned?", "\n".join(
            f"MOVE {value} TO OUT-AMOUNT." for value in range(10)))
        self.assertEqual(len(result["facts"]), 8)
        self.assertNotRegex(result["answer"], r"[\u3400-\u9fff]")
        self.assertIn("Only the statements most relevant to the question are shown", result["answer"])

    def test_explicit_language_controls_fallback(self):
        source = "MOVE 1 TO OUT-AMOUNT."
        english = self.build("OUT-AMOUNT 如何赋值？", source, answer_language="en")
        self.assertNotRegex(english["answer"], r"[\u3400-\u9fff]")
        traditional = self.build("How is OUT-AMOUNT assigned?", source, answer_language="zh-HK")
        self.assertIn("模型說明尚未完成", traditional["answer"])
        self.assertIn("最終結果尚未確認", traditional["answer"])


if __name__ == "__main__":
    unittest.main()
