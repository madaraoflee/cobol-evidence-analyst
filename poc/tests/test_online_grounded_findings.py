"""A grounded business answer follows the verified statements, not a stored example."""
import hashlib
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from online_grounded_findings import grounded_preview, grounded_sequence_answer


QUESTION = "请说明 credit 和 rebate 的扣减顺序、跳过条件和最终结果。"
ENTRY = """IDENTIFICATION DIVISION.
PROGRAM-ID. PROCESSOR.
PROCEDURE DIVISION.
IF CLASS-CODE = 'CREDIT'
 SUBTRACT CREDIT-VALUE FROM OPEN-BILL
END-IF.
IF OPEN-BILL > 4
 CALL 'SECONDARY' USING OPEN-BILL
END-IF.
GOBACK."""
WORKER = """IDENTIFICATION DIVISION.
PROGRAM-ID. SECONDARY.
PROCEDURE DIVISION USING REMAINING.
IF REBATE <= REMAINING
 SUBTRACT REBATE FROM REMAINING
END-IF.
GOBACK."""


def page(text, name="entry.cbl", start=1, revision="one"):
    # Deterministic IDs distinguish overlapping ranges of the same source.
    rows = text.splitlines()
    key = hashlib.sha256((name + revision + text).encode()).hexdigest()[:24]
    return {"id": "E" + key, "citation": {"path": name, "start": start, "end": start + len(rows) - 1,
            "revision": revision, "commit": "a" * 40, "hash": hashlib.sha256(text.encode()).hexdigest()},
            "text": text, "complete_line_count": len(rows), "truncated": False, "last_line_truncated": False}


