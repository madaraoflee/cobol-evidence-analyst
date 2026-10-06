"""Connection setup failures must release Windows file handles immediately."""

from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import source_catalog
import structural_index


class SQLiteConnectionLifetimeTests(unittest.TestCase):
    def test_setup_failures_close_connection_before_propagating(self):
        original_connect = sqlite3.connect
        for module in (source_catalog, structural_index):
            for failure in (sqlite3.OperationalError("setup failed"), KeyboardInterrupt()):
                with self.subTest(module=module.__name__, failure=type(failure).__name__):
                    with tempfile.TemporaryDirectory() as directory:
                        database = Path(directory) / "index.sqlite"
                        opened = []

                        class FailingSetup(sqlite3.Connection):
                            def execute(self, statement, *args, **kwargs):
                                if module is structural_index and "journal_mode" in statement:
                                    raise failure
                                return super().execute(statement, *args, **kwargs)

                            def executescript(self, script):
                                if module is source_catalog:
                                    raise failure
                                return super().executescript(script)

                        def connect(*args, **kwargs):
                            connection = original_connect(*args, factory=FailingSetup, **kwargs)
                            opened.append(connection)
                            return connection

                        try:
                            with mock.patch.object(module.sqlite3, "connect", side_effect=connect):
                                with self.assertRaises(type(failure)) as caught:
                                    module._connect(database)
                            self.assertIs(caught.exception, failure)
                            self.assertEqual(len(opened), 1)
                            with self.assertRaises(sqlite3.ProgrammingError):
                                opened[0].execute("SELECT 1")
                            # Retain the connection object while checking the file is released.
                            database.unlink()
                        finally:
                            for connection in opened:
                                connection.close()

    def test_catalog_migration_failure_closes_connection(self):
        original_connect = sqlite3.connect
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.sqlite"
            with closing(original_connect(database)) as connection, connection:
                connection.execute("CREATE TABLE catalog_files (relative_path TEXT PRIMARY KEY, "
                    "size_bytes INTEGER, mtime_ns INTEGER, inode INTEGER, option_key TEXT, "
                    "payload TEXT, obsolete TEXT)")
            opened = []

            class FailedMigration(sqlite3.Connection):
                def executescript(self, script):
                    if "ALTER TABLE catalog_files" in script:
                        raise sqlite3.OperationalError("migration interrupted")
                    return super().executescript(script)

            def connect(*args, **kwargs):
                connection = original_connect(*args, factory=FailedMigration, **kwargs)
                opened.append(connection)
                return connection

            try:
                with mock.patch.object(source_catalog.sqlite3, "connect", side_effect=connect):
                    with self.assertRaisesRegex(sqlite3.OperationalError, "migration interrupted"):
                        source_catalog._connect(database)
                with self.assertRaises(sqlite3.ProgrammingError):
                    opened[0].execute("SELECT 1")
                database.unlink()
            finally:
                for connection in opened:
                    connection.close()

    def test_successful_setup_transfers_an_open_connection_to_the_caller(self):
        for module in (source_catalog, structural_index):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "index.sqlite"
                with closing(module._connect(database)) as connection, connection:
                    connection.execute("CREATE TABLE saved (value INTEGER)")
                    connection.execute("INSERT INTO saved VALUES (7)")
                with closing(sqlite3.connect(database)) as check:
                    self.assertEqual(check.execute("SELECT value FROM saved").fetchall(), [(7,)])


if __name__ == "__main__":
    unittest.main()
