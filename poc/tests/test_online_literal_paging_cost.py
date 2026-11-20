"""Repeated pages of one pinned text search share decoded line positions."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[1])]
import online_material_query as material
from online_material_format import MaterialError
from test_online_material_query import FakeReader


class LiteralPagingCostTests(unittest.TestCase):
    def setUp(self):
        self.reader = FakeReader()
        self.raw = self.reader.add('rules.cbl', b'IF CREDIT-AMT > 0\n' * 8192)
        self.query = material.MaterialQuery(self.reader)

    def locate(self, needle, offset=0, length=20):
        return self.query.locate('rules.cbl', needle, lambda *a, **kw: self.raw, length, offset=offset)

    def test_dense_hits_decode_once_and_support_out_of_order_pages(self):
        with patch.object(material, '_decoded_line_chunks', wraps=material._decoded_line_chunks) as decode:
            for offset in (0, 1023, 1050, 10, 8185, 8192, 100000):
                result = self.locate('cREDIT-AmT', offset)
                self.assertEqual(result['matches'], list(range(offset + 1, min(8192, offset + 20) + 1)))
                self.assertEqual(result['total_matches'], 8192)
            self.assertEqual(decode.call_count, 1)

    def test_sparse_empty_and_replaced_source_obey_current_snapshot(self):
        self.raw = self.reader.add('rules.cbl', b'A\n' * 1030 + b'ELIGIBLE\n' + b'A\n' * 3100 + b'ELIGIBLE')
        self.assertEqual(self.locate('eligible')['matches'], [1031, 4132])
        self.assertEqual(self.locate('eligible', 1)['matches'], [4132])
        self.assertEqual(self.locate('missing')['matches'], [])
        self.assertEqual(self.locate('A', 1030, 2)['matches'], [1032, 1033])
        self.raw = self.raw.replace(b'ELIGIBLE', b'REJECTED')
        with self.assertRaisesRegex(MaterialError, 'MATERIAL_SOURCE_HASH_MISMATCH'):
            self.locate('A', 10)
