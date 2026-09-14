from __future__ import annotations

import contextlib
from contextlib import closing
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyze_source import analyze_source, main
from company_api import CompanyAPIConfig
from investigation_tools import InvestigationTools
from run_demo import main as demo_main


def program(name: str, factor: int = 2) -> str:
    return (
        f"IDENTIFICATION DIVISION.\nPROGRAM-ID. {name}.\n"
        "DATA DIVISION.\nWORKING-STORAGE SECTION.\n"
        "01 INPUT-COUNT PIC 9(4).\n01 OUTPUT-COUNT PIC 9(6).\n"
        "PROCEDURE DIVISION.\nCALCULATE-COUNT.\n"
        f"COMPUTE OUTPUT-COUNT = INPUT-COUNT * {factor} END-COMPUTE.\n"
        "GOBACK.\n"
    )


class AnalyzeSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "exported members"
        self.source.mkdir()
        self.output = self.root / "analysis output"

    def write_program(self, name: str = "BATCHENTRY", filename: str = "downloaded-member", factor: int = 2) -> Path:
        path = self.source / filename
        path.write_text(program(name, factor), encoding="utf-8")
        return path

    def report(self, filename: str) -> dict:
        return json.loads((self.output / filename).read_text(encoding="utf-8"))

    def test_default_reads_extensionless_arbitrary_program_without_api_config(self) -> None:
        self.write_program()
        with mock.patch("analyze_source.run_investigation") as investigate:
            result = analyze_source(self.source, self.output)
        self.assertIn(result["runner_status"], {"INDEX_READY", "NEEDS_ATTENTION"})
        self.assertEqual(result["program_count"], 1)
        self.assertEqual(self.report("programs.json")["programs"][0]["program_name"], "BATCHENTRY")
        self.assertTrue(result["source_manifest_verified"])
        investigate.assert_not_called()

    def test_replacement_removes_previous_program_and_uses_current_source(self) -> None:
        old_file = self.write_program("BEFOREENTRY", "old.cbl")
        first = analyze_source(self.source, self.output)
        old_file.unlink()
        self.write_program("AFTERENTRY", "new.cob")
        second = analyze_source(self.source, self.output)
        self.assertNotEqual(first["build_report"]["snapshot_id"], second["build_report"]["snapshot_id"])
        self.assertEqual(second["build_report"]["files"]["removed"], 1)
        tools = InvestigationTools(self.output / "structural-index.sqlite")
        self.assertEqual(tools.inspect_symbol("BEFOREENTRY")["status"], "NOT_FOUND")
        self.assertEqual(tools.inspect_symbol("AFTERENTRY")["status"], "OK")

    def test_switching_root_with_same_relative_name_refreshes_contents(self) -> None:
        self.write_program("FIRSTENTRY", "member.cbl")
        analyze_source(self.source, self.output)
        other = self.root / "second export"
        other.mkdir()
        (other / "member.cbl").write_text(program("SECONDENTRY", 8))
        result = analyze_source(other, self.output)
        self.assertEqual(result["source_root"], str(other.resolve()))
        self.assertEqual(self.report("programs.json")["programs"][0]["program_name"], "SECONDENTRY")
        with closing(sqlite3.connect(self.output / "structural-index.sqlite")) as connection:
            text = "\n".join(row[0] for row in connection.execute("SELECT text FROM evidence_spans"))
        self.assertIn("* 8", text)
        self.assertNotIn("FIRSTENTRY", text)

    def test_changed_formula_refreshes_evidence_without_manual_reindex(self) -> None:
        self.write_program(factor=2)
        first = analyze_source(self.source, self.output)
        self.write_program(factor=9)
        second = analyze_source(self.source, self.output)
        self.assertNotEqual(first["build_report"]["snapshot_id"], second["build_report"]["snapshot_id"])
        with closing(sqlite3.connect(self.output / "structural-index.sqlite")) as connection:
            rows = connection.execute("SELECT text FROM evidence_spans WHERE text LIKE '%COMPUTE%'").fetchall()
        self.assertTrue(rows)
        self.assertTrue(all("* 9" in row[0] for row in rows))

    def test_empty_replacement_blocks_api_and_clears_stale_answer_reports(self) -> None:
        source_file = self.write_program()
        analyze_source(self.source, self.output)
        (self.output / "agent-result.md").write_text("OBSOLETE ANSWER")
        source_file.unlink()
        with mock.patch("analyze_source.run_investigation") as investigate:
            result = analyze_source(self.source, self.output, question="Explain", allow_network=True)
        self.assertEqual(result["runner_status"], "BLOCKED")
        self.assertIsNone(self.report("agent-result.json")["agent_result"])
        self.assertNotIn("OBSOLETE ANSWER", (self.output / "agent-result.md").read_text())
        investigate.assert_not_called()

    def test_unrecognized_source_and_bad_entry_block_before_api(self) -> None:
        path = self.source / "notes.txt"
        path.write_text("Readable text without a program definition.")
        with mock.patch("analyze_source.run_investigation") as investigate:
            result = analyze_source(self.source, self.output, question="Explain", allow_network=True)
        self.assertEqual(result["reason_code"], "NO_PROGRAMS_RECOGNIZED")
        investigate.assert_not_called()
        path.unlink()
        self.write_program()
        with mock.patch("analyze_source.run_investigation") as investigate:
            result = analyze_source(self.source, self.output, entry="MISSING", question="Explain", allow_network=True)
        self.assertEqual(result["reason_code"], "ENTRY_NOT_FOUND")
        investigate.assert_not_called()

    def test_missing_source_invalidates_previous_success_reports(self) -> None:
        self.write_program()
        analyze_source(self.source, self.output)
        result = analyze_source(self.root / "missing-export", self.output)
        self.assertEqual(result["runner_status"], "BLOCKED")
        self.assertEqual(self.report("programs.json")["programs"], [])
        self.assertEqual(self.report("diagnosis.json")["runner_status"], "BLOCKED")

    def test_missing_fts5_is_saved_as_actionable_failure(self) -> None:
        self.write_program()
        with mock.patch("analyze_source.build_structural_index", side_effect=RuntimeError("This SQLite build does not include FTS5 support.")):
            result = analyze_source(self.source, self.output)
        self.assertEqual(result["runner_status"], "BLOCKED")
        self.assertIn("FTS5", self.report("diagnosis.json")["messages"][0])

    def test_missing_copy_is_reported_as_incomplete_source_dependency(self) -> None:
        path = self.write_program()
        path.write_text(path.read_text().replace("WORKING-STORAGE SECTION.", "WORKING-STORAGE SECTION.\nCOPY MISSINGAREA."))
        result = analyze_source(self.source, self.output)
        self.assertEqual(result["runner_status"], "NEEDS_ATTENTION")
        self.assertTrue(any(item["relation_type"] == "INCLUDES_COPY" and item["target_name"] == "MISSINGAREA"
                            for item in result["unresolved_dependencies"]))

    def test_duplicate_program_ids_block_even_with_specific_file(self) -> None:
        self.write_program(filename="version-a.cbl")
        self.write_program(filename="version-b.cbl")
        result = analyze_source(self.source, self.output, entry="version-a.cbl")
        self.assertEqual(result["reason_code"], "ENTRY_AMBIGUOUS")

    def test_question_requires_explicit_network_flag(self) -> None:
        self.write_program()
        with mock.patch("analyze_source.run_investigation") as investigate:
            result = analyze_source(self.source, self.output, question="Explain count calculation")
        self.assertEqual(result["question_status"], "NETWORK_DISABLED")
        investigate.assert_not_called()

    def test_selected_actual_filename_and_refreshed_database_reach_runner(self) -> None:
        self.write_program()
        def investigate(question, database, config, **options):
            self.assertEqual(options["entry_program"], "BATCHENTRY")
            self.assertTrue(options["allow_network"])
            self.assertEqual(options["analysis_scope"]["kind"], "full_directory")
            self.assertEqual(InvestigationTools(database).inspect_symbol("BATCHENTRY")["status"], "OK")
            return {"runner_status": "COMPLETED", "agent_result": {
                "status": "CITATION_VERIFIED_ONLY", "answer": "Current source explanation."
            }}
        with mock.patch("analyze_source.run_investigation", side_effect=investigate):
            result = analyze_source(self.source, self.output, entry="downloaded-member", question="Explain count",
                                    allow_network=True, config=CompanyAPIConfig("https://gateway.example/v1", "approved-model", api_key="hidden-test-key"))
        self.assertEqual(result["question_status"], "CITATION_VERIFIED_ONLY")
        self.assertIn("Current source explanation.", (self.output / "agent-result.md").read_text())
        self.assertNotIn("hidden-test-key", json.dumps(result))

    def test_custom_extension_and_explicit_encoding(self) -> None:
        (self.source / "module.member").write_bytes((program("CUSTOMENTRY") + "*> 中文说明\n").encode("gb18030"))
        result = analyze_source(self.source, self.output, extensions=frozenset({".member"}), encoding="gb18030", source_format="free")
        self.assertEqual(result["program_count"], 1)
        self.assertEqual(result["source_options"]["encoding"], "gb18030")

    def test_change_during_indexing_blocks_use_of_snapshot(self) -> None:
        self.write_program()
        from structural_index import build_structural_index
        def changing_build(*args, **kwargs):
            result = build_structural_index(*args, **kwargs)
            self.write_program(factor=7)
            return result
        with mock.patch("analyze_source.build_structural_index", side_effect=changing_build):
            with mock.patch("analyze_source.run_investigation") as investigate:
                result = analyze_source(self.source, self.output, question="Explain", allow_network=True)
        self.assertEqual(result["runner_status"], "BLOCKED")
        investigate.assert_not_called()

    def test_output_cannot_contaminate_source(self) -> None:
        self.write_program()
        with self.assertRaises(ValueError):
            analyze_source(self.source, self.source / "output")

    def test_cli_reports_actual_programs_with_spaces_in_paths(self) -> None:
        self.write_program()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main(["--source", str(self.source), "--output", str(self.output)])
        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["program_count"], 1)

    def test_demo_cli_rejects_user_source_before_creating_database(self) -> None:
        self.write_program()
        database = self.root / "incorrect-demo.sqlite"
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            exit_code = demo_main(["--source", str(self.source), "--database", str(database)])
        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["reason_code"], "DEMO_SOURCE_ONLY")
        self.assertFalse(database.exists())


if __name__ == "__main__":
    unittest.main()
