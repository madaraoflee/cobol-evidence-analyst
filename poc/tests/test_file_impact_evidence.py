"""Offline file-impact leads from synthetic indexed source, with explicit gaps."""

from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from business_index import build_business_index
from file_impact_evidence import is_file_impact_question, nominate_file_impact_evidence
from question_investigation import _visible_ids
from repository_discovery import ensure_repository_search
from structural_index import build_structural_index


class FileImpactEvidenceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = self.root / "source"
        shutil.copytree(Path(__file__).resolve().parents[1] / "fixtures" / "file-impact-v1",
                        self.source, ignore=shutil.ignore_patterns("README.md"))
        self.database = self.root / "index.sqlite"

    def build(self, *, sparse=True, search=True):
        build = build_business_index if sparse else build_structural_index
        build(self.source, self.database, source_format="free")
        if search:
            ensure_repository_search(self.database, self.source)

    def nominate(self, paths=None, **kwargs):
        return nominate_file_impact_evidence(self.database,
            paths or ["programs/debit.cbl", "programs/audit.cbl", "copybooks/ACCOUNTREC.cpy"], **kwargs)

    def test_file_question_intent_does_not_change_program_impact(self):
        for question in ("扣减会影响哪些lf和哪些field？", "哪些 LF/PF 和字段被修改？",
                         "Which files does debit affect?", "List changed record fields"):
            self.assertTrue(is_file_impact_question(question), question)
        for question in ("扣减影响哪些程序？", "How does debit work?", "FIELDVALUE 计算规则"):
            self.assertFalse(is_file_impact_question(question), question)

    def test_located_io_record_copybook_and_dds_are_reading_leads(self):
        self.build()
        candidates, frontier, hashes = self.nominate()
        io = [row["statement"] for row in candidates if row["kind"] == "file_io"]
        self.assertIn("READ RATE-FILE.", io)
        self.assertIn("REWRITE ACCTREC.", io)
        self.assertTrue(any(text.startswith("WRITE AUDITREC") for text in io))
        definitions = [row for row in candidates if row["kind"] == "file_definitions"]
        self.assertTrue(any("SELECT ACCOUNT-FILE ASSIGN TO 'ACCOUNTLF'." in row["statement"] for row in definitions))
        self.assertTrue(any("FD ACCOUNT-FILE. COPY ACCOUNTREC." in row["statement"] for row in definitions))
        self.assertTrue(any("ACCTBAL" in row["statement"] and row["relative_path"].endswith(".cpy") for row in definitions))
        self.assertEqual({row["relative_path"] for row in candidates if row["kind"] == "dds_definitions"},
                         {"dds/ACCOUNTLF.dds", "dds/RATELF.dds"})
        self.assertIn("dds/ACCOUNTLF.dds", hashes)
        self.assertTrue(all(row["end_line"] - row["start_line"] < 96 for row in candidates))
        self.assertTrue(all(row["reads"] == [] and row["writes"] == [] for row in candidates))
        self.assertFalse(any("lf_pf" in row for row in candidates))
        self.assertFalse(any(gap.get("object") in {"ORGANIZATION", "INDEXED", "ACCOUNT-FILE"} for gap in frontier))

    def test_each_complete_candidate_is_verified_against_original_text(self):
        self.build()
        candidates, _, _ = self.nominate()
        for row in candidates:
            lines = (self.source / row["relative_path"]).read_text().splitlines()
            page = {"relative_path": row["relative_path"], "start_line": row["start_line"],
                    "end_line": row["end_line"], "source_sha256": row["source_sha256"],
                    "source_text": "\n".join(lines[row["start_line"] - 1:row["end_line"]]),
                    "evidence_id": "ev_original"}
            self.assertEqual(_visible_ids(row, [page]), ["ev_original"], row)

    def test_missing_dds_keeps_known_io_and_record(self):
        (self.source / "dds" / "ACCOUNTLF.dds").unlink()
        self.build()
        candidates, frontier, _ = self.nominate()
        self.assertTrue(any(row["kind"] == "file_io" and row["statement"] == "REWRITE ACCTREC." for row in candidates))
        self.assertTrue(any("ACCTBAL" in row["statement"] for row in candidates))
        self.assertIn({"kind": "dds_definitions", "reason": "dds_object_not_located", "object": "ACCOUNTLF"}, frontier)
        self.assertFalse(any("ACCOUNTPF" in row["statement"] for row in candidates))

    def test_structural_snapshot_and_no_search_pages_degrade_explicitly(self):
        self.build(sparse=False, search=False)
        candidates, frontier, hashes = self.nominate(["programs/debit.cbl"])
        self.assertTrue(any(row["kind"] == "file_io" and "REWRITE" in row["statement"] for row in candidates))
        self.assertFalse(any(row["kind"] == "file_definitions" for row in candidates))
        self.assertIn("indexed_source_pages_unavailable", {gap["reason"] for gap in frontier})
        self.assertIn("programs/debit.cbl", hashes)

    def test_structural_snapshot_with_search_pages_supplies_definitions(self):
        self.build(sparse=False)
        candidates, _, _ = self.nominate(["programs/debit.cbl"])
        self.assertTrue(any(row["kind"] == "file_io" for row in candidates))
        self.assertTrue(any(row["kind"] == "file_definitions" for row in candidates))

    def test_fts_dds_reference_is_only_a_candidate(self):
        (self.source / "dds" / "ACCOUNTLF.dds").rename(self.source / "dds" / "account-view.dds")
        with (self.source / "dds" / "account-view.dds").open("a") as handle:
            handle.write("     A                                      TEXT('ACCOUNTLF view')\n")
        self.build()
        candidates, _, _ = self.nominate(["programs/debit.cbl"])
        self.assertTrue(any(row["relative_path"] == "dds/account-view.dds" for row in candidates))
        self.assertFalse(any(row.get("semantic_execution_verified") or row.get("lf_pf_confirmed") for row in candidates))

    def test_repeated_io_envelope_uses_first_actual_statement(self):
        source = self.source / "programs" / "repeat.cbl"
        source.write_text("IDENTIFICATION DIVISION.\nPROGRAM-ID. REPEATFLOW.\nPROCEDURE DIVISION.\nMAIN.\n"
                          "READ INPUT-FILE.\n" + "CONTINUE.\n" * 140 + "READ INPUT-FILE.\nGOBACK.\n")
        self.build()
        candidates, _, _ = self.nominate(["programs/repeat.cbl"])
        row = next(row for row in candidates if row["kind"] == "file_io")
        self.assertEqual((row["start_line"], row["end_line"], row["occurrence_count"]), (5, 5, 1))

    def test_candidate_budget_and_scoped_copybooks(self):
        self.build()
        candidates, frontier, _ = self.nominate(["programs/debit.cbl"], max_candidates=2)
        self.assertEqual(len(candidates), 2)
        self.assertTrue(all(row["kind"] == "file_io" for row in candidates))
        self.assertFalse(any(row["relative_path"].endswith(".cpy") for row in candidates))
        self.assertIn("file_impact_candidate_budget", {gap["reason"] for gap in frontier})

    def test_stale_indexed_source_cannot_supply_declarations(self):
        self.build()
        with sqlite3.connect(self.database) as db:
            db.execute("UPDATE repo_pages SET source_sha256='stale' WHERE relative_path='programs/debit.cbl'")
        candidates, frontier, _ = self.nominate(["programs/debit.cbl"])
        self.assertFalse(any(row["kind"] == "file_definitions" for row in candidates))
        self.assertIn("indexed_source_unavailable", {gap["reason"] for gap in frontier})

    def test_bounded_dds_lookup_reports_omitted_same_named_sources(self):
        for number in range(10):
            folder = self.source / "views" / str(number)
            folder.mkdir(parents=True)
            (folder / "ACCOUNTLF.dds").write_text("     A          R ACCTREC PFILE(ACCOUNTPF)\n")
        self.build()
        _, frontier, _ = self.nominate(["programs/debit.cbl"])
        self.assertIn({"kind": "dds_definitions", "reason": "dds_lookup_budget", "object": "ACCOUNTLF"}, frontier)

    def test_assignment_clauses_are_not_additional_file_objects(self):
        path = self.source / "programs" / "debit.cbl"
        path.write_text(path.read_text().replace("SELECT ACCOUNT-FILE ASSIGN TO 'ACCOUNTLF'.",
            "SELECT ACCOUNT-FILE\n ASSIGN TO 'ACCOUNTLF'\n ORGANIZATION IS INDEXED\n"
            " ACCESS MODE IS DYNAMIC\n RECORD KEY IS ACCTKEY."))
        self.build()
        candidates, frontier, _ = self.nominate(["programs/debit.cbl"])
        select = next(row for row in candidates if row["statement"].startswith("SELECT ACCOUNT-FILE"))
        self.assertEqual(select["end_line"] - select["start_line"], 4)
        self.assertFalse(any(gap.get("object") in {"ORGANIZATION", "IS", "INDEXED", "ACCTKEY"} for gap in frontier))

    def test_invalid_candidate_budget_cannot_disable_bounds(self):
        self.build()
        for budget in (0, -1, 129, True):
            with self.assertRaisesRegex(ValueError, "FILE_IMPACT_CANDIDATE_BUDGET_INVALID"):
                self.nominate(max_candidates=budget)


if __name__ == "__main__":
    unittest.main()
