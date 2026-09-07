from __future__ import annotations

import json
from contextlib import closing
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from investigation_tools import InvestigationTools
from structural_index import PARSER_VERSION, build_structural_index


def program(name: str, copies: str = "COPY SHARED.", body: str = "MOVE 1 TO WS-AMOUNT.") -> str:
    return (
        f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\nDATA DIVISION.\n"
        f"WORKING-STORAGE SECTION.\n{copies}\nPROCEDURE DIVISION.\n"
        f"MAIN-PARA.\n{body}\nGOBACK.\n"
    )


class CopyExpansionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.database = self.root / "index.sqlite"
        self.write("shared.cpy", "01 WS-AMOUNT PIC 9(5).\n")
        self.write("first.cbl", program("FIRST"))

    def write(self, path: str, content: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def build(self) -> dict[str, object]:
        result = build_structural_index(self.root, self.database, quiet=True)
        self.tools = InvestigationTools(self.database)
        return result

    def rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return list(connection.execute(sql, params))

    def test_fields_bind_to_consuming_program_with_include_and_origin_evidence(self) -> None:
        self.write("second.cbl", program("SECOND"))
        self.build()
        first = self.tools.inspect_symbol("WS-AMOUNT", program_name="FIRST")
        second = self.tools.inspect_symbol("WS-AMOUNT", program_name="SECOND")
        self.assertEqual(first["status"], "OK")
        self.assertEqual(second["status"], "OK")
        match = first["matches"][0]
        self.assertNotEqual(match["symbol"]["symbol_id"], second["matches"][0]["symbol"]["symbol_id"])
        self.assertEqual(match["definition"]["evidence_ref"]["relative_path"], "shared.cpy")
        refs = match["definition"]["copy_binding"]["inclusion_evidence_refs"]
        self.assertEqual([ref["relative_path"] for ref in refs], ["first.cbl"])
        self.assertEqual({r["source"]["program_name"] for r in match["incoming_relations"]}, {"FIRST"})
        self.assertTrue(all(r["status"] == "confirmed" for r in match["incoming_relations"]))
        self.assertEqual(self.tools.inspect_symbol("WS-AMOUNT")["status"], "AMBIGUOUS")

    def test_nested_copy_preserves_complete_inclusion_path(self) -> None:
        self.write("shared.cpy", "01 SHARED-AREA.\nCOPY DETAIL.\n")
        self.write("detail.cpy", "05 WS-AMOUNT PIC 9(5).\n")
        report = self.build()
        match = self.tools.inspect_symbol("WS-AMOUNT", program_name="FIRST")["matches"][0]
        refs = match["definition"]["copy_binding"]["inclusion_evidence_refs"]
        self.assertEqual([r["relative_path"] for r in refs], ["first.cbl", "shared.cpy"])
        self.assertEqual(report["copy_expansion"]["incomplete_scopes"], 0)

    def test_unchanged_consumer_rebinds_when_copy_changes_or_is_removed(self) -> None:
        self.build()
        self.write("shared.cpy", "01 WS-NEW-AMOUNT PIC 9(5).\n")
        report = self.build()
        self.assertEqual(report["files"]["indexed_or_updated"], 1)
        self.assertEqual(self.tools.inspect_symbol("WS-AMOUNT", program_name="FIRST")["status"], "NOT_FOUND")
        self.assertEqual(self.tools.inspect_symbol("WS-NEW-AMOUNT", program_name="FIRST")["status"], "OK")
        (self.root / "shared.cpy").unlink()
        report = self.build()
        self.assertEqual(report["copy_expansion"]["bound_fields"], 0)
        self.assertEqual(report["copy_expansion"]["boundary_counts"], {"copy_target_not_found": 1})
        writer = self.rows("SELECT status, metadata_json FROM relations WHERE relation_type = 'WRITES'")[0]
        self.assertEqual(writer["status"], "unresolved")
        self.assertEqual(json.loads(writer["metadata_json"])["resolution_reason"], "copy_scope_incomplete")

    def test_unchanged_build_is_stable_and_removed_program_leaves_no_binding(self) -> None:
        first = self.build()
        ids = self.rows("SELECT symbol_id, unit_id FROM copy_expansions")[0]
        second = self.build()
        self.assertEqual(first["database_counts"], second["database_counts"])
        self.assertEqual(tuple(ids), tuple(self.rows("SELECT symbol_id, unit_id FROM copy_expansions")[0]))
        self.assertEqual(second["files"]["indexed_or_updated"], 0)
        (self.root / "first.cbl").unlink()
        self.build()
        self.assertEqual(self.rows("SELECT * FROM copy_expansions"), [])
        self.assertEqual(self.rows("SELECT * FROM code_units_fts WHERE program_name = 'FIRST'"), [])

    def test_parser_version_change_rebuilds_unchanged_sources(self) -> None:
        first = self.build()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE metadata SET value = 'older-parser' WHERE key = 'parser_version'")
        second = self.build()
        self.assertTrue(second["parser_rebuild_required"])
        self.assertEqual(second["files"]["indexed_or_updated"], 2)
        self.assertEqual(second["parser_version"], PARSER_VERSION)
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["database_counts"], second["database_counts"])

    def test_repeated_copy_and_local_collision_are_ambiguous(self) -> None:
        for copies in ("COPY SHARED.\nCOPY SHARED.", "01 WS-AMOUNT PIC 9(3).\nCOPY SHARED."):
            with self.subTest(copies=copies):
                self.write("first.cbl", program("FIRST", copies))
                self.build()
                result = self.tools.inspect_symbol("WS-AMOUNT", program_name="FIRST")
                self.assertEqual(result["status"], "AMBIGUOUS")
                writer = self.rows("SELECT * FROM relations WHERE relation_type = 'WRITES'")[0]
                self.assertEqual(writer["status"], "candidate")
                self.assertIsNone(writer["target_entity_id"])
                self.assertEqual(json.loads(writer["metadata_json"])["candidate_count"], 2)

    def test_replacing_and_library_forms_do_not_bind_original_names(self) -> None:
        for statement in ("COPY SHARED REPLACING WS-AMOUNT BY WS-TOTAL.", "COPY SHARED OF LIBRARY.", "COPY 'SHARED'.", "COPY."):
            with self.subTest(statement=statement):
                self.write("first.cbl", program("FIRST", statement))
                report = self.build()
                self.assertEqual(report["copy_expansion"]["bound_fields"], 0)
                self.assertEqual(report["copy_expansion"]["boundary_counts"], {"copy_form_not_supported": 1})
                self.assertEqual(self.tools.inspect_symbol("WS-AMOUNT", program_name="FIRST")["status"], "NOT_FOUND")

    def test_standalone_replace_in_consumer_or_copy_tree_blocks_bindings(self) -> None:
        for location in ("program", "copy", "inline_program", "inline_copy"):
            with self.subTest(location=location):
                directive = "REPLACE ==WS-AMOUNT== BY ==WS-TOTAL==.\n"
                if location.startswith("inline"):
                    directive = "01 UNUSED-FIELD PIC X. " + directive
                self.write("first.cbl", program("FIRST", (directive if location.endswith("program") else "") + "COPY SHARED."))
                self.write("shared.cpy", (directive if location.endswith("copy") else "") + "01 WS-AMOUNT PIC 9(5).\n")
                report = self.build()
                self.assertEqual(report["copy_expansion"]["bound_fields"], 0)
                self.assertEqual(report["copy_expansion"]["boundary_counts"], {"copy_form_not_supported": 1})
                writer = self.rows("SELECT status FROM relations WHERE relation_type = 'WRITES'")[0]
                self.assertEqual(writer["status"], "unresolved")

    def test_copy_text_in_literal_does_not_create_expansion(self) -> None:
        self.write("first.cbl", program("FIRST", "01 WS-AMOUNT PIC 9(5).", "DISPLAY 'COPY SHARED REPLACE WORD'.\nMOVE 1 TO WS-AMOUNT."))
        report = self.build()
        self.assertEqual(report["copy_expansion"]["bound_fields"], 0)
        self.assertEqual(report["copy_expansion"]["incomplete_scopes"], 0)
        self.assertEqual(self.rows("SELECT * FROM relations WHERE relation_type = 'INCLUDES_COPY'"), [])

    def test_cycles_and_duplicate_copy_names_are_bounded(self) -> None:
        self.write("shared.cpy", "01 WS-AMOUNT PIC 9(5).\nCOPY SHARED.\n")
        report = self.build()
        self.assertEqual(report["copy_expansion"]["bound_fields"], 1)
        self.assertEqual(report["copy_expansion"]["boundary_counts"], {"copy_cycle": 1})
        self.write("other/shared.cpy", "01 DIFFERENT-AMOUNT PIC 9(5).\n")
        report = self.build()
        self.assertEqual(report["copy_expansion"]["bound_fields"], 0)
        self.assertEqual(report["copy_expansion"]["boundary_counts"], {"copy_target_ambiguous": 1})

    def test_incomplete_foreign_scope_does_not_leak_incoming_edges(self) -> None:
        self.write("second.cbl", program("SECOND", "COPY MISSING."))
        self.build()
        inspection = self.tools.inspect_symbol("WS-AMOUNT", program_name="FIRST")
        self.assertEqual({r["source"]["program_name"] for r in inspection["matches"][0]["incoming_relations"]}, {"FIRST"})
        trace = self.tools.trace_relations("WS-AMOUNT", program_name="FIRST", direction="incoming", relation_types=["WRITES"], max_depth=1)
        self.assertEqual({r["source"]["program_name"] for r in trace["edges"]}, {"FIRST"})

    def test_real_factor_lookup_has_scoped_writer_and_reader(self) -> None:
        build_structural_index(POC_ROOT / "fixtures" / "synthetic-insurance-v1", self.database, quiet=True)
        tools = InvestigationTools(self.database)
        result = tools.inspect_symbol("WS-MODE-FACTOR", program_name="SYNP040")
        self.assertEqual(result["status"], "OK")
        incoming = result["matches"][0]["incoming_relations"]
        self.assertEqual({r["source"]["program_name"] for r in incoming}, {"SYNP040"})
        self.assertTrue(any(r["relation_type"] == "WRITES" and r["source"]["name"] == "EXEC_SQL" and r["status"] == "confirmed" for r in incoming))
        self.assertTrue(any(r["relation_type"] == "READS" and r["source"]["name"] == "COMPUTE" and r["status"] == "confirmed" for r in incoming))


if __name__ == "__main__":
    unittest.main()