class GroundedFindingsTests(unittest.TestCase):
    def test_same_order_when_retrieval_finds_worker_first_and_pages_overlap(self):
        entry, worker = page(ENTRY), page(WORKER, "other.cob")
        result = grounded_sequence_answer(QUESTION, [worker, entry, entry], "zh-CN")
        self.assertIsNotNone(result)
        self.assertEqual(result["citations"], [entry["id"], worker["id"]])
        text = result["answer"]
        self.assertLess(text.index("从 `OPEN-BILL` 中减去 `CREDIT-VALUE`"), text.index("将当前的 `OPEN-BILL` 传给"))
        self.assertLess(text.index("将当前的 `OPEN-BILL` 传给"), text.index("从 `REMAINING` 中减去"))
        self.assertIn("`OPEN-BILL` 小于或等于 `4`，跳过整个子程序", text)
        self.assertIn("`REBATE` 小于或等于 `REMAINING`", text)
        self.assertIn("`REBATE` 大于 `REMAINING`，跳过这笔扣减", text)
        self.assertIn("若所有扣减均跳过，则保留进入时的金额", text)

    def test_preview_can_report_one_branch_but_not_final_result_or_cross_file_order(self):
        worker = page(WORKER, "worker.cbl")
        result = grounded_preview(QUESTION, [worker], "en")
        self.assertEqual(result["citations"], [worker["id"]])
        self.assertIn("is at most", result["answer"])
        self.assertNotIn("PROCESSOR", result["answer"])
        self.assertNotIn("returned", result["answer"])
        self.assertIsNone(grounded_sequence_answer(QUESTION, [worker], "en"))
        self.assertIn("小於或等於", grounded_preview(QUESTION, [worker], "zh-HK")["answer"])

    def test_unrelated_question_or_extra_requirements_cannot_be_replaced_by_the_flow(self):
        pages = [page(ENTRY), page(WORKER, "worker.cbl")]
        for question in [QUESTION.replace("credit 和 rebate", "tax 和 penalty"),
                         QUESTION.replace("rebate", "rebate 和罚款"), QUESTION + "并给出表格。",
                         QUESTION + "本金100时是多少？", QUESTION + "失败时会怎样？",
                         "只看 other.cbl，credit 和 rebate 的扣减顺序是什么？"]:
            with self.subTest(question=question):
                self.assertIsNone(grounded_sequence_answer(question, pages, "zh-CN"))
        self.assertIsNone(grounded_preview("tax 和 penalty 的扣款次序是什么？", pages, "zh-CN"))

    def test_topic_substrings_cannot_promote_an_unrelated_deduction_to_a_complete_answer(self):
        entry = ENTRY.replace("CREDIT-VALUE", "ALLOWANCE").replace("'CREDIT'", "'1-RESERVECREDIT'")
        worker = WORKER.replace("REBATE", "POOL")
        entry = entry.replace("SECONDARY", "REDEEMREFUND")
        worker = worker.replace("SECONDARY", "REDEEMREFUND")
        pages = [page(entry), page(worker, "worker.cbl")]
        question = "保费收取时 reserve 和 refund 的扣减顺序及适用条件是什么？"
        self.assertIsNotNone(grounded_sequence_answer(question, pages, "zh-CN"))
        for source, fragment in [("refund", "fund"), ("reserve", "serv")]:
            with self.subTest(fragment=fragment):
                self.assertIsNone(grounded_sequence_answer(question.replace(source, fragment), pages, "zh-CN"))
        self.assertIsNone(grounded_preview("fund 的扣款条件是什么？", pages, "zh-CN"))

    def test_external_name_or_conditional_compilation_prevents_static_call_closure(self):
        for worker in [WORKER.replace("SECONDARY.", 'SECONDARY AS "OTHER-ENTRY".'),
                       "$IF DEFINED(ENABLE-FLOW)\n" + WORKER + "\nEND PROGRAM SECONDARY.\n$END"]:
            with self.subTest(worker=worker):
                self.assertIsNone(grounded_sequence_answer(QUESTION, [page(ENTRY), page(worker, "worker.cbl")], "zh-CN"))

    def test_unresolved_or_uncertain_binding_preserves_the_need_for_investigation(self):
        entry, worker = page(ENTRY), page(WORKER, "worker.cbl")
        for pages in [[entry, worker, page("DISPLAY 'secondary'.", "third.cbl")],
                      [entry, worker, page(WORKER + "\nDISPLAY 'maybe'.", "alternate.cob")],
                      [entry, page(WORKER, "worker.cbl", revision="two")],
                      [entry, worker, page(WORKER, "worker.cbl", revision="two")],
                      [page(ENTRY.replace("'SECONDARY'", "TARGET-PROGRAM")), worker],
                      [page(ENTRY.replace("USING OPEN-BILL", "USING BY CONTENT OPEN-BILL")), worker],
                      [entry, page(WORKER.replace("USING REMAINING", "USING BY VALUE REMAINING"), "worker.cbl")],
                      [entry, page(WORKER.replace("GOBACK.", "CONTINUE."), "worker.cbl")],
                      [entry, page(WORKER.replace("SUBTRACT REBATE FROM REMAINING", "SUBTRACT REBATE FROM OTHER-DUE"), "worker.cbl")],
                      [entry, page(WORKER.replace("GOBACK.", "MOVE 0 TO REMAINING.\nGOBACK."), "worker.cbl")]]:
            with self.subTest(pages=[p["text"][-60:] for p in pages]):
                self.assertIsNone(grounded_sequence_answer(QUESTION, pages, "zh-CN"))

    def test_no_findings_from_clipped_control_compiler_directives_or_display_only(self):
        worker = page(WORKER)
        clipped = {**worker, "truncated": True}
        bad = [clipped, {**worker, "last_line_truncated": True}, page(WORKER, start=9),
               page(WORKER.replace("IF REBATE", ">>IF FLAG\nIF REBATE")),
               page(WORKER.replace(" SUBTRACT REBATE FROM REMAINING", ' DISPLAY "SUBTRACT REBATE FROM REMAINING"'))]
        for original in bad:
            with self.subTest(original=original):
                self.assertIsNone(grounded_preview(QUESTION, [original], "zh-CN"))

    def test_regular_call_and_move_preview_needs_verifiable_matching_evidence(self):
        gate = ENTRY.replace("SUBTRACT CREDIT-VALUE FROM OPEN-BILL", 'CALL "CHECKROW"\nELSE\n DISPLAY "IGNORED"')
        result = grounded_preview("请说明 CHECKROW 的调用条件。", [page(gate)], "zh-CN")
        self.assertIn("发起对 `CHECKROW` 的调用", result["answer"])
        self.assertNotIn("已成功", result["answer"])
        change = WORKER.replace("SUBTRACT REBATE FROM REMAINING", "MOVE 'Y' TO CREDIT-FLAG")
        result = grounded_preview("When is CREDIT-FLAG set?", [page(change)], "en")
        self.assertIn("set `CREDIT-FLAG` to `'Y'`", result["answer"])


if __name__ == "__main__":
    unittest.main()
