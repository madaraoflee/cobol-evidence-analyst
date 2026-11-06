"""Indexed candidates, runtime bindings and bounded source delivery differ."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_policy import AgentPolicy
from business_chat import run_business_chat
from business_index import build_business_index
from business_map import build_business_map
from company_api import APIClientError, CompanyAPIConfig
from question_investigation import _dependencies, _framework_dependencies, build_question_investigation
import repository_discovery as retrieval
from source_session import QuestionSourceSession


class DependencySourceAvailabilityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.database = self.root / 'index.sqlite'
        self.files = {}

    def write(self, path, name, body, data='01 AMOUNT PIC 9(6).'):
        text = (f'IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n'
                f'WORKING-STORAGE SECTION.\n{data}\nPROCEDURE DIVISION.\nMAIN.\n{body}\nGOBACK.\n')
        self.raw(path, text)

    def raw(self, path, text):
        target = self.source / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding='utf-8')
        self.files[path] = text

    def build(self):
        build_business_index(self.source, self.database, source_format='free', quiet=True,
                             verify_content=True, framework_reference_path='')
        retrieval.ensure_repository_search(self.database, self.source)

    def read(self, session=None):
        return retrieval.read_repository_context(self.database, self.source,
            relative_path='z-main.cbl', source_session=session)

    def duplicate_targets(self):
        self.write('z-main.cbl', 'ORDERFLOW', 'CALL "VIEWREAD" USING AMOUNT.')
        self.write('current/view.cbl', 'VIEWREAD', 'MOVE 4 TO AMOUNT.')
        self.write('archive/view.cbl', 'VIEWREAD', 'MOVE 8 TO AMOUNT.')
        self.build()

    def test_incoming_hub_does_not_displace_outgoing_subroutine(self):
        self.write('z-main.cbl', 'ORDERFLOW', 'CALL "VIEWREAD" USING AMOUNT.')
        self.write('view.cbl', 'VIEWREAD', 'CALL "CHECKROW" USING AMOUNT.')
        self.write('check.cbl', 'CHECKROW', 'MOVE 9 TO AMOUNT.')
        for index in range(70):
            self.write(f'a-caller-{index:03}.cbl', f'UPSTREAM-{index:03}', 'CALL "ORDERFLOW".')
        self.build()
        result = self.read()
        first = result['call_chain']['links'][0]
        self.assertEqual((first['caller_path'], first['target_path']), ('z-main.cbl', 'view.cbl'))
        self.assertTrue(result['call_chain']['truncated'])
        searched = retrieval.retrieve_repository_context(self.database, self.source, 'ORDERFLOW', max_pages=6)
        self.assertIn('view.cbl', searched['selected_paths'])

    def test_indexed_duplicate_implementations_are_reading_candidates_not_missing(self):
        self.duplicate_targets()
        with QuestionSourceSession(self.database, self.source) as session:
            link = self.read(session)['call_chain']['links'][0]
            self.assertEqual(link['resolution'], 'candidate')
            self.assertIsNone(link['target_path'])
            self.assertEqual(link['target_source_status'], 'candidate_sources_indexed')
            self.assertEqual(link['target_source_status_scope'], 'indexed_sources')
            self.assertEqual({row['relative_path'] for row in link['target_source_candidates']},
                             {'current/view.cbl', 'archive/view.cbl'})
            self.assertFalse(link['runtime_verified'])
            link['target_source_candidates'][0]['relative_path'] = 'mutated.cbl'
            repeat = self.read(session)['call_chain']['links'][0]
            self.assertNotIn('mutated.cbl', {row['relative_path'] for row in repeat['target_source_candidates']})

    def test_dynamic_field_binding_does_not_assert_missing_or_same_named_program(self):
        self.write('z-main.cbl', 'ORDERFLOW', 'CALL SERVICE-NAME USING AMOUNT.',
                   '01 SERVICE-NAME PIC X(8) VALUE "CHECKROW".\n01 AMOUNT PIC 9(6).')
        self.write('same.cbl', 'SERVICE-NAME', 'MOVE 8 TO AMOUNT.')
        self.write('check.cbl', 'CHECKROW', 'MOVE 9 TO AMOUNT.')
        self.build()
        link = self.read()['call_chain']['links'][0]
        self.assertEqual(link['resolution'], 'unresolved')
        self.assertEqual(link['target_source_status'], 'dynamic_target_unresolved')
        self.assertIsNone(link['target_path'])
        self.assertNotIn('target_source_candidates', link)
        rows, _ = _dependencies(self.database, ['z-main.cbl'], [], {}, business_steps=True)
        self.assertEqual(rows[0]['unresolved_reason'], 'runtime_target_unresolved')
        self.assertIsNone(rows[0]['source_available'])

    def test_dependency_checklist_preserves_binding_gap_and_candidate_paths(self):
        self.duplicate_targets()
        question = 'ORDERFLOW 的完整计算公式'
        page = self.read()['pages'][0]
        mapping = build_business_map(self.database, self.source, question)
        result = build_question_investigation(question, mapping, database_path=self.database,
                                              source_pages=[page])
        item = next(row for row in result['required_items'] if row['kind'] == 'dependencies')
        self.assertEqual(item['status'], 'UNRESOLVED')
        self.assertEqual(item['reason'], 'dependency_binding_unresolved')
        self.assertEqual(set(item['indexed_candidate_paths']), {'current/view.cbl', 'archive/view.cbl'})
        self.assertNotIn('external_implementation_unavailable', item['unresolved_reasons'])
        rows, _ = _dependencies(self.database, ['z-main.cbl'], [], {}, business_steps=True)
        self.assertTrue(rows[0]['source_available'])
        # A documented external-operation contract cannot choose one local
        # implementation from duplicate definitions.
        fact = {**rows[0], 'kind': 'framework_operation', 'dependency_covered': True,
                'interpretation_basis': 'documented_framework_rule', 'runtime_verified': False,
                'fact_id': 'neutral-contract', 'source_evidence_ids': [page['evidence_id']],
                'reference_ids': ['neutral-reference']}
        self.assertTrue(_framework_dependencies(rows, [page], [fact])[0]['unresolved'])

    def test_missing_target_is_only_absent_from_the_index(self):
        self.write('z-main.cbl', 'ORDERFLOW', 'CALL "EXTERNAL-WORK" USING AMOUNT.')
        self.build()
        link = self.read()['call_chain']['links'][0]
        self.assertEqual(link['target_source_status'], 'unavailable')
        self.assertEqual(link['target_source_status_scope'], 'indexed_sources')
        rows, _ = _dependencies(self.database, ['z-main.cbl'], [], {}, business_steps=True)
        self.assertIs(rows[0]['source_available'], False)
        self.assertEqual(rows[0]['unresolved_reason'], 'external_implementation_unavailable')

    def test_duplicate_candidate_projection_is_bounded(self):
        self.write('z-main.cbl', 'ORDERFLOW', 'CALL "VIEWREAD".')
        for index in range(12):
            self.write(f'variant-{index}/view.cbl', 'VIEWREAD', f'MOVE {index} TO AMOUNT.')
        self.build()
        link = self.read()['call_chain']['links'][0]
        self.assertEqual(len(link['target_source_candidates']), 8)
        self.assertTrue(link['target_source_candidates_truncated'])
        self.assertIsNone(link['target_path'])

    def test_actual_request_supplies_seven_hops_copy_and_logical_file_definition(self):
        self.raw('z-main.cbl', 'IDENTIFICATION DIVISION.\nPROGRAM-ID. ORDERFLOW.\n'
            'ENVIRONMENT DIVISION.\nINPUT-OUTPUT SECTION.\nFILE-CONTROL.\n'
            'SELECT ACTIVE-RECORD ASSIGN TO "ACTIVEVIEW".\n'
            'DATA DIVISION.\nWORKING-STORAGE SECTION.\nCOPY REQUEST-CONTEXT.\n'
            'PROCEDURE DIVISION.\nMAIN.\nCALL "STEP-1" USING REQUEST-AMOUNT.\nGOBACK.\n')
        for level in range(1, 8):
            body = (f'CALL "STEP-{level + 1}" USING REQUEST-AMOUNT.' if level < 7 else
                    'IF REQUEST-AMOUNT > 50000\nDISPLAY "approval required before settlement"\nEND-IF.')
            self.write(f'step-{level}.cbl', f'STEP-{level}', body, '01 REQUEST-AMOUNT PIC 9(6).')
        self.raw('REQUEST-CONTEXT.cpy', '01 REQUEST-AMOUNT PIC 9(6).\n')
        self.raw('ACTIVEVIEW.lf', '     A          R ACTIVEREC PFILE(ACTIVEBASE)\n')
        self.raw('ACTIVEBASE.pf', '     A          R BASEREC\n     A            REQUEST-AMOUNT 6S 0\n')
        self.build()
        captured = []
        def transport(request):
            captured.append(json.loads(json.loads(request.body)['messages'][-1]['content']))
            raise APIClientError('OFFLINE_CAPTURE_ONLY')
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
            run_business_chat('请详细解释 ORDERFLOW 的业务流程、logical view 与 subroutine 调用和最终业务规则。',
                self.database, self.source,
                CompanyAPIConfig('https://offline.example.invalid/v1', 'neutral-model', api_key='offline-only'),
                transport=transport, allow_network=False, framework_reference_path='',
                policy=AgentPolicy(max_model_requests=1))
        self.assertEqual(len(captured), 1)
        pages = [page for bundle in captured[0]['source_context'] for page in bundle['pages']]
        supplied = {page['relative_path']: page for page in pages
                    if 'complete_working_set' in page.get('selection_reasons', [])}
        self.assertEqual(set(supplied), set(self.files))
        for path, page in supplied.items():
            self.assertEqual(page['source_text'], '\n'.join(self.files[path].splitlines()))
            self.assertEqual(page['source_sha256'], hashlib.sha256(self.files[path].encode()).hexdigest())
        self.assertIn('approval required before settlement', supplied['step-7.cbl']['source_text'])
        self.assertIn('PFILE(ACTIVEBASE)', supplied['ACTIVEVIEW.lf']['source_text'])
        metadata = captured[0]['source_context'][0]['working_set']
        self.assertTrue(metadata['physical_complete'])
        self.assertFalse(metadata['semantic_execution_verified'])
        self.assertFalse(metadata['closure_complete'], 'DDS names nominate source, not runtime binding')


if __name__ == '__main__':
    unittest.main()
