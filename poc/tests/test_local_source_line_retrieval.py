"""A search hit is actionable only when its complete original line can be supplied."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import ensure_repository_search, retrieve_repository_context
from source_reading import _identify_page
from source_session import QuestionSourceSession


class LocalSourceLineRetrievalTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name) / 'source'
        self.source.mkdir()
        self.database = self.source.parent / 'index.sqlite'
        values = "|".join(f"CODE{i:04d}" for i in range(800))
        self.line = f"DISPLAY 'POLICYRESULT|{values}'."
        (self.source / 'rule.cbl').write_text(
            'IDENTIFICATION DIVISION.\nPROGRAM-ID. RULEENGINE.\nPROCEDURE DIVISION.\n'
            + self.line + '\nGOBACK.\n', encoding='utf-8')
        (self.source / 'check.cbl').write_text(
            "IDENTIFICATION DIVISION.\nPROGRAM-ID. FALLBACKCHECK.\nPROCEDURE DIVISION.\n"
            "DISPLAY 'ALTERNATECODE'.\nGOBACK.\n", encoding='utf-8')
        build_business_index(self.source, self.database, source_format='free', verify_content=True)
        ensure_repository_search(self.database, self.source)

    def ask(self, policy=None):
        requests = []

        def respond(request):
            wire = json.loads(json.loads(request.body)['messages'][-1]['content'])
            requests.append(wire)
            pages = [p for part in wire.get('source_context', []) for p in part.get('pages', [])]
            if wire['response_mode'] == 'source_discovery':
                answer = '{"search":["ALTERNATECODE"]}'
            elif pages:
                answer = f"The located source is in {pages[0]['relative_path']}. [{pages[0]['evidence_id']}]"
            else:
                answer = 'Unable to confirm from the available material.'
            return TransportResponse(200, json.dumps({'choices': [{'message': {
                'role': 'assistant', 'content': answer}, 'finish_reason': 'stop'}]}))

        run_business_chat('POLICYRESULT', self.database, self.source,
                          CompanyAPIConfig('https://neutral.example.invalid/v1', 'offline-model', api_key='offline'),
                          framework_reference_path='', policy=policy, transport=respond)
        return requests

    def test_normal_question_supplies_complete_hashed_matching_line_to_model(self):
        first = self.ask()[0]
        self.assertTrue(first['source_selection']['source_supplied'])
        page, = [p for part in first['source_context'] for p in part['pages'] if 'POLICYRESULT' in p['source_text']]
        self.assertIn(self.line, page['source_text'].splitlines())
        self.assertLessEqual(page['start_line'], 4)
        self.assertGreaterEqual(page['end_line'], 4)
        self.assertEqual(first['response_mode'], 'business_analysis')
        with QuestionSourceSession(self.database, self.source) as session:
            result = retrieve_repository_context(self.database, self.source, 'POLICYRESULT', source_session=session)
        original, = result['pages']
        self.assertEqual(original['source_text'], self.line)
        self.assertEqual((original['start_line'], original['end_line']), (4, 4))
        self.assertEqual(original['evidence_id'], _identify_page(original))
        self.assertEqual(original['source_verification'], 'content_hash')

    def test_line_over_total_allowance_keeps_discovery_open_and_returns_read_location(self):
        limit = 4096
        result = retrieve_repository_context(self.database, self.source, 'POLICYRESULT', max_chars=limit)
        self.assertFalse(result['pages'])
        self.assertEqual(result['matched_file_count'], 1)
        gap, = [row for row in result['boundaries'] if row['reason'] == 'matching_line_exceeds_remaining_budget']
        self.assertEqual((gap['relative_path'], gap['start_line'], gap['required_characters'], gap['remaining_characters']),
                         ('rule.cbl', 4, len(self.line), limit))
        requests = self.ask({'initial_source_characters': limit, 'search_source_characters': limit,
                             'max_source_characters': limit, 'max_model_requests': 3})
        self.assertEqual(requests[0]['response_mode'], 'source_discovery')
        self.assertFalse(requests[0]['source_selection']['source_supplied'])
        self.assertTrue(any('ALTERNATECODE' in p['source_text'] for part in requests[1]['source_context']
                            for p in part.get('pages', [])))

    def test_small_independent_hit_keeps_its_slot_before_spending_remaining_space(self):
        budget = len(self.line) + 512
        result = retrieve_repository_context(self.database, self.source, 'explain', max_pages=2,
                    max_chars=budget, search_terms=['POLICYRESULT', 'ALTERNATECODE'])
        self.assertEqual(set(result['selected_paths']), {'rule.cbl', 'check.cbl'})
        self.assertLessEqual(result['coverage']['selected_characters'], budget)
        self.assertTrue(any(p['source_text'] == self.line for p in result['pages']))
        limited = retrieve_repository_context(self.database, self.source, 'explain', max_pages=2,
                    max_chars=len(self.line), search_terms=['POLICYRESULT', 'ALTERNATECODE'])
        self.assertEqual(limited['selected_paths'], ['check.cbl'])
        self.assertTrue(any(row['reason'] == 'matching_line_exceeds_remaining_budget'
                            for row in limited['boundaries']))
