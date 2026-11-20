"""A ranked filename may coexist with a missing page containing the requested operation."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
import repository_discovery as discovery
from source_session import QuestionSourceSession
from source_reading import _identify_page


class NamedSourcePartialSearchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = Path(directory.name) / 'source'
        self.source.mkdir()
        self.database = self.source.parent / 'index.sqlite'
        lines = ['IDENTIFICATION DIVISION.', 'PROGRAM-ID. FEES.', 'PROCEDURE DIVISION.']
        self.call = 'CALL "PAIDDISPATCH".'
        lines += ['*> Neutral placeholder for the fixture.'] * 500 + [self.call, 'GOBACK.']
        (self.source / 'fees.cbl').write_text('\n'.join(lines)+'\n', encoding='utf-8')
        (self.source / 'unused.cbl').write_text('IDENTIFICATION DIVISION.\nPROGRAM-ID. UNUSED.\nPROCEDURE DIVISION.\nGOBACK.\n', encoding='utf-8')
        build_business_index(self.source, self.database, source_format='free', verify_content=True)
        discovery.ensure_repository_search(self.database, self.source)

    def remove_tail(self, *, remove_page):
        with closing(sqlite3.connect(self.database)) as db, db:
            page_id, = db.execute('SELECT page_id FROM repo_pages WHERE relative_path=? ORDER BY start_line DESC LIMIT 1', ('fees.cbl',)).fetchone()
            db.execute('DELETE FROM repo_fts WHERE rowid=?', (page_id,))
            if remove_page:
                db.execute('DELETE FROM repo_pages WHERE page_id=?', (page_id,))

    def search(self, **kwargs):
        return discovery.retrieve_repository_context(self.database, self.source,
            'Only analyze fees.cbl and check PAIDDISPATCH', search_terms=['PAIDDISPATCH'], **kwargs)

    def test_named_file_recovers_missing_page_from_only_requested_verified_original(self):
        self.remove_tail(remove_page=True)
        visited = []
        original = discovery._verified_pages

        def counted(root, item, *args):
            visited.append(item['relative_path'])
            return original(root, item, *args)

        with QuestionSourceSession(self.database, self.source) as session, \
                patch.object(discovery, '_verified_pages', side_effect=counted), \
                patch.object(Path, 'rglob', side_effect=AssertionError('source rescan')):
            first = self.search(source_session=session)
            second = self.search(source_session=session)
        self.assertEqual(visited, ['fees.cbl'])
        self.assertTrue(second['raw_fallback']['cache_reused'])
        page, = [p for p in first['pages'] if self.call in p['source_text']]
        self.assertEqual(page['relative_path'], 'fees.cbl')
        self.assertEqual(page['source_verification'], 'content_hash')
        self.assertEqual(page['evidence_id'], _identify_page(page))
        self.assertEqual(first['raw_fallback']['original_pages_checked'], 2)
        self.assertEqual(first['raw_fallback']['incomplete_search_path'], 'fees.cbl')

    def test_missing_fts_entry_retains_real_call_while_complete_index_needs_no_source_fallback(self):
        with patch.object(discovery, '_verified_pages', side_effect=AssertionError('unexpected source scan')):
            initial = self.search()
        self.assertNotIn('raw_fallback', initial)
        self.remove_tail(remove_page=False)
        recovered = self.search()
        expected, = [p for p in initial['pages'] if self.call in p['source_text']]
        actual, = [p for p in recovered['pages'] if self.call in p['source_text']]
        self.assertEqual(actual['evidence_id'], expected['evidence_id'])

    def test_named_file_recovers_missing_tail_even_without_any_fts_hit(self):
        self.remove_tail(remove_page=True)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('DELETE FROM repo_fts WHERE rowid IN '
                       '(SELECT page_id FROM repo_pages WHERE relative_path=?)', ('fees.cbl',))
        result = self.search()
        self.assertEqual(result['matched_page_count'], 0)
        self.assertEqual(result['selected_paths'], ['fees.cbl'])
        page, = [page for page in result['pages'] if self.call in page['source_text']]
        self.assertEqual(page['evidence_id'], _identify_page(page))
        self.assertLessEqual(page['start_line'], 504)
        self.assertGreaterEqual(page['end_line'], 504)
        self.assertEqual(result['raw_fallback']['incomplete_search_path'], 'fees.cbl')

    def test_oversized_original_exposes_existing_byte_limit_without_claiming_a_complete_search(self):
        self.remove_tail(remove_page=True)
        with patch.object(discovery, 'RAW_FALLBACK_FILE_BYTES', 4000), \
                patch.object(discovery, '_verified_pages', side_effect=AssertionError('over-budget source scan')):
            result = self.search()
        self.assertTrue(result['raw_fallback']['exhausted'])
        self.assertTrue(any(row['reason'] == 'bounded_raw_search_incomplete' for row in result['boundaries']))
        self.assertFalse(any(self.call in p['source_text'] for p in result['pages']))

    def test_changed_original_is_not_substituted_for_the_requested_version(self):
        self.remove_tail(remove_page=True)
        path = self.source / 'fees.cbl'
        path.write_text(path.read_text().replace('PAIDDISPATCH','FAKEDISPATCH'))
        with QuestionSourceSession(self.database, self.source) as session:
            result = self.search(source_session=session)
        self.assertFalse(result['pages'])
        self.assertEqual(result['raw_fallback']['failures'][0]['reason'], 'SOURCE_HASH_MISMATCH')
