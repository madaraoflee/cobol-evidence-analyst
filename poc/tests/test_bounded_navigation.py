from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from business_map import build_business_map, question_boundaries
import business_map as mapping
from question_investigation import _question_scopes
import question_investigation as investigation
import repository_discovery as discovery


class BoundedNavigationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"

    def write(self, relative, name, body, data=""):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. " + name + ".\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 IN-AMOUNT PIC 9(6)V99 VALUE 1.\n01 OUT-AMOUNT PIC 9(6)V99.\n" + data
            + "PROCEDURE DIVISION.\n" + body + "\n", encoding="utf-8")

    def cycle(self, count=6, *, body=None, data=""):
        self.write("entry.cbl", "SCOPECASE", body or
            "MAIN.\nGOBACK.\nFOCUS-RULE.\nIF IN-AMOUNT > 0\n"
            "MOVE 1 TO OUT-AMOUNT\nEND-IF.\nEXIT.\nOUTSIDE-RULE.\n"
            'CALL "CYCLE-000".\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 7.\nEXIT.', data)
        for number in range(count):
            target = "SCOPECASE" if number == count - 1 else f"CYCLE-{number + 1:03d}"
            self.write(f"cycle/member-{number:03d}.cbl", f"CYCLE-{number:03d}",
                'MAIN.\nCALL "' + target + '".\nGOBACK.')

    def build(self):
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        discovery.ensure_repository_search(self.database, self.source)

    def map(self, question, **options):
        return build_business_map(self.database, self.source, question, **options)

    def test_exclusive_procedure_avoids_static_cycle_before_navigation(self):
        self.cycle()
        self.build()
        with mock.patch.object(discovery, "_navigation_graph", side_effect=AssertionError("global graph")), \
             mock.patch.object(mapping, "_navigation_graph", side_effect=AssertionError("global graph")):
            result = self.map("SCOPECASE 只看 FOCUS-RULE 的业务规则？")
        self.assertEqual(result["direct_paths"], ["entry.cbl"])
        self.assertEqual(result["selected_paths"], ["entry.cbl"])
        self.assertEqual(result["navigation_scope"]["status"], "version_matched_occurrence_roots")
        self.assertEqual(result["navigation_scope"]["root_paths"], ["entry.cbl"])
        self.assertFalse(result["relations"])
        self.assertTrue(result["rule_leads"])
        self.assertTrue(all(rule["paragraph_name"] == "FOCUS-RULE" for rule in result["rule_leads"]))

    def test_first_two_if_occurrences_do_not_follow_call_in_third_if(self):
        self.cycle(body="MAIN.\nGOBACK.\nFOCUS-RULE.\n"
            "IF IN-AMOUNT > 0\nMOVE 1 TO OUT-AMOUNT\nEND-IF.\n"
            "IF IN-AMOUNT > 1\nMOVE 2 TO OUT-AMOUNT\nEND-IF.\n"
            'IF IN-AMOUNT > 2\nCALL "CYCLE-000"\nEND-IF.\nEXIT.')
        self.build()
        question = "SCOPECASE FOCUS-RULE 前两个 IF 的业务规则？"
        scope = _question_scopes(self.database, ["entry.cbl"], question)[0]
        self.assertEqual(scope["range_basis"], "first_if_occurrences")
        with mock.patch.object(discovery, "_navigation_graph", side_effect=AssertionError("global graph")), \
             mock.patch.object(mapping, "_navigation_graph", side_effect=AssertionError("global graph")):
            result = self.map(question)
        self.assertEqual(result["selected_paths"], ["entry.cbl"])
        self.assertEqual(result["navigation_scope"]["source_scopes"][0]["range_basis"], "first_if_occurrences")
        self.assertFalse(result["relations"])
        self.assertTrue(result["rule_leads"])
        self.assertTrue(all(scope["start_line"] <= rule["start_line"] <= rule["end_line"]
                            for rule in result["rule_leads"]))
        self.assertFalse(any("CYCLE-000" in rule["statement"] for rule in result["rule_leads"]))

    def test_local_static_call_copy_and_unknown_effect_remain_evidence_candidates(self):
        self.cycle(body="MAIN.\nGOBACK.\nFOCUS-RULE.\n"
            'CALL "ACTIVE-WORKER" USING OUT-AMOUNT.\n'
            'CALL "UNAVAILABLE-WORKER" USING OUT-AMOUNT.\nEXIT.\nOUTSIDE-RULE.\n'
            'CALL "CYCLE-000".\nEXIT.', data="COPY LOCAL-LAYOUT.\n")
        (self.source / "local-layout.cpy").write_text("01 SHARED-STATE PIC X.\n", encoding="utf-8")
        self.write("active.cbl", "ACTIVE-WORKER", 'MAIN.\nCALL "ACTIVE-LEAF".\nGOBACK.')
        self.write("leaf.cbl", "ACTIVE-LEAF", "MAIN.\nMOVE 2 TO OUT-AMOUNT.\nGOBACK.")
        self.write("sibling.cbl", "OTHER-CALLER", 'MAIN.\nCALL "ACTIVE-WORKER".\nGOBACK.')
        self.build()
        with mock.patch.object(discovery, "_navigation_graph", side_effect=AssertionError("global graph")), \
             mock.patch.object(mapping, "_navigation_graph", side_effect=AssertionError("global graph")):
            result = self.map("SCOPECASE 只看 FOCUS-RULE 调用带来的副作用？")
        self.assertEqual(set(result["selected_paths"]),
                         {"entry.cbl", "active.cbl", "leaf.cbl", "local-layout.cpy"})
        self.assertNotIn("sibling.cbl", result["selected_paths"])
        targets = {edge["target_name"] for edge in result["relations"]}
        self.assertTrue({"ACTIVE-WORKER", "ACTIVE-LEAF", "LOCAL-LAYOUT", "UNAVAILABLE-WORKER"} <= targets)
        self.assertNotIn("CYCLE-000", targets)
        unknown = next(edge for edge in result["relations"] if edge["target_name"] == "UNAVAILABLE-WORKER")
        self.assertNotEqual(unknown["resolution"], "confirmed")
        self.assertIsNone(unknown["target_path"])
        self.assertTrue(any(boundary.get("target_name") == "UNAVAILABLE-WORKER"
                            for boundary in result["boundaries"]))

    def test_no_scope_compound_request_and_impact_preserve_existing_navigation(self):
        self.cycle(body="MAIN.\nGOBACK.\nFOCUS-RULE.\nIF IN-AMOUNT > 0\n"
            "MOVE 1 TO IN-AMOUNT\nEND-IF.\nEXIT.\nOUTSIDE-RULE.\n"
            'CALL "CYCLE-000".\nCOMPUTE OUT-AMOUNT = IN-AMOUNT * 7.\nEXIT.')
        self.build()
        expected = {"entry.cbl", *(f"cycle/member-{number:03d}.cbl" for number in range(6))}
        questions = (
            "SCOPECASE 的业务流程是什么？",
            "SCOPECASE 的 OUT-AMOUNT 如何计算，同时 FOCUS-RULE 的执行范围是什么？",
            "SCOPECASE 只看 FOCUS-RULE，修改这里会影响哪些程序？",
        )
        scopes = _question_scopes(self.database, ["entry.cbl"], questions[1])
        self.assertTrue(scopes)
        self.assertFalse(scopes[0]["exclusive"])
        for question in questions:
            with self.subTest(question=question):
                result = self.map(question)
                self.assertEqual(set(result["selected_paths"]), expected)
                self.assertTrue(any(edge["target_name"] == "CYCLE-000" for edge in result["relations"]))
                self.assertNotEqual(result.get("navigation_scope", {}).get("status"),
                                    "version_matched_occurrence_roots")

    def test_local_perform_keeps_calls_from_its_same_file_target(self):
        self.cycle(body="MAIN.\nGOBACK.\nFOCUS-RULE.\nPERFORM ACTIVE-RULE.\n"
            "PERFORM MISSING-RULE.\nEXIT.\nACTIVE-RULE.\n"
            'CALL "ACTIVE-WORKER".\nCALL "UNAVAILABLE-WORKER".\n'
            'EXIT.\nOUTSIDE-RULE.\nCALL "CYCLE-000".\nEXIT.')
        self.write("active.cbl", "ACTIVE-WORKER", "MAIN.\nMOVE 2 TO OUT-AMOUNT.\nGOBACK.")
        self.build()
        with mock.patch.object(discovery, "_navigation_graph", side_effect=AssertionError("global graph")), \
             mock.patch.object(mapping, "_navigation_graph", side_effect=AssertionError("global graph")):
            question = "SCOPECASE 只看 FOCUS-RULE 的业务规则？"
            result = self.map(question)
        self.assertEqual(set(result["selected_paths"]), {"entry.cbl", "active.cbl"})
        self.assertTrue(any(edge["target_name"] == "ACTIVE-WORKER" for edge in result["relations"]))
        self.assertFalse(any(edge["target_name"] == "CYCLE-000" for edge in result["relations"]))
        self.assertTrue(any(boundary.get("target_name") == "MISSING-RULE"
                            for boundary in result["boundaries"]))
        self.assertTrue(any(edge["target_name"] == "UNAVAILABLE-WORKER" for edge in result["relations"]))
        self.assertTrue(any(boundary.get("target_name") == "UNAVAILABLE-WORKER"
                            for boundary in result["boundaries"]))
        original_scopes = _question_scopes(self.database, ["entry.cbl"], question)
        relevant, catalog = question_boundaries(result["boundaries"], result["selected_paths"],
            source_scopes=original_scopes, source_versions=result["source_versions"])
        self.assertTrue(any(boundary.get("target_name") == "UNAVAILABLE-WORKER" for boundary in relevant))
        self.assertFalse(any(boundary.get("target_name") == "UNAVAILABLE-WORKER" for boundary in catalog))

    def test_perform_thru_keeps_unknown_call_at_inclusive_range_end(self):
        self.cycle(body="MAIN.\nGOBACK.\nFOCUS-RULE.\nPERFORM FIRST-RULE THRU LAST-RULE.\n"
            "EXIT.\nFIRST-RULE.\nMOVE 1 TO OUT-AMOUNT.\nMIDDLE-RULE.\nMOVE 2 TO OUT-AMOUNT.\n"
            'LAST-RULE.\nCALL "UNAVAILABLE-AT-END".\nEXIT.\n'
            'OUTSIDE-RULE.\nCALL "CYCLE-000".\nEXIT.')
        self.build()
        question = "SCOPECASE 只看 FOCUS-RULE 的业务规则？"
        with mock.patch.object(discovery, "_navigation_graph", side_effect=AssertionError("global graph")), \
             mock.patch.object(mapping, "_navigation_graph", side_effect=AssertionError("global graph")):
            result = self.map(question)
        self.assertEqual(result["selected_paths"], ["entry.cbl"])
        self.assertTrue(any(edge["target_name"] == "UNAVAILABLE-AT-END" for edge in result["relations"]))
        self.assertFalse(any(edge["target_name"] == "CYCLE-000" for edge in result["relations"]))
        self.assertTrue(any(boundary.get("target_name") == "UNAVAILABLE-AT-END"
                            for boundary in result["boundaries"]))
        original_scopes = _question_scopes(self.database, ["entry.cbl"], question)
        relevant, catalog = question_boundaries(result["boundaries"], result["selected_paths"],
            source_scopes=original_scopes, source_versions=result["source_versions"])
        self.assertTrue(any(boundary.get("target_name") == "UNAVAILABLE-AT-END" for boundary in relevant))
        self.assertFalse(any(boundary.get("target_name") == "UNAVAILABLE-AT-END" for boundary in catalog))

    def test_old_version_or_invented_procedure_range_cannot_prune_navigation(self):
        self.cycle()
        self.build()
        genuine = _question_scopes(self.database, ["entry.cbl"], "SCOPECASE 只看 FOCUS-RULE？")[0]
        scopes = (
            {**genuine, "source_sha256": "0" * 64},
            {**genuine, "procedure_name": "INVENTED-RULE"},
            {**genuine, "start_line": genuine["start_line"] + 1, "end_line": genuine["end_line"] - 1},
            {**genuine, "range_basis": "unverified_requested_range"},
            {**genuine, "range_basis": "first_if_occurrences", "occurrence_count": 2},
        )
        for candidate in scopes:
            with self.subTest(candidate=candidate):
                with mock.patch.object(investigation, "_question_scopes", return_value=[candidate]):
                    result = self.map("SCOPECASE 只看 FOCUS-RULE？")
                self.assertEqual(len(result["selected_paths"]), 7)
                self.assertTrue(any(edge["target_name"] == "CYCLE-000" for edge in result["relations"]))
                self.assertNotEqual(result.get("navigation_scope", {}).get("status"),
                                    "version_matched_occurrence_roots")

    def test_incomplete_nested_if_window_cannot_hide_later_call(self):
        self.cycle(body="MAIN.\nGOBACK.\nFOCUS-RULE.\nIF IN-AMOUNT > 0\n"
            "IF IN-AMOUNT > 1\nMOVE 1 TO OUT-AMOUNT\nEND-IF\n"
            'IF IN-AMOUNT > 2\nCALL "CYCLE-000"\nEND-IF\nEND-IF.\nEXIT.')
        self.build()
        question = "SCOPECASE FOCUS-RULE 前两个 IF 的业务规则？"
        scope = _question_scopes(self.database, ["entry.cbl"], question)[0]
        self.assertEqual(scope["range_basis"], "first_if_occurrences")
        result = self.map(question)
        self.assertEqual(len(result["selected_paths"]), 7)
        self.assertTrue(any(edge["target_name"] == "CYCLE-000" for edge in result["relations"]))
        self.assertNotEqual(result.get("navigation_scope", {}).get("status"),
                            "version_matched_occurrence_roots")

    def test_more_unrelated_cycle_members_do_not_increase_local_graph_work(self):
        self.cycle(100)
        self.build()
        original = discovery._connect

        def indexes():
            with closing(sqlite3.connect(self.database)) as connection:
                return connection.execute("SELECT name,tbl_name,sql FROM main.sqlite_schema "
                                          "WHERE type='index' ORDER BY name").fetchall()

        def measure():
            operations = []
            queries = []
            temporary_rows = []

            class ScopedConnection:
                def __init__(self, connection):
                    self.connection = connection

                def __getattr__(self, name):
                    return getattr(self.connection, name)

                def close(self):
                    table = self.connection.execute("SELECT 1 FROM temp.sqlite_schema "
                        "WHERE name='question_scope_occurrences'").fetchone()
                    if table:
                        temporary_rows.append(self.connection.execute(
                            "SELECT COUNT(*) FROM temp.question_scope_occurrences").fetchone()[0])
                    self.connection.close()

            def connect(*arguments, **options):
                connection = original(*arguments, **options)
                connection.set_progress_handler(lambda: operations.append(100) or 0, 100)
                connection.set_trace_callback(queries.append)
                return ScopedConnection(connection)

            catalog = indexes()
            with mock.patch.object(discovery, "_connect", side_effect=connect), \
                 mock.patch.object(mapping, "_connect", side_effect=connect), \
                 mock.patch.object(discovery, "_navigation_graph", side_effect=AssertionError("global graph")), \
                 mock.patch.object(mapping, "_navigation_graph", side_effect=AssertionError("global graph")):
                result = self.map("SCOPECASE 只看 FOCUS-RULE 的业务规则？")
            self.assertEqual(indexes(), catalog, "interactive scope must not create repository indexes")
            self.assertTrue(temporary_rows)
            self.assertTrue(all(0 < count <= 8 for count in temporary_rows),
                            "temporary facts must contain only the requested procedure")
            return result, sum(operations), queries

        before, baseline, before_queries = measure()
        self.cycle(200)
        self.build()
        after, operations, after_queries = measure()
        self.assertEqual(before["selected_paths"], after["selected_paths"])
        self.assertEqual(before["relations"], after["relations"])
        self.assertEqual(before["navigation_scope"]["relation_rows_read"],
                         after["navigation_scope"]["relation_rows_read"])
        self.assertLessEqual(after["navigation_scope"]["relation_rows_read"], 2)
        self.assertLessEqual(operations, baseline * 1.35 + 2500,
                            "local navigation must not decode the unrelated static cycle")
        for queries in (before_queries, after_queries):
            creates = [query for query in queries if query.lstrip().upper().startswith("CREATE INDEX")]
            self.assertTrue(all(query == "CREATE INDEX question_scope_occurrence_rule ON question_scope_occurrences(rule_id)"
                                for query in creates))
            self.assertFalse(any("INSERT INTO" in query.upper() for query in queries))


if __name__ == "__main__":
    unittest.main()
