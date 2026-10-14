"""Entity validation must scale with nominated source paths."""
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
import question_investigation
from repository_discovery import ensure_repository_search


class QuestionEntityCostTests(unittest.TestCase):
    def test_unknown_entity_lookup_does_not_scan_unrelated_field_namespace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            for path, program in (("entry.cbl", "ENTRYRULE"), ("other.cbl", "OTHERRULE")):
                (source / path).write_text(f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {program}.\n"
                    "DATA DIVISION.\nWORKING-STORAGE SECTION.\n01 INPUT-AMOUNT PIC 9.\n"
                    "01 OUT-AMOUNT PIC 9.\nPROCEDURE DIVISION.\nMAIN.\n"
                    "COMPUTE OUT-AMOUNT = INPUT-AMOUNT * 2.\nGOBACK.\n", encoding="utf-8")
            database = root / "index.sqlite"
            build_business_index(source, database, source_format="free", verify_content=True)
            ensure_repository_search(database, source)
            question = "ENTRYRULE 的MISSING-AMOUNT从哪里来？"
            mapping = build_business_map(database, source, question)
            with closing(sqlite3.connect(database)) as db, db:
                columns = [row[1] for row in db.execute("PRAGMA table_info(symbols)")]
                template = dict(zip(columns, db.execute("SELECT * FROM symbols WHERE relative_path='other.cbl' "
                                                       "AND symbol_type='Field' LIMIT 1").fetchone()))
                rows = []
                for index in range(12000):
                    row = {**template, "symbol_id": f"noise-field-{index}", "name": f"NOISE-{index}",
                           "qualified_name": f"OTHERRULE::NOISE-{index}"}
                    rows.append(tuple(row[column] for column in columns))
                db.executemany("INSERT INTO symbols VALUES (" + ",".join("?" for _ in columns) + ")", rows)
            original = sqlite3.connect
            observations = []

            class ScopedConnection:
                def __init__(self, connection):
                    object.__setattr__(self, "connection", connection)

                def __getattr__(self, key):
                    return getattr(self.connection, key)

                def __setattr__(self, key, value):
                    setattr(self.connection, key, value)

                def execute(self, query, parameters=()):
                    if not query.startswith("SELECT 1 FROM symbols"):
                        return self.connection.execute(query, parameters)
                    steps = []
                    self.connection.set_progress_handler(lambda: steps.append(100) or 0, 100)
                    cursor = self.connection.execute(query, parameters)
                    self.connection.set_progress_handler(None, 0)
                    observations.append((sum(steps), query))
                    return cursor

            with mock.patch.object(question_investigation.sqlite3, "connect",
                                   side_effect=lambda *args, **kwargs: ScopedConnection(original(*args, **kwargs))):
                result = question_investigation.build_question_investigation(question, mapping,
                                                                             database_path=database)
            self.assertTrue(observations)
            self.assertLessEqual(sum(steps for steps, _ in observations), 3000)
            self.assertTrue(any(gap.get("reason") == "requested_field_not_bound"
                                for gap in result["open_gaps"]))
