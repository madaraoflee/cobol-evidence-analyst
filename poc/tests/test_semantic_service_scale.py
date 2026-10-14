from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from business_index import build_business_index
from semantic_service import FactStore, SemanticService
from structural_index import build_structural_index


def program(name, body):
    return ("IDENTIFICATION DIVISION.\nPROGRAM-ID. " + name + ".\nDATA DIVISION.\nWORKING-STORAGE SECTION.\n"
            "01 INPUT-AMOUNT PIC 9(7)V99.\n01 OUTPUT-AMOUNT PIC 9(7)V99.\nPROCEDURE DIVISION.\nMAIN.\n" + body + "\n")


class SemanticServiceScaleTests(unittest.TestCase):
    def test_repeated_rule_shape_has_every_physical_occurrence(self):
        with tempfile.TemporaryDirectory() as folder:
            root, count = Path(folder), 5000
            (root / "rule.cbl").write_text(program("RULE-FLOW", "COMPUTE OUTPUT-AMOUNT = INPUT-AMOUNT * 2.\n" * count + "GOBACK."))
            database = root / "facts.sqlite"
            build_business_index(root, database, source_format="free")
            with closing(sqlite3.connect(database)) as db:
                self.assertEqual(db.execute("SELECT count(*),sum(occurrence_count) FROM business_rules WHERE rule_kind='COMPUTE'").fetchone(), (1, count))
                self.assertEqual(db.execute("SELECT count(*),count(DISTINCT start_line) FROM business_rule_occurrences").fetchone(), (count, count))
                self.assertEqual(db.execute("SELECT count(*) FROM code_units WHERE unit_type='Statement'").fetchone()[0], 0)

    def test_fact_store_reads_one_program_and_reuses_its_capture_and_parse(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for index in range(80):
                (root / f"rule-{index:03d}.cbl").write_text(program(f"RULE-{index:03d}", "COMPUTE OUTPUT-AMOUNT = INPUT-AMOUNT * 2.\nGOBACK."))
            database = root / "facts.sqlite"
            build_structural_index(root, database, source_format="free")
            facts = FactStore(database, root, source_format="free")
            with mock.patch.object(facts, "rows", wraps=facts.rows) as calls:
                first = facts.program("rule-000.cbl")
                initial_queries, initial_parses = facts.query_count, len(facts._parses)
                with mock.patch("semantic_service.read_source_document", side_effect=AssertionError("warm source read")):
                    for _ in range(100):
                        self.assertIs(facts.program("rule-000.cbl"), first)
                self.assertEqual(facts.query_count, initial_queries)
                self.assertEqual(len(facts._parses), initial_parses)
                self.assertTrue(all("WHERE" in call.args[0].upper() and call.args[1] == ("rule-000.cbl",) for call in calls.call_args_list))
                self.assertEqual(len(facts._programs), 1)

    def test_repeated_perform_binds_new_context_without_reparsing_shared_body(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "rule.cbl").write_text(program("RULE-FLOW", "PERFORM CALCULATE\n" * 100 +
                "GOBACK.\nCALCULATE.\nCOMPUTE OUTPUT-AMOUNT = INPUT-AMOUNT * 2."))
            database = root / "facts.sqlite"
            build_structural_index(root, database, source_format="free")
            facts = FactStore(database, root, source_format="free")
            service = SemanticService(facts).build("rule.cbl")
            writes = [node for node in service.nodes.values() if node.kind == "COMPUTE"]
            self.assertEqual(len(writes), 100)
            self.assertEqual(len({node.context for node in writes}), 100)
            self.assertLessEqual(facts.query_count, 2)
            self.assertEqual(len(facts._parses), 2)
