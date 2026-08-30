from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


POC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(POC_ROOT))

from repo_inventory import build_inventory, report_to_markdown, write_report  # noqa: E402


PROGRAM_A = """000100 IDENTIFICATION DIVISION.
000200 PROGRAM-ID. PAYCALC.
000300 DATA DIVISION.
000400 WORKING-STORAGE SECTION.
000500 01 WS-TOTAL PIC 9(9)V99.
000600 PROCEDURE DIVISION.
000700 MAIN-PARA.
000800     COPY PREMIUMCPY.
000900     CALL 'RATEPGM'.
001000     CALL WS-DYNAMIC-PGM.
001100     PERFORM CALC-PARA.
001200     EXEC SQL
001300       SELECT RATE INTO :WS-TOTAL FROM RATE_TABLE
001400     END-EXEC.
001500     GOBACK.
"""

PROGRAM_B = """       IDENTIFICATION DIVISION.
       PROGRAM-ID. RATEPGM.
       PROCEDURE DIVISION.
           GOBACK.
"""

COPYBOOK = """       01 PREMIUM-AREA.
          05 BASE-PREMIUM PIC 9(9)V99.
"""


class InventoryTests(unittest.TestCase):
    def _make_repo(self, root: Path) -> None:
        (root / "programs").mkdir()
        (root / "copybooks").mkdir()
        (root / "programs" / "paycalc.cbl").write_text(PROGRAM_A, encoding="utf-8")
        (root / "programs" / "ratepgm.cbl").write_text(PROGRAM_B, encoding="utf-8")
        (root / "copybooks" / "premiumcpy.cpy").write_text(COPYBOOK, encoding="utf-8")
        (root / ".git").mkdir()
        (root / ".git" / "ignored.cbl").write_text(PROGRAM_B, encoding="utf-8")
        (root / ".poc-data").mkdir()
        (root / ".poc-data" / "ignored.cbl").write_text(
            PROGRAM_B, encoding="utf-8"
        )

    def test_aggregate_report_excludes_identifiers_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._make_repo(root)

            report = build_inventory(root, quiet=True)

            self.assertNotIn("identifiers", report)
            self.assertEqual(report["snapshot"]["candidate_file_count"], 3)
            self.assertEqual(report["totals"]["program_definition_count"], 2)
            self.assertEqual(report["totals"]["copy_statement_count"], 1)
            self.assertEqual(report["totals"]["literal_call_count"], 1)
            self.assertEqual(report["totals"]["dynamic_call_count"], 1)
            self.assertEqual(report["totals"]["perform_statement_count"], 1)
            self.assertEqual(report["totals"]["exec_sql_block_count"], 1)
            serialized = json.dumps(report)
            self.assertNotIn("PAYCALC", serialized)
            self.assertNotIn("paycalc.cbl", serialized)
            self.assertFalse(report["privacy"]["network_calls"])

    def test_identifier_mode_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._make_repo(root)

            report = build_inventory(root, include_identifiers=True, quiet=True)

            self.assertIn("identifiers", report)
            self.assertIn("PAYCALC", report["identifiers"]["program_ids"])
            self.assertIn("programs/paycalc.cbl", report["identifiers"]["relative_files"])

    def test_snapshot_changes_only_when_source_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._make_repo(root)

            first = build_inventory(root, quiet=True)
            second = build_inventory(root, quiet=True)
            self.assertEqual(
                first["snapshot"]["snapshot_id"], second["snapshot"]["snapshot_id"]
            )

            source = root / "programs" / "ratepgm.cbl"
            source.write_text(PROGRAM_B + "\n       DISPLAY 'CHANGED'.\n", encoding="utf-8")
            changed = build_inventory(root, quiet=True)
            self.assertNotEqual(
                first["snapshot"]["snapshot_id"], changed["snapshot"]["snapshot_id"]
            )

    def test_cp950_source_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. BIG5PGM.\n       *> 保費計算\n"
            (root / "big5.cbl").write_bytes(source.encode("cp950"))

            report = build_inventory(root, quiet=True)

            self.assertEqual(report["distributions"]["encodings"]["cp950"], 1)
            self.assertEqual(report["totals"]["program_definition_count"], 1)

    def test_writes_json_and_markdown_without_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._make_repo(root)
            report = build_inventory(root, quiet=True)
            output_dir = root / "reports"
            json_path = output_dir / "inventory.json"
            markdown_path = output_dir / "inventory.md"

            write_report(report, json_path, markdown_path)

            parsed = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(parsed["schema_version"], "1.0")
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn("COBOL Repository Inventory", markdown)
            self.assertNotIn("PAYCALC", markdown)
            self.assertEqual(markdown, report_to_markdown(report))


if __name__ == "__main__":
    unittest.main()
