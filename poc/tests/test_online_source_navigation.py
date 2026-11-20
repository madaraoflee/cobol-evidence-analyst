"""An ordinary business investigation can navigate the pinned original source."""
from pathlib import Path
import re
import sys
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[1])]
import test_online_investigation as scenarios
import test_online_material_query as materials
from online_investigation import _action
from online_material_format import MaterialError
from online_material_query import MaterialQuery


class OnlineSourceNavigationTests(unittest.TestCase):
    def test_ordinary_question_recovers_original_caller_and_worker_after_business_term_miss(self):
        fixture = scenarios.OnlineInvestigationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)

        def after_search(context):
            result = context['observations'][-1]['result']
            self.assertEqual(result['total_matches'], 0)
            return {'actions': [result['suggested_action']]}

        def locate_caller(context):
            files = context['observations'][-1]['result']['files']
            self.assertIn('main.cbl', [row['relative_path'] for row in files])
            return {'actions': [{'tool': 'locate', 'path': 'main.cbl', 'text': 'CALL'}]}

        def read_caller(context):
            self.assertEqual(context['source_context'], [])
            position = context['observations'][-1]['result']['matches'][0]
            return {'actions': [scenarios.read('main.cbl', position - 1, position + 2)]}

        def follow_call(context):
            caller = context['source_context'][0]['text']
            target = re.search(r"CALL '([^']+)'", caller).group(1)
            inventory = next(obs['result']['files'] for obs in context['observations']
                             if obs['action']['tool'] == 'inventory')
            # Select by the name observed in CALL and verify the declaration on read.
            by_entry = {Path(row['relative_path']).stem.upper() + '-ENTRY': row for row in inventory}
            candidate = by_entry[target]
            return {'actions': [scenarios.read(candidate['relative_path'], 1, candidate['line_count'])]}

        def answer(context):
            original = {page['citation']['path']: page['text'] for page in context['source_context']}
            self.assertIn("IF INPUT-AMOUNT > 0\nCALL 'WORKER-ENTRY'", original['main.cbl'])
            self.assertIn("PROGRAM-ID. WORKER-ENTRY.", original['worker.cbl'])
            self.assertIn("DISPLAY 'PROCESS'", original['worker.cbl'])
            return scenarios.answer(context, 'A positive amount calls the worker, which emits PROCESS.')

        result = fixture.run_question([{'actions': [{'tool': 'search', 'text': '正金额业务如何承接'}]},
                                       after_search, locate_caller, read_caller, follow_call, answer],
                                      question='正金额业务如何承接')
        self.assertEqual(result['status'], 'answered', (result['error'], result['checkpoint']['observations']))
        self.assertCountEqual(fixture.source_calls, ['main.cbl', 'worker.cbl'])
        self.assertEqual(result['model_calls'], 6)
        self.assertEqual(len(result['citations']), 2)
        self.assertFalse(result['checkpoint']['work_ledger']['global'])

    def test_literal_locations_match_evidence_lines_across_chunks_and_pages(self):
        reader = materials.FakeReader()
        query = MaterialQuery(reader)
        raw = reader.add('source.cbl', '\ufeffONE\r\nWX.12\x85wx.12 and WX.12\u2028WX012\rWX.12'.encode('utf-16'), 'utf-16')
        def source(path, *, max_bytes):
            return raw
        with mock.patch('online_material_query._DECODE_CHUNK_BYTES', 3):
            first = query.locate('source.cbl', 'wx.12', source, limit=2)
            tail = query.locate('source.cbl', 'wx.12', source, limit=2, offset=2)
        self.assertEqual((first['total_matches'], first['matches'], tail['matches']), (3, [2, 3], [5]))
        for number in first['matches'] + tail['matches']:
            self.assertIn('wx.12', query.evidence('source.cbl', number, number, source)['text'].casefold())
        self.assertFalse(first['absence_established'])
        self.assertNotIn('citation', first)

    def test_navigation_rejects_changed_source_invalid_locations_and_out_of_scope_paths(self):
        reader = materials.FakeReader()
        query = MaterialQuery(reader)
        reader.add('source.cbl', b'ONE\nTWO\n')
        with self.assertRaisesRegex(MaterialError, 'MATERIAL_SOURCE_HASH_MISMATCH'):
            query.locate('source.cbl', 'ONE', lambda path, **kw: b'ONE\nNEW\n')
        with self.assertRaisesRegex(MaterialError, 'MATERIAL_FILE_NOT_INDEXED'):
            query.locate('missing.cbl', 'ONE', lambda *args, **kw: self.fail('outside snapshot'))
        reader.files['source.cbl']['line_count'] += 1
        with self.assertRaisesRegex(MaterialError, 'MATERIAL_SOURCE_LINE_COUNT_MISMATCH'):
            query.locate('source.cbl', 'ONE', lambda path, **kw: b'ONE\nTWO\n')
        for invalid in ({'text': '\r\n'}, {'limit': 0}, {'offset': -1}, {'path': '../private.cbl'}):
            with self.subTest(invalid=invalid), self.assertRaises(MaterialError):
                _action({'tool': 'locate', 'path': 'source.cbl', 'text': 'one', **invalid})
