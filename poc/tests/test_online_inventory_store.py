"""Partitioned inventory records, immutable delivery and bounded recovery."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path[:0] = [str(Path(__file__).parent), str(Path(__file__).resolve().parents[1])]
import online_investigation as investigation
import test_online_inventory_work as fixture_module
from online_inventory_store import OnlineInventoryStore
from online_material_format import MaterialError, canonical_json


class OnlineInventoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.OnlineInventoryWorkTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = OnlineInventoryStore(Path(self.temp.name))
        self.addCleanup(self.store.close)
        self.options = dict(workspace_id="w", conversation_id="c", user_message_id="u",
                            manifest=self.fixture.fixture.manifest, question=fixture_module.QUESTION)
        self.session = self.store.session(**self.options)
        self.state = self.fixture.state
        self.session.capture(self.state)
        self.session.attach(self.state)

    def execute(self, action):
        return self.fixture.execute(action)

    def complete_work(self):
        self.execute({"tool": "inventory"})
        for path, raw in self.fixture.fixture.source_bytes.items():
            candidates = self.execute({"tool": "arithmetic_candidates", "path": path})["candidates"]
            last = len(raw.decode().splitlines())
            page = self.execute({"tool": "read", "path": path, "start_line": 1, "end_line": last})
            self.state["request_evidence"] = [page["id"]]
            for candidate in candidates:
                self.execute({"tool": "review", "candidate_id": candidate["candidate_id"], "disposition": "included",
                              "summary": "Verified business input conditions and result for " + path, "citations": [page["id"]]})
            self.execute({"tool": "review_file", "path": path, "start_line": 1, "end_line": last,
                          "summary": "All original source reviewed for " + path, "citations": [page["id"]]})
        self.state.update(answer="Reviewed snapshot business inventory [" + page["id"] + "].", answer_citations=[page["id"]])
        self.assertTrue(self.session.summary()["complete"])

    def test_partitioned_checkpoint_roundtrip_retains_all_pending_and_prior_findings(self):
        self.complete_work()
        checkpoint = investigation._checkpoint(self.state)
        self.assertLess(len(canonical_json(checkpoint)), 20000)
        self.assertEqual(checkpoint["work_ledger"]["files"], {})
        reopened = self.store.session(**self.options, identifier=self.session.id)
        restored = investigation._restore(self.options["manifest"], self.options["question"], checkpoint, inventory_session=reopened)
        reopened.attach(restored)
        self.assertTrue(investigation._work_summary(restored["work_ledger"])["complete"])
        self.assertEqual(reopened.count("files"), len(self.fixture.fixture.source_bytes))
        self.assertGreater(reopened.count("findings"), 0)
        with self.assertRaises(MaterialError):
            investigation._restore(self.options["manifest"], self.options["question"], checkpoint)

    def test_immutable_publication_pages_and_export_preserve_business_prose_and_original_hashes(self):
        self.complete_work()
        publication = self.session.publish(self.state, "Business explanation with required sections", True)
        before = publication.export_data()
        self.assertEqual(before["answer"], "Business explanation with required sections")
        self.assertEqual(len(before["source_reviews"]), len(self.fixture.fixture.source_bytes))
        self.assertTrue(all(row["source_sha256"] and row["citation_refs"] for row in before["findings"]))
        self.session.revise()
        key, row = self.session.rows("findings", limit=1)[0]
        row["summary"] = "Updated wording"
        with self.store.connection() as db:
            self.session._put(db, "findings", key, row, row["path"])
        self.assertEqual(publication.export_data(), before)
        self.assertEqual(publication.page(0, 1)["total"], len(before["findings"]))
        with self.assertRaises(MaterialError):
            self.store.publication(publication.id, conversation_id="other", workspace_id="w", revision=self.options["manifest"]["revision"])

    def test_more_than_4096_actions_remain_deduplicated_outside_checkpoint(self):
        with self.store.connection() as db:
            db.executemany("INSERT INTO actions VALUES (?,?)", [(self.session.id, format(i, '064x')) for i in range(5000)])
        self.assertTrue(self.session.action_done(format(4999, '064x')))
        self.assertEqual(investigation._checkpoint(self.state)["completed_actions"], [])

    def test_unknown_resume_does_not_read_sources_or_send_model_and_retains_store(self):
        self.complete_work()
        self.state["request_status"] = "unknown"
        checkpoint = investigation._checkpoint(self.state)
        client = mock.Mock()
        source = mock.Mock()
        result = investigation.run_investigation(self.fixture.fixture.query, source, self.options["question"],
            checkpoint=checkpoint, inventory_session=self.session, allow_source_transfer=True, client=client)
        self.assertEqual(result["status"], "retry_confirmation_required")
        client.complete.assert_not_called()
        source.assert_not_called()
        self.assertGreater(result["inventory_result"]["total"], 0)

    def test_final_source_verification_budget_resumes_after_last_checked_file(self):
        self.complete_work()
        seen = []
        original = self.fixture.fixture.source
        def read(path, **options):
            if len(seen) == 2:
                raise MaterialError("SOURCE_BUDGET_EXCEEDED")
            seen.append(path)
            return original(path, **options)
        sources = investigation._Sources(self.fixture.fixture.query, read, None)
        with self.assertRaises(MaterialError):
            self.session.verify_sources(self.fixture.fixture.query, sources, self.state, set())
        first = list(seen)
        seen.clear()
        sources = investigation._Sources(self.fixture.fixture.query, lambda path, **opts: (seen.append(path), original(path, **opts))[1], None)
        self.session.verify_sources(self.fixture.fixture.query, sources, self.state, set())
        self.assertFalse(set(first) & set(seen))
        self.assertEqual(set(first + seen), set(self.fixture.fixture.source_bytes))
        self.assertFalse(self.session.meta()["verification"]["active"])

    def test_actual_model_protocol_resumes_and_delivers_all_partitioned_findings(self):
        last_page = [None]
        def plan(context):
            pages = context["source_context"]
            if pages:
                last_page[0] = pages[0]["id"]
            pending = context["work_ledger"]["pending"]
            if not pending:
                if not pages:
                    return {"actions": [{"tool": "recall", "evidence_ids": [last_page[0]]}]}
                key = pages[0]["id"]
                return {"answer": "Snapshot business calculations with applicable conditions [" + key + "].",
                        "citations": [key], "complete": True, "remaining": []}
            path = next((row["path"] for row in pending if "path" in row), None)
            tasks = [row for row in pending if row.get("path") == path]
            for row in tasks:
                if "action" in row:
                    return {"actions": [row["action"]]}
            for row in tasks:
                if row["kind"] == "candidate_review":
                    citations = [page["id"] for page in pages if page["citation"]["path"] == path
                                 and page["citation"]["start"] <= row["start_line"] and page["citation"]["end"] >= row["end_line"]]
                    if not citations:
                        return {"actions": [{"tool": "read", "path": path, "start_line": row["start_line"], "end_line": row["end_line"]}]}
                    return {"actions": [{"tool": "review", "candidate_id": row["candidate_id"],
                        "disposition": "included", "summary": "Reviewed business inputs and conditional result at " + path,
                        "citations": citations}]}
            for row in tasks:
                if row["kind"] == "source_review":
                    citations = [page["id"] for page in pages if page["citation"]["path"] == path
                                 and page["citation"]["start"] <= row["start_line"] and page["citation"]["end"] >= row["end_line"]]
                    if not citations:
                        return {"actions": [{"tool": "read", "path": path, "start_line": row["start_line"], "end_line": row["end_line"]}]}
                    return {"actions": [{"tool": "review_file", "path": path, "start_line": row["start_line"],
                        "end_line": row["end_line"], "summary": "Reviewed entire source and conditional business flow.", "citations": citations}]}
            return {"actions": [pending[0]["action"]]}
        client = self.fixture.fixture.client([plan] * 100)
        checkpoint = None
        for _ in range(30):
            result = investigation.run_investigation(self.fixture.fixture.query, self.fixture.fixture.source,
                self.options["question"], checkpoint=checkpoint, inventory_session=self.session,
                allow_source_transfer=True, client=client, max_model_calls=2, max_actions=4)
            self.assertNotIn(result["status"], {"failed", "retry_confirmation_required"}, result)
            checkpoint = result["checkpoint"]
            self.assertLess(len(canonical_json(checkpoint)), 100000)
            if result["status"] == "answered":
                break
        self.assertEqual(result["status"], "answered", result)
        publication = self.store.publication(result["inventory_result"]["id"], conversation_id="c", workspace_id="w",
                                             revision=self.options["manifest"]["revision"])
        exported = publication.export_data()
        self.assertTrue(exported["complete"])
        self.assertEqual(len(exported["source_reviews"]), len(self.fixture.fixture.source_bytes))
        self.assertGreater(len(exported["findings"]), 0)
        self.assertEqual(publication.page()["total"], len(exported["findings"]))

    def test_credential_guard_runs_before_private_records_and_publication_are_written(self):
        from online_workspace import _guard_secrets
        self.complete_work()
        self.session.guard = lambda value: _guard_secrets(value, {"source_token": "synthetic-private-token"})
        before = self.session.count("findings")
        with self.assertRaisesRegex(MaterialError, "ONLINE_CREDENTIAL_EXPOSURE"):
            self.session.publish(self.state, "Leaked synthetic-private-token", True)
        self.assertEqual(self.session.count("findings"), before)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM publications").fetchone()[0], 0)
            for table in ("records", "versions", "sessions"):
                rows = db.execute("SELECT * FROM " + table).fetchall()
                self.assertNotIn("synthetic-private-token", str(rows))

    def test_answered_checkpoint_revalidates_all_partitioned_source_hashes(self):
        self.complete_work()
        self.state["status"] = "answered"
        checkpoint = investigation._checkpoint(self.state)
        original = self.fixture.fixture.source
        def changed(path, **options):
            return original(path, **options)[:-1] + b"x" if path == "worker.cbl" else original(path, **options)
        client = mock.Mock()
        result = investigation.run_investigation(self.fixture.fixture.query, changed, self.options["question"],
            checkpoint=checkpoint, inventory_session=self.session, allow_source_transfer=True, client=client)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "MATERIAL_SOURCE_HASH_MISMATCH")
        client.complete.assert_not_called()

    def test_workspace_pages_complete_exports_and_source_evidence_use_owned_immutable_handle(self):
        from online_workspace import OnlineWorkbench
        import test_online_investigation as material_fixture
        legacy = fixture_module.OnlineInventoryWorkTests()
        legacy.setUp()
        self.addCleanup(legacy.doCleanups)
        completed = legacy.completed_state()
        marker = "LATE-BUSINESS-CONDITION-AFTER-PREVIEW"
        first = completed["answer_citations"][0]
        last = (completed["evidence"] + completed["archived_evidence"])[-1]["id"]
        completed["answer"] = "Scope [" + first + "]. " + "Business detail. " * 330 + marker + " [" + last + "]."
        completed["answer_citations"] = list(dict.fromkeys([first, last]))
        followup_contexts = []
        def runner(query, source, question, **options):
            if "inventory_session" not in options:
                def capture(context):
                    followup_contexts.append(context)
                    return {"actions": [{"tool": "read", "path": "main.cbl", "start_line": 1, "end_line": 5}]}
                def answer(context):
                    identifier = context["source_context"][0]["id"]
                    return {"answer": "Current source checked [" + identifier + "].", "citations": [identifier], "complete": True, "remaining": []}
                return investigation.run_investigation(query, source, question,
                    client=self.fixture.fixture.client([capture, answer]), **options)
            session = options["inventory_session"]
            state = deepcopy(completed)
            session.capture(state)
            session.attach(state)
            options["checkpoint"] = investigation._checkpoint(state)
            return investigation.run_investigation(query, source, question,
                client=self.fixture.fixture.client([]), **options)
        root = Path(self.temp.name) / "workspace"
        app = OnlineWorkbench(root, investigator=runner,
            material_factory=lambda *args: material_fixture.MaterialReader(material_fixture.DirectoryObjectStore(self.fixture.fixture.material)),
            source_factory=lambda *args: self.fixture.fixture.source)
        self.addCleanup(app.close)
        status, connected = app.dispatch("POST", "/api/online/connect", {
            "location": {"kind": "https", "base_url": "https://materials.invalid/source/"},
            "repository": "example/source", "commit": self.options["manifest"]["repository"]["commit"],
            "revision": self.options["manifest"]["revision"]})
        self.assertEqual(status, 202, connected)
        for thread in list(app._threads.values()):
            thread.join(2)
        workspace_id = app.state()["current_workspace_id"]
        status, created = app.dispatch("POST", "/api/online/conversations", {"workspace_id": workspace_id})
        self.assertEqual(status, 201)
        conversation_id = created["conversation"]["id"]
        status, started = app.dispatch("POST", "/api/online/question", {
            "conversation_id": conversation_id, "question": self.options["question"], "allow_source_transfer": True})
        self.assertEqual(status, 202, started)
        for thread in list(app._threads.values()):
            thread.join(2)
        message = app.state()["conversation"]["messages"][-1]
        self.assertEqual(message["status"], "answered", app.state())
        self.assertTrue(message["inventory_answer_preview"])
        self.assertNotIn(marker, message["content"])
        descriptor = message["inventory_result"]
        self.assertEqual(descriptor["findings"], [])
        self.assertEqual(descriptor["next_offset"], 0)
        route = "/api/online/inventories/" + descriptor["id"]
        params = {"conversation_id": conversation_id}
        status, page = app.dispatch("GET", route, query={**params, "limit": "1"})
        self.assertEqual(status, 200, page)
        self.assertEqual(len(page["findings"]), 1)
        citation = page["findings"][0]["citations"][0]
        status, source = app.dispatch("GET", route + "/evidence", query={**params, "citation_id": citation})
        self.assertEqual(status, 200, source)
        self.assertTrue(source["text"])
        status, exported = app.dispatch("GET", route + "/export", query=params)
        self.assertEqual(status, 200)
        full = json.loads(exported["content"])
        self.assertEqual(sum(row["disposition"] == "included" for row in full["findings"]), page["total"])
        status, conversation_export = app.dispatch("GET", "/api/online/export", query={**params, "format": "json"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(conversation_export["content"])["inventories"][descriptor["id"]], full)
        status, followup = app.dispatch("POST", "/api/online/question", {
            "conversation_id": conversation_id, "question": "Explain the final business condition.", "allow_source_transfer": True})
        self.assertEqual(status, 202, followup)
        for thread in list(app._threads.values()):
            thread.join(2)
        self.assertTrue(followup_contexts)
        historical = next(row for row in followup_contexts[0]["history"] if row["role"] == "assistant")
        self.assertIn(marker, historical["content"])
        self.assertEqual(len(historical["citation_hints"]), len(completed["answer_citations"]))
        status, second = app.dispatch("POST", "/api/online/conversations", {"workspace_id": workspace_id})
        status, denied = app.dispatch("GET", route, query={"conversation_id": second["conversation"]["id"]})
        self.assertEqual(status, 404, denied)
        status, deleted = app.dispatch("DELETE", "/api/online/conversations/" + conversation_id)
        self.assertEqual(status, 200, deleted)
        with app.inventory_store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM publications").fetchone()[0], 0)

    def test_many_partial_deliveries_do_not_duplicate_source_windows_or_truncate_saved_prose(self):
        import test_online_workspace as workspace_fixture
        f = workspace_fixture.OnlineWorkspaceTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        raw = ("IDENTIFICATION DIVISION.\nPROGRAM-ID. LONG-REVIEW.\nPROCEDURE DIVISION.\n"
               + "".join("*> " + "SOURCE-CONTEXT " * 70 + "\n" for _ in range(800)) + "GOBACK.\n").encode()
        f.create_material("e" * 40, "long", raw)
        connected = f.connect("long")
        conversation = f.conversation(connected["workspace_id"])
        cid, wid, user = conversation["id"], connected["workspace_id"], "a" * 32
        with f.app.store.transaction() as db:
            workspace = f.app._workspace(db, wid)
        query, source = f.app._session(workspace, {}, lambda: None)
        state = investigation._new_state(query.reader.manifest, self.options["question"])
        state["answer_requirements"] = {"answer_instructions": "", "answer_language": "en"}
        for start in range(1, 801, 50):
            value = query.evidence("main.cbl", start, min(start + 49, 804), source, max_characters=60000)
            page = {key: value[key] for key in ("text", "citation", "requested_range", "truncated",
                    "truncated_at", "last_line_truncated", "complete_line_count")}
            page.update(id=investigation._evidence_id(page), max_characters=60000)
            state["evidence"].append(page)
        session = f.app.inventory_store.session(workspace_id=wid, conversation_id=cid, user_message_id=user,
            manifest=query.reader.manifest, question=self.options["question"])
        session.capture(state)
        session.attach(state)
        self.assertGreater(len(canonical_json(state["evidence"])), 750000)
        with f.app.store.transaction() as db:
            saved = f.app._conversation(db, cid)
            saved["messages"] = [{"id": user, "role": "user", "content": self.options["question"], "status": "pending"}]
            saved["pending"] = {"user_message_id": user, "question": self.options["question"], "status": "needs_continuation", "checkpoint": None}
            f.app._save_conversation(db, saved)
        originals = {}
        for number in range(60):
            answer = f"Business explanation {number}. " + "Detailed business conditions and formulas. " * 1200 + f" FINAL-{number}"
            publication = session.publish(state, answer, False)
            originals[publication.id] = answer
            with f.app.store.transaction() as db:
                job = f.app._new_job(db, "resume", workspace_id=wid, conversation_id=cid)
            f.app._complete(job["job_id"], {"status": "needs_continuation", "checkpoint": investigation._checkpoint(state),
                "answer": answer, "citations": [], "evidence": state["evidence"], "coverage": {},
                "coverage_ledger": session.summary(), "inventory_result": publication.page(preview=False)}, {})
        with f.app.store.transaction() as db:
            saved = f.app._conversation(db, cid)
        self.assertLess(len(canonical_json(saved)), 1500000)
        self.assertEqual(len(saved["messages"]), 61)
        for message in saved["messages"][1:]:
            self.assertEqual(message["evidence"], [])
            self.assertTrue(message["inventory_answer_preview"])
            self.assertLessEqual(len(message["content"].encode()), 4000)
            self.assertNotIn("answer", message["inventory_result"])
        for message in (saved["messages"][1], saved["messages"][-1]):
            descriptor = message["inventory_result"]
            status, page = f.app.dispatch("GET", "/api/online/inventories/" + descriptor["id"], query={"conversation_id": cid})
            self.assertEqual(status, 200)
            self.assertEqual(page["answer"], originals[descriptor["id"]])
            self.assertFalse(page["answer_preview"])
        exported = json.loads(f.app.export({"conversation_id": cid, "format": "json"})["content"])
        self.assertEqual({key: value["answer"] for key, value in exported["inventories"].items()}, originals)

    def test_preview_citation_budget_keeps_only_visible_ids_and_full_answer_keeps_all(self):
        from online_workspace import _inventory_answer_preview
        self.complete_work()
        evidence = [value for _, value in self.session.rows("evidence", limit=40)]
        identifiers = [page["id"] for page in evidence]
        answer = "Beginning [" + identifiers[0] + "]. " + "Business conditions. " * 1000 + " Final [" + identifiers[-1] + "]."
        citations = [{"id": page["id"], **page["citation"]} for page in evidence]
        preview, visible, marked = _inventory_answer_preview(answer, citations)
        self.assertTrue(marked)
        self.assertLessEqual(len(canonical_json({"content": preview, "citations": visible})), 6000)
        self.assertEqual([row["id"] for row in visible], identifiers[:1])
        self.state["answer_citations"] = identifiers
        publication = self.session.publish(self.state, answer, True)
        page = publication.page()
        self.assertEqual(page["answer"], answer)
        self.assertEqual(page["answer_citations"], citations)
        self.assertNotIn("answer", publication.page(preview=False))

    def test_delete_conversation_removes_owned_versions_and_published_views(self):
        self.complete_work()
        publication = self.session.publish(self.state, "Complete", True)
        self.store.delete_conversation("c")
        with self.assertRaises(MaterialError):
            self.store.publication(publication.id, conversation_id="c", workspace_id="w", revision=self.options["manifest"]["revision"])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM versions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
