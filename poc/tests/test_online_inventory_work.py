"""Durable online inventory control and original-source review boundaries."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[1])]
import test_online_investigation as fixture_module
import online_investigation as investigation
from company_api import APIClientError
from online_material_format import MaterialError, canonical_json
from online_workspace import OnlineWorkbench, _inventory_coverage


QUESTION = "List all premium calculation formulas across the entire repository."


class OnlineInventoryWorkTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.OnlineInvestigationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = investigation._new_state(self.fixture.manifest, QUESTION)
        self.state["answer_requirements"] = {"answer_instructions": "", "answer_language": "en"}
        self.sources = investigation._Sources(self.fixture.query, self.fixture.source, None)
        self.verified = set()

    def execute(self, action):
        return investigation._execute(self.fixture.query, self.sources, investigation._action(action), self.state, self.verified)

    def read_main(self):
        return self.execute(fixture_module.read())

    def completed_state(self):
        self.execute({"tool": "inventory"})
        first_id = None
        for path, raw in self.fixture.source_bytes.items():
            candidates = self.execute({"tool": "arithmetic_candidates", "path": path})["candidates"]
            last = len(raw.decode().splitlines())
            page = self.execute({"tool": "read", "path": path, "start_line": 1, "end_line": last})
            first_id = first_id or page["id"]
            self.state["request_evidence"] = [page["id"]]
            for candidate in candidates:
                self.execute({"tool": "review", "candidate_id": candidate["candidate_id"], "disposition": "included",
                    "summary": "Earlier English finding for the input condition and review status.", "citations": [page["id"]]})
            if path == "worker.cbl":
                self.execute({"tool": "record_finding", "path": path, "start_line": 5, "end_line": 5,
                    "disposition": "included", "summary": "Earlier English finding for the worker processing branch.",
                    "citations": [page["id"]]})
            self.execute({"tool": "review_file", "path": path, "start_line": 1, "end_line": last,
                "summary": "All original source lines and visible branch conditions in this file were reviewed.",
                "citations": [page["id"]]})
        self.state.update(status="answered", answer="Completed source inventory [" + first_id + "].", answer_citations=[first_id])
        self.assertTrue(investigation._work_summary(self.state["work_ledger"])["complete"])
        return deepcopy(self.state)

    def test_inventory_pages_cover_every_indexed_file_independent_of_terms(self):
        seen, cursor, cursors = [], None, []
        while True:
            page = self.fixture.query.inventory(1, cursor=cursor)
            seen.extend(item["relative_path"] for item in page["files"])
            if page["complete"]:
                break
            cursor = page["next_cursor"]
            self.assertNotIn(cursor, cursors)
            cursors.append(cursor)
        self.assertCountEqual(seen, self.fixture.source_bytes)
        self.assertEqual(len(seen), len(set(seen)))
        with self.assertRaises(MaterialError):
            self.fixture.query.inventory(1, cursor={"bucket": "00", "after": "../private"})

    def test_sparse_candidate_count_rejects_invalid_and_impossible_values(self):
        original = self.fixture.query.reader.facts("main.cbl")
        candidate = next(item for item in original["rules"] if item["rule_kind"] == "MOVE")
        for value in ([], "2", False, 0, 2 ** 31):
            with self.subTest(value=value):
                facts = deepcopy(original)
                next(item for item in facts["rules"] if item["rule_id"] == candidate["rule_id"])["occurrence_count"] = value
                with mock.patch.object(self.fixture.query.reader, "facts", return_value=facts), self.assertRaises(MaterialError):
                    self.fixture.query.arithmetic_candidates("main.cbl")

    def test_unfinished_search_survives_prompt_and_checkpoint_observation_windows(self):
        self.execute({"tool": "inventory"})
        self.execute({"tool": "search", "text": "ENTRY", "limit": 1})
        work = deepcopy(self.state["work_ledger"])
        for number in range(98):
            investigation._observe(self.state, {"tool": "source_review"}, {"number": number})
        self.assertEqual(len(self.state["observations"]), 96)
        self.assertEqual(self.state["work_ledger"], work)
        restored = investigation._restore(self.fixture.manifest, QUESTION, self.state)
        messages, _ = investigation._messages(self.fixture.query, self.sources, restored, [], self.verified)
        context = json.loads(messages[-1]["content"])
        self.assertEqual(context["observations_omitted"], 86)
        pending = next(item for item in context["work_ledger"]["pending"] if item["kind"] == "query_page")
        self.assertEqual(pending["action"]["offset"], 1)
        self.assertEqual(pending["action"]["text"], "ENTRY")

    def test_later_search_page_cannot_hide_an_earlier_unvisited_page(self):
        self.execute({"tool": "search", "text": "ENTRY", "limit": 1, "offset": 2})
        pending = next(item for item in investigation._work_pending(self.state["work_ledger"]) if item["kind"] == "query_page")
        self.assertEqual(pending["action"]["offset"], 0)

    def test_premature_answer_remains_visible_draft_with_pending_work(self):
        result = self.fixture.run_question([
            {"actions": [fixture_module.read()]},
            lambda context: fixture_module.answer(context, "This is the only formula in the system."),
        ], question=QUESTION, max_model_calls=2)
        self.assertEqual(result["status"], "needs_continuation", result)
        self.assertFalse(result["coverage_ledger"]["complete"])
        self.assertGreater(result["coverage_ledger"]["pending_count"], 0)
        self.assertIn("unfinished draft", result["answer"])
        self.assertIn("not passed coverage verification", result["answer"])
        self.assertIn("This is the only formula", result["answer"])
        self.assertTrue(any(event["phase"] == "investigating" for event in self.fixture.events))
        self.assertFalse(any(event["phase"] == "inventory_continuing" for event in self.fixture.events))

    def test_ordinary_question_is_not_forced_to_review_all_lexical_hits(self):
        result = self.fixture.run_question([
            {"actions": [{"tool": "search", "text": "ENTRY", "limit": 1}, fixture_module.read()]},
            fixture_module.answer,
        ])
        self.assertEqual(result["status"], "answered", result)
        self.assertFalse(result["coverage_ledger"]["active"])

    def test_review_requires_current_verified_complete_source_ranges(self):
        self.execute({"tool": "inventory"})
        candidate = self.execute({"tool": "arithmetic_candidates", "path": "main.cbl"})["candidates"][0]
        page = self.execute(fixture_module.read(start=1, end=1))
        self.state["request_evidence"] = [page["id"]]
        with self.assertRaisesRegex(MaterialError, "ONLINE_INVENTORY_SOURCE_REVIEW_REQUIRED"):
            self.execute({"tool": "review", "candidate_id": candidate["candidate_id"], "disposition": "excluded",
                          "summary": "A diagnostic status assignment, not a business amount.", "citations": [page["id"]]})
        self.assertFalse(self.state["work_ledger"]["findings"])

    def test_reviewed_file_without_arithmetic_still_requires_its_whole_source(self):
        self.execute({"tool": "inventory"})
        self.execute({"tool": "arithmetic_candidates", "path": "worker.cbl"})
        self.assertEqual(self.state["work_ledger"]["files"]["worker.cbl"]["candidate_total"], 0)
        pending = investigation._work_pending(self.state["work_ledger"])
        self.assertTrue(any(row["kind"] == "source_range" and row["path"] == "worker.cbl" for row in pending))
        self.assertTrue(any(row["kind"] == "source_review" and row["path"] == "worker.cbl" for row in pending))

    def test_duplicate_physical_position_cannot_satisfy_multiple_occurrences(self):
        self.execute({"tool": "inventory"})
        candidate = self.execute({"tool": "arithmetic_candidates", "path": "main.cbl"})["candidates"][0]
        self.state["work_ledger"]["candidates"][candidate["candidate_id"]]["occurrence_count"] = 2
        page = self.read_main()
        self.state["request_evidence"] = [page["id"]]
        review = {"tool": "review", "candidate_id": candidate["candidate_id"], "occurrence": 1,
                  "start_line": candidate["start_line"], "end_line": candidate["end_line"],
                  "disposition": "excluded", "summary": "A diagnostic status assignment, not a business formula.",
                  "citations": [page["id"]]}
        self.execute(review)
        with self.assertRaisesRegex(MaterialError, "ONLINE_INVENTORY_OCCURRENCE_POSITION_AMBIGUOUS"):
            self.execute({**review, "occurrence": 2})
        self.assertEqual(len(self.state["work_ledger"]["findings"]), 1)

    def test_reviewed_original_pages_archive_and_batch_recall_without_losing_early_page(self):
        self.execute({"tool": "inventory"})
        pages = [self.execute(fixture_module.read(start=1, end=6)), self.execute(fixture_module.read(start=7, end=13))]
        identifiers = [page["id"] for page in pages]
        self.state["request_evidence"] = identifiers
        self.execute({"tool": "review_file", "path": "main.cbl", "start_line": 1, "end_line": 13,
            "summary": "Positive input calls the worker; otherwise the status is sent for review. The source conditions and call are retained.",
            "citations": identifiers})
        self.assertFalse(self.state["evidence"])
        self.assertEqual(len(self.state["archived_evidence"]), 2)
        self.state = investigation._restore(self.fixture.manifest, QUESTION, self.state)
        self.execute({"tool": "recall", "evidence_ids": identifiers})
        messages, current = investigation._messages(self.fixture.query, self.sources, self.state, [], self.verified)
        self.assertCountEqual(current, identifiers)
        context = json.loads(messages[-1]["content"])
        self.assertEqual(len(context["source_context"]), 2)
        self.assertIn("Positive input calls the worker", context["reviewed_source_summaries"][0]["summary"])

    def test_capacity_failure_keeps_prior_work_and_does_not_mark_candidate_page_complete(self):
        self.execute({"tool": "inventory"})
        old = deepcopy(self.state)
        with mock.patch.object(investigation, "MAX_CHECKPOINT_BYTES", len(canonical_json(old)) + 10):
            with self.assertRaisesRegex(MaterialError, "ONLINE_CHECKPOINT_CAPACITY_LIMIT"):
                self.execute({"tool": "arithmetic_candidates", "path": "main.cbl"})
        self.assertEqual(self.state, old)
        self.assertIsNone(self.state["work_ledger"]["files"]["main.cbl"]["candidate_total"])

    def test_unknown_outcome_retains_ledger_and_never_reissues_without_approval(self):
        class Unknown:
            last_request_outcome = "unknown"
            calls = 0
            def complete(self, **kwargs):
                self.calls += 1
                raise APIClientError("REQUEST_TIMEOUT")
        client = Unknown()
        first = investigation.run_investigation(self.fixture.query, self.fixture.source, QUESTION,
            client=client, allow_source_transfer=True)
        second = investigation.run_investigation(self.fixture.query, self.fixture.source, QUESTION,
            client=client, checkpoint=first["checkpoint"], allow_source_transfer=True)
        self.assertEqual(second["status"], "retry_confirmation_required")
        self.assertEqual(client.calls, 1)
        self.assertEqual(second["checkpoint"]["work_ledger"], first["checkpoint"]["work_ledger"])
        self.assertNotIn("test-secret", json.dumps(second))

    def test_legacy_checkpoint_rechecks_snapshot_without_discarding_verified_evidence(self):
        page = self.read_main()
        legacy = deepcopy(self.state)
        legacy.pop("work_ledger")
        legacy.pop("archived_evidence")
        restored = investigation._restore(self.fixture.manifest, QUESTION, legacy)
        self.assertTrue(restored["work_ledger"]["legacy_checkpoint_recheck"])
        self.assertEqual(restored["evidence"][0]["id"], page["id"])
        self.assertFalse(investigation._work_summary(restored["work_ledger"])["complete"])
        invalid = {**legacy, "unrecognized_work": {}}
        with self.assertRaises(MaterialError):
            investigation._restore(self.fixture.manifest, QUESTION, invalid)

    def test_answered_recovery_revalidates_all_cumulative_active_and_archived_sources(self):
        checkpoint = self.completed_state()
        main_id = checkpoint["answer_citations"][0]
        self.execute({"tool": "recall", "evidence_ids": [main_id]})
        checkpoint = deepcopy(self.state)
        self.assertTrue(checkpoint["evidence"])
        self.assertTrue(checkpoint["archived_evidence"])
        self.fixture.source_calls.clear()
        replay = self.fixture.run_question([], question=QUESTION, checkpoint=checkpoint)
        self.assertEqual(replay["status"], "answered", replay)
        self.assertEqual(replay["model_calls"], 0)
        self.assertCountEqual(self.fixture.source_calls, self.fixture.source_bytes)
        def changed(path, **kwargs):
            raw = self.fixture.source_bytes[path]
            return raw.replace(b"PROCESS", b"PROCESX") if path == "worker.cbl" else raw
        failed = self.fixture.run_question([], question=QUESTION, checkpoint=checkpoint, source_reader=changed)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "MATERIAL_SOURCE_HASH_MISMATCH")
        self.assertEqual(failed["model_calls"], 0)
        self.assertIn("unfinished draft", failed["answer"])

    def test_changed_requirements_restate_every_saved_finding_without_losing_provenance(self):
        checkpoint = self.completed_state()
        originals = deepcopy(checkpoint["work_ledger"]["findings"])
        requirements = "請以繁體中文回答，保留所有業務條件和原文定位。"
        def planner(context):
            pending = context["work_ledger"]["pending"]
            if pending:
                row = next(item for item in pending if item["kind"] == "candidate_review")
                original = next(item for item in context["retained_findings"] if item["candidate_id"] == row["candidate_id"])
                active = {page["id"] for page in context["source_context"]}
                if not set(original["citations"]) <= active:
                    return {"actions": [{"tool": "recall", "evidence_ids": original["citations"]}]}
                return {"actions": [{"tool": "review", "candidate_id": row["candidate_id"],
                    "occurrence": original["occurrence"], "start_line": original["start_line"], "end_line": original["end_line"],
                    "disposition": original["disposition"], "summary": "已確認此來源的金額處理條件及後續業務分支。",
                    "citations": original["citations"]}]}
            identifier = context["source_context"][0]["id"]
            return {"answer": "已按目前要求整理全部已核對結論。[" + identifier + "]",
                    "citations": [identifier], "complete": True, "remaining": []}
        partial = self.fixture.run_question([planner], question=QUESTION, checkpoint=checkpoint,
            answer_instructions=requirements, max_model_calls=1)
        self.assertEqual(partial["status"], "needs_continuation")
        self.assertTrue(partial["checkpoint"]["work_ledger"]["presentation_pending"])
        self.assertNotIn("Earlier English finding", partial["answer"])
        self.assertEqual(partial["checkpoint"]["work_ledger"]["findings"], originals)
        result = self.fixture.run_question([planner] * 10, question=QUESTION, checkpoint=partial["checkpoint"],
            answer_instructions=requirements, max_model_calls=10)
        self.assertEqual(result["status"], "answered", result)
        self.assertEqual(result["answer_language"], "zh-HK")
        self.assertFalse(result["checkpoint"]["work_ledger"]["presentation_pending"])
        self.assertNotIn("Earlier English finding", result["answer"])
        self.assertIn("已確認此來源", result["answer"])
        for key, finding in result["checkpoint"]["work_ledger"]["findings"].items():
            self.assertEqual({k: v for k, v in finding.items() if k != "summary"},
                             {k: v for k, v in originals[key].items() if k != "summary"})

    def test_finding_summary_cannot_add_an_unverified_inline_citation(self):
        self.execute({"tool": "inventory"})
        candidate = self.execute({"tool": "arithmetic_candidates", "path": "main.cbl"})["candidates"][0]
        page = self.read_main()
        self.state["request_evidence"] = [page["id"]]
        action = {"tool": "review", "candidate_id": candidate["candidate_id"], "disposition": "included",
                  "summary": "Source-supported condition [Eaaaaaaaaaaaaaaaaaaaaaaaa].", "citations": [page["id"]]}
        with self.assertRaisesRegex(MaterialError, "ONLINE_INVALID_MODEL_CITATIONS"):
            self.execute(action)
        self.assertFalse(self.state["work_ledger"]["findings"])
        self.execute({**action, "summary": "Source-supported condition [" + page["id"] + "]."})
        invalid = deepcopy(self.state)
        next(iter(invalid["work_ledger"]["findings"].values()))["summary"] = action["summary"]
        with self.assertRaises(MaterialError):
            investigation._restore(self.fixture.manifest, QUESTION, invalid)

    def test_workspace_persists_visible_cumulative_findings_and_scoped_coverage(self):
        completed = self.completed_state()
        def runner(query, source, question, **options):
            return investigation.run_investigation(query, source, question, client=self.fixture.client([]),
                **{**options, "checkpoint": completed})
        root = self.fixture.root / "online-inventory-state"
        app = OnlineWorkbench(root, investigator=runner,
            material_factory=lambda *args: fixture_module.MaterialReader(fixture_module.DirectoryObjectStore(self.fixture.material)),
            source_factory=lambda *args: self.fixture.source)
        self.addCleanup(app.close)
        status, connection = app.dispatch("POST", "/api/online/connect", {
            "location": {"kind": "https", "base_url": "https://materials.invalid/source/"},
            "repository": "example/source", "commit": self.fixture.manifest["repository"]["commit"],
            "revision": self.fixture.manifest["revision"]})
        self.assertEqual(status, 202, connection)
        for thread in list(app._threads.values()):
            thread.join(2)
        workspace_id = app.state()["current_workspace_id"]
        status, created = app.dispatch("POST", "/api/online/conversations", {"workspace_id": workspace_id})
        self.assertEqual(status, 201, created)
        status, started = app.dispatch("POST", "/api/online/question", {"conversation_id": created["conversation"]["id"],
            "question": QUESTION, "allow_source_transfer": True})
        self.assertEqual(status, 202, started)
        for thread in list(app._threads.values()):
            thread.join(2)
        conversation = app.state()["conversation"]
        self.assertEqual(conversation["status"], "answered", conversation)
        self.assertIsNone(conversation["pending"])
        message = conversation["messages"][-1]
        self.assertTrue(message["coverage_ledger"]["complete"])
        self.assertEqual(message["coverage_ledger"]["scope"], "indexed_snapshot")
        self.assertFalse(message["coverage_ledger"]["full_repository_or_runtime_proof"])
        for finding in message["findings"]:
            self.assertIn(finding["summary"], message["content"])
        reloaded = OnlineWorkbench(root)
        self.addCleanup(reloaded.close)
        self.assertEqual(reloaded.state()["conversation"], conversation)
        self.assertNotIn("test-secret", json.dumps(conversation))

    def test_public_coverage_ignores_private_fields_and_never_claims_runtime_completeness(self):
        value = {**investigation._work_summary(self.state["work_ledger"]),
                 "private_debug": "untrusted-error-body", "full_repository_or_runtime_proof": True}
        projected = _inventory_coverage(value)
        self.assertNotIn("private_debug", projected)
        self.assertFalse(projected["full_repository_or_runtime_proof"])
        self.assertIsNone(_inventory_coverage({**value, "files_reviewed": "private-error-body"}))

    def test_more_than_256_reviewed_source_pages_preserve_summaries_and_can_be_recalled(self):
        source = self.fixture.root / "long-source"
        source.mkdir()
        raw = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. LONG-ENTRY.\nPROCEDURE DIVISION.\n"
               + "\n".join("DISPLAY 'STATUS-" + str(number) + "'." for number in range(600)) + "\nGOBACK.\n").encode()
        (source / "long.cbl").write_bytes(raw)
        database = self.fixture.root / "long.sqlite"
        report = fixture_module.build_business_index(source, database, source_format="free", verify_content=True, framework_reference_path="")
        material = self.fixture.root / "long-material"
        manifest = fixture_module.export_material(database, source, material, repository_id="example/source", commit="e" * 40, build_report=report)
        self.fixture.query = fixture_module.MaterialQuery(fixture_module.MaterialReader(fixture_module.DirectoryObjectStore(material)))
        self.state = investigation._new_state(manifest, QUESTION)
        self.state["answer_requirements"] = {"answer_instructions": "", "answer_language": "en"}
        self.sources = investigation._Sources(self.fixture.query, lambda *args, **kwargs: raw, None)
        self.execute({"tool": "inventory"})
        self.execute({"tool": "arithmetic_candidates", "path": "long.cbl"})
        total = len(raw.decode().splitlines())
        for first in range(1, total + 1, 2):
            last = min(total, first + 1)
            page = self.execute({"tool": "read", "path": "long.cbl", "start_line": first, "end_line": last})
            self.state["request_evidence"] = [page["id"]]
            self.execute({"tool": "review_file", "path": "long.cbl", "start_line": first, "end_line": last,
                "summary": "This source range reports a diagnostic status and contains no business amount calculation.",
                "citations": [page["id"]]})
        self.assertGreater(len(self.state["archived_evidence"]), 256)
        self.assertFalse(self.state["evidence"])
        self.assertEqual(len(self.state["work_ledger"]["files"]["long.cbl"]["reviews"]), (total + 1) // 2)
        self.assertTrue(investigation._work_summary(self.state["work_ledger"])["complete"])
        self.state = investigation._restore(manifest, QUESTION, self.state)
        identifiers = [self.state["archived_evidence"][index]["id"] for index in (0, -1)]
        self.execute({"tool": "recall", "evidence_ids": identifiers})
        self.assertEqual({page["id"] for page in self.state["evidence"]}, set(identifiers))
        self.assertEqual(self.sources.requests, 1, "Pinned source bytes are reused within the bounded per-run cache")


if __name__ == "__main__":
    unittest.main()
