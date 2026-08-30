from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from structural_index import build_structural_index, search_code  # noqa: E402


MAIN_PROGRAM = """000100 IDENTIFICATION DIVISION.
000200 PROGRAM-ID. PAYCALC.
000300 DATA DIVISION.
000400 WORKING-STORAGE SECTION.
000500 01 WS-ANNUAL-PREMIUM PIC 9(9)V99.
000600 01 WS-MODE-FACTOR PIC 9V9999.
000700 01 OUT-INSTALMENT-PREMIUM PIC 9(9)V99.
000800 01 WS-STATUS PIC X.
000900 01 WS-DYNAMIC-PGM PIC X(8).
001000 PROCEDURE DIVISION.
001100 MAIN-PARA.
001200     COPY PREMIUMCPY.
001300     CALL 'RATEPGM'.
001400     CALL WS-DYNAMIC-PGM.
001500     PERFORM CALC-PARA.
001600     READ POLICY-FILE.
001700     EXEC SQL
001800       SELECT RATE INTO :WS-MODE-FACTOR FROM RATE_TABLE
001900     END-EXEC.
002000     GOBACK.
002100 CALC-PARA.
002200     IF WS-STATUS = 'A'
002300         COMPUTE OUT-INSTALMENT-PREMIUM ROUNDED =
002400             WS-ANNUAL-PREMIUM * WS-MODE-FACTOR
002500     ELSE
002600         MOVE ZERO TO OUT-INSTALMENT-PREMIUM
002700     END-IF.
002800     EXIT.
"""

RATE_PROGRAM = """000100 IDENTIFICATION DIVISION.
000200 PROGRAM-ID. RATEPGM.
000300 PROCEDURE DIVISION.
000400 MAIN-PARA.
000500     GOBACK.
"""

COPYBOOK = """000100 01 PREMIUM-AREA.
000200    05 BASE-PREMIUM PIC 9(9)V99.
"""


