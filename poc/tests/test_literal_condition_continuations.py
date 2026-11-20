"""Physical line wrapping must not let a display caption hide a business condition."""
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


class LiteralConditionContinuationTests(unittest.TestCase):
    def lookup(self, body, fmt, *, chat=False):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        source = root / 'source'
        source.mkdir()
        for path, name, statements in [('a-notice.cbl', 'NOTICE', ["DISPLAY 'PLANKEY'."]),
                                       ('z-rule.cbl', 'BILLING', body)]:
            lines = ['IDENTIFICATION DIVISION.', 'PROGRAM-ID. ' + name + '.', 'DATA DIVISION.',
                     'WORKING-STORAGE SECTION.', '01 PLAN-CODE PIC X(24).', '01 CREDIT-AMT PIC 9(8).',
                     '01 NET-DUE PIC 9(8).', 'PROCEDURE DIVISION.', *statements, 'GOBACK.']
            if fmt == 'fixed':
                lines = [f'{n:06d}' + (line if line.startswith('*') else ' ' + line) for n, line in enumerate(lines, 1)]
            (source / path).write_text('\n'.join(lines)+'\n', encoding='utf-8')
        db = root / 'index.sqlite'
        build_business_index(source, db, source_format=fmt, verify_content=True, framework_reference_path='')
        ensure_repository_search(db, source)
        question = 'PLANKEY 在什么条件下使用？'
        if not chat:
            return retrieve_repository_context(db, source, question, max_pages=1)
        requests = []
        def capture(request):
            requests.append(json.loads(json.loads(request.body)['messages'][-1]['content']))
            return TransportResponse(200, json.dumps({'choices':[{'message':{'role':'assistant','content':'Pending.'},'finish_reason':'stop'}]}))
        run_business_chat(question, db, source, CompanyAPIConfig('https://example.invalid/v1', 'offline', api_key='offline'),
                          transport=capture, allow_network=False, framework_reference_path='')
        return requests[0]

    def test_fixed_operand_and_free_conjunction_reach_first_model_request(self):
        cases = [('fixed', ['IF PLAN-CODE =', '* text about the condition', "    'P/PLANKEYEXT'"]),
                 ('free', ['IF NET-DUE > ZERO', '    *> branch eligibility', "    AND PLAN-CODE = 'P/PLANKEYEXT'"])]
        for fmt, guard in cases:
            with self.subTest(fmt=fmt):
                wire = self.lookup([*guard, '    SUBTRACT CREDIT-AMT FROM NET-DUE', 'END-IF.'], fmt, chat=True)
                pages = [p for block in wire['source_context'] for p in block['pages'] if p['relative_path']=='z-rule.cbl']
                self.assertTrue(pages)
                evidence = '\n'.join(p['source_text'] for p in pages)
                self.assertIn(guard[0], evidence)
                self.assertIn('P/PLANKEYEXT', evidence)
                self.assertIn('SUBTRACT CREDIT-AMT FROM NET-DUE', evidence)
                self.assertTrue(all(p['evidence_id'] == _identify_page(p) for p in pages))

    def test_display_after_if_and_a_quoted_keyword_cannot_own_the_next_literal(self):
        for body in (["IF CREDIT-AMT > 0 DISPLAY", "   'P/PLANKEYEXT' END-IF."],
                     ["DISPLAY 'IF PLAN-CODE ='", "   'P/PLANKEYEXT'."],
                     ["IF NET-DUE > 0", "    CONTINUE END-IF.", "DISPLAY 'P/PLANKEYEXT'."],
                     ["IF NET-DUE > 0 *> P/PLANKEYEXT", '    CONTINUE END-IF.']):
            with self.subTest(body=body):
                result=self.lookup(body, 'free')
                self.assertEqual(result['selected_paths'], ['a-notice.cbl'])
