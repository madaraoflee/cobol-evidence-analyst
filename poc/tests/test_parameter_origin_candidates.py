"""Input-group layout and relation quotas must not invent missing source."""
from __future__ import annotations

from dataclasses import asdict
import copy
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_policy import AgentPolicy
from business_index import build_business_index
from business_map import build_business_map
from question_investigation import build_question_investigation, _flow_input_origins, _indexed_candidates
from repository_discovery import ensure_repository_search
from semantic_scope import build_business_evidence, prepare_semantic_scope
from source_session import QuestionSourceSession


class ParameterOriginCandidatesTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.db = self.root / 'facts.sqlite'
        self.path = 'offset.cbl'
        self.question = 'OFFSETPLAN 的 AMOUNT-DUE 如何依次抵扣？请解释计算顺序与输入来源。'
        self.write()

    def write(self, *, using=True, repetitions=3, unknown=False):
        declarations = '01 APPLIED-OFFSET PIC 9(7)V99.\n'
        if unknown:
            declarations += '01 UNKNOWN-AMOUNT PIC 9(7)V99.\n'
        lines = ['IDENTIFICATION DIVISION.', 'PROGRAM-ID. OFFSETPLAN.', 'DATA DIVISION.',
                 'WORKING-STORAGE SECTION.', declarations.rstrip(), 'LINKAGE SECTION.',
                 '01 REQUEST-RECORD.', '  05 AMOUNT-DUE PIC 9(7)V99.',
                 '  05 AVAILABLE-AMOUNT PIC 9(7)V99.',
                 'PROCEDURE DIVISION' + (' USING REQUEST-RECORD.' if using else '.'), 'MAIN.']
        for _ in range(repetitions):
            lines.extend(['IF AMOUNT-DUE > 0',
                          '  IF AVAILABLE-AMOUNT > AMOUNT-DUE',
                          '    MOVE AMOUNT-DUE TO APPLIED-OFFSET',
                          '  ELSE', '    MOVE AVAILABLE-AMOUNT TO APPLIED-OFFSET',
                          '  END-IF', '  SUBTRACT APPLIED-OFFSET FROM AMOUNT-DUE', 'END-IF.'])
        if unknown:
            lines.append('SUBTRACT UNKNOWN-AMOUNT FROM AMOUNT-DUE.')
        lines.append('GOBACK.')
        (self.source / self.path).write_text('\n'.join(lines) + '\n')
        build_business_index(self.source, self.db, source_format='free', verify_content=True)
        ensure_repository_search(self.db, self.source)

    def page(self):
        raw = (self.source / self.path).read_bytes()
        lines = raw.decode().splitlines()
        return {'relative_path': self.path, 'source_sha256': hashlib.sha256(raw).hexdigest(),
                'start_line': 1, 'end_line': len(lines), 'source_text': '\n'.join(lines),
                'evidence_id': 'offset-source', 'span_truncated': False}

    def investigate(self, pages=None, groups=()):
        return build_question_investigation(self.question,
            build_business_map(self.db, self.source, self.question), database_path=self.db,
            source_pages=pages if pages is not None else [self.page()], evidence_groups=groups)

    def test_supplied_using_group_fields_are_input_origins_and_not_a_dependency_cycle(self):
        result = self.investigate()
        item = next(i for i in result['required_items'] if i['kind'] == 'inputs')
        self.assertEqual(item['status'], 'SATISFIED', item)
        self.assertFalse(any(g['reason'] == 'input_dependency_cycle' for g in result['open_gaps']))

    def test_missing_signature_group_layout_or_wrong_version_remains_open(self):
        original = self.page()
        for label, needle in [('signature', 'PROCEDURE DIVISION'), ('group', '01 REQUEST-RECORD'),
                              ('field', '05 AMOUNT-DUE'), ('section', 'LINKAGE SECTION')]:
            with self.subTest(label=label):
                page = dict(original)
                page['source_text'] = '\n'.join('' if needle in line else line for line in original['source_text'].splitlines())
                result = self.investigate([page])
                self.assertFalse(result['can_answer'])
                self.assertTrue(any(g['kind'] == 'inputs' for g in result['open_gaps']))
        result = self.investigate([{**original, 'source_sha256': '0' * 64}])
        self.assertFalse(result['can_answer'])

    def test_declared_but_unpassed_group_and_unknown_working_storage_stay_open(self):
        self.write(using=False)
        result = self.investigate()
        self.assertTrue(any(g['reason'] == 'input_source_not_located' for g in result['open_gaps']))
        self.write(unknown=True)
        result = self.investigate()
        item = next(i for i in result['required_items'] if i['kind'] == 'inputs')
        self.assertEqual(item['reason'], 'input_source_not_located')
        self.assertIn('UNKNOWN-AMOUNT', item['fields'])

    def group(self, line=None):
        policy = AgentPolicy()
        page = self.page()
        if line is None:
            line = next(i for i, line in enumerate(page['source_text'].splitlines(), 1) if 'SUBTRACT' in line)
        anchor = {'relative_path': self.path, 'line': line}
        with QuestionSourceSession(self.db, self.source) as session:
            with prepare_semantic_scope(self.db, session, anchors=[anchor], policy=policy) as scope:
                return asdict(build_business_evidence(scope, session, anchor=anchor, policy=policy))

    def test_read_write_relations_share_one_statement_candidate_slot(self):
        group = self.group()
        gaps = [g for g in group['open_frontier'] if g['reason'] == 'same_symbol_candidates_limited']
        self.assertEqual(gaps, [])

    def test_more_than_twelve_distinct_statements_still_reports_candidate_limit(self):
        self.write(repetitions=4)
        gaps = [g for g in self.group()['open_frontier'] if g['reason'] == 'same_symbol_candidates_limited']
        self.assertTrue(gaps)
        self.assertTrue(all(g['candidate_count'] == 16 and g['omitted_count'] == 4 for g in gaps), gaps)

    def test_supplied_source_gets_missing_reader_inspection_and_closes_after_actual_inspection(self):
        first_group = self.group()
        before = self.investigate(groups=[first_group])
        inputs = next(i for i in before['required_items'] if i['kind'] == 'inputs')
        self.assertEqual(inputs['status'], 'PARTIAL', inputs)
        action = next(action for action in before['planned_actions']
                      if action['reason'] == 'input_origin_occurrence_not_inspected')
        self.assertEqual(action['arguments']['relative_path'], self.path)
        line = action['arguments']['line']
        self.assertIn('MOVE AMOUNT-DUE TO APPLIED-OFFSET', self.page()['source_text'].splitlines()[line - 1])
        after = self.investigate(groups=[first_group, self.group(line)])
        inputs = next(i for i in after['required_items'] if i['kind'] == 'inputs')
        self.assertEqual(inputs['status'], 'SATISFIED', inputs)
        self.assertFalse(after['planned_actions'])
        self.assertFalse(any(gap['kind'] == 'inputs' for gap in after['open_gaps']))
        self.assertTrue(all(witness['runtime_value_known'] is False
                            for witness in inputs['source_flow_origins']))

    def replace_source(self, text):
        (self.source / self.path).write_text(text)
        build_business_index(self.source, self.db, source_format='free', verify_content=True)
        ensure_repository_search(self.db, self.source)

    def test_returning_result_is_not_an_incoming_using_parameter(self):
        self.replace_source('''IDENTIFICATION DIVISION.
PROGRAM-ID. OUTPUTPLAN.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 TOTAL-AMOUNT PIC 9(7)V99.
LINKAGE SECTION.
01 INPUT-AMOUNT PIC 9(7)V99.
01 RESULT-AMOUNT PIC 9(7)V99.
PROCEDURE DIVISION USING INPUT-AMOUNT RETURNING RESULT-AMOUNT.
MAIN.
COMPUTE TOTAL-AMOUNT = INPUT-AMOUNT + RESULT-AMOUNT.
GOBACK.
END PROGRAM OUTPUTPLAN.
''')
        self.question = 'OUTPUTPLAN 的 TOTAL-AMOUNT 怎么计算，输入来自哪里？'
        result = self.investigate()
        item = next(i for i in result['required_items'] if i['kind'] == 'inputs')
        self.assertEqual(item['reason'], 'input_source_not_located', item)
        self.assertIn('RESULT-AMOUNT', item['fields'])
        self.assertNotIn('INPUT-AMOUNT', item['fields'])
        self.assertNotEqual(item['status'], 'SATISFIED')

    def test_using_in_another_program_cannot_supply_same_named_local_field(self):
        self.replace_source('''IDENTIFICATION DIVISION.
PROGRAM-ID. INPUTPLAN.
DATA DIVISION.
LINKAGE SECTION.
01 INPUT-AMOUNT PIC 9(7)V99.
PROCEDURE DIVISION USING INPUT-AMOUNT.
GOBACK.
END PROGRAM INPUTPLAN.
IDENTIFICATION DIVISION.
PROGRAM-ID. OUTPUTPLAN.
DATA DIVISION.
WORKING-STORAGE SECTION.
01 INPUT-AMOUNT PIC 9(7)V99.
01 TOTAL-AMOUNT PIC 9(7)V99.
PROCEDURE DIVISION.
MAIN.
COMPUTE TOTAL-AMOUNT = INPUT-AMOUNT + 1.
GOBACK.
END PROGRAM OUTPUTPLAN.
''')
        self.question = 'OUTPUTPLAN 的 TOTAL-AMOUNT 怎么计算，输入来自哪里？'
        result = self.investigate()
        item = next(i for i in result['required_items'] if i['kind'] == 'inputs')
        self.assertEqual(item['reason'], 'input_source_not_located', item)
        self.assertIn('INPUT-AMOUNT', item['fields'])
        self.assertNotEqual(item['status'], 'SATISFIED')

    def test_supplied_parameter_does_not_override_matched_unknown_call_effect(self):
        self.write(repetitions=1)
        text = (self.source / self.path).read_text().replace(
            'MAIN.\n', 'MAIN.\nCALL "EXTERNAL-RULE" USING AMOUNT-DUE.\n')
        self.replace_source('\n'.join(line.lstrip() for line in text.splitlines()) + '\n')
        result = self.investigate(groups=[self.group()])
        item = next(i for i in result['required_items'] if i['kind'] == 'inputs')
        witness = next(w for w in item['source_flow_origins'] if w['field'] == 'AMOUNT-DUE')
        self.assertEqual(witness['status'], 'source_origin_unresolved')
        self.assertIn('reaching_write_effect_not_bounded', witness['unknown_reasons'])
        self.assertFalse(witness['runtime_value_known'])
        self.assertFalse(witness['execution_verified'])
        self.assertIn('AMOUNT-DUE', item['fields'])
        self.assertTrue(any(g['reason'] == 'input_dependency_cycle' for g in result['open_gaps']))

    def test_indented_unknown_call_effect_still_overrides_parameter_origin(self):
        self.write(repetitions=1)
        self.replace_source((self.source / self.path).read_text().replace(
            'MAIN.\n', 'MAIN.\n  CALL "EXTERNAL-RULE" USING AMOUNT-DUE.\n'))
        result = self.investigate(groups=[self.group()])
        item = next(i for i in result['required_items'] if i['kind'] == 'inputs')
        witness = next(w for w in item['source_flow_origins'] if w['field'] == 'AMOUNT-DUE')
        self.assertEqual(witness['status'], 'source_origin_unresolved')
        self.assertIn('reaching_write_effect_not_bounded', witness['unknown_reasons'])
        self.assertIn('AMOUNT-DUE', item['fields'])

    def test_column_adjustment_does_not_match_a_different_statement_on_the_same_line(self):
        self.write(repetitions=1)
        group = self.group()
        candidates, _, _ = _indexed_candidates(self.db, [self.path], self.question, business_steps=True)
        target = next(row for row in candidates if row['statement'] == 'SUBTRACT APPLIED-OFFSET FROM AMOUNT-DUE')
        altered = copy.deepcopy(group)
        for assignment in altered['semantic_analysis']['assignments']:
            for proof in assignment.get('read_origins', []):
                for ref in proof.get('reader_source_refs', []):
                    if ref['start_line'] == target['start_line']:
                        ref['start_column'] += 8
        closed, invalidated, witnesses = _flow_input_origins([target], [self.page()], [altered])
        self.assertEqual((closed, invalidated, witnesses), (set(), set(), []))


if __name__ == '__main__':
    unittest.main()
