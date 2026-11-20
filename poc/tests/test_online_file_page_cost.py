"""Inventory reads each visited immutable file shard once per page."""
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[1])]
from online_material_format import MaterialError, bucket
from online_material_query import MaterialQuery
from online_material_reader import MaterialReader
from test_online_material_reader import Fixture


class OnlineFilePageCostTests(unittest.TestCase):
    def setUp(self):
        self.material = Fixture()
        self.entries = {}
        prefix = bucket(self.material.path)
        number = 0
        while len(self.entries) < 40:
            path = f"programs/charge-{number:05d}.cbl"
            number += 1
            if bucket(path) == prefix:
                self.entries[path] = {**self.material.metadata, "relative_path": path}
        self.paths = sorted(self.entries)
        self.prefix = prefix

    def reader(self, **options):
        shard = self.material.store.put({"kind": "files", "entries": self.entries})
        self.material.body["routes"]["files"] = {self.prefix: shard}
        self.material.body["coverage"]["indexed_files"] = len(self.entries)
        self.material.seal()
        return MaterialReader(self.material.store, **options)

    def test_inventory_page_decodes_each_visited_shard_once_and_preserves_cursor(self):
        reader = self.reader(cache_bytes=0)
        query = MaterialQuery(reader)
        with mock.patch.object(reader, 'read_object', wraps=reader.read_object) as reads:
            first = query.inventory(20)
            second = query.inventory(20, cursor=first['next_cursor'])
        self.assertEqual(reads.call_count, 2)
        self.assertEqual(reader.fetched_objects, 2)
        self.assertFalse(first['complete'])
        self.assertTrue(second['complete'])
        self.assertEqual([row['relative_path'] for row in first['files'] + second['files']], self.paths)
        self.assertTrue(all(row['sha256'] == self.material.sha for row in first['files'] + second['files']))

    def test_operation_budget_allows_one_shard_and_rejects_a_real_excess(self):
        page = self.reader(max_operation_objects=1, max_operation_entries=40).files(40)
        self.assertTrue(page['complete'])
        self.assertEqual(len(page['files']), 40)
        with self.assertRaisesRegex(MaterialError, 'MATERIAL_OPERATION_LIMIT'):
            self.reader(max_operation_entries=39).files(40)

    def test_unreturned_sibling_and_reference_are_validated(self):
        for field, value, code in [('line_count', -1, 'MATERIAL_INVALID_FILE'),
                                   ('facts', [{}], 'MATERIAL_INVALID_REFERENCE')]:
            with self.subTest(field=field):
                self.entries[self.paths[-1]] = {**self.material.metadata, 'relative_path': self.paths[-1], field: value}
                with self.assertRaisesRegex(MaterialError, code):
                    self.reader().files(1)
