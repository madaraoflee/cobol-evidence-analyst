from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import _ensure_business_rules, build_business_index
from business_map import _rule_leads, build_business_map
from question_investigation import build_question_investigation
import business_map as business_mapping
import repository_discovery as discovery


class BusinessMapSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.database = self.root / "index.sqlite"

    def write(self, relative, name, body):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\n"
            "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 WORK-COUNT PIC 9(9).\n"
            "01 FINALRESULT PIC 9(9).\n01 INPUTVALUE PIC 9(9).\nPROCEDURE DIVISION.\nMAIN.\n"
            + body + "\nGOBACK.\n", encoding="utf-8")

    def build(self):
        build_business_index(self.source, self.database, source_format="free", verify_content=True)
        discovery.ensure_repository_search(self.database, self.source)

    def test_program_identity_retains_a_distinct_deep_result_within_four_rules(self):
        body = "\n".join(f"COMPUTE WORK-COUNT = {index} + 1." for index in range(12))
        body += "\n" + "*> neutral source comment\n" * 700
        body += "DEEP.\nIF INPUTVALUE > 5\nCOMPUTE FINALRESULT = INPUTVALUE * 2 + 37\nEND-IF."
        self.write("entry.cbl", "COUNTPLAN", body)
        self.build()
        result = build_business_map(self.database, self.source, "COUNTPLAN 如何计算？")
        self.assertEqual(result["source_identity"]["direct_paths"], ["entry.cbl"])
        self.assertLessEqual(len(result["rule_leads"]), 4)
        self.assertTrue(any("FINALRESULT" in rule["writes"] for rule in result["rule_leads"]))
        self.assertTrue(any("FINALRESULT" in anchor["fields"] for anchor in result["semantic_anchors"]))
        self.assertLessEqual(len(result["semantic_anchors"]), 4)
        coverage = result["rule_lead_coverage"]
        self.assertGreater(coverage["omitted_rules_per_path"]["entry.cbl"], 0)
        self.assertFalse(coverage["candidate_selection_complete"])

    def test_followup_result_field_ranks_before_an_early_counter(self):
        body = "\n".join(f"COMPUTE WORK-COUNT = {index} + 1." for index in range(12))
        body += "\nDEEP.\nCOMPUTE FINALRESULT = INPUTVALUE * 2 + 37."
        self.write("entry.cbl", "COUNTPLAN", body)
        self.build()
        result = build_business_map(self.database, self.source, "COUNTPLAN 如何计算？", search_terms=["FINALRESULT"])
        self.assertIn("FINALRESULT", result["rule_leads"][0]["writes"])
        self.assertIn("FINALRESULT", result["semantic_anchors"][0]["fields"])
        longer = build_business_map(self.database, self.source,
            "COUNTPLAN explain every calculation including intermediate temporary counters and final processing details",
            search_terms=["FINALRESULT"])
        self.assertIn("FINALRESULT", longer["semantic_anchors"][0]["fields"])
        self.assertGreater(longer["rule_lead_coverage"]["omitted_rule_query_terms"], 0)

    def test_resolved_identity_never_starts_unscoped_full_text_discovery(self):
        self.write("aa/target.cbl", "COUNTFIRST", "COMPUTE FINALRESULT = INPUTVALUE * 2.")
        self.write("bb/target.cbl", "COUNTSECOND", "COMPUTE FINALRESULT = INPUTVALUE * 3.")
        self.write("noise.cbl", "ONLYNOISE", "*> aa/target.cbl COUNTFIRST\nCONTINUE.")
        self.build()
        original = discovery._connect
        queries = []

        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            connection.set_trace_callback(queries.append)
            return connection

        with mock.patch.object(discovery, "_connect", side_effect=connect), \
             mock.patch.object(business_mapping, "_connect", side_effect=connect):
            mapping = build_business_map(self.database, self.source, "aa/target.cbl 的计算公式？")
            context = discovery.retrieve_repository_context(self.database, self.source, "aa/target.cbl 的计算公式？")
        self.assertEqual(mapping["direct_paths"], ["aa/target.cbl"])
        self.assertEqual({page["relative_path"] for page in context["pages"]}, {"aa/target.cbl"})
        full_text_queries = [query for query in queries if "repo_fts MATCH" in query]
        self.assertTrue(full_text_queries)
        self.assertTrue(all("p.relative_path IN ('aa/target.cbl')" in query for query in full_text_queries))

    def test_program_names_read_only_selected_paths_even_with_many_unrelated_units(self):
        self.write("target.cbl", "TARGETPLAN", "COMPUTE FINALRESULT = INPUTVALUE * 2.")
        self.write("other.cbl", "OTHERPLAN", "CONTINUE.")
        self.build()
        original = business_mapping._connect

        def measure():
            work = []
            def connect(*args, **kwargs):
                connection = original(*args, **kwargs)
                connection.set_progress_handler(lambda: work.append(100) or 0, 100)
                return connection
            with mock.patch.object(business_mapping, "_connect", side_effect=connect):
                result = build_business_map(self.database, self.source, "target.cbl 的逻辑是什么？")
            return result, sum(work)

        before, baseline = measure()
        with sqlite3.connect(self.database) as connection:
            unit = connection.execute("SELECT * FROM code_units WHERE relative_path='other.cbl' LIMIT 1").fetchone()
            for index in range(12000):
                row = list(unit)
                row[0], row[2], row[3] = f"unrelated-unit-{index}", "Statement", "OTHER"
                connection.execute("INSERT INTO code_units VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
                connection.execute("INSERT INTO relations VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (f"unrelated-perform-{index}", "other.cbl", row[0], "PERFORMS", "OTHER",
                     "OTHERPLAN", None, "unresolved", row[10], "{}"))
        after, work = measure()
        self.assertEqual(after, before)
        self.assertLessEqual(work, baseline * 2 + 1000)

    def test_unmatched_business_question_leaves_discovery_open_without_global_dependencies(self):
        self.write("entry.cbl", "ENTRYPLAN", 'CALL "MISSING-WORKER".')
        self.write("other.cbl", "OTHERPLAN", "CONTINUE.")
        self.build()
        question = "说一说 billing notice 的不同触发条件，还有保费计算是怎么样的。"
        with mock.patch.object(discovery, "_dependency_rows", side_effect=AssertionError("global relation lookup")):
            mapping = build_business_map(self.database, self.source, question)
        self.assertEqual(mapping["matched_files"], 0)
        self.assertEqual(mapping["direct_paths"], [])
        self.assertEqual(mapping["selected_paths"], [])
        self.assertEqual(mapping["relations"], [])
        investigation = build_question_investigation(question, mapping, database_path=self.database)
        self.assertEqual(investigation["state"], "unresolved")
        self.assertFalse(investigation["can_answer"])
        self.assertEqual(investigation["open_gaps"][0]["reason"], "formula_not_located")

    def test_complete_field_phrase_limits_rules_despite_shared_word_noise(self):
        self.write("target.cbl", "RESULTPLAN", "COMPUTE RESULT-AMOUNT = INPUTVALUE * 2 + 37.")
        for index in range(160):
            self.write(f"noise/member-{index:03d}.cbl", f"NOISE{index}",
                       "*> amount is a common source word\nCOMPUTE WORK-COUNT = INPUTVALUE + 1.")
        self.build()
        for question, search_terms in (("How is RESULT-AMOUNT calculated?", None),
                                       ("Explain the calculation", ["RESULT-AMOUNT"])):
            with self.subTest(question=question):
                result = build_business_map(self.database, self.source, question, search_terms=search_terms)
                self.assertEqual(result["source_identity"]["status"], "none")
                self.assertEqual(result["source_identity"]["direct_paths"], [])
                self.assertGreater(result["matched_files"], 1)
                self.assertEqual(result["direct_paths"], ["target.cbl"])
                self.assertEqual(result["selected_paths"], ["target.cbl"])
                self.assertEqual(result["rule_lead_coverage"]["path_candidates"], 1)
                self.assertTrue(result["rule_leads"])
                self.assertEqual({rule["relative_path"] for rule in result["rule_leads"]}, {"target.cbl"})

    def test_unresolved_identity_does_not_start_unscoped_phrase_lookup(self):
        self.write("aa/target.cbl", "COUNTFIRST", "COMPUTE RESULT-AMOUNT = INPUTVALUE * 2.")
        self.write("bb/target.cbl", "COUNTSECOND", "COMPUTE RESULT-AMOUNT = INPUTVALUE * 3.")
        self.build()
        original = discovery._connect
        queries = []

        def connect(*args, **kwargs):
            connection = original(*args, **kwargs)
            connection.set_trace_callback(queries.append)
            return connection

        for question, status in (("target.cbl RESULT-AMOUNT", "ambiguous"),
                                 ("missing/target.cbl RESULT-AMOUNT", "not_found")):
            with self.subTest(status=status):
                queries.clear()
                with mock.patch.object(discovery, "_connect", side_effect=connect), \
                     mock.patch.object(business_mapping, "_connect", side_effect=connect):
                    result = build_business_map(self.database, self.source, question)
                self.assertEqual(result["source_identity"]["status"], status)
                self.assertEqual(result["direct_paths"], [])
                self.assertEqual(result["rule_leads"], [])
                self.assertFalse(any("repo_fts MATCH" in query for query in queries))

    def test_budget_omissions_are_counted_with_deterministic_path_selection(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        _ensure_business_rules(connection)
        for index in range(5):
            connection.execute("INSERT INTO business_rules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"rule{index}", "member-000.cbl", "RULEDEMO", "MAIN", "COMPUTE",
                 100 + index, 100 + index, 1, f"COMPUTE TARGETVALUE = {index} + 1", "[]", "[]", "[]"))
        connection.execute("INSERT INTO business_rules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("deep", "member-090.cbl", "RULEDEMO", "MAIN", "COMPUTE", 900, 900, 1,
             "COMPUTE TARGETVALUE = 37", "[]", "[]", "[]"))
        paths = {f"member-{index:03d}.cbl" for index in range(100)}
        previews = [{"relative_path": "member-000.cbl", "start_line": 100, "end_line": 104}] * 33
        records = []
        for order in (paths, set(reversed(sorted(paths)))):
            coverage = {}
            selected = _rule_leads(connection, order, previews, "TARGETVALUE", None, coverage=coverage)
            records.append((selected, coverage))
            self.assertEqual(coverage["omitted_previews"], 1)
            self.assertEqual(coverage["omitted_paths"], 20)
            self.assertEqual(coverage["omitted_rules_per_path"], {"member-000.cbl": 1})
            self.assertFalse(coverage["candidate_selection_complete"])
            self.assertEqual([rule["start_line"] for rule in selected], [100, 101, 102, 103])
        self.assertEqual(json.dumps(records[0], sort_keys=True), json.dumps(records[1], sort_keys=True))

    def test_generic_calculation_fallback_counts_its_omitted_candidate(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        _ensure_business_rules(connection)
        for index in range(4):
            connection.execute("INSERT INTO business_rules VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"rule{index}", "target.cbl", "RESULTPLAN", "DEEP", "COMPUTE",
                 900 + index, 900 + index, 1, f"COMPUTE OUTPUTVALUE = {index} + 1", "[]", "[]", "[]"))
        coverage = {}
        leads = _rule_leads(connection, {"target.cbl"},
            [{"relative_path": "target.cbl", "start_line": 1, "end_line": 5}],
            "calculation", None, coverage=coverage)
        self.assertEqual([rule["start_line"] for rule in leads], [900, 901, 902])
        self.assertTrue(all(rule["selection_reason"] == "program_calculation" for rule in leads))
        self.assertEqual(coverage["query_frontier"], [{"relative_path": "target.cbl",
            "reason": "program_calculation_budget", "minimum_omitted_rules": 1}])
        self.assertFalse(coverage["candidate_selection_complete"])


if __name__ == "__main__":
    unittest.main()
