"""Sentence punctuation after a complete filename preserves the user's explicit scope."""

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
from repository_identity import _exclusive_source_scope


class NamedScopeSentenceEndTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / 'source'
        self.source.mkdir()
        self.database = self.source.parent / 'index.sqlite'
        for file, program, marker in [('fees.cbl', 'FEES', 'ALLOWEDNOTE'),
                                      ('archive.v2/fees.rules.cbl', 'VERSIONTWO', 'VERSIONEDNOTE'),
                                      ('unused.cbl', 'UNUSED', 'OTHERCLAUSE')]:
            path = self.source / file
            path.parent.mkdir(exist_ok=True)
            path.write_text(f'IDENTIFICATION DIVISION.\nPROGRAM-ID. {program}.\nPROCEDURE DIVISION.\n'
                            f"DISPLAY '{marker}'.\nGOBACK.\n", encoding='utf-8')
        build_business_index(self.source, self.database, source_format='free', verify_content=True)
        ensure_repository_search(self.database, self.source)

    def test_followup_model_search_after_terminal_period_reads_only_the_named_file(self):
        requests = []

        def transport(request):
            wire = json.loads(json.loads(request.body)['messages'][-1]['content'])
            requests.append(wire)
            if len(requests) == 1:
                text = '{"search":["OTHERCLAUSE"]}'
            else:
                page = next(p for part in wire['source_context'] for p in part['pages'])
                text = f"This source displays the allowed note. [{page['evidence_id']}]"
            return TransportResponse(200, json.dumps({'choices': [{'message': {
                'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}]}))

        run_business_chat('Only analyze fees.cbl. Check the note.', self.database, self.source,
                          CompanyAPIConfig('https://neutral.example.invalid/v1', 'offline-model', api_key='offline'),
                          framework_reference_path='', policy={'max_model_requests': 3}, transport=transport)
        self.assertGreaterEqual(len(requests), 2)
        for wire in requests:
            pages = [p for part in wire['source_context'] for p in part['pages']]
            self.assertEqual({p['relative_path'] for p in pages}, {'fees.cbl'})
            self.assertFalse(any('OTHERCLAUSE' in p['source_text'] for p in pages))

    def test_dot_containing_path_keeps_its_full_identity_and_supplementary_search_scope(self):
        path = 'archive.v2/fees.rules.cbl'
        for suffix in ('.', '. Check the marker.', ', check the marker.'):
            with self.subTest(suffix=suffix):
                out = retrieve_repository_context(self.database, self.source, f'Only analyze {path}{suffix}',
                                                  search_terms=['OTHERCLAUSE'])
                self.assertTrue(out['source_identity']['exclusive_source_scope'])
                self.assertEqual(out['source_identity']['direct_paths'], [path])
                self.assertEqual(out['selected_paths'], [path])

    def test_trailing_extension_or_internal_dot_cannot_match_a_shorter_filename(self):
        name = 'fees.cbl'
        identity = {'requested': [name], 'direct_paths': [name]}
        for tail in ('.bak', '.old.cbl.', '.1', '..cbl', '.bak elsewhere'):
            with self.subTest(tail=tail):
                self.assertFalse(_exclusive_source_scope('Only analyze ' + name + tail, identity))
        self.assertTrue(_exclusive_source_scope('Only analyze fees.cbl.bak. Check the file.',
                                                {'requested': ['fees.cbl.bak'], 'direct_paths': ['fees.cbl.bak']}))
