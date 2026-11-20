"""Cross-file literal navigation on a pinned material snapshot."""
import base64
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest import mock
import zlib

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[1])]
import test_online_material_export as fixtures
from online_distribution import _reachable
from online_material_format import LITERAL_LANE_BYTES, MaterialError, canonical_json, seal_manifest
from online_material_reader import DirectoryObjectStore, MaterialReader
from online_material_query import MaterialQuery


class CrossFileLiteralTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.MaterialExportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.raw = {
            'a.cbl': b"IDENTIFICATION DIVISION.\nPROGRAM-ID. FIRST-PROGRAM.\nPROCEDURE DIVISION.\n"
                     b"IF PLAN-CODE = 'PM/XADJKEYENDORSED'\nCONTINUE\nEND-IF.\n"
                     b"DISPLAY 'XADJKEY'.\nGOBACK.\n",
            'b.cbl': ("IDENTIFICATION DIVISION.\nPROGRAM-ID. SECOND-PROGRAM.\nPROCEDURE DIVISION.\n"
                      "DISPLAY 'PM/XADJKEYENDORSED'.\nDISPLAY 'Straße 退款'.\nGOBACK.\n").encode(),
        }
        for name, data in self.raw.items():
            (self.fixture.source / name).write_bytes(data)
        self.fixture.build(self.fixture.source, self.fixture.database)
        destination, self.manifest = self.fixture.export()
        self.destination = destination
        self.store = DirectoryObjectStore(destination)
        self.reader = MaterialReader(self.store)
        self.query = MaterialQuery(self.reader)
        self.calls = []

    def source(self, path, *, max_bytes):
        self.calls.append(path)
        return (self.fixture.source / path).read_bytes()

    def test_embedded_literal_finds_original_lines_in_multiple_files_and_resumes_inside_a_file(self):
        cursor, found = None, []
        for _ in range(5):
            page = self.query.locate_global('xadjkey', self.source, limit=1, cursor=cursor)
            self.assertFalse(page['absence_established'])
            self.assertNotIn('citation', page)
            found.extend((row['path'], row['line']) for row in page['matches'])
            cursor = page['next_cursor']
            if cursor is None:
                break
        self.assertEqual(found, [('a.cbl', 4), ('a.cbl', 7), ('b.cbl', 4)])
        self.assertTrue(page['complete'])
        self.assertEqual(set(self.calls), {'a.cbl', 'b.cbl'})

    def test_search_verifies_unicode_punctuation_whitespace_and_short_literals_against_original(self):
        for term, expected in [('STRASSE 退款', [('b.cbl', 5)]), ('退款', [('b.cbl', 5)]),
                               ('/xadj', [('a.cbl', 4), ('b.cbl', 4)]), ('PM /', [])]:
            with self.subTest(term=term):
                result = self.query.locate_global(term, self.source)
                self.assertEqual([(m['path'], m['line']) for m in result['matches']], expected)

    def legacy_reader(self, **options):
        body = deepcopy(self.manifest)
        del body["revision"]
        body["routes"]["references"] = {}
        manifest = seal_manifest(body)
        (self.destination / "legacy.json").write_bytes(canonical_json(manifest))
        return MaterialReader(self.store, manifest_path="legacy.json", **options)

    def test_pre_locator_material_expands_existing_tokens_without_fetching_unrelated_originals(self):
        legacy = MaterialQuery(self.legacy_reader())
        page = legacy.locate_global('adjkeyendo', self.source)
        self.assertEqual(page['matches'], [{'path': 'a.cbl', 'line': 4}, {'path': 'b.cbl', 'line': 4}])
        self.assertEqual(page['search_mode'], 'legacy_token_candidates_only')
        self.assertTrue(page['complete'])
        self.assertFalse(page['absence_established'])
        self.assertEqual(self.calls, ['a.cbl', 'b.cbl'])
        symbols = legacy.locate_global('./', self.source)
        self.assertEqual((symbols['complete'], symbols['search_mode']), (False, 'legacy_no_searchable_term'))

    def test_legacy_budget_exhaustion_recommends_directory_without_repeating_action_or_swallowing_integrity_errors(self):
        legacy = MaterialQuery(self.legacy_reader(max_operation_objects=1))
        page = legacy.locate_global('adjkeyendo', self.source)
        self.assertEqual((page['complete'], page['next_cursor'], page['search_mode']),
                         (False, None, 'legacy_material_budget_exceeded'))
        self.assertEqual(page['suggested_action']['tool'], 'inventory')
        self.assertEqual(self.calls, [])
        with mock.patch.object(legacy.reader, 'legacy_literal_candidates', side_effect=MaterialError('MATERIAL_HASH_MISMATCH')):
            with self.assertRaisesRegex(MaterialError, 'MATERIAL_HASH_MISMATCH'):
                legacy.locate_global('adjkeyendo', self.source)

    def test_page_byte_and_file_limits_advance_to_next_candidate_and_changed_source_fails_verification(self):
        with mock.patch('online_material_query._LITERAL_PAGE_BYTES', 1):
            first = self.query.locate_global('xadjkey', self.source)
            self.assertEqual((first['scanned_files'], len(first['matches'])), (1, 2))
            self.assertFalse(first['complete'])
            last = self.query.locate_global('xadjkey', self.source, cursor=first['next_cursor'])
            self.assertEqual((last['scanned_files'], last['matches']), (1, [{'path': 'b.cbl', 'line': 4}]))
            self.assertTrue(last['complete'])
        with mock.patch('online_material_query._LITERAL_PAGE_FILES', 1):
            first = self.query.locate_global('xadjkey', self.source)
            self.assertEqual(first['scanned_files'], 1)
            for cursor in ({**first['next_cursor'], 'key': 'other'}, {**first['next_cursor'], 'group': 999}):
                with self.assertRaisesRegex(MaterialError, 'MATERIAL_INVALID_CURSOR'):
                    self.query.locate_global('xadjkey', self.source, cursor=cursor)
        with self.assertRaisesRegex(MaterialError, 'MATERIAL_SOURCE_HASH_MISMATCH'):
            self.query.locate_global('xadjkey', lambda path, **kwargs: b'z' * kwargs['max_bytes'])

    def test_original_publication_walk_reaches_all_search_objects_and_normal_queries_still_work(self):
        reachable = set(_reachable(self.store, self.manifest, None))
        self.assertEqual(reachable, {p.stem for p in (self.destination / 'objects').iterdir()})
        self.assertIn('entry.cbl', self.reader.postings('result-amount'))
        self.assertEqual(self.reader.postings('CALLS:payment-entry', route='references'), ['entry.cbl'])

    def test_literal_filter_rejects_overlong_trailing_or_incomplete_compressed_data(self):
        group = self.reader.literal_directory()[0]
        size = group['count'] * LITERAL_LANE_BYTES
        packed = zlib.compress(bytes(size))
        for invalid in (zlib.compress(bytes(size + 1)), packed + b'extra', packed[:-1]):
            with self.subTest(size=len(invalid)):
                lane = {'kind': 'literal_lane', 'data': base64.b64encode(invalid).decode()}
                with mock.patch.object(self.reader, 'read_object', return_value=lane):
                    with self.assertRaisesRegex(MaterialError, 'MATERIAL_INVALID_LITERAL_INDEX'):
                        self.reader.literal_group_paths(group, [0])