class StructuralIndexTests(unittest.TestCase):
    def _make_repo(self, root: Path) -> None:
        programs = root / "programs"
        copybooks = root / "copybooks"
        programs.mkdir()
        copybooks.mkdir()
        (programs / "paycalc.cbl").write_text(
            MAIN_PROGRAM, encoding="utf-8"
        )
        (programs / "ratepgm.cbl").write_text(
            RATE_PROGRAM, encoding="utf-8"
        )
        (copybooks / "premiumcpy.cpy").write_text(
            COPYBOOK, encoding="utf-8"
        )

    def _build(self, root: Path) -> tuple[Path, dict[str, object]]:
        database = root / "index" / "structural.sqlite"
        report = build_structural_index(root, database, quiet=True)
        return database, report

    def test_builds_symbols_relations_and_exact_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._make_repo(root)
            database, report = self._build(root)

            self.assertFalse(report["privacy"]["network_calls"])
            self.assertEqual(report["files"]["indexed_or_updated"], 3)
            self.assertGreater(report["database_counts"]["code_units"], 10)

            connection = sqlite3.connect(database)
            connection.row_factory = sqlite3.Row
            try:
                symbol_rows = connection.execute(
                    """
                    SELECT symbol_type, name, program_name
                      FROM symbols
                     ORDER BY symbol_type, name
                    """
                ).fetchall()
                symbols = {
                    (row["symbol_type"], row["name"], row["program_name"])
                    for row in symbol_rows
                }
                self.assertIn(("Program", "PAYCALC", "PAYCALC"), symbols)
                self.assertIn(("Program", "RATEPGM", "RATEPGM"), symbols)
                self.assertIn(
                    ("Paragraph", "CALC-PARA", "PAYCALC"), symbols
                )
                self.assertIn(
                    ("Field", "OUT-INSTALMENT-PREMIUM", "PAYCALC"),
                    symbols,
                )
                self.assertIn(
                    ("Copybook", "PREMIUMCPY", None), symbols
                )

                relation_rows = connection.execute(
                    """
                    SELECT relation_type, target_name, status, metadata_json
                      FROM relations
                     WHERE relation_type IN (
                         'CALLS', 'CALL_TARGET_FROM', 'PERFORMS',
                         'INCLUDES_COPY', 'READS_FILE', 'SELECTS_FROM'
                     )
                    """
                ).fetchall()
                relations = {
                    (row["relation_type"], row["target_name"]): row
                    for row in relation_rows
                }
                self.assertEqual(
                    relations[("CALLS", "RATEPGM")]["status"], "confirmed"
                )
                self.assertEqual(
                    relations[("CALL_TARGET_FROM", "WS-DYNAMIC-PGM")][
                        "status"
                    ],
                    "confirmed",
                )
                dynamic_metadata = json.loads(
                    relations[
                        ("CALL_TARGET_FROM", "WS-DYNAMIC-PGM")
                    ]["metadata_json"]
                )
                self.assertEqual(
                    dynamic_metadata["boundary"],
                    "runtime_target_requires_value_flow",
                )
                self.assertEqual(
                    relations[("PERFORMS", "CALC-PARA")]["status"],
                    "confirmed",
                )
                self.assertEqual(
                    relations[("INCLUDES_COPY", "PREMIUMCPY")]["status"],
                    "confirmed",
                )
                self.assertEqual(
                    relations[("READS_FILE", "POLICY-FILE")]["status"],
                    "unresolved",
                )
                self.assertEqual(
                    relations[("SELECTS_FROM", "RATE_TABLE")]["status"],
                    "unresolved",
                )

                compute_row = connection.execute(
                    """
                    SELECT c.unit_id, c.start_line, c.end_line, e.text
                      FROM code_units AS c
                      JOIN evidence_spans AS e
                        ON e.evidence_id = c.evidence_id
                     WHERE c.unit_type = 'Statement'
                       AND c.name = 'COMPUTE'
                    """
                ).fetchone()
                self.assertIsNotNone(compute_row)
                self.assertEqual(compute_row["start_line"], 23)
                self.assertEqual(compute_row["end_line"], 24)
                self.assertIn("002300", compute_row["text"])
                self.assertIn("002400", compute_row["text"])

                compute_reads = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT target_name
                          FROM relations
                         WHERE from_entity_id = ?
                           AND relation_type = 'READS'
                           AND status = 'confirmed'
                        """,
                        (compute_row["unit_id"],),
                    )
                }
                self.assertEqual(
                    compute_reads,
                    {"WS-ANNUAL-PREMIUM", "WS-MODE-FACTOR"},
                )

                control_count = connection.execute(
                    """
                    SELECT COUNT(*)
                      FROM relations
                     WHERE relation_type = 'CONTROL_DEPENDS_ON'
                       AND status = 'confirmed'
                    """
                ).fetchone()[0]
                self.assertGreaterEqual(control_count, 2)

                write_row = connection.execute(
                    """
                    SELECT status, metadata_json
                      FROM relations
                     WHERE relation_type = 'WRITES'
                       AND target_name = 'OUT-INSTALMENT-PREMIUM'
                     ORDER BY relation_id
                     LIMIT 1
                    """
                ).fetchone()
                self.assertEqual(write_row["status"], "confirmed")
            finally:
                connection.close()

    def test_fts_search_returns_code_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._make_repo(root)
            database, _ = self._build(root)

            results = search_code(
                database, "OUT-INSTALMENT-PREMIUM", limit=10
            )

            self.assertTrue(results)
            self.assertTrue(
                any(
                    "OUT-INSTALMENT-PREMIUM"
                    in result["normalized_text"].upper()
                    for result in results
                )
            )
            self.assertTrue(
                all(not Path(result["relative_path"]).is_absolute()
                    for result in results)
            )

    def test_unchanged_files_are_skipped_and_changed_file_is_reindexed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._make_repo(root)
            database, first = self._build(root)
            second = build_structural_index(root, database, quiet=True)

            self.assertEqual(first["files"]["indexed_or_updated"], 3)
            self.assertEqual(second["files"]["indexed_or_updated"], 0)
            self.assertEqual(second["files"]["skipped_unchanged"], 3)
            self.assertEqual(
                first["snapshot_id"], second["snapshot_id"]
            )

            source = root / "programs" / "ratepgm.cbl"
            source.write_text(
                RATE_PROGRAM.replace(
                    "000500     GOBACK.",
                    "000500     DISPLAY 'CHANGED'.\n000600     GOBACK.",
                ),
                encoding="utf-8",
            )
            third = build_structural_index(root, database, quiet=True)

            self.assertEqual(third["files"]["indexed_or_updated"], 1)
            self.assertEqual(third["files"]["skipped_unchanged"], 2)
            self.assertNotEqual(first["snapshot_id"], third["snapshot_id"])

    def test_removed_file_facts_are_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._make_repo(root)
            database, _ = self._build(root)
            (root / "programs" / "ratepgm.cbl").unlink()

            report = build_structural_index(root, database, quiet=True)

            self.assertEqual(report["files"]["removed"], 1)
            connection = sqlite3.connect(database)
            try:
                program_count = connection.execute(
                    """
                    SELECT COUNT(*)
                      FROM symbols
                     WHERE symbol_type = 'Program'
                       AND name = 'RATEPGM'
                    """
                ).fetchone()[0]
                call_status = connection.execute(
                    """
                    SELECT status
                      FROM relations
                     WHERE relation_type = 'CALLS'
                       AND target_name = 'RATEPGM'
                    """
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(program_count, 0)
            self.assertEqual(call_status, "unresolved")


if __name__ == "__main__":
    unittest.main()
