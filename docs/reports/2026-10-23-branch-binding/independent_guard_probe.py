"""Independent offline branch-scope probes; synthetic strings only."""
import hashlib
import json
from pathlib import Path
import sys
import unittest
sys.path[:0] = [str(Path(__file__).resolve().parents[3] / 'poc')]
from claim_ledger import review_answer_claims


def program(name, body):
    return ('IDENTIFICATION DIVISION.\nPROGRAM-ID. ' + name + '.\n'
            'DATA DIVISION.\nWORKING-STORAGE SECTION.\n'
            '01 IN-AMOUNT PIC S99.\n01 OUT-AMOUNT PIC S999.\n'
            '01 ACTIVE-FLAG PIC 9.\n01 REVIEW-FLAG PIC 9.\n'
            'PROCEDURE DIVISION.\nMAIN.\n' + body + '\nGOBACK.\n')


def page(text, identifier='ev_rule', path='rules.cbl', first=1, digest=None):
    return {'evidence_id': identifier, 'relative_path': path, 'source_text': text,
            'source_sha256': digest or hashlib.sha256(text.encode()).hexdigest(),
            'start_line': first, 'end_line': first + len(text.splitlines()) - 1,
            'span_truncated': False}


BODY = 'IF IN-AMOUNT > ZERO\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2\nELSE\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 3\nEND-IF.'
SOURCE = program('AMOUNT-RULE', BODY)
RESULTS = []


