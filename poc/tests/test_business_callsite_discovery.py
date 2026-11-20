"""Ordinary questions should follow a source call site beyond the file graph preview."""
from pathlib import Path
import json
import re
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


class BusinessCallsiteDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.source = Path(directory.name) / 'source'
        cls.source.mkdir()
        cls.db = cls.source.parent / 'index.sqlite'

        def program(path, name, body, data=(), arguments=''):
            lines = ['IDENTIFICATION DIVISION.', f'PROGRAM-ID. {name}.', *data,
                     f'PROCEDURE DIVISION{arguments}.', *body, 'GOBACK.']
            (cls.source / path).write_text('\n'.join(lines) + '\n', encoding='utf-8')

        checks = [f'CHECK{number:03d}' for number in range(75)]
        for name in checks:
            program(name.lower() + '.cbl', name, ['CONTINUE.'])
        program('notice.cbl', 'SCREENOUT', ["DISPLAY 'Premium collection'.", "DISPLAY 'Suspense'."])
        filler = [f'*> Other source text {n} ' + 'x' * 78 for n in range(160)]
        program('rules.cbl', 'PAYMENTS', ['PERFORM LOAD-CODES.', 'PERFORM APPLY-AMOUNT.', 'GOBACK.',
                'LOAD-CODES.', *[f"CALL '{name}'." for name in checks], *filler,
                'APPLY-AMOUNT.', "IF FUND-KIND = 'SUSPENSE'", "  CALL 'NETWORKER' USING NET-DUE CREDIT-AMT",
                'END-IF.', *filler, "IF FUND-KIND = 'REFUND'", "  CALL 'ADJWORKER' USING NET-DUE CREDIT-AMT", 'END-IF.'],
                data=['DATA DIVISION.', 'WORKING-STORAGE SECTION.', '01 FUND-KIND PIC X(16).',
                      '01 NET-DUE PIC 9(8).', '01 CREDIT-AMT PIC 9(8).'])
        for path, name, operation in [('late-a.cbl', 'NETWORKER', 'SUBTRACT LK-AMT FROM LK-DUE'),
                                      ('late-b.cbl', 'ADJWORKER', 'ADD LK-AMT TO LK-DUE')]:
            program(path, name, ['IF LK-AMT > ZERO', operation, 'END-IF.'], data=['DATA DIVISION.',
                    'LINKAGE SECTION.', '01 LK-DUE PIC 9(8).', '01 LK-AMT PIC 9(8).'],
                    arguments=' USING LK-DUE LK-AMT')
        build_business_index(cls.source, cls.db, source_format='free', verify_content=True, framework_reference_path='')
        ensure_repository_search(cls.db, cls.source)

    def test_ordinary_question_supplies_actual_callee_and_arithmetic_without_model_naming_a_path(self):
        requests = []

        def respond(request):
            payload = json.loads(json.loads(request.body)['messages'][-1]['content'])
            pages = [page for part in payload.get('source_context', []) for page in part['pages']]
            requests.append(pages)
            # Answer only from a visible condition, call/definition pair and arithmetic.
            # The responder receives no known path or program name from the fixture.
            answer = '目前资料无法确认。'
            for caller in pages:
                call = re.search(r"IF FUND-KIND = 'SUSPENSE'\s+CALL '([^']+)' USING", caller['source_text'])
                if call:
                    for target in pages:
                        if (f'PROGRAM-ID. {call[1]}.' in target['source_text']
                                and 'IF LK-AMT > ZERO' in target['source_text']
                                and 'SUBTRACT LK-AMT FROM LK-DUE' in target['source_text']):
                            answer = (f"对于 Suspense，只有可用金额大于零时才从应收余额扣除该金额。"
                                      f"[{caller['evidence_id']}][{target['evidence_id']}]")
            return TransportResponse(200, json.dumps({'choices': [{'message': {'role': 'assistant',
                'content': answer}, 'finish_reason': 'stop'}]}))

        output = run_business_chat('premium collection 扣款时，Suspense 在什么条件下参与，怎样计算？', self.db, self.source,
            CompanyAPIConfig('https://example.invalid/v1', 'offline', api_key='offline'), framework_reference_path='',
            allow_network=False, transport=respond)
        self.assertIn('只有可用金额大于零时才从应收余额扣除该金额', output['agent_result']['answer'])
        first = {p['relative_path']: p['source_text'] for p in requests[0]}
        self.assertIn("CALL 'NETWORKER' USING NET-DUE CREDIT-AMT", '\n'.join(p['source_text'] for p in requests[0]))
        self.assertIn('SUBTRACT LK-AMT FROM LK-DUE', first.get('late-a.cbl', ''))
        self.assertIn('PROCEDURE DIVISION USING LK-DUE LK-AMT', first['late-a.cbl'])

    def test_different_leads_in_the_same_large_caller_keep_distinct_targets_and_citable_ranges(self):
        with QuestionSourceSession(self.db, self.source) as session:
            for needle, file, target in [('Suspense', 'late-a.cbl', 'NETWORKER'), ('Refund', 'late-b.cbl', 'ADJWORKER')]:
                with self.subTest(needle=needle):
                    result = retrieve_repository_context(self.db, self.source, f'{needle} 的扣款条件是什么？',
                                                         source_session=session)
                    self.assertIn(file, result['selected_paths'])
                    link, = [item for item in result['call_chain']['links'] if item['target_name'] == target]
                    self.assertTrue(link['caller_evidence_ids'])
                    self.assertTrue(link['target_evidence_ids'])
                    self.assertFalse(link['runtime_verified'])
                    self.assertTrue(result['call_chain']['truncated'])
                    for page in result['pages']:
                        self.assertEqual(page['evidence_id'], _identify_page(page))

    def test_explicit_file_scope_never_fetches_the_outside_callee(self):
        result = retrieve_repository_context(self.db, self.source, '只分析 rules.cbl', search_terms=['suspense'])
        self.assertEqual(result['selected_paths'], ['rules.cbl'])
