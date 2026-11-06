"""Candidate compression must preserve provenance, history and unfinished work."""

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from prior_analysis_ledger import KINDS, RESULT_SCHEMA, digest, wire_bytes
import test_prior_source_memory as memory_fixtures


class PriorAnalysisLedgerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = memory_fixtures.PriorSourceMemoryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory
        self.pages = [self.fixture.page]
        self.records = [self.fixture.round_record(self.pages)]
        self.memory.remember_supplied_sources(payload={"source_context": [{"pages": self.pages}]},
                                             round_record=self.records[0])

    def add_source(self, number):
        page = self.fixture.make_page(relative_path=f"rule-{number}.cbl")
        self.pages.append(page)
        record = self.fixture.round_record([page], len(self.records) + 1)
        self.records.append(record)
        self.memory.remember_supplied_sources(payload={"source_context": [{"pages": [page]}]}, round_record=record)
        return page

    def prepare(self, **options):
        return self.memory.prepare_analysis_compaction(**options)

    def response(self, task, *, entries=None, deferred=()):
        if entries is None:
            entries = [{"kind": kind, "text": f"Candidate {kind}: positive input follows the stated branch.",
                        "evidence_ids": list(task["source_ids"]), "covers_entry_ids": []} for kind in KINDS]
        return {"schema": RESULT_SCHEMA, "task_id": task["task_id"],
                "entries": entries, "deferred_source_ids": list(deferred)}

    def accept(self, task, value=None, *, payload=None, record=None):
        pages = self.memory.archived_pages(task["source_ids"])
        if record is None:
            record = self.fixture.round_record(pages, len(self.records) + 1)
            self.records.append(record)
        return self.memory.accept_analysis_compaction(value or self.response(task), task_id=task["task_id"],
            payload=payload or {"source_context": [{"pages": pages}], "analysis_compaction": task},
            round_record=record)

    @staticmethod
    def retained(task):
        return [{"kind": row["kind"], "text": row["text"], "evidence_ids": list(row["evidence_ids"]),
                 "covers_entry_ids": [row["entry_id"]]} for row in task["previous_entries"]]

    def test_candidate_ledger_is_distinct_from_original_source_and_verified_facts(self):
        task = self.prepare(frontier=[{"reason": "runtime_target_unresolved", "target": "NEXT-STEP"}],
            coverage={"closure_complete": False})
        self.assertTrue(self.accept(task))
        prompt = self.memory.analysis_ledger_prompt()
        self.assertEqual({row["kind"] for row in prompt["entries"]}, set(KINDS))
        self.assertFalse(prompt["semantic_verification"])
        self.assertFalse(prompt["summary_is_original_source"])
        self.assertFalse(prompt["citation_permission"])
        self.assertTrue(prompt["historical_sources_citation_permission"])
        self.assertTrue(prompt["structural_preservation_verified"])
        self.assertTrue(all(row["content_kind"] == "model_candidate" and row["semantic_verification"] is False
                            for row in prompt["entries"]))
        self.assertEqual(self.memory.prompt()["confirmed_facts"], [])
        self.assertEqual(self.memory.prompt()["blocks"], [])
        payload = {"source_context": [{"pages": []}], "analysis_ledger": prompt}
        self.assertEqual(self.memory.visible_ids(payload), {self.pages[0]["evidence_id"]})
        self.assertEqual(self.memory.effective_pages(payload, verify_archive=True), self.pages)
        self.assertNotIn("Candidate", self.memory.effective_pages(payload)[0]["source_text"])

    def test_each_generation_preserves_all_prior_kinds_and_full_local_investigation(self):
        frontier = [{"reason": "unread_call", "relative_path": f"pending-{number}.cbl"} for number in range(180)]
        first = self.prepare(frontier=frontier, coverage={"read": 1, "complete": False})
        self.assertTrue(self.accept(first))
        prior_checkpoint = self.memory.checkpoint()
        next_page = self.add_source(2)
        second = self.prepare(evidence_ids=[next_page["evidence_id"]],
            frontier=frontier, coverage={"read": 2, "complete": False})
        entries = self.retained(second)
        entries[0]["evidence_ids"].append(next_page["evidence_id"])
        entries[0]["text"] += " The next source adds a separate rule."
        self.assertTrue(self.accept(second, self.response(second, entries=entries)))
        checkpoint = self.memory.checkpoint()["analysis_ledger"]
        self.assertEqual(checkpoint["generations"][0], prior_checkpoint["analysis_ledger"]["generations"][0])
        self.assertEqual(len(checkpoint["generations"]), 2)
        self.assertEqual(checkpoint["investigations"][first["investigation_id"]]["frontier"], frontier)
        self.assertEqual(checkpoint["investigations"][second["investigation_id"]]["frontier"], frontier)
        self.assertEqual(len(self.memory.pages), 2)
        self.assertEqual(self.memory.analysis_state()["entries_by_kind"], {kind: 1 for kind in KINDS})

    def test_missing_prior_condition_or_resolving_conflict_by_reclassification_rejects_atomically(self):
        first = self.prepare()
        self.assertTrue(self.accept(first))
        before = self.memory.analysis_ledger_prompt()
        for number, mutation in enumerate(("drop", "reclassify", "drop_source"), 2):
            page = self.add_source(number)
            task = self.prepare(evidence_ids=[page["evidence_id"]])
            entries = self.retained(task)
            entries[0]["evidence_ids"].append(page["evidence_id"])
            if mutation == "drop":
                entries = [row for row in entries if row["kind"] != "conditions"]
            elif mutation == "reclassify":
                next(row for row in entries if row["kind"] == "conflicts")["kind"] = "rules"
            else:
                entries[0]["evidence_ids"] = [page["evidence_id"]]
            with self.subTest(mutation=mutation):
                self.assertFalse(self.accept(task, self.response(task, entries=entries)))
                self.assertEqual(self.memory.analysis_ledger_prompt(), before)
                self.assertEqual(self.memory.analysis_state()["attempt_status"], "failed")

    def test_unrepresented_new_source_must_be_explicitly_deferred_and_stays_pending(self):
        second = self.add_source(2)
        task = self.prepare()
        entry = {"kind": "rules", "text": "Candidate rule from the first source.",
                 "evidence_ids": [self.pages[0]["evidence_id"]], "covers_entry_ids": []}
        self.assertTrue(self.accept(task, self.response(task, entries=[entry], deferred=[second["evidence_id"]])))
        state = self.memory.analysis_state()
        self.assertEqual(state["source_count"], 2)
        self.assertEqual(state["deferred_source_count"], 1)
        self.assertEqual(self.memory.analysis_ledger.source_coverage[second["evidence_id"]]["status"], "deferred")
        self.assertIn(second["evidence_id"], self.memory.analysis_ledger_prompt()["deferred_source_ids"])
        third = self.add_source(3)
        task = self.prepare(evidence_ids=[third["evidence_id"]])
        self.assertFalse(self.accept(task, self.response(task, entries=self.retained(task))))
        self.assertEqual(self.memory.analysis_state()["deferred_source_count"], 1)

    def test_exact_task_and_actually_supplied_original_request_are_required(self):
        for number, mutation in enumerate(("missing_task", "missing_page", "duplicate_page", "extra_page",
                                           "wrong_hash", "unknown_source"), 2):
            page = self.add_source(number)
            task = self.prepare(evidence_ids=[page["evidence_id"]])
            payload = {"source_context": [{"pages": [page]}], "analysis_compaction": deepcopy(task)}
            record = self.fixture.round_record([page], 100 + number)
            value = self.response(task)
            if mutation == "missing_task":
                payload["analysis_compaction"].pop("instruction")
            elif mutation == "missing_page":
                payload["source_context"][0]["pages"] = []
            elif mutation == "duplicate_page":
                payload["source_context"][0]["pages"].append(deepcopy(page))
            elif mutation == "extra_page":
                payload["source_context"][0]["pages"].append(self.pages[0])
            elif mutation == "wrong_hash":
                record["sources"][0]["source_sha256"] = "f" * 64
            else:
                value["entries"][0]["evidence_ids"] = ["ev_page_unknown"]
            with self.subTest(mutation=mutation):
                self.assertFalse(self.accept(task, value, payload=payload, record=record))
                self.assertEqual(self.memory.analysis_state()["generation"], 0)

    def test_summary_or_binding_tampering_and_omitted_declaration_revoke_historical_citations(self):
        task = self.prepare()
        self.assertTrue(self.accept(task))
        original = self.memory.analysis_ledger_prompt()
        self.assertEqual(self.memory.visible_ids({"source_context": [{"pages": []}]}), set())
        for mutation in ("text", "binding", "missing_reference", "missing_entry", "semantic_flag", "hash"):
            prompt = deepcopy(original)
            if mutation == "text":
                prompt["entries"][0]["text"] = "A forged unconditional conclusion."
            elif mutation == "binding":
                prompt["source_manifest_sha256"] = "f" * 64
            elif mutation == "missing_reference":
                prompt["historical_source_references"] = []
            elif mutation == "missing_entry":
                prompt["entries"].pop()
            elif mutation == "semantic_flag":
                prompt["semantic_verification"] = True
            else:
                prompt["summary_sha256"] = "f" * 64
            with self.subTest(mutation=mutation):
                payload = {"source_context": [{"pages": []}], "analysis_ledger": prompt}
                self.assertEqual(self.memory.visible_ids(payload), set())
                self.assertEqual(self.memory.effective_pages(payload, verify_archive=True), [])

    def test_failed_or_accepted_same_input_never_recursively_compacts_again_after_restart(self):
        task = self.prepare()
        self.assertIsNone(self.prepare())
        self.assertTrue(self.memory.mark_analysis_compaction_failed(task["task_id"], "provider_failure"))
        self.assertIsNone(self.prepare())
        restored = self.fixture.new_memory()
        restored.restore(self.memory.checkpoint(), self.records)
        self.assertIsNone(restored.prepare_analysis_compaction())
        page = self.add_source(2)
        second = self.prepare(evidence_ids=[page["evidence_id"]])
        self.assertTrue(self.accept(second))
        self.assertIsNone(self.prepare(evidence_ids=[page["evidence_id"]]))
        restored = self.fixture.new_memory()
        restored.restore(self.memory.checkpoint(), self.records)
        self.assertIsNone(restored.prepare_analysis_compaction(evidence_ids=[page["evidence_id"]]))

    def test_pending_task_and_accepted_generations_survive_restart_and_stale_results_do_not_replace_them(self):
        task = self.prepare()
        restored = self.fixture.new_memory()
        restored.restore(self.memory.checkpoint(), self.records)
        self.assertEqual(restored.analysis_ledger.pending_task, task)
        self.memory = restored
        self.assertTrue(self.accept(task))
        first_prompt = self.memory.analysis_ledger_prompt()
        page = self.add_source(2)
        second = self.prepare(evidence_ids=[page["evidence_id"]])
        self.assertFalse(self.memory.accept_analysis_compaction(self.response(task), task_id=task["task_id"],
            payload={}, round_record=None))
        self.assertEqual(self.memory.analysis_ledger.pending_task, second)
        entries = self.retained(second)
        entries[0]["evidence_ids"].append(page["evidence_id"])
        self.assertTrue(self.accept(second, self.response(second, entries=entries)))
        restored = self.fixture.new_memory()
        restored.restore(self.memory.checkpoint(), self.records)
        self.assertEqual(restored.analysis_ledger_prompt(), self.memory.analysis_ledger_prompt())
        # An earlier request keeps its exact declared proof, never a rewritten
        # candidate presented as that earlier generation.
        self.assertEqual(restored.visible_ids({"analysis_ledger": first_prompt}), {self.pages[0]["evidence_id"]})

    def test_explicit_continuation_retries_failed_material_once_per_persisted_token(self):
        task = self.prepare()
        self.memory.mark_analysis_compaction_failed(task["task_id"], "provider_failure")
        self.assertIsNone(self.prepare())
        retry = self.prepare(retry_token="continuation-one")
        self.assertIsNotNone(retry)
        self.assertNotEqual(retry["task_id"], task["task_id"])
        self.assertEqual(retry["material_fingerprint"], task["material_fingerprint"])
        self.assertEqual(retry["retry_token"], "continuation-one")
        restored = self.fixture.new_memory()
        restored.restore(self.memory.checkpoint(), self.records)
        self.memory = restored
        self.assertEqual(restored.analysis_ledger.pending_task, retry)
        self.assertIsNone(self.prepare(retry_token="continuation-one"))
        self.memory.mark_analysis_compaction_failed(retry["task_id"], "provider_failure")
        restored = self.fixture.new_memory()
        restored.restore(self.memory.checkpoint(), self.records)
        self.memory = restored
        self.assertIsNone(self.prepare())
        self.assertIsNone(self.prepare(retry_token="continuation-one"))
        second = self.prepare(retry_token="continuation-two")
        self.assertIsNotNone(second)
        self.assertTrue(self.accept(second))
        self.assertIsNone(self.prepare(retry_token="continuation-three"))
        self.assertEqual(self.memory.analysis_state()["attempt_count"], 3)

    def test_retry_token_must_be_a_bounded_explicit_request_identity(self):
        for token in (True, 1, [], "", "x" * 181):
            with self.subTest(token=token), self.assertRaisesRegex(ValueError, "INPUT_INVALID"):
                self.prepare(retry_token=token)
        self.assertEqual(self.memory.analysis_state()["attempt_count"], 0)

    def test_explicit_continuation_can_revisit_deferred_but_not_fully_represented_material(self):
        first = self.prepare()
        self.assertTrue(self.accept(first, self.response(first, entries=[], deferred=first["source_ids"])))
        self.assertIsNone(self.prepare())
        retry = self.prepare(retry_token="deferred-continuation-one")
        self.assertIsNotNone(retry)
        self.assertTrue(self.accept(retry, self.response(retry, entries=[], deferred=retry["source_ids"])))
        self.assertIsNone(self.prepare(retry_token="deferred-continuation-one"))
        restored = self.fixture.new_memory()
        restored.restore(self.memory.checkpoint(), self.records)
        self.memory = restored
        self.assertIsNone(self.prepare())
        self.assertIsNone(self.prepare(retry_token="deferred-continuation-one"))
        next_retry = self.prepare(retry_token="deferred-continuation-two")
        self.assertTrue(self.accept(next_retry))
        self.assertIsNone(self.prepare(retry_token="deferred-continuation-three"))
        self.assertEqual(self.memory.analysis_state()["deferred_source_count"], 0)

    def test_oversized_model_output_retains_prior_generation_and_reports_failure(self):
        first = self.prepare()
        self.assertTrue(self.accept(first))
        before = self.memory.analysis_ledger_prompt()
        page = self.add_source(2)
        task = self.prepare(evidence_ids=[page["evidence_id"]], max_output_bytes=2048)
        entries = self.retained(task)
        entries[0]["evidence_ids"].append(page["evidence_id"])
        self.assertFalse(self.accept(task, self.response(task, entries=entries)))
        self.assertEqual(self.memory.analysis_ledger_prompt(), before)
        self.assertEqual(self.memory.analysis_state()["last_failure_reason"], "compressed_analysis_exceeds_budget")
        smaller = self.memory.analysis_ledger_prompt(max_bytes=1024)
        self.assertFalse(smaller["global_analysis_supplied"])
        self.assertTrue(smaller["requires_batched_compaction"])
        self.assertEqual(smaller["omitted_entry_count"], len(before["entries"]))
        self.assertNotIn("historical_source_references", smaller)
        self.assertEqual(self.memory.visible_ids({"analysis_ledger": smaller}), set())

    def test_corrupted_original_archive_revokes_historical_authority_and_compression(self):
        first = self.prepare()
        self.assertTrue(self.accept(first))
        before = self.memory.analysis_ledger_prompt()
        page = self.add_source(2)
        second = self.prepare(evidence_ids=[page["evidence_id"]])
        archive = self.fixture.database.parent / "versioned-evidence.sqlite"
        with closing(sqlite3.connect(archive)) as connection, connection:
            connection.execute("UPDATE excerpts SET text=? WHERE evidence_id=?",
                               ("DISPLAY 'corrupt'.", self.pages[0]["evidence_id"]))
        self.assertEqual(self.memory.visible_ids({"analysis_ledger": before}), set())
        entries = self.retained(second)
        entries[0]["evidence_ids"].append(page["evidence_id"])
        self.assertFalse(self.accept(second, self.response(second, entries=entries)))
        self.assertEqual(self.memory.analysis_state()["generation"], 1)

    def test_150_source_references_fit_a_reasonable_budget_and_survive_persistence_without_truncation(self):
        for number in range(2, 151):
            self.add_source(number)
        task = self.prepare(max_output_bytes=65536,
            frontier=[{"reason": "unresolved_business_condition", "relative_path": "next-step.cbl"}],
            coverage={"source_count": 150, "closure_complete": False})
        entries = [{"kind": "rules", "text": "Candidate rules across the supplied neutral source set.",
                    "evidence_ids": list(task["source_ids"]), "covers_entry_ids": []}]
        self.assertTrue(self.accept(task, self.response(task, entries=entries)))
        prompt = self.memory.analysis_ledger_prompt(max_bytes=65536)
        identifiers = {page["evidence_id"] for page in self.pages}
        self.assertLessEqual(wire_bytes(prompt), 65536)
        self.assertEqual(len(prompt["historical_source_references"]), 150)
        self.assertEqual(set(prompt["historical_source_references"]), identifiers)
        self.assertEqual(self.memory.visible_ids({"analysis_ledger": prompt}), identifiers)
        self.assertEqual(prompt["omitted_entry_count"], 0)
        restored = self.fixture.new_memory()
        restored.restore(self.memory.checkpoint(), self.records)
        self.assertEqual(restored.analysis_ledger_prompt(max_bytes=65536), prompt)
        self.assertEqual(restored.analysis_state()["source_count"], 150)
        self.assertLess(restored.analysis_state()["after_bytes"], restored.analysis_state()["before_bytes"])
        self.assertEqual(len(restored.archived_pages(sorted(identifiers))), 150)

    def test_changed_checkpoint_coverage_or_generation_proof_is_rejected(self):
        task = self.prepare()
        self.assertTrue(self.accept(task))
        saved = self.memory.checkpoint()
        for mutation in ("coverage", "entry", "origin", "binding"):
            changed = deepcopy(saved)
            ledger = changed["analysis_ledger"]
            if mutation == "coverage":
                next(iter(ledger["source_coverage"].values()))["status"] = "verified"
            elif mutation == "entry":
                ledger["generations"][0]["entries"][0]["text"] = "Changed candidate."
            elif mutation == "origin":
                ledger["generations"][0]["response_origin"]["request_body_sha256"] = "f" * 64
            else:
                ledger["generations"][0]["source_manifest_sha256"] = "f" * 64
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, "PRIOR_SOURCE_MEMORY_INVALID"):
                self.fixture.new_memory().restore(changed, self.records)


if __name__ == "__main__":
    unittest.main()
