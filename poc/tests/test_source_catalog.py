from __future__ import annotations

import hashlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import source_catalog
from source_catalog import refresh_source_catalog, select_related_sources


def program(name, body='GOBACK.\n'):
    return f'IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nPROCEDURE DIVISION.\n{body}'


class SourceCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / 'source'
        self.source.mkdir()
        self.database = self.base / 'output' / 'source-catalog.sqlite'

    def write(self, name, text):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        return path

    def refresh(self, **kwargs):
        return refresh_source_catalog(self.source, self.database, **kwargs)

    def test_unchanged_files_do_not_open_or_read_content(self):
        self.write('main.cbl', program('MAIN-ENTRY'))
        self.write('worker.cbl', program('VALUE-WORK'))
        before = self.refresh()
        with mock.patch('source_catalog._read_prefix', side_effect=AssertionError('body read')):
            after = self.refresh()
        self.assertEqual(after['files']['cached'], 2)
        self.assertEqual(after['files']['bytes_read'], 0)
        self.assertEqual(after['snapshot_id'], before['snapshot_id'])
        self.assertFalse(after['source_manifest_verified'])

    def test_single_change_new_file_and_deletion_invalidate_only_affected(self):
        removed = self.write('removed.cbl', program('OLD-WORK'))
        changed = self.write('changed.cbl', program('FIRST-WORK'))
        self.write('unchanged.cbl', program('STABLE-WORK'))
        before = self.refresh()
        removed.unlink()
        changed.write_text(program('NEXT-WORK'), encoding='utf-8')
        self.write('added.cbl', program('NEW-WORK'))
        with mock.patch('source_catalog._read_prefix', wraps=source_catalog._read_prefix) as reads:
            after = self.refresh()
        self.assertEqual(reads.call_count, 2)
        self.assertEqual(after['files']['cached'], 1)
        self.assertEqual(after['files']['removed'], 1)
        self.assertEqual({p['program_name'] for p in after['programs']}, {'NEXT-WORK', 'NEW-WORK', 'STABLE-WORK'})
        self.assertNotEqual(before['snapshot_id'], after['snapshot_id'])

    def test_changed_ctime_detects_equal_size_content_with_restored_mtime(self):
        path = self.write('main.cbl', program('FIRST'))
        stat = path.stat()
        self.refresh()
        path.write_text(program('OTHER'), encoding='utf-8')
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        after = self.refresh()
        self.assertEqual(after['files']['indexed_or_updated'], 1)
        self.assertEqual(after['programs'][0]['program_name'], 'OTHER')

    def test_changed_source_options_invalidate_header_cache(self):
        self.write('main.cbl', program('MAIN-ENTRY'))
        self.refresh()
        after = self.refresh(encoding='utf-8', source_format='free')
        self.assertEqual(after['files']['indexed_or_updated'], 1)
        self.assertEqual(after['files']['cached'], 0)

    def test_checkpoint_resumes_after_cancel_without_reading_completed_files(self):
        for index in range(5):
            self.write(f'{index}.cbl', program(f'WORK-{index}'))
        def cancel_after_two(event):
            if event['phase'] == 'catalog' and event['completed'] == 2:
                raise InterruptedError('Cancelled')
        with self.assertRaises(InterruptedError):
            self.refresh(progress=cancel_after_two)
        with mock.patch('source_catalog._read_prefix', wraps=source_catalog._read_prefix) as reads:
            after = self.refresh()
        self.assertEqual(after['files']['cached'], 2)
        self.assertEqual(reads.call_count, 3)

    def test_eighty_thousand_line_file_reads_only_initial_header(self):
        content = program('LARGE-ENTRY', ('DISPLAY "VALUE".\n' * 79996) + 'GOBACK.\n')
        path = self.write('large.cbl', content)
        result = self.refresh()
        self.assertGreater(path.stat().st_size, 1000000)
        self.assertEqual(result['files']['bytes_read'], 16384)
        self.assertEqual(result['programs'][0]['program_name'], 'LARGE-ENTRY')
        self.assertEqual(result['scope']['truncated_file_count'], 1)
        self.assertFalse(result['scope']['all_program_definitions_discovered'])

    def test_multiple_programs_in_header_and_later_ones_remain_explicitly_bounded(self):
        self.write('nested.cbl', program('OUTER') + program('INNER') + ('*> filler\n' * 2000) + program('TAIL'))
        result = self.refresh()
        self.assertEqual({p['program_name'] for p in result['programs']}, {'OUTER', 'INNER'})
        self.assertFalse(result['scope']['all_program_definitions_discovered'])
        self.assertTrue(any(w['code'] == 'BOUNDED_HEADER_SCAN' for w in result['warnings']))

    def test_physical_line_numbers_ignore_comments_and_fixed_sequence_columns(self):
        self.write('member.cbl', '000100* PROGRAM-ID. FAKE.\n000200 IDENTIFICATION DIVISION.\n000300 PROGRAM-ID.\n000400     ACTUAL.\n')
        result = self.refresh(source_format='fixed')
        self.assertEqual(result['programs'][0]['program_name'], 'ACTUAL')
        self.assertEqual(result['programs'][0]['start_line'], 3)

    def test_filename_fallback_and_copybook_catalog_are_distinguished(self):
        self.write('partial.cbl', 'PROCEDURE DIVISION.\nDISPLAY "VALUE".\n')
        self.write('SHARED.cpy', '01 SHARED-COUNT PIC 9.\n')
        result = self.refresh()
        self.assertEqual(result['programs'][0]['name_origin'], 'path')
        self.assertEqual(result['programs'][0]['program_name'], 'PARTIAL')
        self.assertEqual(result['copybook_count'], 1)
        self.assertEqual(len(result['programs']), 1)

    def test_trailing_multibyte_character_is_not_misdetected(self):
        text = program('MAIN') + (' ' * (16384 - len(program('MAIN').encode()) - 1)) + '中'
        self.write('main.cbl', text)
        result = self.refresh()
        self.assertEqual(result['file_entries'][0]['encoding'], 'utf-8')
        self.assertEqual(result['files']['failed'], 0)

    def test_explicit_full_verification_streams_contents_and_reuses_no_cache(self):
        path = self.write('main.cbl', program('MAIN') + ('DISPLAY "VALUE".\n' * 10000))
        self.refresh()
        result = self.refresh(verify_content=True)
        self.assertEqual(result['files']['bytes_read'], path.stat().st_size)
        self.assertEqual(result['files']['cached'], 0)
        self.assertTrue(result['scope']['full_file_content_verified'])
        self.assertFalse(result['source_manifest_verified'])
        with sqlite3.connect(self.database) as connection:
            payload = connection.execute('SELECT payload FROM catalog_files').fetchone()[0]
        self.assertIn(hashlib.sha256(path.read_bytes()).hexdigest(), payload)

    def test_missing_and_dynamic_dependencies_do_not_block_available_scope(self):
        self.write('main.cbl', program('MAIN', 'COPY AREA.\nCALL "WORKER".\nCALL "CLOSED-WORK".\nCALL TARGET-NAME.\n'))
        self.write('worker.cbl', program('WORKER', 'CALL "MAIN".\n'))
        self.write('AREA.cpy', '01 VALUE-COUNT PIC 9.\n')
        catalog = self.refresh()
        result = select_related_sources(self.source, catalog, 'MAIN')
        self.assertEqual(set(result['relative_paths']), {'main.cbl', 'worker.cbl', 'AREA.cpy'})
        self.assertEqual({d['status'] for d in result['missing_dependencies']}, {'MISSING_SOURCE', 'DYNAMIC_TARGET'})
        self.assertFalse(result['scope']['complete_dependency_closure'])

    def test_dependency_scan_limit_is_reported_and_selected_file_is_whole(self):
        self.write('main.cbl', program('MAIN', ('DISPLAY "VALUE".\n' * 500) + 'CALL "TAIL-WORK".\n'))
        self.write('tail.cbl', program('TAIL-WORK'))
        result = select_related_sources(self.source, self.refresh(), 'MAIN', max_scan_bytes=1024)
        self.assertEqual(result['relative_paths'], ['main.cbl'])
        self.assertIn('DEPENDENCY_SCAN_TRUNCATED', {d['status'] for d in result['missing_dependencies']})
        self.assertTrue(result['scope']['truncated'])

    def test_scope_bytes_limit_and_oversized_entry_are_explicit(self):
        entry = self.write('main.cbl', program('MAIN', 'CALL "WORKER".\n'))
        self.write('worker.cbl', program('WORKER') + ('*> filler\n' * 100))
        catalog = self.refresh()
        result = select_related_sources(self.source, catalog, 'MAIN', max_scope_bytes=entry.stat().st_size)
        self.assertEqual(result['relative_paths'], ['main.cbl'])
        self.assertIn('SOURCE_BYTE_LIMIT', {d['status'] for d in result['missing_dependencies']})
        oversized = select_related_sources(self.source, catalog, 'MAIN', max_scope_bytes=1)
        self.assertEqual(oversized['relative_paths'], ['main.cbl'])
        self.assertIn('ENTRY_EXCEEDS_BYTE_BUDGET', {d['status'] for d in oversized['missing_dependencies']})

    def test_ambiguous_names_need_entry_key_and_missing_copy_is_not_guessed(self):
        self.write('first/main.cbl', program('MAIN', 'COPY AREA.\n'))
        self.write('second/main.cbl', program('MAIN'))
        self.write('first/AREA.cpy', '01 VALUE-COUNT PIC 9.\n')
        self.write('second/AREA.cpy', '01 VALUE-COUNT PIC 9.\n')
        catalog = self.refresh()
        with self.assertRaisesRegex(ValueError, 'ENTRY_AMBIGUOUS'):
            select_related_sources(self.source, catalog, 'MAIN')
        result = select_related_sources(self.source, catalog, catalog['programs'][0]['entry_key'])
        self.assertEqual(result['relative_paths'], ['first/main.cbl'])
        self.assertIn('AMBIGUOUS_SOURCE', {d['status'] for d in result['missing_dependencies']})

    def test_progress_reports_unknown_discovery_total_then_measured_file_counts(self):
        self.write('main.cbl', program('MAIN'))
        events = []
        self.refresh(progress=events.append)
        self.assertEqual(events[0]['phase'], 'discovery')
        self.assertIsNone(events[0]['total'])
        self.assertEqual(events[-1]['completed'], 1)
        self.assertEqual(events[-1]['total'], 1)
        self.assertGreater(events[-1]['bytes_total'], 0)

    def test_partial_final_header_line_does_not_invent_a_program_identifier(self):
        prefix = ('*> comment\n' * 92) + (' ' * 3)
        prefix = prefix[:1008].ljust(1008)
        self.write('main.cbl', prefix + 'PROGRAM-ID. LONG-IDENTIFIER.\n')
        result = self.refresh(header_bytes=1024)
        self.assertEqual(result['programs'][0]['name_origin'], 'path')
        self.assertEqual(result['programs'][0]['program_name'], 'MAIN')

    def test_read_errors_do_not_hide_other_entries_and_are_retried(self):
        self.write('first.cbl', program('FIRST'))
        self.write('second.cbl', program('SECOND'))
        original = source_catalog._read_prefix
        def failing_read(path, *args, **kwargs):
            if path.name == 'first.cbl':
                raise PermissionError('Unreadable source')
            return original(path, *args, **kwargs)
        with mock.patch('source_catalog._read_prefix', side_effect=failing_read):
            failed = self.refresh()
        self.assertEqual(failed['files']['failed'], 1)
        self.assertEqual([p['program_name'] for p in failed['programs']], ['SECOND'])
        repaired = self.refresh()
        self.assertEqual(repaired['files']['failed'], 0)
        self.assertEqual(repaired['files']['cached'], 1)
        self.assertEqual(repaired['files']['indexed_or_updated'], 1)

    def test_scope_file_and_depth_limits_are_reported(self):
        self.write('main.cbl', program('MAIN', 'CALL "WORKER".\n'))
        self.write('worker.cbl', program('WORKER'))
        catalog = self.refresh()
        for options, expected in (({'max_files': 1}, 'FILE_LIMIT'), ({'max_depth': 0}, 'DEPTH_LIMIT')):
            with self.subTest(options=options):
                result = select_related_sources(self.source, catalog, 'MAIN', **options)
                self.assertEqual(result['relative_paths'], ['main.cbl'])
                self.assertIn(expected, {d['status'] for d in result['missing_dependencies']})

    def test_new_source_root_cannot_reuse_previous_root_headers(self):
        self.write('main.cbl', program('FIRST'))
        self.refresh()
        other = self.base / 'other'
        other.mkdir()
        (other / 'main.cbl').write_text(program('OTHER'), encoding='utf-8')
        result = refresh_source_catalog(other, self.database)
        self.assertEqual(result['programs'][0]['program_name'], 'OTHER')
        self.assertEqual(result['files']['cached'], 0)

    def test_symbolic_links_are_excluded(self):
        self.write('main.cbl', program('MAIN'))
        (self.source / 'linked.cbl').symlink_to(self.source / 'main.cbl')
        self.assertEqual(self.refresh()['files']['candidate'], 1)


if __name__ == '__main__':
    unittest.main()
