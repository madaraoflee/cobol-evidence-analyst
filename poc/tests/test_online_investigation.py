from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from business_index import build_business_index
from company_api import APIClientError, CompanyAPIConfig, OpenAICompatibleChatClient, TransportResponse
from online_investigation import run_investigation, _evidence_id
from online_material_export import export_material
from online_material_format import MaterialError, canonical_json, seal_manifest
from online_material_query import MaterialQuery
from online_material_reader import DirectoryObjectStore, MaterialReader


def reply(value, *, finish="stop"):
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return TransportResponse(200, json.dumps({"choices": [{"message": {"role": "assistant", "content": content},
                                                          "finish_reason": finish}]}))


def read(path="main.cbl", start=1, end=13, **options):
    return {"tool": "read", "path": path, "start_line": start, "end_line": end, **options}


def answer(context, text="正金额进入处理分支，零或负金额进入复核分支。", *, complete=True, remaining=None):
    pages = context["source_context"]
    return {"answer": text + " " + " ".join("[" + item["id"] + "]" for item in pages),
            "citations": [item["id"] for item in pages], "complete": complete, "remaining": remaining or []}


class OnlineInvestigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = self.root / "source"
        source.mkdir()
        self.source_bytes = {
            "main.cbl": ("IDENTIFICATION DIVISION.\nPROGRAM-ID. MAIN-ENTRY.\nDATA DIVISION.\n"
                         "WORKING-STORAGE SECTION.\nCOPY SHARED-RECORD.\nPROCEDURE DIVISION.\n"
                         "MAIN-PARA.\nIF INPUT-AMOUNT > 0\nCALL 'WORKER-ENTRY'\nELSE\n"
                         "MOVE 'REVIEW' TO RESULT-STATE\nEND-IF.\nGOBACK.\n").encode(),
            "worker.cbl": ("IDENTIFICATION DIVISION.\nPROGRAM-ID. WORKER-ENTRY.\nPROCEDURE DIVISION.\n"
                           "WORK-PARA.\nDISPLAY 'PROCESS'.\nGOBACK.\n").encode(),
            "SHARED-RECORD.cpy": b"01 INPUT-AMOUNT PIC S9(7).\n01 RESULT-STATE PIC X(10).\n",
            "dynamic.cbl": ("IDENTIFICATION DIVISION.\nPROGRAM-ID. DYNAMIC-ENTRY.\nDATA DIVISION.\n"
                            "WORKING-STORAGE SECTION.\n01 PROGRAM-NAME PIC X(20).\n"
                            "PROCEDURE DIVISION.\nCALL PROGRAM-NAME.\nGOBACK.\n").encode(),
        }
        for name, data in self.source_bytes.items():
            (source / name).write_bytes(data)
        database = self.root / "index.sqlite"
        report = build_business_index(source, database, source_format="free", verify_content=True,
                                      framework_reference_path="")
        self.material = self.root / "material"
        self.manifest = export_material(database, source, self.material, repository_id="example/source",
                                        commit="b" * 40, build_report=report)
        shutil.rmtree(source)
        for path in self.root.glob("index.sqlite*"):
            path.unlink()
        self.query = MaterialQuery(MaterialReader(DirectoryObjectStore(self.material)))
        self.config = CompanyAPIConfig("https://gateway.example.invalid/v1", "test-model", api_key="test-secret")
        self.requests, self.contexts, self.source_calls, self.events = [], [], [], []

    def source(self, path, *, max_bytes):
        self.source_calls.append(path)
        return self.source_bytes[path]

    def client(self, replies):
        replies = iter(replies)
        def transport(request):
            self.requests.append(json.loads(request.body))
            context = json.loads(self.requests[-1]["messages"][-1]["content"])
            self.contexts.append(context)
            value = next(replies)
            if callable(value):
                value = value(context)
            return value if isinstance(value, TransportResponse) else reply(value)
        return OpenAICompatibleChatClient(self.config, transport=transport)

    def run_question(self, replies, **options):
        return run_investigation(self.query, options.pop("source_reader", self.source),
            options.pop("question", "说明 MAIN-ENTRY 的金额处理规则"),
            client=self.client(replies), allow_source_transfer=options.pop("allow_source_transfer", True),
            progress=options.pop("progress", self.events.append), **options)

    def test_default_requires_explicit_source_transfer_authorization(self):
        for authorization in (False, "true", 1, None):
            with self.subTest(authorization=authorization):
                result = self.run_question([], allow_source_transfer=authorization)
                self.assertEqual(result["error"], "ONLINE_SOURCE_TRANSFER_REQUIRED")
        self.assertFalse(self.requests)
        self.assertFalse(self.source_calls)

    def test_real_material_search_relation_read_business_answer_has_only_verified_citations(self):
        result = self.run_question([
            {"actions": [{"tool": "search", "text": "MAIN-ENTRY"}]},
            {"actions": [{"tool": "relations", "path": "main.cbl"}, read()]},
            answer,
        ])
        self.assertEqual(result["status"], "answered", result)
        self.assertEqual(result["model_calls"], 3)
        self.assertEqual(result["source_requests"], 1)
        self.assertEqual(result["citations"][0]["commit"], "b" * 40)
        self.assertIn("正金额", result["answer"])
        self.assertFalse(list(self.root.rglob("*.sqlite")))
        self.assertNotIn(str(self.root), json.dumps(result, ensure_ascii=False))
        self.assertNotIn("test-secret", json.dumps(result, ensure_ascii=False))
        self.assertIn("untrusted data", self.requests[-1]["messages"][0]["content"])
        self.assertFalse(result["coverage"]["full_repository_or_runtime_proof"])

    def test_action_budget_checkpoint_resumes_pending_work_without_repeating_planner(self):
        first = self.run_question([{"actions": [read(), read("worker.cbl", 1, 6),
                                                 {"tool": "incoming", "target": "WORKER-ENTRY"}]}],
                                  max_actions=1)
        self.assertEqual(first["status"], "needs_continuation")
        self.assertEqual(len(first["checkpoint"]["pending_actions"]), 2)
        before = len(self.requests)
        second = self.run_question([answer], checkpoint=first["checkpoint"])
        self.assertEqual(second["status"], "answered", second)
        self.assertEqual(len(self.requests) - before, 1)
        self.assertEqual(second["actions"], 2)
        self.assertEqual({item["path"] for item in second["citations"]}, {"main.cbl", "worker.cbl"})
        self.assertIn("main.cbl", self.source_calls[1:])

    def test_partial_answer_preserves_question_and_remaining_on_continuation(self):
        first = self.run_question([{"actions": [read()]},
                                   lambda context: answer(context, complete=False, remaining=["核对被调用程序"] )])
        self.assertEqual(first["status"], "needs_continuation")
        second = self.run_question([{"actions": [read("worker.cbl", 1, 6)]}, answer],
                                   checkpoint=first["checkpoint"])
        self.assertEqual(second["status"], "answered")
        self.assertEqual(self.contexts[2]["remaining"], ["核对被调用程序"])
        self.assertEqual(self.contexts[2]["question"], first["checkpoint"]["question"])

    def test_same_file_multiple_ranges_use_bounded_per_run_memory_cache(self):
        result = self.run_question([{"actions": [read(start=1, end=6), read(start=7, end=13)]}, answer])
        self.assertEqual(result["status"], "answered")
        self.assertEqual(self.source_calls, ["main.cbl"])
        self.assertEqual(len(result["citations"]), 2)

    def test_unknown_model_outcome_requires_explicit_retry_and_never_auto_repeats(self):
        class Unknown:
            last_request_outcome = "unknown"
            def complete(self, **options):
                raise APIClientError("REQUEST_TIMEOUT")
        first = run_investigation(self.query, self.source, "question", client=Unknown(), allow_source_transfer=True)
        self.assertEqual(first["status"], "retry_confirmation_required")
        no_retry = self.run_question([], checkpoint=first["checkpoint"], question="question")
        self.assertEqual(no_retry["error"], "ONLINE_REQUEST_OUTCOME_UNKNOWN")
        explicit = self.run_question([{"actions": [read()]}, answer], checkpoint=first["checkpoint"],
                                     question="question", approve_retry=True)
        self.assertEqual(explicit["status"], "answered")

    def test_known_auth_failure_is_safe_and_does_not_auto_retry(self):
        result = self.run_question([TransportResponse(401, '{"secret":"remote-body"}')])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("remote-body", json.dumps(result))
        self.assertNotEqual(result["checkpoint"]["request_status"], "unknown")

    def test_response_journal_recovers_without_resending_received_model_exchange(self):
        saved = []
        def journal(event):
            saved.append(deepcopy(event))
            if event["phase"] == "model_response_received":
                raise OSError("private journal location")
        first = self.run_question([{"actions": [read()]}], progress=journal)
        self.assertEqual(first["status"], "failed")
        checkpoint = saved[-1]["checkpoint"]
        self.assertIsNotNone(checkpoint["pending_response"])
        before = len(self.requests)
        resumed = self.run_question([answer], checkpoint=checkpoint)
        self.assertEqual(resumed["status"], "answered")
        self.assertEqual(len(self.requests) - before, 1)
        self.assertNotIn("private journal", json.dumps(first))

    def test_failed_pre_request_journal_never_sends_request(self):
        def journal(event):
            raise OSError("disk unavailable")
        result = self.run_question([], progress=journal)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["model_calls"], 0)
        self.assertFalse(self.requests)

    def test_cancel_after_received_response_retains_it_for_resume(self):
        cancelled = [False]
        def check():
            if cancelled[0]:
                raise MaterialError("ONLINE_CANCELLED")
        def progress(event):
            if event["phase"] == "model_response_received":
                cancelled[0] = True
        first = self.run_question([{"actions": [read()]}], progress=progress, check_cancel=check)
        self.assertEqual(first["status"], "cancelled")
        self.assertIsNotNone(first["checkpoint"]["pending_response"])
        resumed = self.run_question([answer], checkpoint=first["checkpoint"])
        self.assertEqual(resumed["status"], "answered")

    def test_cancel_during_source_read_keeps_action_pending(self):
        cancelled = [False]
        def check():
            if cancelled[0]:
                raise MaterialError("ONLINE_CANCELLED")
        def source(path, *, max_bytes):
            cancelled[0] = True
            return self.source_bytes[path]
        result = self.run_question([{"actions": [read()]}], source_reader=source, check_cancel=check)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["checkpoint"]["pending_actions"][0]["tool"], "read")
        self.assertFalse(result["evidence"])

    def test_checkpoint_cannot_change_source_revision_or_question(self):
        first = self.run_question([{"actions": [read()]}], max_model_calls=1)
        bad = deepcopy(first["checkpoint"])
        bad["revision"] = "c" * 64
        with self.assertRaisesRegex(MaterialError, "ONLINE_REVISION_MISMATCH"):
            self.run_question([], checkpoint=bad)
        with self.assertRaisesRegex(MaterialError, "ONLINE_QUESTION_MISMATCH"):
            self.run_question([], checkpoint=first["checkpoint"], question="another question")

    def test_old_evidence_is_revalidated_and_revoked_access_stops_before_model(self):
        first = self.run_question([{"actions": [read()]}], max_model_calls=1)
        before = len(self.requests)
        def denied(*args, **options):
            raise MaterialError("SOURCE_FORBIDDEN")
        resumed = self.run_question([], checkpoint=first["checkpoint"], source_reader=denied)
        self.assertEqual(resumed["error"], "SOURCE_FORBIDDEN")
        self.assertEqual(len(self.requests), before)

    def test_tampered_excerpt_cannot_be_promoted_by_recomputing_its_id(self):
        first = self.run_question([{"actions": [read()]}], max_model_calls=1)
        bad = deepcopy(first["checkpoint"])
        page = bad["evidence"][0]
        page["text"] = "fabricated source"
        page["id"] = _evidence_id(page)
        bad["focus_evidence"] = [page["id"]]
        bad["request_evidence"] = []
        result = self.run_question([], checkpoint=bad)
        self.assertEqual(result["error"], "ONLINE_EVIDENCE_CHANGED")

    def test_invalid_citations_never_become_delivered_answer(self):
        bad = {"answer": "unsupported [E" + "0" * 24 + "]", "citations": ["E" + "0" * 24],
               "complete": True, "remaining": []}
        result = self.run_question([bad, bad, bad])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "ONLINE_MODEL_PROTOCOL_FAILED")
        self.assertEqual(result["answer"], "")
        self.assertFalse(result["citations"])

    def test_malformed_model_reply_can_recover_without_claiming_it_was_answer(self):
        result = self.run_question(["not a protocol response", {"actions": [read()]}, answer])
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["checkpoint"]["protocol_failures"], 1)

    def test_unsupported_tools_are_rejected_and_never_executed(self):
        response = {"actions": [{"tool": "shell", "command": "cat private-file"}]}
        result = self.run_question([response, response, response])
        self.assertEqual(result["error"], "ONLINE_MODEL_PROTOCOL_FAILED")
        self.assertFalse(self.source_calls)

    def test_partial_line_remains_explicit_and_has_precise_citation(self):
        result = self.run_question([{"actions": [read(start=2, end=2, max_characters=8)]}, answer])
        self.assertEqual(result["status"], "answered")
        self.assertTrue(result["evidence"][0]["truncated"])
        self.assertTrue(result["evidence"][0]["last_line_truncated"])
        self.assertEqual(result["citations"][0]["start"], 2)
        self.assertEqual(result["citations"][0]["end"], 2)

    def test_dynamic_and_missing_copy_boundaries_are_not_promoted_to_confirmed(self):
        result = self.run_question([{"actions": [{"tool": "relations", "path": "dynamic.cbl"}, read()]}],
                                  max_model_calls=1)
        relations = result["checkpoint"]["observations"][0]["result"]["relations"]
        self.assertTrue(any(item["relation_type"] == "CALL_TARGET_FROM" for item in relations))
        self.assertTrue(any(item["code"] == "UNRESOLVED_DEPENDENCY" for item in result["coverage"]["boundaries"]))

    def test_history_is_bounded_and_omission_is_explicit(self):
        history = [{"role": "user", "content": f"Earlier topic {index}"} for index in range(30)]
        self.run_question([{"actions": [read()]}, answer], history=history)
        context = self.contexts[0]
        self.assertEqual(len(context["history"]), 20)
        self.assertEqual(context["history_omitted"], 10)

    def prior_history(self):
        first = self.run_question([{"actions": [read()]}, answer])
        history = [{"role": "user", "content": first["checkpoint"]["question"]},
                   {"role": "assistant", "content": first["answer"], "citations": first["citations"]}]
        self.requests.clear()
        self.contexts.clear()
        self.source_calls.clear()
        return first, history

    def test_followup_uses_prior_citation_location_and_reads_source_again(self):
        first, history = self.prior_history()
        def locate(context):
            self.assertEqual(context["question"], "再解释这个分支")
            self.assertEqual(context["source_context"], [])
            previous = context["history"][-1]
            self.assertFalse(previous["citation_hints_verified_for_current_answer"])
            hint = previous["citation_hints"][0]
            self.assertEqual(set(hint), {"path", "start", "end", "commit", "revision", "hash"})
            self.assertEqual(hint["path"], "main.cbl")
            return {"actions": [read(hint["path"], hint["start"], hint["end"])]}
        second = self.run_question([locate, answer], question="再解释这个分支", history=history)
        self.assertEqual(second["status"], "answered")
        self.assertEqual(second["actions"], 1)
        self.assertEqual(self.source_calls, ["main.cbl"])
        self.assertEqual(self.contexts[1]["observations"][0]["action"]["tool"], "read")
        self.assertEqual(second["citations"][0]["hash"], first["citations"][0]["hash"])

    def test_prior_citation_id_cannot_be_reused_without_current_source_read(self):
        first, history = self.prior_history()
        unverified = {"answer": first["answer"], "citations": [item["id"] for item in first["citations"]],
                      "complete": True, "remaining": []}
        second = self.run_question([unverified, {"actions": [read()]}, answer],
                                   question="再解释这个分支", history=history)
        self.assertEqual(second["status"], "answered")
        self.assertEqual(second["model_calls"], 3)
        self.assertEqual(second["checkpoint"]["protocol_failures"], 1)
        self.assertEqual(self.source_calls, ["main.cbl"])
        self.assertEqual(self.contexts[1]["source_context"], [])

    def test_history_hint_from_another_commit_or_revision_is_rejected_before_model(self):
        _, history = self.prior_history()
        for key, value in (("commit", "c" * 40), ("revision", "d" * 64)):
            bad = deepcopy(history)
            bad[-1]["citations"][0][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(MaterialError, "ONLINE_HISTORY_REVISION_MISMATCH"):
                self.run_question([], question="追问", history=bad)
        self.assertFalse(self.requests)
        self.assertFalse(self.source_calls)

    def test_malformed_history_hint_is_not_a_source_read_instruction(self):
        _, history = self.prior_history()
        for key, value in (("path", "../outside.cbl"), ("hash", "not-a-hash"), ("start", 0), ("end", True)):
            bad = deepcopy(history)
            bad[-1]["citations"][0][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(MaterialError, "ONLINE_INVALID_HISTORY"):
                self.run_question([], question="追问", history=bad)
        self.assertFalse(self.requests)
        self.assertFalse(self.source_calls)

    def test_long_previous_answer_preserves_navigation_hints_and_marks_text_truncation(self):
        _, history = self.prior_history()
        history[-1]["content"] = "此前业务说明。" * 8000
        self.run_question([{"actions": [read()]}, answer], question="追问", history=history)
        previous = self.contexts[0]["history"][-1]
        self.assertTrue(previous["content_truncated"])
        self.assertEqual(previous["citation_hints"][0]["path"], "main.cbl")
        self.assertLess(len(json.dumps(self.contexts[0]["history"], ensure_ascii=False).encode()), 24000)

    def test_history_hint_budget_reports_prior_and_current_omission_counts(self):
        _, history = self.prior_history()
        history[-1]["citations"] *= 64
        history[-1]["citations_omitted"] = 6
        self.run_question([{"actions": [read()]}, answer], question="追问", history=history)
        previous = self.contexts[0]["history"][-1]
        self.assertEqual(previous["citation_hints_omitted"] + len(previous["citation_hints"]), 70)

    def test_actual_skipped_source_reaches_model_even_when_coverage_details_are_compacted(self):
        source = self.root / "mixed-source"
        source.mkdir()
        (source / "main.cbl").write_bytes(self.source_bytes["main.cbl"])
        (source / "unreadable.cbl").write_bytes(b"\x00\x01\x02" * 100)
        database = self.root / "mixed.sqlite"
        report = build_business_index(source, database, source_format="free", verify_content=True,
                                      framework_reference_path="")
        self.assertFalse(report["scope"]["input_coverage_complete"])
        self.assertEqual(len(report["input_skips"]), 1)
        output = self.root / "mixed-material"
        manifest = export_material(database, source, output, repository_id="example/source", commit="b" * 40,
                                   build_report=report)
        shutil.rmtree(source)
        database.unlink()
        for compact in (False, True):
            with self.subTest(compact=compact):
                material = deepcopy(manifest)
                if compact:
                    material["coverage"]["boundaries"].append({"code": "DETAIL" * 4000})
                    material.pop("revision")
                    material = seal_manifest(material)
                    (output / "material.json").write_bytes(canonical_json(material))
                self.query = MaterialQuery(MaterialReader(DirectoryObjectStore(output)))
                self.contexts.clear()
                result = self.run_question([{"actions": [read()]}, answer])
                self.assertEqual(result["status"], "answered")
                for coverage in (self.contexts[0]["coverage"], result["coverage"]):
                    self.assertEqual(coverage["scope"], "indexed_snapshot_only")
                    self.assertEqual(coverage["input_skip_count"], 1)
                    self.assertFalse(coverage["input_coverage_complete"])
                    self.assertEqual(coverage["input_skips"], report["input_skips"])
                    self.assertFalse(coverage["input_skips_truncated"])
                    self.assertEqual(coverage["coverage_report_truncated"], compact)
                    self.assertLessEqual(len(canonical_json(coverage)), 16000)

    def test_skipped_source_details_are_bounded_without_losing_count_or_unknown_completeness(self):
        original = deepcopy(self.manifest)
        original["coverage"].pop("build_scope", None)
        original["coverage"]["input_skips"] = [
            {"relative_path": f"unreadable-{index}.cbl", "reason_code": "SOURCE_ENCODING_INVALID"}
            for index in range(20)]
        original.pop("revision")
        manifest = seal_manifest(original)
        (self.material / "material.json").write_bytes(canonical_json(manifest))
        self.query = MaterialQuery(MaterialReader(DirectoryObjectStore(self.material)))
        result = self.run_question([{"actions": [read()]}, answer])
        coverage = self.contexts[0]["coverage"]
        self.assertEqual(coverage["input_skip_count"], 20)
        self.assertEqual(len(coverage["input_skips"]), 16)
        self.assertTrue(coverage["input_skips_truncated"])
        self.assertIsNone(coverage["input_coverage_complete"])
        self.assertEqual(result["coverage"], coverage)

    def test_per_run_source_budget_preserves_pending_action_for_explicit_continuation(self):
        with mock.patch("online_investigation.MAX_SOURCE_BYTES_PER_RUN", 1):
            result = self.run_question([{"actions": [read()]}])
        self.assertEqual(result["status"], "needs_continuation")
        self.assertEqual(result["error"], "ONLINE_SOURCE_BUDGET_EXCEEDED")
        self.assertEqual(len(result["checkpoint"]["pending_actions"]), 1)

    def test_completed_action_repetition_stops_without_unbounded_model_loop(self):
        action = {"actions": [{"tool": "search", "text": "MAIN-ENTRY"}]}
        result = self.run_question([action] * 4)
        self.assertEqual(result["error"], "ONLINE_NO_PROGRESS")
        self.assertEqual(result["model_calls"], 4)

    def test_call_copy_chain_answer_uses_original_program_and_declaration(self):
        def grounded(context):
            pages = {page["citation"]["path"]: page for page in context["source_context"]}
            self.assertIn("IF INPUT-AMOUNT > 0", pages["main.cbl"]["text"])
            self.assertIn("PIC S9(7)", pages["SHARED-RECORD.cpy"]["text"])
            self.assertIn("DISPLAY 'PROCESS'", pages["worker.cbl"]["text"])
            return answer(context, "INPUT-AMOUNT 是有符号金额字段；金额大于零时调用处理程序请求输出 PROCESS，否则将结果状态置为 REVIEW。")
        result = self.run_question([
            {"actions": [{"tool": "relations", "path": "main.cbl"}, read(),
                         read("SHARED-RECORD.cpy", 1, 2), read("worker.cbl", 1, 6)]}, grounded,
        ])
        self.assertEqual(result["status"], "answered")
        self.assertEqual(len(result["citations"]), 3)
        relation_observation = result["checkpoint"]["observations"][0]["result"]
        self.assertTrue(any(item["relation_type"] == "INCLUDES_COPY" and item["status"] == "confirmed"
                            for item in relation_observation["relations"]))

    def test_material_or_source_access_failure_keeps_read_pending_and_does_not_ask_model_again(self):
        for code in ("SOURCE_FORBIDDEN", "SOURCE_RATE_LIMIT", "SOURCE_NOT_FOUND", "MATERIAL_NOT_FOUND"):
            with self.subTest(code=code):
                self.requests.clear()
                def failed(*args, **kwargs):
                    raise MaterialError(code)
                result = self.run_question([{"actions": [read()]}], source_reader=failed)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"], code)
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(len(result["checkpoint"]["pending_actions"]), 1)
                self.assertFalse(result["checkpoint"]["observations"][-1]["result"]["absence_established"])

    def test_source_mismatch_never_reaches_model_as_evidence(self):
        result = self.run_question([{"actions": [read()]}],
                                   source_reader=lambda path, **kw: self.source_bytes[path].replace(b" > 0", b" > 9"))
        self.assertEqual(result["error"], "MATERIAL_SOURCE_HASH_MISMATCH")
        self.assertFalse(result["evidence"])
        self.assertEqual(len(self.requests), 1)

    def test_received_final_answer_is_revalidated_on_restart_without_new_model_call(self):
        saved = []
        def journal(event):
            saved.append(deepcopy(event))
            response = event["checkpoint"].get("pending_response")
            if event["phase"] == "model_response_received" and response and "answer" in response:
                raise OSError("write unavailable")
        first = self.run_question([{"actions": [read()]}, answer], progress=journal)
        self.assertEqual(first["status"], "failed")
        checkpoint = saved[-1]["checkpoint"]
        before = len(self.requests)
        resumed = self.run_question([], checkpoint=checkpoint)
        self.assertEqual(resumed["status"], "answered")
        self.assertEqual(len(self.requests), before)
        self.assertEqual(resumed["source_requests"], 1)

    def test_evidence_catalog_and_recall_preserve_older_verified_pages(self):
        first = self.run_question([{"actions": [read(start=1, end=6), read(start=7, end=13)]}], max_model_calls=1)
        identifier = first["evidence"][0]["id"]
        resumed = self.run_question([{"actions": [{"tool": "evidence_catalog", "offset": 0, "limit": 1},
                                                   {"tool": "recall", "evidence_ids": [identifier]}]}, answer],
                                    checkpoint=first["checkpoint"])
        self.assertEqual(resumed["status"], "answered")
        catalog = next(item["result"] for item in resumed["checkpoint"]["observations"]
                       if item["action"]["tool"] == "evidence_catalog")
        self.assertTrue(catalog["truncated"])
        self.assertEqual(catalog["next_offset"], 1)
        self.assertEqual(self.contexts[-1]["source_context"][0]["id"], identifier)


if __name__ == "__main__":
    unittest.main()
