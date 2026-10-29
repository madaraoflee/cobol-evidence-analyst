"""Independent real-index checks for static parameter origins, never values."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_policy import AgentPolicy
from business_index import build_business_index
from question_investigation import _flow_input_origins
from repository_discovery import ensure_repository_search
from semantic_scope import build_business_evidence, prepare_semantic_scope
from source_session import QuestionSourceSession


class ParameterSourceOriginReviewTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.db = self.root / 'facts.sqlite'
        self.path = 'entry.cbl'

    def evidence(self, *, layout='01 REQUEST-DATA.\n  05 BASE-AMOUNT PIC 9(7)V99.',
                 using='USING REQUEST-DATA', before='', field='BASE-AMOUNT',
                 storage='LINKAGE', trailing=''):
        self.text = ('IDENTIFICATION DIVISION.\nPROGRAM-ID. INPUTPLAN.\nDATA DIVISION.\n'
            'WORKING-STORAGE SECTION.\n01 FINAL-AMOUNT PIC 9(7)V99.\n' +
            storage + ' SECTION.\n' + layout + '\nPROCEDURE DIVISION ' + using + '.\nMAIN.\n' +
            before + '  COMPUTE FINAL-AMOUNT = ' + field + ' * 2.\nGOBACK.\n' + trailing)
        (self.source / self.path).write_text(self.text)
        build_business_index(self.source, self.db, source_format='free', verify_content=True)
        ensure_repository_search(self.db, self.source)
        line = next(i for i, text in enumerate(self.text.splitlines(), 1) if 'COMPUTE' in text)
        policy = AgentPolicy()
        anchor = {'relative_path': self.path, 'line': line}
        with QuestionSourceSession(self.db, self.source) as session:
            with prepare_semantic_scope(self.db, session, anchors=[anchor], policy=policy) as scope:
                group = asdict(build_business_evidence(scope, session, anchor=anchor, policy=policy))
        candidate = {'kind': 'formula', 'relative_path': self.path,
            'source_sha256': self.page()['source_sha256'], 'start_line': line, 'end_line': line,
            'start_column': 1, 'statement': self.text.splitlines()[line - 1].strip(),
            'occurrence_count': 1, 'reads': [field], 'writes': ['FINAL-AMOUNT']}
        proofs = [proof for assignment in group.get('semantic_analysis', {}).get('assignments', [])
                  for proof in assignment.get('read_origins', [])
                  if proof['reader_statement'].startswith('COMPUTE FINAL-AMOUNT')]
        return group, candidate, proofs

    def page(self):
        return {'relative_path': self.path, 'source_sha256': hashlib.sha256(self.text.encode()).hexdigest(),
            'start_line': 1, 'end_line': len(self.text.splitlines()), 'source_text': self.text,
            'evidence_id': 'entry-source', 'span_truncated': False}

    def test_group_and_scalar_input_locate_source_without_claiming_value(self):
        for options in ({}, {'layout': '01 BASE-AMOUNT PIC 9(7)V99.', 'using': 'USING BASE-AMOUNT'}):
            with self.subTest(options=options):
                group, candidate, proofs = self.evidence(**options)
                self.assertEqual(len(proofs), 1)
                self.assertEqual(proofs[0]['status'], 'resolved_source_origin', proofs)
                self.assertFalse(proofs[0]['execution_verified'])
                origins = proofs[0]['initial_definitions']
                self.assertTrue(origins)
                self.assertTrue(all(o['status'] == 'declared_parameter_source' and o['value'] is None
                                    and o['runtime_value_known'] is False for o in origins), origins)
                closed, invalidated, witnesses = _flow_input_origins([candidate], [self.page()], [group])
                self.assertEqual(closed, {(self.path, 'BASE-AMOUNT')})
                self.assertEqual(invalidated, set())
                self.assertFalse(witnesses[0]['runtime_value_known'])

    def test_layout_identity_and_signature_must_actually_reach_the_answer(self):
        group, candidate, _ = self.evidence()
        original = self.page()
        for marker in ('PROGRAM-ID.', 'LINKAGE SECTION.', '01 REQUEST-DATA.',
                       '05 BASE-AMOUNT', 'PROCEDURE DIVISION'):
            with self.subTest(marker=marker):
                page = {**original, 'source_text': '\n'.join('' if marker in line else line
                        for line in original['source_text'].splitlines())}
                closed, invalidated, _ = _flow_input_origins([candidate], [page], [group])
                self.assertEqual(closed, set())
                self.assertIn((self.path, 'BASE-AMOUNT'), invalidated)
        closed, _, _ = _flow_input_origins([candidate], [{**original, 'source_sha256': '0' * 64}], [group])
        self.assertEqual(closed, set())

    def test_unpassed_return_only_wrong_storage_and_other_group_are_not_inputs(self):
        variants = {
            'unpassed': {'using': ''},
            'return_only': {'layout': '01 OTHER-AMOUNT PIC 9(7)V99.\n01 BASE-AMOUNT PIC 9(7)V99.',
                            'using': 'USING OTHER-AMOUNT RETURNING BASE-AMOUNT'},
            'working_storage': {'storage': 'WORKING-STORAGE'},
            'other_group': {'layout': '01 REQUEST-DATA.\n  05 BASE-AMOUNT PIC 9(7)V99.\n'
                                      '01 OTHER-GROUP.\n  05 BASE-AMOUNT PIC 9(7)V99.',
                            'field': 'BASE-AMOUNT OF OTHER-GROUP'},
        }
        for name, options in variants.items():
            with self.subTest(name=name):
                group, candidate, proofs = self.evidence(**options)
                self.assertTrue(proofs)
                self.assertTrue(all(p['status'] == 'unresolved_source_origin' for p in proofs), proofs)
                self.assertTrue(all(not p['initial_definitions'] for p in proofs), proofs)
                closed, _, _ = _flow_input_origins([candidate], [self.page()], [group])
                self.assertEqual(closed, set())

    def test_same_named_parameter_in_a_second_program_never_closes_first_program(self):
        trailing = ('END PROGRAM INPUTPLAN.\nIDENTIFICATION DIVISION.\nPROGRAM-ID. OTHERPLAN.\n'
            'DATA DIVISION.\nLINKAGE SECTION.\n01 REQUEST-DATA.\n05 BASE-AMOUNT PIC 9(7)V99.\n'
            'PROCEDURE DIVISION USING REQUEST-DATA.\nGOBACK.\nEND PROGRAM OTHERPLAN.\n')
        group, candidate, proofs = self.evidence(using='', trailing=trailing)
        self.assertTrue(proofs)
        self.assertTrue(all(p['status'] == 'unresolved_source_origin' for p in proofs), proofs)
        closed, _, _ = _flow_input_origins([candidate], [self.page()], [group])
        self.assertEqual(closed, set())

    def test_unknown_external_call_after_entry_keeps_origin_open(self):
        group, candidate, proofs = self.evidence(before='  CALL "EXTERNAL-RULE" USING BASE-AMOUNT.\n')
        self.assertTrue(proofs)
        self.assertTrue(all(p['status'] == 'unresolved_source_origin' for p in proofs), proofs)
        self.assertIn('reaching_write_effect_not_bounded', proofs[0]['unknown_reasons'])
        closed, invalidated, _ = _flow_input_origins([candidate], [self.page()], [group])
        self.assertEqual(closed, set())
        self.assertIn((self.path, 'BASE-AMOUNT'), invalidated)


if __name__ == '__main__':
    unittest.main()