class IndependentGuardProbes(unittest.TestCase):
    def review(self, text, pages=None, working_set=None):
        pages = pages or [page(SOURCE)]
        output, ledger = review_answer_claims(text, pages, {p['evidence_id']: p for p in pages},
            source_format='free', working_set=working_set)
        result = ledger.to_dict()
        RESULTS.append({'case': self._testMethodName, 'input': text, 'output': output,
                        'sources': pages, 'ledger': result})
        self.assertFalse(result['prose_semantics_verified'])
        return output, result

    def assertNoAssignmentSupport(self, ledger):
        self.assertFalse([r for r in ledger['entries'] if r['claim_type'] == 'assignment_statement'
                          and r['support_status'] == 'supported'])

    def test_separate_programs_cannot_combine_condition_and_assignment(self):
        first = program('ENTRY-RULE', 'IF IN-AMOUNT > ZERO\nMOVE 1 TO ACTIVE-FLAG\nEND-IF.')
        second = program('OTHER-RULE', 'IF IN-AMOUNT < ZERO\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 3\nEND-IF.')
        text = '在 IN-AMOUNT > ZERO 的条件下，OUT-AMOUNT = IN-AMOUNT * 3。[ev_first][ev_second]'
        _, ledger = self.review(text, [page(first, 'ev_first', 'first.cbl'), page(second, 'ev_second', 'second.cbl')])
        self.assertNoAssignmentSupport(ledger)

    def test_source_versions_cannot_combine_condition_and_assignment(self):
        first = program('ENTRY-RULE', 'IF IN-AMOUNT > ZERO\nMOVE 1 TO ACTIVE-FLAG\nEND-IF.')
        second = program('ENTRY-RULE', 'IF IN-AMOUNT < ZERO\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 3\nEND-IF.')
        text = '在 IN-AMOUNT > ZERO 的条件下，OUT-AMOUNT = IN-AMOUNT * 3。[ev_old][ev_new]'
        _, ledger = self.review(text, [page(first, 'ev_old'), page(second, 'ev_new')])
        self.assertNoAssignmentSupport(ledger)

    def test_two_programs_in_one_physical_file_cannot_share_guard(self):
        source = program('ENTRY-RULE', 'IF IN-AMOUNT > ZERO\nMOVE 1 TO ACTIVE-FLAG\nEND-IF.')
        source += 'END PROGRAM ENTRY-RULE.\n' + program('OTHER-RULE', 'IF IN-AMOUNT < ZERO\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 3\nEND-IF.')
        source += 'END PROGRAM OTHER-RULE.\n'
        text = '在 IN-AMOUNT > ZERO 的条件下，OUT-AMOUNT = IN-AMOUNT * 3。[ev_rule]'
        _, ledger = self.review(text, [page(source)])
        self.assertNoAssignmentSupport(ledger)

    def test_inner_guard_only_is_not_complete_nested_guard(self):
        source = program('NESTED-RULE', 'IF ACTIVE-FLAG = 1\nIF IN-AMOUNT > ZERO\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2\nEND-IF\nEND-IF.')
        text = '在 IN-AMOUNT > ZERO 的条件下，OUT-AMOUNT = IN-AMOUNT * 2。[ev_rule]'
        _, ledger = self.review(text, [page(source)])
        self.assertNoAssignmentSupport(ledger)

    def test_correct_chinese_two_branches_are_preserved(self):
        text = '在 IN-AMOUNT > ZERO 的条件下，OUT-AMOUNT = IN-AMOUNT * 2；否则 OUT-AMOUNT = IN-AMOUNT * 3。[ev_rule]'
        output, ledger = self.review(text)
        self.assertEqual(output, text)
        self.assertEqual(ledger['counts']['contradicted'], 0)

    def test_correct_english_two_branches_are_preserved(self):
        text = 'When IN-AMOUNT > ZERO, OUT-AMOUNT = IN-AMOUNT * 2; otherwise OUT-AMOUNT = IN-AMOUNT * 3.[ev_rule]'
        output, ledger = self.review(text)
        self.assertEqual(output, text)
        self.assertEqual(ledger['counts']['contradicted'], 0)

    def test_correct_table_is_preserved_without_cross_cell_guard(self):
        text = '| 条件 | 计算 |\n| --- | --- |\n| IN-AMOUNT > ZERO | OUT-AMOUNT = IN-AMOUNT * 2 [ev_rule] |\n| 其他情况 | OUT-AMOUNT = IN-AMOUNT * 3 [ev_rule] |'
        output, ledger = self.review(text)
        self.assertEqual(output, text)
        self.assertEqual(ledger['counts']['contradicted'], 0)

    def test_hypothetical_condition_does_not_turn_into_source_claim(self):
        text = '假设 IN-AMOUNT > ZERO，按这个输入例子，OUT-AMOUNT = IN-AMOUNT * 2。[ev_rule]'
        output, ledger = self.review(text)
        self.assertEqual(output, text)
        self.assertEqual(ledger['counts']['contradicted'], 0)

    def test_negated_bad_branch_is_not_repaired_as_positive_claim(self):
        text = '并不是在 IN-AMOUNT > ZERO 的条件下，OUT-AMOUNT = IN-AMOUNT * 3。[ev_rule]'
        output, ledger = self.review(text)
        self.assertEqual(output, text)
        self.assertEqual(ledger['counts']['contradicted'], 0)

    def test_unresolved_branch_is_not_repaired_as_positive_claim(self):
        text = '尚不能确定在 IN-AMOUNT > ZERO 的条件下，OUT-AMOUNT = IN-AMOUNT * 3。[ev_rule]'
        output, ledger = self.review(text)
        self.assertEqual(output, text)
        self.assertEqual(ledger['counts']['contradicted'], 0)

    def test_incomplete_source_does_not_prove_conditional_assignment(self):
        source = 'IF IN-AMOUNT > ZERO\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 2\n'
        text = '在 IN-AMOUNT > ZERO 的条件下，OUT-AMOUNT = IN-AMOUNT * 2。[ev_rule]'
        _, ledger = self.review(text, [page(source, first=30, digest=hashlib.sha256(SOURCE.encode()).hexdigest())])
        self.assertNoAssignmentSupport(ledger)

    def test_later_unconditional_overwrite_prevents_old_final_value(self):
        source = program('OVERWRITE-RULE', 'MOVE 2 TO OUT-AMOUNT.\nMOVE 7 TO OUT-AMOUNT.')
        text = 'OUT-AMOUNT 最终为 2。[ev_rule]'
        p = page(source)
        working = {'root_paths': ['rules.cbl'], 'source_manifest': [{'relative_path': 'rules.cbl', 'sha256': p['source_sha256'], 'line_count': len(source.splitlines())}]}
        output, ledger = self.review(text, [p], working)
        self.assertFalse(ledger['all_claims_supported'])
        self.assertEqual(ledger['counts']['supported'], 0)


if __name__ == '__main__':
    destination = Path(__file__).resolve().parent
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(IndependentGuardProbes))
    (destination / 'independent-guard-probe.json').write_text(json.dumps({'tests': result.testsRun,
        'failures': len(result.failures), 'errors': len(result.errors), 'cases': RESULTS}, ensure_ascii=False, indent=2)+'\n')
    sys.exit(not result.wasSuccessful())
