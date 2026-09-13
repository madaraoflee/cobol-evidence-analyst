from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from repo_inventory import build_inventory, parse_extensions
from structural_index import build_structural_index, normalize_cobol_lines


def program(name: str, statement: str = "GOBACK.") -> str:
    return (
        "       IDENTIFICATION DIVISION.\n"
        f"       PROGRAM-ID. {name}.\n"
        "       PROCEDURE DIVISION.\n"
        f"           {statement}\n"
    )


class SourceIntakeTests(unittest.TestCase):
    def test_replacing_repository_removes_old_symbols_and_finds_extensionless_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            first_root = base / "first"
            first_root.mkdir()
            (first_root / "old.cbl").write_text(program("OLD-ENTRY"), encoding="utf-8")
            database = base / "index.sqlite"
            build_structural_index(first_root, database, quiet=True)

            replacement = base / "replacement"
            (replacement / "members").mkdir(parents=True)
            (replacement / "includes").mkdir()
            (replacement / "members" / "NEWENTRY").write_text(
                "       IDENTIFICATION DIVISION.\n"
                "       PROGRAM-ID. NEW-ENTRY.\n"
                "       DATA DIVISION.\n"
                "       WORKING-STORAGE SECTION.\n"
                "           COPY SHARED-DATA.\n"
                "       PROCEDURE DIVISION.\n"
                "           GOBACK.\n",
                encoding="utf-8",
            )
            (replacement / "includes" / "SHARED-DATA").write_text(
                "       01 SHARED-AREA.\n          05 RESULT-CODE PIC 9.\n",
                encoding="utf-8",
            )
            report = build_structural_index(
                replacement, database, include_extensionless=True, quiet=True
            )

            self.assertEqual(report["files"]["removed"], 1)
            self.assertEqual(report["diagnostics"]["program_count"], 1)
            self.assertEqual(report["diagnostics"]["copybook_count"], 1)
            with sqlite3.connect(database) as connection:
                names = connection.execute(
                    "SELECT name FROM symbols WHERE symbol_type = 'Program'"
                ).fetchall()
                self.assertEqual(names, [("NEW-ENTRY",)])
                self.assertEqual(connection.execute(
                    "SELECT status FROM relations WHERE relation_type = 'INCLUDES_COPY'"
                ).fetchall(), [("confirmed",)])

    def test_empty_or_binary_replacement_preserves_existing_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            (source / "entry.cbl").write_text(program("CURRENT-ENTRY"), encoding="utf-8")
            database = base / "index.sqlite"
            first = build_structural_index(source, database, quiet=True)
            empty = base / "empty"
            empty.mkdir()
            with self.assertRaisesRegex(ValueError, "No source files selected"):
                build_structural_index(empty, database, quiet=True)
            (empty / "binary.cbl").write_bytes(b"\x00\x01\x02\x00" * 100)
            with self.assertRaisesRegex(ValueError, "No source files could be decoded"):
                build_structural_index(empty, database, quiet=True)
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute(
                    "SELECT value FROM metadata WHERE key = 'snapshot_id'"
                ).fetchone()[0], first["snapshot_id"])
                self.assertEqual(connection.execute(
                    "SELECT name FROM symbols WHERE symbol_type = 'Program'"
                ).fetchall(), [("CURRENT-ENTRY",)])

    def test_explicit_encoding_supports_mainframe_and_multibyte_exports(self) -> None:
        for encoding, literal in (("cp037", "READY"), ("gb18030", "处理完成"), ("utf-16-le", "READY")):
            with self.subTest(encoding=encoding), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                source = base / "source"
                source.mkdir()
                (source / "entry.CBL").write_bytes(
                    program("CUSTOM-ENTRY", f"DISPLAY '{literal}'.").encode(encoding)
                )
                database = base / "index.sqlite"
                report = build_structural_index(source, database, encoding=encoding, quiet=True)
                self.assertEqual(report["diagnostics"]["program_count"], 1)
                with sqlite3.connect(database) as connection:
                    self.assertEqual(connection.execute(
                        "SELECT encoding FROM source_files"
                    ).fetchone()[0], encoding)
                    self.assertTrue(any(literal in row[0] for row in connection.execute(
                        "SELECT text FROM evidence_spans"
                    )))

    def test_encoding_change_reindexes_unchanged_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            (source / "entry.cbl").write_bytes(program("CUSTOM-ENTRY").encode("cp037"))
            database = base / "index.sqlite"
            initial = build_structural_index(source, database, encoding="latin-1", quiet=True)
            self.assertEqual(initial["diagnostics"]["program_count"], 0)
            corrected = build_structural_index(source, database, encoding="cp037", quiet=True)
            self.assertTrue(corrected["source_options_rebuild_required"])
            self.assertEqual(corrected["files"]["indexed_or_updated"], 1)
            self.assertEqual(corrected["diagnostics"]["program_count"], 1)

    def test_free_format_change_recovers_call_beyond_column_72(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            (source / "entry.cbl").write_text(
                program("CUSTOM-ENTRY", "CALL " + " " * 65 + "'EXTERNAL-RATE'."),
                encoding="utf-8",
            )
            database = base / "index.sqlite"
            initial = build_structural_index(source, database, quiet=True)
            self.assertTrue(any("column 72" in warning for warning in initial["diagnostics"]["warnings"]))
            corrected = build_structural_index(source, database, source_format="free", quiet=True)
            self.assertTrue(corrected["source_options_rebuild_required"])
            self.assertEqual(corrected["files"]["indexed_or_updated"], 1)
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute(
                    "SELECT target_name FROM relations WHERE relation_type = 'CALLS'"
                ).fetchall(), [("EXTERNAL-RATE",)])

    def test_directive_and_multiline_quoted_program_id_are_respected(self) -> None:
        text = (
            "       >>SOURCE FORMAT FREE\n"
            "       IDENTIFICATION DIVISION.\n"
            "       PROGRAM-ID.\n"
            "          'CUSTOM-ENTRY'.\n"
            "       PROCEDURE DIVISION.\n"
            "           CALL " + " " * 65 + "'EXTERNAL-RATE'.\n"
        )
        lines, _ = normalize_cobol_lines(text)
        self.assertTrue(any("EXTERNAL-RATE" in line.text for line in lines))
        declaration = next(line for line in lines if "PROGRAM-ID" in line.text)
        self.assertEqual((declaration.start_line, declaration.end_line), (3, 4))
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "entry.cbl").write_text(text, encoding="utf-8")
            report = build_structural_index(base, base / "index.sqlite", quiet=True)
            self.assertEqual(report["diagnostics"]["program_count"], 1)

    def test_discovery_explains_custom_extensions_and_excluded_members(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "member").write_text(program("MEMBER-ENTRY"), encoding="utf-8")
            (source / "entry.MBR").write_text(program("CUSTOM-ENTRY"), encoding="utf-8")
            report = build_inventory(source, quiet=True)
            self.assertEqual(report["discovery"]["excluded_extensionless_file_count"], 1)
            self.assertEqual(report["discovery"]["excluded_extensions"], {".mbr": 1})
            selected = build_structural_index(
                source, source / "index.sqlite", extensions=parse_extensions("mbr"), quiet=True
            )
            self.assertEqual(selected["diagnostics"]["program_count"], 1)

    def test_invalid_encoding_is_actionable_and_creates_no_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / "entry.cbl").write_text(program("CUSTOM-ENTRY"), encoding="utf-8")
            database = base / "index.sqlite"
            with self.assertRaisesRegex(ValueError, "Unknown text encoding"):
                build_structural_index(base, database, encoding="invalid-text-codec", quiet=True)
            self.assertFalse(database.exists())

    def test_free_format_group_binding_survives_cross_program_analysis(self) -> None:
        from error_paths import ErrorContract
        from interprogram_paths import audit_interprogram_paths

        def grouped(name: str, fields: tuple[str, ...]) -> str:
            return f"01 {name}.\n" + "\n".join(
                (f"           05 {field} ").ljust(80) + "PIC 9(4) COMP-5."
                for field in fields
            )

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "source"
            source.mkdir()
            (source / "entry.cbl").write_text(
                "IDENTIFICATION DIVISION.\nPROGRAM-ID. ROOTPG.\nDATA DIVISION.\n"
                "WORKING-STORAGE SECTION.\n"
                + grouped("SHARED-AREA", ("INPUT-VAL", "RESULT-VAL", "STATUS-VAL"))
                + "\nPROCEDURE DIVISION.\nMAIN-ENTRY.\n"
                "CALL 'LEAFPG' USING SHARED-AREA END-CALL.\nGOBACK.\n",
                encoding="utf-8",
            )
            (source / "worker.cbl").write_text(
                "IDENTIFICATION DIVISION.\nPROGRAM-ID. LEAFPG.\nDATA DIVISION.\n"
                "LINKAGE SECTION.\n"
                + grouped("ARG-AREA", ("ARG-VAL", "OUT-VAL", "CODE-VAL"))
                + "\nPROCEDURE DIVISION USING ARG-AREA.\nMAIN-ENTRY.\n"
                "MOVE 21 TO CODE-VAL.\nMOVE ZERO TO OUT-VAL.\nGOBACK.\n",
                encoding="utf-8",
            )
            database = base / "index.sqlite"
            index_report = build_structural_index(
                source, database, source_format="free", quiet=True
            )
            self.assertEqual(index_report["call_bindings"]["confirmed_bindings"], 4)
            report = audit_interprogram_paths(
                database,
                "ROOTPG",
                (
                    ErrorContract("ROOTPG", ("STATUS-VAL",), ("RESULT-VAL",)),
                    ErrorContract("LEAFPG", ("CODE-VAL",), ("OUT-VAL",)),
                ),
                call_policy="normal_return_only",
                initial_values={"INPUT-VAL": "5"},
            )
            self.assertTrue(report["summary"]["modeled_exploration_closed"])
            self.assertEqual(report["root_exits"][0]["status_values"], {"STATUS-VAL": "21"})
            self.assertEqual(report["root_exits"][0]["output_values"], {"RESULT-VAL": "0"})


if __name__ == "__main__":
    unittest.main()
