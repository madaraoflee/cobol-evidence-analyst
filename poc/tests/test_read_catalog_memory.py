"""Actual request coverage for source rereads after bounded context eviction."""

from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[1])]

from agent_policy import AgentPolicy
from business_chat import _fit_request, _request_manifest
from prior_source_memory import READ_CATALOG_MAX_BYTES, READ_CATALOG_MAX_ENTRIES
from run_store import RunStore
import test_completion_driven_chat as chat_fixtures
import test_prior_source_memory as memory_fixtures


class ReadCatalogTests(unittest.TestCase):
    def setUp(self):
        self.fixture = memory_fixtures.PriorSourceMemoryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.memory = self.fixture.memory

    def remember(self, page=None, record=None):
        page = page or self.fixture.page
        return self.memory.remember_supplied_sources(
            payload={"source_context": [{"pages": [page]}]},
            round_record=record or self.fixture.round_record([page]))

    def test_valid_read_without_findings_survives_as_navigation_only(self):
        self.assertEqual(self.remember(), 1)
        prompt = self.memory.prompt()
        catalog = prompt["read_catalog"]
        self.assertEqual(catalog["entries"][0]["evidence_id"], self.fixture.page["evidence_id"])
        self.assertFalse(catalog["citation_permission"])
        self.assertFalse(catalog["source_text_supplied"])
        self.assertEqual(prompt["blocks"], [])
        payload = {"source_context": [{"pages": []}], "prior_source_memory": prompt}
        self.assertEqual(self.memory.visible_ids(payload), set())
        self.assertEqual(self.memory.effective_pages(payload, verify_archive=True), [])
        self.assertEqual(_request_manifest(payload, self.memory)["memory_source_ids"], [])
        self.assertEqual(self.memory.archived_pages([self.fixture.page["evidence_id"]]), [self.fixture.page])
        self.assertEqual(self.memory.accept_findings([self.fixture.note()], payload=payload,
            round_record=self.fixture.round_record([], 2)), 0)

    def test_missing_or_changed_actual_request_proof_cannot_register_source(self):
        self.assertEqual(self.memory.remember_supplied_sources(
            payload=self.fixture.payload, round_record=None), 0)
        for field, value in (("source_sha256", "f" * 64), ("excerpt_sha256", "f" * 64),
                             ("binding_sha256", "f" * 64), ("end_line", 100)):
            record = deepcopy(self.fixture.record)
            record["sources"][0][field] = value
            with self.subTest(field=field):
                self.assertEqual(self.remember(record=record), 0)
        self.assertEqual(self.memory.prompt()["read_catalog"]["total_count"], 0)

    def test_checkpoint_restores_catalog_without_requiring_candidate_findings(self):
        self.remember()
        saved = self.memory.checkpoint()
        restored = self.fixture.new_memory()
        restored.restore(saved, [self.fixture.record])
        self.assertEqual(restored.prompt(), self.memory.prompt())
        self.assertEqual(restored.archived_pages([self.fixture.page["evidence_id"]]), [self.fixture.page])
        self.assertEqual(self.remember(record=self.fixture.round_record([self.fixture.page], 2)), 0)
        self.assertEqual(self.memory.checkpoint()["origins"], saved["origins"])

    def test_changed_source_version_or_corrupted_archive_cannot_be_replayed(self):
        self.remember()
        saved = self.memory.checkpoint()
        changed = deepcopy(saved)
        changed["pages"][0]["source_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValueError, "PRIOR_SOURCE_MEMORY_INVALID"):
            self.fixture.new_memory().restore(changed, [self.fixture.record])
        archive = self.fixture.database.parent / "versioned-evidence.sqlite"
        with closing(sqlite3.connect(archive)) as connection, connection:
            connection.execute("UPDATE excerpts SET text=? WHERE evidence_id=?",
                ("DISPLAY 'changed source'.", self.fixture.page["evidence_id"]))
        with self.assertRaisesRegex(ValueError, "PRIOR_SOURCE_MEMORY_INVALID|EVIDENCE_ID_CONFLICT"):
            self.memory.archived_pages([self.fixture.page["evidence_id"]])
        self.assertEqual(self.remember(), 0, "Observation must not overwrite corrupt archived proof")
        with self.assertRaisesRegex(ValueError, "PRIOR_SOURCE_MEMORY_INVALID"):
            self.fixture.new_memory().restore(saved, [self.fixture.record])

    def test_catalog_is_bounded_and_omission_does_not_delete_local_provenance(self):
        records = []
        for number in range(90):
            page = self.fixture.make_page(relative_path=f"rules/rule-{number:03d}.cbl")
            record = self.fixture.round_record([page], number + 1)
            records.append(record)
            self.assertEqual(self.remember(page, record), 1)
        catalog = self.memory.prompt()["read_catalog"]
        self.assertLessEqual(len(catalog["entries"]), READ_CATALOG_MAX_ENTRIES)
        self.assertLessEqual(sum(len(json.dumps(row, ensure_ascii=False).encode())
                                 for row in catalog["entries"]), READ_CATALOG_MAX_BYTES)
        self.assertEqual(catalog["total_count"], 90)
        self.assertEqual(catalog["omitted_count"], 90 - len(catalog["entries"]))
        self.assertEqual(len(self.memory.checkpoint()["pages"]), 90)
        restored = self.fixture.new_memory()
        restored.restore(self.memory.checkpoint(), records)
        self.assertEqual(restored.prompt(), self.memory.prompt())

    def test_request_bytes_trim_catalog_before_dropping_original_source(self):
        for number in range(40):
            self.remember(self.fixture.make_page(relative_path=f"rule-{number:03d}.cbl"))
        payload = {"question": self.fixture.question, "repository": {}, "business_map": {},
            "framework_references": [], "prior_source_memory": self.memory.prompt(),
            "retrieval_status": {"state": "source_candidates"},
            "investigation_budget": {"searches_per_turn": 3},
            "source_context": [{"pages": [self.fixture.page],
                "call_chain": {"links": [], "omitted_links": 0}, "outline": [], "notices": []}]}
        trims = []
        config = chat_fixtures.CompanyAPIConfig("https://neutral.example.invalid/v1", "neutral-model", api_key="offline-only")
        messages, size = _fit_request(config, payload, [], AgentPolicy(max_request_bytes=32768),
            source_memory=self.memory, trim_events=trims)
        wire = json.loads(messages[-1]["content"])
        self.assertLessEqual(size, 32768)
        self.assertEqual(wire["source_context"][0]["pages"][0]["source_text"], self.fixture.page["source_text"])
        catalog = wire["prior_source_memory"]["read_catalog"]
        self.assertEqual(catalog["omitted_count"], 40 - len(catalog["entries"]))
        self.assertTrue(any(row["reason"] == "prior_read_catalog_request_bytes" for row in trims))
        self.assertEqual(len(self.memory.checkpoint()["pages"]), 40)


class ReadCatalogRequestTests(unittest.TestCase):
    # These exact-round scenarios verify source restoration independently of
    # cumulative model compaction, whose default-on path has separate coverage.
    def setUp(self):
        self.fixture = chat_fixtures.CompletionDrivenChatTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.order_source = "PERFORM APPLY-ADVANCE.\nPERFORM APPLY-RECEIPTS."
        for number in range(1, 8):
            body = "\n".join([f'DISPLAY "HELPER-{number}-MARKER".',
                self.order_source if number == 1 else "*> configuration source context",
                *["*> neutral descriptive context " + str(line) + " " + "detail " * 8 for line in range(110)],
                f"MOVE {number} TO HELPER-RESULT.",
                "APPLY-ADVANCE.\nSUBTRACT ADVANCE-AMOUNT FROM DUE-AMOUNT.\nEXIT.",
                "APPLY-RECEIPTS.\nSUBTRACT RECEIPT-AMOUNT FROM DUE-AMOUNT.\nEXIT."])
            self.fixture.write(f"helper-{number}.cbl", f"HELPER-{number}", body,
                "01 HELPER-RESULT PIC 9.\n01 DUE-AMOUNT PIC 9(6).\n"
                "01 ADVANCE-AMOUNT PIC 9(6).\n01 RECEIPT-AMOUNT PIC 9(6).")

    def raw(self, payload):
        return "\n".join(page["source_text"] for page in self.fixture.pages(payload))

    def test_ninth_actual_request_restores_evicted_order_without_optional_findings(self):
        fixture = self.fixture
        def respond(payload, number):
            if number <= 7:
                return fixture.read_helper(number)
            if number == 8:
                self.assertNotIn(self.order_source, self.raw(payload))
                catalog = payload["prior_source_memory"]["read_catalog"]
                self.assertTrue(any(row["relative_path"] == "helper-1.cbl" for row in catalog["entries"]))
                self.assertEqual(payload["prior_source_memory"]["blocks"], [])
                return fixture.read_helper(1)
            self.assertIn(self.order_source, self.raw(payload))
            return fixture.cited_answer(payload)
        result = fixture.ask(respond, request_guard=9,
            policy=AgentPolicy(context_compaction_enabled=False, max_model_requests=9, max_answer_revisions=0))
        self.assertEqual(len(fixture.requests), 9)
        self.assertTrue(result["answer_available"])
        for payload in fixture.requests:
            self.assertLessEqual(sum(len(page["source_text"]) for page in fixture.pages(payload)), 36000)
        saved = RunStore(fixture.root / "investigation-runs.sqlite").load(result["run_id"])
        actions = saved["checkpoint"]["completed_actions"]
        restored = [row for row in actions if row.get("outcome") == "prior_source_restored"]
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["added_pages"], 0)
        progress = saved["checkpoint"]["analysis_continuation"]
        self.assertGreaterEqual(progress["idle_tool_rounds"], 1)

    def test_currently_visible_repeat_does_not_restore_or_extend_tool_loop(self):
        fixture = self.fixture
        def respond(payload, number):
            return fixture.read_helper(1) if number <= 2 else fixture.cited_answer(payload)
        result = fixture.ask(respond, request_guard=3)
        self.assertEqual(len(fixture.requests), 3)
        self.assertEqual(result["metrics"]["convergence_stop_reason"], "no_evidence_progress")
        saved = RunStore(fixture.root / "investigation-runs.sqlite").load(result["run_id"])
        self.assertFalse(any(row.get("outcome") == "prior_source_restored"
            for row in saved["checkpoint"]["completed_actions"]))

    def test_restored_source_repeat_stops_without_claiming_new_progress(self):
        fixture = self.fixture
        def respond(payload, number):
            if number <= 7:
                return fixture.read_helper(number)
            if number <= 9:
                return fixture.read_helper(1)
            self.assertIn(self.order_source, self.raw(payload))
            return fixture.cited_answer(payload)
        result = fixture.ask(respond, request_guard=10,
            policy=AgentPolicy(context_compaction_enabled=False, max_answer_revisions=0))
        self.assertEqual(len(fixture.requests), 10)
        self.assertEqual(result["metrics"]["convergence_stop_reason"], "no_evidence_progress")
        saved = RunStore(fixture.root / "investigation-runs.sqlite").load(result["run_id"])
        self.assertEqual(sum(row.get("outcome") == "prior_source_restored"
            for row in saved["checkpoint"]["completed_actions"]), 1)

    def test_second_eviction_allows_explicit_reread_after_another_actual_supply(self):
        fixture = self.fixture
        for number in range(8, 12):
            text = fixture.files["helper-7.cbl"].replace("HELPER-7", f"HELPER-{number}")
            path = f"helper-{number}.cbl"
            (fixture.source / path).write_text(text)
            fixture.files[path] = text
        def respond(payload, number):
            if number <= 7:
                return fixture.read_helper(number)
            if number in {8, 13}:
                self.assertNotIn(self.order_source, self.raw(payload))
                return fixture.read_helper(1)
            if 9 <= number <= 12:
                return fixture.read_helper(number - 1)
            self.assertIn(self.order_source, self.raw(payload))
            return fixture.cited_answer(payload)
        result = fixture.ask(respond, request_guard=14,
            policy=AgentPolicy(context_compaction_enabled=False, max_answer_revisions=0))
        self.assertEqual(len(fixture.requests), 14)
        self.assertTrue(result["answer_available"])
        saved = RunStore(fixture.root / "investigation-runs.sqlite").load(result["run_id"])
        restored = [row for row in saved["checkpoint"]["completed_actions"]
                    if row.get("outcome") == "prior_source_restored"]
        self.assertEqual(len(restored), 2)
        self.assertEqual(restored[0]["restored_evidence_ids"], restored[1]["restored_evidence_ids"])
        self.assertNotEqual(restored[0]["restored_evidence_epochs"], restored[1]["restored_evidence_epochs"])

    def test_user_continuation_keeps_uncited_read_catalog_and_original_round_binding(self):
        fixture = self.fixture
        prior = fixture.ask(lambda payload, number: fixture.read_helper(number) if number <= 7
                            else fixture.cited_answer(payload), request_guard=8,
            policy=AgentPolicy(context_compaction_enabled=False, max_model_requests=8, max_answer_revisions=0))
        store = RunStore(fixture.root / "investigation-runs.sqlite")
        old_memory = store.load(prior["run_id"])["checkpoint"]["prior_source_memory"]
        old_ids = {page["evidence_id"] for page in old_memory["pages"] if page["relative_path"] == "helper-1.cbl"}
        self.assertTrue(old_ids)
        def supplement(payload, number):
            if number == 9:
                entries = payload["prior_source_memory"]["read_catalog"]["entries"]
                self.assertTrue(old_ids.intersection(row["evidence_id"] for row in entries))
                return fixture.read_helper(1)
            self.assertIn(self.order_source, self.raw(payload))
            return fixture.cited_answer(payload)
        continued = fixture.ask(supplement, request_guard=10, resume_run_id=prior["run_id"],
            continue_investigation=True,
            policy=AgentPolicy(context_compaction_enabled=False, max_model_requests=8, max_answer_revisions=0))
        self.assertTrue(continued["answer_available"])
        new_memory = store.load(continued["run_id"])["checkpoint"]["prior_source_memory"]
        for identifier in old_ids:
            self.assertEqual(new_memory["origins"][identifier], old_memory["origins"][identifier])

    def test_received_tool_response_replay_restores_catalog_without_resending_request(self):
        fixture = self.fixture
        class UserCancelled(RuntimeError):
            pass
        def check_cancel():
            if len(fixture.requests) >= 2:
                raise UserCancelled("Interrupted after a received source-read response")
        with self.assertRaises(UserCancelled):
            fixture.ask(lambda payload, number: fixture.read_helper(number),
                request_guard=2, check_cancel=check_cancel)
        store = RunStore(fixture.root / "investigation-runs.sqlite")
        with store.connect() as connection:
            run_id = connection.execute("SELECT run_id FROM investigation_runs").fetchone()[0]
        def resumed(payload, number):
            self.assertEqual(number, 3, "A received model request must be replayed locally")
            entries = payload["prior_source_memory"]["read_catalog"]["entries"]
            self.assertTrue(any(row["relative_path"] == "helper-1.cbl" for row in entries))
            self.assertIn(self.order_source, self.raw(payload))
            return fixture.cited_answer(payload)
        result = fixture.ask(resumed, request_guard=3, resume_run_id=run_id)
        self.assertTrue(result["answer_available"])
        self.assertEqual(len(fixture.requests), 3)
        self.assertEqual(result["run_id"], run_id)


if __name__ == "__main__":
    unittest.main()
