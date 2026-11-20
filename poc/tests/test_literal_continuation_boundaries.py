"""A page boundary may divide a physical condition across two source citations."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from business_chat import run_business_chat
from business_index import build_business_index
from company_api import CompanyAPIConfig, TransportResponse
from repository_discovery import PAGE_CHARS, ensure_repository_search, retrieve_repository_context


class LiteralContinuationBoundaryTests(unittest.TestCase):
    header = '           IF PLAN-CODE ='
    literal = "               'P/PLANKEYEXT'"
    deduction = '               SUBTRACT CREDIT-AMT FROM NET-DUE'
    question = 'PLANKEY 在什么条件下使用？'

    def fixture(self, layout):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        source = Path(directory.name) / 'src'
        source.mkdir()
        database = source.parent / 'index.sqlite'
        notice = ['       IDENTIFICATION DIVISION.', '       PROGRAM-ID. NOTICE.', '       PROCEDURE DIVISION.',
                  "           DISPLAY 'PLANKEY'.", '           GOBACK.']
        (source / 'a-notice.cbl').write_text('\n'.join(notice) + '\n', encoding='utf-8')
        declarations = ['       IDENTIFICATION DIVISION.', '       PROGRAM-ID. RULES.', '       DATA DIVISION.',
                        '       WORKING-STORAGE SECTION.', '       01 PLAN-CODE PIC X(20).',
                        '       01 CREDIT-AMT PIC 9(7).', '       01 NET-DUE PIC 9(7).', '       PROCEDURE DIVISION.']
        if layout in ('condition', 'next_body'):
            prefix = [self.header]
            suffix = [self.literal, self.deduction, '           END-IF.']
        elif layout == 'display':
            prefix = ['           DISPLAY']
            suffix = [self.literal]
        else:
            prefix = ['           DISPLAY', '      *      IF PLAN-CODE =']
            suffix = [self.literal]
        # Fill the first real search page using valid fixed-format comments.
        # This keeps the production page limit and whole source hashes intact.
        first_page_budget = PAGE_CHARS - (len(self.literal) + 1 if layout == 'next_body' else 0)
        remaining = first_page_budget - len('\n'.join(declarations + prefix))
        comment_count = (remaining + 72) // 73
        width, extra = divmod(remaining, comment_count)
        comments = ['      *'.ljust(width - 1 + int(i < extra)) for i in range(comment_count)]
        first_page = declarations + comments + prefix
        lines = first_page + suffix + ['           GOBACK.']
        (source / 'z-rule.cbl').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        build_business_index(source, database, source_format='fixed', verify_content=True,
                             framework_reference_path='', quiet=True)
        ensure_repository_search(database, source)
        with closing(sqlite3.connect(database)) as connection:
            ranges = connection.execute('SELECT start_line,end_line FROM repo_pages WHERE relative_path=? '
                                        'ORDER BY start_line', ('z-rule.cbl',)).fetchall()
        boundary = len(first_page) + (layout == 'next_body')
        self.assertEqual(ranges, [(1, boundary), (boundary + 1, len(lines))])
        return database, source

    def assert_first_request_supplies_rule(self, layout):
        database, source = self.fixture(layout)
        requests = []

        def respond(request):
            requests.append(json.loads(json.loads(request.body)['messages'][-1]['content']))
            return TransportResponse(200, json.dumps({'choices': [{'message': {'role': 'assistant',
                'content': 'Unable to confirm.'}, 'finish_reason': 'stop'}]}))

        run_business_chat(self.question, database, source,
            CompanyAPIConfig('https://neutral.example.invalid/v1', 'offline', api_key='offline'),
            transport=respond, allow_network=False, framework_reference_path='')
        first = requests[0]
        self.assertEqual(first['response_mode'], 'business_analysis')
        pages = [p for bundle in first['source_context'] for p in bundle['pages'] if p['relative_path'] == 'z-rule.cbl']
        self.assertTrue(pages, 'Only the display program reached the first model request')
        raw = (source / 'z-rule.cbl').read_bytes()
        original = raw.decode().splitlines()
        covered = set()
        for page in pages:
            start, end = page['start_line'], page['end_line']
            self.assertEqual(page['source_sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(page['source_text'], '\n'.join(original[start - 1:end]))
            covered.update(range(start, end + 1))
        for expected in (self.header, self.literal, self.deduction):
            self.assertIn(original.index(expected) + 1, covered, expected)

    def test_first_business_request_contains_both_halves_of_a_condition_split_by_search_pages(self):
        self.assert_first_request_supplies_rule('condition')

    def test_first_business_request_contains_deduction_immediately_after_search_page_boundary(self):
        self.assert_first_request_supplies_rule('next_body')

    def test_display_operand_and_commented_condition_on_previous_page_do_not_displace_exact_hit(self):
        for layout in ('display', 'comment'):
            with self.subTest(layout=layout):
                database, source = self.fixture(layout)
                actual = retrieve_repository_context(database, source, self.question, max_pages=1)
                self.assertEqual(actual['selected_paths'], ['a-notice.cbl'])
                self.assertEqual(actual['literal_search']['retained_matches'], 0)
