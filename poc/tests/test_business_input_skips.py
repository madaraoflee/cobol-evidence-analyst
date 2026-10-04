"""Mixed source exports keep usable files and report excluded input explicitly."""

from contextlib import closing
import errno
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import business_index
from business_index import build_business_index
from repository_discovery import ensure_repository_search


def program(name, body="GOBACK.\n"):
    return (f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\n"
            "PROCEDURE DIVISION.\nMAIN-PARA.\n" + body)


class FailingTextReader:
    def __init__(self, handle):
        self.handle, self.reads = handle, 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def fileno(self):
        return self.handle.fileno()

    def read(self, count):
        self.reads += 1
        if self.reads == 2:
            raise OSError(errno.EIO, "input stream unavailable")
        return self.handle.read(count)


class BusinessInputSkipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="business-input-skips-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "output" / "index.sqlite"

    def write(self, name, text, encoding="utf-8"):
        path = self.source / name
        path.write_bytes(text.encode(encoding))
        return path

    def build(self, **options):
        return build_business_index(self.source, self.database, source_format="free", quiet=True, **options)

    def fingerprint(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return {table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
                    for table in ("source_files", "code_units", "symbols", "relations", "business_rules", "metadata", "code_units_fts")}

    def test_mixed_binary_and_bom_sources_keep_good_programs_without_whole_file_reads(self):
        for name, encoding in (("plain.cbl", "utf-8"), ("signature.cbl", "utf-8-sig"),
                               ("wide.cbl", "utf-16"), ("wider.cbl", "utf-32"), ("legacy.cbl", "cp950")):
            self.write(name, program("FLOW-" + Path(name).stem.upper(), "*> 中性說明\nGOBACK.\n"), encoding)
        (self.source / "binary.txt").write_bytes(b"\x00" * 128)
        (self.source / "broken.cbl").write_bytes(b"\xff\xfeX")
        events = []
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("Input must remain streaming.")):
            result = self.build(encoding="auto", progress=events.append)
        self.assertEqual(result["files"]["candidate"], 7)
        self.assertEqual(result["files"]["decoded"], 5)
        self.assertEqual(result["files"]["unreadable_or_binary"], 2)
        self.assertEqual(result["diagnostics"]["program_count"], 5)
        self.assertEqual(result["input_skips"], [
            {"relative_path": "binary.txt", "reason_code": "SOURCE_ENCODING_INVALID"},
            {"relative_path": "broken.cbl", "reason_code": "SOURCE_ENCODING_INVALID"}])
        self.assertFalse(result["scope"]["input_coverage_complete"])
        self.assertFalse(result["scope"]["complete_dependency_closure"])
        self.assertTrue(result["diagnostics"]["warnings"])
        self.assertEqual(result["diagnostics"]["status"], "needs_attention")
        self.assertEqual(sum(result["distributions"]["encodings"].values()), 5)
        indexing = [event for event in events if event["phase"] == "indexing"]
        self.assertEqual(indexing[-1]["completed"], indexing[-1]["total"])
        self.assertEqual(indexing[-1]["total"], 7)

    def test_forced_encoding_skips_only_invalid_member_and_never_uses_replacement(self):
        self.write("good.cbl", program("REQUEST-FLOW"))
        self.write("legacy.cbl", program("LEGACY-FLOW", "*> 中性說明\nGOBACK.\n"), "cp950")
        result = self.build(encoding="utf-8")
        self.assertEqual(result["files"]["decoded"], 1)
        self.assertEqual(result["files"]["candidate"], 2)
        self.assertEqual(result["input_skips"], [{"relative_path": "legacy.cbl", "reason_code": "SOURCE_ENCODING_INVALID"}])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT relative_path,encoding FROM source_files").fetchall(),
                             [("good.cbl", "utf-8")])
            self.assertFalse(any("\ufffd" in row[0] for row in connection.execute("SELECT text FROM evidence_spans")))

    @unittest.skipUnless(os.name == "posix" and getattr(os, "geteuid", lambda: 0)() != 0,
                         "Real source permissions require a non-root POSIX user.")
    def test_unreadable_input_is_skipped_and_its_path_is_reported(self):
        self.write("good.cbl", program("REQUEST-FLOW"))
        unreadable = self.write("unreadable.cbl", program("OTHER-FLOW"))
        mode = unreadable.stat().st_mode & 0o777
        unreadable.chmod(0)
        try:
            result = self.build()
        finally:
            unreadable.chmod(mode)
        self.assertEqual(result["files"]["decoded"], 1)
        self.assertEqual(result["files"]["unreadable_or_binary"], 1)
        self.assertEqual(result["input_skips"], [{"relative_path": "unreadable.cbl", "reason_code": "SOURCE_READ_FAILED"}])

    def test_unreadable_second_pass_rolls_back_partial_facts_and_still_indexes_large_good_source(self):
        large = "".join(f"MOVE {number} TO RESULT-AMOUNT.\n" for number in range(25000))
        failing = self.write("bad.cbl", program("BROKEN-FLOW", large))
        self.write("good.cbl", program("REQUEST-FLOW", large))
        original_open = Path.open

        def open_input(path, *args, **kwargs):
            handle = original_open(path, *args, **kwargs)
            if path == failing and args and args[0] == "r":
                return FailingTextReader(handle)
            return handle

        with mock.patch.object(Path, "open", open_input), \
                mock.patch.object(Path, "read_bytes", side_effect=AssertionError("Do not load large source files.")):
            result = self.build()
        self.assertEqual(result["files"]["decoded"], 1)
        self.assertEqual(result["files"]["indexed_or_updated"], 1)
        self.assertEqual(result["scope"]["selected_source_bytes"], (self.source / "good.cbl").stat().st_size)
        self.assertEqual(result["input_skips"], [{"relative_path": "bad.cbl", "reason_code": "SOURCE_READ_FAILED"}])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT relative_path,line_count FROM source_files").fetchall(),
                             [("good.cbl", 25004)])
            for table in ("evidence_spans", "code_units", "symbols", "relations", "business_rules"):
                self.assertEqual(connection.execute(f"SELECT count(*) FROM {table} WHERE relative_path='bad.cbl'").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM code_units_fts f LEFT JOIN code_units u "
                                                "ON u.unit_id=f.unit_id WHERE u.unit_id IS NULL").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_one_stat_read_failure_removes_old_member_and_keeps_other_sources(self):
        unreadable = self.write("bad.cbl", program("WORKER-FLOW"))
        self.write("good.cbl", program("REQUEST-FLOW", "CALL 'WORKER-FLOW'.\nGOBACK.\n"))
        self.build()
        original_safe, original_stat = business_index._safe_path, Path.stat
        selections, fail_next_stat = 0, False

        def safe_path(root, relative):
            nonlocal selections, fail_next_stat
            path = original_safe(root, relative)
            if relative == "bad.cbl":
                selections += 1
                # Discovery has validated this path. Fail the separate input
                # metadata read once the queue starts processing this member.
                fail_next_stat = selections == 2
            return path

        def input_stat(path, *args, **kwargs):
            nonlocal fail_next_stat
            if path == unreadable and fail_next_stat:
                fail_next_stat = False
                raise OSError(errno.EIO, "input metadata unavailable")
            return original_stat(path, *args, **kwargs)

        with mock.patch.object(business_index, "_safe_path", safe_path), mock.patch.object(Path, "stat", input_stat):
            result = self.build(verify_content=True)
        self.assertEqual(result["files"]["candidate"], 2)
        self.assertEqual(result["files"]["decoded"], 1)
        self.assertEqual(result["files"]["removed"], 1)
        self.assertEqual(result["input_skips"], [{"relative_path": "bad.cbl", "reason_code": "SOURCE_READ_FAILED"}])
        self.assertFalse(result["scope"]["input_coverage_complete"])
        self.assertIn({"relative_path": "good.cbl", "relation_type": "CALLS",
                       "target_name": "WORKER-FLOW", "status": "MISSING_SOURCE"}, result["missing_dependencies"])
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT relative_path FROM source_files").fetchall(), [("good.cbl",)])
            self.assertEqual(connection.execute("SELECT count(*) FROM symbols WHERE name='WORKER-FLOW'").fetchone()[0], 0)

    def test_now_bad_members_remove_old_facts_search_rows_and_dependency_targets(self):
        self.write("entry.cbl", program("ENTRY-FLOW", "CALL 'WORKER-FLOW'.\nCOPY SHARED-AREA.\nGOBACK.\n"))
        worker = self.write("worker.cbl", program("WORKER-FLOW", "MOVE REMOVED-MARKER TO RESULT-AMOUNT.\nGOBACK.\n"))
        shared = self.write("shared-area.cpy", "01 REQUEST-AMOUNT PIC 9(5).\n")
        self.build()
        ensure_repository_search(self.database, self.source)
        worker.write_bytes(b"\x00" * 128)
        shared.write_bytes(b"\xff\xfeX")
        result = self.build(verify_content=True)
        self.assertEqual(result["files"]["decoded"], 1)
        self.assertEqual(result["files"]["removed"], 2)
        self.assertEqual(result["files"]["unreadable_or_binary"], 2)
        self.assertFalse(result["scope"]["input_coverage_complete"])
        gaps = {(item["relation_type"], item["target_name"], item["status"]) for item in result["missing_dependencies"]}
        self.assertIn(("CALLS", "WORKER-FLOW", "MISSING_SOURCE"), gaps)
        self.assertIn(("INCLUDES_COPY", "SHARED-AREA", "MISSING_SOURCE"), gaps)
        ensure_repository_search(self.database, self.source)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT relative_path FROM source_files").fetchall(), [("entry.cbl",)])
            self.assertEqual(connection.execute("SELECT relative_path FROM repo_sources").fetchall(), [("entry.cbl",)])
            self.assertEqual(connection.execute("SELECT count(*) FROM code_units_fts f LEFT JOIN code_units u "
                                                "ON u.unit_id=f.unit_id WHERE u.unit_id IS NULL").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM symbols WHERE name='WORKER-FLOW'").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM business_rules WHERE normalized_text LIKE '%REMOVED-MARKER%'").fetchone()[0], 0)

    def test_all_skipped_inputs_keep_original_snapshot_and_return_excluded_paths(self):
        paths = [self.write("first.cbl", program("FIRST-FLOW")), self.write("second.cbl", program("SECOND-FLOW"))]
        first = self.build()
        before = self.fingerprint()
        for path in paths:
            path.write_bytes(b"\x00" * 128)
        with self.assertRaisesRegex(ValueError, "^SOURCE_INDEX_EMPTY$") as caught:
            self.build(verify_content=True)
        self.assertEqual(caught.exception.source_input_count, 2)
        self.assertEqual(caught.exception.input_skips, [
            {"relative_path": "first.cbl", "reason_code": "SOURCE_ENCODING_INVALID"},
            {"relative_path": "second.cbl", "reason_code": "SOURCE_ENCODING_INVALID"}])
        self.assertEqual(self.fingerprint(), before)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT value FROM metadata WHERE key='snapshot_id'").fetchone()[0], first["snapshot_id"])

    def test_progress_output_error_is_not_treated_as_an_unreadable_input(self):
        self.write("entry.cbl", program("ENTRY-FLOW"))
        self.build()
        before = self.fingerprint()

        def progress(event):
            if event["phase"] == "reading" and event.get("file_bytes_completed"):
                raise OSError(errno.ENOSPC, "progress output unavailable")

        with self.assertRaises(OSError) as caught:
            self.build(verify_content=True, progress=progress)
        self.assertEqual(caught.exception.errno, errno.ENOSPC)
        self.assertEqual(self.fingerprint(), before)

    def test_parser_progress_and_database_write_failures_abort_the_transaction(self):
        self.write("entry.cbl", program("ENTRY-FLOW", "MOVE 1 TO RESULT-AMOUNT.\n" * 6000))
        self.build()
        before = self.fingerprint()
        self.write("entry.cbl", program("ENTRY-FLOW", "MOVE 2 TO RESULT-AMOUNT.\n" * 6000))

        def progress(event):
            if event["phase"] == "parsing":
                raise OSError(errno.ENOSPC, "progress output unavailable")

        with self.assertRaises(OSError):
            self.build(verify_content=True, progress=progress)
        self.assertEqual(self.fingerprint(), before)
        with mock.patch.object(business_index._Facts, "consume", side_effect=sqlite3.OperationalError("database is full")):
            with self.assertRaises(sqlite3.OperationalError):
                self.build(verify_content=True)
        self.assertEqual(self.fingerprint(), before)

    def test_source_change_and_path_errors_are_not_skipped(self):
        self.write("entry.cbl", program("ENTRY-FLOW"))
        self.build()
        before = self.fingerprint()
        for message in ("SOURCE_CHANGED_DURING_READ", "SOURCE_PATH_INVALID", "SOURCE_ENCODING_INVALID extra"):
            with self.subTest(message=message), mock.patch.object(business_index, "_verify_file", side_effect=ValueError(message)):
                with self.assertRaisesRegex(ValueError, "^" + message + "$"):
                    self.build(verify_content=True)
                self.assertEqual(self.fingerprint(), before)


if __name__ == "__main__":
    unittest.main()
