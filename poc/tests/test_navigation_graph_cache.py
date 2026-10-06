from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from business_map import build_business_map
import repository_discovery as discovery


class NavigationGraphCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"
        discovery._GRAPH_CACHE.clear()
        self.addCleanup(discovery._GRAPH_CACHE.clear)
        self.write("entry.cbl", "ENTRYPLAN", 'CALL "FIRSTWORKER".')
        self.write("first.cbl", "FIRSTWORKER", "GOBACK.")
        self.build()

    def write(self, path, name, body):
        (self.source / path).write_text(
            f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nPROCEDURE DIVISION.\n{body}\n")

    def build(self):
        build_business_index(self.source, self.database, source_format="free", quiet=True)
        discovery.ensure_repository_search(self.database, self.source)

    def mapping(self):
        return build_business_map(self.database, self.source, "entry.cbl 的逻辑是什么？")

    def test_discovery_and_mapping_share_facts_and_source_locations_per_snapshot(self):
        with mock.patch.object(discovery, "_load_navigation_graph", wraps=discovery._load_navigation_graph) as facts, \
             mock.patch.object(discovery, "_graph_details", wraps=discovery._graph_details) as details:
            first = self.mapping()
            second = self.mapping()
            self.assertEqual(first, second)
            self.assertEqual(facts.call_count, 1)
            self.assertEqual(details.call_count, 1)
            first["relations"][0]["target_path"] = "changed-by-consumer"
            self.assertEqual(self.mapping(), second)
            self.assertEqual(facts.call_count, 1)
            self.assertEqual(details.call_count, 1)
        self.assertEqual(second["relations"][0]["caller_line"], 4)
        self.assertEqual(second["relations"][0]["target_path"], "first.cbl")

    def test_refresh_reloads_new_snapshot_and_drops_the_old_generation(self):
        with mock.patch.object(discovery, "_load_navigation_graph", wraps=discovery._load_navigation_graph) as facts:
            before = self.mapping()
            old_keys = set(discovery._GRAPH_CACHE)
            self.write("second.cbl", "SECONDWORKER", "GOBACK.")
            self.write("entry.cbl", "ENTRYPLAN", 'CALL "FIRSTWORKER".\nCALL "SECONDWORKER".')
            self.build()
            after = self.mapping()
            self.assertNotEqual(before["snapshot_id"], after["snapshot_id"])
            self.assertEqual(facts.call_count, 2)
            self.assertEqual({edge["target_path"] for edge in after["relations"]}, {"first.cbl", "second.cbl"})
            self.assertFalse(old_keys.intersection(discovery._GRAPH_CACHE))

    def test_same_content_rebuild_options_invalidate_the_cached_generation(self):
        self.mapping()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE metadata SET value=value || '-refreshed' WHERE key='indexed_at_utc'")
        with mock.patch.object(discovery, "_load_navigation_graph", wraps=discovery._load_navigation_graph) as facts:
            self.mapping()
            self.assertEqual(facts.call_count, 1)

    def test_cancel_is_checked_before_even_a_cached_graph_reads_metadata(self):
        self.mapping()
        with closing(discovery._connect(self.database)) as connection:
            queries = []
            connection.set_trace_callback(queries.append)
            def cancel():
                raise RuntimeError("navigation cancelled")
            with self.assertRaisesRegex(RuntimeError, "navigation cancelled"):
                discovery._navigation_graph(connection, cancel, details=True)
            self.assertEqual(queries, [])

    def test_cache_has_two_snapshot_and_byte_bounds_without_clipping_results(self):
        rows = ({"relation_id": "first", "target_path": "member.cbl"},)
        for index in range(3):
            discovery._cache_graph((str(index), "snapshot"), rows, False)
        self.assertEqual(len(discovery._GRAPH_CACHE), 2)
        self.assertNotIn(("0", "snapshot"), discovery._GRAPH_CACHE)
        self.assertLessEqual(sum(entry[2] for entry in discovery._GRAPH_CACHE.values()), discovery._GRAPH_CACHE_BYTES)
        with mock.patch.object(discovery, "_GRAPH_CACHE_BYTES", 1), \
             mock.patch.object(discovery, "_graph_cache_key", return_value=("oversized", "snapshot")), \
             mock.patch.object(discovery, "_load_navigation_graph", side_effect=lambda *_: iter(rows)) as loader:
            for _ in range(2):
                self.assertEqual(discovery._navigation_graph(None), rows)
            self.assertEqual(loader.call_count, 2)
            self.assertNotIn(("oversized", "snapshot"), discovery._GRAPH_CACHE)


if __name__ == "__main__":
    unittest.main()
