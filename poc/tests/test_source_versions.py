"""Content-version history remains chronological and isolated by source scope."""

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from source_versions import record_version, versions


class SourceVersionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="source-versions-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.output = self.root / "output"
        self.output.mkdir()
        self.database = self.output / "structural-index.sqlite"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE source_files (relative_path TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")

    def indexed_content(self, text):
        digest = hashlib.sha256(text.encode()).hexdigest()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT OR REPLACE INTO source_files VALUES (?,?)", ("rule.cbl", digest))
        return "sha256:" + digest

    def test_returning_to_previous_content_records_a_new_chronological_revision(self):
        first_snapshot = self.indexed_content("MOVE FIRST-VALUE TO RESULT.")
        first = record_version(self.output, self.source, first_snapshot)
        second_snapshot = self.indexed_content("MOVE SECOND-VALUE TO RESULT.")
        second = record_version(self.output, self.source, second_snapshot)
        self.indexed_content("MOVE FIRST-VALUE TO RESULT.")
        returned = record_version(self.output, self.source, first_snapshot)

        history = versions(self.output, self.source)
        self.assertEqual([item["version_id"] for item in history],
                         [first["version_id"], second["version_id"], first["version_id"]])
        self.assertEqual(returned["previous_version_id"], second["version_id"])
        self.assertEqual(second["previous_version_id"], first["version_id"])
        self.assertIsNone(first["previous_version_id"])
        self.assertEqual(returned["changes"], {"added": 0, "modified": 1, "removed": 0, "unchanged": 0})
        unchanged = record_version(self.output, self.source, first_snapshot)
        self.assertEqual(unchanged["created_at"], returned["created_at"])
        self.assertEqual(len(versions(self.output, self.source)), 3)

    def test_two_source_directories_have_independent_histories_in_one_output(self):
        other = self.root / "other-source"
        other.mkdir()
        first_snapshot = self.indexed_content("MOVE FIRST-VALUE TO RESULT.")
        first = record_version(self.output, self.source, first_snapshot)
        other_first = record_version(self.output, other, first_snapshot)
        self.assertEqual(first["version_id"], other_first["version_id"])
        self.assertNotEqual(first["source_key"], other_first["source_key"])
        self.assertIsNone(other_first["previous_version_id"])
        second_snapshot = self.indexed_content("MOVE SECOND-VALUE TO RESULT.")
        second = record_version(self.output, self.source, second_snapshot)

        self.assertEqual([item["version_id"] for item in versions(self.output, self.source)],
                         [second["version_id"], first["version_id"]])
        self.assertEqual(versions(self.output, other), [other_first])
        self.assertEqual(second["previous_version_id"], first["version_id"])
        self.assertEqual(second["changes"], {"added": 0, "modified": 1, "removed": 0, "unchanged": 0})


if __name__ == "__main__":
    unittest.main()
