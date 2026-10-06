"""The workspace graph preview keeps source order without scanning field facts."""

from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from structural_index import build_structural_index
import web_app


GRAPH_TYPES = "('CALLS','CALL_TARGET_FROM','INCLUDES_COPY')"
ORIGINAL_SQL = """SELECT r.relation_id, u.program_name AS source_program,
                  r.target_name, r.relation_type, r.status, r.target_entity_id,
                  e.evidence_id, e.relative_path, e.start_line, e.end_line
             FROM relations r
             JOIN code_units u ON u.unit_id = r.from_entity_id
             JOIN evidence_spans e ON e.evidence_id = r.evidence_id
            WHERE r.relation_type IN ('CALLS', 'CALL_TARGET_FROM', 'INCLUDES_COPY')
            ORDER BY e.relative_path, e.start_line, r.relation_type LIMIT ?"""


def original_preview(database, snapshot, limit):
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute(ORIGINAL_SQL, (limit + 1,))]
    return {"edges": rows[:limit], "truncated": len(rows) > limit, "snapshot_id": snapshot}


def program(name, calls, *, copies=False, linkage=False):
    lines = ["IDENTIFICATION DIVISION.", f"PROGRAM-ID. {name}.", "DATA DIVISION.",
             "WORKING-STORAGE SECTION.", "01 NEXT-PROGRAM PIC X(12)."]
    if copies:
        lines.append("COPY SHARED-AREA.")
    else:
        lines.append("01 REQUEST-AMOUNT PIC 9(5).")
    if linkage:
        lines.extend(["LINKAGE SECTION.", "01 INCOMING-AMOUNT PIC 9(5).",
                      "PROCEDURE DIVISION USING INCOMING-AMOUNT."])
    else:
        lines.append("PROCEDURE DIVISION.")
    lines.append("MAIN-PARA.")
    lines.extend("CALL 'SERVICE-FLOW' USING REQUEST-AMOUNT." for _ in range(calls))
    lines.extend(["CALL NEXT-PROGRAM.", "GOBACK."])
    return "\n".join(lines) + "\n"


class WorkspaceGraphPreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="workspace-graph-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "a-entry.cbl").write_text(program("ENTRY-FLOW", 90, copies=True), encoding="utf-8")
        (self.source / "middle.cbl").write_text(program("MIDDLE-FLOW", 70), encoding="utf-8")
        (self.source / "z-service.cbl").write_text(program("SERVICE-FLOW", 90, linkage=True), encoding="utf-8")
        (self.source / "shared-area.cpy").write_text(
            "01 REQUEST-AREA.\n05 REQUEST-AMOUNT PIC 9(5).\nCOPY INNER-AREA.\n", encoding="utf-8")
        (self.source / "inner-area.cpy").write_text("05 RESULT-STATE PIC X(12).\n", encoding="utf-8")
        self.database = self.root / "output" / "structural-index.sqlite"
        report = build_structural_index(self.source, self.database, encoding="utf-8", source_format="free", quiet=True)
        self.snapshot = report["snapshot_id"]

    def measured_preview(self):
        original_connect = sqlite3.connect
        instructions = 0
        statements = []

        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)

            def progress():
                nonlocal instructions
                instructions += 1
                return 0

            connection.set_progress_handler(progress, 1)
            connection.set_trace_callback(statements.append)
            return connection

        with mock.patch.object(web_app.sqlite3, "connect", side_effect=connect):
            result = web_app._relationships(self.database, self.snapshot)
        return result, instructions, statements

    def test_real_strict_copy_and_call_facts_match_original_preview(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            # Strict COPY expansion creates cross-file provenance, but those
            # derived containment facts do not belong to the graph preview.
            mismatches = dict(connection.execute(
                "SELECT r.relation_type,count(*) FROM relations r JOIN evidence_spans e "
                "ON e.evidence_id=r.evidence_id WHERE r.relative_path<>e.relative_path "
                "GROUP BY r.relation_type"))
            self.assertGreater(mismatches.get("CONTAINS", 0), 0)
            self.assertFalse(set(mismatches).intersection({"CALLS", "CALL_TARGET_FROM", "INCLUDES_COPY"}))
            missing_sources = connection.execute(
                "SELECT count(*) FROM relations r LEFT JOIN source_files f ON f.relative_path=r.relative_path "
                f"WHERE r.relation_type IN {GRAPH_TYPES} AND f.relative_path IS NULL").fetchone()[0]
            self.assertEqual(missing_sources, 0)
        expected = original_preview(self.database, self.snapshot, web_app.MAX_GRAPH_EDGES)
        result, _, statements = self.measured_preview()
        self.assertEqual(result, expected)
        self.assertEqual(len(result["edges"]), 200)
        self.assertTrue(result["truncated"])
        self.assertFalse(any("ORDER BY e.relative_path" in statement for statement in statements))

    def test_all_paths_and_untruncated_result_keep_original_order(self):
        with mock.patch.object(web_app, "MAX_GRAPH_EDGES", 1000):
            result = web_app._relationships(self.database, self.snapshot)
        self.assertEqual(result, original_preview(self.database, self.snapshot, 1000))
        self.assertFalse(result["truncated"])
        self.assertGreater(len({edge["relative_path"] for edge in result["edges"]}), 2)
        self.assertEqual({edge["relation_type"] for edge in result["edges"]},
                         {"CALLS", "CALL_TARGET_FROM", "INCLUDES_COPY"})

    def test_cross_file_graph_provenance_uses_original_global_order(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            unit = connection.execute("SELECT unit_id FROM code_units WHERE relative_path='z-service.cbl' LIMIT 1").fetchone()[0]
            evidence = connection.execute("SELECT evidence_id FROM evidence_spans WHERE relative_path='a-entry.cbl' "
                                          "ORDER BY start_line LIMIT 1").fetchone()[0]
            connection.execute("INSERT INTO relations VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("preview-cross-file", "z-service.cbl", unit, "CALLS", "ENTRY-FLOW", None, None,
                 "unresolved", evidence, "{}"))
        result, _, statements = self.measured_preview()
        self.assertEqual(result, original_preview(self.database, self.snapshot, web_app.MAX_GRAPH_EDGES))
        self.assertEqual(result["edges"][0]["relation_id"], "preview-cross-file")
        self.assertTrue(any("ORDER BY e.relative_path" in statement for statement in statements))

    def test_archive_without_path_index_keeps_original_preview(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DROP INDEX repo_relations_path")
        self.assertEqual(web_app._relationships(self.database, self.snapshot),
                         original_preview(self.database, self.snapshot, web_app.MAX_GRAPH_EDGES))
        with closing(sqlite3.connect(self.database)) as connection, connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='repo_relations_path'").fetchone())

    def test_snapshot_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "relationship snapshot mismatch"):
            web_app._relationships(self.database, "different-snapshot")

    def test_unrelated_field_growth_does_not_increase_query_work(self):
        before, before_instructions, _ = self.measured_preview()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            unit, evidence = connection.execute("SELECT unit_id,evidence_id FROM code_units "
                                                "WHERE relative_path='a-entry.cbl' LIMIT 1").fetchone()
            connection.executemany("INSERT INTO relations VALUES (?,?,?,?,?,?,?,?,?,?)", (
                (f"preview-field-{number}", "a-entry.cbl", unit, "READS" if number % 2 else "WRITES",
                 "REQUEST-AMOUNT", "ENTRY-FLOW", None, "unresolved", evidence, "{}")
                for number in range(30000)))
        after, after_instructions, statements = self.measured_preview()
        self.assertEqual(after, before)
        self.assertLess(after_instructions, before_instructions * 1.1)
        self.assertFalse(any("CREATE " in statement.upper() or "INSERT " in statement.upper() for statement in statements))

    def test_preview_does_not_read_source_contents(self):
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.resolve().is_relative_to(self.source):
                raise AssertionError("The graph preview must only use published metadata.")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", guarded_open):
            result = web_app._relationships(self.database, self.snapshot)
        self.assertEqual(result, original_preview(self.database, self.snapshot, web_app.MAX_GRAPH_EDGES))


if __name__ == "__main__":
    unittest.main()
