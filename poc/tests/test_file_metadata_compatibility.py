"""Regressions for changing file metadata and previously saved local indexes."""
from __future__ import annotations

from contextlib import closing
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import framework_knowledge
from business_index import build_business_index
from repository_discovery import ensure_repository_search, retrieve_repository_context
from source_catalog import refresh_source_catalog
from source_reading import _verified_lines
from structural_index import build_structural_index


SOURCE = ('IDENTIFICATION DIVISION.\nPROGRAM-ID. SAMPLE-ENTRY.\n'
          'PROCEDURE DIVISION.\n*> RESERVE-ADJUSTMENT\n'
          'COMPUTE RESULT-VALUE = INPUT-VALUE * 12.\nGOBACK.\n')


@contextmanager
def changing_creation_metadata():
    original_stat, original_fstat = Path.stat, os.fstat
    reads = []

    class Metadata:
        def __init__(self, value):
            self.value = value

        def __getattr__(self, name):
            if name == 'st_ctime_ns':
                reads.append(name)
                return len(reads)
            return getattr(self.value, name)

    with mock.patch.object(Path, 'stat', lambda path, **kw: Metadata(original_stat(path, **kw))), \
         mock.patch('os.fstat', lambda fd: Metadata(original_fstat(fd))):
        yield reads


class FileMetadataCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / 'source'
        self.source.mkdir()
        self.path = self.source / 'sample.cbl'
        self.path.write_text(SOURCE, encoding='utf-8')
        self.database = self.base / 'index.sqlite'

    def test_fluctuating_creation_metadata_does_not_block_import_or_verified_read(self):
        with changing_creation_metadata() as reads:
            report = build_business_index(self.source, self.database, source_format='free')
            ensure_repository_search(self.database, self.source)
            item = {'relative_path': 'sample.cbl', 'encoding': 'utf-8',
                    'sha256': hashlib.sha256(self.path.read_bytes()).hexdigest(),
                    'line_count': len(SOURCE.splitlines())}
            self.assertEqual([line for line, _ in _verified_lines(self.source, item, None, 1000)], SOURCE.splitlines())
            with mock.patch('business_index._verify_file', side_effect=AssertionError('unchanged source reread')):
                cached = build_business_index(self.source, self.database, source_format='free')
            self.assertEqual(report['snapshot_id'], cached['snapshot_id'])
            self.assertEqual(cached['files']['metadata_cache_reused'], 1)
            result = retrieve_repository_context(self.database, self.source, 'RESERVE-ADJUSTMENT')
            self.assertEqual(result['selected_paths'], ['sample.cbl'])
            self.assertEqual(result['cache']['content_verified_files'], 0)
            self.assertEqual(reads, [])

    def test_existing_catalog_schema_is_migrated_without_rereading_source(self):
        catalog = self.base / 'catalog.sqlite'
        first = refresh_source_catalog(self.source, catalog, source_format='free')
        with closing(sqlite3.connect(catalog)) as connection, connection:
            connection.execute('ALTER TABLE catalog_files ADD COLUMN ctime_ns INTEGER NOT NULL DEFAULT 1')
        with changing_creation_metadata() as reads, \
             mock.patch('source_catalog._read_prefix', side_effect=AssertionError('cached header reread')):
            second = refresh_source_catalog(self.source, catalog, source_format='free')
        self.assertEqual(second['files']['cached'], 1)
        self.assertEqual(second['snapshot_id'], first['snapshot_id'])
        self.assertEqual(reads, [])
        with closing(sqlite3.connect(catalog)) as connection, connection:
            self.assertNotIn('ctime_ns', {row[1] for row in connection.execute('PRAGMA table_info(catalog_files)')})

    def test_existing_retrieval_state_recovers_and_then_reuses_verified_text(self):
        build_business_index(self.source, self.database, source_format='free')
        ensure_repository_search(self.database, self.source)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('ALTER TABLE repo_source_state ADD COLUMN ctime_ns INTEGER NOT NULL DEFAULT 1')
            stats = json.loads(connection.execute("SELECT value FROM metadata WHERE key='file_stats'").fetchone()[0])
            for values in stats.values():
                values.append(1)
            connection.execute("UPDATE metadata SET value=? WHERE key='file_stats'", (json.dumps(stats),))
        with changing_creation_metadata() as reads:
            first = retrieve_repository_context(self.database, self.source, 'RESERVE-ADJUSTMENT')
            with mock.patch('repository_discovery._verified_lines', side_effect=AssertionError('cached source reread')):
                second = retrieve_repository_context(self.database, self.source, 'RESERVE-ADJUSTMENT')
        self.assertEqual(first['selected_paths'], ['sample.cbl'])
        self.assertEqual(first['cache']['content_verified_files'], 1)
        self.assertEqual(second['cache']['reused_files'], 1)
        self.assertEqual(reads, [])

    def test_structural_and_framework_reads_ignore_creation_metadata(self):
        reference = self.base / 'reference.md'
        reference.write_text('# Processing guide\n\n## Record update\n\nSAVE-ROW stores a record.\n', encoding='utf-8')
        framework_knowledge._CACHE.clear()
        self.addCleanup(framework_knowledge._CACHE.clear)
        with changing_creation_metadata() as reads:
            first = build_structural_index(self.source, self.database, source_format='free', quiet=True)
            with mock.patch('structural_index.read_source_document', side_effect=AssertionError('cached source reread')):
                second = build_structural_index(self.source, self.database, source_format='free', quiet=True)
            document = framework_knowledge._load_file(reference)
            self.assertIs(document, framework_knowledge._load_file(reference))
        self.assertEqual(first['snapshot_id'], second['snapshot_id'])
        self.assertEqual(reads, [])

    def test_actual_content_change_is_still_rejected_by_hash_verification(self):
        item = {'relative_path': 'sample.cbl', 'encoding': 'utf-8',
                'sha256': hashlib.sha256(self.path.read_bytes()).hexdigest(),
                'line_count': len(SOURCE.splitlines())}
        before = self.path.stat()
        self.path.write_text(SOURCE.replace('* 12', '* 24'), encoding='utf-8')
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with changing_creation_metadata(), self.assertRaisesRegex(ValueError, 'SOURCE_HASH_MISMATCH'):
            list(_verified_lines(self.source, item, None, 1000))


if __name__ == '__main__':
    unittest.main()
