"""History lookup stays bounded without relaxing source or archive validation."""

from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

import prior_source_memory
import test_prior_source_memory as memory_fixtures
import test_prior_analysis_ledger as ledger_fixtures


class PriorSourceMemoryRestoreEfficiencyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = memory_fixtures.PriorSourceMemoryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_restore_looks_up_each_history_record_once_instead_of_per_page(self):
        records = []
        count = 120
        for number in range(1, count + 1):
            page = self.fixture.make_page(relative_path=f"step-{number}.cbl")
            record = self.fixture.round_record([page], number)
            records.append(record)
            self.fixture.memory.remember_supplied_sources(
                payload={"source_context": [{"pages": [page]}]}, round_record=record)
        saved = self.fixture.memory.checkpoint()
        probes = []

        class CountedRecord(dict):
            def get(self, key, default=None):
                if key == "round_id":
                    probes.append(key)
                return super().get(key, default)

        restored = self.fixture.new_memory()
        restored.restore(saved, [CountedRecord(row) for row in records])
        self.assertEqual(restored.checkpoint(), saved)
        self.assertLessEqual(len(probes), 2 * count,
                             "Restoring more history must not rescan its prefix for every source page")

    def test_reset_round_numbers_still_select_the_exact_request_hash(self):
        first = self.fixture.record
        self.fixture.memory.remember_supplied_sources(payload=self.fixture.payload, round_record=first)
        page = self.fixture.make_page(relative_path="later.cbl")
        later = self.fixture.round_record([page], 2)
        later["round_id"] = first["round_id"]
        self.fixture.memory.remember_supplied_sources(
            payload={"source_context": [{"pages": [page]}]}, round_record=later)
        saved = self.fixture.memory.checkpoint()
        for records in ([first, later], [later, first]):
            with self.subTest(order=[row["request_body_sha256"] for row in records]):
                restored = self.fixture.new_memory()
                restored.restore(saved, records)
                self.assertEqual(restored.checkpoint(), saved)

    def test_duplicate_request_keys_keep_the_first_record_proof(self):
        self.assertEqual(self.fixture.accept(), 1)
        saved = self.fixture.memory.checkpoint()
        invalid = {**deepcopy(self.fixture.record), "sources": []}
        with self.assertRaisesRegex(ValueError, "PRIOR_SOURCE_MEMORY_INVALID"):
            self.fixture.new_memory().restore(saved, [invalid, self.fixture.record])
        restored = self.fixture.new_memory()
        restored.restore(saved, [self.fixture.record, invalid])
        self.assertEqual(restored.checkpoint(), saved)
        restored.restore(saved, [self.fixture.record, deepcopy(self.fixture.record)])
        self.assertEqual(restored.checkpoint(), saved)

    def test_effective_pages_validate_each_archive_once_and_revalidate_next_call(self):
        ledger = ledger_fixtures.PriorAnalysisLedgerTests()
        ledger.setUp()
        self.addCleanup(ledger.doCleanups)
        ledger.add_source(2)
        self.assertTrue(ledger.accept(ledger.prepare()))
        payload = {"source_context": [{"pages": []}], "analysis_ledger": ledger.memory.analysis_ledger_prompt()}
        original_reader = prior_source_memory.read_archived_evidence
        with patch.object(prior_source_memory, "read_archived_evidence", wraps=original_reader) as reader:
            for verify in (False, True):
                with self.subTest(verify_archive=verify):
                    reader.reset_mock()
                    pages = ledger.memory.effective_pages(payload, verify_archive=verify)
                    self.assertEqual({page["evidence_id"] for page in pages},
                                     {page["evidence_id"] for page in ledger.pages})
                    self.assertEqual(reader.call_count, len(ledger.pages))
        archive = ledger.fixture.database.parent / "versioned-evidence.sqlite"
        with closing(sqlite3.connect(archive)) as connection, connection:
            connection.execute("UPDATE excerpts SET text=? WHERE evidence_id=?",
                               ("MOVE ZERO TO RESULT-AMOUNT.", ledger.pages[0]["evidence_id"]))
        for verify in (False, True):
            with self.subTest(tampered=True, verify_archive=verify):
                self.assertEqual(ledger.memory.effective_pages(payload, verify_archive=verify), [],
                                 "A previous validation cannot authorize a changed archive")


if __name__ == "__main__":
    unittest.main()
