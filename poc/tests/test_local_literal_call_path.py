"""Only close a literal-selected call path when the source and the delivered explanation agree."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_grep_acceptance import DISPATCH, ENTRY, QUESTION, WORKER, choose_reply
import test_local_deduction_review as review_fixture
from local_literal_call_path import prove_literal_call_path


class LocalLiteralCallPathTests(unittest.TestCase):
    def setUp(self):
        self.originals = review_fixture.LocalDeductionReviewTests()
        self.addCleanup(self.originals.doCleanups)
        self.originals.setUp()

    def case(self, *, entry=ENTRY, route=DISPATCH, worker=WORKER, windows=None):
        self.originals.files.clear()
        sources = [entry, route, worker]
        names = ["start.cbl", "route.cbl", "settlement.cbl"]
        windows = windows or {}
        pages = [self.originals.page(name, text, *windows.get(name, ()))
                 for name, text in zip(names, sources)]
        # Keep the answer's references valid when mutating a file. Each case
        # must fail on its changed semantics, not on stale citation hashes.
        answer_pages = [dict(p, source_text=s) for p, s in zip(pages, (ENTRY, DISPATCH, WORKER))]
        answer = choose_reply(answer_pages, [], initial_deferral=False)
        self.assertIsInstance(answer, str)
        return pages, answer

    def proof(self, pages, answer, question=QUESTION):
        return prove_literal_call_path(question, pages, source_session=self.originals.session, answer=answer)

    def test_complete_source_and_qualified_three_file_answer_close_the_path(self):
        pages, answer = self.case()
        self.assertTrue(self.proof(pages, answer))

    def test_reassigned_target_wrong_parameter_or_nested_guard_leave_path_open(self):
        mutations = [
            {"entry": ENTRY.replace(" CALL REFUND-ROUTINE", ' MOVE "ALTERNATE" TO REFUND-ROUTINE\n CALL REFUND-ROUTINE')},
            {"route": DISPATCH.replace('CALL NEXT-ROUTINE', 'IF MODE-FLAG = "X"\n MOVE "ALTERNATE" TO NEXT-ROUTINE\nEND-IF.\nCALL NEXT-ROUTINE')},
            {"route": DISPATCH.replace("CALL NEXT-ROUTINE USING REMAINING-DUE", "CALL NEXT-ROUTINE USING OTHER-DUE")},
            {"worker": WORKER.replace("FROM REMAINING-DUE", "FROM OTHER-DUE")},
            {"entry": ENTRY.replace("IF NET-DUE > 0", 'IF MODE-FLAG = "X"\nIF NET-DUE > 0').replace("GOBACK.", "END-IF.\nGOBACK.")},
            {"worker": WORKER.replace("IF POOL-AMOUNT", 'IF MODE-FLAG = "X"\nIF POOL-AMOUNT').replace("GOBACK.", "END-IF.\nGOBACK.")},
        ]
        for change in mutations:
            with self.subTest(change=change):
                self.assertFalse(self.proof(*self.case(**change)))

    def test_duplicate_target_in_other_grep_hit_never_selects_one_implementation(self):
        # Even an uncited navigation read containing a competing PROGRAM-ID
        # matters when its body did not fit in the model's source window.
        for name, alternate in (("route", DISPATCH), ("worker", WORKER)):
            for end in (None, 2):
                with self.subTest(name=name, visible_end=end):
                    pages, answer = self.case()
                    alternative = alternate.replace("GOBACK.", "MOVE 0 TO REMAINING-DUE.\nGOBACK.")
                    extra = self.originals.page("alternative-" + name + ".cbl", alternative, last=end)
                    self.assertFalse(self.proof([*pages, extra], answer))

    def test_missing_or_truncated_conditions_and_operations_leave_path_open(self):
        for path, bounds in (("start.cbl", (1, 6)), ("route.cbl", (1, 4)), ("settlement.cbl", (5, 7))):
            with self.subTest(path=path):
                self.assertFalse(self.proof(*self.case(windows={path: bounds})))
        pages, answer = self.case()
        self.assertFalse(self.proof([*pages[:2], dict(pages[2], span_truncated=True)], answer))

    def test_separate_supplied_guard_and_debit_windows_use_the_actual_page_citations(self):
        whole, answer = self.case()
        for last_header in (4, 5):
            with self.subTest(overlap=last_header == 5):
                header = self.originals.page("settlement.cbl", WORKER, 1, last_header)
                debit = self.originals.page("settlement.cbl", WORKER, 5)
                draft = answer.replace(whole[-1]["evidence_id"], debit["evidence_id"])
                proof = self.proof([debit, *whole[:2], header], draft)
                self.assertIsNotNone(proof, "the supplied windows cover every original line")
                delivered = proof["answer"]
                rule = next(line for line in delivered.splitlines() if "POOL-AMOUNT" in line)
                for ref in (header, debit):
                    self.assertIn("[" + ref["evidence_id"] + "]", rule)
                self.assertNotIn(whole[-1]["evidence_id"], delivered)
                self.assertIn("仍剩 80", delivered)

    def test_unsupplied_stale_or_transformed_guard_does_not_close_the_split_path(self):
        whole, answer = self.case()
        before = self.originals.page("settlement.cbl", WORKER, 1, 3)
        debit = self.originals.page("settlement.cbl", WORKER, 5)
        guard = self.originals.page("settlement.cbl", WORKER, 4, 4)
        draft = answer.replace(whole[-1]["evidence_id"], debit["evidence_id"])
        for invalid in ([], [dict(guard, source_sha256="another-version")],
                        [dict(guard, source_text="IF POOL-AMOUNT <= REMAINING-DUE")],
                        [dict(guard, copy_instance="another-context")], [dict(guard, span_truncated=True)]):
            with self.subTest(invalid=invalid):
                self.assertFalse(self.proof([*whole[:2], before, *invalid, debit], draft))
        self.assertTrue(self.proof([*whole[:2], before, guard, debit], draft))
        bad_ref = dict(guard, source_verification="metadata")
        self.assertFalse(self.proof([*whole, bad_ref], answer + f' [{bad_ref["evidence_id"]}]'))

    def test_passing_by_value_or_content_cannot_establish_caller_receivable(self):
        for mode in ("VALUE", "CONTENT"):
            for path, text, call in (("entry", ENTRY, "CALL REFUND-ROUTINE USING"),
                                     ("route", DISPATCH, "CALL NEXT-ROUTINE USING")):
                with self.subTest(mode=mode, path=path):
                    value = text.replace(call, call + " BY " + mode)
                    self.assertFalse(self.proof(*self.case(**{path: value})))

    def test_answer_needs_all_decisions_skip_cases_final_amount_and_each_original_reference(self):
        pages, answer = self.case()
        essential = ["只有 ITEM-TYPE 等于 1-RESERVECREDIT 才从 NET-DUE 减去准备金 CREDIT-AMT。其他类型跳过准备金。",
                     "大于零才调用 ADJ731，否则跳过退款并保留当时应收",
                     "只有 POOL-AMOUNT 严格小于当前 REMAINING-DUE 才扣退款，相等或更大时跳过。",
                     "最终应收等于进入时的应收减去实际生效的扣减额，两步都跳过时保持原额。"]
        essential.extend("[" + p["evidence_id"] + "]" for p in pages)
        for clause in essential:
            with self.subTest(clause=clause):
                self.assertIn(clause, answer)
                self.assertFalse(self.proof(pages, answer.replace(clause, "")))
        self.assertFalse(self.proof(pages, answer.replace("严格小于", "小于或等于")))

    def test_delivered_answer_is_derived_from_source_despite_wrong_model_fields_targets_and_arithmetic(self):
        variants = ({}, {"CREDIT-AMT": "POLICY-CREDIT", "NET-DUE": "BILL-DUE",
                         "POOL-AMOUNT": "REFUND-POT", "REMAINING-DUE": "AMOUNT-LEFT",
                         "ADJ731": "ROUTE903", "CUT526": "DEC512"})
        for names in variants:
            with self.subTest(names=names):
                def renamed(text):
                    for old, new in names.items():
                        text = text.replace(old, new)
                    return text

                pages, model = self.case(entry=renamed(ENTRY), route=renamed(DISPATCH), worker=renamed(WORKER))
                correct = self.proof(pages, renamed(model))
                self.assertTrue(correct)
                wrong = model.replace("CREDIT-AMT", "UNRELATED-AMT").replace("才调用 ADJ731", "才调用 WRONG-PROGRAM")
                wrong = wrong.replace("从 NET-DUE 减去", "从 UNRELATED-DUE 减去").replace("剩 50", "剩 1000")
                wrong = wrong.replace("POOL-AMOUNT", "UNRELATED-POOL")
                corrected = self.proof(pages, renamed(wrong))
                self.assertTrue(corrected)
                # The delivered prose must explain the guard, the balance and the
                # subtracted operand together, as recorded in the selected source.
                text = corrected["answer"].replace("`", "")
                self.assertEqual(text, correct["answer"].replace("`", ""))
                for bad in ("UNRELATED-AMT", "UNRELATED-DUE", "UNRELATED-POOL", "WRONG-PROGRAM", "1000"):
                    self.assertNotIn(bad, text)
                self.assertRegex(text, renamed(r"从\s*NET-DUE\s*(?:中)?(?:扣除|扣减|减去)\s*CREDIT-AMT"))
                self.assertRegex(text, renamed(r"POOL-AMOUNT\s*(?:严格)?小于\s*REMAINING-DUE"))
                self.assertRegex(text, renamed(r"从\s*REMAINING-DUE\s*(?:中)?(?:扣除|扣减|减去)\s*POOL-AMOUNT"))
                self.assertRegex(text, renamed(r"CASHENTRY\s*将\s*ADJ731\s*赋给\s*REFUND-ROUTINE[^。]*按引用传递\s*NET-DUE"))
                self.assertRegex(text, renamed(r"ADJ731\s*将\s*CUT526\s*赋给\s*NEXT-ROUTINE[^。]*按引用传递\s*REMAINING-DUE"))
                self.assertRegex(text, renamed(r"POOL-AMOUNT\s*为\s*30[^。]*最终应收为\s*50[^。]*相等[^。]*仍剩\s*80"))
                self.assertRegex(text, renamed(r"最终[^。]*NET-DUE[^。]*(?:减去|扣减|扣除)"))
                for page in pages:
                    self.assertIn("[" + page["evidence_id"] + "]", text)

    def test_unrelated_question_and_upstream_amount_followup_stay_open(self):
        pages, answer = self.case()
        for question in ("这笔交易由哪个批处理写入数据库？",
                         QUESTION + "另请追溯准备金金额上游从哪个文件、哪个字段取值。"):
            with self.subTest(question=question):
                self.assertFalse(self.proof(pages, answer, question))
