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


    def capture_dynamic(self):
        captured = []
        def transport(request):
            captured.append(json.loads(json.loads(request.body)['messages'][-1]['content']))
            raise APIClientError('OFFLINE_CAPTURE_ONLY')
        with patch.object(socket.socket, 'connect', side_effect=AssertionError('network forbidden')):
            run_business_chat('请详细解释 ORDERFLOW 的业务流程与 subroutine 最终处理规则。',
                self.database, self.source,
                CompanyAPIConfig('https://offline.example.invalid/v1', 'neutral-model', api_key='offline-only'),
                transport=transport, allow_network=False, framework_reference_path='',
                policy=AgentPolicy(max_model_requests=1))
        self.assertEqual(len(captured), 1)
        payload = captured[0]
        pages = [page for bundle in payload['source_context'] for page in bundle['pages']]
        return payload, {page['relative_path']: page for page in pages}

    def dynamic_entry(self, body='CALL SERVICE-NAME USING AMOUNT.', initial='CHECKROW'):
        self.write('z-main.cbl', 'ORDERFLOW', body,
            f'01 SERVICE-NAME PIC X(16) VALUE "{initial}".\n01 AMOUNT PIC 9(6).')

    def test_dynamic_initial_value_supplies_first_request_without_model_search(self):
        self.dynamic_entry()
        self.write('check.cbl', 'CHECKROW',
                   'IF AMOUNT > 50000\nDISPLAY "approval before settlement"\nEND-IF.')
        self.build()
        payload, pages = self.capture_dynamic()
        self.assertIn('approval before settlement', pages['check.cbl']['source_text'])
        self.assertEqual(pages['check.cbl']['source_text'], self.files['check.cbl'].rstrip('\n'))
        working = payload['source_context'][0]['working_set']
        self.assertFalse(working['closure_complete'])
        gap = next(row for row in working['frontier'] if row['reason'] == 'runtime_target_unresolved')
        self.assertEqual(gap['candidate_paths'], ['check.cbl'])
        self.assertTrue(gap['source_available'])
        self.assertFalse(gap['runtime_target_resolved'])
        candidate = working['dynamic_target_candidates'][0]
        self.assertFalse(candidate['runtime_binding_verified'])
        self.assertEqual(candidate['source_provenance'][0]['kind'], 'initial_value')
        self.assertEqual(candidate['source_provenance'][0]['source_sha256'],
                         hashlib.sha256(self.files['z-main.cbl'].encode()).hexdigest())
        edge = next(row for row in payload['business_map']['relations']
                    if row['relation_type'] == 'CALL_TARGET_FROM')
        self.assertEqual(edge['resolution'], 'unresolved')
        self.assertEqual(edge['target_field_resolution'], 'confirmed')
        self.assertIsNone(edge['target_path'])

    def test_accept_and_branch_assignments_supply_candidates_without_binding(self):
        self.dynamic_entry('ACCEPT SERVICE-NAME.\nIF AMOUNT > 10\n'
            'MOVE "ALTERNATE" TO SERVICE-NAME\nEND-IF.\nCALL SERVICE-NAME USING AMOUNT.')
        self.write('check.cbl', 'CHECKROW', 'MOVE 4 TO AMOUNT.')
        self.write('alternate.cbl', 'ALTERNATE', 'MOVE 8 TO AMOUNT.')
        self.build()
        payload, pages = self.capture_dynamic()
        self.assertTrue({'check.cbl', 'alternate.cbl'} <= pages.keys())
        gap = next(row for row in payload['source_context'][0]['working_set']['frontier']
                   if row['reason'] == 'runtime_target_unresolved')
        self.assertEqual(set(gap['candidate_paths']), {'check.cbl', 'alternate.cbl'})
        self.assertFalse(gap['runtime_target_resolved'])
        self.assertEqual({origin['kind'] for row in payload['source_context'][0]['working_set']['dynamic_target_candidates']
                          for origin in row['source_provenance']}, {'initial_value', 'literal_assignment'})
        dependencies = next(row for row in payload['question_investigation']['required_items']
                            if row['kind'] == 'dependencies')
        self.assertEqual(dependencies['reason'], 'runtime_target_unresolved')

    def test_dynamic_nomination_rejects_unrelated_program_field_and_post_call_assignment(self):
        self.dynamic_entry('DISPLAY "MOVE BAD-DISPLAY TO SERVICE-NAME".\n'
            'CALL SERVICE-NAME USING AMOUNT.\nMOVE "AFTERCALL" TO SERVICE-NAME.', initial='NOTINDEXED')
        self.write('other.cbl', 'OTHER-FLOW', 'MOVE "OTHER-RULE" TO SERVICE-NAME.',
                   '01 SERVICE-NAME PIC X(16) VALUE "OTHER-RULE".')
        self.write('same-name.cbl', 'SERVICE-NAME', 'CONTINUE.')
        self.write('other-rule.cbl', 'OTHER-RULE', 'CONTINUE.')
        self.write('after.cbl', 'AFTERCALL', 'CONTINUE.')
        self.write('display.cbl', 'BAD-DISPLAY', 'CONTINUE.')
        self.build()
        link = next(row for row in self.read()['call_chain']['links'] if row['relation_type'] == 'CALL_TARGET_FROM')
        self.assertEqual(link['dynamic_target_candidates'], [])
        _, pages = self.capture_dynamic()
        self.assertEqual(set(pages), {'z-main.cbl'})

    def test_same_file_other_program_and_indirect_move_do_not_leak_literals(self):
        self.dynamic_entry('MOVE ROUTE-INPUT TO SERVICE-NAME.\nCALL SERVICE-NAME.', initial='NOTINDEXED')
        first = self.files['z-main.cbl'] + 'END PROGRAM ORDERFLOW.\n'
        second = ('IDENTIFICATION DIVISION.\nPROGRAM-ID. OTHER-FLOW.\nDATA DIVISION.\n'
            'WORKING-STORAGE SECTION.\n01 SERVICE-NAME PIC X(16) VALUE "CHECKROW".\n'
            'PROCEDURE DIVISION.\nMAIN.\nMOVE "CHECKROW" TO SERVICE-NAME.\nGOBACK.\n')
        self.raw('z-main.cbl', first + second)
        self.write('check.cbl', 'CHECKROW', 'CONTINUE.')
        self.build()
        link = next(row for row in self.read()['call_chain']['links'] if row['relation_type'] == 'CALL_TARGET_FROM')
        self.assertEqual(link['dynamic_target_candidates'], [])

    def test_dynamic_duplicate_implementations_are_both_read_without_choosing_one(self):
        self.dynamic_entry()
        self.write('current/check.cbl', 'CHECKROW', 'MOVE 4 TO AMOUNT.')
        self.write('archive/check.cbl', 'CHECKROW', 'MOVE 8 TO AMOUNT.')
        self.build()
        payload, pages = self.capture_dynamic()
        self.assertTrue({'current/check.cbl', 'archive/check.cbl'} <= pages.keys())
        gap = next(row for row in payload['source_context'][0]['working_set']['frontier']
                   if row['reason'] == 'runtime_target_unresolved')
        self.assertEqual(len(payload['source_context'][0]['working_set']['dynamic_target_candidates']), 2)
        self.assertFalse(gap['runtime_target_resolved'])
        self.assertTrue(all(not row['runtime_binding_verified'] for row in payload['source_context'][0]['working_set']['dynamic_target_candidates']))

    def test_dynamic_candidates_and_assignment_scan_have_explicit_limits(self):
        moves = '\n'.join(f'MOVE "WORK-{number:02}" TO SERVICE-NAME.' for number in range(40))
        self.dynamic_entry(moves + '\nCALL SERVICE-NAME.', initial='NOTINDEXED')
        for number in range(40):
            self.write(f'worker-{number:02}.cbl', f'WORK-{number:02}', 'CONTINUE.')
        self.build()
        link = next(row for row in self.read()['call_chain']['links'] if row['relation_type'] == 'CALL_TARGET_FROM')
        self.assertLessEqual(len(link['dynamic_target_candidates']), 8)
        self.assertTrue(link['dynamic_target_candidates_truncated'])
        self.assertFalse(link['runtime_target_resolved'])

    def test_dynamic_candidate_source_hash_still_rejects_changed_file(self):
        from complete_working_set import build_complete_working_set
        self.dynamic_entry()
        self.write('check.cbl', 'CHECKROW', 'MOVE 4 TO AMOUNT.')
        self.build()
        mapping = build_business_map(self.database, self.source, 'ORDERFLOW 业务流程')
        self.write('check.cbl', 'CHECKROW', 'MOVE 8 TO AMOUNT.')
        with QuestionSourceSession(self.database, self.source) as session:
            result = build_complete_working_set(self.database, session, mapping, AgentPolicy(), business_overview=True)
        self.assertTrue(result['needs_refresh'])
        self.assertFalse(any(page['relative_path'] == 'check.cbl' for page in result['pages']))


    def test_direct_literal_move_without_initial_value_supplies_candidate(self):
        self.write('z-main.cbl', 'ORDERFLOW', 'MOVE "CHECKROW" TO SERVICE-NAME.\nCALL SERVICE-NAME.',
                   '01 SERVICE-NAME PIC X(16).')
        self.write('check.cbl', 'CHECKROW', 'DISPLAY "review outcome".')
        self.build()
        payload, pages = self.capture_dynamic()
        self.assertIn('review outcome', pages['check.cbl']['source_text'])
        gap = next(row for row in payload['source_context'][0]['working_set']['frontier']
                   if row['reason'] == 'runtime_target_unresolved')
        self.assertEqual(payload['source_context'][0]['working_set']['dynamic_target_candidates'][0]['source_provenance'][0]['kind'], 'literal_assignment')

    def test_repeated_literal_provenance_is_bounded_without_copying_statements(self):
        self.dynamic_entry('\n'.join(['MOVE "CHECKROW" TO SERVICE-NAME.'] * 40 + ['CALL SERVICE-NAME.']))
        self.write('check.cbl', 'CHECKROW', 'CONTINUE.')
        self.build()
        link = next(row for row in self.read()['call_chain']['links'] if row['relation_type'] == 'CALL_TARGET_FROM')
        candidate = link['dynamic_target_candidates'][0]
        self.assertEqual(len(candidate['source_provenance']), 4)
        self.assertTrue(candidate['source_provenance_truncated'])
        self.assertEqual(candidate['source_provenance_omitted_count'], 29)
        self.assertTrue(link['dynamic_target_candidates_truncated'])
        self.assertFalse(any('statement' in row for row in candidate['source_provenance']))
        self.assertLess(len(json.dumps(candidate)), 2200)


    def test_many_dynamic_calls_share_provenance_and_keep_all_candidate_source(self):
        moves = '\n'.join(f'MOVE "STEP-{index % 8 + 1}" TO SERVICE-NAME.' for index in range(32))
        self.dynamic_entry(moves + '\n' + 'CALL SERVICE-NAME.\n' * 40, initial='STEP-1')
        for number in range(1, 9):
            self.write(f'step-{number}.cbl', f'STEP-{number}', f'DISPLAY "BUSINESS STEP {number}".')
        self.build()
        payload, pages = self.capture_dynamic()
        self.assertEqual(set(pages), {'z-main.cbl', *(f'step-{number}.cbl' for number in range(1, 9))})
        working = payload['source_context'][0]['working_set']
        self.assertEqual(len(working['dynamic_target_candidates']), 8)
        self.assertLess(len(json.dumps(working, ensure_ascii=False).encode()), 40000)
        self.assertLess(len(json.dumps(payload, ensure_ascii=False).encode()), AgentPolicy().max_request_bytes)
        candidates = {row['candidate_id'] for row in working['dynamic_target_candidates']}
        gaps = [row for row in working['frontier'] if row['reason'] == 'runtime_target_unresolved']
        self.assertTrue(gaps)
        self.assertTrue(all(set(row['dynamic_target_candidate_ids']) <= candidates for row in gaps))
        self.assertTrue(all(not row['runtime_target_resolved'] for row in gaps))
        for number in range(1, 9):
            self.assertIn(f'BUSINESS STEP {number}', pages[f'step-{number}.cbl']['source_text'])


    def test_far_literal_assignment_survives_many_unrelated_statements(self):
        self.write('z-main.cbl', 'ORDERFLOW',
            'MOVE "CHECKROW" TO SERVICE-NAME.\n' + 'MOVE 1 TO AMOUNT.\n' * 1500 + 'CALL SERVICE-NAME.',
            '01 SERVICE-NAME PIC X(16).\n01 AMOUNT PIC 9(6).')
        self.write('check.cbl', 'CHECKROW', 'DISPLAY "review outcome".')
        self.build()
        _, pages = self.capture_dynamic()
        self.assertIn('review outcome', pages['check.cbl']['source_text'])

    def test_field_assignment_window_reuses_reads_and_invalidates_on_new_move(self):
        from dependency_sources import dynamic_target_source_candidates
        self.dynamic_entry('MOVE "CHECKROW" TO SERVICE-NAME.\nCALL SERVICE-NAME.\n'
            'DISPLAY "no write".\nCALL SERVICE-NAME.\nMOVE "ALTERNATE" TO SERVICE-NAME.\nCALL SERVICE-NAME.')
        self.write('check.cbl', 'CHECKROW', 'CONTINUE.')
        self.write('alternate.cbl', 'ALTERNATE', 'CONTINUE.')
        self.build()
        with closing(sqlite3.connect(self.database)) as db:
            db.row_factory = sqlite3.Row
            calls = db.execute("SELECT r.relation_id FROM relations r JOIN evidence_spans e USING(evidence_id) "
                "WHERE r.relation_type='CALL_TARGET_FROM' ORDER BY e.start_line").fetchall()
            statements = []
            db.set_trace_callback(statements.append)
            cache = {}
            first, second, third = [dynamic_target_source_candidates(db, row['relation_id'], candidate_cache=cache)
                                    for row in calls]
        scans = [statement for statement in statements if 'ORDER BY o.start_line DESC,o.start_column DESC LIMIT' in statement]
        self.assertEqual(len(scans), 2)
        self.assertEqual(first['dynamic_target_candidates'], second['dynamic_target_candidates'])
        self.assertEqual({row['relative_path'] for row in third['dynamic_target_candidates']},
                         {'check.cbl', 'alternate.cbl'})


    def test_cached_multiline_move_window_matches_uncached_later_call(self):
        from dependency_sources import dynamic_target_source_candidates
        self.write('z-main.cbl', 'ORDERFLOW',
            'MOVE "CHECKROW"\nTO SERVICE-NAME.\nCALL SERVICE-NAME.\nCALL SERVICE-NAME.',
            '01 SERVICE-NAME PIC X(16).')
        self.write('check.cbl', 'CHECKROW', 'CONTINUE.')
        self.build()
        with closing(sqlite3.connect(self.database)) as db:
            db.row_factory = sqlite3.Row
            calls = db.execute("SELECT r.relation_id FROM relations r JOIN evidence_spans e USING(evidence_id) "
                "WHERE r.relation_type='CALL_TARGET_FROM' ORDER BY e.start_line").fetchall()
            self.assertEqual(len(calls), 2)
            # A source-fact span can end on the first call's physical line.
            # Exercise the saved-window boundary independently of parser
            # support for several executable statements on one source line.
            cutoff = db.execute("SELECT e.start_line FROM relations r JOIN evidence_spans e USING(evidence_id) "
                "WHERE r.relation_id=?", (calls[0]['relation_id'],)).fetchone()[0]
            db.execute("UPDATE business_rule_occurrences SET end_line=? WHERE rule_id IN "
                       "(SELECT rule_id FROM business_rules WHERE rule_kind='MOVE')", (cutoff,))
            cache = {}
            first = dynamic_target_source_candidates(db, calls[0]['relation_id'], candidate_cache=cache)
            second = dynamic_target_source_candidates(db, calls[1]['relation_id'], candidate_cache=cache)
            uncached = dynamic_target_source_candidates(db, calls[1]['relation_id'])
        self.assertEqual(first['dynamic_target_candidates'], [])
        self.assertEqual(second, uncached)
        self.assertEqual([row['relative_path'] for row in second['dynamic_target_candidates']], ['check.cbl'])


if __name__ == '__main__':
    unittest.main()
