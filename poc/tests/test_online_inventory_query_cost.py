"""Storage query costs remain bounded without losing source or version data."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[1])]
import test_online_inventory_store as fixture_module
from online_inventory_store import OnlineInventoryStore


class OnlineInventoryQueryCostTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture_module.OnlineInventoryStoreTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.store, self.session = self.case.store, self.case.session

    def full_scan_summary(self):
        with self.store.connection() as db:
            records = [(kind, key, json.loads(data)) for kind, key, data in db.execute(
                "SELECT kind,key,data FROM records WHERE session=?", (self.session.id,))]
            tasks = list(db.execute("SELECT group_key FROM tasks WHERE session=?", (self.session.id,)))
            work = json.loads(db.execute("SELECT meta FROM sessions WHERE id=?", (self.session.id,)).fetchone()[0])["work"]
        counts = Counter(kind for kind, _, _ in records)
        inventoried = sum(kind == "files" and value["inventoried"] for kind, _, value in records)
        complete = bool(work["inventory_complete"] and work["expected_files"] == inventoried)
        return {"scope": "indexed_snapshot", "active": True, "inventory_complete": complete,
            "files_discovered": counts["files"],
            "files_reviewed": counts["files"] - len({row[0] for row in tasks if row[0].startswith("file:")}),
            "candidates_total": sum(value["occurrence_count"] for kind, _, value in records if kind == "candidates"),
            "candidates_reviewed": counts["findings"], "pending_count": len(tasks) + (not complete),
            "legacy_checkpoint_recheck": work.get("legacy_checkpoint_recheck", False),
            "complete": bool(complete and not tasks), "full_repository_or_runtime_proof": False}

    def test_path_pages_preserve_order_and_filter_both_kind_and_session(self):
        other = self.store.session(**{**self.case.options, "conversation_id": "other-conversation"})
        expected = [(key, {"summary": "own " + key}) for key in ("a", "d", "z")]
        with self.store.connection() as db:
            for key, value in reversed(expected):
                self.session._put(db, "findings", key, value, "selected.cbl")
                other._put(db, "findings", key, {"summary": "foreign"}, "selected.cbl")
                self.session._put(db, "candidates", key, {"occurrence_count": 1}, "selected.cbl")
            self.session._put(db, "findings", "b", {"summary": "different path"}, "unrelated.cbl")
        observed = self.session.rows("findings", path="selected.cbl", limit=2)
        observed += self.session.rows("findings", path="selected.cbl", offset=2, limit=2)
        self.assertEqual(observed, expected)
        self.assertEqual(self.session.rows("findings", path="missing.cbl"), [])

    def test_single_path_read_does_not_scan_thousands_of_unrelated_records(self):
        with self.store.connection() as db:
            for index in range(3000):
                self.session._put(db, "findings", f"entry-{index:05d}", {"summary": "unrelated"}, f"unit-{index:05d}.cbl")
            self.session._put(db, "findings", "selected", {"summary": "retained exact row"}, "selected.cbl")
        original, steps = sqlite3.connect, [0]
        def tick():
            steps[0] += 1
            return 0
        def connect(*args, **options):
            db = original(*args, **options)
            db.set_progress_handler(tick, 1)
            return db
        self.store.close()
        with mock.patch("online_inventory_store.sqlite3.connect", side_effect=connect):
            result = self.session.rows("findings", path="selected.cbl", limit=100)
        self.assertEqual(result, [("selected", {"summary": "retained exact row"})])
        self.assertLess(steps[0], 512, "A single path must not iterate all unrelated kind rows")

    def test_summary_matches_independent_full_scan_without_reparsing_record_json(self):
        self.case.complete_work()
        baseline = self.full_scan_summary()
        original, calls = sqlite3.connect, []
        def extract(raw, path):
            calls.append(path)
            value = json.loads(raw).get(path[2:])
            return int(value) if isinstance(value, bool) else value
        def connect(*args, **options):
            db = original(*args, **options)
            db.create_function("json_extract", 2, extract, deterministic=True)
            return db
        self.store.close()
        with mock.patch("online_inventory_store.sqlite3.connect", side_effect=connect):
            observed = self.session.summary()
        self.assertEqual(observed, baseline)
        self.assertEqual(calls, [], "Summary must read indexed expressions, not reparse every saved record")
        key, value = self.session.rows("files", limit=1)[0]
        value["inventoried"] = False
        with self.store.connection() as db:
            self.session._put(db, "files", key, value, key)
            self.session._put(db, "candidates", "additional", {"occurrence_count": 17}, key)
        self.assertEqual(self.session.summary(), self.full_scan_summary())
        self.assertFalse(self.session.summary()["inventory_complete"])

    def test_legacy_index_upgrade_preserves_all_records_and_old_publication(self):
        self.case.complete_work()
        publication = self.session.publish(self.case.state, "Existing complete business explanation", True)
        exported = publication.export_data()
        tables = ("sessions", "versions", "records", "tasks", "actions", "publications")
        with self.store.connection() as db:
            before = {table: db.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall() for table in tables}
            db.execute("DROP INDEX record_path_key")
            db.execute("DROP INDEX summary_candidate_occurrences")
            db.execute("DROP INDEX summary_inventoried_files")
            db.execute("CREATE INDEX record_path ON records(session,kind,path)")
        reopened = OnlineInventoryStore(self.store.root)
        self.addCleanup(reopened.close)
        with reopened.connection() as db:
            after = {table: db.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall() for table in tables}
            indexes = {row[1] for row in db.execute("PRAGMA index_list(records)")}
        self.assertEqual(after, before)
        self.assertNotIn("record_path", indexes)
        self.assertTrue({"record_path_key", "summary_candidate_occurrences", "summary_inventoried_files"} <= indexes)
        self.assertEqual(publication.export_data(), exported)
        additional = OnlineInventoryStore(self.store.root)
        self.addCleanup(additional.close)
        self.assertEqual(publication.export_data(), exported)

    def test_rollback_keeps_summary_and_published_history_consistent(self):
        self.case.complete_work()
        publication = self.session.publish(self.case.state, "Original explanation", True)
        summary, exported = self.session.summary(), publication.export_data()
        key, row = self.session.rows("files", limit=1)[0]
        changed = deepcopy(row)
        changed["inventoried"] = False
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.store.connection() as db:
                self.session._put(db, "files", key, changed, key)
                self.session._put(db, "candidates", "uncommitted", {"occurrence_count": 99}, key)
                raise RuntimeError("rollback")
        self.assertEqual(self.session.get("files", key), row)
        self.assertIsNone(self.session.get("candidates", "uncommitted"))
        self.assertEqual(self.session.summary(), summary)
        self.assertEqual(self.session.summary(), self.full_scan_summary())
        self.assertEqual(publication.export_data(), exported)


if __name__ == "__main__":
    unittest.main()
